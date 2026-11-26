"""Browser-safe Inspect depth studies; expensive work is always queued."""

import io
import json

from flask import Blueprint, jsonify, request, send_file

from posetestbot.bop import depth_evaluation as depth
from posetestbot.bop import depth_videos as videos
from posetestbot.bop.depth_metrics import ComparisonSubmission, EvaluationSubmission
from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.web.inspect_jobs import (
    reconcile_inspect_job,
    submit_inspect_job,
)
from posetestbot.web.runtime import job_runner
from posetestbot.web.security import resolve_web_run_root


depth_evaluation_bp = Blueprint("depth_evaluation", __name__)
DEPTH_JOBS = {"evaluations": "depth_evaluation", "comparisons": "depth_comparison"}


def _error(exc):
    return jsonify({"output": str(exc)}), 404 if isinstance(
        exc, (KeyError, FileNotFoundError)
    ) else 409 if isinstance(exc, ResourceBusyError) else 400


def _json():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A versioned JSON object is required")
    return value


def _pagination():
    offset, limit = request.args.get("offset", "0"), request.args.get("limit", "20")
    if not offset.isdecimal() or not limit.isdecimal() or not 1 <= int(limit) <= 100:
        raise ValueError(
            "offset must be nonnegative and limit must be between 1 and 100"
        )
    return int(offset), int(limit)


def _status(root, kind, item):
    if item["state"] == "invalid":
        return item
    record_id = item["id"]
    return reconcile_inspect_job(
        job_runner, root=root, path=depth.folder(root, kind, record_id), item=item,
        recorded_root=lambda: depth._request(root, kind, record_id)["run_root"],
        name=DEPTH_JOBS[kind], id_parameter="depth_id", availability="report_available",
    )


@depth_evaluation_bp.get("/depth/evaluation/setup")
def depth_evaluation_setup():
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        return jsonify(depth.depth_setup(root))
    except Exception as exc:
        return _error(exc)


def _queue(kind):
    value = _json()
    # Validate the complete public contract before resolving any paths.
    if kind == "evaluations":
        EvaluationSubmission.model_validate(value)
    else:
        ComparisonSubmission.model_validate(value)
    root = resolve_web_run_root(value["run_root"])
    if kind == "comparisons":
        for item in value["evaluations"]:
            item["run_root"] = str(resolve_web_run_root(item["run_root"]))
    value["run_root"] = str(root)
    if kind == "evaluations" and not depth.renderer_status()["available"]:
        raise ValueError(depth.renderer_status()["reason"])
    record = (
        depth.create_evaluation if kind == "evaluations" else depth.create_comparison
    )(root, value)
    path = depth.folder(root, kind, record["id"])
    job = submit_inspect_job(
        job_runner,
        root=root,
        folder=path,
        name=DEPTH_JOBS[kind],
        script="scripts/run_depth_evaluation.py",
        args=("--kind", kind),
        parameters={
            "run_root": str(root),
            "depth_id": record["id"],
            "depth_kind": kind,
        },
    )
    return jsonify(
        {
            "schema_version": "depth_submission.v1",
            "id": record["id"],
            "job_id": job.id,
            "job": job.to_dict(),
        }
    ), 202


@depth_evaluation_bp.post("/depth/evaluations")
def queue_depth_evaluation():
    try:
        return _queue("evaluations")
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.post("/depth/comparisons")
def queue_depth_comparison():
    try:
        return _queue("comparisons")
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>")
def depth_history(kind):
    try:
        if kind not in depth.KINDS:
            raise KeyError("Unknown depth record kind")
        root = resolve_web_run_root(request.args.get("run_root"))
        offset, limit = _pagination()
        page = depth.history(
            root,
            kind,
            offset=offset,
            limit=limit,
            search=request.args.get("search", ""),
        )
        page["records"] = [_status(root, kind, r) for r in page["records"]]
        return jsonify(page)
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>")
@depth_evaluation_bp.get("/depth/<kind>/<record_id>/status")
def depth_status(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        return jsonify(
            {
                "schema_version": "depth_status.v1",
                **_status(root, kind, depth.record_status(root, kind, record_id)),
            }
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/report")
def depth_report(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        path = depth.artifact(root, kind, record_id, "report.json")
        return jsonify(json.loads(path.read_text()))
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/frames")
def depth_frames(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        offset, limit = _pagination()
        scene_id, input_id = request.args.get("scene_id"), request.args.get("input_id")
        if scene_id is not None and not scene_id.isdecimal():
            raise ValueError("scene_id must be nonnegative")
        if input_id is not None and not depth.re.fullmatch(r"[0-9]{1,2}", input_id):
            raise ValueError("input_id must be a comparison input ordinal")
        return jsonify(
            depth.frames_page(
                root,
                kind,
                record_id,
                offset=offset,
                limit=limit,
                scene_id=int(scene_id) if scene_id is not None else None,
                input_id=input_id,
            )
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/frames/<frame_key>")
@depth_evaluation_bp.get(
    "/depth/<kind>/<record_id>/frames/<frame_key>/media/<media_kind>"
)
def depth_frame_media(kind, record_id, frame_key, media_kind=None):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        if not depth.re.fullmatch(r"(?:[0-9]{1,2}_)?[0-9]{6,}_[0-9]{6,}", frame_key):
            raise KeyError("Unknown depth frame")
        if media_kind is None:
            path = depth.artifact(root, kind, record_id, f"frames/{frame_key}.json")
            return jsonify(json.loads(path.read_text()))
        if media_kind not in (
            "rgb",
            "measured",
            "reference",
            "error",
            "missing",
            "numeric",
        ):
            raise KeyError("Unknown depth frame media")
        relative = (
            f"frames/{frame_key}.npz"
            if media_kind == "numeric"
            else f"frames/{frame_key}-{media_kind}.png"
        )
        path = depth.artifact(root, kind, record_id, relative)
        return send_file(
            path,
            mimetype="application/octet-stream"
            if media_kind == "numeric"
            else "image/png",
            as_attachment=media_kind == "numeric",
            max_age=0,
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/download")
def depth_download(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        return send_file(
            depth.artifact(root, kind, record_id, "research.zip"),
            as_attachment=True,
            download_name=f"{record_id}-research.zip",
            mimetype="application/zip",
            max_age=0,
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/download/csv")
def depth_download_csv(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        return send_file(
            io.BytesIO(depth.depth_quality_csv(root, kind, record_id)),
            as_attachment=True,
            download_name=f"{record_id}-depth-quality.csv",
            mimetype="text/csv",
            max_age=0,
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/frame-series")
def depth_frame_series(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        publication = depth.completed(root, kind, record_id)
        if "frame_series.json" not in publication.files:
            return jsonify(
                {
                    "schema_version": depth.FRAME_SERIES_SCHEMA,
                    "id": record_id,
                    "available": False,
                    "reason": "This study was published before per-frame series "
                    "existed. Re-run it to get per-frame charts.",
                    "rows": [],
                }
            )
        value = json.loads(publication.artifact("frame_series.json").read_text())
        return jsonify({**value, "available": True})
    except Exception as exc:
        return _error(exc)


def _video_status(root, item):
    if item["state"] == "invalid":
        return item
    video_id = item["id"]
    return reconcile_inspect_job(
        job_runner, root=root, path=videos.folder(root, video_id), item=item,
        recorded_root=lambda: videos._request(root, video_id)["run_root"],
        name="depth_video", id_parameter="depth_video_id", availability="video_available",
    )


@depth_evaluation_bp.get("/depth/<kind>/<record_id>/videos")
def depth_video_choices(kind, record_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        offset, limit = _pagination()
        page = videos.history(root, kind, record_id, offset=offset, limit=limit)
        page["records"] = [_video_status(root, item) for item in page["records"]]
        return jsonify(
            {
                "schema_version": "depth_video_choices.v1",
                "encoder": videos.mp4_status(),
                "series": videos.series(root, kind, record_id),
                **page,
            }
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.post("/depth/videos")
def queue_depth_video():
    try:
        value = _json()
        videos.VideoSubmission.model_validate(value)
        root = resolve_web_run_root(value["run_root"])
        value["run_root"] = str(root)
        record = videos.create_video(root, value)
        path = videos.folder(root, record["id"])
        job = submit_inspect_job(
            job_runner,
            root=root,
            folder=path,
            name="depth_video",
            script="scripts/run_depth_video.py",
            parameters={
                "run_root": str(root),
                "depth_video_id": record["id"],
                "depth_id": value["source_id"],
            },
        )
        return jsonify(
            {
                "schema_version": "depth_video_queued.v1",
                "id": record["id"],
                "job_id": job.id,
                "job": job.to_dict(),
            }
        ), 202
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/videos/<video_id>")
def depth_video_status(video_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        return jsonify(
            {
                "schema_version": "depth_video_status.v1",
                **_video_status(root, videos.record_status(root, video_id)),
            }
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/videos/<video_id>/media")
@depth_evaluation_bp.get("/depth/videos/<video_id>/download")
@depth_evaluation_bp.get("/depth/videos/<video_id>/poster")
def depth_video_media(video_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        poster = request.path.endswith("/poster")
        return send_file(
            videos.artifact(root, video_id, "preview.png" if poster else "quality.mp4"),
            mimetype="image/png" if poster else "video/mp4",
            as_attachment=request.path.endswith("/download"),
            download_name=f"{video_id}.mp4",
            conditional=True,
            max_age=0,
        )
    except Exception as exc:
        return _error(exc)


@depth_evaluation_bp.get("/depth/videos/<video_id>/manifest")
def depth_video_manifest(video_id):
    try:
        root = resolve_web_run_root(request.args.get("run_root"))
        return jsonify(videos._publication(root, video_id)[2])
    except Exception as exc:
        return _error(exc)
