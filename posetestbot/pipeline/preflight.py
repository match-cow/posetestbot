"""Current run-level readiness evidence for canonical capture workflows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from posetestbot.calibration.target_library import validate_run_target_selection
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import CALIBRATION_PROFILE_SELECTION, RUN_PREFLIGHT_REPORT
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.pose_templates.selection import load_pose_template_selection
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    configured_sunrise_reference_frame_path,
)
from posetestbot.robot.status import collect_robot_status
from posetestbot.runtime.status import collect_runtime_status
from posetestbot.sensors.readiness import (
    probe_selected_sensor_readiness,
    selected_sensor_readiness_checks,
    selected_sensor_readiness_matches_config,
)
from posetestbot.sensors.status import collect_sensor_status


SCHEMA_VERSION = "run_preflight.v2"


def _check(
    name: str,
    status: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "message": message,
        "details": dict(details or {}),
    }


def _overall_status(checks: list[Mapping[str, Any]]) -> str:
    statuses = {str(check.get("status")) for check in checks}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


def load_run_preflight_report(run_root: str | Path) -> dict[str, Any] | None:
    path = Path(run_root) / RUN_PREFLIGHT_REPORT
    if not path.is_file():
        return None
    with open(path, "r") as handle:
        report = json.load(handle)
    if not isinstance(report, dict):
        raise ValueError(f"{RUN_PREFLIGHT_REPORT} must contain a JSON object")
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{RUN_PREFLIGHT_REPORT} schema_version must be {SCHEMA_VERSION}"
        )
    return report


def preflight_config_matches(
    report: Mapping[str, Any],
    config: Mapping[str, Any],
) -> bool:
    return report.get("config") == config


def run_preflight_queue_summary(
    run_root: str | Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Summarize whether current saved preflight evidence permits queueing."""

    path = Path(run_root) / RUN_PREFLIGHT_REPORT
    try:
        report = load_run_preflight_report(run_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "path": path.as_posix(),
            "exists": path.exists(),
            "overall_status": None,
            "matches_config": None,
            "ready_for_queue": False,
            "queue_blocker": "invalid_preflight",
            "error": str(exc),
        }
    if report is None:
        return {
            "path": path.as_posix(),
            "exists": False,
            "overall_status": None,
            "matches_config": None,
            "ready_for_queue": False,
            "queue_blocker": "missing_preflight",
        }

    matches_config = preflight_config_matches(report, config)
    overall_status = report.get("overall_status")
    if overall_status == "error":
        blocker = "failed_preflight"
    elif not matches_config:
        blocker = "stale_preflight"
    elif not selected_sensor_readiness_matches_config(
        report.get("selected_sensor_readiness"),
        config,
    ):
        blocker = "invalid_preflight"
    else:
        blocker = None
    return {
        "path": path.as_posix(),
        "exists": True,
        "overall_status": overall_status,
        "matches_config": matches_config,
        "ready_for_queue": blocker is None,
        "queue_blocker": blocker,
    }


def _enabled_sensors(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        sensor
        for sensor in config["capture"]["sensors"]
        if sensor.get("enabled", True) is True
    ]


def _sensor_counts(config: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sensor in _enabled_sensors(config):
        sensor_type = str(sensor["sensor_type"])
        counts[sensor_type] = counts.get(sensor_type, 0) + 1
    return counts


def _calibration_arrangement_check(
    config: Mapping[str, Any],
    target_selection: Mapping[str, Any],
) -> dict[str, Any]:
    enabled = _enabled_sensors(config)
    mountings = {str(sensor.get("mounting_mode") or "") for sensor in enabled}
    if not enabled:
        return _check(
            "calibration_arrangement",
            "error",
            "Calibration recording requires at least one enabled camera.",
        )
    if not mountings.issubset({"eye_in_hand", "static"}) or len(mountings) != 1:
        return _check(
            "calibration_arrangement",
            "error",
            "A calibration run must contain one explicit camera mounting group.",
            details={"camera_mounting_modes": sorted(mountings)},
        )

    camera_mounting = next(iter(mountings))
    calibration_mode = "eye_to_hand" if camera_mounting == "static" else "eye_in_hand"
    if (
        camera_mounting == "static"
        and configured_sunrise_reference_frame_path(config)
        != POSE_TEMPLATE_BASE_SUNRISE_PATH
    ):
        return _check(
            "calibration_arrangement",
            "error",
            "Static calibration requires PoseTemplateBase robot-pose provenance.",
            details={
                "required_sunrise_reference_frame_path": (
                    POSE_TEMPLATE_BASE_SUNRISE_PATH
                )
            },
        )
    expected_target_frame = (
        "robot_flange" if camera_mounting == "static" else "template_base"
    )
    recorded_target_frame = target_selection.get("mounting_frame")
    if recorded_target_frame != expected_target_frame:
        return _check(
            "calibration_arrangement",
            "error",
            (
                f"{calibration_mode} calibration requires the target mounted in "
                f"{expected_target_frame}; selection records "
                f"{recorded_target_frame!r}."
            ),
            details={
                "calibration_mode": calibration_mode,
                "camera_mounting_mode": camera_mounting,
                "expected_target_mounting_frame": expected_target_frame,
                "recorded_target_mounting_frame": recorded_target_frame,
            },
        )
    placement_mode = str(target_selection.get("placement_mode") or "")
    return _check(
        "calibration_arrangement",
        "ok",
        (
            f"{calibration_mode} arrangement is explicit and target provenance "
            "matches the camera mounting."
        ),
        details={
            "calibration_mode": calibration_mode,
            "camera_mounting_mode": camera_mounting,
            "target_mounting_frame": expected_target_frame,
            "placement_mode": placement_mode,
        },
    )


def _validate_calibration_intent(
    config: Mapping[str, Any], run_root: Path
) -> list[dict[str, Any]]:
    if config.get("calibration_target") is None:
        return [
            _check(
                "calibration_target_selection",
                "error",
                "Calibration capture requires a selected immutable target.",
            )
        ]
    try:
        selection = validate_run_target_selection(run_root)
    except Exception as exc:
        return [
            _check(
                "calibration_target_selection",
                "error",
                f"Selected calibration target is invalid: {type(exc).__name__}: {exc}",
            )
        ]
    return [
        _check(
            "calibration_target_selection",
            "ok",
            "Selected calibration target passed containment and hash checks.",
            details=selection,
        ),
        _calibration_arrangement_check(config, selection),
    ]


def _validate_dataset_intent(
    config: Mapping[str, Any], run_root: Path
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    has_selection = (
        config.get("calibration_profile_selection") is not None
        or (run_root / CALIBRATION_PROFILE_SELECTION).is_file()
    )
    if not has_selection:
        checks.append(
            _check(
                "calibration_profile_selection",
                "error",
                "Dataset capture requires a promoted reusable calibration selection.",
            )
        )
    else:
        try:
            from posetestbot.calibration.profile_library import (
                verify_calibration_profile_selection,
            )

            selection = verify_calibration_profile_selection(
                run_root,
                expected_calibration_profiles=config.get("calibration_profiles"),
                expected_intrinsic_calibration_profiles=config.get(
                    "intrinsic_calibration_profiles"
                ),
            )
            checks.append(
                _check(
                    "calibration_profile_selection",
                    "ok",
                    "Calibration snapshots, hashes, camera mapping, and provenance are verified.",
                    details=selection,
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "calibration_profile_selection",
                    "error",
                    f"Selected calibration is invalid: {type(exc).__name__}: {exc}",
                )
            )
        try:
            from posetestbot.sync.calibration_policy import (
                resolve_calibration_profile_sync_policy,
            )

            policy = resolve_calibration_profile_sync_policy(run_root)
            if policy is None:
                raise ValueError("selection has no hash-bound timing policy")
            checks.append(
                _check(
                    "calibration_profile_sync_policy",
                    "ok",
                    "Every enabled camera has verified hash-bound synchronization timing.",
                    details=policy,
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "calibration_profile_sync_policy",
                    "error",
                    f"Calibration timing cannot authorize synchronization: {exc}",
                )
            )

    if config["dataset_mode"] == "pose_template":
        try:
            pose_selection = load_pose_template_selection(run_root)
            if pose_selection.get("placement_confirmed") is not True:
                raise ValueError("pose-template placement is not operator-confirmed")
            checks.append(
                _check(
                    "pose_template_selection",
                    "ok",
                    "Immutable pose-template selection and placement are verified.",
                    details={
                        "template_uuid": pose_selection["template_uuid"],
                        "bundle_sha256": pose_selection["bundle_sha256"],
                        "instance_count": len(pose_selection["instances"]),
                    },
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "pose_template_selection",
                    "error",
                    f"Object dataset requires a valid pose-template selection: {exc}",
                )
            )
    return checks


def build_run_preflight(
    run_root: str | Path,
    *,
    include_sensor_status: bool = True,
    include_runtime_status: bool = True,
    collect_robot: Callable[[], dict] = collect_robot_status,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    collect_runtimes: Callable[[], dict] = collect_runtime_status,
    probe_selected_sensors: Callable[[Mapping[str, Any]], dict] = (
        probe_selected_sensor_readiness
    ),
) -> dict[str, Any]:
    """Build readiness evidence, including a no-write selected-camera open probe."""

    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    robot = collect_robot()
    sensors = collect_sensors() if include_sensor_status else None
    selected_sensor_readiness = (
        probe_selected_sensors(config) if include_sensor_status else None
    )
    runtimes = collect_runtimes() if include_runtime_status else None
    enabled = _enabled_sensors(config)
    selected_profile = robot.get("selected_profile", {})
    robot_matches = selected_profile.get("mode") == "real"
    checks = [
        _check(
            "run_root",
            "ok" if root.is_dir() else "error",
            f"Run root {'exists' if root.is_dir() else 'is missing'}: {root}",
            details={"run_root": root.as_posix()},
        ),
        _check(
            "run_config",
            "ok",
            (
                f"Loaded {config['schema_version']} for {config['capture']['intent']} "
                f"capture with {len(enabled)} enabled sensor(s)."
            ),
            details={
                "intent": config["capture"]["intent"],
                "configured_sensor_count": len(config["capture"]["sensors"]),
                "enabled_sensor_count": len(enabled),
                "sensor_counts": _sensor_counts(config),
            },
        ),
        _check(
            "robot_profile",
            "ok" if robot_matches else "error",
            (
                "Runtime status and run config use the sole lab iiwa profile."
                if robot_matches
                else "Runtime robot status does not report the sole lab iiwa profile."
            ),
            details={"selected_profile": selected_profile},
        ),
    ]

    if config["capture"]["intent"] == "calibration":
        checks.extend(_validate_calibration_intent(config, root))
    else:
        checks.extend(_validate_dataset_intent(config, root))

    if sensors is None:
        checks.append(
            _check(
                "sensor_status",
                "error",
                (
                    "Live sensor discovery and selected-camera open probes were "
                    "skipped; capture readiness cannot be established."
                ),
            )
        )
    else:
        detected = int(sensors.get("total_connected", 0))
        checks.append(
            _check(
                "sensor_status",
                "ok" if detected >= len(enabled) else "warning",
                f"Detected {detected} connected sensor(s) for {len(enabled)} enabled entries.",
                details={
                    "total_connected": detected,
                    "enabled_sensor_count": len(enabled),
                },
            )
        )
        checks.extend(
            selected_sensor_readiness_checks(
                selected_sensor_readiness,
                config=config,
            )
        )

    if runtimes is None:
        checks.append(
            _check(
                "runtime_status",
                "warning",
                "Optional runtime checks were intentionally skipped.",
            )
        )
    else:
        checks.append(
            _check(
                "runtime_status",
                "ok" if runtimes.get("all_available") else "warning",
                (
                    f"{runtimes.get('available_count', 0)} of "
                    f"{runtimes.get('runtime_count', 0)} optional runtime(s) available."
                ),
                details={
                    "available_count": runtimes.get("available_count", 0),
                    "runtime_count": runtimes.get("runtime_count", 0),
                    "all_available": bool(runtimes.get("all_available")),
                },
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_root": root.as_posix(),
        "overall_status": _overall_status(checks),
        "checks": checks,
        "config": config,
        "robot_status": robot,
        "sensor_status": sensors,
        "selected_sensor_readiness": selected_sensor_readiness,
        "runtime_status": runtimes,
    }


def write_run_preflight_report(
    run_root: str | Path,
    report: Mapping[str, Any],
    *,
    filename: str = RUN_PREFLIGHT_REPORT,
) -> Path:
    return atomic_write_json(Path(run_root) / filename, dict(report))


def write_run_preflight_with_manifest(
    run_root: str | Path,
    *,
    include_sensor_status: bool = True,
    include_runtime_status: bool = True,
    collect_robot: Callable[[], dict] = collect_robot_status,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    collect_runtimes: Callable[[], dict] = collect_runtime_status,
    probe_selected_sensors: Callable[[Mapping[str, Any]], dict] = (
        probe_selected_sensor_readiness
    ),
) -> tuple[Path, dict[str, Any]]:
    root = Path(run_root)
    manifest = load_or_create_run_manifest(root)
    upsert_stage(manifest, name="run_preflight", status="running")
    write_run_manifest(manifest, root)
    try:
        report = build_run_preflight(
            root,
            include_sensor_status=include_sensor_status,
            include_runtime_status=include_runtime_status,
            collect_robot=collect_robot,
            collect_sensors=collect_sensors,
            collect_runtimes=collect_runtimes,
            probe_selected_sensors=probe_selected_sensors,
        )
        path = write_run_preflight_report(root, report)
        manifest["robot_profile"] = dict(report["config"].get("robot_profile") or {})
        manifest["capture_config"] = dict(report["config"].get("capture") or {})
        upsert_stage(
            manifest,
            name="run_preflight",
            status="succeeded" if report["overall_status"] != "error" else "failed",
            artifacts={RUN_PREFLIGHT_REPORT: path},
            run_root=root,
            message=f"Run preflight status: {report['overall_status']}.",
        )
        write_run_manifest(manifest, root)
    except Exception as exc:
        upsert_stage(manifest, name="run_preflight", status="failed", message=str(exc))
        write_run_manifest(manifest, root)
        raise
    return path, report
