"""Browser-safe, ID-addressed APIs for read-only BOP pose inspection."""

from __future__ import annotations

import io
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
from posetestbot.web.security import resolve_web_run_root


bop_inspection_bp = Blueprint("bop_inspection", __name__)


def _error(exc: Exception):
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


@bop_inspection_bp.get("/bop/inspection/setup")
def bop_inspection_setup():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        result_id = request.args.get("result_id") or None
        response = inspection_setup(run_root, result_id=result_id)
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
