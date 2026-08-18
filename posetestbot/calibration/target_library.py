"""Immutable calibration-target bundles and transactional run selection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from scipy.spatial.transform import Rotation

from posetestbot.calibration.posegridgen import (
    POSEGRIDGEN_COMPATIBLE_BUNDLE_REVISIONS,
    POSEGRIDGEN_REVISION,
    render_posegridgen_bundle,
)
from posetestbot.calibration.targets import (
    SCHEMA_VERSION,
    load_calibration_target_spec,
    normalize_calibration_target_spec,
    target_from_posegridgen_manifest,
)
from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json
from posetestbot.io.artifacts import (
    ARUCO_DETECTIONS,
    BOP_DIR,
    CAPTURE_EXECUTION_LOGS_DIR,
    CAPTURE_EXECUTION_REPORT,
    CAPTURE_EXECUTION_STATUS,
    CALIBRATION_PROFILES,
    CALIBRATION_TARGET,
    CAMERA_RECTIFICATION_REPORT,
    DATASET_MANIFEST,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    INTRINSIC_CALIBRATION_PROFILES,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
    RUN_CONFIG,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    set_manifest_artifact,
    upsert_stage,
)
from posetestbot.pipeline.run_config import (
    load_run_config_for_run_root,
    run_config_lock,
    validate_run_config,
)


BUNDLE_SCHEMA_VERSION = "calibration_target_bundle.v1"
LIBRARY_DIRECTORY = "calibration_targets"
BUNDLE_MANIFEST = "calibration_target_bundle.json"
POSEGRIDGEN_SOURCE = "posegridgen_source.json"
TARGET_SPEC = "calibration_target.json"
TARGET_PDF = "calibration_target.pdf"
PLACEMENT_MODES = {
    "unknown",
    "template_base_identity",
    "posegridgen_board_to_base",
}
TARGET_MOUNTING_FRAMES = {"robot_flange", "template_base"}
_FILE_CONTRACT = {
    "source": (POSEGRIDGEN_SOURCE, "application/json"),
    "target": (TARGET_SPEC, "application/json"),
    "pdf": (TARGET_PDF, "application/pdf"),
}
_TARGET_DEPENDENT_NAMES = {
    ARUCO_DETECTIONS,
    INTRINSIC_CALIBRATION_PROFILES,
    CAMERA_RECTIFICATION_REPORT,
    CALIBRATION_PROFILES,
}


class CalibrationTargetConflict(RuntimeError):
    def __init__(self, message: str, *, blockers: list[str] | None = None):
        super().__init__(message)
        self.blockers = blockers or []


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def default_target_library_root() -> Path:
    configured = os.environ.get("POSETESTBOT_APP_ROOT")
    if configured:
        app_root = Path(configured).expanduser().resolve()
    else:
        source_root = Path(__file__).resolve().parents[2]
        app_root = (
            source_root if (source_root / "pyproject.toml").is_file() else Path.cwd()
        )
    return app_root / "working_data" / LIBRARY_DIRECTORY


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, *, relative_path: str, media_type: str) -> dict[str, Any]:
    return {
        "path": relative_path,
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _validate_target_id(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Calibration target ID must be a UUID") from exc
    return str(parsed)


def _ensure_contained(path: Path, root: Path, *, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes calibration-target library") from exc
    return resolved


def _reject_symlinks(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    if root.is_symlink():
        raise ValueError(f"Calibration-target library must not be a symlink: {root}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Calibration-target bundle contains a symlink: {current}")


def generate_target_bundle(
    *,
    display_name: str,
    configuration: Mapping[str, Any],
    library_root: str | Path | None = None,
    checkout: str | Path | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    name = str(display_name).strip()
    if not name:
        raise ValueError("Calibration target display name must not be empty")
    if len(name) > 120:
        raise ValueError(
            "Calibration target display name must not exceed 120 characters"
        )
    opaque_id = _validate_target_id(target_id or str(uuid.uuid4()))
    library = Path(library_root or default_target_library_root())
    library.mkdir(parents=True, exist_ok=True)
    destination = library / opaque_id
    if destination.exists():
        raise CalibrationTargetConflict(
            f"Calibration target already exists: {opaque_id}"
        )
    staging = library / f".{opaque_id}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        source_bytes, pdf_bytes, configuration_hash = render_posegridgen_bundle(
            configuration, checkout
        )
        source = json.loads(source_bytes)
        if not isinstance(source, Mapping):
            raise ValueError("PoseGridGen source manifest must be a JSON object")
        target = target_from_posegridgen_manifest(
            source,
            target_id=opaque_id,
            display_name=name,
        )
        target["posegridgen"] = {
            **dict(target["posegridgen"]),
            "revision": POSEGRIDGEN_REVISION,
        }
        target = normalize_calibration_target_spec(target)

        atomic_write_bytes(staging / POSEGRIDGEN_SOURCE, source_bytes)
        atomic_write_json(staging / TARGET_SPEC, target)
        atomic_write_bytes(staging / TARGET_PDF, pdf_bytes)
        files = {
            key: _file_record(
                staging / relative_path,
                relative_path=relative_path,
                media_type=media_type,
            )
            for key, (relative_path, media_type) in _FILE_CONTRACT.items()
        }
        bundle = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "target_id": opaque_id,
            "display_name": name,
            "created_at": utc_now_iso(),
            "generator": {
                "name": "PoseGridGen",
                "revision": POSEGRIDGEN_REVISION,
            },
            "configuration_sha256": configuration_hash,
            "geometry_sha256": target["geometry_sha256"],
            "files": files,
        }
        atomic_write_json(staging / BUNDLE_MANIFEST, bundle)
        validate_target_bundle(staging, library_root=library, allow_staging=True)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return validate_target_bundle(destination, library_root=library)


def validate_target_bundle(
    bundle_path: str | Path,
    *,
    library_root: str | Path | None = None,
    allow_staging: bool = False,
) -> dict[str, Any]:
    bundle_dir = Path(bundle_path)
    library = Path(library_root or bundle_dir.parent)
    _ensure_contained(bundle_dir, library, label="Bundle path")
    _reject_symlinks(bundle_dir, library)
    if not bundle_dir.is_dir():
        raise FileNotFoundError(
            f"Calibration-target bundle does not exist: {bundle_dir}"
        )
    manifest_path = bundle_dir / BUNDLE_MANIFEST
    _reject_symlinks(manifest_path, library)
    with open(manifest_path, "r") as handle:
        bundle = json.load(handle)
    if (
        not isinstance(bundle, Mapping)
        or bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError(f"Bundle schema must be {BUNDLE_SCHEMA_VERSION!r}")
    target_id = _validate_target_id(str(bundle.get("target_id", "")))
    if not allow_staging and bundle_dir.name != target_id:
        raise ValueError("Bundle directory name does not match target_id")
    if not str(bundle.get("display_name", "")).strip():
        raise ValueError("Bundle display_name must not be empty")
    generator = bundle.get("generator")
    generator_revision = (
        generator.get("revision") if isinstance(generator, Mapping) else None
    )
    if (
        not isinstance(generator_revision, str)
        or generator_revision not in POSEGRIDGEN_COMPATIBLE_BUNDLE_REVISIONS
    ):
        raise ValueError(
            "Bundle generator revision is not a compatible PoseGridGen revision"
        )
    records = bundle.get("files")
    if not isinstance(records, Mapping) or set(records) != set(_FILE_CONTRACT):
        raise ValueError("Bundle files must contain exactly source, target, and pdf")
    paths: dict[str, Path] = {}
    for key, (expected_relative, expected_media) in _FILE_CONTRACT.items():
        record = records.get(key)
        if not isinstance(record, Mapping):
            raise ValueError(f"Bundle file record is invalid: {key}")
        if (
            record.get("path") != expected_relative
            or record.get("media_type") != expected_media
        ):
            raise ValueError(f"Bundle file contract is invalid: {key}")
        path = bundle_dir / expected_relative
        _reject_symlinks(path, library)
        _ensure_contained(path, bundle_dir, label=f"Bundle {key} path")
        if not path.is_file():
            raise FileNotFoundError(f"Bundle file is missing: {path}")
        if path.stat().st_size != int(record.get("size_bytes", -1)):
            raise ValueError(f"Bundle file size does not match: {key}")
        if _sha256(path) != record.get("sha256"):
            raise ValueError(f"Bundle file SHA-256 does not match: {key}")
        paths[key] = path
    if not paths["pdf"].read_bytes().startswith(b"%PDF"):
        raise ValueError("Bundle PDF does not have a PDF signature")
    with open(paths["source"], "r") as handle:
        source = json.load(handle)
    if not isinstance(source, Mapping):
        raise ValueError("PoseGridGen source manifest must be an object")
    if source.get("configuration_hash") != bundle.get("configuration_sha256"):
        raise ValueError("Bundle configuration hash does not match source")
    target = load_calibration_target_spec(paths["target"])
    if target.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Immutable bundle target must use calibration_target.v2")
    if target.get("target_id") != target_id:
        raise ValueError("Bundle target_id does not match target spec")
    if target.get("geometry_sha256") != bundle.get("geometry_sha256"):
        raise ValueError("Bundle geometry hash does not match target spec")
    posegridgen = target.get("posegridgen")
    if not isinstance(posegridgen, Mapping):
        raise ValueError("Bundle target is missing PoseGridGen provenance")
    if posegridgen.get("revision") != generator_revision:
        raise ValueError(
            "Target generator revision does not match the bundle generator"
        )
    if posegridgen.get("configuration_hash") != bundle.get("configuration_sha256"):
        raise ValueError("Target configuration hash does not match bundle")
    canonical_target = target_from_posegridgen_manifest(
        source,
        target_id=target_id,
        display_name=str(bundle["display_name"]),
    )
    canonical_target["posegridgen"] = {
        **dict(canonical_target["posegridgen"]),
        "revision": generator_revision,
    }
    canonical_target = normalize_calibration_target_spec(canonical_target)
    if target != canonical_target:
        raise ValueError(
            "Bundle target does not canonically agree with PoseGridGen source"
        )
    return {**dict(bundle), "bundle_path": bundle_dir.as_posix(), "target": target}


def list_target_bundles(
    *,
    library_root: str | Path | None = None,
    run_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    library = Path(library_root or default_target_library_root())
    selected_id = None
    selected_placement = None
    if run_root is not None:
        try:
            selection = load_run_config_for_run_root(run_root).get("calibration_target")
        except FileNotFoundError:
            selection = None
        if isinstance(selection, Mapping):
            selected_id = selection.get("target_id")
            selected_placement = selection.get("placement")
    if not library.is_dir():
        return []
    result = []
    for child in sorted(
        library.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True
    ):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            bundle = validate_target_bundle(child, library_root=library)
        except (OSError, ValueError) as exc:
            result.append(
                {
                    "target_id": child.name,
                    "valid": False,
                    "error": str(exc),
                    "selected": False,
                }
            )
            continue
        bundle["valid"] = True
        bundle["selected"] = bundle["target_id"] == selected_id
        bundle["selected_placement"] = (
            selected_placement if bundle["selected"] else None
        )
        result.append(bundle)
    return result


def delete_target_bundle(
    *,
    target_id: str,
    library_root: str | Path | None = None,
    run_root: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically remove one library bundle while protecting the active target."""

    target_uuid = _validate_target_id(target_id)
    library = Path(library_root or default_target_library_root())
    bundle_path = library / target_uuid
    bundle = validate_target_bundle(bundle_path, library_root=library)
    if run_root is not None:
        config = load_run_config_for_run_root(run_root)
        selection = config.get("calibration_target")
        if isinstance(selection, Mapping) and selection.get("target_id") == target_uuid:
            raise CalibrationTargetConflict(
                "The calibration target is active for the selected run; select a different "
                "target before deleting it from the library.",
                blockers=[RUN_CONFIG],
            )

    tombstone = library / f".{target_uuid}.{uuid.uuid4().hex}.delete"
    os.replace(bundle_path, tombstone)
    try:
        _remove_path(tombstone)
    except Exception:
        # Restore only when deletion failed before altering the bundle. A partially
        # removed directory remains hidden instead of reappearing as a valid target.
        try:
            validate_target_bundle(
                tombstone,
                library_root=library,
                allow_staging=True,
            )
        except (OSError, ValueError):
            pass
        else:
            if not bundle_path.exists():
                os.replace(tombstone, bundle_path)
        raise
    return {
        "status": "deleted",
        "target_id": target_uuid,
        "display_name": bundle["display_name"],
    }


def replacement_blockers(run_root: str | Path) -> list[str]:
    root = Path(run_root)
    blockers = _raw_capture_replacement_blockers(root)
    bop = root / BOP_DIR
    if bop.exists():
        blockers.append(bop.relative_to(root).as_posix())
    rectified = root / "processed" / "rectified"
    if rectified.exists():
        blockers.append(rectified.relative_to(root).as_posix())
    for path in root.rglob("*"):
        if not path.is_file() or path.name not in _TARGET_DEPENDENT_NAMES:
            continue
        if LIBRARY_DIRECTORY in path.relative_to(root).parts:
            continue
        blockers.append(path.relative_to(root).as_posix())
    return sorted(set(blockers))


def _raw_capture_replacement_blockers(root: Path) -> list[str]:
    """Return acquisition evidence that freezes the target's physical mounting."""

    if not root.is_dir():
        return []
    blockers: set[str] = set()

    def add_if_present(path: Path) -> None:
        if os.path.lexists(path):
            blockers.add(path.relative_to(root).as_posix())

    for artifact in (
        RAW_ROBOT_EE_POSES,
        CAPTURE_EXECUTION_STATUS,
        CAPTURE_EXECUTION_REPORT,
        CAPTURE_EXECUTION_LOGS_DIR,
    ):
        add_if_present(root / artifact)
    raw_pose_stem = Path(RAW_ROBOT_EE_POSES).stem
    for candidate in root.iterdir():
        if candidate.name.startswith(raw_pose_stem):
            add_if_present(candidate)

    excluded_directories = {
        LIBRARY_DIRECTORY,
        CAPTURE_EXECUTION_LOGS_DIR,
        "processed",
    }
    for candidate in root.iterdir():
        if candidate.name in excluded_directories:
            continue
        if not candidate.is_dir() and not candidate.is_symlink():
            continue
        metadata = candidate / FRAME_METADATA_JSONL
        if metadata.is_file():
            add_if_present(metadata)
            continue
        for directory in (RGB_DIR, DEPTH_DIR):
            frame_directory = candidate / directory
            if frame_directory.is_dir() and any(frame_directory.glob("*.png")):
                blockers.add(frame_directory.relative_to(root).as_posix())
                break
    for relative in (FRAME_METADATA_JSONL, RGB_DIR, DEPTH_DIR):
        candidate = root / relative
        if candidate.is_file() or (candidate.is_dir() and any(candidate.glob("*.png"))):
            add_if_present(candidate)
    return sorted(blockers)


def normalize_target_mounting_frame(
    placement_mode: str,
    mounting_frame: str | None,
) -> str:
    """Validate the explicit physical target mounting contract."""

    if placement_mode not in PLACEMENT_MODES:
        raise ValueError(
            "placement mode must be one of: " + ", ".join(sorted(PLACEMENT_MODES))
        )
    if mounting_frame is None:
        raise ValueError("mounting_frame is required")
    frame = str(mounting_frame).strip()
    if frame not in TARGET_MOUNTING_FRAMES:
        raise ValueError(
            "mounting_frame must be one of: "
            + ", ".join(sorted(TARGET_MOUNTING_FRAMES))
        )
    if placement_mode != "unknown" and frame != "template_base":
        raise ValueError(
            f"{placement_mode} is a known template-base placement and requires "
            "mounting_frame=template_base"
        )
    return frame


def validate_configured_target_mounting(
    config: Mapping[str, Any],
    mounting_frame: str,
) -> None:
    """Bind a new explicit target mounting to one homogeneous camera group."""

    capture = config.get("capture")
    raw_sensors = capture.get("sensors") if isinstance(capture, Mapping) else None
    if not isinstance(raw_sensors, list):
        return
    modes = {
        str(sensor.get("mounting_mode", ""))
        for sensor in raw_sensors
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is not False
    }
    modes.discard("")
    if len(modes) > 1:
        raise ValueError(
            "Calibration target selection requires one homogeneous enabled camera "
            "mounting group. Record robot-mounted and static cameras in separate "
            "calibration runs, then combine their promoted profiles later."
        )
    if modes == {"static"} and mounting_frame != "robot_flange":
        raise ValueError(
            "Static-camera calibration requires the target mounted on robot_flange"
        )
    if modes == {"eye_in_hand"} and mounting_frame != "template_base":
        raise ValueError(
            "Eye-in-hand calibration requires the target mounted on template_base"
        )


def validate_run_target_selection(
    run_root: str | Path,
    *,
    require_placement: bool = False,
) -> dict[str, Any]:
    """Cross-check run config, copied bundle, root target, hashes, and placement."""

    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    selection = config.get("calibration_target")
    if not isinstance(selection, Mapping):
        raise FileNotFoundError("Run config has no selected calibration target")
    target_id = _validate_target_id(str(selection.get("target_id", "")))
    expected_relative = Path(LIBRARY_DIRECTORY) / target_id
    if Path(str(selection.get("bundle_path", ""))) != expected_relative:
        raise ValueError("Run calibration-target bundle_path is not canonical")
    bundle = validate_target_bundle(
        root / expected_relative,
        library_root=root / LIBRARY_DIRECTORY,
    )
    records = bundle["files"]
    expected_hashes = {
        "source_sha256": records["source"]["sha256"],
        "spec_sha256": records["target"]["sha256"],
        "pdf_sha256": records["pdf"]["sha256"],
        "configuration_sha256": bundle["configuration_sha256"],
        "geometry_sha256": bundle["geometry_sha256"],
    }
    for key, expected in expected_hashes.items():
        if selection.get(key) != expected:
            raise ValueError(
                f"Run calibration-target {key} does not match copied bundle"
            )
    active = load_calibration_target_spec(root / CALIBRATION_TARGET)
    if active.get("target_id") != target_id:
        raise ValueError("Root calibration target ID does not match run config")
    if active.get("geometry_sha256") != bundle["geometry_sha256"]:
        raise ValueError(
            "Root calibration target geometry does not match copied bundle"
        )
    placement_selection = selection.get("placement")
    if not isinstance(placement_selection, Mapping):
        raise ValueError("Run calibration-target placement selection is invalid")
    mode = str(placement_selection.get("mode", ""))
    explicit_mounting_frame = normalize_target_mounting_frame(
        mode,
        (
            str(placement_selection["mounting_frame"])
            if "mounting_frame" in placement_selection
            else None
        ),
    )
    placement = active.get("placement")
    if mode == "unknown":
        if placement is not None:
            raise ValueError(
                "Unknown target placement must be omitted from root target"
            )
        if require_placement:
            raise ValueError(
                "known_target/compare requires calibration-target placement"
            )
    else:
        if not isinstance(placement, Mapping):
            raise ValueError(f"{mode} requires placement in the root target")
        if placement_selection.get("transform") != placement:
            raise ValueError(
                "Run-config placement transform does not match root target"
            )
    return {
        "target_id": target_id,
        "geometry_sha256": bundle["geometry_sha256"],
        "configuration_sha256": bundle["configuration_sha256"],
        "placement_mode": mode,
        "mounting_frame": explicit_mounting_frame,
        "effective_mounting_frame": explicit_mounting_frame,
        "placement": dict(placement_selection),
        "selection": dict(selection),
        "target": active,
        "bundle_path": expected_relative.as_posix(),
    }


def _placement_from_source(
    source: Mapping[str, Any], mode: str
) -> dict[str, Any] | None:
    if mode == "unknown":
        return None
    if mode == "template_base_identity":
        return {
            "from": "aruco_grid",
            "to": "template_base",
            "rotation_quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "translation_mm": [0.0, 0.0, 0.0],
            "source": "operator_selected_template_base_identity",
        }
    transform = source.get("board_to_base")
    if not isinstance(transform, Mapping):
        raise ValueError(
            "posegridgen_board_to_base placement requires source board_to_base"
        )
    matrix = np.asarray(transform.get("board_to_base_matrix"), dtype=float)
    translation = np.asarray(transform.get("translation_m"), dtype=float)
    quaternion = np.asarray(transform.get("quaternion_xyzw"), dtype=float)
    if (
        matrix.shape != (4, 4)
        or translation.shape != (3,)
        or quaternion.shape != (4,)
        or not np.isfinite(matrix).all()
        or not np.isfinite(translation).all()
        or not np.isfinite(quaternion).all()
    ):
        raise ValueError("PoseGridGen board_to_base transform is malformed")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise ValueError("PoseGridGen board_to_base matrix is not homogeneous")
    if not np.allclose(matrix[:3, 3], translation, atol=1e-9):
        raise ValueError("PoseGridGen translation does not match its matrix")
    norm = float(np.dot(quaternion, quaternion))
    if not math.isclose(norm, 1.0, abs_tol=1e-8):
        raise ValueError("PoseGridGen quaternion is not normalized")
    if not np.allclose(
        matrix[:3, :3], Rotation.from_quat(quaternion).as_matrix(), atol=1e-8
    ):
        raise ValueError("PoseGridGen quaternion does not match its matrix")
    x, y, z, w = quaternion.tolist()
    return {
        "from": "aruco_grid",
        "to": "template_base",
        "rotation_quaternion_wxyz": [w, x, y, z],
        "translation_mm": (translation * 1000.0).tolist(),
        "source": "posegridgen_board_to_base",
        "source_base_frame_interpretation": "template_base",
    }


def validate_bundle_placement(
    bundle: Mapping[str, Any],
    mode: str,
    *,
    mounting_frame: str,
) -> None:
    normalize_target_mounting_frame(mode, mounting_frame)
    bundle_path = Path(str(bundle.get("bundle_path", "")))
    with open(bundle_path / POSEGRIDGEN_SOURCE, "r") as handle:
        source = json.load(handle)
    if not isinstance(source, Mapping):
        raise ValueError("PoseGridGen source manifest must be an object")
    _placement_from_source(source, mode)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _promote_paths(promotions: list[tuple[Path, Path]]) -> None:
    backups = [
        target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
        for _source, target in promotions
    ]
    moved_existing: list[int] = []
    promoted: list[int] = []
    try:
        for index, ((_source, target), backup) in enumerate(
            zip(promotions, backups, strict=True)
        ):
            if target.exists():
                os.replace(target, backup)
                moved_existing.append(index)
        for index, (source, target) in enumerate(promotions):
            os.replace(source, target)
            promoted.append(index)
    except Exception:
        for index in reversed(promoted):
            source, target = promotions[index]
            if target.exists() and not source.exists():
                os.replace(target, source)
        for index in reversed(moved_existing):
            _source, target = promotions[index]
            backup = backups[index]
            if backup.exists():
                os.replace(backup, target)
        raise
    else:
        for backup in backups:
            if backup.exists():
                _remove_path(backup)


def select_target_bundle(
    *,
    run_root: str | Path,
    target_id: str,
    placement_mode: str,
    mounting_frame: str,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with run_config_lock(root):
        return _select_target_bundle_locked(
            run_root=root,
            target_id=target_id,
            placement_mode=placement_mode,
            mounting_frame=mounting_frame,
            library_root=library_root,
        )


def _select_target_bundle_locked(
    *,
    run_root: str | Path,
    target_id: str,
    placement_mode: str,
    mounting_frame: str,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(run_root)
    target_uuid = _validate_target_id(target_id)
    normalized_mounting_frame = normalize_target_mounting_frame(
        placement_mode, mounting_frame
    )
    config = load_run_config_for_run_root(root)
    validate_configured_target_mounting(config, normalized_mounting_frame)
    library = Path(library_root or default_target_library_root())
    bundle = validate_target_bundle(library / target_uuid, library_root=library)
    validate_bundle_placement(
        bundle,
        placement_mode,
        mounting_frame=normalized_mounting_frame,
    )
    records = bundle["files"]
    selection_placement = {
        "mode": placement_mode,
        "mounting_frame": normalized_mounting_frame,
    }
    existing = config.get("calibration_target")
    if isinstance(existing, Mapping):
        existing_mode = (
            existing.get("placement", {}).get("mode")
            if isinstance(existing.get("placement"), Mapping)
            else None
        )
        existing_mounting_frame = (
            existing.get("placement", {}).get("mounting_frame")
            if isinstance(existing.get("placement"), Mapping)
            else None
        )
        if (
            existing.get("target_id") == target_uuid
            and existing_mode == placement_mode
            and existing_mounting_frame == normalized_mounting_frame
        ):
            evidence = validate_run_target_selection(root)
            return {
                "status": "unchanged",
                "run_root": root.as_posix(),
                "selection": dict(existing),
                "evidence": evidence,
                "blockers": [],
            }
    blockers = replacement_blockers(root)
    if blockers:
        raise CalibrationTargetConflict(
            "The calibration target and its mounting must be bound before raw "
            "acquisition or target-dependent evidence exists; create a new run.",
            blockers=blockers,
        )

    source = bundle["bundle_path"]
    with open(Path(source) / POSEGRIDGEN_SOURCE, "r") as handle:
        posegridgen_source = json.load(handle)
    placement = _placement_from_source(posegridgen_source, placement_mode)
    root_target = dict(bundle["target"])
    root_target.pop("placement", None)
    if placement is not None:
        root_target["placement"] = placement
        selection_placement["transform"] = placement

    bundle_relative = Path(LIBRARY_DIRECTORY) / target_uuid
    selection = {
        "target_id": target_uuid,
        "bundle_path": bundle_relative.as_posix(),
        "source_sha256": records["source"]["sha256"],
        "spec_sha256": records["target"]["sha256"],
        "pdf_sha256": records["pdf"]["sha256"],
        "configuration_sha256": bundle["configuration_sha256"],
        "geometry_sha256": bundle["geometry_sha256"],
        "placement": selection_placement,
    }
    updated_config = dict(config)
    updated_config.pop("warnings", None)
    updated_config["calibration_target"] = selection
    validate_run_config(updated_config)
    manifest = load_or_create_run_manifest(root)
    set_manifest_artifact(
        manifest, CALIBRATION_TARGET, root / CALIBRATION_TARGET, run_root=root
    )
    upsert_stage(
        manifest,
        name="calibration_target_select",
        status="succeeded",
        artifacts={
            CALIBRATION_TARGET: root / CALIBRATION_TARGET,
            "calibration_target_bundle": root / bundle_relative,
        },
        run_root=root,
        message=(
            f"Selected immutable calibration target {target_uuid} ({placement_mode}, "
            f"mounting={normalized_mounting_frame})."
        ),
    )
    manifest["updated_at"] = utc_now_iso()

    run_bundle_parent = root / LIBRARY_DIRECTORY
    run_bundle_parent.mkdir(parents=True, exist_ok=True)
    staging_bundle = run_bundle_parent / f".{target_uuid}.{uuid.uuid4().hex}.tmp"
    staging_target = root / f".{CALIBRATION_TARGET}.{uuid.uuid4().hex}.tmp"
    staging_config = root / f".{RUN_CONFIG}.{uuid.uuid4().hex}.tmp"
    staging_manifest = root / f".{DATASET_MANIFEST}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copytree(source, staging_bundle, symlinks=False)
        atomic_write_json(staging_target, root_target)
        atomic_write_json(staging_config, updated_config)
        atomic_write_json(staging_manifest, manifest)
        _promote_paths(
            [
                (staging_bundle, root / bundle_relative),
                (staging_target, root / CALIBRATION_TARGET),
                (staging_config, root / RUN_CONFIG),
                (staging_manifest, root / DATASET_MANIFEST),
            ]
        )
    finally:
        for path in (staging_bundle, staging_target, staging_config, staging_manifest):
            if path.exists():
                _remove_path(path)
    return {
        "status": "selected",
        "run_root": root.as_posix(),
        "selection": selection,
        "blockers": [],
    }
