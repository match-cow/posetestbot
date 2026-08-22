"""Run-level quality checks for non-destructive synchronization output."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping

from posetestbot.io._report_checks import (
    make_check as _check,
    overall_status as _overall_status,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
    CURRENT_SENSOR_METADATA_ARTIFACTS,
    PROCESSED_DIR,
    RAW_ROBOT_EE_POSES,
    SYNC_QUALITY_REPORT,
    SYNC_REPORT,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.sensor_selection import filter_enabled_sensor_folders
from posetestbot.pipeline.sensor_selection import enabled_sensor_folder_names
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.robot.pose_receiver import (
    POSE_PACKET_SCHEMA_VERSION,
)
from posetestbot.robot.reference_frames import configured_sunrise_reference_frame_path
from posetestbot.sync.non_destructive import (
    _reject_symlink_components,
    _source_sensor_artifact_evidence,
    _synchronization_owned_artifact_evidence,
    _validate_source_sensor_identity,
    indexed_robot_poses,
    load_frame_metadata,
    robot_pose_packet_loss,
    robot_timestamp_ns,
    validate_frame_timestamp_sequence,
)


SCHEMA_VERSION = "sync_quality_report.v2"
SYNC_REPORT_SCHEMA_VERSION = "sync_report.v4"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("Artifact evidence requires a non-empty path")
    declared = Path(value)
    return declared if declared.is_absolute() else root / declared


def _validate_file_evidence(
    evidence: object,
    *,
    expected_path: Path,
    root: Path,
    label: str,
) -> None:
    if not isinstance(evidence, Mapping):
        raise ValueError(f"{label} evidence must be an object")
    declared = _artifact_path(root, evidence.get("path"))
    if declared.resolve() != expected_path.resolve():
        raise ValueError(f"{label} evidence path does not match {expected_path}")
    if expected_path.is_symlink() or not expected_path.is_file():
        raise ValueError(f"{label} must be a regular file: {expected_path}")
    size = evidence.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"{label} evidence has invalid size_bytes")
    if size != expected_path.stat().st_size:
        raise ValueError(f"{label} size evidence is stale or mismatched")
    digest = evidence.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError(f"{label} evidence has invalid sha256")
    if digest != _sha256_file(expected_path):
        raise ValueError(f"{label} hash evidence is stale or mismatched")


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Sync report must be a JSON object: {path}")
    return value


def _run_robot_pose_packet_loss(root: Path) -> tuple[bool, int | None]:
    path = root / RAW_ROBOT_EE_POSES
    if path.is_symlink() or not path.is_file():
        return False, None
    try:
        value = _read_json(path)
        config = load_run_config_for_run_root(root)
        records = indexed_robot_poses(
            value,
            timestamp_source="host_received",
            expected_run_id=str(config["run_id"]),
            expected_reference_frame_path=configured_sunrise_reference_frame_path(
                config
            ),
        )
        audited, total = robot_pose_packet_loss(records)
    except (KeyError, TypeError, ValueError):
        return False, None
    return (True, total) if audited else (False, None)


def discover_sync_reports(run_root: str | Path) -> list[Path]:
    root = Path(run_root)
    sync_root = root / PROCESSED_DIR / SYNCHRONIZED_DIR
    if not sync_root.is_dir():
        return []
    folders = filter_enabled_sensor_folders(
        root,
        (path for path in sorted(sync_root.iterdir()) if path.is_dir()),
    )
    return [
        folder / SYNC_REPORT for folder in folders if (folder / SYNC_REPORT).is_file()
    ]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolved_report_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("Sync report path fields must be non-empty strings")
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _load_derived_metadata(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Derived frame metadata must be a regular file: {path}")
    records: list[dict[str, Any]] = []
    with open(path, "r") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(
                    f"Derived frame metadata line {line_number} is not newline-committed"
                )
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid derived frame metadata line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"Derived frame metadata line {line_number} must be an object"
                )
            records.append(value)
    return records


def _validate_sync_artifacts(
    report_path: Path,
    report: Mapping[str, Any],
    root: Path,
    *,
    managed_run_output: bool,
) -> None:
    _reject_symlink_components(report_path, label="Synchronization report path")
    if report_path.is_symlink() or not report_path.is_file():
        raise ValueError(f"Sync report must be a regular file: {report_path}")
    output_folder = report_path.parent
    sensor_name = output_folder.name
    expected_sensor = root / sensor_name
    _reject_symlink_components(expected_sensor, label="Raw sensor path")
    declared_sensor = _resolved_report_path(root, report.get("sensor_folder"))
    declared_output = _resolved_report_path(root, report.get("output_folder"))
    if declared_sensor != expected_sensor.resolve():
        raise ValueError(
            f"Sync report sensor_folder is not the canonical raw folder for {sensor_name}"
        )
    if declared_output != output_folder.resolve():
        raise ValueError(
            f"Sync report output_folder does not match its artifact folder: {sensor_name}"
        )

    generation_id = report.get("sync_generation_id")
    if not isinstance(generation_id, str) or not generation_id.strip():
        raise ValueError("sync_report.v4 requires a non-empty sync_generation_id")
    output_contract = report.get("output_contract")
    if output_contract not in {"rgbd_copy", "pairing_only"}:
        raise ValueError("sync_report.v4 has an unsupported output_contract")
    if managed_run_output and output_contract != "rgbd_copy":
        raise ValueError("Managed synchronization requires copied RGB-D artifacts")
    sync_delta_ms = report.get("sync_delta_ms")
    if (
        isinstance(sync_delta_ms, bool)
        or not isinstance(sync_delta_ms, int | float)
        or not math.isfinite(float(sync_delta_ms))
    ):
        raise ValueError(f"{sensor_name} sync_delta_ms must be finite")
    nearest_threshold_ms = report.get("max_nearest_pose_delta_ms")
    if nearest_threshold_ms is not None and (
        isinstance(nearest_threshold_ms, bool)
        or not isinstance(nearest_threshold_ms, int | float)
        or not math.isfinite(float(nearest_threshold_ms))
        or float(nearest_threshold_ms) < 0
    ):
        raise ValueError(
            f"{sensor_name} max_nearest_pose_delta_ms must be null or non-negative"
        )
    required_domain = report.get("required_frame_timestamp_domain")
    if required_domain is not None and (
        not isinstance(required_domain, str) or not required_domain.strip()
    ):
        raise ValueError(
            f"{sensor_name} required timestamp domain must be null or non-empty"
        )
    if report.get("timestamp_fallback_allowed") is not False:
        raise ValueError(f"{sensor_name} current synchronization forbids fallback")
    if report.get("calibration_sync") is not None and not isinstance(
        report.get("calibration_sync"), Mapping
    ):
        raise ValueError(f"{sensor_name} calibration_sync must be null or an object")
    timestamp_pair = report.get("timestamp_pair")
    expected_pair = {
        "frame_timestamp_source": report.get("frame_timestamp_source"),
        "requested_frame_timestamp_source": report.get(
            "requested_frame_timestamp_source"
        ),
        "robot_timestamp_source": report.get("robot_timestamp_source"),
    }
    if (
        report.get("timestamp_pair_provenance_audited") is not True
        or not isinstance(timestamp_pair, Mapping)
        or dict(timestamp_pair) != expected_pair
        or report.get("timestamp_source") != report.get("frame_timestamp_source")
        or report.get("requested_timestamp_source")
        != report.get("requested_frame_timestamp_source")
    ):
        raise ValueError(
            f"{sensor_name} timestamp-pair provenance is internally inconsistent"
        )

    input_evidence = report.get("input_evidence")
    if not isinstance(input_evidence, Mapping):
        raise ValueError("sync_report.v4 input_evidence must be an object")
    _validate_file_evidence(
        input_evidence.get("frame_metadata.jsonl"),
        expected_path=expected_sensor / "frame_metadata.jsonl",
        root=root,
        label=f"{sensor_name} source frame metadata",
    )
    source_metadata = load_frame_metadata(expected_sensor)
    validate_frame_timestamp_sequence(
        source_metadata,
        str(report.get("requested_frame_timestamp_source")),
    )
    _validate_source_sensor_identity(
        load_run_config_for_run_root(root),
        expected_sensor,
        source_metadata,
    )
    current_source_artifacts = _source_sensor_artifact_evidence(
        expected_sensor,
        source_metadata,
    )
    if input_evidence.get("sensor_artifact_set") != current_source_artifacts:
        raise ValueError(
            f"{sensor_name} raw RGB-D and camera-sidecar evidence is stale or "
            "mismatched"
        )
    robot_evidence = input_evidence.get(RAW_ROBOT_EE_POSES)
    if managed_run_output or not (
        isinstance(robot_evidence, Mapping)
        and robot_evidence.get("source") == "verified_in_memory_snapshot"
    ):
        _validate_file_evidence(
            robot_evidence,
            expected_path=root / RAW_ROBOT_EE_POSES,
            root=root,
            label=f"{sensor_name} raw robot poses",
        )
    else:
        size = robot_evidence.get("size_bytes")
        digest = robot_evidence.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("Verified robot-pose snapshot evidence is invalid")

    output_evidence = report.get("output_evidence")
    if not isinstance(output_evidence, Mapping):
        raise ValueError("sync_report.v4 output_evidence must be an object")
    if output_evidence.get(
        "artifact_set"
    ) != _synchronization_owned_artifact_evidence(
        output_folder,
        output_contract=str(output_contract),
    ):
        raise ValueError(
            f"{sensor_name} synchronized artifact-set evidence is stale or mismatched"
        )
    matched_path = output_folder / "match_robot_ee_poses.json"
    _validate_file_evidence(
        output_evidence.get("match_robot_ee_poses.json"),
        expected_path=matched_path,
        root=root,
        label=f"{sensor_name} matched robot poses",
    )
    matched = _read_json(matched_path)
    matched_frames = _nonnegative_int(
        report.get("matched_frames"), label="matched_frames"
    )
    if len(matched) != matched_frames:
        raise ValueError(
            f"{sensor_name} matched pose membership disagrees with sync_report"
        )

    if output_contract == "rgbd_copy":
        expected_sidecars = [
            name
            for name in CURRENT_SENSOR_METADATA_ARTIFACTS
            if name != "frame_metadata.jsonl"
        ]
        if report.get("copied_metadata_artifacts") != expected_sidecars:
            raise ValueError(
                f"{sensor_name} copied camera-sidecar evidence is incomplete"
            )
        for name in expected_sidecars:
            sidecar = output_folder / name
            if sidecar.is_symlink() or not sidecar.is_file():
                raise ValueError(
                    f"{sensor_name} synchronized camera sidecar is missing: {name}"
                )
            if managed_run_output and _sha256_file(sidecar) != _sha256_file(
                expected_sensor / name
            ):
                raise ValueError(
                    f"{sensor_name} synchronized camera sidecar does not match "
                    f"its raw source: {name}"
                )
        metadata_path = output_folder / "frame_metadata.jsonl"
        _validate_file_evidence(
            output_evidence.get("frame_metadata.jsonl"),
            expected_path=metadata_path,
            root=root,
            label=f"{sensor_name} derived frame metadata",
        )
        metadata = _load_derived_metadata(metadata_path)
        frame_ids: list[str] = []
        for index, record in enumerate(metadata):
            frame_id = record.get("frame_id")
            if not isinstance(frame_id, str) or not frame_id:
                raise ValueError(
                    f"{sensor_name} derived metadata record {index} lacks frame_id"
                )
            if (
                record.get("rgb_path") != f"rgb/{frame_id}"
                or record.get("depth_path") != f"depth/{frame_id}"
            ):
                raise ValueError(
                    f"{sensor_name} derived metadata paths do not match {frame_id}"
                )
            if record.get("schema_version") != "frame_metadata.v1":
                raise ValueError(
                    f"{sensor_name} derived metadata {frame_id} has retired schema"
                )
            if record.get("frame_index") != index or frame_id != f"{index:06d}.png":
                raise ValueError(
                    f"{sensor_name} derived metadata frame ordering is inconsistent"
                )
            for field in ("sensor_type", "sensor_id", "source_frame_id", "motion"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ValueError(
                        f"{sensor_name} derived metadata {frame_id} requires {field}"
                    )
            for field in ("host_received_timestamp_ns", "host_wall_timestamp_ns"):
                timestamp = record.get(field)
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, int)
                    or timestamp <= 0
                ):
                    raise ValueError(
                        f"{sensor_name} derived metadata {frame_id} requires {field}"
                    )
            _nonnegative_int(
                record.get("matched_robot_pose_index"),
                label=f"{frame_id}.matched_robot_pose_index",
            )
            nearest_delta = record.get("nearest_robot_delta_ns")
            if isinstance(nearest_delta, bool) or not isinstance(nearest_delta, int):
                raise ValueError(
                    f"{sensor_name} derived metadata {frame_id} requires nearest delta"
                )
            frame_ids.append(frame_id)
        if len(frame_ids) != len(set(frame_ids)) or len(frame_ids) != matched_frames:
            raise ValueError(
                f"{sensor_name} derived frame membership disagrees with sync_report"
            )
        if set(frame_ids) != set(matched):
            raise ValueError(
                f"{sensor_name} frame metadata and matched poses have different membership"
            )
        for directory_name in ("rgb", "depth"):
            directory = output_folder / directory_name
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(
                    f"{sensor_name} synchronized {directory_name} must be a directory"
                )
            filenames = {
                path.name
                for path in directory.iterdir()
                if path.is_file() and not path.is_symlink()
            }
            if filenames != set(frame_ids):
                raise ValueError(
                    f"{sensor_name} {directory_name} membership disagrees with metadata"
                )
        managed_raw_poses = (
            _read_json(root / RAW_ROBOT_EE_POSES) if managed_run_output else None
        )
        nearest_deltas: list[int] = []
        raw_metadata_by_id = {
            str(record.get("frame_id")): record
            for record in load_frame_metadata(expected_sensor)
        }
        for frame_id, metadata_record in zip(frame_ids, metadata, strict=True):
            matched_record = matched[frame_id]
            if not isinstance(matched_record, Mapping):
                raise ValueError(
                    f"{sensor_name} matched pose {frame_id} is not an object"
                )
            linked_fields = {
                "source_frame_id": metadata_record.get("source_frame_id"),
                "matched_robot_pose_index": metadata_record.get(
                    "matched_robot_pose_index"
                ),
                "nearest_robot_delta_ns": metadata_record.get("nearest_robot_delta_ns"),
                "motion": metadata_record.get("motion"),
            }
            for field, expected in linked_fields.items():
                if matched_record.get(field) != expected:
                    raise ValueError(
                        f"{sensor_name} {frame_id} has inconsistent {field} linkage"
                    )
            source_frame_id = str(metadata_record["source_frame_id"])
            raw_metadata = raw_metadata_by_id.get(source_frame_id)
            if not isinstance(raw_metadata, Mapping):
                raise ValueError(
                    f"{sensor_name} {frame_id} does not identify a current raw frame"
                )
            if required_domain is not None and raw_metadata.get(
                "color_timestamp_domain"
            ) != required_domain:
                raise ValueError(
                    f"{sensor_name} {frame_id} raw timestamp domain no longer "
                    "matches the synchronization contract"
                )
            preserved_raw_fields = set(raw_metadata) - {
                "frame_index",
                "frame_id",
                "rgb_path",
                "depth_path",
            }
            if any(
                metadata_record.get(field) != raw_metadata.get(field)
                for field in preserved_raw_fields
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} did not preserve its raw metadata"
                )
            source_fields = {
                "source_frame_index": raw_metadata.get("frame_index"),
                "source_rgb_path": raw_metadata.get("rgb_path"),
                "source_depth_path": raw_metadata.get("depth_path"),
                "sensor_type": raw_metadata.get("sensor_type"),
                "sensor_id": raw_metadata.get("sensor_id"),
                "host_received_timestamp_ns": raw_metadata.get(
                    "host_received_timestamp_ns"
                ),
                "host_wall_timestamp_ns": raw_metadata.get(
                    "host_wall_timestamp_ns"
                ),
                "sensor_timestamp_ns": raw_metadata.get("sensor_timestamp_ns"),
            }
            if any(
                metadata_record.get(field) != expected
                for field, expected in source_fields.items()
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} derived metadata is not bound to "
                    "the current raw frame record"
                )
            if (
                matched_record.get("source_rgb") != raw_metadata.get("rgb_path")
                or matched_record.get("source_depth")
                != raw_metadata.get("depth_path")
                or matched_record.get("image_timestamp_ns")
                != metadata_record.get("sync_timestamp_ns")
                or matched_record.get("timestamp_source")
                != metadata_record.get("sync_timestamp_source")
                or matched_record.get("robot_timestamp_source")
                != metadata_record.get("sync_robot_timestamp_source")
                or metadata_record.get("sync_delta_ms") != sync_delta_ms
                or metadata_record.get("sync_requested_timestamp_source")
                != report.get("requested_frame_timestamp_source")
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} has inconsistent raw/timestamp linkage"
                )
            nearest_delta = int(metadata_record["nearest_robot_delta_ns"])
            if matched_record.get("robot_timestamp_ns") != (
                matched_record.get("delayed_timestamp_ns", 0) + nearest_delta
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} nearest-pose delta is inconsistent"
                )
            nearest_deltas.append(nearest_delta)
            pose = matched_record.get("robot_ee_pose")
            if not isinstance(pose, Mapping) or any(
                isinstance(pose.get(axis), bool)
                or not isinstance(pose.get(axis), int | float)
                or not math.isfinite(float(pose[axis]))
                for axis in ("X", "Y", "Z", "A", "B", "C")
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} has invalid matched robot pose"
                )
            source_packet = matched_record.get("source_packet")
            if (
                not isinstance(source_packet, Mapping)
                or source_packet.get("schema_version") != POSE_PACKET_SCHEMA_VERSION
                or source_packet.get("packet_kind") != "pose"
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} lacks current matched packet provenance"
                )
            expected_rgb = (output_folder / "rgb" / frame_id).resolve()
            expected_depth = (output_folder / "depth" / frame_id).resolve()
            if (
                _resolved_report_path(root, matched_record.get("synchronized_rgb"))
                != expected_rgb
                or _resolved_report_path(root, matched_record.get("synchronized_depth"))
                != expected_depth
            ):
                raise ValueError(
                    f"{sensor_name} {frame_id} has inconsistent synchronized paths"
                )
            if managed_run_output:
                assert managed_raw_poses is not None
                raw_pose = managed_raw_poses.get(
                    str(matched_record["matched_robot_pose_index"])
                )
                if (
                    not isinstance(raw_pose, Mapping)
                    or raw_pose.get("pose") != dict(pose)
                    or raw_pose.get("source_packet") != dict(source_packet)
                    or raw_pose.get("motion") != matched_record.get("motion")
                ):
                    raise ValueError(
                        f"{sensor_name} {frame_id} does not match current raw robot evidence"
                    )
                selected_robot_timestamp = robot_timestamp_ns(
                    raw_pose,
                    str(matched_record.get("robot_timestamp_source")),
                )
                if matched_record.get("robot_timestamp_ns") != selected_robot_timestamp:
                    raise ValueError(
                        f"{sensor_name} {frame_id} matched robot timestamp does not "
                        "match current raw robot evidence"
                    )
                raw_rgb = expected_sensor / str(raw_metadata["rgb_path"])
                raw_depth = expected_sensor / str(raw_metadata["depth_path"])
                if (
                    _sha256_file(raw_rgb) != _sha256_file(expected_rgb)
                    or _sha256_file(raw_depth) != _sha256_file(expected_depth)
                ):
                    raise ValueError(
                        f"{sensor_name} {frame_id} synchronized RGB-D bytes do not "
                        "match their bound raw source"
                    )
        absolute_deltas = [abs(value) for value in nearest_deltas]
        expected_mean_delta = mean(absolute_deltas) if absolute_deltas else None
        expected_max_delta = max(absolute_deltas) if absolute_deltas else None
        if (
            report.get("mean_abs_nearest_pose_delta_ns") != expected_mean_delta
            or report.get("max_abs_nearest_pose_delta_ns") != expected_max_delta
        ):
            raise ValueError(
                f"{sensor_name} nearest-pose summary does not match derived records"
            )

    total_frames = _nonnegative_int(report.get("total_frames"), label="total_frames")
    dropped_frames = _nonnegative_int(
        report.get("dropped_frames"), label="dropped_frames"
    )
    dropped = report.get("dropped")
    if not isinstance(dropped, list) or any(
        not isinstance(row, Mapping) for row in dropped
    ):
        raise ValueError("sync_report.v4 dropped must be an array of objects")
    if (
        dropped_frames != len(dropped)
        or total_frames != matched_frames + dropped_frames
    ):
        raise ValueError("Synchronization frame counts are internally inconsistent")
    outside_count = sum(
        row.get("reason") == "outside robot motion intervals" for row in dropped
    )
    in_motion = [
        row
        for row in dropped
        if isinstance(row.get("motion"), str) and row.get("motion")
    ]
    nearest_rejections = sum(
        row.get("reason") == "nearest robot pose delta exceeds threshold"
        for row in in_motion
    )
    expected_counts = {
        "outside_motion_interval_frame_count": outside_count,
        "eligible_in_motion_frames": matched_frames + len(in_motion),
        "matched_eligible_frames": matched_frames,
        "in_motion_exclusion_count": len(in_motion),
        "nearest_pose_delta_rejection_count": nearest_rejections,
        "unexplained_in_motion_exclusion_count": len(in_motion) - nearest_rejections,
    }
    for field, expected in expected_counts.items():
        if _nonnegative_int(report.get(field), label=field) != expected:
            raise ValueError(f"Synchronization count {field} is inconsistent")
    eligible_frames = expected_counts["eligible_in_motion_frames"]
    expected_coverage = matched_frames / eligible_frames if eligible_frames else 0.0
    coverage = report.get("eligible_motion_coverage")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, int | float)
        or not math.isfinite(float(coverage))
        or float(coverage) != expected_coverage
    ):
        raise ValueError("Synchronization eligible-motion coverage is inconsistent")
    timestamp_counts = report.get("timestamp_source_counts")
    if not isinstance(timestamp_counts, Mapping):
        raise ValueError("timestamp_source_counts must be an object")
    timestamp_total = sum(
        _nonnegative_int(value, label=f"timestamp_source_counts.{key}")
        for key, value in timestamp_counts.items()
    )
    if timestamp_total != total_frames:
        raise ValueError("Timestamp source counts do not cover every input frame")


def _sensor_summary(
    report_path: Path,
    report: Mapping[str, Any],
    root: Path,
    *,
    managed_run_output: bool,
) -> dict[str, Any]:
    total_frames = _nonnegative_int(report.get("total_frames"), label="total_frames")
    matched_frames = _nonnegative_int(
        report.get("matched_frames"), label="matched_frames"
    )
    dropped_frames = _nonnegative_int(
        report.get("dropped_frames"), label="dropped_frames"
    )
    report_schema = str(report.get("schema_version") or "")
    if report_schema != SYNC_REPORT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported sync report schema: {report_schema!r}")
    required_fields = {
        "sync_generation_id",
        "output_contract",
        "input_evidence",
        "output_evidence",
        "copied_metadata_artifacts",
        "sensor_folder",
        "output_folder",
        "requested_timestamp_source",
        "requested_frame_timestamp_source",
        "timestamp_source",
        "frame_timestamp_source",
        "robot_timestamp_source",
        "timestamp_pair",
        "timestamp_pair_provenance_audited",
        "timestamp_source_counts",
        "timestamp_fallback_count",
        "timestamp_missing_count",
        "incompatible_timestamp_pair_count",
        "sync_delta_ms",
        "max_nearest_pose_delta_ms",
        "required_frame_timestamp_domain",
        "timestamp_fallback_allowed",
        "calibration_sync",
        "nearest_pose_delta_rejection_count",
        "total_frames",
        "matched_frames",
        "dropped_frames",
        "outside_motion_interval_frame_count",
        "eligible_in_motion_frames",
        "matched_eligible_frames",
        "eligible_motion_coverage",
        "in_motion_exclusion_count",
        "unexplained_in_motion_exclusion_count",
        "robot_pose_packet_loss_audited",
        "robot_pose_packet_loss_count",
        "motion_intervals",
        "dropped",
        "mean_abs_nearest_pose_delta_ns",
        "max_abs_nearest_pose_delta_ns",
    }
    missing_fields = sorted(required_fields - set(report))
    if missing_fields:
        raise ValueError(
            "sync_report.v4 is missing required fields: " + ", ".join(missing_fields)
        )
    _validate_sync_artifacts(
        report_path,
        report,
        root,
        managed_run_output=managed_run_output,
    )
    dropped_rows = (
        [row for row in report.get("dropped", []) if isinstance(row, Mapping)]
        if isinstance(report.get("dropped"), list)
        else []
    )
    outside_motion_interval_frame_count = int(
        report.get(
            "outside_motion_interval_frame_count",
            sum(
                row.get("reason") == "outside robot motion intervals"
                for row in dropped_rows
            ),
        )
        or 0
    )
    dropped_in_motion_rows = [
        row
        for row in dropped_rows
        if isinstance(row.get("motion"), str) and row.get("motion")
    ]
    eligible_in_motion_frames = int(
        report.get(
            "eligible_in_motion_frames",
            (
                matched_frames + len(dropped_in_motion_rows)
                if dropped_rows
                else total_frames
            ),
        )
        or 0
    )
    matched_eligible_frames = int(
        report.get("matched_eligible_frames", matched_frames) or 0
    )
    match_ratio = (
        matched_eligible_frames / eligible_in_motion_frames
        if eligible_in_motion_frames
        else 0.0
    )
    in_motion_exclusion_count = int(
        report.get(
            "in_motion_exclusion_count",
            max(0, eligible_in_motion_frames - matched_eligible_frames),
        )
        or 0
    )
    unexplained_in_motion_exclusion_count = int(
        report.get(
            "unexplained_in_motion_exclusion_count",
            sum(
                row.get("reason") != "nearest robot pose delta exceeds threshold"
                for row in dropped_in_motion_rows
            ),
        )
        or 0
    )
    incompatible_timestamp_pair_count = int(
        report.get(
            "incompatible_timestamp_pair_count",
            sum(
                row.get("reason")
                == "frame/robot timestamp fallback clocks are incompatible"
                for row in dropped_rows
            ),
        )
        or 0
    )
    robot_pose_packet_loss_audited = (
        report.get("robot_pose_packet_loss_audited") is True
    )
    robot_pose_packet_loss_count = report.get("robot_pose_packet_loss_count")
    if robot_pose_packet_loss_audited:
        robot_pose_packet_loss_count = int(robot_pose_packet_loss_count or 0)
    else:
        robot_pose_packet_loss_count = None
    motion_intervals = report.get("motion_intervals")
    motion_windows = report.get("motion_windows", {})
    timestamp_source_counts = report.get("timestamp_source_counts")
    provenance_audited = isinstance(timestamp_source_counts, Mapping)
    timestamp_pair = report.get("timestamp_pair")
    pair_audited = report.get(
        "timestamp_pair_provenance_audited"
    ) is True and isinstance(timestamp_pair, Mapping)
    return {
        "sync_report_schema_version": report_schema,
        "sync_generation_id": report["sync_generation_id"],
        "output_contract": report["output_contract"],
        "sensor_name": report_path.parent.name,
        "report_path": _relative(report_path, root),
        "sensor_folder": report.get("sensor_folder"),
        "output_folder": report.get("output_folder"),
        "timestamp_source": report.get("timestamp_source"),
        "requested_timestamp_source": report.get(
            "requested_timestamp_source", report.get("timestamp_source")
        ),
        "timestamp_source_counts": (
            dict(timestamp_source_counts)
            if isinstance(timestamp_source_counts, Mapping)
            else {}
        ),
        "timestamp_fallback_count": int(report.get("timestamp_fallback_count", 0) or 0),
        "timestamp_missing_count": int(report.get("timestamp_missing_count", 0) or 0),
        "timestamp_provenance_audited": provenance_audited,
        "frame_timestamp_source": report.get(
            "frame_timestamp_source", report.get("timestamp_source")
        ),
        "requested_frame_timestamp_source": report.get(
            "requested_frame_timestamp_source",
            report.get("requested_timestamp_source", report.get("timestamp_source")),
        ),
        "robot_timestamp_source": report.get("robot_timestamp_source"),
        "timestamp_pair": (
            dict(timestamp_pair) if isinstance(timestamp_pair, Mapping) else {}
        ),
        "timestamp_pair_provenance_audited": pair_audited,
        "sync_delta_ms": report.get("sync_delta_ms"),
        "max_nearest_pose_delta_ms": report.get("max_nearest_pose_delta_ms"),
        "required_frame_timestamp_domain": report.get(
            "required_frame_timestamp_domain"
        ),
        "timestamp_fallback_allowed": report.get("timestamp_fallback_allowed"),
        "calibration_sync": (
            dict(report["calibration_sync"])
            if isinstance(report.get("calibration_sync"), Mapping)
            else None
        ),
        "nearest_pose_delta_rejection_count": int(
            report.get("nearest_pose_delta_rejection_count", 0) or 0
        ),
        "total_frames": total_frames,
        "matched_frames": matched_frames,
        "dropped_frames": dropped_frames,
        "outside_motion_interval_frame_count": (outside_motion_interval_frame_count),
        "eligible_in_motion_frames": eligible_in_motion_frames,
        "matched_eligible_frames": matched_eligible_frames,
        "eligible_motion_coverage": match_ratio,
        "in_motion_exclusion_count": in_motion_exclusion_count,
        "unexplained_in_motion_exclusion_count": (
            unexplained_in_motion_exclusion_count
        ),
        "incompatible_timestamp_pair_count": (incompatible_timestamp_pair_count),
        "robot_pose_packet_loss_audited": robot_pose_packet_loss_audited,
        "robot_pose_packet_loss_count": robot_pose_packet_loss_count,
        "match_ratio": match_ratio,
        "motion_count": (
            len(motion_intervals)
            if isinstance(motion_intervals, list)
            else len(motion_windows)
            if isinstance(motion_windows, Mapping)
            else 0
        ),
        "mean_abs_nearest_pose_delta_ns": report.get("mean_abs_nearest_pose_delta_ns"),
        "max_abs_nearest_pose_delta_ns": report.get("max_abs_nearest_pose_delta_ns"),
    }


def _sensor_checks(
    sensor: Mapping[str, Any],
    *,
    min_match_ratio: float,
    max_dropped_frames: int | None,
    max_nearest_pose_delta_ms: float | None,
    require_timestamp_source: str | None,
    require_robot_timestamp_source: str | None,
    expected_calibration_sync: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    name = str(sensor["sensor_name"])
    checks: list[dict[str, Any]] = []
    eligible_frames = int(sensor["eligible_in_motion_frames"])
    matched_frames = int(sensor["matched_eligible_frames"])
    match_ratio = float(sensor["match_ratio"])
    counts_valid = eligible_frames > 0 and 0 <= matched_frames <= eligible_frames

    checks.append(
        _check(
            f"sync_frames:{name}",
            "ok" if counts_valid and matched_frames > 0 else "error",
            (
                f"{name} synchronized {matched_frames}/{eligible_frames} "
                "eligible in-motion frame(s)."
                if counts_valid and matched_frames > 0
                else f"{name} has no valid synchronized in-motion frame coverage."
            ),
            details={
                "matched_eligible_frames": matched_frames,
                "eligible_in_motion_frames": eligible_frames,
            },
        )
    )
    checks.append(
        _check(
            f"sync_eligible_motion_coverage:{name}",
            "ok" if counts_valid and match_ratio >= min_match_ratio else "warning",
            (
                f"{name} eligible in-motion coverage is {match_ratio:.3f}."
                if counts_valid and match_ratio >= min_match_ratio
                else (
                    f"{name} eligible in-motion coverage is {match_ratio:.3f}; "
                    f"recommended minimum is {min_match_ratio:.3f}."
                )
            ),
            details={
                "eligible_motion_coverage": match_ratio,
                "min_match_ratio": min_match_ratio,
                "denominator": "eligible_in_motion_frames",
            },
        )
    )

    in_motion_exclusion_count = int(sensor["in_motion_exclusion_count"])
    if max_dropped_frames is not None:
        checks.append(
            _check(
                f"sync_in_motion_exclusions:{name}",
                (
                    "ok"
                    if in_motion_exclusion_count <= max_dropped_frames
                    else "warning"
                ),
                (
                    f"{name} excluded {in_motion_exclusion_count} eligible "
                    "in-motion frame(s)."
                    if in_motion_exclusion_count <= max_dropped_frames
                    else (
                        f"{name} excluded {in_motion_exclusion_count} eligible "
                        "in-motion frame(s); "
                        f"threshold is {max_dropped_frames}."
                    )
                ),
                details={
                    "in_motion_exclusion_count": in_motion_exclusion_count,
                    "max_dropped_frames": max_dropped_frames,
                },
            )
        )

    unexplained_exclusions = int(sensor["unexplained_in_motion_exclusion_count"])
    checks.append(
        _check(
            f"sync_unexplained_in_motion_exclusions:{name}",
            "ok" if unexplained_exclusions == 0 else "error",
            (
                f"{name} has no unexplained in-motion frame exclusions."
                if unexplained_exclusions == 0
                else (
                    f"{name} has {unexplained_exclusions} unexplained "
                    "in-motion frame exclusion(s)."
                )
            ),
            details={"unexplained_in_motion_exclusion_count": unexplained_exclusions},
        )
    )
    nearest_rejections = int(sensor["nearest_pose_delta_rejection_count"])
    checks.append(
        _check(
            f"sync_nearest_pose_rejections:{name}",
            "ok",
            (
                f"{name} rejected {nearest_rejections} eligible frame(s) "
                "at the nearest-pose threshold."
            ),
            details={"nearest_pose_delta_rejection_count": nearest_rejections},
        )
    )
    missing_count = int(sensor.get("timestamp_missing_count", 0) or 0)
    fallback_count = int(sensor.get("timestamp_fallback_count", 0) or 0)
    incompatible_count = int(sensor.get("incompatible_timestamp_pair_count", 0) or 0)
    fallback_allowed = sensor.get("timestamp_fallback_allowed") is True
    timestamp_complete = (
        missing_count == 0
        and incompatible_count == 0
        and (fallback_count == 0 or fallback_allowed)
    )
    checks.append(
        _check(
            f"sync_timestamp_completeness:{name}",
            (
                "ok"
                if timestamp_complete
                else ("error" if expected_calibration_sync else "warning")
            ),
            (
                f"{name} has complete compatible timestamp evidence."
                if timestamp_complete
                else (
                    f"{name} timestamp evidence has missing={missing_count}, "
                    f"fallback={fallback_count}, incompatible={incompatible_count}."
                )
            ),
            details={
                "timestamp_missing_count": missing_count,
                "timestamp_fallback_count": fallback_count,
                "incompatible_timestamp_pair_count": incompatible_count,
                "timestamp_fallback_allowed": fallback_allowed,
            },
        )
    )
    if max_nearest_pose_delta_ms is not None:
        max_delta_ns = sensor.get("max_abs_nearest_pose_delta_ns")
        threshold_ns = int(max_nearest_pose_delta_ms * 1_000_000)
        ok = max_delta_ns is not None and int(max_delta_ns) <= threshold_ns
        checks.append(
            _check(
                f"sync_nearest_pose_delta:{name}",
                "ok" if ok else ("error" if expected_calibration_sync else "warning"),
                (
                    f"{name} max nearest-pose delta is {max_delta_ns} ns."
                    if ok
                    else (
                        f"{name} has no nearest-pose delta metric."
                        if max_delta_ns is None
                        else (
                            f"{name} max nearest-pose delta is {max_delta_ns} ns; "
                            f"threshold is {threshold_ns} ns."
                        )
                    )
                ),
                details={
                    "max_abs_nearest_pose_delta_ns": max_delta_ns,
                    "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
                },
            )
        )

    if require_timestamp_source:
        timestamp_source = str(sensor.get("timestamp_source"))
        requested_source = str(sensor.get("requested_timestamp_source"))
        counts = sensor.get("timestamp_source_counts")
        if not isinstance(counts, Mapping):
            counts = {}
        fallback_count = int(sensor.get("timestamp_fallback_count", 0) or 0)
        missing_count = int(sensor.get("timestamp_missing_count", 0) or 0)
        audited = bool(sensor.get("timestamp_provenance_audited"))
        actual_sources = {str(key) for key, count in counts.items() if int(count) > 0}
        source_ok = (
            audited
            and requested_source == require_timestamp_source
            and actual_sources <= {require_timestamp_source}
            and fallback_count == 0
            and missing_count == 0
        )
        checks.append(
            _check(
                f"sync_timestamp_source:{name}",
                "ok" if source_ok else "error",
                (
                    f"{name} exclusively used timestamp source {timestamp_source}."
                    if source_ok
                    else (
                        f"{name} did not prove exclusive use of "
                        f"{require_timestamp_source}; actual={timestamp_source}, "
                        f"fallbacks={fallback_count}, missing={missing_count}."
                    )
                ),
                details={
                    "timestamp_source": timestamp_source,
                    "requested_timestamp_source": requested_source,
                    "timestamp_source_counts": dict(counts),
                    "timestamp_fallback_count": fallback_count,
                    "timestamp_missing_count": missing_count,
                    "timestamp_provenance_audited": audited,
                    "require_timestamp_source": require_timestamp_source,
                },
            )
        )
    if require_robot_timestamp_source:
        robot_source = sensor.get("robot_timestamp_source")
        pair = sensor.get("timestamp_pair")
        if not isinstance(pair, Mapping):
            pair = {}
        audited = bool(sensor.get("timestamp_pair_provenance_audited"))
        source_ok = (
            audited
            and robot_source == require_robot_timestamp_source
            and pair.get("robot_timestamp_source") == require_robot_timestamp_source
        )
        checks.append(
            _check(
                f"sync_robot_timestamp_source:{name}",
                "ok" if source_ok else "error",
                (
                    f"{name} used robot timestamp source {robot_source}."
                    if source_ok
                    else (
                        f"{name} did not prove robot timestamp source "
                        f"{require_robot_timestamp_source}; actual={robot_source}."
                    )
                ),
                details={
                    "robot_timestamp_source": robot_source,
                    "timestamp_pair": dict(pair),
                    "timestamp_pair_provenance_audited": audited,
                    "require_robot_timestamp_source": (require_robot_timestamp_source),
                },
            )
        )
    if expected_calibration_sync is not None:
        actual_calibration_sync = sensor.get("calibration_sync")
        expected_sensor = expected_calibration_sync.get("sensor")
        expected_delta = (
            expected_sensor.get("sync_delta_ms")
            if isinstance(expected_sensor, Mapping)
            else None
        )
        expected_frame_source = (
            expected_sensor.get("frame_timestamp_source")
            if isinstance(expected_sensor, Mapping)
            else None
        )
        expected_robot_source = (
            expected_sensor.get("robot_timestamp_source")
            if isinstance(expected_sensor, Mapping)
            else None
        )
        expected_threshold = (
            expected_sensor.get("max_nearest_pose_delta_ms")
            if isinstance(expected_sensor, Mapping)
            else None
        )
        expected_domain = (
            expected_sensor.get("required_frame_timestamp_domain")
            if isinstance(expected_sensor, Mapping)
            else None
        )
        expected_fallback = (
            expected_sensor.get("timestamp_fallback_allowed")
            if isinstance(expected_sensor, Mapping)
            else None
        )
        operational_values_match = (
            sensor.get("sync_delta_ms") == expected_delta
            and sensor.get("requested_frame_timestamp_source") == expected_frame_source
            and sensor.get("robot_timestamp_source") == expected_robot_source
            and sensor.get("max_nearest_pose_delta_ms") == expected_threshold
            and sensor.get("required_frame_timestamp_domain") == expected_domain
            and sensor.get("timestamp_fallback_allowed") == expected_fallback
        )
        provenance_matches = isinstance(actual_calibration_sync, Mapping) and dict(
            actual_calibration_sync
        ) == dict(expected_calibration_sync)
        timing_ok = provenance_matches and operational_values_match
        checks.append(
            _check(
                f"sync_calibration_timing:{name}",
                "ok" if timing_ok else "error",
                (
                    f"{name} used the hash-bound timing from calibration profile "
                    f"{expected_sensor.get('profile_id')}."
                    if timing_ok and isinstance(expected_sensor, Mapping)
                    else (
                        f"{name} synchronization does not match the selected "
                        "calibration profile timing."
                    )
                ),
                details={
                    "expected": dict(expected_calibration_sync),
                    "actual": (
                        dict(actual_calibration_sync)
                        if isinstance(actual_calibration_sync, Mapping)
                        else actual_calibration_sync
                    ),
                    "operational_values_match": operational_values_match,
                },
            )
        )
    return checks


def _required_source_for_sensor(
    value: str | Mapping[str, str] | None,
    sensor_name: str,
) -> str | None:
    if isinstance(value, Mapping):
        selected = value.get(sensor_name)
        return str(selected) if selected is not None else None
    return value


def _number_for_sensor(
    value: float | Mapping[str, float] | None,
    sensor_name: str,
) -> float | None:
    if isinstance(value, Mapping):
        selected = value.get(sensor_name)
        return float(selected) if selected is not None else None
    return value


def calibration_sync_provenance(
    policy: Mapping[str, Any],
    sensor: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact profile-timing evidence embedded in one sync report."""

    calibration_profiles = policy.get("calibration_profiles")
    return {
        "schema_version": policy.get("schema_version"),
        "source": policy.get("source"),
        "selection_artifact": policy.get("selection_artifact"),
        "bundle_sha256": policy.get("bundle_sha256"),
        "calibration_profiles": (
            dict(calibration_profiles)
            if isinstance(calibration_profiles, Mapping)
            else calibration_profiles
        ),
        "sensor": dict(sensor),
    }


def _calibration_sync_by_sensor(
    policy: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if policy is None:
        return {}
    raw_sensors = policy.get("sensors")
    if not isinstance(raw_sensors, list):
        raise ValueError("calibration_sync_policy.sensors must be a list")
    result: dict[str, dict[str, Any]] = {}
    for sensor in raw_sensors:
        if not isinstance(sensor, Mapping):
            raise ValueError("calibration_sync_policy sensor rows must be objects")
        sensor_name = sensor.get("sensor_folder")
        if not isinstance(sensor_name, str) or not sensor_name:
            raise ValueError(
                "calibration_sync_policy sensors require canonical sensor_folder"
            )
        if sensor_name in result:
            raise ValueError(
                f"calibration_sync_policy duplicates sensor folder {sensor_name}"
            )
        result[sensor_name] = calibration_sync_provenance(policy, sensor)
    return result


def build_sync_quality_report(
    run_root: str | Path,
    *,
    min_match_ratio: float = 0.8,
    max_dropped_frames: int | None = None,
    max_nearest_pose_delta_ms: float | Mapping[str, float] | None = 50.0,
    require_timestamp_source: str | Mapping[str, str] | None = None,
    require_robot_timestamp_source: str | Mapping[str, str] | None = None,
    report_paths: Iterable[str | Path] | None = None,
    calibration_sync_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not 0.0 <= min_match_ratio <= 1.0:
        raise ValueError("min_match_ratio must be between 0 and 1")
    if max_dropped_frames is not None and max_dropped_frames < 0:
        raise ValueError("max_dropped_frames cannot be negative")
    if isinstance(max_nearest_pose_delta_ms, Mapping):
        if any(float(value) < 0 for value in max_nearest_pose_delta_ms.values()):
            raise ValueError("max_nearest_pose_delta_ms cannot be negative")
    elif max_nearest_pose_delta_ms is not None and max_nearest_pose_delta_ms < 0:
        raise ValueError("max_nearest_pose_delta_ms cannot be negative")

    root = Path(run_root)
    expected_calibration_sync = _calibration_sync_by_sensor(calibration_sync_policy)
    reports_were_discovered = report_paths is None
    paths = (
        discover_sync_reports(root)
        if reports_were_discovered
        else [Path(path) for path in report_paths or ()]
    )
    checks: list[dict[str, Any]] = []
    sensors: list[dict[str, Any]] = []

    if not paths:
        checks.append(
            _check(
                "sync_reports_present",
                "error",
                "No synchronized sync_report.json files were found.",
                details={
                    "expected_root": (
                        root / PROCESSED_DIR / SYNCHRONIZED_DIR
                    ).as_posix()
                },
            )
        )
    else:
        checks.append(
            _check(
                "sync_reports_present",
                "ok",
                f"Found {len(paths)} sync report(s).",
                details={"report_count": len(paths)},
            )
        )

    for path in paths:
        # Discovery already returns paths rooted at ``run_root``. When that root
        # is relative, prepending it again produces ``run/run/processed/...``.
        # Explicit report paths retain the documented run-root-relative behavior.
        resolved = (
            path if path.is_absolute() or reports_were_discovered else root / path
        )
        try:
            report = _read_json(resolved)
            sensor = _sensor_summary(
                resolved,
                report,
                root,
                managed_run_output=reports_were_discovered,
            )
            sensors.append(sensor)
            sensor_name = str(sensor["sensor_name"])
            checks.extend(
                _sensor_checks(
                    sensor,
                    min_match_ratio=min_match_ratio,
                    max_dropped_frames=max_dropped_frames,
                    max_nearest_pose_delta_ms=_number_for_sensor(
                        max_nearest_pose_delta_ms, sensor_name
                    ),
                    require_timestamp_source=_required_source_for_sensor(
                        require_timestamp_source, sensor_name
                    ),
                    require_robot_timestamp_source=_required_source_for_sensor(
                        require_robot_timestamp_source, sensor_name
                    ),
                    expected_calibration_sync=expected_calibration_sync.get(
                        sensor_name
                    ),
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    f"sync_report_load:{_relative(resolved, root)}",
                    "error",
                    f"Could not read sync report {resolved}: {type(exc).__name__}: {exc}",
                    details={"path": resolved.as_posix()},
                )
            )

    if reports_were_discovered:
        try:
            expected_sensor_names = set(enabled_sensor_folder_names(root))
        except Exception as exc:
            expected_sensor_names = set()
            checks.append(
                _check(
                    "sync_sensor_configuration",
                    "error",
                    f"Could not load enabled sensor configuration: {type(exc).__name__}: {exc}",
                )
            )
        actual_sensor_names = {str(sensor["sensor_name"]) for sensor in sensors}
        coverage_ok = actual_sensor_names == expected_sensor_names
        checks.append(
            _check(
                "sync_sensor_coverage",
                "ok" if coverage_ok else "error",
                (
                    "Synchronization artifacts cover every enabled sensor exactly."
                    if coverage_ok
                    else "Synchronization artifact coverage does not match run_config.json."
                ),
                details={
                    "expected_sensor_folders": sorted(expected_sensor_names),
                    "actual_sensor_folders": sorted(actual_sensor_names),
                    "missing_sensor_folders": sorted(
                        expected_sensor_names - actual_sensor_names
                    ),
                    "unexpected_sensor_folders": sorted(
                        actual_sensor_names - expected_sensor_names
                    ),
                },
            )
        )
        generation_ids = {str(sensor["sync_generation_id"]) for sensor in sensors}
        generation_ok = coverage_ok and len(generation_ids) == 1
        checks.append(
            _check(
                "sync_generation_coherence",
                "ok" if generation_ok else "error",
                (
                    "Every enabled sensor belongs to one synchronization generation."
                    if generation_ok
                    else "Enabled sensors contain missing or mixed synchronization generations."
                ),
                details={"sync_generation_ids": sorted(generation_ids)},
            )
        )

    if expected_calibration_sync:
        actual_sensor_names = {str(sensor["sensor_name"]) for sensor in sensors}
        expected_sensor_names = set(expected_calibration_sync)
        coverage_ok = actual_sensor_names == expected_sensor_names
        checks.append(
            _check(
                "sync_calibration_timing_coverage",
                "ok" if coverage_ok else "error",
                (
                    "Synchronization reports cover every selected calibration "
                    "timing policy exactly once."
                    if coverage_ok
                    else (
                        "Synchronization report coverage does not match the "
                        "selected calibration timing policy."
                    )
                ),
                details={
                    "expected_sensor_folders": sorted(expected_sensor_names),
                    "actual_sensor_folders": sorted(actual_sensor_names),
                    "missing_sensor_folders": sorted(
                        expected_sensor_names - actual_sensor_names
                    ),
                    "unexpected_sensor_folders": sorted(
                        actual_sensor_names - expected_sensor_names
                    ),
                },
            )
        )

    sensor_packet_loss_audited = bool(sensors) and all(
        sensor.get("robot_pose_packet_loss_audited") is True for sensor in sensors
    )
    if reports_were_discovered:
        raw_audited, raw_packet_loss_count = _run_robot_pose_packet_loss(root)
        sensor_counts = {
            int(sensor.get("robot_pose_packet_loss_count", 0) or 0)
            for sensor in sensors
            if sensor.get("robot_pose_packet_loss_audited") is True
        }
        packet_loss_consistent = (
            raw_audited
            and raw_packet_loss_count is not None
            and sensor_packet_loss_audited
            and sensor_counts == {raw_packet_loss_count}
        )
        checks.append(
            _check(
                "sync_robot_pose_packet_loss_consistency",
                "ok" if packet_loss_consistent else "error",
                (
                    "Every synchronized sensor matches the current robot packet-loss evidence."
                    if packet_loss_consistent
                    else "Synchronized robot packet-loss evidence is missing, invalid, or inconsistent."
                ),
                details={
                    "raw_robot_pose_packet_loss_count": raw_packet_loss_count,
                    "sensor_robot_pose_packet_loss_counts": sorted(sensor_counts),
                },
            )
        )
        robot_pose_packet_loss_audited = packet_loss_consistent
        robot_pose_packet_loss_count = raw_packet_loss_count
    elif sensor_packet_loss_audited:
        robot_pose_packet_loss_audited = True
        robot_pose_packet_loss_count = max(
            int(sensor.get("robot_pose_packet_loss_count", 0) or 0)
            for sensor in sensors
        )
    else:
        robot_pose_packet_loss_audited, robot_pose_packet_loss_count = (
            _run_robot_pose_packet_loss(root)
        )
    if robot_pose_packet_loss_audited:
        assert robot_pose_packet_loss_count is not None
        checks.append(
            _check(
                "sync_robot_pose_packet_loss",
                "ok" if robot_pose_packet_loss_count == 0 else "warning",
                (
                    "Robot pose stream recorded "
                    f"{robot_pose_packet_loss_count} lost packet(s)."
                ),
                details={"robot_pose_packet_loss_count": robot_pose_packet_loss_count},
            )
        )
    else:
        checks.append(
            _check(
                "sync_robot_pose_packet_loss",
                "error",
                "Robot pose packet-loss evidence is missing or invalid.",
                details={"robot_pose_packet_loss_count": None},
            )
        )

    total_frames = sum(int(sensor["total_frames"]) for sensor in sensors)
    matched_frames = sum(int(sensor["matched_frames"]) for sensor in sensors)
    dropped_frames = sum(int(sensor["dropped_frames"]) for sensor in sensors)
    eligible_in_motion_frames = sum(
        int(sensor["eligible_in_motion_frames"]) for sensor in sensors
    )
    matched_eligible_frames = sum(
        int(sensor["matched_eligible_frames"]) for sensor in sensors
    )
    in_motion_exclusion_count = sum(
        int(sensor["in_motion_exclusion_count"]) for sensor in sensors
    )
    unexplained_in_motion_exclusion_count = sum(
        int(sensor["unexplained_in_motion_exclusion_count"]) for sensor in sensors
    )
    outside_motion_interval_frame_count = sum(
        int(sensor["outside_motion_interval_frame_count"]) for sensor in sensors
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _generated_at(),
        "run_root": root.as_posix(),
        "overall_status": _overall_status(checks),
        "checks": checks,
        "sensor_count": len(sensors),
        "total_frames": total_frames,
        "matched_frames": matched_frames,
        "dropped_frames": dropped_frames,
        "outside_motion_interval_frame_count": (outside_motion_interval_frame_count),
        "eligible_in_motion_frames": eligible_in_motion_frames,
        "matched_eligible_frames": matched_eligible_frames,
        "in_motion_exclusion_count": in_motion_exclusion_count,
        "unexplained_in_motion_exclusion_count": (
            unexplained_in_motion_exclusion_count
        ),
        "overall_match_ratio": (
            matched_eligible_frames / eligible_in_motion_frames
            if eligible_in_motion_frames
            else 0.0
        ),
        "overall_eligible_motion_coverage": (
            matched_eligible_frames / eligible_in_motion_frames
            if eligible_in_motion_frames
            else 0.0
        ),
        "match_ratio_denominator": "eligible_in_motion_frames",
        "robot_pose_packet_loss_audited": robot_pose_packet_loss_audited,
        "robot_pose_packet_loss_count": robot_pose_packet_loss_count,
        "min_match_ratio": min_match_ratio,
        "max_dropped_frames": max_dropped_frames,
        "max_nearest_pose_delta_ms": (
            dict(max_nearest_pose_delta_ms)
            if isinstance(max_nearest_pose_delta_ms, Mapping)
            else max_nearest_pose_delta_ms
        ),
        "require_timestamp_source": (
            dict(require_timestamp_source)
            if isinstance(require_timestamp_source, Mapping)
            else require_timestamp_source
        ),
        "require_robot_timestamp_source": (
            dict(require_robot_timestamp_source)
            if isinstance(require_robot_timestamp_source, Mapping)
            else require_robot_timestamp_source
        ),
        "calibration_sync_policy": (
            dict(calibration_sync_policy)
            if calibration_sync_policy is not None
            else None
        ),
        "sensors": sensors,
    }


def sync_quality_report_path(run_root: str | Path) -> Path:
    return Path(run_root) / SYNC_QUALITY_REPORT


def verify_profile_bound_sync_evidence(
    run_root: str | Path,
    calibration_sync_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Recheck derived sync reports against the selected calibration timing.

    The saved run-level report is required as operator evidence, but downstream
    stages also rebuild its strict timing checks from the current per-camera
    reports. This prevents a stale or manually produced quality report from
    authorizing rectification/export.
    """

    root = Path(run_root)
    path = sync_quality_report_path(root)
    if not path.is_file():
        raise FileNotFoundError(
            "Profile-bound synchronization requires sync_quality_report.json"
        )
    saved = _read_json(path)
    if saved.get("calibration_sync_policy") != dict(calibration_sync_policy):
        raise ValueError(
            "Saved sync quality evidence is not bound to the selected "
            "calibration timing policy"
        )
    policy_sensors = calibration_sync_policy.get("sensors")
    if not isinstance(policy_sensors, list):
        raise ValueError("calibration_sync_policy.sensors must be a list")
    frame_sources: dict[str, str] = {}
    robot_sources: dict[str, str] = {}
    nearest_thresholds: dict[str, float] = {}
    for sensor in policy_sensors:
        if not isinstance(sensor, Mapping):
            raise ValueError("calibration_sync_policy sensor rows must be objects")
        folder = str(sensor["sensor_folder"])
        frame_sources[folder] = str(sensor["frame_timestamp_source"])
        robot_sources[folder] = str(sensor["robot_timestamp_source"])
        nearest_thresholds[folder] = float(sensor["max_nearest_pose_delta_ms"])

    rebuilt = build_sync_quality_report(
        root,
        min_match_ratio=float(saved.get("min_match_ratio", 0.8)),
        max_dropped_frames=(
            int(saved["max_dropped_frames"])
            if saved.get("max_dropped_frames") is not None
            else None
        ),
        max_nearest_pose_delta_ms=nearest_thresholds,
        require_timestamp_source=frame_sources,
        require_robot_timestamp_source=robot_sources,
        calibration_sync_policy=calibration_sync_policy,
    )
    failures = [
        str(check.get("message"))
        for check in rebuilt["checks"]
        if check.get("status") == "error"
    ]
    if failures:
        raise ValueError(
            "Profile-bound synchronization evidence failed: " + "; ".join(failures)
        )
    if saved.get("overall_status") == "error":
        raise ValueError("Saved sync quality evidence has error status")
    return {
        "sync_quality_report": _relative(path, root),
        "overall_status": rebuilt["overall_status"],
        "bundle_sha256": calibration_sync_policy.get("bundle_sha256"),
        "sensor_count": rebuilt["sensor_count"],
        "sensors": rebuilt["sensors"],
    }


def write_sync_quality_report(
    run_root: str | Path,
    report: Mapping[str, Any],
) -> Path:
    path = sync_quality_report_path(run_root)
    return atomic_write_json(path, dict(report))


def write_sync_quality_report_with_manifest(
    run_root: str | Path,
    *,
    min_match_ratio: float = 0.8,
    max_dropped_frames: int | None = None,
    max_nearest_pose_delta_ms: float | Mapping[str, float] | None = 50.0,
    require_timestamp_source: str | Mapping[str, str] | None = None,
    require_robot_timestamp_source: str | Mapping[str, str] | None = None,
    calibration_sync_policy: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    run_root_path = Path(run_root)
    manifest = load_or_create_run_manifest(run_root_path)
    upsert_stage(manifest, name="sync_quality", status="running")
    write_run_manifest(manifest, run_root_path)
    try:
        report = build_sync_quality_report(
            run_root_path,
            min_match_ratio=min_match_ratio,
            max_dropped_frames=max_dropped_frames,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
            require_timestamp_source=require_timestamp_source,
            require_robot_timestamp_source=require_robot_timestamp_source,
            calibration_sync_policy=calibration_sync_policy,
        )
        path = write_sync_quality_report(run_root_path, report)
        upsert_stage(
            manifest,
            name="sync_quality",
            status="succeeded" if report["overall_status"] != "error" else "failed",
            artifacts={SYNC_QUALITY_REPORT: path},
            run_root=run_root_path,
            message=f"Sync quality status: {report['overall_status']}.",
        )
        write_run_manifest(manifest, run_root_path)
    except Exception as exc:
        upsert_stage(manifest, name="sync_quality", status="failed", message=str(exc))
        write_run_manifest(manifest, run_root_path)
        raise
    return path, report
