"""Browser-safe, ID-addressed inspection and queued visualization APIs."""

from __future__ import annotations

import io
import json
from typing import Any

from flask import Blueprint, jsonify, request, send_file, url_for

from posetestbot.bop.inspection import (
    DEFAULT_HYPOTHESES,
    DEFAULT_PAGE_SIZE,
    inspection_depth_png,
    inspection_frame,
    inspection_image_path,
    inspection_mask_path,
    inspection_model_path,
    inspection_setup,
    list_inspection_frames,
)
from posetestbot.bop.evaluation import RESULT_ID_RE
from posetestbot.bop.inspection_exports import (
    create_export_request, export_download_path, export_folder, mp4_status,
    read_export_artifact, read_export_request,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner
from posetestbot.web.routes.ui import discover_web_runs
from posetestbot.web.security import resolve_web_run_root


bop_inspection_bp = Blueprint("bop_inspection", __name__)
MAX_RESULT_METADATA_BYTES = 1024 * 1024


def _error(exc: Exception):
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, KeyError | FileNotFoundError):
        return jsonify({"output": str(exc)}), 404
    return jsonify({"output": str(exc)}), 400


def _required_int(name: str, *, minimum: int = 0) -> int:
    raw = request.args.get(name)
    try:
        value = int(raw) if raw is not None else None
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value is None or value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _optional_int(name: str, *, minimum: int = 0) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _media_url(
    endpoint: str,
    *,
    run_root: str,
    result_id: str,
    **values: Any,
) -> str:
    return url_for(
        endpoint,
        run_root=run_root,
        result_id=result_id,
        **values,
    )


@bop_inspection_bp.get("/bop/inspection/result-location")
def bop_inspection_result_location():
    """Locate one retained result in directly indexed, approved run folders."""

    try:
        result_id = request.args.get("result_id")
        if not isinstance(result_id, str) or RESULT_ID_RE.fullmatch(result_id) is None:
            raise ValueError("result_id must be a retained BOP result ID")
        matches: list[str] = []
        for run in discover_web_runs():
            run_root = resolve_web_run_root(run["path"])
            parts = [
                run_root / "processed",
                run_root / "processed" / "bop_evaluation",
                run_root / "processed" / "bop_evaluation" / "results",
                run_root / "processed" / "bop_evaluation" / "results" / result_id,
            ]
            record_path = parts[-1] / "result.json"
            if any(part.is_symlink() for part in [*parts, record_path]):
                continue
            try:
                if (
                    not record_path.is_file()
                    or record_path.stat().st_size > MAX_RESULT_METADATA_BYTES
                ):
                    continue
                with record_path.open("rb") as handle:
                    content = handle.read(MAX_RESULT_METADATA_BYTES + 1)
                if len(content) > MAX_RESULT_METADATA_BYTES:
                    continue
                record = json.loads(content)
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("result_id") == result_id:
                matches.append(run_root.as_posix())
        if not matches:
            raise FileNotFoundError(
                "Retained BOP result was not found in indexed run folders"
            )
        if len(matches) != 1:
            raise ValueError("Retained BOP result ID is ambiguous across run folders")
        return jsonify({"result_id": result_id, "run_root": matches[0]})
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get("/bop/inspection/setup")
def bop_inspection_setup():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        result_id = request.args.get("result_id") or None
        response = inspection_setup(run_root, result_id=result_id)
        response["exports"] = {"zip": {"available": True}, "mp4": mp4_status()}
        selected = response.get("selected_result_id")
        if isinstance(selected, str):
            for model in response.get("objects", []):
                model["model_url"] = _media_url(
                    "bop_inspection.bop_inspection_model",
                    run_root=run_root.as_posix(),
                    result_id=selected,
                    obj_id=model["obj_id"],
                )
        return jsonify(response)
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get("/bop/inspection/frames")
def bop_inspection_frames():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        result_id = request.args.get("result_id")
        if not result_id:
            raise ValueError("result_id is required")
        response = list_inspection_frames(
            run_root,
            result_id=result_id,
            scene_id=_required_int("scene_id"),
            frame_filter=request.args.get("filter") or "all",
            object_id=_optional_int("object_id", minimum=1),
            page=_optional_int("page", minimum=1) or 1,
            page_size=_optional_int("page_size", minimum=1) or DEFAULT_PAGE_SIZE,
        )
        return jsonify(response)
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get("/bop/inspection/frame")
def bop_inspection_frame():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        result_id = request.args.get("result_id")
        if not result_id:
            raise ValueError("result_id is required")
        scene_id = _required_int("scene_id")
        im_id = _required_int("im_id")
        response = inspection_frame(
            run_root,
            result_id=result_id,
            scene_id=scene_id,
            im_id=im_id,
            max_hypotheses=(
                _optional_int("max_hypotheses", minimum=1)
                or DEFAULT_HYPOTHESES
            ),
        )
        run_value = run_root.as_posix()
        response["media"] = {
            "rgb_url": _media_url(
                "bop_inspection.bop_inspection_image",
                run_root=run_value,
                result_id=result_id,
                scene_id=scene_id,
                im_id=im_id,
                kind="rgb",
            ),
            "depth_url": _media_url(
                "bop_inspection.bop_inspection_image",
                run_root=run_value,
                result_id=result_id,
                scene_id=scene_id,
                im_id=im_id,
                kind="depth",
            ),
        }
        for gt in response["ground_truth"]:
            gt["mask_urls"] = {
                kind: (
                    _media_url(
                        "bop_inspection.bop_inspection_mask",
                        run_root=run_value,
                        result_id=result_id,
                        scene_id=scene_id,
                        im_id=im_id,
                        gt_id=gt["gt_id"],
                        kind=kind,
                    )
                    if gt["masks"][kind]
                    else None
                )
                for kind in ("full", "visible")
            }
        model_urls = {
            obj_id: _media_url(
                "bop_inspection.bop_inspection_model",
                run_root=run_value,
                result_id=result_id,
                obj_id=obj_id,
            )
            for obj_id in {
                *[item["obj_id"] for item in response["ground_truth"]],
                *[item["obj_id"] for item in response["estimates"]],
            }
        }
        response["model_urls"] = {str(key): value for key, value in model_urls.items()}
        return jsonify(response)
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get(
    "/bop/inspection/media/<result_id>/<int:scene_id>/<int:im_id>/<kind>"
)
def bop_inspection_image(result_id: str, scene_id: int, im_id: int, kind: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        if kind == "depth":
            content = inspection_depth_png(
                run_root,
                result_id=result_id,
                scene_id=scene_id,
                im_id=im_id,
            )
            return send_file(
                io.BytesIO(content), mimetype="image/png", max_age=0, conditional=True
            )
        path = inspection_image_path(
            run_root,
            result_id=result_id,
            scene_id=scene_id,
            im_id=im_id,
            kind=kind,
        )
        return send_file(path, mimetype="image/png", max_age=0, conditional=True)
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get(
    "/bop/inspection/masks/<result_id>/<int:scene_id>/<int:im_id>/<int:gt_id>/<kind>"
)
def bop_inspection_mask(
    result_id: str, scene_id: int, im_id: int, gt_id: int, kind: str
):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        path = inspection_mask_path(
            run_root,
            result_id=result_id,
            scene_id=scene_id,
            im_id=im_id,
            gt_id=gt_id,
            kind=kind,
        )
        return send_file(path, mimetype="image/png", max_age=0, conditional=True)
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get("/bop/inspection/models/<result_id>/<int:obj_id>")
def bop_inspection_model(result_id: str, obj_id: int):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        path = inspection_model_path(
            run_root, result_id=result_id, obj_id=obj_id
        )
        return send_file(
            path,
            mimetype="application/octet-stream",
            max_age=0,
            conditional=True,
        )
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.post("/bop/inspection/exports")
def queue_bop_inspection_export():
    try:
        request.max_content_length = 16 * 1024
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("A JSON object is required")
        run_root = resolve_web_run_root(value.get("run_root"))
        export = create_export_request(run_root, value)
        export_id = export["export_id"]
        folder = export_folder(run_root, export_id)
        try:
            job = job_runner.submit(
                name="bop_inspection_export",
                command=["uv", "run", "python", "scripts/run_bop_inspection_export.py", "--request", (folder / "request.json").as_posix()],
                cwd=APP_ROOT, resources=["cpu", "disk_io"], scope_kind="run", run_root=run_root,
                parameters={
                    "export_id": export_id, "result_id": export["settings"]["result_id"],
                    "scene_id": export["settings"]["scene_id"], "format": export["settings"]["format"],
                },
            )
        except Exception as exc:
            atomic_write_json(folder / "progress.json", {"state": "failed", "error": str(exc), "completed_frames": 0, "total_frames": len(export["frame_ids"])})
            raise
        atomic_write_json(folder / "job.json", {"job_id": job.id})
        return jsonify({"export_id": export_id, "job_id": job.id}), 202
    except Exception as exc:
        return _error(exc)


def _export_job(run_root, export_id):
    saved = read_export_artifact(run_root, export_id, "job.json")
    job = job_runner.get(saved["job_id"])
    if job.run_root != run_root.as_posix() or job.name != "bop_inspection_export" or job.parameters.get("export_id") != export_id:
        raise ValueError("Export job identity does not match the run")
    return job


@bop_inspection_bp.get("/bop/inspection/exports/<export_id>")
def bop_inspection_export_status(export_id: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        saved = read_export_request(run_root, export_id)
        progress = read_export_artifact(run_root, export_id, "progress.json")
        job = _export_job(run_root, export_id)
        available = job.status == "succeeded" and progress.get("state") == "completed"
        return jsonify({
            "export_id": export_id, "settings": saved["settings"],
            "scene_name": saved["scene_name"], "frame_count": len(saved["frame_ids"]),
            "source_identity": saved["source_identity"], "job_id": job.id,
            "job_state": job.status, "progress": progress,
            "error": progress.get("error") or (job.message if job.status in {"failed", "canceled"} else None),
            "download_available": available,
            "download_url": url_for("bop_inspection.bop_inspection_export_download", export_id=export_id, run_root=run_root.as_posix()) if available else None,
        })
    except Exception as exc:
        return _error(exc)


@bop_inspection_bp.get("/bop/inspection/exports/<export_id>/download")
def bop_inspection_export_download(export_id: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        job = _export_job(run_root, export_id)
        progress = read_export_artifact(run_root, export_id, "progress.json")
        if job.status != "succeeded" or progress.get("state") != "completed":
            return jsonify({"output": "Export is not completed"}), 409
        path = export_download_path(run_root, export_id)
        return send_file(path, as_attachment=True, download_name=path.name, max_age=0, mimetype="application/zip" if path.suffix == ".zip" else "video/mp4")
    except Exception as exc:
        return _error(exc)
