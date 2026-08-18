from __future__ import annotations

import json

import os

import sys

from pathlib import Path

import pytest

import posetestbot.run_folders as run_folders_module

import posetestbot.web.routes.run_folders as run_folders_routes_module

import posetestbot.web.routes.ui as ui_routes_module

from posetestbot.jobs.runner import JobRecord

from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    load_run_config_for_run_root,
    write_run_config,
)

from posetestbot.run_folders import (
    LOCATION_FILE,
    build_run_folder_inventory,
    delete_run_folder,
    move_run_folder,
    resolve_direct_run_folder,
    run_identity,
    write_run_folder_inventory,
)

from posetestbot.web.app import create_app


def _write_run(path: Path, *, with_object: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    config = create_run_config(
        run_root=path,
        sensors=(
            SensorRunConfig(
                sensor_type="realsense_d435",
                device_id="123",
                display_name="Wrist D435",
                mounting_mode="eye_in_hand",
            ),
        ),
        sequence_id="sync_aruco",
    )
    write_run_config(path, config)
    if with_object:
        (path / "object_instances.json").write_text(
            json.dumps(
                {
                    "schema_version": "object_instances.v1",
                    "template_uuid": "template-1",
                    "instances": [
                        {"name": "Clamp"},
                        {"name": "Clamp"},
                        {"name": "Bracket"},
                    ],
                }
            )
        )


def test_inventory_sizes_and_summarizes_without_following_symlinks(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    run = storage / "run-a"
    outside = tmp_path / "outside"
    _write_run(run, with_object=True)
    outside.mkdir()
    (outside / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    (run / "outside-link").symlink_to(outside, target_is_directory=True)
    raw = run / "realsense_123"
    (raw / "rgb").mkdir(parents=True)
    (raw / "rgb" / "000001.png").write_bytes(b"rgb")
    synchronized = run / "processed" / "synchronized" / "realsense_123"
    synchronized.mkdir(parents=True)
    (synchronized / "sync_report.json").write_text("{}")
    bop = run / "bop"
    bop.mkdir()
    (bop / "bop_export_manifest.json").write_text("{}")

    value = build_run_folder_inventory([storage])

    assert value["schema_version"] == "run_folder_inventory.v1"
    assert len(value["runs"]) == 1
    record = value["runs"][0]
    assert record["path"] == run.as_posix()
    assert record["size_bytes"] < 2 * 1024 * 1024
    assert record["symlink_count"] == 1
    assert record["scan_complete"] is True
    assert record["config"] == {
        "valid": True,
        "error": None,
        "run_name": "run-a",
        "sequence": "sync_aruco",
        "plan_only": True,
    }
    assert record["contents"]["sensor_count"] == 1
    assert record["contents"]["enabled_sensor_count"] == 1
    assert record["contents"]["sensors"][0]["name"] == "Wrist D435"
    assert record["contents"]["object_count"] == 3
    assert record["contents"]["object_names"] == ["Clamp", "Bracket"]
    assert record["contents"]["template_uuid"] == "template-1"
    assert record["contents"]["evidence"]["raw_capture"] is True
    assert record["contents"]["evidence"]["synchronized"] is True
    assert record["contents"]["evidence"]["bop_export"] is True
    assert set(record["breakdown"]) >= {"raw_capture", "processed", "bop", "other"}


def test_general_run_discovery_hides_move_quarantine_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage"
    visible = storage / "visible-run"
    hidden = storage / ".posetestbot_run_move_staging_transaction"
    _write_run(visible)
    _write_run(hidden)
    monkeypatch.setattr(
        ui_routes_module,
        "web_run_roots",
        lambda: (storage.resolve(),),
    )

    records = ui_routes_module.discover_web_runs()

    assert [item["path"] for item in records] == [visible.as_posix()]


def test_move_preserves_path_bound_config_via_alias_and_supports_move_back(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    source = first_root / "run-a"
    _write_run(source)
    first_identity = run_identity(source)

    moved = move_run_folder(
        source,
        second_root,
        expected_identity=first_identity,
        allowed_roots=[first_root, second_root],
    )
    destination = second_root / "run-a"

    assert moved["destination_run_root"] == destination.as_posix()
    assert source.is_symlink()
    assert source.resolve() == destination
    assert destination.is_dir() and not destination.is_symlink()
    assert load_run_config_for_run_root(destination)["run_name"] == "run-a"
    location = json.loads((destination / LOCATION_FILE).read_text())
    assert location["original_path"] == source.as_posix()
    assert location["aliases"] == [source.as_posix()]
    assert len(location["history"]) == 1

    moved_back = move_run_folder(
        destination,
        first_root,
        expected_identity=run_identity(destination),
        allowed_roots=[first_root, second_root],
    )

    assert moved_back["destination_run_root"] == source.as_posix()
    assert source.is_dir() and not source.is_symlink()
    assert destination.is_symlink()
    assert destination.resolve() == source
    assert load_run_config_for_run_root(source)["run_name"] == "run-a"
    location = json.loads((source / LOCATION_FILE).read_text())
    assert location["original_path"] == source.as_posix()
    assert location["aliases"] == [destination.as_posix()]
    assert len(location["history"]) == 2


def test_interrupted_cross_device_copy_rolls_back_before_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    source = first_root / "run-a"
    _write_run(source)
    destination = second_root / source.name

    def interrupt_copy(_source: Path, staging: Path) -> str:
        (staging / "partial.bin").write_bytes(b"partial")
        raise KeyboardInterrupt("simulated runner shutdown")

    with monkeypatch.context() as patch:
        patch.setattr(
            run_folders_module,
            "_same_filesystem_mount",
            lambda _source, _destination: False,
        )
        patch.setattr(
            run_folders_module,
            "_copy_tree_with_content_hash",
            interrupt_copy,
        )
        with pytest.raises(KeyboardInterrupt, match="runner shutdown"):
            move_run_folder(
                source,
                second_root,
                expected_identity=run_identity(source),
                allowed_roots=[first_root, second_root],
            )

    assert not source.exists()
    assert not destination.exists()
    assert next(first_root.glob(".posetestbot_run_folder_transaction_*.json"))

    inventory = write_run_folder_inventory(
        tmp_path / "inventory.json",
        allowed_roots=[first_root, second_root],
    )

    assert source.is_dir() and not source.is_symlink()
    assert not destination.exists()
    assert inventory["maintenance"]["transactions"][0]["action"] == ("rolled_back_move")
    assert not list(first_root.glob(".posetestbot_run_folder_transaction_*.json"))
    assert not list(second_root.glob(".posetestbot_run_move_staging_*"))
    second_inventory = write_run_folder_inventory(
        tmp_path / "inventory-second.json",
        allowed_roots=[first_root, second_root],
    )
    assert second_inventory["maintenance"]["recovered_count"] == 0
    assert second_inventory["maintenance"]["unresolved_count"] == 0


def test_delete_removes_run_and_only_verified_compatibility_aliases(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    source = first_root / "run-a"
    _write_run(source)
    move_run_folder(
        source,
        second_root,
        expected_identity=run_identity(source),
        allowed_roots=[first_root, second_root],
    )
    destination = second_root / "run-a"
    unrelated = first_root / "unrelated"
    unrelated.symlink_to(tmp_path)
    unrecorded = second_root / "unrecorded-run-alias"
    unrecorded.symlink_to(destination, target_is_directory=True)

    result = delete_run_folder(
        destination,
        expected_identity=run_identity(destination),
        allowed_roots=[first_root, second_root],
    )

    assert result["status"] == "deleted"
    assert not destination.exists()
    assert not source.exists() and not source.is_symlink()
    assert unrelated.is_symlink()
    assert unrecorded.is_symlink()


def test_delete_removes_owned_read_only_snapshot_directories(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    source = storage / "run-a"
    _write_run(source)
    snapshot = source / "processed" / "calibration_inputs" / ("a" * 64)
    snapshot.mkdir(parents=True)
    profile = snapshot / "calibration_profiles.json"
    profile.write_text("{}")
    profile.chmod(0o444)
    snapshot.chmod(0o555)

    result = delete_run_folder(
        source,
        expected_identity=run_identity(source),
        allowed_roots=[storage],
    )

    assert result["status"] == "deleted"
    assert not source.exists()
    assert not list(storage.glob(".posetestbot_run_folder_transaction_*.json"))
    assert not list(storage.glob(".posetestbot_run_move_source_*"))


def test_delete_fails_closed_at_nested_filesystem_boundary_before_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    source = storage / "run-a"
    _write_run(source)
    foreign = source / "foreign-mount"
    foreign.mkdir()
    (foreign / "evidence.bin").write_bytes(b"preserve")
    identity = run_identity(source)
    real_lstat = Path.lstat

    def foreign_device(path: Path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        if path.name != foreign.name:
            return metadata
        values = list(metadata)
        values[2] = int(metadata.st_dev) + 1
        return os.stat_result(values)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", foreign_device)
        with pytest.raises(ValueError, match="filesystem boundary"):
            delete_run_folder(
                source,
                expected_identity=identity,
                allowed_roots=[storage],
            )

    assert source.is_dir() and not source.is_symlink()
    assert (foreign / "evidence.bin").read_bytes() == b"preserve"
    assert not list(storage.glob(".posetestbot_run_folder_transaction_*.json"))
    assert not list(storage.glob(".posetestbot_run_move_source_*"))


def test_operations_reject_nested_symlink_collision_and_stale_identity(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    source = first_root / "run-a"
    _write_run(source)
    nested = first_root / "nested" / "run"
    _write_run(nested)
    alias = first_root / "run-link"
    alias.symlink_to(source, target_is_directory=True)

    with pytest.raises(ValueError, match="direct child"):
        resolve_direct_run_folder(nested, allowed_roots=[first_root, second_root])
    with pytest.raises(ValueError, match="symbolic link"):
        resolve_direct_run_folder(alias, allowed_roots=[first_root, second_root])
    with pytest.raises(RuntimeError, match="identity changed"):
        move_run_folder(
            source,
            second_root,
            expected_identity={"device": 0, "inode": 1},
            allowed_roots=[first_root, second_root],
        )

    collision = second_root / source.name
    _write_run(collision)
    with pytest.raises(FileExistsError, match="already exists"):
        move_run_folder(
            source,
            second_root,
            expected_identity=run_identity(source),
            allowed_roots=[first_root, second_root],
        )

    collision.replace(second_root / "retired-collision")
    collision.symlink_to(source, target_is_directory=True)
    with pytest.raises(FileExistsError, match="already exists"):
        move_run_folder(
            source,
            second_root,
            expected_identity=run_identity(source),
            allowed_roots=[first_root, second_root],
        )


class _FakeRunner:
    def __init__(self, job_root: Path):
        self.job_root = job_root
        self.job_root.mkdir(parents=True)
        self.submissions: list[dict] = []
        self.jobs: list[JobRecord] = []

    def list(self, *, include_services: bool = True):
        return list(self.jobs)

    def submit(self, **values):
        self.submissions.append(values)
        job = JobRecord(
            id=f"job-{len(self.submissions)}",
            name=values["name"],
            command=list(values["command"]),
            cwd=Path(values["cwd"]).as_posix(),
            status="queued",
            created_at=f"2026-07-29T00:00:0{len(self.submissions)}+00:00",
            log_path=(self.job_root / f"job-{len(self.submissions)}.log").as_posix(),
            resources=sorted(values.get("resources", [])),
            parameters=dict(values.get("parameters", {})),
            scope_kind=values["scope_kind"],
            run_root=(
                Path(values["run_root"]).resolve().as_posix()
                if values.get("run_root") is not None
                else None
            ),
        )
        self.jobs.append(job)
        return job


def test_api_returns_cached_inventory_and_queues_scoped_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    source = first_root / "run-a"
    _write_run(source)
    monkeypatch.setenv(
        "POSETESTBOT_WEB_RUN_ROOTS",
        f"{first_root}{os.pathsep}{second_root}",
    )
    test_roots = (first_root.resolve(), second_root.resolve())
    monkeypatch.setattr(
        run_folders_routes_module,
        "web_run_roots",
        lambda: test_roots,
    )
    runner = _FakeRunner(tmp_path / "jobs")
    write_run_folder_inventory(
        runner.job_root / "run_folder_inventory.json",
        allowed_roots=test_roots,
    )
    client = create_app(job_runner=runner).test_client()

    inventory = client.get("/ui/run-folders")
    assert inventory.status_code == 200
    payload = inventory.get_json()
    assert payload["schema_version"] == "run_folder_inventory.v1"
    assert payload["inventory_state"] == "ready"
    assert payload["operation_job"] is None
    assert any(item["path"] == source.as_posix() for item in payload["runs"])
    root_records = {item["path"]: item for item in payload["roots"]}
    assert root_records[second_root.as_posix()]["identity"] == run_identity(second_root)

    cache_path = runner.job_root / "run_folder_inventory.json"
    malformed = json.loads(cache_path.read_text())
    malformed["runs"][0]["identity"]["device"] = "not-an-integer"
    cache_path.write_text(json.dumps(malformed))
    assert client.get("/ui/run-folders").get_json()["inventory_state"] == "stale"
    write_run_folder_inventory(cache_path, allowed_roots=test_roots)

    run_folders_module._new_transaction(
        operation="move",
        source=source,
        expected_identity=run_identity(source),
        aliases=[],
        destination_root=second_root,
    )
    assert client.get("/ui/run-folders").get_json()["inventory_state"] == "stale"
    recovered_cache = write_run_folder_inventory(
        cache_path,
        allowed_roots=test_roots,
    )
    assert recovered_cache["maintenance"]["transactions"][0]["action"] == (
        "rolled_back_move"
    )

    refresh = client.post("/ui/run-folders/refresh")
    assert refresh.status_code == 202
    assert runner.submissions[-1]["scope_kind"] == "global"
    assert runner.submissions[-1]["resources"] == [
        "disk_io",
        "run_folder_storage",
    ]
    assert runner.submissions[-1]["parameters"]["cancelable"] is False

    runner.jobs.clear()
    identity = run_identity(source)
    missing_destination_identity = client.post(
        "/ui/run-folders/move",
        json={
            "run_root": source.as_posix(),
            "destination_root": second_root.as_posix(),
            "expected_identity": identity,
        },
    )
    assert missing_destination_identity.status_code == 400
    move = client.post(
        "/ui/run-folders/move",
        json={
            "run_root": source.as_posix(),
            "destination_root": second_root.as_posix(),
            "expected_identity": identity,
            "expected_destination_root_identity": run_identity(second_root),
        },
    )
    assert move.status_code == 202
    moved = move.get_json()
    assert moved["source_run_root"] == source.as_posix()
    assert moved["destination_run_root"] == (second_root / source.name).as_posix()
    assert moved["compatibility_alias"] == source.as_posix()
    submission = runner.submissions[-1]
    assert submission["scope_kind"] == "run"
    assert submission["run_root"] == source
    assert submission["resources"] == ["disk_io", "run_folder_storage"]
    assert "--expected-destination-device" in submission["command"]
    assert "--expected-destination-inode" in submission["command"]
    active_operation = client.get("/ui/run-folders").get_json()["operation_job"]
    assert active_operation["id"] == moved["job_id"]
    assert active_operation["parameters"]["run_folder_operation"] == "move"

    runner.jobs.clear()
    refused = client.delete(
        "/ui/run-folders",
        json={
            "run_root": source.as_posix(),
            "confirm": False,
            "expected_identity": identity,
        },
    )
    assert refused.status_code == 400
    refused_string = client.delete(
        "/ui/run-folders",
        json={
            "run_root": source.as_posix(),
            "confirm": "true",
            "expected_identity": identity,
        },
    )
    assert refused_string.status_code == 400
    deleted = client.delete(
        "/ui/run-folders",
        json={
            "run_root": source.as_posix(),
            "confirm": True,
            "expected_identity": identity,
        },
    )
    assert deleted.status_code == 202
    assert runner.submissions[-1]["name"] == "run_folder_delete"
    assert runner.submissions[-1]["resources"] == ["disk_io", "run_folder_storage"]
    assert "--confirm-delete" in runner.submissions[-1]["command"]


def test_api_rejects_active_run_job_and_symlink_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    source = first_root / "run-a"
    _write_run(source)
    alias = first_root / "alias"
    alias.symlink_to(source, target_is_directory=True)
    monkeypatch.setenv(
        "POSETESTBOT_WEB_RUN_ROOTS",
        f"{first_root}{os.pathsep}{second_root}",
    )
    monkeypatch.setattr(
        run_folders_routes_module,
        "web_run_roots",
        lambda: (first_root.resolve(), second_root.resolve()),
    )
    runner = _FakeRunner(tmp_path / "jobs")
    write_run_folder_inventory(
        runner.job_root / "run_folder_inventory.json",
        allowed_roots=[first_root, second_root],
    )
    active = runner.submit(
        name="active",
        command=[sys.executable, "-c", "pass"],
        cwd=tmp_path,
        resources=[],
        scope_kind="run",
        run_root=source,
        parameters={},
    )
    active.status = "running"
    client = create_app(job_runner=runner).test_client()
    identity = run_identity(source)

    blocked = client.delete(
        "/ui/run-folders",
        json={
            "run_root": source.as_posix(),
            "confirm": True,
            "expected_identity": identity,
        },
    )
    assert blocked.status_code == 409
    assert "active background work" in blocked.get_json()["output"]

    runner.jobs.clear()
    symlinked = client.delete(
        "/ui/run-folders",
        json={
            "run_root": alias.as_posix(),
            "confirm": True,
            "expected_identity": identity,
        },
    )
    assert symlinked.status_code == 400
    assert "symbolic link" in symlinked.get_json()["output"]

    bridge = tmp_path / "bridge"
    bridge.symlink_to(first_root, target_is_directory=True)
    nested_through_link = client.delete(
        "/ui/run-folders",
        json={
            "run_root": (bridge / source.name).as_posix(),
            "confirm": True,
            "expected_identity": identity,
        },
    )
    assert nested_through_link.status_code == 400
    assert "direct child" in nested_through_link.get_json()["output"]
