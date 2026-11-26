"""Serialize derived CLI work and recover abandoned staging directories."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path


def frame_worker_count(frame_count: int, *, limit: int) -> int:
    """Bound frame concurrency by the CPUs available to the current process."""

    cpu_count = (
        len(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else os.cpu_count() or 1
    )
    return min(frame_count, cpu_count, limit)


@contextmanager
def derived_stage_lock(run_root: Path, stage: str):
    lock = run_root / f".{stage}.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def sweep_staging(parent: Path, stem: str, suffix: str) -> None:
    pattern = re.compile(rf"\.{re.escape(stem)}\.[0-9a-f]{{32}}\.{re.escape(suffix)}")
    if parent.is_dir():
        for path in parent.iterdir():
            if pattern.fullmatch(path.name) and not path.is_symlink() and path.is_dir():
                shutil.rmtree(path)
