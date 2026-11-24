"""Crash-resistant helpers for replace-in-place text and JSON artifacts."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import signal
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


def atomic_write_text(path: str | Path, text: str, *, overwrite: bool = True) -> Path:
    """Write text through a same-directory temporary file and atomically replace."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "x", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
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
    overwrite: bool = True,
) -> Path:
    """Serialize JSON without allowing non-standard NaN/Infinity values."""

    text = json.dumps(
        value,
        indent=indent,
        sort_keys=sort_keys,
        default=default,
        allow_nan=False,
    )
    return atomic_write_text(path, f"{text}\n", overwrite=overwrite)


def _exchange_directories(source: Path, target: Path) -> bool:
    """Exchange two directory entries atomically when Linux and the mount support it."""

    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd, rename_exchange = -100, 2
    if (
        renameat2(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(target),
            rename_exchange,
        )
        == 0
    ):
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return False
    raise OSError(error, os.strerror(error), os.fspath(target))


@contextmanager
def _defer_promotion_signals():
    """Keep the CLI's cancellation handlers out of the rename and rollback sequence."""

    pending: list[int] = []
    if threading.current_thread() is not threading.main_thread():
        yield pending
        return

    def defer(signum, _frame):
        pending.append(signum)

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            if handler is not None and handler != signal.SIG_IGN:
                signal.signal(signum, defer)
                previous[signum] = handler
        yield pending
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        for signum in dict.fromkeys(pending):
            signal.raise_signal(signum)


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def replace_directory(staging: str | Path, destination: str | Path) -> Path:
    """Promote a complete sibling directory while preserving the old one on error."""

    return replace_directories([(staging, destination)])[0]


def replace_directories(
    promotions: Iterable[tuple[str | Path, str | Path]],
) -> list[Path]:
    """Promote several sibling directories as one rollback-capable operation."""

    pairs = [(Path(source), Path(target)) for source, target in promotions]
    if not pairs:
        return []
    sources = [source.resolve() for source, _target in pairs]
    targets = [target.resolve() for _source, target in pairs]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        raise ValueError("Directory promotion sources and destinations must be unique")
    if not set(sources).isdisjoint(targets):
        raise ValueError(
            "Directory promotion sources and destinations must be distinct"
        )
    for source, target in pairs:
        if not source.is_dir():
            raise FileNotFoundError(f"Staging directory does not exist: {source}")
        if (
            source.is_symlink()
            or target.is_symlink()
            or (target.exists() and not target.is_dir())
        ):
            raise ValueError("Directory promotion requires plain directories")
        if source.parent.resolve() != target.parent.resolve():
            raise ValueError(
                f"Staging directory must be a sibling of its destination: {source}, {target}"
            )

    previous_generations = []
    attempted = []
    with _defer_promotion_signals() as pending:
        try:
            for source, target in pairs:
                backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
                staged_identity = _directory_identity(source)
                if staged_identity is None:
                    raise FileNotFoundError(source)
                attempted.append((source, target, backup, staged_identity))
                if target.exists():
                    if _exchange_directories(source, target):
                        previous_generations.append(source)
                        continue
                    os.replace(target, backup)
                    previous_generations.append(backup)
                os.replace(source, target)
            if pending:
                raise InterruptedError("Directory promotion canceled")
        except BaseException as exc:
            for source, target, backup, staged_identity in reversed(attempted):
                try:
                    if _directory_identity(target) == staged_identity:
                        if source.exists():
                            if not _exchange_directories(source, target):
                                raise OSError(
                                    f"Could not reverse the directory exchange at {target}"
                                )
                        else:
                            os.replace(target, source)
                    if backup.exists():
                        os.replace(backup, target)
                except OSError as rollback_error:
                    exc.add_note(
                        f"Directory promotion rollback failed: {rollback_error}"
                    )
            raise

    for previous in previous_generations:
        shutil.rmtree(previous)
    return [target for _source, target in pairs]
