"""Run-level quality checks for non-destructive synchronization output."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from posetestbot.io._report_checks import (
    make_check as _check,
    overall_status as _overall_status,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
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


SCHEMA_VERSION = "sync_quality_report.v2"
SYNC_REPORT_SCHEMA_VERSION = "sync_report.v3"


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
    if not path.is_file():
        return False, None
    value = _read_json(path)
    if not value:
        return False, None
    total = 0
    for record in value.values():
        if not isinstance(record, Mapping):
            return False, None
        source_packet = record.get("source_packet")
        if not isinstance(source_packet, Mapping):
            return False, None
        loss = source_packet.get("estimated_packets_lost")
        if isinstance(loss, bool) or not isinstance(loss, int) or loss < 0:
            return False, None
        total += loss
    return True, total


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


def _sensor_summary(
    report_path: Path,
    report: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    total_frames = int(report.get("total_frames", 0))
    matched_frames = int(report.get("matched_frames", 0))
    dropped_frames = int(report.get("dropped_frames", 0))
    report_schema = str(report.get("schema_version") or "")
    if report_schema != SYNC_REPORT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported sync report schema: {report_schema!r}")
    required_fields = {
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
        "total_frames",
        "matched_frames",
        "dropped_frames",
        "outside_motion_interval_frame_count",
        "eligible_in_motion_frames",
        "matched_eligible_frames",
        "in_motion_exclusion_count",
        "unexplained_in_motion_exclusion_count",
        "robot_pose_packet_loss_audited",
        "robot_pose_packet_loss_count",
        "motion_intervals",
        "dropped",
    }
    missing_fields = sorted(required_fields - set(report))
    if missing_fields:
        raise ValueError(
            "sync_report.v3 is missing required fields: " + ", ".join(missing_fields)
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
            sensor = _sensor_summary(resolved, report, root)
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
    if sensor_packet_loss_audited:
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
