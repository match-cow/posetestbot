"""Bootstrap and run-discovery endpoints for the operator console."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from posetestbot.config import DEFAULT_ROBOT_PORT, LAB_ROBOT_IP
from posetestbot.io.artifacts import DATASET_MANIFEST, RUN_CONFIG
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.cell.scene import (
    build_cell_scene,
    cell_calibration_target_pdf_path,
    cell_camera_frame_path,
    cell_depth_frame_preview_png,
    cell_timeline_page,
)
from posetestbot.pose_templates.selection import load_pose_template_selection
from posetestbot.run_folders import MOVE_STAGING_PREFIX
from posetestbot.web.security import (
    DEFAULT_RUN_ROOT,
    resolve_web_run_root,
    web_run_roots,
)


ui_bp = Blueprint("ui", __name__)

GIB = 1024**3
STORAGE_CRITICAL_FREE_BYTES_CAP = 100 * GIB
STORAGE_WARNING_FREE_BYTES_CAP = 500 * GIB
STORAGE_CRITICAL_FREE_FRACTION = 0.05
STORAGE_WARNING_FREE_FRACTION = 0.15
CELL_POSE_TEMPLATE_ASSET_KEYS = {
    "mesh": "canonical_ply",
    "texture": "texture",
}


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def default_web_run_root() -> Path:
    """Return the initial run folder advertised to the browser."""

    configured = os.environ.get("POSETESTBOT_WEB_DEFAULT_RUN_ROOT")
    if configured:
        return resolve_web_run_root(configured)
    candidate = DEFAULT_RUN_ROOT / "test_run"
    try:
        return resolve_web_run_root(candidate)
    except ValueError:
        return resolve_web_run_root(web_run_roots()[0] / "test_run")


def _modified_at(path: Path) -> tuple[float, str]:
    candidates = [path]
    config_path = path / RUN_CONFIG
    if config_path.is_file() and not config_path.is_symlink():
        candidates.append(config_path)
    timestamp = max(candidate.stat().st_mtime for candidate in candidates)
    value = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return timestamp, value.isoformat().replace("+00:00", "Z")


def _run_record(path: Path) -> dict[str, Any]:
    run_name = None
    run_id = None
    intent = None
    annotation_mode = None
    config_valid = False
    config_error = None
    try:
        config = load_run_config_for_run_root(path)
        raw_run_name = config.get("run_name")
        if isinstance(raw_run_name, str) and raw_run_name.strip():
            run_name = raw_run_name.strip()
        run_id = str(config["run_id"])
        intent = str(config["capture"]["intent"])
        annotation_mode = str(config["bop"]["annotation_mode"])
        config_valid = True
    except (FileNotFoundError, OSError, ValueError) as exc:
        config_error = str(exc)

    sort_timestamp, modified_at = _modified_at(path)
    return {
        "path": path.as_posix(),
        "name": path.name,
        "run_name": run_name,
        "run_id": run_id,
        "intent": intent,
        "annotation_mode": annotation_mode,
        "config_valid": config_valid,
        "config_error": config_error,
        "modified_at": modified_at,
        "_sort_timestamp": sort_timestamp,
    }


def discover_web_runs() -> list[dict[str, Any]]:
    """List direct, contained run directories without following symlinks."""

    records: dict[str, dict[str, Any]] = {}
    for allowed_root in web_run_roots():
        if not allowed_root.is_dir():
            continue
        for candidate in allowed_root.iterdir():
            try:
                if candidate.name == "calibration_targets" or candidate.name.startswith(
                    MOVE_STAGING_PREFIX
                ):
                    continue
                if candidate.is_symlink() or not candidate.is_dir():
                    continue
                if not any(
                    (candidate / artifact).is_file()
                    for artifact in (RUN_CONFIG, DATASET_MANIFEST)
                ):
                    continue
                resolved = candidate.resolve()
                if not _is_below(resolved, allowed_root):
                    continue
                record = _run_record(resolved)
            except OSError:
                # Allowed roots such as /tmp may contain service-private folders
                # that are intentionally not traversable by the web process.
                continue
            records[resolved.as_posix()] = record

    ordered = sorted(
        records.values(),
        key=lambda item: (-item["_sort_timestamp"], item["path"]),
    )
    for item in ordered:
        item.pop("_sort_timestamp", None)
    return ordered


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    if not candidate.exists():
        raise FileNotFoundError(f"No existing filesystem path for {path}")
    return candidate


def _filesystem_mount_path(path: Path) -> Path:
    candidate = path if path.is_dir() else path.parent
    while candidate.parent != candidate and not os.path.ismount(candidate):
        candidate = candidate.parent
    return candidate


def _storage_thresholds(total_bytes: int | None) -> dict[str, int | float]:
    critical_free_bytes = STORAGE_CRITICAL_FREE_BYTES_CAP
    warning_free_bytes = STORAGE_WARNING_FREE_BYTES_CAP
    if total_bytes is not None:
        critical_free_bytes = min(
            critical_free_bytes,
            int(total_bytes * STORAGE_CRITICAL_FREE_FRACTION),
        )
        warning_free_bytes = min(
            warning_free_bytes,
            int(total_bytes * STORAGE_WARNING_FREE_FRACTION),
        )
    return {
        "critical_free_bytes": critical_free_bytes,
        "warning_free_bytes": warning_free_bytes,
        "critical_free_bytes_cap": STORAGE_CRITICAL_FREE_BYTES_CAP,
        "warning_free_bytes_cap": STORAGE_WARNING_FREE_BYTES_CAP,
        "critical_free_fraction": STORAGE_CRITICAL_FREE_FRACTION,
        "warning_free_fraction": STORAGE_WARNING_FREE_FRACTION,
    }


def run_storage_status(run_root: Path) -> dict[str, Any]:
    """Report capacity for the filesystem that will contain the selected run."""

    try:
        probe = _nearest_existing_path(run_root)
        usage = shutil.disk_usage(probe)
        if usage.total <= 0:
            raise OSError("Filesystem reported zero total capacity")
        thresholds = _storage_thresholds(usage.total)
        free_fraction = usage.free / usage.total
        if usage.free <= thresholds["critical_free_bytes"]:
            status = "error"
        elif usage.free <= thresholds["warning_free_bytes"]:
            status = "warning"
        else:
            status = "ready"
        return {
            "schema_version": "run_storage.v1",
            "run_root": run_root.as_posix(),
            "filesystem_path": _filesystem_mount_path(probe).as_posix(),
            "status": status,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_fraction": free_fraction,
            "thresholds": thresholds,
            "error": None,
        }
    except OSError as exc:
        thresholds = _storage_thresholds(None)
        return {
            "schema_version": "run_storage.v1",
            "run_root": run_root.as_posix(),
            "filesystem_path": None,
            "status": "unavailable",
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "free_fraction": None,
            "thresholds": thresholds,
            "error": f"{type(exc).__name__}: {exc}",
        }


@ui_bp.get("/ui/bootstrap")
def ui_bootstrap():
    return jsonify(
        {
            "schema_version": "web_bootstrap.v1",
            "brand": {
                "name": "PoseTestBot",
                "logo_url": "/assets/cow_light.png",
                "logo_urls": {
                    "light": "/assets/cow_light.png",
                    "dark": "/assets/cow_dark.png",
                },
                "favicon_url": "/assets/cow_favicon.png",
            },
            "robot": {
                "ip": LAB_ROBOT_IP,
                "port": DEFAULT_ROBOT_PORT,
            },
            "default_run_root": default_web_run_root().as_posix(),
            "allowed_run_roots": [root.as_posix() for root in web_run_roots()],
        }
    )


@ui_bp.get("/ui/runs")
def ui_runs():
    return jsonify(
        {
            "schema_version": "web_run_index.v1",
            "runs": discover_web_runs(),
        }
    )


def _requested_run_root() -> Path:
    value = request.args.get("run_root")
    if not value:
        raise ValueError("run_root is required")
    return resolve_web_run_root(value)


@ui_bp.get("/ui/storage")
def ui_storage():
    try:
        return jsonify(run_storage_status(_requested_run_root()))
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400


@ui_bp.get("/ui/cell-scene")
def ui_cell_scene():
    try:
        return jsonify(build_cell_scene(_requested_run_root()))
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


@ui_bp.get("/ui/cell-scene/timeline")
def ui_cell_timeline():
    timeline_id = request.args.get("timeline_id")
    if not timeline_id:
        return jsonify({"output": "timeline_id is required"}), 400
    try:
        payload = cell_timeline_page(
            _requested_run_root(),
            timeline_id,
            offset=int(request.args.get("offset", "0")),
            limit=int(request.args.get("limit", str(2_000))),
        )
        return jsonify(payload)
    except KeyError as exc:
        return jsonify({"output": str(exc)}), 404
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


@ui_bp.get("/ui/cell-scene/camera-frame")
def ui_cell_camera_frame():
    timeline_id = request.args.get("timeline_id")
    if not timeline_id:
        return jsonify({"output": "timeline_id is required"}), 400
    timeline_index = request.args.get("timeline_index")
    frame_id = request.args.get("frame_id")
    if timeline_index is None and frame_id is None:
        return jsonify({"output": "timeline_index or frame_id is required"}), 400
    modality = request.args.get("modality", "rgb")
    try:
        parsed_index = int(timeline_index) if timeline_index is not None else None
        if modality == "depth":
            return send_file(
                BytesIO(
                    cell_depth_frame_preview_png(
                        _requested_run_root(),
                        timeline_id,
                        parsed_index,
                        frame_id=frame_id,
                    )
                ),
                mimetype="image/png",
                download_name="depth-preview.png",
                conditional=True,
                max_age=3600,
            )
        return send_file(
            cell_camera_frame_path(
                _requested_run_root(),
                timeline_id,
                parsed_index,
                frame_id=frame_id,
                modality=modality,
            ),
            mimetype="image/png",
            conditional=True,
            max_age=3600,
        )
    except KeyError as exc:
        return jsonify({"output": str(exc)}), 404
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


@ui_bp.get("/ui/cell-calibration-target-pdf")
def ui_cell_calibration_target_pdf():
    try:
        return send_file(
            cell_calibration_target_pdf_path(_requested_run_root()),
            mimetype="application/pdf",
            conditional=True,
            max_age=3600,
        )
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400


@ui_bp.get("/ui/cell-pose-template-assets/<instance_uuid>/<asset_kind>")
def ui_cell_pose_template_asset(instance_uuid: str, asset_kind: str):
    try:
        run_root = _requested_run_root()
        selection = load_pose_template_selection(run_root)
        item = next(
            (
                entry
                for entry in selection["instances"]
                if entry["instance_uuid"] == instance_uuid
            ),
            None,
        )
        if item is None:
            return jsonify({"output": "Unknown selected template instance"}), 404
        key = CELL_POSE_TEMPLATE_ASSET_KEYS.get(asset_kind)
        if key is None:
            return jsonify({"output": "Unknown instance asset kind"}), 404
        if key not in item["assets"]:
            return jsonify({"output": "Unknown or unavailable instance asset"}), 404
        snapshot = run_root / selection["bundle_snapshot"]
        path = snapshot / item["assets"][key]["path"]
        path.resolve(strict=True).relative_to(snapshot.resolve())
        return send_file(
            path,
            mimetype="image/png" if key == "texture" else "application/octet-stream",
            conditional=True,
            max_age=3600,
        )
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400
