"""Retained copies of promoted calibration bundles that outlive their source run.

A promoted calibration is two small profile collections at the root of the
calibration run that produced it. That run also holds gigabytes of capture
evidence, so it is the first thing an operator deletes when storage runs low.
This library keeps a hash-verified copy of the two collections below the
working-data root, where dataset setup can still select it after the run folder
is gone.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from posetestbot.calibration.profile_library import (
    _LoadedBundle,
    _bundle_source_artifacts,
    _json_object,
    _latest_calibrated_at,
    _load_bundle,
    _profile_summary,
    _read_regular_file,
    _safe_error,
    _safe_snapshot_path,
    _source_run_name,
    _timestamp_sort_value,
    load_calibration_profile_selection,
)
from posetestbot.calibration.profiles import CalibrationStatus
from posetestbot.io.artifacts import (
    CALIBRATION_PROFILES,
    INTRINSIC_CALIBRATION_PROFILES,
    RUN_CONFIG,
)
from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json


LIBRARY_DIRECTORY = "calibration_library"
OVERVIEW_SCHEMA_VERSION = "calibration_library_overview.v1"
RETAINED_SCHEMA_VERSION = "retained_calibration.v1"
RETAINED_RECORD = "retained_calibration.json"
RETENTION_REASONS = ("run_deleted", "operator_request", "promotion")
ORIGIN_KINDS = ("calibration_run", "run_snapshot")
_LOCK_FILE = ".calibration_library.lock"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_THREAD_LOCK = threading.RLock()
_LIBRARY_LOCKS: set[Path] = set()


@dataclass(frozen=True)
class RetainedCalibration:
    """One verified library entry: its directory, record, and parsed bundle."""

    path: Path
    record: Mapping[str, Any]
    bundle: _LoadedBundle


def default_calibration_library_root() -> Path:
    configured = os.environ.get("POSETESTBOT_WORKING_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve() / LIBRARY_DIRECTORY
    app_root = os.environ.get("POSETESTBOT_APP_ROOT")
    root = (
        Path(app_root).expanduser().resolve()
        if app_root
        else Path(__file__).resolve().parents[2]
    )
    return root / "working_data" / LIBRARY_DIRECTORY


def calibration_age_warning_days() -> int | None:
    value = os.environ.get("POSETESTBOT_CALIBRATION_AGE_WARNING_DAYS")
    if not value:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise ValueError("POSETESTBOT_CALIBRATION_AGE_WARNING_DAYS must be a positive integer")
    return int(value)


def _library_root(library_root: str | Path | None) -> Path:
    root = (
        Path(library_root).expanduser()
        if library_root is not None
        else default_calibration_library_root()
    )
    if root.is_symlink():
        raise ValueError(f"Calibration library must not be a symbolic link: {root}")
    return root.resolve()


def _require_digest(value: Any, *, label: str = "bundle_sha256") -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def calibration_library_lock(
    library_root: str | Path | None = None,
) -> Iterator[Path]:
    """Serialize library mutations across threads and worker processes."""

    root = _library_root(library_root)
    root.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    with _THREAD_LOCK:
        if root in _LIBRARY_LOCKS:
            yield root
            return
        descriptor = os.open(root / _LOCK_FILE, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _LIBRARY_LOCKS.add(root)
            try:
                yield root
            finally:
                _LIBRARY_LOCKS.remove(root)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _sweep_leftovers(root: Path) -> None:
    """Remove staging and trash directories a crashed mutation left behind.

    Only called with the library lock held, when no live mutation owns them.
    """

    for child in root.iterdir():
        if child.name.startswith(".deleted.") or child.name.endswith(".tmp"):
            if child.is_dir() and not child.is_symlink():
                os.chmod(child, 0o700)
                shutil.rmtree(child, ignore_errors=True)


def _read_entry(entry: Path) -> RetainedCalibration:
    digest = _require_digest(entry.name, label="Retained calibration directory name")
    if entry.is_symlink() or not entry.is_dir():
        raise ValueError(f"Retained calibration must be a real directory: {entry}")
    record = _json_object(
        _read_regular_file(entry / RETAINED_RECORD), label=RETAINED_RECORD
    )
    if record.get("schema_version") != RETAINED_SCHEMA_VERSION:
        raise ValueError(f"{RETAINED_RECORD} schema must be {RETAINED_SCHEMA_VERSION}")
    name = record.get("name")
    origin = record.get("origin")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError(f"{RETAINED_RECORD} name is invalid")
    if not isinstance(origin, Mapping) or origin.get("kind") not in ORIGIN_KINDS:
        raise ValueError(f"{RETAINED_RECORD} origin is invalid")
    if record.get("reason") not in RETENTION_REASONS:
        raise ValueError(f"{RETAINED_RECORD} reason is invalid")
    if not isinstance(record.get("retained_at"), str):
        raise ValueError(f"{RETAINED_RECORD} retained_at is invalid")
    bundle = _load_bundle(entry)
    if bundle.bundle_sha256 != digest or record.get("bundle_sha256") != digest:
        raise ValueError(
            "Retained calibration bytes do not match their content-addressed identity"
        )
    if any(
        record.get(key) != artifact
        for key, artifact in _bundle_source_artifacts(bundle).items()
    ):
        raise ValueError(f"{RETAINED_RECORD} does not describe the retained files")
    return RetainedCalibration(path=entry, record=dict(record), bundle=bundle)


def resolve_retained_entry(value: Any) -> RetainedCalibration | None:
    """Return the verified entry for a library path, or None for any other path."""

    if not isinstance(value, str | Path) or not str(value).strip():
        return None
    path = Path(value)
    root = _library_root(None)
    if not path.is_absolute() or path.parent != root or not _SHA256.fullmatch(path.name):
        return None
    if not os.path.lexists(path):
        raise FileNotFoundError(f"Retained calibration no longer exists: {path}")
    return _read_entry(path)


def list_retained_calibrations(
    library_root: str | Path | None = None,
) -> list[tuple[Path, RetainedCalibration | None, str | None]]:
    """List ``(path, entry, error)``; a corrupt entry is reported, never hidden."""

    root = _library_root(library_root)
    if not root.is_dir():
        return []
    found: list[tuple[Path, RetainedCalibration | None, str | None]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not _SHA256.fullmatch(child.name):
            continue
        try:
            found.append((child, _read_entry(child), None))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            found.append(
                (child, None, _safe_error("Retained calibration is not usable", exc))
            )
    return found


def _run_id(run_root: Path) -> str | None:
    try:
        value = _json_object(_read_regular_file(run_root / RUN_CONFIG), label=RUN_CONFIG)
    except (OSError, ValueError):
        return None
    run_id = value.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _retain_bundle(
    bundle: _LoadedBundle,
    *,
    name: str,
    reason: str,
    origin: Mapping[str, Any],
    source_runs: Sequence[Mapping[str, Any]],
    library_root: str | Path | None,
) -> dict[str, Any]:
    if reason not in RETENTION_REASONS:
        raise ValueError("Retention reason must be one of: " + ", ".join(RETENTION_REASONS))
    if not any(profile.status == CalibrationStatus.VALID for profile in bundle.profiles):
        raise ValueError("The calibration contains no profiles marked valid")
    digest = bundle.bundle_sha256
    with calibration_library_lock(library_root) as root:
        _sweep_leftovers(root)
        destination = root / digest
        if os.path.lexists(destination):
            # Content addressing makes a repeat request a no-op. A corrupt
            # existing entry raises here so it is never reported as retained.
            existing = _read_entry(destination)
            return {
                "bundle_sha256": digest,
                "path": destination.as_posix(),
                "name": existing.record["name"],
                "already_retained": True,
            }
        record = {
            "schema_version": RETAINED_SCHEMA_VERSION,
            "bundle_sha256": digest,
            "name": name,
            "retained_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "reason": reason,
            "origin": dict(origin),
            "source_runs": [dict(item) for item in source_runs],
            **_bundle_source_artifacts(bundle),
        }
        staging = root / f".{digest}.{uuid.uuid4().hex}.tmp"
        staging.mkdir()
        try:
            atomic_write_bytes(staging / CALIBRATION_PROFILES, bundle.calibration_bytes)
            atomic_write_bytes(
                staging / INTRINSIC_CALIBRATION_PROFILES, bundle.intrinsic_bytes
            )
            atomic_write_json(staging / RETAINED_RECORD, record)
            for filename in (
                CALIBRATION_PROFILES,
                INTRINSIC_CALIBRATION_PROFILES,
                RETAINED_RECORD,
            ):
                os.chmod(staging / filename, 0o444)
            _fsync_directory(staging)
            os.rename(staging, destination)
            # The caller may delete the only other copy next, so the new
            # directory entry has to be durable before this returns.
            _fsync_directory(root)
            os.chmod(destination, 0o555)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return {
        "bundle_sha256": digest,
        "path": destination.as_posix(),
        "name": name,
        "already_retained": False,
    }


def run_has_promoted_calibration(run_root: str | Path) -> bool:
    """True when a run root publishes ``calibration_profiles.json``.

    A symbolic link does not count: no reader accepts one as a calibration.
    """

    try:
        return stat.S_ISREG((Path(run_root) / CALIBRATION_PROFILES).lstat().st_mode)
    except OSError:
        return False


def retain_run_calibration(
    run_root: str | Path,
    *,
    reason: str,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Copy the calibration promoted at a run root into the library."""

    root = Path(run_root)
    bundle = _load_bundle(root)
    name = _source_run_name(root)
    return _retain_bundle(
        bundle,
        name=name,
        reason=reason,
        origin={
            "kind": "calibration_run",
            "run_root": root.as_posix(),
            "run_name": name,
            "run_id": _run_id(root),
        },
        source_runs=[
            {
                "run_root": root.as_posix(),
                "run_name": name,
                "bundle_sha256": bundle.bundle_sha256,
            }
        ],
        library_root=library_root,
    )


def _selection_snapshot(run_root: Path) -> tuple[Mapping[str, Any], Path]:
    """Return a run's verified selection and its snapshot directory."""

    selection = load_calibration_profile_selection(run_root)
    calibration_path = _safe_snapshot_path(
        run_root,
        selection["snapshot"]["calibration_profiles"]["relative_path"],
        label="snapshot.calibration_profiles.relative_path",
    )
    return selection, calibration_path.parent


def _snapshot_name(selection: Mapping[str, Any]) -> str:
    """Name a snapshot after its calibration run when it is one run's bundle."""

    sources = selection.get("sources")
    source = selection["source"]
    if (
        isinstance(sources, list)
        and len(sources) == 1
        and isinstance(sources[0], Mapping)
        and sources[0].get("bundle_sha256") == source.get("bundle_sha256")
    ):
        return str(sources[0]["run_name"])
    return str(source["run_name"])


def retain_run_snapshot(
    run_root: str | Path,
    *,
    reason: str = "operator_request",
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Copy the verified calibration snapshot a dataset run selected."""

    root = Path(run_root).resolve()
    selection, snapshot = _selection_snapshot(root)
    bundle = _load_bundle(snapshot)
    if bundle.bundle_sha256 != selection["source"]["bundle_sha256"]:
        raise ValueError("Run calibration snapshot does not match its selection")
    return _retain_bundle(
        bundle,
        name=_snapshot_name(selection),
        reason=reason,
        origin={
            "kind": "run_snapshot",
            "run_root": root.as_posix(),
            "run_name": _source_run_name(root),
            "run_id": _run_id(root),
        },
        source_runs=[
            {
                "run_root": item["run_root"],
                "run_name": item["run_name"],
                "bundle_sha256": item["bundle_sha256"],
            }
            for item in selection.get("sources", [])
        ],
        library_root=library_root,
    )


def run_calibration_retention_targets(runs: Sequence[Path]) -> dict[str, str]:
    """Identify promoted calibrations and verified snapshots with no other copy."""
    required: dict[str, str] = {}
    promoted: set[str] = set()
    snapshots: dict[str, list[str]] = {}
    for run in runs:
        if run_has_promoted_calibration(run):
            required[run.as_posix()] = "promoted"
            try:
                promoted.add(_load_bundle(run).bundle_sha256)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        try:
            selection, _ = _selection_snapshot(run)
            digest = selection["source"]["bundle_sha256"]
            snapshots.setdefault(digest, []).append(run.as_posix())
        except (OSError, ValueError, KeyError, TypeError):
            continue
    library_root = _library_root(None)
    for digest, holders in snapshots.items():
        if len(holders) == 1 and digest not in promoted:
            try:
                _read_entry(library_root / digest)
            except (OSError, ValueError, KeyError, TypeError):
                required.setdefault(holders[0], "last_snapshot")
    return required


def retain_calibration(
    run_root: str | Path,
    *,
    expected_bundle_sha256: str,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Retain the calibration a run holds, promoted or selected, by its hash."""

    digest = _require_digest(expected_bundle_sha256, label="expected_bundle_sha256")
    root = Path(run_root).resolve()
    if run_has_promoted_calibration(root):
        try:
            promoted = _load_bundle(root).bundle_sha256
        except (OSError, ValueError, KeyError, TypeError):
            promoted = None
        if promoted == digest:
            return retain_run_calibration(
                root, reason="operator_request", library_root=library_root
            )
    try:
        selected = _selection_snapshot(root)[0]["source"]["bundle_sha256"]
    except FileNotFoundError:
        selected = None
    if selected == digest:
        return retain_run_snapshot(root, library_root=library_root)
    raise LookupError(
        "The run no longer holds this calibration; refresh the calibration list"
    )


def delete_retained_calibration(
    bundle_sha256: str,
    *,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Permanently remove one library entry.

    Dataset runs that already selected it keep their own immutable snapshots.
    """

    digest = _require_digest(bundle_sha256)
    with calibration_library_lock(library_root) as root:
        _sweep_leftovers(root)
        entry = root / digest
        if entry.is_symlink() or not entry.is_dir():
            raise FileNotFoundError(f"No retained calibration {digest}")
        trash = root / f".deleted.{digest}.{uuid.uuid4().hex}"
        os.rename(entry, trash)
        _fsync_directory(root)
        # Entries are published read-only, which also blocks unlinking their files.
        os.chmod(trash, 0o700)
        shutil.rmtree(trash)
    return {"bundle_sha256": digest, "status": "deleted"}


def _newest_first(record: Mapping[str, Any]) -> tuple[float, str, str]:
    calibrated_at = record["calibrated_at"]
    return (
        -_timestamp_sort_value(calibrated_at) if calibrated_at else float("inf"),
        str(record["name"]),
        str(record["bundle_sha256"]),
    )


def _bundle_overview(bundle: _LoadedBundle) -> dict[str, Any]:
    valid_count = sum(
        profile.status == CalibrationStatus.VALID for profile in bundle.profiles
    )
    return {
        "bundle_sha256": bundle.bundle_sha256,
        "valid": valid_count > 0,
        "issues": (
            []
            if valid_count
            else ["The calibration collection contains no profiles marked valid."]
        ),
        "calibrated_at": _latest_calibrated_at(bundle),
        "valid_profile_count": valid_count,
        "profiles": [_profile_summary(profile) for profile in bundle.profiles],
    }


def _unusable(name: str, path: Path, error: str) -> dict[str, Any]:
    return {
        "bundle_sha256": None,
        "name": name,
        "valid": False,
        "issues": [error],
        "calibrated_at": None,
        "valid_profile_count": 0,
        "profiles": [],
        "path": path.as_posix(),
    }


def _run_folders() -> list[Path]:
    """Direct, non-symlink children of the approved run roots."""

    from posetestbot.web.security import web_run_roots

    runs: list[Path] = []
    for allowed_root in web_run_roots():
        root = allowed_root.resolve()
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
                if child.resolve().parent == root:
                    runs.append(child.resolve())
            except OSError:
                continue
    return runs


def calibration_library_overview(
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Every calibration on disk: promoted in a run, retained, or snapshot-only.

    ``availability`` says where the selectable copy lives. A ``snapshot``
    calibration exists only inside dataset runs that already selected it and
    has to be retained before a new run can use it.
    """

    root = _library_root(library_root)
    calibrations: dict[str, dict[str, Any]] = {}
    unusable: list[dict[str, Any]] = []

    def record_for(bundle: _LoadedBundle, name: str) -> dict[str, Any]:
        return calibrations.setdefault(
            bundle.bundle_sha256,
            {
                **_bundle_overview(bundle),
                "name": name,
                "availability": "snapshot",
                "source_run": None,
                "retained": None,
                "snapshot_runs": [],
                "used_by": [],
            },
        )

    runs = _run_folders()
    for run in runs:
        if not run_has_promoted_calibration(run):
            continue
        name = _source_run_name(run)
        try:
            bundle = _load_bundle(run)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unusable.append(
                {
                    **_unusable(
                        name, run, _safe_error("Calibration bundle is not usable", exc)
                    ),
                    "availability": "run",
                }
            )
            continue
        record = record_for(bundle, name)
        if record["source_run"] is None:
            record["availability"] = "run"
            record["source_run"] = {"run_root": run.as_posix(), "run_name": name}

    for path, entry, error in list_retained_calibrations(root):
        if entry is None:
            unusable.append(
                {
                    **_unusable(path.name[:12], path, str(error)),
                    "availability": "library",
                }
            )
            continue
        record = record_for(entry.bundle, str(entry.record["name"]))
        record["retained"] = {
            "path": entry.path.as_posix(),
            "retained_at": entry.record["retained_at"],
            "reason": entry.record["reason"],
            "origin": dict(entry.record["origin"]),
        }
        if record["availability"] == "snapshot":
            record["availability"] = "library"

    usage: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        try:
            selection, snapshot = _selection_snapshot(run)
        except (OSError, ValueError, KeyError, TypeError):
            # A missing or invalid selection gives no trustworthy usage evidence.
            continue
        run_name = _source_run_name(run)
        digest = str(selection["source"]["bundle_sha256"])
        selected_at = selection.get("selected_at")
        usage.setdefault(digest, []).append(
            {
                "run_root": run.as_posix(),
                "run_name": run_name,
                "selected_at": selected_at,
                "sensor_keys": sorted(selection.get("sensor_profiles", {})),
            }
        )
        for source in selection.get("sources", []):
            if source["bundle_sha256"] != digest:
                usage.setdefault(str(source["bundle_sha256"]), []).append(
                    {
                        "run_root": run.as_posix(),
                        "run_name": run_name,
                        "selected_at": selected_at,
                        "sensor_keys": sorted(source["selected_sensor_keys"]),
                    }
                )
        if digest not in calibrations:
            try:
                bundle = _load_bundle(snapshot)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            record_for(bundle, _snapshot_name(selection))
        calibrations[digest]["snapshot_runs"].append(
            {"run_root": run.as_posix(), "run_name": run_name}
        )

    for digest, record in calibrations.items():
        record["used_by"] = usage.get(digest, [])
        record["selectable"] = record["valid"] and record["availability"] != "snapshot"
    ordered = sorted(calibrations.values(), key=_newest_first)
    return {
        "schema_version": OVERVIEW_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "library_root": root.as_posix(),
        "age_warning_days": calibration_age_warning_days(),
        "calibrations": ordered,
        "unusable": unusable,
    }
