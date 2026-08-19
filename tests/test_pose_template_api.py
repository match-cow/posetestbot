from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from posetestbot.jobs.runner import ResourceBusyError
from posetestbot.web.app import create_app
from posetestbot.web.routes import pose_templates as routes


@dataclass
class FakeJob:
    id: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": "pose_template",
            "command": [],
            "cwd": None,
            "status": "queued",
            "created_at": "2026-07-20T00:00:00+00:00",
            "started_at": None,
            "ended_at": None,
            "returncode": None,
            "message": None,
            "tail": [],
            "resources": [],
            "parameters": {},
            "log_path": "log.txt",
            "visibility": "operator",
        }


def test_workpieces_owns_catalogue_routes_and_pose_templates_keeps_library(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("POSETESTBOT_WORKING_DATA_ROOT", tmp_path.as_posix())
    client = create_app().test_client()

    assert client.get("/workpieces/catalog").status_code == 200
    assert client.get("/pose-templates/library").status_code == 200
    assert client.get("/pose-templates/catalog").status_code == 404
    assert client.post("/pose-templates/catalog/upload").status_code == 404


def test_pose_template_delete_reports_pending_after_cleanup_queue_conflict(
    monkeypatch,
) -> None:
    template_uuid = "22222222-2222-4222-8222-222222222222"
    pending = {
        "schema_version": "pose_template_library_delete.v1",
        "template_uuid": template_uuid,
        "status": "deleted_cleanup_pending",
        "asset_cleanup": {
            "status": "pending",
            "path": f"{template_uuid}.assets",
            "last_error": None,
        },
    }

    class BusyRunner:
        def submit(self, **_kwargs):
            raise ResourceBusyError("Requested resources are busy: disk_io")

    monkeypatch.setattr(routes, "job_runner", BusyRunner())
    monkeypatch.setattr(
        routes,
        "delete_template_bundle",
        lambda _template_uuid, cleanup_assets: pending,
    )
    monkeypatch.setattr(
        routes,
        "record_template_cleanup_submission_failure",
        lambda _template_uuid, error: {
            **pending,
            "asset_cleanup": {
                **pending["asset_cleanup"],
                "last_error": str(error),
            },
        },
    )

    response = (
        create_app()
        .test_client()
        .delete(
            f"/pose-templates/library/{template_uuid}",
            json={"confirm": True},
        )
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "deleted_cleanup_pending"
    assert "resources are busy" in response.get_json()["cleanup_job_error"]


def test_pose_template_request_pruning_does_not_follow_symlink_races(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_root = tmp_path / "requests"
    kind_root = request_root / "preview"
    kind_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("untouched")

    direct_symlink = kind_root / "direct-symlink"
    direct_symlink.symlink_to(outside, target_is_directory=True)
    raced = kind_root / "raced"
    raced.mkdir()
    (raced / "request.json").write_text("{}\n")
    ordinary = kind_root / "ordinary"
    ordinary.mkdir()
    (ordinary / "request.json").write_text("{}\n")
    stale_time = time.time() - routes.REQUEST_RETENTION_SECONDS - 60
    os.utime(raced, (stale_time, stale_time))
    os.utime(ordinary, (stale_time, stale_time))

    class NoJobsRunner:
        def list(self, *, include_services: bool = True) -> list[FakeJob]:
            return []

    monkeypatch.setattr(routes, "job_runner", NoJobsRunner())
    original_rmtree = routes.shutil.rmtree
    displaced = kind_root / "raced-original"

    def replace_with_symlink(
        path: str | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        candidate = Path(path)
        if candidate.name == raced.name:
            raced.rename(displaced)
            raced.symlink_to(outside, target_is_directory=True)
        original_rmtree(candidate, dir_fd=dir_fd)

    monkeypatch.setattr(routes.shutil, "rmtree", replace_with_symlink)

    routes._prune_stale_requests("preview", request_root=request_root)

    assert direct_symlink.is_symlink()
    assert raced.is_symlink()
    assert displaced.is_dir()
    assert not ordinary.exists()
    assert marker.read_text() == "untouched"


def test_pose_template_request_pruning_rejects_symlinked_kind_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_root = tmp_path / "requests"
    request_root.mkdir()
    outside = tmp_path / "outside"
    stale = outside / "stale-request"
    stale.mkdir(parents=True)
    marker = stale / "keep.txt"
    marker.write_text("untouched")
    stale_time = time.time() - routes.REQUEST_RETENTION_SECONDS - 60
    os.utime(stale, (stale_time, stale_time))
    (request_root / "preview").symlink_to(outside, target_is_directory=True)

    class NoJobsRunner:
        def list(self, *, include_services: bool = True) -> list[FakeJob]:
            return []

    monkeypatch.setattr(routes, "job_runner", NoJobsRunner())

    routes._prune_stale_requests("preview", request_root=request_root)

    assert stale.is_dir()
    assert marker.read_text() == "untouched"


def test_pose_template_request_pruning_stays_anchored_during_root_replacement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request_root = tmp_path / "requests"
    kind_root = request_root / "preview"
    stale = kind_root / "ordinary"
    stale.mkdir(parents=True)
    (stale / "request.json").write_text("{}\n")
    outside = tmp_path / "outside"
    outside_stale = outside / "ordinary"
    outside_stale.mkdir(parents=True)
    marker = outside_stale / "keep.txt"
    marker.write_text("untouched")
    stale_time = time.time() - routes.REQUEST_RETENTION_SECONDS - 60
    os.utime(stale, (stale_time, stale_time))
    os.utime(outside_stale, (stale_time, stale_time))

    class NoJobsRunner:
        def list(self, *, include_services: bool = True) -> list[FakeJob]:
            return []

    monkeypatch.setattr(routes, "job_runner", NoJobsRunner())
    original_scandir = routes.os.scandir
    displaced = request_root / "preview-original"
    replaced = False

    def replace_root(directory: int):
        nonlocal replaced
        if not replaced:
            kind_root.rename(displaced)
            kind_root.symlink_to(outside, target_is_directory=True)
            replaced = True
        return original_scandir(directory)

    monkeypatch.setattr(routes.os, "scandir", replace_root)

    routes._prune_stale_requests("preview", request_root=request_root)

    assert kind_root.is_symlink()
    assert not (displaced / "ordinary").exists()
    assert outside_stale.is_dir()
    assert marker.read_text() == "untouched"
