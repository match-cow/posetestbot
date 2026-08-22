from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import threading
from pathlib import Path

import pytest

from posetestbot.io import atomic


class _InjectedStop(BaseException):
    pass


def _exit_at_directory_transaction_boundary(
    pairs: list[tuple[Path, Path]], boundary: str
) -> None:
    def stop_at(label: str) -> None:
        if label == boundary:
            os._exit(77)

    atomic._transaction_boundary = stop_at
    atomic.replace_directories(pairs)


def test_atomic_write_json_replaces_complete_document(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    path.write_text('{"old":true}\n')

    result = atomic.atomic_write_json(path, {"new": [1, 2, 3]})

    assert result == path
    assert json.loads(path.read_text()) == {"new": [1, 2, 3]}
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_preserves_existing_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    path.write_text("original\n")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(atomic.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        atomic.atomic_write_text(path, "replacement\n")

    assert path.read_text() == "original\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_json_rejects_nonstandard_nan(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        atomic.atomic_write_json(tmp_path / "bad.json", {"value": float("nan")})


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_rename_path_no_replace_moves_one_path_across_real_parents(
    tmp_path: Path,
    kind: str,
) -> None:
    source_parent = tmp_path / "source-parent"
    destination_parent = tmp_path / "destination-parent"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "artifact"
    destination = destination_parent / "published"
    if kind == "file":
        source.write_text("payload")
    else:
        source.mkdir()
        (source / "payload.txt").write_text("payload")
    source_stat = source.stat()

    assert atomic.rename_path_no_replace(source, destination) == destination

    assert not source.exists()
    installed = destination.stat()
    assert (installed.st_dev, installed.st_ino) == (
        source_stat.st_dev,
        source_stat.st_ino,
    )
    payload = destination if kind == "file" else destination / "payload.txt"
    assert payload.read_text() == "payload"
    assert not (source_parent / atomic._DIRECTORY_LOCK_NAME).exists()
    assert not (destination_parent / atomic._DIRECTORY_LOCK_NAME).exists()


def test_rename_path_no_replace_preserves_an_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source")
    destination = tmp_path / "destination.txt"
    destination.write_text("destination")
    destination_stat = destination.stat()

    with pytest.raises(FileExistsError, match="destination already exists"):
        atomic.rename_path_no_replace(source, destination)

    assert source.read_text() == "source"
    assert destination.read_text() == "destination"
    current = destination.stat()
    assert (current.st_dev, current.st_ino) == (
        destination_stat.st_dev,
        destination_stat.st_ino,
    )


def test_rename_path_no_replace_never_clobbers_a_raced_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_parent = tmp_path / "source-parent"
    destination_parent = tmp_path / "destination-parent"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "artifact"
    source.write_text("source")
    destination = destination_parent / "published"
    original = atomic._rename_child_no_replace
    raced_identity: tuple[int, int] | None = None

    def install_destination_then_rename(
        locked_source: atomic._LockedParent,
        source_name: str,
        locked_destination: atomic._LockedParent,
        destination_name: str,
    ) -> None:
        nonlocal raced_identity
        os.mkdir(destination_name, dir_fd=locked_destination.directory_fd)
        created = os.stat(
            destination_name,
            dir_fd=locked_destination.directory_fd,
            follow_symlinks=False,
        )
        raced_identity = (created.st_dev, created.st_ino)
        original(
            locked_source,
            source_name,
            locked_destination,
            destination_name,
        )

    monkeypatch.setattr(
        atomic, "_rename_child_no_replace", install_destination_then_rename
    )

    with pytest.raises(FileExistsError):
        atomic.rename_path_no_replace(source, destination)

    assert source.read_text() == "source"
    current = destination.stat()
    assert (current.st_dev, current.st_ino) == raced_identity


def test_rename_path_no_replace_rejects_source_and_ancestor_symlinks(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_source = real_parent / "source.txt"
    real_source.write_text("source")
    source_link = real_parent / "source-link"
    source_link.symlink_to(real_source)

    with pytest.raises(ValueError, match="source must not be a symlink"):
        atomic.rename_path_no_replace(source_link, real_parent / "published")

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="must not use symlinks"):
        atomic.rename_path_no_replace(
            linked_parent / "source.txt", tmp_path / "published"
        )
    assert real_source.read_text() == "source"


def test_rename_path_no_replace_detects_a_parent_path_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_parent = tmp_path / "source-parent"
    destination_parent = tmp_path / "destination-parent"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "artifact.txt"
    source.write_text("payload")
    destination = destination_parent / "published.txt"
    retained_parent = tmp_path / "retained-source-parent"
    original = atomic._rename_child_no_replace

    def replace_parent_then_rename(
        locked_source: atomic._LockedParent,
        source_name: str,
        locked_destination: atomic._LockedParent,
        destination_name: str,
    ) -> None:
        locked_source.path.rename(retained_parent)
        locked_source.path.mkdir()
        (locked_source.path / "unrelated.txt").write_text("keep")
        original(
            locked_source,
            source_name,
            locked_destination,
            destination_name,
        )

    monkeypatch.setattr(atomic, "_rename_child_no_replace", replace_parent_then_rename)

    with pytest.raises(RuntimeError, match="Rename parent identity changed"):
        atomic.rename_path_no_replace(source, destination)

    assert destination.read_text() == "payload"
    assert (source_parent / "unrelated.txt").read_text() == "keep"
    assert not (retained_parent / "artifact.txt").exists()


def test_rename_path_no_replace_fails_closed_without_linux_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source")
    destination = tmp_path / "published.txt"
    monkeypatch.setattr(atomic, "_RENAMEAT2", None)

    with pytest.raises(
        RuntimeError,
        match=r"requires Linux renameat2\(RENAME_NOREPLACE\)",
    ):
        atomic.rename_path_no_replace(source, destination)

    assert source.read_text() == "source"
    assert not destination.exists()


def test_replace_directories_promotes_complete_batch(tmp_path: Path) -> None:
    destinations = [tmp_path / "one", tmp_path / "two"]
    stagings = [tmp_path / ".one.stage", tmp_path / ".two.stage"]
    for index, destination in enumerate(destinations):
        destination.mkdir()
        (destination / "old.txt").write_text(str(index))
        stagings[index].mkdir()
        (stagings[index] / "new.txt").write_text(str(index))

    assert atomic.replace_directories(zip(stagings, destinations)) == destinations

    for index, destination in enumerate(destinations):
        assert (destination / "new.txt").read_text() == str(index)
        assert not (destination / "old.txt").exists()


def _directory_transaction_pairs(
    tmp_path: Path, *, stage_label: str = "stage"
) -> tuple[list[tuple[Path, Path]], list[Path], list[Path]]:
    parents = [tmp_path / "parent-one", tmp_path / "parent-two"]
    destinations: list[Path] = []
    stagings: list[Path] = []
    for index, parent in enumerate(parents):
        parent.mkdir(exist_ok=True)
        destination = parent / "published"
        destination.mkdir()
        (destination / "generation.txt").write_text(f"old-{index}")
        staging = parent / f".{stage_label}"
        staging.mkdir()
        (staging / "generation.txt").write_text(f"new-{index}")
        destinations.append(destination)
        stagings.append(staging)
    return list(zip(stagings, destinations, strict=True)), stagings, destinations


def _assert_no_directory_transaction_debris(parents: list[Path]) -> None:
    for parent in parents:
        assert not list(parent.glob(".posetestbot-directory-replace.*.json*"))
        assert not list(parent.glob(".*.posetestbot-backup.*"))


_DIRECTORY_CRASH_BOUNDARIES = [
    "ready",
    "journal-prepared:0",
    "journal-prepared:1",
    "journal-applying:0",
    "journal-applying:1",
    "backup:0",
    "backup:1",
    "promotion:0",
    "promotion:1",
    "journal-committed:0",
    "journal-committed:1",
    "backup-removed:0",
    "backup-removed:1",
    "journal-cleaned:0",
    "journal-cleaned:1",
    "journal-temporaries-removed:0",
    "journal-temporaries-removed:1",
    "journal-removed:0",
    "journal-removed:1",
]


@pytest.mark.parametrize("boundary", _DIRECTORY_CRASH_BOUNDARIES)
def test_directory_transaction_recovers_every_process_death_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    pairs, original_stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, boundary),
    )

    process.start()
    process.join(timeout=10)

    assert process.exitcode == 77
    fresh_pairs: list[tuple[Path, Path]] = []
    for index, destination in enumerate(destinations):
        fresh = destination.parent / ".fresh-stage"
        fresh.mkdir()
        (fresh / "generation.txt").write_text(f"fresh-{index}")
        fresh_pairs.append((fresh, destination))

    def stop_after_recovery(label: str) -> None:
        if label == "ready":
            raise _InjectedStop

    monkeypatch.setattr(atomic, "_transaction_boundary", stop_after_recovery)
    with pytest.raises(_InjectedStop):
        atomic.replace_directories(fresh_pairs)

    committed = boundary.startswith(
        (
            "journal-committed:",
            "backup-removed:",
            "journal-cleaned:",
            "journal-temporaries-removed:",
            "journal-removed:",
        )
    )
    for index, (original_staging, destination) in enumerate(
        zip(original_stagings, destinations, strict=True)
    ):
        expected = f"new-{index}" if committed else f"old-{index}"
        assert (destination / "generation.txt").read_text() == expected
        assert original_staging.exists() is not committed
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )

    monkeypatch.setattr(atomic, "_transaction_boundary", lambda _label: None)
    atomic.replace_directories(fresh_pairs)
    for index, destination in enumerate(destinations):
        assert (destination / "generation.txt").read_text() == f"fresh-{index}"
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


@pytest.mark.parametrize(
    "recovery_boundary",
    [
        "rollback-promotion:1",
        "rollback-backup:1",
        "rollback-promotion:0",
        "rollback-backup:0",
        "journal-rolled_back:0",
        "journal-rolled_back:1",
        "journal-temporaries-removed:0",
        "journal-temporaries-removed:1",
        "journal-removed:0",
        "journal-removed:1",
    ],
)
def test_directory_transaction_restarts_interrupted_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_boundary: str,
) -> None:
    pairs, original_stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    initial = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "promotion:1"),
    )
    initial.start()
    initial.join(timeout=10)
    assert initial.exitcode == 77

    retry_pairs: list[tuple[Path, Path]] = []
    for index, destination in enumerate(destinations):
        retry = destination.parent / ".retry-stage"
        retry.mkdir()
        (retry / "generation.txt").write_text(f"retry-{index}")
        retry_pairs.append((retry, destination))
    interrupted_recovery = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(retry_pairs, recovery_boundary),
    )
    interrupted_recovery.start()
    interrupted_recovery.join(timeout=10)
    assert interrupted_recovery.exitcode == 77

    inspect_pairs: list[tuple[Path, Path]] = []
    for index, destination in enumerate(destinations):
        inspect = destination.parent / ".inspect-stage"
        inspect.mkdir()
        (inspect / "generation.txt").write_text(f"inspect-{index}")
        inspect_pairs.append((inspect, destination))

    def stop_after_recovery(label: str) -> None:
        if label == "ready":
            raise _InjectedStop

    monkeypatch.setattr(atomic, "_transaction_boundary", stop_after_recovery)
    with pytest.raises(_InjectedStop):
        atomic.replace_directories(inspect_pairs)

    for index, (staging, destination) in enumerate(
        zip(original_stagings, destinations, strict=True)
    ):
        assert (destination / "generation.txt").read_text() == f"old-{index}"
        assert (staging / "generation.txt").read_text() == f"new-{index}"
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


def test_directory_transaction_removes_phase_temps_before_the_journal(
    tmp_path: Path,
) -> None:
    pairs, original_stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    initial = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "promotion:1"),
    )
    initial.start()
    initial.join(timeout=10)
    assert initial.exitcode == 77
    journal = next(destinations[0].parent.glob(".posetestbot-directory-replace.*.json"))
    stale_temp = journal.with_name(f"{journal.name}.committed.tmp")
    stale_temp.write_text("interrupted phase write")

    retry_pairs: list[tuple[Path, Path]] = []
    for index, destination in enumerate(destinations):
        retry = destination.parent / ".retry-stage"
        retry.mkdir()
        (retry / "generation.txt").write_text(f"retry-{index}")
        retry_pairs.append((retry, destination))
    interrupted_cleanup = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(retry_pairs, "journal-temporaries-removed:0"),
    )
    interrupted_cleanup.start()
    interrupted_cleanup.join(timeout=10)
    assert interrupted_cleanup.exitcode == 77
    assert not stale_temp.exists()
    assert journal.is_file()

    atomic.replace_directories(retry_pairs)

    for index, destination in enumerate(destinations):
        assert (
            original_stagings[index] / "generation.txt"
        ).read_text() == f"new-{index}"
        assert (destination / "generation.txt").read_text() == f"retry-{index}"
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


def test_partial_prepared_replica_does_not_block_a_replica_free_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs, original_stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    initial = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "journal-prepared:0"),
    )
    initial.start()
    initial.join(timeout=10)
    assert initial.exitcode == 77

    independent_stage = destinations[1].parent / ".independent-stage"
    independent_stage.mkdir()
    (independent_stage / "generation.txt").write_text("independent")
    atomic.replace_directory(independent_stage, destinations[1])

    inspect_stage = destinations[0].parent / ".inspect-stage"
    inspect_stage.mkdir()

    def stop_after_recovery(label: str) -> None:
        if label == "ready":
            raise _InjectedStop

    monkeypatch.setattr(atomic, "_transaction_boundary", stop_after_recovery)
    with pytest.raises(_InjectedStop):
        atomic.replace_directory(inspect_stage, destinations[0])

    assert (destinations[0] / "generation.txt").read_text() == "old-0"
    assert (destinations[1] / "generation.txt").read_text() == "independent"
    assert all(staging.is_dir() for staging in original_stagings)
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


def test_partially_removed_cleaned_journal_does_not_block_supersession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs, original_stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    initial = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "journal-removed:0"),
    )
    initial.start()
    initial.join(timeout=10)
    assert initial.exitcode == 77

    independent_stage = destinations[0].parent / ".independent-stage"
    independent_stage.mkdir()
    (independent_stage / "generation.txt").write_text("independent")
    atomic.replace_directory(independent_stage, destinations[0])

    inspect_stage = destinations[1].parent / ".inspect-stage"
    inspect_stage.mkdir()

    def stop_after_recovery(label: str) -> None:
        if label == "ready":
            raise _InjectedStop

    monkeypatch.setattr(atomic, "_transaction_boundary", stop_after_recovery)
    with pytest.raises(_InjectedStop):
        atomic.replace_directory(inspect_stage, destinations[1])

    assert (destinations[0] / "generation.txt").read_text() == "independent"
    assert (destinations[1] / "generation.txt").read_text() == "new-1"
    assert not any(staging.exists() for staging in original_stagings)
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


def test_partially_removed_rollback_journal_does_not_block_supersession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs, original_stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    initial = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "promotion:1"),
    )
    initial.start()
    initial.join(timeout=10)
    assert initial.exitcode == 77

    retry_pairs: list[tuple[Path, Path]] = []
    for destination in destinations:
        retry = destination.parent / ".retry-stage"
        retry.mkdir()
        retry_pairs.append((retry, destination))
    rollback = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(retry_pairs, "journal-removed:0"),
    )
    rollback.start()
    rollback.join(timeout=10)
    assert rollback.exitcode == 77

    independent_stage = destinations[0].parent / ".independent-stage"
    independent_stage.mkdir()
    (independent_stage / "generation.txt").write_text("independent")
    atomic.replace_directory(independent_stage, destinations[0])

    inspect_stage = destinations[1].parent / ".inspect-stage"
    inspect_stage.mkdir()

    def stop_after_recovery(label: str) -> None:
        if label == "ready":
            raise _InjectedStop

    monkeypatch.setattr(atomic, "_transaction_boundary", stop_after_recovery)
    with pytest.raises(_InjectedStop):
        atomic.replace_directory(inspect_stage, destinations[1])

    assert (destinations[0] / "generation.txt").read_text() == "independent"
    assert (destinations[1] / "generation.txt").read_text() == "old-1"
    assert all(staging.is_dir() for staging in original_stagings)
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


def test_directory_transaction_rolls_back_a_normal_promotion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs, stagings, destinations = _directory_transaction_pairs(tmp_path)

    def fail_after_first_promotion(label: str) -> None:
        if label == "promotion:0":
            raise OSError("simulated promotion error")

    monkeypatch.setattr(atomic, "_transaction_boundary", fail_after_first_promotion)

    with pytest.raises(OSError, match="simulated promotion error"):
        atomic.replace_directories(pairs)

    for index, (staging, destination) in enumerate(
        zip(stagings, destinations, strict=True)
    ):
        assert (destination / "generation.txt").read_text() == f"old-{index}"
        assert (staging / "generation.txt").read_text() == f"new-{index}"
    _assert_no_directory_transaction_debris(
        [destination.parent for destination in destinations]
    )


def test_directory_transaction_serializes_overlapping_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "generation.txt").write_text("old")
    first_stage = tmp_path / ".first-stage"
    first_stage.mkdir()
    (first_stage / "generation.txt").write_text("first")
    second_stage = tmp_path / ".second-stage"
    second_stage.mkdir()
    (second_stage / "generation.txt").write_text("second")
    first_paused = threading.Event()
    release_first = threading.Event()
    second_done = threading.Event()
    failures: list[BaseException] = []

    def pause_first(label: str) -> None:
        if threading.current_thread().name == "first" and label == "backup:0":
            first_paused.set()
            assert release_first.wait(timeout=10)

    monkeypatch.setattr(atomic, "_transaction_boundary", pause_first)

    def run(source: Path, *, done: threading.Event | None = None) -> None:
        try:
            atomic.replace_directory(source, destination)
        except BaseException as exc:
            failures.append(exc)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=run, args=(first_stage,), name="first")
    second = threading.Thread(
        target=run,
        args=(second_stage,),
        kwargs={"done": second_done},
        name="second",
    )
    first.start()
    assert first_paused.wait(timeout=10)
    second.start()
    assert not second_done.wait(timeout=0.2)
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not failures
    assert (destination / "generation.txt").read_text() == "second"
    _assert_no_directory_transaction_debris([tmp_path])


def test_no_clobber_rename_serializes_with_a_directory_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "generation.txt").write_text("old")
    staging = tmp_path / ".stage"
    staging.mkdir()
    (staging / "generation.txt").write_text("new")
    file_source = tmp_path / "source.txt"
    file_source.write_text("payload")
    file_destination = tmp_path / "moved.txt"
    transaction_paused = threading.Event()
    release_transaction = threading.Event()
    rename_done = threading.Event()
    failures: list[BaseException] = []

    def pause_transaction(label: str) -> None:
        if label == "backup:0":
            transaction_paused.set()
            assert release_transaction.wait(timeout=10)

    monkeypatch.setattr(atomic, "_transaction_boundary", pause_transaction)

    def run_transaction() -> None:
        try:
            atomic.replace_directory(staging, destination)
        except BaseException as exc:
            failures.append(exc)

    def run_rename() -> None:
        try:
            atomic.rename_path_no_replace(file_source, file_destination)
        except BaseException as exc:
            failures.append(exc)
        finally:
            rename_done.set()

    transaction = threading.Thread(target=run_transaction)
    rename = threading.Thread(target=run_rename)
    transaction.start()
    assert transaction_paused.wait(timeout=10)
    rename.start()
    assert not rename_done.wait(timeout=0.2)
    release_transaction.set()
    transaction.join(timeout=10)
    rename.join(timeout=10)

    assert not transaction.is_alive()
    assert not rename.is_alive()
    assert not failures
    assert (destination / "generation.txt").read_text() == "new"
    assert file_destination.read_text() == "payload"


@pytest.mark.parametrize(
    ("source_factory", "target_factory", "message"),
    [
        (
            lambda root: root / "missing",
            lambda root: root / "published",
            "Staging directory does not exist",
        ),
        (
            lambda root: _make_file(root / "stage"),
            lambda root: root / "published",
            "Staging directory must be a directory",
        ),
        (
            lambda root: _make_directory(root / "stage"),
            lambda root: _make_file(root / "published"),
            "Destination directory must be a directory",
        ),
    ],
)
def test_directory_transaction_rejects_missing_or_non_directory_paths(
    tmp_path: Path,
    source_factory: object,
    target_factory: object,
    message: str,
) -> None:
    source = source_factory(tmp_path)  # type: ignore[operator]
    target = target_factory(tmp_path)  # type: ignore[operator]

    with pytest.raises((FileNotFoundError, RuntimeError), match=message):
        atomic.replace_directory(source, target)


def _make_file(path: Path) -> Path:
    path.write_text("unrelated")
    return path


def _make_directory(path: Path) -> Path:
    path.mkdir()
    return path


def test_directory_transaction_rejects_cross_parent_and_overlapping_paths(
    tmp_path: Path,
) -> None:
    first_parent = tmp_path / "one"
    second_parent = tmp_path / "two"
    first_parent.mkdir()
    second_parent.mkdir()
    source = first_parent / "stage"
    source.mkdir()

    with pytest.raises(ValueError, match="must be a sibling"):
        atomic.replace_directory(source, second_parent / "published")
    with pytest.raises(ValueError, match="distinct and unique"):
        atomic.replace_directories([(source, source)])
    with pytest.raises(ValueError, match="distinct and unique"):
        atomic.replace_directories(
            [(source, first_parent / "published"), (source, first_parent / "other")]
        )

    reserved = first_parent / ".posetestbot-directory-replace.lock"
    with pytest.raises(ValueError, match="reserved transaction namespace"):
        atomic.replace_directory(source, reserved)


def test_directory_transaction_rejects_a_nested_participating_parent(
    tmp_path: Path,
) -> None:
    outer_stage = tmp_path / "outer-stage"
    outer_stage.mkdir()
    outer_target = tmp_path / "outer-target"
    inner_parent = outer_target / "nested"
    inner_parent.mkdir(parents=True)
    inner_stage = inner_parent / "inner-stage"
    inner_stage.mkdir()

    with pytest.raises(
        ValueError, match="must not contain another participating parent"
    ):
        atomic.replace_directories(
            [
                (outer_stage, outer_target),
                (inner_stage, inner_parent / "inner-target"),
            ]
        )

    assert outer_stage.is_dir()
    assert inner_stage.is_dir()


def test_directory_transaction_rejects_source_target_and_ancestor_symlinks(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel.txt").write_text("keep")
    source_target_parent = tmp_path / "direct"
    source_target_parent.mkdir()
    real_source = source_target_parent / "real-source"
    real_source.mkdir()
    source_link = source_target_parent / "source-link"
    source_link.symlink_to(real_source, target_is_directory=True)
    target_link = source_target_parent / "target-link"
    target_link.symlink_to(unrelated, target_is_directory=True)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        atomic.replace_directory(source_link, source_target_parent / "published")
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        atomic.replace_directory(real_source, target_link)

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(source_target_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="must not use symlinks"):
        atomic.replace_directory(
            linked_parent / "real-source", linked_parent / "published"
        )
    assert (unrelated / "sentinel.txt").read_text() == "keep"


def test_directory_transaction_rejects_symlinks_inside_staging(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep")
    (staging / "linked.txt").symlink_to(unrelated)

    with pytest.raises(ValueError, match="must not contain symlinks"):
        atomic.replace_directory(staging, tmp_path / "published")

    assert unrelated.read_text() == "keep"
    assert staging.is_dir()


def test_directory_transaction_never_clobbers_a_raced_empty_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "new.txt").write_text("new")
    destination = tmp_path / "published"
    raced_identity: tuple[int, int] | None = None

    def install_unrelated_destination(label: str) -> None:
        nonlocal raced_identity
        if label == "journal-applying:0":
            destination.mkdir()
            value = destination.stat()
            raced_identity = (value.st_dev, value.st_ino)

    monkeypatch.setattr(atomic, "_transaction_boundary", install_unrelated_destination)

    with pytest.raises(
        RuntimeError, match="failed and durable recovery could not finish"
    ):
        atomic.replace_directory(staging, destination)

    current = destination.stat()
    assert (current.st_dev, current.st_ino) == raced_identity
    assert staging.is_dir()
    assert (staging / "new.txt").read_text() == "new"
    assert list(tmp_path.glob(".posetestbot-directory-replace.*.json"))


def test_directory_transaction_fails_closed_without_no_replace_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "new.txt").write_text("new")
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old")
    monkeypatch.setattr(atomic, "_RENAMEAT2", None)

    with pytest.raises(
        RuntimeError, match=r"requires Linux renameat2\(RENAME_NOREPLACE\)"
    ):
        atomic.replace_directory(staging, destination)

    assert (staging / "new.txt").read_text() == "new"
    assert (destination / "old.txt").read_text() == "old"
    _assert_no_directory_transaction_debris([tmp_path])


def test_directory_recovery_refuses_to_delete_an_unrelated_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairs, _stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "promotion:0"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 77
    backup = next(destinations[0].parent.glob(".*.posetestbot-backup.*"))
    shutil.rmtree(backup)
    backup.mkdir()
    (backup / "unrelated.txt").write_text("keep")
    fresh = destinations[0].parent / ".fresh"
    fresh.mkdir()

    with pytest.raises(RuntimeError, match="unrelated generation"):
        atomic.replace_directory(fresh, destinations[0])

    assert (backup / "unrelated.txt").read_text() == "keep"


def test_recovery_does_not_create_a_lock_in_a_replaced_declared_parent(
    tmp_path: Path,
) -> None:
    pairs, _stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "journal-prepared:0"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 77

    replaced_parent = destinations[1].parent
    retained_parent = tmp_path / ".retained-parent-two"
    replaced_parent.rename(retained_parent)
    replaced_parent.mkdir()
    sentinel = replaced_parent / "unrelated.txt"
    sentinel.write_text("keep")
    fresh = destinations[0].parent / ".fresh"
    fresh.mkdir()

    with pytest.raises(RuntimeError, match="parent identity changed"):
        atomic.replace_directory(fresh, destinations[0])

    assert sentinel.read_text() == "keep"
    assert not (replaced_parent / atomic._DIRECTORY_LOCK_NAME).exists()
    assert (retained_parent / "published" / "generation.txt").read_text() == "old-1"
    assert list(destinations[0].parent.glob(".posetestbot-directory-replace.*.json"))


@pytest.mark.parametrize(
    ("boundary", "installed_generation"),
    [
        ("journal-prepared:0", "old-0"),
        ("journal-cleaned:0", "new-0"),
    ],
)
def test_directory_recovery_retains_prepared_or_cleaned_journal_after_tamper(
    tmp_path: Path,
    boundary: str,
    installed_generation: str,
) -> None:
    pairs, _stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, boundary),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 77
    destination = destinations[0]
    retained = destination.parent / ".retained-generation"
    destination.rename(retained)
    assert (retained / "generation.txt").read_text() == installed_generation
    destination.mkdir()
    (destination / "unrelated.txt").write_text("keep")
    fresh = destination.parent / ".fresh"
    fresh.mkdir()

    with pytest.raises(RuntimeError, match="unrelated generation"):
        atomic.replace_directory(fresh, destination)

    assert (destination / "unrelated.txt").read_text() == "keep"
    assert list(destination.parent.glob(".posetestbot-directory-replace.*.json"))


def test_directory_recovery_retains_rolled_back_journal_after_tamper(
    tmp_path: Path,
) -> None:
    pairs, _stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    initial = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "promotion:1"),
    )
    initial.start()
    initial.join(timeout=10)
    assert initial.exitcode == 77
    retry_pairs: list[tuple[Path, Path]] = []
    for index, destination in enumerate(destinations):
        retry = destination.parent / ".retry"
        retry.mkdir()
        (retry / "generation.txt").write_text(f"retry-{index}")
        retry_pairs.append((retry, destination))
    recovery = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(retry_pairs, "journal-rolled_back:0"),
    )
    recovery.start()
    recovery.join(timeout=10)
    assert recovery.exitcode == 77
    destination = destinations[0]
    destination.rename(destination.parent / ".retained-old")
    destination.mkdir()
    (destination / "unrelated.txt").write_text("keep")
    fresh = destination.parent / ".fresh"
    fresh.mkdir()

    with pytest.raises(RuntimeError, match="unrelated generation"):
        atomic.replace_directory(fresh, destination)

    assert (destination / "unrelated.txt").read_text() == "keep"
    assert list(destination.parent.glob(".posetestbot-directory-replace.*.json"))


def test_directory_recovery_fails_closed_on_corrupt_or_symlinked_journal(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    destination = tmp_path / "published"
    destination.mkdir()
    corrupt = tmp_path / (
        ".posetestbot-directory-replace.0123456789abcdef0123456789abcdef.json"
    )
    corrupt.write_text("not-json")

    with pytest.raises(RuntimeError, match="Invalid directory transaction journal"):
        atomic.replace_directory(staging, destination)
    assert staging.is_dir()
    assert destination.is_dir()

    corrupt.unlink()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep")
    corrupt.symlink_to(unrelated)
    with pytest.raises(OSError):
        atomic.replace_directory(staging, destination)
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("extra-field", "journal shape"),
        ("duplicate-key", "Invalid directory transaction journal"),
    ],
)
def test_directory_recovery_rejects_noncanonical_journal_json(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    destination = tmp_path / "published"
    destination.mkdir()
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=([(staging, destination)], "journal-prepared:0"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 77
    journal = next(tmp_path.glob(".posetestbot-directory-replace.*.json"))
    if mutation == "extra-field":
        value = json.loads(journal.read_text())
        value["ignored"] = True
        journal.write_text(json.dumps(value))
    else:
        journal.write_text(journal.read_text().replace("{", '{"phase":"prepared",', 1))
    fresh = tmp_path / ".fresh"
    fresh.mkdir()

    with pytest.raises(RuntimeError, match=message):
        atomic.replace_directory(fresh, destination)

    assert staging.is_dir()
    assert destination.is_dir()
    assert journal.is_file()


def test_directory_recovery_rejects_malformed_reserved_control_name(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()
    destination = tmp_path / "published"
    destination.mkdir()
    malformed = tmp_path / ".posetestbot-directory-replace.not-a-transaction.json"
    malformed.write_text("unrelated")

    with pytest.raises(RuntimeError, match="Malformed reserved"):
        atomic.replace_directory(staging, destination)

    assert malformed.read_text() == "unrelated"
    assert staging.is_dir()
    assert destination.is_dir()


def test_directory_recovery_rejects_orphan_backup_after_journal_rename(
    tmp_path: Path,
) -> None:
    pairs, _stagings, destinations = _directory_transaction_pairs(tmp_path)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_exit_at_directory_transaction_boundary,
        args=(pairs, "backup:0"),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 77
    for index, destination in enumerate(destinations):
        journal = next(destination.parent.glob(".posetestbot-directory-replace.*.json"))
        journal.rename(destination.parent / f"lost-journal-{index}.json")
    backup = next(destinations[0].parent.glob(".*.posetestbot-backup.*"))
    fresh = destinations[0].parent / ".fresh"
    fresh.mkdir()

    with pytest.raises(RuntimeError, match="Orphan reserved"):
        atomic.replace_directory(fresh, destinations[0])

    assert backup.is_dir()
    assert fresh.is_dir()
