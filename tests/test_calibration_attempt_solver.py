from __future__ import annotations

import cv2

import hashlib

import json

import numpy as np

import pytest

from pathlib import Path

from pytransform3d import rotations as pr

from pytransform3d import transformations as pt

from types import SimpleNamespace

from posetestbot.calibration.attempt_solver import (
    evaluate_extrinsic_candidate,
    rank_candidates,
    solve_planar_pnp_candidates,
)
from posetestbot.calibration.transforms import (
    transform_from_record,
    transform_record,
    transform_residual,
)

from posetestbot.calibration import attempts as attempt_module

from posetestbot.calibration.transforms import robot_ee_to_reference

from posetestbot.calibration.intrinsics import (
    factory_intrinsic_profile,
    write_intrinsic_profile_collection,
)

from posetestbot.calibration.targets import DEFAULT_TARGET_SPEC, opencv_grid_board

from posetestbot.io.artifacts import (
    INTRINSIC_CALIBRATION_PROFILES,
    INTRINSIC_COMPARISON,
)

from posetestbot.sensors.contracts import CameraIntrinsics

from posetestbot.sensors.frame_writer import write_camera_sidecars


@pytest.mark.parametrize(
    ("actual_sync_delta_ms", "should_fail"),
    [(0.0, False), (100.0, True), ("nan", True)],
)
def test_prepare_attempt_normalizes_paths_and_requires_zero_sync_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actual_sync_delta_ms: float | str,
    should_fail: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    run_root = Path("run")
    attempt_root = run_root / "processed" / "calibration" / ("a" * 32)
    attempt_root.mkdir(parents=True)
    sensor_folder = run_root / "realsense_1"
    sensor_folder.mkdir(parents=True)
    (sensor_folder / "frame_metadata.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "1",
                "frame_index": 0,
                "frame_id": "1000.png",
                "rgb_path": "rgb/1000.png",
                "depth_path": "depth/1000.png",
                "sensor_timestamp_ns": 10_000_000_000,
                "host_received_timestamp_ns": 10_000_000_000,
                "host_wall_timestamp_ns": 10_000_000_000,
                "color_timestamp_domain": "global_time",
            }
        )
        + "\n"
    )
    (run_root / "raw_robot_ee_poses.json").write_text(
        json.dumps(
            {
                "0": {
                    "host_wall_timestamp_ns": 10_000_000_000,
                    "motion": "pose_0",
                    "pose": {
                        "X": 0.0,
                        "Y": 0.0,
                        "Z": 500.0,
                        "A": 0.0,
                        "B": 0.0,
                        "C": 0.0,
                    },
                }
            }
        )
    )
    verified_robot_poses = {
        "raw_robot_ee_poses.json": json.loads(
            (run_root / "raw_robot_ee_poses.json").read_text()
        )
    }
    output_folder = (
        attempt_root / "processed" / "preparation_synchronized" / "realsense_1"
    )
    report_path = output_folder / "sync_report.json"

    def fake_synchronize_run(*_args, **kwargs):
        assert kwargs["output_root"] == (
            attempt_root / "processed" / "preparation_synchronized"
        )
        assert kwargs["sync_delta"] == 0.0
        assert kwargs["timestamp_source"] == "sensor"
        assert kwargs["robot_timestamp_source"] == "host_wall"
        assert kwargs["max_nearest_pose_delta_ms"] == 150.0
        assert (
            kwargs["raw_robot_poses"]
            is (verified_robot_poses["raw_robot_ee_poses.json"])
        )
        return [
            SimpleNamespace(
                sensor_folder=sensor_folder,
                output_folder=output_folder,
                report_path=report_path,
            )
        ]

    monkeypatch.setattr(
        attempt_module,
        "synchronize_run",
        fake_synchronize_run,
    )

    def fake_quality(
        root: Path,
        *,
        report_paths: list[Path],
        max_nearest_pose_delta_ms: float,
        require_timestamp_source: dict[str, str],
        require_robot_timestamp_source: dict[str, str],
    ) -> dict:
        assert root == run_root
        assert report_paths == [report_path.resolve()]
        assert max_nearest_pose_delta_ms == 150.0
        assert require_timestamp_source == {"realsense_1": "sensor"}
        assert require_robot_timestamp_source == {"realsense_1": "host_wall"}
        return {
            "overall_status": "ok",
            "sensors": [
                {
                    "sensor_name": "realsense_1",
                    "sync_delta_ms": actual_sync_delta_ms,
                }
            ],
            "checks": [
                {
                    "name": "sync_timestamp_source:realsense_1",
                    "status": "ok",
                },
                {
                    "name": "sync_robot_timestamp_source:realsense_1",
                    "status": "ok",
                },
                {
                    "name": "sync_nearest_pose_delta:realsense_1",
                    "status": "ok",
                },
            ],
        }

    monkeypatch.setattr(attempt_module, "build_sync_quality_report", fake_quality)
    monkeypatch.setattr(
        attempt_module,
        "_intrinsics_for_sensors",
        lambda *_args: ([], {}),
    )

    arguments = (
        run_root,
        attempt_root,
        {
            "sensors": [
                {
                    "sensor_key": "realsense_d435:1",
                    "folder": "realsense_1",
                    "sensor_type": "realsense_d435",
                    "robot_pose_path": "raw_robot_ee_poses.json",
                }
            ]
        },
        verified_robot_poses,
    )
    if should_fail:
        with pytest.raises(ValueError, match="strict eye-in-hand policy"):
            attempt_module._prepare_attempt_data(*arguments)
        return

    synchronized, intrinsics = attempt_module._prepare_attempt_data(*arguments)

    assert synchronized == {"realsense_d435:1": output_folder.resolve()}
    assert intrinsics == {}


def test_authoritative_sync_uses_selected_offset_without_replacing_preparation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    attempt_id = "a" * 32
    attempt_root = run_root / "processed" / "calibration" / attempt_id
    sensor_folder = run_root / "realsense_1"
    preparation_folder = (
        attempt_root / "processed" / "preparation_synchronized" / "realsense_1"
    )
    preparation_folder.mkdir(parents=True)
    sensor_folder.mkdir(parents=True)
    preparation_detection = preparation_folder / "aruco_detections.json"
    preparation_detection.write_text('{"retained": true}')

    request_value = {
        "attempt_id": attempt_id,
        "sensor_keys": ["realsense_d435:1"],
        "sensors": [
            {
                "sensor_key": "realsense_d435:1",
                "sensor_name": "realsense_1",
                "sensor_type": "realsense_d435",
                "device_id": "1",
                "folder": "realsense_1",
            }
        ],
    }
    time_offset_search = {
        "policy": "auto_offset",
        "sign_convention": attempt_module.time_offset_sign_convention(),
        "search": {"max_nearest_pose_delta_ms": 20.0},
        "sensors": [
            {
                "sensor_key": "realsense_d435:1",
                "status": "applied",
                "selected_robot_pose_time_offset_ms": 75.0,
                "selected_sync_delta_ms": -75.0,
            }
        ],
    }
    observations = {
        "realsense_d435:1": {
            "IPPE": [
                {
                    "observation_id": "old",
                    "frame_id": "000004.png",
                    "source_frame_id": "source-004.png",
                    "image_timestamp_ns": 1_000_000_000,
                    "motion": "old_motion",
                    "robot_ee_pose": {"X": 0.0},
                }
            ]
        }
    }
    timestamp_policy = {
        "schema_version": "calibration_timestamp_policy.v1",
        "per_sensor": {
            "realsense_d435:1": {
                "frame_timestamp_source": "sensor",
                "robot_timestamp_source": "host_wall",
            }
        },
    }
    verified_robot_poses = {"raw_robot_ee_poses.json": {"0": {"pose": {}}}}
    monkeypatch.setattr(
        attempt_module,
        "_calibration_timestamp_preflight",
        lambda *_args: timestamp_policy,
    )

    final_folder = attempt_root / "processed" / "synchronized" / "realsense_1"
    report_path = final_folder / "sync_report.json"

    def fake_synchronize_run(*_args, **kwargs):
        assert kwargs["output_root"] == attempt_root / "processed" / "synchronized"
        assert kwargs["sync_delta"] == -75.0
        assert kwargs["copy_files"] is False
        assert kwargs["timestamp_source"] == "sensor"
        assert kwargs["robot_timestamp_source"] == "host_wall"
        assert kwargs["max_nearest_pose_delta_ms"] == 20.0
        assert (
            kwargs["raw_robot_poses"]
            is (verified_robot_poses["raw_robot_ee_poses.json"])
        )
        final_folder.mkdir(parents=True)
        (final_folder / "match_robot_ee_poses.json").write_text(
            json.dumps(
                {
                    "000000.png": {
                        "source_frame_id": "source-004.png",
                        "image_timestamp_ns": 1_000_000_000,
                        "delayed_timestamp_ns": 1_075_000_000,
                        "motion": "motion_4",
                        "robot_ee_pose": {
                            "X": 1.0,
                            "Y": 2.0,
                            "Z": 3.0,
                            "A": 0.1,
                            "B": 0.2,
                            "C": 0.3,
                        },
                        "matched_robot_pose_index": 44,
                        "robot_timestamp_ns": 1_074_000_000,
                        "nearest_robot_delta_ns": -1_000_000,
                    }
                }
            )
        )
        return [
            SimpleNamespace(
                sensor_folder=sensor_folder,
                output_folder=final_folder,
                report_path=report_path,
            )
        ]

    monkeypatch.setattr(attempt_module, "synchronize_run", fake_synchronize_run)
    monkeypatch.setattr(
        attempt_module,
        "build_sync_quality_report",
        lambda *_args, **_kwargs: {
            "overall_status": "ok",
            "sensors": [{"sensor_name": "realsense_1", "sync_delta_ms": -75.0}],
            "checks": [
                {
                    "name": "sync_nearest_pose_delta:realsense_1",
                    "status": "ok",
                }
            ],
        },
    )

    synchronized, remapped = attempt_module._materialize_authoritative_synchronization(
        run_root,
        attempt_root,
        request_value,
        time_offset_search,
        observations,
        verified_robot_poses,
    )

    assert synchronized == {"realsense_d435:1": final_folder.resolve()}
    assert json.loads(preparation_detection.read_text()) == {"retained": True}
    item = remapped["realsense_d435:1"]["IPPE"][0]
    assert item["frame_id"] == "000000.png"
    assert item["source_frame_id"] == "source-004.png"
    assert item["robot_pose_time_offset_ms"] == 75.0
    assert item["sync_delta_ms"] == -75.0
    assert item["timestamp_alignment"]["source"] == (
        f"processed/calibration/{attempt_id}/time_offset_search.json"
    )
    quality = json.loads((attempt_root / "sync_quality_report.json").read_text())
    policy = quality["calibration_attempt_policy"]
    assert policy["per_sensor"] == timestamp_policy["per_sensor"]
    assert policy["per_sensor_offsets"]["realsense_d435:1"] == {
        "robot_pose_time_offset_ms": 75.0,
        "sync_delta_ms": -75.0,
        "status": "applied",
    }


def test_realsense_calibration_timestamp_preflight_requires_global_time(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    sensor_folder = run_root / "realsense_1"
    sensor_folder.mkdir(parents=True)
    (sensor_folder / "frame_metadata.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "1",
                "frame_index": 0,
                "frame_id": "1000.png",
                "rgb_path": "rgb/1000.png",
                "depth_path": "depth/1000.png",
                "sensor_timestamp_ns": 10_000_000_000,
                "host_received_timestamp_ns": 10_000_000_000,
                "host_wall_timestamp_ns": 10_000_000_000,
                "color_timestamp_domain": "system_time",
            }
        )
        + "\n"
    )
    (run_root / "raw_robot_ee_poses.json").write_text(
        json.dumps(
            {
                "0": {
                    "host_wall_timestamp_ns": 10_000_000_000,
                    "motion": "pose_0",
                    "pose": {},
                }
            }
        )
    )
    sensors = [
        {
            "sensor_key": "realsense_d435:1",
            "sensor_type": "realsense_d435",
            "folder": "realsense_1",
            "robot_pose_path": "raw_robot_ee_poses.json",
        }
    ]

    with pytest.raises(ValueError, match="must all use global_time"):
        attempt_module._calibration_timestamp_preflight(run_root, sensors)


def test_prepare_attempt_rejects_mutated_per_sensor_timestamp_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    attempt_root = run_root / "processed" / "calibration" / ("a" * 32)
    sensor_folder = run_root / "realsense_1"
    attempt_root.mkdir(parents=True)
    sensor_folder.mkdir(parents=True)
    (sensor_folder / "frame_metadata.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "1",
                "frame_index": 0,
                "frame_id": "1000.png",
                "rgb_path": "rgb/1000.png",
                "depth_path": "depth/1000.png",
                "sensor_timestamp_ns": 10_000_000_000,
                "host_received_timestamp_ns": 10_000_000_000,
                "host_wall_timestamp_ns": 10_000_000_000,
                "color_timestamp_domain": "global_time",
            }
        )
        + "\n"
    )
    (run_root / "raw_robot_ee_poses.json").write_text(
        json.dumps(
            {
                "0": {
                    "host_wall_timestamp_ns": 10_000_000_000,
                    "motion": "pose_0",
                    "pose": {},
                }
            }
        )
    )
    sensors = [
        {
            "sensor_key": "realsense_d435:1",
            "sensor_type": "realsense_d435",
            "folder": "realsense_1",
            "robot_pose_path": "raw_robot_ee_poses.json",
        }
    ]
    recorded_policy = attempt_module._calibration_timestamp_preflight(run_root, sensors)
    recorded_policy["per_sensor"]["realsense_d435:1"]["frame_timestamp_source"] = (
        "host_received"
    )
    monkeypatch.setattr(
        attempt_module,
        "synchronize_run",
        lambda *_args, **_kwargs: pytest.fail(
            "synchronization must not run after timestamp-policy mutation"
        ),
    )

    with pytest.raises(
        ValueError,
        match="realsense_d435:1: frame_timestamp_source",
    ):
        attempt_module._prepare_attempt_data(
            run_root,
            attempt_root,
            {"sensors": sensors, "timestamp_policy": recorded_policy},
            {
                "raw_robot_ee_poses.json": json.loads(
                    (run_root / "raw_robot_ee_poses.json").read_text()
                )
            },
        )


def _intrinsic_sensor_fixture(folder: Path) -> dict:
    folder.mkdir(parents=True)
    write_camera_sidecars(
        folder,
        CameraIntrinsics(
            cam_k=(600.0, 0.0, 320.0, 0.0, 601.0, 240.0, 0.0, 0.0, 1.0),
            width=640,
            height=480,
            distortion=(0.01, -0.02, 0.001, -0.002, 0.003),
            depth_scale_to_mm=1.0,
            distortion_model="brown_conrady",
            projection_source="test_factory_color",
        ),
        include_distortion_in_cam_k=True,
    )
    (folder / "frame_metadata.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": folder.name.removeprefix("realsense_"),
                "frame_index": 0,
                "frame_id": "000000.png",
                "rgb_path": "rgb/000000.png",
                "depth_path": "depth/000000.png",
                "sensor_timestamp_ns": 1,
                "host_received_timestamp_ns": 1,
                "host_wall_timestamp_ns": 1,
            }
        )
        + "\n"
    )
    return factory_intrinsic_profile(folder)


def _unsupported_intrinsic_profile(profile: dict, *, profile_id: str) -> dict:
    return {
        **profile,
        "profile_id": profile_id,
        "native": {
            **profile["native"],
            "distortion_model": "inverse_brown_conrady",
        },
        "rectified": None,
        "source": {
            **profile["source"],
            "opencv_projection_compatible": False,
            "rectification_available": False,
            "rectification_unavailable_reason": (
                "sdk_distortion_model_is_not_forward_opencv_compatible"
            ),
        },
    }


def _intrinsic_split_detections(
    count: int,
    *,
    reverse_mapping: bool = False,
) -> dict:
    _dictionary, board = opencv_grid_board(DEFAULT_TARGET_SPEC)
    ids = board.getIds().reshape(-1).astype(int).tolist()
    objects = [
        np.asarray(item, dtype=np.float32).reshape(4, 3)
        for item in board.getObjPoints()
    ]
    camera = np.asarray(
        [[600.0, 0.0, 320.0], [0.0, 605.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=float,
    )
    cell_x = (-300.0, -125.0, 50.0)
    cell_y = (-250.0, -90.0, 160.0)
    frames = {}
    for index in range(count):
        unique_index = index // 3
        cell = unique_index % 9
        cycle = unique_index // 9
        row, column = divmod(cell, 3)
        rvec = np.asarray(
            [
                -0.28 + 0.035 * (cycle % 9),
                -0.22 + 0.045 * ((cycle * 2 + cell) % 9),
                -0.20 + 0.05 * ((cycle + cell * 2) % 9),
            ]
        )
        tvec = np.asarray(
            [
                cell_x[column] + 8.0 * (cycle % 5),
                cell_y[row] + 6.0 * ((cycle + cell) % 5),
                650.0 + 28.0 * (cycle % 7),
            ]
        )
        corners = [
            cv2.projectPoints(points, rvec, tvec, camera, np.zeros(5))[0]
            .reshape(4, 2)
            .tolist()
            for points in objects
        ]
        all_points = np.concatenate([np.asarray(item, dtype=float) for item in corners])
        name = f"{index:06d}.png"
        frames[name] = {
            "ids": ids,
            "corners": corners,
            "marker_count": len(ids),
            "image_centroid_px": all_points.mean(axis=0).tolist(),
        }
    items = list(frames.items())
    if reverse_mapping:
        items.reverse()
    return {
        "schema_version": "aruco_detections.v1",
        "image_size": [640, 480],
        "frames": dict(items),
    }


def test_intrinsic_split_caps_views_preserves_coverage_and_blocks_leakage() -> None:
    detections = _intrinsic_split_detections(240)

    training, holdout, split = attempt_module._intrinsic_detection_split(
        detections,
        DEFAULT_TARGET_SPEC,
    )
    shuffled_training, shuffled_holdout, shuffled_split = (
        attempt_module._intrinsic_detection_split(
            _intrinsic_split_detections(240, reverse_mapping=True),
            DEFAULT_TARGET_SPEC,
        )
    )

    assert list(training["frames"]) == split["training_views"]
    assert list(holdout["frames"]) == split["heldout_views"]
    assert len(training["frames"]) == 45
    assert len(holdout["frames"]) == 15
    assert set(training["frames"]).isdisjoint(holdout["frames"])
    assert len(split["training_coverage_cells"]) >= 6
    assert split["holdout_guard"] == {
        "requested_temporal_radius_views": 5,
        "effective_temporal_radius_views": 5,
        "requested_descriptor_distance": 1.0,
        "effective_descriptor_distance": 1.0,
        "relaxed_for_minimum_split_feasibility": False,
    }
    assert shuffled_split["training_views"] == split["training_views"]
    assert shuffled_split["heldout_views"] == split["heldout_views"]
    assert list(shuffled_training["frames"]) == split["training_views"]
    assert list(shuffled_holdout["frames"]) == split["heldout_views"]

    evidence = {item["frame"]: item for item in split["selected_view_evidence"]}
    scale = split["descriptor"]["normalized_corner_coordinate_scale"]
    for training_name in split["training_views"]:
        training_view = evidence[training_name]
        training_descriptor = (
            np.asarray(training_view["projected_board_corners_normalized"]) / scale
        )
        for holdout_name in split["heldout_views"]:
            holdout_view = evidence[holdout_name]
            holdout_descriptor = (
                np.asarray(holdout_view["projected_board_corners_normalized"]) / scale
            )
            assert (
                abs(
                    training_view["chronological_index"]
                    - holdout_view["chronological_index"]
                )
                > 5
            )
            assert np.linalg.norm(training_descriptor - holdout_descriptor) >= 1.0


def test_reuse_intrinsics_rejects_incompatible_existing_and_uses_factory(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    attempt_root = run_root / "processed" / "calibration" / ("d" * 32)
    attempt_root.mkdir(parents=True)
    folder = attempt_root / "processed" / "synchronized" / "realsense_1"
    factory = _intrinsic_sensor_fixture(folder)
    existing = _unsupported_intrinsic_profile(
        factory,
        profile_id="stored-inverse-projection",
    )
    write_intrinsic_profile_collection(
        [existing],
        run_root / INTRINSIC_CALIBRATION_PROFILES,
    )

    profiles, by_sensor = attempt_module._intrinsics_for_sensors(
        run_root,
        attempt_root,
        {"realsense_d435:1": folder},
        {
            "attempt_id": "d" * 32,
            "intrinsics_policy": "reuse_compatible_or_factory",
            "target": {},
        },
    )

    assert profiles[0]["profile_id"] == factory["profile_id"]
    selected = by_sensor["realsense_d435:1"]
    assert selected["attempt_intrinsics_source"] == (
        "factory_capture_sidecars_existing_projection_unusable"
    )
    comparison = json.loads((attempt_root / INTRINSIC_COMPARISON).read_text())
    sensor = comparison["sensors"][0]
    assert sensor["status"] == "factory_selected"
    assert sensor["existing_projection"] == {
        "profile_id": "stored-inverse-projection",
        "opencv_projection_compatible": False,
        "distortion_model": "inverse_brown_conrady",
        "reason": "distortion_model_is_not_forward_opencv_compatible",
    }
    assert sensor["factory_projection"]["opencv_projection_compatible"] is True
    assert sensor["unusable_projection"] is None
    assert {item["profile_id"] for item in sensor["candidates"]} == {
        factory["profile_id"],
        "stored-inverse-projection",
    }


def test_manual_intrinsic_plausibility_rejects_absurd_parameters(
    tmp_path: Path,
) -> None:
    factory = _intrinsic_sensor_fixture(tmp_path / "realsense_1")
    manual = {
        **factory,
        "native": {
            **factory["native"],
            "cam_K": [
                50.0,
                0.0,
                -100.0,
                0.0,
                3000.0,
                900.0,
                0.0,
                0.0,
                1.0,
            ],
            "distortion": [2.0, -4.0, 0.2, -0.2, 8.0],
        },
    }

    result = attempt_module._manual_intrinsic_plausibility(factory, manual)

    assert result["status"] == "rejected"
    assert result["checks"]["principal_point_inside_image"] is False
    assert result["checks"]["distortion_magnitude"] is False


def _fixture_observations(
    mode: str,
    *,
    count: int = 10,
) -> tuple[list[dict], np.ndarray, np.ndarray]:
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
    poses = [
        {
            "X": 50.0 + 15 * index,
            "Y": -40.0 + 8 * (index % 3),
            "Z": 500.0 + 10 * (index % 4),
            "A": -0.18 + 0.05 * (index % 4),
            "B": 0.12 - 0.04 * (index % 5),
            "C": -0.25 + 0.07 * index,
        }
        for index in range(count)
    ]
    observations = []
    for index, robot_pose in enumerate(poses):
        coverage_row, coverage_column = divmod(index % 9, 3)
        flange_to_base = robot_ee_to_reference(robot_pose)
        if mode == "eye_in_hand":
            target_to_camera = (
                pt.invert_transform(camera_to_flange)
                @ pt.invert_transform(flange_to_base)
                @ target_to_base
            )
            expected_primary, expected_companion = camera_to_flange, target_to_base
        else:
            target_to_camera = (
                pt.invert_transform(camera_to_base) @ flange_to_base @ target_to_flange
            )
            expected_primary, expected_companion = camera_to_base, target_to_flange
        observations.append(
            {
                "frame_id": f"{index:06d}.png",
                "motion": f"pose_{index:02d}",
                "robot_ee_pose": robot_pose,
                "target_to_camera": transform_record(
                    target_to_camera,
                    from_frame="aruco_grid",
                    to_frame="camera",
                ),
                "mean_reprojection_error_px": 0.1,
                "image_coverage_cell": index % 9,
                "image_centroid_px": [
                    (coverage_column + 0.5) * 640.0 / 3.0,
                    (coverage_row + 0.5) * 480.0 / 3.0,
                ],
                "image_size": [640, 480],
            }
        )
    return observations, expected_primary, expected_companion


@pytest.mark.parametrize("mode", ["eye_in_hand", "eye_to_hand"])
def test_leave_one_pose_out_ranking_recovers_known_transform(mode: str) -> None:
    observations, expected, _companion = _fixture_observations(mode)

    candidate = evaluate_extrinsic_candidate(
        observations,
        mode=mode,
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )

    assert candidate["status"] == "passing"
    assert candidate["inlier_count"] == len(observations)
    actual = np.asarray(candidate["primary_transform"]["matrix"])
    assert transform_residual(actual, expected)["translation_mm"] < 1e-5
    assert candidate["held_out_residuals"]["median_translation_mm"] < 1e-5


@pytest.mark.parametrize("mode", ["eye_in_hand", "eye_to_hand"])
def test_robust_closure_rejects_one_outlier_and_recovers_transform(mode: str) -> None:
    observations, expected, _companion = _fixture_observations(mode)
    corrupted = transform_from_record(observations[-1]["target_to_camera"])
    corrupted[:3, 3] += np.asarray([20.0, -10.0, 5.0])
    observations[-1]["target_to_camera"] = transform_record(
        corrupted,
        from_frame="aruco_grid",
        to_frame="camera",
    )

    candidate = evaluate_extrinsic_candidate(
        observations,
        mode=mode,
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )

    assert candidate["status"] == "passing"
    assert candidate["inlier_count"] == 9
    assert candidate["outlier_count"] == 1
    assert candidate["outlier_ratio"] == pytest.approx(0.1)
    actual = np.asarray(candidate["primary_transform"]["matrix"])
    assert transform_residual(actual, expected)["translation_mm"] < 1e-5
    assert candidate["leave_one_pose_out"][-1]["validation_split"] == (
        "rejected_closure_outlier"
    )


def test_degenerate_motion_is_reported_as_candidate_failure() -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    same_pose = dict(observations[0]["robot_ee_pose"])
    for observation in observations:
        observation["robot_ee_pose"] = same_pose

    candidate = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="IPPE",
        extrinsic_method="tsai",
        sensor_key="realsense_d435:1",
    )

    assert candidate["status"] == "error"
    assert "degenerate robot motion" in candidate["error"]


def test_attempt_quality_gates_require_fifteen_views_and_six_coverage_cells() -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")

    too_few = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
        min_accepted_views=15,
        min_coverage_cells=6,
    )

    assert too_few["status"] == "error"
    assert "accepted view count 10 is below required 15" in too_few["error"]

    many, _expected, _companion = _fixture_observations("eye_in_hand", count=18)
    for observation in many:
        observation["image_coverage_cell"] = 4
    poor_coverage = evaluate_extrinsic_candidate(
        many,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
        min_accepted_views=15,
        min_coverage_cells=6,
    )

    assert poor_coverage["status"] == "error"
    assert "image-centroid coverage 1/9 is below required 6/9" in poor_coverage["error"]


def _coplanar_pnp_ransac_regression_fixture() -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    object_points = np.asarray(
        [
            [column * 55.275 + x, row * 55.0 + y, 0.0]
            for row in range(5)
            for column in range(7)
            for x, y in (
                (0.0, 0.0),
                (45.225, 0.0),
                (45.225, 45.0),
                (0.0, 45.0),
            )
        ],
        dtype=np.float64,
    )
    camera = np.asarray(
        [
            [903.128737, 0.0, 632.458263],
            [0.0, 914.254590, 385.141689],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    distortion = np.asarray(
        [0.124566, -0.378566, 0.002004, -0.000095, 0.288739],
        dtype=np.float64,
    )
    projected = cv2.projectPoints(
        object_points,
        np.asarray([-0.812730, 0.342254, 0.149498]),
        np.asarray([122.142598, -10.936042, 797.931882]),
        camera,
        distortion,
    )[0].reshape(-1, 2)
    image_points = projected + np.random.default_rng(0).normal(
        0.0,
        0.65,
        projected.shape,
    )
    return object_points, image_points, camera, distortion


def test_planar_pnp_uses_shared_inliers_refines_and_retains_ippe_ambiguity() -> None:
    object_points = np.asarray(
        [[x, y, 0.0] for y in (0.0, 40.0, 80.0) for x in (0.0, 40.0, 80.0, 120.0)],
        dtype=float,
    )
    camera = np.asarray(
        [[700.0, 0.0, 320.0], [0.0, 705.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=float,
    )
    image_points = cv2.projectPoints(
        object_points,
        np.asarray([0.1, -0.05, 0.03]),
        np.asarray([10.0, -20.0, 600.0]),
        camera,
        np.zeros(5),
    )[0].reshape(-1, 2)

    result = solve_planar_pnp_candidates(
        object_points,
        image_points,
        camera,
        np.zeros(5),
    )

    assert set(result["selected"]) == {"IPPE", "ITERATIVE", "SQPNP"}
    assert result["common_inlier_count"] == len(object_points)
    assert len([item for item in result["candidates"] if item["method"] == "IPPE"]) == 2
    assert all(
        item["common_inlier_indices"] == result["common_inlier_indices"]
        and item["refinement"] == "solvePnPRefineLM"
        for item in result["candidates"]
    )
    assert result["duplicate_marker_clutter_filtered"] is False
    assert result["raw_common_inlier_ratio"] == result["common_inlier_ratio"]


def test_planar_pnp_isolates_strong_target_instance_from_duplicate_marker_clutter() -> (
    None
):
    marker_origins = [
        (column * 50.0, row * 50.0) for row in range(3) for column in range(4)
    ]
    marker_objects = [
        np.asarray(
            [
                [x, y, 0.0],
                [x + 35.0, y, 0.0],
                [x + 35.0, y + 35.0, 0.0],
                [x, y + 35.0, 0.0],
            ],
            dtype=float,
        )
        for x, y in marker_origins
    ]
    camera = np.asarray(
        [[700.0, 0.0, 640.0], [0.0, 705.0, 360.0], [0.0, 0.0, 1.0]],
        dtype=float,
    )

    def project(points: np.ndarray, translation: list[float]) -> np.ndarray:
        return cv2.projectPoints(
            points,
            np.asarray([0.08, -0.04, 0.02]),
            np.asarray(translation),
            camera,
            np.zeros(5),
        )[0].reshape(-1, 2)

    object_groups = list(marker_objects)
    image_groups = [project(points, [10.0, -20.0, 650.0]) for points in marker_objects]
    marker_ids = list(range(12))
    grid_indices = [(marker_id // 4, marker_id % 4) for marker_id in marker_ids]
    for translation in ([-250.0, 180.0, 700.0], [270.0, 160.0, 720.0]):
        for marker_id in range(8):
            object_groups.append(marker_objects[marker_id])
            image_groups.append(project(marker_objects[marker_id], translation))
            marker_ids.append(marker_id)
            grid_indices.append((marker_id // 4, marker_id % 4))

    object_points = np.concatenate(object_groups)
    image_points = np.concatenate(image_groups)
    point_marker_ids = np.repeat(np.asarray(marker_ids), 4)
    point_grid_indices = np.repeat(np.asarray(grid_indices), 4, axis=0)
    result = solve_planar_pnp_candidates(
        object_points,
        image_points,
        camera,
        np.zeros(5),
        methods=("ITERATIVE",),
        point_marker_ids=point_marker_ids,
        point_grid_indices=point_grid_indices,
    )

    selected = result["selected"]["ITERATIVE"]
    expected_centroid = np.mean(np.concatenate(image_groups[:12]), axis=0)
    assert result["duplicate_marker_clutter_filtered"] is True
    assert result["duplicate_marker_ids"] == list(range(8))
    assert result["raw_common_inlier_ratio"] < 0.5
    assert result["common_inlier_ratio"] == pytest.approx(1.0)
    assert result["common_inlier_ratio_basis"] == (
        "unique_marker_correspondence_capacity"
    )
    assert result["supported_marker_count"] == 12
    assert result["ignored_clutter_correspondence_count"] == 64
    assert result["consensus_image_centroid_px"] == pytest.approx(
        expected_centroid.tolist(), abs=1e-4
    )
    assert selected["quality_reprojection_scope"] == (
        "homography_consensus_target_instance"
    )
    assert selected["quality_mean_reprojection_error_px"] < 0.01
    assert selected["all_point_mean_reprojection_error_px"] > 100.0


def test_planar_pnp_rejects_small_duplicate_marker_clutter_consensus() -> None:
    object_points = np.asarray(
        [
            [column * 45.0 + x, row * 45.0 + y, 0.0]
            for row in range(2)
            for column in range(2)
            for x, y in ((0.0, 0.0), (35.0, 0.0), (35.0, 35.0), (0.0, 35.0))
        ],
        dtype=float,
    )
    camera = np.asarray(
        [[700.0, 0.0, 320.0], [0.0, 705.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=float,
    )
    primary = cv2.projectPoints(
        object_points,
        np.zeros(3),
        np.asarray([0.0, 0.0, 600.0]),
        camera,
        np.zeros(5),
    )[0].reshape(-1, 2)
    clutter = cv2.projectPoints(
        object_points,
        np.zeros(3),
        np.asarray([180.0, 120.0, 650.0]),
        camera,
        np.zeros(5),
    )[0].reshape(-1, 2)
    marker_ids = np.tile(np.repeat(np.arange(4), 4), 2)
    grid_indices = np.tile(
        np.repeat(np.asarray([(0, 0), (0, 1), (1, 0), (1, 1)]), 4, axis=0),
        (2, 1),
    )

    result = solve_planar_pnp_candidates(
        np.tile(object_points, (2, 1)),
        np.concatenate([primary, clutter]),
        camera,
        np.zeros(5),
        methods=("ITERATIVE",),
        point_marker_ids=marker_ids,
        point_grid_indices=grid_indices,
    )

    assert result["selected"] == {}
    assert result["failures"][0]["reason"] == (
        "insufficient_duplicate_marker_clutter_consensus"
    )


def test_candidate_ranking_has_stable_method_tie_breaks() -> None:
    common = {
        "status": "passing",
        "score": 0.5,
        "mean_reprojection_error_px": 0.2,
        "inlier_count": 8,
        "sensor_key": "realsense_d435:1",
    }
    values = [
        {
            **common,
            "candidate_id": "sq",
            "pnp_method": "SQPNP",
            "extrinsic_method": "tsai",
        },
        {
            **common,
            "candidate_id": "it",
            "pnp_method": "ITERATIVE",
            "extrinsic_method": "tsai",
        },
        {
            **common,
            "candidate_id": "ip",
            "pnp_method": "IPPE",
            "extrinsic_method": "park",
        },
        {
            **common,
            "candidate_id": "ip-tsai",
            "pnp_method": "IPPE",
            "extrinsic_method": "tsai",
        },
    ]

    ranked = rank_candidates(values)

    assert [item["candidate_id"] for item in ranked] == ["ip-tsai", "ip", "it", "sq"]
    assert ranked[0]["recommended"] is True


def test_parent_attempt_runs_five_phases_writes_evidence_and_cannot_be_replayed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand", count=18)
    run_root = tmp_path / "run"
    attempt_id = "a" * 32
    attempt_root = run_root / "processed" / "calibration" / attempt_id
    attempt_root.mkdir(parents=True)
    robot_pose_path = run_root / "raw_robot_ee_poses.json"
    run_id = "11111111-1111-4111-8111-111111111111"
    robot_pose_path.write_text(
        json.dumps(
            {
                "0": {
                    "pose": {},
                    "source_packet": {
                        "schema_version": "robot_pose.v1",
                        "packet_kind": "pose",
                        "run_id": run_id,
                        "from_frame": "robot_flange",
                        "to_frame": "template_base",
                        "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase",
                    },
                }
            }
        )
    )
    robot_pose_payload = robot_pose_path.read_bytes()
    sensor_key = "realsense_d435:1"
    request_value = {
        "schema_version": attempt_module.REQUEST_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "run_root": run_root.as_posix(),
        "created_at": "2026-07-17T00:00:00+00:00",
        "mode": "eye_in_hand",
        "sensor_keys": [sensor_key],
        "sensors": [
            {
                "sensor_key": sensor_key,
                "sensor_name": "realsense_1",
                "sensor_type": "realsense_d435",
                "device_id": "1",
                "display_name": "D435",
                "robot_pose_path": "raw_robot_ee_poses.json",
            }
        ],
        "target_id": "target-1",
        "target": {"target_type": "aruco_grid", "unit": "mm"},
        "target_mounting": {
            "from": "aruco_grid",
            "to": "template_base",
            "state": "estimated",
        },
        "robot_pose_reference": {
            "schema_version": "robot_pose_reference.v1",
            "status": "verified",
            "packet_schema_version": "robot_pose.v1",
            "from": "robot_flange",
            "to": "template_base",
            "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase",
            "run_id": run_id,
            "artifacts": ["raw_robot_ee_poses.json"],
            "pose_counts": {"raw_robot_ee_poses.json": 1},
            "artifact_bindings": [
                {
                    "path": "raw_robot_ee_poses.json",
                    "size_bytes": len(robot_pose_payload),
                    "sha256": hashlib.sha256(robot_pose_payload).hexdigest(),
                }
            ],
        },
        "solver_policy": "auto_compare",
        "pnp_methods": ["ITERATIVE"],
        "extrinsic_methods": ["park"],
        "intrinsics_policy": "reuse_compatible_or_factory",
        "synchronization_policy": "fixed_zero",
        "synchronization_search": attempt_module.time_offset_search_configuration(),
        "synchronization_implementation_revision": (
            attempt_module.TIME_OFFSET_IMPLEMENTATION_REVISION
        ),
    }
    request_value["timestamp_policy"] = attempt_module._attempt_timestamp_policy(
        request_value["sensors"]
    )
    (attempt_root / "request.json").write_text(json.dumps(request_value))
    (attempt_root / "progress.json").write_text(
        json.dumps(attempt_module._initial_progress(attempt_id))
    )
    intrinsic = {
        "profile_id": "factory-1",
        "native": {
            "cam_K": [600.0, 0.0, 320.0, 0.0, 600.0, 240.0, 0.0, 0.0, 1.0],
            "width": 640,
            "height": 480,
            "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
            "distortion_model": "inverse_brown_conrady",
        },
        "depth": {"scale_to_mm": 1.0},
    }
    verified_snapshots = []

    def fake_prepare_attempt_data(*args):
        verified_snapshots.append(args[3])
        return {sensor_key: attempt_root / "sensor"}, {sensor_key: intrinsic}

    monkeypatch.setattr(
        attempt_module,
        "_prepare_attempt_data",
        fake_prepare_attempt_data,
    )
    monkeypatch.setattr(
        attempt_module,
        "_estimate_target_poses",
        lambda *_args: (
            {"sensors": []},
            {sensor_key: {"ITERATIVE": observations}},
        ),
    )
    time_offset_search = {
        "schema_version": "calibration_time_offset_search.v1",
        "policy": "fixed_zero",
        "status": "complete",
        "sign_convention": attempt_module.time_offset_sign_convention(),
        "sensors": [
            attempt_module.fixed_zero_sensor_result(
                sensor_key=sensor_key,
                observation_count=len(observations),
            )
        ],
    }

    def fake_time_offsets(*_args):
        verified_snapshots.append(_args[4])
        attempt_module.atomic_write_json(
            attempt_root / "time_offset_search.json",
            time_offset_search,
        )
        return time_offset_search, {sensor_key: {"ITERATIVE": observations}}

    monkeypatch.setattr(
        attempt_module,
        "_estimate_and_apply_time_offsets",
        fake_time_offsets,
    )

    def fake_authoritative_synchronization(*args):
        verified_snapshots.append(args[5])
        return (
            {sensor_key: attempt_root / "sensor"},
            {sensor_key: {"ITERATIVE": observations}},
        )

    monkeypatch.setattr(
        attempt_module,
        "_materialize_authoritative_synchronization",
        fake_authoritative_synchronization,
    )

    monkeypatch.chdir(tmp_path)
    ranking = attempt_module.run_calibration_attempt(Path("run"), attempt_id)

    assert ranking["status"] == "complete"
    assert ranking["recommended_camera_count"] == 1
    assert ranking["results"][0]["recommendation"]["extrinsic_method"] == "park"
    assert len(verified_snapshots) == 3
    assert verified_snapshots[0] is verified_snapshots[1] is verified_snapshots[2]
    assert set(verified_snapshots[0]) == {"raw_robot_ee_poses.json"}
    progress = json.loads((attempt_root / "progress.json").read_text())
    assert progress["status"] == "complete"
    assert [item["status"] for item in progress["phases"]] == [
        "complete",
        "complete",
        "complete",
        "complete",
        "complete",
    ]
    for filename in (
        "observations.json",
        "extrinsic_candidates.json",
        "ranking.json",
        "checks.json",
        "candidate_profiles.json",
        "time_offset_search.json",
    ):
        assert (attempt_root / filename).is_file()
    candidate_profiles = json.loads(
        (attempt_root / "candidate_profiles.json").read_text()
    )
    candidate_profile = candidate_profiles["profiles"][0]
    assert candidate_profile["sync_delta_ms"] == 0.0
    assert (
        candidate_profile["metadata"]["robot_pose_reference"]
        == (request_value["robot_pose_reference"])
    )
    assert candidate_profile["intrinsics"]["native"]["distortion_model"] == (
        "inverse_brown_conrady"
    )
    assert candidate_profile["intrinsics"]["rectified"] is not None
    assert candidate_profile["intrinsics"]["rectified"]["distortion"] == [0.0] * 5
    assert candidate_profile["metadata"]["synchronization"] == {
        "policy": "fixed_zero",
        "status": "fixed_zero",
        "robot_pose_time_offset_ms": 0.0,
        "sync_delta_ms": 0.0,
        "source": (
            "processed/calibration/"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/time_offset_search.json"
        ),
        "timestamp_source": "sensor",
        "frame_timestamp_source": "sensor",
        "robot_timestamp_source": "host_wall",
        "required_frame_timestamp_domain": "global_time",
        "timestamp_fallback_allowed": False,
        "max_nearest_pose_delta_ms": 150.0,
        "warning_nearest_pose_delta_ms": 20.0,
        "warning_fallback_used": False,
        "auto_estimated_per_sensor_offset": False,
        "sensor_key": sensor_key,
        "quality_report": (
            f"processed/calibration/{attempt_id}/sync_quality_report.json"
        ),
    }
    with pytest.raises(ValueError, match="immutable"):
        attempt_module.run_calibration_attempt(Path("run"), attempt_id)


def _multi_camera_candidate_variant(
    base: dict,
    *,
    sensor_key: str,
    pnp_method: str,
    extrinsic_method: str,
    score: float,
    companion_translation_offset_mm: float,
) -> dict:
    candidate = json.loads(json.dumps(base))
    candidate.update(
        {
            "candidate_id": f"{sensor_key}|{pnp_method}|{extrinsic_method}",
            "sensor_key": sensor_key,
            "pnp_method": pnp_method,
            "extrinsic_method": extrinsic_method,
            "algorithms": [pnp_method, extrinsic_method],
            "score": score,
        }
    )
    companion = transform_from_record(candidate["companion_transform"])
    companion[0, 3] += companion_translation_offset_mm
    candidate["companion_transform"] = transform_record(
        companion,
        from_frame="aruco_grid",
        to_frame="template_base",
    )
    candidate["synchronization"] = {
        "policy": "fixed_zero",
        "status": "fixed_zero",
        "robot_pose_time_offset_ms": 0.0,
        "sync_delta_ms": 0.0,
        "source": "processed/calibration/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/time_offset_search.json",
        "warning_fallback_used": False,
    }
    return candidate


def _multi_camera_request() -> dict:
    value = {
        "attempt_id": "a" * 32,
        "mode": "eye_in_hand",
        "sensor_keys": ["realsense_d435:1", "oak_d_pro:2"],
        "sensors": [
            {
                "sensor_key": "realsense_d435:1",
                "sensor_name": "realsense_1",
                "sensor_type": "realsense_d435",
                "device_id": "1",
                "display_name": "D435",
            },
            {
                "sensor_key": "oak_d_pro:2",
                "sensor_name": "luxonis_2",
                "sensor_type": "oak_d_pro",
                "device_id": "2",
                "display_name": "OAK",
            },
        ],
        "target_id": "target-1",
        "target_mounting": {
            "from": "aruco_grid",
            "to": "template_base",
            "state": "estimated",
        },
        "solver_policy": "auto_compare",
        "pnp_methods": ["IPPE", "ITERATIVE"],
        "extrinsic_methods": ["park"],
        "intrinsics_policy": "reuse_compatible_or_factory",
        "robot_pose_reference": {
            "schema_version": "robot_pose_reference.v1",
            "status": "verified",
            "packet_schema_version": "robot_pose.v1",
            "from": "robot_flange",
            "to": "template_base",
            "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase",
            "run_id": "11111111-1111-4111-8111-111111111111",
        },
        "synchronization_policy": "fixed_zero",
        "synchronization_search": attempt_module.time_offset_search_configuration(),
        "synchronization_implementation_revision": (
            attempt_module.TIME_OFFSET_IMPLEMENTATION_REVISION
        ),
    }
    value["timestamp_policy"] = attempt_module._attempt_timestamp_policy(
        value["sensors"]
    )
    return value


def _multi_camera_intrinsics() -> dict[str, dict]:
    intrinsic = {
        "profile_id": "factory",
        "sensor_id": "1",
        "native": {
            "cam_K": [600.0, 0.0, 320.0, 0.0, 600.0, 240.0, 0.0, 0.0, 1.0],
            "width": 640,
            "height": 480,
            "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "depth": {"scale_to_mm": 1.0},
    }
    return {
        "realsense_d435:1": intrinsic,
        "oak_d_pro:2": {**intrinsic, "sensor_id": "2"},
    }


def test_multi_camera_ranking_selects_best_common_bundle_and_records_evidence(
    tmp_path: Path,
) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    base = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )
    candidates = [
        _multi_camera_candidate_variant(
            base,
            sensor_key="realsense_d435:1",
            pnp_method="IPPE",
            extrinsic_method="park",
            score=0.05,
            companion_translation_offset_mm=0.0,
        ),
        _multi_camera_candidate_variant(
            base,
            sensor_key="realsense_d435:1",
            pnp_method="ITERATIVE",
            extrinsic_method="park",
            score=0.20,
            companion_translation_offset_mm=0.0,
        ),
        _multi_camera_candidate_variant(
            base,
            sensor_key="oak_d_pro:2",
            pnp_method="IPPE",
            extrinsic_method="park",
            score=0.60,
            companion_translation_offset_mm=2.0,
        ),
        _multi_camera_candidate_variant(
            base,
            sensor_key="oak_d_pro:2",
            pnp_method="ITERATIVE",
            extrinsic_method="park",
            score=0.21,
            companion_translation_offset_mm=6.0,
        ),
    ]
    request_value = _multi_camera_request()

    ranking = attempt_module._validate_and_rank(
        tmp_path,
        request_value,
        candidates,
        _multi_camera_intrinsics(),
    )

    assert ranking["status"] == "complete"
    assert ranking["recommended_camera_count"] == 2
    assert {
        result["sensor_key"]: result["recommendation"]["pnp_method"]
        for result in ranking["results"]
    } == {
        "realsense_d435:1": "ITERATIVE",
        "oak_d_pro:2": "ITERATIVE",
    }
    consistency = ranking["multi_camera_consistency"]
    assert consistency["status"] == "passing"
    assert consistency["recommended_bundle_id"] == "ITERATIVE|park"
    assert consistency["passing_bundle_count"] == 2
    assert [bundle["bundle_id"] for bundle in consistency["bundles"]] == [
        "ITERATIVE|park",
        "IPPE|park",
    ]
    recommended = consistency["recommendation"]
    assert recommended["mean_score"] == pytest.approx(0.205)
    assert recommended["aggregate_score"] == pytest.approx(0.41)
    assert recommended["max_pairwise_companion_translation_mm"] == pytest.approx(6.0)
    assert recommended["max_pairwise_companion_rotation_deg"] == pytest.approx(0.0)
    assert recommended["pairwise_companion_residuals"] == [
        {
            "left_sensor_key": "realsense_d435:1",
            "right_sensor_key": "oak_d_pro:2",
            "left_candidate_id": "realsense_d435:1|ITERATIVE|park",
            "right_candidate_id": "oak_d_pro:2|ITERATIVE|park",
            "translation_mm": pytest.approx(6.0),
            "rotation_deg": pytest.approx(0.0),
            "status": "ok",
        }
    ]
    checks = json.loads((tmp_path / "checks.json").read_text())["checks"]
    joint_checks = [
        check for check in checks if check.get("scope") == "multi_camera_bundle"
    ]
    assert len(joint_checks) == 12
    assert {check["bundle_id"] for check in joint_checks} == {
        "IPPE|park",
        "ITERATIVE|park",
    }

    attempt = {"request": request_value, "results": ranking}
    expected = {
        "realsense_d435:1": "realsense_d435:1|ITERATIVE|park",
        "oak_d_pro:2": "oak_d_pro:2|ITERATIVE|park",
    }
    assert attempt_module._promotion_selections(attempt, None) == expected
    assert attempt_module._promotion_selections(
        attempt,
        {
            "realsense_d435:1": "realsense_d435:1|IPPE|park",
            "oak_d_pro:2": "oak_d_pro:2|IPPE|park",
        },
    ) == {
        "realsense_d435:1": "realsense_d435:1|IPPE|park",
        "oak_d_pro:2": "oak_d_pro:2|IPPE|park",
    }
    with pytest.raises(ValueError, match="common algorithm bundle"):
        attempt_module._promotion_selections(
            attempt,
            {
                "realsense_d435:1": "realsense_d435:1|IPPE|park",
                "oak_d_pro:2": "oak_d_pro:2|ITERATIVE|park",
            },
        )
    with pytest.raises(ValueError, match="every jointly ranked sensor"):
        attempt_module._promotion_selections(
            attempt,
            {"realsense_d435:1": "realsense_d435:1|ITERATIVE|park"},
        )


def test_multi_camera_ranking_ignores_failed_unselected_solver_alternative(
    tmp_path: Path,
) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    base = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )
    passing = [
        _multi_camera_candidate_variant(
            base,
            sensor_key=sensor_key,
            pnp_method="ITERATIVE",
            extrinsic_method="park",
            score=0.2,
            companion_translation_offset_mm=offset,
        )
        for sensor_key, offset in (
            ("realsense_d435:1", 0.0),
            ("oak_d_pro:2", 2.0),
        )
    ]
    failed_alternative = _multi_camera_candidate_variant(
        base,
        sensor_key="realsense_d435:1",
        pnp_method="ITERATIVE",
        extrinsic_method="daniilidis",
        score=0.3,
        companion_translation_offset_mm=0.0,
    )
    failed_alternative.update(
        {
            "status": "error",
            "score": None,
            "error": "degenerate robot motion",
        }
    )
    request_value = _multi_camera_request()
    request_value["pnp_methods"] = ["ITERATIVE"]
    request_value["extrinsic_methods"] = ["park", "daniilidis"]

    ranking = attempt_module._validate_and_rank(
        tmp_path,
        request_value,
        [*passing, failed_alternative],
        _multi_camera_intrinsics(),
    )

    assert ranking["status"] == "complete"
    assert ranking["recommended_camera_count"] == 2
    assert ranking["multi_camera_consistency"]["recommended_bundle_id"] == (
        "ITERATIVE|park"
    )
    review = attempt_module._promotion_review(request_value, ranking)
    assert review is not None
    assert review["status"] == "promotable"
    assert review["alternative_failure_count"] == 1


def test_multi_camera_ranking_retains_bounded_companion_disagreement_as_warning(
    tmp_path: Path,
) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    base = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )
    candidates = [
        _multi_camera_candidate_variant(
            base,
            sensor_key=sensor_key,
            pnp_method="ITERATIVE",
            extrinsic_method="park",
            score=0.2,
            companion_translation_offset_mm=offset,
        )
        for sensor_key, offset in (
            ("realsense_d435:1", 0.0),
            ("oak_d_pro:2", 10.01),
        )
    ]
    request_value = _multi_camera_request()
    request_value["pnp_methods"] = ["ITERATIVE"]

    ranking = attempt_module._validate_and_rank(
        tmp_path,
        request_value,
        candidates,
        _multi_camera_intrinsics(),
    )

    assert all(candidate["status"] == "passing" for candidate in candidates)
    assert ranking["status"] == "complete"
    assert ranking["recommended_camera_count"] == 2
    bundle = ranking["multi_camera_consistency"]["bundles"][0]
    assert bundle["status"] == "passing"
    assert bundle["quality_state"] == "warning"
    assert bundle["max_pairwise_companion_translation_mm"] == pytest.approx(10.01)
    assert next(
        check
        for check in bundle["checks"]
        if check["name"] == "joint_companion_translation_consistency"
    ) == {
        "name": "joint_companion_translation_consistency",
        "status": "warning",
        "actual": pytest.approx(10.01),
        "warning_threshold": 10.0,
        "threshold": 20.0,
        "unit": "mm",
    }


def test_legacy_hard_failed_bundle_is_promotable_under_current_warning_policy(
    tmp_path: Path,
) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    base = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )
    candidates = [
        _multi_camera_candidate_variant(
            base,
            sensor_key=sensor_key,
            pnp_method="ITERATIVE",
            extrinsic_method="park",
            score=0.2,
            companion_translation_offset_mm=offset,
        )
        for sensor_key, offset in (
            ("realsense_d435:1", 0.0),
            ("oak_d_pro:2", 15.0),
        )
    ]
    request_value = _multi_camera_request()
    request_value["pnp_methods"] = ["ITERATIVE"]
    current = attempt_module._validate_and_rank(
        tmp_path,
        request_value,
        candidates,
        _multi_camera_intrinsics(),
    )
    ranked_by_sensor = {
        result["sensor_key"]: result["candidates"] for result in current["results"]
    }
    legacy = attempt_module._joint_consistency_ranking(
        request_value,
        ranked_by_sensor,
        policy_revision=attempt_module.LEGACY_JOINT_CONSISTENCY_POLICY_REVISION,
    )
    assert legacy is not None
    legacy.pop("policy_revision")
    legacy["thresholds"].pop("warning_pairwise_companion_translation_mm")
    legacy["thresholds"].pop("warning_pairwise_companion_rotation_deg")
    for bundle in legacy["bundles"]:
        bundle.pop("consistency_policy_revision")
        bundle.pop("quality_state")
        bundle.pop("quality_warning_count")
        bundle.pop("quality_warnings")
    historical_ranking = {
        **current,
        "status": "failed",
        "recommended_camera_count": 0,
        "failed_camera_count": 2,
        "multi_camera_consistency": legacy,
        "results": [
            {
                **result,
                "status": "failed",
                "recommended_candidate_id": None,
                "recommended_profile_id": None,
                "recommendation": None,
            }
            for result in current["results"]
        ],
    }
    attempt = {"request": request_value, "results": historical_ranking}

    review = attempt_module._promotion_review(request_value, historical_ranking)

    assert review is not None
    assert review["status"] == "promotable_with_warnings"
    assert review["selected_camera_count"] == 2
    assert review["selected_bundle"][
        "max_pairwise_companion_translation_mm"
    ] == pytest.approx(15.0)
    selections = attempt_module._promotion_selections(attempt, None)
    assert selections == {
        "realsense_d435:1": "realsense_d435:1|ITERATIVE|park",
        "oak_d_pro:2": "oak_d_pro:2|ITERATIVE|park",
    }
    revalidated = attempt_module._revalidate_joint_promotion(attempt, selections)
    assert revalidated is not None
    assert revalidated["status"] == "passing"
    assert revalidated["quality_state"] == "warning"


def test_multi_camera_ranking_rejects_companion_disagreement_above_hard_limit(
    tmp_path: Path,
) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    base = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )
    candidates = [
        _multi_camera_candidate_variant(
            base,
            sensor_key=sensor_key,
            pnp_method="ITERATIVE",
            extrinsic_method="park",
            score=0.2,
            companion_translation_offset_mm=offset,
        )
        for sensor_key, offset in (
            ("realsense_d435:1", 0.0),
            ("oak_d_pro:2", 20.01),
        )
    ]
    request_value = _multi_camera_request()
    request_value["pnp_methods"] = ["ITERATIVE"]

    ranking = attempt_module._validate_and_rank(
        tmp_path,
        request_value,
        candidates,
        _multi_camera_intrinsics(),
    )

    assert ranking["status"] == "failed"
    assert ranking["recommended_camera_count"] == 0
    bundle = ranking["multi_camera_consistency"]["bundles"][0]
    assert bundle["status"] == "failed"
    assert bundle["max_pairwise_companion_translation_mm"] == pytest.approx(20.01)
    assert (
        next(
            check
            for check in bundle["checks"]
            if check["name"] == "joint_companion_translation_consistency"
        )["status"]
        == "error"
    )


def test_multi_camera_ranking_fails_closed_when_peer_fails(tmp_path) -> None:
    observations, _expected, _companion = _fixture_observations("eye_in_hand")
    passing = evaluate_extrinsic_candidate(
        observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="realsense_d435:1",
    )
    failing_observations = [dict(item) for item in observations]
    same_pose = dict(failing_observations[0]["robot_ee_pose"])
    for observation in failing_observations:
        observation["robot_ee_pose"] = same_pose
    failing = evaluate_extrinsic_candidate(
        failing_observations,
        mode="eye_in_hand",
        pnp_method="ITERATIVE",
        extrinsic_method="park",
        sensor_key="oak_d_pro:2",
    )
    for candidate in (passing, failing):
        candidate["synchronization"] = {
            "policy": "fixed_zero",
            "status": "fixed_zero",
            "robot_pose_time_offset_ms": 0.0,
            "sync_delta_ms": 0.0,
            "source": (
                "processed/calibration/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
                "time_offset_search.json"
            ),
            "warning_fallback_used": False,
        }
    request_value = _multi_camera_request()
    request_value["pnp_methods"] = ["ITERATIVE"]
    intrinsic = {
        "profile_id": "factory",
        "sensor_id": "1",
        "native": {
            "cam_K": [600.0, 0.0, 320.0, 0.0, 600.0, 240.0, 0.0, 0.0, 1.0],
            "width": 640,
            "height": 480,
            "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "depth": {"scale_to_mm": 1.0},
    }

    ranking = attempt_module._validate_and_rank(
        tmp_path,
        request_value,
        [passing, failing],
        {
            "realsense_d435:1": intrinsic,
            "oak_d_pro:2": {**intrinsic, "sensor_id": "2"},
        },
    )

    assert ranking["status"] == "failed"
    assert ranking["recommended_camera_count"] == 0
    assert ranking["failed_camera_count"] == 2
    assert ranking["results"][0]["recommended_candidate_id"] is None
    assert ranking["results"][1]["recommended_candidate_id"] is None
    consistency = ranking["multi_camera_consistency"]
    assert consistency["status"] == "failed"
    assert consistency["recommended_bundle_id"] is None
    assert consistency["bundles"][0]["candidate_ids"] == {
        "realsense_d435:1": "realsense_d435:1|ITERATIVE|park",
        "oak_d_pro:2": "oak_d_pro:2|ITERATIVE|park",
    }
    assert (
        next(
            check
            for check in consistency["bundles"][0]["checks"]
            if check["name"] == "joint_individual_candidate_validation"
        )["status"]
        == "error"
    )
