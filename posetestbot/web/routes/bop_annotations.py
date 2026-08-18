"""Guided Workflow APIs for run-scoped BOP ground-truth generation."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from posetestbot.bop.annotations import (
    inspect_annotation_setup,
    validate_annotation_mode,
)
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner
from posetestbot.web.security import resolve_web_run_root


bop_annotations_bp = Blueprint("bop_annotations", __name__)


def _error(exc: Exception):
    if isinstance(exc, FileNotFoundError):
        return jsonify({"output": str(exc)}), 404
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    return jsonify({"output": str(exc)}), 400


def _json_object() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


@bop_annotations_bp.get("/bop/annotations/setup")
def bop_annotations_setup():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        return jsonify(inspect_annotation_setup(run_root, app_root=APP_ROOT))
    except Exception as exc:
        return _error(exc)


@bop_annotations_bp.post("/bop/annotations")
def queue_bop_annotations():
    try:
        value = _json_object()
        if set(value) != {"run_root", "mode"}:
            raise ValueError(
                "Ground-truth request fields must be exactly run_root and mode"
            )
        run_root = resolve_web_run_root(value.get("run_root"))
        mode = validate_annotation_mode(value.get("mode"))
        setup = inspect_annotation_setup(run_root, app_root=APP_ROOT)
        if setup.get("configured_mode") != mode:
            raise ValueError(
                "Requested ground-truth mode does not match run_config.json"
            )
        readiness = setup["readiness_by_mode"][mode]
        if not readiness["ready"]:
            message = "; ".join(str(item["message"]) for item in readiness["blockers"])
            raise ValueError(f"Ground-truth generation is not ready: {message}")
        job = job_runner.submit(
            name="bop_annotations",
            command=[
                "uv",
                "run",
                "python",
                "scripts/run_bop_annotations.py",
                run_root.as_posix(),
                "--mode",
                mode,
            ],
            cwd=APP_ROOT,
            resources=["cpu", "render", "disk_io"],
            scope_kind="run",
            run_root=run_root,
            parameters={
                "run_root": run_root.as_posix(),
                "bop_annotations": True,
                "annotation_mode": mode,
            },
        )
        return (
            jsonify(
                {
                    "job": job.to_dict(),
                    "job_id": job.id,
                    "mode": mode,
                }
            ),
            202,
        )
    except Exception as exc:
        return _error(exc)
