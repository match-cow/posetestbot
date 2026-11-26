"""Object-dataset subrun creation: reuse a run's setup for a new variant."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pipeline.subrun import (
    inspect_subrun_parent,
    subrun_target,
    validate_variant,
)
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import get_job_runner
from posetestbot.web.security import require_run_folder_target, resolve_web_run_root


subruns_bp = Blueprint("subruns", __name__)


def _error(exc: Exception):
    if isinstance(exc, FileNotFoundError):
        return jsonify({"output": str(exc)}), 404
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    return jsonify({"output": str(exc)}), 400


@subruns_bp.get("/runs/subrun")
def subrun_parent():
    """What a subrun of ``run_root`` would reuse, or why it cannot be created."""

    try:
        parent = resolve_web_run_root(request.args.get("run_root"))
        return jsonify(inspect_subrun_parent(parent))
    except Exception as exc:
        return _error(exc)


@subruns_bp.post("/runs/subrun")
def queue_subrun():
    try:
        value: Any = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != {
            "parent_run_root",
            "folder_name",
            "variant",
        }:
            raise ValueError(
                "Subrun request fields must be exactly parent_run_root, folder_name, "
                "and variant"
            )
        parent = resolve_web_run_root(value["parent_run_root"])
        inspect_subrun_parent(parent)
        target = resolve_web_run_root(
            subrun_target(parent, value["folder_name"]).as_posix()
        )
        require_run_folder_target(target)
        variant = validate_variant(value["variant"])
        job = get_job_runner().submit(
            name="subrun_create",
            command=[
                "uv",
                "run",
                "python",
                "scripts/run_subrun_create.py",
                "--parent",
                parent.as_posix(),
                "--folder-name",
                target.name,
                "--variant",
                variant,
            ],
            cwd=APP_ROOT,
            resources=["disk_io"],
            scope_kind="run",
            run_root=target,
            parameters={
                "run_root": target.as_posix(),
                "parent_run_root": parent.as_posix(),
                "subrun": True,
            },
        )
        return (
            jsonify(
                {
                    "job": job.to_dict(),
                    "job_id": job.id,
                    "run_root": target.as_posix(),
                }
            ),
            202,
        )
    except Exception as exc:
        return _error(exc)
