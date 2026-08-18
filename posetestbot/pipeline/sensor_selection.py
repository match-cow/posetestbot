"""Resolve enabled run-config sensors to their canonical capture folders."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.sensors.contracts import MountingMode
from posetestbot.sensors.registry import sensor_folder_name


def enabled_sensor_folder_names(
    run_root: str | Path,
) -> tuple[str, ...]:
    """Return enabled canonical folders from the required current run config."""

    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    return tuple(
        sensor_folder_name(str(sensor["sensor_type"]), str(sensor["device_id"]))
        for sensor in config["capture"]["sensors"]
        if sensor.get("enabled", True) is True
    )


def enabled_sensor_mounting_modes_by_folder(
    config: Mapping[str, Any],
) -> dict[str, MountingMode]:
    """Return authoritative enabled sensor mounts from a current run config."""

    capture = config.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("run_config.capture must be an object")
    sensors = capture.get("sensors")
    if not isinstance(sensors, list):
        raise ValueError("run_config.capture.sensors must be an array")

    modes: dict[str, MountingMode] = {}
    for index, sensor in enumerate(sensors):
        if not isinstance(sensor, Mapping):
            raise ValueError(f"run_config.capture.sensors[{index}] must be an object")
        if sensor.get("enabled", True) is not True:
            continue
        folder = sensor_folder_name(
            str(sensor.get("sensor_type") or ""),
            str(sensor.get("device_id") or ""),
        )
        if folder in modes:
            raise ValueError(
                f"run_config.capture.sensors contains duplicate folder {folder!r}"
            )
        modes[folder] = MountingMode(str(sensor.get("mounting_mode") or ""))
    return modes


def filter_enabled_sensor_folders(
    run_root: str | Path,
    folders: Iterable[Path],
) -> list[Path]:
    """Filter discovered folders using authoritative current participation."""

    discovered = list(folders)
    enabled = enabled_sensor_folder_names(run_root)
    enabled_names = set(enabled)
    return [folder for folder in discovered if folder.name in enabled_names]
