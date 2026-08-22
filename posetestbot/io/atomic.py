"""Crash-resistant helpers for replace-in-place text and JSON artifacts."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import shutil
import stat
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


_DIRECTORY_TRANSACTION_SCHEMA = "directory_replacement_transaction.v1"
_DIRECTORY_TRANSACTION_PREFIX = ".posetestbot-directory-replace."
_DIRECTORY_TRANSACTION_SUFFIX = ".json"
_DIRECTORY_LOCK_NAME = ".posetestbot-directory-replace.lock"
_DIRECTORY_PHASES = {
    "prepared",
    "applying",
    "committed",
    "rolled_back",
    "cleaned",
}
_MAX_JOURNAL_BYTES = 1_048_576
_RENAME_NOREPLACE = 1
_DIRECTORY_TRANSACTION_FIELDS = {
    "schema_version",
    "transaction_id",
    "phase",
    "parents",
    "entries",
    "replicas",
}


def _load_renameat2() -> Any | None:
    if not sys.platform.startswith("linux"):
        return None
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    return renameat2


_RENAMEAT2 = _load_renameat2()


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _DirectoryIdentity:
        return cls(device=value.st_dev, inode=value.st_ino)

    @classmethod
    def from_json(cls, value: object, *, label: str) -> _DirectoryIdentity:
        if not isinstance(value, Mapping) or set(value) != {"device", "inode"}:
            raise RuntimeError(f"Invalid {label} directory identity")
        device = value.get("device")
        inode = value.get("inode")
        if (
            not isinstance(device, int)
            or isinstance(device, bool)
            or device < 0
            or not isinstance(inode, int)
            or isinstance(inode, bool)
            or inode <= 0
        ):
            raise RuntimeError(f"Invalid {label} directory identity")
        return cls(device=device, inode=inode)

    def to_json(self) -> dict[str, int]:
        return {"device": self.device, "inode": self.inode}


@dataclass
class _LockedParent:
    path: Path
    directory_fd: int
    lock_fd: int
    identity: _DirectoryIdentity


def _transaction_boundary(_label: str) -> None:
    """Fault-injection seam used to exercise process-death recovery."""


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Write text through a same-directory temporary file and atomically replace."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def atomic_write_bytes(path: str | Path, payload: bytes) -> Path:
    """Write binary data through a same-directory temporary file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = True,
    default: Any = None,
) -> Path:
    """Serialize JSON without allowing non-standard NaN/Infinity values."""

    text = json.dumps(
        value,
        indent=indent,
        sort_keys=sort_keys,
        default=default,
        allow_nan=False,
    )
    return atomic_write_text(path, f"{text}\n")


def _absolute_path(path: str | Path) -> Path:
    value = Path(os.path.abspath(os.fspath(path)))
    if not value.name:
        raise ValueError(f"Directory replacement path has no final component: {path}")
    return value


def _require_plain_directory_path(path: Path, *, label: str) -> os.stat_result:
    current = Path(path.anchor)
    current_stat = os.lstat(current)
    for component in path.parts[1:]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"{label} directory does not exist: {path}"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise ValueError(f"{label} directory must not use symlinks: {path}")
        if not stat.S_ISDIR(current_stat.st_mode):
            raise NotADirectoryError(f"{label} path is not a directory: {path}")
    return current_stat


def _child_stat(parent: _LockedParent, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent.directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _child_directory_identity(
    parent: _LockedParent,
    name: str,
    *,
    label: str,
) -> _DirectoryIdentity | None:
    value = _child_stat(parent, name)
    if value is None:
        return None
    if stat.S_ISLNK(value.st_mode):
        raise RuntimeError(f"{label} must not be a symlink: {parent.path / name}")
    if not stat.S_ISDIR(value.st_mode):
        raise RuntimeError(f"{label} must be a directory: {parent.path / name}")
    return _DirectoryIdentity.from_stat(value)


def _fsync_parent(parent: _LockedParent) -> None:
    os.fsync(parent.directory_fd)


def _validate_locked_parent_path(parent: _LockedParent) -> None:
    current = _require_plain_directory_path(parent.path, label="Rename parent")
    if _DirectoryIdentity.from_stat(current) != parent.identity:
        raise RuntimeError(f"Rename parent identity changed: {parent.path}")


def _fsync_directory_tree(root: Path) -> None:
    """Make a staged regular-file tree durable without following links."""

    for current, directories, filenames in os.walk(
        root, topdown=False, followlinks=False
    ):
        current_path = Path(current)
        for name in [*directories, *filenames]:
            child = current_path / name
            child_stat = os.lstat(child)
            if stat.S_ISLNK(child_stat.st_mode):
                raise ValueError(
                    f"Staging directory must not contain symlinks: {child}"
                )
            if not stat.S_ISDIR(child_stat.st_mode) and not stat.S_ISREG(
                child_stat.st_mode
            ):
                raise ValueError(
                    f"Staging directory contains an unsupported filesystem entry: {child}"
                )
        for name in filenames:
            child = current_path / name
            descriptor = os.open(
                child,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ValueError(f"Staged artifact is not a regular file: {child}")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(
            current_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _locked_parents(
    paths: set[Path],
    *,
    expected_identities: Mapping[Path, _DirectoryIdentity] | None = None,
) -> Iterator[dict[Path, _LockedParent]]:
    locked: dict[Path, _LockedParent] = {}
    identities = expected_identities or {}
    try:
        for path in sorted(paths, key=os.fspath):
            path_stat = _require_plain_directory_path(path, label="Replacement parent")
            path_identity = _DirectoryIdentity.from_stat(path_stat)
            expected_identity = identities.get(path)
            if expected_identity is not None and path_identity != expected_identity:
                raise RuntimeError(
                    f"Directory transaction parent identity changed: {path}"
                )
            directory_fd = os.open(
                path,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
            )
            lock_fd = -1
            try:
                opened = os.fstat(directory_fd)
                opened_identity = _DirectoryIdentity.from_stat(opened)
                if opened_identity != path_identity:
                    raise RuntimeError(
                        f"Replacement parent changed while opening: {path}"
                    )
                if (
                    expected_identity is not None
                    and opened_identity != expected_identity
                ):
                    raise RuntimeError(
                        f"Directory transaction parent identity changed: {path}"
                    )
                # Directory-FD locks coordinate with the public no-clobber
                # rename primitive without forcing persistent control files
                # into transaction-owned temporary directories. The existing
                # lock-file protocol remains nested inside this lock for
                # compatibility with other directory-transaction processes.
                fcntl.flock(directory_fd, fcntl.LOCK_EX)
                lock_fd = os.open(
                    _DIRECTORY_LOCK_NAME,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                lock_stat = os.fstat(lock_fd)
                if not stat.S_ISREG(lock_stat.st_mode):
                    raise RuntimeError(
                        f"Directory transaction lock is not a regular file: "
                        f"{path / _DIRECTORY_LOCK_NAME}"
                    )
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                locked_path_stat = _require_plain_directory_path(
                    path, label="Replacement parent"
                )
                if _DirectoryIdentity.from_stat(
                    locked_path_stat
                ) != _DirectoryIdentity.from_stat(opened):
                    raise RuntimeError(
                        f"Replacement parent changed while acquiring its lock: {path}"
                    )
                locked[path] = _LockedParent(
                    path=path,
                    directory_fd=directory_fd,
                    lock_fd=lock_fd,
                    identity=opened_identity,
                )
            except BaseException:
                if lock_fd >= 0:
                    os.close(lock_fd)
                os.close(directory_fd)
                raise
        yield locked
    finally:
        for parent in reversed(list(locked.values())):
            try:
                fcntl.flock(parent.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(parent.lock_fd)
                try:
                    fcntl.flock(parent.directory_fd, fcntl.LOCK_UN)
                finally:
                    os.close(parent.directory_fd)


@contextmanager
def _locked_directory_parents(
    paths: set[Path],
) -> Iterator[dict[Path, _LockedParent]]:
    """Lock real parent directory FDs without creating control files."""

    locked: dict[Path, _LockedParent] = {}
    try:
        for path in sorted(paths, key=os.fspath):
            path_stat = _require_plain_directory_path(path, label="Rename parent")
            path_identity = _DirectoryIdentity.from_stat(path_stat)
            directory_fd = os.open(
                path,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened_identity = _DirectoryIdentity.from_stat(os.fstat(directory_fd))
                if opened_identity != path_identity:
                    raise RuntimeError(f"Rename parent changed while opening: {path}")
                fcntl.flock(directory_fd, fcntl.LOCK_EX)
                locked_path_stat = _require_plain_directory_path(
                    path, label="Rename parent"
                )
                if _DirectoryIdentity.from_stat(locked_path_stat) != opened_identity:
                    raise RuntimeError(
                        f"Rename parent changed while acquiring its lock: {path}"
                    )
                locked[path] = _LockedParent(
                    path=path,
                    directory_fd=directory_fd,
                    lock_fd=-1,
                    identity=opened_identity,
                )
            except BaseException:
                os.close(directory_fd)
                raise
        yield locked
    finally:
        for parent in reversed(list(locked.values())):
            try:
                fcntl.flock(parent.directory_fd, fcntl.LOCK_UN)
            finally:
                os.close(parent.directory_fd)


def _journal_name(transaction_id: str) -> str:
    return (
        f"{_DIRECTORY_TRANSACTION_PREFIX}{transaction_id}"
        f"{_DIRECTORY_TRANSACTION_SUFFIX}"
    )


def _journal_transaction_id(name: str) -> str | None:
    if not name.startswith(_DIRECTORY_TRANSACTION_PREFIX) or not name.endswith(
        _DIRECTORY_TRANSACTION_SUFFIX
    ):
        return None
    transaction_id = name[
        len(_DIRECTORY_TRANSACTION_PREFIX) : -len(_DIRECTORY_TRANSACTION_SUFFIX)
    ]
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        return None
    return transaction_id


def _journal_temp_name(transaction_id: str, phase: str) -> str:
    return f"{_journal_name(transaction_id)}.{phase}.tmp"


def _journal_temp_identity(name: str) -> tuple[str, str] | None:
    for phase in _DIRECTORY_PHASES:
        suffix = f"{_DIRECTORY_TRANSACTION_SUFFIX}.{phase}.tmp"
        if not name.startswith(_DIRECTORY_TRANSACTION_PREFIX) or not name.endswith(
            suffix
        ):
            continue
        transaction_id = name[len(_DIRECTORY_TRANSACTION_PREFIX) : -len(suffix)]
        if _journal_transaction_id(_journal_name(transaction_id)) == transaction_id:
            return transaction_id, phase
    return None


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _read_child_json(parent: _LockedParent, name: str) -> dict[str, Any]:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent.directory_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(
                f"Directory transaction journal is not regular: {parent.path / name}"
            )
        payload = bytearray()
        while len(payload) <= _MAX_JOURNAL_BYTES:
            chunk = os.read(
                descriptor, min(65_536, _MAX_JOURNAL_BYTES + 1 - len(payload))
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _MAX_JOURNAL_BYTES:
            raise RuntimeError(
                f"Directory transaction journal is too large: {parent.path / name}"
            )
    finally:
        os.close(descriptor)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid directory transaction journal: {parent.path / name}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Invalid directory transaction journal: {parent.path / name}"
        )
    return value


def _journal_paths(value: Mapping[str, Any]) -> tuple[set[Path], set[Path]]:
    if set(value) != _DIRECTORY_TRANSACTION_FIELDS:
        raise RuntimeError("Invalid directory transaction journal shape")
    transaction_id = value.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or _journal_transaction_id(_journal_name(transaction_id)) != transaction_id
    ):
        raise RuntimeError("Invalid directory transaction identifier")
    if value.get("schema_version") != _DIRECTORY_TRANSACTION_SCHEMA:
        raise RuntimeError("Unsupported directory transaction journal schema")
    if value.get("phase") not in _DIRECTORY_PHASES:
        raise RuntimeError("Invalid directory transaction phase")
    raw_parents = value.get("parents")
    raw_entries = value.get("entries")
    raw_replicas = value.get("replicas")
    if not isinstance(raw_parents, list) or not raw_parents:
        raise RuntimeError("Directory transaction has no parents")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RuntimeError("Directory transaction has no entries")
    if not isinstance(raw_replicas, list) or not raw_replicas:
        raise RuntimeError("Directory transaction has no journal replicas")

    parents: set[Path] = set()
    for index, raw_parent in enumerate(raw_parents):
        if not isinstance(raw_parent, Mapping) or set(raw_parent) != {
            "path",
            "identity",
        }:
            raise RuntimeError(f"Invalid directory transaction parent {index}")
        raw_path = raw_parent.get("path")
        if not isinstance(raw_path, str):
            raise RuntimeError(f"Invalid directory transaction parent path {index}")
        path = _absolute_path(raw_path)
        if os.fspath(path) != raw_path or path in parents:
            raise RuntimeError(f"Invalid directory transaction parent path {index}")
        _DirectoryIdentity.from_json(
            raw_parent.get("identity"), label=f"transaction parent {index}"
        )
        parents.add(path)

    sources: set[Path] = set()
    targets: set[Path] = set()
    backups: set[Path] = set()
    entry_parents: set[Path] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {
            "backup",
            "index",
            "new_identity",
            "old_identity",
            "parent",
            "source",
            "target",
        }:
            raise RuntimeError(f"Invalid directory transaction entry {index}")
        raw_index = raw_entry.get("index")
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index != index
        ):
            raise RuntimeError(f"Invalid directory transaction entry index {index}")
        values: dict[str, Path] = {}
        for field in ("parent", "source", "target", "backup"):
            raw_path = raw_entry.get(field)
            if not isinstance(raw_path, str):
                raise RuntimeError(f"Invalid directory transaction {field} {index}")
            path = _absolute_path(raw_path)
            if os.fspath(path) != raw_path:
                raise RuntimeError(f"Invalid directory transaction {field} {index}")
            values[field] = path
        if values["parent"] not in parents or not all(
            values[field].parent == values["parent"]
            for field in ("source", "target", "backup")
        ):
            raise RuntimeError(f"Directory transaction entry {index} changed parent")
        expected_backup = values["target"].with_name(
            f".{values['target'].name}.posetestbot-backup.{transaction_id}.{index}"
        )
        if values["backup"] != expected_backup:
            raise RuntimeError(f"Invalid directory transaction backup {index}")
        for field in ("source", "target"):
            name = values[field].name
            if (
                name == _DIRECTORY_LOCK_NAME
                or name.startswith(_DIRECTORY_TRANSACTION_PREFIX)
                or ".posetestbot-backup." in name
            ):
                raise RuntimeError(
                    f"Directory transaction {field} uses a reserved name at {index}"
                )
        new_identity = _DirectoryIdentity.from_json(
            raw_entry.get("new_identity"), label=f"new generation {index}"
        )
        old_identity_value = raw_entry.get("old_identity")
        old_identity = (
            None
            if old_identity_value is None
            else _DirectoryIdentity.from_json(
                old_identity_value, label=f"old generation {index}"
            )
        )
        if old_identity == new_identity:
            raise RuntimeError(f"Directory transaction generations overlap at {index}")
        sources.add(values["source"])
        targets.add(values["target"])
        backups.add(values["backup"])
        entry_parents.add(values["parent"])
    if (
        len(sources) != len(raw_entries)
        or len(targets) != len(raw_entries)
        or len(backups) != len(raw_entries)
        or sources & targets
        or sources & backups
        or targets & backups
    ):
        raise RuntimeError("Directory transaction paths are not unique")
    if entry_parents != parents:
        raise RuntimeError(
            "Directory transaction parents do not match its entry parents"
        )
    for path in [*sources, *targets]:
        if any(parent == path or parent.is_relative_to(path) for parent in parents):
            raise RuntimeError(
                "Directory transaction path contains a participating parent"
            )

    replicas: set[Path] = set()
    for index, raw_replica in enumerate(raw_replicas):
        if not isinstance(raw_replica, str):
            raise RuntimeError(f"Invalid directory transaction replica {index}")
        replica = _absolute_path(raw_replica)
        if os.fspath(replica) != raw_replica or replica.name != _journal_name(
            transaction_id
        ):
            raise RuntimeError(f"Invalid directory transaction replica {index}")
        replicas.add(replica)
    expected_replicas = {parent / _journal_name(transaction_id) for parent in parents}
    if replicas != expected_replicas or len(raw_replicas) != len(expected_replicas):
        raise RuntimeError("Directory transaction replicas do not match its parents")
    return parents, replicas


def _immutable_journal(value: Mapping[str, Any]) -> str:
    immutable = dict(value)
    immutable.pop("phase", None)
    return json.dumps(immutable, sort_keys=True, separators=(",", ":"))


def _validate_locked_journal(
    value: Mapping[str, Any], parents: Mapping[Path, _LockedParent]
) -> None:
    declared_parents, _replicas = _journal_paths(value)
    if not declared_parents <= parents.keys():
        raise RuntimeError("Directory transaction parents are not all locked")
    expected_identities = {
        Path(raw["path"]): _DirectoryIdentity.from_json(
            raw["identity"], label="transaction parent"
        )
        for raw in value["parents"]
    }
    for path, expected in expected_identities.items():
        if parents[path].identity != expected:
            raise RuntimeError(f"Directory transaction parent identity changed: {path}")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])


def _write_journal_phase(
    value: dict[str, Any],
    phase: str,
    parents: Mapping[Path, _LockedParent],
) -> None:
    if phase not in _DIRECTORY_PHASES:
        raise ValueError(f"Unknown directory transaction phase: {phase}")
    updated = dict(value)
    updated["phase"] = phase
    payload = (
        json.dumps(updated, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    transaction_id = str(updated["transaction_id"])
    marker_name = _journal_name(transaction_id)
    temp_name = _journal_temp_name(transaction_id, phase)
    for index, raw_parent in enumerate(updated["parents"]):
        parent = parents[Path(raw_parent["path"])]
        existing_temp = _child_stat(parent, temp_name)
        if existing_temp is not None:
            if not stat.S_ISREG(existing_temp.st_mode):
                raise RuntimeError(
                    f"Directory transaction temporary journal is not regular: "
                    f"{parent.path / temp_name}"
                )
            os.unlink(temp_name, dir_fd=parent.directory_fd)
            _fsync_parent(parent)
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent.directory_fd,
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temp_name,
            marker_name,
            src_dir_fd=parent.directory_fd,
            dst_dir_fd=parent.directory_fd,
        )
        _fsync_parent(parent)
        _transaction_boundary(f"journal-{phase}:{index}")
    value["phase"] = phase


def _phase_from_replicas(
    value: Mapping[str, Any],
    parents: Mapping[Path, _LockedParent],
) -> tuple[str, list[dict[str, Any]]]:
    transaction_id = str(value["transaction_id"])
    marker_name = _journal_name(transaction_id)
    copies: list[dict[str, Any]] = []
    for raw_parent in value["parents"]:
        parent = parents[Path(raw_parent["path"])]
        if _child_stat(parent, marker_name) is not None:
            copy = _read_child_json(parent, marker_name)
            _validate_locked_journal(copy, parents)
            if copy.get("transaction_id") != transaction_id:
                raise RuntimeError("Directory transaction replica identifier changed")
            copies.append(copy)
    if not copies:
        raise RuntimeError("Directory transaction lost every journal replica")
    immutable = _immutable_journal(copies[0])
    if any(_immutable_journal(copy) != immutable for copy in copies[1:]):
        raise RuntimeError("Directory transaction journal replicas disagree")
    phases = {str(copy["phase"]) for copy in copies}
    if "cleaned" in phases:
        if not phases <= {"committed", "cleaned"}:
            raise RuntimeError(
                "Directory transaction has contradictory terminal phases"
            )
        return "cleaned", copies
    if "rolled_back" in phases:
        if not phases <= {"applying", "rolled_back"}:
            raise RuntimeError(
                "Directory transaction has contradictory terminal phases"
            )
        return "rolled_back", copies
    if "committed" in phases:
        if not phases <= {"applying", "committed"}:
            raise RuntimeError("Directory transaction has contradictory commit phases")
        phase = "committed"
    elif "applying" in phases:
        if not phases <= {"prepared", "applying"}:
            raise RuntimeError("Directory transaction has contradictory apply phases")
        phase = "applying"
    elif phases == {"prepared"}:
        phase = "prepared"
    else:
        raise RuntimeError("Directory transaction has contradictory phases")
    if phase != "prepared" and len(copies) != len(value["parents"]):
        raise RuntimeError("Active directory transaction lost a journal replica")
    return phase, copies


def _entry_paths(raw_entry: Mapping[str, Any]) -> tuple[Path, Path, Path, Path]:
    return (
        Path(raw_entry["parent"]),
        Path(raw_entry["source"]),
        Path(raw_entry["target"]),
        Path(raw_entry["backup"]),
    )


def _entry_identities(
    raw_entry: Mapping[str, Any], parent: _LockedParent
) -> tuple[
    _DirectoryIdentity | None,
    _DirectoryIdentity | None,
    _DirectoryIdentity | None,
]:
    _parent_path, source, target, backup = _entry_paths(raw_entry)
    return (
        _child_directory_identity(parent, source.name, label="Staged generation"),
        _child_directory_identity(parent, target.name, label="Destination generation"),
        _child_directory_identity(parent, backup.name, label="Backup generation"),
    )


def _validate_generation_state(
    raw_entry: Mapping[str, Any],
    parent: _LockedParent,
    *,
    committed: bool,
) -> tuple[
    _DirectoryIdentity | None,
    _DirectoryIdentity | None,
    _DirectoryIdentity | None,
]:
    source_id, target_id, backup_id = _entry_identities(raw_entry, parent)
    new_id = _DirectoryIdentity.from_json(
        raw_entry["new_identity"], label="new generation"
    )
    old_value = raw_entry["old_identity"]
    old_id = (
        None
        if old_value is None
        else _DirectoryIdentity.from_json(old_value, label="old generation")
    )
    present = [item for item in (source_id, target_id, backup_id) if item is not None]
    allowed = {new_id} | ({old_id} if old_id is not None else set())
    if any(item not in allowed for item in present):
        raise RuntimeError(
            "Directory transaction path contains an unrelated generation"
        )
    if present.count(new_id) != 1:
        raise RuntimeError(
            "Directory transaction lost or duplicated its new generation"
        )
    if committed:
        if old_id is not None and present.count(old_id) > 1:
            raise RuntimeError("Directory transaction duplicated its prior generation")
    elif old_id is not None and present.count(old_id) != 1:
        raise RuntimeError(
            "Directory transaction lost or duplicated its prior generation"
        )
    if old_id is None and any(item != new_id for item in present):
        raise RuntimeError(
            "Directory transaction contains an unexpected prior generation"
        )
    return source_id, target_id, backup_id


def _validate_settled_generation(
    raw_entry: Mapping[str, Any],
    parent: _LockedParent,
    *,
    committed: bool,
) -> None:
    source_id, target_id, backup_id = _validate_generation_state(
        raw_entry, parent, committed=committed
    )
    new_id = _DirectoryIdentity.from_json(
        raw_entry["new_identity"], label="new generation"
    )
    old_value = raw_entry["old_identity"]
    old_id = (
        None
        if old_value is None
        else _DirectoryIdentity.from_json(old_value, label="old generation")
    )
    expected = (None, new_id, None) if committed else (new_id, old_id, None)
    if (source_id, target_id, backup_id) != expected:
        outcome = "commit" if committed else "rollback"
        raise RuntimeError(
            f"Directory transaction {outcome} did not reach its settled generation"
        )


def _rename_child_no_replace(
    source_parent: _LockedParent,
    source: str,
    destination_parent: _LockedParent,
    destination: str,
) -> None:
    if _RENAMEAT2 is None:
        raise RuntimeError(
            "No-clobber path rename requires Linux renameat2(RENAME_NOREPLACE)"
        )
    ctypes.set_errno(0)
    result = _RENAMEAT2(
        source_parent.directory_fd,
        os.fsencode(source),
        destination_parent.directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise RuntimeError(
                "No-clobber path rename requires filesystem support for "
                "renameat2(RENAME_NOREPLACE)"
            ) from OSError(error, os.strerror(error))
        raise OSError(
            error,
            os.strerror(error),
            os.fspath(source_parent.path / source),
            None,
            os.fspath(destination_parent.path / destination),
        )


def _replace_child(parent: _LockedParent, source: str, target: str) -> None:
    _rename_child_no_replace(parent, source, parent, target)
    _fsync_parent(parent)


def _rollback_transaction(
    value: Mapping[str, Any], parents: Mapping[Path, _LockedParent]
) -> None:
    for raw_entry in value["entries"]:
        parent_path, _source, _target, _backup = _entry_paths(raw_entry)
        _validate_generation_state(raw_entry, parents[parent_path], committed=False)
    for raw_entry in reversed(value["entries"]):
        parent_path, source, target, backup = _entry_paths(raw_entry)
        parent = parents[parent_path]
        source_id, target_id, backup_id = _entry_identities(raw_entry, parent)
        new_id = _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        )
        old_value = raw_entry["old_identity"]
        old_id = (
            None
            if old_value is None
            else _DirectoryIdentity.from_json(old_value, label="old generation")
        )
        if target_id == new_id:
            if source_id is not None:
                raise RuntimeError(
                    "Cannot restore a staged generation over an occupied path"
                )
            _replace_child(parent, target.name, source.name)
            _transaction_boundary(f"rollback-promotion:{raw_entry['index']}")
            target_id = None
        if backup_id is not None:
            if backup_id != old_id or target_id is not None:
                raise RuntimeError(
                    "Cannot restore a prior generation over an occupied path"
                )
            _replace_child(parent, backup.name, target.name)
            _transaction_boundary(f"rollback-backup:{raw_entry['index']}")
    for raw_entry in value["entries"]:
        parent_path, _source, _target, _backup = _entry_paths(raw_entry)
        source_id, target_id, backup_id = _validate_generation_state(
            raw_entry, parents[parent_path], committed=False
        )
        new_id = _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        )
        old_value = raw_entry["old_identity"]
        old_id = (
            None
            if old_value is None
            else _DirectoryIdentity.from_json(old_value, label="old generation")
        )
        if source_id != new_id or target_id != old_id or backup_id is not None:
            raise RuntimeError(
                "Directory transaction rollback did not restore its inputs"
            )


def _remove_backup(
    parent: _LockedParent, name: str, expected: _DirectoryIdentity
) -> None:
    current = _child_directory_identity(parent, name, label="Backup generation")
    if current is None:
        return
    if current != expected:
        raise RuntimeError(
            f"Refusing to delete an unrelated backup path: {parent.path / name}"
        )
    shutil.rmtree(name, dir_fd=parent.directory_fd)
    _fsync_parent(parent)


def _finish_committed_transaction(
    value: Mapping[str, Any], parents: Mapping[Path, _LockedParent]
) -> None:
    for raw_entry in value["entries"]:
        parent_path, _source, _target, _backup = _entry_paths(raw_entry)
        source_id, target_id, backup_id = _validate_generation_state(
            raw_entry, parents[parent_path], committed=True
        )
        new_id = _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        )
        old_value = raw_entry["old_identity"]
        old_id = (
            None
            if old_value is None
            else _DirectoryIdentity.from_json(old_value, label="old generation")
        )
        if target_id not in {None, new_id} or source_id not in {None, new_id}:
            raise RuntimeError(
                "Committed directory transaction has an invalid new generation"
            )
        if target_id is None and source_id != new_id:
            raise RuntimeError(
                "Committed directory transaction lost its new generation"
            )
        if backup_id not in {None, old_id}:
            raise RuntimeError("Committed directory transaction has an invalid backup")
    for raw_entry in value["entries"]:
        parent_path, source, target, backup = _entry_paths(raw_entry)
        parent = parents[parent_path]
        source_id, target_id, _backup_id = _entry_identities(raw_entry, parent)
        new_id = _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        )
        if target_id is None and source_id == new_id:
            _replace_child(parent, source.name, target.name)
            _transaction_boundary(f"finish-promotion:{raw_entry['index']}")
        old_value = raw_entry["old_identity"]
        if old_value is not None:
            _remove_backup(
                parent,
                backup.name,
                _DirectoryIdentity.from_json(old_value, label="old generation"),
            )
            _transaction_boundary(f"backup-removed:{raw_entry['index']}")
    for raw_entry in value["entries"]:
        parent_path, _source, _target, _backup = _entry_paths(raw_entry)
        source_id, target_id, backup_id = _validate_generation_state(
            raw_entry, parents[parent_path], committed=True
        )
        new_id = _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        )
        if source_id is not None or target_id != new_id or backup_id is not None:
            raise RuntimeError("Committed directory transaction cleanup is incomplete")


def _unlink_transaction_artifacts(
    value: Mapping[str, Any], parents: Mapping[Path, _LockedParent]
) -> None:
    transaction_id = str(value["transaction_id"])
    marker_name = _journal_name(transaction_id)
    temp_names = [
        _journal_temp_name(transaction_id, phase) for phase in sorted(_DIRECTORY_PHASES)
    ]
    for index, raw_parent in enumerate(value["parents"]):
        parent = parents[Path(raw_parent["path"])]
        for name in temp_names:
            current = _child_stat(parent, name)
            if current is None:
                continue
            if not stat.S_ISREG(current.st_mode):
                raise RuntimeError(
                    f"Refusing to remove non-regular transaction control path: "
                    f"{parent.path / name}"
                )
            os.unlink(name, dir_fd=parent.directory_fd)
        # The journal is the discoverable recovery authority. Make every
        # temporary-control deletion durable before removing that authority;
        # otherwise a crash can expose an orphan active-phase temp that has no
        # journal from which to discover and recover the transaction.
        _fsync_parent(parent)
        _transaction_boundary(f"journal-temporaries-removed:{index}")
        current = _child_stat(parent, marker_name)
        if current is not None:
            if not stat.S_ISREG(current.st_mode):
                raise RuntimeError(
                    f"Refusing to remove non-regular transaction control path: "
                    f"{parent.path / marker_name}"
                )
            os.unlink(marker_name, dir_fd=parent.directory_fd)
            _fsync_parent(parent)
        _transaction_boundary(f"journal-removed:{index}")


def _recover_transaction(
    value: dict[str, Any], parents: Mapping[Path, _LockedParent]
) -> str:
    _validate_locked_journal(value, parents)
    marker_name = _journal_name(str(value["transaction_id"]))
    if not any(
        _child_stat(parents[Path(raw_parent["path"])], marker_name) is not None
        for raw_parent in value["parents"]
    ):
        # Failure while writing the first prepared replica cannot follow a
        # directory rename. Only transaction-owned temporary controls exist.
        _unlink_transaction_artifacts(value, parents)
        return "old"
    phase, copies = _phase_from_replicas(value, parents)
    canonical = copies[0]
    if len(copies) != len(canonical["parents"]):
        # A prepared transaction has not renamed a generation yet. Likewise,
        # cleaned and rolled-back replicas are only removed after their data
        # outcome is complete. Once one of those replicas is gone, a later
        # transaction can legitimately supersede that parent's generation
        # without seeing this transaction. The remaining controls therefore
        # carry cleanup intent only; revalidating or replaying their stale
        # generation identities would incorrectly block the independent
        # transaction (or, for rollback, try to undo it).
        if phase not in {"prepared", "cleaned", "rolled_back"}:
            raise RuntimeError("Active directory transaction lost a journal replica")
        marker_name = _journal_name(str(canonical["transaction_id"]))
        retained_parents = {
            Path(raw_parent["path"])
            for raw_parent in canonical["parents"]
            if _child_stat(parents[Path(raw_parent["path"])], marker_name) is not None
        }
        for raw_entry in canonical["entries"]:
            parent_path, _source, _target, _backup = _entry_paths(raw_entry)
            if parent_path in retained_parents:
                _validate_settled_generation(
                    raw_entry,
                    parents[parent_path],
                    committed=phase == "cleaned",
                )
        _unlink_transaction_artifacts(canonical, parents)
        return "new" if phase == "cleaned" else "old"
    if phase == "cleaned":
        _finish_committed_transaction(canonical, parents)
        _unlink_transaction_artifacts(canonical, parents)
        return "new"
    if phase in {"rolled_back", "prepared"}:
        _rollback_transaction(canonical, parents)
        _unlink_transaction_artifacts(canonical, parents)
        return "old"
    if phase == "applying":
        _rollback_transaction(canonical, parents)
        _write_journal_phase(canonical, "rolled_back", parents)
        _unlink_transaction_artifacts(canonical, parents)
        return "old"
    _finish_committed_transaction(canonical, parents)
    _write_journal_phase(canonical, "cleaned", parents)
    _unlink_transaction_artifacts(canonical, parents)
    return "new"


def _discover_journals(
    parents: Mapping[Path, _LockedParent],
) -> dict[str, dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {}
    names_by_parent: dict[Path, set[str]] = {}
    for parent in parents.values():
        names = set(os.listdir(parent.directory_fd))
        names_by_parent[parent.path] = names
        for name in sorted(names):
            if name == _DIRECTORY_LOCK_NAME or not name.startswith(
                _DIRECTORY_TRANSACTION_PREFIX
            ):
                continue
            if (
                _journal_transaction_id(name) is None
                and _journal_temp_identity(name) is None
            ):
                raise RuntimeError(
                    f"Malformed reserved directory transaction control: "
                    f"{parent.path / name}"
                )
        for name in sorted(names):
            transaction_id = _journal_transaction_id(name)
            if transaction_id is None:
                continue
            value = _read_child_json(parent, name)
            if value.get("transaction_id") != transaction_id:
                raise RuntimeError(
                    f"Directory transaction filename and content disagree: "
                    f"{parent.path / name}"
                )
            _journal_paths(value)
            existing = discovered.get(transaction_id)
            if existing is not None and _immutable_journal(
                existing
            ) != _immutable_journal(value):
                raise RuntimeError("Directory transaction journal replicas disagree")
            discovered[transaction_id] = value

    expected_backups = {
        Path(raw_entry["backup"])
        for value in discovered.values()
        for raw_entry in value["entries"]
    }
    for parent in parents.values():
        names = names_by_parent[parent.path]
        for name in sorted(names):
            temp_identity = _journal_temp_identity(name)
            if temp_identity is None:
                continue
            transaction_id, phase = temp_identity
            if _journal_name(transaction_id) in names:
                continue
            if phase != "prepared":
                raise RuntimeError(
                    f"Orphan active directory transaction control: {parent.path / name}"
                )
            current = _child_stat(parent, name)
            if current is None:
                continue
            if not stat.S_ISREG(current.st_mode):
                raise RuntimeError(
                    f"Orphan transaction journal is not a regular file: {parent.path / name}"
                )
            # A prepared temporary file is written before its visible journal.
            # No directory rename can have happened while only this file exists.
            os.unlink(name, dir_fd=parent.directory_fd)
            _fsync_parent(parent)
        for name in sorted(names):
            if ".posetestbot-backup." not in name:
                continue
            backup = parent.path / name
            if backup not in expected_backups:
                raise RuntimeError(
                    f"Orphan reserved directory transaction backup: {backup}"
                )
    return discovered


@contextmanager
def _locked_recovered_parents(
    initial_parents: set[Path],
) -> Iterator[dict[Path, _LockedParent]]:
    required = set(initial_parents)
    expected_identities: dict[Path, _DirectoryIdentity] = {}
    while True:
        with _locked_parents(
            required, expected_identities=expected_identities
        ) as parents:
            journals = _discover_journals(parents)
            expanded = set(required)
            for value in journals.values():
                declared, _replicas = _journal_paths(value)
                expanded.update(declared)
                for raw_parent in value["parents"]:
                    path = Path(raw_parent["path"])
                    identity = _DirectoryIdentity.from_json(
                        raw_parent["identity"], label="transaction parent"
                    )
                    existing = expected_identities.get(path)
                    if existing is not None and existing != identity:
                        raise RuntimeError(
                            "Directory transactions disagree on a parent identity: "
                            f"{path}"
                        )
                    expected_identities[path] = identity
            if expanded != required:
                required = expanded
                continue
            for transaction_id in sorted(journals):
                _recover_transaction(journals[transaction_id], parents)
            if _discover_journals(parents):
                raise RuntimeError("Directory transaction recovery left a live journal")
            yield parents
            return


def _build_transaction(
    pairs: list[tuple[Path, Path]], parents: Mapping[Path, _LockedParent]
) -> dict[str, Any]:
    transaction_id = uuid.uuid4().hex
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise RuntimeError("UUID provider returned an invalid transaction identifier")
    entries: list[dict[str, Any]] = []
    for index, (source, target) in enumerate(pairs):
        parent = parents[source.parent]
        source_id = _child_directory_identity(
            parent, source.name, label="Staging directory"
        )
        if source_id is None:
            raise FileNotFoundError(f"Staging directory does not exist: {source}")
        target_id = _child_directory_identity(
            parent, target.name, label="Destination directory"
        )
        backup = target.with_name(
            f".{target.name}.posetestbot-backup.{transaction_id}.{index}"
        )
        if _child_stat(parent, backup.name) is not None:
            raise FileExistsError(
                f"Directory transaction backup already exists: {backup}"
            )
        _fsync_directory_tree(source)
        entries.append(
            {
                "index": index,
                "parent": os.fspath(source.parent),
                "source": os.fspath(source),
                "target": os.fspath(target),
                "backup": os.fspath(backup),
                "new_identity": source_id.to_json(),
                "old_identity": None if target_id is None else target_id.to_json(),
            }
        )
    for raw_entry in entries:
        parent = parents[Path(raw_entry["parent"])]
        source_id, target_id, backup_id = _entry_identities(raw_entry, parent)
        if source_id != _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        ) or target_id != (
            None
            if raw_entry["old_identity"] is None
            else _DirectoryIdentity.from_json(
                raw_entry["old_identity"], label="old generation"
            )
        ):
            raise RuntimeError(
                "Directory generation changed while preparing transaction"
            )
        if backup_id is not None:
            raise RuntimeError("Directory transaction backup appeared while preparing")
    ordered_parents = sorted(parents.values(), key=lambda item: os.fspath(item.path))
    return {
        "schema_version": _DIRECTORY_TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "parents": [
            {"path": os.fspath(parent.path), "identity": parent.identity.to_json()}
            for parent in ordered_parents
        ],
        "entries": entries,
        "replicas": [
            os.fspath(parent.path / _journal_name(transaction_id))
            for parent in ordered_parents
        ],
    }


def _apply_transaction(
    value: dict[str, Any], parents: Mapping[Path, _LockedParent]
) -> None:
    _write_journal_phase(value, "prepared", parents)
    _write_journal_phase(value, "applying", parents)
    for raw_entry in value["entries"]:
        if raw_entry["old_identity"] is None:
            continue
        parent_path, _source, target, backup = _entry_paths(raw_entry)
        _replace_child(parents[parent_path], target.name, backup.name)
        _transaction_boundary(f"backup:{raw_entry['index']}")
    for raw_entry in value["entries"]:
        parent_path, source, target, _backup = _entry_paths(raw_entry)
        _replace_child(parents[parent_path], source.name, target.name)
        _transaction_boundary(f"promotion:{raw_entry['index']}")
    for raw_entry in value["entries"]:
        parent_path, _source, _target, _backup = _entry_paths(raw_entry)
        source_id, target_id, backup_id = _validate_generation_state(
            raw_entry, parents[parent_path], committed=False
        )
        new_id = _DirectoryIdentity.from_json(
            raw_entry["new_identity"], label="new generation"
        )
        old_value = raw_entry["old_identity"]
        old_id = (
            None
            if old_value is None
            else _DirectoryIdentity.from_json(old_value, label="old generation")
        )
        if source_id is not None or target_id != new_id or backup_id != old_id:
            raise RuntimeError("Directory transaction promotion is incomplete")
    _write_journal_phase(value, "committed", parents)
    _recover_transaction(value, parents)


def rename_path_no_replace(
    source: str | Path,
    destination: str | Path,
) -> Path:
    """Durably rename one regular file or directory without clobbering.

    Both parents must already exist, use no symlink ancestors, and reside on a
    filesystem that supports Linux ``renameat2(RENAME_NOREPLACE)``. The move is
    atomic, but this single-path helper is not a substitute for a journal when
    several moves must commit or roll back as one transaction.
    """

    requested_destination = Path(destination)
    source_path = _absolute_path(source)
    destination_path = _absolute_path(destination)
    if source_path == destination_path:
        raise ValueError("No-clobber rename source and destination must be distinct")
    for path in (source_path, destination_path):
        if (
            path.name == _DIRECTORY_LOCK_NAME
            or path.name.startswith(_DIRECTORY_TRANSACTION_PREFIX)
            or ".posetestbot-backup." in path.name
        ):
            raise ValueError(
                f"No-clobber rename path uses the reserved transaction namespace: {path}"
            )
    if destination_path.parent == source_path or destination_path.parent.is_relative_to(
        source_path
    ):
        raise ValueError("No-clobber rename destination must not be inside its source")

    parent_paths = {source_path.parent, destination_path.parent}
    with _locked_directory_parents(parent_paths) as parents:
        source_parent = parents[source_path.parent]
        destination_parent = parents[destination_path.parent]
        for parent in parents.values():
            _validate_locked_parent_path(parent)

        source_stat = _child_stat(source_parent, source_path.name)
        if source_stat is None:
            raise FileNotFoundError(
                f"No-clobber rename source does not exist: {source_path}"
            )
        if stat.S_ISLNK(source_stat.st_mode):
            raise ValueError(
                f"No-clobber rename source must not be a symlink: {source_path}"
            )
        if not stat.S_ISREG(source_stat.st_mode) and not stat.S_ISDIR(
            source_stat.st_mode
        ):
            raise ValueError(
                f"No-clobber rename source is not a regular file or directory: "
                f"{source_path}"
            )
        if _child_stat(destination_parent, destination_path.name) is not None:
            raise FileExistsError(
                f"No-clobber rename destination already exists: {destination_path}"
            )
        source_identity = _DirectoryIdentity.from_stat(source_stat)

        _rename_child_no_replace(
            source_parent,
            source_path.name,
            destination_parent,
            destination_path.name,
        )

        sync_error: OSError | None = None
        for parent in parents.values():
            try:
                _fsync_parent(parent)
            except OSError as exc:
                if sync_error is None:
                    sync_error = exc

        remaining_source = _child_stat(source_parent, source_path.name)
        installed = _child_stat(destination_parent, destination_path.name)
        if (
            remaining_source is not None
            or installed is None
            or _DirectoryIdentity.from_stat(installed) != source_identity
        ):
            raise RuntimeError(
                "No-clobber rename did not install exactly its expected source"
            ) from sync_error
        for parent in parents.values():
            _validate_locked_parent_path(parent)
        if sync_error is not None:
            raise sync_error
    return requested_destination


def replace_directory(staging: str | Path, destination: str | Path) -> Path:
    """Durably promote one complete sibling directory with crash recovery."""

    replace_directories([(staging, destination)])
    return Path(destination)


def replace_directories(
    promotions: Iterable[tuple[str | Path, str | Path]],
) -> list[Path]:
    """Durably promote one directory generation across one or more parents.

    Every source must be a real sibling of its destination. Overlapping
    replacements are serialized per parent. A process death before the durable
    commit record is recovered to the complete prior generation; a death after
    that record is recovered to the complete new generation.
    """

    requested = [(Path(source), Path(target)) for source, target in promotions]
    if not requested:
        return []
    pairs = [
        (_absolute_path(source), _absolute_path(target)) for source, target in requested
    ]
    sources = [source for source, _target in pairs]
    targets = [target for _source, target in pairs]
    if (
        len(set(sources)) != len(sources)
        or len(set(targets)) != len(targets)
        or set(sources) & set(targets)
    ):
        raise ValueError(
            "Directory promotion sources and destinations must be distinct and unique"
        )
    for source, target in pairs:
        if source.parent != target.parent:
            raise ValueError(
                f"Staging directory must be a sibling of its destination: {source}, {target}"
            )
        for path in (source, target):
            if (
                path.name == _DIRECTORY_LOCK_NAME
                or path.name.startswith(_DIRECTORY_TRANSACTION_PREFIX)
                or ".posetestbot-backup." in path.name
            ):
                raise ValueError(
                    f"Directory promotion path uses the reserved transaction namespace: {path}"
                )
    parent_paths = {source.parent for source, _target in pairs}
    for path in [*sources, *targets]:
        if any(
            parent == path or parent.is_relative_to(path) for parent in parent_paths
        ):
            raise ValueError(
                "Directory promotion paths must not contain another "
                f"participating parent: {path}"
            )
    with _locked_recovered_parents(parent_paths) as parents:
        # Recovery happens while the same locks are held. Recheck exact paths
        # afterwards so a recovered staged generation is accepted, but a link,
        # file, missing generation, or occupied reserved path still fails closed.
        operation_parents = {path: parents[path] for path in parent_paths}
        transaction = _build_transaction(pairs, operation_parents)
        _transaction_boundary("ready")
        try:
            _apply_transaction(transaction, operation_parents)
        except BaseException as original:
            try:
                outcome = _recover_transaction(transaction, operation_parents)
            except BaseException as recovery_error:
                raise RuntimeError(
                    "Directory replacement failed and durable recovery could not finish"
                ) from recovery_error
            if outcome == "new" and isinstance(original, Exception):
                return [target for _source, target in requested]
            raise original
    return [target for _source, target in requested]
