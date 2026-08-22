from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.bop import writer as bop_writer
from posetestbot.bop.writer import (
    export_sensor_scene_to_bop,
    validate_bop_export_artifact_set,
    write_bop_frame_map,
)
from posetestbot.calibration.intrinsics import factory_intrinsic_profile
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
from posetestbot.calibration.rectification import rectify_run
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
from scripts.run_bop_export_stage import (
    calibration_profile_for_sensor,
    validate_calibrated_bop_inputs,
    validate_managed_export_paths,
)


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


def create_rectified_sensor_fixture(tmp_path: Path) -> tuple[Path, Path]:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    sensor = run_root / "processed" / "synchronized" / "realsense_123"
    (sensor / "camera_data.json").write_text(
        json.dumps(
            {
                "K": [[1, 0, 2], [0, 3, 4], [0, 0, 1]],
                "resolution": [5, 6],
                "distortion": [0.0] * 5,
                "distortion_model": "brown_conrady",
            }
        )
    )
    metadata = []
    matched = {}
    for frame_index, frame_id in enumerate(("000010.png", "000020.png")):
        metadata.append(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "123",
                "orientation": "normal",
                "frame_index": frame_index,
                "frame_id": frame_id,
                "rgb_path": f"rgb/{frame_id}",
                "depth_path": f"depth/{frame_id}",
                "host_received_timestamp_ns": 100 + frame_index,
                "host_wall_timestamp_ns": 200 + frame_index,
            }
        )
        matched[frame_id] = {
            "robot_ee_pose": {
                "X": frame_index,
                "Y": 0,
                "Z": 500,
                "A": 0,
                "B": 0,
                "C": 0,
            }
        }
    (sensor / "frame_metadata.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in metadata)
    )
    (sensor / "match_robot_ee_poses.json").write_text(json.dumps(matched))
    rectify_run(run_root, [factory_intrinsic_profile(sensor)])
    return run_root, sensor


def calibrated_profile_for_rectified_fixture(sensor: Path) -> CalibrationProfile:
    intrinsic_profile = factory_intrinsic_profile(sensor)
    native = intrinsic_profile["native"]
    rectified = intrinsic_profile["rectified"]
    assert rectified is not None
    return CalibrationProfile(
        schema_version=CALIBRATION_SCHEMA_VERSION,
        profile_id="realsense_123_rectified_bop_test",
        sensor_id="123",
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=MountingMode.EYE_IN_HAND,
        rig_position="wrist",
        intrinsics=CameraIntrinsics(
            cam_k=tuple(native["cam_K"]),
            width=6,
            height=5,
            distortion=tuple(native["distortion"]),
            depth_scale_to_mm=0.001,
            distortion_model=native["distortion_model"],
        ),
        rectified_intrinsics=CameraIntrinsics(
            cam_k=tuple(rectified["cam_K"]),
            width=6,
            height=5,
            distortion=tuple(rectified["distortion"]),
            depth_scale_to_mm=0.001,
            distortion_model=rectified["distortion_model"],
        ),
        rectified_valid_roi=tuple(rectified["valid_roi"]),
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=TransformFrame.ROBOT_FLANGE,
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(10.0, 20.0, 30.0),
        ),
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(num_observations=8, num_inliers=8),
    )


def export_command(run_root: Path, *, annotation_mode: str = "none") -> list[str]:
    repo_root = Path(__file__).resolve().parents[1]
    return [
        sys.executable,
        str(repo_root / "scripts" / "run_bop_export_stage.py"),
        str(run_root),
        "--annotation-mode",
        annotation_mode,
        "--diagnostic-unmanaged",
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


def test_calibrated_bop_inputs_bind_rectified_and_authoritative_hashes(
    tmp_path: Path,
) -> None:
    run_root, synchronized_sensor = create_rectified_sensor_fixture(tmp_path)
    rectified_root = run_root / "processed" / "rectified"

    inputs = validate_calibrated_bop_inputs(
        run_root,
        rectified_root,
        ("realsense_123",),
    )

    evidence = inputs["realsense_123"]
    provenance = json.loads(
        (evidence.sensor_folder / "rectification_provenance.json").read_text()
    )
    assert evidence.sensor_folder == rectified_root / "realsense_123"
    assert evidence.authoritative_source_sensor_folder == synchronized_sensor
    assert (
        evidence.input_fingerprint_sha256 == provenance["output_fingerprint"]["digest"]
    )
    assert (
        evidence.authoritative_source_fingerprint_sha256
        == provenance["source_fingerprint"]["digest"]
    )

    output = tmp_path / "bop"
    export = export_sensor_scene_to_bop(
        evidence.sensor_folder,
        output,
        annotation_mode="none",
        object_name_to_id={},
        source_projection="rectified",
        input_sensor_folder="processed/rectified/realsense_123",
        authoritative_source_sensor_folder=("processed/synchronized/realsense_123"),
        input_fingerprint_sha256=evidence.input_fingerprint_sha256,
        authoritative_source_fingerprint_sha256=(
            evidence.authoritative_source_fingerprint_sha256
        ),
    )
    frame_map_path = write_bop_frame_map(output, [export])
    scene = json.loads(frame_map_path.read_text())["scenes"]["1"]
    assert scene["projection"] == "rectified"
    assert scene["input_sensor_folder"] == "processed/rectified/realsense_123"
    assert scene["authoritative_source_sensor_folder"] == (
        "processed/synchronized/realsense_123"
    )
    assert scene["input_fingerprint_sha256"] == evidence.input_fingerprint_sha256
    assert scene["authoritative_source_fingerprint_sha256"] == (
        evidence.authoritative_source_fingerprint_sha256
    )
    assert export.targets == []


def test_calibrated_bop_inputs_reject_noncanonical_synchronized_root(
    tmp_path: Path,
) -> None:
    run_root, _sensor = create_rectified_sensor_fixture(tmp_path)

    with pytest.raises(ValueError, match="canonical rectified input root"):
        validate_calibrated_bop_inputs(
            run_root,
            run_root / "processed" / "synchronized",
            ("realsense_123",),
        )


def test_calibrated_bop_inputs_require_every_enabled_sensor(tmp_path: Path) -> None:
    run_root, _sensor = create_rectified_sensor_fixture(tmp_path)

    with pytest.raises(FileNotFoundError, match="luxonis_missing"):
        validate_calibrated_bop_inputs(
            run_root,
            run_root / "processed" / "rectified",
            ("realsense_123", "luxonis_missing"),
        )


@pytest.mark.parametrize(
    ("mutated_tree", "message"),
    [
        ("synchronized", "source fingerprint"),
        ("rectified", "output fingerprint"),
    ],
)
def test_calibrated_bop_inputs_reject_stale_or_mutated_rectification(
    tmp_path: Path,
    mutated_tree: str,
    message: str,
) -> None:
    run_root, synchronized_sensor = create_rectified_sensor_fixture(tmp_path)
    rectified_root = run_root / "processed" / "rectified"
    target_sensor = (
        synchronized_sensor
        if mutated_tree == "synchronized"
        else rectified_root / "realsense_123"
    )
    target = target_sensor / RGB_DIR / "000010.png"
    target.write_bytes(target.read_bytes() + b"mutated")

    with pytest.raises(ValueError, match=message):
        validate_calibrated_bop_inputs(
            run_root,
            rectified_root,
            ("realsense_123",),
        )


def test_calibrated_bop_export_stage_writes_verified_objectless_provenance(
    tmp_path: Path,
) -> None:
    run_root, synchronized_sensor = create_rectified_sensor_fixture(tmp_path)
    profile_path = tmp_path / "calibration-profiles.json"
    write_profile_collection(
        [calibrated_profile_for_rectified_fixture(synchronized_sensor)],
        profile_path,
    )

    result = subprocess.run(
        [
            *export_command(run_root),
            "--calibration-profiles",
            str(profile_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Exported 1 synchronized sensor folder" in result.stdout
    bop = run_root / BOP_DIR
    manifest = json.loads((bop / BOP_EXPORT_MANIFEST).read_text())
    exported = manifest["exports"][0]
    assert manifest["objectless"] is True
    assert exported["projection"] == "rectified"
    assert exported["input_sensor_folder"] == "processed/rectified/realsense_123"
    assert exported["authoritative_source_sensor_folder"] == (
        "processed/synchronized/realsense_123"
    )
    assert len(exported["input_fingerprint_sha256"]) == 64
    assert len(exported["authoritative_source_fingerprint_sha256"]) == 64
    frame_map = json.loads((bop / BOP_FRAME_MAP_JSON).read_text())["scenes"]["1"]
    assert frame_map["input_fingerprint_sha256"] == exported["input_fingerprint_sha256"]
    assert (
        frame_map["authoritative_source_fingerprint_sha256"]
        == exported["authoritative_source_fingerprint_sha256"]
    )


def test_calibrated_bop_export_stage_requires_all_enabled_rectified_sensors(
    tmp_path: Path,
) -> None:
    run_root, synchronized_sensor = create_rectified_sensor_fixture(tmp_path)
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                SensorRunConfig("realsense_d435", "123", "D435"),
                SensorRunConfig("oak_d_pro", "456", "OAK-D Pro"),
            ),
        ),
    )
    profile_path = tmp_path / "calibration-profiles.json"
    write_profile_collection(
        [calibrated_profile_for_rectified_fixture(synchronized_sensor)],
        profile_path,
    )

    result = subprocess.run(
        [
            *export_command(run_root),
            "--calibration-profiles",
            str(profile_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "missing enabled sensor folder(s): luxonis_456" in result.stderr
    assert not (run_root / BOP_DIR).exists()


def test_bop_export_rejects_profile_with_wrong_run_mount(tmp_path: Path) -> None:
    run_root, _sensor = create_rectified_sensor_fixture(tmp_path)
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
                    distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
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
    assert manifest["output_artifact_set"]["schema_version"] == (
        "bop_output_artifact_set.v1"
    )
    assert len(manifest["output_artifact_set"]["sha256"]) == 64
    assert validate_bop_export_artifact_set(bop) == manifest["output_artifact_set"]
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


def test_managed_bop_export_requires_run_owned_calibration_selection(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    command = [
        item for item in export_command(run_root) if item != "--diagnostic-unmanaged"
    ]

    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "requires a run-owned calibration_profile_selection.v2" in result.stderr
    assert not (run_root / BOP_DIR).exists()


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("grayscale", "Invalid BOP RGB-D pixel contract"),
        ("valid_changed_pixels", "bytes do not match"),
    ],
)
def test_bop_export_rejects_corrupted_copied_rgb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    message: str,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    sensor = run_root / "processed" / "synchronized" / "realsense_123"
    output = tmp_path / "bop-output"
    original_copy2 = bop_writer.shutil.copy2
    corrupted = False

    def corrupt_first_rgb_copy(source: str | Path, destination: str | Path):
        nonlocal corrupted
        result = original_copy2(source, destination)
        destination_path = Path(destination)
        if not corrupted and destination_path.parent.name == RGB_DIR:
            copied = cv2.imread(
                destination_path.as_posix(),
                cv2.IMREAD_UNCHANGED,
            )
            assert copied is not None
            if corruption == "grayscale":
                replacement = np.zeros(copied.shape[:2], dtype=np.uint8)
            else:
                replacement = copied.copy()
                replacement[0, 0, 0] ^= np.uint8(1)
            assert cv2.imwrite(destination_path.as_posix(), replacement)
            corrupted = True
        return result

    monkeypatch.setattr(bop_writer.shutil, "copy2", corrupt_first_rgb_copy)

    with pytest.raises(ValueError, match=message):
        export_sensor_scene_to_bop(
            sensor,
            output,
            annotation_mode="none",
            object_name_to_id={},
        )

    assert corrupted is True


def test_bop_artifact_set_rejects_valid_post_manifest_pixel_tamper(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    subprocess.run(
        export_command(run_root),
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        text=True,
        capture_output=True,
    )
    bop = run_root / BOP_DIR
    rgb_path = bop / "test" / "000001" / RGB_DIR / "000000.png"
    rgb = cv2.imread(rgb_path.as_posix(), cv2.IMREAD_UNCHANGED)
    assert rgb is not None
    rgb[0, 0, 0] ^= np.uint8(1)
    assert cv2.imwrite(rgb_path.as_posix(), rgb)

    with pytest.raises(ValueError, match="artifact set is stale or mismatched"):
        validate_bop_export_artifact_set(bop)


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
        check=False,
        text=True,
        capture_output=True,
    )

    assert explicit_result.returncode != 0
    assert "canonical <run>/bop" in explicit_result.stderr
    assert not explicit_output.exists()


def test_bop_export_rejects_run_root_output_without_touching_raw_data(
    tmp_path: Path,
) -> None:
    run_root = create_synchronized_sensor_fixture(tmp_path)
    sentinel = run_root / "raw-capture-sentinel.txt"
    sentinel.write_text("preserve me\n")
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            *export_command(run_root),
            "--output-folder",
            str(run_root),
            "--overwrite",
        ],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "canonical <run>/bop" in result.stderr
    assert sentinel.read_text() == "preserve me\n"
    assert (run_root / "run_config.json").is_file()


def test_bop_export_rejects_symlinked_run_root_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    run_root = create_synchronized_sensor_fixture(real_parent)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    aliased_run = linked_parent / run_root.name

    with pytest.raises(ValueError, match="symlink components"):
        validate_managed_export_paths(
            aliased_run,
            aliased_run / "processed" / "synchronized",
            aliased_run / BOP_DIR,
        )


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
