"""Run-scoped BOP19 result inspection, simulation, and evaluation artifacts.

This module is deliberately outside acquisition orchestration. It consumes an
already exported ``bop/`` tree and writes only below
``processed/bop_evaluation/``.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from posetestbot.io.atomic import atomic_write_json, atomic_write_text


RESULT_HEADER = ["scene_id", "im_id", "obj_id", "score", "R", "t", "time"]
RESULT_FILENAME_RE = re.compile(
    r"^(?P<method>[A-Za-z0-9][A-Za-z0-9-]*)_"
    r"(?P<dataset>[a-z][a-z0-9]*)-"
    r"(?P<split>[a-z][a-z0-9]*)"
    r"(?:-(?P<split_type>[A-Za-z0-9][A-Za-z0-9-]*))?"
    r"(?:_(?P<optional_id>[A-Za-z0-9][A-Za-z0-9-]*))?\.csv$"
)
RESULT_ID_RE = re.compile(r"^result-[0-9a-f]{12}$")
EVALUATION_ID_RE = re.compile(r"^evaluation-[0-9a-f]{12}$")
EXTERNAL_POSE_JOB_RE = re.compile(
    r"^pose-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROVENANCE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SLURM_JOB_ID_RE = re.compile(r"^[0-9]{1,32}$")
ESTIMATOR_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
DRIVER_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,95}$")
CONTRACT_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{2,127}$")
RUNTIME_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUNTIME_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
EVALUATION_ROOT = Path("processed") / "bop_evaluation"
RESULTS_DIR = "results"
EVALUATIONS_DIR = "evaluations"
RESULT_METADATA = "result.json"
EVALUATION_REQUEST = "request.json"
EVALUATION_PROGRESS = "progress.json"
EVALUATION_REPORT = "report.json"
DATASET_ADAPTER_REVISION = "posetestbot_bop19_dataset_adapter.v1"
TOOLKIT_REVISION = "cea62d651c7e395b2e1962b9749e4e89693c6ac4"
DEFAULT_VSD_DELTA_MM = 15.0
DEFAULT_RENDERER = "vispy"
MAX_RESULT_BYTES = 128 * 1024 * 1024
MAX_RESULT_ROWS = 10_000_000


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_files(root: Path, paths: Iterable[Path]) -> str:
    """Hash small evaluation-semantic artifacts by path and content."""

    digest = hashlib.sha256()
    seen: set[str] = set()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"Evaluation input escapes the BOP dataset root: {path}"
            ) from exc
        if relative in seen or not path.is_file() or path.is_symlink():
            continue
        seen.add(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _digest_file_metadata(root: Path, paths: Iterable[Path]) -> str:
    """Cheaply bind large image inputs for request-time compatibility checks."""

    digest = hashlib.sha256()
    seen: set[str] = set()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        stat = path.stat(follow_symlinks=False)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _digest_depth_content(root: Path, paths: Iterable[Path]) -> str:
    """Content-bind every target depth image inside a queued worker."""

    digest = hashlib.sha256()
    seen: set[str] = set()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _combined_digest(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _has_symlink_component(path: Path, *, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _resolve_bop_relative(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} must remain below the BOP dataset root")
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must remain below the BOP dataset root") from exc
    if _has_symlink_component(candidate, root=root):
        raise ValueError(f"{label} must not use symbolic links")
    return candidate


def _plain_file(path: Path, *, root: Path) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and not _has_symlink_component(path, root=root)
    )


def _png_size(
    path: Path,
    *,
    label: str = "image",
    expected_kind: str | None = None,
) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(26)
    if (
        len(header) != 26
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise ValueError(f"BOP {label} is not a valid PNG: {path}")
    bit_depth = header[24]
    color_type = header[25]
    if expected_kind == "depth" and (bit_depth != 16 or color_type != 0):
        raise ValueError(f"BOP {label} must be a 16-bit single-channel PNG: {path}")
    if expected_kind == "rgb" and (bit_depth != 8 or color_type not in {2, 6}):
        raise ValueError(f"BOP {label} must be an 8-bit RGB or RGBA PNG: {path}")
    size = (
        int.from_bytes(header[16:20], "big"),
        int.from_bytes(header[20:24], "big"),
    )
    if min(size) < 1:
        raise ValueError(f"BOP {label} has invalid dimensions: {path}")
    return size


def _finite_json_vector(
    value: Any,
    *,
    count: int,
    label: str,
) -> np.ndarray:
    if (
        not isinstance(value, list)
        or len(value) != count
        or any(type(item) not in {int, float} for item in value)
    ):
        raise ValueError(f"{label} must contain exactly {count} numbers")
    try:
        vector = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain exactly {count} numbers") from exc
    if vector.shape != (count,) or not np.all(np.isfinite(vector)):
        raise ValueError(f"{label} must contain finite numbers")
    return vector


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _public_issue_list(values: Iterable[Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for index, value in enumerate(values):
        if isinstance(value, Mapping):
            code = str(value.get("code") or f"issue_{index + 1}")
            message = str(value.get("message") or code)
        else:
            code = f"issue_{index + 1}"
            message = str(value)
        issues.append(_issue(code, message))
    return issues


def _dataset_inventory(run_root: str | Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    bop_root = root / "bop"
    manifest_path = bop_root / "bop_export_manifest.json"
    dataset_info_path = bop_root / "dataset_info.json"
    blockers: list[str] = []
    warnings: list[str] = []
    semantic_paths: list[Path] = []
    rgb_paths: list[Path] = []
    depth_paths: list[Path] = []

    if not bop_root.is_dir() or bop_root.is_symlink():
        return {
            "run_root": root,
            "bop_root": bop_root,
            "manifest": {},
            "dataset_info": {},
            "targets": [],
            "target_counts": {},
            "target_rows": {},
            "object_ids": set(),
            "image_size": None,
            "splits": [],
            "counts": {
                "scenes": 0,
                "images": 0,
                "objects": 0,
                "targets": 0,
                "annotations": 0,
            },
            "blockers": ["BOP export is missing for the selected run."],
            "warnings": [],
            "semantic_paths": [],
            "rgb_paths": [],
            "depth_paths": [],
        }
    if not _plain_file(manifest_path, root=bop_root):
        blockers.append("BOP export manifest is missing.")
        manifest: Mapping[str, Any] = {}
    else:
        try:
            loaded_manifest = _load_json(manifest_path)
            if not isinstance(loaded_manifest, Mapping):
                raise ValueError("manifest must be a JSON object")
            manifest = loaded_manifest
            semantic_paths.append(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"BOP export manifest is invalid: {exc}")
            manifest = {}

    if not _plain_file(dataset_info_path, root=bop_root):
        dataset_info: Mapping[str, Any] = {}
        blockers.append("BOP dataset_info.json is missing.")
    else:
        try:
            loaded_info = _load_json(dataset_info_path)
            if not isinstance(loaded_info, Mapping):
                raise ValueError("dataset_info.json must be a JSON object")
            dataset_info = loaded_info
            semantic_paths.append(dataset_info_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"BOP dataset_info.json is invalid: {exc}")
            dataset_info = {}

    schema_version = manifest.get("schema_version")
    if schema_version != "bop_export_manifest.v5":
        blockers.append(
            "BOP evaluation requires an annotation-capability-explicit "
            "bop_export_manifest.v5 export."
        )
    if (
        manifest.get("format") != "bop-scenewise"
        or manifest.get("layout") != "<split>/<scene_id>"
        or manifest.get("dataset_root") != "."
    ):
        blockers.append(
            "BOP export manifest does not declare the standard scenewise layout."
        )
    if (
        manifest.get("annotation_source") != "blenderproc"
        or manifest.get("annotation_state") != "complete"
    ):
        blockers.append(
            "BOP19 evaluation requires complete BlenderProc ground-truth annotations."
        )
    if dataset_info and (
        dataset_info.get("schema_version") != "posetestbot_bop_dataset_info.v1"
        or dataset_info.get("bop_format") != "scenewise"
    ):
        blockers.append("BOP dataset_info.json is not a supported scenewise record.")

    exports_value = manifest.get("exports")
    exports = (
        [item for item in exports_value if isinstance(item, Mapping)]
        if isinstance(exports_value, list)
        else []
    )
    if isinstance(exports_value, list) and len(exports) != len(exports_value):
        blockers.append("Every BOP export scene entry must be a JSON object.")
    if not exports:
        blockers.append("BOP export contains no scenes.")
    splits = sorted(
        {
            str(item.get("split"))
            for item in exports
            if isinstance(item.get("split"), str) and item.get("split")
        }
    )
    if len(splits) != 1:
        blockers.append("BOP19 evaluation requires exactly one exported split.")
    split = splits[0] if len(splits) == 1 else "test"

    targets_path_value = manifest.get("targets_path") or "test_targets_bop19.json"
    try:
        targets_path = _resolve_bop_relative(
            bop_root,
            targets_path_value,
            label="BOP19 targets_path",
        )
    except ValueError as exc:
        blockers.append(str(exc))
        targets_path = bop_root / "test_targets_bop19.json.invalid"
    targets: list[Mapping[str, Any]] = []
    if not _plain_file(targets_path, root=bop_root):
        blockers.append("BOP19 target list is missing.")
    else:
        try:
            loaded_targets = _load_json(targets_path)
            if not isinstance(loaded_targets, list):
                raise ValueError("target list must be a JSON array")
            targets = [item for item in loaded_targets if isinstance(item, Mapping)]
            if len(targets) != len(loaded_targets):
                raise ValueError("every target row must be a JSON object")
            semantic_paths.append(targets_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"BOP19 target list is invalid: {exc}")
    if not targets:
        blockers.append("BOP19 target list is empty.")

    models_eval = bop_root / "models_eval"
    models_info_path = models_eval / "models_info.json"
    object_ids: set[int] = set()
    models_info: Mapping[str, Any] = {}
    if not _plain_file(models_info_path, root=bop_root):
        blockers.append(
            "BOP evaluation models_info.json is missing below models_eval/."
        )
    else:
        try:
            value = _load_json(models_info_path)
            if not isinstance(value, Mapping):
                raise ValueError("models_info.json must be a JSON object")
            normalized_ids = [int(key) for key in value]
            if any(obj_id < 1 for obj_id in normalized_ids) or len(
                set(normalized_ids)
            ) != len(normalized_ids):
                raise ValueError("model object IDs must be unique positive integers")
            for key, info in value.items():
                if not isinstance(info, Mapping):
                    raise ValueError(f"model {key} metadata must be a JSON object")
                raw_diameter = info["diameter"]
                if type(raw_diameter) not in {int, float}:
                    raise ValueError(f"model {key} diameter must be numeric")
                diameter = float(raw_diameter)
                if not math.isfinite(diameter) or diameter <= 0:
                    raise ValueError(
                        f"model {key} diameter must be finite and positive"
                    )
            models_info = value
            object_ids = set(normalized_ids)
            semantic_paths.append(models_info_path)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"BOP evaluation model metadata is invalid: {exc}")
    for obj_id in sorted(object_ids):
        model_path = models_eval / f"obj_{obj_id:06d}.ply"
        if not _plain_file(model_path, root=bop_root):
            blockers.append(f"BOP evaluation model is missing for object {obj_id}.")
        else:
            semantic_paths.append(model_path)

    target_counts: Counter[tuple[int, int, int]] = Counter()
    target_rows: dict[tuple[int, int, int], Mapping[str, Any]] = {}
    target_scenes: set[int] = set()
    for index, target in enumerate(targets):
        try:
            raw_ids = (
                target["scene_id"],
                target["im_id"],
                target["obj_id"],
                target["inst_count"],
            )
        except KeyError:
            blockers.append(f"BOP19 target row {index + 1} is invalid.")
            continue
        if any(type(value) is not int for value in raw_ids):
            blockers.append(
                f"BOP19 target row {index + 1} must use integer IDs and count."
            )
            continue
        scene_id, im_id, obj_id, inst_count = raw_ids
        if scene_id < 0 or im_id < 0 or obj_id < 1 or inst_count < 1:
            blockers.append(f"BOP19 target row {index + 1} has invalid IDs or count.")
            continue
        key = (scene_id, im_id, obj_id)
        if key in target_rows:
            blockers.append(
                "BOP19 target list contains duplicate scene/image/object rows."
            )
            continue
        target_counts[key] = inst_count
        target_rows[key] = target
        target_scenes.add(scene_id)
        if object_ids and obj_id not in object_ids:
            blockers.append(f"BOP target references missing object model {obj_id}.")

    image_sizes: set[tuple[int, int]] = set()
    image_keys: set[tuple[int, int]] = set()
    annotations = 0
    below_visibility_target = False
    export_by_scene: dict[int, Mapping[str, Any]] = {}
    for item in exports:
        try:
            raw_scene_id = item["scene_id"]
            if type(raw_scene_id) is not int or raw_scene_id < 0:
                raise ValueError
            scene_id = raw_scene_id
        except (KeyError, TypeError, ValueError):
            blockers.append("BOP export contains a scene with an invalid ID.")
            continue
        if scene_id in export_by_scene:
            blockers.append(f"BOP export contains duplicate scene ID {scene_id}.")
            continue
        expected_scene_folder = f"{split}/{scene_id:06d}"
        if item.get("split") != split:
            blockers.append(
                f"BOP scene {scene_id} does not use the selected split {split!r}."
            )
        if item.get("scene_folder") != expected_scene_folder:
            blockers.append(
                f"BOP scene {scene_id} must use standard folder "
                f"{expected_scene_folder!r}."
            )
        export_by_scene[scene_id] = item

    for scene_id in sorted(target_scenes):
        export = export_by_scene.get(scene_id)
        if export is None:
            blockers.append(
                f"BOP target references scene {scene_id}, which is absent from "
                "the export manifest."
            )
        scene_folder = bop_root / split / f"{scene_id:06d}"
        if _has_symlink_component(scene_folder, root=bop_root):
            blockers.append(f"BOP scene {scene_id} must not use symbolic links.")
            continue
        camera_path = scene_folder / "scene_camera.json"
        gt_path = scene_folder / "scene_gt.json"
        gt_info_path = scene_folder / "scene_gt_info.json"
        required = (
            (camera_path, "camera metadata"),
            (gt_path, "ground truth"),
            (gt_info_path, "ground-truth visibility"),
        )
        missing = False
        for path, label in required:
            if not _plain_file(path, root=bop_root):
                blockers.append(
                    f"BOP scene {scene_id} is missing required {label} evidence."
                )
                missing = True
            else:
                semantic_paths.append(path)
        if missing:
            continue
        try:
            camera = _load_json(camera_path)
            scene_gt = _load_json(gt_path)
            scene_gt_info = _load_json(gt_info_path)
            if not all(
                isinstance(value, Mapping)
                for value in (camera, scene_gt, scene_gt_info)
            ):
                raise ValueError("scene JSON artifacts must be objects")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"BOP scene {scene_id} annotations are invalid: {exc}")
            continue

        validated_gt: dict[
            tuple[int, int], list[tuple[int, float, Mapping[str, Any]]]
        ] = {}
        for raw_image_id, gt_rows in scene_gt.items():
            try:
                image_id = int(raw_image_id)
                if image_id < 0:
                    raise ValueError
            except (TypeError, ValueError):
                blockers.append(f"BOP scene {scene_id} has an invalid image ID.")
                continue
            image_keys.add((scene_id, image_id))
            info_rows = scene_gt_info.get(str(raw_image_id))
            if not isinstance(gt_rows, list):
                blockers.append(
                    f"BOP scene {scene_id}, image {image_id} ground truth "
                    "must be a JSON array."
                )
                continue
            annotations += len(gt_rows)
            if not isinstance(info_rows, list) or len(info_rows) != len(gt_rows):
                blockers.append(
                    f"BOP scene {scene_id}, image {image_id} ground-truth "
                    "visibility rows must exactly match scene_gt."
                )
                continue
            for gt_index, (gt, info) in enumerate(zip(gt_rows, info_rows, strict=True)):
                label = f"BOP scene {scene_id}, image {image_id}, GT row {gt_index}"
                if not isinstance(gt, Mapping):
                    blockers.append(f"{label} must be a JSON object.")
                    continue
                raw_obj_id = gt.get("obj_id")
                if type(raw_obj_id) is not int or raw_obj_id < 1:
                    blockers.append(f"{label} has an invalid object ID.")
                    continue
                try:
                    rotation = _finite_json_vector(
                        gt.get("cam_R_m2c"),
                        count=9,
                        label=f"{label} cam_R_m2c",
                    ).reshape(3, 3)
                    _finite_json_vector(
                        gt.get("cam_t_m2c"),
                        count=3,
                        label=f"{label} cam_t_m2c",
                    )
                    orthogonality_error = float(
                        np.linalg.norm(
                            rotation.T @ rotation - np.eye(3),
                            ord="fro",
                        )
                    )
                    determinant = float(np.linalg.det(rotation))
                    if orthogonality_error > 0.02 or abs(determinant - 1.0) > 0.02:
                        raise ValueError(
                            f"{label} cam_R_m2c must be an orthonormal "
                            "rotation with determinant +1"
                        )
                    if not isinstance(info, Mapping):
                        raise ValueError(
                            f"{label} visibility info must be a JSON object"
                        )
                    raw_visibility = info["visib_fract"]
                    if type(raw_visibility) not in {int, float}:
                        raise ValueError(f"{label} visib_fract must be numeric")
                    visibility = float(raw_visibility)
                    if not math.isfinite(visibility) or not 0.0 <= visibility <= 1.0:
                        raise ValueError(
                            f"{label} visib_fract must be finite and between 0 and 1"
                        )
                except (KeyError, TypeError, ValueError) as exc:
                    blockers.append(str(exc))
                    continue
                validated_gt.setdefault((image_id, raw_obj_id), []).append(
                    (gt_index, visibility, gt)
                )

        for scene_target in (key for key in target_counts if key[0] == scene_id):
            _, im_id, obj_id = scene_target
            key = str(im_id)
            if key not in camera or key not in scene_gt or key not in scene_gt_info:
                blockers.append(
                    f"BOP target scene {scene_id}, image {im_id} lacks camera or GT data."
                )
                continue
            depth_path = scene_folder / "depth" / f"{im_id:06d}.png"
            rgb_path = scene_folder / "rgb" / f"{im_id:06d}.png"
            camera_row = camera.get(key)
            if not isinstance(camera_row, Mapping):
                blockers.append(
                    f"BOP target scene {scene_id}, image {im_id} camera "
                    "metadata must be a JSON object."
                )
            else:
                try:
                    _finite_json_vector(
                        camera_row.get("cam_K"),
                        count=9,
                        label=f"BOP scene {scene_id}, image {im_id} cam_K",
                    )
                    raw_depth_scale = camera_row["depth_scale"]
                    if type(raw_depth_scale) not in {int, float}:
                        raise ValueError(
                            f"BOP scene {scene_id}, image {im_id} depth_scale "
                            "must be numeric"
                        )
                    depth_scale = float(raw_depth_scale)
                    if not math.isfinite(depth_scale) or depth_scale <= 0:
                        raise ValueError(
                            f"BOP scene {scene_id}, image {im_id} depth_scale "
                            "must be finite and positive"
                        )
                except (KeyError, TypeError, ValueError) as exc:
                    blockers.append(str(exc))
            depth_size: tuple[int, int] | None = None
            rgb_size: tuple[int, int] | None = None
            if not _plain_file(depth_path, root=bop_root):
                blockers.append(
                    f"BOP target scene {scene_id}, image {im_id} lacks depth for VSD."
                )
            else:
                try:
                    depth_size = _png_size(
                        depth_path,
                        label="depth image",
                        expected_kind="depth",
                    )
                    depth_paths.append(depth_path)
                except (OSError, ValueError) as exc:
                    blockers.append(str(exc))
            if not _plain_file(rgb_path, root=bop_root):
                blockers.append(
                    f"BOP target scene {scene_id}, image {im_id} lacks an RGB image."
                )
            else:
                try:
                    rgb_size = _png_size(
                        rgb_path,
                        label="RGB image",
                        expected_kind="rgb",
                    )
                    image_sizes.add(rgb_size)
                    rgb_paths.append(rgb_path)
                except (OSError, ValueError) as exc:
                    blockers.append(str(exc))
            if (
                depth_size is not None
                and rgb_size is not None
                and depth_size != rgb_size
            ):
                blockers.append(
                    f"BOP target scene {scene_id}, image {im_id} RGB and depth "
                    "dimensions do not match."
                )
            matching = validated_gt.get((im_id, obj_id), [])
            if len(matching) < target_counts[scene_target]:
                blockers.append(
                    f"BOP target scene {scene_id}, image {im_id}, object {obj_id} "
                    "requests more instances than ground truth provides."
                )
            visible_count = sum(
                visibility >= 0.1 for _index, visibility, _gt in matching
            )
            if visible_count != target_counts[scene_target]:
                below_visibility_target = True

    if len(image_sizes) > 1:
        blockers.append(
            "BOP19 VSD evaluation requires one RGB/depth resolution across all "
            "target scenes."
        )
    if below_visibility_target:
        warnings.append(
            "The exported target inventory does not exactly match the BOP "
            "challenge's at-least-10%-visible target policy; scores are valid "
            "for this exported target list but are not leaderboard-comparable."
        )
    if models_info and all(
        not (
            isinstance(value, Mapping)
            and (value.get("symmetries_discrete") or value.get("symmetries_continuous"))
        )
        for value in models_info.values()
    ):
        warnings.append(
            "models_eval/models_info.json declares no object symmetries; "
            "symmetric workpieces will be scored as asymmetric."
        )
    if models_info:
        warnings.append(
            "PoseTestBot evaluation models are toolkit-readable but are not "
            "declared as uniformly resampled BOP leaderboard geometry."
        )

    capabilities = manifest.get("capabilities")
    evaluation_capability = bool(
        isinstance(capabilities, Mapping)
        and capabilities.get("bop19_evaluation") is True
    )
    if not evaluation_capability:
        blockers.append(
            "The BOP export has no complete ground-truth annotations and is not "
            "declared ready for BOP19 evaluation."
        )

    counts_value = manifest.get("validation")
    validation = counts_value if isinstance(counts_value, Mapping) else {}
    counts = {
        "scenes": len(export_by_scene),
        "images": int(validation.get("frame_count") or len(image_keys)),
        "objects": len(object_ids),
        "targets": sum(target_counts.values()),
        "annotations": annotations,
    }
    return {
        "run_root": root,
        "bop_root": bop_root,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "dataset_info": dataset_info,
        "targets": targets,
        "target_counts": dict(target_counts),
        "target_rows": target_rows,
        "object_ids": object_ids,
        "image_size": next(iter(image_sizes)) if len(image_sizes) == 1 else None,
        "splits": splits,
        "split": split,
        "counts": counts,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "semantic_paths": semantic_paths,
        "rgb_paths": rgb_paths,
        "depth_paths": depth_paths,
    }


def inspect_dataset(
    run_root: str | Path,
    *,
    include_depth_content: bool = False,
) -> dict[str, Any]:
    """Inspect one run-owned BOP export for BOP19 localization evaluation."""

    inventory = _dataset_inventory(run_root)
    manifest_path = inventory.get("manifest_path")
    manifest_sha256 = (
        _sha256_file(manifest_path)
        if isinstance(manifest_path, Path) and manifest_path.is_file()
        else None
    )
    semantic_paths = inventory.get("semantic_paths", [])
    semantic_sha256 = _digest_files(
        inventory["bop_root"],
        semantic_paths,
    )
    image_metadata_sha256 = _digest_file_metadata(
        inventory["bop_root"],
        [
            *inventory.get("rgb_paths", []),
            *inventory.get("depth_paths", []),
        ],
    )
    dataset_sha256 = _combined_digest(
        semantic_sha256,
        image_metadata_sha256,
    )
    dataset_content_sha256 = (
        _combined_digest(
            dataset_sha256,
            _digest_depth_content(
                inventory["bop_root"],
                inventory.get("depth_paths", []),
            ),
        )
        if include_depth_content
        else None
    )
    dataset_alias = f"ptb{semantic_sha256[:12]}"
    counts = dict(inventory["counts"])
    blockers = list(inventory["blockers"])
    split = str(inventory.get("split") or "test")
    manifest = inventory["manifest"]
    dataset_info = inventory["dataset_info"]
    ready = not blockers
    return {
        "schema_version": "bop_evaluation_dataset.v1",
        "run_root": inventory["run_root"].as_posix(),
        "bop_root": inventory["bop_root"].as_posix(),
        "status": "ready" if ready else "blocked",
        "dataset_alias": dataset_alias,
        "dataset_id": dataset_alias,
        "dataset_sha256": dataset_sha256,
        "dataset_content_sha256": dataset_content_sha256,
        "depth_content_hashed": include_depth_content,
        "export_manifest_sha256": manifest_sha256,
        "manifest_schema_version": manifest.get("schema_version"),
        "name": dataset_info.get("name") or inventory["run_root"].name,
        "split": split,
        "counts": counts,
        "scene_count": counts["scenes"],
        "frame_count": counts["images"],
        "model_count": counts["objects"],
        "target_count": counts["targets"],
        "annotation_count": counts["annotations"],
        "annotation_source": manifest.get("annotation_source") or "unknown",
        "image_size": (
            list(inventory["image_size"])
            if inventory.get("image_size") is not None
            else None
        ),
        "evaluation_ready": ready,
        "simulation_ready": ready,
        "result_registration_ready": bool(
            manifest_path
            and isinstance(manifest_path, Path)
            and manifest_path.is_file()
            and counts["targets"] > 0
        ),
        "result_filename_template": f"{{method}}_{dataset_alias}-{split}.csv",
        "blockers": blockers,
        "warnings": list(inventory["warnings"]),
    }


def public_dataset_descriptor(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Return the browser contract without exposing filesystem internals."""

    hidden = {"bop_root", "dataset_sha256"}
    public = {key: value for key, value in dataset.items() if key not in hidden}
    public["dataset_sha256"] = dataset.get("dataset_sha256")
    public["blockers"] = _public_issue_list(dataset.get("blockers", []))
    public["warnings"] = _public_issue_list(dataset.get("warnings", []))
    return public


def _parse_result_filename(path: Path) -> dict[str, str | None]:
    match = RESULT_FILENAME_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(
            "BOP result filename must follow "
            "METHOD_DATASET-test[_OPTIONAL-ID].csv using delimiter-safe slugs"
        )
    return match.groupdict()


def _finite_float(value: str, *, label: str, row_number: int) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label} on BOP result row {row_number}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid {label} on BOP result row {row_number}")
    return parsed


def _float_vector(
    value: str,
    *,
    count: int,
    label: str,
    row_number: int,
) -> np.ndarray:
    parts = value.split()
    if len(parts) != count:
        raise ValueError(
            f"Invalid {label} on BOP result row {row_number}: expected {count} values"
        )
    return np.asarray(
        [_finite_float(part, label=label, row_number=row_number) for part in parts],
        dtype=np.float64,
    )


def validate_bop_result_csv(
    path: str | Path,
    *,
    dataset: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a standard BOP19 CSV against one exact exported target list."""

    result_path = Path(path)
    if not result_path.is_file() or result_path.is_symlink():
        raise FileNotFoundError(f"BOP result CSV not found: {result_path}")
    size = result_path.stat().st_size
    if size > MAX_RESULT_BYTES:
        raise ValueError("BOP result CSV exceeds the 128 MiB limit")
    parsed_name = _parse_result_filename(result_path)
    expected_alias = str(dataset["dataset_alias"])
    if parsed_name["dataset"] != expected_alias:
        raise ValueError(
            f"BOP result dataset {parsed_name['dataset']!r} does not match "
            f"selected dataset {expected_alias!r}"
        )
    expected_split = str(dataset.get("split") or "test")
    if parsed_name["split"] != expected_split:
        raise ValueError(
            f"BOP result split {parsed_name['split']!r} does not match "
            f"selected split {expected_split!r}"
        )
    if parsed_name["split_type"] is not None:
        raise ValueError(
            "BOP result split_type is not supported for this exported dataset"
        )

    inventory = _dataset_inventory(str(dataset["run_root"]))
    target_counts: dict[tuple[int, int, int], int] = inventory["target_counts"]
    object_ids: set[int] = inventory["object_ids"]
    scene_ids = {key[0] for key in target_counts}
    image_keys = {(key[0], key[1]) for key in target_counts}
    estimates_per_target: Counter[tuple[int, int, int]] = Counter()
    timings: dict[tuple[int, int], float] = {}
    timings_available = True
    row_count = 0

    with result_path.open(newline="", encoding="utf-8-sig") as handle:
        raw_header = handle.readline()
        if not raw_header:
            raise ValueError("BOP result CSV is empty")
        if '"' in raw_header:
            raise ValueError(
                "BOP result CSV must not use CSV quoting; the official "
                "BOP19 loader expects raw comma-separated fields"
            )
        header = raw_header.rstrip("\r\n").split(",")
        if header != RESULT_HEADER:
            raise ValueError(
                "BOP result CSV header must be exactly: " + ",".join(RESULT_HEADER)
            )
        for row_number, raw_row in enumerate(handle, start=2):
            if row_number > MAX_RESULT_ROWS + 1:
                raise ValueError("BOP result CSV contains too many rows")
            if '"' in raw_row:
                raise ValueError(
                    "BOP result CSV must not use CSV quoting; the official "
                    f"BOP19 loader cannot parse row {row_number}"
                )
            row = raw_row.rstrip("\r\n").split(",")
            if len(row) != len(RESULT_HEADER):
                raise ValueError(
                    f"BOP result row {row_number} must contain exactly 7 columns"
                )
            row_count += 1
            try:
                scene_id = int(row[0])
                im_id = int(row[1])
                obj_id = int(row[2])
            except ValueError as exc:
                raise ValueError(
                    f"Invalid scene/image/object ID on BOP result row {row_number}"
                ) from exc
            if scene_id not in scene_ids:
                raise ValueError(
                    f"BOP result row {row_number} references unknown target scene "
                    f"{scene_id}"
                )
            if (scene_id, im_id) not in image_keys:
                raise ValueError(
                    f"BOP result row {row_number} references unknown target image "
                    f"{scene_id}/{im_id}"
                )
            if obj_id not in object_ids:
                raise ValueError(
                    f"BOP result row {row_number} references unknown object {obj_id}"
                )
            target_key = (scene_id, im_id, obj_id)
            if target_key not in target_counts:
                raise ValueError(
                    f"BOP result row {row_number} does not match a target object"
                )
            _finite_float(row[3], label="score", row_number=row_number)
            rotation = _float_vector(
                row[4], count=9, label="R", row_number=row_number
            ).reshape(3, 3)
            _float_vector(row[5], count=3, label="t", row_number=row_number)
            runtime = _finite_float(row[6], label="time", row_number=row_number)
            if runtime < 0:
                timings_available = False
            else:
                image_key = (scene_id, im_id)
                previous = timings.get(image_key)
                if previous is not None and abs(previous - runtime) > 0.001:
                    raise ValueError(
                        "BOP result time must be identical for all estimates from "
                        f"scene {scene_id}, image {im_id}"
                    )
                timings[image_key] = runtime
            orthogonality_error = float(
                np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro")
            )
            determinant = float(np.linalg.det(rotation))
            if orthogonality_error > 0.02 or abs(determinant - 1.0) > 0.02:
                raise ValueError(
                    f"Invalid R on BOP result row {row_number}: rotation is not "
                    "orthonormal with determinant +1"
                )
            estimates_per_target[target_key] += 1
    if row_count == 0:
        raise ValueError("BOP result CSV contains no estimates")

    required = sum(target_counts.values())
    matched = sum(
        min(estimates_per_target[key], count) for key, count in target_counts.items()
    )
    return {
        "schema_version": "bop_result_validation.v1",
        "status": "ok",
        "method": str(parsed_name["method"]),
        "dataset_alias": expected_alias,
        "split": expected_split,
        "row_count": row_count,
        "estimate_count": row_count,
        "target_estimate_count": matched,
        "target_required_count": required,
        "target_coverage": matched / required if required else 0.0,
        "timings_available": timings_available,
        "average_time_per_image": (
            sum(timings.values()) / len(timings)
            if timings_available and timings
            else None
        ),
        "sha256": _sha256_file(result_path),
        "size_bytes": size,
    }


def _evaluation_root(run_root: str | Path) -> Path:
    return Path(run_root).resolve() / EVALUATION_ROOT


def _result_dir(run_root: str | Path, result_id: str) -> Path:
    return _evaluation_root(run_root) / RESULTS_DIR / result_id


def _evaluation_dir(run_root: str | Path, evaluation_id: str) -> Path:
    return _evaluation_root(run_root) / EVALUATIONS_DIR / evaluation_id


def _relative_to_run(path: Path, run_root: str | Path) -> str:
    return path.relative_to(Path(run_root).resolve()).as_posix()


def _required_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _provenance_hashes(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty hash object")
    normalized: dict[str, str] = {}
    for raw_key, raw_digest in value.items():
        if not isinstance(raw_key, str) or PROVENANCE_KEY_RE.fullmatch(raw_key) is None:
            raise ValueError(f"{label} contains an invalid key")
        normalized[raw_key] = _required_sha256(
            raw_digest,
            label=f"{label}.{raw_key}",
        )
    return dict(sorted(normalized.items()))


def _generic_runtime_artifact(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"filename", "sha256"}:
        raise ValueError(f"{label} must be exact filename/hash evidence")
    filename = value.get("filename")
    if not isinstance(filename, str) or RUNTIME_FILENAME_RE.fullmatch(filename) is None:
        raise ValueError(f"{label}.filename is invalid")
    return {
        "filename": filename,
        "sha256": _required_sha256(value.get("sha256"), label=f"{label}.sha256"),
    }


def _generic_external_result_provenance(
    value: Mapping[str, Any],
    *,
    external_job_id: str,
    expected_dataset_sha256: str,
    source_provenance_sha256: str,
    source_path: Path,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    estimator = value.get("estimator")
    if not isinstance(estimator, Mapping) or set(estimator) != {
        "estimator_id",
        "driver_id",
        "runtime_id",
        "input_contracts",
        "output_contract",
    }:
        raise ValueError("Controller provenance has invalid estimator evidence")
    estimator_id = estimator.get("estimator_id")
    driver_id = estimator.get("driver_id")
    runtime_id = estimator.get("runtime_id")
    input_contracts = estimator.get("input_contracts")
    output_contract = estimator.get("output_contract")
    if (
        not isinstance(estimator_id, str)
        or ESTIMATOR_ID_RE.fullmatch(estimator_id) is None
        or not isinstance(driver_id, str)
        or DRIVER_ID_RE.fullmatch(driver_id) is None
        or not isinstance(runtime_id, str)
        or RUNTIME_ID_RE.fullmatch(runtime_id) is None
        or not isinstance(input_contracts, list)
        or not input_contracts
        or len(set(input_contracts)) != len(input_contracts)
        or any(
            not isinstance(contract, str) or CONTRACT_ID_RE.fullmatch(contract) is None
            for contract in input_contracts
        )
        or output_contract != "bop19.csv.v1"
        or validation.get("method") != estimator_id
    ):
        raise ValueError("Controller provenance has invalid estimator identity")

    external_job = value.get("external_job")
    if (
        not isinstance(external_job, Mapping)
        or set(external_job)
        != {
            "provider",
            "job_id",
            "slurm_job_id",
            "estimator_id",
            "driver_id",
            "runtime_id",
        }
        or external_job.get("provider") != "posetestbot-cluster"
        or external_job.get("job_id") != external_job_id
        or not isinstance(external_job.get("slurm_job_id"), str)
        or SLURM_JOB_ID_RE.fullmatch(external_job["slurm_job_id"]) is None
        or external_job.get("estimator_id") != estimator_id
        or external_job.get("driver_id") != driver_id
        or external_job.get("runtime_id") != runtime_id
    ):
        raise ValueError("Controller provenance has invalid external-job evidence")

    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("Controller provenance has invalid runtime evidence")
    if (
        runtime.get("estimator_id") != estimator_id
        or runtime.get("driver_id") != driver_id
        or runtime.get("runtime_id") != runtime_id
        or runtime.get("input_contracts") != input_contracts
        or runtime.get("output_contract") != output_contract
        or runtime.get("qualified") is not True
        or runtime.get("ready") is not True
    ):
        raise ValueError("Controller provenance runtime does not match its estimator")
    container = _generic_runtime_artifact(
        runtime.get("container"), label="runtime.container"
    )
    assets_value = runtime.get("assets")
    if not isinstance(assets_value, Mapping):
        raise ValueError("runtime.assets must be an object")
    assets: dict[str, dict[str, str]] = {}
    for asset_id, artifact in assets_value.items():
        if (
            not isinstance(asset_id, str)
            or PROVENANCE_KEY_RE.fullmatch(asset_id) is None
        ):
            raise ValueError("runtime.assets contains an invalid identifier")
        assets[asset_id] = _generic_runtime_artifact(
            artifact, label=f"runtime.assets.{asset_id}"
        )

    def revisions(field: str, *, required: bool) -> dict[str, str]:
        source = runtime.get(field)
        if not isinstance(source, Mapping) or (required and not source):
            raise ValueError(f"runtime.{field} is invalid")
        normalized: dict[str, str] = {}
        for key, revision in source.items():
            if (
                not isinstance(key, str)
                or PROVENANCE_KEY_RE.fullmatch(key) is None
                or not isinstance(revision, str)
                or RUNTIME_REVISION_RE.fullmatch(revision) is None
            ):
                raise ValueError(f"runtime.{field} contains invalid evidence")
            normalized[key] = revision
        return dict(sorted(normalized.items()))

    source_revisions = revisions("source_revisions", required=True)
    build_provenance = revisions("build_provenance", required=True)
    licenses_value = runtime.get("licenses")
    if not isinstance(licenses_value, list) or not licenses_value:
        raise ValueError("runtime.licenses is invalid")
    licenses: list[dict[str, str]] = []
    for index, license_value in enumerate(licenses_value):
        if not isinstance(license_value, Mapping) or set(license_value) != {
            "name",
            "sha256",
        }:
            raise ValueError("runtime.licenses contains invalid evidence")
        name = license_value.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 160
            or any(character in name for character in "/\\\r\n\0")
        ):
            raise ValueError("runtime.licenses contains an invalid name")
        licenses.append(
            {
                "name": name,
                "sha256": _required_sha256(
                    license_value.get("sha256"),
                    label=f"runtime.licenses.{index}.sha256",
                ),
            }
        )
    profiles = runtime.get("qualified_resource_profiles")
    if (
        not isinstance(profiles, list)
        or not profiles
        or len(set(profiles)) != len(profiles)
        or any(
            not isinstance(profile, str) or PROVENANCE_KEY_RE.fullmatch(profile) is None
            for profile in profiles
        )
    ):
        raise ValueError("runtime qualified resource profiles are invalid")
    qualification_blockers = runtime.get("qualification_blockers")
    if qualification_blockers not in (None, []):
        raise ValueError("Controller runtime was not ready at submission")
    normalized_runtime = {
        "estimator_id": estimator_id,
        "driver_id": driver_id,
        "runtime_id": runtime_id,
        "container": container,
        "assets": dict(sorted(assets.items())),
        "source_revisions": source_revisions,
        "build_provenance": build_provenance,
        "licenses": licenses,
        "input_contracts": list(input_contracts),
        "output_contract": output_contract,
        "qualified_resource_profiles": list(profiles),
        "qualification_manifest_sha256": _required_sha256(
            runtime.get("qualification_manifest_sha256"),
            label="runtime.qualification_manifest_sha256",
        ),
        "qualified": True,
        "ready": True,
    }

    input_hashes = _provenance_hashes(value.get("input_hashes"), label="input_hashes")
    output_hashes = _provenance_hashes(
        value.get("output_hashes"), label="output_hashes"
    )
    result_provenance = value.get("result")
    if (
        not isinstance(result_provenance, Mapping)
        or result_provenance.get("filename") != source_path.name
        or result_provenance.get("sha256") != validation["sha256"]
        or result_provenance.get("size_bytes") != validation["size_bytes"]
        or output_hashes.get(source_path.name) != validation["sha256"]
    ):
        raise ValueError("Controller result bytes do not match its provenance")
    project_copy = value.get("project_copy")
    if (
        not isinstance(project_copy, Mapping)
        or set(project_copy) != {"state", "artifact_sha256"}
        or project_copy.get("state") != "verified"
        or project_copy.get("artifact_sha256") != output_hashes
    ):
        raise ValueError("Controller provenance has invalid verified-copy evidence")
    estimate_count = value.get("estimate_count")
    failure_count = value.get("failure_count")
    if (
        type(estimate_count) is not int
        or estimate_count != validation["estimate_count"]
        or type(failure_count) is not int
        or failure_count < 0
    ):
        raise ValueError("Controller provenance has invalid result counts")
    collected_at = value.get("collected_at")
    if not isinstance(collected_at, str) or len(collected_at) > 64:
        raise ValueError("Controller provenance has invalid collection time")
    try:
        datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Controller provenance has invalid collection time") from exc

    return {
        "schema_version": "posetestbot_external_result_provenance.v1",
        "source_schema_version": value["schema_version"],
        "source_provenance_sha256": _required_sha256(
            source_provenance_sha256,
            label="source_provenance_sha256",
        ),
        "external_job": {
            "provider": "posetestbot-cluster",
            "job_id": external_job_id,
            "slurm_job_id": external_job["slurm_job_id"],
            "estimator_id": estimator_id,
            "driver_id": driver_id,
            "runtime_id": runtime_id,
        },
        "method": estimator_id,
        "estimator": dict(estimator),
        "runtime": normalized_runtime,
        "dataset_sha256": expected_dataset_sha256,
        "bop_content_sha256": _required_sha256(
            value.get("bop_content_sha256"), label="bop_content_sha256"
        ),
        "input_manifest_sha256": _required_sha256(
            value.get("input_manifest_sha256"),
            label="input_manifest_sha256",
        ),
        "input_hashes": input_hashes,
        "estimate_count": estimate_count,
        "failure_count": failure_count,
        "result": {
            "filename": source_path.name,
            "sha256": validation["sha256"],
            "size_bytes": validation["size_bytes"],
        },
        "collected_at": collected_at,
    }


def _external_result_provenance(
    value: Mapping[str, Any],
    *,
    external_job_id: str,
    expected_dataset_sha256: str,
    source_provenance_sha256: str,
    source_path: Path,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate controller evidence and retain only local inspection metadata."""

    if value.get("schema_version") != "posetestbot_cluster_collected_result.v1":
        raise ValueError("Controller provenance schema is not supported")
    if value.get("job_id") != external_job_id:
        raise ValueError("Controller provenance belongs to another job")
    if value.get("dataset_sha256") != expected_dataset_sha256:
        raise ValueError("Controller provenance does not match the staged dataset")
    if not isinstance(value.get("estimator"), Mapping):
        raise ValueError("Controller provenance lacks current estimator evidence")
    return _generic_external_result_provenance(
        value,
        external_job_id=external_job_id,
        expected_dataset_sha256=expected_dataset_sha256,
        source_provenance_sha256=source_provenance_sha256,
        source_path=source_path,
        validation=validation,
    )


def _store_result(
    run_root: str | Path,
    *,
    source_path: Path,
    method_name: str,
    simulated: bool,
    simulation: Mapping[str, Any] | None,
    dataset: Mapping[str, Any] | None = None,
    validation: Mapping[str, Any] | None = None,
    record_extensions: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    source_kind: str | None = None,
) -> dict[str, Any]:
    current_dataset = (
        dict(dataset) if dataset is not None else inspect_dataset(run_root)
    )
    current_validation = (
        dict(validation)
        if validation is not None
        else validate_bop_result_csv(source_path, dataset=current_dataset)
    )
    result_id = f"result-{uuid.uuid4().hex[:12]}"
    folder = _result_dir(run_root, result_id)
    folder.mkdir(parents=True, exist_ok=False)
    destination = folder / source_path.name
    temporary = folder / f".{source_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with source_path.open("rb") as source, temporary.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        if _sha256_file(destination) != current_validation["sha256"]:
            raise OSError("BOP result changed while it was being imported")
        destination.chmod(0o444)
        destination_stat = destination.stat()
        record = {
            "schema_version": "bop_result_record.v1",
            "result_id": result_id,
            "method": current_validation["method"],
            "method_name": method_name,
            "display_name": method_name,
            "filename": destination.name,
            "path": _relative_to_run(destination, run_root),
            "result_path": _relative_to_run(destination, run_root),
            "created_at": _utc_now(),
            "source_kind": source_kind
            or ("gt_simulation" if simulated else "registered_result"),
            "simulated": simulated,
            "simulation": dict(simulation) if simulation is not None else None,
            "sha256": current_validation["sha256"],
            "size_bytes": destination_stat.st_size,
            "mtime_ns": destination_stat.st_mtime_ns,
            "dataset_sha256": current_dataset["dataset_sha256"],
            "dataset_alias": current_dataset["dataset_alias"],
            "split": current_dataset["split"],
            "row_count": current_validation["row_count"],
            "estimate_count": current_validation["estimate_count"],
            "target_estimate_count": current_validation["target_estimate_count"],
            "target_required_count": current_validation["target_required_count"],
            "target_coverage": current_validation["target_coverage"],
            "timings_available": current_validation["timings_available"],
            "average_time_per_image": current_validation["average_time_per_image"],
        }
        if record_extensions is not None:
            protected = set(record) & set(record_extensions)
            if protected:
                raise ValueError(
                    "External result metadata cannot replace immutable fields: "
                    + ", ".join(sorted(protected))
                )
            record.update(dict(record_extensions))
        if provenance is not None:
            provenance_path = folder / "controller-provenance.json"
            atomic_write_json(provenance_path, dict(provenance))
            provenance_path.chmod(0o444)
            record["controller_provenance_path"] = _relative_to_run(
                provenance_path, run_root
            )
            record["controller_provenance_sha256"] = _sha256_file(provenance_path)
        metadata_path = folder / RESULT_METADATA
        atomic_write_json(metadata_path, record)
        metadata_path.chmod(0o444)
        return record
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(folder, ignore_errors=True)
        raise


def import_bop_result(
    run_root: str | Path,
    source_path: str | Path,
    *,
    method_name: str | None = None,
) -> dict[str, Any]:
    """Copy and register an immutable standard BOP19 result CSV."""

    dataset = inspect_dataset(run_root)
    if not dataset["result_registration_ready"]:
        raise ValueError("Selected run has no BOP target inventory for result import")
    source = Path(source_path)
    validation = validate_bop_result_csv(source, dataset=dataset)
    display_name = (method_name or validation["method"]).strip()
    if not display_name:
        raise ValueError("method_name must not be empty")
    if len(display_name) > 120:
        raise ValueError("method_name must contain at most 120 characters")
    return _store_result(
        run_root,
        source_path=source,
        method_name=display_name,
        simulated=False,
        simulation=None,
        dataset=dataset,
        validation=validation,
    )


@contextmanager
def _external_result_import_lock(run_root: str | Path):
    root = _evaluation_root(run_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".external-result-import.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("External result import lock is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _result_for_external_job(
    run_root: str | Path, external_job_id: str
) -> dict[str, Any] | None:
    root = _evaluation_root(run_root) / RESULTS_DIR
    try:
        folders = list(root.iterdir())
    except FileNotFoundError:
        return None
    matches = []
    for folder in folders:
        if not folder.is_dir() or folder.is_symlink():
            continue
        if not _plain_file(folder / RESULT_METADATA, root=Path(run_root).resolve()):
            continue
        record = _load_record(folder / RESULT_METADATA)
        external_job = record.get("external_job") if record is not None else None
        if (
            isinstance(external_job, Mapping)
            and external_job.get("job_id") == external_job_id
        ):
            matches.append(record)
    if len(matches) > 1:
        raise RuntimeError(
            "Multiple immutable results claim the same external controller job"
        )
    return matches[0] if matches else None


def import_external_bop_result(
    run_root: str | Path,
    source_path: str | Path,
    *,
    external_job_id: str,
    expected_dataset_sha256: str,
    source_provenance_sha256: str,
    controller_provenance: Mapping[str, Any],
    method_name: str = "FoundationPose (oracle GT masks)",
) -> tuple[dict[str, Any], bool]:
    """Idempotently register one controller-owned immutable BOP19 result."""

    if (
        not isinstance(external_job_id, str)
        or EXTERNAL_POSE_JOB_RE.fullmatch(external_job_id) is None
    ):
        raise ValueError("External controller job ID is invalid")
    display_name = method_name.strip()
    if not display_name or len(display_name) > 120:
        raise ValueError("method_name must contain 1–120 characters")
    source = Path(source_path)
    with _external_result_import_lock(run_root):
        existing = _result_for_external_job(run_root, external_job_id)
        if existing is not None:
            return existing, False
        dataset = inspect_dataset(run_root)
        if dataset["dataset_sha256"] != expected_dataset_sha256:
            raise RuntimeError(
                "The local BOP export changed after this cluster job was staged. "
                "Restore or select the matching dataset snapshot before importing."
            )
        validation = validate_bop_result_csv(source, dataset=dataset)
        provenance = _external_result_provenance(
            controller_provenance,
            external_job_id=external_job_id,
            expected_dataset_sha256=expected_dataset_sha256,
            source_provenance_sha256=source_provenance_sha256,
            source_path=source,
            validation=validation,
        )
        external_job = provenance["external_job"]
        summary = {
            "external_job": {
                "provider": "posetestbot-cluster",
                "job_id": external_job_id,
                "slurm_job_id": external_job["slurm_job_id"],
            },
        }
        result = _store_result(
            run_root,
            source_path=source,
            method_name=display_name,
            simulated=False,
            simulation=None,
            dataset=dataset,
            validation=validation,
            record_extensions={
                **summary,
            },
            provenance=provenance,
            source_kind="external_controller",
        )
        return result, True


def _rotation_from_rotvec(rotvec: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(rotvec))
    if angle == 0.0:
        return np.eye(3, dtype=np.float64)
    axis = rotvec / angle
    x, y, z = axis
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)


def _format_vector(values: Iterable[float]) -> str:
    return " ".join(f"{float(value):.12g}" for value in values)


def _normalize_simulation(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = dict(value or {})
    method_name = str(source.get("method_name") or "GT slight offset").strip()
    try:
        translation_sigma_mm = float(source.get("translation_sigma_mm", 1.0))
        rotation_sigma_deg = float(source.get("rotation_sigma_deg", 0.25))
        seed = int(source.get("seed", 42))
        score = float(source.get("score", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Simulation parameters must be numeric") from exc
    if not method_name or len(method_name) > 120:
        raise ValueError("Simulation method_name must contain 1–120 characters")
    if (
        not math.isfinite(translation_sigma_mm)
        or not 0.0 <= translation_sigma_mm <= 100.0
    ):
        raise ValueError("translation_sigma_mm must be between 0 and 100")
    if not math.isfinite(rotation_sigma_deg) or not 0.0 <= rotation_sigma_deg <= 30.0:
        raise ValueError("rotation_sigma_deg must be between 0 and 30")
    if not -(2**31) <= seed < 2**31:
        raise ValueError("seed must be a signed 32-bit integer")
    if not math.isfinite(score):
        raise ValueError("score must be finite")
    return {
        "method_name": method_name,
        "translation_sigma_mm": translation_sigma_mm,
        "rotation_sigma_deg": rotation_sigma_deg,
        "seed": seed,
        "score": score,
    }


def create_simulated_bop_result(
    run_root: str | Path,
    *,
    method_name: str = "GT slight offset",
    translation_sigma_mm: float = 1.0,
    rotation_sigma_deg: float = 0.25,
    seed: int = 42,
    score: float = 1.0,
) -> dict[str, Any]:
    """Create deterministic GT-derived estimates without changing the BOP tree."""

    simulation = _normalize_simulation(
        {
            "method_name": method_name,
            "translation_sigma_mm": translation_sigma_mm,
            "rotation_sigma_deg": rotation_sigma_deg,
            "seed": seed,
            "score": score,
        }
    )
    dataset = inspect_dataset(run_root)
    if not dataset["simulation_ready"]:
        detail = " ".join(dataset["blockers"])
        raise ValueError(
            f"Simulation requires complete BOP ground truth and annotations. {detail}"
        )
    inventory = _dataset_inventory(run_root)
    rng = np.random.default_rng(simulation["seed"])
    rows: list[dict[str, Any]] = []
    bop_root = inventory["bop_root"]
    split = inventory["split"]
    scene_cache: dict[int, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}

    for target in inventory["targets"]:
        scene_id = int(target["scene_id"])
        im_id = int(target["im_id"])
        obj_id = int(target["obj_id"])
        inst_count = int(target["inst_count"])
        if scene_id not in scene_cache:
            scene_root = bop_root / split / f"{scene_id:06d}"
            scene_gt = _load_json(scene_root / "scene_gt.json")
            scene_gt_info = _load_json(scene_root / "scene_gt_info.json")
            scene_cache[scene_id] = (scene_gt, scene_gt_info)
        scene_gt, scene_gt_info = scene_cache[scene_id]
        gt_rows = scene_gt[str(im_id)]
        info_rows = scene_gt_info[str(im_id)]
        candidates = [
            (
                -float(
                    info_rows[index].get("visib_fract", 0.0)
                    if index < len(info_rows) and isinstance(info_rows[index], Mapping)
                    else 0.0
                ),
                index,
                row,
            )
            for index, row in enumerate(gt_rows)
            if isinstance(row, Mapping) and int(row.get("obj_id", -1)) == obj_id
        ]
        candidates.sort(key=lambda item: (item[0], item[1]))
        if len(candidates) < inst_count:
            raise ValueError(
                f"Target {scene_id}/{im_id}/{obj_id} has insufficient ground truth"
            )
        for rank, (_negative_visibility, _gt_index, gt) in enumerate(
            candidates[:inst_count]
        ):
            rotation_gt = np.asarray(gt["cam_R_m2c"], dtype=np.float64).reshape(3, 3)
            translation_gt = np.asarray(gt["cam_t_m2c"], dtype=np.float64).reshape(3)
            translation_noise = np.clip(
                rng.normal(0.0, simulation["translation_sigma_mm"], size=3),
                -3.0 * simulation["translation_sigma_mm"],
                3.0 * simulation["translation_sigma_mm"],
            )
            rotation_noise_deg = np.clip(
                rng.normal(0.0, simulation["rotation_sigma_deg"], size=3),
                -3.0 * simulation["rotation_sigma_deg"],
                3.0 * simulation["rotation_sigma_deg"],
            )
            delta_rotation = _rotation_from_rotvec(np.deg2rad(rotation_noise_deg))
            rotation_estimate = rotation_gt @ delta_rotation
            translation_estimate = translation_gt + translation_noise
            rows.append(
                {
                    "scene_id": scene_id,
                    "im_id": im_id,
                    "obj_id": obj_id,
                    "score": simulation["score"] - rank * 1e-6,
                    "R": _format_vector(rotation_estimate.reshape(-1)),
                    "t": _format_vector(translation_estimate),
                    "time": -1,
                }
            )

    temporary_root = _evaluation_root(run_root) / ".simulation"
    temporary_root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    filename = f"gtperturb_{dataset['dataset_alias']}-{dataset['split']}_{token}.csv"
    temporary = temporary_root / filename
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=RESULT_HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(temporary, output.getvalue())
    try:
        public_simulation = {
            "translation_sigma_mm": simulation["translation_sigma_mm"],
            "rotation_sigma_deg": simulation["rotation_sigma_deg"],
            "seed": simulation["seed"],
        }
        return _store_result(
            run_root,
            source_path=temporary,
            method_name=simulation["method_name"],
            simulated=True,
            simulation=public_simulation,
            dataset=dataset,
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        value = _load_json(path)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _stored_result_path(
    run_root: str | Path,
    *,
    folder: Path,
    record: Mapping[str, Any],
) -> Path:
    root = Path(run_root).resolve()
    result_id = record.get("result_id")
    if not isinstance(result_id, str) or not RESULT_ID_RE.fullmatch(result_id):
        raise ValueError("Registered BOP result has an invalid ID")
    if folder.name != result_id:
        raise ValueError("Registered BOP result metadata does not match its folder")
    filename = record.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
        or "\\" in filename
    ):
        raise ValueError("Registered BOP result has an invalid filename")
    relative_value = record.get("path") or record.get("result_path")
    if not isinstance(relative_value, str) or not relative_value:
        raise ValueError("Registered BOP result has no stored path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Registered BOP result path escapes its run")
    candidate = root / relative
    expected = folder / filename
    if candidate.resolve(strict=False) != expected.resolve(
        strict=False
    ) or _has_symlink_component(candidate, root=root):
        raise ValueError("Registered BOP result path escapes its immutable folder")
    return candidate


def list_results(
    run_root: str | Path,
    *,
    dataset: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current_dataset = (
        dict(dataset) if dataset is not None else inspect_dataset(run_root)
    )
    root = _evaluation_root(run_root) / RESULTS_DIR
    records: list[dict[str, Any]] = []
    try:
        folders = list(root.iterdir())
    except FileNotFoundError:
        return []
    for folder in folders:
        if not folder.is_dir() or folder.is_symlink():
            continue
        if not RESULT_ID_RE.fullmatch(folder.name):
            continue
        metadata_path = folder / RESULT_METADATA
        if not _plain_file(metadata_path, root=Path(run_root).resolve()):
            continue
        record = _load_record(metadata_path)
        if record is None or record.get("result_id") != folder.name:
            continue
        blockers: list[dict[str, str]] = []
        if record.get("dataset_sha256") != current_dataset["dataset_sha256"]:
            blockers.append(
                _issue(
                    "dataset_changed",
                    "The selected dataset changed after this result was registered.",
                )
            )
        try:
            result_path = _stored_result_path(
                run_root,
                folder=folder,
                record=record,
            )
        except ValueError as exc:
            result_path = None
            blockers.append(_issue("result_path_invalid", str(exc)))
        if result_path is not None and not _plain_file(
            result_path,
            root=Path(run_root).resolve(),
        ):
            blockers.append(
                _issue("result_missing", "The registered BOP result CSV is missing.")
            )
        elif result_path is not None:
            stat = result_path.stat(follow_symlinks=False)
            if stat.st_size != record.get(
                "size_bytes"
            ) or stat.st_mtime_ns != record.get("mtime_ns"):
                blockers.append(
                    _issue(
                        "result_changed",
                        "The registered BOP result CSV metadata changed after import.",
                    )
                )
        if not isinstance(record.get("sha256"), str):
            blockers.append(
                _issue(
                    "result_hash_missing",
                    "The registered BOP result has no immutable content hash.",
                )
            )
        record["compatible"] = not blockers
        record["blockers"] = blockers
        records.append(record)
    return sorted(
        records, key=lambda item: str(item.get("created_at", "")), reverse=True
    )


def get_result(
    run_root: str | Path,
    result_id: str,
    *,
    dataset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not RESULT_ID_RE.fullmatch(result_id):
        raise KeyError("Unknown BOP result")
    metadata_path = _result_dir(run_root, result_id) / RESULT_METADATA
    if not _plain_file(metadata_path, root=Path(run_root).resolve()):
        raise KeyError("Unknown BOP result")
    record = _load_record(metadata_path)
    if record is None:
        raise KeyError("Unknown BOP result")
    current = next(
        (
            item
            for item in list_results(run_root, dataset=dataset)
            if item["result_id"] == result_id
        ),
        None,
    )
    if current is None:
        raise KeyError("Unknown BOP result")
    return current


def result_file_path(
    run_root: str | Path,
    result_id: str,
    *,
    dataset: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve a registered CSV only inside its immutable result folder."""

    result = get_result(run_root, result_id, dataset=dataset)
    if not result["compatible"]:
        raise ValueError(
            "Registered BOP result is not compatible: "
            + " ".join(issue["message"] for issue in result["blockers"])
        )
    return _stored_result_path(
        run_root,
        folder=_result_dir(run_root, result_id),
        record=result,
    )


def result_download_path(run_root: str | Path, result_id: str) -> Path:
    """Resolve an intact historical CSV independent of current dataset drift."""

    result = get_result(run_root, result_id)
    path = _stored_result_path(
        run_root,
        folder=_result_dir(run_root, result_id),
        record=result,
    )
    if not _plain_file(path, root=Path(run_root).resolve()):
        raise FileNotFoundError("Registered BOP result CSV is missing")
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size != result.get("size_bytes"):
        raise RuntimeError("Registered BOP result size changed after import")
    expected = result.get("sha256")
    if not isinstance(expected, str) or _sha256_file(path) != expected:
        raise RuntimeError("Registered BOP result integrity check failed")
    return path


def create_evaluation_request(
    run_root: str | Path,
    *,
    result_id: str | None = None,
    simulation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an immutable queued-work request after current-input validation."""

    dataset = inspect_dataset(run_root)
    if not dataset["evaluation_ready"]:
        raise ValueError("BOP evaluation is blocked: " + " ".join(dataset["blockers"]))
    if (result_id is None) == (simulation is None):
        raise ValueError("Choose exactly one registered result or GT simulation")
    normalized_simulation = (
        _normalize_simulation(simulation) if simulation is not None else None
    )
    if (
        normalized_simulation is not None
        and isinstance(simulation, Mapping)
        and "score" not in simulation
    ):
        normalized_simulation.pop("score", None)
    if result_id is not None:
        result = get_result(run_root, result_id, dataset=dataset)
        if result["dataset_sha256"] != dataset["dataset_sha256"]:
            raise ValueError(
                "The BOP dataset changed after this result was registered; "
                "the dataset hash no longer matches."
            )
        if not result["compatible"]:
            raise ValueError(
                "Registered BOP result is not compatible: "
                + " ".join(issue["message"] for issue in result["blockers"])
            )
        result_path = result_file_path(
            run_root,
            result_id,
            dataset=dataset,
        )
        validation = validate_bop_result_csv(result_path, dataset=dataset)
        if validation["sha256"] != result["sha256"]:
            raise ValueError(
                "Registered BOP result CSV no longer matches its immutable hash"
            )

    evaluation_id = f"evaluation-{uuid.uuid4().hex[:12]}"
    request_value = {
        "schema_version": "bop_evaluation_request.v1",
        "evaluation_id": evaluation_id,
        "run_root": Path(run_root).resolve().as_posix(),
        "dataset_alias": dataset["dataset_alias"],
        "dataset_sha256": dataset["dataset_sha256"],
        "export_manifest_sha256": dataset["export_manifest_sha256"],
        "split": dataset["split"],
        "protocol": "bop19_localization",
        "result_id": result_id,
        "simulation": normalized_simulation,
        "vsd_delta_mm": DEFAULT_VSD_DELTA_MM,
        "renderer_type": DEFAULT_RENDERER,
        "num_workers": max(
            1, min(int(os.environ.get("POSETESTBOT_BOP_NUM_WORKERS", "4")), 32)
        ),
        "toolkit_revision": TOOLKIT_REVISION,
        "adapter_revision": DATASET_ADAPTER_REVISION,
        "created_at": _utc_now(),
    }
    folder = _evaluation_dir(run_root, evaluation_id)
    folder.mkdir(parents=True, exist_ok=False)
    try:
        atomic_write_json(folder / EVALUATION_REQUEST, request_value)
        atomic_write_json(
            folder / EVALUATION_PROGRESS,
            {
                "schema_version": "bop_evaluation_progress.v1",
                "evaluation_id": evaluation_id,
                "status": "queued",
                "updated_at": _utc_now(),
            },
        )
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return request_value


def evaluation_request_path(run_root: str | Path, evaluation_id: str) -> Path:
    if not EVALUATION_ID_RE.fullmatch(evaluation_id):
        raise KeyError("Unknown BOP evaluation")
    return _evaluation_dir(run_root, evaluation_id) / EVALUATION_REQUEST


def evaluation_report_path(run_root: str | Path, evaluation_id: str) -> Path:
    if not EVALUATION_ID_RE.fullmatch(evaluation_id):
        raise KeyError("Unknown BOP evaluation")
    folder = _evaluation_dir(run_root, evaluation_id)
    report = folder / EVALUATION_REPORT
    if (
        not folder.is_dir()
        or folder.is_symlink()
        or not _plain_file(report, root=Path(run_root).resolve())
    ):
        raise KeyError("BOP evaluation report is not available")
    return report


def list_evaluations(
    run_root: str | Path,
    *,
    dataset: Mapping[str, Any] | None = None,
    results: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    root = _evaluation_root(run_root) / EVALUATIONS_DIR
    try:
        folders = list(root.iterdir())
    except FileNotFoundError:
        return []
    result_records = (
        [dict(item) for item in results]
        if results is not None
        else list_results(run_root, dataset=dataset)
    )
    results_by_id = {item["result_id"]: item for item in result_records}
    evaluations: list[dict[str, Any]] = []
    for folder in folders:
        if not folder.is_dir() or folder.is_symlink():
            continue
        if not EVALUATION_ID_RE.fullmatch(folder.name):
            continue
        request_path = folder / EVALUATION_REQUEST
        if not _plain_file(request_path, root=Path(run_root).resolve()):
            continue
        request_value = _load_record(request_path)
        if request_value is None or request_value.get("evaluation_id") != folder.name:
            continue
        report_path = folder / EVALUATION_REPORT
        progress_path = folder / EVALUATION_PROGRESS
        report = (
            _load_record(report_path)
            if _plain_file(report_path, root=Path(run_root).resolve())
            else None
        )
        progress = (
            _load_record(progress_path)
            if _plain_file(progress_path, root=Path(run_root).resolve())
            else None
        ) or {}
        result_id = (
            report.get("result_id")
            if report is not None
            else request_value.get("result_id")
        )
        result = results_by_id.get(str(result_id)) if result_id else None
        summary = {
            "evaluation_id": request_value["evaluation_id"],
            "created_at": request_value.get("created_at"),
            "completed_at": report.get("completed_at") if report else None,
            "result_id": result_id,
            "result": result,
            "source_kind": (
                "gt_simulation"
                if request_value.get("simulation") is not None
                else "registered_result"
            ),
            "simulation": request_value.get("simulation"),
            "protocol": "BOP19 localization",
            "status": (
                report.get("status")
                if report is not None
                else progress.get("status", "queued")
            ),
            "metrics": report.get("metrics", []) if report else [],
            "provenance": report.get("provenance", {}) if report else {},
            "report_available": report is not None,
        }
        evaluations.append(summary)
    return sorted(
        evaluations,
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )


def toolkit_status(app_root: str | Path) -> dict[str, Any]:
    root = Path(app_root).resolve()
    toolkit_root = root / "third_party" / "bop_toolkit"
    runtime_root = root / "tools" / "bop_toolkit_runtime"
    python_path = runtime_root / ".venv" / "bin" / "python"
    revision = None
    checkout_clean = False
    if (toolkit_root / ".git").exists():
        try:
            revision = subprocess.run(
                ["git", "-C", toolkit_root.as_posix(), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            checkout_clean = not subprocess.run(
                [
                    "git",
                    "-C",
                    toolkit_root.as_posix(),
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            revision = None
            checkout_clean = False
    checkout_ready = (
        revision == TOOLKIT_REVISION
        and checkout_clean
        and (toolkit_root / "scripts" / "eval_calc_errors.py").is_file()
        and (toolkit_root / "scripts" / "eval_calc_scores.py").is_file()
    )
    environment_ready = python_path.is_file()
    available = checkout_ready and environment_ready
    reason = None
    if revision == TOOLKIT_REVISION and not checkout_clean:
        reason = "The pinned BOP Toolkit source checkout has local changes."
    elif not checkout_ready:
        reason = (
            "The pinned BOP Toolkit source checkout is missing or at another revision."
        )
    elif not environment_ready:
        reason = "The isolated BOP Toolkit uv environment has not been synchronized."
    return {
        "status": "ready" if available else "unavailable",
        "available": available,
        "revision": revision,
        "required_revision": TOOLKIT_REVISION,
        "checkout_clean": checkout_clean,
        "environment_ready": environment_ready,
        "renderer": DEFAULT_RENDERER,
        "install_command": (
            None if available else "bash scripts/install.sh --with-bop-toolkit"
        ),
        "reason": reason,
    }


def _progress_path(request_path: Path) -> Path:
    return request_path.parent / EVALUATION_PROGRESS


def _write_progress(
    request_path: Path,
    *,
    evaluation_id: str,
    status: str,
    message: str | None = None,
) -> None:
    atomic_write_json(
        _progress_path(request_path),
        {
            "schema_version": "bop_evaluation_progress.v1",
            "evaluation_id": evaluation_id,
            "status": status,
            "message": message,
            "updated_at": _utc_now(),
        },
    )


def _metric_values(scores: Mapping[str, Any]) -> list[dict[str, Any]]:
    definitions = (
        ("bop19_average_recall", "Average Recall", None),
        ("bop19_average_recall_vsd", "AR VSD", None),
        ("bop19_average_recall_mssd", "AR MSSD", None),
        ("bop19_average_recall_mspd", "AR MSPD", None),
        ("bop19_average_time_per_image", "Average time / image", "s"),
    )
    metrics: list[dict[str, Any]] = []
    for identifier, label, unit in definitions:
        if identifier not in scores:
            raise ValueError(f"Official BOP score output is missing {identifier}")
        value = float(scores[identifier])
        if not math.isfinite(value):
            raise ValueError(f"Official BOP score {identifier} is not finite")
        display = (
            "Unavailable"
            if identifier == "bop19_average_time_per_image" and value < 0
            else f"{value:.4f}"
        )
        metric = {
            "id": identifier,
            "label": label,
            "value": value,
            "display": display,
        }
        if unit is not None:
            metric["unit"] = unit
        metrics.append(metric)
    return metrics


def run_evaluation_request(
    request_path: str | Path,
    *,
    app_root: str | Path,
) -> dict[str, Any]:
    """Resolve one queued request and run official BOP19 metric scripts."""

    path = Path(request_path).absolute()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"BOP evaluation request is invalid: {path}")
    request_value = _load_record(path)
    if request_value is None:
        raise ValueError(f"BOP evaluation request is invalid: {path}")
    if request_value.get("schema_version") != "bop_evaluation_request.v1":
        raise ValueError("Unsupported BOP evaluation request schema")
    evaluation_id = str(request_value["evaluation_id"])
    run_root = Path(str(request_value["run_root"])).resolve()
    expected_path = evaluation_request_path(run_root, evaluation_id).absolute()
    if path != expected_path or not _plain_file(path, root=run_root):
        raise ValueError("BOP evaluation request path does not match its run and ID")
    report_path = path.parent / EVALUATION_REPORT
    if report_path.exists() and not _plain_file(report_path, root=run_root):
        raise ValueError("BOP evaluation report path is not a regular run-owned file")
    existing = _load_record(report_path)
    if existing is not None and existing.get("status") == "succeeded":
        return existing

    _write_progress(
        path,
        evaluation_id=evaluation_id,
        status="running",
        message="Validating immutable dataset and result inputs.",
    )
    try:
        dataset = inspect_dataset(run_root, include_depth_content=True)
        if not dataset["evaluation_ready"]:
            raise ValueError(
                "BOP evaluation dataset is no longer ready: "
                + " ".join(dataset["blockers"])
            )
        if dataset["dataset_sha256"] != request_value.get("dataset_sha256"):
            raise ValueError(
                "BOP evaluation dataset changed after the request was queued"
            )
        dataset_content_sha256 = dataset.get("dataset_content_sha256")
        if not isinstance(dataset_content_sha256, str):
            raise ValueError("BOP evaluation depth inputs were not content-hashed")
        root = Path(app_root).resolve()
        status = toolkit_status(root)
        if not status["available"]:
            raise RuntimeError(
                "Pinned BOP Toolkit runtime is unavailable: "
                + str(status.get("reason") or "")
            )

        resolved_path = path.parent / "resolved_source.json"
        if resolved_path.exists() and not _plain_file(
            resolved_path,
            root=run_root,
        ):
            raise ValueError(
                "BOP evaluation resolved-source path is not a regular run-owned file"
            )
        resolved = _load_record(resolved_path)
        if resolved is not None:
            result = get_result(
                run_root,
                str(resolved["result_id"]),
                dataset=dataset,
            )
        elif request_value.get("simulation") is not None:
            simulation = dict(request_value["simulation"])
            result = create_simulated_bop_result(
                run_root,
                method_name=str(simulation.get("method_name") or "GT slight offset"),
                translation_sigma_mm=float(simulation.get("translation_sigma_mm", 1.0)),
                rotation_sigma_deg=float(simulation.get("rotation_sigma_deg", 0.25)),
                seed=int(simulation.get("seed", 42)),
                score=float(simulation.get("score", 1.0)),
            )
            result = get_result(
                run_root,
                str(result["result_id"]),
                dataset=dataset,
            )
            atomic_write_json(
                resolved_path,
                {
                    "schema_version": "bop_evaluation_resolved_source.v1",
                    "evaluation_id": evaluation_id,
                    "result_id": result["result_id"],
                    "resolved_at": _utc_now(),
                },
            )
        else:
            result = get_result(
                run_root,
                str(request_value["result_id"]),
                dataset=dataset,
            )
            atomic_write_json(
                resolved_path,
                {
                    "schema_version": "bop_evaluation_resolved_source.v1",
                    "evaluation_id": evaluation_id,
                    "result_id": result["result_id"],
                    "resolved_at": _utc_now(),
                },
            )
        if not result["compatible"]:
            raise ValueError("Resolved BOP result is no longer compatible")
        result_path = result_file_path(
            run_root,
            str(result["result_id"]),
            dataset=dataset,
        )
        validation = validate_bop_result_csv(result_path, dataset=dataset)
        if validation["sha256"] != result["sha256"]:
            raise ValueError("Resolved BOP result no longer matches its immutable hash")

        inventory = _dataset_inventory(run_root)
        image_size = dataset.get("image_size")
        if not isinstance(image_size, list) or len(image_size) != 2:
            raise ValueError("BOP dataset has no uniform evaluation image size")
        adapter_path = path.parent / "dataset_adapter.json"
        adapter = {
            "schema_version": "posetestbot_bop_toolkit_adapter.v1",
            "adapter_revision": DATASET_ADAPTER_REVISION,
            "dataset_alias": dataset["dataset_alias"],
            "dataset_sha256": dataset["dataset_sha256"],
            "bop_root": (run_root / "bop").as_posix(),
            "split": dataset["split"],
            "image_size": image_size,
            "scene_ids": sorted(
                {int(target["scene_id"]) for target in inventory["targets"]}
            ),
            "object_ids": sorted(inventory["object_ids"]),
            "vsd_delta_mm": float(request_value["vsd_delta_mm"]),
        }
        atomic_write_json(adapter_path, adapter)

        toolkit_root = root / "third_party" / "bop_toolkit"
        runtime_root = root / "tools" / "bop_toolkit_runtime"
        toolkit_eval = path.parent / "toolkit"
        toolkit_eval.mkdir(parents=True, exist_ok=True)
        command = [
            "uv",
            "run",
            "--project",
            runtime_root.as_posix(),
            "--no-sync",
            "python",
            "-m",
            "posetestbot.bop.toolkit_driver",
            "--toolkit-root",
            toolkit_root.as_posix(),
            "--datasets-path",
            (run_root / "bop").parent.as_posix(),
            "--results-path",
            result_path.parent.as_posix(),
            "--eval-path",
            toolkit_eval.as_posix(),
            "--result-filename",
            result_path.name,
            "--dataset-alias",
            str(dataset["dataset_alias"]),
            "--split",
            str(dataset["split"]),
            "--image-size",
            str(image_size[0]),
            str(image_size[1]),
            "--renderer-type",
            str(request_value["renderer_type"]),
            "--vsd-delta-mm",
            str(request_value["vsd_delta_mm"]),
            "--num-workers",
            str(request_value["num_workers"]),
        ]
        overlay = root / "posetestbot" / "bop" / "toolkit_overlay"
        python_path = os.pathsep.join(
            item
            for item in (
                overlay.as_posix(),
                root.as_posix(),
                os.environ.get("PYTHONPATH", ""),
            )
            if item
        )
        environment = os.environ.copy()
        # Do not let the parent PoseTestBot environment override the isolated
        # uv project's NumPy<2 BOP Toolkit environment.
        environment.pop("VIRTUAL_ENV", None)
        environment.update(
            {
                "PYTHONPATH": python_path,
                "POSETESTBOT_BOP_ADAPTER_CONFIG": adapter_path.as_posix(),
                "BOP_PATH": (run_root / "bop").parent.as_posix(),
                "BOP_RESULTS_PATH": result_path.parent.as_posix(),
                "BOP_EVAL_PATH": toolkit_eval.as_posix(),
                "BOP_NUM_WORKERS": str(request_value["num_workers"]),
                "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", "/tmp/uv-cache"),
            }
        )
        # Vispy's EGL backend needs an explicit headless platform on Mesa-only
        # lab hosts; it may still use hardware acceleration when available.
        environment.setdefault("EGL_PLATFORM", "surfaceless")
        environment.setdefault("PYOPENGL_PLATFORM", "egl")
        _write_progress(
            path,
            evaluation_id=evaluation_id,
            status="running",
            message="Running official BOP19 VSD, MSSD, and MSPD metrics.",
        )
        print("$ " + " ".join(command), flush=True)
        subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=True,
        )

        if _sha256_file(result_path) != result["sha256"]:
            raise ValueError(
                "Resolved BOP result changed while the metrics were running"
            )
        final_dataset = inspect_dataset(run_root, include_depth_content=True)
        if (
            not final_dataset["evaluation_ready"]
            or final_dataset["dataset_sha256"] != dataset["dataset_sha256"]
            or final_dataset.get("dataset_content_sha256") != dataset_content_sha256
        ):
            raise ValueError(
                "BOP evaluation dataset changed while the metrics were running"
            )

        result_name = result_path.name.split(".", 1)[0]
        official_scores_path = toolkit_eval / result_name / "scores_bop19.json"
        if not _plain_file(official_scores_path, root=run_root):
            raise ValueError(
                "Official BOP score output is missing or not a regular run-owned file"
            )
        scores = _load_json(official_scores_path)
        if not isinstance(scores, Mapping):
            raise ValueError("Official BOP score output must be a JSON object")
        completed_at = _utc_now()
        report = {
            "schema_version": "bop_evaluation_report.v1",
            "evaluation_id": evaluation_id,
            "status": "succeeded",
            "created_at": request_value["created_at"],
            "completed_at": completed_at,
            "protocol": "BOP19 localization",
            "result_id": result["result_id"],
            "source_kind": result["source_kind"],
            "simulation": result.get("simulation"),
            "metrics": _metric_values(scores),
            "official_scores": dict(scores),
            "provenance": {
                "toolkit_revision": TOOLKIT_REVISION,
                "adapter_revision": DATASET_ADAPTER_REVISION,
                "renderer_type": request_value["renderer_type"],
                "vsd_delta_mm": request_value["vsd_delta_mm"],
                "num_workers": request_value["num_workers"],
                "dataset_alias": dataset["dataset_alias"],
                "dataset_sha256": dataset["dataset_sha256"],
                "dataset_content_sha256": dataset_content_sha256,
                "export_manifest_sha256": dataset["export_manifest_sha256"],
                "result_filename": result["filename"],
                "result_sha256": result["sha256"],
                "result_validation": validation,
                "official_scores_path": _relative_to_run(
                    official_scores_path, run_root
                ),
                "adapter_config_path": _relative_to_run(adapter_path, run_root),
                "command": command,
            },
        }
        atomic_write_json(report_path, report)
        _write_progress(
            path,
            evaluation_id=evaluation_id,
            status="succeeded",
            message="Official BOP19 metrics and provenance are available.",
        )
        return report
    except Exception as exc:
        _write_progress(
            path,
            evaluation_id=evaluation_id,
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
        )
        raise
