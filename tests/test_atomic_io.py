from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from posetestbot.io import atomic


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


def test_directory_exchange_keeps_the_live_path_and_removes_old_generation(
    tmp_path, monkeypatch
):
    live, staging = tmp_path / "bop", tmp_path / ".bop.staging"
    live.mkdir()
    staging.mkdir()
    (live / "manifest").write_text("previous")
    (staging / "manifest").write_text("replacement")
    exchange = atomic._exchange_directories

    def check_exchange(source, target):
        assert (target / "manifest").read_text() == "previous"
        if not exchange(source, target):
            pytest.skip("Filesystem does not support atomic directory exchange")
        assert (target / "manifest").read_text() == "replacement"
        assert (source / "manifest").read_text() == "previous"
        return True

    monkeypatch.setattr(atomic, "_exchange_directories", check_exchange)
    assert atomic.replace_directory(staging, live) == live
    assert (live / "manifest").read_text() == "replacement"
    assert not staging.exists()


@pytest.mark.parametrize("batch", [False, True])
def test_interrupted_fallback_restores_every_live_generation(
    tmp_path, monkeypatch, batch
):
    pairs = []
    for i in range(2 if batch else 1):
        live = tmp_path / f"dataset-{i}"
        staging = tmp_path / f".dataset-{i}.staging"
        live.mkdir()
        staging.mkdir()
        (live / "manifest").write_text(f"previous {i}")
        (staging / "manifest").write_text(f"replacement {i}")
        pairs.append((staging, live))
    replace = atomic.os.replace
    exchange = atomic._exchange_directories

    def exchange_first(source, target):
        return batch and target == pairs[0][1] and exchange(source, target)

    def interrupt(source, target):
        replace(source, target)
        if source == pairs[-1][1] and target.suffix == ".bak":
            if batch:
                signal.raise_signal(signal.SIGINT)
            else:
                raise KeyboardInterrupt

    monkeypatch.setattr(atomic, "_exchange_directories", exchange_first)
    monkeypatch.setattr(atomic.os, "replace", interrupt)
    original_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            if batch:
                atomic.replace_directories(pairs)
            else:
                atomic.replace_directory(*pairs[0])
    finally:
        signal.signal(signal.SIGINT, original_handler)
    for i, (staging, live) in enumerate(pairs):
        assert (live / "manifest").read_text() == f"previous {i}"
        assert (staging / "manifest").read_text() == f"replacement {i}"
    assert not list(tmp_path.glob(".*.bak"))
