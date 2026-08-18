from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from posetestbot.calibration.intrinsics import write_intrinsic_profile_collection
from posetestbot.calibration.profile_library import (
    list_calibration_library,
    select_calibration_profile_snapshot,
)
from posetestbot.calibration.profiles import (
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    CalibrationTargetType,
    RigidTransform,
    TransformFrame,
    write_profile_collection,
)
from posetestbot.io.artifacts import (
    CALIBRATION_PROFILE_SELECTION,
    INTRINSIC_CALIBRATION_PROFILES,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    load_run_config_for_run_root,
    write_run_config,
)
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType
from posetestbot.sync.calibration_policy import (
    resolve_calibration_profile_sync_policy,
)
from posetestbot.sync.non_destructive import resolve_sync_delta_ms


def _intrinsics() -> CameraIntrinsics:
    return CameraIntrinsics(
        cam_k=(900.0, 0.0, 640.0, 0.0, 900.0, 360.0, 0.0, 0.0, 1.0),
        width=1280,
        height=720,
        distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
        depth_scale_to_mm=1.0,
        distortion_model="brown_conrady",
        projection_source="factory_color_stream",
    )


def _sensor(device_id: str) -> SensorRunConfig:
    return SensorRunConfig(
        sensor_type="realsense_d435",
        device_id=device_id,
        display_name=f"D435 {device_id}",
        mounting_mode="eye_in_hand",
    )


def _profile(
    device_id: str,
    *,
    sync_delta_ms: Any,
    metadata_sync_delta_ms: Any | None = None,
    frame_timestamp_source: str | None = "sensor",
    robot_timestamp_source: str = "host_wall",
    max_nearest_pose_delta_ms: Any = 20.0,
    omit_sync_fields: tuple[str, ...] = (),
) -> CalibrationProfile:
    profile_id = f"{device_id}_eye_in_hand_auto"
    sensor_key = f"realsense_d435:{device_id}"
    recorded_delta = (
        sync_delta_ms if metadata_sync_delta_ms is None else metadata_sync_delta_ms
    )
    robot_offset = -float(recorded_delta or 0.0)
    synchronization = {
        "policy": "auto_offset",
        "status": "applied" if robot_offset else "kept_zero",
        "source": f"processed/calibration/{device_id}/time_offset_search.json",
        "robot_pose_time_offset_ms": robot_offset,
        "sync_delta_ms": recorded_delta,
        "timestamp_source": frame_timestamp_source,
        "frame_timestamp_source": frame_timestamp_source,
        "robot_timestamp_source": robot_timestamp_source,
        "required_frame_timestamp_domain": (
            "global_time" if frame_timestamp_source == "sensor" else None
        ),
        "timestamp_fallback_allowed": False,
        "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
        "auto_estimated_per_sensor_offset": True,
        "sensor_key": sensor_key,
        "quality_report": (
            f"processed/calibration/{device_id}/sync_quality_report.json"
        ),
    }
    for field in omit_sync_fields:
        synchronization.pop(field, None)
    return CalibrationProfile(
        schema_version="calibration.v2",
        profile_id=profile_id,
        sensor_id=device_id,
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=MountingMode.EYE_IN_HAND,
        rig_position="wrist",
        intrinsics=_intrinsics(),
        rectified_intrinsics=_intrinsics(),
        rectified_valid_roi=(0, 0, 1280, 720),
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=TransformFrame.ROBOT_FLANGE,
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(10.0, 20.0, 30.0),
        ),
        target_type=CalibrationTargetType.ARUCO_GRID,
        method="auto_compare:IPPE+shah",
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(
            num_observations=20,
            num_inliers=18,
            mean_reprojection_error_px=0.4,
        ),
        sync_delta_ms=sync_delta_ms,
        metadata={
            "sensor_key": sensor_key,
            "sensor_name": f"realsense_{device_id}",
            "intrinsic_profile_id": f"{device_id}_intrinsic",
            "synchronization": synchronization,
            "promotion_synchronization_provenance": {
                "source": synchronization.get("source"),
                "status": synchronization.get("status"),
                "robot_pose_time_offset_ms": robot_offset,
                "sync_delta_ms": recorded_delta,
            },
        },
    )


def _intrinsic_profile(device_id: str) -> dict[str, Any]:
    native = {
        "cam_K": list(_intrinsics().cam_k),
        "width": 1280,
        "height": 720,
        "distortion_model": "brown_conrady",
        "distortion": [0.0] * 5,
    }
    return {
        "schema_version": "intrinsic_calibration.v1",
        "profile_id": f"{device_id}_intrinsic",
        "sensor_id": device_id,
        "sensor_name": f"realsense_{device_id}",
        "resolution": [1280, 720],
        "orientation": "normal",
        "native": native,
        "rectified": {
            **native,
            "alpha": 0.0,
            "valid_roi": [0, 0, 1280, 720],
        },
        "depth": {
            "scale_to_mm": 1.0,
            "alignment": {"target": "rgb", "recalibrated": False},
        },
        "source": {"mode": "factory"},
        "quality": {"status": "accepted"},
    }


def _selected_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profiles: list[CalibrationProfile],
) -> tuple[Path, dict[str, Any]]:
    runs = tmp_path / "runs"
    source = runs / "calibration_source"
    run_root = runs / "object_run"
    source.mkdir(parents=True)
    run_root.mkdir(parents=True)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs.as_posix())

    sensors = tuple(_sensor(profile.sensor_id) for profile in profiles)
    write_profile_collection(profiles, source / "calibration_profiles.json")
    write_intrinsic_profile_collection(
        [_intrinsic_profile(profile.sensor_id) for profile in profiles],
        source / INTRINSIC_CALIBRATION_PROFILES,
    )
    write_run_config(
        source,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=source,
            sensors=sensors,
            run_name="Calibration source",
        ),
    )
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=sensors,
        ),
    )
    library = list_calibration_library(run_root)
    candidate = next(
        item
        for item in library["calibrations"]
        if item["source_run_root"] == source.as_posix()
    )
    assert candidate["compatible"], candidate["issues"]
    selected = select_calibration_profile_snapshot(
        run_root,
        source_run_root=source,
        expected_bundle_sha256=candidate["bundle_sha256"],
        operator="test-operator",
    )
    profile_ids = selected["sensor_profiles"]
    configured_sensors = tuple(
        replace(
            sensor,
            calibration_profile_id=profile_ids[
                f"{sensor.sensor_type}:{sensor.device_id}"
            ],
        )
        for sensor in sensors
    )
    selection = selected["selection"]
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=configured_sensors,
            calibration_profiles=selected["calibration_profiles"],
            intrinsic_calibration_profiles=selected["intrinsic_calibration_profiles"],
            calibration_profile_selection={
                "selection_artifact": CALIBRATION_PROFILE_SELECTION,
                "bundle_sha256": selection["source"]["bundle_sha256"],
                "selected_at": selection["selected_at"],
            },
        ),
    )
    return run_root, selected


def test_profile_sync_policy_preserves_manual_mode_without_selection(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "manual"
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(_sensor("manual-1"),),
            calibration_profiles="operator_profiles.json",
        ),
    )

    assert resolve_calibration_profile_sync_policy(run_root) is None


def test_profile_sync_policy_resolves_exact_same_family_camera_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, selected = _selected_run(
        tmp_path,
        monkeypatch,
        [
            _profile("camera-A", sync_delta_ms=-70.0),
            _profile(
                "camera-B",
                sync_delta_ms=-85.0,
                frame_timestamp_source="host_wall",
                robot_timestamp_source="host_wall",
                max_nearest_pose_delta_ms=12.5,
            ),
        ],
    )

    policy = resolve_calibration_profile_sync_policy(run_root)

    assert policy is not None
    assert json.loads(json.dumps(policy)) == policy
    assert policy["schema_version"] == "calibration_sync_policy.v1"
    assert policy["source"] == "selected_calibration_profile"
    assert policy["selection_artifact"] == CALIBRATION_PROFILE_SELECTION
    assert policy["bundle_sha256"] == selected["selection"]["source"]["bundle_sha256"]
    assert policy["calibration_profiles"] == {
        "relative_path": selected["calibration_profiles"],
        "sha256": selected["selection"]["snapshot"]["calibration_profiles"]["sha256"],
    }
    sensors = {item["sensor_key"]: item for item in policy["sensors"]}
    assert sensors["realsense_d435:camera-A"] == {
        "sensor_key": "realsense_d435:camera-A",
        "sensor_name": "realsense_camera-A",
        "sensor_folder": "realsense_camera-A",
        "sensor_type": "realsense_d435",
        "device_id": "camera-A",
        "profile_id": "camera-A_eye_in_hand_auto",
        "robot_pose_time_offset_ms": 70.0,
        "sync_delta_ms": -70.0,
        "frame_timestamp_source": "sensor",
        "robot_timestamp_source": "host_wall",
        "required_frame_timestamp_domain": "global_time",
        "timestamp_fallback_allowed": False,
        "max_nearest_pose_delta_ms": 20.0,
        "timing_source": ("processed/calibration/camera-A/time_offset_search.json"),
        "timing_policy": "auto_offset",
        "timing_status": "applied",
    }
    assert sensors["realsense_d435:camera-B"]["sync_delta_ms"] == -85.0
    assert sensors["realsense_d435:camera-B"]["frame_timestamp_source"] == ("host_wall")
    assert sensors["realsense_d435:camera-B"]["max_nearest_pose_delta_ms"] == 12.5

    exact_deltas = {
        item["sensor_folder"]: item["sync_delta_ms"] for item in policy["sensors"]
    }
    assert (
        resolve_sync_delta_ms(
            run_root / "realsense_camera-A",
            exact_deltas,
        )
        == -70.0
    )
    assert (
        resolve_sync_delta_ms(
            run_root / "realsense_camera-B",
            exact_deltas,
        )
        == -85.0
    )


@pytest.mark.parametrize(
    ("profile", "error"),
    [
        (
            _profile("camera-A", sync_delta_ms=None),
            "sync_delta_ms must be a finite number",
        ),
        (
            _profile("camera-A", sync_delta_ms=True),
            "sync_delta_ms must be a finite number",
        ),
        (
            _profile(
                "camera-A",
                sync_delta_ms=-70.0,
                metadata_sync_delta_ms=-75.0,
            ),
            "contradictory sync deltas",
        ),
        (
            _profile(
                "camera-A",
                sync_delta_ms=-70.0,
                omit_sync_fields=("quality_report",),
            ),
            "timing quality report must be a non-empty string",
        ),
        (
            _profile(
                "camera-A",
                sync_delta_ms=-70.0,
                omit_sync_fields=("frame_timestamp_source",),
            ),
            "frame_timestamp_source must be a non-empty string",
        ),
    ],
)
def test_profile_sync_policy_rejects_missing_or_contradictory_timing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: CalibrationProfile,
    error: str,
) -> None:
    run_root, _ = _selected_run(tmp_path, monkeypatch, [profile])

    with pytest.raises(ValueError, match=error):
        resolve_calibration_profile_sync_policy(run_root)


def test_profile_sync_policy_rejects_stale_current_sensor_profile_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _ = _selected_run(
        tmp_path,
        monkeypatch,
        [_profile("camera-A", sync_delta_ms=-70.0)],
    )
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    config["capture"]["sensors"][0]["calibration_profile_id"] = "other-profile"
    atomic_write_json(config_path, config)

    with pytest.raises(ValueError, match="profile selection is stale"):
        resolve_calibration_profile_sync_policy(run_root)


def test_profile_sync_policy_rejects_current_enabled_sensor_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _ = _selected_run(
        tmp_path,
        monkeypatch,
        [_profile("camera-A", sync_delta_ms=-70.0)],
    )
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    config["capture"]["sensors"].append(_sensor("camera-B").to_dict())
    atomic_write_json(config_path, config)

    with pytest.raises(ValueError, match="enabled sensors no longer match"):
        resolve_calibration_profile_sync_policy(run_root)


def test_profile_sync_policy_rejects_duplicate_current_sensor_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _ = _selected_run(
        tmp_path,
        monkeypatch,
        [_profile("camera-A", sync_delta_ms=-70.0)],
    )
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    config["capture"]["sensors"].append(dict(config["capture"]["sensors"][0]))
    atomic_write_json(config_path, config)

    with pytest.raises(ValueError, match="repeat identity"):
        resolve_calibration_profile_sync_policy(run_root)


def test_profile_sync_policy_rejects_stale_run_config_bundle_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _ = _selected_run(
        tmp_path,
        monkeypatch,
        [_profile("camera-A", sync_delta_ms=-70.0)],
    )
    config_path = run_root / "run_config.json"
    config = json.loads(config_path.read_text())
    config["calibration_profile_selection"]["bundle_sha256"] = "0" * 64
    atomic_write_json(config_path, config)

    with pytest.raises(ValueError, match="bundle does not match"):
        resolve_calibration_profile_sync_policy(run_root)


def test_profile_sync_policy_rejects_snapshot_hash_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _ = _selected_run(
        tmp_path,
        monkeypatch,
        [_profile("camera-A", sync_delta_ms=-70.0)],
    )
    config = load_run_config_for_run_root(run_root)
    snapshot = run_root / config["calibration_profiles"]
    os.chmod(snapshot, 0o644)
    snapshot.write_text("{}\n")

    with pytest.raises(ValueError, match="snapshot hash changed"):
        resolve_calibration_profile_sync_policy(run_root)
