"""Authoritative completion validation for one supervised physical capture."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from posetestbot.io.artifacts import (
    CAMERA_DATA_JSON,
    CAMERA_JSON,
    CAM_K,
    DEPTH_DIR,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
)
from posetestbot.sensors.frame_writer import SCHEMA_VERSION as FRAME_METADATA_SCHEMA
from posetestbot.sensors.registry import is_auto_device_id, sensor_folder_name


SCHEMA_VERSION = "capture_completion.v1"
ROBOT_POSE_SCHEMA_VERSION = "robot_pose.v1"


def _check(name: str, ok: bool, message: str, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": "ok" if ok else "error",
        "message": message,
        "details": details,
    }


def _positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _load_current_frame_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing current frame metadata: {path}")
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(
                    f"Frame metadata line {line_number} is not committed with a newline"
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Frame metadata line {line_number} is invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(f"Frame metadata line {line_number} must be an object")
            if record.get("schema_version") != FRAME_METADATA_SCHEMA:
                raise ValueError(
                    f"Frame metadata line {line_number} must use {FRAME_METADATA_SCHEMA}"
                )
            for field in (
                "sensor_timestamp_ns",
                "host_received_timestamp_ns",
                "host_wall_timestamp_ns",
            ):
                if not _positive_integer(record.get(field)):
                    raise ValueError(
                        f"Frame metadata line {line_number} requires positive {field}"
                    )
            if (
                isinstance(record.get("frame_index"), bool)
                or not isinstance(record.get("frame_index"), int)
                or record["frame_index"] < 0
            ):
                raise ValueError(
                    f"Frame metadata line {line_number} requires a non-negative frame_index"
                )
            for field in ("sensor_id", "frame_id", "rgb_path", "depth_path"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ValueError(
                        f"Frame metadata line {line_number} requires non-empty {field}"
                    )
            records.append(record)
    if not records:
        raise ValueError(f"Frame metadata must contain at least one record: {path}")
    return records


def _sensor_check(root: Path, sensor: Mapping[str, Any]) -> dict[str, Any]:
    folder_name = sensor_folder_name(
        str(sensor["sensor_type"]), str(sensor["device_id"])
    )
    folder = root / folder_name
    sidecar_names = (CAM_K, DEPTH_SCALE, CAMERA_JSON, CAMERA_DATA_JSON)
    sidecars_ok = all(
        (folder / name).is_file()
        and not (folder / name).is_symlink()
        and (folder / name).stat().st_size > 0
        for name in sidecar_names
    )
    rgb_files = {
        path.relative_to(folder).as_posix()
        for path in (folder / RGB_DIR).glob("*.png")
        if path.is_file() and not path.is_symlink()
    }
    depth_files = {
        path.relative_to(folder).as_posix()
        for path in (folder / DEPTH_DIR).glob("*.png")
        if path.is_file() and not path.is_symlink()
    }
    try:
        records = _load_current_frame_records(folder / FRAME_METADATA_JSONL)
        metadata_error = None
    except (OSError, UnicodeError, ValueError) as exc:
        records = []
        metadata_error = str(exc)
    recorded_rgb = {str(record.get("rgb_path")) for record in records}
    recorded_depth = {str(record.get("depth_path")) for record in records}
    frame_ids = [str(record.get("frame_id")) for record in records]
    frame_indices = [record.get("frame_index") for record in records]
    paths_match_frame_ids = bool(records) and all(
        record.get("rgb_path") == f"{RGB_DIR}/{record['frame_id']}"
        and record.get("depth_path") == f"{DEPTH_DIR}/{record['frame_id']}"
        for record in records
    )
    expected_sensor_id = str(sensor["device_id"])
    identity_ok = bool(records) and all(
        record.get("sensor_type") == sensor["sensor_type"]
        and (
            isinstance(record.get("sensor_id"), str)
            and bool(record["sensor_id"])
            and (
                is_auto_device_id(expected_sensor_id)
                or record.get("sensor_id") == expected_sensor_id
            )
        )
        for record in records
    )
    balanced = (
        bool(records)
        and len(rgb_files) == len(depth_files) == len(records)
        and recorded_rgb == rgb_files
        and recorded_depth == depth_files
        and len(frame_ids) == len(set(frame_ids))
        and len(frame_indices) == len(set(frame_indices))
        and paths_match_frame_ids
    )
    ok = metadata_error is None and sidecars_ok and identity_ok and balanced
    return _check(
        f"sensor:{folder_name}",
        ok,
        (
            f"{folder_name} has balanced current RGB-D metadata and strict timestamps."
            if ok
            else f"{folder_name} does not satisfy the current raw-frame contract."
        ),
        folder=folder.as_posix(),
        rgb_count=len(rgb_files),
        depth_count=len(depth_files),
        metadata_count=len(records),
        metadata_error=metadata_error,
        required_sidecars=list(sidecar_names),
        sidecars_ok=sidecars_ok,
        identity_ok=identity_ok,
        paths_match_frame_ids=paths_match_frame_ids,
        balanced=balanced,
    )


def _robot_pose_check(
    root: Path,
    *,
    run_id: str,
    expected_reference_frame_path: str,
) -> dict[str, Any]:
    path = root / RAW_ROBOT_EE_POSES
    error: str | None = None
    records: list[Mapping[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or not value:
            raise ValueError("raw robot pose artifact must be a nonempty object")
        records = [record for record in value.values() if isinstance(record, Mapping)]
        if len(records) != len(value):
            raise ValueError("every raw robot pose entry must be an object")
        previous_sequence: int | None = None
        for index, record in enumerate(records):
            for field in ("host_received_timestamp_ns", "host_wall_timestamp_ns"):
                if not _positive_integer(record.get(field)):
                    raise ValueError(
                        f"robot pose {index} requires positive {field}"
                    )
            if not isinstance(record.get("motion"), str) or not record[
                "motion"
            ].strip():
                raise ValueError(f"robot pose {index} requires a non-empty motion")
            pose = record.get("pose")
            if not isinstance(pose, Mapping):
                raise ValueError(f"robot pose {index} is missing pose coordinates")
            for axis in ("X", "Y", "Z", "A", "B", "C"):
                coordinate = pose.get(axis)
                if (
                    isinstance(coordinate, bool)
                    or not isinstance(coordinate, (int, float))
                    or not math.isfinite(float(coordinate))
                ):
                    raise ValueError(
                        f"robot pose {index} coordinate {axis} must be finite"
                    )
            source = record.get("source_packet")
            if not isinstance(source, Mapping):
                raise ValueError(f"robot pose {index} is missing source_packet")
            if source.get("schema_version") != ROBOT_POSE_SCHEMA_VERSION:
                raise ValueError(
                    f"robot pose {index} must use {ROBOT_POSE_SCHEMA_VERSION}"
                )
            if source.get("packet_kind") != "pose":
                raise ValueError(f"robot pose {index} packet_kind must be pose")
            if source.get("run_id") != run_id:
                raise ValueError(f"robot pose {index} run_id does not match the run")
            sequence = source.get("sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
            ):
                raise ValueError(
                    f"robot pose {index} sequence must be a non-negative integer"
                )
            if previous_sequence is not None and sequence <= previous_sequence:
                raise ValueError(
                    f"robot pose {index} sequence must increase strictly"
                )
            previous_sequence = sequence
            for field in ("sender_monotonic_ns", "sender_wall_timestamp_ms"):
                timestamp = source.get(field)
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, int)
                    or timestamp < 0
                ):
                    raise ValueError(
                        f"robot pose {index} requires non-negative {field}"
                    )
            for field in ("sequence_delta", "estimated_packets_lost"):
                derived = source.get(field)
                if (
                    isinstance(derived, bool)
                    or not isinstance(derived, int)
                    or derived < 0
                ):
                    raise ValueError(
                        f"robot pose {index} requires non-negative {field}"
                    )
            if (
                source.get("from_frame") != "robot_flange"
                or source.get("to_frame") != "template_base"
                or source.get("sunrise_reference_frame_path")
                != expected_reference_frame_path
            ):
                raise ValueError(
                    f"robot pose {index} does not match the configured frame provenance"
                )
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        error = str(exc)
    ok = error is None and bool(records)
    return _check(
        "robot_pose_stream",
        ok,
        (
            "The raw robot-pose stream is nonempty and uses the current packet contract."
            if ok
            else "The raw robot-pose stream does not satisfy the current packet contract."
        ),
        path=path.as_posix(),
        pose_count=len(records),
        error=error,
    )


def _process_check(
    processes: list[Mapping[str, Any]], *, expected_sensor_count: int
) -> dict[str, Any]:
    receiver = [item for item in processes if item.get("role") == "robot_pose_receiver"]
    sensors = [item for item in processes if item.get("role") == "sensor_capture"]
    receiver_ok = len(receiver) == 1 and receiver[0].get("status") == "succeeded"
    completed_sensors = [
        item
        for item in sensors
        if item.get("status") == "succeeded"
        or (
            item.get("status") == "stopped"
            and item.get("termination_reason") == "stopped_after_receiver_exit"
        )
    ]
    clean_retries = [
        item
        for item in sensors
        if item not in completed_sensors
        and item.get("output_mutated") is False
        and item.get("termination_reason")
        in {
            "startup_spawn_failed",
            "startup_exit_retry",
            "startup_readiness_timeout_retry",
        }
    ]
    sensors_ok = len(completed_sensors) == expected_sensor_count and len(sensors) == (
        len(completed_sensors) + len(clean_retries)
    )
    sensors_ok = sensors_ok and all(
        item.get("status") == "succeeded"
        or (
            item.get("status") == "stopped"
            and item.get("termination_reason") == "stopped_after_receiver_exit"
        )
        for item in completed_sensors
    )
    released = bool(processes) and all(
        item.get("status") not in {"starting", "running"}
        and bool(item.get("ended_at"))
        for item in processes
    )
    ok = receiver_ok and sensors_ok and released
    return _check(
        "child_processes_and_resources",
        ok,
        (
            "Capture children completed and all local resources were released."
            if ok
            else "Capture children did not complete cleanly or remain unreleased."
        ),
        receiver_count=len(receiver),
        sensor_count=len(sensors),
        expected_sensor_count=expected_sensor_count,
        completed_sensor_count=len(completed_sensors),
        clean_retry_count=len(clean_retries),
        receiver_ok=receiver_ok,
        sensors_ok=sensors_ok,
        resources_released=released,
    )


def build_capture_completion(
    run_root: str | Path,
    config: Mapping[str, Any],
    processes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate current raw acquisition evidence after all children have stopped."""

    root = Path(run_root)
    enabled_sensors = [
        sensor
        for sensor in config["capture"]["sensors"]
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is True
    ]
    robot_pose = config["frames"]["robot_pose"]
    checks = [_sensor_check(root, sensor) for sensor in enabled_sensors]
    checks.append(
        _robot_pose_check(
            root,
            run_id=str(config["run_id"]),
            expected_reference_frame_path=str(
                robot_pose["sunrise_reference_frame_path"]
            ),
        )
    )
    checks.append(_process_check(processes, expected_sensor_count=len(enabled_sensors)))
    errors = [check for check in checks if check["status"] == "error"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if not errors else "error",
        "enabled_sensor_count": len(enabled_sensors),
        "checks": checks,
        "error_count": len(errors),
    }
