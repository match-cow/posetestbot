from __future__ import annotations

import copy

import hashlib

import json

import sys

from pathlib import Path

import cv2

import numpy as np

import pytest


from posetestbot.calibration import target_library

from posetestbot.calibration.posegridgen import (
    POSEGRIDGEN_REVISION,
    posegridgen_capabilities,
    posegridgen_status,
)

from posetestbot.calibration.target_library import (
    CalibrationTargetConflict,
    delete_target_bundle,
    generate_target_bundle,
    replacement_blockers,
    select_target_bundle,
    validate_run_target_selection,
    validate_target_bundle,
)

from posetestbot.calibration.targets import (
    geometry_sha256,
    normalize_calibration_target_spec,
    opencv_grid_board,
)

from posetestbot.io.artifacts import ARUCO_DETECTIONS, RAW_ROBOT_EE_POSES, RUN_CONFIG

from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_configs_from_values,
    write_run_config_with_manifest,
)


def aruco_configuration(*, with_pose: bool = False) -> dict:
    value = copy.deepcopy(posegridgen_capabilities()["defaults"])
    value["page"] = {"paper_size": "A4", "orientation": "landscape"}
    value["board"].update(
        {
            "dictionary": "DICT_5X5_50",
            "rows": 2,
            "columns": 3,
            "marker_size_mm": 30.0,
            "separation_mm": 10.0,
        }
    )
    value["print_compensation"] = {"x_percent": 101.0, "y_percent": 99.0}
    value["annotations"] = {
        "show_ruler": False,
        "show_parameters": False,
        "show_frame_legend": False,
    }
    if with_pose:
        value["coordinate_frame"] = {
            "enabled": True,
            "pose": {
                "translation_x_m": 0.1,
                "translation_y_m": -0.2,
                "translation_z_m": 0.3,
                "roll_deg": 10.0,
                "pitch_deg": 20.0,
                "yaw_deg": 30.0,
            },
        }
    return value


def test_pinned_loader_is_private_and_reports_renderer_capabilities() -> None:
    original_path = list(sys.path)
    status = posegridgen_status()
    capabilities = posegridgen_capabilities()

    assert status["available"] is True
    assert status["revision"] == POSEGRIDGEN_REVISION
    assert status["clean"] is True
    assert capabilities["board_types"] == ["aruco"]
    assert capabilities["defaults"]["board"]["type"] == "aruco"
    assert capabilities["paper_sizes_mm"]["A5"] == (148.0, 210.0)
    assert capabilities["paper_sizes_mm"]["A6"] == (105.0, 148.0)
    assert sys.path == original_path
    assert "backend.app" not in sys.modules
    assert any(name.startswith("_posetestbot_posegridgen_") for name in sys.modules)


@pytest.mark.parametrize(
    ("paper_size", "expected_page_mm"),
    (("A5", (148.0, 210.0)), ("A6", (105.0, 148.0))),
)
def test_din_a5_and_a6_generate_through_the_immutable_bundle_pipeline(
    tmp_path: Path,
    paper_size: str,
    expected_page_mm: tuple[float, float],
) -> None:
    configuration = aruco_configuration()
    configuration["page"] = {
        "paper_size": paper_size,
        "orientation": "portrait",
    }
    configuration["board"].update(
        {
            "rows": 2,
            "columns": 2,
            "marker_size_mm": 20.0,
            "separation_mm": 5.0,
        }
    )

    bundle = generate_target_bundle(
        display_name=f"DIN {paper_size}",
        configuration=configuration,
        library_root=tmp_path,
    )

    bundle_path = Path(bundle["bundle_path"])
    source = json.loads((bundle_path / "posegridgen_source.json").read_text())
    assert source["request"]["page"] == configuration["page"]
    assert source["page_bounds"]["width_mm"] == pytest.approx(expected_page_mm[0])
    assert source["page_bounds"]["height_mm"] == pytest.approx(expected_page_mm[1])
    assert (
        bundle["target"]["posegridgen"]["configuration"]["page"]
        == configuration["page"]
    )
    assert (bundle_path / "calibration_target.pdf").read_bytes().startswith(b"%PDF")


def test_anisotropic_geometry_is_authoritative_for_generic_opencv_board(
    tmp_path: Path,
) -> None:
    bundle = generate_target_bundle(
        display_name="101 by 99",
        configuration=aruco_configuration(),
        library_root=tmp_path,
    )
    target = bundle["target"]
    _dictionary, board = opencv_grid_board(target)
    points = [np.asarray(item) for item in board.getObjPoints()]

    assert target["schema_version"] == "calibration_target.v2"
    assert target["print_compensation"] == {
        "x_percent": 101.0,
        "y_percent": 99.0,
        "application": "already_applied",
    }
    assert target["target_bounds"]["width_mm"] == pytest.approx(111.1)
    assert target["target_bounds"]["height_mm"] == pytest.approx(69.3)
    assert points[0][1, 0] - points[0][0, 0] == pytest.approx(30.3)
    assert points[0][3, 1] - points[0][0, 1] == pytest.approx(29.7)
    assert points[1][0, 0] - points[0][1, 0] == pytest.approx(10.1)
    assert points[3][0, 1] - points[0][3, 1] == pytest.approx(9.9)
    assert target["geometry_sha256"] == geometry_sha256(target)

    object_points = np.concatenate(points).astype(np.float32)
    true_k = np.asarray(
        [[610.0, 0.0, 320.0], [0.0, 615.0, 240.0], [0.0, 0.0, 1.0]],
        dtype=float,
    )
    distortion = np.zeros(5)
    object_views = []
    image_views = []
    view_poses = []
    for index in range(18):
        rvec = np.asarray(
            [
                -0.22 + 0.04 * (index % 5),
                -0.16 + 0.05 * (index % 7),
                -0.08 + 0.02 * (index % 4),
            ],
            dtype=float,
        )
        tvec = np.asarray(
            [
                -85.0 + 30.0 * (index % 6),
                -65.0 + 32.0 * (index % 5),
                560.0 + 8.0 * index,
            ],
            dtype=float,
        )
        projected = cv2.projectPoints(object_points, rvec, tvec, true_k, distortion)[
            0
        ].reshape(-1, 2)
        object_views.append(object_points)
        image_views.append(projected.astype(np.float32))
        view_poses.append((rvec, tvec, projected))
    rms, recovered_k, _recovered_distortion, _rvecs, _tvecs = cv2.calibrateCamera(
        object_views,
        image_views,
        (640, 480),
        None,
        None,
    )
    assert rms < 1e-3
    assert np.allclose(recovered_k, true_k, atol=0.5)

    expected_rvec, expected_tvec, projected = view_poses[0]
    ids = board.getIds().reshape(-1, 1)
    matched_object, matched_image = board.matchImagePoints(
        [
            item.reshape(1, 4, 2).astype(np.float32)
            for item in projected.reshape(-1, 4, 2)
        ],
        ids,
    )
    success, recovered_rvec, recovered_tvec = cv2.solvePnP(
        matched_object,
        matched_image,
        true_k,
        distortion,
    )
    assert success is True
    assert np.allclose(recovered_tvec.reshape(3), expected_tvec, atol=1e-3)
    assert np.allclose(recovered_rvec.reshape(3), expected_rvec, atol=1e-3)


def test_v2_validation_rejects_hash_winding_bounds_and_dictionary_capacity(
    tmp_path: Path,
) -> None:
    target = generate_target_bundle(
        display_name="validation",
        configuration=aruco_configuration(),
        library_root=tmp_path,
    )["target"]

    bad_hash = copy.deepcopy(target)
    for corner in bad_hash["markers"][0]["corners_mm"]:
        corner[0] += 0.1
    with pytest.raises(ValueError, match="geometry_sha256"):
        normalize_calibration_target_spec(bad_hash)

    bad_winding = copy.deepcopy(target)
    bad_winding.pop("geometry_sha256")
    bad_winding["markers"][0]["corners_mm"].reverse()
    with pytest.raises(ValueError, match="consistent winding"):
        normalize_calibration_target_spec(bad_winding)

    outside = copy.deepcopy(target)
    outside.pop("geometry_sha256")
    outside["target_bounds"]["width_mm"] = 1
    with pytest.raises(ValueError, match="outside target_bounds"):
        normalize_calibration_target_spec(outside)

    capacity = copy.deepcopy(target)
    capacity.pop("geometry_sha256")
    capacity["markers"][0]["id"] = 50
    with pytest.raises(ValueError, match="capacity"):
        normalize_calibration_target_spec(capacity)


def test_bundle_hashes_tampering_selection_placements_and_replacement_blockers(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    bundle = generate_target_bundle(
        display_name="posed target",
        configuration=aruco_configuration(with_pose=True),
        library_root=library,
    )
    bundle_path = library / bundle["target_id"]
    validate_target_bundle(bundle_path, library_root=library)

    tampered = bundle_path / "calibration_target.pdf"
    original_pdf = tampered.read_bytes()
    tampered.write_bytes(original_pdf + b"tamper")
    with pytest.raises(ValueError, match="size"):
        validate_target_bundle(bundle_path, library_root=library)
    tampered.write_bytes(original_pdf)

    manifest_path = bundle_path / "calibration_target_bundle.json"
    original_manifest = manifest_path.read_bytes()
    source_path = bundle_path / "posegridgen_source.json"
    original_source = source_path.read_bytes()
    source = json.loads(original_source)
    marker = next(item for item in source["features"] if item["kind"] == "marker")
    for corner in marker["corners_mm"]:
        corner[0] += 1.0
    changed_source = json.dumps(source, indent=2, sort_keys=True).encode() + b"\n"
    source_path.write_bytes(changed_source)
    manifest = json.loads(original_manifest)
    manifest["files"]["source"]["size_bytes"] = len(changed_source)
    manifest["files"]["source"]["sha256"] = hashlib.sha256(changed_source).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="canonically agree"):
        validate_target_bundle(bundle_path, library_root=library)
    source_path.write_bytes(original_source)
    manifest_path.write_bytes(original_manifest)

    run = tmp_path / "run"
    write_run_config_with_manifest(
        run,
        create_run_config(
            capture_intent="calibration", bop_annotation_mode="none", run_root=run
        ),
    )
    selected = select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="template_base",
        library_root=library,
    )
    assert selected["status"] == "selected"
    assert "placement" not in json.loads((run / "calibration_target.json").read_text())
    assert validate_run_target_selection(run)["placement_mode"] == "unknown"
    assert validate_run_target_selection(run)["mounting_frame"] == "template_base"

    unchanged = select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="template_base",
        library_root=library,
    )
    assert unchanged["status"] == "unchanged"

    (run / "sync_report.json").write_text("{}\n")
    changed = select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="template_base_identity",
        mounting_frame="template_base",
        library_root=library,
    )
    assert changed["selection"]["placement"]["transform"]["translation_mm"] == [
        0.0,
        0.0,
        0.0,
    ]
    assert replacement_blockers(run) == []

    sensor = run / "processed" / "synchronized" / "realsense_1"
    sensor.mkdir(parents=True)
    (sensor / ARUCO_DETECTIONS).write_text("{}\n")
    assert replacement_blockers(run) == [
        f"processed/synchronized/realsense_1/{ARUCO_DETECTIONS}"
    ]
    with pytest.raises(CalibrationTargetConflict) as captured:
        select_target_bundle(
            run_root=run,
            target_id=bundle["target_id"],
            placement_mode="posegridgen_board_to_base",
            mounting_frame="template_base",
            library_root=library,
        )
    assert captured.value.blockers == replacement_blockers(run)

    posed_run = tmp_path / "posed-run"
    write_run_config_with_manifest(
        posed_run,
        create_run_config(
            capture_intent="calibration", bop_annotation_mode="none", run_root=posed_run
        ),
    )
    result = select_target_bundle(
        run_root=posed_run,
        target_id=bundle["target_id"],
        placement_mode="posegridgen_board_to_base",
        mounting_frame="template_base",
        library_root=library,
    )
    transform = result["selection"]["placement"]["transform"]
    assert transform["translation_mm"] == pytest.approx([100.0, -200.0, 300.0])
    source = json.loads((bundle_path / "posegridgen_source.json").read_text())
    qx, qy, qz, qw = source["board_to_base"]["quaternion_xyzw"]
    assert transform["rotation_quaternion_wxyz"] == pytest.approx([qw, qx, qy, qz])
    assert transform["source_base_frame_interpretation"] == "template_base"


def test_explicit_target_mounting_is_bound_to_camera_group_and_raw_capture(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    bundle = generate_target_bundle(
        display_name="robot-carried target",
        configuration=aruco_configuration(),
        library_root=library,
    )
    run = tmp_path / "static-run"
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
        run,
        create_run_config(
            capture_intent="calibration",
            bop_annotation_mode="none",
            run_root=run,
            sensors=sensors,
        ),
    )

    selected = select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="robot_flange",
        library_root=library,
    )

    assert selected["selection"]["placement"] == {
        "mode": "unknown",
        "mounting_frame": "robot_flange",
    }
    evidence = validate_run_target_selection(run)
    assert evidence["mounting_frame"] == "robot_flange"
    assert evidence["effective_mounting_frame"] == "robot_flange"

    with pytest.raises(ValueError, match="Static-camera calibration requires"):
        select_target_bundle(
            run_root=run,
            target_id=bundle["target_id"],
            placement_mode="unknown",
            mounting_frame="template_base",
            library_root=library,
        )
    with pytest.raises(ValueError, match="known template-base placement"):
        select_target_bundle(
            run_root=run,
            target_id=bundle["target_id"],
            placement_mode="template_base_identity",
            mounting_frame="robot_flange",
            library_root=library,
        )

    camera_metadata = run / "realsense_1" / "frame_metadata.jsonl"
    camera_metadata.parent.mkdir()
    camera_metadata.write_text("{}\n")
    (run / RAW_ROBOT_EE_POSES).write_text("{}\n")
    blockers = replacement_blockers(run)
    assert "realsense_1/frame_metadata.jsonl" in blockers
    assert RAW_ROBOT_EE_POSES in blockers
    unchanged = select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="robot_flange",
        library_root=library,
    )
    assert unchanged["status"] == "unchanged"


def test_bundle_validation_rejects_symlinks(tmp_path: Path) -> None:
    library = tmp_path / "library"
    bundle = generate_target_bundle(
        display_name="symlink test",
        configuration=aruco_configuration(),
        library_root=library,
    )
    bundle_path = library / bundle["target_id"]
    pdf = bundle_path / "calibration_target.pdf"
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(pdf.read_bytes())
    pdf.unlink()
    pdf.symlink_to(replacement)

    with pytest.raises(ValueError, match="symlink"):
        validate_target_bundle(bundle_path, library_root=library)


def test_selection_promotion_rolls_back_every_active_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    bundle = generate_target_bundle(
        display_name="rollback test",
        configuration=aruco_configuration(),
        library_root=library,
    )
    run = tmp_path / "run"
    write_run_config_with_manifest(
        run,
        create_run_config(
            capture_intent="calibration", bop_annotation_mode="none", run_root=run
        ),
    )
    select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="template_base",
        library_root=library,
    )
    tracked = [
        run / "calibration_targets" / bundle["target_id"],
        run / "calibration_target.json",
        run / "run_config.json",
        run / "dataset_manifest.json",
    ]
    before = {
        path: (
            sorted(
                (item.relative_to(path).as_posix(), item.read_bytes())
                for item in path.rglob("*")
                if item.is_file()
            )
            if path.is_dir()
            else path.read_bytes()
        )
        for path in tracked
    }
    original_replace = target_library.os.replace
    calls = {"count": 0, "failed": False}

    def fail_during_promotion(source, destination):
        calls["count"] += 1
        if calls["count"] == 6 and not calls["failed"]:
            calls["failed"] = True
            raise OSError("injected promotion failure")
        return original_replace(source, destination)

    monkeypatch.setattr(target_library.os, "replace", fail_during_promotion)
    with pytest.raises(OSError, match="injected promotion failure"):
        select_target_bundle(
            run_root=run,
            target_id=bundle["target_id"],
            placement_mode="template_base_identity",
            mounting_frame="template_base",
            library_root=library,
        )

    after = {
        path: (
            sorted(
                (item.relative_to(path).as_posix(), item.read_bytes())
                for item in path.rglob("*")
                if item.is_file()
            )
            if path.is_dir()
            else path.read_bytes()
        )
        for path in tracked
    }
    assert calls["failed"] is True
    assert after == before
    assert not list(run.glob(".*.bak"))
    assert not list((run / "calibration_targets").glob(".*.tmp"))


def test_bundle_deletion_protects_active_target_and_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    active = generate_target_bundle(
        display_name="active target",
        configuration=aruco_configuration(),
        library_root=library,
    )
    run = tmp_path / "run"
    write_run_config_with_manifest(
        run,
        create_run_config(
            capture_intent="calibration", bop_annotation_mode="none", run_root=run
        ),
    )
    select_target_bundle(
        run_root=run,
        target_id=active["target_id"],
        placement_mode="unknown",
        mounting_frame="template_base",
        library_root=library,
    )

    with pytest.raises(CalibrationTargetConflict) as captured:
        delete_target_bundle(
            target_id=active["target_id"],
            library_root=library,
            run_root=run,
        )
    assert captured.value.blockers == [RUN_CONFIG]
    assert (library / active["target_id"]).is_dir()

    removable = generate_target_bundle(
        display_name="remove me",
        configuration=aruco_configuration(),
        library_root=library,
    )
    removable_path = library / removable["target_id"]
    original_remove = target_library._remove_path

    def fail_before_removal(_path: Path) -> None:
        raise OSError("injected delete failure")

    monkeypatch.setattr(target_library, "_remove_path", fail_before_removal)
    with pytest.raises(OSError, match="injected delete failure"):
        delete_target_bundle(
            target_id=removable["target_id"],
            library_root=library,
            run_root=run,
        )
    validate_target_bundle(removable_path, library_root=library)
    assert not list(library.glob(".*.delete"))

    monkeypatch.setattr(target_library, "_remove_path", original_remove)
    result = delete_target_bundle(
        target_id=removable["target_id"],
        library_root=library,
        run_root=run,
    )
    assert result == {
        "status": "deleted",
        "target_id": removable["target_id"],
        "display_name": "remove me",
    }
    assert not removable_path.exists()
