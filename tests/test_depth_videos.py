"""Depth videos use completed evidence, isolate camera series, and publish atomically."""

import json
import shutil
import sqlite3
import subprocess

import pytest

from posetestbot.bop import depth_evaluation as depth, depth_videos as videos
from posetestbot.io.atomic import atomic_write_json
from posetestbot.web.app import create_app
from posetestbot.web.routes import depth_evaluation as routes
from tests.test_bop_evaluation_api import FakeRunner
from tests.test_depth_evaluation import evaluate, make_depth_run


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = make_depth_run(tmp_path)
    request, _ = evaluate(root)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", str(tmp_path))
    runner = FakeRunner()
    monkeypatch.setattr(routes, "job_runner", runner)
    return root, request["id"], create_app().test_client(), runner


def submission(root, record_id, **patch):
    return {
        "schema_version": "depth_video_submission.v1",
        "run_root": str(root),
        "source_kind": "evaluations",
        "source_id": record_id,
        "scene_id": 1,
        "view": "overview",
        "fps": 10,
        "width": 960,
        **patch,
    }


def extend_retained_frames(root, record_id):
    """A published three-frame fixture with deliberately unsorted image IDs."""
    complete = depth.completed(root, "evaluations", record_id).directory
    frame = json.loads((complete / "frames/000001_000000.json").read_text())
    with sqlite3.connect(complete / "frames.sqlite3") as db:
        for ordinal, im_id in enumerate((9, 2), 1):
            key = f"000001_{im_id:06d}"
            metadata = {**frame, "key": key, "im_id": im_id}
            for media in ("rgb", "measured", "reference", "error", "missing"):
                shutil.copyfile(
                    complete / f"frames/000001_000000-{media}.png",
                    complete / f"frames/{key}-{media}.png",
                )
            atomic_write_json(complete / f"frames/{key}.json", metadata)
            db.execute(
                "INSERT INTO frames VALUES (?, ?)", (ordinal, json.dumps(metadata))
            )
    report = json.loads((complete / "report.json").read_text())
    report["frame_count"] = 3
    atomic_write_json(complete / "report.json", report)
    manifest = json.loads((complete / "manifest.json").read_text())
    manifest["files"] = depth._manifest_files(complete)
    atomic_write_json(complete / "manifest.json", manifest)
    return complete


def require_encoder():
    if not videos.mp4_status()["available"] or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/libx264 and ffprobe are required for real-video regression")


def test_video_api_real_mp4_order_stride_range_and_integrity(source):
    require_encoder()
    root, record_id, client, runner = source
    extend_retained_frames(root, record_id)
    root = root.rename(root.with_name("video source #?%"))
    query = {"run_root": str(root)}
    choices = client.get(f"/depth/evaluations/{record_id}/videos", query_string=query)
    assert choices.status_code == 200 and choices.json["series"][0]["frame_count"] == 3
    response = client.post("/depth/videos", json=submission(root, record_id, stride=2))
    assert response.status_code == 202
    video_id = response.json["id"]
    job = runner.submissions[-1]
    assert job["resources"] == ["cpu", "disk_io"] and job["scope_kind"] == "run"
    assert job["name"] == "depth_video" and job["command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_depth_video.py",
    ]
    path = videos.folder(root, video_id)
    frozen = json.loads((path / "request.json").read_text())
    assert frozen["frame_keys"] == ["000001_000000", "000001_000009"]
    assert (
        frozen["scales"]
        == json.loads(
            depth.artifact(root, "evaluations", record_id, "report.json").read_text()
        )["scales"]
    )
    assert (
        client.get(f"/depth/videos/{video_id}/media", query_string=query).status_code
        == 404
    )
    result = videos.run_video(path / "request.json")
    assert result["video_available"] and result["frame_count"] == 2
    assert result["duration_seconds"] == 0.2
    moved = root.rename(root.with_name("moved video #?%"))
    query = {"run_root": str(moved)}
    status = client.get(f"/depth/videos/{video_id}", query_string=query)
    assert (
        status.status_code == 200 and status.json["job_id"] == response.json["job_id"]
    )
    assert status.json["video_available"]
    with pytest.raises(ValueError, match="run-owned record"):
        videos.run_video(videos.folder(moved, video_id) / "request.json")
    media = videos.artifact(moved, video_id)
    probe = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(media)]
        )
    )
    stream = probe["streams"][0]
    assert stream["codec_name"] == "h264" and stream["pix_fmt"] == "yuv420p"
    assert stream["nb_frames"] == "2" and stream["r_frame_rate"] == "10/1"
    assert [stream["width"], stream["height"]] == result["output_size"]
    poster = client.get(f"/depth/videos/{video_id}/poster", query_string=query)
    assert poster.status_code == 200 and poster.mimetype == "image/png"
    response = client.get(
        f"/depth/videos/{video_id}/media",
        query_string=query,
        headers={"Range": "bytes=0-99"},
    )
    assert response.status_code == 206 and len(response.data) == 100
    response = client.get(f"/depth/videos/{video_id}/download", query_string=query)
    assert response.status_code == 200 and response.mimetype == "video/mp4"
    assert f"{video_id}.mp4" in response.headers["Content-Disposition"]
    manifest = client.get(f"/depth/videos/{video_id}/manifest", query_string=query)
    assert manifest.status_code == 200 and manifest.json["submission"]["stride"] == 2
    assert "capture timestamps are not reproduced" in manifest.json["timing"]
    (moved / "bop/test/000001/depth/000000.png").unlink()
    assert (
        client.get(f"/depth/videos/{video_id}/media", query_string=query).status_code
        == 200
    )
    media.write_bytes(media.read_bytes() + b"tampered")
    assert (
        client.get(f"/depth/videos/{video_id}/download", query_string=query).status_code
        == 400
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"fps": 0},
        {"fps": 61},
        {"fps": True},
        {"stride": 0},
        {"stride": 1.5},
        {"stride": 121},
        {"width": 9999},
        {"view": "arbitrary"},
        {"source_id": "../../etc"},
        {"source_kind": "videos"},
        {"scene_id": 99},
        {"input_id": "0"},
        {"output_path": "/tmp/escape.mp4"},
        {"run_root": "/etc"},
    ],
)
def test_invalid_video_settings_never_queue(source, monkeypatch, patch):
    root, record_id, client, runner = source
    monkeypatch.setattr(
        videos, "mp4_status", lambda: {"available": True, "reason": None}
    )
    response = client.post("/depth/videos", json=submission(root, record_id, **patch))
    assert response.status_code in (400, 404) and not runner.submissions


@pytest.mark.parametrize("mutation", ["before", "during", "missing_manifest_hash"])
def test_changed_evidence_cannot_publish_video(source, monkeypatch, mutation):
    require_encoder()
    root, record_id, _, _ = source
    request = videos.create_video(root, submission(root, record_id))
    path = videos.folder(root, request["id"])
    media = depth.artifact(
        root, "evaluations", record_id, "frames/000001_000000-rgb.png"
    )
    if mutation == "during":
        compose = videos._compose

        def mutate(*args):
            image = compose(*args)
            media.write_bytes(media.read_bytes() + b"changed")
            return image

        monkeypatch.setattr(videos, "_compose", mutate)
    elif mutation == "before":
        media.write_bytes(media.read_bytes() + b"changed")
    else:
        request["source_manifest_sha256"] = None
        atomic_write_json(path / "request.json", request)
    with pytest.raises(ValueError, match="(hash|manifest) changed"):
        videos.run_video(path / "request.json")
    assert not (path / "complete").exists()
    assert videos.record_status(root, request["id"])["state"] == "failed"
    with pytest.raises(FileNotFoundError):
        videos.artifact(root, request["id"])


def test_video_cancellation_stops_encoder_and_keeps_partial_private(
    source, monkeypatch
):
    require_encoder()
    root, record_id, _, _ = source
    extend_retained_frames(root, record_id)
    request = videos.create_video(root, submission(root, record_id))
    compose = videos._compose

    def cancel(*args):
        if args[-1] == 1:
            raise depth.DepthCanceled("test cancellation after one frame")
        return compose(*args)

    monkeypatch.setattr(videos, "_compose", cancel)
    path = videos.folder(root, request["id"])
    with pytest.raises(depth.DepthCanceled):
        videos.run_video(path / "request.json")
    status = videos.record_status(root, request["id"])
    assert status["state"] == "canceled" and status["progress"]["completed_frames"] == 1
    assert not status["video_available"] and (path / ".work").is_dir()
    with pytest.raises(FileNotFoundError):
        videos.artifact(root, request["id"])


def test_comparison_videos_and_frame_filter_keep_inputs_separate(source, tmp_path):
    require_encoder()
    root, record_id, client, _ = source
    other = make_depth_run(tmp_path, name="polished")
    other_record, _ = evaluate(other, label="polished")
    comparison = depth.create_comparison(
        root,
        {
            "schema_version": "depth_comparison_submission.v1",
            "run_root": str(root),
            "evaluations": [
                {"run_root": str(root), "evaluation_id": record_id},
                {"run_root": str(other), "evaluation_id": other_record["id"]},
            ],
        },
    )
    depth.run_comparison(
        depth.folder(root, "comparisons", comparison["id"]) / "request.json"
    )
    choices = videos.series(root, "comparisons", comparison["id"])
    assert [(s["input_id"], s["finish_label"], s["frame_count"]) for s in choices] == [
        ("0", "matte", 1),
        ("1", "polished", 1),
    ]
    response = client.get(
        f"/depth/comparisons/{comparison['id']}/frames",
        query_string={"run_root": str(root), "scene_id": 1, "input_id": "1"},
    )
    assert response.status_code == 200 and response.json["total"] == 1
    assert response.json["frames"][0]["finish_label"] == "polished"
    request = videos.create_video(
        root,
        submission(
            root,
            comparison["id"],
            source_kind="comparisons",
            input_id="1",
            view="missing",
        ),
    )
    assert (
        request["frame_keys"] == ["1_000001_000000"]
        and request["finish_label"] == "polished"
    )
    videos.run_video(videos.folder(root, request["id"]) / "request.json")
    assert videos.record_status(root, request["id"])["frame_count"] == 1


def test_encoder_missing_and_symlink_output_fail_closed(source, monkeypatch, tmp_path):
    root, record_id, client, runner = source
    monkeypatch.setattr(
        videos,
        "mp4_status",
        lambda: {"available": False, "reason": "FFmpeg/libx264 unavailable"},
    )
    response = client.post("/depth/videos", json=submission(root, record_id))
    assert (
        response.status_code == 400
        and "FFmpeg" in response.json["output"]
        and not runner.submissions
    )
    choices = client.get(
        f"/depth/evaluations/{record_id}/videos", query_string={"run_root": str(root)}
    )
    assert choices.status_code == 200 and not choices.json["encoder"]["available"]
    monkeypatch.setattr(
        videos, "mp4_status", lambda: {"available": True, "reason": None}
    )
    base = root / depth.BASE / "videos"
    base.symlink_to(tmp_path / "outside", target_is_directory=True)
    response = client.post("/depth/videos", json=submission(root, record_id))
    assert response.status_code == 400 and "symbolic" in response.json["output"]
    assert not (tmp_path / "outside").exists()
