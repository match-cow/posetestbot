"""Scoped APIs for PoseGridGen previews and immutable target bundles."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file
from pydantic import ValidationError

from posetestbot.calibration.posegridgen import (
    PoseGridGenUnavailable,
    build_posegridgen_scene,
    fit_posegridgen_request,
    posegridgen_capabilities,
    posegridgen_status,
    render_posegridgen_preview,
)
from posetestbot.calibration.target_library import (
    CalibrationTargetConflict,
    PLACEMENT_MODES,
    POSEGRIDGEN_SOURCE,
    _FILE_CONTRACT,
    default_target_library_root,
    delete_target_bundle,
    list_target_bundles,
    normalize_target_mounting_frame,
    replacement_blockers,
    validate_configured_target_mounting,
    validate_bundle_placement,
    validate_run_target_selection,
    validate_target_bundle,
)
from posetestbot.calibration.target_preview import render_target_preview_png
from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner


calibration_targets_bp = Blueprint("calibration_targets", __name__)
MAX_TARGET_REQUEST_BYTES = 256 * 1024


def _request_json() -> dict:
    if (
        request.content_length is not None
        and request.content_length > MAX_TARGET_REQUEST_BYTES
    ):
        raise ValueError("Calibration-target request exceeds 256 KiB")
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _validation_errors(exc: ValidationError) -> list[dict]:
    return [
        {
            "code": item["type"],
            "path": [part for part in item["loc"]],
            "message": item["msg"],
        }
        for item in exc.errors()
    ]


def _fit_error_response(exc: Exception):
    if hasattr(exc, "detail"):
        return jsonify({"errors": [exc.detail()]}), 422
    raise exc


@calibration_targets_bp.get("/calibration-targets/status")
def calibration_target_status():
    status = posegridgen_status()
    return jsonify(
        {
            "schema_version": "calibration_target_generator_status.v1",
            "generation_available": bool(status["available"]),
            "generator": status,
        }
    )


@calibration_targets_bp.get("/calibration-targets/capabilities")
def calibration_target_capabilities():
    try:
        return jsonify(posegridgen_capabilities())
    except PoseGridGenUnavailable as exc:
        return jsonify({"output": str(exc)}), 409


@calibration_targets_bp.post("/calibration-targets/fit")
def calibration_target_fit():
    try:
        return jsonify(fit_posegridgen_request(_request_json()))
    except ValidationError as exc:
        return jsonify({"errors": _validation_errors(exc)}), 422
    except PoseGridGenUnavailable as exc:
        return jsonify({"output": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    except Exception as exc:
        return _fit_error_response(exc)


@calibration_targets_bp.post("/calibration-targets/preview")
def calibration_target_preview():
    try:
        payload, configuration_hash = render_posegridgen_preview(_request_json())
    except ValidationError as exc:
        return jsonify({"errors": _validation_errors(exc)}), 422
    except PoseGridGenUnavailable as exc:
        return jsonify({"output": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    except Exception as exc:
        return _fit_error_response(exc)
    return Response(
        payload,
        mimetype="image/png",
        headers={
            "X-Configuration-Hash": configuration_hash,
            "Cache-Control": "no-store",
        },
    )


@calibration_targets_bp.get("/calibration-targets/bundles")
def calibration_target_bundles():
    run_root = request.args.get("run_root") or None
    try:
        bundles = list_target_bundles(
            library_root=default_target_library_root(), run_root=run_root
        )
        blockers = replacement_blockers(run_root) if run_root else []
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify(
        {
            "schema_version": "calibration_target_library.v1",
            "run_root": run_root,
            "bundles": bundles,
            "replacement_blockers": blockers,
        }
    )


@calibration_targets_bp.delete("/calibration-targets/bundles/<target_id>")
def calibration_target_delete(target_id: str):
    try:
        value = _request_json()
        if value.get("confirm") is not True:
            raise ValueError("confirm must be true to delete a calibration target")
        run_root = value.get("run_root")
        if not run_root:
            raise ValueError("run_root is required")
        result = delete_target_bundle(
            target_id=target_id,
            library_root=default_target_library_root(),
            run_root=run_root,
        )
    except CalibrationTargetConflict as exc:
        return jsonify({"output": str(exc), "blockers": exc.blockers}), 409
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify(result)


@calibration_targets_bp.post("/calibration-targets/generate")
def calibration_target_generate():
    try:
        value = _request_json()
        display_name = str(value.get("display_name", "")).strip()
        if not display_name:
            raise ValueError("display_name is required")
        if len(display_name) > 120:
            raise ValueError("display_name must not exceed 120 characters")
        configuration = value.get("configuration")
        if not isinstance(configuration, dict):
            raise ValueError("configuration must be an object")
        # Validate Pydantic and page-fit errors before accepting a background job.
        build_posegridgen_scene(configuration)
        request_id = uuid.uuid4().hex
        request_root = (
            APP_ROOT / "working_data" / "jobs" / "calibration_target_requests"
        )
        request_path = request_root / f"{request_id}.json"
        atomic_write_json(
            request_path,
            {"display_name": display_name, "configuration": configuration},
        )
        job = job_runner.submit(
            name="calibration_target_generate",
            command=[
                "uv",
                "run",
                "python",
                "scripts/run_calibration_target_generate.py",
                "--request",
                request_path.as_posix(),
            ],
            cwd=APP_ROOT,
            resources=["cpu", "disk_io"],
            scope_kind="library",
            parameters={"display_name": display_name, "request_id": request_id},
        )
    except ValidationError as exc:
        return jsonify({"errors": _validation_errors(exc)}), 422
    except PoseGridGenUnavailable as exc:
        return jsonify({"output": str(exc)}), 409
    except ResourceBusyError as exc:
        return jsonify({"output": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    except Exception as exc:
        return _fit_error_response(exc)
    return jsonify({"job": job.to_dict(), "job_id": job.id}), 202


@calibration_targets_bp.post("/calibration-targets/bundles/<target_id>/select")
def calibration_target_select(target_id: str):
    try:
        value = _request_json()
        run_root = value.get("run_root")
        placement = str(value.get("placement", ""))
        raw_mounting_frame = value.get("mounting_frame")
        mounting_frame = (
            str(raw_mounting_frame) if raw_mounting_frame is not None else None
        )
        if not run_root:
            raise ValueError("run_root is required")
        if placement not in PLACEMENT_MODES:
            raise ValueError(
                "placement must be one of: " + ", ".join(sorted(PLACEMENT_MODES))
            )
        mounting_frame = normalize_target_mounting_frame(placement, mounting_frame)
        config = load_run_config_for_run_root(run_root)
        validate_configured_target_mounting(config, mounting_frame)
        existing = config.get("calibration_target")
        if isinstance(existing, dict):
            existing_placement = existing.get("placement", {})
            current_mode = existing_placement.get("mode")
            current_mounting_frame = existing_placement.get("mounting_frame")
            if (
                existing.get("target_id") == target_id
                and current_mode == placement
                and current_mounting_frame == mounting_frame
            ):
                evidence = validate_run_target_selection(run_root)
                return (
                    jsonify(
                        {
                            "schema_version": "calibration_target_selection.v1",
                            "status": "unchanged",
                            "run_root": str(Path(run_root)),
                            "selection": existing,
                            "evidence": evidence,
                            "blockers": [],
                        }
                    ),
                    200,
                )
        bundle = validate_target_bundle(
            default_target_library_root() / target_id,
            library_root=default_target_library_root(),
        )
        validate_bundle_placement(
            bundle,
            placement,
            mounting_frame=mounting_frame,
        )
        blockers = replacement_blockers(run_root)
        if blockers:
            raise CalibrationTargetConflict(
                "The calibration target and its mounting must be bound before raw "
                "acquisition or target-dependent evidence exists; create a new run.",
                blockers=blockers,
            )
        command = [
            "uv",
            "run",
            "python",
            "scripts/run_calibration_target_select.py",
            str(run_root),
            bundle["target_id"],
            "--placement",
            placement,
        ]
        if mounting_frame is not None:
            command.extend(["--mounting-frame", mounting_frame])
        job = job_runner.submit(
            name="calibration_target_select",
            command=command,
            cwd=APP_ROOT,
            resources=["disk_io"],
            scope_kind="run",
            run_root=run_root,
            parameters={
                "run_root": str(run_root),
                "target_id": bundle["target_id"],
                "placement": placement,
                "mounting_frame": mounting_frame,
            },
        )
    except CalibrationTargetConflict as exc:
        return jsonify({"output": str(exc), "blockers": exc.blockers}), 409
    except ResourceBusyError as exc:
        return jsonify({"output": str(exc)}), 409
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify({"job": job.to_dict(), "job_id": job.id}), 202


@calibration_targets_bp.get("/calibration-targets/bundles/<target_id>/preview.png")
def calibration_target_bundle_preview(target_id: str):
    try:
        bundle = validate_target_bundle(
            default_target_library_root() / target_id,
            library_root=default_target_library_root(),
        )
        with open(Path(bundle["bundle_path"]) / POSEGRIDGEN_SOURCE) as handle:
            source_manifest = json.load(handle)
        payload = render_target_preview_png(
            bundle["target"], source_manifest=source_manifest
        )
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return Response(
        payload,
        mimetype="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{bundle["configuration_sha256"]}"',
        },
    )


@calibration_targets_bp.get(
    "/calibration-targets/bundles/<target_id>/download/<artifact>"
)
def calibration_target_download(target_id: str, artifact: str):
    if artifact not in _FILE_CONTRACT:
        return jsonify({"output": "Unknown calibration-target download"}), 404
    try:
        bundle = validate_target_bundle(
            default_target_library_root() / target_id,
            library_root=default_target_library_root(),
        )
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    relative_path, media_type = _FILE_CONTRACT[artifact]
    return send_file(
        Path(bundle["bundle_path"]) / relative_path,
        mimetype=media_type,
        as_attachment=True,
        download_name=relative_path,
        conditional=True,
    )
