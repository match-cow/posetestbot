"""Detection-first sensor web routes."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from datetime import UTC, datetime
from typing import Any, Mapping

from flask import Blueprint, jsonify, request, send_file

from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pipeline.run_config import normalize_inverted
from posetestbot.sensors.aliases import (
    DEFAULT_SENSOR_ALIASES_PATH,
    save_sensor_aliases,
    sensor_alias_file_state,
)
from posetestbot.sensors.previews import (
    build_preview_command,
    load_preview_status,
    preview_stream_root,
    preview_status_health,
    resolve_preview_image,
    stop_preview,
)
from posetestbot.sensors.registry import get_sensor_adapter
from posetestbot.sensors.snapshots import (
    build_snapshot_command,
    load_snapshot_manifest,
    resolve_snapshot_image,
    snapshot_batch_root,
    snapshot_specs_from_status,
)
from posetestbot.sensors.status import collect_sensor_status, parse_expected_counts
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner


sensors_bp = Blueprint("sensors", __name__)
ACTIVE_JOB_STATUSES = {"queued", "running", "canceling"}
_preview_replacement_lock = threading.Lock()
_preview_replacement_job_ids: set[str] = set()


def _json_payload() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _status_from_request() -> tuple[
    dict[str, Any] | None, tuple[dict[str, str], int] | None
]:
    try:
        expected_counts = (
            parse_expected_counts(request.args.getlist("expected"))
            if request.args.getlist("expected")
            else None
        )
    except ValueError as exc:
        return None, ({"output": str(exc)}, 400)
    return collect_sensor_status(expected_counts=expected_counts), None


@sensors_bp.get("/sensors/status")
def sensor_status():
    status, error = _status_from_request()
    if error is not None:
        payload, code = error
        return jsonify(payload), code
    assert status is not None
    return jsonify(_include_preview_claimed_devices(status))


@sensors_bp.get("/sensors/aliases")
def get_sensor_aliases():
    response = jsonify(sensor_alias_file_state(DEFAULT_SENSOR_ALIASES_PATH))
    response.cache_control.no_store = True
    return response


@sensors_bp.put("/sensors/aliases")
@sensors_bp.patch("/sensors/aliases")
def put_sensor_aliases():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        # An unparsed body must not be read as "no aliases": that would
        # replace every saved lab alias with an empty file.
        return jsonify({"output": "A JSON object is required"}), 400
    aliases = data.get("aliases", data)
    if not isinstance(aliases, Mapping):
        return jsonify({"output": "aliases must be a JSON object"}), 400
    try:
        path = save_sensor_aliases(aliases, DEFAULT_SENSOR_ALIASES_PATH, merge=request.method == "PATCH")
    except (OSError, ValueError, TypeError) as exc:
        return jsonify({"output": str(exc)}), 400
    state = sensor_alias_file_state(path)
    response = jsonify({"output": f"Wrote {path}", **state})
    response.cache_control.no_store = True
    return response


def _requested_sensor_specs(
    data: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict]:
    explicit_sensors = data.get("sensors")
    if isinstance(explicit_sensors, list) and explicit_sensors:
        specs = [
            dict(sensor) for sensor in explicit_sensors if isinstance(sensor, Mapping)
        ]
        return specs, {"schema_version": "sensor_status.v1", "families": []}

    status = collect_sensor_status()
    raw_selected = data.get("selected") or data.get("sensor_keys") or []
    if isinstance(raw_selected, str):
        raw_selected = [raw_selected]
    selected = {str(value) for value in raw_selected} if raw_selected else None
    return snapshot_specs_from_status(status, selected=selected), status


def _sensor_key(spec: Mapping[str, Any]) -> str:
    return f"{spec.get('sensor_type')}:{spec.get('device_id')}"


def _camera_resource(spec: Mapping[str, Any]) -> str:
    return f"camera:{_sensor_key(spec)}"


def _preview_job_health(job: Any) -> tuple[bool, str | None]:
    if job.status == "queued":
        return True, None
    preview_root = job.parameters.get("preview_root")
    if not preview_root:
        return False, "Preview job is missing its artifact root."
    try:
        status = load_preview_status(preview_root)
    except (OSError, ValueError) as exc:
        return False, str(exc)
    if status is None:
        if job.started_at is not None:
            try:
                started = datetime.fromisoformat(job.started_at.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if (datetime.now(UTC) - started).total_seconds() <= 5.0:
                    return True, None
            except ValueError:
                pass
    return preview_status_health(preview_root, status)


def _cancel_preview_in_background(job: Any) -> None:
    def cancel() -> None:
        preview_root = job.parameters.get("preview_root")
        if preview_root:
            stop_preview(preview_root)
        try:
            job_runner.cancel(job.id)
        except KeyError:
            pass

    threading.Thread(
        target=cancel,
        name=f"stop-stale-preview-{job.id}",
        daemon=True,
    ).start()


def _active_preview_jobs_by_key() -> tuple[dict[str, Any], dict[str, Any]]:
    active = {}
    stale = {}
    for job in job_runner.active_jobs():
        if not job.parameters.get("sensor_preview"):
            continue
        if job.status not in ACTIVE_JOB_STATUSES:
            continue
        sensor_key = job.parameters.get("sensor_key")
        healthy, _reason = _preview_job_health(job)
        if sensor_key and healthy:
            active[str(sensor_key)] = job
        elif sensor_key:
            stale[str(sensor_key)] = job
    return active, stale


def _include_preview_claimed_devices(status: Mapping[str, Any]) -> dict[str, Any]:
    """Keep healthy preview-owned sensors visible while SDK discovery is exclusive.

    DepthAI discovery does not return an OAK device while another process owns
    it.  The live preview worker is direct evidence that the device is still
    connected, so merge its submitted sensor specification into the web status
    response until the preview releases it.
    """

    merged = dict(status)
    families = []
    families_by_type: dict[str, dict[str, Any]] = {}
    known_keys: set[str] = set()
    for raw_family in status.get("families", []):
        if not isinstance(raw_family, Mapping):
            continue
        family = dict(raw_family)
        devices = [
            dict(device)
            for device in raw_family.get("devices", [])
            if isinstance(device, Mapping)
        ]
        family["devices"] = devices
        sensor_type = str(family.get("sensor_type", ""))
        if sensor_type:
            families_by_type[sensor_type] = family
        for device in devices:
            known_keys.add(_sensor_key(device))
        families.append(family)

    active_by_key, _stale_by_key = _active_preview_jobs_by_key()
    for key, job in active_by_key.items():
        if key in known_keys:
            continue
        spec = job.parameters.get("sensor_spec")
        if not isinstance(spec, Mapping):
            continue
        sensor_type = str(spec.get("sensor_type", ""))
        family = families_by_type.get(sensor_type)
        if family is None:
            continue
        claimed_device = dict(spec)
        claimed_device["connected"] = True
        claimed_device["capture_ready"] = True
        claimed_device["capture_readiness_reason"] = None
        claimed_device["discovery_state"] = "claimed_by_preview"
        family["devices"].append(claimed_device)
        known_keys.add(key)

    for family in families:
        connected_count = sum(
            device.get("connected") is not False for device in family["devices"]
        )
        capture_ready_count = sum(
            device.get("connected") is not False
            and device.get("capture_ready") is not False
            for device in family["devices"]
        )
        family["connected_count"] = connected_count
        family["capture_ready_count"] = capture_ready_count
        expected_count = family.get("expected_count")
        if isinstance(expected_count, int):
            family["meets_expected"] = capture_ready_count >= expected_count
            diagnostics = family.get("diagnostics")
            if capture_ready_count >= expected_count and isinstance(diagnostics, list):
                family["diagnostics"] = [
                    item
                    for item in diagnostics
                    if not (
                        isinstance(item, Mapping)
                        and item.get("code") == "expected_count_not_met"
                    )
                ]

    merged["families"] = families
    merged["total_connected"] = sum(
        int(family.get("connected_count", 0)) for family in families
    )
    merged["total_capture_ready"] = sum(
        int(family.get("capture_ready_count", family.get("connected_count", 0)))
        for family in families
    )
    merged["all_expected_connected"] = all(
        family.get("meets_expected") is not False for family in families
    )
    return merged


def _preview_job_payload(job) -> dict[str, Any]:
    preview_root = job.parameters.get("preview_root")
    preview_status = None
    if preview_root:
        status_name = "waiting" if job.status in ACTIVE_JOB_STATUSES else job.status
        preview_status = {
            "status": status_name,
            "preview_root": preview_root,
            "sensor_key": job.parameters.get("sensor_key"),
            "sensor_type": job.parameters.get("sensor_type"),
            "device_id": job.parameters.get("device_id"),
            "inverted": job.parameters.get("inverted"),
            "frame_count": 0,
            "latest_image": None,
            "selected_node": None,
            "error": job.message if job.status == "failed" else None,
        }
        try:
            loaded = load_preview_status(preview_root)
            if loaded is not None:
                preview_status = loaded
        except (OSError, ValueError) as exc:
            preview_status.update({"status": "error", "error": str(exc)})
    return {
        "job": job.to_dict(),
        "preview_root": preview_root,
        "preview_status": preview_status,
    }


def _stop_preview_job(job) -> dict[str, Any]:
    preview_root = job.parameters.get("preview_root")
    if preview_root:
        stop_preview(preview_root)
    if job.status in ACTIVE_JOB_STATUSES:
        try:
            job = job_runner.cancel(job.id)
        except KeyError:
            pass
    return _preview_job_payload(job)


def _preview_submission(
    spec: Mapping[str, Any],
    *,
    fps: int,
    width: int,
    height: int,
    jpeg_quality: int,
) -> Any:
    key = _sensor_key(spec)
    adapter = get_sensor_adapter(str(spec.get("sensor_type", "")))
    if not adapter.live_rgb_preview_supported:
        raise ValueError(
            f"Live RGB preview is not supported for {adapter.display_name}."
        )
    preview_root = preview_stream_root()
    command = build_preview_command(
        preview_root=preview_root,
        spec=spec,
        fps=fps,
        width=width,
        height=height,
        jpeg_quality=jpeg_quality,
    )
    return job_runner.submit(
        name=f"sensor-preview:{key}",
        command=command,
        cwd=APP_ROOT,
        resources=[_camera_resource(spec)],
        scope_kind="global",
        parameters={
            "preview_root": preview_root.as_posix(),
            "sensor_key": key,
            "sensor_type": spec.get("sensor_type"),
            "device_id": spec.get("device_id"),
            "inverted": normalize_inverted(spec.get("inverted", False)),
            "sensor_spec": dict(spec),
            "sensor_preview": True,
        },
    )


def _schedule_preview_replacement(
    stale_job: Any,
    spec: Mapping[str, Any],
    *,
    fps: int,
    width: int,
    height: int,
    jpeg_quality: int,
) -> None:
    with _preview_replacement_lock:
        if stale_job.id in _preview_replacement_job_ids:
            return
        _preview_replacement_job_ids.add(stale_job.id)

    def replace() -> None:
        try:
            preview_root = stale_job.parameters.get("preview_root")
            if preview_root:
                stop_preview(preview_root)
            try:
                job_runner.cancel(stale_job.id)
            except KeyError:
                pass
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    current = job_runner.get(stale_job.id)
                except KeyError:
                    break
                if current.status not in ACTIVE_JOB_STATUSES:
                    break
                time.sleep(0.05)
            active, _stale = _active_preview_jobs_by_key()
            if _sensor_key(spec) not in active:
                try:
                    _preview_submission(
                        spec,
                        fps=fps,
                        width=width,
                        height=height,
                        jpeg_quality=jpeg_quality,
                    )
                except (ResourceBusyError, TypeError, ValueError):
                    pass
        finally:
            with _preview_replacement_lock:
                _preview_replacement_job_ids.discard(stale_job.id)

    threading.Thread(
        target=replace,
        name=f"replace-preview-{stale_job.id}",
        daemon=True,
    ).start()


@sensors_bp.post("/sensors/snapshots")
def post_sensor_snapshots():
    data = _json_payload()
    try:
        specs, status = _requested_sensor_specs(data)
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    if not specs:
        return jsonify({"output": "No connected sensors selected for snapshot"}), 400

    snapshot_root = snapshot_batch_root()
    try:
        command = build_snapshot_command(
            snapshot_root=snapshot_root,
            specs=specs,
            fps=int(data.get("fps", 6)),
            resolution=str(data.get("resolution", "720p")),
            max_frames=int(data.get("max_frames", 1)),
        )
        job = job_runner.submit(
            name="sensor-snapshot",
            command=command,
            cwd=APP_ROOT,
            resources=[*sorted({_camera_resource(spec) for spec in specs}), "disk_io"],
            scope_kind="global",
            parameters={
                "snapshot_root": snapshot_root.as_posix(),
                "sensor_keys": [
                    f"{spec.get('sensor_type')}:{spec.get('device_id')}"
                    for spec in specs
                ],
                "sensor_count": len(specs),
                "sensor_snapshot": True,
            },
        )
    except ResourceBusyError as exc:
        return jsonify({"output": str(exc)}), 409
    except (TypeError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400

    return (
        jsonify(
            {
                "output": f"Queued sensor snapshot as job {job.id}",
                "job_id": job.id,
                "status": job.status,
                "job": job.to_dict(),
                "snapshot_root": snapshot_root.as_posix(),
                "sensor_status": status,
                "sensors": specs,
            }
        ),
        202,
    )


@sensors_bp.post("/sensors/previews")
def post_sensor_previews():
    data = _json_payload()
    try:
        specs, status = _requested_sensor_specs(data)
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    if not specs:
        return jsonify({"output": "No connected sensors selected for preview"}), 400

    active_by_key, stale_by_key = _active_preview_jobs_by_key()
    jobs = []
    errors = []
    for spec in specs:
        key = _sensor_key(spec)
        existing = active_by_key.get(key)
        if existing is not None:
            jobs.append(_preview_job_payload(existing))
            continue

        stale = stale_by_key.get(key)
        if stale is not None:
            _schedule_preview_replacement(
                stale,
                spec,
                fps=int(data.get("fps", 6)),
                width=int(data.get("width", 640)),
                height=int(data.get("height", 480)),
                jpeg_quality=int(data.get("jpeg_quality", 82)),
            )
            jobs.append(_preview_job_payload(stale))
            continue

        try:
            job = _preview_submission(
                spec,
                fps=int(data.get("fps", 6)),
                width=int(data.get("width", 640)),
                height=int(data.get("height", 480)),
                jpeg_quality=int(data.get("jpeg_quality", 82)),
            )
        except ResourceBusyError as exc:
            errors.append({"sensor_key": key, "error": str(exc)})
            continue
        except (TypeError, ValueError) as exc:
            errors.append({"sensor_key": key, "error": str(exc)})
            continue
        jobs.append(_preview_job_payload(job))

    status_code = 202 if jobs else 409
    return (
        jsonify(
            {
                "output": f"Queued {len(jobs)} sensor preview(s)",
                "jobs": jobs,
                "errors": errors,
                "sensor_status": status,
                "sensors": specs,
            }
        ),
        status_code,
    )


@sensors_bp.get("/sensors/previews")
def get_sensor_previews():
    include_terminal = request.args.get("include_terminal", "").lower() in {
        "1",
        "true",
        "yes",
    }
    jobs = []
    for job in job_runner.list():
        if not job.parameters.get("sensor_preview"):
            continue
        if not include_terminal and job.status not in ACTIVE_JOB_STATUSES:
            continue
        if job.status in ACTIVE_JOB_STATUSES:
            healthy, _reason = _preview_job_health(job)
            if not healthy:
                _cancel_preview_in_background(job)
                # A persisted active job can still point at a JPEG from an old
                # worker. Never expose that artifact as a current preview,
                # including on the terminal-history view used by the UI.
                continue
        jobs.append(_preview_job_payload(job))
    return jsonify({"jobs": jobs})


@sensors_bp.get("/sensors/previews/<job_id>")
def get_sensor_preview(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    if not job.parameters.get("sensor_preview"):
        return jsonify({"output": "Job is not a sensor preview"}), 400
    return jsonify(_preview_job_payload(job))


@sensors_bp.post("/sensors/previews/stop")
def stop_sensor_previews():
    stopped = []
    for job in job_runner.active_jobs():
        if not job.parameters.get("sensor_preview"):
            continue
        if job.status not in ACTIVE_JOB_STATUSES:
            continue
        stopped.append(_stop_preview_job(job))
    return jsonify({"output": f"Stopping {len(stopped)} preview(s)", "jobs": stopped})


@sensors_bp.post("/sensors/previews/<job_id>/stop")
def stop_sensor_preview(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    if not job.parameters.get("sensor_preview"):
        return jsonify({"output": "Job is not a sensor preview"}), 400
    return jsonify(_stop_preview_job(job))


@sensors_bp.get("/sensors/previews/<job_id>/latest.jpg")
def get_sensor_preview_image(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    if not job.parameters.get("sensor_preview"):
        return jsonify({"output": "Job is not a sensor preview"}), 400
    preview_root = job.parameters.get("preview_root")
    if not preview_root:
        return jsonify({"output": "Missing preview root"}), 400
    try:
        path = resolve_preview_image(Path(preview_root))
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    response = send_file(path, mimetype="image/jpeg", conditional=False)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@sensors_bp.get("/sensors/snapshots/<job_id>")
def get_sensor_snapshot(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    snapshot_root = job.parameters.get("snapshot_root")
    manifest = None
    if snapshot_root:
        try:
            manifest = load_snapshot_manifest(snapshot_root)
        except (OSError, ValueError) as exc:
            return jsonify({"output": str(exc), "job": job.to_dict()}), 400
    return jsonify(
        {
            "job": job.to_dict(),
            "snapshot_root": snapshot_root,
            "manifest": manifest,
        }
    )


@sensors_bp.get("/sensors/snapshots/<job_id>/image")
def get_sensor_snapshot_image(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    snapshot_root = job.parameters.get("snapshot_root")
    image_path = request.args.get("path")
    if not snapshot_root or not image_path:
        return jsonify({"output": "Missing snapshot root or image path"}), 400
    try:
        path = resolve_snapshot_image(Path(snapshot_root), image_path)
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return send_file(path, mimetype="image/png", conditional=True)
