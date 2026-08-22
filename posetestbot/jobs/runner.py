"""Small process-backed job runner for local PoseTestBot commands."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import signal
import shlex
import shutil
import selectors
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - the lab/service runtime is Linux
    fcntl = None

from posetestbot.io.manifest import utc_now_iso
from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.supervisor import read_process_start_time


QUEUED = "queued"
RUNNING = "running"
CANCELING = "canceling"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELED = "canceled"
TERMINAL_STATUSES = {SUCCEEDED, FAILED, CANCELED}
OPERATOR_VISIBILITY = "operator"
SERVICE_VISIBILITY = "service"
JOB_VISIBILITIES = {OPERATOR_VISIBILITY, SERVICE_VISIBILITY}
DEFAULT_MAX_LOG_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TAIL_LINE_CHARS = 16 * 1024
DEFAULT_MAX_TAIL_CHARS = 256 * 1024
OUTPUT_READ_CHARS = 64 * 1024
TAIL_PERSIST_INTERVAL_SECONDS = 0.25
RUN_SCOPE = "run"
LIBRARY_SCOPE = "library"
GLOBAL_SCOPE = "global"
JOB_SCOPE_KINDS = {RUN_SCOPE, LIBRARY_SCOPE, GLOBAL_SCOPE}
DEFAULT_JOB_PAGE_LIMIT = 50
MAX_JOB_PAGE_LIMIT = 100
JOB_INDEX_FILENAME = "index.sqlite3"
JOB_INDEX_SCHEMA_VERSION = 1
RESOURCE_LOCK_FILENAME = ".resource-claims.lock"
JOB_STATE_LOCK_FILENAME = ".job-state.lock"
JOB_CANCEL_REQUEST_FILENAME = "cancel_request.json"
_RESOURCE_THREAD_LOCKS_GUARD = threading.Lock()
_RESOURCE_THREAD_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_JOB_THREAD_LOCKS_GUARD = threading.Lock()
_JOB_THREAD_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)


def _resource_thread_lock(job_root: Path) -> threading.RLock:
    key = Path(os.path.abspath(job_root)).as_posix()
    with _RESOURCE_THREAD_LOCKS_GUARD:
        return _RESOURCE_THREAD_LOCKS.setdefault(key, threading.RLock())


def _job_thread_lock(job_dir: Path) -> threading.RLock:
    key = Path(os.path.abspath(job_dir)).as_posix()
    with _JOB_THREAD_LOCKS_GUARD:
        return _JOB_THREAD_LOCKS.setdefault(key, threading.RLock())


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_stable_directory(path: Path, *, label: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    descriptor_stat = os.fstat(descriptor)
    try:
        pathname_stat = os.stat(path, follow_symlinks=False)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(descriptor_stat.st_mode) or not _same_inode(
        descriptor_stat,
        pathname_stat,
    ):
        os.close(descriptor)
        raise RuntimeError(f"{label} directory identity changed while opening: {path}")
    return descriptor, descriptor_stat


def _verify_locked_path(
    *,
    directory_path: Path,
    directory_fd: int,
    directory_stat: os.stat_result,
    lock_name: str,
    lock_stat: os.stat_result,
    label: str,
) -> None:
    try:
        current_directory = os.stat(directory_path, follow_symlinks=False)
        current_lock = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"{label} lock identity disappeared while waiting") from exc
    if not _same_inode(directory_stat, current_directory):
        raise RuntimeError(f"{label} directory identity changed while waiting")
    if not stat.S_ISREG(lock_stat.st_mode) or not _same_inode(lock_stat, current_lock):
        raise RuntimeError(f"{label} lock identity changed while waiting")


def _resolve_supervised_command(
    command: list[str],
    *,
    home: Path | None = None,
) -> list[str]:
    """Resolve uv from its supported user installs when a service PATH omits it."""
    resolved = list(command)
    if not resolved or resolved[0] != "uv" or shutil.which("uv") is not None:
        return resolved

    user_home = home if home is not None else Path.home()
    for relative_path in (Path(".local/bin/uv"), Path(".cargo/bin/uv")):
        candidate = user_home / relative_path
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved[0] = candidate.as_posix()
            break
    return resolved


@dataclass(kw_only=True)
class JobRecord:
    id: str
    name: str
    command: list[str]
    cwd: str | None
    status: str
    created_at: str
    log_path: str
    started_at: str | None = None
    ended_at: str | None = None
    returncode: int | None = None
    message: str | None = None
    tail: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    process_pid: int | None = None
    process_group_id: int | None = None
    process_start_time: int | None = None
    runner_pid: int | None = None
    runner_start_time: int | None = None
    supervisor_pid: int | None = None
    supervisor_process_group_id: int | None = None
    supervisor_start_time: int | None = None
    visibility: str = OPERATOR_VISIBILITY
    scope_kind: str
    run_root: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class JobPage:
    jobs: list[JobRecord]
    total: int
    status_counts: dict[str, int]
    next_cursor: str | None


class LocalJobRunner:
    """Run structured command arrays in background threads and keep job logs."""

    def __init__(
        self,
        job_root: str | Path,
        *,
        tail_limit: int = 200,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        max_tail_line_chars: int = DEFAULT_MAX_TAIL_LINE_CHARS,
        max_tail_chars: int = DEFAULT_MAX_TAIL_CHARS,
    ):
        if tail_limit < 1:
            raise ValueError("tail_limit must be at least 1")
        if max_log_bytes < 1024:
            raise ValueError("max_log_bytes must be at least 1024")
        if max_tail_line_chars < 64:
            raise ValueError("max_tail_line_chars must be at least 64")
        if max_tail_chars < 64:
            raise ValueError("max_tail_chars must be at least 64")
        self.job_root = Path(job_root)
        self.index_path = self.job_root / JOB_INDEX_FILENAME
        self.tail_limit = tail_limit
        self.max_log_bytes = max_log_bytes
        self.max_tail_line_chars = max_tail_line_chars
        self.max_tail_chars = max_tail_chars
        self.job_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._local_job_ids: set[str] = set()
        self._pending_terminal_jobs: dict[str, tuple[JobRecord, bool]] = {}
        self._runner_pid = os.getpid()
        self._runner_start_time = self._read_process_start_time(self._runner_pid)
        self._ensure_index()
        self._load_persisted_jobs()

    def submit(
        self,
        *,
        name: str,
        command: list[str],
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        resources: list[str] | None = None,
        parameters: Mapping[str, object] | None = None,
        scope_kind: str,
        run_root: str | Path | None = None,
        visibility: str = OPERATOR_VISIBILITY,
    ) -> JobRecord:
        if not command:
            raise ValueError("Job command must not be empty")
        if visibility not in JOB_VISIBILITIES:
            raise ValueError(
                f"visibility must be one of: {', '.join(sorted(JOB_VISIBILITIES))}"
            )
        normalized_run_root = self._validate_scope(scope_kind, run_root)

        raw_resources = list(resources or [])
        if not all(
            isinstance(resource, str)
            and resource.strip() == resource
            and bool(resource)
            and all(resource.split(":"))
            for resource in raw_resources
        ):
            raise ValueError(
                "Job resources must be trimmed strings with non-empty hierarchy segments"
            )
        requested_resources = sorted(set(raw_resources))
        self._reconcile_pending_terminal_jobs()
        with self._resource_transaction():
            with self._lock:
                self._check_resources_available(requested_resources)
                job_id = uuid.uuid4().hex[:12]
                job_dir = self.job_root / job_id
                job_dir.mkdir(parents=True, exist_ok=False)
                job = JobRecord(
                    id=job_id,
                    name=name,
                    command=list(command),
                    cwd=Path(cwd).as_posix() if cwd is not None else None,
                    status=QUEUED,
                    created_at=utc_now_iso(),
                    log_path=(job_dir / "log.txt").as_posix(),
                    resources=requested_resources,
                    parameters=dict(parameters or {}),
                    runner_pid=self._runner_pid,
                    runner_start_time=self._runner_start_time,
                    visibility=visibility,
                    scope_kind=scope_kind,
                    run_root=normalized_run_root,
                )
                try:
                    self._jobs[job_id] = job
                    self._local_job_ids.add(job_id)
                    self._persist_job(job)
                except BaseException:
                    self._jobs.pop(job_id, None)
                    self._local_job_ids.discard(job_id)
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, dict(env or {})),
            name=f"posetestbot-job-{job_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[job_id] = thread
        try:
            thread.start()
        except BaseException as exc:
            with self._lock:
                self._threads.pop(job_id, None)
                job = self._jobs[job_id]
                terminal = JobRecord(**job.to_dict())
                terminal.status = FAILED
                terminal.ended_at = utc_now_iso()
                terminal.message = (
                    "Job worker thread could not start: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._append_tail(terminal, terminal.message)
            self._defer_or_publish_terminal_job(job_id, terminal)
            raise
        return self.get(job_id)

    def resource_holders(self, *, include_services: bool = False) -> dict[str, str]:
        self._reconcile_pending_terminal_jobs()
        with self._resource_transaction():
            with self._lock:
                return self._resource_holders(include_services=include_services)

    def get(self, job_id: str) -> JobRecord:
        self._refresh_foreign_jobs(job_id=job_id)
        with self._lock:
            has_pending_terminal = job_id in self._pending_terminal_jobs
        if has_pending_terminal:
            self._reconcile_pending_terminal_jobs()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                job = self._load_indexed_job(job_id)
            return JobRecord(**job.to_dict())

    def list(self, *, include_services: bool = True) -> list[JobRecord]:
        records: list[JobRecord] = []
        cursor = None
        while True:
            page = self.list_page(
                limit=MAX_JOB_PAGE_LIMIT,
                cursor=cursor,
                include_services=include_services,
            )
            records.extend(page.jobs)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return records

    def list_page(
        self,
        *,
        limit: int = DEFAULT_JOB_PAGE_LIMIT,
        cursor: str | None = None,
        search: str | None = None,
        statuses: list[str] | tuple[str, ...] | set[str] | None = None,
        scope_kinds: list[str] | tuple[str, ...] | set[str] | None = None,
        run_root: str | Path | None = None,
        include_services: bool = True,
    ) -> JobPage:
        """Return active work plus one stable keyset page of terminal history."""

        self._refresh_foreign_jobs()

        if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_JOB_PAGE_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {MAX_JOB_PAGE_LIMIT}")
        limit = int(limit)
        normalized_statuses = self._normalize_filter_values(statuses)
        normalized_scopes = self._normalize_filter_values(scope_kinds)
        invalid_scopes = set(normalized_scopes) - JOB_SCOPE_KINDS
        if invalid_scopes:
            raise ValueError(
                "scope_kind must contain only: " + ", ".join(sorted(JOB_SCOPE_KINDS))
            )
        normalized_run_root = (
            Path(run_root).resolve().as_posix() if run_root is not None else None
        )
        normalized_search = (search or "").strip().lower()
        filter_signature = self._page_filter_signature(
            search=normalized_search,
            statuses=normalized_statuses,
            scope_kinds=normalized_scopes,
            run_root=normalized_run_root,
            include_services=include_services,
        )
        after = self._decode_cursor(cursor, filter_signature) if cursor else None

        with self._lock:
            self._ensure_index()
            base_where, base_parameters = self._index_filters(
                search=normalized_search,
                scope_kinds=normalized_scopes,
                run_root=normalized_run_root,
                include_services=include_services,
            )
            where = list(base_where)
            parameters = list(base_parameters)
            if normalized_statuses:
                placeholders = ", ".join("?" for _ in normalized_statuses)
                where.append(f"status IN ({placeholders})")
                parameters.extend(normalized_statuses)
            where_sql = " AND ".join(where) if where else "1 = 1"

            with self._index_connection() as connection:
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM jobs WHERE {where_sql}",
                        parameters,
                    ).fetchone()[0]
                )
                counts_where_sql = " AND ".join(base_where) if base_where else "1 = 1"
                status_counts = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) FROM jobs "
                        f"WHERE {counts_where_sql} GROUP BY status",
                        base_parameters,
                    )
                }

                terminal_where = [
                    *where,
                    "status IN (?, ?, ?)",
                ]
                terminal_parameters = [
                    *parameters,
                    SUCCEEDED,
                    FAILED,
                    CANCELED,
                ]
                if after is not None:
                    terminal_where.append(
                        "(created_at < ? OR (created_at = ? AND id < ?))"
                    )
                    terminal_parameters.extend([after[0], after[0], after[1]])
                terminal_rows = connection.execute(
                    "SELECT id, created_at FROM jobs WHERE "
                    + " AND ".join(terminal_where)
                    + " ORDER BY created_at DESC, id DESC LIMIT ?",
                    [*terminal_parameters, limit + 1],
                ).fetchall()

            active_jobs: list[JobRecord] = []
            if cursor is None:
                active_jobs = sorted(
                    (
                        JobRecord(**job.to_dict())
                        for job in self._jobs.values()
                        if job.status not in TERMINAL_STATUSES
                        and self._record_matches_filters(
                            job,
                            search=normalized_search,
                            statuses=normalized_statuses,
                            scope_kinds=normalized_scopes,
                            run_root=normalized_run_root,
                            include_services=include_services,
                        )
                    ),
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )

            has_more = len(terminal_rows) > limit
            page_rows = terminal_rows[:limit]
            terminal_jobs = [
                JobRecord(**self._load_indexed_job(str(row[0])).to_dict())
                for row in page_rows
            ]
            next_cursor = None
            if has_more and page_rows:
                last = page_rows[-1]
                next_cursor = self._encode_cursor(
                    created_at=str(last[1]),
                    job_id=str(last[0]),
                    filter_signature=filter_signature,
                )
            return JobPage(
                jobs=[*active_jobs, *terminal_jobs],
                total=total,
                status_counts=status_counts,
                next_cursor=next_cursor,
            )

    def wait(self, job_id: str, timeout: float | None = None) -> JobRecord:
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout=timeout)
        return self.get(job_id)

    def cancel(self, job_id: str) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                job = self._load_indexed_job(job_id)
                if job.status not in TERMINAL_STATUSES:
                    self._jobs[job_id] = job
            if job.status in TERMINAL_STATUSES:
                return JobRecord(**job.to_dict())
            process = self._processes.get(job_id)
            snapshot = JobRecord(**job.to_dict())

        try:
            requested = self._request_cancellation(snapshot)
        except BaseException:
            if process is not None:
                try:
                    self._terminate_process_group(process)
                except BaseException:
                    pass
            else:
                try:
                    persisted = self._load_job_with_supervisor_identity(
                        job_id,
                        wait_s=0.0,
                    )
                    self._terminate_persisted_process_group(persisted)
                except BaseException:
                    pass
            raise
        with self._lock:
            local = self._jobs.get(job_id)
            if local is not None:
                local.status = requested.status
                local.ended_at = requested.ended_at
                local.message = requested.message
                if not local.tail or local.tail[-1] != "Cancellation requested.":
                    self._append_tail(local, "Cancellation requested.")

        stopped = requested.status == CANCELED
        if not stopped and process is not None:
            stopped = self._terminate_process_group(process)
        elif not stopped:
            persisted = self._load_job_with_supervisor_identity(job_id, wait_s=2.0)
            stopped = self._terminate_persisted_process_group(persisted)

        if stopped and requested.status != CANCELED:
            self._finish_verified_cancellation(job_id, process=process)
        return self.get(job_id)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop all locally owned groups, escalating once the grace period ends."""

        with self._lock:
            active = [
                JobRecord(**job.to_dict())
                for job in self._jobs.values()
                if job.id in self._local_job_ids and job.status not in TERMINAL_STATUSES
            ]
        active_ids = [job.id for job in active]
        processes: dict[str, subprocess.Popen] = {}
        for snapshot in active:
            try:
                requested = self._request_cancellation(snapshot)
            except BaseException:
                requested = JobRecord(**snapshot.to_dict())
                requested.status = (
                    CANCELED if requested.status == QUEUED else CANCELING
                )
                requested.ended_at = (
                    utc_now_iso() if requested.status == CANCELED else None
                )
            with self._lock:
                job = self._jobs.get(snapshot.id)
                if job is None:
                    continue
                job.status = requested.status
                job.ended_at = requested.ended_at
                job.message = "Shutdown requested."
                self._append_tail(job, job.message)
                process = self._processes.get(snapshot.id)
                if process is not None and process.poll() is None:
                    processes[snapshot.id] = process

        for process in processes.values():
            self._signal_supervisor(process, signal.SIGTERM)

        deadline = time.monotonic() + max(timeout, 0.0)
        for job_id in active_ids:
            with self._lock:
                thread = self._threads.get(job_id)
            if thread is None:
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for process in processes.values():
            if process.poll() is None:
                self._terminate_process_group(process, timeout_s=1.0)
        for job_id in active_ids:
            with self._lock:
                thread = self._threads.get(job_id)
            if thread is not None:
                thread.join(timeout=1.0)
        self._reconcile_pending_terminal_jobs()

    def log_text(self, job_id: str) -> str:
        job = self.get(job_id)
        log_path = Path(job.log_path)
        if not log_path.is_file():
            return ""
        return log_path.read_text()

    def _run_job(self, job_id: str, env: dict[str, str]) -> None:
        try:
            self._run_job_inner(job_id, env)
        except BaseException as exc:
            self._fail_job_after_runner_error(job_id, exc)

    def _run_job_inner(self, job_id: str, env: dict[str, str]) -> None:
        canceled_before_start: JobRecord | None = None
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {CANCELED, CANCELING}:
                canceled_before_start = JobRecord(**job.to_dict())
                canceled_before_start.status = CANCELED
                canceled_before_start.ended_at = (
                    canceled_before_start.ended_at or utc_now_iso()
                )
                canceled_before_start.message = "Canceled."
            else:
                job.status = RUNNING
                job.started_at = utc_now_iso()
                self._persist_job(job)
                if job.status in {CANCELED, CANCELING}:
                    canceled_before_start = JobRecord(**job.to_dict())
                    canceled_before_start.status = CANCELED
                    canceled_before_start.ended_at = (
                        canceled_before_start.ended_at or utc_now_iso()
                    )
                    canceled_before_start.message = "Canceled."
        if canceled_before_start is not None:
            self._defer_or_publish_terminal_job(job_id, canceled_before_start)
            return

        with open(job.log_path, "ab", buffering=0) as log:
            log_bytes = log.tell()
            log_truncated = log_bytes >= self.max_log_bytes

            def write_log(value: str) -> None:
                nonlocal log_bytes, log_truncated
                if log_truncated:
                    return
                encoded = value.encode("utf-8", errors="replace")
                marker = (
                    f"\n[PoseTestBot job log truncated at {self.max_log_bytes} bytes]\n"
                ).encode("utf-8")
                data_limit = max(0, self.max_log_bytes - len(marker))
                remaining = data_limit - log_bytes
                if len(encoded) <= remaining:
                    log.write(encoded)
                    log_bytes += len(encoded)
                    return
                if remaining > 0:
                    log.write(encoded[:remaining])
                    log_bytes += remaining
                marker_remaining = self.max_log_bytes - log_bytes
                if marker_remaining > 0:
                    log.write(marker[:marker_remaining])
                    log_bytes += min(len(marker), marker_remaining)
                log_truncated = True

            write_log(f"$ {self._format_command(job.command)}\n")
            try:
                identity_path = Path(job.log_path).parent / "supervisor.json"
                cancel_request_path = (
                    Path(job.log_path).parent / JOB_CANCEL_REQUEST_FILENAME
                )
                supervised_command = _resolve_supervised_command(job.command)
                if supervised_command != job.command:
                    write_log(
                        "[PoseTestBot] Resolved uv outside the service PATH: "
                        f"{supervised_command[0]}\n"
                    )
                supervisor_command = [
                    sys.executable,
                    "-m",
                    "posetestbot.jobs.supervisor",
                    "--owner-pid",
                    str(self._runner_pid),
                    "--owner-start-time",
                    str(self._runner_start_time),
                    "--identity-path",
                    identity_path.as_posix(),
                    "--cancel-request-path",
                    cancel_request_path.as_posix(),
                    "--termination-timeout",
                    "5",
                    "--",
                    *supervised_command,
                ]
                process = subprocess.Popen(
                    supervisor_command,
                    cwd=job.cwd,
                    env={**os.environ, **env},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=(os.name != "nt"),
                )
            except Exception as exc:
                with self._lock:
                    job = self._jobs[job_id]
                    terminal = JobRecord(**job.to_dict())
                    terminal.status = FAILED
                    terminal.ended_at = utc_now_iso()
                    terminal.message = f"{type(exc).__name__}: {exc}"
                    self._append_tail(terminal, terminal.message)
                self._defer_or_publish_terminal_job(job_id, terminal)
                return

            with self._lock:
                self._processes[job_id] = process
                current = self._jobs[job_id]
                current.supervisor_pid = process.pid
                current.supervisor_process_group_id = (
                    os.getpgid(process.pid) if os.name != "nt" else process.pid
                )
                current.supervisor_start_time = self._read_process_start_time(
                    process.pid
                )
                self._persist_job(current)
                should_terminate = self._jobs[job_id].status in {
                    CANCELED,
                    CANCELING,
                }

            if should_terminate:
                self._terminate_process_group(process)

            self._refresh_supervisor_identity(job_id, wait_s=2.0)

            assert process.stdout is not None
            pending_tail = ""
            pending_tail_truncated = False
            last_tail_persisted_at = time.monotonic()

            def consume_tail_fragment(fragment: str) -> None:
                nonlocal pending_tail
                nonlocal pending_tail_truncated
                nonlocal last_tail_persisted_at
                pieces = fragment.split("\n")
                for index, piece in enumerate(pieces):
                    has_newline = index < len(pieces) - 1
                    value = f"{piece}\n" if has_newline else piece
                    room = max(0, self.max_tail_line_chars - len(pending_tail))
                    if room > 0:
                        pending_tail += value[:room]
                    if len(value) > room:
                        pending_tail_truncated = True
                    if not has_newline:
                        continue
                    line = pending_tail.rstrip("\r\n")
                    if pending_tail_truncated:
                        line += "… [line truncated]"
                    with self._lock:
                        current = self._jobs[job_id]
                        self._append_tail(current, line)
                        now = time.monotonic()
                        if (
                            now - last_tail_persisted_at
                            >= TAIL_PERSIST_INTERVAL_SECONDS
                        ):
                            self._persist_job(current)
                            last_tail_persisted_at = now
                    pending_tail = ""
                    pending_tail_truncated = False

            for fragment in self._iter_supervisor_output(process):
                write_log(fragment)
                consume_tail_fragment(fragment)

            if pending_tail or pending_tail_truncated:
                line = pending_tail.rstrip("\r\n")
                if pending_tail_truncated:
                    line += "… [line truncated]"
                with self._lock:
                    current = self._jobs[job_id]
                    self._append_tail(current, line)

            returncode = process.wait()
            workload_cleanup = self._cleanup_recorded_workload(job_id, timeout_s=1.0)
            if workload_cleanup is False or (
                returncode < 0 and workload_cleanup is not True
            ):
                raise RuntimeError(
                    "Supervisor exited without verified workload-group cleanup"
                )
            with self._lock:
                job = self._jobs[job_id]
                terminal = JobRecord(**job.to_dict())
                terminal.returncode = returncode
                terminal.ended_at = utc_now_iso()
                if terminal.status in {CANCELED, CANCELING}:
                    terminal.status = CANCELED
                    terminal.message = "Canceled."
                elif returncode == 0:
                    terminal.status = SUCCEEDED
                    terminal.message = "Command completed successfully."
                else:
                    terminal.status = FAILED
                    terminal.message = f"Command exited with status {returncode}."
                self._append_tail(terminal, terminal.message)
            self._defer_or_publish_terminal_job(job_id, terminal)

    def _fail_job_after_runner_error(
        self,
        job_id: str,
        exc: BaseException,
    ) -> None:
        """Stop spawned work and release a claim after an internal runner fault."""

        with self._lock:
            process = self._processes.get(job_id)
        stop_verified = process is None
        if process is not None:
            for attempt in range(3):
                try:
                    stop_verified = self._terminate_process_group(process)
                except BaseException:
                    stop_verified = False
                if stop_verified:
                    break
                if attempt < 2:
                    time.sleep(0.05)

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                if stop_verified:
                    self._processes.pop(job_id, None)
                    self._threads.pop(job_id, None)
                return
            terminal = JobRecord(**job.to_dict())
            terminal.returncode = process.returncode if process is not None else None
            terminal.ended_at = utc_now_iso()
            if terminal.status in {CANCELED, CANCELING}:
                terminal.status = CANCELED
                terminal.message = "Canceled after an internal job runner failure."
            else:
                terminal.status = FAILED
                terminal.message = (
                    "Internal job runner failure: "
                    f"{type(exc).__name__}: {exc}"
                )
            self._append_tail(terminal, terminal.message)
            if not stop_verified:
                job.message = (
                    "Internal job runner failure; process termination has not been "
                    "verified and the resource claim is retained: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._append_tail(job, job.message)
                self._threads.pop(job_id, None)
                self._pending_terminal_jobs[job_id] = (terminal, True)
                try:
                    self._persist_job(job)
                except BaseException:
                    pass
                return
        self._defer_or_publish_terminal_job(job_id, terminal)

    @staticmethod
    def _iter_supervisor_output(process: subprocess.Popen) -> Iterator[str]:
        """Yield output without hanging when an orphan keeps the pipe open."""

        assert process.stdout is not None
        if os.name == "nt":  # pragma: no cover - the service runtime is Linux
            while True:
                fragment = process.stdout.readline(OUTPUT_READ_CHARS)
                if not fragment:
                    return
                yield fragment

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        descriptor = process.stdout.fileno()
        exited_at: float | None = None
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                if process.poll() is not None and exited_at is None:
                    exited_at = time.monotonic()
                timeout = 0.0 if exited_at is not None else 0.1
                events = selector.select(timeout)
                if not events:
                    if exited_at is not None:
                        break
                    continue
                payload = os.read(descriptor, OUTPUT_READ_CHARS)
                if not payload:
                    break
                fragment = decoder.decode(payload)
                if fragment:
                    yield fragment
                # Drain bytes already buffered at supervisor exit, but do not let a
                # surviving descendant keep this worker blocked on the shared pipe.
                if exited_at is not None and time.monotonic() - exited_at >= 0.1:
                    break
        final_fragment = decoder.decode(b"", final=True)
        if final_fragment:
            yield final_fragment

    def _append_tail(self, job: JobRecord, line: str) -> None:
        job.tail.append(self._bounded_tail_line(line))
        if len(job.tail) > self.tail_limit:
            del job.tail[: len(job.tail) - self.tail_limit]
        while (
            len(job.tail) > 1
            and sum(len(item) for item in job.tail) > self.max_tail_chars
        ):
            del job.tail[0]

    def _bounded_tail_line(self, line: str) -> str:
        limit = min(self.max_tail_line_chars, self.max_tail_chars)
        if len(line) <= limit:
            return line
        suffix = "… [line truncated]"
        return line[: max(0, limit - len(suffix))] + suffix

    @staticmethod
    def _adopt_job_record(destination: JobRecord, source: JobRecord) -> None:
        for item in fields(JobRecord):
            setattr(destination, item.name, getattr(source, item.name))

    @contextmanager
    def _job_state_transaction(self, job_dir: Path) -> Iterator[None]:
        """Serialize one job's state transitions across runner processes."""

        thread_lock = _job_thread_lock(job_dir)
        with thread_lock:
            directory_fd, directory_stat = _open_stable_directory(
                job_dir,
                label="Job state",
            )
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(
                    JOB_STATE_LOCK_FILENAME,
                    flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except BaseException:
                os.close(directory_fd)
                raise
            try:
                descriptor_stat = os.fstat(descriptor)
                if not stat.S_ISREG(descriptor_stat.st_mode):
                    raise RuntimeError("Job state lock must be a regular file")
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                _verify_locked_path(
                    directory_path=job_dir,
                    directory_fd=directory_fd,
                    directory_stat=directory_stat,
                    lock_name=JOB_STATE_LOCK_FILENAME,
                    lock_stat=descriptor_stat,
                    label="Job state",
                )
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                os.close(directory_fd)

    def _read_authoritative_job(self, job_dir: Path, job_id: str) -> JobRecord:
        path = job_dir / "job.json"
        if path.is_symlink() or job_dir.is_symlink():
            raise RuntimeError(f"Persisted job state path is a symlink: {path}")
        try:
            with open(path, encoding="utf-8") as handle:
                job = self._job_from_dict(json.load(handle))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot verify persisted job state: {path}") from exc
        if job.id != job_id:
            raise RuntimeError(f"Persisted job identity does not match: {path}")
        if Path(job.log_path).resolve() != (job_dir / "log.txt").resolve():
            raise RuntimeError(f"Persisted job log path escapes its directory: {path}")
        self._validate_process_identities(job)
        return job

    @staticmethod
    def _validate_process_identities(job: JobRecord) -> None:
        for field_name in (
            "runner_pid",
            "runner_start_time",
            "supervisor_pid",
            "supervisor_process_group_id",
            "supervisor_start_time",
            "process_pid",
            "process_group_id",
            "process_start_time",
        ):
            value = getattr(job, field_name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"Persisted job {field_name} identity is invalid")
        for prefix, values in (
            ("runner", (job.runner_pid, job.runner_start_time)),
            (
                "supervisor",
                (
                    job.supervisor_pid,
                    job.supervisor_process_group_id,
                    job.supervisor_start_time,
                ),
            ),
            (
                "workload",
                (job.process_pid, job.process_group_id, job.process_start_time),
            ),
        ):
            populated = [value is not None for value in values]
            if any(populated) and not all(populated):
                raise ValueError(f"Persisted job {prefix} identity is incomplete")
        if os.name != "nt":
            if (
                job.supervisor_pid is not None
                and job.supervisor_process_group_id != job.supervisor_pid
            ):
                raise ValueError("Persisted supervisor group is not a dedicated session")
            if job.process_pid is not None and job.process_group_id != job.process_pid:
                raise ValueError("Persisted workload group is not a dedicated session")

    def _cancellation_requested(self, job_dir: Path, job_id: str) -> bool:
        path = job_dir / JOB_CANCEL_REQUEST_FILENAME
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(path_stat.st_mode):
            raise RuntimeError(f"Job cancellation request is not a regular file: {path}")
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot verify job cancellation request: {path}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != "job_cancel_request.v1"
            or value.get("job_id") != job_id
        ):
            raise RuntimeError(f"Job cancellation request is invalid: {path}")
        return True

    def _merge_durable_job_state(
        self,
        candidate: JobRecord,
        *,
        existing: JobRecord | None,
        cancellation_requested: bool,
    ) -> JobRecord:
        effective = JobRecord(**candidate.to_dict())
        if existing is not None and existing.status in TERMINAL_STATUSES:
            return existing
        canceling = cancellation_requested or (
            existing is not None and existing.status == CANCELING
        )
        if not canceling:
            return effective

        if effective.status in TERMINAL_STATUSES:
            effective.status = CANCELED
            effective.ended_at = effective.ended_at or utc_now_iso()
            effective.message = "Canceled."
            return effective

        has_spawned_identity = any(
            value is not None
            for value in (
                effective.supervisor_pid,
                effective.process_pid,
                existing.supervisor_pid if existing is not None else None,
                existing.process_pid if existing is not None else None,
            )
        )
        if effective.status == QUEUED or not has_spawned_identity:
            effective.status = CANCELED
            effective.ended_at = effective.ended_at or utc_now_iso()
            effective.message = "Canceled."
        else:
            effective.status = CANCELING
            effective.ended_at = None
            effective.message = "Cancellation requested."
        return effective

    def _write_authoritative_job(self, path: Path, job: JobRecord) -> None:
        self._validate_process_identities(job)
        atomic_write_json(path, job.to_dict())
        try:
            self._upsert_index(path, job)
        except Exception:
            # The SQLite index is a rebuildable view.  Authoritative job state has
            # already committed and must not be mistaken for a lifecycle failure.
            pass

    def _request_cancellation(self, snapshot: JobRecord) -> JobRecord:
        job_dir = Path(snapshot.log_path).parent
        with self._job_state_transaction(job_dir):
            current = self._read_authoritative_job(job_dir, snapshot.id)
            if current.status in TERMINAL_STATUSES:
                return current
            request_path = job_dir / JOB_CANCEL_REQUEST_FILENAME
            if not self._cancellation_requested(job_dir, current.id):
                atomic_write_json(
                    request_path,
                    {
                        "schema_version": "job_cancel_request.v1",
                        "job_id": current.id,
                        "requested_at": utc_now_iso(),
                        "requester_pid": self._runner_pid,
                        "requester_start_time": self._runner_start_time,
                    },
                )
            requested = JobRecord(**current.to_dict())
            requested.status = CANCELED if current.status == QUEUED else CANCELING
            requested.ended_at = utc_now_iso() if requested.status == CANCELED else None
            requested.message = (
                "Canceled." if requested.status == CANCELED else "Cancellation requested."
            )
            if not requested.tail or requested.tail[-1] != "Cancellation requested.":
                self._append_tail(requested, "Cancellation requested.")
            self._write_authoritative_job(job_dir / "job.json", requested)
            return requested

    def _load_job_with_supervisor_identity(
        self,
        job_id: str,
        *,
        wait_s: float = 0.0,
    ) -> JobRecord:
        job_dir = self.job_root / job_id
        deadline = time.monotonic() + max(wait_s, 0.0)
        while True:
            with self._job_state_transaction(job_dir):
                job = self._read_authoritative_job(job_dir, job_id)
                identity_path = job_dir / "supervisor.json"
                self._merge_supervisor_identity(job, identity_path, strict=True)
            if (
                job.supervisor_pid is not None
                or job.status in TERMINAL_STATUSES
                or time.monotonic() >= deadline
            ):
                return job
            time.sleep(0.01)

    def _finish_verified_cancellation(
        self,
        job_id: str,
        *,
        process: subprocess.Popen | None,
    ) -> None:
        job_dir = self.job_root / job_id
        deferred = False
        with self._job_state_transaction(job_dir):
            current = self._read_authoritative_job(job_dir, job_id)
            if current.status in TERMINAL_STATUSES:
                terminal = current
            else:
                terminal = JobRecord(**current.to_dict())
                terminal.status = CANCELED
                terminal.ended_at = utc_now_iso()
                terminal.returncode = (
                    process.returncode if process is not None else terminal.returncode
                )
                terminal.message = "Canceled."
                self._append_tail(terminal, terminal.message)
                try:
                    self._write_authoritative_job(job_dir / "job.json", terminal)
                except BaseException:
                    deferred = True
        if deferred:
            cleanup = job_id not in self._local_job_ids
            with self._lock:
                self._pending_terminal_jobs[job_id] = (terminal, cleanup)
            return
        with self._lock:
            local = self._jobs.get(job_id)
            if local is not None:
                self._adopt_job_record(local, terminal)
            if job_id not in self._local_job_ids:
                self._jobs.pop(job_id, None)

    def _defer_or_publish_terminal_job(
        self,
        job_id: str,
        terminal: JobRecord,
        *,
        cleanup: bool = True,
    ) -> bool:
        try:
            self._persist_job(terminal)
        except BaseException:
            with self._lock:
                self._pending_terminal_jobs[job_id] = (terminal, cleanup)
            return False
        with self._lock:
            self._pending_terminal_jobs.pop(job_id, None)
            local = self._jobs.get(job_id)
            if local is not None:
                self._adopt_job_record(local, terminal)
            if cleanup:
                self._processes.pop(job_id, None)
                self._threads.pop(job_id, None)
                self._jobs.pop(job_id, None)
                self._local_job_ids.discard(job_id)
        return True

    def _reconcile_pending_terminal_jobs(self) -> None:
        with self._lock:
            pending = [
                (job_id, JobRecord(**terminal.to_dict()), cleanup)
                for job_id, (terminal, cleanup) in self._pending_terminal_jobs.items()
            ]
        for job_id, terminal, cleanup in pending:
            with self._lock:
                process = self._processes.get(job_id)
            if process is not None:
                try:
                    if not self._terminate_process_group(process):
                        continue
                except BaseException:
                    continue
            self._defer_or_publish_terminal_job(
                job_id,
                terminal,
                cleanup=cleanup,
            )

    def _refresh_foreign_jobs(self, *, job_id: str | None = None) -> None:
        with self._lock:
            foreign_ids = [
                item_id
                for item_id in self._jobs
                if item_id not in self._local_job_ids
                and (job_id is None or item_id == job_id)
            ]
        for item_id in foreign_ids:
            job_dir = self.job_root / item_id
            with self._job_state_transaction(job_dir):
                current = self._read_authoritative_job(job_dir, item_id)
            with self._lock:
                if item_id in self._local_job_ids:
                    continue
                if current.status in TERMINAL_STATUSES:
                    self._jobs.pop(item_id, None)
                else:
                    self._jobs[item_id] = current

    @contextmanager
    def _resource_transaction(self) -> Iterator[None]:
        """Serialize resource discovery and job publication across runners."""

        thread_lock = _resource_thread_lock(self.job_root)
        with thread_lock:
            directory_fd, directory_stat = _open_stable_directory(
                self.job_root,
                label="Resource claim",
            )
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(
                    RESOURCE_LOCK_FILENAME,
                    flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except BaseException:
                os.close(directory_fd)
                raise
            try:
                descriptor_stat = os.fstat(descriptor)
                if not stat.S_ISREG(descriptor_stat.st_mode):
                    raise RuntimeError("Resource claim lock must be a regular file")
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                _verify_locked_path(
                    directory_path=self.job_root,
                    directory_fd=directory_fd,
                    directory_stat=directory_stat,
                    lock_name=RESOURCE_LOCK_FILENAME,
                    lock_stat=descriptor_stat,
                    label="Resource claim",
                )
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                os.close(directory_fd)

    def _persisted_active_jobs(self) -> list[JobRecord]:
        """Load active claims and reclaim jobs whose verified owner disappeared."""

        active: list[JobRecord] = []
        for path in sorted(self.job_root.glob("*/job.json")):
            try:
                if path.is_symlink() or path.parent.is_symlink():
                    raise ValueError("Persisted job claim path is a symlink")
                with open(path, encoding="utf-8") as handle:
                    job = self._job_from_dict(json.load(handle))
                if job.id != path.parent.name:
                    raise ValueError(
                        "Persisted job identity does not match its directory"
                    )
                if job.status not in {
                    QUEUED,
                    RUNNING,
                    CANCELING,
                    *TERMINAL_STATUSES,
                }:
                    raise ValueError("Persisted job status is invalid")
                if job.status in TERMINAL_STATUSES:
                    if job.id not in self._local_job_ids:
                        self._jobs.pop(job.id, None)
                    continue
                if (
                    len(job.id) != 12
                    or job.id != job.id.lower()
                    or any(character not in "0123456789abcdef" for character in job.id)
                ):
                    raise ValueError("Persisted active job identity is invalid")
                if not isinstance(job.resources, list) or not all(
                    isinstance(resource, str)
                    and resource.strip() == resource
                    and bool(resource)
                    and all(resource.split(":"))
                    for resource in job.resources
                ):
                    raise ValueError("Persisted job resources are invalid")
                expected_log_path = (path.parent / "log.txt").resolve()
                if Path(job.log_path).resolve() != expected_log_path:
                    raise ValueError("Persisted job log path escapes its job directory")
                self._cancellation_requested(path.parent, job.id)
                supervisor_path = path.parent / "supervisor.json"
                if supervisor_path.is_symlink():
                    raise ValueError("Persisted supervisor identity is a symlink")
                self._merge_supervisor_identity(job, supervisor_path, strict=True)
                self._validate_process_identities(job)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Cannot verify persisted resource claim: {path}"
                ) from exc
            if self._job_owner_is_alive(job):
                if job.id not in self._local_job_ids:
                    self._jobs[job.id] = job
                active.append(job)
                continue

            orphan_stopped = self._terminate_persisted_process_group(job)
            has_process_evidence = job.supervisor_pid is not None or job.process_pid is not None
            if has_process_evidence and not orphan_stopped:
                raise RuntimeError(
                    "Cannot release persisted resource claim because its process "
                    f"groups could not be verified stopped: {path}"
                )
            job.status = FAILED
            job.ended_at = utc_now_iso()
            job.returncode = None
            job.message = "Job owner exited before this job completed."
            if orphan_stopped:
                job.message += " Its orphaned process group was stopped."
            self._append_tail(job, job.message)
            self._persist_job(job)
            self._jobs.pop(job.id, None)
        return active

    def _resource_holders(self, *, include_services: bool = True) -> dict[str, str]:
        holders = {}
        for job in self._persisted_active_jobs():
            if not include_services and job.visibility == SERVICE_VISIBILITY:
                continue
            for resource in job.resources:
                conflicting = next(
                    (
                        (held, holder)
                        for held, holder in holders.items()
                        if holder != job.id and self._resources_conflict(resource, held)
                    ),
                    None,
                )
                if conflicting is not None:
                    held, holder = conflicting
                    raise RuntimeError(
                        "Persisted resource claims overlap: "
                        f"{resource} held by {job.id} conflicts with {held} held by {holder}"
                    )
                holders[resource] = job.id
        return holders

    def _check_resources_available(self, resources: list[str]) -> None:
        if not resources:
            return
        holders = self._resource_holders(include_services=True)
        conflicts: dict[str, str] = {}
        for requested in resources:
            for held, job_id in holders.items():
                if self._resources_conflict(requested, held):
                    label = (
                        requested
                        if requested == held
                        else f"{requested} conflicts with {held}"
                    )
                    conflicts[label] = job_id
        if conflicts:
            details = ", ".join(
                f"{resource} held by job {job_id}"
                for resource, job_id in sorted(conflicts.items())
            )
            raise ResourceBusyError(f"Requested resources are busy: {details}")

    @staticmethod
    def _resources_conflict(left: str, right: str) -> bool:
        return (
            left == right
            or left.startswith(f"{right}:")
            or right.startswith(f"{left}:")
        )

    def _terminate_process_group(
        self, process: subprocess.Popen, *, timeout_s: float = 5.0
    ) -> bool:
        if process.poll() is not None:
            cleanup = self._cleanup_workload_for_supervisor(process, timeout_s=1.0)
            return cleanup is True

        self._signal_supervisor(process, signal.SIGTERM)

        try:
            process.wait(timeout=timeout_s)
            cleanup = self._cleanup_workload_for_supervisor(process, timeout_s=1.0)
            return cleanup is not False
        except subprocess.TimeoutExpired:
            pass

        self._signal_supervisor(process, signal.SIGKILL)
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
        cleanup = self._cleanup_workload_for_supervisor(process, timeout_s=0.0)
        # SIGKILL bypasses the supervisor's workload cleanup handler.  If no
        # workload identity was ever made durable, absence cannot be proven.
        return cleanup is True

    def _cleanup_workload_for_supervisor(
        self,
        process: subprocess.Popen,
        *,
        timeout_s: float,
    ) -> bool | None:
        with self._lock:
            job_id = next(
                (
                    item_id
                    for item_id, item_process in self._processes.items()
                    if item_process is process
                ),
                None,
            )
        if job_id is not None:
            return self._cleanup_recorded_workload(job_id, timeout_s=timeout_s)
        return None

    def _cleanup_recorded_workload(
        self,
        job_id: str,
        *,
        timeout_s: float,
    ) -> bool | None:
        try:
            self._refresh_supervisor_identity(job_id)
        except BaseException:
            pass
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return None
            job = JobRecord(**current.to_dict())
        self._merge_supervisor_identity(
            job,
            Path(job.log_path).parent / "supervisor.json",
        )
        if (
            job.process_pid is None
            or job.process_group_id is None
            or job.process_start_time is None
        ):
            return None
        if not self._persisted_process_matches(job):
            return True
        self._terminate_recorded_workload(job, signal.SIGTERM)
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while self._persisted_process_matches(job) and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._persisted_process_matches(job):
            self._terminate_recorded_workload(job, signal.SIGKILL)
        return not self._persisted_process_matches(job)

    @staticmethod
    def _signal_supervisor(process: subprocess.Popen, signum: int) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            process.terminate() if signum == signal.SIGTERM else process.kill()
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def _refresh_supervisor_identity(self, job_id: str, *, wait_s: float = 0.0) -> None:
        with self._lock:
            job = self._jobs[job_id]
            path = Path(job.log_path).parent / "supervisor.json"
        deadline = time.monotonic() + max(wait_s, 0.0)
        while True:
            try:
                with self._lock:
                    candidate = JobRecord(**self._jobs[job_id].to_dict())
                self._merge_supervisor_identity(candidate, path, strict=True)
                if candidate.process_pid is not None:
                    with self._lock:
                        job = self._jobs[job_id]
                        job.process_pid = candidate.process_pid
                        job.process_group_id = candidate.process_group_id
                        job.process_start_time = candidate.process_start_time
                        self._persist_job(job)
                    return
            except FileNotFoundError:
                pass
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)

    def _load_persisted_jobs(self) -> None:
        try:
            with self._index_connection() as connection:
                paths = [
                    self.job_root / str(row[0])
                    for row in connection.execute(
                        "SELECT source_path FROM jobs "
                        "WHERE status NOT IN (?, ?, ?) "
                        "ORDER BY created_at, id",
                        (SUCCEEDED, FAILED, CANCELED),
                    )
                ]
        except sqlite3.DatabaseError:
            self._rebuild_index()
            return self._load_persisted_jobs()

        for path in paths:
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                job = self._job_from_dict(data)
                self._normalize_loaded_tail(job)
                self._merge_supervisor_identity(
                    job,
                    path.parent / "supervisor.json",
                    strict=True,
                )
                self._validate_process_identities(job)
            except Exception:
                continue

            owner_alive = self._job_owner_is_alive(job)
            orphan_stopped = (
                self._terminate_persisted_process_group(job)
                if not owner_alive
                else False
            )
            if job.status not in TERMINAL_STATUSES:
                if owner_alive:
                    self._jobs[job.id] = job
                    continue
                job.status = FAILED
                job.ended_at = utc_now_iso()
                job.returncode = None
                job.message = "Job runner restarted before this job completed."
                if orphan_stopped:
                    job.message += " Its orphaned process group was stopped."
                self._append_tail(job, job.message)
                self._persist_job(job)

    @staticmethod
    def _job_from_dict(data: Mapping[str, object]) -> JobRecord:
        job_data = dict(data)
        job_data.setdefault("tail", [])
        job_data.setdefault("resources", [])
        job_data.setdefault("parameters", {})
        job_data.setdefault("process_pid", None)
        job_data.setdefault("process_group_id", None)
        job_data.setdefault("process_start_time", None)
        job_data.setdefault("runner_pid", None)
        job_data.setdefault("runner_start_time", None)
        job_data.setdefault("supervisor_pid", None)
        job_data.setdefault("supervisor_process_group_id", None)
        job_data.setdefault("supervisor_start_time", None)
        job_data.setdefault("visibility", OPERATOR_VISIBILITY)
        job_data.setdefault("run_root", None)
        scope_kind = job_data.get("scope_kind")
        if scope_kind not in JOB_SCOPE_KINDS:
            raise ValueError("Persisted job has no current scope_kind")
        if scope_kind == RUN_SCOPE:
            run_root = job_data.get("run_root")
            if not isinstance(run_root, str) or not run_root.strip():
                raise ValueError("Run-scoped persisted job has no run_root")
            job_data["run_root"] = Path(run_root).resolve().as_posix()
        elif job_data.get("run_root") is not None:
            raise ValueError("Non-run persisted job contains run_root")
        return JobRecord(**job_data)

    @staticmethod
    def _read_process_start_time(pid: int) -> int | None:
        """Return Linux process start ticks, used to guard against PID reuse."""

        return read_process_start_time(pid)

    @staticmethod
    def _process_is_zombie(pid: int) -> bool:
        if os.name == "nt":
            return False
        try:
            value = Path(f"/proc/{pid}/stat").read_text()
            fields_after_name = value[value.rfind(")") + 2 :].split()
            return fields_after_name[0] == "Z"
        except (IndexError, OSError):
            return False

    @staticmethod
    def _dedicated_group_has_live_members(group_id: int) -> bool:
        """Find descendants that remain in a workload's dedicated session."""

        if os.name == "nt":
            return False
        try:
            process_entries = Path("/proc").iterdir()
        except OSError:
            return False
        for entry in process_entries:
            if not entry.name.isdigit():
                continue
            try:
                value = (entry / "stat").read_text()
                fields = value[value.rfind(")") + 2 :].split()
                state = fields[0]
                process_group = int(fields[2])
                session_id = int(fields[3])
            except (IndexError, OSError, ValueError):
                continue
            if (
                state != "Z"
                and process_group == group_id
                and session_id == group_id
            ):
                return True
        return False

    @staticmethod
    def _merge_supervisor_identity(
        job: JobRecord,
        path: Path,
        *,
        strict: bool = False,
    ) -> bool:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if strict:
                raise ValueError(f"Persisted supervisor identity is unreadable: {path}") from exc
            return False
        if not isinstance(value, dict) or value.get("schema_version") != (
            "job_process_supervisor.v1"
        ):
            if strict:
                raise ValueError(f"Persisted supervisor identity is invalid: {path}")
            return False
        for field_name, identity_name in (
            ("runner_pid", "owner_pid"),
            ("runner_start_time", "owner_start_time"),
        ):
            recorded = getattr(job, field_name)
            identity_value = value.get(identity_name)
            if recorded is not None and identity_value != recorded:
                if strict:
                    raise ValueError(
                        f"Persisted supervisor owner identity conflicts with {field_name}"
                    )
                return False
        mappings = {
            "supervisor_pid": "supervisor_pid",
            "supervisor_process_group_id": "supervisor_process_group_id",
            "supervisor_start_time": "supervisor_start_time",
            "process_pid": "workload_pid",
            "process_group_id": "workload_process_group_id",
            "process_start_time": "workload_start_time",
        }
        for field_name, identity_name in mappings.items():
            value_item = value.get(identity_name)
            recorded = getattr(job, field_name)
            if recorded is not None and value_item is not None and value_item != recorded:
                if strict:
                    raise ValueError(
                        f"Persisted supervisor identity conflicts with {field_name}"
                    )
                return False
            if recorded is None and type(value_item) is int and value_item > 0:
                setattr(job, field_name, value_item)
            elif value_item is not None and (type(value_item) is not int or value_item <= 0):
                if strict:
                    raise ValueError(
                        f"Persisted supervisor identity has invalid {identity_name}"
                    )
                return False
        return True

    @classmethod
    def _persisted_process_matches(cls, job: JobRecord) -> bool:
        pid = job.process_pid
        group_id = job.process_group_id
        start_time = job.process_start_time
        if pid is None or group_id is None or start_time is None or os.name == "nt":
            return False
        if group_id != pid:
            return False
        current_start_time = cls._read_process_start_time(pid)
        if current_start_time is None:
            return cls._dedicated_group_has_live_members(group_id)
        if current_start_time != start_time:
            return False
        if cls._process_is_zombie(pid):
            return cls._dedicated_group_has_live_members(group_id)
        try:
            return os.getpgid(pid) == group_id
        except ProcessLookupError:
            return False

    @classmethod
    def _job_owner_is_alive(cls, job: JobRecord) -> bool:
        if job.runner_pid is None or job.runner_start_time is None:
            return False
        return cls._read_process_start_time(job.runner_pid) == job.runner_start_time

    @classmethod
    def _terminate_persisted_process_group(
        cls,
        job: JobRecord,
        *,
        timeout_s: float = 2.0,
    ) -> bool:
        """Stop verified supervisor/workload groups and prove both are absent."""

        has_identity = (
            job.supervisor_pid is not None
            and job.supervisor_process_group_id is not None
            and job.supervisor_start_time is not None
        ) or (
            job.process_pid is not None
            and job.process_group_id is not None
            and job.process_start_time is not None
        )
        if cls._persisted_supervisor_matches(job):
            assert job.supervisor_process_group_id is not None
            try:
                os.killpg(job.supervisor_process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if cls._persisted_process_matches(job):
            assert job.process_group_id is not None
            try:
                os.killpg(job.process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass

        deadline = time.monotonic() + max(timeout_s, 0.0)
        while (
            cls._persisted_supervisor_matches(job)
            or cls._persisted_process_matches(job)
        ) and time.monotonic() < deadline:
            time.sleep(0.02)
        if cls._persisted_supervisor_matches(job):
            assert job.supervisor_process_group_id is not None
            try:
                os.killpg(job.supervisor_process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if cls._persisted_process_matches(job):
            assert job.process_group_id is not None
            try:
                os.killpg(job.process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        final_deadline = time.monotonic() + 1.0
        while (
            cls._persisted_supervisor_matches(job)
            or cls._persisted_process_matches(job)
        ) and time.monotonic() < final_deadline:
            time.sleep(0.02)
        return has_identity and not (
            cls._persisted_supervisor_matches(job)
            or cls._persisted_process_matches(job)
        )

    @classmethod
    def _persisted_supervisor_matches(cls, job: JobRecord) -> bool:
        pid = job.supervisor_pid
        group_id = job.supervisor_process_group_id
        start_time = job.supervisor_start_time
        if pid is None or group_id is None or start_time is None or os.name == "nt":
            return False
        if group_id != pid:
            return False
        if cls._read_process_start_time(pid) != start_time:
            return False
        if cls._process_is_zombie(pid):
            return False
        try:
            return os.getpgid(pid) == group_id
        except ProcessLookupError:
            return False

    @classmethod
    def _terminate_recorded_workload(cls, job: JobRecord, signum: int) -> bool:
        if not cls._persisted_process_matches(job):
            return False
        assert job.process_group_id is not None
        try:
            os.killpg(job.process_group_id, signum)
            return True
        except ProcessLookupError:
            return False

    def _persist_job(self, job: JobRecord) -> None:
        job_dir = Path(job.log_path).parent
        path = job_dir / "job.json"
        with self._job_state_transaction(job_dir):
            existing = (
                self._read_authoritative_job(job_dir, job.id)
                if path.is_file()
                else None
            )
            effective = self._merge_durable_job_state(
                job,
                existing=existing,
                cancellation_requested=self._cancellation_requested(job_dir, job.id),
            )
            self._write_authoritative_job(path, effective)
        self._adopt_job_record(job, effective)

    @staticmethod
    def _validate_scope(
        scope_kind: str,
        run_root: str | Path | None,
    ) -> str | None:
        if scope_kind not in JOB_SCOPE_KINDS:
            raise ValueError(
                "scope_kind for a new job must be one of: "
                + ", ".join(sorted(JOB_SCOPE_KINDS))
            )
        if scope_kind == RUN_SCOPE:
            if run_root is None or not str(run_root).strip():
                raise ValueError("run_root is required when scope_kind='run'")
            return Path(run_root).resolve().as_posix()
        if run_root is not None:
            raise ValueError("run_root is only valid when scope_kind='run'")
        return None

    @staticmethod
    def _normalize_filter_values(
        values: list[str] | tuple[str, ...] | set[str] | None,
    ) -> tuple[str, ...]:
        if not values:
            return ()
        return tuple(
            sorted(
                {str(value).strip().lower() for value in values if str(value).strip()}
            )
        )

    @staticmethod
    def _page_filter_signature(
        *,
        search: str,
        statuses: tuple[str, ...],
        scope_kinds: tuple[str, ...],
        run_root: str | None,
        include_services: bool,
    ) -> str:
        value = json.dumps(
            {
                "search": search,
                "statuses": statuses,
                "scope_kinds": scope_kinds,
                "run_root": run_root,
                "include_services": include_services,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _encode_cursor(
        *,
        created_at: str,
        job_id: str,
        filter_signature: str,
    ) -> str:
        raw = json.dumps(
            {
                "v": 1,
                "created_at": created_at,
                "job_id": job_id,
                "filters": filter_signature,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(
        cursor: str,
        filter_signature: str,
    ) -> tuple[str, str]:
        try:
            padding = "=" * (-len(cursor) % 4)
            value = json.loads(
                base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
            )
            if (
                not isinstance(value, dict)
                or value.get("v") != 1
                or value.get("filters") != filter_signature
                or not isinstance(value.get("created_at"), str)
                or not isinstance(value.get("job_id"), str)
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "cursor is invalid or does not match the current filters"
            ) from exc
        return value["created_at"], value["job_id"]

    @staticmethod
    def _index_filters(
        *,
        search: str,
        scope_kinds: tuple[str, ...],
        run_root: str | None,
        include_services: bool,
    ) -> tuple[list[str], list[object]]:
        where: list[str] = []
        parameters: list[object] = []
        if not include_services:
            where.append("visibility = ?")
            parameters.append(OPERATOR_VISIBILITY)
        if search:
            where.append("search_text LIKE ?")
            parameters.append(f"%{search}%")
        if scope_kinds:
            placeholders = ", ".join("?" for _ in scope_kinds)
            where.append(f"scope_kind IN ({placeholders})")
            parameters.extend(scope_kinds)
        if run_root is not None:
            where.append("run_root = ?")
            parameters.append(run_root)
        return where, parameters

    @staticmethod
    def _record_matches_filters(
        job: JobRecord,
        *,
        search: str,
        statuses: tuple[str, ...],
        scope_kinds: tuple[str, ...],
        run_root: str | None,
        include_services: bool,
    ) -> bool:
        if not include_services and job.visibility != OPERATOR_VISIBILITY:
            return False
        if statuses and job.status not in statuses:
            return False
        if scope_kinds and job.scope_kind not in scope_kinds:
            return False
        if run_root is not None and job.run_root != run_root:
            return False
        if not search:
            return True
        return search in LocalJobRunner._search_text(job)

    @staticmethod
    def _search_text(job: JobRecord) -> str:
        return " ".join(
            (
                job.id,
                job.name,
                job.status,
                job.message or "",
                " ".join(job.resources),
                job.scope_kind,
                job.run_root or "",
                json.dumps(job.parameters, sort_keys=True, default=str),
            )
        ).lower()

    def _index_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_path, timeout=10.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _create_index_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL UNIQUE,
                source_mtime_ns INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                visibility TEXT NOT NULL,
                scope_kind TEXT NOT NULL,
                run_root TEXT,
                search_text TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX jobs_history_order ON jobs(status, created_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX jobs_scope_run "
            "ON jobs(scope_kind, run_root, created_at DESC, id DESC)"
        )
        connection.execute(f"PRAGMA user_version = {JOB_INDEX_SCHEMA_VERSION}")

    def _ensure_index(self) -> None:
        if not self.index_path.exists():
            self._rebuild_index()
            return
        try:
            sources = {
                path.relative_to(self.job_root).as_posix(): (
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                )
                for path in self.job_root.glob("*/job.json")
                if path.is_file()
            }
            with self._index_connection() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version != JOB_INDEX_SCHEMA_VERSION:
                    raise sqlite3.DatabaseError("unsupported job index schema")
                check = connection.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    raise sqlite3.DatabaseError("job index integrity check failed")
                indexed = {
                    str(row[0]): (int(row[1]), int(row[2]))
                    for row in connection.execute(
                        "SELECT source_path, source_mtime_ns, source_size FROM jobs"
                    )
                }
            if indexed != sources:
                self._rebuild_index()
        except (OSError, sqlite3.DatabaseError):
            self._rebuild_index()

    def _rebuild_index(self) -> None:
        temporary = self.job_root / f".{JOB_INDEX_FILENAME}.{uuid.uuid4().hex}.tmp"
        try:
            with sqlite3.connect(temporary) as connection:
                self._create_index_schema(connection)
                for path in sorted(self.job_root.glob("*/job.json")):
                    try:
                        with open(path, encoding="utf-8") as handle:
                            job = self._job_from_dict(json.load(handle))
                        self._insert_index_record(connection, path, job)
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                connection.commit()
            os.replace(temporary, self.index_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _upsert_index(self, path: Path, job: JobRecord) -> None:
        try:
            with self._index_connection() as connection:
                self._insert_index_record(
                    connection,
                    path,
                    job,
                    replace_existing=True,
                )
                connection.commit()
        except sqlite3.DatabaseError:
            self._rebuild_index()

    def _insert_index_record(
        self,
        connection: sqlite3.Connection,
        path: Path,
        job: JobRecord,
        *,
        replace_existing: bool = False,
    ) -> None:
        stat = path.stat()
        relative = path.relative_to(self.job_root).as_posix()
        verb = "INSERT OR REPLACE" if replace_existing else "INSERT"
        connection.execute(
            f"""
            {verb} INTO jobs (
                id, source_path, source_mtime_ns, source_size, name, status,
                created_at, visibility, scope_kind, run_root, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                relative,
                stat.st_mtime_ns,
                stat.st_size,
                job.name,
                job.status,
                job.created_at,
                job.visibility,
                job.scope_kind,
                job.run_root,
                self._search_text(job),
            ),
        )

    def _load_indexed_job(self, job_id: str, *, repair_missing: bool = True) -> JobRecord:
        try:
            with self._index_connection() as connection:
                row = connection.execute(
                    "SELECT source_path FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
        except sqlite3.DatabaseError:
            self._rebuild_index()
            return self._load_indexed_job(job_id, repair_missing=False)
        if row is None:
            if repair_missing:
                self._ensure_index()
                return self._load_indexed_job(job_id, repair_missing=False)
            raise KeyError(f"Unknown job: {job_id}")
        path = (self.job_root / str(row[0])).resolve()
        try:
            path.relative_to(self.job_root.resolve())
            with open(path, encoding="utf-8") as handle:
                job = self._job_from_dict(json.load(handle))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KeyError(f"Unknown job: {job_id}") from exc
        if job.id != job_id:
            raise KeyError(f"Unknown job: {job_id}")
        self._normalize_loaded_tail(job)
        return job

    def _normalize_loaded_tail(self, job: JobRecord) -> None:
        persisted_tail = job.tail[-self.tail_limit :]
        job.tail = []
        for line in persisted_tail:
            self._append_tail(job, str(line))

    @staticmethod
    def _format_command(command: list[str]) -> str:
        return " ".join(shlex.quote(part) for part in command)


class ResourceBusyError(RuntimeError):
    """Raised when a job requests resources held by another active job."""
