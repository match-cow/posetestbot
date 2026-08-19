from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.bop import annotations
from posetestbot.bop.mask_driver import (
    GENERATION_REPORT,
    RENDERER_TYPE,
    TOOLKIT_REVISION,
    VISIBILITY_DELTA_MM,
    bop_gt_output_sha256,
)
from posetestbot.blenderproc.rendering import ANALYTIC_IMPLEMENTATION_REVISION
from posetestbot.io.atomic import atomic_write_json
from scripts.run_bop_annotations import build_annotation_commands


def _minimal_inputs(root: Path) -> Path:
    sensor = root / "processed" / "rectified" / "realsense_test"
    (sensor / "rgb").mkdir(parents=True)
    (sensor / "depth").mkdir()
    (sensor / "rgb" / "000001.png").write_bytes(b"rgb")
    (sensor / "depth" / "000001.png").write_bytes(b"depth")
    (sensor / "cam_K.txt").write_text(
        "100 0 32\n0 100 24\n0 0 1\n0 0 0 0 0\n",
        encoding="utf-8",
    )
    atomic_write_json(
        sensor / "match_robot_ee_poses.json",
        {
            "000001.png": {
                "robot_ee_pose": {
                    "X": 0,
                    "Y": 0,
                    "Z": 500,
                    "A": 0,
                    "B": 0,
                    "C": 0,
                }
            }
        },
    )
    (root / "bop").mkdir()
    atomic_write_json(
        root / "bop" / "bop_export_manifest.json",
        {
            "schema_version": "bop_export_manifest.v5",
            "annotation_source": "none",
            "annotation_state": "absent",
            "capabilities": {
                "pose_estimation_input": True,
                "bop19_evaluation": False,
            },
            "validation": {"frame_count": 1, "annotation_count": 0},
            "exports": [],
        },
    )
    atomic_write_json(root / "pose_template_selection.json", {"fixture": True})
    calibration = root / "processed" / "calibration.json"
    calibration.write_text("{}\n", encoding="utf-8")
    return calibration


def _patch_valid_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calibration: Path,
    toolkit_available: bool,
    configured_mode: str = "pose",
) -> None:
    monkeypatch.setattr(
        annotations,
        "load_run_config_for_run_root",
        lambda _root: {
            "dataset_mode": "pose_template",
            "capture": {"intent": "dataset"},
            "bop": {"annotation_mode": configured_mode},
        },
    )
    monkeypatch.setattr(
        annotations,
        "load_pose_template_selection",
        lambda _root: {
            "placement_confirmed": True,
            "template_uuid": "template",
            "bundle_sha256": "a" * 64,
            "instances": [{"obj_id": 4}],
        },
    )
    monkeypatch.setattr(
        annotations,
        "selected_calibration_profiles",
        lambda _root: calibration,
    )
    monkeypatch.setattr(
        annotations,
        "enabled_sensor_folder_names",
        lambda _root: ("realsense_test",),
    )
    monkeypatch.setattr(
        annotations,
        "_blenderproc_status",
        lambda: {
            "available": True,
            "required_version": "2.8.0",
            "detected_version": "2.8.0",
            "executable": "/tools/blenderproc",
            "install_command": None,
            "reason": None,
        },
    )
    monkeypatch.setattr(
        annotations,
        "toolkit_status",
        lambda _root: {
            "available": toolkit_available,
            "status": "ready" if toolkit_available else "unavailable",
            "revision": "revision" if toolkit_available else None,
            "required_revision": "required",
            "install_command": (
                None
                if toolkit_available
                else "bash scripts/install.sh --with-bop-toolkit"
            ),
            "reason": None if toolkit_available else "Toolkit runtime is missing.",
        },
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish_annotation_manifest(
    run: Path,
    scene: Path,
    *,
    mode: str,
) -> None:
    bop_root = run / "bop"
    provenance_path = scene / "posetestbot_gt_provenance.json"
    atomic_write_json(
        provenance_path,
        {
            "schema_version": "posetestbot_gt_provenance.v1",
            "blenderproc_version": "2.8.0",
            "supported_blenderproc_version": "2.8.0",
            "annotation_mode": mode,
            "pose_contract": "analytic_model_to_opencv_camera_rigid_transform.v1",
            "translation_unit": "mm",
            "rotation_storage": "row_major_3x3",
            "analytic_implementation": {
                "revision": ANALYTIC_IMPLEMENTATION_REVISION,
                "script_sha256": "a" * 64,
            },
            "frame_bindings": [
                {
                    "output_image_id": 0,
                    "source_frame_id": 0,
                    "source_filename": "000000.png",
                }
            ],
        },
    )
    scene_gt_path = scene / "scene_gt.json"
    pose_record = {
        "artifact": provenance_path.relative_to(bop_root).as_posix(),
        "sha256": _sha256(provenance_path),
        "schema_version": "posetestbot_gt_provenance.v1",
        "blenderproc_version": "2.8.0",
        "annotation_mode": mode,
        "pose_contract": "analytic_model_to_opencv_camera_rigid_transform.v1",
        "analytic_implementation": {
            "revision": ANALYTIC_IMPLEMENTATION_REVISION,
            "script_sha256": "a" * 64,
        },
        "calibration_profile_id": "realsense_test_profile",
        "frame_binding_count": 1,
        "scene_gt_sha256": _sha256(scene_gt_path),
    }
    pose_generation = {
        "source": "blenderproc_analytic_gt",
        "scenes": [
            {
                "scene_id": 1,
                "sensor_name": "realsense_test",
                **pose_record,
            }
        ],
    }
    if mode == "pose_and_masks":
        output_paths = [
            scene / "scene_gt_info.json",
            scene / "mask" / "000000_000000.png",
            scene / "mask_visib" / "000000_000000.png",
        ]
        mask_generation = {
            "schema_version": "posetestbot_bop_gt_generation.v1",
            "annotation_mode": "pose_and_masks",
            "pose_source": "blenderproc_scene_gt",
            "generator": "official_bop_toolkit_algorithms",
            "toolkit_revision": TOOLKIT_REVISION,
            "toolkit_clean_checkout": True,
            "upstream_algorithms": [
                "scripts/calc_gt_masks.py",
                "scripts/calc_gt_info.py",
            ],
            "renderer_type": RENDERER_TYPE,
            "visibility_delta_mm": VISIBILITY_DELTA_MM,
            "visibility_mode": "bop19",
            "depth_source": "exported_captured_depth",
            "artifact_path": GENERATION_REPORT,
            "split": "test",
            "scenes": {
                "1": {
                    "image_count": 1,
                    "annotation_count": 1,
                    "full_mask_count": 1,
                    "visible_mask_count": 1,
                }
            },
            "output_sha256": bop_gt_output_sha256(
                output_paths,
                root=bop_root,
            ),
        }
        atomic_write_json(bop_root / GENERATION_REPORT, mask_generation)
    else:
        mask_generation = {"state": "absent"}

    manifest_path = bop_root / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "annotation_source": "blenderproc",
            "annotation_mode": mode,
            "annotation_state": "complete" if mode == "pose_and_masks" else "poses",
            "capabilities": {
                "pose_estimation_input": True,
                "gt_annotations": True,
                "gt_poses": True,
                "bop19_evaluation": mode == "pose_and_masks",
            },
            "validation": {"frame_count": 1, "annotation_count": 1},
            "exports": [
                {
                    "sensor_name": "realsense_test",
                    "scene_folder": "test/000001",
                    "scene_id": 1,
                    "split": "test",
                    "rgb_count": 1,
                    "calibration_profile_id": "realsense_test_profile",
                    "annotation_provenance": pose_record,
                }
            ],
            "targets_path": "test_targets_bop19.json",
            "annotation_provenance": {
                "schema_version": "posetestbot_bop_gt_generation.v1",
                "annotation_mode": mode,
                "pose_generation": pose_generation,
                "mask_generation": mask_generation,
            },
        }
    )
    atomic_write_json(manifest_path, manifest)


def test_setup_keeps_pose_gt_available_without_mask_toolkit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    calibration = _minimal_inputs(run)
    _patch_valid_run(
        monkeypatch,
        calibration=calibration,
        toolkit_available=False,
    )

    setup = annotations.inspect_annotation_setup(run, app_root=tmp_path)

    assert setup["counts"] == {"sensors": 1, "frames": 1, "instances": 1}
    assert setup["configured_mode"] == "pose"
    assert setup["readiness_by_mode"]["pose"]["ready"] is True
    assert setup["readiness_by_mode"]["pose_and_masks"]["ready"] is False
    assert {
        item["code"] for item in setup["readiness_by_mode"]["pose"]["warnings"]
    } == {"pose_gt_not_evaluation_ready"}
    assert {
        item["code"]
        for item in setup["readiness_by_mode"]["pose_and_masks"]["blockers"]
    } == {"annotation_mode_not_configured", "bop_toolkit_unavailable"}


def test_setup_reports_current_pose_and_mask_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    calibration = _minimal_inputs(run)
    _patch_valid_run(
        monkeypatch,
        calibration=calibration,
        toolkit_available=True,
        configured_mode="pose_and_masks",
    )
    scene = run / "bop" / "test" / "000001"
    (scene / "mask").mkdir(parents=True)
    (scene / "mask_visib").mkdir()
    (scene / "depth").mkdir()
    mask = np.zeros((2, 3), dtype=np.uint8)
    mask[0, 1] = 255
    depth = np.zeros((2, 3), dtype=np.uint16)
    depth[0, 1] = 500
    assert cv2.imwrite(
        (scene / "mask" / "000000_000000.png").as_posix(),
        mask,
    )
    assert cv2.imwrite(
        (scene / "mask_visib" / "000000_000000.png").as_posix(),
        mask,
    )
    assert cv2.imwrite((scene / "depth" / "000000.png").as_posix(), depth)
    atomic_write_json(
        scene / "scene_gt.json",
        {
            "0": [
                {
                    "obj_id": 4,
                    "cam_R_m2c": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                    "cam_t_m2c": [0, 0, 500],
                }
            ]
        },
    )
    atomic_write_json(
        scene / "scene_gt_info.json",
        {
            "0": [
                {
                    "bbox_obj": [1, 0, 0, 0],
                    "bbox_visib": [1, 0, 0, 0],
                    "px_count_all": 1,
                    "px_count_valid": 1,
                    "px_count_visib": 1,
                    "visib_fract": 1.0,
                }
            ]
        },
    )
    atomic_write_json(
        run / "bop" / "test_targets_bop19.json",
        [{"scene_id": 1, "im_id": 0, "obj_id": 4, "inst_count": 1}],
    )
    _publish_annotation_manifest(run, scene, mode="pose_and_masks")

    output = annotations.inspect_annotation_setup(
        run,
        app_root=tmp_path,
    )["current_output"]

    assert output is not None
    assert output["mode"] == "pose_and_masks"
    assert output["annotation_count"] == 1
    assert output["mask_count"] == 1
    assert output["visible_mask_count"] == 1
    assert output["evaluation_ready"] is True
    assert output["blenderproc_version"] == "2.8.0"
    assert output["toolkit_revision"] == TOOLKIT_REVISION

    scene_gt = json.loads((scene / "scene_gt.json").read_text(encoding="utf-8"))
    scene_gt["0"][0]["cam_t_m2c"][2] = 501
    atomic_write_json(scene / "scene_gt.json", scene_gt)
    invalid = annotations.inspect_annotation_setup(
        run,
        app_root=tmp_path,
    )["current_output"]
    assert invalid is not None
    assert invalid["verified"] is False
    assert invalid["evaluation_ready"] is False
    assert "pose/provenance hashes" in invalid["integrity_error"]


def test_setup_rejects_internally_consistent_rewritten_mask_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    calibration = _minimal_inputs(run)
    _patch_valid_run(
        monkeypatch,
        calibration=calibration,
        toolkit_available=True,
        configured_mode="pose_and_masks",
    )
    scene = run / "bop" / "test" / "000001"
    (scene / "mask").mkdir(parents=True)
    (scene / "mask_visib").mkdir()
    (scene / "depth").mkdir()
    mask = np.zeros((2, 3), dtype=np.uint8)
    mask[0, 1] = 255
    depth = np.full((2, 3), 500, dtype=np.uint16)
    assert cv2.imwrite(scene.joinpath("mask/000000_000000.png").as_posix(), mask)
    assert cv2.imwrite(
        scene.joinpath("mask_visib/000000_000000.png").as_posix(),
        mask,
    )
    assert cv2.imwrite(scene.joinpath("depth/000000.png").as_posix(), depth)
    atomic_write_json(
        scene / "scene_gt.json",
        {
            "0": [
                {
                    "obj_id": 4,
                    "cam_R_m2c": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                    "cam_t_m2c": [0, 0, 500],
                }
            ]
        },
    )
    atomic_write_json(
        scene / "scene_gt_info.json",
        {
            "0": [
                {
                    "bbox_obj": [1, 0, 0, 0],
                    "bbox_visib": [1, 0, 0, 0],
                    "px_count_all": 1,
                    "px_count_valid": 1,
                    "px_count_visib": 1,
                    "visib_fract": 1.0,
                }
            ]
        },
    )
    atomic_write_json(
        run / "bop" / "test_targets_bop19.json",
        [{"scene_id": 1, "im_id": 0, "obj_id": 4, "inst_count": 1}],
    )
    _publish_annotation_manifest(run, scene, mode="pose_and_masks")
    assert annotations._current_output(run)["verified"] is True

    rewritten = np.zeros((2, 3), dtype=np.uint8)
    rewritten[1, 2] = 255
    assert cv2.imwrite(
        scene.joinpath("mask/000000_000000.png").as_posix(),
        rewritten,
    )
    assert cv2.imwrite(
        scene.joinpath("mask_visib/000000_000000.png").as_posix(),
        rewritten,
    )
    atomic_write_json(
        scene / "scene_gt_info.json",
        {
            "0": [
                {
                    "bbox_obj": [2, 1, 0, 0],
                    "bbox_visib": [2, 1, 0, 0],
                    "px_count_all": 1,
                    "px_count_valid": 1,
                    "px_count_visib": 1,
                    "visib_fract": 1.0,
                }
            ]
        },
    )

    invalid = annotations._current_output(run)
    assert invalid is not None
    assert invalid["verified"] is False
    assert invalid["evaluation_ready"] is False
    assert "output hash is invalid" in invalid["integrity_error"]


def test_setup_verifies_pose_only_without_claiming_evaluation_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    calibration = _minimal_inputs(run)
    _patch_valid_run(
        monkeypatch,
        calibration=calibration,
        toolkit_available=False,
    )
    scene = run / "bop" / "test" / "000001"
    scene.mkdir(parents=True)
    atomic_write_json(
        scene / "scene_gt.json",
        {
            "0": [
                {
                    "obj_id": 4,
                    "cam_R_m2c": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                    "cam_t_m2c": [0, 0, 500],
                }
            ]
        },
    )
    atomic_write_json(
        run / "bop" / "test_targets_bop19.json",
        [{"scene_id": 1, "im_id": 0, "obj_id": 4, "inst_count": 1}],
    )
    _publish_annotation_manifest(run, scene, mode="pose")

    output = annotations.inspect_annotation_setup(
        run,
        app_root=tmp_path,
    )["current_output"]

    assert output is not None
    assert output["mode"] == "pose"
    assert output["verified"] is True
    assert output["annotation_count"] == 1
    assert output["mask_count"] == 0
    assert output["visible_mask_count"] == 0
    assert output["evaluation_ready"] is False


@pytest.mark.parametrize("value", [None, "", "masks", "pose-only"])
def test_annotation_mode_is_closed_to_supported_products(value: object) -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        annotations.validate_annotation_mode(value)


@pytest.mark.parametrize("mode", ["pose", "pose_and_masks"])
def test_orchestration_binds_one_mode_through_all_three_stages(
    tmp_path: Path,
    mode: str,
) -> None:
    run = tmp_path / "run"
    calibration = run / "processed" / "calibration_profiles.json"

    commands = build_annotation_commands(
        run_root=run,
        calibration_profiles=calibration,
        mode=mode,
    )

    assert len(commands) == 3
    for command in commands:
        mode_index = command.index("--annotation-mode")
        assert command[mode_index + 1] == mode
        assert "--input-folder" not in command
    assert "--annotation-source" not in commands[0]
    assert "--annotation-source" not in commands[1]
    assert commands[2][commands[2].index("--annotation-source") + 1] == "blenderproc"
