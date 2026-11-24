"""Queued WebRTC room-monitor routes."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from posetestbot.jobs.runner import (
    ResourceBusyError,
    SERVICE_VISIBILITY,
    TERMINAL_STATUSES,
)
from posetestbot.monitoring.webrtc import (
    MAX_SDP_BYTES,
    MONITOR_STATUS_SCHEMA,
    UGREEN_USB_PRODUCT_ID,
    UGREEN_USB_VENDOR_ID,
    build_monitor_webrtc_command,
    load_monitor_status,
    monitor_status_health,
    monitor_stream_root,
    public_monitor_status,
)
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner


monitoring_bp = Blueprint("monitoring", __name__)

UGREEN_DEVICE_ID = f"{UGREEN_USB_VENDOR_ID}:{UGREEN_USB_PRODUCT_ID}"
ACTIVE_JOB_STATUSES = {"queued", "running", "canceling"}
MONITOR_STUN_PORT = int(os.environ.get("POSETESTBOT_MONITOR_STUN_PORT", "3478"))
if not 1 <= MONITOR_STUN_PORT <= 65535:
    raise ValueError("POSETESTBOT_MONITOR_STUN_PORT must be from 1 to 65535")
_replacement_lock = threading.Lock()
_replacement_job_ids: set[str] = set()


def _active_monitor_job() -> Any | None:
    return next(
        (job for job in job_runner.active_jobs() if job.parameters.get("monitor_webrtc")),
        None,
    )


def _default_status(job: Any) -> dict[str, Any]:
    return {
        "schema_version": MONITOR_STATUS_SCHEMA,
        "transport": "webrtc",
        "status": "starting" if job.status in ACTIVE_JOB_STATUSES else job.status,
        "signaling_ready": False,
        "peer_count": 0,
        "frame_count": 0,
        "selected_node": None,
        "error": job.message if job.status == "failed" else None,
    }


def _private_monitor_status(job: Any) -> dict[str, Any]:
    status = _default_status(job)
    monitor_root = job.parameters.get("monitor_root")
    if not monitor_root:
        status.update(status="failed", error="Monitor job is missing its status root.")
        return status
    try:
        loaded = load_monitor_status(monitor_root)
        if loaded is not None:
            status = loaded
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        status.update(status="failed", signaling_ready=False, error=str(exc))
    return status


def _monitor_health(job: Any, status: Mapping[str, Any]) -> tuple[bool, str | None]:
    if job.status == "queued":
        return True, None
    healthy, reason = monitor_status_health(status)
    if healthy:
        return True, None
    if status.get("status") in {"starting", "opening"} and not (
        status.get("error_reason") or status.get("error")
    ):
        if job.started_at is not None:
            try:
                started = datetime.fromisoformat(job.started_at.replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if (datetime.now(UTC) - started).total_seconds() <= 5.0:
                    return True, None
            except ValueError:
                pass
    return False, reason


def _monitor_payload(job: Any | None) -> dict[str, Any]:
    if job is None:
        return {"job": None, "webrtc_status": None}
    return {
        "job": job.to_dict(),
        "webrtc_status": public_monitor_status(_private_monitor_status(job)),
    }


def _submit_monitor_service() -> Any:
    root = monitor_stream_root()
    return job_runner.submit(
        name="monitor-webrtc:ugreen",
        command=build_monitor_webrtc_command(
            monitor_root=root,
            stun_port=MONITOR_STUN_PORT,
        ),
        cwd=APP_ROOT,
        resources=[f"monitoring_camera:{UGREEN_DEVICE_ID}"],
        scope_kind="global",
        visibility=SERVICE_VISIBILITY,
        parameters={
            "monitor_root": root.as_posix(),
            "sensor_key": f"monitor_webcam:{UGREEN_DEVICE_ID}",
            "sensor_type": "monitor_webcam",
            "device_id": UGREEN_DEVICE_ID,
            "monitor_webcam": True,
            "monitor_webrtc": True,
            "managed_service": True,
            "transport": "webrtc",
            "stun_port": MONITOR_STUN_PORT,
        },
    )


def _schedule_monitor_replacement(job: Any, reason: str) -> None:
    with _replacement_lock:
        if job.id in _replacement_job_ids:
            return
        _replacement_job_ids.add(job.id)

    def replace() -> None:
        try:
            try:
                job_runner.cancel(job.id)
            except KeyError:
                pass
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    current = job_runner.get(job.id)
                except KeyError:
                    break
                if current.status in TERMINAL_STATUSES:
                    break
                time.sleep(0.05)
            if _active_monitor_job() is None:
                try:
                    _submit_monitor_service()
                except ResourceBusyError:
                    pass
        finally:
            with _replacement_lock:
                _replacement_job_ids.discard(job.id)

    thread = threading.Thread(
        target=replace,
        name=f"replace-monitor-{job.id}",
        daemon=True,
    )
    thread.start()


# The monitor worker is on loopback. The default opener would route these
# requests through a site HTTP_PROXY unless NO_PROXY happened to cover it.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def _proxy_webrtc_offer(port: int, payload: Mapping[str, str]) -> dict[str, Any]:
    body = json.dumps(dict(payload)).encode("utf-8")
    upstream = urllib.request.Request(
        f"http://127.0.0.1:{port}/offer",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _LOOPBACK_OPENER.open(upstream, timeout=10) as response:
            response_body = response.read(MAX_SDP_BYTES + 4096)
    except (OSError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise RuntimeError(f"Monitor signaling request failed: {exc}") from exc
    try:
        answer = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Monitor signaling returned malformed JSON.") from exc
    if not isinstance(answer, dict):
        raise RuntimeError("Monitor signaling returned a non-object response.")
    return answer


class MonitorWorkerRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def _proxy_brightness_autocalibration(port: int) -> dict[str, Any]:
    upstream = urllib.request.Request(
        f"http://127.0.0.1:{port}/brightness/autocalibrate",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _LOOPBACK_OPENER.open(upstream, timeout=5) as response:
            response_body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read(64 * 1024))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_payload = {}
        message = (
            error_payload.get("error") if isinstance(error_payload, dict) else None
        )
        raise MonitorWorkerRequestError(
            str(
                message
                or f"Monitor worker rejected brightness calibration ({exc.code})."
            ),
            status_code=exc.code if 400 <= exc.code < 500 else 503,
        ) from exc
    except (OSError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise MonitorWorkerRequestError(
            f"Monitor brightness request failed: {exc}"
        ) from exc
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorWorkerRequestError(
            "Monitor brightness request returned malformed JSON."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("brightness"), dict):
        raise MonitorWorkerRequestError(
            "Monitor brightness request returned an invalid response."
        )
    return payload


@monitoring_bp.get("/monitoring/webcam")
def get_monitor_webcam():
    active = _active_monitor_job()
    if active is not None:
        status = _private_monitor_status(active)
        healthy, reason = _monitor_health(active, status)
        if not healthy and active.status == "running":
            _schedule_monitor_replacement(active, reason or "Monitor is unhealthy.")
            status.update(status="unhealthy", error=reason, error_reason=reason)
            return jsonify(
                {
                    "job": active.to_dict(),
                    "webrtc_status": public_monitor_status(status),
                }
            )
        return jsonify(_monitor_payload(active))
    jobs = job_runner.list_page(search="monitor-webrtc:ugreen", limit=1).jobs
    last = next((job for job in jobs if job.parameters.get("monitor_webrtc")), None)
    return jsonify(_monitor_payload(last))


@monitoring_bp.post("/monitoring/webcam")
def start_monitor_webcam():
    existing = _active_monitor_job()
    if existing is not None:
        status = _private_monitor_status(existing)
        healthy, reason = _monitor_health(existing, status)
        if not healthy and existing.status == "running":
            _schedule_monitor_replacement(existing, reason or "Monitor is unhealthy.")
            status.update(status="unhealthy", error=reason, error_reason=reason)
            return (
                jsonify(
                    {
                        "job": existing.to_dict(),
                        "webrtc_status": public_monitor_status(status),
                    }
                ),
                202,
            )
        return jsonify(_monitor_payload(existing))

    try:
        job = _submit_monitor_service()
    except ResourceBusyError as exc:
        return jsonify({"output": str(exc)}), 409
    return jsonify(_monitor_payload(job)), 202


@monitoring_bp.post("/monitoring/webcam/<job_id>/brightness/autocalibrate")
def autocalibrate_monitor_brightness(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown monitor job"}), 404
    if not job.parameters.get("monitor_webrtc"):
        return jsonify({"output": "Unknown monitor job"}), 404
    if job.status in TERMINAL_STATUSES:
        return jsonify({"output": "Monitor job is terminal"}), 409
    if job.status != "running":
        return jsonify({"output": "Monitor worker is not running"}), 503

    status = _private_monitor_status(job)
    healthy, reason = _monitor_health(job, status)
    if not healthy:
        _schedule_monitor_replacement(job, reason or "Monitor worker is unhealthy.")
        return jsonify({"output": reason or "Monitor worker is unhealthy"}), 503
    port = status.get("signaling_port")
    if (
        status.get("schema_version") != MONITOR_STATUS_SCHEMA
        or status.get("signaling_ready") is not True
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return jsonify({"output": "Monitor signaling is not ready"}), 503

    try:
        payload = _proxy_brightness_autocalibration(port)
    except MonitorWorkerRequestError as exc:
        return jsonify({"output": str(exc)}), exc.status_code
    return jsonify(payload), 202


@monitoring_bp.post("/monitoring/webcam/<job_id>/webrtc/offer")
def offer_monitor_webcam(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown monitor job"}), 404
    if not job.parameters.get("monitor_webrtc"):
        return jsonify({"output": "Unknown monitor job"}), 404
    if job.status in TERMINAL_STATUSES:
        return jsonify({"output": "Monitor job is terminal"}), 409
    if job.status != "running":
        return jsonify({"output": "Monitor worker is not running"}), 503

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"output": "Offer must be a JSON object"}), 400
    offer_type = payload.get("type")
    sdp = payload.get("sdp")
    if offer_type != "offer" or not isinstance(sdp, str) or not sdp.strip():
        return jsonify({"output": "Expected a non-empty SDP offer"}), 400
    if len(sdp.encode("utf-8")) > MAX_SDP_BYTES:
        return jsonify({"output": "SDP offer is too large"}), 400

    status = _private_monitor_status(job)
    healthy, reason = _monitor_health(job, status)
    if not healthy:
        _schedule_monitor_replacement(job, reason or "Monitor worker is unhealthy.")
        return jsonify({"output": reason or "Monitor worker is unhealthy"}), 503
    port = status.get("signaling_port")
    if (
        status.get("schema_version") != MONITOR_STATUS_SCHEMA
        or status.get("signaling_ready") is not True
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return jsonify({"output": "Monitor signaling is not ready"}), 503

    try:
        answer = _proxy_webrtc_offer(port, {"type": "offer", "sdp": sdp})
    except RuntimeError as exc:
        return jsonify({"output": str(exc)}), 503
    answer_sdp = answer.get("sdp")
    if (
        answer.get("type") != "answer"
        or not isinstance(answer_sdp, str)
        or not answer_sdp.strip()
        or len(answer_sdp.encode("utf-8")) > MAX_SDP_BYTES
    ):
        return jsonify({"output": "Monitor signaling returned an invalid answer"}), 503
    return jsonify({"type": "answer", "sdp": answer_sdp})
