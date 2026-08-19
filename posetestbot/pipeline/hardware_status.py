"""Run-scoped read-only hardware and runtime status snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from posetestbot.io._report_checks import (
    make_check as _check,
    overall_status as _overall_status,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import HARDWARE_STATUS_REPORT
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.robot.status import collect_robot_status
from posetestbot.runtime.status import collect_runtime_status
from posetestbot.sensors.status import collect_sensor_status


SCHEMA_VERSION = "hardware_status_report.v1"


def _sensor_family_checks(sensor_status: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for family in sensor_status.get("families", []):
        if not isinstance(family, Mapping):
            continue
        sensor_type = str(family.get("sensor_type", "unknown"))
        connected_count = family.get("connected_count", 0)
        capture_ready_count = family.get("capture_ready_count", connected_count)
        if family.get("error"):
            checks.append(
                _check(
                    f"sensor:{sensor_type}",
                    "error",
                    f"{family.get('display_name', sensor_type)} discovery failed.",
                    details={"error": family.get("error")},
                )
            )
            continue
        if not family.get("sdk_available", False):
            checks.append(
                _check(
                    f"sensor:{sensor_type}",
                    "warning",
                    f"{family.get('display_name', sensor_type)} SDK is not available.",
                    details={"sdk_module": family.get("sdk_module")},
                )
            )
            continue
        if family.get("meets_expected") is False:
            checks.append(
                _check(
                    f"sensor:{sensor_type}",
                    "warning",
                    (
                        f"{family.get('display_name', sensor_type)} capture-ready "
                        f"{capture_ready_count} / expected "
                        f"{family.get('expected_count')}."
                    ),
                    details={
                        "connected_count": connected_count,
                        "capture_ready_count": capture_ready_count,
                        "expected_count": family.get("expected_count"),
                    },
                )
            )
            continue
        checks.append(
            _check(
                f"sensor:{sensor_type}",
                "ok",
                (
                    f"{family.get('display_name', sensor_type)} status is ready "
                    f"with {capture_ready_count} capture-ready device(s) "
                    f"from {connected_count} detected record(s)."
                ),
                details={
                    "connected_count": connected_count,
                    "capture_ready_count": capture_ready_count,
                    "expected_count": family.get("expected_count"),
                },
            )
        )
    return checks


def _runtime_checks(runtime_status: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for runtime in runtime_status.get("runtimes", []):
        if not isinstance(runtime, Mapping):
            continue
        runtime_id = str(runtime.get("runtime_id", "unknown"))
        available = bool(runtime.get("available", False))
        checks.append(
            _check(
                f"runtime:{runtime_id}",
                "ok" if available else "warning",
                (
                    f"{runtime.get('display_name', runtime_id)} is available."
                    if available
                    else f"{runtime.get('display_name', runtime_id)} is not available."
                ),
                details={
                    "category": runtime.get("category"),
                    "required_for": runtime.get("required_for"),
                },
            )
        )
    return checks


def _collect_robot_status_for_run(
    run_root: Path,
    collect_robot: Callable[..., dict],
) -> dict[str, Any]:
    return collect_robot()


def build_hardware_status_report(
    run_root: str | Path,
    *,
    include_sensor_status: bool = True,
    include_runtime_status: bool = True,
    collect_robot: Callable[..., dict] = collect_robot_status,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    collect_runtimes: Callable[[], dict] = collect_runtime_status,
) -> dict[str, Any]:
    """Collect a read-only hardware snapshot for a run."""

    run_root_path = Path(run_root)
    robot = _collect_robot_status_for_run(run_root_path, collect_robot)
    sensors = collect_sensors() if include_sensor_status else None
    runtimes = collect_runtimes() if include_runtime_status else None

    selected_profile = robot.get("selected_profile", {})
    selected_mode = (
        selected_profile.get("mode")
        if isinstance(selected_profile, Mapping)
        else None
    )
    checks = [
        _check(
            "robot_profile",
            "ok" if selected_mode == "real" else "error",
            (
                "Real iiwa profile is selected."
                if selected_mode == "real"
                else f"Unexpected iiwa profile {selected_mode!r}."
            ),
            details={"selected_mode": selected_mode},
        )
    ]

    if sensors is None:
        checks.append(
            _check(
                "sensor_status",
                "warning",
                "Sensor status collection was skipped.",
            )
        )
    else:
        expected_counts_requested = bool(sensors.get("expected_counts_requested")) or any(
            isinstance(family, Mapping) and family.get("expected_count") is not None
            for family in sensors.get("families", [])
        )
        checks.append(
            _check(
                "sensor_status",
                "ok"
                if sensors.get("all_expected_connected") or not expected_counts_requested
                else "warning",
                (
                    f"Detected {sensors.get('total_connected', 0)} connected sensor(s)."
                    if not expected_counts_requested
                    else (
                        f"Connected {sensors.get('total_connected', 0)} sensor(s); requested sensor counts are satisfied."
                    )
                    if sensors.get("all_expected_connected")
                    else (
                        f"Connected {sensors.get('total_connected', 0)} sensor(s); "
                        "one or more requested sensor counts are missing or blocked."
                    )
                ),
                details={
                    "total_connected": sensors.get("total_connected"),
                    "total_capture_ready": sensors.get(
                        "total_capture_ready",
                        sensors.get("total_connected"),
                    ),
                    "all_expected_connected": sensors.get("all_expected_connected"),
                    "expected_counts_requested": expected_counts_requested,
                },
            )
        )
        checks.extend(_sensor_family_checks(sensors))

    if runtimes is None:
        checks.append(
            _check(
                "runtime_status",
                "warning",
                "External runtime status collection was skipped.",
            )
        )
    else:
        checks.append(
            _check(
                "runtime_status",
                "ok" if runtimes.get("all_available") else "warning",
                (
                    f"{runtimes.get('available_count', 0)} of "
                    f"{runtimes.get('runtime_count', 0)} runtime(s) available."
                ),
                details={
                    "available_count": runtimes.get("available_count"),
                    "runtime_count": runtimes.get("runtime_count"),
                    "all_available": runtimes.get("all_available"),
                },
            )
        )
        checks.extend(_runtime_checks(runtimes))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_root": run_root_path.as_posix(),
        "overall_status": _overall_status(checks),
        "checks": checks,
        "robot_status": robot,
        "sensor_status": sensors,
        "runtime_status": runtimes,
        "include_sensor_status": include_sensor_status,
        "include_runtime_status": include_runtime_status,
    }


def hardware_status_report_path(run_root: str | Path) -> Path:
    return Path(run_root) / HARDWARE_STATUS_REPORT


def load_hardware_status_report(run_root: str | Path) -> dict[str, Any]:
    path = hardware_status_report_path(run_root)
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Hardware status report must be a JSON object: {path}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported hardware status report schema: "
            f"{value.get('schema_version')!r}"
        )
    return value


def write_hardware_status_report(
    run_root: str | Path,
    report: Mapping[str, Any],
) -> Path:
    path = hardware_status_report_path(run_root)
    return atomic_write_json(path, dict(report))


def write_hardware_status_report_with_manifest(
    run_root: str | Path,
    *,
    include_sensor_status: bool = True,
    include_runtime_status: bool = True,
    collect_robot: Callable[..., dict] = collect_robot_status,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    collect_runtimes: Callable[[], dict] = collect_runtime_status,
) -> tuple[Path, dict[str, Any]]:
    """Write ``hardware_status_report.json`` and record the manifest stage."""

    run_root_path = Path(run_root)
    manifest = load_or_create_run_manifest(run_root_path)
    upsert_stage(manifest, name="hardware_status", status="running")
    write_run_manifest(manifest, run_root_path)
    try:
        report = build_hardware_status_report(
            run_root_path,
            include_sensor_status=include_sensor_status,
            include_runtime_status=include_runtime_status,
            collect_robot=collect_robot,
            collect_sensors=collect_sensors,
            collect_runtimes=collect_runtimes,
        )
        path = write_hardware_status_report(run_root_path, report)
        robot_status = report.get("robot_status", {})
        if isinstance(robot_status, Mapping):
            selected = robot_status.get("selected_profile")
            if isinstance(selected, Mapping):
                manifest["robot_profile"] = dict(selected)
        upsert_stage(
            manifest,
            name="hardware_status",
            status="succeeded" if report["overall_status"] != "error" else "failed",
            artifacts={HARDWARE_STATUS_REPORT: path},
            run_root=run_root_path,
            message=f"Hardware status report: {report['overall_status']}.",
        )
        write_run_manifest(manifest, run_root_path)
    except Exception as exc:
        upsert_stage(
            manifest,
            name="hardware_status",
            status="failed",
            message=str(exc),
        )
        write_run_manifest(manifest, run_root_path)
        raise
    return path, report
