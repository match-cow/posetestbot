from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import zipfile

import cv2
import numpy as np
from PIL import Image
import pytest

from posetestbot.bop import inspection_exports as exports
from posetestbot.bop.inspection import (
    inspection_depth_png,
    inspection_frame,
    inspection_model_path,
)
from posetestbot.bop.inspection_render import (
    GEOMETRY_DEFAULTS,
    POSE_COLORS,
    geometry_color,
    load_geometry,
    render_geometry,
)
from posetestbot.jobs.runner import LocalJobRunner
from posetestbot.web.app import create_app
from tests.test_bop_inspection import _import_result, _write_json, make_inspection_run


def settings(result, **updates):
    return {
        "result_id": result["result_id"],
        "scene_id": 1,
        "format": "zip",
        "geometry": {
            key: False
            for key, value in GEOMETRY_DEFAULTS.items()
            if type(value) is bool
        },
        **updates,
    }


def run_export(run, value):
    request = exports.create_export_request(run, value)
    folder = exports.export_folder(run, request["export_id"])
    exports.run_export_request(folder / "request.json")
    return request, exports.export_download_path(run, request["export_id"])


def expand_frames(run, frame_ids, size=(8, 8)):
    scene = run / "bop/test/000001"
    for name in ("scene_camera.json", "scene_gt.json", "scene_gt_info.json"):
        path = scene / name
        first = json.loads(path.read_text())["0"]
        _write_json(path, {str(i): first for i in frame_ids})
    for i in frame_ids:
        for kind in ("rgb", "depth", "mask", "mask_visib"):
            pixels = (
                np.full((size[1], size[0], 3), i % 256, np.uint8)
                if kind == "rgb"
                else np.full(
                    (size[1], size[0]),
                    500 if kind == "depth" else 255,
                    np.uint16 if kind == "depth" else np.uint8,
                )
            )
            filename = (
                f"{i:06d}.png" if kind in {"rgb", "depth"} else f"{i:06d}_000000.png"
            )
            assert cv2.imwrite(str(scene / kind / filename), pixels)
    path = run / "bop/bop_export_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["exports"][0].update(rgb_count=len(frame_ids), depth_count=len(frame_ids))
    manifest["validation"].update(
        frame_count=len(frame_ids),
        annotation_count=len(frame_ids),
        target_count=len(frame_ids),
    )
    _write_json(path, manifest)
    _write_json(
        run / "bop/test_targets_bop19.json",
        [{"scene_id": 1, "im_id": i, "obj_id": 1, "inst_count": 1} for i in frame_ids],
    )


def test_zip_all_pages_numeric_order_original_pixels_and_manifest(tmp_path):
    run = make_inspection_run(tmp_path)
    ids = list(reversed(range(83))) + [100, 90]
    expand_frames(run, ids)
    result = _import_result(run, tmp_path)
    value = settings(result)
    request = exports.create_export_request(run, value)
    value["geometry"]["estimateSurface"] = True
    folder = exports.export_folder(run, request["export_id"])
    exports.run_export_request(folder / "request.json")
    path = exports.export_download_path(run, request["export_id"])
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [f"000001/{i:06d}.png" for i in sorted(ids)] + [
            "manifest.json"
        ]
        pixels = np.array(Image.open(io.BytesIO(archive.read("000001/000090.png"))))
        assert pixels.shape == (8, 8, 3) and np.all(pixels == 90)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["settings"]["geometry"]["estimateSurface"] is False
        assert manifest["frame_count"] == len(ids)
        assert manifest["source_identity"]["result_sha256"] == result["sha256"]
        assert len(manifest["source_hashes"]) > len(ids)
    # A retained completed visualization remains downloadable after source drift.
    source = run / "bop/test/000001/rgb/000000.png"
    source.write_bytes(source.read_bytes() + b"changed later")
    assert exports.export_download_path(run, request["export_id"]) == path
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        exports.export_download_path(run, request["export_id"])


@pytest.mark.parametrize("kind", ["rgb", "depth", "full", "visible"])
def test_background_and_mask_pixels(tmp_path, kind):
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    mask = kind in {"full", "visible"}
    _, path = run_export(
        run,
        settings(
            result,
            background="depth" if kind == "depth" else "rgb",
            masks={kind: True, kind + "Opacity": 0.5} if mask else {},
        ),
    )
    with zipfile.ZipFile(path) as archive:
        pixels = np.array(Image.open(io.BytesIO(archive.read("000001/000000.png"))))
    if kind == "depth":
        expected = np.array(
            Image.open(
                io.BytesIO(
                    inspection_depth_png(
                        run, result_id=result["result_id"], scene_id=1, im_id=0
                    )
                )
            )
        )
        np.testing.assert_array_equal(pixels, expected)
    elif mask:
        np.testing.assert_array_equal(
            pixels[3, 3], np.rint(np.array(exports.MASK_COLORS[kind]) * 0.5)
        )
        assert not pixels[0, 0].any()
    else:
        assert not pixels.any()


@pytest.mark.parametrize("kind", ["estimate", "gt"])
@pytest.mark.parametrize("layer", ["Surface", "Wireframe", "Axes", "Box"])
def test_geometry_layers_opacity_projection_and_xray_order(tmp_path, kind, layer):
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    frame = inspection_frame(run, result_id=result["result_id"], scene_id=1, im_id=0)
    frame["camera"]["cam_K"] = [300, 15, 40, 0, 300, 40, 0, 0, 1]
    model = load_geometry(
        inspection_model_path(run, result_id=result["result_id"], obj_id=1)
    )
    layers = {
        **GEOMETRY_DEFAULTS,
        **settings(result)["geometry"],
        kind + layer: True,
        kind + "Opacity": 0.2,
    }
    pixels = render_geometry(np.zeros((80, 80, 3), np.uint8), frame, {1: model}, layers)
    assert np.count_nonzero(pixels) > 10
    # Background stays untouched; projected origin lies at (40, 40).
    assert not pixels[:10].any()
    if layer == "Surface":
        bright = render_geometry(
            np.zeros_like(pixels), frame, {1: model}, {**layers, kind + "Opacity": 1}
        )
        assert bright.sum() > pixels.sum()
    if layer == "Wireframe":
        floor = render_geometry(
            np.zeros_like(pixels), frame, {1: model}, {**layers, kind + "Opacity": 0.75}
        )
        np.testing.assert_array_equal(pixels, floor)
    if layer == "Surface":
        both = render_geometry(
            np.zeros_like(pixels),
            frame,
            {1: model},
            {**layers, "estimateSurface": True, "gtSurface": True, "gtOpacity": 1},
        )
        np.testing.assert_array_equal(both[40, 40], geometry_color(POSE_COLORS["gt"]))
    # Behind the near plane: clip all primitives, never mirror them into view.
    for pose in frame["estimates"] + frame["ground_truth"]:
        pose["translation_mm"] = [0, 0, -100]
    behind = render_geometry(np.zeros_like(pixels), frame, {1: model}, layers)
    assert not behind.any()
    # Crossing the near plane, with off-screen vertices, must clip safely.
    for pose in frame["estimates"] + frame["ground_truth"]:
        pose["translation_mm"] = [0, 0, 10]
    assert (
        render_geometry(np.zeros_like(pixels), frame, {1: model}, layers).shape
        == pixels.shape
    )


def test_filters_missing_estimates_and_hypothesis_limit(tmp_path):
    run = make_inspection_run(tmp_path, two_frames=True)
    result = _import_result(run, tmp_path)
    request, path = run_export(
        run,
        settings(
            result,
            filter="missing_estimate",
            object_id=1,
            geometry={"estimateSurface": True, "gtWireframe": False},
        ),
    )
    assert request["frame_ids"] == [1]
    with zipfile.ZipFile(path) as archive:
        assert not np.array(
            Image.open(io.BytesIO(archive.read("000001/000001.png")))
        ).any()
    with pytest.raises(ValueError, match="No frames"):
        exports.create_export_request(run, settings(result, filter="tracking"))
    frame = inspection_frame(
        run, result_id=result["result_id"], scene_id=1, im_id=0, max_hypotheses=1
    )
    assert len(frame["estimates"]) == 1


@pytest.mark.parametrize(
    "update",
    [
        {"fps": 0},
        {"fps": 121},
        {"fps": 1.5},
        {"fps": True},
        {"fps": "30"},
        {"scene_id": True},
        {"max_hypotheses": 51},
        {"geometry": {"gtOpacity": float("nan")}},
        {"geometry": {"estimateSurface": 1}},
        {"background": "../rgb"},
        {"format": "avi"},
        {"page": 2},
    ],
)
def test_invalid_settings(update):
    with pytest.raises(ValueError):
        exports.normalize_settings(
            settings({"result_id": "result-aaaaaaaaaaaa"}, **update)
        )


def test_unavailable_encoder_leaves_zip_available(tmp_path, monkeypatch):
    monkeypatch.setattr(exports.shutil, "which", lambda _: None)
    assert exports.mp4_status()["available"] is False
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    with pytest.raises(ValueError, match="FFmpeg"):
        exports.create_export_request(run, settings(result, format="mp4"))
    assert run_export(run, settings(result))[1].is_file()


def test_submission_revalidates_image_inventory_after_browser_cache(tmp_path):
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    inspection_frame(run, result_id=result["result_id"], scene_id=1, im_id=0)
    image = run / "bop/test/000001/rgb/000000.png"
    assert cv2.imwrite(str(image), np.zeros((9, 9, 3), np.uint8))
    with pytest.raises(ValueError, match="valid annotation|compatible"):
        exports.create_export_request(run, settings(result))


def test_ffmpeg_without_libx264_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        exports.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=" V..... mpeg4 other encoder"
        ),
    )
    status = exports._encoder_status("/nonexistent-test-ffmpeg", 123)
    assert status["available"] is False
    assert "libx264" in status["reason"]


@pytest.mark.parametrize(
    "change",
    [
        "media",
        "result",
        "model",
        "symlink",
        "during_render",
        "cancel",
        "encoder_failure",
    ],
)
def test_failures_never_publish_partial_downloads(tmp_path, monkeypatch, change):
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    if change == "encoder_failure":
        monkeypatch.setattr(exports, "mp4_status", lambda: {"available": True})
        monkeypatch.setattr(exports.shutil, "which", lambda _: "/bin/false")
    request = exports.create_export_request(
        run, settings(result, format="mp4" if change == "encoder_failure" else "zip")
    )
    folder = exports.export_folder(run, request["export_id"])
    image = run / "bop/test/000001/rgb/000000.png"
    if change in {"media", "model", "result"}:
        path = (
            image
            if change == "media"
            else run / "bop/models_eval/obj_000001.ply"
            if change == "model"
            else run
            / "processed/bop_evaluation/results"
            / result["result_id"]
            / result["filename"]
        )
        stat = path.stat()
        path.chmod(0o600)
        content = path.read_bytes()
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif change == "symlink":
        image.unlink()
        image.symlink_to(tmp_path / "outside.png")
    elif change in {"cancel", "during_render"}:
        original = exports._read_image

        def read(*args, **kwargs):
            if change == "cancel":
                raise exports.ExportCanceled("canceled")
            value = original(*args, **kwargs)
            image.write_bytes(image.read_bytes() + b"x")
            return value

        monkeypatch.setattr(exports, "_read_image", read)
    with pytest.raises((ValueError, BrokenPipeError, exports.ExportCanceled)):
        exports.run_export_request(folder / "request.json")
    assert json.loads((folder / "progress.json").read_text())["state"] == (
        "canceled" if change == "cancel" else "failed"
    )
    assert not (folder / "output.json").exists()
    assert not list(folder.glob(".partial.*"))


def test_export_path_containment(tmp_path):
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    parent = run / "processed/bop_evaluation/visualizations"
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        exports.create_export_request(run, settings(result))
    with pytest.raises(KeyError):
        exports.export_folder(run, "../result-aaaaaaaaaaaa")


def test_runner_cancellation_stops_encoder_and_keeps_partial_unavailable(
    tmp_path, monkeypatch
):
    from posetestbot.web.paths import APP_ROOT

    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    encoder = bin_dir / "ffmpeg"
    marker = tmp_path / "encoder.pid"
    encoder.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "if '-encoders' in sys.argv:\n"
        "    print(' V..... libx264 test encoder')\n"
        "else:\n"
        f"    Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
    )
    encoder.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    request = exports.create_export_request(run, settings(result, format="mp4"))
    folder = exports.export_folder(run, request["export_id"])
    runner = LocalJobRunner(tmp_path / "cancel-jobs")
    job = runner.submit(
        name="bop_inspection_export",
        command=[
            "uv",
            "run",
            "python",
            "scripts/run_bop_inspection_export.py",
            "--request",
            str(folder / "request.json"),
        ],
        cwd=APP_ROOT,
        resources=["cpu", "disk_io"],
        scope_kind="run",
        run_root=run,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), runner.get(job.id).tail
        pid = int(marker.read_text())
        runner.cancel(job.id)
        canceled = runner.wait(job.id, timeout=10)
        assert canceled.status == "canceled"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not (folder / "output.json").exists()
        assert not list(folder.glob(".partial.*"))
        assert (
            exports.read_export_artifact(run, request["export_id"], "progress.json")[
                "state"
            ]
            == "canceled"
        )
    finally:
        runner.shutdown()


def test_real_mp4_count_fps_padding_duration_and_no_audio(tmp_path):
    if not exports.mp4_status()["available"] or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg with libx264 and ffprobe required")
    run = make_inspection_run(tmp_path)
    expand_frames(run, [0, 1, 2], size=(9, 7))
    result = _import_result(run, tmp_path)
    request, path = run_export(run, settings(result, format="mp4", fps=12))
    probe = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]
        )
    )
    assert len(probe["streams"]) == 1
    stream = probe["streams"][0]
    assert stream["codec_name"] == "h264"
    assert (stream["width"], stream["height"]) == (10, 8)
    assert stream["pix_fmt"] == "yuv420p"
    assert stream["nb_frames"] == "3" and stream["avg_frame_rate"] == "12/1"
    assert float(stream["duration"]) == pytest.approx(0.25)
    data = path.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")
    manifest = exports.read_export_artifact(run, request["export_id"], "manifest.json")
    assert manifest["image_size"] == [9, 7] and manifest["output_size"] == [10, 8]


def test_http_queued_export_status_download_and_job_identity(tmp_path, monkeypatch):
    from posetestbot.web.routes import bop_inspection as routes

    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", str(tmp_path))
    runner = LocalJobRunner(tmp_path / "jobs")
    monkeypatch.setattr(routes, "job_runner", runner)
    client = create_app().test_client()
    response = client.post(
        "/bop/inspection/exports", json={"run_root": str(run), **settings(result)}
    )
    assert response.status_code == 202, response.json
    export_id = response.json["export_id"]
    job_id = response.json["job_id"]
    url = f"/bop/inspection/exports/{export_id}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job.status in {"succeeded", "failed", "canceled"}:
            break
        time.sleep(0.05)
    assert job.status == "succeeded", job.tail
    assert job.resources == ["cpu", "disk_io"]
    status = client.get(url, query_string={"run_root": str(run)})
    assert status.status_code == 200 and status.json["download_available"]
    assert status.json["settings"]["fps"] == 30
    assert "sources" not in status.json
    download = client.get(status.json["download_url"])
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith("attachment;")
    assert zipfile.is_zipfile(io.BytesIO(download.data))
    folder = exports.export_folder(run, export_id)
    (folder / "progress.json").write_text('{"state": "rendering"}')
    assert client.get(status.json["download_url"]).status_code == 409
    assert (
        client.post(
            "/bop/inspection/exports", json={"run_root": "/etc", **settings(result)}
        ).status_code
        == 400
    )
