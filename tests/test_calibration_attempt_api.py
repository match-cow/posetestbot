from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.calibration import attempts as attempt_module
from posetestbot.calibration.posegridgen import posegridgen_capabilities
from posetestbot.calibration.target_library import (
    generate_target_bundle,
    select_target_bundle,
)
from posetestbot.io.artifacts import (
    ARUCO_DETECTIONS,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    RGB_DIR,
)
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_configs_from_values,
    write_run_config_with_manifest,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH
from posetestbot.web.app import create_app
from posetestbot.web.routes import calibration as routes


@dataclass
class FakeJob:
    id: str
    status: str = "queued"


class FakeRunner:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return FakeJob(id=f"calibrationjob{len(self.submissions)}")


def _configuration() -> dict:
    value = copy.deepcopy(posegridgen_capabilities()["defaults"])
    value["page"]["orientation"] = "landscape"
    value["board"].update({"rows": 2, "columns": 3, "marker_size_mm": 25.0})
    value["annotations"] = {
        "show_ruler": False,
        "show_parameters": False,
        "show_frame_legend": False,
    }
    return value


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(path.as_posix(), np.zeros((8, 8), dtype=np.uint16))


def _write_camera(run_root: Path, name: str) -> None:
    _write_png(run_root / name / RGB_DIR / "1000.png")
    _write_png(run_root / name / DEPTH_DIR / "1000.png")
    (run_root / name / FRAME_METADATA_JSONL).write_text(
        json.dumps(
            {
                "frame_id": "1000.png",
                "host_received_timestamp_ns": 1_000_000_000,
                "sensor_timestamp_ns": 10_000_000_000,
                "color_timestamp_domain": "global_time",
            }
        )
        + "\n"
    )


def _write_robot_pose(
    run_root: Path,
    *,
    reference_path: str = POSE_TEMPLATE_BASE_SUNRISE_PATH,
    relative_path: str = "raw_robot_ee_poses.json",
) -> None:
    destination = run_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "0": {
                    "motion": "pose_0",
                    "framename": 1000,
                    "host_wall_timestamp_ns": 10_000_000_000,
                    "source_packet": {
                        "schema_version": "robot_pose.v1",
                        "from_frame": "robot_flange",
                        "to_frame": "template_base",
                        "sunrise_reference_frame_path": reference_path,
                    },
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


def test_robot_pose_reference_binds_every_selected_source(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    sensors = sensor_configs_from_values(
        [
            {
                "sensor_type": "realsense_d435",
                "device_id": str(index),
                "display_name": f"D435 {index}",
                "mounting_mode": "eye_in_hand",
            }
            for index in range(1, 3)
        ]
    )
    write_run_config_with_manifest(
        run_root,
        create_run_config(
            run_root=run_root,
            sensors=sensors,
            robot_pose_sunrise_reference_frame_path=(POSE_TEMPLATE_BASE_SUNRISE_PATH),
        ),
    )
    paths = [
        "realsense_1/raw_robot_ee_poses.json",
        "realsense_2/raw_robot_ee_poses.json",
    ]
    for path in paths:
        _write_robot_pose(run_root, relative_path=path)
    cameras = [
        {
            "sensor_key": f"realsense_d435:{index}",
            "robot_pose_path": path,
        }
        for index, path in enumerate(paths, start=1)
    ]

    evidence = attempt_module._attempt_robot_pose_reference(run_root, cameras)

    assert evidence["artifacts"] == paths
    assert [item["path"] for item in evidence["artifact_bindings"]] == paths
    request_value = {
        "schema_version": "calibration_attempt_request.v1",
        "sensors": cameras,
        "robot_pose_reference": evidence,
    }
    loaded = attempt_module._verify_robot_pose_artifact_bindings(
        run_root, request_value
    )
    assert set(loaded) == set(paths)

    for field, tampered_value in (
        ("sunrise_reference_frame_path", "/PoseTestBot/TemplateBase"),
        ("pose_counts", {paths[0]: 1, paths[1]: 2}),
    ):
        tampered_request = copy.deepcopy(request_value)
        tampered_request["robot_pose_reference"][field] = tampered_value
        with pytest.raises(
            ValueError,
            match="reference identity or pose counts",
        ):
            attempt_module._verify_robot_pose_artifact_bindings(
                run_root, tampered_request
            )

    second_path = run_root / paths[1]
    changed = json.loads(second_path.read_text())
    changed["0"]["pose"]["Y"] = 1.0
    second_path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="changed after calibration attempt creation"):
        attempt_module._verify_robot_pose_artifact_bindings(run_root, request_value)


@pytest.fixture
def calibration_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run_root = tmp_path / "runs" / "run"
    sensors = [
        {
            "sensor_type": "realsense_d435",
            "device_id": "1",
            "display_name": "Wrist D435",
            "mounting_mode": "eye_in_hand",
        },
        {
            "sensor_type": "oak_d_pro",
            "device_id": "2",
            "display_name": "Wrist OAK-D Pro",
            "mounting_mode": "eye_in_hand",
        },
    ]
    config = create_run_config(
        run_root=run_root,
        sensors=sensor_configs_from_values(sensors),
    )
    write_run_config_with_manifest(run_root, config)
    library = tmp_path / "library"
    bundle = generate_target_bundle(
        display_name="Saved board",
        configuration=_configuration(),
        library_root=library,
    )
    select_target_bundle(
        run_root=run_root,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="template_base",
        library_root=library,
    )
    _write_camera(run_root, "realsense_1")
    _write_camera(run_root, "luxonis_2")
    _write_robot_pose(run_root)
    runner = FakeRunner()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", (tmp_path / "runs").as_posix())
    monkeypatch.setattr(attempt_module, "default_target_library_root", lambda: library)
    monkeypatch.setattr(routes, "job_runner", runner)
    return create_app().test_client(), runner, run_root, bundle, library


def test_setup_exposes_exact_two_modes_ready_cameras_targets_and_defaults(
    calibration_client,
) -> None:
    client, _runner, run_root, bundle, _library = calibration_client

    response = client.get(
        "/calibration/setup", query_string={"run_root": run_root.as_posix()}
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert [item["id"] for item in payload["modes"]] == [
        "eye_in_hand",
        "eye_to_hand",
    ]
    assert {item["sensor_key"] for item in payload["cameras"]} == {
        "realsense_d435:1",
        "oak_d_pro:2",
    }
    assert payload["saved_targets"][0]["target_id"] == bundle["target_id"]
    assert payload["saved_targets"][0]["selected"] is True
    assert payload["saved_targets"][0]["selected_placement"] == {
        "mode": "unknown",
        "mounting_frame": "template_base",
    }
    assert payload["solver"]["default_policy"] == "auto_compare"
    assert payload["solver"]["default_pnp_methods"] == [
        "IPPE",
        "ITERATIVE",
        "SQPNP",
    ]
    assert payload["solver"]["intrinsics_policy"] == ("compare_factory_opencv")
    assert [item["id"] for item in payload["solver"]["intrinsics_policies"]] == [
        "compare_factory_opencv",
        "reuse_compatible_or_factory",
    ]
    synchronization = payload["solver"]["synchronization"]
    assert synchronization["implementation_revision"] == (
        attempt_module.TIME_OFFSET_IMPLEMENTATION_REVISION
    )
    assert synchronization["default_policy"] == "auto_offset"
    assert [item["id"] for item in synchronization["policies"]] == [
        "auto_offset",
        "fixed_zero",
    ]
    assert synchronization["search"]["minimum_robot_pose_time_offset_ms"] == -300.0
    assert synchronization["search"]["maximum_robot_pose_time_offset_ms"] == 300.0
    assert synchronization["search"]["step_ms"] == 5.0
    assert synchronization["search"]["max_nearest_pose_delta_ms"] == 150.0
    assert synchronization["search"]["warning_nearest_pose_delta_ms"] == 20.0
    assert (
        synchronization["search"]["warning_absolute_robot_pose_time_offset_ms"] == 150.0
    )
    assert (
        synchronization["search"]["time_offset_failure_policy"]
        == "warn_keep_zero"
    )
    assert (
        synchronization["search"]["minimum_motion_count_per_cross_validation_fold"] == 4
    )
    assert (
        synchronization["search"][
            "maximum_leave_one_motion_out_search_adjusted_sign_p_value"
        ]
        == 0.05
    )
    assert synchronization["sign_convention"]["conversion"] == (
        "sync_delta_ms = -robot_pose_time_offset_ms"
    )
    assert payload["solver"]["thresholds"] == {
        "min_inliers": 6,
        "min_pnp_common_inliers": 12,
        "min_pnp_common_inlier_ratio": 0.5,
        "max_pnp_all_point_mean_reprojection_error_px": 3.0,
        "min_pnp_supported_markers": 4,
        "min_pnp_supported_corners_per_marker": 3,
        "min_pnp_grid_rows": 2,
        "min_pnp_grid_columns": 2,
        "min_pnp_clutter_supported_markers": 8,
        "min_pnp_clutter_grid_rows": 3,
        "min_pnp_clutter_grid_columns": 3,
        "min_target_marker_coverage_ratio": 0.5,
        "min_target_row_coverage_ratio": 0.6,
        "min_target_column_coverage_ratio": 0.6,
        "min_accepted_views": 15,
        "min_coverage_cells": 6,
        "image_coverage_tail_support_views": 5,
        "min_image_centroid_x_span_ratio": 0.45,
        "min_image_centroid_y_span_ratio": 0.35,
        "min_image_centroid_hull_area_ratio": 0.1,
        "image_coverage_by_mode": {
            "eye_in_hand": {
                "image_coverage_tail_support_views": 5,
                "min_image_centroid_x_span_ratio": 0.45,
                "min_image_centroid_y_span_ratio": 0.35,
                "min_image_centroid_hull_area_ratio": 0.1,
            },
            "eye_to_hand": {
                "image_coverage_tail_support_views": 5,
                "min_image_centroid_x_span_ratio": 0.15,
                "min_image_centroid_y_span_ratio": 0.2,
                "min_image_centroid_hull_area_ratio": 0.03,
            },
        },
        "max_per_view_reprojection_error_px": 3.0,
        "max_intrinsic_rms_reprojection_error_px": 1.5,
        "min_motion_poses": 4,
        "min_translation_span_mm": 20.0,
        "min_rotation_span_deg": 5.0,
        "min_rotation_axis_angle_deg": 2.0,
        "min_rotation_axis_second_to_first_ratio": 0.15,
        "max_observations_per_motion": 5,
        "max_nearest_pose_delta_ms": 150.0,
        "warning_nearest_pose_delta_ms": 20.0,
        "max_mean_translation_mm": 10.0,
        "max_mean_rotation_deg": 5.0,
        "max_outlier_ratio": 0.25,
    }


def test_setup_rejects_camera_without_frame_timestamp_evidence(
    calibration_client,
) -> None:
    client, _runner, run_root, _bundle, _library = calibration_client
    (run_root / "realsense_1" / FRAME_METADATA_JSONL).unlink()

    response = client.get(
        "/calibration/setup", query_string={"run_root": run_root.as_posix()}
    )
    payload = response.get_json()

    assert response.status_code == 200
    unavailable = next(
        item
        for item in payload["unavailable_cameras"]
        if item["sensor_key"] == "realsense_d435:1"
    )
    assert "missing frame timestamp evidence" in unavailable["errors"]


def test_setup_and_attempt_submission_exclude_disabled_camera(
    calibration_client,
) -> None:
    client, runner, run_root, bundle, _library = calibration_client
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    oak = next(
        sensor for sensor in config["capture"]["sensors"] if sensor["device_id"] == "2"
    )
    oak["enabled"] = False
    oak["calibration_profile_id"] = "preserved-profile-id"
    config_path.write_text(json.dumps(config))

    setup = client.get(
        "/calibration/setup", query_string={"run_root": run_root.as_posix()}
    ).get_json()
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["oak_d_pro:2"],
            "target_id": bundle["target_id"],
        },
    )

    assert [camera["sensor_key"] for camera in setup["cameras"]] == ["realsense_d435:1"]
    assert setup["unavailable_cameras"] == []
    assert response.status_code == 400
    assert "Unknown sensor key(s)" in response.get_json()["output"]
    assert runner.submissions == []
    persisted = json.loads(config_path.read_text())["capture"]["sensors"][1]
    assert persisted["device_id"] == "2"
    assert persisted["calibration_profile_id"] == "preserved-profile-id"


def test_attempt_submission_scopes_exact_cameras_and_queues_cpu_disk_parent_job(
    calibration_client,
) -> None:
    client, runner, run_root, bundle, _library = calibration_client

    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["oak_d_pro:2"],
            "target_id": bundle["target_id"],
        },
    )
    payload = response.get_json()

    assert response.status_code == 202
    assert payload["attempt_id"]
    assert payload["job_id"] == "calibrationjob1"
    submission = runner.submissions[0]
    assert submission["resources"] == ["cpu", "disk_io"]
    assert submission["scope_kind"] == "run"
    assert submission["run_root"] == run_root.as_posix()
    assert submission["parameters"]["sensor_keys"] == ["oak_d_pro:2"]
    assert submission["command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_calibration_attempt.py",
    ]
    request_path = (
        run_root / "processed" / "calibration" / payload["attempt_id"] / "request.json"
    )
    saved = json.loads(request_path.read_text())
    assert saved["mode"] == "eye_in_hand"
    assert saved["sensor_keys"] == ["oak_d_pro:2"]
    assert [item["sensor_key"] for item in saved["sensors"]] == ["oak_d_pro:2"]
    assert saved["target_mounting"]["to"] == "template_base"
    assert saved["synchronization_policy"] == "auto_offset"
    assert saved["synchronization_search"] == (
        attempt_module.time_offset_search_configuration()
    )
    assert saved["synchronization_implementation_revision"] == (
        attempt_module.TIME_OFFSET_IMPLEMENTATION_REVISION
    )


def test_all_static_three_camera_attempt_uses_robot_mounted_unknown_target(
    calibration_client,
) -> None:
    client, runner, fixture_run, bundle, library = calibration_client
    run_root = fixture_run.parent / "three-static"
    sensors = sensor_configs_from_values(
        [
            {
                "sensor_type": "realsense_d435",
                "device_id": str(index),
                "display_name": f"Static D435 {index}",
                "mounting_mode": "static",
            }
            for index in range(1, 4)
        ]
    )
    write_run_config_with_manifest(
        run_root,
        create_run_config(
            run_root=run_root,
            sensors=sensors,
            robot_pose_sunrise_reference_frame_path=(POSE_TEMPLATE_BASE_SUNRISE_PATH),
        ),
    )
    select_target_bundle(
        run_root=run_root,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="robot_flange",
        library_root=library,
    )
    for index in range(1, 4):
        _write_camera(run_root, f"realsense_{index}")
    _write_robot_pose(run_root)

    setup = client.get(
        "/calibration/setup",
        query_string={"run_root": run_root.as_posix()},
    ).get_json()
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_to_hand",
            "sensor_keys": [
                "realsense_d435:1",
                "realsense_d435:2",
                "realsense_d435:3",
            ],
            "target_id": bundle["target_id"],
        },
    )

    assert setup["saved_targets"][0]["selected_placement"] == {
        "mode": "unknown",
        "mounting_frame": "robot_flange",
    }
    assert response.status_code == 202
    saved = json.loads(
        (
            run_root
            / "processed"
            / "calibration"
            / response.get_json()["attempt_id"]
            / "request.json"
        ).read_text()
    )
    assert saved["sensor_keys"] == [
        "realsense_d435:1",
        "realsense_d435:2",
        "realsense_d435:3",
    ]
    assert saved["target_mounting"] == {
        "from": "aruco_grid",
        "to": "robot_flange",
        "state": "estimated",
    }
    assert saved["target_bundle"]["selection"]["placement"] == {
        "mode": "unknown",
        "mounting_frame": "robot_flange",
    }
    assert saved["robot_pose_reference"] == {
        "schema_version": "robot_pose_reference.v1",
        "status": "verified",
        "packet_schema_version": "robot_pose.v1",
        "from": "robot_flange",
        "to": "template_base",
        "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
        "artifacts": ["raw_robot_ee_poses.json"],
        "pose_counts": {"raw_robot_ee_poses.json": 1},
        "artifact_bindings": [
            {
                "path": "raw_robot_ee_poses.json",
                "size_bytes": len((run_root / "raw_robot_ee_poses.json").read_bytes()),
                "sha256": hashlib.sha256(
                    (run_root / "raw_robot_ee_poses.json").read_bytes()
                ).hexdigest(),
            }
        ],
    }
    assert runner.submissions[-1]["parameters"]["sensor_keys"] == saved["sensor_keys"]


def test_attempt_worker_rejects_robot_pose_mutation_after_request_creation(
    calibration_client,
) -> None:
    client, _runner, run_root, bundle, _library = calibration_client
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
            "synchronization_policy": "fixed_zero",
        },
    )
    assert response.status_code == 202
    attempt_id = response.get_json()["attempt_id"]
    request_path = run_root / "processed" / "calibration" / attempt_id / "request.json"
    request_value = json.loads(request_path.read_text())
    binding = request_value["robot_pose_reference"]["artifact_bindings"][0]
    assert binding["path"] == "raw_robot_ee_poses.json"

    robot_path = run_root / "raw_robot_ee_poses.json"
    changed = json.loads(robot_path.read_text())
    changed["0"]["pose"]["X"] = 1.0
    robot_path.write_text(json.dumps(changed))

    with pytest.raises(
        ValueError,
        match="Raw robot-pose artifact changed after calibration attempt creation",
    ):
        attempt_module.run_calibration_attempt(run_root, attempt_id)

    progress = json.loads(
        (
            run_root / "processed" / "calibration" / attempt_id / "progress.json"
        ).read_text()
    )
    assert progress["status"] == "failed"
    assert "changed after calibration attempt creation" in progress["message"]


@pytest.mark.parametrize(
    ("configured_path", "captured_path", "expected_message"),
    [
        (
            "/PoseTestBot/TemplateBase",
            "/PoseTestBot/TemplateBase",
            "Static camera calibration must declare",
        ),
        (
            POSE_TEMPLATE_BASE_SUNRISE_PATH,
            "/PoseTestBot/TemplateBase",
            "does not match run config",
        ),
    ],
)
def test_static_attempt_rejects_non_pose_template_base_robot_pose_reference(
    calibration_client,
    configured_path: str,
    captured_path: str,
    expected_message: str,
) -> None:
    client, runner, fixture_run, bundle, library = calibration_client
    run_root = fixture_run.parent / (
        "wrong-static-reference-" + configured_path.rsplit("/", 1)[-1]
    )
    sensors = sensor_configs_from_values(
        [
            {
                "sensor_type": "realsense_d435",
                "device_id": "1",
                "display_name": "Static D435",
                "mounting_mode": "static",
            }
        ]
    )
    write_run_config_with_manifest(
        run_root,
        create_run_config(
            run_root=run_root,
            sensors=sensors,
            robot_pose_sunrise_reference_frame_path=configured_path,
        ),
    )
    select_target_bundle(
        run_root=run_root,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="robot_flange",
        library_root=library,
    )
    _write_camera(run_root, "realsense_1")
    _write_robot_pose(run_root, reference_path=captured_path)

    before = len(runner.submissions)
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_to_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
        },
    )

    assert response.status_code == 400
    assert expected_message in response.get_json()["output"]
    assert POSE_TEMPLATE_BASE_SUNRISE_PATH in response.get_json()["output"]
    assert len(runner.submissions) == before


def test_static_attempt_rejects_known_template_base_target_selection(
    calibration_client,
) -> None:
    client, runner, fixture_run, bundle, library = calibration_client
    run_root = fixture_run.parent / "inconsistent-static"
    eye_in_hand = sensor_configs_from_values(
        [
            {
                "sensor_type": "realsense_d435",
                "device_id": "1",
                "display_name": "D435",
                "mounting_mode": "eye_in_hand",
            }
        ]
    )
    write_run_config_with_manifest(
        run_root,
        create_run_config(
            run_root=run_root,
            sensors=eye_in_hand,
            robot_pose_sunrise_reference_frame_path=(POSE_TEMPLATE_BASE_SUNRISE_PATH),
        ),
    )
    select_target_bundle(
        run_root=run_root,
        target_id=bundle["target_id"],
        placement_mode="template_base_identity",
        mounting_frame="template_base",
        library_root=library,
    )
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    config["capture"]["sensors"][0]["mounting_mode"] = "static"
    config_path.write_text(json.dumps(config))
    _write_camera(run_root, "realsense_1")
    _write_robot_pose(run_root)

    before = len(runner.submissions)
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_to_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
        },
    )

    assert response.status_code == 400
    assert (
        "unknown target placement mounted on robot_flange"
        in response.get_json()["output"]
    )
    assert len(runner.submissions) == before


def test_attempt_rejects_legacy_target_selection_without_mounting_frame(
    calibration_client,
) -> None:
    client, runner, fixture_run, bundle, library = calibration_client
    run_root = fixture_run.parent / "legacy-target-mounting"
    sensors = sensor_configs_from_values(
        [
            {
                "sensor_type": "realsense_d435",
                "device_id": "1",
                "display_name": "Static D435",
                "mounting_mode": "static",
            }
        ]
    )
    write_run_config_with_manifest(
        run_root,
        create_run_config(
            run_root=run_root,
            sensors=sensors,
            robot_pose_sunrise_reference_frame_path=(POSE_TEMPLATE_BASE_SUNRISE_PATH),
        ),
    )
    select_target_bundle(
        run_root=run_root,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        library_root=library,
    )
    _write_camera(run_root, "realsense_1")
    _write_robot_pose(run_root)

    setup = client.get(
        "/calibration/setup",
        query_string={"run_root": run_root.as_posix()},
    ).get_json()
    before = len(runner.submissions)
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_to_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
        },
    )

    assert setup["saved_targets"][0]["selected_placement"] == {"mode": "unknown"}
    assert response.status_code == 400
    assert "mounting_frame is missing" in response.get_json()["output"]
    assert "explicitly reselect" in response.get_json()["output"]
    assert len(runner.submissions) == before


def test_attempt_validation_rejects_identity_methods_and_target_conflicts(
    calibration_client,
) -> None:
    client, runner, run_root, bundle, library = calibration_client
    invalid_sensor = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:not-this-camera"],
            "target_id": bundle["target_id"],
        },
    )
    unsuitable_method = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
            "pnp_methods": ["IPPE_SQUARE"],
        },
    )
    duplicate_method = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
            "pnp_methods": ["IPPE", "IPPE"],
        },
    )
    mounting_mismatch = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_to_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
        },
    )
    escaped_root = client.get(
        "/calibration/setup",
        query_string={"run_root": (run_root.parents[1] / "outside").as_posix()},
    )

    assert invalid_sensor.status_code == 400
    assert "Unknown sensor key" in invalid_sensor.get_json()["output"]
    assert unsuitable_method.status_code == 400
    assert "unsupported fields: pnp_methods" in unsuitable_method.get_json()["output"]
    assert duplicate_method.status_code == 400
    assert "unsupported fields: pnp_methods" in duplicate_method.get_json()["output"]
    assert mounting_mismatch.status_code == 400
    assert (
        "requires cameras configured as static"
        in mounting_mismatch.get_json()["output"]
    )
    assert escaped_root.status_code == 400
    assert "allowed root" in escaped_root.get_json()["output"]
    assert runner.submissions == []

    second = generate_target_bundle(
        display_name="Different board",
        configuration={
            **_configuration(),
            "print_compensation": {"x_percent": 99.0, "y_percent": 100.0},
        },
        library_root=library,
    )
    blocker = run_root / "processed" / "synchronized" / "realsense_1" / ARUCO_DETECTIONS
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("{}\n")
    conflict = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": second["target_id"],
        },
    )

    assert conflict.status_code == 409
    assert (
        "processed/synchronized/realsense_1/aruco_detections.json"
        in (conflict.get_json()["blockers"])
    )
    assert "raw_robot_ee_poses.json" in conflict.get_json()["blockers"]


def test_attempt_history_is_immutable_and_promotion_accepts_partial_results(
    calibration_client,
) -> None:
    client, runner, run_root, bundle, _library = calibration_client
    payloads = []
    for mode, sensor_key in (
        ("eye_in_hand", "realsense_d435:1"),
        ("eye_in_hand", "oak_d_pro:2"),
    ):
        response = client.post(
            "/calibration/attempts",
            json={
                "run_root": run_root.as_posix(),
                "mode": mode,
                "sensor_keys": [sensor_key],
                "target_id": bundle["target_id"],
            },
        )
        assert response.status_code == 202
        payloads.append(response.get_json())
    first_id, second_id = (item["attempt_id"] for item in payloads)
    history = client.get(
        "/calibration/attempts", query_string={"run_root": run_root.as_posix()}
    ).get_json()["attempts"]
    assert {item["attempt_id"] for item in history} == {first_id, second_id}
    first_request = json.loads(
        (run_root / "processed" / "calibration" / first_id / "request.json").read_text()
    )
    assert first_request["mode"] == "eye_in_hand"
    # This fixture fabricates an old completed ranking rather than running the
    # current five-phase job. Mark its request as historical so promotion does
    # not accept it as a new report-backed fixed-zero attempt.
    first_request.pop("synchronization_policy")
    (run_root / "processed" / "calibration" / first_id / "request.json").write_text(
        json.dumps(first_request)
    )

    attempt_root = run_root / "processed" / "calibration" / first_id
    progress = json.loads((attempt_root / "progress.json").read_text())
    progress["status"] = "complete"
    (attempt_root / "progress.json").write_text(json.dumps(progress))
    candidate_id = "realsense_d435:1|IPPE|tsai"
    (attempt_root / "ranking.json").write_text(
        json.dumps(
            {
                "status": "partial",
                "recommended_camera_count": 1,
                "failed_camera_count": 1,
                "results": [
                    {
                        "sensor_key": "realsense_d435:1",
                        "recommended_candidate_id": candidate_id,
                        "candidates": [
                            {
                                "candidate_id": candidate_id,
                                "status": "passing",
                            }
                        ],
                    },
                    {
                        "sensor_key": "oak_d_pro:2",
                        "recommended_candidate_id": None,
                        "candidates": [],
                    },
                ],
            }
        )
    )
    promoted = client.post(
        f"/calibration/attempts/{first_id}/promote",
        json={
            "run_root": run_root.as_posix(),
            "candidate_ids": {"realsense_d435:1": candidate_id},
        },
    )

    assert promoted.status_code == 202
    assert promoted.get_json()["selections"] == {"realsense_d435:1": candidate_id}
    assert runner.submissions[-1]["resources"] == ["cpu", "disk_io"]
    assert runner.submissions[-1]["scope_kind"] == "run"
    assert runner.submissions[-1]["run_root"] == run_root.as_posix()
    assert runner.submissions[-1]["command"][-1] == "--promote"


def test_parent_queue_conflict_is_recorded_on_the_immutable_attempt(
    calibration_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _runner, run_root, bundle, _library = calibration_client

    class BusyRunner:
        def submit(self, **_kwargs):
            raise ResourceBusyError("Requested resources are busy: cpu")

    monkeypatch.setattr(routes, "job_runner", BusyRunner())
    response = client.post(
        "/calibration/attempts",
        json={
            "run_root": run_root.as_posix(),
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:1"],
            "target_id": bundle["target_id"],
        },
    )

    assert response.status_code == 409
    history = client.get(
        "/calibration/attempts", query_string={"run_root": run_root.as_posix()}
    ).get_json()["attempts"]
    assert len(history) == 1
    attempt = client.get(
        f"/calibration/attempts/{history[0]['attempt_id']}",
        query_string={"run_root": run_root.as_posix()},
    ).get_json()
    assert attempt["progress"]["status"] == "failed"
    assert attempt["progress"]["failure_stage"] == "job_submission"


def test_camera_discovery_rejects_escaped_folders_and_all_duplicate_identities(
    calibration_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, _runner, run_root, _bundle, _library = calibration_client
    outside = run_root.parents[1] / "outside-camera"
    monkeypatch.setattr(
        attempt_module,
        "discover_sensor_records",
        lambda _root: [
            {
                "folder": "realsense_1",
                "sensor_type": "realsense_d435",
                "device_id": "1",
            },
            {
                "folder": "realsense_duplicate",
                "sensor_type": "realsense_d435",
                "device_id": "1",
            },
            {
                "folder": outside.as_posix(),
                "sensor_type": "oak_d_pro",
                "device_id": "2",
            },
        ],
    )

    cameras = attempt_module.discover_calibration_cameras(run_root)

    duplicates = [item for item in cameras if item["sensor_key"] == "realsense_d435:1"]
    assert len(duplicates) == 2
    assert all(not item["data_ready"] for item in duplicates)
    assert all(
        "duplicate stable sensor identity" in item["errors"] for item in duplicates
    )
    escaped = next(item for item in cameras if item["sensor_key"] == "oak_d_pro:2")
    assert escaped["data_ready"] is False
    assert "captured sensor folder escapes the run root" in escaped["errors"]
