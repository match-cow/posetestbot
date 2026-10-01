from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from posetestbot.calibration import attempts
from posetestbot.calibration import reprojection_refinement as refinement
from posetestbot.calibration.profiles import (
    CalibrationProfile,
    CalibrationStatus,
    CalibrationQuality,
    RigidTransform,
    TransformFrame,
    SCHEMA_VERSION,
)
from posetestbot.sensors.contracts import CameraIntrinsics, SensorType, MountingMode
from posetestbot.calibration.transforms import (
    invert_transform,
    robot_ee_to_reference,
    transform_from_record,
    transform_residual,
)


def _cameras() -> tuple[list[dict], np.ndarray, list[np.ndarray]]:
    rng = np.random.default_rng(127)
    objects = np.array(
        [[x, y, 0] for y in np.linspace(0, 200, 5) for x in np.linspace(0, 300, 6)]
    )
    k = np.array([[900, 0, 640], [0, 905, 360], [0, 0, 1.0]])
    y = np.eye(4)
    y[:3, 3] = [-120, -90, 1000]
    cameras, xs = [], []
    for index in range(2):
        x = refinement._delta(
            np.array([0.03, -0.02, 0.01, 35 * index, -20 * index, 50])
        )
        xs.append(x)
        rows = []
        for motion in range(12):
            for frame in range(2):
                pose = dict(
                    zip(
                        ("X", "Y", "Z", "A", "B", "C"),
                        np.r_[rng.uniform(-70, 70, 3), rng.uniform(-0.3, 0.3, 3)],
                        strict=True,
                    )
                )
                g = robot_ee_to_reference(pose)
                vision = invert_transform(g @ x) @ y
                pixels = cv2.projectPoints(
                    objects,
                    cv2.Rodrigues(vision[:3, :3])[0],
                    vision[:3, 3],
                    k,
                    np.zeros(5),
                )[0].reshape(-1, 2)
                rows.append(
                    {
                        "frame_id": f"{motion:02}_{frame}.png",
                        "source_frame_id": f"{motion:02}_{frame}.png",
                        "motion": f"motion_{motion:02}",
                        "reserved": False,
                        "robot_ee_pose": pose,
                        "G": g,
                        "objects": objects,
                        "pixels": pixels + rng.normal(0, 0.015, pixels.shape),
                        "vision_pose": vision
                        @ refinement._delta(
                            np.r_[rng.normal(0, 0.002, 3), rng.normal(0, 0.7, 3)]
                        ),
                    }
                )
        reserved = copy.deepcopy(rows[0])
        reserved.update(
            frame_id="reserved.png", source_frame_id="reserved.png", reserved=True
        )
        settled = copy.deepcopy(rows[1])
        settled.update(
            frame_id="first.png", source_frame_id="first.png", motion="first_settled"
        )
        rows.extend([reserved, settled])
        cameras.append(
            {
                "sensor_key": str(index),
                "K": k.copy(),
                "D": np.zeros(5),
                "rows": rows,
                "X": x @ refinement._delta(np.array([0.002, 0, 0, 1.5, 0, 0])),
                "Y": y @ refinement._delta(np.array([0, 0.002, 0, 0, 1.5, 0])),
            }
        )
    return cameras, y, xs


@pytest.fixture(scope="module")
def computed() -> tuple[list[dict], dict, np.ndarray, list[np.ndarray]]:
    cameras, y, xs = _cameras()
    result = refinement._compute(cameras)
    return cameras, result, y, xs


def test_shared_grid_recovers_metric_transforms_without_changing_intrinsics(computed):
    cameras, result, y, xs = computed
    assert result["status"] == "passing"
    assert (
        transform_residual(y, transform_from_record(result["grid_to_template_base"]))[
            "translation_mm"
        ]
        < 0.05
    )
    for camera, sensor, x in zip(cameras, result["sensors"], xs, strict=True):
        delta = transform_residual(
            x, transform_from_record(sensor["camera_to_robot_flange"])
        )
        assert delta["translation_mm"] < 0.05
        assert delta["rotation_deg"] < 0.005
        assert sensor["motion_disjoint_audit"]["after"]["mean"] < 0.04
        assert {"first.png", "reserved.png"}.isdisjoint(
            sensor["training_source_frames"]
        )
        np.testing.assert_array_equal(
            camera["K"], [[900, 0, 640], [0, 905, 360], [0, 0, 1]]
        )
    for fold in result["motion_folds"]:
        assert set(fold["training_motions"]).isdisjoint(fold["validation_motions"])
        assert len(fold["training_motions"]) == 8


def test_refinement_fails_when_shared_motion_support_is_incomplete():
    cameras, _, _ = _cameras()
    cameras[1]["rows"] = [
        r for r in cameras[1]["rows"] if r["motion"] in {"motion_00", "motion_01"}
    ]
    with pytest.raises(ValueError, match="nine shared motion"):
        refinement._compute(cameras)


@pytest.mark.parametrize(
    "change", ["report_after_request", "input", "solution", "fold", "schema"]
)
def test_promotion_rejects_changed_or_nonreproducible_evidence(
    tmp_path, monkeypatch, computed, change
):
    cameras, result, _, _ = computed
    attempt_id, refinement_id = "a" * 32, "b" * 32
    attempt = {
        "attempt_id": attempt_id,
        "request": {"target_id": "target", "target": {"geometry_sha256": "hash"}},
    }
    selections = {"0": "candidate_0", "1": "candidate_1"}
    report = {
        "schema_version": refinement.SCHEMA_VERSION,
        "implementation_revision": refinement.IMPLEMENTATION_REVISION,
        "policy": refinement.POLICY,
        "attempt_id": attempt_id,
        "refinement_id": refinement_id,
        "target_id": "target",
        "geometry_sha256": "hash",
        "selections": selections,
        "input_bindings": [{"path": "raw", "sha256": "hash", "size_bytes": 3}],
        **copy.deepcopy(result),
    }
    path = refinement.refinement_report_path(
        attempts.calibration_attempt_root(tmp_path, attempt_id), refinement_id
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))
    binding = refinement._binding(tmp_path, path)
    monkeypatch.setattr(
        refinement,
        "_inputs",
        lambda *_: (cameras, [{"path": "raw", "sha256": "hash", "size_bytes": 3}]),
    )
    monkeypatch.setattr(refinement, "_compute", lambda _: result)
    assert (
        refinement.validate_reprojection_refinement(
            tmp_path, attempt, refinement_id, selections, expected_binding=binding
        )
        == binding
    )
    if change == "input":
        report["input_bindings"][0]["sha256"] = "changed"
    elif change == "solution":
        report["grid_to_template_base"]["matrix"][0][3] += 1
    elif change == "fold":
        report["motion_folds"][0]["training_motions"].append("heldout_motion")
    elif change == "schema":
        report["schema_version"] = "retired"
    else:
        report["status"] = "failed"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        refinement.validate_reprojection_refinement(
            tmp_path,
            attempt,
            refinement_id,
            selections,
            expected_binding=binding if change == "report_after_request" else None,
        )


def test_refinement_paths_reject_traversal_and_symlinks(tmp_path: Path):
    with pytest.raises(ValueError, match="identity"):
        refinement.refinement_report_path(tmp_path, "../other")
    directory = tmp_path / refinement.DIRECTORY
    directory.mkdir()
    (directory / ("a" * 32)).symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        refinement.refinement_report_path(tmp_path, "a" * 32)
    (directory / ("a" * 32)).unlink()
    directory.rmdir()
    directory.symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        refinement.refinement_report_path(tmp_path, "a" * 32)


def test_refined_profiles_retain_seed_and_timing_provenance(
    tmp_path, monkeypatch, computed
):
    _, report, _, _ = computed
    attempt_id, refinement_id = "a" * 32, "b" * 32
    path = refinement.refinement_report_path(
        attempts.calibration_attempt_root(tmp_path, attempt_id), refinement_id
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report))
    binding = refinement._binding(tmp_path, path)
    monkeypatch.setattr(
        refinement, "validate_reprojection_refinement", lambda *_, **__: binding
    )
    base = CalibrationProfile(
        schema_version=SCHEMA_VERSION,
        profile_id="seed_0",
        sensor_id="0",
        sensor_type=SensorType.REALSENSE_D435,
        mounting_mode=MountingMode.EYE_IN_HAND,
        rig_position="wrist",
        status=CalibrationStatus.VALID,
        intrinsics=CameraIntrinsics((900, 0, 640, 0, 905, 360, 0, 0, 1), 1280, 720),
        extrinsics=RigidTransform(
            TransformFrame.CAMERA, TransformFrame.ROBOT_FLANGE, (1, 0, 0, 0), (0, 0, 0)
        ),
        quality=CalibrationQuality(
            num_observations=100,
            num_inliers=90,
            residual_translation_mm=1,
            residual_rotation_deg=0.2,
        ),
        sync_delta_ms=-25,
        metadata={
            "sensor_key": "0",
            "candidate_id": "candidate_0",
            "companion_transform": report["grid_to_template_base"],
            "promotion_solver_provenance": {
                "pnp_method": "SQPNP",
                "extrinsic_method": "tsai",
            },
        },
    )
    other = replace(
        base,
        profile_id="seed_1",
        sensor_id="1",
        metadata={**base.metadata, "sensor_key": "1", "candidate_id": "candidate_1"},
    )
    refined = refinement.apply_reprojection_refinement(
        tmp_path,
        {"attempt_id": attempt_id},
        [base, other],
        {"refinement_id": refinement_id, "report": binding},
        {"0": "candidate_0", "1": "candidate_1"},
    )
    for profile, source, sensor in zip(
        refined, [base, other], report["sensors"], strict=True
    ):
        profile.validate()
        assert profile.profile_id != source.profile_id
        assert profile.intrinsics == source.intrinsics
        assert profile.sync_delta_ms == source.sync_delta_ms
        assert (
            profile.quality.residual_translation_mm
            == source.quality.residual_translation_mm
        )
        assert (
            profile.metadata["promotion_solver_provenance"]
            == source.metadata["promotion_solver_provenance"]
        )
        assert (
            profile.metadata["companion_transform"] == report["grid_to_template_base"]
        )
        assert profile.metadata["reprojection_refinement"]["report"] == binding
        assert (
            profile.quality.mean_reprojection_error_px
            == sensor["motion_disjoint_audit"]["after"]["mean"]
        )


def test_reserved_views_follow_source_identity_after_timing_renumbers_frames(
    tmp_path, monkeypatch
):
    attempt_id = "a" * 32
    folder = attempts.calibration_attempt_root(tmp_path, attempt_id)
    folder.mkdir(parents=True)
    pose = {"X": 0, "Y": 0, "Z": 0, "A": 0, "B": 0, "C": 0}
    transform = {
        "rotation_quaternion_wxyz": [1, 0, 0, 0],
        "translation_mm": [0, 0, 900],
    }
    request = {
        "attempt_id": attempt_id,
        "mode": "eye_in_hand",
        "target_mounting": {"state": "estimated"},
        "sensor_keys": ["realsense_d435:0", "realsense_d435:1"],
        "target": {},
        "sensors": [
            {
                "sensor_key": f"realsense_d435:{i}",
                "sensor_name": f"sensor_{i}",
                "folder": f"sensor_{i}",
                "robot_pose_path": "raw_robot.json",
            }
            for i in range(2)
        ],
    }

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    for name in (
        "request.json",
        "ranking.json",
        "candidate_profiles.json",
        "time_offset_search.json",
    ):
        write(folder / name, {})
    write(tmp_path / "raw_robot.json", {"0": {"pose": pose}})
    write(
        folder / "observations.json",
        {
            "observations": [
                {
                    "source_frame_id": "raw.png",
                    "sensor_type": "realsense_d435",
                    "device_id": str(i),
                    "pnp_method": "SQPNP",
                    "robot_ee_pose": pose,
                    "target_to_camera": transform,
                }
                for i in range(2)
            ]
        },
    )
    write(
        folder / "pnp_candidates.json",
        {
            "sensors": [
                {
                    "sensor_key": s["sensor_key"],
                    "frames": [
                        {
                            "source_frame_id": "raw.png",
                            "frame_id": "000002.png",
                            "common_inlier_indices": list(range(6)),
                        }
                    ],
                }
                for s in request["sensors"]
            ]
        },
    )
    write(
        folder / "intrinsic_comparison.json",
        {
            "sensors": [
                {
                    "sensor_key": s["sensor_key"],
                    "comparison_split": {"heldout_views": ["000002.png"]},
                }
                for s in request["sensors"]
            ]
        },
    )
    profiles = []
    for i, sensor in enumerate(request["sensors"]):
        write(
            folder
            / "processed/synchronized"
            / sensor["sensor_name"]
            / "match_robot_ee_poses.json",
            {
                "000001.png": {
                    "source_frame_id": "raw.png",
                    "matched_robot_pose_index": 0,
                    "robot_ee_pose": pose,
                    "motion": "moving",
                    "source_rgb": "raw.png",
                }
            },
        )
        write(
            folder
            / "processed/preparation_synchronized"
            / sensor["sensor_name"]
            / "aruco_detections.json",
            {"target": {}, "frames": {"000002.png": {}}},
        )
        write(tmp_path / sensor["folder"] / "raw.png", {})
        profiles.append(
            SimpleNamespace(
                metadata={
                    "candidate_id": str(i),
                    "pnp_method": "SQPNP",
                    "companion_transform": transform,
                },
                intrinsics=CameraIntrinsics(
                    (900, 0, 640, 0, 905, 360, 0, 0, 1), 1280, 720
                ),
                extrinsics=RigidTransform(
                    TransformFrame.CAMERA,
                    TransformFrame.ROBOT_FLANGE,
                    (1, 0, 0, 0),
                    (0, 0, 0),
                ),
            )
        )
    monkeypatch.setattr(attempts, "_require_current_attempt_request", lambda _: None)
    monkeypatch.setattr(
        attempts,
        "_verify_robot_pose_artifact_bindings",
        lambda *_: {"raw_robot.json": {"0": {"pose": pose}}},
    )
    monkeypatch.setattr(refinement, "load_profile_collection", lambda _: profiles)
    monkeypatch.setattr(refinement, "opencv_grid_board", lambda _: (None, None))
    monkeypatch.setattr(refinement, "validate_target_identity", lambda *_, **__: None)
    monkeypatch.setattr(
        refinement, "_matched_points", lambda *_: (np.zeros((6, 3)), np.zeros((6, 2)))
    )
    cameras, _ = refinement._inputs(
        tmp_path,
        {"request": request, "progress": {"status": "complete"}},
        {s["sensor_key"]: str(i) for i, s in enumerate(request["sensors"])},
    )
    for camera in cameras:
        row = camera["rows"][0]
        assert row["frame_id"] == "000001.png"
        assert row["preparation_frame_id"] == "000002.png"
        assert row["reserved"] is True
        assert refinement._sample(camera["rows"]) == []


def test_explicit_refinement_replacement_preserves_and_binds_previous_promotion(
    tmp_path, monkeypatch
):
    attempt_id, previous_id, refinement_id = "a" * 32, "b" * 32, "c" * 32
    folder = attempts.calibration_attempt_root(tmp_path, attempt_id)
    folder.mkdir(parents=True)
    old_status = {
        "schema_version": attempts.PROMOTION_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "status": "promoted",
        "reprojection_refinement": {"refinement_id": previous_id},
    }
    prior_contents = {}
    for name in (
        "calibration_profiles.json",
        "intrinsic_calibration_profiles.json",
        "calibration_target.json",
        "run_config.json",
        "dataset_manifest.json",
    ):
        content = json.dumps({"previous": name})
        (tmp_path / name).write_text(content)
        prior_contents[name] = content
    (folder / "promotion.json").write_text(json.dumps(old_status))
    (folder / "promotion_request.json").write_text("{}")
    attempt = {
        "request": {},
        "progress": {"status": "complete"},
        "promotion": old_status,
    }
    monkeypatch.setattr(attempts, "load_calibration_attempt", lambda *_: attempt)
    monkeypatch.setattr(attempts, "_require_current_attempt_request", lambda _: None)
    monkeypatch.setattr(attempts, "_promotion_time_offset_evidence", lambda _: {})
    monkeypatch.setattr(
        attempts, "_promotion_selections", lambda *_: {"sensor": "candidate"}
    )
    monkeypatch.setattr(attempts, "_revalidate_joint_promotion", lambda *_: None)
    monkeypatch.setattr(
        refinement,
        "validate_reprojection_refinement",
        lambda *_, **__: {"path": "refinement", "sha256": "hash", "size_bytes": 1},
    )
    with pytest.raises(ValueError, match="already has promotion"):
        attempts.create_promotion_request(
            tmp_path, attempt_id, reprojection_refinement_id=refinement_id
        )
    with pytest.raises(ValueError, match="different report"):
        attempts.create_promotion_request(
            tmp_path,
            attempt_id,
            reprojection_refinement_id=previous_id,
            replace_promoted_refinement=True,
        )
    request = attempts.create_promotion_request(
        tmp_path,
        attempt_id,
        reprojection_refinement_id=refinement_id,
        replace_promoted_refinement=True,
    )
    status = json.loads((folder / "promotion.json").read_text())
    assert len(request["previous_promotion"]) == 7
    for binding in request["previous_promotion"]:
        assert refinement._binding(tmp_path, tmp_path / binding["path"]) == binding
        if Path(binding["path"]).name in prior_contents:
            assert (tmp_path / binding["path"]).read_text() == prior_contents[
                Path(binding["path"]).name
            ]
    assert (
        json.loads((tmp_path / request["previous_promotion"][1]["path"]).read_text())
        == old_status
    )
    assert status["previous_promotion"] == request["previous_promotion"]
    attempts._validate_promotion_request_identity(tmp_path, attempt_id, request, status)
    (tmp_path / request["previous_promotion"][0]["path"]).write_text("tampered")
    with pytest.raises(ValueError, match="prior promotion evidence changed"):
        attempts._validate_promotion_request_identity(
            tmp_path, attempt_id, request, status
        )
