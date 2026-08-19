from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from posetestbot.web.app import create_app
from posetestbot.web.routes import bop_annotations as route


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
