"""Managed object, immutable template, and per-run Ground Truth APIs."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pose_templates.adapter import (
    PoseTemplateCreatorUnavailable,
    posetemplatecreator_status,
)
from posetestbot.pose_templates.catalog import (
    default_working_data_root,
    get_catalog_object,
)
from posetestbot.pose_templates.library import (
    BUNDLE_MANIFEST,
    TEMPLATE_PDF,
    clone_template_configuration,
    delete_template_bundle,
    load_template_bundle_detail,
    load_template_bundle_preview,
    load_template_thumbnail,
    list_template_bundle_summaries,
    record_template_cleanup_submission_failure,
    resolve_template_bundle_asset,
    resolve_template_bundle_download,
    set_template_archive_state,
)
from posetestbot.pose_templates.orientations import (
    OrientationAnalysisStaleError,
    load_catalog_orientation_analysis,
    load_catalog_orientation_thumbnail,
)
from posetestbot.pose_templates.selection import (
    PoseTemplateSelectionConflict,
    load_pose_template_selection,
    replacement_blockers,
)
from posetestbot.web.runtime import job_runner
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.security import resolve_web_run_root


pose_templates_bp = Blueprint("pose_templates", __name__)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BATCH_BYTES = 100 * 1024 * 1024
REQUEST_ROOT = default_working_data_root() / "jobs" / "pose_template_requests"
WORKPIECE_REQUEST_ROOT = (
    default_working_data_root() / "jobs" / "workpiece_catalog_requests"
)
REQUEST_RETENTION_SECONDS = 24 * 60 * 60


def _json() -> dict[str, Any]:
    request.max_content_length = MAX_JSON_BYTES
    if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
        raise RequestEntityTooLarge()
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _write_request(kind: str, value: dict[str, Any]) -> tuple[str, Path]:
    _prune_stale_requests(kind)
    request_id = uuid.uuid4().hex
    folder = REQUEST_ROOT / kind / request_id
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "request.json"
    atomic_write_json(path, value)
    return request_id, path


def _prune_stale_requests(kind: str, *, request_root: Path | None = None) -> None:
    """Bound abandoned request/result storage without touching active jobs."""

    root = (request_root or REQUEST_ROOT) / kind
    active_ids = {
        str(job.parameters.get("request_id"))
        for job in job_runner.list(include_services=True)
        if job.status not in {"succeeded", "failed", "canceled"}
        and job.parameters.get("request_id")
    }
    cutoff = time.time() - REQUEST_RETENTION_SECONDS
    try:
        folders = list(root.iterdir())
    except FileNotFoundError:
        return
    for folder in folders:
        try:
            if (
                folder.is_dir()
                and folder.name not in active_ids
                and folder.stat().st_mtime < cutoff
            ):
                shutil.rmtree(folder)
        except FileNotFoundError:
            continue


def _submit(
    *,
    name: str,
    script: str,
    request_path: Path,
    request_id: str,
    resources: list[str],
    scope_kind: str = "library",
    run_root: Path | None = None,
):
    try:
        job = job_runner.submit(
            name=name,
            command=[
                "uv",
                "run",
                "python",
                script,
                "--request",
                request_path.as_posix(),
            ],
            cwd=APP_ROOT,
            resources=resources,
            scope_kind=scope_kind,
            run_root=run_root,
            parameters={
                "request_id": request_id,
                "request_path": request_path.as_posix(),
            },
        )
    except Exception:
        shutil.rmtree(request_path.parent, ignore_errors=True)
        raise
    return jsonify(
        {"job": job.to_dict(), "job_id": job.id, "request_id": request_id}
    ), 202


def _error(exc: Exception):
    if isinstance(exc, RequestEntityTooLarge):
        message = (
            "Pose-template JSON request exceeds 2 MiB"
            if request.mimetype == "application/json"
            else "Upload exceeds the 100 MiB batch limit"
        )
        return jsonify({"output": message}), 413
    if isinstance(exc, PoseTemplateSelectionConflict):
        return jsonify({"output": str(exc), "blockers": exc.blockers}), 409
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return jsonify({"output": str(exc)}), 404
    if isinstance(exc, PoseTemplateCreatorUnavailable):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, OrientationAnalysisStaleError):
        return jsonify({"output": str(exc), "analysis_required": True}), 409
    code = getattr(exc, "code", None)
    if code:
        return jsonify(
            {"errors": [{"code": code, "message": getattr(exc, "message", str(exc))}]}
        ), 422
    return jsonify({"output": str(exc)}), 400


@pose_templates_bp.get("/pose-templates/status")
def source_status():
    return jsonify(posetemplatecreator_status())


@pose_templates_bp.get("/pose-templates/workpieces/<catalog_uuid>/orientations")
def workpiece_orientations(catalog_uuid: str):
    """Return only a hash-current, revision-current orientation analysis."""

    try:
        return jsonify(load_catalog_orientation_analysis(catalog_uuid))
    except FileNotFoundError:
        return (
            jsonify(
                {
                    "output": "Stable orientations have not been analyzed for this workpiece.",
                    "analysis_required": True,
                }
            ),
            404,
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get(
    "/pose-templates/workpieces/<catalog_uuid>/orientation-thumbnail"
)
def workpiece_orientation_thumbnail(catalog_uuid: str):
    """Serve one bounded default orientation for catalogue/list cards."""

    try:
        return jsonify(load_catalog_orientation_thumbnail(catalog_uuid))
    except FileNotFoundError:
        return (
            jsonify(
                {
                    "output": "A compact 3D preview has not been prepared for this workpiece.",
                    "analysis_required": True,
                }
            ),
            404,
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.post("/pose-templates/workpieces/<catalog_uuid>/orientations")
def analyze_workpiece_orientations(catalog_uuid: str):
    """Queue CPU/disk-heavy stable-pose and footprint extraction."""

    try:
        item = get_catalog_object(catalog_uuid, verify_assets=False)
        request_id, request_path = _write_request(
            "orientations",
            {
                "catalog_uuid": item["catalog_uuid"],
                "catalog_root": item["catalog_root"],
            },
        )
        return _submit(
            name="pose_template_orientation_analysis",
            script="scripts/run_pose_template_orientation_analysis.py",
            request_path=request_path,
            request_id=request_id,
            resources=[
                "cpu",
                "disk_io",
                f"workpiece_catalog:{item['catalog_uuid']}",
            ],
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.post("/pose-templates/preview")
def preview():
    request_path: Path | None = None
    try:
        value = _json()
        configuration = value.get("configuration")
        if not isinstance(configuration, dict):
            raise ValueError("configuration must be an object")
        request_id, request_path = _write_request(
            "preview", {"configuration": configuration}
        )
        output = request_path.parent / "preview.json"
        try:
            job = job_runner.submit(
                name="pose_template_preview",
                command=[
                    "uv",
                    "run",
                    "python",
                    "scripts/run_pose_template_preview.py",
                    "--request",
                    request_path.as_posix(),
                    "--output",
                    output.as_posix(),
                ],
                cwd=APP_ROOT,
                resources=["cpu", "disk_io"],
                scope_kind="global",
                parameters={"request_id": request_id, "result": output.as_posix()},
            )
        except Exception:
            shutil.rmtree(request_path.parent, ignore_errors=True)
            raise
        return jsonify(
            {"job": job.to_dict(), "job_id": job.id, "request_id": request_id}
        ), 202
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get("/pose-templates/preview/<request_id>")
def preview_result(request_id: str):
    try:
        if not request_id.isalnum() or len(request_id) != 32:
            raise ValueError("Invalid preview request ID")
        path = REQUEST_ROOT / "preview" / request_id / "preview.json"
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        response = jsonify(value)
        shutil.rmtree(path.parent, ignore_errors=True)
        return response
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.post("/pose-templates/generate")
def generate():
    try:
        value = _json()
        configuration = value.get("configuration")
        if not isinstance(configuration, dict):
            raise ValueError("configuration must be an object")
        request_value = {
            "configuration": configuration,
            "cloned_from": value.get("cloned_from"),
        }
        request_id, request_path = _write_request("generate", request_value)
        return _submit(
            name="pose_template_generate",
            script="scripts/run_pose_template_generate.py",
            request_path=request_path,
            request_id=request_id,
            resources=["cpu", "disk_io"],
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get("/pose-templates/library")
def library_list():
    return jsonify(
        {
            "schema_version": "pose_template_library.v1",
            "templates": list_template_bundle_summaries(),
        }
    )


@pose_templates_bp.get("/pose-templates/library/<template_uuid>")
def library_detail(template_uuid: str):
    try:
        return jsonify(load_template_bundle_detail(template_uuid))
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.delete("/pose-templates/library/<template_uuid>")
def library_delete(template_uuid: str):
    try:
        value = _json()
        if value.get("confirm") is not True:
            raise ValueError("confirm must be true to delete a pose template")
        result = delete_template_bundle(template_uuid, cleanup_assets=False)
        if result["asset_cleanup"]["status"] == "complete":
            return jsonify(result)
        try:
            job = job_runner.submit(
                name="pose_template_delete_cleanup",
                command=[
                    "uv",
                    "run",
                    "python",
                    "scripts/run_pose_template_delete_cleanup.py",
                    "--template-uuid",
                    result["template_uuid"],
                ],
                cwd=APP_ROOT,
                resources=[
                    "disk_io",
                    f"pose_template_library:{result['template_uuid']}",
                ],
                scope_kind="library",
                parameters={"template_uuid": result["template_uuid"]},
            )
        except Exception as cleanup_error:
            result = record_template_cleanup_submission_failure(
                template_uuid, cleanup_error
            )
            return jsonify({**result, "cleanup_job_error": str(cleanup_error)[:2_000]})
        return (
            jsonify(
                {
                    **result,
                    "job": job.to_dict(),
                    "job_id": job.id,
                }
            ),
            202,
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get("/pose-templates/library/<template_uuid>/preview")
def library_preview(template_uuid: str):
    """Serve the hash-verified immutable JSON preview stored in the bundle."""

    try:
        return jsonify(load_template_bundle_preview(template_uuid))
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get("/pose-templates/library/<template_uuid>/thumbnail")
def library_thumbnail(template_uuid: str):
    """Serve a bounded footprint without loading full card preview payloads."""

    try:
        return jsonify(load_template_thumbnail(template_uuid))
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get(
    "/pose-templates/library/<template_uuid>/assets/<instance_uuid>/<kind>"
)
def library_asset(template_uuid: str, instance_uuid: str, kind: str):
    """Serve a verified immutable per-instance mesh or texture snapshot."""

    try:
        path = resolve_template_bundle_asset(template_uuid, instance_uuid, kind)
        media_types = {
            "canonical_ply": "application/octet-stream",
            "texture": "image/png",
        }
        return send_file(
            path,
            mimetype=media_types.get(kind, "application/octet-stream"),
            as_attachment=False,
            download_name=path.name,
            conditional=True,
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.post("/pose-templates/library/<template_uuid>/<action>")
def library_action(template_uuid: str, action: str):
    try:
        if action in {"archive", "restore"}:
            return jsonify(
                set_template_archive_state(
                    template_uuid, state="archived" if action == "archive" else "active"
                )
            )
        if action == "clone":
            configuration = clone_template_configuration(template_uuid)
            value = _json() if request.content_length else {}
            if value.get("display_name"):
                configuration["display_name"] = str(value["display_name"])
            request_id, request_path = _write_request(
                "generate",
                {"configuration": configuration, "cloned_from": template_uuid},
            )
            return _submit(
                name="pose_template_clone",
                script="scripts/run_pose_template_generate.py",
                request_path=request_path,
                request_id=request_id,
                resources=["cpu", "disk_io"],
            )
        raise KeyError("Unknown template action")
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get("/pose-templates/library/<template_uuid>/download/<kind>")
def library_download(template_uuid: str, kind: str):
    try:
        names = {"pdf": TEMPLATE_PDF, "manifest": BUNDLE_MANIFEST}
        if kind not in names:
            raise KeyError("Unknown template download")
        path = resolve_template_bundle_download(template_uuid, kind)
        return send_file(
            path, as_attachment=True, download_name=names[kind], conditional=True
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.get("/pose-templates/runs/selection")
def run_selection_detail():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        try:
            selection = load_pose_template_selection(run_root)
        except FileNotFoundError:
            selection = None
        return jsonify(
            {
                "schema_version": "pose_template_run_status.v1",
                "run_root": run_root.as_posix(),
                "selection": selection,
                "replacement_blockers": replacement_blockers(run_root),
                "ready": bool(selection and selection.get("placement_confirmed")),
            }
        )
    except Exception as exc:
        return _error(exc)


@pose_templates_bp.post("/pose-templates/runs/selection")
def run_selection_update():
    try:
        value = _json()
        run_root = resolve_web_run_root(value.get("run_root"))
        placement = value.get("placement")
        if not isinstance(placement, dict):
            raise ValueError("placement must be a transform object")
        confirmed = value.get("confirmed", False)
        if type(confirmed) is not bool:
            raise ValueError("confirmed must be a boolean")
        request_value = {
            "run_root": run_root.as_posix(),
            "template_uuid": value.get("template_uuid"),
            "placement": placement,
            "confirmed": confirmed,
            "operator": value.get("operator"),
        }
        request_id, request_path = _write_request("select", request_value)
        return _submit(
            name="pose_template_select",
            script="scripts/run_pose_template_select.py",
            request_path=request_path,
            request_id=request_id,
            resources=["disk_io"],
            scope_kind="run",
            run_root=run_root,
        )
    except Exception as exc:
        return _error(exc)
