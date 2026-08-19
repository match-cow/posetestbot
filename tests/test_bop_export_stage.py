from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.io.artifacts import (
    BOP_DIR,
    BOP_EXPORT_MANIFEST,
    BOP_FRAME_MAP_JSON,
    BOP_TARGETS_BOP19,
    CAM_K,
    DATASET_MANIFEST,
    DEPTH_DIR,
    DEPTH_SCALE,
    RGB_DIR,
)
from posetestbot.calibration.profiles import (
    SCHEMA_VERSION as CALIBRATION_SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    RigidTransform,
    TransformFrame,
    write_profile_collection,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType
from scripts.run_bop_export_stage import calibration_profile_for_sensor


def create_synchronized_sensor_fixture(
    tmp_path: Path, *, annotation_mode: str = "none"
) -> Path:
    run_root = tmp_path / "run-1"
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode=annotation_mode,
            sensors=(SensorRunConfig("realsense_d435", "123", "D435"),),
        ),
    )
    sensor = run_root / "processed" / "synchronized" / "realsense_123"
    rgb = sensor / RGB_DIR
    depth = sensor / DEPTH_DIR
    rgb.mkdir(parents=True)
    depth.mkdir()
    for frame_id, value in ((10, 1), (20, 2)):
        assert cv2.imwrite(
            (rgb / f"{frame_id:06d}.png").as_posix(),
            np.full((5, 6, 3), value, dtype=np.uint8),
        )
        assert cv2.imwrite(
            (depth / f"{frame_id:06d}.png").as_posix(),
            np.full((5, 6), value, dtype=np.uint16),
        )
    (sensor / CAM_K).write_text("1 0 2\n0 3 4\n0 0 1\n")
    (sensor / DEPTH_SCALE).write_text("0.001\n")
    return run_root


def export_command(run_root: Path, *, annotation_mode: str = "none") -> list[str]:
    repo_root = Path(__file__).resolve().parents[1]
    return [
        sys.executable,
        str(repo_root / "scripts" / "run_bop_export_stage.py"),
        str(run_root),
        "--annotation-mode",
        annotation_mode,
    ]


def test_bop_export_uses_exact_selected_profile_for_ambiguous_sensor() -> None:
    intrinsics = CameraIntrinsics(
        cam_k=(10.0, 0.0, 3.0, 0.0, 10.0, 2.5, 0.0, 0.0, 1.0),
        width=6,
        height=5,
    )
    eye_profile = CalibrationProfile(
        schema_version=CALIBRATION_SCHEMA_VERSION,
        profile_id="realsense_123_eye_in_hand_bop_test",
        sensor_id="123",
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=MountingMode.EYE_IN_HAND,
        rig_position="wrist",
        intrinsics=intrinsics,
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=TransformFrame.ROBOT_FLANGE,
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(10.0, 20.0, 30.0),
        ),
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(num_observations=8, num_inliers=8),
    )
    static_profile = CalibrationProfile(
        schema_version=CALIBRATION_SCHEMA_VERSION,
        profile_id="realsense_123_static_bop_test",
        sensor_id="123",
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=MountingMode.STATIC,
        rig_position="cell_front",
        intrinsics=intrinsics,
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=TransformFrame.TEMPLATE_BASE,
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(100.0, 200.0, 300.0),
        ),
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(num_observations=8, num_inliers=8),
        metadata={
            "robot_pose_reference": {
                "schema_version": "robot_pose_reference.v1",
                "status": "verified",
                "packet_schema_version": "robot_pose.v1",
                "from": "robot_flange",
                "to": "template_base",
                "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
            }
        },
    )
    profiles = [eye_profile, static_profile]

    with pytest.raises(ValueError, match="Ambiguous calibration profiles"):
        calibration_profile_for_sensor(profiles, "realsense_123")

    selected = calibration_profile_for_sensor(
        profiles,
        "realsense_123",
        profile_ids_by_sensor_name={
            "realsense_123": static_profile.profile_id,
        },
        mounting_modes_by_sensor_name={
            "realsense_123": MountingMode.STATIC,
        },
    )

    assert selected is static_profile

    with pytest.raises(KeyError, match="No static calibration profile"):
        calibration_profile_for_sensor(
            [eye_profile],
            "realsense_123",
            mounting_modes_by_sensor_name={
                "realsense_123": MountingMode.STATIC,
            },
        )

    with pytest.raises(KeyError, match="No static calibration profile"):
        calibration_profile_for_sensor(
            profiles,
            "realsense_123",
            profile_ids_by_sensor_name={
                "realsense_123": eye_profile.profile_id,
            },
            mounting_modes_by_sensor_name={
                "realsense_123": MountingMode.STATIC,
            },
        )


def test_bop_export_rejects_profile_with_wrong_run_mount(tmp_path: Path) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                SensorRunConfig(
                    "realsense_d435",
                    "123",
                    "Static camera",
                    mounting_mode="static",
                ),
            ),
        ),
    )
    profiles_path = tmp_path / "eye-only-calibration-profiles.json"
    write_profile_collection(
        [
            CalibrationProfile(
                schema_version=CALIBRATION_SCHEMA_VERSION,
                profile_id="realsense_123_eye_in_hand_wrong_mount",
                sensor_id="123",
                sensor_type=SensorType.REALSENSE_D435,
                mounting_mode=MountingMode.EYE_IN_HAND,
                rig_position="wrist",
                intrinsics=CameraIntrinsics(
                    cam_k=(10.0, 0.0, 3.0, 0.0, 10.0, 2.5, 0.0, 0.0, 1.0),
                    width=6,
                    height=5,
                ),
                extrinsics=RigidTransform(
                    from_frame=TransformFrame.CAMERA,
                    to_frame=TransformFrame.ROBOT_FLANGE,
                    rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
                    translation_mm=(10.0, 20.0, 30.0),
                ),
                status=CalibrationStatus.VALID,
                quality=CalibrationQuality(num_observations=8, num_inliers=8),
            )
        ],
        profiles_path,
    )

    result = subprocess.run(
        [
            *export_command(run_root),
            "--calibration-profiles",
            str(profiles_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "No static calibration profile matches" in result.stderr
    assert not (run_root / BOP_DIR).exists()


def test_bop_export_stage_writes_objectless_dataset_and_manifest(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        export_command(run_root),
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Exported 1 synchronized sensor folder" in result.stdout
    bop = run_root / BOP_DIR
    scene = bop / "test" / "000001"
    manifest = json.loads((bop / BOP_EXPORT_MANIFEST).read_text())
    assert manifest["schema_version"] == "bop_export_manifest.v5"
    assert manifest["dataset_mode"] == "objectless"
    assert manifest["objectless"] is True
    assert manifest["annotation_source"] == "none"
    assert manifest["annotation_mode"] == "none"
    assert manifest["annotation_state"] == "absent"
    assert manifest["capabilities"]["bop19_evaluation"] is False
    assert manifest["exports"][0]["input_sensor_folder"] == (
        "processed/synchronized/realsense_123"
    )
    assert str(run_root.resolve()) not in json.dumps(manifest)
    assert manifest["object_models"] == []
    assert manifest["stable_id_mapping"] == {}
    assert manifest["targets_path"] is None
    assert "targets" not in manifest["exports"][0]
    assert not (bop / BOP_TARGETS_BOP19).exists()
    frame_map = json.loads((bop / BOP_FRAME_MAP_JSON).read_text())
    assert frame_map["schema_version"] == "posetestbot_bop_frame_map.v3"
    assert all(
        set(frame) == {"source_rgb", "source_depth", "bop_rgb", "bop_depth"}
        for frame in frame_map["scenes"]["1"]["frames"].values()
    )
    assert "input_fingerprint_sha256" not in frame_map["scenes"]["1"]
    assert len(list((scene / RGB_DIR).glob("*.png"))) == 2
    assert len(list((scene / DEPTH_DIR).glob("*.png"))) == 2
    scene_camera = json.loads((scene / "scene_camera.json").read_text())
    assert all(
        set(camera) == {"cam_K", "depth_scale"} for camera in scene_camera.values()
    )
    assert not (scene / "scene_gt.json").exists()
    assert not (scene / "scene_gt_info.json").exists()
    run_manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        item for item in run_manifest["stages"] if item["name"] == "bop_export"
    )
    assert stage["status"] == "succeeded"


def test_annotation_free_bop_export_rejects_annotation_derived_extras(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [*export_command(run_root), "--write-coco-annotations"],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "--annotation-source blenderproc" in result.stderr
    assert not (run_root / BOP_DIR).exists()


def test_bop_export_default_ignores_disabled_stale_sensor_folder(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    synchronized = run_root / "processed" / "synchronized"
    shutil.copytree(synchronized / "realsense_123", synchronized / "realsense_999")
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                SensorRunConfig("realsense_d435", "123", "Enabled"),
                SensorRunConfig("realsense_d435", "999", "Disabled", enabled=False),
            ),
        ),
    )
    repo_root = Path(__file__).resolve().parents[1]

    default_result = subprocess.run(
        export_command(run_root),
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Exported 1 synchronized sensor folder" in default_result.stdout
    default_manifest = json.loads(
        (run_root / BOP_DIR / BOP_EXPORT_MANIFEST).read_text()
    )
    assert [item["sensor_name"] for item in default_manifest["exports"]] == [
        "realsense_123"
    ]

    explicit_output = run_root / "explicit-bop"
    explicit_result = subprocess.run(
        [
            *export_command(run_root),
            "--input-folder",
            str(synchronized),
            "--output-folder",
            str(explicit_output),
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Exported 2 synchronized sensor folder" in explicit_result.stdout
    explicit_manifest = json.loads((explicit_output / BOP_EXPORT_MANIFEST).read_text())
    assert [item["sensor_name"] for item in explicit_manifest["exports"]] == [
        "realsense_123",
        "realsense_999",
    ]


def test_bop_export_default_ignores_stale_rendered_gt(tmp_path: Path) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    output = (
        run_root
        / "processed"
        / "synchronized"
        / "realsense_123"
        / "blenderproc"
        / "output"
    )
    output.mkdir(parents=True)
    (output / "scene_gt.json").write_text(
        json.dumps(
            {
                "0": [
                    {
                        "obj_id": 1,
                        "cam_R_m2c": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                        "cam_t_m2c": [0, 0, 1],
                    }
                ],
                "1": [],
            }
        )
    )
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        export_command(run_root),
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    bop = run_root / BOP_DIR
    manifest = json.loads((bop / BOP_EXPORT_MANIFEST).read_text())
    assert manifest["annotation_source"] == "none"
    assert not (bop / "test" / "000001" / "scene_gt.json").exists()
    assert not (bop / "test" / "000001" / "scene_gt_info.json").exists()


def test_bop_export_rendered_annotation_mode_rejects_unknown_object_gt(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path, annotation_mode="pose")
    output = (
        run_root
        / "processed"
        / "synchronized"
        / "realsense_123"
        / "blenderproc"
        / "output"
    )
    output.mkdir(parents=True)
    (output / "scene_gt.json").write_text(
        json.dumps(
            {
                "0": [
                    {
                        "obj_id": 1,
                        "cam_R_m2c": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                        "cam_t_m2c": [0, 0, 1],
                    }
                ],
                "1": [],
            }
        )
    )
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            *export_command(run_root, annotation_mode="pose"),
            "--annotation-source",
            "blenderproc",
        ],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Unknown BOP obj_id" in result.stderr
    assert not (run_root / BOP_DIR).exists()


def test_bop_overwrite_failure_preserves_previous_dataset(tmp_path: Path) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    command = export_command(run_root)
    subprocess.run(command, cwd=repo_root, check=True, capture_output=True, text=True)
    manifest_path = run_root / BOP_DIR / BOP_EXPORT_MANIFEST
    previous_manifest = manifest_path.read_bytes()

    sensor = run_root / "processed" / "synchronized" / "realsense_123"
    (sensor / DEPTH_DIR / "000020.png").unlink()
    failed = subprocess.run(
        [*command, "--overwrite"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert failed.returncode != 0
    assert manifest_path.read_bytes() == previous_manifest
    assert (run_root / BOP_DIR / "test" / "000001" / RGB_DIR / "000001.png").is_file()
    assert not list(run_root.glob(".bop.*.tmp"))
