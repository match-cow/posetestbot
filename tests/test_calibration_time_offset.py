from __future__ import annotations

import math

import numpy as np
import pytest
from pytransform3d import rotations as pr
from pytransform3d import transformations as pt

from posetestbot.calibration import time_offset as time_offset_module
from posetestbot.calibration.attempt_solver import transform_record
from posetestbot.calibration.candidates import _robot_ee_to_reference
from posetestbot.calibration.time_offset import (
    IMPROVEMENT_EVIDENCE_STRATEGY,
    apply_sensor_time_offset,
    estimate_sensor_time_offset,
    offset_values,
    sign_convention,
)


def _robot_pose(motion_index: int, local_ms: float) -> dict[str, float]:
    return {
        "X": 80.0 + 32.0 * motion_index + 0.10 * local_ms,
        "Y": -90.0 + 18.0 * (motion_index % 4) - 0.055 * local_ms,
        "Z": 430.0 + 13.0 * (motion_index % 5) + 0.035 * local_ms,
        "A": -0.28 + 0.055 * motion_index + 0.00045 * local_ms,
        "B": 0.22 - 0.037 * (motion_index % 6) - 0.00030 * local_ms,
        "C": -0.31 + 0.061 * motion_index + 0.00055 * local_ms,
    }


def _synthetic_offset_evidence(
    *,
    mode: str,
    planted_offset_ms: int,
    motion_count: int = 12,
    stationary_within_motion: bool = False,
) -> tuple[list[dict], list[dict]]:
    camera_to_flange = pt.transform_from(
        pr.matrix_from_compact_axis_angle(np.array([0.08, -0.04, 0.03])),
        np.array([35.0, -20.0, 80.0]),
    )
    target_to_base = pt.transform_from(
        pr.matrix_from_compact_axis_angle(np.array([0.03, 0.02, -0.01])),
        np.array([100.0, 20.0, 400.0]),
    )
    camera_to_base = pt.transform_from(
        pr.matrix_from_compact_axis_angle(np.array([-0.1, 0.03, 0.06])),
        np.array([400.0, -100.0, 800.0]),
    )
    target_to_flange = pt.transform_from(
        pr.matrix_from_compact_axis_angle(np.array([0.02, -0.07, 0.04])),
        np.array([20.0, 10.0, 120.0]),
    )
    records: list[dict] = []
    observations: list[dict] = []
    pose_index = 0
    for motion_index in range(motion_count):
        motion = f"motion_{motion_index:02d}"
        base_ns = 1_000_000_000 + motion_index * 1_000_000_000
        poses_by_local_ms = {}
        for local_ms in range(0, 601, 10):
            pose = _robot_pose(
                motion_index,
                300.0 if stationary_within_motion else float(local_ms),
            )
            poses_by_local_ms[local_ms] = pose
            records.append(
                {
                    "pose_index": pose_index,
                    "timestamp_ns": base_ns + local_ms * 1_000_000,
                    "motion": motion,
                    "pose": pose,
                }
            )
            pose_index += 1
        for frame_index, local_ms in enumerate(range(220, 341, 20)):
            physical_local_ms = local_ms + planted_offset_ms
            robot_pose = poses_by_local_ms[physical_local_ms]
            flange_to_base = _robot_ee_to_reference(robot_pose)
            if mode == "eye_in_hand":
                target_to_camera = (
                    pt.invert_transform(camera_to_flange)
                    @ pt.invert_transform(flange_to_base)
                    @ target_to_base
                )
            else:
                target_to_camera = (
                    pt.invert_transform(camera_to_base)
                    @ flange_to_base
                    @ target_to_flange
                )
            observations.append(
                {
                    "observation_id": (
                        f"sensor:IPPE:{motion_index:02d}-{frame_index:02d}.png"
                    ),
                    "frame_id": f"{motion_index:02d}-{frame_index:02d}.png",
                    "source_frame_id": (
                        f"source-{motion_index:02d}-{frame_index:02d}.png"
                    ),
                    "image_timestamp_ns": base_ns + local_ms * 1_000_000,
                    "motion": motion,
                    "robot_ee_pose": poses_by_local_ms[local_ms],
                    "target_to_camera": transform_record(
                        target_to_camera,
                        from_frame="aruco_grid",
                        to_frame="camera",
                    ),
                    "mean_reprojection_error_px": 0.1,
                    "image_coverage_cell": motion_index % 9,
                }
            )
    return observations, records


@pytest.mark.parametrize("mode", ["eye_in_hand", "eye_to_hand"])
@pytest.mark.parametrize("planted_offset_ms", [-20, 20])
def test_auto_offset_recovers_planted_latency_with_exact_sign(
    mode: str,
    planted_offset_ms: int,
) -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode=mode,
        planted_offset_ms=planted_offset_ms,
    )

    result, adjusted = estimate_sensor_time_offset(
        observations,
        sensor_key="realsense_d435:test",
        robot_records=robot_records,
        mode=mode,
        offsets_ms=[float(value) for value in range(-40, 41, 10)],
        methods=("shah",),
        max_search_motions=12,
    )

    assert result["status"] == "applied"
    assert result["candidate_robot_pose_time_offset_ms"] == planted_offset_ms
    assert result["selected_robot_pose_time_offset_ms"] == planted_offset_ms
    assert result["selected_sync_delta_ms"] == -planted_offset_ms
    assert result["boundary_hit"] is False
    assert result["split"]["motion_count"] == 12
    assert set(result["split"]["frame_ids"]) == {"fold_0", "fold_1", "fold_2"}
    assert result["cross_validation"]["improvement"]["relative_translation"] > 0.05
    assert result["improvement_evidence_strategy"] == IMPROVEMENT_EVIDENCE_STRATEGY
    assert result["motion_consistency"]["status"] == "ok"
    method_evidence = result["motion_consistency"]["methods"]["shah"]
    assert method_evidence["positive_motion_count"] == 12
    assert method_evidence["candidate_search_adjusted_positive_sign_p_value"] <= 0.05
    assert len(adjusted) == len(observations)
    assert {item["robot_pose_time_offset_ms"] for item in adjusted} == {
        float(planted_offset_ms)
    }
    assert {item["sync_delta_ms"] for item in adjusted} == {float(-planted_offset_ms)}


def test_auto_offset_applies_large_supported_offset_with_warning() -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_in_hand",
        planted_offset_ms=200,
    )

    result, adjusted = estimate_sensor_time_offset(
        observations,
        sensor_key="realsense_d435:test",
        robot_records=robot_records,
        mode="eye_in_hand",
        offsets_ms=[float(value) for value in range(-300, 301, 50)],
        methods=("shah",),
        max_search_motions=12,
        warning_abs_offset_ms=150.0,
    )

    assert result["status"] == "applied"
    assert result["selected_robot_pose_time_offset_ms"] == 200.0
    magnitude = next(
        item
        for item in result["checks"]
        if item["name"] == "candidate_offset_magnitude_warning"
    )
    assert magnitude["status"] == "warning"
    assert len(adjusted) == len(observations)


def test_auto_offset_boundary_optimum_fails_closed() -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_in_hand",
        planted_offset_ms=40,
    )

    result, adjusted = estimate_sensor_time_offset(
        observations,
        sensor_key="realsense_d435:test",
        robot_records=robot_records,
        mode="eye_in_hand",
        offsets_ms=[float(value) for value in range(-40, 41, 10)],
        methods=("shah",),
        max_search_motions=12,
    )

    assert result["status"] == "failed"
    assert result["evidence_strength"] == "failed"
    assert result["boundary_hit"] is True
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    boundary_check = next(
        item
        for item in result["checks"]
        if item["name"] == "search_optimum_not_at_boundary"
    )
    assert boundary_check["status"] == "error"
    assert all(item["robot_pose_time_offset_ms"] == 0.0 for item in adjusted)


def test_auto_offset_requires_three_motion_disjoint_folds() -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_in_hand",
        planted_offset_ms=20,
        motion_count=11,
    )

    with pytest.raises(ValueError, match="at least 12 motion groups"):
        estimate_sensor_time_offset(
            observations,
            sensor_key="realsense_d435:test",
            robot_records=robot_records,
            mode="eye_in_hand",
            offsets_ms=[-40.0, -20.0, 0.0, 20.0, 40.0],
            methods=("shah",),
        )


def test_auto_offset_flat_curve_fails_closed() -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_in_hand",
        planted_offset_ms=20,
        stationary_within_motion=True,
    )

    result, adjusted = estimate_sensor_time_offset(
        observations,
        sensor_key="realsense_d435:test",
        robot_records=robot_records,
        mode="eye_in_hand",
        offsets_ms=[float(value) for value in range(-40, 41, 10)],
        methods=("shah",),
        max_search_motions=12,
    )

    assert result["status"] == "failed"
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    assert (
        next(
            item
            for item in result["checks"]
            if item["name"] == "zero_offset_identifiability"
        )["status"]
        == "error"
    )
    assert all(item["robot_pose_time_offset_ms"] == 0.0 for item in adjusted)


def test_auto_offset_flat_curve_keeps_recorded_timing_with_warning() -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_to_hand",
        planted_offset_ms=20,
        stationary_within_motion=True,
    )

    result, adjusted = estimate_sensor_time_offset(
        observations,
        sensor_key="realsense_d435:test",
        robot_records=robot_records,
        mode="eye_to_hand",
        offsets_ms=[float(value) for value in range(-40, 41, 10)],
        methods=("shah",),
        max_search_motions=12,
        failure_policy=time_offset_module.FAILURE_POLICY_WARN_KEEP_ZERO,
    )

    assert result["status"] == "kept_zero"
    assert result["decision"] == "recorded_timing_kept"
    assert result["evidence_strength"] == "degraded"
    assert result["warning_fallback_used"] is True
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    assert any(
        item.get("status") == "warning" and item.get("original_status") == "error"
        for item in result["checks"]
    )
    assert not any(item.get("status") == "error" for item in result["checks"])
    assert all(item["robot_pose_time_offset_ms"] == 0.0 for item in adjusted)


def test_auto_offset_motion_consistency_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_in_hand",
        planted_offset_ms=20,
    )
    monkeypatch.setattr(
        time_offset_module,
        "_leave_one_motion_out_consistency",
        lambda *_args, **_kwargs: {
            "status": "error",
            "motion_count": 12,
            "methods": {"shah": {"status": "error"}},
        },
    )

    result, adjusted = estimate_sensor_time_offset(
        observations,
        sensor_key="realsense_d435:test",
        robot_records=robot_records,
        mode="eye_in_hand",
        offsets_ms=[float(value) for value in range(-40, 41, 10)],
        methods=("shah",),
        max_search_motions=12,
    )

    assert result["status"] == "failed"
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    assert (
        next(
            item
            for item in result["checks"]
            if item["name"] == "leave_one_motion_out_timing_consistency"
        )["status"]
        == "error"
    )
    assert all(item["robot_pose_time_offset_ms"] == 0.0 for item in adjusted)


def test_full_search_correction_requires_16_of_17_positive_motions() -> None:
    sixteen_positive = time_offset_module._positive_sign_p_value(16, 17)
    fifteen_positive = time_offset_module._positive_sign_p_value(15, 17)

    assert sixteen_positive * 120 < 0.05
    assert fifteen_positive * 120 > 0.05


def test_time_offset_public_contract_is_explicit_and_deterministic() -> None:
    assert (
        time_offset_module.search_configuration()["time_offset_failure_policy"]
        == time_offset_module.FAILURE_POLICY_WARN_KEEP_ZERO
    )
    assert offset_values(-20.0, 20.0, 5.0) == [
        -20.0,
        -15.0,
        -10.0,
        -5.0,
        0.0,
        5.0,
        10.0,
        15.0,
        20.0,
    ]
    assert sign_convention()["conversion"] == (
        "sync_delta_ms = -robot_pose_time_offset_ms"
    )

    observations, robot_records = _synthetic_offset_evidence(
        mode="eye_in_hand",
        planted_offset_ms=20,
    )
    adjusted = apply_sensor_time_offset(
        observations,
        robot_records=robot_records,
        robot_pose_time_offset_ms=20.0,
    )
    assert len(adjusted) == len(observations)
    assert all(
        math.isclose(
            item["timestamp_alignment"]["robot_pose_query_timestamp_ns"],
            item["image_timestamp_ns"] + 20_000_000,
        )
        for item in adjusted
    )
