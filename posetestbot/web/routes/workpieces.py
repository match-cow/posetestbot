"""Persistent workpiece-catalogue APIs for the operator console."""

from __future__ import annotations

import io
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pose_templates.adapter import (
    PoseTemplateCreatorUnavailable,
    load_posetemplatecreator_backend,
    posetemplatecreator_status,
)
from posetestbot.pose_templates.catalog import (
    CATALOG_MANIFEST,
    UNIT_CORRECTION_FACTORS,
    CatalogGeometryRevisionConflict,
    CatalogObjectInUseError,
    catalog_export_manifest,
    default_catalog_root,
    default_working_data_root,
    get_catalog_object,
    import_catalog_metadata,
    load_catalog,
    normalize_catalog_metadata,
    preflight_catalog_unit_correction,
    resolve_catalog_asset,
    set_catalog_object_state,
    update_catalog_object_metadata,
)
from posetestbot.pose_templates.library import list_template_bundle_summaries
from posetestbot.web.runtime import job_runner
from posetestbot.web.paths import APP_ROOT


workpieces_bp = Blueprint("workpieces", __name__)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMPORT_JSON_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_BATCH_BYTES = 100 * 1024 * 1024
REQUEST_ROOT = default_working_data_root() / "jobs" / "workpiece_catalog_requests"
REQUEST_RETENTION_SECONDS = 24 * 60 * 60
EDITABLE_FIELDS = {"name", "alias", "description", "tags", "groups", "attributes"}


def _error(exc: Exception):
    if isinstance(exc, RequestEntityTooLarge):
        return jsonify(
            {"output": "Request body exceeds this endpoint's size limit"}
        ), 413
    if isinstance(exc, CatalogObjectInUseError):
        return (
            jsonify(
                {
                    "output": str(exc),
                    "blockers": exc.blockers,
                }
            ),
            409,
        )
    if isinstance(exc, CatalogGeometryRevisionConflict):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return jsonify({"output": str(exc)}), 404
    if isinstance(exc, PoseTemplateCreatorUnavailable):
        return jsonify({"output": str(exc)}), 409
    return jsonify({"output": str(exc)}), 400


def _json() -> dict[str, Any]:
    request.max_content_length = MAX_JSON_BYTES
    if request.content_length is not None and request.content_length > MAX_JSON_BYTES:
        raise RequestEntityTooLarge()
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _form_json(name: str, default: Any) -> Any:
    raw = request.form.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON") from exc


def _prune_stale_requests(kind: str) -> None:
    """Remove abandoned staging folders while preserving active job inputs."""

    root = REQUEST_ROOT / kind
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
                and not folder.is_symlink()
                and folder.name not in active_ids
                and folder.stat().st_mtime < cutoff
            ):
                shutil.rmtree(folder)
        except FileNotFoundError:
            continue


def _public_item(item: Mapping[str, Any], usage: Mapping[str, Any] | None = None):
    result = {key: value for key, value in item.items() if key != "catalog_root"}
    if usage is not None:
        result["usage"] = dict(usage)
    return result


def _template_usage() -> dict[str, dict[str, Any]]:
    usage: dict[str, dict[str, Any]] = {}
    for bundle in list_template_bundle_summaries():
        seen: set[str] = set()
        for instance in bundle.get("instances", []):
            if not isinstance(instance, Mapping):
                continue
            catalog_uuid = instance.get("catalog_uuid")
            if not isinstance(catalog_uuid, str) or catalog_uuid in seen:
                continue
            seen.add(catalog_uuid)
            record = usage.setdefault(
                catalog_uuid, {"template_count": 0, "templates": []}
            )
            record["template_count"] += 1
            record["templates"].append(
                {
                    "template_uuid": bundle["template_uuid"],
                    "display_name": bundle["display_name"],
                    "state": bundle["archive"]["state"],
                }
            )
    return usage


def _public_catalog(*, verify_assets: bool = False) -> dict[str, Any]:
    catalog = load_catalog(verify_assets=verify_assets)
    usage = _template_usage()
    result = {key: value for key, value in catalog.items() if key != "catalog_root"}
    result["objects"] = [
        _public_item(
            item,
            usage.get(item["catalog_uuid"], {"template_count": 0, "templates": []}),
        )
        for item in catalog["objects"]
    ]
    return result


@workpieces_bp.get("/workpieces/status")
def workpiece_status():
    try:
        source = posetemplatecreator_status()
        catalog = load_catalog(verify_assets=False)
        active = sum(item["state"] == "active" for item in catalog["objects"])
        archived = len(catalog["objects"]) - active
        capabilities = source.get("capabilities") or {}
        limits = capabilities.get("limits") or {}
        return jsonify(
            {
                "schema_version": "workpiece_catalog_status.v1",
                "available": bool(source.get("available")),
                "status": source.get("status", "unavailable"),
                "reason": source.get("reason"),
                "catalog_root": catalog["catalog_root"],
                "manifest": CATALOG_MANIFEST,
                "formats": capabilities.get("formats", ["ply", "stl", "obj"]),
                "limits": {
                    "cad_bytes": int(limits.get("cad_bytes", 50 * 1024 * 1024)),
                    "batch_bytes": int(
                        limits.get("batch_bytes", MAX_UPLOAD_BATCH_BYTES)
                    ),
                },
                "counts": {
                    "active": active,
                    "archived": archived,
                    "total": len(catalog["objects"]),
                },
                "unit_corrections": {
                    "supported": bool(source.get("available")),
                    "requires_archived": True,
                    "conversions": [
                        {"id": name, "factor": factor}
                        for name, factor in UNIT_CORRECTION_FACTORS.items()
                    ],
                },
            }
        )
    except (OSError, ValueError) as exc:
        return _error(exc)


@workpieces_bp.get("/workpieces/catalog")
def workpiece_catalog_list():
    try:
        return jsonify(_public_catalog())
    except (OSError, ValueError) as exc:
        return _error(exc)


@workpieces_bp.get("/workpieces/catalog/<catalog_uuid>")
def workpiece_catalog_detail(catalog_uuid: str):
    try:
        item = get_catalog_object(catalog_uuid, verify_assets=False)
        usage = _template_usage().get(
            item["catalog_uuid"], {"template_count": 0, "templates": []}
        )
        return jsonify(_public_item(item, usage))
    except Exception as exc:
        return _error(exc)


@workpieces_bp.post("/workpieces/catalog/upload")
def workpiece_catalog_upload():
    folder: Path | None = None
    try:
        # Flask/Werkzeug enforces this while streaming multipart input, including
        # requests without Content-Length. The extra MiB covers form headers.
        request.max_content_length = MAX_UPLOAD_BATCH_BYTES + 1024 * 1024
        if (
            request.content_length is not None
            and request.content_length > MAX_UPLOAD_BATCH_BYTES + 1024 * 1024
        ):
            raise RequestEntityTooLarge()
        cad = request.files.get("cad")
        textures = request.files.getlist("texture")
        if cad is None or not cad.filename:
            raise ValueError("One CAD file is required")
        if len(textures) > 1:
            raise ValueError("At most one PNG texture is supported")
        backend = load_posetemplatecreator_backend()
        safe_name = backend.safe_filename(cad.filename)
        backend.file_format(safe_name)
        metadata = normalize_catalog_metadata(
            {
                "name": request.form.get("name") or Path(safe_name).stem,
                "alias": request.form.get("alias") or None,
                "description": request.form.get("description") or None,
                "tags": _form_json("tags", []),
                "groups": _form_json("groups", []),
                "attributes": _form_json("attributes", {}),
            }
        )
        _prune_stale_requests("catalog_upload")
        request_id = uuid.uuid4().hex
        folder = REQUEST_ROOT / "catalog_upload" / request_id
        folder.mkdir(parents=True, exist_ok=False)
        cad_path = folder / safe_name
        cad.save(cad_path)
        if cad_path.stat().st_size > int(backend.constants.MAX_UPLOAD_BYTES):
            raise ValueError("CAD upload exceeds the 50 MiB file limit")
        texture_path = None
        if textures and textures[0].filename:
            texture_name = backend.safe_filename(textures[0].filename)
            if Path(texture_name).suffix.lower() != ".png":
                raise ValueError("Texture must be PNG")
            texture_path = folder / "texture.png"
            textures[0].save(texture_path)
        total = cad_path.stat().st_size + (
            texture_path.stat().st_size if texture_path else 0
        )
        if total > MAX_UPLOAD_BATCH_BYTES:
            raise ValueError("Upload exceeds the 100 MiB batch limit")
        value = {
            **metadata,
            "cad_path": cad_path.as_posix(),
            "texture_path": texture_path.as_posix() if texture_path else None,
            "catalog_root": default_catalog_root().as_posix(),
            "cleanup_request_folder": True,
        }
        request_path = folder / "request.json"
        atomic_write_json(request_path, value)
        job = job_runner.submit(
            name="workpiece_catalog_import",
            command=[
                "uv",
                "run",
                "python",
                "scripts/run_object_catalog_import.py",
                "--request",
                request_path.as_posix(),
            ],
            cwd=APP_ROOT,
            resources=["cpu", "disk_io", "workpiece_catalog"],
            scope_kind="library",
            parameters={
                "request_id": request_id,
                "request_path": request_path.as_posix(),
            },
        )
        return (
            jsonify(
                {
                    "job": job.to_dict(),
                    "job_id": job.id,
                    "request_id": request_id,
                }
            ),
            202,
        )
    except Exception as exc:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        return _error(exc)


@workpieces_bp.patch("/workpieces/catalog/<catalog_uuid>")
def workpiece_catalog_update(catalog_uuid: str):
    try:
        value = _json()
        unknown = sorted(set(value) - EDITABLE_FIELDS)
        if unknown:
            raise ValueError(
                "Unknown or immutable workpiece fields: " + ", ".join(unknown)
            )
        return jsonify(
            _public_item(update_catalog_object_metadata(catalog_uuid, value))
        )
    except Exception as exc:
        return _error(exc)


@workpieces_bp.post("/workpieces/catalog/<catalog_uuid>/unit-corrections")
def workpiece_catalog_unit_correction(catalog_uuid: str):
    folder: Path | None = None
    try:
        correction = preflight_catalog_unit_correction(catalog_uuid, _json())
        _prune_stale_requests("unit_correction")
        request_id = uuid.uuid4().hex
        folder = REQUEST_ROOT / "unit_correction" / request_id
        folder.mkdir(parents=True, exist_ok=False)
        request_value = {
            key: correction[key]
            for key in (
                "catalog_uuid",
                "conversion",
                "confirm",
                "operator",
                "expected_geometry_revision",
                "expected_canonical_sha256",
            )
        }
        request_value.update(
            catalog_root=default_catalog_root().as_posix(),
            cleanup_request_folder=True,
        )
        request_path = folder / "request.json"
        atomic_write_json(request_path, request_value)
        job = job_runner.submit(
            name="workpiece_unit_correction",
            command=[
                "uv",
                "run",
                "python",
                "scripts/run_workpiece_unit_correction.py",
                "--request",
                request_path.as_posix(),
            ],
            cwd=APP_ROOT,
            resources=["cpu", "disk_io", "workpiece_catalog"],
            scope_kind="library",
            parameters={
                "request_id": request_id,
                "request_path": request_path.as_posix(),
                "catalog_uuid": correction["catalog_uuid"],
                "conversion": correction["conversion"],
            },
        )
        return (
            jsonify(
                {
                    "job": job.to_dict(),
                    "job_id": job.id,
                    "request_id": request_id,
                    "correction": {
                        key: correction[key]
                        for key in (
                            "conversion",
                            "factor",
                            "expected_geometry_revision",
                            "expected_canonical_sha256",
                            "current_bounds_mm",
                            "resulting_bounds_mm",
                        )
                    },
                }
            ),
            202,
        )
    except Exception as exc:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        return _error(exc)


@workpieces_bp.post("/workpieces/catalog/<catalog_uuid>/<action>")
def workpiece_catalog_state(catalog_uuid: str, action: str):
    try:
        if action not in {"archive", "restore"}:
            raise KeyError("Unknown workpiece catalogue action")
        return jsonify(
            _public_item(
                set_catalog_object_state(
                    catalog_uuid,
                    state="archived" if action == "archive" else "active",
                )
            )
        )
    except Exception as exc:
        return _error(exc)


@workpieces_bp.delete("/workpieces/catalog/<catalog_uuid>")
def workpiece_catalog_delete(catalog_uuid: str):
    try:
        value = _json()
        if value.get("confirm") is not True:
            raise ValueError("confirm must be true to delete a workpiece")
        opaque_id = str(uuid.UUID(catalog_uuid))
        job = job_runner.submit(
            name="workpiece_delete",
            command=["uv", "run", "python", "scripts/run_workpiece_delete.py", opaque_id, "--confirm-delete"],
            cwd=APP_ROOT, resources=["cpu", "disk_io", f"workpiece:{opaque_id}"],
            scope_kind="library", parameters={"catalog_uuid": opaque_id, "cancelable": False},
        )
        return jsonify({"status": job.status, "job_id": job.id, "job": job.to_dict()}), 202
    except Exception as exc:
        return _error(exc)


@workpieces_bp.get("/workpieces/catalog/<catalog_uuid>/assets/<kind>")
def workpiece_catalog_asset(catalog_uuid: str, kind: str):
    try:
        item, record, path = resolve_catalog_asset(catalog_uuid, kind)
        download = kind == "source" or request.args.get("download") == "true"
        return send_file(
            path,
            mimetype=(
                "application/vnd.ply"
                if kind == "canonical_ply"
                else record.get("media_type", "application/octet-stream")
            ),
            as_attachment=download,
            download_name=item["source_filename"] if kind == "source" else path.name,
            conditional=True,
            max_age=3600,
        )
    except Exception as exc:
        return _error(exc)


@workpieces_bp.get("/workpieces/catalog/export")
def workpiece_catalog_export():
    try:
        payload = (
            json.dumps(
                catalog_export_manifest(),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        return send_file(
            io.BytesIO(payload),
            mimetype="application/json",
            as_attachment=True,
            download_name=CATALOG_MANIFEST,
            conditional=False,
        )
    except Exception as exc:
        return _error(exc)


@workpieces_bp.post("/workpieces/catalog/import")
def workpiece_catalog_import():
    try:
        request.max_content_length = MAX_IMPORT_JSON_BYTES + 1024 * 1024
        if (
            request.content_length is not None
            and request.content_length > MAX_IMPORT_JSON_BYTES + 1024 * 1024
        ):
            raise RequestEntityTooLarge()
        upload = request.files.get("catalog")
        if upload is None or not upload.filename:
            raise ValueError("One catalogue JSON file is required")
        if Path(upload.filename).suffix.lower() != ".json":
            raise ValueError("Catalogue import must be a JSON file")
        payload = upload.read(MAX_IMPORT_JSON_BYTES + 1)
        if len(payload) > MAX_IMPORT_JSON_BYTES:
            raise ValueError("Imported catalogue JSON exceeds 16 MiB")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Catalogue import is not valid UTF-8 JSON") from exc
        return jsonify(import_catalog_metadata(value))
    except Exception as exc:
        return _error(exc)
