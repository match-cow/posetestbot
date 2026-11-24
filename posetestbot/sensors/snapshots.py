"""Queued sensor snapshot helpers for web/API surfaces."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from posetestbot.sensors.contracts import SensorType
from posetestbot.web.paths import APP_ROOT


DEFAULT_SENSOR_SNAPSHOT_ROOT = APP_ROOT / "working_data" / "sensor_snapshots"
SNAPSHOT_MANIFEST = "sensor_snapshot_manifest.json"


def snapshot_batch_root(
    snapshot_root: str | Path = DEFAULT_SENSOR_SNAPSHOT_ROOT,
    *,
    snapshot_id: str | None = None,
) -> Path:
    return Path(snapshot_root) / (snapshot_id or uuid.uuid4().hex[:12])


def sensor_key(sensor_type: str, device_id: str) -> str:
    return f"{SensorType(sensor_type).value}:{str(device_id).strip()}"


def snapshot_specs_from_status(
    sensor_status: Mapping[str, Any],
    *,
    selected: set[str] | None = None,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for family in sensor_status.get("families", []):
        if not isinstance(family, Mapping):
            continue
        for device in family.get("devices", []):
            if (
                not isinstance(device, Mapping)
                or not device.get("connected", True)
                or device.get("capture_ready") is False
            ):
                continue
            key = sensor_key(str(device.get("sensor_type")), str(device.get("device_id")))
            if selected is not None and key not in selected:
                continue
            specs.append(
                {
                    "sensor_type": str(device.get("sensor_type")),
                    "device_id": str(device.get("device_id")),
                    "display_name": device.get("display_name"),
                    "alias": device.get("alias"),
                    "effective_display_name": device.get("effective_display_name"),
                    "mounting_mode": device.get("mounting_mode"),
                    "inverted": bool(device.get("inverted", False)),
                    "metadata": device.get("metadata", {}),
                }
            )
    return specs


def build_snapshot_command(
    *,
    snapshot_root: str | Path,
    specs: list[Mapping[str, Any]],
    fps: int = 6,
    resolution: str = "720p",
    max_frames: int = 1,
) -> list[str]:
    command = [
        "uv",
        "run",
        "python",
        "scripts/capture_sensor_snapshot.py",
        Path(snapshot_root).as_posix(),
        "--fps",
        str(fps),
        "--resolution",
        resolution,
        "--max-frames",
        str(max_frames),
    ]
    for spec in specs:
        command.extend(["--sensor-json", json.dumps(dict(spec), sort_keys=True)])
    return command


def load_snapshot_manifest(snapshot_root: str | Path) -> dict[str, Any] | None:
    path = Path(snapshot_root) / SNAPSHOT_MANIFEST
    if not path.is_file():
        return None
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def resolve_snapshot_image(snapshot_root: str | Path, relative_path: str) -> Path:
    root = Path(snapshot_root).resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Snapshot image path escapes snapshot root") from exc
    if path.suffix.lower() != ".png":
        raise ValueError("Snapshot image path must be a PNG")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
