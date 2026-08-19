from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.blenderproc.preparation import (
    load_camera_transformations,
    prepare_sensor_folders,
)
from posetestbot.calibration.profiles import (
    SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    RigidTransform,
    TransformFrame,
    write_profile_collection,
)
from posetestbot.io.artifacts import (
    CALIBRATION_PROFILES,
    CAM_K,
    DATASET_MANIFEST,
    DERIVED_CAMERA_EE_TRANSFORM,
    MATCH_ROBOT_EE_POSES,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType
from scripts.run_blenderproc_prepare_stage import (
    camera_transformations_from_calibration_profiles,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def create_blenderproc_prepare_fixture(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "run-1"
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="pose",
            sensors=(SensorRunConfig("realsense_d435", "123", "D435"),),
        ),
    )
    sensor_folder = run_root / "processed" / "synchronized" / "realsense_123"
    sensor_folder.mkdir(parents=True)
    (sensor_folder / "rgb").mkdir()
    (sensor_folder / "depth").mkdir()
    assert cv2.imwrite(
        (sensor_folder / "rgb" / "000000.png").as_posix(),
        np.zeros((60, 80, 3), dtype=np.uint8),
    )
    assert cv2.imwrite(
        (sensor_folder / "depth" / "000000.png").as_posix(),
        np.ones((60, 80), dtype=np.uint16),
    )
    (sensor_folder / CAM_K).write_text("50 0 40\n0 50 40\n0 0 1\n")
    write_json(
        sensor_folder / MATCH_ROBOT_EE_POSES,
        {
            "000000.png": {
                "motion": "circ_far",
                "robot_ee_pose": {
                    "X": 0.0,
                    "Y": 0.0,
                    "Z": 0.0,
                    "A": 0.0,
                    "B": 0.0,
                    "C": 0.0,
                },
            }
        },
    )

    camera_transforms = tmp_path / "camera_ee_transform.json"
    write_json(
        camera_transforms,
        {
            "realsense_123": {
                "quaternion": [1.0, 0.0, 0.0, 0.0],
                "position": [0.0, 0.0, 0.0],
            }
        },
    )
    return run_root, camera_transforms


def test_blenderproc_prepare_stage_writes_artifacts_and_manifest(
    tmp_path: Path,
) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_prepare_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose",
            "--camera-transformations",
            str(camera_transforms),
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Prepared BlenderProc inputs for 1 sensor folder" in result.stdout

    blenderproc_folder = (
        run_root / "processed" / "synchronized" / "realsense_123" / "blenderproc"
    )
    assert (
        json.loads((blenderproc_folder / "objects.json").read_text())["instances"] == []
    )
    assert list((blenderproc_folder / "objects").iterdir()) == []
    np.testing.assert_allclose(
        np.load(blenderproc_folder / "camera_matrix.npy"),
        np.array([[50.0, 0.0, 40.0], [0.0, 50.0, 40.0], [0.0, 0.0, 1.0]]),
    )
    np.testing.assert_allclose(
        np.load(blenderproc_folder / "dist_coefficients.npy"),
        np.zeros((5, 1)),
    )
    camera_poses = np.load(blenderproc_folder / "camera_poses.npy")
    assert camera_poses.shape == (1, 4, 4)
    frame_contract = json.loads(
        (blenderproc_folder / "frame_contract.json").read_text()
    )
    matched_pose_sha256 = hashlib.sha256(
        (
            run_root
            / "processed"
            / "synchronized"
            / "realsense_123"
            / MATCH_ROBOT_EE_POSES
        ).read_bytes()
    ).hexdigest()
    assert frame_contract == {
        "schema_version": "blenderproc_frame_contract.v1",
        "annotation_mode": "pose",
        "projection": "native",
        "resolution": {"width": 80, "height": 60},
        "source_artifact_sha256": {MATCH_ROBOT_EE_POSES: matched_pose_sha256},
        "frames": [
            {
                "output_image_id": 0,
                "source_frame_id": 0,
                "source_filename": "000000.png",
            }
        ],
    }

    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage for stage in manifest["stages"] if stage["name"] == "blenderproc_prepare"
    )
    assert stage["status"] == "succeeded"
    assert stage["artifacts"]["realsense_123:blenderproc"].endswith(
        "processed/synchronized/realsense_123/blenderproc"
    )


def test_blenderproc_prepare_default_ignores_disabled_stale_sensor_folder(
    tmp_path: Path,
) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)
    synchronized = run_root / "processed" / "synchronized"
    disabled = synchronized / "realsense_999"
    shutil.copytree(synchronized / "realsense_123", disabled)
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="pose",
            run_root=run_root,
            sensors=(
                SensorRunConfig("realsense_d435", "123", "Enabled"),
                SensorRunConfig("realsense_d435", "999", "Disabled", enabled=False),
            ),
        ),
    )
    repo_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        str(repo_root / "scripts" / "run_blenderproc_prepare_stage.py"),
        str(run_root),
        "--annotation-mode",
        "pose",
        "--camera-transformations",
        str(camera_transforms),
    ]

    subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert (synchronized / "realsense_123" / "blenderproc").is_dir()
    assert not (disabled / "blenderproc").exists()

    transforms = json.loads(camera_transforms.read_text())
    transforms["realsense_999"] = transforms["realsense_123"]
    write_json(camera_transforms, transforms)
    subprocess.run(
        [*command, "--input-folder", str(synchronized)],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert (disabled / "blenderproc").is_dir()


def test_blenderproc_prepare_prefers_rectified_sensor_tree(tmp_path: Path) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)
    synchronized = run_root / "processed" / "synchronized" / "realsense_123"
    rectified = run_root / "processed" / "rectified" / "realsense_123"
    shutil.copytree(synchronized, rectified)
    repo_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_prepare_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose",
            "--camera-transformations",
            str(camera_transforms),
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert (rectified / "blenderproc" / "camera_matrix.npy").is_file()
    assert not (synchronized / "blenderproc").exists()


def test_blenderproc_prepare_stage_accepts_calibration_profiles(
    tmp_path: Path,
) -> None:
    run_root, _ = create_blenderproc_prepare_fixture(tmp_path)
    calibration_profiles = tmp_path / "calibration_profiles.json"
    write_profile_collection(
        [
            CalibrationProfile(
                schema_version=SCHEMA_VERSION,
                profile_id="realsense_d435_123_eye_in_hand_wrist_test",
                sensor_id="123",
                sensor_type=SensorType.REALSENSE_D435,
                mounting_mode=MountingMode.EYE_IN_HAND,
                rig_position="wrist",
                intrinsics=CameraIntrinsics(
                    cam_k=(50.0, 0.0, 40.0, 0.0, 50.0, 40.0, 0.0, 0.0, 1.0),
                    width=80,
                    height=80,
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
        calibration_profiles,
    )
    repo_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_prepare_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose",
            "--calibration-profiles",
            str(calibration_profiles),
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    derived_transform = (
        run_root / "processed" / "calibration" / DERIVED_CAMERA_EE_TRANSFORM
    )
    transform_map = json.loads(derived_transform.read_text())
    assert transform_map["realsense_123"]["position"] == [10.0, 20.0, 30.0]

    blenderproc_folder = (
        run_root / "processed" / "synchronized" / "realsense_123" / "blenderproc"
    )
    camera_poses = np.load(blenderproc_folder / "camera_poses.npy")
    np.testing.assert_allclose(camera_poses[0, :3, 3], [0.01, 0.02, 0.03])

    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage for stage in manifest["stages"] if stage["name"] == "blenderproc_prepare"
    )
    assert stage["artifacts"][CALIBRATION_PROFILES].endswith(
        "calibration_profiles.json"
    )
    assert stage["artifacts"][DERIVED_CAMERA_EE_TRANSFORM] == (
        "processed/calibration/camera_ee_transform_from_calibration_profiles.json"
    )


def test_blenderproc_prepare_stage_accepts_static_calibration_profiles(
    tmp_path: Path,
) -> None:
    run_root, _ = create_blenderproc_prepare_fixture(tmp_path)
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="pose",
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
    sensor_folder = run_root / "processed" / "synchronized" / "realsense_123"
    write_json(
        sensor_folder / MATCH_ROBOT_EE_POSES,
        {
            "000000.png": {
                "motion": "circ_far",
                "robot_ee_pose": {
                    "X": 0.0,
                    "Y": 0.0,
                    "Z": 0.0,
                    "A": 0.0,
                    "B": 0.0,
                    "C": 0.0,
                },
            },
            "000001.png": {
                "motion": "circ_close",
                "robot_ee_pose": {
                    "X": 500.0,
                    "Y": 600.0,
                    "Z": 700.0,
                    "A": 0.1,
                    "B": 0.2,
                    "C": 0.3,
                },
            },
        },
    )
    for folder_name, value in (
        ("rgb", np.zeros((60, 80, 3), dtype=np.uint8)),
        ("depth", np.ones((60, 80), dtype=np.uint16)),
    ):
        assert cv2.imwrite(
            (sensor_folder / folder_name / "000001.png").as_posix(),
            value,
        )
    calibration_profiles = tmp_path / "calibration_profiles.json"
    write_profile_collection(
        [
            CalibrationProfile(
                schema_version=SCHEMA_VERSION,
                profile_id="realsense_d435_123_static_cell_front_test",
                sensor_id="123",
                sensor_type=SensorType.REALSENSE_D435,
                mounting_mode=MountingMode.STATIC,
                rig_position="cell_front",
                intrinsics=CameraIntrinsics(
                    cam_k=(50.0, 0.0, 40.0, 0.0, 50.0, 40.0, 0.0, 0.0, 1.0),
                    width=80,
                    height=80,
                ),
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
                        "sunrise_reference_frame_path": (
                            POSE_TEMPLATE_BASE_SUNRISE_PATH
                        ),
                    }
                },
            )
        ],
        calibration_profiles,
    )
    repo_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_prepare_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose",
            "--calibration-profiles",
            str(calibration_profiles),
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    derived_transform = (
        run_root / "processed" / "calibration" / DERIVED_CAMERA_EE_TRANSFORM
    )
    transform_map = json.loads(derived_transform.read_text())
    assert transform_map["realsense_123"]["mounting_mode"] == "static"
    assert transform_map["realsense_123"]["to"] == "template_base"

    blenderproc_folder = sensor_folder / "blenderproc"
    camera_poses = np.load(blenderproc_folder / "camera_poses.npy")
    assert camera_poses.shape == (2, 4, 4)
    np.testing.assert_allclose(camera_poses[0, :3, 3], [0.1, 0.2, 0.3])
    np.testing.assert_allclose(camera_poses[1, :3, 3], [0.1, 0.2, 0.3])


def test_blenderproc_prepare_rejects_profile_with_wrong_run_mount(
    tmp_path: Path,
) -> None:
    run_root, _ = create_blenderproc_prepare_fixture(tmp_path)
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="pose",
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
                schema_version=SCHEMA_VERSION,
                profile_id="realsense_123_eye_in_hand_wrong_mount",
                sensor_id="123",
                sensor_type=SensorType.REALSENSE_D435,
                mounting_mode=MountingMode.EYE_IN_HAND,
                rig_position="wrist",
                intrinsics=CameraIntrinsics(
                    cam_k=(50.0, 0.0, 40.0, 0.0, 50.0, 40.0, 0.0, 0.0, 1.0),
                    width=80,
                    height=80,
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
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_blenderproc_prepare_stage.py"
            ),
            str(run_root),
            "--annotation-mode",
            "pose",
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
    assert not (
        run_root / "processed" / "calibration" / DERIVED_CAMERA_EE_TRANSFORM
    ).exists()
    assert not (
        run_root / "processed" / "synchronized" / "realsense_123" / "blenderproc"
    ).exists()


def test_blenderproc_prepare_uses_exact_selected_profile_for_ambiguous_sensor(
    tmp_path: Path,
) -> None:
    run_root, _ = create_blenderproc_prepare_fixture(tmp_path)
    profiles_path = tmp_path / "mixed_mount_calibration_profiles.json"
    intrinsics = CameraIntrinsics(
        cam_k=(50.0, 0.0, 40.0, 0.0, 50.0, 40.0, 0.0, 0.0, 1.0),
        width=80,
        height=80,
    )
    eye_profile_id = "realsense_d435_123_eye_in_hand_selected_test"
    static_profile_id = "realsense_d435_123_static_selected_test"
    write_profile_collection(
        [
            CalibrationProfile(
                schema_version=SCHEMA_VERSION,
                profile_id=eye_profile_id,
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
            ),
            CalibrationProfile(
                schema_version=SCHEMA_VERSION,
                profile_id=static_profile_id,
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
                        "sunrise_reference_frame_path": (
                            POSE_TEMPLATE_BASE_SUNRISE_PATH
                        ),
                    }
                },
            ),
        ],
        profiles_path,
    )

    with pytest.raises(ValueError, match="Ambiguous calibration profiles"):
        camera_transformations_from_calibration_profiles(
            input_folder=run_root / "processed" / "synchronized",
            calibration_profiles_path=profiles_path,
        )

    transforms = camera_transformations_from_calibration_profiles(
        input_folder=run_root / "processed" / "synchronized",
        calibration_profiles_path=profiles_path,
        profile_ids_by_sensor_name={"realsense_123": static_profile_id},
        mounting_modes_by_sensor_name={
            "realsense_123": MountingMode.STATIC,
        },
    )

    assert transforms["realsense_123"]["profile_id"] == static_profile_id
    assert transforms["realsense_123"]["mounting_mode"] == "static"


def test_blenderproc_prepare_failure_preserves_all_existing_outputs(
    tmp_path: Path,
) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)
    synchronized = run_root / "processed" / "synchronized"
    first_output = synchronized / "realsense_123" / "blenderproc"
    first_output.mkdir()
    (first_output / "previous.txt").write_text("keep")
    invalid_sensor = synchronized / "zed_2i_456"
    invalid_sensor.mkdir()
    (invalid_sensor / CAM_K).write_text("50 0 40\n0 50 40\n0 0 1\n")

    transforms = dict(load_camera_transformations(camera_transforms))
    transforms["zed_2i_456"] = transforms["realsense_123"]
    with pytest.raises(FileNotFoundError, match="matched robot poses"):
        prepare_sensor_folders(
            input_folder=synchronized,
            camera_transformations=transforms,
            annotation_mode="pose",
        )

    assert (first_output / "previous.txt").read_text() == "keep"
    assert not (invalid_sensor / "blenderproc").exists()
    assert not list(synchronized.rglob("*.staging"))


def test_blenderproc_prepare_objectless_clears_stale_models(tmp_path: Path) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)
    sensor = run_root / "processed" / "synchronized" / "realsense_123"
    stale = sensor / "blenderproc" / "objects"
    stale.mkdir(parents=True)
    (stale / "stale.ply").write_text("stale")

    prepared = prepare_sensor_folders(
        input_folder=run_root / "processed" / "synchronized",
        camera_transformations=load_camera_transformations(camera_transforms),
        annotation_mode="pose",
    )

    output = prepared[0].output_folder
    assert prepared[0].object_count == 0
    assert json.loads((output / "objects.json").read_text())["instances"] == []
    assert list((output / "objects").iterdir()) == []
    assert (output / "camera_poses.npy").is_file()


@pytest.mark.parametrize("missing_from", ["rgb", "depth", "poses"])
def test_blenderproc_prepare_rejects_rgb_depth_pose_key_mismatch(
    tmp_path: Path,
    missing_from: str,
) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)
    sensor = run_root / "processed" / "synchronized" / "realsense_123"
    if missing_from == "poses":
        poses = json.loads((sensor / MATCH_ROBOT_EE_POSES).read_text())
        poses["000001.png"] = poses["000000.png"]
        write_json(sensor / MATCH_ROBOT_EE_POSES, poses)
    else:
        folder = sensor / missing_from
        image = (
            np.zeros((60, 80, 3), dtype=np.uint8)
            if missing_from == "rgb"
            else np.ones((60, 80), dtype=np.uint16)
        )
        assert cv2.imwrite((folder / "000001.png").as_posix(), image)

    with pytest.raises(
        ValueError,
        match="RGB/depth/matched-pose frame names must be identical",
    ):
        prepare_sensor_folders(
            input_folder=run_root / "processed" / "synchronized",
            camera_transformations=load_camera_transformations(camera_transforms),
            annotation_mode="pose",
        )


@pytest.mark.parametrize("annotation_mode", ["pose", "pose_and_masks"])
def test_blenderproc_prepare_records_annotation_mode(
    tmp_path: Path,
    annotation_mode: str,
) -> None:
    run_root, camera_transforms = create_blenderproc_prepare_fixture(tmp_path)

    prepared = prepare_sensor_folders(
        input_folder=run_root / "processed" / "synchronized",
        camera_transformations=load_camera_transformations(camera_transforms),
        annotation_mode=annotation_mode,
    )

    assert prepared[0].annotation_mode == annotation_mode
    contract = json.loads(
        (prepared[0].output_folder / "frame_contract.json").read_text()
    )
    assert contract["annotation_mode"] == annotation_mode
