"""Non-destructive frame/robot-pose synchronization.

It consumes current frame and robot-pose metadata, copies synchronized frames
into a derived folder, and keeps raw capture folders unchanged.
"""

from __future__ import annotations

import json
import math
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from posetestbot.io.atomic import (
    atomic_write_json,
    atomic_write_text,
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
from posetestbot.pipeline.sensor_selection import filter_enabled_sensor_folders
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    configured_sunrise_reference_frame_path,
)
from posetestbot.sensors.contracts import SensorType


SCHEMA_VERSION = "sync_report.v3"
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
    return int(value)


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

    actual_source = timestamp_source if value is not None else None
    return (
        (int(value), actual_source, False) if value is not None else (None, None, False)
    )


def frame_timestamp_ns(record: Mapping[str, Any], timestamp_source: str) -> int | None:
    """Return the explicitly selected current timestamp."""

    return resolve_frame_timestamp(record, timestamp_source)[0]


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
        if not record.get("motion"):
            raise ValueError(f"Robot pose {key!r} is missing motion")
        if not isinstance(record.get("pose"), Mapping):
            raise ValueError(f"Robot pose {key!r} is missing pose coordinates")
        source_packet = record.get("source_packet")
        if (
            not isinstance(source_packet, Mapping)
            or source_packet.get("schema_version") != "robot_pose.v1"
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
        run_ids.add(run_id)
        reference_paths.add(reference_path)
        record["timestamp_ns"] = robot_timestamp_ns(record, timestamp_source)
        records.append(record)
    if len(run_ids) != 1 or len(reference_paths) != 1:
        raise ValueError("Robot pose stream mixes run or reference-frame provenance")
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
    resolved = (sensor_folder / relative).resolve()
    sensor_resolved = sensor_folder.resolve()
    try:
        descendant = resolved.relative_to(sensor_resolved)
    except ValueError as exc:
        raise ValueError(f"Frame path escapes sensor folder: {value}") from exc
    if not descendant.parts or descendant.parts[0] != expected_dir:
        raise ValueError(f"Frame path must be below {expected_dir}/: {value}")
    if not resolved.is_file():
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
) -> SyncResult:
    sensor_path = Path(sensor_folder)
    run_path = Path(run_root) if run_root is not None else sensor_path.parent
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
    output_base = (
        Path(output_root)
        if output_root is not None
        else run_path / PROCESSED_DIR / SYNCHRONIZED_DIR
    )
    output_folder = output_base / sensor_path.name
    output_base.mkdir(parents=True, exist_ok=True)
    staging_folder = output_base / f".{sensor_path.name}.{uuid.uuid4().hex}.tmp"
    staging_folder.mkdir(parents=False, exist_ok=False)

    try:
        frame_records = load_frame_metadata(sensor_path)
        if not frame_records:
            raise ValueError(f"No frame metadata or RGB frames found in {sensor_path}")
        if raw_robot_poses is None:
            selected_robot_poses = load_robot_poses(run_path)
        else:
            selected_robot_poses = raw_robot_poses
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
        in_motion_exclusion_count = eligible_in_motion_frames - len(matched)
        unexplained_in_motion_exclusion_count = (
            in_motion_exclusion_count - nearest_pose_delta_rejection_count
        )
        report = {
            "schema_version": SCHEMA_VERSION,
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
        replace_directory(staging_folder, output_folder)
    except Exception:
        if staging_folder.exists():
            shutil.rmtree(staging_folder)
        raise

    matched_path = output_folder / MATCH_ROBOT_EE_POSES
    report_path = output_folder / SYNC_REPORT

    return SyncResult(
        sensor_folder=sensor_path.as_posix(),
        output_folder=output_folder.as_posix(),
        matched_poses_path=matched_path.as_posix(),
        report_path=report_path.as_posix(),
        total_frames=len(frame_records),
        matched_frames=len(matched),
        dropped_frames=len(dropped),
    )


def sync_result_artifacts(result: SyncResult) -> dict[str, str]:
    result_dict = asdict(result)
    return {
        MATCH_ROBOT_EE_POSES: result_dict["matched_poses_path"],
        SYNC_REPORT: result_dict["report_path"],
    }


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
) -> list[SyncResult]:
    """Synchronize discovered sensors or an explicit contained subset.

    Omitting ``sensor_folders`` preserves the original run-wide behavior.
    Supplying it lets intent-level orchestration reuse the stage without
    allowing an unselected or out-of-run folder to enter the calculation.
    """

    run_path = Path(run_root)
    results = []
    if sensor_folders is None:
        selected = filter_enabled_sensor_folders(
            run_path,
            (
                run_path / str(sensor_record["folder"])
                for sensor_record in discover_sensor_records(run_path)
            ),
        )
    else:
        selected = []
        seen: set[Path] = set()
        for value in sensor_folders:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = run_path / candidate
            resolved = candidate.resolve()
            try:
                resolved.relative_to(run_path.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Explicit sensor folder must remain below the run root: {value}"
                ) from exc
            if resolved in seen:
                raise ValueError(f"Explicit sensor folder is duplicated: {value}")
            seen.add(resolved)
            selected.append(resolved)

    if raw_robot_poses is not None and len(selected) != 1:
        raise ValueError(
            "A raw_robot_poses override requires exactly one selected sensor folder"
        )

    for sensor_folder in selected:
        results.append(
            synchronize_sensor_folder(
                sensor_folder,
                run_root=run_path,
                output_root=output_root,
                sync_delta=sync_delta,
                timestamp_source=timestamp_source,
                robot_timestamp_source=robot_timestamp_source,
                copy_files=copy_files,
                max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
                required_frame_timestamp_domain=required_frame_timestamp_domain,
                timestamp_fallback_allowed=timestamp_fallback_allowed,
                calibration_sync=calibration_sync,
                raw_robot_poses=raw_robot_poses,
            )
        )

    return results
