"""Non-destructive frame/robot-pose synchronization.

It consumes current frame and robot-pose metadata, copies synchronized frames
into a derived folder, and keeps raw capture folders unchanged.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Iterator, Mapping, Sequence

from posetestbot.io.atomic import (
    atomic_write_json,
    atomic_write_text,
    replace_directories,
    replace_directory,
)
from posetestbot.io.artifacts import (
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    CURRENT_SENSOR_METADATA_ARTIFACTS,
    MATCH_ROBOT_EE_POSES,
    PROCESSED_DIR,
    RGB_DIR,
    RAW_ROBOT_EE_POSES,
    SYNC_REPORT,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.manifest import discover_sensor_records
from posetestbot.pipeline.sensor_selection import (
    enabled_sensor_folder_names,
    filter_enabled_sensor_folders,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.robot.pose_receiver import (
    POSE_PACKET_SCHEMA_VERSION,
    STREAM_END_SOURCE_PACKET,
)
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    configured_sunrise_reference_frame_path,
)
from posetestbot.sensors.contracts import SensorType
from posetestbot.sensors.registry import is_auto_device_id, sensor_folder_name


SCHEMA_VERSION = "sync_report.v4"
FRAME_TIMESTAMP_SOURCES = ("host_received", "host_wall", "sensor")
ROBOT_TIMESTAMP_SOURCES = ("host_received", "host_wall")
SUPPORTED_TIMESTAMP_PAIRS = {
    ("host_received", "host_received"),
    ("host_wall", "host_wall"),
    ("sensor", "host_wall"),
}


@dataclass(frozen=True)
class SyncResult:
    sensor_folder: str
    output_folder: str
    matched_poses_path: str
    report_path: str
    total_frames: int
    matched_frames: int
    dropped_frames: int


@dataclass(frozen=True)
class SensorSyncSettings:
    """Per-sensor parameters for one transactionally published sync generation."""

    sensor_folder: str | Path
    sync_delta: int | float | Mapping[str, Any] | None = None
    timestamp_source: str = "host_received"
    robot_timestamp_source: str | None = None
    max_nearest_pose_delta_ms: int | float | None = None
    required_frame_timestamp_domain: str | None = None
    timestamp_fallback_allowed: bool = False
    calibration_sync: Mapping[str, Any] | None = None
    raw_robot_poses: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _PreparedSync:
    result: SyncResult
    staging_folder: Path
    output_folder: Path


DATASET_PROCESSING_LOCK = ".synchronization.lock"
SYNC_PUBLICATION_LOCK = DATASET_PROCESSING_LOCK
_HELD_DATASET_PROCESSING_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "held_dataset_processing_locks",
    default=frozenset(),
)


@contextmanager
def dataset_processing_lock(run_root: str | Path) -> Iterator[None]:
    """Serialize one run's derived-data transactions across processes.

    The context is re-entrant so a CLI can cover manifest updates and then call
    a library writer that independently protects its publication boundary.
    """

    root = Path(run_root)
    lock_parent = root / PROCESSED_DIR
    _reject_symlink_components(lock_parent, label="Synchronization lock parent")
    lock_key = lock_parent.resolve().as_posix()
    held = _HELD_DATASET_PROCESSING_LOCKS.get()
    if lock_key in held:
        yield
        return
    lock_parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(lock_parent, label="Synchronization lock parent")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(lock_parent, directory_flags)
    descriptor = -1
    locked = False
    token = None
    try:
        opened_parent = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened_parent.st_mode):
            raise ValueError(
                f"Synchronization lock parent must be a directory: {lock_parent}"
            )
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            DATASET_PROCESSING_LOCK,
            lock_flags,
            0o600,
            dir_fd=directory_fd,
        )
        opened_lock = os.fstat(descriptor)
        if not stat.S_ISREG(opened_lock.st_mode):
            raise ValueError(
                "Dataset-processing lock must be a regular file: "
                f"{lock_parent / DATASET_PROCESSING_LOCK}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        _reject_symlink_components(lock_parent, label="Synchronization lock parent")
        current_parent = os.stat(lock_parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or current_parent.st_dev != opened_parent.st_dev
            or current_parent.st_ino != opened_parent.st_ino
        ):
            raise ValueError(
                f"Synchronization lock parent changed while locking: {lock_parent}"
            )
        current_lock = os.stat(
            DATASET_PROCESSING_LOCK,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current_lock.st_mode)
            or current_lock.st_dev != opened_lock.st_dev
            or current_lock.st_ino != opened_lock.st_ino
        ):
            raise ValueError(
                "Dataset-processing lock changed while locking: "
                f"{lock_parent / DATASET_PROCESSING_LOCK}"
            )
        token = _HELD_DATASET_PROCESSING_LOCKS.set(held | {lock_key})
        yield
    finally:
        if token is not None:
            _HELD_DATASET_PROCESSING_LOCKS.reset(token)
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_evidence(
    path: Path,
    *,
    run_root: Path,
    recorded_path: Path | None = None,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Synchronization input must be a regular file: {path}")
    return {
        "path": _relative_path(recorded_path or path, run_root),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _snapshot_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "source": "verified_in_memory_snapshot",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


_SYNC_DOWNSTREAM_ROOTS = frozenset({"blenderproc", "masks"})
_DIRECTORY_REPLACEMENT_LOCK = ".posetestbot-directory-replace.lock"


def _synchronization_owned_artifact_evidence(
    folder: Path,
    *,
    output_contract: str,
) -> dict[str, Any]:
    """Fingerprint only the immutable synchronization-owned artifact set.

    Optional rendering deliberately adds ``blenderproc/`` and ``masks/`` below
    a synchronized sensor. Directory replacement also retains a lock file in
    that parent. Those namespaces are validated but excluded so downstream
    publication cannot stale the synchronization generation.
    """

    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(
            f"Synchronization output must be a regular directory: {folder}"
        )
    if output_contract not in {"rgbd_copy", "pairing_only"}:
        raise ValueError("Unsupported synchronization output contract")
    owned_root_files = {MATCH_ROBOT_EE_POSES}
    owned_directories: set[str] = set()
    if output_contract == "rgbd_copy":
        owned_root_files.update(CURRENT_SENSOR_METADATA_ARTIFACTS)
        owned_directories.update({RGB_DIR, DEPTH_DIR})
    relative_paths = [Path(name) for name in sorted(owned_root_files)]
    for directory_name in sorted(owned_directories):
        directory = folder / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(
                f"Synchronization output requires regular {directory_name}/: {folder}"
            )
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError(
                    "Synchronization-owned RGB-D directories may contain only "
                    f"regular files: {path}"
                )
            relative_paths.append(path.relative_to(folder))

    allowed_names = owned_root_files | owned_directories | _SYNC_DOWNSTREAM_ROOTS | {
        SYNC_REPORT
    }
    for path in folder.iterdir():
        if path.name in allowed_names:
            if path.name in _SYNC_DOWNSTREAM_ROOTS and (
                path.is_symlink() or not path.is_dir()
            ):
                raise ValueError(
                    f"Derived synchronization namespace must be a directory: {path}"
                )
            continue
        if path.name == _DIRECTORY_REPLACEMENT_LOCK:
            if path.is_symlink() or not path.is_file():
                raise ValueError(
                    f"Directory-replacement lock must be a regular file: {path}"
                )
            continue
        raise ValueError(f"Unexpected artifact in synchronized sensor root: {path}")
    return _file_set_evidence(folder, relative_paths)


def _file_set_evidence(folder: Path, relative_paths: Iterable[Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative in sorted(relative_paths, key=lambda item: item.as_posix()):
        relative_text = relative.as_posix()
        if relative.is_absolute() or relative_text in seen:
            raise ValueError(
                f"Synchronization source artifact path is invalid or duplicated: {relative}"
            )
        seen.add(relative_text)
        path = folder / relative
        resolved = path.resolve()
        try:
            resolved.relative_to(folder.resolve())
        except ValueError as exc:
            raise ValueError(
                f"Synchronization source artifact escapes its sensor folder: {path}"
            ) from exc
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                f"Synchronization source artifact must be a regular file: {path}"
            )
        rows.append(
            {
                "path": relative_text,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    payload = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "file_count": len(rows),
        "total_size_bytes": sum(int(row["size_bytes"]) for row in rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _source_sensor_artifact_evidence(
    sensor_folder: Path,
    frame_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    relative_paths = [Path(name) for name in CURRENT_SENSOR_METADATA_ARTIFACTS]
    rgb_names: set[str] = set()
    depth_names: set[str] = set()
    for record in frame_records:
        for field, expected_dir, names in (
            ("rgb_path", RGB_DIR, rgb_names),
            ("depth_path", DEPTH_DIR, depth_names),
        ):
            value = record.get(field)
            _resolve_source_frame_path(sensor_folder, value, expected_dir)
            relative = Path(str(value))
            if relative.name in names:
                raise ValueError(
                    f"Frame metadata duplicates {expected_dir} file {relative.name}"
                )
            names.add(relative.name)
            relative_paths.append(relative)
    for expected_dir, expected_names in (
        (RGB_DIR, rgb_names),
        (DEPTH_DIR, depth_names),
    ):
        directory = sensor_folder / expected_dir
        png_entries = [path for path in directory.iterdir() if path.suffix == ".png"]
        invalid_entries = [
            path for path in png_entries if path.is_symlink() or not path.is_file()
        ]
        if invalid_entries:
            raise ValueError(
                f"Raw {expected_dir} contains non-regular PNG artifacts: "
                + ", ".join(path.name for path in invalid_entries)
            )
        actual_names = {path.name for path in png_entries}
        if actual_names != expected_names:
            raise ValueError(
                f"Raw {expected_dir} membership does not match frame metadata: "
                f"{sensor_folder}"
            )
    return _file_set_evidence(sensor_folder, relative_paths)


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    try:
        first_resolved.relative_to(second_resolved)
        return True
    except ValueError:
        pass
    try:
        second_resolved.relative_to(first_resolved)
        return True
    except ValueError:
        return False


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path, *, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symlink components: {current}")


def validate_sync_output_root(
    run_root: str | Path,
    output_root: str | Path,
    *,
    diagnostic_unmanaged: bool,
) -> Path:
    """Validate a managed or explicitly diagnostic synchronization root."""

    root = Path(run_root)
    output_base = Path(output_root)
    canonical = root / PROCESSED_DIR / SYNCHRONIZED_DIR
    if not isinstance(diagnostic_unmanaged, bool):
        raise ValueError("diagnostic_unmanaged must be a boolean")
    is_canonical = output_base.resolve() == canonical.resolve()
    if diagnostic_unmanaged:
        if is_canonical:
            raise ValueError(
                "diagnostic_unmanaged synchronization cannot target the canonical output"
            )
    elif not is_canonical:
        raise ValueError(
            "Managed synchronization output must be canonical "
            "<run>/processed/synchronized"
        )
    if _is_within(root, output_base):
        raise ValueError(
            "Synchronization output must not equal or contain the run root"
        )
    if _is_within(output_base, root):
        relative_output = output_base.resolve().relative_to(root.resolve())
        if not relative_output.parts or relative_output.parts[0] != PROCESSED_DIR:
            raise ValueError(
                "Run-contained synchronization output must remain below processed/"
            )
    _reject_symlink_components(output_base, label="Synchronization output")
    return output_base


def validate_sync_output_boundary(
    run_root: str | Path,
    sensor_folder: str | Path,
    output_root: str | Path,
    *,
    diagnostic_unmanaged: bool,
) -> Path:
    """Validate one synchronization destination before creating any directory."""

    source = Path(sensor_folder)
    output_base = validate_sync_output_root(
        run_root,
        output_root,
        diagnostic_unmanaged=diagnostic_unmanaged,
    )
    output_folder = output_base / source.name
    if _paths_overlap(output_folder, source):
        raise ValueError("Synchronization output must not overlap the raw sensor input")
    return output_base


def _validate_sensor_source_boundary(run_root: Path, sensor_folder: Path) -> Path:
    """Require raw sensor input to be one regular run-contained directory."""

    _reject_symlink_components(sensor_folder, label="Synchronization sensor input")
    if sensor_folder.is_symlink() or not sensor_folder.is_dir():
        raise ValueError(
            f"Synchronization sensor input must be a regular directory: {sensor_folder}"
        )
    resolved = sensor_folder.resolve()
    try:
        relative = resolved.relative_to(run_root.resolve())
    except ValueError as exc:
        raise ValueError(
            "Synchronization sensor input must remain below the run root: "
            f"{sensor_folder}"
        ) from exc
    if not relative.parts:
        raise ValueError("Synchronization sensor input cannot equal the run root")
    return resolved


def _read_json(path: Path) -> Any:
    with open(path, "r") as f:
        return json.load(f)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_json(path, value)


def load_frame_metadata(sensor_folder: str | Path) -> list[dict[str, Any]]:
    folder = Path(sensor_folder)
    metadata_path = folder / FRAME_METADATA_JSONL
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise FileNotFoundError(f"Current frame metadata is required: {metadata_path}")
    records = []
    seen_frame_ids: set[str] = set()
    with open(metadata_path, "r") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(
                    f"Frame metadata line {line_number} is not newline-committed"
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {metadata_path} line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"Frame metadata line {line_number} must be a JSON object"
                )
            if record.get("schema_version") != "frame_metadata.v1":
                raise ValueError(
                    f"Frame metadata line {line_number} must use frame_metadata.v1"
                )
            try:
                SensorType(str(record.get("sensor_type")))
            except ValueError as exc:
                raise ValueError(
                    f"Frame metadata line {line_number} has an unknown sensor_type"
                ) from exc
            for field in ("sensor_id", "rgb_path", "depth_path"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ValueError(
                        f"Frame metadata line {line_number} requires {field}"
                    )
            for field in ("host_received_timestamp_ns", "host_wall_timestamp_ns"):
                timestamp = record.get(field)
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, int)
                    or timestamp <= 0
                ):
                    raise ValueError(
                        f"Frame metadata line {line_number} requires positive {field}"
                    )
            frame_id = str(record.get("frame_id") or "")
            if not frame_id:
                raise ValueError(
                    f"Frame metadata line {line_number} is missing frame_id"
                )
            if frame_id in seen_frame_ids:
                raise ValueError(f"Duplicate frame_id in metadata: {frame_id}")
            seen_frame_ids.add(frame_id)
            records.append(record)
    if not records:
        raise ValueError(f"Current frame metadata is empty: {metadata_path}")
    return records


def _validate_source_sensor_identity(
    config: Mapping[str, Any],
    sensor_folder: Path,
    frame_records: Sequence[Mapping[str, Any]],
) -> None:
    configured = [
        sensor
        for sensor in config["capture"]["sensors"]
        if sensor_folder_name(
            str(sensor["sensor_type"]), str(sensor["device_id"])
        )
        == sensor_folder.name
    ]
    if len(configured) != 1:
        raise ValueError(
            f"Sensor folder {sensor_folder.name} is not uniquely bound in run_config.json"
        )
    expected = configured[0]
    expected_type = str(expected["sensor_type"])
    expected_device_id = str(expected["device_id"])
    identities = {
        (str(record.get("sensor_type")), str(record.get("sensor_id")))
        for record in frame_records
    }
    if len(identities) != 1:
        raise ValueError(
            f"Sensor folder {sensor_folder.name} mixes frame sensor identities"
        )
    actual_type, actual_device_id = next(iter(identities))
    if actual_type != expected_type or (
        not is_auto_device_id(expected_device_id)
        and actual_device_id != expected_device_id
    ):
        raise ValueError(
            f"Sensor folder {sensor_folder.name} frame identity does not match "
            "run_config.json"
        )


def load_robot_poses(run_root: str | Path) -> dict[str, Any]:
    path = Path(run_root) / RAW_ROBOT_EE_POSES
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Current robot poses are required: {path}")
    return _read_json(path)


def resolve_timestamp_pair(
    frame_timestamp_source: str,
    robot_timestamp_source: str | None,
) -> tuple[str, str]:
    """Resolve one explicit, clock-compatible frame/robot timestamp pair."""

    if frame_timestamp_source not in FRAME_TIMESTAMP_SOURCES:
        raise ValueError("timestamp_source must be host_received, host_wall, or sensor")
    if robot_timestamp_source is None:
        if frame_timestamp_source in {"host_received", "host_wall"}:
            robot_timestamp_source = frame_timestamp_source
        else:
            raise ValueError(
                f"timestamp_source={frame_timestamp_source!r} requires an explicit "
                "robot_timestamp_source"
            )
    if robot_timestamp_source not in ROBOT_TIMESTAMP_SOURCES:
        raise ValueError("robot_timestamp_source must be host_received or host_wall")
    if (frame_timestamp_source, robot_timestamp_source) not in (
        SUPPORTED_TIMESTAMP_PAIRS
    ):
        raise ValueError(
            "Frame/robot timestamp sources must share a clock domain; unsupported "
            f"pair: {frame_timestamp_source}->{robot_timestamp_source}"
        )
    return frame_timestamp_source, robot_timestamp_source


def robot_timestamp_ns(
    record: Mapping[str, Any], timestamp_source: str = "host_received"
) -> int:
    if timestamp_source == "host_received":
        value = record.get("host_received_timestamp_ns")
    elif timestamp_source == "host_wall":
        value = record.get("host_wall_timestamp_ns")
    else:
        raise ValueError("robot timestamp source must be host_received or host_wall")
    if value is None:
        raise ValueError(
            f"Robot pose is missing required {timestamp_source} timestamp evidence"
        )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"Robot pose {timestamp_source} timestamp must be a positive integer"
        )
    return value


def resolve_frame_timestamp(
    record: Mapping[str, Any], timestamp_source: str
) -> tuple[int | None, str | None, bool]:
    if timestamp_source == "host_received":
        value = record.get("host_received_timestamp_ns")
    elif timestamp_source == "host_wall":
        value = record.get("host_wall_timestamp_ns")
    elif timestamp_source == "sensor":
        value = record.get("sensor_timestamp_ns")
    else:
        raise ValueError("timestamp_source must be host_received, host_wall, or sensor")

    if value is None:
        return None, None, False
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"Frame {timestamp_source} timestamp must be a positive integer"
        )
    return value, timestamp_source, False


def frame_timestamp_ns(record: Mapping[str, Any], timestamp_source: str) -> int | None:
    """Return the explicitly selected current timestamp."""

    return resolve_frame_timestamp(record, timestamp_source)[0]


def validate_frame_timestamp_sequence(
    frame_records: Sequence[Mapping[str, Any]],
    timestamp_source: str,
) -> None:
    """Require selected timestamps to follow capture-record order exactly."""

    previous: int | None = None
    for index, record in enumerate(frame_records):
        timestamp, actual_source, fallback = resolve_frame_timestamp(
            record,
            timestamp_source,
        )
        if timestamp is None or actual_source != timestamp_source or fallback:
            raise ValueError(
                f"Frame metadata record {index} is missing required "
                f"{timestamp_source} timestamp evidence and cannot synchronize "
                "without fallback"
            )
        if previous is not None and timestamp <= previous:
            raise ValueError(
                f"Frame {timestamp_source} timestamps must strictly increase "
                "in frame_metadata.jsonl capture order"
            )
        previous = timestamp


def indexed_robot_poses(
    raw_poses: Mapping[str, Any],
    *,
    timestamp_source: str = "host_received",
    expected_run_id: str,
    expected_reference_frame_path: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw_poses, Mapping) or not raw_poses:
        raise ValueError("Raw robot pose artifact must be a non-empty JSON object")
    records = []
    run_ids: set[str] = set()
    reference_paths: set[str] = set()
    for key, value in raw_poses.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"Robot pose {key!r} must be a JSON object")
        record = dict(value)
        try:
            record["pose_index"] = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Robot pose key must be numeric: {key!r}") from exc
        if (
            not isinstance(record.get("motion"), str)
            or not record["motion"].strip()
        ):
            raise ValueError(f"Robot pose {key!r} is missing motion")
        pose = record.get("pose")
        if not isinstance(pose, Mapping):
            raise ValueError(f"Robot pose {key!r} is missing pose coordinates")
        for axis in ("X", "Y", "Z", "A", "B", "C"):
            coordinate = pose.get(axis)
            if (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, int | float)
                or not math.isfinite(float(coordinate))
            ):
                raise ValueError(
                    f"Robot pose {key!r} coordinate {axis} must be finite"
                )
        source_packet = record.get("source_packet")
        if (
            not isinstance(source_packet, Mapping)
            or source_packet.get("schema_version") != POSE_PACKET_SCHEMA_VERSION
            or source_packet.get("packet_kind") != "pose"
            or source_packet.get("from_frame") != "robot_flange"
            or source_packet.get("to_frame") != "template_base"
        ):
            raise ValueError(
                f"Robot pose {key!r} requires a current robot_pose.v1 source packet"
            )
        run_id = source_packet.get("run_id")
        reference_path = source_packet.get("sunrise_reference_frame_path")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"Robot pose {key!r} is missing run_id provenance")
        if not isinstance(reference_path, str) or not reference_path:
            raise ValueError(
                f"Robot pose {key!r} is missing Sunrise reference provenance"
            )
        if run_id != expected_run_id:
            raise ValueError(
                f"Robot pose {key!r} run_id does not match run_config.json"
            )
        if reference_path != expected_reference_frame_path:
            raise ValueError(
                f"Robot pose {key!r} Sunrise frame does not match run_config.json"
            )
        for field in ("sequence", "sequence_delta", "estimated_packets_lost"):
            packet_value = source_packet.get(field)
            if (
                isinstance(packet_value, bool)
                or not isinstance(packet_value, int)
                or packet_value < 0
            ):
                raise ValueError(
                    f"Robot pose {key!r} requires a non-negative integer "
                    f"source_packet.{field}"
                )
        for field in ("sender_monotonic_ns", "sender_wall_timestamp_ms"):
            packet_value = source_packet.get(field)
            if (
                isinstance(packet_value, bool)
                or not isinstance(packet_value, int)
                or packet_value < 0
            ):
                raise ValueError(
                    f"Robot pose {key!r} requires a non-negative integer "
                    f"source_packet.{field}"
                )
        sequence_delta = int(source_packet["sequence_delta"])
        packet_loss = int(source_packet["estimated_packets_lost"])
        if packet_loss != max(0, sequence_delta - 1):
            raise ValueError(
                f"Robot pose {key!r} packet-loss evidence is inconsistent with "
                "source_packet.sequence_delta"
            )
        run_ids.add(run_id)
        reference_paths.add(reference_path)
        for required_timestamp_source in ROBOT_TIMESTAMP_SOURCES:
            robot_timestamp_ns(record, required_timestamp_source)
        record["timestamp_ns"] = robot_timestamp_ns(record, timestamp_source)
        records.append(record)
    if len(run_ids) != 1 or len(reference_paths) != 1:
        raise ValueError("Robot pose stream mixes run or reference-frame provenance")
    stream_order = sorted(records, key=lambda item: int(item["pose_index"]))
    if [int(record["pose_index"]) for record in stream_order] != list(
        range(len(stream_order))
    ):
        raise ValueError("Robot pose keys must be unique and contiguous from zero")
    previous_sequence: int | None = None
    previous_timestamp_ns: int | None = None
    for record in stream_order:
        source_packet = record["source_packet"]
        sequence = int(source_packet["sequence"])
        sequence_delta = int(source_packet["sequence_delta"])
        if previous_sequence is None:
            if sequence_delta != 0:
                raise ValueError(
                    "The first retained robot pose must record sequence_delta=0"
                )
        elif (
            sequence <= previous_sequence
            or sequence_delta != sequence - previous_sequence
        ):
            raise ValueError(
                "Robot pose sequence and sequence_delta evidence are inconsistent"
            )
        timestamp_ns = int(record["timestamp_ns"])
        if (
            previous_timestamp_ns is not None
            and timestamp_ns <= previous_timestamp_ns
        ):
            raise ValueError(
                f"Robot pose {timestamp_source} timestamps must strictly increase "
                "in packet/pose order"
            )
        previous_sequence = sequence
        previous_timestamp_ns = timestamp_ns
    for record in stream_order[:-1]:
        if STREAM_END_SOURCE_PACKET in record:
            raise ValueError(
                "Stream-end packet evidence may appear only on the final robot pose"
            )
    terminal = stream_order[-1].get(STREAM_END_SOURCE_PACKET)
    if (
        not isinstance(terminal, Mapping)
        or terminal.get("schema_version") != POSE_PACKET_SCHEMA_VERSION
        or terminal.get("packet_kind") != "end"
        or terminal.get("run_id") != expected_run_id
        or terminal.get("from_frame") != "robot_flange"
        or terminal.get("to_frame") != "template_base"
        or terminal.get("sunrise_reference_frame_path") != expected_reference_frame_path
    ):
        raise ValueError("Final robot pose requires current stream-end packet evidence")
    for field in ("sender_monotonic_ns", "sender_wall_timestamp_ms"):
        value = terminal.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"Stream-end packet requires a non-negative integer {field}"
            )
    terminal_sequence = terminal.get("sequence")
    if (
        isinstance(terminal_sequence, bool)
        or not isinstance(terminal_sequence, int)
        or previous_sequence is None
        or terminal_sequence <= previous_sequence
    ):
        raise ValueError("Stream-end packet sequence must follow the final pose")
    terminal_delta = terminal.get("sequence_delta")
    terminal_loss = terminal.get("estimated_packets_lost")
    expected_terminal_delta = terminal_sequence - previous_sequence
    if (
        isinstance(terminal_delta, bool)
        or not isinstance(terminal_delta, int)
        or terminal_delta != expected_terminal_delta
        or isinstance(terminal_loss, bool)
        or not isinstance(terminal_loss, int)
        or terminal_loss != max(0, expected_terminal_delta - 1)
    ):
        raise ValueError("Stream-end packet loss evidence is inconsistent")
    return sorted(records, key=lambda item: item["timestamp_ns"])


def motion_intervals(
    robot_records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    for record in robot_records:
        motion = str(record["motion"])
        timestamp = int(record["timestamp_ns"])
        if not intervals or intervals[-1]["motion"] != motion:
            intervals.append(
                {
                    "motion": motion,
                    "min_timestamp_ns": timestamp,
                    "max_timestamp_ns": timestamp,
                    "pose_count": 1,
                }
            )
        else:
            intervals[-1]["max_timestamp_ns"] = timestamp
            intervals[-1]["pose_count"] += 1
    return intervals


def motion_for_timestamp(
    timestamp_ns: int, intervals: Iterable[Mapping[str, Any]]
) -> str | None:
    for interval in intervals:
        if (
            int(interval["min_timestamp_ns"])
            <= timestamp_ns
            <= int(interval["max_timestamp_ns"])
        ):
            return str(interval["motion"])
    return None


def robot_pose_packet_loss(
    robot_records: Iterable[Mapping[str, Any]],
) -> tuple[bool, int]:
    """Return whether packet-loss evidence is complete and its recorded total."""

    audited = True
    total = 0
    found = False
    for record in robot_records:
        source_packet = record.get("source_packet")
        if not isinstance(source_packet, Mapping):
            audited = False
            continue
        value = source_packet.get("estimated_packets_lost")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            audited = False
            continue
        found = True
        total += value
        terminal = record.get(STREAM_END_SOURCE_PACKET)
        if terminal is not None:
            if not isinstance(terminal, Mapping):
                audited = False
                continue
            terminal_loss = terminal.get("estimated_packets_lost")
            if (
                isinstance(terminal_loss, bool)
                or not isinstance(terminal_loss, int)
                or terminal_loss < 0
            ):
                audited = False
                continue
            total += terminal_loss
    return audited and found, total


def closest_robot_pose(
    timestamp_ns: int, robot_records: list[dict[str, Any]]
) -> dict[str, Any]:
    return min(
        robot_records,
        key=lambda record: abs(int(record["timestamp_ns"]) - timestamp_ns),
    )


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _sensor_sync_keys(sensor_folder_name: str) -> tuple[str, ...]:
    return (sensor_folder_name,)


def resolve_sync_delta_ms(
    sensor_folder: str | Path, sync_delta: int | float | Mapping[str, Any] | None
) -> float:
    value: object = 100.0
    if sync_delta is not None:
        if isinstance(sync_delta, bool):
            raise ValueError(
                "Synchronization delta must be a finite number, not a boolean"
            )
        if isinstance(sync_delta, int | float):
            value = sync_delta
        elif isinstance(sync_delta, Mapping):
            for key in _sensor_sync_keys(Path(sensor_folder).name):
                if key in sync_delta:
                    value = sync_delta[key]
                    break
        else:
            raise ValueError("Synchronization delta must be a number or sensor mapping")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Synchronization delta must be numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError("Synchronization delta must be finite")
    return result


def resolve_max_nearest_pose_delta_ms(
    value: int | float | None,
) -> float | None:
    """Validate an optional strict nearest-pose matching threshold."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            "Maximum nearest-pose delta must be a finite non-negative number, "
            "not a boolean"
        )
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Maximum nearest-pose delta must be numeric: {value!r}"
        ) from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(
            "Maximum nearest-pose delta must be finite and greater than or equal to 0"
        )
    return result


def _copy_frame_pair(
    *,
    sensor_folder: Path,
    output_folder: Path,
    frame_metadata: Mapping[str, Any],
    output_frame_id: str,
) -> tuple[Path, Path]:
    source_rgb = _resolve_source_frame_path(
        sensor_folder, frame_metadata.get("rgb_path"), RGB_DIR
    )
    source_depth = _resolve_source_frame_path(
        sensor_folder, frame_metadata.get("depth_path"), DEPTH_DIR
    )
    output_rgb = output_folder / RGB_DIR / output_frame_id
    output_depth = output_folder / DEPTH_DIR / output_frame_id
    output_rgb.parent.mkdir(parents=True, exist_ok=True)
    output_depth.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_rgb, output_rgb)
    shutil.copy2(source_depth, output_depth)
    return output_rgb, output_depth


def _resolve_source_frame_path(
    sensor_folder: Path, value: Any, expected_dir: str
) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Frame metadata is missing {expected_dir} path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"Frame path must be relative: {value}")
    source_path = sensor_folder / relative
    _reject_symlink_components(source_path, label="Raw frame path")
    resolved = source_path.resolve()
    sensor_resolved = sensor_folder.resolve()
    try:
        descendant = resolved.relative_to(sensor_resolved)
    except ValueError as exc:
        raise ValueError(f"Frame path escapes sensor folder: {value}") from exc
    if not descendant.parts or descendant.parts[0] != expected_dir:
        raise ValueError(f"Frame path must be below {expected_dir}/: {value}")
    if source_path.is_symlink() or not resolved.is_file():
        raise FileNotFoundError(f"Frame file does not exist: {resolved}")
    return resolved


def copy_sensor_metadata_artifacts(
    sensor_folder: Path, output_folder: Path
) -> list[str]:
    copied = []
    for artifact in CURRENT_SENSOR_METADATA_ARTIFACTS:
        if artifact == FRAME_METADATA_JSONL:
            continue
        source = sensor_folder / artifact
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"Current camera sidecar is required: {source}")
        destination = output_folder / artifact
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(artifact)
    return copied


def _delta_stats(deltas_ns: list[int]) -> dict[str, float | int | None]:
    if not deltas_ns:
        return {
            "mean_abs_nearest_pose_delta_ns": None,
            "max_abs_nearest_pose_delta_ns": None,
        }
    abs_deltas = [abs(delta) for delta in deltas_ns]
    return {
        "mean_abs_nearest_pose_delta_ns": mean(abs_deltas),
        "max_abs_nearest_pose_delta_ns": max(abs_deltas),
    }


def _prepare_sensor_folder(
    sensor_folder: str | Path,
    *,
    run_root: str | Path | None = None,
    output_root: str | Path | None = None,
    sync_delta: int | float | Mapping[str, Any] | None = None,
    timestamp_source: str = "host_received",
    robot_timestamp_source: str | None = None,
    copy_files: bool = True,
    max_nearest_pose_delta_ms: int | float | None = None,
    required_frame_timestamp_domain: str | None = None,
    timestamp_fallback_allowed: bool = False,
    calibration_sync: Mapping[str, Any] | None = None,
    raw_robot_poses: Mapping[str, Any] | None = None,
    sync_generation_id: str | None = None,
    diagnostic_unmanaged: bool = False,
) -> _PreparedSync:
    sensor_path = Path(sensor_folder)
    run_path = Path(run_root) if run_root is not None else sensor_path.parent
    sensor_path = _validate_sensor_source_boundary(run_path, sensor_path)
    config = load_run_config_for_run_root(run_path)
    expected_reference_path = configured_sunrise_reference_frame_path(config)
    if expected_reference_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        raise ValueError(
            "Current synchronization requires the canonical PoseTemplateBase frame"
        )
    timestamp_source, resolved_robot_timestamp_source = resolve_timestamp_pair(
        timestamp_source, robot_timestamp_source
    )
    nearest_pose_threshold_ms = resolve_max_nearest_pose_delta_ms(
        max_nearest_pose_delta_ms
    )
    nearest_pose_threshold_ns = (
        int(nearest_pose_threshold_ms * 1_000_000)
        if nearest_pose_threshold_ms is not None
        else None
    )
    if required_frame_timestamp_domain is not None and (
        not isinstance(required_frame_timestamp_domain, str)
        or not required_frame_timestamp_domain.strip()
    ):
        raise ValueError(
            "Required frame timestamp domain must be a non-empty string or null"
        )
    if timestamp_fallback_allowed is not False:
        raise ValueError("Current synchronization forbids timestamp fallback")
    if calibration_sync is not None and not isinstance(calibration_sync, Mapping):
        raise ValueError("calibration_sync provenance must be an object")
    if sync_generation_id is None:
        sync_generation_id = uuid.uuid4().hex
    elif not isinstance(sync_generation_id, str) or not sync_generation_id.strip():
        raise ValueError("sync_generation_id must be a non-empty string")
    output_base = (
        Path(output_root)
        if output_root is not None
        else run_path / PROCESSED_DIR / SYNCHRONIZED_DIR
    )
    output_base = validate_sync_output_boundary(
        run_path,
        sensor_path,
        output_base,
        diagnostic_unmanaged=diagnostic_unmanaged,
    )
    output_folder = output_base / sensor_path.name
    staging_folder = output_base / f".{sensor_path.name}.{uuid.uuid4().hex}.tmp"

    try:
        frame_records = load_frame_metadata(sensor_path)
        _validate_source_sensor_identity(config, sensor_path, frame_records)
        input_sensor_artifacts = _source_sensor_artifact_evidence(
            sensor_path,
            frame_records,
        )
        input_frame_metadata = _file_evidence(
            sensor_path / FRAME_METADATA_JSONL,
            run_root=run_path,
        )
        if not frame_records:
            raise ValueError(f"No frame metadata or RGB frames found in {sensor_path}")
        if raw_robot_poses is None:
            selected_robot_poses = load_robot_poses(run_path)
            input_robot_poses = _file_evidence(
                run_path / RAW_ROBOT_EE_POSES,
                run_root=run_path,
            )
        else:
            selected_robot_poses = raw_robot_poses
            input_robot_poses = _snapshot_evidence(raw_robot_poses)
        robot_records = indexed_robot_poses(
            selected_robot_poses,
            timestamp_source=resolved_robot_timestamp_source,
            expected_run_id=str(config["run_id"]),
            expected_reference_frame_path=expected_reference_path,
        )
        intervals = motion_intervals(robot_records)
        pose_packet_loss_audited, pose_packet_loss_count = robot_pose_packet_loss(
            robot_records
        )
        sensor_sync_delta_ms = resolve_sync_delta_ms(sensor_path, sync_delta)
        sync_delta_ns = int(sensor_sync_delta_ms * 1_000_000)

        resolved_records: list[tuple[int | None, str | None, bool, dict[str, Any]]] = []
        validate_frame_timestamp_sequence(frame_records, timestamp_source)
        for frame_record in frame_records:
            _resolve_source_frame_path(
                sensor_path, frame_record.get("rgb_path"), RGB_DIR
            )
            _resolve_source_frame_path(
                sensor_path, frame_record.get("depth_path"), DEPTH_DIR
            )
            if (
                required_frame_timestamp_domain is not None
                and frame_record.get("color_timestamp_domain")
                != required_frame_timestamp_domain
            ):
                raise ValueError(
                    f"Frame {frame_record.get('frame_id')!r} in "
                    f"{sensor_path.name} has color timestamp domain "
                    f"{frame_record.get('color_timestamp_domain')!r}; required "
                    f"{required_frame_timestamp_domain!r}"
                )
            resolved = resolve_frame_timestamp(frame_record, timestamp_source)
            if resolved[0] is None or resolved[1] != timestamp_source or resolved[2]:
                raise ValueError(
                    f"Frame {frame_record.get('frame_id')!r} in "
                    f"{sensor_path.name} cannot prove required "
                    f"{timestamp_source!r} timing without fallback"
                )
            resolved_records.append((*resolved, frame_record))
        resolved_records.sort(
            key=lambda item: (item[0] is None, item[0] if item[0] is not None else 0)
        )

        output_base.mkdir(parents=True, exist_ok=True)
        staging_folder.mkdir(parents=False, exist_ok=False)

        matched: dict[str, Any] = {}
        derived_metadata: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        nearest_deltas_ns: list[int] = []
        timestamp_source_counts: dict[str, int] = {}
        timestamp_fallback_count = 0
        timestamp_missing_count = 0
        incompatible_timestamp_pair_count = 0
        outside_motion_interval_frame_count = 0
        eligible_in_motion_frames = 0
        nearest_pose_delta_rejection_count = 0
        previous_frame_timestamp_ns: int | None = None
        output_counter = 0
        copied_metadata_artifacts = (
            copy_sensor_metadata_artifacts(sensor_path, staging_folder)
            if copy_files
            else []
        )

        for timestamp_ns, actual_source, fallback, frame_record in resolved_records:
            if timestamp_ns is None or actual_source is None:
                raise ValueError(
                    f"Frame {frame_record.get('frame_id')!r} is missing "
                    f"{timestamp_source} timestamp evidence"
                )
            timestamp_source_counts[actual_source] = (
                timestamp_source_counts.get(actual_source, 0) + 1
            )
            if fallback:
                timestamp_fallback_count += 1
            if (actual_source, resolved_robot_timestamp_source) not in (
                SUPPORTED_TIMESTAMP_PAIRS
            ):
                incompatible_timestamp_pair_count += 1
                dropped.append(
                    {
                        "frame_id": frame_record.get("frame_id"),
                        "timestamp_ns": timestamp_ns,
                        "timestamp_source": actual_source,
                        "robot_timestamp_source": (resolved_robot_timestamp_source),
                        "reason": "frame/robot timestamp fallback clocks are incompatible",
                    }
                )
                continue

            delayed_timestamp_ns = timestamp_ns - sync_delta_ns
            motion = motion_for_timestamp(delayed_timestamp_ns, intervals)
            if motion is None:
                outside_motion_interval_frame_count += 1
                dropped.append(
                    {
                        "frame_id": frame_record.get("frame_id"),
                        "timestamp_ns": timestamp_ns,
                        "timestamp_source": actual_source,
                        "robot_timestamp_source": (resolved_robot_timestamp_source),
                        "delayed_timestamp_ns": delayed_timestamp_ns,
                        "reason": "outside robot motion intervals",
                    }
                )
                continue

            eligible_in_motion_frames += 1
            closest_pose = closest_robot_pose(delayed_timestamp_ns, robot_records)
            nearest_delta_ns = int(closest_pose["timestamp_ns"]) - delayed_timestamp_ns
            if (
                nearest_pose_threshold_ns is not None
                and abs(nearest_delta_ns) > nearest_pose_threshold_ns
            ):
                nearest_pose_delta_rejection_count += 1
                dropped.append(
                    {
                        "frame_id": frame_record.get("frame_id"),
                        "timestamp_ns": timestamp_ns,
                        "timestamp_source": actual_source,
                        "robot_timestamp_source": (resolved_robot_timestamp_source),
                        "delayed_timestamp_ns": delayed_timestamp_ns,
                        "motion": motion,
                        "matched_robot_pose_index": closest_pose["pose_index"],
                        "robot_timestamp_ns": int(closest_pose["timestamp_ns"]),
                        "nearest_robot_delta_ns": nearest_delta_ns,
                        "abs_nearest_robot_delta_ns": abs(nearest_delta_ns),
                        "max_nearest_pose_delta_ms": nearest_pose_threshold_ms,
                        "max_nearest_pose_delta_ns": nearest_pose_threshold_ns,
                        "reason": "nearest robot pose delta exceeds threshold",
                    }
                )
                continue
            nearest_deltas_ns.append(nearest_delta_ns)
            frame_delta_ns = (
                0
                if previous_frame_timestamp_ns is None
                else timestamp_ns - previous_frame_timestamp_ns
            )
            previous_frame_timestamp_ns = timestamp_ns

            output_frame_id = f"{output_counter:06d}.png"
            if copy_files:
                _copy_frame_pair(
                    sensor_folder=sensor_path,
                    output_folder=staging_folder,
                    frame_metadata=frame_record,
                    output_frame_id=output_frame_id,
                )
            synchronized_rgb = _relative_path(
                output_folder / RGB_DIR / output_frame_id, run_path
            )
            synchronized_depth = _relative_path(
                output_folder / DEPTH_DIR / output_frame_id, run_path
            )

            matched_record = {
                "motion": motion,
                "image_frame": timestamp_ns // 1_000_000,
                "image_timestamp_ns": timestamp_ns,
                "timestamp_source": actual_source,
                "timestamp_fallback": fallback,
                "robot_timestamp_source": resolved_robot_timestamp_source,
                "sensor_timestamp_ns": frame_record.get("sensor_timestamp_ns"),
                "host_received_timestamp_ns": frame_record.get(
                    "host_received_timestamp_ns"
                ),
                "host_wall_timestamp_ns": frame_record.get("host_wall_timestamp_ns"),
                "delayed_frame": delayed_timestamp_ns // 1_000_000,
                "delayed_timestamp_ns": delayed_timestamp_ns,
                "frame_delta": frame_delta_ns // 1_000_000,
                "frame_delta_ns": frame_delta_ns,
                "robot_frame": int(closest_pose["timestamp_ns"]) // 1_000_000,
                "robot_timestamp_ns": int(closest_pose["timestamp_ns"]),
                "nearest_robot_delta_ns": nearest_delta_ns,
                "matched_robot_pose_index": closest_pose["pose_index"],
                "source_frame_id": frame_record.get("frame_id"),
                "source_rgb": frame_record.get("rgb_path"),
                "source_depth": frame_record.get("depth_path"),
                "synchronized_rgb": synchronized_rgb,
                "synchronized_depth": synchronized_depth,
                "robot_ee_pose": closest_pose["pose"],
            }
            source_packet = closest_pose.get("source_packet")
            if isinstance(source_packet, Mapping):
                matched_record["source_packet"] = dict(source_packet)
            matched[output_frame_id] = matched_record
            derived_record = dict(frame_record)
            derived_record.update(
                {
                    "frame_index": output_counter,
                    "frame_id": output_frame_id,
                    "rgb_path": f"{RGB_DIR}/{output_frame_id}",
                    "depth_path": f"{DEPTH_DIR}/{output_frame_id}",
                    "source_frame_index": frame_record.get("frame_index"),
                    "source_frame_id": frame_record.get("frame_id"),
                    "source_rgb_path": frame_record.get("rgb_path"),
                    "source_depth_path": frame_record.get("depth_path"),
                    "sync_requested_timestamp_source": timestamp_source,
                    "sync_timestamp_source": actual_source,
                    "sync_robot_timestamp_source": (resolved_robot_timestamp_source),
                    "sync_timestamp_fallback": fallback,
                    "sync_timestamp_ns": timestamp_ns,
                    "sync_delta_ms": sensor_sync_delta_ms,
                    "matched_robot_pose_index": closest_pose["pose_index"],
                    "nearest_robot_delta_ns": nearest_delta_ns,
                    "motion": motion,
                }
            )
            derived_metadata.append(derived_record)
            output_counter += 1

        if copy_files:
            metadata_text = "".join(
                json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
                for record in derived_metadata
            )
            atomic_write_text(staging_folder / FRAME_METADATA_JSONL, metadata_text)

        _write_json(staging_folder / MATCH_ROBOT_EE_POSES, matched)
        current_frame_metadata = _file_evidence(
            sensor_path / FRAME_METADATA_JSONL,
            run_root=run_path,
        )
        if current_frame_metadata != input_frame_metadata:
            raise RuntimeError(
                f"Frame metadata changed during synchronization: {sensor_path}"
            )
        if (
            _source_sensor_artifact_evidence(sensor_path, frame_records)
            != input_sensor_artifacts
        ):
            raise RuntimeError(
                f"Raw RGB-D or camera sidecars changed during synchronization: {sensor_path}"
            )
        if raw_robot_poses is None:
            current_robot_poses = _file_evidence(
                run_path / RAW_ROBOT_EE_POSES,
                run_root=run_path,
            )
            if current_robot_poses != input_robot_poses:
                raise RuntimeError(
                    f"Robot pose evidence changed during synchronization: {run_path}"
                )
        output_evidence: dict[str, Any] = {
            MATCH_ROBOT_EE_POSES: _file_evidence(
                staging_folder / MATCH_ROBOT_EE_POSES,
                run_root=run_path,
                recorded_path=output_folder / MATCH_ROBOT_EE_POSES,
            )
        }
        if copy_files:
            output_evidence[FRAME_METADATA_JSONL] = _file_evidence(
                staging_folder / FRAME_METADATA_JSONL,
                run_root=run_path,
                recorded_path=output_folder / FRAME_METADATA_JSONL,
            )
        output_evidence["artifact_set"] = _synchronization_owned_artifact_evidence(
            staging_folder,
            output_contract="rgbd_copy" if copy_files else "pairing_only",
        )
        in_motion_exclusion_count = eligible_in_motion_frames - len(matched)
        unexplained_in_motion_exclusion_count = (
            in_motion_exclusion_count - nearest_pose_delta_rejection_count
        )
        report = {
            "schema_version": SCHEMA_VERSION,
            "sync_generation_id": sync_generation_id,
            "output_contract": "rgbd_copy" if copy_files else "pairing_only",
            "input_evidence": {
                FRAME_METADATA_JSONL: input_frame_metadata,
                RAW_ROBOT_EE_POSES: input_robot_poses,
                "sensor_artifact_set": input_sensor_artifacts,
            },
            "output_evidence": output_evidence,
            "sensor_folder": _relative_path(sensor_path, run_path),
            "output_folder": _relative_path(output_folder, run_path),
            "requested_timestamp_source": timestamp_source,
            "requested_frame_timestamp_source": timestamp_source,
            "timestamp_source": (
                timestamp_source if timestamp_fallback_count == 0 else "mixed"
            ),
            "frame_timestamp_source": (
                timestamp_source if timestamp_fallback_count == 0 else "mixed"
            ),
            "robot_timestamp_source": resolved_robot_timestamp_source,
            "timestamp_pair": {
                "frame_timestamp_source": (
                    timestamp_source if timestamp_fallback_count == 0 else "mixed"
                ),
                "requested_frame_timestamp_source": timestamp_source,
                "robot_timestamp_source": resolved_robot_timestamp_source,
            },
            "timestamp_pair_provenance_audited": True,
            "timestamp_source_counts": timestamp_source_counts,
            "timestamp_fallback_count": timestamp_fallback_count,
            "timestamp_missing_count": timestamp_missing_count,
            "incompatible_timestamp_pair_count": (incompatible_timestamp_pair_count),
            "sync_delta_ms": sensor_sync_delta_ms,
            "max_nearest_pose_delta_ms": nearest_pose_threshold_ms,
            "required_frame_timestamp_domain": required_frame_timestamp_domain,
            "timestamp_fallback_allowed": timestamp_fallback_allowed,
            "calibration_sync": (
                dict(calibration_sync) if calibration_sync is not None else None
            ),
            "nearest_pose_delta_rejection_count": (nearest_pose_delta_rejection_count),
            "total_frames": len(frame_records),
            "matched_frames": len(matched),
            "dropped_frames": len(dropped),
            "outside_motion_interval_frame_count": (
                outside_motion_interval_frame_count
            ),
            "eligible_in_motion_frames": eligible_in_motion_frames,
            "matched_eligible_frames": len(matched),
            "eligible_motion_coverage": (
                len(matched) / eligible_in_motion_frames
                if eligible_in_motion_frames
                else 0.0
            ),
            "in_motion_exclusion_count": in_motion_exclusion_count,
            "unexplained_in_motion_exclusion_count": (
                unexplained_in_motion_exclusion_count
            ),
            "robot_pose_packet_loss_audited": pose_packet_loss_audited,
            "robot_pose_packet_loss_count": (
                pose_packet_loss_count if pose_packet_loss_audited else None
            ),
            "motion_intervals": intervals,
            "dropped": dropped,
            "copied_metadata_artifacts": copied_metadata_artifacts,
            **_delta_stats(nearest_deltas_ns),
        }
        _write_json(staging_folder / SYNC_REPORT, report)
    except BaseException:
        if staging_folder.exists():
            shutil.rmtree(staging_folder)
        raise

    matched_path = output_folder / MATCH_ROBOT_EE_POSES
    report_path = output_folder / SYNC_REPORT

    return _PreparedSync(
        result=SyncResult(
            sensor_folder=sensor_path.as_posix(),
            output_folder=output_folder.as_posix(),
            matched_poses_path=matched_path.as_posix(),
            report_path=report_path.as_posix(),
            total_frames=len(frame_records),
            matched_frames=len(matched),
            dropped_frames=len(dropped),
        ),
        staging_folder=staging_folder,
        output_folder=output_folder,
    )


def synchronize_sensor_folder(
    sensor_folder: str | Path,
    *,
    run_root: str | Path | None = None,
    output_root: str | Path | None = None,
    sync_delta: int | float | Mapping[str, Any] | None = None,
    timestamp_source: str = "host_received",
    robot_timestamp_source: str | None = None,
    copy_files: bool = True,
    max_nearest_pose_delta_ms: int | float | None = None,
    required_frame_timestamp_domain: str | None = None,
    timestamp_fallback_allowed: bool = False,
    calibration_sync: Mapping[str, Any] | None = None,
    raw_robot_poses: Mapping[str, Any] | None = None,
    sync_generation_id: str | None = None,
    diagnostic_unmanaged: bool = False,
) -> SyncResult:
    """Synchronize one sensor.

    Managed publication is valid only when this is the run's sole enabled
    sensor; multi-sensor managed runs must use :func:`synchronize_sensor_batch`.
    """

    sensor_path = Path(sensor_folder)
    run_path = Path(run_root) if run_root is not None else sensor_path.parent
    results = synchronize_sensor_batch(
        run_path,
        [
            SensorSyncSettings(
                sensor_folder=sensor_path,
                sync_delta=sync_delta,
                timestamp_source=timestamp_source,
                robot_timestamp_source=robot_timestamp_source,
                max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
                required_frame_timestamp_domain=required_frame_timestamp_domain,
                timestamp_fallback_allowed=timestamp_fallback_allowed,
                calibration_sync=calibration_sync,
                raw_robot_poses=raw_robot_poses,
            )
        ],
        output_root=output_root,
        copy_files=copy_files,
        sync_generation_id=sync_generation_id,
        diagnostic_unmanaged=diagnostic_unmanaged,
    )
    return results[0]


def sync_result_artifacts(result: SyncResult) -> dict[str, str]:
    result_dict = asdict(result)
    return {
        MATCH_ROBOT_EE_POSES: result_dict["matched_poses_path"],
        SYNC_REPORT: result_dict["report_path"],
    }


def _contained_sensor_folders(
    run_path: Path,
    values: Sequence[str | Path],
) -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()
    seen_names: set[str] = set()
    for value in values:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = run_path / candidate
        _reject_symlink_components(candidate, label="Explicit sensor folder")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(run_path.resolve())
        except ValueError as exc:
            raise ValueError(
                f"Explicit sensor folder must remain below the run root: {value}"
            ) from exc
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError(
                f"Explicit sensor folder must be a regular directory: {value}"
            )
        if resolved in seen:
            raise ValueError(f"Explicit sensor folder is duplicated: {value}")
        if resolved.name in seen_names:
            raise ValueError(
                "Explicit sensor folders must have unique destination names: "
                f"{resolved.name}"
            )
        seen.add(resolved)
        seen_names.add(resolved.name)
        selected.append(resolved)
    return selected


def _validate_managed_sensor_coverage(
    run_path: Path,
    selected: Sequence[Path],
) -> None:
    expected_names = set(enabled_sensor_folder_names(run_path))
    expected_paths = {
        (run_path / folder_name).resolve() for folder_name in expected_names
    }
    actual_paths = set(selected)
    if actual_paths != expected_paths or len(actual_paths) != len(selected):
        raise ValueError(
            "Managed synchronization must publish every enabled sensor exactly; "
            f"expected={sorted(expected_names)}, "
            f"selected={sorted(path.relative_to(run_path.resolve()).as_posix() for path in actual_paths)}"
        )


def _publish_prepared_batch(
    run_path: Path,
    prepared: Sequence[_PreparedSync],
    *,
    diagnostic_unmanaged: bool,
) -> None:
    if diagnostic_unmanaged:
        replace_directories(
            (item.staging_folder, item.output_folder) for item in prepared
        )
        return

    canonical_root = run_path / PROCESSED_DIR / SYNCHRONIZED_DIR
    generation_staging = canonical_root.with_name(
        f".{canonical_root.name}.{uuid.uuid4().hex}.tmp"
    )
    generation_staging.mkdir(parents=False, exist_ok=False)
    try:
        for item in prepared:
            os.replace(
                item.staging_folder,
                generation_staging / item.output_folder.name,
            )
        # Replacing the owned root prevents readers from ever accepting a
        # mixture of old and new sensor generations. ``replace_directory``
        # restores the prior complete root if promotion raises.
        replace_directory(generation_staging, canonical_root)
    except BaseException:
        if generation_staging.exists():
            shutil.rmtree(generation_staging)
        raise


def _verify_prepared_inputs_unchanged(
    run_path: Path,
    prepared: _PreparedSync,
    *,
    raw_robot_poses: Mapping[str, Any] | None,
) -> None:
    report = _read_json(prepared.staging_folder / SYNC_REPORT)
    input_evidence = report.get("input_evidence")
    if not isinstance(input_evidence, Mapping):
        raise RuntimeError("Prepared synchronization report lost its input evidence")
    sensor_path = Path(prepared.result.sensor_folder)
    if not sensor_path.is_absolute():
        sensor_path = run_path / sensor_path
    frame_records = load_frame_metadata(sensor_path)
    if input_evidence.get(FRAME_METADATA_JSONL) != _file_evidence(
        sensor_path / FRAME_METADATA_JSONL,
        run_root=run_path,
    ):
        raise RuntimeError(
            f"Frame metadata changed before synchronization publish: {sensor_path}"
        )
    if input_evidence.get("sensor_artifact_set") != _source_sensor_artifact_evidence(
        sensor_path,
        frame_records,
    ):
        raise RuntimeError(
            "Raw RGB-D or camera sidecars changed before synchronization publish: "
            f"{sensor_path}"
        )
    current_robot_evidence = (
        _file_evidence(run_path / RAW_ROBOT_EE_POSES, run_root=run_path)
        if raw_robot_poses is None
        else _snapshot_evidence(raw_robot_poses)
    )
    if input_evidence.get(RAW_ROBOT_EE_POSES) != current_robot_evidence:
        raise RuntimeError(
            f"Robot pose evidence changed before synchronization publish: {run_path}"
        )


def synchronize_sensor_batch(
    run_root: str | Path,
    settings: Sequence[SensorSyncSettings],
    *,
    output_root: str | Path | None = None,
    copy_files: bool = True,
    sync_generation_id: str | None = None,
    diagnostic_unmanaged: bool = False,
) -> list[SyncResult]:
    """Stage every requested sensor, then publish the generation as one batch."""

    run_path = Path(run_root)
    requested = list(settings)
    if not all(isinstance(item, SensorSyncSettings) for item in requested):
        raise TypeError("settings must contain only SensorSyncSettings values")
    generation_id = (
        uuid.uuid4().hex if sync_generation_id is None else sync_generation_id
    )
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("sync_generation_id must be a non-empty string")
    selected = _contained_sensor_folders(
        run_path,
        [item.sensor_folder for item in requested],
    )
    if not diagnostic_unmanaged:
        _validate_managed_sensor_coverage(run_path, selected)
    if not selected:
        return []

    prepared: list[_PreparedSync] = []
    with dataset_processing_lock(run_path):
        try:
            for sensor_folder, item in zip(selected, requested, strict=True):
                prepared.append(
                    _prepare_sensor_folder(
                        sensor_folder,
                        run_root=run_path,
                        output_root=output_root,
                        sync_delta=item.sync_delta,
                        timestamp_source=item.timestamp_source,
                        robot_timestamp_source=item.robot_timestamp_source,
                        copy_files=copy_files,
                        max_nearest_pose_delta_ms=item.max_nearest_pose_delta_ms,
                        required_frame_timestamp_domain=(
                            item.required_frame_timestamp_domain
                        ),
                        timestamp_fallback_allowed=item.timestamp_fallback_allowed,
                        calibration_sync=item.calibration_sync,
                        raw_robot_poses=item.raw_robot_poses,
                        sync_generation_id=generation_id,
                        diagnostic_unmanaged=diagnostic_unmanaged,
                    )
                )
            for item, sensor_settings in zip(prepared, requested, strict=True):
                _verify_prepared_inputs_unchanged(
                    run_path,
                    item,
                    raw_robot_poses=sensor_settings.raw_robot_poses,
                )
            _publish_prepared_batch(
                run_path,
                prepared,
                diagnostic_unmanaged=diagnostic_unmanaged,
            )
        except BaseException:
            for item in prepared:
                if item.staging_folder.exists():
                    shutil.rmtree(item.staging_folder)
            raise
    return [item.result for item in prepared]


def synchronize_run(
    run_root: str | Path,
    *,
    sensor_folders: Sequence[str | Path] | None = None,
    output_root: str | Path | None = None,
    sync_delta: int | float | Mapping[str, Any] | None = None,
    timestamp_source: str = "host_received",
    robot_timestamp_source: str | None = None,
    copy_files: bool = True,
    max_nearest_pose_delta_ms: int | float | None = None,
    required_frame_timestamp_domain: str | None = None,
    timestamp_fallback_allowed: bool = False,
    calibration_sync: Mapping[str, Any] | None = None,
    raw_robot_poses: Mapping[str, Any] | None = None,
    sync_generation_id: str | None = None,
    diagnostic_unmanaged: bool = False,
) -> list[SyncResult]:
    """Synchronize discovered sensors or an explicit contained subset.

    Omitting ``sensor_folders`` preserves the original run-wide behavior.
    Supplying it lets intent-level orchestration reuse the stage without
    allowing an unselected or out-of-run folder to enter the calculation.
    """

    run_path = Path(run_root)
    if sensor_folders is None:
        selected = filter_enabled_sensor_folders(
            run_path,
            (
                run_path / str(sensor_record["folder"])
                for sensor_record in discover_sensor_records(run_path)
            ),
        )
    else:
        selected = _contained_sensor_folders(run_path, sensor_folders)

    if raw_robot_poses is not None and len(selected) != 1:
        raise ValueError(
            "A raw_robot_poses override requires exactly one selected sensor folder"
        )

    return synchronize_sensor_batch(
        run_path,
        [
            SensorSyncSettings(
                sensor_folder=sensor_folder,
                sync_delta=sync_delta,
                timestamp_source=timestamp_source,
                robot_timestamp_source=robot_timestamp_source,
                max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
                required_frame_timestamp_domain=required_frame_timestamp_domain,
                timestamp_fallback_allowed=timestamp_fallback_allowed,
                calibration_sync=calibration_sync,
                raw_robot_poses=raw_robot_poses,
            )
            for sensor_folder in selected
        ],
        output_root=output_root,
        copy_files=copy_files,
        sync_generation_id=sync_generation_id,
        diagnostic_unmanaged=diagnostic_unmanaged,
    )
