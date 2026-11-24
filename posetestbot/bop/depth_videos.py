"""Cancelable MP4 exports from immutable, completed Inspect depth evidence."""

from __future__ import annotations

from posetestbot.jobs.cancellation import write_terminal_progress

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Literal
import uuid

from PIL import Image, ImageDraw, ImageFont
from pydantic import Field

from posetestbot.bop import depth_evaluation as depth
from posetestbot.bop.depth_metrics import Contract
from posetestbot.bop.depth_sources import read_json
from posetestbot.bop.evaluation import _has_symlink_component, _sha256_file
from posetestbot.bop.inspection import (
    _required_plain_file,
    _safe_relative,
    _valid_recorded_run_root,
)
from posetestbot.bop.inspection_exports import mp4_status
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.sqlite import open_readonly


class VideoSubmission(Contract):
    schema_version: Literal["depth_video_submission.v1"]
    run_root: str
    source_kind: Literal["evaluations", "comparisons"]
    source_id: str
    scene_id: int = Field(ge=0)
    input_id: str | None = Field(default=None, pattern=r"^[0-9]{1,2}$")
    view: Literal["overview", "measured", "error", "missing"] = "overview"
    fps: int = Field(default=15, ge=1, le=60)
    stride: int = Field(default=1, ge=1, le=120)
    width: Literal[960, 1280, 1920] = 1280


def folder(root, video_id):
    root = Path(root).resolve()
    if not isinstance(video_id, str) or not re.fullmatch(
        r"depth-video-[0-9a-f]{12}", video_id
    ):
        raise KeyError("Unknown depth video")
    path = root / depth.BASE / "videos" / video_id
    if _has_symlink_component(path, root=root):
        raise ValueError("Depth video path must not use symbolic links")
    return path


def _request(root, video_id):
    root = Path(root).resolve()
    path = folder(root, video_id)
    value = read_json(root, (path / "request.json").relative_to(root).as_posix())
    if (
        value.get("schema_version") != "depth_video_request.v1"
        or value.get("id") != video_id
        or not _valid_recorded_run_root(value.get("run_root"))
    ):
        raise ValueError("Depth video request identity/schema is invalid")
    submission = VideoSubmission.model_validate(value["submission"])
    if submission.run_root != value["run_root"]:
        raise ValueError("Depth video submission run identity differs")
    return value


def _frames(publication):
    with open_readonly(publication.artifact("frames.sqlite3")) as db:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT metadata FROM frames ORDER BY ordinal")
        ]


def series(root, kind, record_id):
    publication = depth.completed(root, kind, record_id)
    report = json.loads(publication.artifact("report.json").read_text())
    groups = {}
    for frame in _frames(publication):
        key = frame.get("input_id"), frame["scene_id"]
        if key not in groups:
            groups[key] = {
                "input_id": key[0],
                "scene_id": key[1],
                "sensor_name": frame["sensor_name"],
                "finish_label": frame.get("finish_label", report.get("finish_label")),
                "width": frame["width"],
                "height": frame["height"],
                "frame_count": 0,
            }
        groups[key]["frame_count"] += 1
    return list(groups.values())


def create_video(root, value):
    submission = VideoSubmission.model_validate(value)
    root = Path(root).resolve()
    if submission.run_root != str(root):
        raise ValueError("Depth video owner differs from its run")
    status = mp4_status()
    if not status["available"]:
        raise ValueError(status["reason"])
    publication = depth.completed(root, submission.source_kind, submission.source_id)
    report = json.loads(publication.artifact("report.json").read_text())
    frames = [
        f
        for f in _frames(publication)
        if f["scene_id"] == submission.scene_id
        and f.get("input_id") == submission.input_id
    ]
    frames.sort(key=lambda f: f["im_id"])
    if not frames:
        raise ValueError("Choose a retained camera series with frames")
    if len({(f["width"], f["height"], f["sensor_name"]) for f in frames}) != 1:
        raise ValueError("Video frames must have one sensor and resolution")
    frames = frames[:: submission.stride]
    media = (
        ("rgb", "measured", "reference", "error")
        if submission.view == "overview"
        else (submission.view,)
    )
    sources = {
        "report.json": publication.files["report.json"],
        "frames.sqlite3": publication.files["frames.sqlite3"],
    }
    for frame in frames:
        if not re.fullmatch(r"(?:[0-9]{1,2}_)?[0-9]{6,}_[0-9]{6,}", frame["key"]):
            raise ValueError("Invalid retained depth frame key")
        for name in media:
            relative = f"frames/{frame['key']}-{name}.png"
            if relative not in publication.files:
                raise ValueError("Retained frame media is incomplete")
            sources[relative] = publication.files[relative]
    video_id = "depth-video-" + uuid.uuid4().hex[:12]
    value = {
        "schema_version": "depth_video_request.v1",
        "id": video_id,
        "run_root": str(root),
        "created_at": depth.now(),
        "submission": submission.model_dump(),
        "source_manifest_sha256": publication.manifest_sha256,
        "source_files": sources,
        "frame_keys": [f["key"] for f in frames],
        "sensor_name": frames[0]["sensor_name"],
        "finish_label": frames[0].get("finish_label", report.get("finish_label")),
        "scales": report["scales"],
        "implementation_sha256": {
            p: _sha256_file(depth.APP_ROOT / p)
            for p in ("posetestbot/bop/depth_videos.py", "scripts/run_depth_video.py")
        },
    }
    path = folder(root, video_id)
    path.mkdir(parents=True, exist_ok=False)
    atomic_write_json(path / "request.json", value)
    atomic_write_json(
        path / "progress.json",
        {"state": "queued", "completed_frames": 0, "total_frames": len(frames)},
    )
    return value


def _publication(root, video_id):
    path = folder(root, video_id)
    request = _request(root, video_id)
    complete = _safe_relative(path, "complete", label="Depth video completion")
    if not complete.is_dir():
        raise FileNotFoundError(
            "Depth video has not completed; partial videos are unavailable"
        )
    manifest = read_json(path, "complete/manifest.json")
    if (
        manifest.get("schema_version") != "depth_video_publication.v1"
        or manifest.get("id") != video_id
        or manifest.get("request_sha256") != _sha256_file(path / "request.json")
        or manifest.get("source_manifest_sha256") != request["source_manifest_sha256"]
        or manifest.get("submission") != request["submission"]
        or set(manifest.get("files", {})) != {"quality.mp4", "preview.png"}
    ):
        raise ValueError("Depth video publication does not bind its request and output")
    return request, complete, manifest


def artifact(root, video_id, relative="quality.mp4"):
    if relative not in {"quality.mp4", "preview.png"}:
        raise KeyError("Unknown depth video media")
    _, complete, manifest = _publication(root, video_id)
    path = _safe_relative(complete, relative, label="Depth video output")
    _required_plain_file(path, root=complete, label="Depth video output")
    if _sha256_file(path) != manifest["files"][relative]:
        raise ValueError("Depth video content hash changed")
    return path


def record_status(root, video_id):
    path = folder(root, video_id)
    value = _request(root, video_id)
    available = (path / "complete").is_dir()
    manifest = _publication(root, video_id)[2] if available else None
    progress = read_json(path, "progress.json")
    return {
        "id": video_id,
        "created_at": value["created_at"],
        "submission": value["submission"],
        "sensor_name": value["sensor_name"],
        "finish_label": value["finish_label"],
        "state": "complete" if available else progress["state"],
        "video_available": available,
        "progress": progress,
        "frame_count": len(value["frame_keys"]),
        "duration_seconds": len(value["frame_keys"]) / value["submission"]["fps"],
        "output_size": manifest["output_size"] if manifest else None,
    }


def history(root, kind, record_id, *, offset=0, limit=20):
    depth.completed(root, kind, record_id)
    base = Path(root).resolve() / depth.BASE / "videos"
    if _has_symlink_component(base, root=Path(root).resolve()):
        raise ValueError("Depth video history uses a symbolic link")
    rows = []
    for path in base.glob("depth-video-*/request.json"):
        try:
            value = _request(root, path.parent.name)
            submission = value["submission"]
            if submission["source_kind"] == kind and submission["source_id"] == record_id:
                rows.append(record_status(root, value["id"]))
        except (OSError, ValueError, KeyError) as exc:
            rows.append({
                "id": path.parent.name, "created_at": "", "state": "invalid",
                "video_available": False, "submission": None,
                "progress": {"state": "invalid", "error": str(exc)},
            })
    rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    return {
        "records": rows[offset : offset + limit],
        "total": len(rows),
        "offset": offset,
        "limit": limit,
    }


def _font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _compose(request, frame, images, ordinal):
    width = request["submission"]["width"]
    overview = request["submission"]["view"] == "overview"
    margin, gap, label_height, header, footer = 16, 12, 28, 76, 90
    panel_width = (width - 2 * margin - (gap if overview else 0)) // (
        2 if overview else 1
    )
    panel_height = round(panel_width * frame["height"] / frame["width"])
    height = (
        header + (panel_height + label_height + gap) * (2 if overview else 1) + footer
    )
    height += height % 2
    canvas = Image.new("RGB", (width, height), (15, 23, 42))
    draw = ImageDraw.Draw(canvas)
    font, small = _font(max(14, width // 75)), _font(max(12, width // 90))
    draw.text(
        (margin, 12),
        f"Depth quality | {request['finish_label']}",
        font=font,
        fill="white",
    )
    draw.text(
        (margin, 40),
        f"{request['sensor_name']} | frame {frame['im_id']} | {ordinal + 1}/{len(request['frame_keys'])}",
        font=small,
        fill="#cbd5e1",
    )
    titles = {
        "rgb": "RGB",
        "measured": "Measured depth",
        "reference": "Reference depth",
        "error": "Signed error (measured - reference)",
        "missing": "Missing returns",
    }
    for i, (name, image) in enumerate(images.items()):
        x = margin + (i % 2) * (panel_width + gap) if overview else margin
        y = (
            header + (i // 2) * (panel_height + label_height + gap)
            if overview
            else header
        )
        draw.text((x, y), titles[name], font=small, fill="white")
        canvas.paste(
            image.resize(
                (panel_width, panel_height),
                Image.Resampling.NEAREST if name != "rgb" else Image.Resampling.LANCZOS,
            ),
            (x, y + label_height),
        )
    metrics = [i["regions"]["interior"] for i in frame["instances"]]
    reference = sum(m["reference_pixels"] for m in metrics)
    valid = sum(m["valid_pixels"] for m in metrics)
    completeness = f"{100 * valid / reference:.1f}%" if reference else "unavailable"
    y = height - footer + 4
    draw.text(
        (margin, y),
        f"Interior completeness: {completeness} | Missing: {reference - valid:,} / {reference:,} px",
        font=small,
        fill="white",
    )
    scales = request["scales"]
    draw.text(
        (margin, y + 26),
        f"Depth 0-{scales['depth_max_mm']:g} mm | Error -{scales['error_max_mm']:g} to +{scales['error_max_mm']:g} mm (blue to red)",
        font=small,
        fill="#cbd5e1",
    )
    draw.text(
        (margin, y + 50),
        "Magenta: missing depth | Dark grey: outside object | Constant-rate review; capture timing is not reproduced",
        font=small,
        fill="#cbd5e1",
    )
    return canvas


def run_video(request_path):
    path = Path(request_path).absolute()
    value = json.loads(path.read_text())
    root, video_id = Path(value["run_root"]).resolve(), value["id"]
    target = folder(root, video_id)
    if value["run_root"] != str(root) or path != target / "request.json":
        raise ValueError("Depth video request must live in its run-owned record")
    request = _request(root, video_id)
    submission = VideoSubmission.model_validate(request["submission"])
    request_hash = _sha256_file(path)
    with (target / ".started").open("x"):
        pass
    work = target / ".work"
    work.mkdir()
    encoder = None
    count = 0

    def progress(state, **extra):
        atomic_write_json(
            target / "progress.json",
            {
                "state": state,
                "completed_frames": count,
                "total_frames": len(request["frame_keys"]),
                **extra,
            },
        )

    def check():
        publication = depth.completed(
            root,
            submission.source_kind,
            submission.source_id,
            expected_manifest_sha256=request["source_manifest_sha256"],
        )
        if _sha256_file(path) != request_hash:
            raise ValueError("Depth video request changed")
        for relative, expected in request["source_files"].items():
            if publication.files.get(relative) != expected:
                raise ValueError("Depth video source manifest changed")
            publication.artifact(relative)
        if any(
            _sha256_file(depth.APP_ROOT / p) != h
            for p, h in request["implementation_sha256"].items()
        ):
            raise ValueError("Depth video implementation changed")
        return publication

    try:
        progress("validating")
        publication = check()
        status = mp4_status()
        if not status["available"]:
            raise ValueError(status["reason"])
        frames = {f["key"]: f for f in _frames(publication)}
        progress("encoding")
        media = (
            ("rgb", "measured", "reference", "error")
            if submission.view == "overview"
            else (submission.view,)
        )
        output_size = None
        for key in request["frame_keys"]:
            frame = frames[key]
            images = {}
            for name in media:
                relative = f"frames/{key}-{name}.png"
                source = publication.artifact(relative)
                with Image.open(source) as image:
                    if image.size != (frame["width"], frame["height"]):
                        raise ValueError("Depth video frame dimensions changed")
                    images[name] = image.convert("RGB")
            image = _compose(request, frame, images, count)
            if encoder is None:
                image.save(work / "preview.png")
                output_size = image.size
                encoder = subprocess.Popen(
                    [
                        shutil.which("ffmpeg"),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "-s",
                        f"{image.width}x{image.height}",
                        "-r",
                        str(submission.fps),
                        "-i",
                        "pipe:0",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-threads",
                        "2",
                        "-crf",
                        "18",
                        "-preset",
                        "veryfast",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        "-n",
                        str(work / "quality.mp4"),
                    ],
                    stdin=subprocess.PIPE,
                )
            encoder.stdin.write(image.tobytes())
            count += 1
            progress("encoding")
        if encoder is None:
            raise ValueError("Depth video has no frames")
        encoder.stdin.close()
        if encoder.wait(timeout=120) != 0:
            raise ValueError("FFmpeg failed to encode the depth video; see job logs")
        progress("publishing")
        atomic_write_json(
            work / "manifest.json",
            {
                "schema_version": "depth_video_publication.v1",
                "id": video_id,
                "request_sha256": request_hash,
                "source_manifest_sha256": request["source_manifest_sha256"],
                "submission": submission.model_dump(),
                "scales": request["scales"],
                "frame_count": count,
                "duration_seconds": count / submission.fps,
                "output_size": list(output_size),
                "codec": "libx264",
                "pixel_format": "yuv420p",
                "timing": "constant-rate review in ascending image-ID order; capture timestamps are not reproduced",
                "files": {
                    name: _sha256_file(work / name)
                    for name in ("quality.mp4", "preview.png")
                },
            },
        )
        check()
        os.rename(work, target / "complete")
        progress("complete")
        return record_status(root, video_id)
    except BaseException as exc:
        write_terminal_progress(progress, exc, depth.DepthCanceled)
        raise
    finally:
        if encoder is not None and encoder.poll() is None:
            encoder.terminate()
            try:
                encoder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                encoder.kill()
                encoder.wait()
