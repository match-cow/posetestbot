"""Run-scoped readiness and provenance for optional BOP ground truth.

This module is deliberately orchestration-only.  It validates the immutable
pose-template and calibration inputs that define model-to-camera ground truth;
it does not estimate poses and it never touches acquisition data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping

from posetestbot.bop.evaluation import toolkit_status
from posetestbot.bop.mask_driver import (
    GENERATION_REPORT,
    RENDERER_TYPE,
    TOOLKIT_REVISION,
    VISIBILITY_DELTA_MM,
    bop_gt_output_sha256,
)
from posetestbot.bop.writer import (
    targets_from_scene_gt,
    validate_scene_gt,
)
from posetestbot.blenderproc.rendering import ANALYTIC_IMPLEMENTATION_REVISION
from posetestbot.calibration.profile_library import (
    verify_calibration_profile_selection,
)
from posetestbot.io.artifacts import (
    BOP_ANNOTATION_GENERATION_REPORT,
    BOP_DIR,
    BOP_EXPORT_MANIFEST,
    CAM_K,
    DEPTH_DIR,
    MATCH_ROBOT_EE_POSES,
    PROCESSED_DIR,
    RGB_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.pipeline.sensor_selection import enabled_sensor_folder_names
from posetestbot.pose_templates.selection import load_pose_template_selection


ANNOTATION_MODES = frozenset({"pose", "pose_and_masks"})
BLENDERPROC_REQUIRED_VERSION = "2.8.0"
ANNOTATION_REPORT = (
    Path(PROCESSED_DIR) / "bop_annotations" / BOP_ANNOTATION_GENERATION_REPORT
)
_OUTPUT_CACHE_LOCK = threading.Lock()
_OUTPUT_CACHE: dict[
    tuple[str, str],
    tuple[dict[str, Any] | None, str | None],
] = {}


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def validate_annotation_mode(value: object) -> str:
    mode = str(value or "").strip()
    if mode not in ANNOTATION_MODES:
        choices = ", ".join(sorted(ANNOTATION_MODES))
        raise ValueError(f"mode must be one of: {choices}")
    return mode


def annotation_input_folder(run_root: str | Path) -> Path:
    root = Path(run_root).resolve()
    rectified = root / PROCESSED_DIR / "rectified"
    candidate = (
        rectified if rectified.is_dir() else root / PROCESSED_DIR / SYNCHRONIZED_DIR
    )
    if candidate.is_symlink():
        raise ValueError(f"Annotation input folder must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Annotation inputs must remain inside the run") from exc
    return resolved


def selected_calibration_profiles(run_root: str | Path) -> Path:
    root = Path(run_root).resolve()
    config = load_run_config_for_run_root(root)
    raw = config.get("calibration_profiles")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "Run config does not select an immutable calibration_profiles snapshot"
        )
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else root / candidate
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Selected calibration_profiles snapshot must remain inside the run"
        ) from exc
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Selected calibration_profiles snapshot is missing: {resolved}"
        )
    verify_calibration_profile_selection(
        root,
        expected_calibration_profiles=resolved,
    )
    return resolved


def _load_json_mapping(path: Path, *, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _png_names(folder: Path, *, label: str) -> set[str]:
    if folder.is_symlink() or not folder.is_dir():
        raise FileNotFoundError(f"{label} folder is missing: {folder}")
    names = {item.name for item in folder.glob("*.png") if item.is_file()}
    if not names:
        raise FileNotFoundError(f"{label} folder contains no PNG frames: {folder}")
    return names


def _sensor_frame_count(sensor_folder: Path) -> int:
    rgb_names = _png_names(sensor_folder / RGB_DIR, label="RGB")
    depth_names = _png_names(sensor_folder / DEPTH_DIR, label="depth")
    if rgb_names != depth_names:
        raise ValueError(f"RGB/depth frame names do not match in {sensor_folder.name}")
    if not (sensor_folder / CAM_K).is_file():
        raise FileNotFoundError(
            f"Camera intrinsics are missing: {sensor_folder / CAM_K}"
        )
    poses = _load_json_mapping(
        sensor_folder / MATCH_ROBOT_EE_POSES,
        label="matched robot poses",
    )
    pose_names = {str(key) for key in poses}
    if pose_names != rgb_names:
        raise ValueError(
            "Matched robot-pose keys must exactly match the RGB/depth frame names "
            f"in {sensor_folder.name}"
        )
    return len(rgb_names)


def _blenderproc_status() -> dict[str, Any]:
    executable = shutil.which("blenderproc")
    detected_version = None
    reason = None
    if executable is not None:
        try:
            detected_version = subprocess.run(
                [executable, "-v"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            reason = "BlenderProc did not report its version."
    available = (
        executable is not None and detected_version == BLENDERPROC_REQUIRED_VERSION
    )
    if executable is None:
        reason = "BlenderProc is not on PATH."
    elif detected_version is not None and not available:
        reason = (
            f"BlenderProc {detected_version} is installed; "
            f"version {BLENDERPROC_REQUIRED_VERSION} is required."
        )
    return {
        "available": available,
        "required_version": BLENDERPROC_REQUIRED_VERSION,
        "detected_version": detected_version,
        "executable": executable,
        "install_command": (
            None if available else "bash scripts/install.sh --with-blenderproc"
        ),
        "reason": reason,
    }


def _nested_value(value: object, *keys: str) -> object | None:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _safe_scene_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("BOP scene_folder must be a non-empty relative path")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("BOP scene_folder must remain inside the dataset")
    bop_root = (root / BOP_DIR).resolve()
    scene = bop_root / relative_path
    current = bop_root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("BOP scene_folder must not use symbolic links")
    try:
        scene.resolve(strict=True).relative_to(bop_root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError("BOP scene_folder escapes the dataset root") from exc
    if not scene.is_dir():
        raise ValueError(f"BOP scene folder is missing: {scene}")
    return scene


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_bop_file(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} must be a non-empty BOP-relative path")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"{label} must remain inside the BOP dataset")
    bop_root = (root / BOP_DIR).resolve()
    path = bop_root / relative_path
    current = bop_root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} must not use symbolic links")
    try:
        path.resolve(strict=True).relative_to(bop_root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"{label} is missing or escapes the BOP dataset") from exc
    if not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    return path


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _verified_pose_provenance(
    root: Path,
    export: Mapping[str, Any],
    *,
    scene: Path,
    scene_gt_path: Path,
    mode: str,
) -> dict[str, Any]:
    record = export.get("annotation_provenance")
    expected_record_keys = {
        "artifact",
        "sha256",
        "schema_version",
        "blenderproc_version",
        "annotation_mode",
        "pose_contract",
        "analytic_implementation",
        "calibration_profile_id",
        "frame_binding_count",
        "scene_gt_sha256",
    }
    if not isinstance(record, Mapping) or set(record) != expected_record_keys:
        raise ValueError("BOP scene export has incomplete immutable pose provenance")
    provenance_path = _safe_bop_file(
        root,
        record.get("artifact"),
        label="BlenderProc GT provenance",
    )
    if provenance_path.parent != scene:
        raise ValueError("BlenderProc GT provenance is not stored with its BOP scene")
    provenance = _load_json_value(
        provenance_path,
        label="BlenderProc GT provenance",
    )
    if not isinstance(provenance, Mapping):
        raise ValueError("BlenderProc GT provenance must be a JSON object")
    implementation = provenance.get("analytic_implementation")
    if (
        provenance.get("schema_version") != "posetestbot_gt_provenance.v1"
        or provenance.get("blenderproc_version") != BLENDERPROC_REQUIRED_VERSION
        or provenance.get("supported_blenderproc_version")
        != BLENDERPROC_REQUIRED_VERSION
        or provenance.get("annotation_mode") != mode
        or provenance.get("pose_contract")
        != "analytic_model_to_opencv_camera_rigid_transform.v1"
        or provenance.get("translation_unit") != "mm"
        or provenance.get("rotation_storage") != "row_major_3x3"
        or not isinstance(implementation, Mapping)
        or implementation.get("revision") != ANALYTIC_IMPLEMENTATION_REVISION
        or not _valid_sha256(implementation.get("script_sha256"))
    ):
        raise ValueError("BlenderProc GT provenance contract is incompatible")
    frame_bindings = provenance.get("frame_bindings")
    if not isinstance(frame_bindings, list):
        raise ValueError("BlenderProc GT provenance has no frame bindings")
    calibration_profile_id = export.get("calibration_profile_id")
    if (
        not isinstance(calibration_profile_id, str)
        or not calibration_profile_id
        or record.get("calibration_profile_id") != calibration_profile_id
    ):
        raise ValueError(
            "BOP scene pose provenance does not bind its calibration profile"
        )
    expected_record = {
        "artifact": provenance_path.relative_to(root / BOP_DIR).as_posix(),
        "sha256": _sha256_file(provenance_path),
        "schema_version": provenance["schema_version"],
        "blenderproc_version": provenance["blenderproc_version"],
        "annotation_mode": provenance["annotation_mode"],
        "pose_contract": provenance["pose_contract"],
        "analytic_implementation": dict(implementation),
        "calibration_profile_id": calibration_profile_id,
        "frame_binding_count": len(frame_bindings),
        "scene_gt_sha256": _sha256_file(scene_gt_path),
    }
    if dict(record) != expected_record:
        raise ValueError(
            "BOP scene pose/provenance hashes do not match the published artifacts"
        )
    try:
        scene_id = int(export["scene_id"])
        sensor_name = str(export["sensor_name"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("BOP scene provenance identity is invalid") from exc
    if not sensor_name:
        raise ValueError("BOP scene provenance sensor name is missing")
    return {
        "scene_id": scene_id,
        "sensor_name": sensor_name,
        **expected_record,
    }


def _load_json_value(path: Path, *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {exc}") from exc


def _annotation_artifact_summary(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    exports = manifest.get("exports")
    if not isinstance(exports, list) or not exports:
        raise ValueError("BOP manifest contains no scene exports")
    annotation_count = 0
    mask_count = 0
    visible_mask_count = 0
    expected_targets: list[dict[str, int]] = []
    expected_pose_scenes: list[dict[str, Any]] = []
    mask_output_paths: list[Path] = []
    mask_scene_summaries: dict[str, dict[str, int]] = {}
    export_splits: set[str] = set()
    models = manifest.get("object_models")
    known_ids = (
        {
            int(item["obj_id"])
            for item in models
            if isinstance(item, Mapping) and "obj_id" in item
        }
        if isinstance(models, list)
        else set()
    )
    object_name_to_id = {str(obj_id): obj_id for obj_id in known_ids}
    for export in exports:
        if not isinstance(export, Mapping):
            raise ValueError("BOP scene export entry must be an object")
        scene = _safe_scene_path(root, export.get("scene_folder"))
        scene_gt = _load_json_value(
            scene / "scene_gt.json",
            label="scene_gt.json",
        )
        if not isinstance(scene_gt, Mapping):
            raise ValueError("scene_gt.json must be a JSON object")
        try:
            frame_count = int(export["rgb_count"])
            scene_id = int(export["scene_id"])
            split = str(export["split"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("BOP scene export counts/identity are invalid") from exc
        if not split:
            raise ValueError("BOP scene export split is invalid")
        export_splits.add(split)
        validate_scene_gt(
            scene_gt,
            frame_count=frame_count,
            object_name_to_id=object_name_to_id or None,
        )
        expected_pose_scenes.append(
            _verified_pose_provenance(
                root,
                export,
                scene=scene,
                scene_gt_path=scene / "scene_gt.json",
                mode=mode,
            )
        )
        expected_names: set[str] = set()
        scene_annotation_count = 0
        scene_info: Any = None
        if mode == "pose_and_masks":
            scene_info = _load_json_value(
                scene / "scene_gt_info.json",
                label="scene_gt_info.json",
            )
            if not isinstance(scene_info, Mapping) or set(scene_info) != set(scene_gt):
                raise ValueError(
                    "scene_gt_info.json image IDs do not match scene_gt.json"
                )
        elif any(
            path.exists()
            for path in (
                scene / "scene_gt_info.json",
                scene / "mask",
                scene / "mask_visib",
            )
        ):
            raise ValueError("Pose-only output contains stale visibility artifacts")

        for image_id, rows in scene_gt.items():
            if (
                not isinstance(image_id, str)
                or not image_id.isdigit()
                or not isinstance(rows, list)
            ):
                raise ValueError("scene_gt.json has an invalid image entry")
            image_info = scene_info[image_id] if scene_info is not None else None
            if image_info is not None and (
                not isinstance(image_info, list) or len(image_info) != len(rows)
            ):
                raise ValueError("scene_gt_info.json row counts do not match GT")
            annotation_count += len(rows)
            scene_annotation_count += len(rows)
            for gt_id, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    raise ValueError("scene_gt.json annotation must be an object")
                filename = f"{int(image_id):06d}_{gt_id:06d}.png"
                expected_names.add(filename)

        if mode == "pose_and_masks":
            for folder_name in ("mask", "mask_visib"):
                folder = scene / folder_name
                if folder.is_symlink() or not folder.is_dir():
                    raise ValueError(f"BOP {folder_name} folder is missing")
                entries = list(folder.iterdir())
                if any(path.is_symlink() or not path.is_file() for path in entries):
                    raise ValueError(
                        f"BOP {folder_name} folder contains a non-file artifact"
                    )
                if {path.name for path in entries} != expected_names:
                    raise ValueError(
                        f"BOP {folder_name} filenames do not match scene_gt.json"
                    )
                mask_output_paths.extend(entries)
            mask_count += len(expected_names)
            visible_mask_count += len(expected_names)
            mask_output_paths.append(scene / "scene_gt_info.json")
            # Publication already performs the expensive pixel/depth/ROI
            # validation. This read path re-hashes those exact validated mask
            # and GT-info bytes instead of decoding every RGB-D frame in a
            # Flask request.
            mask_scene_summaries[str(scene_id)] = {
                "image_count": len(scene_gt),
                "annotation_count": scene_annotation_count,
                "full_mask_count": len(expected_names),
                "visible_mask_count": len(expected_names),
            }
        expected_targets.extend(
            targets_from_scene_gt(
                scene_gt,
                scene_id=scene_id,
                scene_gt_info=scene_info,
            )
        )

    provenance = manifest.get("annotation_provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("schema_version") != "posetestbot_bop_gt_generation.v1"
        or provenance.get("annotation_mode") != mode
    ):
        raise ValueError(
            "BOP manifest annotation provenance is missing or incompatible"
        )
    pose_generation = provenance.get("pose_generation")
    if not isinstance(pose_generation, Mapping) or dict(pose_generation) != {
        "source": "blenderproc_analytic_gt",
        "scenes": expected_pose_scenes,
    }:
        raise ValueError(
            "BOP manifest pose-generation provenance does not match its scenes"
        )

    validation = manifest.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    if int(validation.get("annotation_count") or 0) != annotation_count:
        raise ValueError("BOP manifest annotation count is stale")
    if mode == "pose_and_masks":
        report = _load_json_value(
            root / BOP_DIR / GENERATION_REPORT,
            label="official BOP mask-generation report",
        )
        if len(export_splits) != 1:
            raise ValueError("Official BOP masks require one exported split")
        expected_report = {
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
            "split": next(iter(export_splits)),
            "scenes": mask_scene_summaries,
            "output_sha256": bop_gt_output_sha256(
                mask_output_paths,
                root=root / BOP_DIR,
            ),
        }
        if not isinstance(report, Mapping) or dict(report) != expected_report:
            raise ValueError(
                "Official BOP mask-generation report or output hash is invalid"
            )
        if provenance.get("mask_generation") != expected_report:
            raise ValueError(
                "BOP manifest mask-generation provenance does not match its report"
            )
    else:
        if provenance.get("mask_generation") != {"state": "absent"}:
            raise ValueError("Pose-only BOP provenance must declare masks absent")
        if (root / BOP_DIR / GENERATION_REPORT).exists():
            raise ValueError("Pose-only output contains a stale mask-generation report")
    targets_path = manifest.get("targets_path")
    targets = _load_json_value(
        root / BOP_DIR / str(targets_path or ""),
        label="BOP19 targets",
    )
    if not isinstance(targets, list) or not targets or targets != expected_targets:
        raise ValueError(
            "BOP19 targets do not exactly match the current annotation inventory"
        )
    return {
        "annotation_count": annotation_count,
        "mask_count": mask_count,
        "visible_mask_count": visible_mask_count,
    }


def _annotation_artifact_signature(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    mode: str,
) -> str:
    """Hash relevant file metadata so strict image validation can be cached."""

    bop_root = (root / BOP_DIR).resolve()
    paths: list[Path] = [
        bop_root / BOP_EXPORT_MANIFEST,
        bop_root / str(manifest.get("targets_path") or ""),
    ]
    if mode == "pose_and_masks":
        paths.append(bop_root / "posetestbot_gt_generation.json")
    exports = manifest.get("exports")
    if not isinstance(exports, list):
        raise ValueError("BOP manifest exports are invalid")
    for export in exports:
        if not isinstance(export, Mapping):
            raise ValueError("BOP scene export entry must be an object")
        scene = _safe_scene_path(root, export.get("scene_folder"))
        paths.append(scene / "scene_gt.json")
        scene_provenance = export.get("annotation_provenance")
        if not isinstance(scene_provenance, Mapping):
            raise ValueError("BOP scene export has no immutable pose provenance")
        paths.append(
            _safe_bop_file(
                root,
                scene_provenance.get("artifact"),
                label="BlenderProc GT provenance",
            )
        )
        if mode == "pose_and_masks":
            paths.append(scene / "scene_gt_info.json")
            for folder_name in (DEPTH_DIR, "mask", "mask_visib"):
                folder = scene / folder_name
                if folder.is_symlink() or not folder.is_dir():
                    raise ValueError(f"BOP {folder_name} folder is missing")
                paths.extend(sorted(folder.iterdir(), key=lambda item: item.name))
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        try:
            relative = path.resolve().relative_to(bop_root).as_posix()
        except ValueError as exc:
            raise ValueError("BOP annotation artifact escapes the dataset") from exc
        stat = path.lstat()
        digest.update(relative.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_ctime_ns).encode("ascii"))
        digest.update(str(stat.st_ino).encode("ascii"))
        digest.update(b"L" if path.is_symlink() else b"F")
    return digest.hexdigest()


def _current_output(root: Path) -> dict[str, Any] | None:
    manifest_path = root / BOP_DIR / BOP_EXPORT_MANIFEST
    try:
        manifest = _load_json_mapping(
            manifest_path,
            label="BOP export manifest",
        )
    except (FileNotFoundError, OSError, ValueError):
        return None
    if manifest.get("annotation_source") != "blenderproc":
        return None
    raw_mode = manifest.get("annotation_mode")
    mode = str(raw_mode) if raw_mode in ANNOTATION_MODES else "pose_and_masks"
    validation = manifest.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    capabilities = manifest.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    try:
        signature = _annotation_artifact_signature(root, manifest, mode=mode)
        cache_key = (root.as_posix(), signature)
        with _OUTPUT_CACHE_LOCK:
            cached = _OUTPUT_CACHE.get(cache_key)
        if cached is None:
            try:
                summary = _annotation_artifact_summary(root, manifest, mode=mode)
                integrity_error = None
            except (FileNotFoundError, OSError, ValueError) as exc:
                summary = None
                integrity_error = str(exc)
            with _OUTPUT_CACHE_LOCK:
                if len(_OUTPUT_CACHE) >= 8:
                    _OUTPUT_CACHE.pop(next(iter(_OUTPUT_CACHE)))
                _OUTPUT_CACHE[cache_key] = (summary, integrity_error)
        else:
            summary, integrity_error = cached
    except (FileNotFoundError, OSError, ValueError) as exc:
        summary = None
        integrity_error = str(exc)
    verified = summary is not None
    if summary is None:
        summary = {
            "annotation_count": int(validation.get("annotation_count") or 0),
            "mask_count": 0,
            "visible_mask_count": 0,
        }
    provenance = manifest.get("annotation_provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    pose_scenes = _nested_value(provenance, "pose_generation", "scenes")
    scene_versions = (
        {
            str(item["blenderproc_version"])
            for item in pose_scenes
            if isinstance(item, Mapping) and item.get("blenderproc_version")
        }
        if isinstance(pose_scenes, list)
        else set()
    )
    blenderproc_version = (
        next(iter(scene_versions))
        if len(scene_versions) == 1
        else provenance.get("blenderproc_version")
        or _nested_value(provenance, "blenderproc", "version")
    )
    toolkit_revision = (
        provenance.get("toolkit_revision")
        or _nested_value(provenance, "mask_generation", "toolkit_revision")
        or _nested_value(provenance, "bop_toolkit", "revision")
    )
    if len(scene_versions) > 1:
        verified = False
        integrity_error = (
            "BOP scenes declare different BlenderProc versions in annotation provenance"
        )
    return {
        "mode": mode,
        "state": str(manifest.get("annotation_state") or "unknown"),
        **summary,
        "verified": verified,
        "integrity_error": integrity_error,
        "evaluation_ready": bool(capabilities.get("bop19_evaluation")) and verified,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "blenderproc_version": blenderproc_version,
        "toolkit_revision": toolkit_revision,
    }


def inspect_annotation_setup(
    run_root: str | Path,
    *,
    app_root: str | Path,
) -> dict[str, Any]:
    """Return read-only readiness for both supported ground-truth products."""

    root = Path(run_root).resolve()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    counts = {"sensors": 0, "frames": 0, "instances": 0}
    provenance: dict[str, Any] = {}
    configured_mode: str | None = None

    try:
        config = load_run_config_for_run_root(root)
        if config.get("dataset_mode") != "pose_template":
            blockers.append(
                _issue(
                    "pose_template_dataset_required",
                    "Ground truth requires a run configured in pose_template dataset mode.",
                )
            )
        if config.get("capture", {}).get("intent") != "dataset":
            blockers.append(
                _issue(
                    "dataset_capture_intent_required",
                    "Ground truth requires a run configured for dataset capture.",
                )
            )
        raw_mode = config.get("bop", {}).get("annotation_mode")
        if raw_mode not in {"none", *ANNOTATION_MODES}:
            raise ValueError("Run config BOP annotation mode is invalid")
        configured_mode = str(raw_mode)
    except (FileNotFoundError, OSError, ValueError) as exc:
        config = {}
        blockers.append(_issue("invalid_run_config", str(exc)))

    try:
        selection = load_pose_template_selection(root)
        if not selection.get("placement_confirmed"):
            blockers.append(
                _issue(
                    "pose_template_placement_unconfirmed",
                    "Confirm the run-owned pose-template placement before generating GT.",
                )
            )
        instances = selection.get("instances")
        counts["instances"] = len(instances) if isinstance(instances, list) else 0
        if counts["instances"] < 1:
            blockers.append(
                _issue(
                    "pose_template_has_no_instances",
                    "The selected pose-template contains no object instances.",
                )
            )
        provenance.update(
            {
                "pose_template_uuid": selection.get("template_uuid"),
                "pose_template_bundle_sha256": selection.get("bundle_sha256"),
                "pose_template_selection_sha256": hashlib.sha256(
                    (root / "pose_template_selection.json").read_bytes()
                ).hexdigest(),
            }
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        blockers.append(_issue("invalid_pose_template_selection", str(exc)))

    try:
        calibration_path = selected_calibration_profiles(root)
        provenance["calibration_profiles"] = calibration_path.relative_to(
            root
        ).as_posix()
        provenance["calibration_profiles_sha256"] = hashlib.sha256(
            calibration_path.read_bytes()
        ).hexdigest()
    except (FileNotFoundError, OSError, ValueError) as exc:
        blockers.append(_issue("invalid_calibration_selection", str(exc)))

    try:
        input_folder = annotation_input_folder(root)
        sensor_names = enabled_sensor_folder_names(root)
        if not sensor_names:
            raise ValueError("Run config enables no sensors")
        for sensor_name in sensor_names:
            sensor_folder = input_folder / sensor_name
            if sensor_folder.is_symlink() or not sensor_folder.is_dir():
                raise FileNotFoundError(
                    f"Derived sensor folder is missing: {sensor_folder}"
                )
            counts["frames"] += _sensor_frame_count(sensor_folder)
        counts["sensors"] = len(sensor_names)
        provenance["input_folder"] = input_folder.relative_to(root).as_posix()
    except (FileNotFoundError, OSError, ValueError) as exc:
        blockers.append(_issue("invalid_annotation_inputs", str(exc)))

    manifest_path = root / BOP_DIR / BOP_EXPORT_MANIFEST
    try:
        manifest = _load_json_mapping(
            manifest_path,
            label="BOP export manifest",
        )
        capabilities = manifest.get("capabilities")
        if (
            manifest.get("schema_version") != "bop_export_manifest.v5"
            or not isinstance(capabilities, Mapping)
            or not capabilities.get("pose_estimation_input")
        ):
            raise ValueError(
                "Generate the standard BOP v5 pose-estimation input export first"
            )
        manifest_validation = manifest.get("validation")
        if isinstance(manifest_validation, Mapping):
            exported_frames = int(manifest_validation.get("frame_count") or 0)
            if counts["frames"] and exported_frames != counts["frames"]:
                raise ValueError(
                    "BOP export frame count does not match the selected derived inputs"
                )
        provenance["base_bop_manifest_sha256"] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
    except (FileNotFoundError, OSError, ValueError) as exc:
        blockers.append(_issue("invalid_bop_export", str(exc)))

    runtime = _blenderproc_status()
    common_blockers = list(blockers)
    if not runtime["available"]:
        common_blockers.append(
            _issue(
                "blenderproc_unavailable",
                str(runtime["reason"]),
            )
        )

    toolkit = toolkit_status(app_root)
    pose_warnings = [
        *warnings,
        _issue(
            "pose_gt_not_evaluation_ready",
            "Pose GT omits visibility masks and scene_gt_info.json, so BOP19 "
            "evaluation remains disabled.",
        ),
    ]
    full_blockers = list(common_blockers)
    if not toolkit["available"]:
        full_blockers.append(
            _issue(
                "bop_toolkit_unavailable",
                str(toolkit.get("reason") or "The pinned BOP Toolkit is unavailable."),
            )
        )

    pose_blockers = list(common_blockers)
    if configured_mode != "pose":
        pose_blockers.append(
            _issue(
                "annotation_mode_not_configured",
                "Run setup does not request pose-only ground truth.",
            )
        )
    if configured_mode != "pose_and_masks":
        full_blockers.append(
            _issue(
                "annotation_mode_not_configured",
                "Run setup does not request pose-and-mask ground truth.",
            )
        )

    return {
        "schema_version": "bop_annotation_setup.v1",
        "run_root": root.as_posix(),
        "configured_mode": configured_mode,
        "runtime": runtime,
        "toolkit": toolkit,
        "readiness_by_mode": {
            "pose": {
                "ready": not pose_blockers,
                "blockers": pose_blockers,
                "warnings": pose_warnings,
            },
            "pose_and_masks": {
                "ready": not full_blockers,
                "blockers": full_blockers,
                "warnings": warnings,
            },
        },
        "current_output": _current_output(root),
        "counts": counts,
        "provenance": provenance,
    }
