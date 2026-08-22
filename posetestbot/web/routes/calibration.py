"""Intent-level calibration setup, attempts, results, and promotion APIs."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, jsonify, request

from posetestbot.calibration.attempts import (
    CalibrationTargetConflict,
    calibration_setup,
    create_calibration_attempt,
    create_promotion_request,
    list_calibration_attempts,
    load_calibration_attempt,
    record_attempt_job,
    record_attempt_job_submission_failure,
)
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pipeline.run_config import run_config_lock
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


@calibration_bp.get("/calibration/setup")
def calibration_setup_endpoint():
    try:
        return jsonify(calibration_setup(_run_root_from_query()))
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _error_response(exc)


@calibration_bp.get("/calibration/attempts")
def calibration_attempts_endpoint():
    try:
        root = _run_root_from_query()
        return jsonify(
            {
                "schema_version": "calibration_attempt_history.v1",
                "run_root": root.as_posix(),
                "attempts": list_calibration_attempts(root),
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
                    "synchronization_policy": attempt["synchronization_policy"],
                },
            )
        except ResourceBusyError as exc:
            record_attempt_job_submission_failure(
                run_root,
                attempt["attempt_id"],
                kind="calculation",
                error=exc,
            )
            raise
        record_attempt_job(
            run_root,
            attempt["attempt_id"],
            job_id=job.id,
            kind="calculation",
        )
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
        payload = load_calibration_attempt(_run_root_from_query(), attempt_id)
        job_id = payload["progress"].get("job_id")
        if job_id:
            try:
                job = job_runner.get(str(job_id))
            except (AttributeError, KeyError):
                job = None
            if job is not None:
                payload["job"] = job.to_dict()
                if job.status in {"failed", "canceled"} and payload["progress"].get(
                    "status"
                ) in {"queued", "running"}:
                    payload["progress"] = {
                        **payload["progress"],
                        "status": "failed",
                        "message": job.message or f"Parent job {job.status}.",
                    }
        promotion = payload.get("promotion")
        promotion_job_id = (
            promotion.get("job_id") if isinstance(promotion, dict) else None
        )
        if promotion_job_id:
            try:
                promotion_job = job_runner.get(str(promotion_job_id))
            except (AttributeError, KeyError):
                promotion_job = None
            if promotion_job is not None:
                payload["promotion_job"] = promotion_job.to_dict()
                if promotion_job.status in {"failed", "canceled"} and promotion.get(
                    "status"
                ) in {"queued", "running"}:
                    payload["promotion"] = {
                        **promotion,
                        "status": "failed",
                        "error": (
                            promotion_job.message
                            or f"Promotion job {promotion_job.status}."
                        ),
                    }
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
        # Keep approval creation, queue submission, and durable job binding on
        # one run lock. The worker may start immediately, but its promotion
        # process blocks on this same lock until promotion.json names the job.
        with run_config_lock(run_root) as locked_root:
            existing_attempt = load_calibration_attempt(locked_root, attempt_id)
            existing_promotion = existing_attempt.get("promotion")
            existing_job_id = (
                existing_promotion.get("job_id")
                if isinstance(existing_promotion, dict)
                else None
            )
            if existing_job_id and existing_promotion.get("status") in {
                "queued",
                "running",
            }:
                try:
                    existing_job = job_runner.get(str(existing_job_id))
                except (AttributeError, KeyError):
                    existing_job = None
                if existing_job is not None and existing_job.status in {
                    "failed",
                    "canceled",
                }:
                    record_attempt_job_submission_failure(
                        locked_root,
                        attempt_id,
                        kind="promotion",
                        error=RuntimeError(
                            existing_job.message
                            or f"Promotion job {existing_job.status}."
                        ),
                    )
            promotion = create_promotion_request(
                locked_root,
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
                        locked_root.as_posix(),
                        "--attempt-id",
                        attempt_id,
                        "--promote",
                    ],
                    cwd=APP_ROOT,
                    resources=["cpu", "disk_io"],
                    scope_kind="run",
                    run_root=locked_root,
                    parameters={
                        "calibration_attempt_promotion": attempt_id,
                        "run_root": locked_root.as_posix(),
                        "selections": dict(promotion["selections"]),
                    },
                )
            except (OSError, ResourceBusyError, RuntimeError, ValueError) as exc:
                record_attempt_job_submission_failure(
                    locked_root,
                    attempt_id,
                    kind="promotion",
                    error=exc,
                )
                raise
            try:
                record_attempt_job(
                    locked_root,
                    attempt_id,
                    job_id=job.id,
                    kind="promotion",
                )
            except (OSError, RuntimeError, ValueError) as exc:
                failure_recorded = False
                cancellation_terminal = False
                secondary_errors: list[str] = []
                try:
                    record_attempt_job_submission_failure(
                        locked_root,
                        attempt_id,
                        kind="promotion",
                        error=exc,
                    )
                    failure_recorded = True
                except (OSError, RuntimeError, ValueError) as record_error:
                    secondary_errors.append(
                        "promotion failure evidence could not be written: "
                        f"{type(record_error).__name__}: {record_error}"
                    )
                try:
                    canceled = job_runner.cancel(job.id)
                    cancellation_terminal = canceled.status in {
                        "failed",
                        "canceled",
                    }
                    if not cancellation_terminal:
                        secondary_errors.append(
                            "submitted job did not reach a terminal state: "
                            f"{canceled.status}"
                        )
                except (
                    AttributeError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ) as cancel_error:
                    secondary_errors.append(
                        "submitted job could not be canceled: "
                        f"{type(cancel_error).__name__}: {cancel_error}"
                    )
                detail = f"{type(exc).__name__}: {exc}"
                if secondary_errors:
                    detail += "; " + "; ".join(secondary_errors)
                if not failure_recorded and not cancellation_terminal:
                    raise RuntimeError(
                        "Promotion job binding failed and neither durable failure "
                        f"evidence nor terminal cancellation was confirmed: {detail}"
                    ) from exc
                raise RuntimeError(
                    f"Promotion job binding failed; promotion was stopped: {detail}"
                ) from exc
    except (
        FileNotFoundError,
        OSError,
        ResourceBusyError,
        RuntimeError,
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
