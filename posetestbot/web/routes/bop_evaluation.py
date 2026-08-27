"""Inspect-only BOP result registration and queued evaluation APIs."""

from __future__ import annotations

import io
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from posetestbot.bop.evaluation import (
    MAX_RESULT_BYTES,
    create_evaluation_request,
    evaluation_report_path,
    evaluation_request_path,
    get_result,
    import_bop_result,
    inspect_dataset,
    list_evaluations,
    list_results,
    public_dataset_descriptor,
    public_result_descriptor,
    result_download_path,
    result_package_bytes,
    result_provenance_path,
    toolkit_status,
)
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.web.runtime import job_runner
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.security import resolve_web_run_root


bop_evaluation_bp = Blueprint("bop_evaluation", __name__)
UPLOAD_OVERHEAD_BYTES = 1024 * 1024


def _error(exc: Exception):
    if isinstance(exc, RequestEntityTooLarge):
        return jsonify({"output": "BOP result upload exceeds 128 MiB"}), 413
    if isinstance(exc, KeyError):
        return jsonify({"output": str(exc)}), 404
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    return jsonify({"output": str(exc)}), 400


def _json_object() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _source_from_request(value: Mapping[str, Any]) -> tuple[str | None, dict | None]:
    source = value.get("source")
    if isinstance(source, Mapping):
        kind = source.get("kind")
        if kind == "registered_result":
            return str(source.get("result_id") or ""), None
        if kind == "gt_simulation":
            return None, {
                "method_name": source.get("method_name") or "GT slight offset",
                "translation_sigma_mm": source.get("translation_sigma_mm", 1.0),
                "rotation_sigma_deg": source.get("rotation_sigma_deg", 0.25),
                "seed": source.get("seed", 42),
                "score": source.get("score", 1.0),
            }
        raise ValueError("source.kind must be registered_result or gt_simulation")
    result_id = value.get("result_id")
    simulation = value.get("simulation")
    return (
        str(result_id) if isinstance(result_id, str) else None,
        dict(simulation) if isinstance(simulation, Mapping) else None,
    )


@bop_evaluation_bp.get("/bop/evaluation/setup")
def bop_evaluation_setup():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        dataset = inspect_dataset(run_root)
        results = list_results(run_root, dataset=dataset)
        public_results = [public_result_descriptor(result) for result in results]
        return jsonify(
            {
                "schema_version": "bop_evaluation_setup.v1",
                "run_root": run_root.as_posix(),
                "toolkit": toolkit_status(APP_ROOT),
                "dataset": public_dataset_descriptor(dataset),
                "results": public_results,
                "evaluations": list_evaluations(
                    run_root,
                    dataset=dataset,
                    results=public_results,
                ),
            }
        )
    except Exception as exc:
        return _error(exc)


@bop_evaluation_bp.post("/bop/evaluation/results")
def bop_result_import():
    upload_root: Path | None = None
    try:
        request.max_content_length = MAX_RESULT_BYTES + UPLOAD_OVERHEAD_BYTES
        if (
            request.content_length is not None
            and request.content_length > request.max_content_length
        ):
            raise RequestEntityTooLarge()
        run_root = resolve_web_run_root(request.form.get("run_root"))
        upload = request.files.get("file") or request.files.get("result")
        if upload is None or not upload.filename:
            raise ValueError("A BOP result CSV file is required")
        filename = upload.filename
        if Path(filename).name != filename or "\\" in filename:
            raise ValueError("BOP result filename must not contain a path")
        if not filename.lower().endswith(".csv"):
            raise ValueError("BOP result upload must be a .csv file")
        upload_root = (
            run_root / "processed" / "bop_evaluation" / ".uploads" / uuid.uuid4().hex
        )
        upload_root.mkdir(parents=True, exist_ok=False)
        staged = upload_root / filename
        upload.save(staged)
        if staged.stat().st_size > MAX_RESULT_BYTES:
            raise RequestEntityTooLarge()
        display_name = (
            request.form.get("display_name") or request.form.get("method_name") or None
        )
        result = import_bop_result(
            run_root,
            staged,
            method_name=display_name,
        )
        return jsonify({"result": public_result_descriptor(result)}), 201
    except Exception as exc:
        return _error(exc)
    finally:
        if upload_root is not None:
            shutil.rmtree(upload_root, ignore_errors=True)


@bop_evaluation_bp.post("/bop/evaluations")
def queue_bop_evaluation():
    try:
        value = _json_object()
        run_root = resolve_web_run_root(value.get("run_root"))
        status = toolkit_status(APP_ROOT)
        if not status["available"]:
            raise ValueError(
                "BOP Toolkit is unavailable. "
                + str(status.get("reason") or status.get("install_command") or "")
            )
        result_id, simulation = _source_from_request(value)
        if result_id == "":
            raise ValueError("A registered BOP result_id is required")
        evaluation = create_evaluation_request(
            run_root,
            result_id=result_id,
            simulation=simulation,
        )
        request_path = evaluation_request_path(run_root, evaluation["evaluation_id"])
        try:
            job = job_runner.submit(
                name="bop_evaluation",
                command=[
                    "uv",
                    "run",
                    "python",
                    "scripts/run_bop_evaluation.py",
                    "--request",
                    request_path.as_posix(),
                ],
                cwd=APP_ROOT,
                resources=["cpu", "disk_io"],
                scope_kind="run",
                run_root=run_root,
                parameters={
                    "run_root": run_root.as_posix(),
                    "evaluation_id": evaluation["evaluation_id"],
                    "request_path": request_path.as_posix(),
                    "result_id": result_id,
                    "source_kind": (
                        "gt_simulation"
                        if simulation is not None
                        else "registered_result"
                    ),
                },
            )
        except Exception:
            shutil.rmtree(request_path.parent, ignore_errors=True)
            raise
        return (
            jsonify(
                {
                    "evaluation": evaluation,
                    "evaluation_id": evaluation["evaluation_id"],
                    "job": job.to_dict(),
                    "job_id": job.id,
                }
            ),
            202,
        )
    except Exception as exc:
        return _error(exc)


@bop_evaluation_bp.get("/bop/evaluation/results")
def bop_result_list():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        limit = request.args.get("limit", default=100, type=int)
        if limit is None or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        dataset = inspect_dataset(run_root)
        results = list_results(run_root, dataset=dataset)
        return jsonify(
            {
                "schema_version": "bop_result_list.v1",
                "results": [
                    public_result_descriptor(result) for result in results[:limit]
                ],
                "total_count": len(results),
            }
        )
    except Exception as exc:
        return _error(exc)


@bop_evaluation_bp.get("/bop/evaluation/results/<result_id>/download")
def download_bop_result(result_id: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        result = get_result(run_root, result_id)
        path = result_download_path(run_root, result_id)
        return send_file(
            path,
            as_attachment=True,
            download_name=str(result["filename"]),
            mimetype="text/csv",
        )
    except Exception as exc:
        return _error(exc)


@bop_evaluation_bp.get("/bop/evaluation/results/<result_id>/provenance")
def download_bop_result_provenance(result_id: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        path = result_provenance_path(run_root, result_id)
        return send_file(
            path,
            as_attachment=True,
            download_name=f"{result_id}-provenance.json",
            mimetype="application/json",
        )
    except Exception as exc:
        return _error(exc)


@bop_evaluation_bp.get("/bop/evaluation/results/<result_id>/package")
def download_bop_result_package(result_id: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        package, _manifest = result_package_bytes(run_root, result_id)
        return send_file(
            io.BytesIO(package),
            as_attachment=True,
            download_name=f"{result_id}-package.zip",
            mimetype="application/zip",
            max_age=0,
        )
    except Exception as exc:
        return _error(exc)


@bop_evaluation_bp.get("/bop/evaluations/<evaluation_id>/report")
def download_bop_evaluation_report(evaluation_id: str):
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        report = evaluation_report_path(run_root, evaluation_id)
        return send_file(
            report,
            as_attachment=True,
            download_name=f"{evaluation_id}-report.json",
            mimetype="application/json",
        )
    except Exception as exc:
        return _error(exc)
