"""Manifest-tracked RealSense-only capture smoke workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from posetestbot.io._report_checks import make_check as _check
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import REALSENSE_CAPTURE_SMOKE_REPORT
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    make_sensor_record,
    set_manifest_sensors,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root, normalize_inverted
from posetestbot.sensors.contracts import SensorDeviceInfo, SensorType
from posetestbot.sensors.discovery import discover_realsense_d435
from posetestbot.sensors.realsense import capture_realsense_rgbd
from posetestbot.sensors.registry import is_auto_device_id, sensor_folder_name


SCHEMA_VERSION = "realsense_capture_smoke.v1"
STAGE_NAME = "realsense_capture_smoke"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled_sensors(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    capture = config.get("capture", {})
    sensors = capture.get("sensors", []) if isinstance(capture, Mapping) else []
    return [
        dict(sensor)
        for sensor in sensors
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is True
    ]


def _status_from_checks(checks: list[Mapping[str, Any]]) -> str:
    return "failed" if any(check.get("status") == "error" for check in checks) else "ready"


def _capture_status(captures: list[Mapping[str, Any]]) -> str:
    return (
        "failed"
        if any(capture.get("status") != "succeeded" for capture in captures)
        else "succeeded"
    )


def _visible_serials(devices: list[SensorDeviceInfo]) -> set[str]:
    return {device.device_id for device in devices if device.connected}


def _device_dict(device: SensorDeviceInfo) -> dict[str, Any]:
    return {
        "sensor_type": device.sensor_type.value,
        "device_id": device.device_id,
        "display_name": device.display_name,
        "connected": device.connected,
        "metadata": dict(device.metadata),
    }


def _validate_realsense_smoke(
    *,
    run_root: Path,
    config: Mapping[str, Any],
    devices: list[SensorDeviceInfo],
    expected_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enabled = _enabled_sensors(config)
    realsense_sensors = [
        sensor
        for sensor in enabled
        if str(sensor.get("sensor_type")) == SensorType.REALSENSE_D435.value
    ]
    non_realsense = [
        sensor
        for sensor in enabled
        if str(sensor.get("sensor_type")) != SensorType.REALSENSE_D435.value
    ]
    configured_serials = [str(sensor.get("device_id", "")) for sensor in realsense_sensors]
    auto_serials = [serial for serial in configured_serials if is_auto_device_id(serial)]
    duplicate_serials = sorted(
        serial
        for serial in set(configured_serials)
        if configured_serials.count(serial) > 1
    )
    visible_serials = _visible_serials(devices)
    checks = [
        _check(
            "realsense_only_config",
            "ok" if not non_realsense else "error",
            (
                "Run config contains only enabled RealSense sensors."
                if not non_realsense
                else "RealSense smoke requires a RealSense-only run config."
            ),
            details={"non_realsense_count": len(non_realsense)},
        ),
        _check(
            "configured_realsense_count",
            "ok" if len(realsense_sensors) == expected_count else "error",
            (
                f"Run config has {expected_count} configured RealSense sensor(s)."
                if len(realsense_sensors) == expected_count
                else (
                    f"Run config has {len(realsense_sensors)} configured "
                    f"RealSense sensor(s); expected {expected_count}."
                )
            ),
            details={"configured_serials": configured_serials},
        ),
        _check(
            "explicit_realsense_serials",
            "ok" if not auto_serials else "error",
            (
                "All RealSense sensors use explicit serials."
                if not auto_serials
                else "RealSense smoke requires explicit serials, not auto/default."
            ),
            details={"auto_serials": auto_serials},
        ),
        _check(
            "duplicate_realsense_serials",
            "ok" if not duplicate_serials else "error",
            (
                "Configured RealSense serials are unique."
                if not duplicate_serials
                else "Configured RealSense serials must be unique."
            ),
            details={"duplicate_serials": duplicate_serials},
        ),
        _check(
            "visible_realsense_count",
            "ok" if len(visible_serials) >= expected_count else "error",
            (
                f"At least {expected_count} RealSense device(s) are visible."
                if len(visible_serials) >= expected_count
                else (
                    f"{len(visible_serials)} RealSense device(s) are visible; "
                    f"expected at least {expected_count}."
                )
            ),
            details={"visible_serials": sorted(visible_serials)},
        ),
    ]

    for serial in configured_serials:
        if is_auto_device_id(serial):
            continue
        checks.append(
            _check(
                f"visible_realsense:{serial}",
                "ok" if serial in visible_serials else "error",
                (
                    f"Configured RealSense {serial} is visible."
                    if serial in visible_serials
                    else f"Configured RealSense {serial} is not visible."
                ),
                details={"visible_serials": sorted(visible_serials)},
            )
        )

    folder_records = []
    folders: dict[str, int] = {}
    for sensor in realsense_sensors:
        serial = str(sensor.get("device_id", ""))
        if is_auto_device_id(serial):
            continue
        folder_name = sensor_folder_name(SensorType.REALSENSE_D435, serial)
        folders[folder_name] = folders.get(folder_name, 0) + 1
        folder_path = run_root / folder_name
        child_count = (
            sum(1 for _ in folder_path.iterdir())
            if folder_path.exists() and folder_path.is_dir()
            else 0
        )
        check_status = "error" if child_count else "ok"
        checks.append(
            _check(
                f"output_folder:{folder_name}",
                check_status,
                (
                    f"Planned output folder is fresh: {folder_name}."
                    if not child_count
                    else (
                        f"Planned output folder already contains {child_count} "
                        f"item(s): {folder_name}."
                    )
                ),
                details={
                    "path": folder_path.as_posix(),
                    "exists": folder_path.exists(),
                    "child_count": child_count,
                },
            )
        )
        folder_records.append(
            {
                "sensor": sensor,
                "serial": serial,
                "folder_name": folder_name,
                "folder_path": folder_path,
            }
        )
    for folder_name, count in sorted(folders.items()):
        if count > 1:
            checks.append(
                _check(
                    f"duplicate_output_folder:{folder_name}",
                    "error",
                    f"Multiple sensors map to output folder {folder_name}.",
                    details={"folder": folder_name, "sensor_count": count},
                )
            )
    return checks, folder_records


def build_realsense_capture_smoke_report(
    run_root: str | Path,
    *,
    expected_count: int = 3,
    fps: int = 6,
    max_frames: int = 30,
    warmup_frames: int = 10,
    preview: bool = False,
    discoverer: Callable[[], list[SensorDeviceInfo]] = discover_realsense_d435,
    capture_func: Callable[..., dict[str, Any]] = capture_realsense_rgbd,
) -> dict[str, Any]:
    """Run sequential RealSense smoke capture and return a report object."""

    if expected_count <= 0:
        raise ValueError("expected_count must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if max_frames <= 0:
        raise ValueError("max_frames must be positive for smoke capture")
    if warmup_frames < 0:
        raise ValueError("warmup_frames must be greater than or equal to 0")

    run_root_path = Path(run_root)
    config = load_run_config_for_run_root(run_root_path)
    devices: list[SensorDeviceInfo] = []
    checks: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    discovery_error: str | None = None
    try:
        devices = discoverer()
        checks.append(
            _check(
                "realsense_discovery",
                "ok",
                f"Discovered {len(devices)} RealSense device(s).",
                details={"visible_devices": [_device_dict(device) for device in devices]},
            )
        )
    except Exception as exc:
        discovery_error = f"{type(exc).__name__}: {exc}"
        checks.append(
            _check(
                "realsense_discovery",
                "error",
                f"RealSense discovery failed: {discovery_error}",
            )
        )

    validation_checks, folder_records = _validate_realsense_smoke(
        run_root=run_root_path,
        config=config,
        devices=devices,
        expected_count=expected_count,
    )
    checks.extend(validation_checks)
    status = _status_from_checks(checks)
    message = (
        "RealSense smoke capture is blocked by validation checks."
        if status == "failed"
        else "RealSense smoke capture validation passed."
    )

    if status != "failed":
        for record in folder_records:
            serial = record["serial"]
            folder_path = record["folder_path"]
            inverted = normalize_inverted(record["sensor"].get("inverted", False))
            try:
                summary = capture_func(
                    folder_path,
                    device_id=serial,
                    fps=fps,
                    max_frames=max_frames,
                    warmup_frames=warmup_frames,
                    preview=preview,
                    record=True,
                    inverted=inverted,
                )
                frame_count = int(summary.get("frame_count") or 0)
                if frame_count != max_frames:
                    raise RuntimeError(
                        f"captured {frame_count} frame(s), expected {max_frames}"
                    )
                captures.append(
                    {
                        "status": "succeeded",
                        "sensor_id": serial,
                        "sensor_name": record["folder_name"],
                        "output_folder": folder_path.as_posix(),
                        "inverted": inverted,
                        "image_rotation_degrees": 180 if inverted else 0,
                        "summary": summary,
                    }
                )
                checks.append(
                    _check(
                        f"capture:{serial}",
                        "ok",
                        f"Captured {frame_count} frame(s) from {serial}.",
                        details={
                            "frame_count": frame_count,
                            "inverted": inverted,
                            "image_rotation_degrees": 180 if inverted else 0,
                        },
                    )
                )
            except Exception as exc:
                captures.append(
                    {
                        "status": "failed",
                        "sensor_id": serial,
                        "sensor_name": record["folder_name"],
                        "output_folder": folder_path.as_posix(),
                        "inverted": inverted,
                        "image_rotation_degrees": 180 if inverted else 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                checks.append(
                    _check(
                        f"capture:{serial}",
                        "error",
                        f"Capture failed for RealSense {serial}: {type(exc).__name__}: {exc}",
                    )
                )
                break
        status = _capture_status(captures)
        message = (
            "RealSense smoke capture succeeded."
            if status == "succeeded"
            else "RealSense smoke capture failed during sequential capture."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "run_root": run_root_path.as_posix(),
        "status": status,
        "message": message,
        "expected_count": expected_count,
        "fps": fps,
        "max_frames": max_frames,
        "warmup_frames": warmup_frames,
        "preview": preview,
        "discovery_error": discovery_error,
        "visible_devices": [_device_dict(device) for device in devices],
        "configured_sensors": _enabled_sensors(config),
        "checks": checks,
        "captures": captures,
    }


def realsense_capture_smoke_report_path(run_root: str | Path) -> Path:
    return Path(run_root) / REALSENSE_CAPTURE_SMOKE_REPORT


def write_realsense_capture_smoke_report(
    run_root: str | Path,
    report: Mapping[str, Any],
) -> Path:
    path = realsense_capture_smoke_report_path(run_root)
    return atomic_write_json(path, dict(report))


def write_realsense_capture_smoke_with_manifest(
    run_root: str | Path,
    *,
    expected_count: int = 3,
    fps: int = 6,
    max_frames: int = 30,
    warmup_frames: int = 10,
    preview: bool = False,
    discoverer: Callable[[], list[SensorDeviceInfo]] = discover_realsense_d435,
    capture_func: Callable[..., dict[str, Any]] = capture_realsense_rgbd,
) -> tuple[Path, dict[str, Any]]:
    run_root_path = Path(run_root)
    manifest = load_or_create_run_manifest(run_root_path)
    upsert_stage(manifest, name=STAGE_NAME, status="running")
    write_run_manifest(manifest, run_root_path)

    report = build_realsense_capture_smoke_report(
        run_root_path,
        expected_count=expected_count,
        fps=fps,
        max_frames=max_frames,
        warmup_frames=warmup_frames,
        preview=preview,
        discoverer=discoverer,
        capture_func=capture_func,
    )
    path = write_realsense_capture_smoke_report(run_root_path, report)

    sensor_records = []
    capture_by_serial = {
        str(capture.get("sensor_id")): capture for capture in report.get("captures", [])
    }
    for sensor in report.get("configured_sensors", []):
        if not isinstance(sensor, Mapping):
            continue
        if str(sensor.get("sensor_type")) != SensorType.REALSENSE_D435.value:
            continue
        serial = str(sensor.get("device_id", ""))
        if is_auto_device_id(serial):
            continue
        folder = run_root_path / sensor_folder_name(SensorType.REALSENSE_D435, serial)
        capture = capture_by_serial.get(serial)
        status = "captured" if capture and capture.get("status") == "succeeded" else "planned"
        if capture and capture.get("status") == "failed":
            status = "failed"
        inverted = normalize_inverted(sensor.get("inverted", False))
        sensor_records.append(
            make_sensor_record(
                sensor_type=SensorType.REALSENSE_D435,
                device_id=serial,
                folder=folder,
                run_root=run_root_path,
                display_name=str(sensor.get("display_name") or f"RealSense {serial}"),
                mounting_mode=str(sensor.get("mounting_mode") or ""),
                status=status,
                metadata={
                    "realsense_capture_smoke": True,
                    "inverted": inverted,
                    "image_rotation_degrees": 180 if inverted else 0,
                    "frame_count": (
                        capture.get("summary", {}).get("frame_count")
                        if isinstance(capture, Mapping)
                        and isinstance(capture.get("summary"), Mapping)
                        else None
                    ),
                },
            )
        )
    if sensor_records:
        set_manifest_sensors(manifest, sensor_records)
    upsert_stage(
        manifest,
        name=STAGE_NAME,
        status="succeeded" if report["status"] == "succeeded" else "failed",
        artifacts={REALSENSE_CAPTURE_SMOKE_REPORT: path},
        run_root=run_root_path,
        message=report["message"],
    )
    write_run_manifest(manifest, run_root_path)
    return path, report
