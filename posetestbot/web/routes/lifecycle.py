"""Browser-safe lifecycle status and managed backend restart API."""

from __future__ import annotations

from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from posetestbot.jobs.runner import TERMINAL_STATUSES
from posetestbot.web.runtime import get_job_runner, get_web_runtime


lifecycle_bp = Blueprint("lifecycle", __name__)


def _restart_status() -> dict[str, Any]:
    runtime = get_web_runtime()
    manager = runtime.web_service_manager
    if manager is None:
        return {
            "configured": False,
            "available": False,
            "service_unit": None,
            "state": "unmanaged",
            "blockers": [
                {
                    "code": "web_service_management_not_configured",
                    "message": (
                        "Backend restart requires PoseTestBot to run directly as a "
                        "managed user-systemd service."
                    ),
                }
            ],
        }
    return manager.status()


def _active_local_jobs() -> int:
    return sum(
        job.status not in TERMINAL_STATUSES
        for job in get_job_runner().list(include_services=True)
    )


@lifecycle_bp.get("/system/lifecycle")
def web_lifecycle_status():
    runtime = get_web_runtime()
    response = jsonify(
        {
            "schema_version": "web_lifecycle.v1",
            "instance_id": runtime.instance_id,
            "backend_restart": _restart_status(),
            "active_local_jobs": _active_local_jobs(),
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@lifecycle_bp.post("/system/restart-backend")
def restart_web_backend():
    value = request.get_json(silent=True)
    if not isinstance(value, Mapping):
        return jsonify({"output": "A JSON object is required"}), 400
    if set(value) != {"confirm"}:
        return jsonify({"output": "Backend restart contains unsupported fields"}), 400
    if value.get("confirm") is not True:
        return jsonify({"output": "Backend restart requires explicit confirmation"}), 400

    runtime = get_web_runtime()
    manager = runtime.web_service_manager
    if manager is None:
        return jsonify({"output": _restart_status()["blockers"][0]["message"]}), 409
    status = manager.status()
    if not status.get("available"):
        blockers = status.get("blockers")
        message = (
            blockers[0].get("message")
            if isinstance(blockers, list)
            and blockers
            and isinstance(blockers[0], Mapping)
            else "The managed PoseTestBot backend cannot be restarted in its current state."
        )
        return jsonify({"output": message}), 409

    try:
        manager.schedule_restart()
    except (OSError, RuntimeError) as exc:
        return jsonify({"output": str(exc)}), 409
    return (
        jsonify(
            {
                "accepted": True,
                "instance_id": runtime.instance_id,
                "retry_after_ms": 750,
            }
        ),
        202,
    )
