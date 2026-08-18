"""Inventory and safely relocate run folders between approved storage roots."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from posetestbot.io.artifacts import (
    BOP_DIR,
    BOP_EXPORT_MANIFEST,
    CALIBRATION_PROFILES,
    CALIBRATION_VALIDATION_REPORT,
    CAPTURE_EXECUTION_LOGS_DIR,
    CAPTURE_EXECUTION_REPORT,
    DATASET_MANIFEST,
    OBJECT_INSTANCES,
    POSE_TEMPLATE_SELECTION,
    PROCESSED_DIR,
    RAW_ROBOT_EE_POSES,
    RUN_CONFIG,
    SYNC_QUALITY_REPORT,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.sensors.registry import sensor_folder_name


INVENTORY_SCHEMA_VERSION = "run_folder_inventory.v1"
MAINTENANCE_SCHEMA_VERSION = "run_folder_maintenance.v1"
LOCATION_SCHEMA_VERSION = "run_folder_location.v1"
LOCATION_FILE = ".posetestbot_run_location.json"
INVENTORY_FILENAME = "run_folder_inventory.json"
ROOT_LOCK_FILE = ".posetestbot_run_folders.lock"
MOVE_STAGING_PREFIX = ".posetestbot_run_move_"
TRANSACTION_SCHEMA_VERSION = "run_folder_transaction.v1"
TRANSACTION_PREFIX = ".posetestbot_run_folder_transaction_"
TRANSACTION_PATTERN = re.compile(
    rf"^{re.escape(TRANSACTION_PREFIX)}(?P<id>[0-9a-f]{{32}})\.json$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MOUNTINFO_ESCAPE_PATTERN = re.compile(r"\\([0-7]{3})")
MAX_SUMMARY_JSON_BYTES = 8 * 1024 * 1024
MAX_SCAN_ERRORS = 100
MAX_SENSOR_SUMMARIES = 100
MAX_OBJECT_NAMES = 100


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _normalized_roots(allowed_roots: Iterable[str | Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in allowed_roots:
        root = Path(value).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _lexical_absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _decode_mountinfo_path(value: str) -> Path:
    """Decode Linux mountinfo's octal path escapes without shell interpretation."""

    decoded = MOUNTINFO_ESCAPE_PATTERN.sub(
        lambda match: chr(int(match.group(1), 8)),
        value,
    )
    return _lexical_absolute(decoded)


def _linux_mount_entries() -> tuple[tuple[int, Path], ...]:
    """Return mount IDs and mountpoints from the current Linux namespace.

    Linux ``mountinfo`` identifies bind mounts even when their device number is
    the same as the surrounding tree, unlike a device-only traversal guard.
    """

    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_text(
            encoding="utf-8",
            errors="surrogateescape",
        ).splitlines()
    except OSError as exc:
        if sys.platform == "linux":
            raise RuntimeError("Cannot inspect Linux mount boundaries") from exc
        return ()
    mount_entries: list[tuple[int, Path]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or "-" not in fields:
            raise RuntimeError("Linux mount boundary metadata is malformed")
        try:
            mount_id = int(fields[0])
        except ValueError as exc:
            raise RuntimeError("Linux mount boundary metadata is malformed") from exc
        mount_entries.append((mount_id, _decode_mountinfo_path(fields[4])))
    return tuple(mount_entries)


def _linux_mount_points() -> tuple[Path, ...]:
    return tuple(path for _mount_id, path in _linux_mount_entries())


def _containing_mount_id(path: Path) -> int | None:
    candidate = _lexical_absolute(path)
    matches: list[tuple[int, int, int]] = []
    for order, (mount_id, mount_point) in enumerate(_linux_mount_entries()):
        try:
            candidate.relative_to(mount_point)
        except ValueError:
            continue
        matches.append((len(mount_point.parts), order, mount_id))
    return max(matches)[2] if matches else None


def _same_filesystem_mount(source: Path, destination_root: Path) -> bool:
    if source.lstat().st_dev != destination_root.lstat().st_dev:
        return False
    if sys.platform != "linux":
        return True
    source_mount = _containing_mount_id(source)
    destination_mount = _containing_mount_id(destination_root)
    if source_mount is None or destination_mount is None:
        raise RuntimeError("Cannot identify run-folder filesystem mounts")
    return source_mount == destination_mount


def _nested_mount_points(path: Path) -> tuple[Path, ...]:
    root = _lexical_absolute(path)
    nested = []
    for mount_point in _linux_mount_points():
        if mount_point == root:
            continue
        try:
            mount_point.relative_to(root)
        except ValueError:
            continue
        nested.append(mount_point)
    return tuple(sorted(set(nested), key=lambda item: item.as_posix()))


def _assert_no_nested_mounts(path: Path) -> None:
    nested = _nested_mount_points(path)
    if nested:
        raise ValueError(
            "Run tree contains a nested mountpoint: "
            + ", ".join(item.as_posix() for item in nested)
        )


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _has_run_marker(path: Path) -> bool:
    return any(_regular_file(path / name) for name in (RUN_CONFIG, DATASET_MANIFEST))


def run_identity(path: str | Path) -> dict[str, int]:
    item = Path(path)
    metadata = item.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Run folder must be a real directory: {item}")
    return {"device": int(metadata.st_dev), "inode": int(metadata.st_ino)}


def validate_expected_identity(
    path: str | Path,
    expected: Mapping[str, Any],
) -> dict[str, int]:
    if not isinstance(expected, Mapping):
        raise ValueError("expected_identity must be an object")
    device = expected.get("device")
    inode = expected.get("inode")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or device < 0
        or inode <= 0
    ):
        raise ValueError("expected_identity requires nonnegative device and positive inode")
    actual = run_identity(path)
    if actual != {"device": device, "inode": inode}:
        raise RuntimeError(
            "Run folder identity changed; refresh the inventory before retrying"
        )
    return actual


def resolve_direct_run_folder(
    value: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
    require_marker: bool = True,
) -> Path:
    """Resolve one existing, non-symlink direct child of an approved root."""

    roots = _normalized_roots(allowed_roots)
    path = _lexical_absolute(value)
    if path.is_symlink():
        raise ValueError("Run folder must not be a symbolic link")
    if path.parent not in roots:
        raise ValueError("Run folder must be a direct child of an allowed run root")
    if not _real_directory(path):
        raise FileNotFoundError(f"Run folder does not exist: {path}")
    resolved = path.resolve()
    if resolved != path or resolved.parent not in roots:
        raise ValueError("Run folder resolution changed unexpectedly")
    if require_marker and not _has_run_marker(path):
        raise ValueError(
            f"Run folder must contain {RUN_CONFIG} or {DATASET_MANIFEST}"
        )
    return path


def resolve_destination_root(
    value: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
) -> Path:
    roots = _normalized_roots(allowed_roots)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("destination_root must be an absolute allowed root")
    resolved = path.resolve()
    if resolved not in roots:
        raise ValueError("destination_root must exactly match an allowed run root")
    if not _real_directory(resolved):
        raise FileNotFoundError(f"Destination root does not exist: {resolved}")
    return resolved


def discover_run_folders(
    allowed_roots: Iterable[str | Path],
) -> list[tuple[Path, Path]]:
    """Return ``(storage_root, run)`` pairs without following directory links."""

    found: list[tuple[Path, Path]] = []
    for root in _normalized_roots(allowed_roots):
        if not _real_directory(root):
            continue
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if child.name.startswith(MOVE_STAGING_PREFIX):
                continue
            try:
                if child.is_symlink() or not _real_directory(child):
                    continue
                if not _has_run_marker(child):
                    continue
                resolved = child.resolve()
                if resolved.parent != root or resolved != child:
                    continue
                found.append((root, child))
            except OSError:
                continue
    return found


def run_folder_transaction_fingerprint(
    allowed_roots: Iterable[str | Path],
) -> str:
    """Fingerprint exact durable transaction journals without following links."""

    digest = hashlib.sha256()
    for root in _normalized_roots(allowed_roots):
        encoded_root = root.as_posix().encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded_root).to_bytes(8, "big"))
        digest.update(encoded_root)
        digest.update(b"\0")
        if not _real_directory(root):
            digest.update(b"missing\n")
            continue
        candidates = sorted(root.iterdir(), key=lambda item: item.name)
        for candidate in candidates:
            if TRANSACTION_PATTERN.fullmatch(candidate.name) is None:
                continue
            metadata = candidate.lstat()
            encoded_name = candidate.name.encode(
                "utf-8",
                errors="surrogateescape",
            )
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            for value in (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ):
                digest.update(int(value).to_bytes(16, "big", signed=False))
            digest.update(b"\n")
    return digest.hexdigest()


def run_root_identity_snapshot(
    allowed_roots: Iterable[str | Path],
) -> dict[str, dict[str, int] | None]:
    """Capture the exact directory inode behind each configured root path."""

    snapshot: dict[str, dict[str, int] | None] = {}
    for root in _normalized_roots(allowed_roots):
        try:
            snapshot[root.as_posix()] = (
                run_identity(root) if _real_directory(root) else None
            )
        except OSError:
            snapshot[root.as_posix()] = None
    return snapshot


def _allocated_bytes(metadata: os.stat_result) -> int:
    blocks = getattr(metadata, "st_blocks", None)
    return int(blocks * 512) if isinstance(blocks, int) else int(metadata.st_size)


def _empty_stats() -> dict[str, int]:
    return {
        "size_bytes": 0,
        "allocated_bytes": 0,
        "file_count": 0,
        "directory_count": 0,
        "symlink_count": 0,
    }


def _merge_stats(target: dict[str, int], source: Mapping[str, int]) -> None:
    for name in target:
        target[name] += int(source.get(name, 0))


def _scan_entry(
    path: Path,
    *,
    relative_root: Path,
    expected_device: int,
) -> tuple[dict[str, int], list[str]]:
    """Measure a tree using lstat/scandir and never traverse a symbolic link."""

    totals = _empty_stats()
    errors: list[str] = []
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            metadata = current.lstat()
        except OSError as exc:
            if len(errors) < MAX_SCAN_ERRORS:
                try:
                    label = current.relative_to(relative_root).as_posix()
                except ValueError:
                    label = current.as_posix()
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        if metadata.st_dev != expected_device:
            if len(errors) < MAX_SCAN_ERRORS:
                try:
                    label = current.relative_to(relative_root).as_posix()
                except ValueError:
                    label = current.as_posix()
                errors.append(f"{label}: entry crosses a filesystem boundary")
            continue
        totals["allocated_bytes"] += _allocated_bytes(metadata)
        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            totals["symlink_count"] += 1
            continue
        if stat.S_ISREG(mode):
            totals["size_bytes"] += int(metadata.st_size)
            totals["file_count"] += 1
            continue
        if not stat.S_ISDIR(mode):
            totals["file_count"] += 1
            continue
        totals["directory_count"] += 1
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(current, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise RuntimeError("directory changed while it was being scanned")
            with os.scandir(descriptor) as entries:
                stack.extend(current / entry.name for entry in entries)
        except OSError as exc:
            if len(errors) < MAX_SCAN_ERRORS:
                try:
                    label = current.relative_to(relative_root).as_posix()
                except ValueError:
                    label = current.as_posix()
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
        except RuntimeError as exc:
            if len(errors) < MAX_SCAN_ERRORS:
                try:
                    label = current.relative_to(relative_root).as_posix()
                except ValueError:
                    label = current.as_posix()
                errors.append(f"{label}: {exc}")
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return totals, errors


def _breakdown_group(name: str) -> str:
    lowered = name.lower()
    if lowered == PROCESSED_DIR:
        return "processed"
    if lowered == BOP_DIR:
        return "bop"
    if lowered == CAPTURE_EXECUTION_LOGS_DIR:
        return "capture_logs"
    if lowered.startswith(("realsense_", "luxonis_", "oak_", "zed_")):
        return "raw_capture"
    if lowered == RAW_ROBOT_EE_POSES:
        return "raw_capture"
    return "other"


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_SUMMARY_JSON_BYTES
        ):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return value if isinstance(value, Mapping) else None


def _config_summary(root: Path) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    try:
        config = load_run_config_for_run_root(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return (
            {
                "valid": False,
                "error": str(exc),
                "run_name": None,
                "sequence": None,
                "plan_only": None,
            },
            _read_json_object(root / RUN_CONFIG),
        )
    pipeline = config.get("pipeline")
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    return (
        {
            "valid": True,
            "error": None,
            "run_name": (
                str(config["run_name"]) if config.get("run_name") is not None else None
            ),
            "sequence": (
                str(pipeline["sequence_id"])
                if pipeline.get("sequence_id") is not None
                else None
            ),
            "plan_only": (
                pipeline.get("plan_only")
                if isinstance(pipeline.get("plan_only"), bool)
                else None
            ),
        },
        config,
    )


def _sensor_summaries(
    config: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], int, int]:
    if not isinstance(config, Mapping):
        return [], 0, 0
    capture = config.get("capture")
    if not isinstance(capture, Mapping) or not isinstance(capture.get("sensors"), list):
        return [], 0, 0
    sensors = []
    sensor_count = 0
    enabled_sensor_count = 0
    for item in capture["sensors"]:
        if not isinstance(item, Mapping):
            continue
        sensor_count += 1
        enabled = item.get("enabled", True) is True
        enabled_sensor_count += int(enabled)
        sensor_type = str(item.get("sensor_type") or "")
        device_id = str(item.get("device_id") or "")
        name = str(
            item.get("operator_alias")
            or item.get("display_name")
            or (f"{sensor_type}:{device_id}" if sensor_type and device_id else "")
            or "Unnamed sensor"
        )
        if len(sensors) < MAX_SENSOR_SUMMARIES:
            sensors.append(
                {
                    "sensor_type": sensor_type,
                    "device_id": device_id,
                    "name": name,
                    "mounting_mode": str(item.get("mounting_mode") or ""),
                    "enabled": enabled,
                }
            )
    return sensors, sensor_count, enabled_sensor_count


def _object_summary(root: Path) -> tuple[int, list[str], str | None]:
    value = _read_json_object(root / OBJECT_INSTANCES)
    if value is None:
        value = _read_json_object(root / POSE_TEMPLATE_SELECTION)
    if value is None:
        return 0, [], None
    raw_instances = value.get("instances")
    instances = raw_instances if isinstance(raw_instances, list) else []
    names: list[str] = []
    for item in instances:
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("name"), str)
            and str(item["name"]).strip()
            and str(item["name"]) not in names
            and len(names) < MAX_OBJECT_NAMES
        ):
            names.append(str(item["name"]))
    template_uuid = value.get("template_uuid")
    return (
        len(instances),
        names,
        str(template_uuid) if isinstance(template_uuid, str) else None,
    )


def _contains_real_entry(path: Path) -> bool:
    if not _real_directory(path):
        return False
    try:
        return any(not item.is_symlink() for item in path.iterdir())
    except OSError:
        return False


def _evidence_summary(root: Path, sensors: list[dict[str, Any]]) -> dict[str, bool]:
    raw_capture = _regular_file(root / RAW_ROBOT_EE_POSES) or _regular_file(
        root / CAPTURE_EXECUTION_REPORT
    )
    if not raw_capture:
        for sensor in sensors:
            sensor_type = sensor["sensor_type"]
            device_id = sensor["device_id"]
            if sensor_type and device_id:
                try:
                    folder = root / sensor_folder_name(sensor_type, device_id)
                except (KeyError, ValueError):
                    continue
                if _contains_real_entry(folder):
                    raw_capture = True
                    break
    return {
        "raw_capture": raw_capture,
        "synchronized": (
            _contains_real_entry(root / PROCESSED_DIR / SYNCHRONIZED_DIR)
            or _regular_file(root / SYNC_QUALITY_REPORT)
        ),
        "calibration": (
            _regular_file(root / CALIBRATION_PROFILES)
            or _regular_file(root / CALIBRATION_VALIDATION_REPORT)
        ),
        "bop_export": _regular_file(root / BOP_DIR / BOP_EXPORT_MANIFEST),
        "bop_evaluation": _contains_real_entry(
            root / PROCESSED_DIR / "bop_evaluation"
        ),
    }


def _load_location(root: Path, *, strict: bool = False) -> dict[str, Any] | None:
    path = root / LOCATION_FILE
    if os.path.lexists(path) and not _regular_file(path):
        if strict:
            raise ValueError(
                f"Run location metadata must be a regular file: {path}"
            )
        return None
    value = _read_json_object(path)
    if value is None or value.get("schema_version") != LOCATION_SCHEMA_VERSION:
        if strict and os.path.lexists(path):
            raise ValueError(f"Run location metadata is invalid: {path}")
        return None
    original = value.get("original_path")
    current = value.get("current_path")
    transaction_id = value.get("transaction_id")
    aliases = value.get("aliases")
    history = value.get("history")
    if (
        not isinstance(original, str)
        or (current is not None and not isinstance(current, str))
        or (transaction_id is not None and not isinstance(transaction_id, str))
        or not isinstance(aliases, list)
        or not all(isinstance(item, str) for item in aliases)
        or not isinstance(history, list)
    ):
        if strict:
            raise ValueError(f"Run location metadata is invalid: {path}")
        return None
    return {
        "schema_version": LOCATION_SCHEMA_VERSION,
        "original_path": original,
        "current_path": current,
        "transaction_id": transaction_id,
        "aliases": list(dict.fromkeys(aliases)),
        "history": [dict(item) for item in history if isinstance(item, Mapping)],
    }


def _public_location(root: Path) -> dict[str, Any] | None:
    value = _load_location(root)
    if value is None:
        return None
    return {
        "original_path": value["original_path"],
        "aliases": value["aliases"],
        "history_count": len(value["history"]),
    }


def inspect_run_folder(storage_root: Path, root: Path) -> dict[str, Any]:
    totals = _empty_stats()
    errors: list[str] = []
    breakdown_totals: dict[str, dict[str, int]] = {}
    initial_metadata = root.lstat()
    if not stat.S_ISDIR(initial_metadata.st_mode):
        raise ValueError(f"Run folder changed before inventory scan: {root}")
    identity = {
        "device": int(initial_metadata.st_dev),
        "inode": int(initial_metadata.st_ino),
    }
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != initial_metadata.st_dev
            or opened.st_ino != initial_metadata.st_ino
        ):
            raise RuntimeError("run folder changed while inventory scan opened it")
        with os.scandir(descriptor) as entries:
            children = sorted(
                (root / entry.name for entry in entries),
                key=lambda item: item.name,
            )
    except (OSError, RuntimeError) as exc:
        children = []
        errors.append(f".: {type(exc).__name__}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    for child in children:
        if child.name == ROOT_LOCK_FILE or child.name.startswith(MOVE_STAGING_PREFIX):
            continue
        measured, child_errors = _scan_entry(
            child,
            relative_root=root,
            expected_device=int(initial_metadata.st_dev),
        )
        _merge_stats(totals, measured)
        errors.extend(child_errors[: max(0, MAX_SCAN_ERRORS - len(errors))])
        group = _breakdown_group(child.name)
        grouped = breakdown_totals.setdefault(group, _empty_stats())
        _merge_stats(grouped, measured)

    config_summary, config = _config_summary(root)
    sensors, sensor_count, enabled_sensor_count = _sensor_summaries(config)
    capture = config.get("capture") if isinstance(config, Mapping) else None
    capture = capture if isinstance(capture, Mapping) else {}
    synchronization = capture.get("synchronization")
    synchronization = synchronization if isinstance(synchronization, Mapping) else {}
    object_count, object_names, template_uuid = _object_summary(root)
    modified_candidates = [root]
    if _regular_file(root / RUN_CONFIG):
        modified_candidates.append(root / RUN_CONFIG)
    try:
        modified_timestamp = max(item.stat().st_mtime for item in modified_candidates)
        modified_at = (
            datetime.fromtimestamp(modified_timestamp, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except OSError:
        modified_at = utc_now_iso().replace("+00:00", "Z")
    return {
        "path": root.as_posix(),
        "name": root.name,
        "root": storage_root.as_posix(),
        "modified_at": modified_at,
        "size_bytes": totals["size_bytes"],
        "allocated_bytes": totals["allocated_bytes"],
        "file_count": totals["file_count"],
        "directory_count": totals["directory_count"],
        "symlink_count": totals["symlink_count"],
        "scan_complete": not errors,
        "scan_error_count": len(errors),
        "scan_errors": errors,
        "identity": identity,
        "config": config_summary,
        "contents": {
            "dataset_mode": (
                str(config["dataset_mode"])
                if isinstance(config, Mapping) and config.get("dataset_mode") is not None
                else None
            ),
            "resolution": (
                str(capture["resolution"])
                if capture.get("resolution") is not None
                else None
            ),
            "fps": (
                capture.get("fps")
                if isinstance(capture.get("fps"), int)
                and not isinstance(capture.get("fps"), bool)
                else None
            ),
            "synchronization_mode": (
                str(synchronization["mode"])
                if synchronization.get("mode") is not None
                else None
            ),
            "sensor_count": sensor_count,
            "enabled_sensor_count": enabled_sensor_count,
            "sensors": sensors,
            "object_count": object_count,
            "object_names": object_names,
            "template_uuid": template_uuid,
            "evidence": _evidence_summary(root, sensors),
        },
        "breakdown": {
            name: {
                "size_bytes": value["size_bytes"],
                "allocated_bytes": value["allocated_bytes"],
                "file_count": value["file_count"],
            }
            for name, value in sorted(breakdown_totals.items())
        },
        "relocation": _public_location(root),
    }


def build_run_folder_inventory(
    allowed_roots: Iterable[str | Path],
    *,
    maintenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    roots = _normalized_roots(allowed_roots)
    root_identities = run_root_identity_snapshot(roots)
    runs = []
    for storage_root, run in discover_run_folders(roots):
        try:
            runs.append(inspect_run_folder(storage_root, run))
        except (OSError, ValueError):
            # A run may be relocated or removed between discovery and scan.
            # Never follow its replacement, and let the next refresh discover
            # the new stable direct child.
            continue
    runs.sort(key=lambda item: (-_iso_sort_value(item["modified_at"]), item["path"]))
    if run_root_identity_snapshot(roots) != root_identities:
        raise RuntimeError(
            "An allowed run root changed while its inventory was being built"
        )
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "run_roots": [root.as_posix() for root in roots],
        "root_identities": root_identities,
        "runs": runs,
        "maintenance": dict(
            maintenance
            or {
                "schema_version": MAINTENANCE_SCHEMA_VERSION,
                "journal_fingerprint": run_folder_transaction_fingerprint(roots),
                "recovered_count": 0,
                "transactions": [],
                "unresolved_count": 0,
                "unresolved": [],
            }
        ),
    }


def _iso_sort_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def write_run_folder_inventory(
    cache_path: str | Path,
    *,
    allowed_roots: Iterable[str | Path],
) -> dict[str, Any]:
    roots = _normalized_roots(allowed_roots)
    maintenance = recover_run_folder_transactions(roots)
    value = build_run_folder_inventory(roots, maintenance=maintenance)
    atomic_write_json(cache_path, value)
    return value


def load_run_folder_inventory(cache_path: str | Path) -> dict[str, Any] | None:
    path = Path(cache_path)
    value = _read_json_object(path)
    run_roots = value.get("run_roots") if value is not None else None
    root_identities = value.get("root_identities") if value is not None else None
    if (
        value is None
        or value.get("schema_version") != INVENTORY_SCHEMA_VERSION
        or not isinstance(value.get("generated_at"), str)
        or not isinstance(value.get("runs"), list)
        or not isinstance(run_roots, list)
        or not all(isinstance(item, str) for item in run_roots)
        or len(run_roots) != len(set(run_roots))
        or not isinstance(root_identities, Mapping)
        or set(root_identities) != set(run_roots)
    ):
        return None
    try:
        for root in run_roots:
            identity = root_identities[root]
            if identity is not None:
                _validated_identity_value(
                    identity,
                    label=f"Inventory root identity for {root}",
                )
    except ValueError:
        return None
    result = dict(value)
    maintenance = result.get("maintenance")
    if maintenance is None:
        return None
    if (
        not isinstance(maintenance, Mapping)
        or maintenance.get("schema_version") != MAINTENANCE_SCHEMA_VERSION
        or not isinstance(maintenance.get("journal_fingerprint"), str)
        or SHA256_PATTERN.fullmatch(maintenance["journal_fingerprint"]) is None
        or isinstance(maintenance.get("recovered_count"), bool)
        or not isinstance(maintenance.get("recovered_count"), int)
        or not isinstance(maintenance.get("transactions"), list)
        or isinstance(maintenance.get("unresolved_count"), bool)
        or not isinstance(maintenance.get("unresolved_count"), int)
        or not isinstance(maintenance.get("unresolved"), list)
    ):
        return None
    else:
        transactions = maintenance["transactions"]
        unresolved = maintenance["unresolved"]
        if (
            maintenance["recovered_count"] != len(transactions)
            or maintenance["unresolved_count"] != len(unresolved)
            or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("transaction_id"), str)
                or item.get("operation") not in {"move", "delete"}
                or item.get("action")
                not in {"rolled_back_move", "completed_move", "resumed_delete"}
                for item in transactions
            )
            or any(
                not isinstance(item, Mapping)
                or (
                    item.get("transaction_id") is not None
                    and not isinstance(item.get("transaction_id"), str)
                )
                or item.get("operation") not in {None, "move", "delete"}
                or not isinstance(item.get("error"), str)
                or (
                    item.get("remnant_bytes") is not None
                    and (
                        isinstance(item.get("remnant_bytes"), bool)
                        or not isinstance(item.get("remnant_bytes"), int)
                        or item["remnant_bytes"] < 0
                    )
                )
                for item in unresolved
            )
        ):
            return None
    return result


@contextmanager
def _storage_locks(roots: Iterable[Path]):
    handles = []
    try:
        for root in sorted(set(roots), key=lambda item: item.as_posix()):
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(root / ROOT_LOCK_FILE, flags, 0o600)
            handle = os.fdopen(descriptor, "a+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_rename(source: Path, destination: Path) -> None:
    os.rename(source, destination)
    _fsync_directory(destination.parent)
    if source.parent != destination.parent:
        _fsync_directory(source.parent)


def _quarantine_run(
    run: Path,
    *,
    expected_identity: Mapping[str, Any],
    quarantine: Path | None = None,
) -> Path:
    """Atomically isolate a run, then prove the renamed inode is the selected one."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(run, flags)
    quarantine = quarantine or (
        run.parent / f"{MOVE_STAGING_PREFIX}source_{uuid.uuid4().hex}"
    )
    renamed = False
    try:
        opened = os.fstat(descriptor)
        opened_identity = {
            "device": int(opened.st_dev),
            "inode": int(opened.st_ino),
        }
        selected_identity = {
            "device": int(expected_identity["device"]),
            "inode": int(expected_identity["inode"]),
        }
        if not stat.S_ISDIR(opened.st_mode) or opened_identity != selected_identity:
            raise RuntimeError(
                "Run folder identity changed; refresh the inventory before retrying"
            )
        _durable_rename(run, quarantine)
        renamed = True
        isolated = quarantine.lstat()
        isolated_identity = {
            "device": int(isolated.st_dev),
            "inode": int(isolated.st_ino),
        }
        if (
            not stat.S_ISDIR(isolated.st_mode)
            or isolated_identity != opened_identity
        ):
            raise RuntimeError(
                "Run folder changed while it was being isolated; no mutation was applied"
            )
        return quarantine
    except Exception as exc:
        if renamed and os.path.lexists(quarantine):
            if os.path.lexists(run):
                raise RuntimeError(
                    "Run folder isolation failed and its original path was occupied; "
                    f"preserved isolated data at {quarantine}"
                ) from exc
            try:
                _durable_rename(quarantine, run)
            except OSError as rollback_exc:
                raise RuntimeError(
                    "Run folder isolation failed and rollback could not restore "
                    f"{run}; preserved data may remain at {quarantine}"
                ) from rollback_exc
        raise
    finally:
        os.close(descriptor)


def _restore_quarantined_run(quarantine: Path, run: Path) -> None:
    if not os.path.lexists(quarantine):
        return
    if os.path.lexists(run):
        raise RuntimeError(
            "Cannot restore the isolated run because its original path is occupied; "
            f"preserved data remains at {quarantine}"
        )
    _durable_rename(quarantine, run)


def _link_target_path(alias: Path, link_value: str) -> Path:
    target = Path(link_value)
    if not target.is_absolute():
        target = alias.parent / target
    return _lexical_absolute(target)


def _recorded_aliases(
    target: Path,
    *,
    allowed_roots: Iterable[str | Path],
    location: Mapping[str, Any] | None,
) -> list[tuple[Path, str]]:
    """Validate aliases named by the run-owned location record.

    Never infer ownership from an arbitrary symlink merely resolving to the
    run. Only aliases explicitly recorded by PoseTestBot are eligible for
    replacement or cleanup.
    """

    if location is None:
        return []
    roots = set(_normalized_roots(allowed_roots))
    aliases: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for value in location["aliases"]:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Recorded compatibility aliases must be absolute paths")
        alias = _lexical_absolute(candidate)
        if alias in seen:
            continue
        seen.add(alias)
        if alias == target or alias.parent not in roots:
            raise ValueError(
                f"Recorded compatibility alias is outside an allowed direct-child "
                f"path: {alias}"
            )
        if not os.path.lexists(alias):
            continue
        if not alias.is_symlink():
            raise RuntimeError(f"Recorded compatibility alias was replaced: {alias}")
        try:
            expected_link = os.readlink(alias)
            resolved = alias.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                f"Recorded compatibility alias cannot be verified: {alias}"
            ) from exc
        if _link_target_path(alias, expected_link) != target or resolved != target:
            raise RuntimeError(
                f"Recorded compatibility alias target changed: {alias}"
            )
        aliases.append((alias, expected_link))
    return aliases


def _unlink_verified_alias(
    alias: Path,
    expected_link: str,
    link_target: Path,
    *,
    resolved_target: Path | None = None,
) -> None:
    resolved_target = resolved_target or link_target
    if not alias.is_symlink():
        raise RuntimeError(f"Compatibility alias changed before cleanup: {alias}")
    if (
        os.readlink(alias) != expected_link
        or _link_target_path(alias, expected_link) != link_target
        or alias.resolve(strict=True) != resolved_target
    ):
        raise RuntimeError(f"Compatibility alias target changed before cleanup: {alias}")
    alias.unlink()


def _unlink_alias_after_target_removal(
    alias: Path,
    expected_link: str,
    removed_target: Path,
) -> None:
    """Remove a saved alias after its target tree no longer exists."""

    if not alias.is_symlink():
        raise RuntimeError(f"Compatibility alias changed before cleanup: {alias}")
    if (
        os.readlink(alias) != expected_link
        or _link_target_path(alias, expected_link) != removed_target
    ):
        raise RuntimeError(f"Compatibility alias target changed before cleanup: {alias}")
    alias.unlink()


def _destination_for_move(
    source: Path,
    destination_root: Path,
    *,
    recorded_aliases: Mapping[Path, str],
) -> tuple[Path, tuple[Path, str] | None]:
    destination = destination_root / source.name
    if not os.path.lexists(destination):
        return destination, None
    expected_link = recorded_aliases.get(destination)
    if expected_link is not None and destination.is_symlink():
        try:
            if (
                os.readlink(destination) == expected_link
                and _link_target_path(destination, expected_link) == source
                and destination.resolve(strict=True) == source
            ):
                return destination, (destination, expected_link)
        except OSError:
            pass
    raise FileExistsError(f"Destination run folder already exists: {destination}")


def preflight_move_run_folder(
    source: str | Path,
    destination_root: str | Path,
    *,
    expected_identity: Mapping[str, Any],
    expected_destination_root_identity: Mapping[str, Any] | None = None,
    allowed_roots: Iterable[str | Path],
) -> dict[str, Any]:
    run = resolve_direct_run_folder(source, allowed_roots=allowed_roots)
    validate_expected_identity(run, expected_identity)
    target_root = resolve_destination_root(
        destination_root, allowed_roots=allowed_roots
    )
    if expected_destination_root_identity is not None:
        validate_expected_identity(
            target_root,
            expected_destination_root_identity,
        )
    if target_root == run.parent:
        raise ValueError("Run folder is already stored in destination_root")
    location = _load_location(run, strict=True)
    aliases = _recorded_aliases(
        run,
        allowed_roots=allowed_roots,
        location=location,
    )
    destination, alias = _destination_for_move(
        run,
        target_root,
        recorded_aliases=dict(aliases),
    )
    return {
        "source": run,
        "destination_root": target_root,
        "destination_root_identity": run_identity(target_root),
        "destination": destination,
        "destination_alias": alias,
        "location": location,
        "recorded_aliases": aliases,
    }


def _signature(path: Path) -> dict[str, Any]:
    """Hash deterministic no-follow ``(path, type, size, link-target)`` rows."""

    _assert_no_nested_mounts(path)
    digest = hashlib.sha256()
    counts = _empty_stats()
    root_device = path.lstat().st_dev
    stack = [(path, Path("."))]
    while stack:
        current, relative = stack.pop()
        metadata = current.lstat()
        if metadata.st_dev != root_device:
            raise ValueError(
                f"Run tree crosses a filesystem boundary at {relative}"
            )
        mode = metadata.st_mode
        kind = (
            "link"
            if stat.S_ISLNK(mode)
            else "file"
            if stat.S_ISREG(mode)
            else "directory"
            if stat.S_ISDIR(mode)
            else "special"
        )
        if kind == "special":
            raise ValueError(f"Run tree contains unsupported special file: {relative}")
        digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(kind.encode("ascii"))
        digest.update(b"\0")
        if kind == "link":
            target = os.readlink(current)
            digest.update(str(len(os.fsencode(target))).encode("ascii"))
            digest.update(b"\0")
            digest.update(target.encode("utf-8", errors="surrogateescape"))
            counts["symlink_count"] += 1
        elif kind == "file":
            digest.update(str(metadata.st_size).encode("ascii"))
            digest.update(b"\0")
            counts["size_bytes"] += int(metadata.st_size)
            counts["allocated_bytes"] += _allocated_bytes(metadata)
            counts["file_count"] += 1
        else:
            digest.update(b"0\0")
            counts["directory_count"] += 1
            children = _directory_children_nofollow(
                current,
                metadata=metadata,
                relative=relative,
            )
            stack.extend(children)
        digest.update(b"\n")
    return {"sha256": digest.hexdigest(), **counts}


def _directory_children_nofollow(
    path: Path,
    *,
    metadata: os.stat_result,
    relative: Path,
) -> list[tuple[Path, Path]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise RuntimeError(f"Directory changed during traversal: {relative}")
        with os.scandir(descriptor) as entries:
            return sorted(
                ((path / entry.name, relative / entry.name) for entry in entries),
                key=lambda item: item[1].as_posix(),
                reverse=True,
            )
    finally:
        os.close(descriptor)


def _update_content_header(
    digest: Any,
    *,
    relative: Path,
    size: int,
) -> None:
    encoded = relative.as_posix().encode("utf-8", errors="surrogateescape")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(size.to_bytes(16, "big"))


def _copy_tree_with_content_hash(source: Path, destination: Path) -> str:
    """Copy a no-follow tree while hashing each source file's copied bytes."""

    _assert_no_nested_mounts(source)
    digest = hashlib.sha256()
    source_device = source.lstat().st_dev
    stack: list[tuple[str, Path, Path, Path]] = [
        ("visit", source, destination, Path("."))
    ]
    while stack:
        action, current, copied, relative = stack.pop()
        if action == "finalize_directory":
            shutil.copystat(current, copied, follow_symlinks=False)
            _fsync_directory(copied)
            continue

        metadata = current.lstat()
        if metadata.st_dev != source_device:
            raise ValueError(
                f"Run tree crosses a filesystem boundary at {relative}"
            )
        mode = metadata.st_mode
        if stat.S_ISDIR(mode):
            if relative == Path(".") and _real_directory(copied):
                try:
                    if any(copied.iterdir()):
                        raise RuntimeError(
                            f"Copy staging directory is not empty: {copied}"
                        )
                except OSError as exc:
                    raise RuntimeError(
                        f"Copy staging directory cannot be inspected: {copied}"
                    ) from exc
            else:
                copied.mkdir()
            stack.append(("finalize_directory", current, copied, relative))
            children = _directory_children_nofollow(
                current,
                metadata=metadata,
                relative=relative,
            )
            for child, child_relative in children:
                stack.append(
                    (
                        "visit",
                        child,
                        copied / child.name,
                        child_relative,
                    )
                )
            continue
        if stat.S_ISLNK(mode):
            os.symlink(os.readlink(current), copied)
            shutil.copystat(current, copied, follow_symlinks=False)
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Run tree contains unsupported special file: {relative}")

        _update_content_header(digest, relative=relative, size=int(metadata.st_size))
        copied_bytes = 0
        with open(current, "rb") as source_handle, open(copied, "xb") as target_handle:
            for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                copied_bytes += len(block)
                digest.update(block)
                target_handle.write(block)
            target_handle.flush()
            if copied_bytes != metadata.st_size:
                raise RuntimeError(
                    f"Source file changed while it was copied: {relative}"
                )
            shutil.copystat(current, copied, follow_symlinks=False)
            os.fsync(target_handle.fileno())
    return digest.hexdigest()


def _content_signature(path: Path) -> str:
    """Hash regular-file bytes in deterministic path order without links."""

    _assert_no_nested_mounts(path)
    digest = hashlib.sha256()
    root_device = path.lstat().st_dev
    stack = [(path, Path("."))]
    while stack:
        current, relative = stack.pop()
        metadata = current.lstat()
        if metadata.st_dev != root_device:
            raise ValueError(
                f"Run tree crosses a filesystem boundary at {relative}"
            )
        mode = metadata.st_mode
        if stat.S_ISDIR(mode):
            children = _directory_children_nofollow(
                current,
                metadata=metadata,
                relative=relative,
            )
            stack.extend(children)
            continue
        if stat.S_ISLNK(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Run tree contains unsupported special file: {relative}")
        _update_content_header(digest, relative=relative, size=int(metadata.st_size))
        measured = 0
        with open(current, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                measured += len(block)
                digest.update(block)
        if measured != metadata.st_size:
            raise RuntimeError(f"Copied file changed during verification: {relative}")
    return digest.hexdigest()


def _stable_tree_signature(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sha256": value["sha256"],
        "size_bytes": int(value["size_bytes"]),
        "file_count": int(value["file_count"]),
        "directory_count": int(value["directory_count"]),
        "symlink_count": int(value["symlink_count"]),
    }


def _verified_tree_evidence(path: Path) -> tuple[dict[str, Any], str]:
    before = _stable_tree_signature(_signature(path))
    content_sha256 = _content_signature(path)
    after = _stable_tree_signature(_signature(path))
    if before != after:
        raise RuntimeError(f"Run tree changed during content verification: {path}")
    return after, content_sha256


def _write_location_after_move(
    storage_path: Path,
    *,
    source: Path,
    current_path: Path | None = None,
    aliases: list[Path],
    existing: Mapping[str, Any] | None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    published_path = current_path or storage_path
    if (
        transaction_id is not None
        and existing is not None
        and existing.get("transaction_id") == transaction_id
        and existing.get("current_path") == published_path.as_posix()
    ):
        return dict(existing)
    history = list(existing.get("history", [])) if existing else []
    history.append(
        {
            "moved_at": utc_now_iso(),
            "from": source.as_posix(),
            "to": published_path.as_posix(),
        }
    )
    value = {
        "schema_version": LOCATION_SCHEMA_VERSION,
        "original_path": (
            str(existing["original_path"]) if existing else source.as_posix()
        ),
        "current_path": published_path.as_posix(),
        "transaction_id": transaction_id,
        "aliases": sorted(
            {
                item.as_posix()
                for item in aliases
                if item != published_path
            }
        ),
        "history": history,
    }
    path = storage_path / LOCATION_FILE
    if path.is_symlink():
        raise ValueError(f"Run location metadata must not be a symbolic link: {path}")
    atomic_write_json(path, value)
    return value


def _validated_identity_value(value: Any, *, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an identity object")
    device = value.get("device")
    inode = value.get("inode")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or device < 0
        or inode <= 0
    ):
        raise ValueError(f"{label} is invalid")
    return {"device": device, "inode": inode}


def _validated_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _validated_tree_signature(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a tree-signature object")
    result: dict[str, Any] = {
        "sha256": _validated_sha256(value.get("sha256"), label=f"{label}.sha256")
    }
    for name in (
        "size_bytes",
        "file_count",
        "directory_count",
        "symlink_count",
    ):
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"{label}.{name} is invalid")
        result[name] = item
    return result


def _write_transaction(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_json(path, dict(value))
    _fsync_directory(path.parent)


def _update_transaction(
    path: Path,
    value: dict[str, Any],
    *,
    phase: str,
    **updates: Any,
) -> None:
    value.update(updates)
    value["phase"] = phase
    value["updated_at"] = utc_now_iso()
    _write_transaction(path, value)


def _finish_transaction(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _transaction_alias_records(
    aliases: Iterable[tuple[Path, str]],
) -> list[dict[str, str]]:
    return [
        {"path": path.as_posix(), "link": link}
        for path, link in aliases
    ]


def _new_transaction(
    *,
    operation: str,
    source: Path,
    expected_identity: Mapping[str, Any],
    aliases: list[tuple[Path, str]],
    prior_location: Mapping[str, Any] | None = None,
    source_tree_signature: Mapping[str, Any] | None = None,
    destination_root: Path | None = None,
    destination_root_identity: Mapping[str, Any] | None = None,
    destination_alias: tuple[Path, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    if operation not in {"move", "delete"}:
        raise ValueError("Run-folder transaction operation is invalid")
    if operation == "move" and source_tree_signature is None:
        source_tree_signature = _stable_tree_signature(_signature(source))
    if destination_root is not None:
        if destination_root_identity is None:
            destination_root_identity = run_identity(destination_root)
        else:
            destination_root_identity = _validated_identity_value(
                destination_root_identity,
                label="destination_root_identity",
            )
    transaction_id = uuid.uuid4().hex
    journal_path = (
        source.parent / f"{TRANSACTION_PREFIX}{transaction_id}.json"
    )
    quarantine = (
        source.parent / f"{MOVE_STAGING_PREFIX}source_{transaction_id}"
    )
    destination = (
        destination_root / source.name
        if destination_root is not None
        else None
    )
    staging = (
        destination_root / f"{MOVE_STAGING_PREFIX}staging_{transaction_id}"
        if destination_root is not None
        else None
    )
    value: dict[str, Any] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "operation": operation,
        "phase": "prepared",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "source": source.as_posix(),
        "source_root": source.parent.as_posix(),
        "expected_identity": dict(expected_identity),
        "quarantine": quarantine.as_posix(),
        "aliases": _transaction_alias_records(aliases),
        "prior_location": (
            dict(prior_location) if prior_location is not None else None
        ),
        "destination_root": (
            destination_root.as_posix()
            if destination_root is not None
            else None
        ),
        "destination_root_identity": (
            dict(destination_root_identity)
            if destination_root is not None
            else None
        ),
        "destination": destination.as_posix() if destination is not None else None,
        "staging": staging.as_posix() if staging is not None else None,
        "staging_identity": None,
        "destination_identity": None,
        "source_tree_signature": (
            dict(source_tree_signature)
            if source_tree_signature is not None
            else None
        ),
        "copy_content_sha256": None,
        "final_destination_signature": None,
        "final_destination_content_sha256": None,
        "destination_alias": (
            destination_alias[0].as_posix()
            if destination_alias is not None
            else None
        ),
        "confirmed_delete": operation == "delete",
    }
    _write_transaction(journal_path, value)
    return journal_path, value


def _transaction_direct_path(
    value: Any,
    *,
    label: str,
    roots: set[Path],
) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Run-folder transaction {label} is invalid")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"Run-folder transaction {label} must be absolute")
    path = _lexical_absolute(candidate)
    if path.parent not in roots:
        raise ValueError(
            f"Run-folder transaction {label} is outside allowed direct-child paths"
        )
    return path


def _load_transaction(
    journal_path: Path,
    *,
    allowed_roots: Iterable[str | Path],
) -> dict[str, Any]:
    roots = set(_normalized_roots(allowed_roots))
    match = TRANSACTION_PATTERN.fullmatch(journal_path.name)
    if (
        match is None
        or journal_path.parent not in roots
        or not _regular_file(journal_path)
    ):
        raise ValueError(f"Run-folder transaction journal is invalid: {journal_path}")
    raw = _read_json_object(journal_path)
    if (
        raw is None
        or raw.get("schema_version") != TRANSACTION_SCHEMA_VERSION
        or raw.get("transaction_id") != match.group("id")
        or raw.get("operation") not in {"move", "delete"}
        or raw.get("phase")
        not in {
            "prepared",
            "isolated",
            "copying",
            "copy_verified",
            "destination_published",
            "committed",
            "destination_verified",
        }
    ):
        raise ValueError(f"Run-folder transaction journal is invalid: {journal_path}")
    transaction_id = match.group("id")
    operation = str(raw["operation"])
    source = _transaction_direct_path(
        raw.get("source"),
        label="source",
        roots=roots,
    )
    if source.parent != journal_path.parent or raw.get("source_root") != (
        source.parent.as_posix()
    ):
        raise ValueError("Run-folder transaction source root is invalid")
    expected_identity = _validated_identity_value(
        raw.get("expected_identity"),
        label="Run-folder transaction expected_identity",
    )
    quarantine = _transaction_direct_path(
        raw.get("quarantine"),
        label="quarantine",
        roots=roots,
    )
    expected_quarantine = (
        source.parent / f"{MOVE_STAGING_PREFIX}source_{transaction_id}"
    )
    if quarantine != expected_quarantine:
        raise ValueError("Run-folder transaction quarantine path is invalid")

    raw_aliases = raw.get("aliases")
    if not isinstance(raw_aliases, list):
        raise ValueError("Run-folder transaction aliases are invalid")
    aliases: list[tuple[Path, str]] = []
    seen_aliases: set[Path] = set()
    for item in raw_aliases:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("link"), str)
        ):
            raise ValueError("Run-folder transaction alias is invalid")
        alias = _transaction_direct_path(
            item.get("path"),
            label="alias",
            roots=roots,
        )
        link = str(item["link"])
        if (
            alias == source
            or alias in seen_aliases
            or _link_target_path(alias, link) != source
        ):
            raise ValueError("Run-folder transaction alias ownership is invalid")
        seen_aliases.add(alias)
        aliases.append((alias, link))
    prior_location_raw = raw.get("prior_location")
    if prior_location_raw is not None and (
        not isinstance(prior_location_raw, Mapping)
        or prior_location_raw.get("schema_version") != LOCATION_SCHEMA_VERSION
        or not isinstance(prior_location_raw.get("original_path"), str)
        or not isinstance(prior_location_raw.get("aliases"), list)
        or not all(
            isinstance(item, str)
            for item in prior_location_raw.get("aliases", [])
        )
        or not isinstance(prior_location_raw.get("history"), list)
    ):
        raise ValueError("Run-folder transaction prior location is invalid")
    prior_location = (
        dict(prior_location_raw)
        if isinstance(prior_location_raw, Mapping)
        else None
    )

    destination_root: Path | None = None
    destination: Path | None = None
    staging: Path | None = None
    destination_alias: tuple[Path, str] | None = None
    destination_root_identity: dict[str, int] | None = None
    if operation == "move":
        destination_root_value = raw.get("destination_root")
        if (
            not isinstance(destination_root_value, str)
            or not Path(destination_root_value).expanduser().is_absolute()
        ):
            raise ValueError("Run-folder transaction destination root is invalid")
        destination_root = _lexical_absolute(destination_root_value)
        if (
            destination_root not in roots
            or destination_root == source.parent
        ):
            raise ValueError("Run-folder transaction destination root is invalid")
        destination_root_identity = _validated_identity_value(
            raw.get("destination_root_identity"),
            label="Run-folder transaction destination_root_identity",
        )
        destination = _transaction_direct_path(
            raw.get("destination"),
            label="destination",
            roots=roots,
        )
        staging = _transaction_direct_path(
            raw.get("staging"),
            label="staging",
            roots=roots,
        )
        if (
            destination != destination_root / source.name
            or staging
            != destination_root
            / f"{MOVE_STAGING_PREFIX}staging_{transaction_id}"
        ):
            raise ValueError("Run-folder transaction destination paths are invalid")
        destination_alias_value = raw.get("destination_alias")
        if destination_alias_value is not None:
            alias_path = _transaction_direct_path(
                destination_alias_value,
                label="destination alias",
                roots=roots,
            )
            matches = [item for item in aliases if item[0] == alias_path]
            if alias_path != destination or len(matches) != 1:
                raise ValueError(
                    "Run-folder transaction destination alias is invalid"
                )
            destination_alias = matches[0]
    elif (
        raw.get("confirmed_delete") is not True
        or any(
            raw.get(name) is not None
            for name in (
                "destination_root",
                "destination_root_identity",
                "destination",
                "staging",
                "destination_alias",
            )
        )
    ):
        raise ValueError("Run-folder delete transaction is not confirmed")

    staging_identity = (
        _validated_identity_value(
            raw.get("staging_identity"),
            label="Run-folder transaction staging_identity",
        )
        if raw.get("staging_identity") is not None
        else None
    )
    destination_identity = (
        _validated_identity_value(
            raw.get("destination_identity"),
            label="Run-folder transaction destination_identity",
        )
        if raw.get("destination_identity") is not None
        else None
    )
    source_tree_signature = (
        _validated_tree_signature(
            raw.get("source_tree_signature"),
            label="Run-folder transaction source_tree_signature",
        )
        if raw.get("source_tree_signature") is not None
        else None
    )
    copy_content_sha256 = (
        _validated_sha256(
            raw.get("copy_content_sha256"),
            label="Run-folder transaction copy_content_sha256",
        )
        if raw.get("copy_content_sha256") is not None
        else None
    )
    final_destination_signature = (
        _validated_tree_signature(
            raw.get("final_destination_signature"),
            label="Run-folder transaction final_destination_signature",
        )
        if raw.get("final_destination_signature") is not None
        else None
    )
    final_destination_content_sha256 = (
        _validated_sha256(
            raw.get("final_destination_content_sha256"),
            label="Run-folder transaction final_destination_content_sha256",
        )
        if raw.get("final_destination_content_sha256") is not None
        else None
    )
    if operation == "move":
        if source_tree_signature is None:
            raise ValueError(
                "Run-folder move transaction source signature is missing"
            )
        if (
            staging_identity is not None
            and raw["phase"]
            in {"copy_verified", "destination_published", "committed"}
            and copy_content_sha256 is None
        ):
            raise ValueError(
                "Run-folder move transaction copy evidence is missing"
            )
        if raw["phase"] in {"committed", "destination_verified"} and (
            final_destination_signature is None
            or final_destination_content_sha256 is None
        ):
            raise ValueError(
                "Committed run-folder transaction final evidence is missing"
            )
    elif any(
        item is not None
        for item in (
            source_tree_signature,
            copy_content_sha256,
            final_destination_signature,
            final_destination_content_sha256,
        )
    ):
        raise ValueError("Run-folder delete transaction contains move evidence")
    return {
        "journal_path": journal_path,
        "raw": dict(raw),
        "transaction_id": transaction_id,
        "operation": operation,
        "phase": raw["phase"],
        "source": source,
        "expected_identity": expected_identity,
        "quarantine": quarantine,
        "aliases": aliases,
        "prior_location": prior_location,
        "destination_root": destination_root,
        "destination_root_identity": destination_root_identity,
        "destination": destination,
        "staging": staging,
        "staging_identity": staging_identity,
        "destination_identity": destination_identity,
        "source_tree_signature": source_tree_signature,
        "copy_content_sha256": copy_content_sha256,
        "final_destination_signature": final_destination_signature,
        "final_destination_content_sha256": final_destination_content_sha256,
        "destination_alias": destination_alias,
    }


def _remove_validated_tree(
    path: Path,
    *,
    expected_identity: Mapping[str, Any],
) -> None:
    validate_expected_identity(path, expected_identity)
    _signature(path)
    _assert_no_nested_mounts(path)
    # Run-owned immutable snapshots intentionally remove owner write access
    # from their directories.  A confirmed run deletion (and move-source
    # cleanup) must be able to remove those snapshots without weakening the
    # normal on-disk immutability contract.  Only relax directories owned by
    # this process; ownership/ACL problems outside that narrow case must still
    # fail closed and remain visible through durable recovery.
    effective_uid = os.geteuid()
    for _root, _directories, _files, descriptor in os.fwalk(
        path,
        topdown=True,
        follow_symlinks=False,
    ):
        metadata = os.fstat(descriptor)
        if metadata.st_uid != effective_uid:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if not mode & stat.S_IWUSR:
            os.fchmod(descriptor, mode | stat.S_IWUSR)
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _restore_transaction_alias(
    alias: Path,
    saved_link: str,
    *,
    source: Path,
    destination: Path | None,
) -> None:
    if not os.path.lexists(alias):
        alias.symlink_to(saved_link, target_is_directory=True)
        _fsync_directory(alias.parent)
        return
    if not alias.is_symlink():
        raise RuntimeError(f"Transaction alias path is occupied: {alias}")
    current_link = os.readlink(alias)
    current_target = _link_target_path(alias, current_link)
    if current_link == saved_link and current_target == source:
        return
    if destination is None or current_target != destination:
        raise RuntimeError(f"Transaction alias target changed: {alias}")
    alias.unlink()
    alias.symlink_to(saved_link, target_is_directory=True)
    _fsync_directory(alias.parent)


def _publish_transaction_alias(alias: Path, destination: Path) -> None:
    if not os.path.lexists(alias):
        alias.symlink_to(destination, target_is_directory=True)
        _fsync_directory(alias.parent)
        return
    if not alias.is_symlink():
        raise RuntimeError(f"Transaction alias path is occupied: {alias}")
    current_link = os.readlink(alias)
    current_target = _link_target_path(alias, current_link)
    if current_target == destination and current_link == destination.as_posix():
        return
    if alias.resolve(strict=True) != destination:
        raise RuntimeError(f"Transaction alias target changed: {alias}")
    alias.unlink()
    alias.symlink_to(destination, target_is_directory=True)
    _fsync_directory(alias.parent)


def _restore_transaction_location(
    transaction: Mapping[str, Any],
    *,
    source: Path,
) -> None:
    path = source / LOCATION_FILE
    prior = transaction.get("prior_location")
    current = _read_json_object(path) if os.path.lexists(path) else None
    if prior is None:
        if not os.path.lexists(path):
            return
        if (
            current is None
            or current.get("transaction_id") != transaction["transaction_id"]
        ):
            raise RuntimeError(
                "Run-folder transaction location metadata changed during rollback"
            )
        path.unlink()
        _fsync_directory(source)
        return
    if os.path.lexists(path) and (
        current is None
        or (
            dict(current) != dict(prior)
            and current.get("transaction_id") != transaction["transaction_id"]
        )
    ):
        raise RuntimeError(
            "Run-folder transaction location metadata changed during rollback"
        )
    atomic_write_json(path, prior)
    _fsync_directory(source)


def _remove_transaction_staging(transaction: Mapping[str, Any]) -> None:
    staging = transaction.get("staging")
    if not isinstance(staging, Path) or not os.path.lexists(staging):
        return
    identity = transaction.get("staging_identity")
    if identity is None:
        if not _real_directory(staging):
            raise RuntimeError(
                f"Unidentified transaction staging path is not a directory: {staging}"
            )
        try:
            if any(staging.iterdir()):
                raise RuntimeError(
                    f"Unidentified transaction staging path is not empty: {staging}"
                )
        except OSError as exc:
            raise RuntimeError(
                f"Unidentified transaction staging cannot be inspected: {staging}"
            ) from exc
        staging.rmdir()
        _fsync_directory(staging.parent)
        return
    _remove_validated_tree(staging, expected_identity=identity)


def _recover_move_transaction(transaction: dict[str, Any]) -> str:
    source: Path = transaction["source"]
    quarantine: Path = transaction["quarantine"]
    destination: Path = transaction["destination"]
    expected_identity: dict[str, int] = transaction["expected_identity"]
    roll_forward = transaction["phase"] in {
        "committed",
        "destination_verified",
    }
    destination_identity = transaction.get("destination_identity")
    staging_identity = transaction.get("staging_identity")
    destination_root: Path = transaction["destination_root"]
    destination_root_identity = transaction["destination_root_identity"]

    # The journal exists only in the source root. Never infer that an absent
    # destination/staging path was cleaned up while its mounted root is offline
    # or has been replaced by a different directory.
    validate_expected_identity(destination_root, destination_root_identity)

    if roll_forward:
        identity = destination_identity
        if identity is None:
            raise RuntimeError(
                "Committed run-folder transaction lacks destination identity"
            )
        if not _real_directory(destination):
            if os.path.lexists(destination):
                destination_alias = transaction.get("destination_alias")
                if (
                    destination_alias is None
                    or destination_alias[0] != destination
                ):
                    raise RuntimeError(
                        "Committed run-folder destination path is occupied"
                    )
                _unlink_alias_after_target_removal(
                    destination_alias[0],
                    destination_alias[1],
                    source,
                )
                _fsync_directory(destination.parent)
            candidate = (
                transaction["staging"]
                if staging_identity is not None
                else quarantine
            )
            validate_expected_identity(candidate, identity)
            _assert_no_nested_mounts(candidate)
            _durable_rename(candidate, destination)
        validate_expected_identity(destination, identity)
        location = _load_location(destination, strict=True)
        if (
            location is None
            or location.get("transaction_id") != transaction["transaction_id"]
            or location.get("current_path") != destination.as_posix()
        ):
            raise RuntimeError(
                "Committed run-folder transaction location evidence is missing"
            )
        if transaction["phase"] == "committed":
            actual_signature, actual_content_sha256 = _verified_tree_evidence(
                destination
            )
            if (
                actual_signature != transaction["final_destination_signature"]
                or actual_content_sha256
                != transaction["final_destination_content_sha256"]
            ):
                raise RuntimeError(
                    "Committed run-folder destination content no longer matches "
                    "durable transaction evidence"
                )
            _update_transaction(
                transaction["journal_path"],
                transaction["raw"],
                phase="destination_verified",
            )
        _publish_transaction_alias(source, destination)
        for alias, _saved_link in transaction["aliases"]:
            if alias == destination:
                continue
            _publish_transaction_alias(alias, destination)
        if os.path.lexists(quarantine):
            _remove_validated_tree(
                quarantine,
                expected_identity=expected_identity,
            )
        _remove_transaction_staging(transaction)
        _finish_transaction(transaction["journal_path"])
        return "completed_move"

    if source.is_symlink():
        expected_link = destination.as_posix()
        if (
            os.readlink(source) != expected_link
            or source.resolve(strict=True) != destination
        ):
            raise RuntimeError(
                f"Run-folder transaction source alias changed: {source}"
            )
        source.unlink()
        _fsync_directory(source.parent)
    elif os.path.lexists(source):
        validate_expected_identity(source, expected_identity)

    quarantine_exists = os.path.lexists(quarantine)
    destination_is_real = _real_directory(destination)
    if quarantine_exists:
        validate_expected_identity(quarantine, expected_identity)
        if os.path.lexists(source):
            raise RuntimeError(
                "Run-folder transaction cannot restore an occupied source path"
            )
        _restore_quarantined_run(quarantine, source)
        if destination_is_real:
            identity = destination_identity or staging_identity
            if identity is None:
                raise RuntimeError(
                    "Run-folder transaction cannot identify the published copy"
                )
            _remove_validated_tree(destination, expected_identity=identity)
    elif destination_is_real:
        destination_actual = run_identity(destination)
        if destination_actual == expected_identity:
            if os.path.lexists(source):
                raise RuntimeError(
                    "Run-folder transaction cannot restore an occupied source path"
                )
            _durable_rename(destination, source)
        else:
            identity = destination_identity or staging_identity
            if identity is None or destination_actual != identity:
                raise RuntimeError(
                    "Run-folder transaction destination identity changed"
                )
            if not _real_directory(source):
                raise RuntimeError(
                    "Run-folder transaction lost its authoritative source"
                )
            _remove_validated_tree(destination, expected_identity=identity)
    elif not _real_directory(source):
        raise RuntimeError("Run-folder transaction lost its authoritative source")

    validate_expected_identity(source, expected_identity)
    _restore_transaction_location(transaction, source=source)
    _remove_transaction_staging(transaction)
    for alias, saved_link in transaction["aliases"]:
        _restore_transaction_alias(
            alias,
            saved_link,
            source=source,
            destination=destination,
        )
    _finish_transaction(transaction["journal_path"])
    return "rolled_back_move"


def _recover_delete_transaction(transaction: dict[str, Any]) -> str:
    source: Path = transaction["source"]
    quarantine: Path = transaction["quarantine"]
    expected_identity: dict[str, int] = transaction["expected_identity"]
    journal_path: Path = transaction["journal_path"]
    raw: dict[str, Any] = transaction["raw"]

    source_exists = os.path.lexists(source)
    quarantine_exists = os.path.lexists(quarantine)
    if source_exists and quarantine_exists:
        raise RuntimeError(
            "Confirmed delete transaction has both source and quarantine paths"
        )
    if source_exists:
        validate_expected_identity(source, expected_identity)
        isolated = _quarantine_run(
            source,
            expected_identity=expected_identity,
            quarantine=quarantine,
        )
        if isolated != quarantine:
            raise RuntimeError("Confirmed delete isolation path changed")
        _update_transaction(journal_path, raw, phase="isolated")
        quarantine_exists = True
    if quarantine_exists:
        _remove_validated_tree(
            quarantine,
            expected_identity=expected_identity,
        )
        _update_transaction(journal_path, raw, phase="committed")

    for alias, expected_link in transaction["aliases"]:
        if not os.path.lexists(alias):
            continue
        _unlink_alias_after_target_removal(alias, expected_link, source)
        _fsync_directory(alias.parent)
    _finish_transaction(journal_path)
    return "resumed_delete"


def recover_run_folder_transactions(
    allowed_roots: Iterable[str | Path],
) -> dict[str, Any]:
    """Recover only durable, validated run-folder transaction journals."""

    roots = _normalized_roots(allowed_roots)
    existing_roots = tuple(root for root in roots if _real_directory(root))
    recovered: list[dict[str, str]] = []
    unresolved: list[dict[str, Any]] = []
    if not existing_roots:
        return {
            "schema_version": MAINTENANCE_SCHEMA_VERSION,
            "journal_fingerprint": run_folder_transaction_fingerprint(roots),
            "recovered_count": 0,
            "transactions": [],
            "unresolved_count": 0,
            "unresolved": [],
        }
    with _storage_locks(existing_roots):
        journal_paths: list[Path] = []
        for root in existing_roots:
            try:
                candidates = list(root.iterdir())
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot inspect run-folder transactions below {root}"
                ) from exc
            for candidate in candidates:
                if TRANSACTION_PATTERN.fullmatch(candidate.name):
                    journal_paths.append(candidate)
        for journal_path in sorted(
            journal_paths,
            key=lambda item: item.as_posix(),
        ):
            transaction: dict[str, Any] | None = None
            try:
                transaction = _load_transaction(
                    journal_path,
                    allowed_roots=roots,
                )
                if transaction["operation"] == "move":
                    action = _recover_move_transaction(transaction)
                else:
                    action = _recover_delete_transaction(transaction)
            except Exception as exc:
                remnant_bytes: int | None = None
                if transaction is not None:
                    try:
                        measured = 0
                        for path, identity in (
                            (
                                transaction["quarantine"],
                                transaction["expected_identity"],
                            ),
                            (
                                transaction.get("staging"),
                                transaction.get("staging_identity"),
                            ),
                        ):
                            if (
                                isinstance(path, Path)
                                and identity is not None
                                and os.path.lexists(path)
                            ):
                                validate_expected_identity(path, identity)
                                measured += int(_signature(path)["size_bytes"])
                        remnant_bytes = measured
                    except Exception:
                        remnant_bytes = None
                match = TRANSACTION_PATTERN.fullmatch(journal_path.name)
                unresolved.append(
                    {
                        "transaction_id": (
                            transaction["transaction_id"]
                            if transaction is not None
                            else match.group("id") if match is not None else None
                        ),
                        "operation": (
                            transaction["operation"]
                            if transaction is not None
                            else None
                        ),
                        "error": f"{type(exc).__name__}: {exc}",
                        "remnant_bytes": remnant_bytes,
                    }
                )
                continue
            recovered.append(
                {
                    "transaction_id": transaction["transaction_id"],
                    "operation": transaction["operation"],
                    "action": action,
                }
            )
        journal_fingerprint = run_folder_transaction_fingerprint(roots)
    return {
        "schema_version": MAINTENANCE_SCHEMA_VERSION,
        "journal_fingerprint": journal_fingerprint,
        "recovered_count": len(recovered),
        "transactions": recovered,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
    }


def move_run_folder(
    source: str | Path,
    destination_root: str | Path,
    *,
    expected_identity: Mapping[str, Any],
    expected_destination_root_identity: Mapping[str, Any] | None = None,
    allowed_roots: Iterable[str | Path],
) -> dict[str, Any]:
    """Move one real run and retain old absolute-path compatibility aliases."""

    roots = _normalized_roots(allowed_roots)
    maintenance = recover_run_folder_transactions(roots)
    if maintenance["unresolved_count"]:
        raise RuntimeError(
            "Unresolved run-folder storage maintenance must be repaired before moving"
        )
    initial = preflight_move_run_folder(
        source,
        destination_root,
        expected_identity=expected_identity,
        expected_destination_root_identity=(
            expected_destination_root_identity
        ),
        allowed_roots=roots,
    )
    run: Path = initial["source"]
    target_root: Path = initial["destination_root"]
    pinned_destination_root_identity = initial["destination_root_identity"]
    with _storage_locks((run.parent, target_root)):
        checked = preflight_move_run_folder(
            run,
            target_root,
            expected_identity=expected_identity,
            expected_destination_root_identity=pinned_destination_root_identity,
            allowed_roots=roots,
        )
        destination: Path = checked["destination"]
        destination_alias: tuple[Path, str] | None = checked["destination_alias"]
        existing_location: Mapping[str, Any] | None = checked["location"]
        prior_aliases: list[tuple[Path, str]] = checked["recorded_aliases"]
        journal_path: Path | None = None
        transaction: dict[str, Any] | None = None
        quarantine: Path | None = None
        staging: Path | None = None
        cleanup_warning: str | None = None
        cleanup_remaining_path: str | None = None
        location: Mapping[str, Any] | None = None
        try:
            # This full no-follow preflight happens under both root locks and
            # before any journal or quarantine path is created.
            source_tree_signature = _stable_tree_signature(_signature(run))
            journal_path, transaction = _new_transaction(
                operation="move",
                source=run,
                expected_identity=expected_identity,
                aliases=prior_aliases,
                prior_location=existing_location,
                source_tree_signature=source_tree_signature,
                destination_root=target_root,
                destination_root_identity=pinned_destination_root_identity,
                destination_alias=destination_alias,
            )
            quarantine = Path(transaction["quarantine"])
            staging = Path(transaction["staging"])
            quarantine = _quarantine_run(
                run,
                expected_identity=expected_identity,
                quarantine=quarantine,
            )
            _update_transaction(
                journal_path,
                transaction,
                phase="isolated",
            )
            same_device = _same_filesystem_mount(quarantine, target_root)
            candidate = quarantine
            if not same_device:
                source_signature = _stable_tree_signature(_signature(quarantine))
                free_bytes = shutil.disk_usage(target_root).free
                if free_bytes < int(source_signature["size_bytes"]):
                    raise OSError(
                        "Destination root does not have enough free space for "
                        f"{source_signature['size_bytes']} logical bytes"
                    )
                staging.mkdir()
                _fsync_directory(target_root)
                staging_identity = run_identity(staging)
                _update_transaction(
                    journal_path,
                    transaction,
                    phase="copying",
                    staging_identity=staging_identity,
                )
                source_content_sha256 = _copy_tree_with_content_hash(
                    quarantine,
                    staging,
                )
                (
                    destination_signature,
                    destination_content_sha256,
                ) = _verified_tree_evidence(staging)
                if (
                    source_signature != destination_signature
                    or source_content_sha256 != destination_content_sha256
                ):
                    raise RuntimeError(
                        "Copied run tree verification failed; source was preserved"
                    )
                _update_transaction(
                    journal_path,
                    transaction,
                    phase="copy_verified",
                    source_tree_signature=source_signature,
                    copy_content_sha256=source_content_sha256,
                )
                candidate = staging
            aliases = [
                run,
                *[
                    alias
                    for alias, _expected_link in prior_aliases
                    if alias not in {run, destination}
                ],
            ]
            location = _write_location_after_move(
                candidate,
                source=run,
                current_path=destination,
                aliases=aliases,
                existing=existing_location,
                transaction_id=transaction["transaction_id"],
            )
            _fsync_directory(candidate)
            (
                final_destination_signature,
                final_destination_content_sha256,
            ) = _verified_tree_evidence(candidate)
            candidate_identity = run_identity(candidate)
            _update_transaction(
                journal_path,
                transaction,
                phase="committed",
                destination_identity=candidate_identity,
                final_destination_signature=final_destination_signature,
                final_destination_content_sha256=(
                    final_destination_content_sha256
                ),
            )
            recovery = _load_transaction(
                journal_path,
                allowed_roots=roots,
            )
            if _recover_move_transaction(recovery) != "completed_move":
                raise RuntimeError("Committed run-folder move did not roll forward")
            location = _load_location(destination, strict=True)
            quarantine = None
            staging = None
        except Exception as operation_exc:
            if journal_path is None or not os.path.lexists(journal_path):
                raise
            try:
                recovery = _load_transaction(
                    journal_path,
                    allowed_roots=roots,
                )
                recovery_action = _recover_move_transaction(recovery)
            except Exception as recovery_exc:
                raise RuntimeError(
                    "Run-folder move failed and durable recovery remains pending at "
                    f"{journal_path}: {recovery_exc}"
                ) from operation_exc
            if recovery_action != "completed_move":
                raise
            location = _load_location(destination, strict=True)
            cleanup_warning = (
                f"{type(operation_exc).__name__}: {operation_exc}; "
                "the committed move was recovered"
            )

    if location is None:
        raise RuntimeError("Run-folder move completed without location evidence")
    return {
        "status": "moved",
        "source_run_root": run.as_posix(),
        "destination_run_root": destination.as_posix(),
        "compatibility_alias": run.as_posix(),
        "source_cleanup_complete": cleanup_warning is None,
        "source_cleanup_warning": cleanup_warning,
        "source_cleanup_remaining_path": cleanup_remaining_path,
        "relocation": {
            "original_path": location["original_path"],
            "aliases": location["aliases"],
            "history_count": len(location["history"]),
        },
    }


def delete_run_folder(
    source: str | Path,
    *,
    expected_identity: Mapping[str, Any],
    allowed_roots: Iterable[str | Path],
) -> dict[str, Any]:
    """Permanently remove one run and only aliases verified to resolve to it."""

    roots = _normalized_roots(allowed_roots)
    maintenance = recover_run_folder_transactions(roots)
    if maintenance["unresolved_count"]:
        raise RuntimeError(
            "Unresolved run-folder storage maintenance must be repaired before deleting"
        )
    run = resolve_direct_run_folder(source, allowed_roots=roots)
    validate_expected_identity(run, expected_identity)
    with _storage_locks((run.parent,)):
        run = resolve_direct_run_folder(run, allowed_roots=roots)
        validate_expected_identity(run, expected_identity)
        location = _load_location(run, strict=True)
        aliases = _recorded_aliases(
            run,
            allowed_roots=roots,
            location=location,
        )
        # Fail while the selected run is still visible if traversal would
        # cross a device or a same-device bind mount.
        _signature(run)
        journal_path, transaction = _new_transaction(
            operation="delete",
            source=run,
            expected_identity=expected_identity,
            aliases=aliases,
        )
        quarantine = Path(transaction["quarantine"])
        quarantine = _quarantine_run(
            run,
            expected_identity=expected_identity,
            quarantine=quarantine,
        )
        _update_transaction(
            journal_path,
            transaction,
            phase="isolated",
        )
        _remove_validated_tree(
            quarantine,
            expected_identity=expected_identity,
        )
        _update_transaction(
            journal_path,
            transaction,
            phase="committed",
        )
        for alias, expected_link in aliases:
            _unlink_alias_after_target_removal(alias, expected_link, run)
            _fsync_directory(alias.parent)
        _finish_transaction(journal_path)
    return {
        "status": "deleted",
        "source_run_root": run.as_posix(),
        "deleted_aliases": [alias.as_posix() for alias, _link in aliases],
    }
