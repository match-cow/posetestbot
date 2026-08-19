from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from posetestbot.calibration.profiles import (
    SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    CalibrationTargetType,
    RigidTransform,
    TransformFrame,
    blenderproc_camera_transform_map_from_profiles,
    load_profile,
    rectified_intrinsics_from_native,
    write_profile,
    write_profile_collection,
)
import pytest
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType


def static_profile() -> CalibrationProfile:
    return CalibrationProfile(
        schema_version=SCHEMA_VERSION,
        profile_id="zed_2i_SN0001_static_cell_top_v2026_01",
        sensor_id="SN0001",
        sensor_type=SensorType.ZED_2I,
        mounting_mode=MountingMode.STATIC,
        rig_position="cell_top",
        intrinsics=CameraIntrinsics(
            cam_k=(1.0, 0.0, 2.0, 0.0, 3.0, 4.0, 0.0, 0.0, 1.0),
            width=1280,
            height=720,
            distortion=(0.1, 0.2, 0.0, 0.0, 0.0),
            depth_scale_to_mm=1.0,
        ),
        rectified_intrinsics=CameraIntrinsics(
            cam_k=(1.1, 0.0, 2.0, 0.0, 3.1, 4.0, 0.0, 0.0, 1.0),
            width=1280,
            height=720,
            distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
            depth_scale_to_mm=1.0,
        ),
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=TransformFrame.TEMPLATE_BASE,
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(100.0, 200.0, 300.0),
        ),
        target_type=CalibrationTargetType.CHARUCO,
        method="fixture_static_solve",
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(
            num_observations=12,
            num_inliers=11,
            mean_reprojection_error_px=0.4,
        ),
        sync_delta_ms=2.5,
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


def test_calibration_profile_round_trips_with_baseline_json_keys(
    tmp_path: Path,
) -> None:
    profile = static_profile()
    path = tmp_path / "profile.json"

    write_profile(profile, path)
    value = json.loads(path.read_text())

    assert value["schema_version"] == "calibration.v2"
    assert value["intrinsics"]["native"]["cam_K"] == [
        1.0,
        0.0,
        2.0,
        0.0,
        3.0,
        4.0,
        0.0,
        0.0,
        1.0,
    ]
    assert value["intrinsics"]["rectified"]["distortion"] == [0.0] * 5
    assert value["extrinsics"]["from"] == "camera"
    assert value["extrinsics"]["to"] == "template_base"

    loaded = load_profile(path)
    assert loaded.profile_id == profile.profile_id
    assert loaded.intrinsics == profile.intrinsics
    assert loaded.rectified_intrinsics == profile.rectified_intrinsics
    assert loaded.rectified_valid_roi == tuple(
        value["intrinsics"]["rectified"]["valid_roi"]
    )


def test_nonzero_inverse_sdk_distortion_round_trips_without_opencv_rectification(
    tmp_path: Path,
) -> None:
    native = replace(
        static_profile().intrinsics,
        distortion_model="inverse_brown_conrady",
        projection_source="realsense_sdk_color_stream",
    )
    profile = replace(
        static_profile(),
        intrinsics=native,
        rectified_intrinsics=None,
        rectified_valid_roi=None,
    )
    path = tmp_path / "inverse-profile.json"

    write_profile(profile, path)
    loaded = load_profile(path)

    assert loaded.intrinsics.distortion_model == "inverse_brown_conrady"
    assert loaded.intrinsics.projection_source == "realsense_sdk_color_stream"
    assert rectified_intrinsics_from_native(loaded.intrinsics) is None


def test_exact_zero_inverse_sdk_distortion_keeps_rectified_projection(
    tmp_path: Path,
) -> None:
    native = replace(
        static_profile().intrinsics,
        distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
        distortion_model="inverse_brown_conrady",
        projection_source="realsense_sdk_color_stream",
    )
    profile = replace(
        static_profile(),
        intrinsics=native,
        rectified_intrinsics=None,
        rectified_valid_roi=None,
    )
    path = tmp_path / "zero-inverse-profile.json"

    write_profile(profile, path)
    serialized = json.loads(path.read_text())
    loaded = load_profile(path)

    assert serialized["intrinsics"]["rectified"] is not None
    assert serialized["intrinsics"]["rectified"]["distortion"] == [0.0] * 5
    assert loaded.rectified_intrinsics is not None
    assert loaded.rectified_intrinsics.distortion == (0.0,) * 5


def test_eye_in_hand_profile_requires_camera_to_robot_flange() -> None:
    profile = static_profile()
    invalid = replace(profile, mounting_mode=MountingMode.EYE_IN_HAND)

    try:
        invalid.validate()
    except ValueError as exc:
        assert "eye_in_hand calibration must transform camera to robot_flange" in str(
            exc
        )
    else:
        raise AssertionError("invalid eye-in-hand transform direction was accepted")


def test_blenderproc_transform_map_accepts_static_profiles() -> None:
    profile = static_profile()

    transform_map = blenderproc_camera_transform_map_from_profiles(
        [profile],
        ["zed_2i_SN0001"],
    )

    entry = transform_map["zed_2i_SN0001"]
    assert entry["mounting_mode"] == "static"
    assert entry["to"] == "template_base"
    assert entry["profile_id"] == "zed_2i_SN0001_static_cell_top_v2026_01"
    assert entry["position"] == [100.0, 200.0, 300.0]


def test_blenderproc_transform_map_requires_complete_run_mount_mapping() -> None:
    static = static_profile()
    eye_in_hand = replace(
        static,
        profile_id="zed_2i_SN0001_eye_in_hand_wrist_v2026_01",
        mounting_mode=MountingMode.EYE_IN_HAND,
        rig_position="wrist",
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=TransformFrame.ROBOT_FLANGE,
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(10.0, 20.0, 30.0),
        ),
        metadata={},
    )

    with pytest.raises(KeyError, match="No static calibration profile"):
        blenderproc_camera_transform_map_from_profiles(
            [eye_in_hand],
            ["zed_2i_SN0001"],
            mounting_modes_by_sensor_name={
                "zed_2i_SN0001": MountingMode.STATIC,
            },
        )

    with pytest.raises(KeyError, match="no mounting mode"):
        blenderproc_camera_transform_map_from_profiles(
            [static],
            ["zed_2i_SN0001"],
            mounting_modes_by_sensor_name={},
        )


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (
            replace(
                static_profile(),
                extrinsics=replace(
                    static_profile().extrinsics,
                    rotation_quaternion_wxyz=(2.0, 0.0, 0.0, 0.0),
                ),
            ),
            "normalized",
        ),
        (
            replace(
                static_profile(),
                intrinsics=replace(
                    static_profile().intrinsics,
                    depth_scale_to_mm=float("nan"),
                ),
            ),
            "depth_scale_to_mm",
        ),
        (
            replace(
                static_profile(),
                quality=replace(
                    static_profile().quality,
                    residual_translation_mm=-1.0,
                ),
            ),
            "nonnegative",
        ),
    ],
)
def test_calibration_profile_rejects_invalid_numeric_contracts(
    profile: CalibrationProfile, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        profile.validate()


def test_profile_collection_rejects_duplicate_valid_sensor_slot(
    tmp_path: Path,
) -> None:
    first = static_profile()
    second = replace(first, profile_id="second_valid_profile")

    with pytest.raises(ValueError, match="same sensor/mount/rig slot"):
        write_profile_collection([first, second], tmp_path / "profiles.json")
