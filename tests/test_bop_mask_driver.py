from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from posetestbot.bop import mask_driver, writer
from scripts.run_bop_export_stage import complete_official_mask_annotations
from tests.test_bop_writer import _write_official_annotation_fixture


def test_mask_runner_uses_isolated_pinned_toolkit_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_root = tmp_path / "checkout"
    toolkit = app_root / "third_party" / "bop_toolkit"
    runtime_python = (
        app_root / "tools" / "bop_toolkit_runtime" / ".venv" / "bin" / "python"
    )
    toolkit.mkdir(parents=True)
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("")
    bop_root = tmp_path / "run" / "bop"
    bop_root.mkdir(parents=True)
    monkeypatch.setattr(
        mask_driver,
        "_checkout_revision",
        lambda _toolkit_root: mask_driver.TOOLKIT_REVISION,
    )
    monkeypatch.setattr(
        mask_driver,
        "_checkout_is_clean",
        lambda _toolkit_root: True,
    )
    invocation: dict[str, Any] = {}

    def fake_runner(command, *, cwd, env, check):
        invocation.update(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "check": check,
            }
        )
        adapter = json.loads(Path(env["POSETESTBOT_BOP_ADAPTER_CONFIG"]).read_text())
        assert adapter["scene_ids"] == [1, 3]
        assert adapter["object_ids"] == [2, 7]
        assert adapter["image_size"] == [640, 480]
        report_path = Path(command[command.index("--report-path") + 1])
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": "posetestbot_bop_gt_generation.v1",
                    "annotation_mode": "pose_and_masks",
                    "pose_source": "blenderproc_scene_gt",
                    "generator": "official_bop_toolkit_algorithms",
                    "toolkit_revision": mask_driver.TOOLKIT_REVISION,
                    "toolkit_clean_checkout": True,
                    "upstream_algorithms": [
                        "scripts/calc_gt_masks.py",
                        "scripts/calc_gt_info.py",
                    ],
                    "renderer_type": "vispy",
                    "visibility_delta_mm": 15.0,
                    "visibility_mode": "bop19",
                    "depth_source": "exported_captured_depth",
                    "artifact_path": mask_driver.GENERATION_REPORT,
                    "split": "test",
                    "scenes": {
                        "1": {"annotation_count": 1},
                        "3": {"annotation_count": 2},
                    },
                    "output_sha256": "0" * 64,
                }
            )
        )

    report = mask_driver.run_official_bop_mask_generation(
        bop_root,
        split="test",
        scene_ids=[3, 1, 3],
        object_ids=[7, 2, 7],
        image_size=(640, 480),
        app_root=app_root,
        command_runner=fake_runner,
    )

    assert report["toolkit_revision"] == mask_driver.TOOLKIT_REVISION
    assert invocation["command"][:5] == [
        "uv",
        "run",
        "--project",
        (app_root / "tools" / "bop_toolkit_runtime").as_posix(),
        "--no-sync",
    ]
    assert invocation["command"].count("--bop-root") == 1
    assert invocation["cwd"] == app_root
    assert invocation["check"] is True
    assert invocation["env"]["EGL_PLATFORM"] == "surfaceless"
    assert invocation["env"]["PYOPENGL_PLATFORM"] == "egl"
    assert not list(bop_root.glob(".*.adapter.json"))


def test_export_postprocessor_assembles_targets_before_the_single_mask_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    export = _write_official_annotation_fixture(tmp_path)
    calls: list[dict[str, Any]] = []
    validated_scenes: list[Path] = []
    validate_masks = writer.validate_official_scene_annotations

    def track_validation(scene_folder):
        validated_scenes.append(scene_folder)
        return validate_masks(scene_folder)

    monkeypatch.setattr(writer, "validate_official_scene_annotations", track_validation)

    def fake_mask_runner(output_root, **kwargs):
        calls.append({"output_root": output_root, **kwargs})
        return {
            "schema_version": "posetestbot_bop_gt_generation.v1",
            "annotation_mode": "pose_and_masks",
            "generator": "official_bop_toolkit_algorithms",
        }

    exports, report = complete_official_mask_annotations(
        tmp_path,
        [export],
        [SimpleNamespace(obj_id=1)],
        split="test",
        mask_runner=fake_mask_runner,
    )

    assert report["annotation_mode"] == "pose_and_masks"
    assert exports[0].targets == [
        {"scene_id": 1, "im_id": 0, "obj_id": 1, "inst_count": 1}
    ]
    assert calls[0]["scene_ids"] == [1]
    assert calls[0]["object_ids"] == [1]
    assert calls[0]["image_size"] == (5, 4)
    assert not validated_scenes

    targets_path = writer.write_bop_targets(tmp_path, exports, split="test")
    validation = writer.validate_bop_dataset(
        tmp_path, exports, targets_path=targets_path
    )

    assert validation["target_count"] == 1
    assert validated_scenes == [tmp_path / export.scene_folder]


def test_official_algorithm_driver_uses_captured_depth_and_3x_info_canvas(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise the real driver loop with a deterministic fake toolkit surface."""

    alias = "ptbgt_fixture"
    bop_root = tmp_path / "bop"
    scene_root = bop_root / "test" / "000001"
    scene_root.mkdir(parents=True)
    saved_images: dict[str, np.ndarray] = {}
    saved_json: dict[str, Any] = {}
    renderer_calls: dict[str, Any] = {"objects": [], "renders": []}
    visibility_calls: list[dict[str, Any]] = []

    dataset_params = types.ModuleType("bop_toolkit_lib.dataset_params")
    dataset_params.get_split_params = lambda *_args, **_kwargs: {
        "im_size": (5, 4),
        "scene_ids": [1],
        "scene_camera_tpath": (
            bop_root / "test" / "{scene_id:06d}" / "scene_camera.json"
        ).as_posix(),
        "scene_gt_tpath": (
            bop_root / "test" / "{scene_id:06d}" / "scene_gt.json"
        ).as_posix(),
        "depth_tpath": (
            bop_root / "test" / "{scene_id:06d}" / "depth" / "{im_id:06d}.png"
        ).as_posix(),
        "mask_tpath": (
            bop_root
            / "test"
            / "{scene_id:06d}"
            / "mask"
            / "{im_id:06d}_{gt_id:06d}.png"
        ).as_posix(),
        "mask_visib_tpath": (
            bop_root
            / "test"
            / "{scene_id:06d}"
            / "mask_visib"
            / "{im_id:06d}_{gt_id:06d}.png"
        ).as_posix(),
        "scene_gt_info_tpath": (
            bop_root / "test" / "{scene_id:06d}" / "scene_gt_info.json"
        ).as_posix(),
    }
    dataset_params.get_model_params = lambda *_args, **_kwargs: {
        "obj_ids": [1, 2],
        "model_tpath": (bop_root / "models" / "obj_{obj_id:06d}.ply").as_posix(),
    }

    inout = types.ModuleType("bop_toolkit_lib.inout")
    intrinsic = np.asarray([[100.0, 0.0, 2.25], [0.0, 125.0, 0.75], [0.0, 0.0, 1.0]])
    inout.load_scene_camera = lambda _path: {
        0: {"cam_K": intrinsic, "depth_scale": 2.0}
    }
    inout.load_scene_gt = lambda _path: {
        0: [
            {
                "obj_id": 1,
                "cam_R_m2c": np.eye(3),
                "cam_t_m2c": np.asarray([0.0, 0.0, 500.0]),
            },
            {
                "obj_id": 1,
                "cam_R_m2c": np.eye(3),
                "cam_t_m2c": np.asarray([0.0, 0.0, 600.0]),
            },
            {
                "obj_id": 2,
                "cam_R_m2c": np.eye(3),
                "cam_t_m2c": np.asarray([0.0, 0.0, 700.0]),
            },
        ]
    }

    def load_depth(_path):
        depth = np.zeros((4, 5), dtype=np.float32)
        depth[1, 0] = 5.0
        depth[2, 4] = 15.0
        return depth

    inout.load_depth = load_depth

    def save_im(path, image):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        array = np.asarray(image).copy()
        saved_images[destination.relative_to(bop_root).as_posix()] = array
        destination.write_bytes(array.tobytes() or b"\0")

    def save_json(path, value):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(value)
        saved_json[destination.relative_to(bop_root).as_posix()] = json.loads(
            serialized
        )
        destination.write_text(serialized, encoding="utf-8")

    inout.save_im = save_im
    inout.save_json = save_json

    misc = types.ModuleType("bop_toolkit_lib.misc")

    def bbox(xs, ys, _im_size):
        return [xs.min(), ys.min(), xs.max() - xs.min(), ys.max() - ys.min()]

    misc.calc_2d_bbox = bbox

    visibility = types.ModuleType("bop_toolkit_lib.visibility")

    def visible_mask(captured, rendered, delta, *, visib_mode):
        visibility_calls.append(
            {
                "captured": np.asarray(captured).copy(),
                "rendered": np.asarray(rendered).copy(),
                "delta": delta,
                "mode": visib_mode,
            }
        )
        difference = rendered.astype(np.float32) - captured.astype(np.float32)
        return (rendered > 0) & ((difference <= delta) | (captured == 0))

    visibility.estimate_visib_mask_gt = visible_mask

    renderer_module = types.ModuleType("bop_toolkit_lib.rendering.renderer")

    class FakeRenderer:
        def add_object(self, obj_id, path):
            renderer_calls["objects"].append((obj_id, path))

        def render_object(self, obj_id, rotation, translation, fx, fy, cx, cy):
            renderer_calls["renders"].append(
                {
                    "obj_id": obj_id,
                    "rotation": rotation,
                    "translation": translation,
                    "intrinsics": (fx, fy, cx, cy),
                }
            )
            depth = np.zeros((12, 15), dtype=np.float32)
            if translation[2] == 500.0:
                # The first pixel is out of frame, the second is occluded,
                # and the third is where the captured depth is missing.
                depth[5, 4:7] = 40.0
            else:
                depth[6, 9] = 20.0 if obj_id == 1 else 90.0
            return {"depth": depth}

    def create_renderer(width, height, renderer_type, mode):
        renderer_calls["create"] = (width, height, renderer_type, mode)
        return FakeRenderer()

    renderer_module.create_renderer = create_renderer
    rendering = types.ModuleType("bop_toolkit_lib.rendering")
    rendering.renderer = renderer_module
    package = types.ModuleType("bop_toolkit_lib")
    package.dataset_params = dataset_params
    package.inout = inout
    package.misc = misc
    package.visibility = visibility
    monkeypatch.setitem(sys.modules, "bop_toolkit_lib", package)
    monkeypatch.setitem(
        sys.modules,
        "bop_toolkit_lib.dataset_params",
        dataset_params,
    )
    monkeypatch.setitem(sys.modules, "bop_toolkit_lib.inout", inout)
    monkeypatch.setitem(sys.modules, "bop_toolkit_lib.misc", misc)
    monkeypatch.setitem(sys.modules, "bop_toolkit_lib.visibility", visibility)
    monkeypatch.setitem(sys.modules, "bop_toolkit_lib.rendering", rendering)
    monkeypatch.setitem(
        sys.modules,
        "bop_toolkit_lib.rendering.renderer",
        renderer_module,
    )
    monkeypatch.setenv("POSETESTBOT_BOP_ADAPTER_LOADED", alias)

    report = mask_driver._compute_masks_and_info(
        SimpleNamespace(
            dataset_alias=alias,
            toolkit_revision=mask_driver.TOOLKIT_REVISION,
            bop_root=bop_root.as_posix(),
            datasets_path=tmp_path.as_posix(),
            split="test",
            renderer_type="vispy",
            delta_mm=15.0,
            report_path=(bop_root / mask_driver.GENERATION_REPORT).as_posix(),
        )
    )

    assert renderer_calls["create"] == (15, 12, "vispy", "depth")
    assert renderer_calls["objects"] == [
        (1, (bop_root / "models" / "obj_000001.ply").as_posix()),
        (2, (bop_root / "models" / "obj_000002.ply").as_posix()),
    ]
    assert renderer_calls["renders"][0]["intrinsics"] == (
        100.0,
        125.0,
        7.25,
        4.75,
    )
    assert [call["captured"].shape for call in visibility_calls] == [
        (1, 2),
        (1, 1),
        (1, 1),
    ]
    np.testing.assert_allclose(
        visibility_calls[0]["captured"],
        [[10.0 * math.sqrt(1.0 + (-2.25 / 100) ** 2 + (0.25 / 125) ** 2), 0.0]],
    )
    assert all(call["delta"] == 15.0 for call in visibility_calls)
    assert all(call["mode"] == "bop19" for call in visibility_calls)
    assert set(saved_images) == {
        f"test/000001/{kind}/000000_{index:06d}.png"
        for kind in ("mask", "mask_visib")
        for index in range(3)
    }
    expected_full = np.zeros((4, 5), dtype=np.uint8)
    expected_full[1, :2] = 255
    expected_visible = np.zeros_like(expected_full)
    expected_visible[1, 1] = 255
    np.testing.assert_array_equal(
        saved_images["test/000001/mask/000000_000000.png"], expected_full
    )
    np.testing.assert_array_equal(
        saved_images["test/000001/mask_visib/000000_000000.png"], expected_visible
    )
    scene_gt_info = saved_json["test/000001/scene_gt_info.json"]
    assert scene_gt_info["0"] == [
        {
            "bbox_obj": [-1, 1, 2, 0],
            "bbox_visib": [1, 1, 0, 0],
            "px_count_all": 3,
            "px_count_valid": 1,
            "px_count_visib": 1,
            "visib_fract": 1.0 / 3.0,
        },
        {
            "bbox_obj": [4, 2, 0, 0],
            "bbox_visib": [4, 2, 0, 0],
            "px_count_all": 1,
            "px_count_valid": 1,
            "px_count_visib": 1,
            "visib_fract": 1.0,
        },
        {
            "bbox_obj": [-1, -1, -1, -1],
            "bbox_visib": [-1, -1, -1, -1],
            "px_count_all": 1,
            "px_count_valid": 1,
            "px_count_visib": 0,
            "visib_fract": 0.0,
        },
    ]
    scene_gt = {"0": inout.load_scene_gt(scene_root / "scene_gt.json")[0]}
    assert writer.targets_from_scene_gt(
        scene_gt, scene_id=1, scene_gt_info=scene_gt_info
    ) == [{"scene_id": 1, "im_id": 0, "obj_id": 1, "inst_count": 2}]
    assert report["scenes"]["1"]["annotation_count"] == 3
    assert report["depth_source"] == "exported_captured_depth"
