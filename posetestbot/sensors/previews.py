"""Queued live RGB preview helpers for web/API surfaces."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from posetestbot.web.paths import APP_ROOT


DEFAULT_SENSOR_PREVIEW_ROOT = APP_ROOT / "working_data" / "sensor_previews"
PREVIEW_STATUS_NAME = "preview_status.json"
PREVIEW_IMAGE_NAME = "latest.jpg"
PREVIEW_STOP_NAME = "stop"
PREVIEW_STATUS_SCHEMA = "sensor_rgb_preview.v1"
PREVIEW_HEARTBEAT_STALE_S = 5.0


def preview_stream_root(
    preview_root: str | Path = DEFAULT_SENSOR_PREVIEW_ROOT,
    *,
    preview_id: str | None = None,
) -> Path:
    return Path(preview_root) / (preview_id or uuid.uuid4().hex[:12])


def build_preview_command(
    *,
    preview_root: str | Path,
    spec: Mapping[str, Any],
    fps: int = 6,
    width: int = 640,
    height: int = 480,
    jpeg_quality: int = 82,
) -> list[str]:
    return [
        "uv",
        "run",
        "python",
        "scripts/stream_sensor_rgb_preview.py",
        Path(preview_root).as_posix(),
        "--sensor-json",
        json.dumps(dict(spec), sort_keys=True),
        "--fps",
        str(fps),
        "--width",
        str(width),
        "--height",
        str(height),
        "--jpeg-quality",
        str(jpeg_quality),
    ]


def load_preview_status(preview_root: str | Path) -> dict[str, Any] | None:
    path = Path(preview_root) / PREVIEW_STATUS_NAME
    if not path.is_file():
        return None
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def preview_status_health(
    preview_root: str | Path,
    status: Mapping[str, Any] | None,
    *,
    now_epoch_s: float | None = None,
) -> tuple[bool, str | None]:
    """Reject running preview artifacts whose worker heartbeat stopped."""

    if status is None:
        return False, "Preview worker has not published status."
    if status.get("schema_version") != PREVIEW_STATUS_SCHEMA:
        return False, "Preview worker status schema is not supported."
    if status.get("status") in {"failed", "stopped", "error"}:
        return False, str(status.get("error") or "Preview worker is not running.")
    heartbeat_epoch: float | None = None
    heartbeat = status.get("heartbeat_at")
    if isinstance(heartbeat, str):
        try:
            from datetime import datetime

            heartbeat_epoch = datetime.fromisoformat(
                heartbeat.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            heartbeat_epoch = None
    if heartbeat_epoch is None:
        return False, "Preview heartbeat is missing or malformed."
    age = max(0.0, (now_epoch_s or time.time()) - heartbeat_epoch)
    if age > PREVIEW_HEARTBEAT_STALE_S:
        return False, f"Preview heartbeat is stale ({age:.1f}s old)."
    return True, None


def stop_preview(preview_root: str | Path) -> Path:
    path = Path(preview_root) / PREVIEW_STOP_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n")
    return path


def resolve_preview_image(preview_root: str | Path) -> Path:
    root = Path(preview_root).resolve()
    path = (root / PREVIEW_IMAGE_NAME).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Preview image path escapes preview root") from exc
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError("Preview image path must be a JPEG")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
