"""Robot-pose stream cadence evidence and commissioning checks."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
    PROCESSED_DIR,
    RAW_ROBOT_EE_POSES,
    ROBOT_POSE_CADENCE_REPORT,
)
from posetestbot.robot.pose_receiver import STREAM_END_SOURCE_PACKET


CADENCE_REPORT_SCHEMA_VERSION = "robot_pose_cadence_report.v1"
DEFAULT_MINIMUM_MEDIAN_RATE_HZ = 50.0
DEFAULT_MAXIMUM_P95_GAP_MS = 25.0
DEFAULT_MAXIMUM_GAP_MS = 40.0
DEFAULT_REPORT_PATH = Path(PROCESSED_DIR) / ROBOT_POSE_CADENCE_REPORT


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot calculate a percentile without values")
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _ordered_records(raw_poses: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    indexed: list[tuple[int, Mapping[str, Any]]] = []
    for key, value in raw_poses.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"Robot pose record {key!r} must be an object")
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Robot pose record key {key!r} must be an integer"
            ) from exc
        indexed.append((index, value))
    indexed.sort(key=lambda item: item[0])
    return [record for _, record in indexed]


def _is_motion_sample(record: Mapping[str, Any]) -> bool:
    motion = record.get("motion")
    return (
        isinstance(motion, str)
        and bool(motion)
        and motion != "end"
        and not motion.endswith("_settled")
    )


def _timestamp_value(
    record: Mapping[str, Any],
    field: str,
    *,
    source_packet: bool,
) -> int | None:
    container: Mapping[str, Any] = record
    if source_packet:
        value = record.get("source_packet")
        if not isinstance(value, Mapping):
            return None
        container = value
    timestamp = container.get(field)
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        return None
    return timestamp


def _motion_intervals_ms(
    records: Sequence[Mapping[str, Any]],
    timestamp: Callable[[Mapping[str, Any]], int | None],
) -> tuple[list[float], int, int]:
    intervals_ms: list[float] = []
    motion_sample_count = 0
    motion_segment_count = 0
    previous_motion: str | None = None
    previous_timestamp: int | None = None

    for record in records:
        if not _is_motion_sample(record):
            previous_motion = None
            previous_timestamp = None
            continue
        motion = str(record["motion"])
        current_timestamp = timestamp(record)
        motion_sample_count += 1
        if motion != previous_motion:
            motion_segment_count += 1
            previous_motion = motion
            previous_timestamp = current_timestamp
            continue
        if previous_timestamp is None or current_timestamp is None:
            previous_timestamp = current_timestamp
            continue
        delta_ns = current_timestamp - previous_timestamp
        if delta_ns <= 0:
            raise ValueError(
                f"Robot pose timestamps must increase within motion {motion!r}"
            )
        intervals_ms.append(delta_ns / 1_000_000.0)
        previous_timestamp = current_timestamp

    return intervals_ms, motion_sample_count, motion_segment_count


def _cadence_summary(
    intervals_ms: Sequence[float],
    *,
    motion_sample_count: int,
    motion_segment_count: int,
) -> dict[str, Any]:
    if not intervals_ms:
        return {
            "available": False,
            "motion_sample_count": motion_sample_count,
            "motion_segment_count": motion_segment_count,
            "interval_count": 0,
            "reason": "No consecutive timestamped samples within one motion.",
        }
    mean_gap_ms = statistics.fmean(intervals_ms)
    median_gap_ms = statistics.median(intervals_ms)
    return {
        "available": True,
        "motion_sample_count": motion_sample_count,
        "motion_segment_count": motion_segment_count,
        "interval_count": len(intervals_ms),
        "minimum_gap_ms": min(intervals_ms),
        "median_gap_ms": median_gap_ms,
        "mean_gap_ms": mean_gap_ms,
        "p95_gap_ms": _percentile(intervals_ms, 0.95),
        "p99_gap_ms": _percentile(intervals_ms, 0.99),
        "maximum_gap_ms": max(intervals_ms),
        "median_rate_hz": 1000.0 / median_gap_ms,
        "mean_rate_hz": 1000.0 / mean_gap_ms,
    }


def analyze_robot_pose_cadence(
    raw_poses: Mapping[str, Any],
    *,
    minimum_median_rate_hz: float = DEFAULT_MINIMUM_MEDIAN_RATE_HZ,
    maximum_p95_gap_ms: float = DEFAULT_MAXIMUM_P95_GAP_MS,
    maximum_gap_ms: float = DEFAULT_MAXIMUM_GAP_MS,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Summarize in-motion sender and end-to-end host receive cadence."""

    for name, value in (
        ("minimum_median_rate_hz", minimum_median_rate_hz),
        ("maximum_p95_gap_ms", maximum_p95_gap_ms),
        ("maximum_gap_ms", maximum_gap_ms),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and greater than zero")

    records = _ordered_records(raw_poses)
    host_intervals, motion_sample_count, motion_segment_count = _motion_intervals_ms(
        records,
        lambda record: _timestamp_value(
            record, "host_received_timestamp_ns", source_packet=False
        ),
    )
    host_summary = _cadence_summary(
        host_intervals,
        motion_sample_count=motion_sample_count,
        motion_segment_count=motion_segment_count,
    )

    sender_intervals, sender_sample_count, sender_segment_count = _motion_intervals_ms(
        records,
        lambda record: _timestamp_value(
            record, "sender_monotonic_ns", source_packet=True
        ),
    )
    sender_summary = _cadence_summary(
        sender_intervals,
        motion_sample_count=sender_sample_count,
        motion_segment_count=sender_segment_count,
    )
    if sender_summary["available"] and len(sender_intervals) != len(host_intervals):
        sender_summary = {
            "available": False,
            "motion_sample_count": sender_sample_count,
            "motion_segment_count": sender_segment_count,
            "interval_count": len(sender_intervals),
            "reason": "Sender timestamps are missing from some in-motion packets.",
        }

    estimated_packets_lost = 0
    sender_target_periods: set[int] = set()
    for record in records:
        terminal = record.get(STREAM_END_SOURCE_PACKET)
        if isinstance(terminal, Mapping):
            terminal_lost = terminal.get("estimated_packets_lost")
            if (
                isinstance(terminal_lost, int)
                and not isinstance(terminal_lost, bool)
                and terminal_lost > 0
            ):
                estimated_packets_lost += terminal_lost
        source_packet = record.get("source_packet")
        if not isinstance(source_packet, Mapping):
            continue
        lost = source_packet.get("estimated_packets_lost")
        if isinstance(lost, int) and not isinstance(lost, bool) and lost > 0:
            estimated_packets_lost += lost
        target = source_packet.get("sender_target_period_ms")
        if isinstance(target, int) and not isinstance(target, bool) and target > 0:
            sender_target_periods.add(target)

    checks: list[dict[str, Any]] = []
    if not host_summary["available"]:
        checks.append(
            {
                "name": "host_receive_cadence_available",
                "passed": False,
                "observed": host_summary["reason"],
            }
        )
    else:
        checks.extend(
            [
                {
                    "name": "minimum_median_rate_hz",
                    "passed": host_summary["median_rate_hz"] >= minimum_median_rate_hz,
                    "observed": host_summary["median_rate_hz"],
                    "required": minimum_median_rate_hz,
                },
                {
                    "name": "maximum_p95_gap_ms",
                    "passed": host_summary["p95_gap_ms"] <= maximum_p95_gap_ms,
                    "observed": host_summary["p95_gap_ms"],
                    "required": maximum_p95_gap_ms,
                },
                {
                    "name": "maximum_gap_ms",
                    "passed": host_summary["maximum_gap_ms"] <= maximum_gap_ms,
                    "observed": host_summary["maximum_gap_ms"],
                    "required": maximum_gap_ms,
                },
            ]
        )

    report: dict[str, Any] = {
        "schema_version": CADENCE_REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "passed"
        if checks and all(check["passed"] for check in checks)
        else "failed",
        "thresholds": {
            "minimum_median_rate_hz": minimum_median_rate_hz,
            "maximum_p95_gap_ms": maximum_p95_gap_ms,
            "maximum_gap_ms": maximum_gap_ms,
        },
        "host_receive_cadence": host_summary,
        "sender_cadence": sender_summary,
        "sender_target_period_ms": (
            next(iter(sender_target_periods))
            if len(sender_target_periods) == 1
            else None
        ),
        "sender_target_period_values_ms": sorted(sender_target_periods),
        "estimated_packets_lost": estimated_packets_lost,
        "checks": checks,
    }
    if source_path is not None:
        report["source_path"] = source_path
    return report


def analyze_run_robot_pose_cadence(
    run_root: Path | str,
    *,
    minimum_median_rate_hz: float = DEFAULT_MINIMUM_MEDIAN_RATE_HZ,
    maximum_p95_gap_ms: float = DEFAULT_MAXIMUM_P95_GAP_MS,
    maximum_gap_ms: float = DEFAULT_MAXIMUM_GAP_MS,
    write: bool = False,
) -> tuple[dict[str, Any], Path | None]:
    """Load a run's raw poses and optionally write a derived cadence report."""

    import json

    root = Path(run_root).expanduser().resolve()
    raw_pose_path = root / RAW_ROBOT_EE_POSES
    raw_poses = json.loads(raw_pose_path.read_text(encoding="utf-8"))
    if not isinstance(raw_poses, Mapping):
        raise ValueError(f"{raw_pose_path} must contain a JSON object")
    report = analyze_robot_pose_cadence(
        raw_poses,
        minimum_median_rate_hz=minimum_median_rate_hz,
        maximum_p95_gap_ms=maximum_p95_gap_ms,
        maximum_gap_ms=maximum_gap_ms,
        source_path=RAW_ROBOT_EE_POSES,
    )
    if not write:
        return report, None
    report_path = root / DEFAULT_REPORT_PATH
    atomic_write_json(report_path, report, indent=2, sort_keys=False)
    return report, report_path
