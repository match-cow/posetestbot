"""Recover interrupted publication of a calibration's canonical artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Sequence

from posetestbot.io.artifacts import (
    CALIBRATION_PROFILES,
    CALIBRATION_TARGET,
    DATASET_MANIFEST,
    INTRINSIC_CALIBRATION_PROFILES,
    RUN_CONFIG,
)
from posetestbot.io.atomic import atomic_write_json


JOURNAL = ".calibration-promotion.transaction.json"
BACKUP_PATTERN = re.compile(r"\.calibration-promotion-backup-[0-9a-f]{32}")
STAGING_PATTERN = re.compile(r"\.calibration-promotion-[0-9a-f]{32}-[0-9a-f]{32}")
ATTEMPT_ARTIFACT = re.compile(
    r"processed/calibration/[0-9a-f]{32}/promotion(_request)?\.json"
)
TARGET_ARTIFACT = re.compile(
    r"calibration_targets/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)
CANONICAL_ARTIFACTS = {
    CALIBRATION_PROFILES,
    CALIBRATION_TARGET,
    DATASET_MANIFEST,
    INTRINSIC_CALIBRATION_PROFILES,
    RUN_CONFIG,
}


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _target(root: Path, relative: str) -> Path:
    path = root / relative
    managed = (
        relative in CANONICAL_ARTIFACTS
        or ATTEMPT_ARTIFACT.fullmatch(relative)
        or TARGET_ARTIFACT.fullmatch(relative)
    )
    if not managed or path.resolve() != path:
        raise ValueError("Calibration promotion journal contains an unmanaged target")
    return path


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover_promotion_transaction(root: Path) -> None:
    """Run under the run-config lock before using or replacing canonical files."""
    journal_path = root / JOURNAL
    if not journal_path.exists() and not journal_path.is_symlink():
        if any(root.glob(".calibration-promotion-backup-*")):
            raise ValueError(
                "Calibration promotion has unjournaled backups; recover the retained originals before retrying"
            )
        return
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError("Invalid calibration promotion journal")
    journal = json.loads(journal_path.read_text())
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != "calibration_promotion_transaction.v1"
        or journal.get("phase") not in {"prepared", "committed"}
        or not BACKUP_PATTERN.fullmatch(str(journal.get("backup", "")))
        or not isinstance(journal.get("targets"), list)
        or not journal["targets"]
    ):
        raise ValueError("Invalid calibration promotion journal")
    backup = root / journal["backup"]
    if backup.is_symlink():
        raise ValueError("Invalid calibration promotion backup")
    entries = []
    for index, entry in enumerate(journal["targets"]):
        if not isinstance(entry, dict) or type(entry.get("existed")) is not bool:
            raise ValueError("Invalid calibration promotion journal entry")
        target = _target(root, str(entry.get("path", "")))
        original = backup / str(index)
        if original.is_symlink():
            raise ValueError("Invalid calibration promotion backup entry")
        entries.append((target, original, entry["existed"]))
    if len({target for target, _, _ in entries}) != len(entries):
        raise ValueError("Calibration promotion journal repeats a target")
    for target, original, existed in reversed(entries):
        if journal["phase"] == "committed":
            if not target.exists():
                raise ValueError(
                    "Committed calibration promotion is missing an artifact"
                )
        elif original.exists():
            if original.is_dir():
                _remove(target)
            os.replace(original, target)
            _sync(target.parent)
            _sync(backup)
        elif not existed:
            if target.exists():
                _remove(target)
                _sync(target.parent)
        elif not target.exists():
            raise ValueError("Calibration promotion cannot recover a missing original")
    _remove(backup)
    _sync(root)
    journal_path.unlink()
    _sync(root)


def replace_promotion_artifacts(
    root: Path,
    promotions: Sequence[tuple[Path, Path]],
) -> None:
    """Commit staged artifacts together or restore their previous generation."""
    recover_promotion_transaction(root)
    backup = root / f".calibration-promotion-backup-{uuid.uuid4().hex}"
    targets = []
    for source, target in promotions:
        _target(root, target.relative_to(root).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for directory, _, filenames in os.walk(source, topdown=False):
                for filename in filenames:
                    _sync(Path(directory) / filename)
                _sync(Path(directory))
        else:
            _sync(source)
        targets.append(
            {"path": target.relative_to(root).as_posix(), "existed": target.exists()}
        )
    journal_path = root / JOURNAL
    journal = {
        "schema_version": "calibration_promotion_transaction.v1",
        "phase": "prepared",
        "backup": backup.name,
        "targets": targets,
    }
    atomic_write_json(journal_path, journal)
    _sync(root)
    try:
        backup.mkdir()
        _sync(root)
        for index, (source, target) in enumerate(promotions):
            if target.exists():
                original = backup / str(index)
                if target.is_dir():
                    os.replace(target, original)
                else:
                    os.link(target, original)
                _sync(backup)
                _sync(target.parent)
            os.replace(source, target)
            _sync(target.parent)
            if source.parent != target.parent:
                _sync(source.parent)
        atomic_write_json(journal_path, {**journal, "phase": "committed"})
        _sync(root)
    except BaseException:
        try:
            recover_promotion_transaction(root)
        except (OSError, ValueError) as recovery_error:
            raise RuntimeError(
                f"Calibration promotion rollback failed; retained originals at {backup}"
            ) from recovery_error
        raise
    recover_promotion_transaction(root)


def clear_promotion_staging(root: Path) -> None:
    for path in root.iterdir():
        if (
            STAGING_PATTERN.fullmatch(path.name)
            and path.is_dir()
            and not path.is_symlink()
        ):
            shutil.rmtree(path)
