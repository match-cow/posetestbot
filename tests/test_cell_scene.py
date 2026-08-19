from __future__ import annotations

import json


from dataclasses import replace

from pathlib import Path

import cv2

import numpy as np

import pytest

from posetestbot.calibration.profiles import (
    SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    RigidTransform,
    TransformFrame,
    write_profile_collection,
)
from posetestbot.calibration.targets import (
    DEFAULT_TARGET_SPEC,
    normalize_calibration_target_spec,
)

from posetestbot.cell.scene import (
    _pose_template_footprint,
    build_cell_scene,
    cell_timeline_page,
)

from posetestbot.io.artifacts import BOP_DIR, BOP_EXPORT_MANIFEST

from posetestbot.pipeline.run_config import (
    FixedFrameTransform,
    create_run_config,
    sensor_config_from_token,
    write_run_config,
)

from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH

from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType

from posetestbot.web.app import create_app


def profile(
    sensor_id: str,
    mounting: MountingMode,
    *,
    profile_id: str | None = None,
    rig_position: str | None = None,
    rotation_quaternion_wxyz: tuple[float, float, float, float] = (1, 0, 0, 0),
    translation_mm: tuple[float, float, float] = (10, 20, 30),
) -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=SCHEMA_VERSION,
        profile_id=profile_id or f"realsense_{sensor_id}_{mounting.value}",
        sensor_id=sensor_id,
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=mounting,
        rig_position=rig_position or f"slot_{sensor_id}",
        intrinsics=CameraIntrinsics(
            cam_k=(600, 0, 320, 0, 600, 240, 0, 0, 1),
            width=640,
            height=480,
        ),
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=(
                TransformFrame.ROBOT_FLANGE
                if mounting == MountingMode.EYE_IN_HAND
                else TransformFrame.TEMPLATE_BASE
            ),
            rotation_quaternion_wxyz=rotation_quaternion_wxyz,
            translation_mm=translation_mm,
        ),
        calibration_dataset_id="attempt-dataset-1",
        method="auto_compare:IPPE+park",
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(
            num_observations=8,
            num_inliers=7,
            mean_reprojection_error_px=0.25,
            residual_translation_mm=0.5,
            residual_rotation_deg=0.2,
        ),
        operator="cell-test",
        calibrated_at="2026-07-21T12:00:00+00:00",
        metadata={
            "target_id": "target-1",
            "intrinsic_profile_id": "intrinsic-1",
            "promotion_attempt_id": "a" * 32,
            "promotion_candidate_id": "candidate-1",
            "promotion_multi_camera_bundle_id": "joint:IPPE:park",
            "promotion_solver_provenance": {
                "solver_policy": "auto_compare",
                "pnp_method": "IPPE",
                "extrinsic_method": "park",
            },
            "promoted_at": "2026-07-21T12:00:00+00:00",
            "promoted_by": "cell-test",
            "outlier_count": 1,
            "outlier_ratio": 0.125,
            **(
                {
                    "robot_pose_reference": {
                        "schema_version": "robot_pose_reference.v1",
                        "status": "verified",
                        "packet_schema_version": "robot_pose.v1",
                        "from": "robot_flange",
                        "to": "template_base",
                        "sunrise_reference_frame_path": (
                            POSE_TEMPLATE_BASE_SUNRISE_PATH
                        ),
                    }
                }
                if mounting == MountingMode.STATIC
                else {}
            ),
            "companion_transform": {
                "from": "aruco_grid",
                "to": (
                    "template_base"
                    if mounting == MountingMode.EYE_IN_HAND
                    else "robot_flange"
                ),
                "matrix": [
                    [1, 0, 0, 1],
                    [0, 1, 0, 2],
                    [0, 0, 1, 3],
                    [0, 0, 0, 1],
                ],
                "rotation_quaternion_wxyz": [1, 0, 0, 0],
                "translation_mm": [1, 2, 3],
            },
        },
    )


def make_scene_run(tmp_path: Path) -> Path:
    profiles_path = tmp_path / "profiles.json"
    write_profile_collection(
        [
            profile(
                "111",
                MountingMode.EYE_IN_HAND,
                profile_id="promoted_wrist_111",
            ),
            profile(
                "111",
                MountingMode.EYE_IN_HAND,
                profile_id="older_wrist_111",
                rig_position="legacy_wrist_slot",
            ),
            profile(
                "111",
                MountingMode.STATIC,
                profile_id="static_profile_111",
                rig_position="static_slot",
            ),
            profile(
                "222",
                MountingMode.EYE_IN_HAND,
                profile_id="wrong_wrist_222",
                rig_position="other_wrist_slot",
            ),
            profile("222", MountingMode.STATIC),
        ],
        profiles_path,
    )
    run_root = tmp_path / "run"
    wrist = replace(
        sensor_config_from_token("realsense_d435:111:eye_in_hand:Wrist camera"),
        calibration_profile_id="promoted_wrist_111",
    )
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        calibration_profiles=profiles_path.as_posix(),
        sensors=(
            wrist,
            sensor_config_from_token("realsense_d435:222:static:Static camera"),
        ),
        fixed_transforms=(
            FixedFrameTransform(
                "physical_robot_base", "template_base", (1, 0, 0, 0), (100, 0, 0)
            ),
            FixedFrameTransform("tcp", "robot_flange", (1, 0, 0, 0), (0, 0, 120)),
        ),
    )
    write_run_config(run_root, config)
    for sensor in ("realsense_111", "realsense_222"):
        folder = run_root / "processed" / "synchronized" / sensor
        folder.mkdir(parents=True)
        sensor_id = sensor.removeprefix("realsense_")
        (folder / "frame_metadata.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "schema_version": "frame_metadata.v1",
                        "sensor_type": "realsense_d435",
                        "sensor_id": sensor_id,
                        "frame_index": index,
                        "frame_id": f"{index:06d}.png",
                        "rgb_path": f"rgb/{index:06d}.png",
                        "depth_path": f"depth/{index:06d}.png",
                        "sensor_timestamp_ns": 1_000_000_000 + index,
                        "host_received_timestamp_ns": 2_000_000_000 + index,
                        "host_wall_timestamp_ns": 3_000_000_000 + index,
                        "sync_timestamp_ns": 2_000_000_000 + index,
                        "image_rotation_degrees": 0,
                        "orientation": "normal",
                    }
                )
                + "\n"
                for index in range(3)
            )
        )
        (folder / "match_robot_ee_poses.json").write_text(
            json.dumps(
                {
                    f"{index:06d}.png": {
                        "motion": "arc",
                        "image_timestamp_ns": 2_000_000_000 + index,
                        "timestamp_source": "host_received",
                        "source_packet": {
                            "schema_version": "robot_pose.v1",
                            "packet_kind": "pose",
                            "run_id": config.run_id,
                            "from_frame": "robot_flange",
                            "to_frame": "template_base",
                            "sunrise_reference_frame_path": (
                                POSE_TEMPLATE_BASE_SUNRISE_PATH
                            ),
                        },
                        "robot_ee_pose": {
                            "X": index,
                            "Y": 2,
                            "Z": 3,
                            "A": 0,
                            "B": 0,
                            "C": 0,
                        },
                    }
                    for index in range(3)
                }
            )
        )
    (run_root / "calibration_target.json").write_text(
        json.dumps(
            normalize_calibration_target_spec(
                {
                    **DEFAULT_TARGET_SPEC,
                    "placement": {
                        "from": "aruco_grid",
                        "to": "template_base",
                        "rotation_quaternion_wxyz": [1, 0, 0, 0],
                        "translation_mm": [5, 6, 7],
                    },
                }
            )
        )
    )
    return run_root


def write_wrist_run_config(
    run_root: Path,
    calibration_profiles: str,
    *,
    profile_id: str = "wrist_111",
) -> None:
    wrist = replace(
        sensor_config_from_token("realsense_d435:111:eye_in_hand:Wrist camera"),
        calibration_profile_id=profile_id,
    )
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            calibration_profiles=calibration_profiles,
            sensors=(wrist,),
        ),
    )


def test_scene_composes_frames_sensors_and_exact_timelines(tmp_path: Path) -> None:
    run_root = make_scene_run(tmp_path)
    scene = build_cell_scene(run_root)
    entities = {entity["id"]: entity for entity in scene["entities"]}

    assert scene["schema_version"] == "cell_scene.v1"
    assert scene["coordinate_system"]["up_axis"] == "-Z"
    presentation = scene["coordinate_system"]["presentation"]
    assert presentation["mode"] == "calibration_target_front"
    assert presentation["presentation_only"] is True
    assert presentation["target_frame"] == {
        "name": "aruco_grid",
        "origin": "compensated_outer_board_top_left",
        "axes": {"x": "right", "y": "down", "z": "into_board"},
    }
    target_to_reference = np.eye(4)
    target_to_reference[:3, 3] = [5, 6, 7]
    target_to_display = np.asarray(presentation["matrix"]) @ target_to_reference
    assert np.allclose(target_to_display, np.diag([1, -1, -1, 1]))
    assert np.linalg.det(target_to_display[:3, :3]) == pytest.approx(1)
    assert np.allclose(
        target_to_display @ np.asarray([0, 0, -500, 1]),
        [0, 0, 500, 1],
    )
    assert entities["physical_robot_base"]["transform"]["translation_mm"] == [
        100.0,
        0.0,
        0.0,
    ]
    assert entities["tcp"]["transform"]["parent_frame"] == "robot_flange"
    assert (
        entities["camera:realsense_111"]["transform"]["parent_frame"] == "robot_flange"
    )
    assert (
        entities["camera:realsense_222"]["transform"]["parent_frame"] == "template_base"
    )
    wrist_calibration = entities["camera:realsense_111"]["calibration"]
    assert wrist_calibration["profile_id"] == "promoted_wrist_111"
    assert wrist_calibration["status"] == "valid"
    assert wrist_calibration["extrinsics"] == {
        "from": "camera",
        "to": "robot_flange",
        "matrix": [
            [1.0, 0.0, 0.0, 10.0],
            [0.0, 1.0, 0.0, 20.0],
            [0.0, 0.0, 1.0, 30.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        "rotation_quaternion_wxyz": [1, 0, 0, 0],
        "translation_mm": [10, 20, 30],
    }
    assert wrist_calibration["companion_transform"] == {
        "from": "aruco_grid",
        "to": "template_base",
        "matrix": [
            [1.0, 0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0, 2.0],
            [0.0, 0.0, 1.0, 3.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        "rotation_quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
        "translation_mm": [1.0, 2.0, 3.0],
    }
    assert wrist_calibration["quality"]["num_inliers"] == 7
    assert wrist_calibration["quality"]["outlier_count"] == 1
    assert (
        wrist_calibration["evidence"]["profile_source"]
        == (run_root.parent / "profiles.json").as_posix()
    )
    assert wrist_calibration["evidence"]["promotion_attempt_id"] == "a" * 32
    assert wrist_calibration["evidence"]["promotion_multi_camera_bundle_id"] == (
        "joint:IPPE:park"
    )
    assert wrist_calibration["evidence"]["promotion_solver_provenance"] == {
        "solver_policy": "auto_compare",
        "pnp_method": "IPPE",
        "extrinsic_method": "park",
    }
    assert not any(entity["type"] == "object" for entity in scene["entities"])
    assert scene["object_selection"]["dataset_mode"] == "objectless"
    assert entities["calibration_target"]["transform"]["translation_mm"] == [
        5.0,
        6.0,
        7.0,
    ]
    assert (
        entities["calibration_target"]["geometry"]["frame"]
        == (presentation["target_frame"])
    )
    assert len(scene["timelines"]) == 2
    assert [pose["index"] for pose in scene["trajectory_preview"]] == [0, 1, 2]
    assert scene["object_selection"]["bop_export"]["status"] == "not_exported"

    timeline = cell_timeline_page(
        run_root, scene["default_timeline_id"], offset=1, limit=5000
    )
    assert timeline["schema_version"] == "cell_timeline.v1"
    assert timeline["limit"] == 2000
    assert [pose["frame_id"] for pose in timeline["poses"]] == [
        "000001.png",
        "000002.png",
    ]
    assert timeline["poses"][0]["transform"]["translation_mm"] == [1.0, 2.0, 3.0]


def test_cell_timeline_rejects_missing_current_timestamp_evidence(
    tmp_path: Path,
) -> None:
    run_root = make_scene_run(tmp_path)
    matched_path = (
        run_root
        / "processed"
        / "synchronized"
        / "realsense_111"
        / "match_robot_ee_poses.json"
    )
    matched = json.loads(matched_path.read_text())
    matched["000000.png"].pop("image_timestamp_ns")
    matched_path.write_text(json.dumps(matched))

    with pytest.raises(ValueError, match="lacks image_timestamp_ns"):
        build_cell_scene(run_root)


def test_static_camera_scene_stays_in_pose_template_base_and_tracks_target(
    tmp_path: Path,
) -> None:
    run_root = make_scene_run(tmp_path)
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    config["capture"]["sensors"] = [
        sensor
        for sensor in config["capture"]["sensors"]
        if sensor["device_id"] == "222"
    ]
    config_path.write_text(json.dumps(config))
    target_path = run_root / "calibration_target.json"
    target = json.loads(target_path.read_text())
    target.pop("placement", None)
    target_path.write_text(json.dumps(target))

    scene = build_cell_scene(run_root)
    entities = {entity["id"]: entity for entity in scene["entities"]}

    assert scene["coordinate_system"]["reference_frame"] == "template_base"
    assert scene["coordinate_system"]["reference_frame_label"] == "PoseTemplateBase"
    assert scene["coordinate_system"]["sunrise_reference_frame_path"] == (
        POSE_TEMPLATE_BASE_SUNRISE_PATH
    )
    assert scene["coordinate_system"]["up_axis"] == "+Z"
    assert scene["coordinate_system"]["presentation"]["mode"] == "reference_z_up"
    assert scene["coordinate_system"]["presentation"]["matrix"] == np.eye(4).tolist()
    assert entities["camera:realsense_222"]["transform"]["parent_frame"] == (
        "template_base"
    )
    assert entities["calibration_target"]["transform"]["parent_frame"] == (
        "robot_flange"
    )
    assert scene["trajectory"] == {
        "entity_id": "calibration_target",
        "label": "Calibration target",
        "reference_frame": "template_base",
        "reference_frame_label": "PoseTemplateBase",
        "source_timeline_id": "sensor:realsense_222",
        "derivation": (
            "promoted_calibration_target_to_robot_flange_composed_with_"
            "recorded_robot_flange_to_template_base"
        ),
    }
    assert [
        pose["transform"]["translation_mm"] for pose in scene["trajectory_preview"]
    ] == [[1.0, 4.0, 6.0], [2.0, 4.0, 6.0], [3.0, 4.0, 6.0]]

    timeline = cell_timeline_page(run_root, scene["default_timeline_id"])
    assert timeline["poses"][0]["transform"]["translation_mm"] == [0.0, 2.0, 3.0]


def test_scene_omits_disabled_camera_even_when_a_valid_profile_exists(
    tmp_path: Path,
) -> None:
    run_root = make_scene_run(tmp_path)
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    wrist = next(
        sensor
        for sensor in config["capture"]["sensors"]
        if sensor["device_id"] == "111"
    )
    wrist["enabled"] = False
    config_path.write_text(json.dumps(config))

    scene = build_cell_scene(run_root)
    camera_ids = {
        entity["id"] for entity in scene["entities"] if entity["type"] == "camera"
    }

    assert "camera:realsense_111" not in camera_ids
    assert "camera:realsense_222" in camera_ids
    assert [timeline["id"] for timeline in scene["timelines"]] == [
        "sensor:realsense_222"
    ]
    assert scene["default_timeline_id"] == "sensor:realsense_222"
    flange = next(
        entity for entity in scene["entities"] if entity["id"] == "robot_flange"
    )
    assert flange["provenance"]["source"].endswith(
        "processed/synchronized/realsense_222/match_robot_ee_poses.json"
    )


def test_scene_marks_mismatched_bop_export_provenance_stale(tmp_path: Path) -> None:
    run_root = make_scene_run(tmp_path)
    manifest_path = run_root / BOP_DIR / BOP_EXPORT_MANIFEST
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "bop_export_manifest.v2",
                "dataset_mode": "pose_template",
            }
        )
    )

    scene = build_cell_scene(run_root)

    provenance = scene["object_selection"]["bop_export"]
    assert provenance["status"] == "stale"
    assert provenance["dataset_mode_matches"] is False
    assert any(
        warning["code"] == "stale_bop_export_provenance"
        for warning in scene["warnings"]
    )


def test_scene_marks_missing_calibration_and_uses_current_raw_timeline(
    tmp_path: Path,
) -> None:
    run_root = make_scene_run(tmp_path)
    for path in (run_root / "processed").rglob("match_robot_ee_poses.json"):
        path.unlink()
    (run_root / "raw_robot_ee_poses.json").write_text(
        json.dumps(
            {
                "0": {
                    "motion": "raw",
                    "framename": 1_000,
                    "host_received_timestamp_ns": 1_000_000_000,
                    "host_wall_timestamp_ns": 2_000_000_000,
                    "source_packet": {
                        "schema_version": "robot_pose.v1",
                        "packet_kind": "pose",
                        "run_id": json.loads(
                            (run_root / "run_config.json").read_text()
                        )["run_id"],
                        "from_frame": "robot_flange",
                        "to_frame": "template_base",
                        "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
                    },
                    "pose": {"X": 9, "Y": 8, "Z": 7, "A": 0, "B": 0, "C": 0},
                }
            }
        )
    )
    config = json.loads((run_root / "run_config.json").read_text())
    config["calibration_profiles"] = None
    config["frames"]["fixed_transforms"] = []
    (run_root / "run_config.json").write_text(json.dumps(config))

    scene = build_cell_scene(run_root)

    assert scene["default_timeline_id"] == "raw:robot"
    assert any(
        entity["type"] == "camera" and entity["status"] == "unresolved"
        for entity in scene["entities"]
    )
    assert any(
        warning["code"] == "missing_calibration_profiles"
        for warning in scene["warnings"]
    )
    entities = {item["id"]: item for item in scene["entities"]}
    assert entities["physical_robot_base"]["status"] == "not_configured"
    assert entities["physical_robot_base"]["unresolved_reason"] is None
    assert entities["tcp"]["status"] == "not_configured"


def test_pose_template_footprint_uses_exact_snapshot_preview(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    snapshot = run_root / "processed" / "pose_template_selection"
    snapshot.mkdir(parents=True)
    (snapshot / "pose_template_bundle.json").write_text(
        json.dumps(
            {
                "display_name": "Fixture footprint",
                "configuration": {
                    "page": {
                        "size": "A3",
                        "orientation": "landscape",
                        "origin_from_lower_left_mm": [15, 15],
                    }
                },
            }
        )
    )
    contours = [
        [{"x_mm": 10, "y_mm": 20}, {"x_mm": 30, "y_mm": 20}, {"x_mm": 10, "y_mm": 40}]
    ]
    (snapshot / "pose_template_preview.json").write_text(
        json.dumps(
            {
                "page": {"width_mm": 420, "height_mm": 297},
                "instances": [
                    {"instance_uuid": "instance-1", "compensated_contours": contours}
                ],
            }
        )
    )

    geometry = _pose_template_footprint(
        run_root,
        {
            "bundle_snapshot": "processed/pose_template_selection",
            "template_uuid": "template-1",
            "bundle_sha256": "b" * 64,
        },
    )

    assert geometry["kind"] == "pose_template_footprint"
    assert geometry["page"] == {"width_mm": 420, "height_mm": 297}
    assert geometry["contours"] == [
        {"instance_uuid": "instance-1", "contours": contours}
    ]


def test_scene_rejects_pinned_profile_for_another_device(tmp_path: Path) -> None:
    run_root = make_scene_run(tmp_path)
    config = json.loads((run_root / "run_config.json").read_text())
    config["capture"]["sensors"][0]["calibration_profile_id"] = "wrong_wrist_222"
    (run_root / "run_config.json").write_text(json.dumps(config))

    scene = build_cell_scene(run_root)
    wrist = next(
        entity for entity in scene["entities"] if entity["id"] == "camera:realsense_111"
    )

    assert wrist["status"] == "unresolved"
    assert wrist["transform"] is None
    assert (
        "does not match sensor identity realsense_d435:111"
        in wrist["unresolved_reason"]
    )


def test_cell_apis_assets_and_objectless_state(tmp_path: Path, monkeypatch) -> None:
    run_root = make_scene_run(tmp_path)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setenv("POSETESTBOT_WEB_INPUT_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()

    scene = client.get("/ui/cell-scene", query_string={"run_root": run_root}).get_json()
    rejected = client.get(f"/ui/cell-assets/cube/mesh?run_root={run_root.as_posix()}")
    timeline = client.get(
        "/ui/cell-scene/timeline",
        query_string={
            "run_root": run_root,
            "timeline_id": scene["default_timeline_id"],
            "offset": 0,
            "limit": 2,
        },
    )

    assert scene["object_selection"]["objectless"] is True
    assert not any(entity["type"] == "object" for entity in scene["entities"])
    assert rejected.status_code == 404
    assert timeline.status_code == 200
    assert len(timeline.get_json()["poses"]) == 2


def test_cell_camera_frames_follow_exact_timeline_indices(
    tmp_path: Path, monkeypatch
) -> None:
    run_root = make_scene_run(tmp_path)
    sensor_folder = run_root / "processed" / "synchronized" / "realsense_111"
    rgb = sensor_folder / "rgb"
    depth = sensor_folder / "depth"
    rgb.mkdir()
    depth.mkdir()
    frame_bytes = b"\x89PNG\r\n\x1a\nexact-frame-one"
    (rgb / "000001.png").write_bytes(frame_bytes)
    depth_values = np.array([[0, 200], [1_000, 3_000]], dtype=np.uint16)
    assert cv2.imwrite((depth / "000001.png").as_posix(), depth_values)
    (sensor_folder / "depthscale.txt").write_text("1.0\n")
    metadata_path = sensor_folder / "frame_metadata.jsonl"
    metadata_path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": "frame_metadata.v1",
                    "sensor_type": "realsense_d435",
                    "sensor_id": "111",
                    "frame_index": index,
                    "frame_id": f"{index:06d}.png",
                    "rgb_path": f"rgb/{index:06d}.png",
                    "depth_path": f"depth/{index:06d}.png",
                    "sensor_timestamp_ns": 1_000_000_000 + index,
                    "host_received_timestamp_ns": 2_000_000_000 + index,
                    "host_wall_timestamp_ns": 3_000_000_000 + index,
                    "sync_timestamp_ns": 2_000_000_000 + index,
                    "inverted": True,
                    "image_rotation_degrees": 180,
                    "orientation": "inverted",
                }
            )
            + "\n"
            for index in range(3)
        )
    )
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    next(
        sensor
        for sensor in config["capture"]["sensors"]
        if sensor["device_id"] == "111"
    )["inverted"] = True
    config_path.write_text(json.dumps(config))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\noutside-run")
    (rgb / "000000.png").symlink_to(outside)

    scene = build_cell_scene(run_root)
    timelines = {item["id"]: item for item in scene["timelines"]}
    assert timelines["sensor:realsense_111"]["camera"] == {
        "sensor_folder": "realsense_111",
        "sensor_type": "realsense_d435",
        "device_id": "111",
        "display_name": "Wrist camera",
        "mounting_mode": "eye_in_hand",
        "inverted": True,
        "image_presentation": {
            "configured_inverted": True,
            "stored_rotation_degrees": 180,
            "display_rotation_degrees": 0,
            "correction": "capture",
        },
    }
    assert timelines["sensor:realsense_111"]["camera_frames"] == {
        "available": True,
        "rgb": {
            "available": True,
            "kind": "rgb",
            "media_type": "image/png",
            "source": rgb.as_posix(),
        },
        "depth": {
            "available": True,
            "kind": "depth",
            "media_type": "image/png",
            "source": depth.as_posix(),
            "depth_scale_to_mm": 1.0,
            "visualization": "turbo_near_warm_fixed_range",
            "preview_min_depth_mm": 200.0,
            "preview_max_depth_mm": 3_000.0,
            "invalid_depth_value": 0,
        },
    }
    assert timelines["sensor:realsense_222"]["camera_frames"]["available"] is False

    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setenv("POSETESTBOT_WEB_INPUT_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()
    response = client.get(
        "/ui/cell-scene/camera-frame",
        query_string={
            "run_root": run_root,
            "timeline_id": "sensor:realsense_111",
            "timeline_index": 1,
        },
    )
    response_by_frame_id = client.get(
        "/ui/cell-scene/camera-frame",
        query_string={
            "run_root": run_root,
            "timeline_id": "sensor:realsense_111",
            "frame_id": "000001.png",
        },
    )
    missing = client.get(
        "/ui/cell-scene/camera-frame",
        query_string={
            "run_root": run_root,
            "timeline_id": "sensor:realsense_111",
            "timeline_index": 2,
        },
    )
    escaped = client.get(
        "/ui/cell-scene/camera-frame",
        query_string={
            "run_root": run_root,
            "timeline_id": "sensor:realsense_111",
            "timeline_index": 0,
        },
    )
    depth_preview = client.get(
        "/ui/cell-scene/camera-frame",
        query_string={
            "run_root": run_root,
            "timeline_id": "sensor:realsense_111",
            "timeline_index": 1,
            "modality": "depth",
        },
    )

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == frame_bytes
    assert response.cache_control.max_age == 3600
    assert response_by_frame_id.status_code == 200
    assert response_by_frame_id.data == frame_bytes
    assert missing.status_code == 404
    assert escaped.status_code == 400
    assert depth_preview.status_code == 200
    decoded_depth = cv2.imdecode(
        np.frombuffer(depth_preview.data, dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert decoded_depth.dtype == np.uint8
    assert decoded_depth.shape == (2, 2, 3)
    assert decoded_depth[0, 0].tolist() == [0, 0, 0]
    assert decoded_depth[0, 1].tolist() != decoded_depth[1, 1].tolist()

    metadata_path.unlink()
    with pytest.raises(FileNotFoundError, match="Current frame metadata is required"):
        build_cell_scene(run_root)


def test_retired_cell_registry_asset_route_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    run_root = make_scene_run(tmp_path)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setenv("POSETESTBOT_WEB_INPUT_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()

    response = client.get(f"/ui/cell-assets/cube/mesh?run_root={run_root.as_posix()}")

    assert response.status_code == 404
