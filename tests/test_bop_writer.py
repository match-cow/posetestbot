from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
import trimesh

from posetestbot.bop.writer import (
    BopSceneExport,
    finalize_official_scene_annotations,
    model_geometry_info,
    read_depth_scale,
    resolve_annotation_mode,
    targets_from_scene_gt,
    validate_bop_model_ply,
    validate_scene_gt,
    validate_official_scene_annotations,
    write_bop_model_ply,
    write_bop_export_manifest,
)


def test_depth_scale_reader_requires_current_sidecar(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Missing depth-scale sidecar"):
        read_depth_scale(tmp_path / "depthscale.txt")

    empty = tmp_path / "depthscale.txt"
    empty.write_text("")
    with pytest.raises(ValueError, match="sidecar is empty"):
        read_depth_scale(empty)


def test_large_model_diameter_is_exact_not_aabb_diagonal(tmp_path: Path) -> None:
    vertices = [
        (0.0, 0.0, 0.0),
        (3.0, 0.0, 0.0),
        (0.0, 4.0, 0.0),
        (0.0, 0.0, 12.0),
    ]
    vertices.extend(vertices[index % 4] for index in range(4_997))
    path = tmp_path / "large_tetrahedron.ply"
    lines = [
        "ply",
        "format ascii 1.0",
        f"element vertex {len(vertices)}",
        "property float x",
        "property float y",
        "property float z",
        "element face 0",
        "property list uchar int vertex_indices",
        "end_header",
        *(f"{x} {y} {z}" for x, y, z in vertices),
        "",
    ]
    path.write_text("\n".join(lines))

    info = model_geometry_info(path)

    assert math.isclose(float(info["diameter"]), math.sqrt(160.0))
    assert not math.isclose(float(info["diameter"]), 13.0)
    geometry = info["posetestbot_geometry"]
    assert geometry["diameter_method"] == "exact_convex_hull_vertex_pairwise"
    assert geometry["vertex_count"] == 5_001
    assert geometry["convex_hull_vertex_count"] == 4


def _scene_export(sensor_name: str, scene_id: int) -> BopSceneExport:
    return BopSceneExport(
        sensor_name=sensor_name,
        scene_id=scene_id,
        split="test",
        scene_folder=f"test/{scene_id:06d}",
        rgb_count=1,
        depth_count=1,
        artifacts={},
        frame_map={
            "0": {
                "source_rgb": "rgb/000010.png",
                "source_depth": "depth/000010.png",
                "bop_rgb": "rgb/000000.png",
                "bop_depth": "depth/000000.png",
            }
        },
    )


def test_export_manifest_requires_explicit_annotation_contract(tmp_path: Path) -> None:
    manifest_path = write_bop_export_manifest(
        tmp_path,
        [_scene_export("realsense_123", 1)],
        annotation_source="none",
        annotation_mode="none",
    )

    manifest = json.loads(manifest_path.read_text())
    assert manifest["annotation_source"] == "none"
    assert manifest["annotation_mode"] == "none"
    assert manifest["annotation_state"] == "absent"


def test_annotation_modes_require_an_exact_source_pair() -> None:
    assert resolve_annotation_mode("none", "none") == "none"
    assert resolve_annotation_mode("blenderproc", "pose_and_masks") == "pose_and_masks"
    assert resolve_annotation_mode("blenderproc", "pose") == "pose"
    with pytest.raises(ValueError, match="requires annotation_source"):
        resolve_annotation_mode("none", "pose")


def test_scene_gt_rejects_a_finite_but_non_rigid_rotation() -> None:
    with pytest.raises(ValueError, match="orthonormal rotation"):
        validate_scene_gt(
            {
                "0": [
                    {
                        "obj_id": 1,
                        "cam_R_m2c": [2, 0, 0, 0, 1, 0, 0, 0, 1],
                        "cam_t_m2c": [0, 0, 500],
                    }
                ]
            },
            frame_count=1,
            object_name_to_id={"fixture": 1},
        )


def test_manifest_records_pose_only_capability_and_provenance(tmp_path: Path) -> None:
    export = replace(
        _scene_export("realsense_123", 1),
        annotation_source="blenderproc",
        annotation_mode="pose",
    )

    manifest_path = write_bop_export_manifest(
        tmp_path,
        [export],
        annotation_source="blenderproc",
        annotation_mode="pose",
        annotation_provenance={
            "pose_source": "blenderproc_scene_gt",
            "masks": "absent",
        },
    )

    manifest = json.loads(manifest_path.read_text())
    assert manifest["annotation_source"] == "blenderproc"
    assert manifest["annotation_mode"] == "pose"
    assert manifest["annotation_state"] == "poses"
    assert manifest["annotation_provenance"] == {
        "pose_source": "blenderproc_scene_gt",
        "masks": "absent",
    }


def _write_official_annotation_fixture(root: Path) -> BopSceneExport:
    scene = root / "test" / "000001"
    (scene / "depth").mkdir(parents=True)
    (scene / "rgb").mkdir()
    (scene / "mask").mkdir()
    (scene / "mask_visib").mkdir()
    depth = np.full((4, 5), 500, dtype=np.uint16)
    assert cv2.imwrite((scene / "depth" / "000000.png").as_posix(), depth)
    assert cv2.imwrite(
        (scene / "rgb" / "000000.png").as_posix(),
        np.zeros((4, 5, 3), dtype=np.uint8),
    )
    (scene / "scene_gt.json").write_text(
        json.dumps(
            {
                "0": [
                    {
                        "obj_id": 1,
                        "cam_R_m2c": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                        "cam_t_m2c": [0, 0, 500],
                    }
                ]
            }
        )
    )
    full = np.zeros((4, 5), dtype=np.uint8)
    full[1:3, 1:3] = 255
    visible = np.zeros((4, 5), dtype=np.uint8)
    visible[1, 1] = 255
    assert cv2.imwrite((scene / "mask" / "000000_000000.png").as_posix(), full)
    assert cv2.imwrite(
        (scene / "mask_visib" / "000000_000000.png").as_posix(),
        visible,
    )
    (scene / "scene_gt_info.json").write_text(
        json.dumps(
            {
                "0": [
                    {
                        "bbox_obj": [1, 1, 1, 1],
                        "bbox_visib": [1, 1, 0, 0],
                        "px_count_all": 4,
                        "px_count_valid": 4,
                        "px_count_visib": 1,
                        "visib_fract": 0.25,
                    }
                ]
            }
        )
    )
    return BopSceneExport(
        sensor_name="realsense_123",
        scene_id=1,
        split="test",
        scene_folder="test/000001",
        rgb_count=1,
        depth_count=1,
        artifacts={"scene_gt": "test/000001/scene_gt.json"},
        annotation_source="blenderproc",
        annotation_mode="pose_and_masks",
    )


def test_official_mask_bundle_validation_and_finalization(tmp_path: Path) -> None:
    export = _write_official_annotation_fixture(tmp_path)

    assert validate_official_scene_annotations(tmp_path / export.scene_folder) == {
        "annotation_count": 1,
        "mask_count": 1,
        "visible_mask_count": 1,
    }
    finalized = finalize_official_scene_annotations(tmp_path, [export])

    assert finalized[0].annotation_mode == "pose_and_masks"
    assert finalized[0].targets == [
        {"scene_id": 1, "im_id": 0, "obj_id": 1, "inst_count": 1}
    ]
    assert finalized[0].artifacts["mask"] == "test/000001/mask"
    assert finalized[0].artifacts["mask_visib"] == "test/000001/mask_visib"


def test_official_mask_validation_rejects_non_binary_or_non_subset_masks(
    tmp_path: Path,
) -> None:
    export = _write_official_annotation_fixture(tmp_path)
    visible_path = tmp_path / export.scene_folder / "mask_visib" / "000000_000000.png"
    visible = np.zeros((4, 5), dtype=np.uint8)
    visible[0, 0] = 127
    assert cv2.imwrite(visible_path.as_posix(), visible)

    with pytest.raises(ValueError, match="exactly binary"):
        validate_official_scene_annotations(tmp_path / export.scene_folder)


def test_bop19_targets_count_only_instances_with_at_least_ten_percent_visibility() -> (
    None
):
    scene_gt = {
        "0": [
            {"obj_id": 1},
            {"obj_id": 1},
            {"obj_id": 2},
            {"obj_id": 2},
        ]
    }
    scene_gt_info = {
        "0": [
            {"visib_fract": 0.09},
            {"visib_fract": 0.1},
            {"visib_fract": 0.85},
            {"visib_fract": 0.0},
        ]
    }

    assert targets_from_scene_gt(
        scene_gt,
        scene_id=7,
        scene_gt_info=scene_gt_info,
    ) == [
        {"scene_id": 7, "im_id": 0, "obj_id": 1, "inst_count": 1},
        {"scene_id": 7, "im_id": 0, "obj_id": 2, "inst_count": 1},
    ]


def test_bop_model_texture_requires_and_preserves_uv_coordinates(
    tmp_path: Path,
) -> None:
    untextured = trimesh.creation.box()
    untextured_path = tmp_path / "untextured.ply"
    untextured_path.write_bytes(
        untextured.export(file_type="ply", encoding="binary_little_endian")
    )
    with pytest.raises(ValueError, match="no usable UV coordinates"):
        write_bop_model_ply(
            untextured_path,
            tmp_path / "invalid-textured.ply",
            texture_filename="obj_000001.png",
        )

    textured = trimesh.creation.box()
    uv = np.column_stack(
        (
            np.linspace(0.0, 1.0, len(textured.vertices)),
            np.linspace(1.0, 0.0, len(textured.vertices)),
        )
    )
    textured.visual = trimesh.visual.texture.TextureVisuals(uv=uv)
    textured_path = tmp_path / "textured.ply"
    textured_path.write_bytes(
        textured.export(file_type="ply", encoding="binary_little_endian")
    )
    exported_path = tmp_path / "obj_000001.ply"

    write_bop_model_ply(
        textured_path,
        exported_path,
        texture_filename="obj_000001.png",
    )

    assert validate_bop_model_ply(exported_path)["vertex_normals"] is True
    header = exported_path.read_bytes().split(b"end_header\n", 1)[0]
    assert b"comment TextureFile obj_000001.png" in header
    assert b"property double texture_u" in header
    assert b"property double texture_v" in header


def test_bop_model_validation_rejects_importer_specific_face_properties(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unsupported-face-property.ply"
    path.write_text(
        "\n".join(
            (
                "ply",
                "format ascii 1.0",
                "element vertex 3",
                "property float x",
                "property float y",
                "property float z",
                "property float nx",
                "property float ny",
                "property float nz",
                "element face 1",
                "property ushort stl",
                "property list uchar int vertex_indices",
                "end_header",
                "0 0 0 0 0 1",
                "1 0 0 0 0 1",
                "0 1 0 0 0 1",
                "0 3 0 1 2",
                "",
            )
        )
    )

    with pytest.raises(ValueError, match="unsupported face properties"):
        validate_bop_model_ply(path)
