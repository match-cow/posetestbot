from __future__ import annotations

import json
from pathlib import Path

import pytest

from posetestbot.calibration.profiles import (
    SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    RigidTransform,
    TransformFrame,
)
from posetestbot.calibration.static_reuse import (
    verify_static_profile_destination_reference,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType


RUN_ID = "11111111-1111-4111-8111-111111111111"


def _profile(sensor_id: str, mounting_mode: MountingMode) -> CalibrationProfile:
    static = mounting_mode == MountingMode.STATIC
    return CalibrationProfile(
        schema_version=SCHEMA_VERSION,
        profile_id=f"realsense_{sensor_id}_{mounting_mode.value}",
        sensor_id=sensor_id,
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=mounting_mode,
        rig_position="cell" if static else "wrist",
        intrinsics=CameraIntrinsics(
            cam_k=(600.0, 0.0, 320.0, 0.0, 600.0, 240.0, 0.0, 0.0, 1.0),
            width=640,
            height=480,
        ),
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=(
                TransformFrame.TEMPLATE_BASE if static else TransformFrame.ROBOT_FLANGE
            ),
            rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
            translation_mm=(10.0, 20.0, 30.0),
        ),
        status=CalibrationStatus.VALID,
        quality=CalibrationQuality(num_observations=8, num_inliers=8),
        metadata=(
            {
                "robot_pose_reference": {
                    "schema_version": "robot_pose_reference.v1",
                    "status": "verified",
                    "packet_schema_version": "robot_pose.v1",
                    "from": "robot_flange",
                    "to": "template_base",
                    "sunrise_reference_frame_path": (POSE_TEMPLATE_BASE_SUNRISE_PATH),
                }
            }
            if static
            else {}
        ),
    )


def _config(run_root: Path) -> dict:
    return create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        run_id=RUN_ID,
        sensors=(
            SensorRunConfig(
                "realsense_d435",
                "static",
                "Static",
                mounting_mode="static",
            ),
            SensorRunConfig(
                "realsense_d435",
                "wrist",
                "Wrist",
                mounting_mode="eye_in_hand",
            ),
        ),
    ).to_dict()


def _write_matched(path: Path, reference_path: str | None) -> None:
    packet = (
        {
            "schema_version": "robot_pose.v1",
            "packet_kind": "pose",
            "run_id": RUN_ID,
            "from_frame": "robot_flange",
            "to_frame": "template_base",
            "sunrise_reference_frame_path": reference_path,
        }
        if reference_path is not None
        else None
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"000000.png": {"source_packet": packet}}))


def test_static_only_reuse_requires_canonical_destination_config(
    tmp_path: Path,
) -> None:
    static = _profile("static", MountingMode.STATIC)

    result = verify_static_profile_destination_reference(
        tmp_path,
        _config(tmp_path),
        [static],
    )

    assert result is not None
    assert result["matched_robot_pose_artifacts"] == []
    invalid = _config(tmp_path)
    invalid["frames"]["robot_pose"]["sunrise_reference_frame_path"] = None
    with pytest.raises(ValueError, match="no Sunrise reference-frame path"):
        verify_static_profile_destination_reference(
            tmp_path,
            invalid,
            [static],
        )


@pytest.mark.parametrize(
    "matched_reference",
    (None, "/PoseTestBot/TemplateBase"),
    ids=("legacy-unverified", "wrong-frame"),
)
def test_mixed_reuse_rejects_unprovenanced_or_wrong_matched_robot_poses(
    tmp_path: Path,
    matched_reference: str | None,
) -> None:
    matched_path = tmp_path / "realsense_wrist" / "match_robot_ee_poses.json"
    _write_matched(matched_path, matched_reference)

    expected = (
        "must retain its robot_pose.v1 source_packet"
        if matched_reference is None
        else "does not use /PoseTestBot/PoseTemplateBase"
    )
    with pytest.raises(ValueError, match=expected):
        verify_static_profile_destination_reference(
            tmp_path,
            _config(tmp_path),
            [
                _profile("static", MountingMode.STATIC),
                _profile("wrist", MountingMode.EYE_IN_HAND),
            ],
            matched_robot_pose_paths_by_sensor_name={
                "realsense_wrist": matched_path,
            },
        )


def test_mixed_reuse_accepts_verified_pose_template_base_matches(
    tmp_path: Path,
) -> None:
    matched_path = tmp_path / "realsense_wrist" / "match_robot_ee_poses.json"
    _write_matched(matched_path, POSE_TEMPLATE_BASE_SUNRISE_PATH)

    result = verify_static_profile_destination_reference(
        tmp_path,
        _config(tmp_path),
        [
            _profile("static", MountingMode.STATIC),
            _profile("wrist", MountingMode.EYE_IN_HAND),
        ],
        matched_robot_pose_paths_by_sensor_name={"realsense_wrist": matched_path},
    )

    assert result is not None
    assert result["sunrise_reference_frame_path"] == (POSE_TEMPLATE_BASE_SUNRISE_PATH)
    assert result["eye_in_hand_profile_ids"] == ["realsense_wrist_eye_in_hand"]
