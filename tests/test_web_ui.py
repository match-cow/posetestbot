from __future__ import annotations

import os
import re
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from posetestbot.pipeline.run_config import create_run_config, write_run_config
from posetestbot.web.app import create_app
from posetestbot.web.routes import ui as web_ui
from posetestbot.web.security import DEFAULT_RUN_ROOTS


def _write_valid_run(
    path: Path,
    *,
    sequence: str,
    plan_only: bool = True,
    run_name: str | None = None,
) -> None:
    config = create_run_config(
        run_root=path,
        run_name=run_name,
        sequence_id=sequence,
        plan_only=plan_only,
    )
    write_run_config(path, config)


def test_ui_run_discovery_is_contained_safe_and_newest_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    older = allowed / "older"
    newer = allowed / "newer"
    invalid = allowed / "invalid"
    jobs = allowed / "jobs"
    object_catalog = allowed / "object_catalog"
    outside = tmp_path / "outside"
    _write_valid_run(older, sequence="sync_aruco")
    _write_valid_run(
        newer,
        sequence="real_full_capture_validation",
        run_name="Newest calibration recording",
    )
    invalid.mkdir()
    (invalid / "run_config.json").write_text("{not json\n")
    jobs.mkdir()
    (jobs / "job.json").write_text("{}\n")
    object_catalog.mkdir()
    (object_catalog / "object_catalog.json").write_text("{}\n")
    outside.mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", allowed.as_posix())
    monkeypatch.setenv(
        "POSETESTBOT_WEB_DEFAULT_RUN_ROOT",
        (allowed / "web_run").as_posix(),
    )

    payload = create_app().test_client().get("/ui/runs").get_json()

    assert payload["schema_version"] == "web_run_index.v1"
    paths = [item["path"] for item in payload["runs"]]
    assert (allowed / "escape").as_posix() not in paths
    assert outside.as_posix() not in paths
    assert jobs.as_posix() not in paths
    assert object_catalog.as_posix() not in paths
    assert paths.index(newer.as_posix()) < paths.index(older.as_posix())
    records = {item["name"]: item for item in payload["runs"]}
    assert records["newer"]["sequence"] == "real_full_capture_validation"
    assert records["newer"]["run_name"] == "Newest calibration recording"
    assert records["older"]["run_name"] == "older"
    assert records["newer"]["plan_only"] is True
    assert records["newer"]["config_valid"] is True
    assert records["invalid"]["config_valid"] is False
    assert records["invalid"]["run_name"] is None
    assert records["invalid"]["config_error"]


def test_ui_bootstrap_and_run_query_reject_outside_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", allowed.as_posix())
    monkeypatch.setenv(
        "POSETESTBOT_WEB_DEFAULT_RUN_ROOT",
        (allowed / "console-default").as_posix(),
    )
    client = create_app().test_client()

    bootstrap = client.get("/ui/bootstrap").get_json()
    outside = client.get("/ui/overview", query_string={"run_root": tmp_path / "outside"})

    assert bootstrap["schema_version"] == "web_bootstrap.v1"
    assert bootstrap["default_run_root"] == (allowed / "console-default").as_posix()
    assert bootstrap["allowed_run_roots"][: len(DEFAULT_RUN_ROOTS)] == [
        root.resolve().as_posix() for root in DEFAULT_RUN_ROOTS
    ]
    assert allowed.as_posix() in bootstrap["allowed_run_roots"]
    assert outside.status_code == 400
    assert "allowed root" in outside.get_json()["output"]


def test_ui_storage_reports_selected_run_filesystem_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    allowed = tmp_path / "allowed"
    run_root = allowed / "new-run"
    run_root.mkdir(parents=True)
    total_bytes = 4 * 1024**4
    free_bytes = 400 * 1024**3
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", allowed.as_posix())
    monkeypatch.setattr(
        web_ui.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=total_bytes,
            used=total_bytes - free_bytes,
            free=free_bytes,
        ),
    )

    response = create_app().test_client().get(
        "/ui/storage",
        query_string={"run_root": run_root.as_posix()},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["schema_version"] == "run_storage.v1"
    assert payload["run_root"] == run_root.as_posix()
    assert payload["status"] == "warning"
    assert payload["total_bytes"] == total_bytes
    assert payload["free_bytes"] == free_bytes
    assert payload["free_fraction"] == free_bytes / total_bytes
    assert payload["thresholds"]["warning_free_bytes"] == 500 * 1024**3
    assert payload["thresholds"]["warning_free_bytes_cap"] == 500 * 1024**3
    assert payload["error"] is None


def test_spa_index_and_hashed_assets_are_served() -> None:
    client = create_app().test_client()

    response = client.get("/")
    html = response.get_data(as_text=True)
    asset_paths = re.findall(r'(?:src|href)="(/static/ui/assets/[^"]+)"', html)

    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("no-cache")
    assert '<div id="root"></div>' in html
    assert "cdn.jsdelivr.net" not in html
    assert "bootstrap" not in html.lower()
    assert asset_paths
    for asset_path in asset_paths:
        asset = client.get(asset_path)
        assert asset.status_code == 200
        assert asset.content_length


def test_installed_package_data_contains_self_contained_ui() -> None:
    package = files("posetestbot.web")
    index = package.joinpath("static", "ui", "index.html")

    assert index.is_file()
    html = index.read_text()
    names = re.findall(r"/static/ui/assets/([^\"']+)", html)
    assert names
    assert all(package.joinpath("static", "ui", "assets", name).is_file() for name in names)
    assert "http://" not in html
    assert "https://" not in html
    hri = package.joinpath("static", "cell", "template_HRI_LBR_all_center_v2.svg")
    assert hri.is_file()
    assert 'width="420mm"' in hri.read_text()


def test_run_config_endpoint_refuses_non_plan_capture_sequence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "physical"
    _write_valid_run(
        run_root,
        sequence="real_full_capture_validation",
        plan_only=False,
    )
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    client = create_app().test_client()

    response = client.post(
        "/pipeline/run-config",
        json={"run_root": run_root.as_posix()},
    )

    assert response.status_code == 409
    assert "cannot be queued" in response.get_json()["output"]
    assert "gated physical capture" in response.get_json()["output"]


def test_run_config_endpoint_never_persists_capture_gates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    run_root = tmp_path / "unsafe-options"
    response = create_app().test_client().post(
        "/run-config",
        json={
            "run_root": run_root.as_posix(),
            "sequence_options": {
                "capture_execution": {
                    "allow_cameras": True,
                    "allow_real_robot": True,
                }
            },
        },
    )

    assert response.status_code == 400
    assert "must not be persisted" in response.get_json()["output"]
    assert not (run_root / "run_config.json").exists()
