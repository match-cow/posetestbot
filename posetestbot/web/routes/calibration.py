"""Intent-level calibration setup, attempts, results, and promotion APIs."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request

from posetestbot.calibration.attempts import (
    ACTIVE_ATTEMPT_STATES,
    CalibrationTargetConflict,
    calibration_setup,
    create_calibration_attempt,
    create_promotion_request,
    list_calibration_attempts,
    load_calibration_attempt,
    reconcile_attempt_job_failure,
    record_attempt_job,
    record_attempt_job_submission_failure,
)
from posetestbot.jobs.runner import TERMINAL_STATUSES, ResourceBusyError
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner


calibration_bp = Blueprint("calibration", __name__)


def _request_object() -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _run_root_from_query() -> Path:
    value = request.args.get("run_root")
    if not value:
        raise ValueError("run_root is required")
    return Path(value)


def _error_response(exc: Exception):
    if isinstance(exc, CalibrationTargetConflict):
        return jsonify({"output": str(exc), "blockers": exc.blockers}), 409
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, FileNotFoundError):
        return jsonify({"output": str(exc)}), 404
    return jsonify({"output": str(exc)}), 400


def _bound_job(root: Path, attempt_id: str, evidence: dict | None, *, kind: str):
    job_id = (evidence or {}).get("job_id")
    if not job_id:
        return None, False
    try:
        job = job_runner.get(job_id)
    except KeyError:
        job = None
    if evidence.get("status") in ACTIVE_ATTEMPT_STATES and (
        job is None or job.status in TERMINAL_STATUSES
    ):
        message = (
            job.message or f"Job ended ({job.status}) without completing calibration."
            if job is not None else "Calibration job record is unavailable."
        )
        reconcile_attempt_job_failure(root, attempt_id, kind=kind, job_id=job_id, message=message)
        return job, True
    return job, False


def _reconcile_history(root: Path, attempts: list[dict]) -> None:
    for attempt in attempts:
        attempt_id = attempt["attempt_id"]
        _, progress_changed = _bound_job(root, attempt_id, attempt, kind="calculation")
        _, promotion_changed = _bound_job(root, attempt_id, attempt.get("promotion"), kind="promotion")
        if progress_changed or promotion_changed:
            current = load_calibration_attempt(root, attempt_id)
            attempt.update(status=current["progress"]["status"], promotion=current["promotion"])


@calibration_bp.get("/calibration/setup")
def calibration_setup_endpoint():
    try:
        root = _run_root_from_query()
        setup = calibration_setup(root)
        if setup["latest_attempt"] is not None:
            _reconcile_history(root, [setup["latest_attempt"]])
        return jsonify(setup)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _error_response(exc)


@calibration_bp.get("/calibration/attempts")
def calibration_attempts_endpoint():
    try:
        root = _run_root_from_query()
        attempts = list_calibration_attempts(root)
        _reconcile_history(root, attempts)
        return jsonify(
            {
                "schema_version": "calibration_attempt_history.v1",
                "run_root": root.as_posix(),
                "attempts": attempts,
            }
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _error_response(exc)


@calibration_bp.post("/calibration/attempts")
def calibration_attempt_create_endpoint():
    try:
        value = _request_object()
        run_root = value.get("run_root")
        if not run_root:
            raise ValueError("run_root is required")
        attempt = create_calibration_attempt(run_root, value)
        try:
            job = job_runner.submit(
                name=f"Calibration attempt {attempt['attempt_id']}",
                command=[
                    "uv",
                    "run",
                    "python",
                    "scripts/run_calibration_attempt.py",
                    str(run_root),
                    "--attempt-id",
                    str(attempt["attempt_id"]),
                ],
                cwd=APP_ROOT,
                resources=["cpu", "disk_io"],
                scope_kind="run",
                run_root=run_root,
                parameters={
                    "calibration_attempt": attempt["attempt_id"],
                    "run_root": str(run_root),
                    "mode": attempt["mode"],
                    "sensor_keys": list(attempt["sensor_keys"]),
                    "target_id": attempt["target_id"],
                    "synchronization_policy": attempt[
                        "synchronization_policy"
                    ],
                },
                before_start=lambda job_id: record_attempt_job(
                    run_root, attempt["attempt_id"], kind="calculation", job_id=job_id,
                ),
            )
        except BaseException as exc:
            record_attempt_job_submission_failure(
                run_root,
                attempt["attempt_id"],
                kind="calculation",
                error=exc,
            )
            raise
    except (
        CalibrationTargetConflict,
        FileNotFoundError,
        OSError,
        ResourceBusyError,
        ValueError,
    ) as exc:
        return _error_response(exc)
    return (
        jsonify(
            {
                "schema_version": "calibration_attempt_submission.v1",
                "attempt_id": attempt["attempt_id"],
                "job_id": job.id,
                "status": job.status,
            }
        ),
        202,
    )


@calibration_bp.get("/calibration/attempts/<attempt_id>")
def calibration_attempt_endpoint(attempt_id: str):
    try:
        root = _run_root_from_query()
        payload = load_calibration_attempt(root, attempt_id)
        job, progress_changed = _bound_job(root, attempt_id, payload["progress"], kind="calculation")
        promotion_job, promotion_changed = _bound_job(root, attempt_id, payload.get("promotion"), kind="promotion")
        if progress_changed or promotion_changed:
            payload = load_calibration_attempt(root, attempt_id)
        if job is not None:
            payload["job"] = job.to_dict()
        if promotion_job is not None:
            payload["promotion_job"] = promotion_job.to_dict()
        promotion = payload.get("promotion")
        if promotion and promotion.get("status") in ACTIVE_ATTEMPT_STATES and not promotion.get("job_id"):
            payload["promotion"] = {**promotion, "status": "failed", "error": "No web job owns this promotion request. You can retry it."}
        return jsonify(payload)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _error_response(exc)


@calibration_bp.post("/calibration/attempts/<attempt_id>/promote")
def calibration_attempt_promote_endpoint(attempt_id: str):
    try:
        value = _request_object()
        run_root = value.get("run_root")
        if not run_root:
            raise ValueError("run_root is required")
        selections = value.get("candidate_ids", value.get("selections"))
        if selections is not None and not isinstance(selections, dict):
            raise ValueError("candidate_ids must be a sensor-key mapping")
        existing_attempt = load_calibration_attempt(run_root, attempt_id)
        _bound_job(Path(run_root), attempt_id, existing_attempt.get("promotion"), kind="promotion")
        promotion = create_promotion_request(
            run_root,
            attempt_id,
            selections=selections,
            operator=value.get("operator"),
        )
        try:
            job = job_runner.submit(
                name=f"Promote calibration attempt {attempt_id}",
                command=[
                    "uv",
                    "run",
                    "python",
                    "scripts/run_calibration_attempt.py",
                    str(run_root),
                    "--attempt-id",
                    attempt_id,
                    "--promote",
                    "--requested-at",
                    promotion["created_at"],
                ],
                cwd=APP_ROOT,
                resources=["cpu", "disk_io"],
                scope_kind="run",
                run_root=run_root,
                parameters={
                    "calibration_attempt_promotion": attempt_id,
                    "run_root": str(run_root),
                    "selections": dict(promotion["selections"]),
                },
                before_start=lambda job_id: record_attempt_job(
                    run_root, attempt_id, kind="promotion", job_id=job_id,
                    requested_at=promotion["created_at"],
                ),
            )
        except BaseException as exc:
            record_attempt_job_submission_failure(
                run_root,
                attempt_id,
                kind="promotion",
                error=exc,
                requested_at=promotion["created_at"],
            )
            raise
    except (
        FileNotFoundError,
        OSError,
        ResourceBusyError,
        ValueError,
    ) as exc:
        return _error_response(exc)
    return (
        jsonify(
            {
                "schema_version": "calibration_promotion_submission.v1",
                "attempt_id": attempt_id,
                "job_id": job.id,
                "status": job.status,
                "selections": promotion["selections"],
            }
        ),
        202,
    )
