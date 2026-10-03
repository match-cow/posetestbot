"""Retained, queued visualization exports; writes only under bop_evaluation."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import zipfile
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image

from posetestbot.bop.evaluation import _has_symlink_component, _sha256_file
from posetestbot.bop.inspection import (
    DEFAULT_HYPOTHESES,
    MAX_HYPOTHESES,
    MAX_MEDIA_BYTES,
    MAX_MEDIA_PIXELS,
    _artifact_signature,
    _index,
    _inspection_signature,
    _integer_id,
    _load_json,
    _required_plain_file,
    _safe_relative,
    colorize_depth,
    inspection_frame_from_index,
    matching_inspection_frames,
)
from posetestbot.bop.inspection_render import (
    GEOMETRY_DEFAULTS,
    MASK_DEFAULTS,
    MASK_COLORS,
    blend,
    load_geometry,
    render_geometry,
)
from posetestbot.io.atomic import atomic_write_json

EXPORT_ID_RE = re.compile(r"^visualization-[0-9a-f]{12}$")
SCHEMA = "bop_inspection_export.v1"
RENDERER = "bop_xray_cpu.v1"


class ExportCanceled(Exception):
    """Raised by the job process's termination handler."""


@lru_cache(maxsize=4)
def _encoder_status(executable: str, mtime_ns: int) -> dict:
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if re.search(r"^\s*V\S*\s+libx264\s", result.stdout, re.MULTILINE):
            return {"available": True, "reason": None}
        reason = "FFmpeg does not provide the libx264 encoder."
    except (OSError, subprocess.SubprocessError):
        reason = "FFmpeg encoder availability could not be verified."
    return {"available": False, "reason": reason}


def mp4_status() -> dict:
    executable = shutil.which("ffmpeg")
    if not executable:
        return {
            "available": False,
            "reason": "FFmpeg is missing. Install FFmpeg with libx264 to enable MP4; image ZIP remains available.",
        }
    try:
        return dict(_encoder_status(executable, Path(executable).stat().st_mtime_ns))
    except OSError:
        return {"available": False, "reason": "FFmpeg executable is unavailable."}


def normalize_settings(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Export settings must be an object")
    allowed = {
        "run_root",
        "result_id",
        "scene_id",
        "filter",
        "object_id",
        "background",
        "geometry",
        "masks",
        "format",
        "fps",
        "max_hypotheses",
    }
    if set(value) - allowed:
        raise ValueError(
            "Unknown export settings: " + ", ".join(sorted(set(value) - allowed))
        )
    scene_id = _integer_id(value.get("scene_id"), label="scene_id")
    object_id = value.get("object_id")
    if object_id is not None:
        _integer_id(object_id, label="object_id", minimum=1)
    fps = _integer_id(value.get("fps", 30), label="fps", minimum=1)
    if fps > 120:
        raise ValueError("fps must be between 1 and 120")
    hypotheses = _integer_id(
        value.get("max_hypotheses", DEFAULT_HYPOTHESES),
        label="max_hypotheses",
        minimum=1,
    )
    if hypotheses > MAX_HYPOTHESES:
        raise ValueError(f"max_hypotheses must be at most {MAX_HYPOTHESES}")
    if value.get("format") not in ("zip", "mp4"):
        raise ValueError("format must be zip or mp4")
    if value.get("background", "rgb") not in ("rgb", "depth"):
        raise ValueError("background must be rgb or depth")
    layers = {}
    for group, defaults in (("geometry", GEOMETRY_DEFAULTS), ("masks", MASK_DEFAULTS)):
        supplied = value.get(group, {})
        if not isinstance(supplied, dict) or set(supplied) - set(defaults):
            raise ValueError(f"{group} contains unknown layer settings")
        layers[group] = {**defaults, **supplied}
        for key, item in layers[group].items():
            if isinstance(defaults[key], bool):
                if type(item) is not bool:
                    raise ValueError(f"{group}.{key} must be boolean")
            elif (
                type(item) not in (int, float)
                or not math.isfinite(item)
                or not 0 <= item <= 1
            ):
                raise ValueError(f"{group}.{key} must be a finite opacity from 0 to 1")
    return {
        "result_id": value.get("result_id"),
        "scene_id": scene_id,
        "filter": value.get("filter", "all"),
        "object_id": object_id,
        "format": value["format"],
        "fps": fps,
        "max_hypotheses": hypotheses,
        "background": value.get("background", "rgb"),
        **layers,
    }


def export_folder(run_root: str | Path, export_id: str) -> Path:
    root = Path(run_root).resolve()
    if not isinstance(export_id, str) or not EXPORT_ID_RE.fullmatch(export_id):
        raise KeyError("Unknown visualization export")
    folder = root / "processed" / "bop_evaluation" / "visualizations" / export_id
    if _has_symlink_component(folder, root=root):
        raise ValueError("Visualization export path uses a symbolic link")
    return folder


def _stat_record(root: Path, path: Path) -> dict:
    _required_plain_file(path, root=root, label="Visualization source")
    return {
        "path": path.relative_to(root).as_posix(),
        "stat": list(_artifact_signature(path)[1:]),
    }


def _check_sources(root: Path, sources: list[dict]) -> None:
    for source in sources:
        path = _safe_relative(root, source["path"], label="Visualization source")
        if _stat_record(root, path) != source:
            raise ValueError(f"Visualization source changed: {source['path']}")


def _signature_hash(signature) -> str:
    return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()


def create_export_request(run_root: str | Path, value: dict) -> dict:
    settings = normalize_settings(value)
    if settings["format"] == "mp4" and not mp4_status()["available"]:
        raise ValueError(mp4_status()["reason"])
    root = Path(run_root).resolve()
    # Submission is an integrity boundary. Revalidate image inventory too,
    # rather than trusting the browser adapter's metadata-only index cache.
    index = _index(root, settings["result_id"], refresh=True)
    rows = matching_inspection_frames(
        index, settings["scene_id"], settings["filter"], settings["object_id"]
    )
    if not rows:
        raise ValueError("No frames match the selected scene and filters")
    scene = index["scenes_by_id"][settings["scene_id"]]
    paths = set()
    # Bind metadata/model/result identities at submission, without reading all
    # frame pixels in the HTTP handler. ctime/ino also catch restored mtimes.
    signature = _inspection_signature(root, settings["result_id"])
    for item in signature:
        if item[1] >= 0:
            paths.add(Path(item[0]))
    for row in rows:
        im_id = row["im_id"]
        paths.add(scene["scene_path"] / settings["background"] / f"{im_id:06d}.png")
        for kind, directory in (("full", "mask"), ("visible", "mask_visib")):
            if not settings["masks"][kind]:
                continue
            if not scene["capabilities"][kind + "_mask"]:
                raise ValueError(f"The selected scene has no {kind} masks")
            for gt_id in range(len(scene["ground_truth"][str(im_id)])):
                paths.add(
                    scene["scene_path"] / directory / f"{im_id:06d}_{gt_id:06d}.png"
                )
    sources = [_stat_record(root, path) for path in sorted(paths)]
    export_id = "visualization-" + uuid.uuid4().hex[:12]
    request = {
        "schema_version": SCHEMA,
        "renderer": RENDERER,
        "export_id": export_id,
        "run_root": root.as_posix(),
        "settings": settings,
        "scene_name": scene["display_name"],
        "frame_ids": [row["im_id"] for row in rows],
        "image_size": list(index["expected_size"]),
        "source_identity": {
            "dataset_sha256": index["dataset"]["dataset_sha256"],
            "result_sha256": index["result"]["sha256"],
            "inspection_signature_sha256": _signature_hash(signature),
        },
        "sources": sources,
    }
    folder = export_folder(root, export_id)
    folder.mkdir(parents=True, exist_ok=False)
    atomic_write_json(folder / "request.json", request)
    atomic_write_json(
        folder / "progress.json",
        {"state": "queued", "completed_frames": 0, "total_frames": len(rows)},
    )
    return request


def read_export_request(root, export_id):
    folder = export_folder(root, export_id)
    path = _required_plain_file(
        folder / "request.json", root=Path(root).resolve(), label="Export request"
    )
    request = _load_json(path, label="Export request")
    if (
        request.get("schema_version") != SCHEMA
        or request.get("renderer") != RENDERER
        or request.get("export_id") != export_id
        or request.get("run_root") != Path(root).resolve().as_posix()
    ):
        raise ValueError("Export request identity is invalid")
    if normalize_settings(request["settings"]) != request["settings"]:
        raise ValueError("Export request settings are invalid")
    return request


def read_export_artifact(root, export_id, name):
    folder = export_folder(root, export_id)
    return _load_json(
        _required_plain_file(
            folder / name, root=Path(root).resolve(), label="Export evidence"
        ),
        label="Export evidence",
    )


def export_download_path(root, export_id):
    request = read_export_request(root, export_id)
    folder = export_folder(root, export_id)
    record = read_export_artifact(root, export_id, "output.json")
    path = folder / f"{export_id}.{request['settings']['format']}"
    _required_plain_file(path, root=Path(root).resolve(), label="Completed export")
    if (
        record.get("request_sha256") != _sha256_file(folder / "request.json")
        or record.get("manifest_sha256")
        != _sha256_file(
            _required_plain_file(
                folder / "manifest.json",
                root=Path(root).resolve(),
                label="Export manifest",
            )
        )
        or record.get("size_bytes") != path.stat().st_size
        or record.get("sha256") != _sha256_file(path)
    ):
        raise ValueError("Completed export failed its integrity check")
    return path


def _read_image(root, relative, expected_size, *, depth=False):
    path = _safe_relative(root, relative, label="Frame image")
    _required_plain_file(path, root=root, label="Frame image")
    if path.stat().st_size > MAX_MEDIA_BYTES:
        raise ValueError("Frame image exceeds inspection size cap")
    with path.open("rb") as handle:
        content = handle.read(MAX_MEDIA_BYTES + 1)
    if len(content) > MAX_MEDIA_BYTES:
        raise ValueError("Frame image exceeds inspection size cap")
    with Image.open(io.BytesIO(content)) as image:
        if (
            image.format != "PNG"
            or list(image.size) != expected_size
            or image.width * image.height > MAX_MEDIA_PIXELS
        ):
            raise ValueError("Frame image dimensions or format changed")
        if depth:
            decoded = cv2.imdecode(
                np.frombuffer(content, np.uint8), cv2.IMREAD_UNCHANGED
            )
            if decoded is None or decoded.dtype != np.uint16 or decoded.ndim != 2:
                raise ValueError("Depth must be a 16-bit PNG")
            pixels = cv2.cvtColor(colorize_depth(decoded), cv2.COLOR_BGR2RGB)
        else:
            pixels = np.array(image.convert("RGB"))
    return pixels, hashlib.sha256(content).hexdigest()


def run_export_request(request_path: str | Path) -> dict:
    request_path = Path(request_path).absolute()
    root = request_path.parents[4]
    export_id = request_path.parent.name
    folder = export_folder(root, export_id)
    if request_path != folder / "request.json":
        raise ValueError("Export request must be in the run visualization folder")
    request = read_export_request(root, export_id)
    request_sha256 = _sha256_file(request_path)
    settings = request["settings"]
    output = folder / f"{export_id}.{settings['format']}"
    partial = folder / f".partial.{settings['format']}"
    # Never overwrite an existing completion or run the same request twice.
    lock = folder / ".started"
    with lock.open("x"):
        pass
    encoder = None
    archive = None
    completed = 0
    total = len(request["frame_ids"])

    def progress(state, **extra):
        export_folder(root, export_id)
        atomic_write_json(
            folder / "progress.json",
            {
                "state": state,
                "completed_frames": completed,
                "total_frames": total,
                **extra,
            },
        )

    def check_sources():
        _check_sources(root, request["sources"])
        if (
            _signature_hash(_inspection_signature(root, settings["result_id"]))
            != request["source_identity"]["inspection_signature_sha256"]
        ):
            raise ValueError("Visualization inspection sources changed")
        if _sha256_file(request_path) != request_sha256:
            raise ValueError("Visualization request changed while rendering")

    try:
        progress("validating")
        check_sources()
        index = _index(root, settings["result_id"], refresh=True)
        rows = matching_inspection_frames(
            index, settings["scene_id"], settings["filter"], settings["object_id"]
        )
        if [row["im_id"] for row in rows] != request["frame_ids"] or not rows:
            raise ValueError("Visualization frame selection changed")
        models = {}
        hashes = {}
        for source in request["sources"]:
            path = root / source["path"]
            if path.suffix != ".png":
                hashes[source["path"]] = _sha256_file(path)
        if any(
            value
            for key, value in settings["geometry"].items()
            if not key.endswith("Opacity")
        ):
            for model in index["objects"]:
                models[model["obj_id"]] = load_geometry(model["model_path"])
        width, height = request["image_size"]
        if settings["format"] == "zip":
            archive = zipfile.ZipFile(
                partial, "x", compression=zipfile.ZIP_STORED, allowZip64=True
            )
        else:
            status = mp4_status()
            if not status["available"]:
                raise ValueError(status["reason"])
            # Inherit the LocalJobRunner process group: cancel stops both the
            # Python worker and its encoder. stderr goes to the bounded job log.
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
                    f"{width}x{height}",
                    "-r",
                    str(settings["fps"]),
                    "-i",
                    "pipe:0",
                    "-an",
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:black",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-preset",
                    "medium",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-n",
                    str(partial),
                ],
                stdin=subprocess.PIPE,
            )
        scene = index["scenes_by_id"][settings["scene_id"]]
        scene_relative = scene["scene_path"].relative_to(root)
        source_lookup = {source["path"]: source for source in request["sources"]}

        def read(relative, *, depth=False):
            key = relative.as_posix()
            source = source_lookup.get(key)
            if source is None:
                raise ValueError("Frame source was not bound at submission")
            _check_sources(root, [source])
            pixels, digest = _read_image(root, key, request["image_size"], depth=depth)
            _check_sources(root, [source])
            hashes[key] = digest
            return pixels

        for im_id in request["frame_ids"]:
            frame = inspection_frame_from_index(
                index,
                scene_id=settings["scene_id"],
                im_id=im_id,
                max_hypotheses=settings["max_hypotheses"],
            )
            pixels = read(
                scene_relative / settings["background"] / f"{im_id:06d}.png",
                depth=settings["background"] == "depth",
            )
            for kind, directory in (("full", "mask"), ("visible", "mask_visib")):
                if settings["masks"][kind]:
                    for gt in frame["ground_truth"]:
                        mask = read(
                            scene_relative
                            / directory
                            / f"{im_id:06d}_{gt['gt_id']:06d}.png"
                        )
                        # Masks exported by BOP are binary luminance images.
                        blend(
                            pixels,
                            mask[:, :, 0] > 0,
                            MASK_COLORS[kind],
                            settings["masks"][kind + "Opacity"],
                        )
            if models:
                render_geometry(pixels, frame, models, settings["geometry"])
            if archive:
                buffer = io.BytesIO()
                Image.fromarray(pixels).save(buffer, format="PNG")
                archive.writestr(
                    f"{settings['scene_id']:06d}/{im_id:06d}.png", buffer.getvalue()
                )
            else:
                assert encoder is not None and encoder.stdin is not None
                encoder.stdin.write(pixels.tobytes())
            completed += 1
            progress("rendering")
        progress("finalizing")
        if encoder:
            encoder.stdin.close()
            if encoder.wait(timeout=120) != 0:
                raise ValueError("FFmpeg encoding failed; see the Jobs log")
        check_sources()
        manifest = {
            key: value
            for key, value in request.items()
            if key not in {"sources", "run_root"}
        }
        manifest.update(
            {
                "source_hashes": hashes,
                "frame_count": completed,
                "output_size": [width + width % 2, height + height % 2]
                if encoder
                else [width, height],
                "duration_seconds": total / settings["fps"] if encoder else None,
                "encoding": {
                    "codec": "libx264",
                    "crf": 18,
                    "preset": "medium",
                    "pixel_format": "yuv420p",
                    "faststart": True,
                    "audio": False,
                }
                if encoder
                else None,
            }
        )
        if archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.close()
        atomic_write_json(folder / "manifest.json", manifest)
        record = {
            "filename": output.name,
            "size_bytes": partial.stat().st_size,
            "sha256": _sha256_file(partial),
            "request_sha256": request_sha256,
            "manifest_sha256": _sha256_file(folder / "manifest.json"),
        }
        export_folder(root, export_id)
        os.replace(partial, output)
        atomic_write_json(folder / "output.json", record)
        progress("completed")
        return record
    except BaseException as exc:
        progress(
            "canceled" if isinstance(exc, ExportCanceled) else "failed",
            error=str(exc) or "Export interrupted",
        )
        raise
    finally:
        if encoder and encoder.poll() is None:
            encoder.kill()
            encoder.wait(timeout=10)
        if encoder and encoder.stdin and not encoder.stdin.closed:
            try:
                encoder.stdin.close()
            except BrokenPipeError:
                pass
        if archive:
            archive.close()
        partial.unlink(missing_ok=True)
