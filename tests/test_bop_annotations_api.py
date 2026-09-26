from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from posetestbot.web.app import create_app
from posetestbot.web.routes import bop_annotations as route
from tests.test_bop_evaluation import make_tiny_evaluation_run


@dataclass
class _Job:
    id: str = "job-ground-truth"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": "bop_annotations",
            "status": "queued",
            "parameters": {},
        }


def _setup(
    *,
    configured_mode: str = "pose_and_masks",
    pose_ready: bool = True,
    full_ready: bool = True,
) -> dict:
    def readiness(ready: bool) -> dict:
        return {
            "ready": ready,
            "blockers": (
                [] if ready else [{"code": "blocked", "message": "Not ready"}]
            ),
            "warnings": [],
        }

    return {
        "schema_version": "bop_annotation_setup.v1",
        "configured_mode": configured_mode,
        "readiness_by_mode": {
            "pose": readiness(pose_ready),
            "pose_and_masks": readiness(full_ready),
        },
    }


def test_api_queues_one_run_scoped_annotation_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setattr(
        route,
        "inspect_annotation_setup",
        lambda _run_root, app_root: _setup(),
    )
    submission: dict = {}

    def submit(**kwargs):
        submission.update(kwargs)
        return _Job()

    monkeypatch.setattr(route.job_runner, "submit", submit)
    client = create_app().test_client()

    response = client.post(
        "/bop/annotations",
        json={"run_root": run.as_posix(), "mode": "pose_and_masks"},
    )

    assert response.status_code == 202
    assert response.get_json()["job_id"] == "job-ground-truth"
    assert submission["name"] == "bop_annotations"
    assert submission["command"] == [
        "uv",
        "run",
        "python",
        "scripts/run_bop_annotations.py",
        run.as_posix(),
        "--mode",
        "pose_and_masks",
    ]
    assert submission["resources"] == ["cpu", "render", "disk_io"]
    assert submission["scope_kind"] == "run"
    assert submission["run_root"] == run
    assert submission["parameters"] == {
        "run_root": run.as_posix(),
        "bop_annotations": True,
        "annotation_mode": "pose_and_masks",
    }


def test_api_applies_readiness_to_the_selected_product_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setattr(
        route,
        "inspect_annotation_setup",
        lambda _run_root, app_root: _setup(pose_ready=True, full_ready=False),
    )
    submissions = []
    monkeypatch.setattr(
        route.job_runner,
        "submit",
        lambda **kwargs: submissions.append(kwargs) or _Job(),
    )
    client = create_app().test_client()

    full = client.post(
        "/bop/annotations",
        json={"run_root": run.as_posix(), "mode": "pose_and_masks"},
    )
    mismatch = client.post(
        "/bop/annotations",
        json={"run_root": run.as_posix(), "mode": "pose"},
    )

    assert full.status_code == 400
    assert "Not ready" in full.get_json()["output"]
    assert mismatch.status_code == 400
    assert "does not match run_config.json" in mismatch.get_json()["output"]
    monkeypatch.setattr(
        route,
        "inspect_annotation_setup",
        lambda _run_root, app_root: _setup(configured_mode="pose"),
    )
    pose = client.post(
        "/bop/annotations",
        json={"run_root": run.as_posix(), "mode": "pose"},
    )
    assert pose.status_code == 202
    assert len(submissions) == 1


def test_api_rejects_unknown_annotation_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()

    response = client.post(
        "/bop/annotations",
        json={"run_root": run.as_posix(), "mode": "mask_crops"},
    )

    assert response.status_code == 400
    assert "mode must be one of" in response.get_json()["output"]


def _bind_scene_gt(
    run: Path, *, mode: str, scene_id: int = 1, sensor_name: str = "fixture"
) -> Path:
    scene_folder = f"test/{scene_id:06d}"
    if scene_id != 1:
        shutil.copytree(run / "bop" / "test" / "000001", run / "bop" / scene_folder)
    gt_path = run / "bop" / scene_folder / "scene_gt.json"
    if scene_id != 1:
        gt_path.write_text(gt_path.read_text().replace("500.0", "700.0"))
    provenance_path = gt_path.with_name("posetestbot_gt_provenance.json")
    provenance_path.write_text(
        json.dumps(
            {
                "schema_version": "posetestbot_gt_provenance.v1",
                "annotation_mode": mode,
                "frame_bindings": [{"im_id": 0}],
            }
        )
    )
    manifest_path = run / "bop" / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    scene = manifest["exports"][0]
    if scene_id != 1:
        scene = json.loads(json.dumps(scene))
        scene["scene_id"] = scene_id
        scene["scene_folder"] = scene_folder
        scene["artifacts"] = {
            key: value.replace("test/000001/", f"{scene_folder}/")
            for key, value in scene["artifacts"].items()
        }
        manifest["exports"].append(scene)
    scene["sensor_name"] = sensor_name
    scene["annotation_mode"] = mode
    scene["artifacts"]["gt_provenance"] = (
        f"{scene_folder}/posetestbot_gt_provenance.json"
    )
    scene["annotation_provenance"] = {
        "schema_version": "posetestbot_gt_provenance.v1",
        "annotation_mode": mode,
        "artifact": f"{scene_folder}/posetestbot_gt_provenance.json",
        "frame_binding_count": 1,
        "scene_gt_sha256": hashlib.sha256(gt_path.read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest))
    return gt_path


def test_combined_gt_download_maps_sensors_and_rejects_tampered_scene(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = make_tiny_evaluation_run(tmp_path)
    first_gt_path = _bind_scene_gt(run, mode="pose", sensor_name="fixture_left")
    second_gt_path = _bind_scene_gt(
        run, mode="pose", scene_id=2, sensor_name="fixture_right"
    )
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()
    url = f"/bop/annotations/ground-truth/download?run_root={run.as_posix()}"

    response = client.get(url)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["schema_version"] == "posetestbot_bop_scene_gt_collection.v1"
    assert payload["split"] == "test"
    assert payload["sensor_by_scene_id"] == {
        "1": "fixture_left",
        "2": "fixture_right",
    }
    assert payload["scene_gt"] == {
        "1": json.loads(first_gt_path.read_text()),
        "2": json.loads(second_gt_path.read_text()),
    }
    assert payload["scene_gt_sha256_by_scene_id"] == {
        "1": hashlib.sha256(first_gt_path.read_bytes()).hexdigest(),
        "2": hashlib.sha256(second_gt_path.read_bytes()).hexdigest(),
    }
    assert response.mimetype == "application/json"
    assert "scene_gt_all_sensors.json" in response.headers["Content-Disposition"]
    assert client.get(url.replace("/download", "/1/download")).status_code == 404

    second_gt_path.write_text(second_gt_path.read_text().replace("700.0", "701.0"))
    mismatch = client.get(url)
    assert mismatch.status_code == 400
    assert "hash does not match" in mismatch.get_json()["output"]


def test_combined_gt_download_rejects_symlinked_scene(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = make_tiny_evaluation_run(tmp_path)
    _bind_scene_gt(run, mode="pose_and_masks")
    gt_path = _bind_scene_gt(run, mode="pose_and_masks", scene_id=2)
    outside = tmp_path / "outside.json"
    outside.write_bytes(gt_path.read_bytes())
    gt_path.unlink()
    gt_path.symlink_to(outside)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()

    response = client.get(
        f"/bop/annotations/ground-truth/download?run_root={run.as_posix()}"
    )

    assert response.status_code == 404
