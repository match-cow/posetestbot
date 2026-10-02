from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest
from pytransform3d import rotations as pr
from pytransform3d import transformations as pt
from scipy.spatial.transform import Rotation

from posetestbot.calibration import time_offset as time_offset_module
from posetestbot.calibration import attempts as attempt_module
from posetestbot.calibration.attempt_solver import transform_record
from posetestbot.calibration.rotational_timing import (
    STRATEGY as ROTATIONAL_TIMING_STRATEGY,
    estimate_rotational_timing,
)
from posetestbot.calibration.transforms import robot_ee_to_reference
from posetestbot.calibration.time_offset import (
    IMPROVEMENT_EVIDENCE_STRATEGY,
    apply_sensor_time_offset,
    estimate_sensor_time_offset,
    offset_values,
    sign_convention,
)
from posetestbot.pipeline.run_config import (
    create_run_config,
    write_run_config_with_manifest,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH


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
            flange_to_base = robot_ee_to_reference(robot_pose)
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


def _rotational_offset_evidence(
    offsets_ms: tuple[float, float, float],
    *,
    tilt_deg: float = 20.0,
) -> tuple[list[dict], list[dict]]:
    """A mounted camera observes a fixed target through forward/reverse spins."""

    mount = pt.transform_from(
        Rotation.from_euler("X", tilt_deg, degrees=True).as_matrix(),
        np.array([35.0, -20.0, 80.0]),
    )
    target = pt.transform_from(np.eye(3), np.array([100.0, 20.0, 900.0]))
    starts = (0.0, -0.25, 0.25)
    ends = (-0.25, 0.25, 0.0)
    records = []
    observations = []
    rng = np.random.default_rng(82)
    for motion_index, offset in enumerate(offsets_ms):
        motion = f"spin_{motion_index}"
        base_ns = 1_000_000_000 + motion_index * 6_000_000_000

        def pose(local_ms: float) -> dict[str, float]:
            phase = local_ms / 4000.0
            angle = starts[motion_index] + (
                ends[motion_index] - starts[motion_index]
            ) * phase**2 * (3 - 2 * phase)
            return {"X": 100.0, "Y": -90.0, "Z": 430.0, "A": angle, "B": 0.0, "C": 0.0}

        for local_ms in range(0, 4001, 10):
            records.append(
                {
                    "pose_index": len(records),
                    "timestamp_ns": base_ns + local_ms * 1_000_000,
                    "motion": motion,
                    "pose": pose(local_ms),
                }
            )
        for frame_index, local_ms in enumerate(range(350, 3651, 40)):
            transform = (
                pt.invert_transform(mount)
                @ pt.invert_transform(robot_ee_to_reference(pose(local_ms + offset)))
                @ target
            )
            # Retain repeatable PnP orientation noise, rather than an exact fit.
            transform[:3, :3] = (
                Rotation.from_rotvec(rng.normal(0, np.radians(0.003), 3)).as_matrix()
                @ transform[:3, :3]
            )
            observations.append(
                {
                    "observation_id": f"sensor:IPPE:{motion}-{frame_index}.png",
                    "source_frame_id": f"{motion}-{frame_index}.png",
                    "frame_id": f"{motion}-{frame_index}.png",
                    "image_timestamp_ns": base_ns + local_ms * 1_000_000,
                    "motion": motion,
                    "robot_ee_pose": pose(local_ms),
                    "target_to_camera": transform_record(
                        transform, from_frame="aruco_grid", to_frame="camera"
                    ),
                    "mean_reprojection_error_px": 0.1,
                    "image_coverage_cell": frame_index % 9,
                }
            )
    return observations, records


@pytest.mark.parametrize("offset_ms", [-20.0, 20.0, 0.0])
def test_angular_timing_recovers_latency_without_false_nonzero_at_zero(
    offset_ms: float,
) -> None:
    observations, records = _rotational_offset_evidence((offset_ms,) * 3)
    result = estimate_rotational_timing(
        observations,
        robot_records=records,
        offsets_ms=offset_values(-80, 80, 5),
        maximum_uncertainty_ms=20,
    )

    assert result["estimated_robot_pose_time_offset_ms"] == pytest.approx(
        offset_ms, abs=1.0
    )
    low, high = result["confidence_interval_ms"]
    assert low < offset_ms < high
    assert high - low >= 10.0  # Robot cadence limits the claimed precision.
    assert result["motion_count"] == 3
    assert result["view_count"] > 200
    assert result["status"] == ("inconclusive" if offset_ms == 0 else "identified")
    assert result["candidate_robot_pose_time_offset_ms"] == offset_ms


def test_angular_timing_rejects_motion_dependent_latency_and_tilt_axis() -> None:
    observations, records = _rotational_offset_evidence((-60.0, 20.0, 70.0))
    inconsistent = estimate_rotational_timing(
        observations,
        robot_records=records,
        offsets_ms=offset_values(-80, 80, 5),
        maximum_uncertainty_ms=20,
    )
    assert inconsistent["status"] == "inconclusive"
    assert (
        inconsistent["confidence_interval_ms"][1]
        - inconsistent["confidence_interval_ms"][0]
        > 20
    )

    observations, records = _rotational_offset_evidence((20.0,) * 3, tilt_deg=70)
    tilted = estimate_rotational_timing(
        observations,
        robot_records=records,
        offsets_ms=offset_values(-80, 80, 5),
        maximum_uncertainty_ms=20,
    )
    assert tilted["status"] == "unavailable"
    assert {motion["reason"] for motion in tilted["motion_diagnostics"]} == {
        "rotation_not_near_optical_axis"
    }


def test_angular_timing_does_not_interpolate_across_missing_pose_packets() -> None:
    observations, records = _rotational_offset_evidence((20.0,) * 3)
    sparse = [record for index, record in enumerate(records) if index % 10 == 0]
    result = estimate_rotational_timing(
        observations,
        robot_records=sparse,
        offsets_ms=offset_values(-80, 80, 5),
        maximum_uncertainty_ms=20,
    )
    assert result["status"] == "unavailable"


def test_auto_offset_applies_angular_delay_even_below_translation_materiality(
    tmp_path: Path,
) -> None:
    observations, records = _rotational_offset_evidence((20.0,) * 3)
    others, other_records = _synthetic_offset_evidence(
        mode="eye_in_hand", planted_offset_ms=20
    )
    for item in others:
        item["image_timestamp_ns"] += 30_000_000_000
    for item in other_records:
        item["timestamp_ns"] += 30_000_000_000
        item["pose_index"] += len(records)
    # Both motion sets must describe the same physical mounting and target.
    mount = pt.transform_from(
        Rotation.from_euler("X", 20, degrees=True).as_matrix(),
        np.array([35.0, -20.0, 80.0]),
    )
    target = pt.transform_from(np.eye(3), np.array([100.0, 20.0, 900.0]))
    poses = {item["timestamp_ns"]: item["pose"] for item in other_records}
    for item in others:
        transform = (
            pt.invert_transform(mount)
            @ pt.invert_transform(
                robot_ee_to_reference(poses[item["image_timestamp_ns"] + 20_000_000])
            )
            @ target
        )
        item["target_to_camera"] = transform_record(
            transform, from_frame="aruco_grid", to_frame="camera"
        )
    source = observations + others
    result, adjusted = estimate_sensor_time_offset(
        source,
        sensor_key="realsense_d435:test",
        robot_records=records + other_records,
        mode="eye_in_hand",
        offsets_ms=offset_values(-40, 40, 10),
        methods=("shah",),
        min_absolute_improvement_mm=1000,
    )

    assert result["status"] == "applied"
    assert result["selected_robot_pose_time_offset_ms"] == 20.0
    assert result["selected_sync_delta_ms"] == -20.0
    assert result["rotational_timing"]["status"] == "identified"
    assert result["improvement_evidence_strategy"] == ROTATIONAL_TIMING_STRATEGY
    assert (
        result["cross_validation"]["offset_selection_uses_validation_metrics"] is False
    )
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["cross_validated_translation_improvement"]["status"] == "not_needed"
    assert checks["cross_validated_rotation_guard"]["status"] == "ok"
    assert checks["rotational_timing_identifiability"]["status"] == "ok"
    assert {item["robot_pose_time_offset_ms"] for item in adjusted} == {20.0}
    # Timing estimation must not mutate source poses or raw timestamps.
    assert all("robot_pose_time_offset_ms" not in item for item in source)

    # Promotion must reproduce the delay from persisted, rematched observations,
    # and the request's hash-bound raw robot stream, rather than trust the number.
    config = create_run_config(
        capture_intent="calibration", bop_annotation_mode="none", run_root=tmp_path
    )
    write_run_config_with_manifest(tmp_path, config)
    raw = {}
    for index, record in enumerate(records + other_records):
        raw[str(index)] = {
            **record,
            "host_wall_timestamp_ns": record["timestamp_ns"],
            "source_packet": {
                "schema_version": "robot_pose.v1",
                "packet_kind": "pose",
                "run_id": str(config.run_id),
                "from_frame": "robot_flange",
                "to_frame": "template_base",
                "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
            },
        }
    (tmp_path / "raw_robot_ee_poses.json").write_text(json.dumps(raw))
    sensor = {
        "sensor_key": "realsense_d435:test",
        "sensor_type": "realsense_d435",
        "device_id": "test",
        "robot_pose_path": "raw_robot_ee_poses.json",
    }
    request = {
        "attempt_id": "a" * 32,
        "mode": "eye_in_hand",
        "sensors": [sensor],
        "timestamp_policy": attempt_module._attempt_timestamp_policy([sensor]),
        "robot_pose_reference": attempt_module._attempt_robot_pose_reference(
            tmp_path, [sensor]
        ),
    }
    attempt_root = attempt_module.calibration_attempt_root(
        tmp_path, request["attempt_id"]
    )
    attempt_root.mkdir(parents=True)
    (attempt_root / "observations.json").write_text(
        json.dumps(
            {
                "observations": [
                    {
                        **item,
                        "sensor_type": "realsense_d435",
                        "device_id": "test",
                        "pnp_method": "IPPE",
                        "frame_id": f"rematched-{index}.png",
                    }
                    for index, item in enumerate(adjusted)
                ]
            }
        )
    )
    attempt = {"run_root": str(tmp_path), "request": request}
    search = time_offset_module.search_configuration()
    grid = offset_values(-40, 40, 10)
    attempt_module._validate_promotion_rotational_timing(
        attempt,
        result,
        sensor_key=sensor["sensor_key"],
        recorded_search=search,
        search_grid=grid,
        check_by_name=checks,
    )
    forged = copy.deepcopy(result)
    forged["rotational_timing"]["estimated_robot_pose_time_offset_ms"] += 1.0
    with pytest.raises(ValueError, match="could not be reproduced"):
        attempt_module._validate_promotion_rotational_timing(
            attempt,
            forged,
            sensor_key=sensor["sensor_key"],
            recorded_search=search,
            search_grid=grid,
            check_by_name=checks,
        )


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


def test_auto_offset_boundary_optimum_keeps_recorded_timing_with_warning() -> None:
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

    assert result["status"] == "kept_zero"
    assert result["evidence_strength"] == "degraded"
    assert result["boundary_hit"] is True
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    boundary_check = next(
        item
        for item in result["checks"]
        if item["name"] == "search_optimum_not_at_boundary"
    )
    assert boundary_check["status"] == "warning"
    assert boundary_check["original_status"] == "error"
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


def test_auto_offset_flat_curve_keeps_zero_with_warning() -> None:
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

    assert result["status"] == "kept_zero"
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    assert (
        next(
            item
            for item in result["checks"]
            if item["name"] == "zero_offset_identifiability"
        )["status"]
        == "warning"
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


def test_auto_offset_motion_consistency_keeps_zero_with_warning(
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

    assert result["status"] == "kept_zero"
    assert result["selected_robot_pose_time_offset_ms"] == 0.0
    assert (
        next(
            item
            for item in result["checks"]
            if item["name"] == "leave_one_motion_out_timing_consistency"
        )["status"]
        == "warning"
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
