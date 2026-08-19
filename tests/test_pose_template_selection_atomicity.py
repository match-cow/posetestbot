from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh

from posetestbot.calibration import attempts as calibration_attempts_module
from posetestbot.calibration import target_library as target_library_module
from posetestbot.pipeline import run_config as run_config_module
from posetestbot.pipeline.run_config import (
    create_run_config,
    run_config_lock,
    write_run_config,
)
from posetestbot.pose_templates import selection as selection_module
from posetestbot.pose_templates.catalog import import_catalog_object
from posetestbot.pose_templates.library import (
    generate_template_bundle,
    set_template_archive_state,
)
from posetestbot.pose_templates.orientations import analyze_catalog_orientations
from posetestbot.pose_templates.selection import (
    load_pose_template_selection,
    select_pose_template,
)


def _configuration(
    catalog_uuid: str, *, orientation_id: str, name: str, x_mm: float
) -> dict[str, Any]:
    return {
        "display_name": name,
        "description": "selection transaction fixture",
        "instances": [
            {
                "instance_uuid": "11111111-1111-4111-8111-111111111111",
                "catalog_uuid": catalog_uuid,
                "orientation_id": orientation_id,
                "pose": {
                    "x_mm": x_mm,
                    "y_mm": 40,
                    "rotation_deg": 0,
                },
            }
        ],
    }


def _selection_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    cad = tmp_path / "box.stl"
    cad.write_bytes(trimesh.creation.box(extents=(20, 10, 8)).export(file_type="stl"))
    catalog = tmp_path / "catalog"
    workpiece = import_catalog_object(
        name="Box",
        cad_path=cad,
        catalog_root=catalog,
    )
    orientation_id = analyze_catalog_orientations(
        workpiece["catalog_uuid"], catalog_root=catalog
    )["orientations"][0]["orientation_id"]
    library = tmp_path / "library"
    first = generate_template_bundle(
        _configuration(
            workpiece["catalog_uuid"],
            orientation_id=orientation_id,
            name="First",
            x_mm=40,
        ),
        catalog_root=catalog,
        library_root=library,
    )
    second = generate_template_bundle(
        _configuration(
            workpiece["catalog_uuid"],
            orientation_id=orientation_id,
            name="Second",
            x_mm=80,
        ),
        catalog_root=catalog,
        library_root=library,
    )
    run = tmp_path / "run"
    run.mkdir()
    write_run_config(
        run,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run,
            dataset_mode="pose_template",
        ),
    )
    select_pose_template(
        run,
        first["template_uuid"],
        placement={"matrix": np.eye(4).tolist()},
        confirmed=True,
        operator="pytest",
        library_root=library,
    )
    return run, library, first, second


def _artifact_bytes(path: Path) -> Any:
    if path.is_dir():
        return [
            (item.relative_to(path).as_posix(), item.read_bytes())
            for item in sorted(path.rglob("*"))
            if item.is_file()
        ]
    return path.read_bytes()


def _active_state(run: Path) -> dict[Path, Any]:
    paths = (
        run / "processed" / "pose_template_selection",
        run / "pose_template_selection.json",
        run / "run_config.json",
    )
    return {path: _artifact_bytes(path) for path in paths}


def test_replacement_prevalidation_and_promotion_faults_leave_old_selection_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, library, first, second = _selection_fixture(tmp_path)
    before = _active_state(run)

    with monkeypatch.context() as patch:

        def fail_validation(_value: Any) -> None:
            raise ValueError("injected validation failure")

        patch.setattr(
            run_config_module,
            "validate_run_config",
            fail_validation,
        )
        with pytest.raises(ValueError, match="injected validation failure"):
            select_pose_template(
                run,
                second["template_uuid"],
                placement={"matrix": np.eye(4).tolist()},
                confirmed=True,
                operator="pytest",
                library_root=library,
            )

    assert _active_state(run) == before
    assert load_pose_template_selection(run)["template_uuid"] == first["template_uuid"]

    original_replace = selection_module.os.replace
    failure = {"injected": False}

    def fail_config_promotion(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == run / "run_config.json" and not failure["injected"]:
            failure["injected"] = True
            raise OSError("injected promotion failure")
        original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(selection_module.os, "replace", fail_config_promotion)
        with pytest.raises(OSError, match="injected promotion failure"):
            select_pose_template(
                run,
                second["template_uuid"],
                placement={"matrix": np.eye(4).tolist()},
                confirmed=True,
                operator="pytest",
                library_root=library,
            )

    assert failure["injected"] is True
    assert _active_state(run) == before
    assert load_pose_template_selection(run)["template_uuid"] == first["template_uuid"]
    assert not list(run.rglob("*.tmp"))
    assert not list(run.rglob("*.bak"))


def test_reader_waits_while_replacement_snapshot_is_between_atomic_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, library, _first, second = _selection_fixture(tmp_path)
    snapshot = run / "processed" / "pose_template_selection"
    original_replace = selection_module.os.replace
    snapshot_gap = threading.Event()
    allow_promotion = threading.Event()

    def pause_snapshot_promotion(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if (
            Path(destination) == snapshot
            and source_path.name.endswith(".tmp")
            and not snapshot_gap.is_set()
        ):
            snapshot_gap.set()
            if not allow_promotion.wait(timeout=5):
                raise TimeoutError("test did not release snapshot promotion")
        original_replace(source, destination)

    monkeypatch.setattr(selection_module.os, "replace", pause_snapshot_promotion)
    writer_result: list[dict[str, Any]] = []
    writer_error: list[BaseException] = []

    def replace_selection() -> None:
        try:
            writer_result.append(
                select_pose_template(
                    run,
                    second["template_uuid"],
                    placement={"matrix": np.eye(4).tolist()},
                    confirmed=True,
                    operator="pytest",
                    library_root=library,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            writer_error.append(exc)

    writer = threading.Thread(target=replace_selection)
    writer.start()
    assert snapshot_gap.wait(timeout=5)
    assert not snapshot.exists()

    reader_result: list[dict[str, Any]] = []
    reader_error: list[BaseException] = []
    reader_started = threading.Event()

    def read_selection() -> None:
        reader_started.set()
        try:
            reader_result.append(load_pose_template_selection(run))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            reader_error.append(exc)

    reader = threading.Thread(target=read_selection)
    reader.start()
    assert reader_started.wait(timeout=1)
    reader.join(timeout=0.1)
    assert reader.is_alive()

    allow_promotion.set()
    writer.join(timeout=5)
    reader.join(timeout=5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert writer_error == []
    assert reader_error == []
    assert writer_result[0]["template_uuid"] == second["template_uuid"]
    assert reader_result[0]["template_uuid"] == second["template_uuid"]


def test_archive_waits_until_active_template_snapshot_copy_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, library, first, _second = _selection_fixture(tmp_path)
    # Use a fresh run so this operation must create a new snapshot.
    fresh_run = tmp_path / "fresh-run"
    fresh_run.mkdir()
    write_run_config(
        fresh_run,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=fresh_run,
            dataset_mode="pose_template",
        ),
    )
    original_copytree = selection_module.shutil.copytree
    copy_started = threading.Event()
    allow_copy = threading.Event()

    def paused_copytree(source: str | Path, destination: str | Path, *args, **kwargs):
        copy_started.set()
        if not allow_copy.wait(timeout=5):
            raise TimeoutError("test did not release bundle snapshot copy")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(selection_module.shutil, "copytree", paused_copytree)
    selection_errors: list[BaseException] = []
    archive_errors: list[BaseException] = []

    def select() -> None:
        try:
            select_pose_template(
                fresh_run,
                first["template_uuid"],
                placement={"matrix": np.eye(4).tolist()},
                confirmed=True,
                operator="pytest",
                library_root=library,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            selection_errors.append(exc)

    def archive() -> None:
        try:
            set_template_archive_state(
                first["template_uuid"], state="archived", library_root=library
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            archive_errors.append(exc)

    selection_thread = threading.Thread(target=select)
    selection_thread.start()
    assert copy_started.wait(timeout=5)
    archive_thread = threading.Thread(target=archive)
    archive_thread.start()
    archive_thread.join(timeout=0.1)
    assert archive_thread.is_alive()

    allow_copy.set()
    selection_thread.join(timeout=5)
    archive_thread.join(timeout=5)

    assert selection_errors == []
    assert archive_errors == []
    assert (
        load_pose_template_selection(fresh_run)["template_uuid"]
        == first["template_uuid"]
    )


def test_reader_recovers_prepared_selection_transaction_after_process_loss(
    tmp_path: Path,
) -> None:
    run, _library, first, _second = _selection_fixture(tmp_path)
    before = _active_state(run)
    targets = [
        run / "processed" / "pose_template_selection",
        run / "pose_template_selection.json",
        run / "run_config.json",
    ]
    entries = []
    for target in targets:
        backup = target.with_name(f".{target.name}.deadbeef.bak")
        staged = target.with_name(f".{target.name}.deadbeef.tmp")
        target.rename(backup)
        if target.suffix:
            target.write_text('{"partial":true}')
            staged.write_text('{"staged":true}')
        else:
            target.mkdir()
            (target / "partial.txt").write_text("partial")
            staged.mkdir()
        entries.append(
            {
                "staged": staged.relative_to(run).as_posix(),
                "target": target.relative_to(run).as_posix(),
                "backup": backup.relative_to(run).as_posix(),
                "had_target": True,
            }
        )
    journal = run / selection_module.SELECTION_TRANSACTION
    journal.write_text(
        json.dumps(
            {
                "schema_version": (
                    selection_module.SELECTION_TRANSACTION_SCHEMA_VERSION
                ),
                "phase": "prepared",
                "entries": entries,
            }
        )
    )

    recovered = load_pose_template_selection(run)

    assert recovered["template_uuid"] == first["template_uuid"]
    assert _active_state(run) == before
    assert not journal.exists()
    assert not list(run.rglob("*.bak"))
    assert not list(run.rglob("*.tmp"))


def test_selection_serializes_with_normal_run_config_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, library, _first, second = _selection_fixture(tmp_path)
    writer_entered = threading.Event()
    allow_writer = threading.Event()
    selection_copied = threading.Event()
    original_atomic_write = run_config_module.atomic_write_json
    original_copytree = selection_module.shutil.copytree

    def paused_atomic_write(path: str | Path, value: Any, **kwargs: Any) -> Path:
        if Path(path) == run / "run_config.json" and not writer_entered.is_set():
            writer_entered.set()
            if not allow_writer.wait(timeout=5):
                raise TimeoutError("test did not release run-config writer")
        return original_atomic_write(path, value, **kwargs)

    def observed_copytree(source: str | Path, destination: str | Path, *args, **kwargs):
        selection_copied.set()
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(run_config_module, "atomic_write_json", paused_atomic_write)
    monkeypatch.setattr(selection_module.shutil, "copytree", observed_copytree)
    writer_errors: list[BaseException] = []
    selection_errors: list[BaseException] = []

    def write_config() -> None:
        try:
            write_run_config(
                run,
                create_run_config(
                    capture_intent="dataset",
                    bop_annotation_mode="none",
                    run_root=run,
                    dataset_mode="pose_template",
                    fps=17,
                ),
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            writer_errors.append(exc)

    def select() -> None:
        try:
            select_pose_template(
                run,
                second["template_uuid"],
                placement={"matrix": np.eye(4).tolist()},
                confirmed=True,
                operator="pytest",
                library_root=library,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            selection_errors.append(exc)

    writer = threading.Thread(target=write_config)
    writer.start()
    assert writer_entered.wait(timeout=5)
    selector = threading.Thread(target=select)
    selector.start()
    selector.join(timeout=0.1)
    assert selector.is_alive()
    assert not selection_copied.is_set()

    allow_writer.set()
    writer.join(timeout=5)
    selector.join(timeout=5)

    assert not writer.is_alive()
    assert not selector.is_alive()
    assert writer_errors == []
    assert selection_errors == []
    config = json.loads((run / "run_config.json").read_text())
    assert config["capture"]["fps"] == 17
    assert config["pose_template"]["template_uuid"] == second["template_uuid"]


def test_recovery_rejects_symlink_ancestor_before_touching_outside_run(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched")
    (run / "processed").symlink_to(outside, target_is_directory=True)
    transaction_id = "a" * 32
    entries = [
        {
            "staged": (f"processed/.pose_template_selection.{transaction_id}.tmp"),
            "target": "processed/pose_template_selection",
            "backup": (f"processed/.pose_template_selection.{transaction_id}.bak"),
            "had_target": False,
        },
        {
            "staged": f".pose_template_selection.json.{transaction_id}.tmp",
            "target": "pose_template_selection.json",
            "backup": f".pose_template_selection.json.{transaction_id}.bak",
            "had_target": False,
        },
    ]
    (run / selection_module.SELECTION_TRANSACTION).write_text(
        json.dumps(
            {
                "schema_version": (
                    selection_module.SELECTION_TRANSACTION_SCHEMA_VERSION
                ),
                "phase": "prepared",
                "entries": entries,
            }
        )
    )

    with pytest.raises(ValueError, match="must not contain symlink ancestors"):
        load_pose_template_selection(run)

    assert sentinel.read_text() == "untouched"
    assert (run / selection_module.SELECTION_TRANSACTION).is_file()


def test_reader_cleans_only_exact_unjournaled_selection_staging_names(
    tmp_path: Path,
) -> None:
    run, _library, first, _second = _selection_fixture(tmp_path)
    transaction_id = "b" * 32
    snapshot_orphan = (
        run / "processed" / f".pose_template_selection.{transaction_id}.tmp"
    )
    snapshot_orphan.mkdir()
    (snapshot_orphan / "partial.txt").write_text("partial")
    selection_orphan = run / f".pose_template_selection.json.{transaction_id}.tmp"
    selection_orphan.write_text('{"partial": true}')
    snapshot_decoy = run / "processed" / ".pose_template_selection.not-a-uuid.tmp"
    snapshot_decoy.mkdir()
    selection_decoy = run / ".pose_template_selection.json.bbbbbbbb.tmp"
    selection_decoy.write_text("keep")

    selected = load_pose_template_selection(run)

    assert selected["template_uuid"] == first["template_uuid"]
    assert not snapshot_orphan.exists()
    assert not selection_orphan.exists()
    assert snapshot_decoy.is_dir()
    assert selection_decoy.read_text() == "keep"


def test_other_run_config_promoters_use_the_shared_per_run_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    cases = (
        (
            target_library_module,
            "_select_target_bundle_locked",
            lambda: target_library_module.select_target_bundle(
                run_root=run,
                target_id="11111111-1111-4111-8111-111111111111",
                placement_mode="unknown",
                mounting_frame="robot_flange",
                library_root=tmp_path / "targets",
            ),
        ),
        (
            calibration_attempts_module,
            "_promote_calibration_attempt_locked",
            lambda: calibration_attempts_module.promote_calibration_attempt(
                run, "a" * 32
            ),
        ),
    )

    for module, helper_name, invoke in cases:
        helper_reached = threading.Event()
        lock_held = threading.Event()
        release_lock = threading.Event()
        errors: list[BaseException] = []

        def fake_helper(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            helper_reached.set()
            return {"status": "stub"}

        monkeypatch.setattr(module, helper_name, fake_helper)

        def hold_lock() -> None:
            with run_config_lock(run):
                lock_held.set()
                if not release_lock.wait(timeout=5):
                    errors.append(TimeoutError("test did not release run-config lock"))

        def call_promoter() -> None:
            try:
                invoke()
            except BaseException as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert lock_held.wait(timeout=5)
        caller = threading.Thread(target=call_promoter)
        caller.start()
        reached_while_locked = helper_reached.wait(timeout=0.1)
        release_lock.set()
        holder.join(timeout=5)
        caller.join(timeout=5)

        assert reached_while_locked is False
        assert helper_reached.is_set()
        assert not holder.is_alive()
        assert not caller.is_alive()
        assert errors == []
