"""Read-only aggregation of published BOP pose ground truth by sensor scene."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from posetestbot.bop.writer import validate_scene_gt


MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SCENE_GT_BYTES = 32 * 1024 * 1024
MAX_GT_PROVENANCE_BYTES = 8 * 1024 * 1024
MAX_COMBINED_GT_BYTES = 64 * 1024 * 1024
MAX_SENSOR_SCENES = 16
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SENSOR_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
SPLIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


def _read_regular_file(path: Path, *, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"BOP artifact is missing: {path.name}")
    if path.stat().st_size > limit:
        raise ValueError(f"BOP artifact exceeds the {limit} byte download limit")
    with path.open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"BOP artifact exceeds the {limit} byte download limit")
    return content


def _read_exported_scene_gt(
    bop_root: Path, export: Mapping[str, Any]
) -> tuple[int, str, str, Mapping[str, Any], str, int]:
    scene_id = export.get("scene_id")
    if type(scene_id) is not int or scene_id < 1:
        raise ValueError("The BOP export has an invalid scene ID")
    split = export.get("split")
    sensor_name = export.get("sensor_name")
    if not isinstance(split, str) or SPLIT_RE.fullmatch(split) is None:
        raise ValueError("The exported scene has an invalid split")
    if (
        not isinstance(sensor_name, str)
        or SENSOR_NAME_RE.fullmatch(sensor_name) is None
    ):
        raise ValueError("The exported scene has an invalid sensor name")
    scene_folder = f"{split}/{scene_id:06d}"
    gt_relative = f"{scene_folder}/scene_gt.json"
    artifacts = export.get("artifacts")
    provenance = export.get("annotation_provenance")
    provenance_relative = f"{scene_folder}/posetestbot_gt_provenance.json"
    expected_hash = (
        provenance.get("scene_gt_sha256") if isinstance(provenance, Mapping) else None
    )
    provenance_hash = (
        provenance.get("sha256") if isinstance(provenance, Mapping) else None
    )
    annotation_mode = export.get("annotation_mode")
    if (
        export.get("scene_folder") != scene_folder
        or export.get("annotation_source") != "blenderproc"
        or annotation_mode not in {"pose", "pose_and_masks"}
        or not isinstance(artifacts, Mapping)
        or artifacts.get("scene_gt") != gt_relative
        or artifacts.get("gt_provenance") != provenance_relative
        or not isinstance(provenance, Mapping)
        or provenance.get("artifact") != provenance_relative
        or provenance.get("schema_version") != "posetestbot_gt_provenance.v1"
        or provenance.get("annotation_mode") != annotation_mode
        or not isinstance(expected_hash, str)
        or SHA256_RE.fullmatch(expected_hash) is None
        or not isinstance(provenance_hash, str)
        or SHA256_RE.fullmatch(provenance_hash) is None
    ):
        raise ValueError("The exported scene has no valid pose GT binding")
    split_root = bop_root / split
    scene_root = split_root / f"{scene_id:06d}"
    if split_root.is_symlink() or scene_root.is_symlink():
        raise ValueError("The BOP scene must not use symbolic links")
    provenance_content = _read_regular_file(
        scene_root / "posetestbot_gt_provenance.json",
        limit=MAX_GT_PROVENANCE_BYTES,
    )
    if hashlib.sha256(provenance_content).hexdigest() != provenance_hash:
        raise ValueError("The BOP scene GT provenance hash does not match its manifest")
    provenance_record = json.loads(provenance_content)
    frame_count = export.get("rgb_count")
    if (
        not isinstance(provenance_record, Mapping)
        or provenance_record.get("schema_version") != "posetestbot_gt_provenance.v1"
        or provenance_record.get("annotation_mode") != annotation_mode
        or not isinstance(provenance_record.get("frame_bindings"), list)
        or type(frame_count) is not int
        or frame_count < 1
        or len(provenance_record["frame_bindings"]) != frame_count
        or provenance.get("frame_binding_count") != frame_count
    ):
        raise ValueError("The BOP scene GT provenance is invalid")
    content = _read_regular_file(scene_root / "scene_gt.json", limit=MAX_SCENE_GT_BYTES)
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("The BOP scene ground-truth hash does not match its manifest")
    scene_gt = json.loads(content)
    if not isinstance(scene_gt, Mapping):
        raise ValueError("The BOP scene ground truth is invalid")
    validate_scene_gt(scene_gt, frame_count=frame_count, object_name_to_id=None)
    return scene_id, sensor_name, split, scene_gt, expected_hash, len(content)


def combined_scene_gt_bytes(run_root: Path) -> bytes:
    """Return one bounded JSON file keyed by BOP scene and image ID."""

    bop_root = run_root / "bop"
    if bop_root.is_symlink() or not bop_root.is_dir():
        raise FileNotFoundError("The run has no BOP export")
    manifest_bytes = _read_regular_file(
        bop_root / "bop_export_manifest.json", limit=MAX_MANIFEST_BYTES
    )
    manifest: Any = json.loads(manifest_bytes)
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "bop_export_manifest.v5"
    ):
        raise ValueError("The BOP export manifest is unsupported")
    if (
        manifest.get("annotation_state") != "complete"
        or manifest.get("annotation_source") != "blenderproc"
    ):
        raise ValueError("The BOP export has no completed pose ground truth")
    exports = manifest.get("exports")
    if not isinstance(exports, list) or not 1 <= len(exports) <= MAX_SENSOR_SCENES:
        raise ValueError("The BOP export has no bounded sensor scene inventory")

    scene_gt_by_id: dict[str, Mapping[str, Any]] = {}
    sensor_by_scene_id: dict[str, str] = {}
    source_hash_by_scene_id: dict[str, str] = {}
    split_name: str | None = None
    total_source_bytes = 0
    for export in exports:
        if not isinstance(export, Mapping):
            raise ValueError("The BOP export has an invalid sensor scene entry")
        scene_id, sensor_name, split, scene_gt, digest, source_bytes = (
            _read_exported_scene_gt(bop_root, export)
        )
        key = str(scene_id)
        if key in scene_gt_by_id:
            raise ValueError("The BOP export has duplicate sensor scene IDs")
        if split_name is not None and split != split_name:
            raise ValueError("The BOP export mixes sensor scene splits")
        split_name = split
        total_source_bytes += source_bytes
        if total_source_bytes > MAX_COMBINED_GT_BYTES:
            raise ValueError(
                "The combined scene ground truth exceeds the download limit"
            )
        scene_gt_by_id[key] = scene_gt
        sensor_by_scene_id[key] = sensor_name
        source_hash_by_scene_id[key] = digest

    result = json.dumps(
        {
            "schema_version": "posetestbot_bop_scene_gt_collection.v1",
            "split": split_name,
            "sensor_by_scene_id": sensor_by_scene_id,
            "scene_gt_sha256_by_scene_id": source_hash_by_scene_id,
            "scene_gt": scene_gt_by_id,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(result) > MAX_COMBINED_GT_BYTES:
        raise ValueError("The combined scene ground truth exceeds the download limit")
    return result
