"""Small process-backed job runner for local PoseTestBot commands."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

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

        requested_resources = sorted(set(resources or []))
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
            self._jobs[job_id] = job
            self._local_job_ids.add(job_id)
            self._persist_job(job)

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, dict(env or {})),
            name=f"posetestbot-job-{job_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return self.get(job_id)

    def resource_holders(self, *, include_services: bool = False) -> dict[str, str]:
        with self._lock:
            return self._resource_holders(include_services=include_services)

    def get(self, job_id: str) -> JobRecord:
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
            job.status = CANCELED if job.status == QUEUED else CANCELING
            job.message = "Cancellation requested."
            if job.status == CANCELED:
                job.ended_at = utc_now_iso()
            self._append_tail(job, "Cancellation requested.")
            self._persist_job(job)

        if process is not None and process.poll() is None:
            self._terminate_process_group(process)
            with self._lock:
                job = self._jobs.get(job_id)
                if (
                    job is not None
                    and job.status == CANCELING
                    and process.poll() is not None
                ):
                    job.status = CANCELED
                    job.ended_at = utc_now_iso()
                    job.returncode = process.returncode
                    job.message = "Canceled."
                    self._persist_job(job)
        return self.get(job_id)

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop all locally owned groups, escalating once the grace period ends."""

        with self._lock:
            active_ids = [
                job.id
                for job in self._jobs.values()
                if job.id in self._local_job_ids and job.status not in TERMINAL_STATUSES
            ]
        processes: dict[str, subprocess.Popen] = {}
        with self._lock:
            for job_id in active_ids:
                job = self._jobs[job_id]
                job.status = CANCELED if job.status == QUEUED else CANCELING
                job.message = "Shutdown requested."
                self._append_tail(job, job.message)
                self._persist_job(job)
                process = self._processes.get(job_id)
                if process is not None and process.poll() is None:
                    processes[job_id] = process

        for process in processes.values():
            self._signal_supervisor(process, signal.SIGTERM)

        deadline = time.monotonic() + max(timeout, 0.0)
        for job_id in active_ids:
            with self._lock:
                thread = self._threads.get(job_id)
            if thread is None:
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for job_id, process in processes.items():
            if process.poll() is None:
                self._signal_supervisor(process, signal.SIGKILL)
                self._terminate_recorded_workload(self.get(job_id), signal.SIGKILL)
        for job_id in active_ids:
            with self._lock:
                thread = self._threads.get(job_id)
            if thread is not None:
                thread.join(timeout=1.0)

    def log_text(self, job_id: str) -> str:
        job = self.get(job_id)
        log_path = Path(job.log_path)
        if not log_path.is_file():
            return ""
        return log_path.read_text()

    def _run_job(self, job_id: str, env: dict[str, str]) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in {CANCELED, CANCELING}:
                if job.status == CANCELING:
                    job.status = CANCELED
                    job.ended_at = utc_now_iso()
                    self._persist_job(job)
                self._jobs.pop(job_id, None)
                self._threads.pop(job_id, None)
                return
            job.status = RUNNING
            job.started_at = utc_now_iso()
            self._persist_job(job)

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
                    job.status = FAILED
                    job.ended_at = utc_now_iso()
                    job.message = f"{type(exc).__name__}: {exc}"
                    self._append_tail(job, job.message)
                    self._persist_job(job)
                    self._jobs.pop(job_id, None)
                    self._threads.pop(job_id, None)
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
            while True:
                fragment = process.stdout.readline(OUTPUT_READ_CHARS)
                if not fragment:
                    break
                write_log(fragment)
                room = self.max_tail_line_chars - len(pending_tail)
                if room > 0:
                    pending_tail += fragment[:room]
                if len(fragment) > room:
                    pending_tail_truncated = True
                if fragment.endswith("\n"):
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

            if pending_tail or pending_tail_truncated:
                line = pending_tail.rstrip("\r\n")
                if pending_tail_truncated:
                    line += "… [line truncated]"
                with self._lock:
                    current = self._jobs[job_id]
                    self._append_tail(current, line)

            returncode = process.wait()
            self._cleanup_recorded_workload(job_id, timeout_s=1.0)
            with self._lock:
                job = self._jobs[job_id]
                job.returncode = returncode
                job.ended_at = utc_now_iso()
                if job.status in {CANCELED, CANCELING}:
                    job.status = CANCELED
                    job.message = "Canceled."
                elif returncode == 0:
                    job.status = SUCCEEDED
                    job.message = "Command completed successfully."
                else:
                    job.status = FAILED
                    job.message = f"Command exited with status {returncode}."
                self._append_tail(job, job.message)
                self._persist_job(job)
                self._processes.pop(job_id, None)
                self._threads.pop(job_id, None)
                self._jobs.pop(job_id, None)

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

    def _resource_holders(self, *, include_services: bool = True) -> dict[str, str]:
        holders = {}
        for job in self._jobs.values():
            if job.status in TERMINAL_STATUSES:
                continue
            if not include_services and job.visibility == SERVICE_VISIBILITY:
                continue
            for resource in job.resources:
                holders[resource] = job.id
        return holders

    def _check_resources_available(self, resources: list[str]) -> None:
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
    ) -> None:
        if process.poll() is not None:
            return

        self._signal_supervisor(process, signal.SIGTERM)

        try:
            process.wait(timeout=timeout_s)
            self._cleanup_workload_for_supervisor(process, timeout_s=1.0)
            return
        except subprocess.TimeoutExpired:
            pass

        self._signal_supervisor(process, signal.SIGKILL)
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            pass
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
            self._cleanup_recorded_workload(job_id, timeout_s=0.0)

    def _cleanup_workload_for_supervisor(
        self,
        process: subprocess.Popen,
        *,
        timeout_s: float,
    ) -> None:
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
            self._cleanup_recorded_workload(job_id, timeout_s=timeout_s)

    def _cleanup_recorded_workload(self, job_id: str, *, timeout_s: float) -> None:
        self._refresh_supervisor_identity(job_id)
        job = self.get(job_id)
        if not self._persisted_process_matches(job):
            return
        self._terminate_recorded_workload(job, signal.SIGTERM)
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while self._persisted_process_matches(job) and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._persisted_process_matches(job):
            self._terminate_recorded_workload(job, signal.SIGKILL)

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
                with open(path, encoding="utf-8") as handle:
                    value = json.load(handle)
                workload_pid = value.get("workload_pid")
                if isinstance(workload_pid, int):
                    with self._lock:
                        job = self._jobs[job_id]
                        job.process_pid = workload_pid
                        job.process_group_id = value.get("workload_process_group_id")
                        job.process_start_time = value.get("workload_start_time")
                        self._persist_job(job)
                    return
            except (OSError, ValueError, json.JSONDecodeError):
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
                self._merge_supervisor_identity(job, path.parent / "supervisor.json")
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
    def _merge_supervisor_identity(job: JobRecord, path: Path) -> None:
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return
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
            if getattr(job, field_name) is None and isinstance(value_item, int):
                setattr(job, field_name, value_item)

    @classmethod
    def _persisted_process_matches(cls, job: JobRecord) -> bool:
        pid = job.process_pid
        group_id = job.process_group_id
        start_time = job.process_start_time
        if pid is None or group_id is None or start_time is None or os.name == "nt":
            return False
        if cls._read_process_start_time(pid) != start_time:
            return False
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
        """Stop a verified process group left by an interrupted runner."""

        stopped = False
        if cls._persisted_supervisor_matches(job):
            assert job.supervisor_process_group_id is not None
            try:
                os.killpg(job.supervisor_process_group_id, signal.SIGTERM)
                stopped = True
            except ProcessLookupError:
                pass
        if not cls._persisted_process_matches(job):
            return stopped
        assert job.process_group_id is not None
        try:
            os.killpg(job.process_group_id, signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            return stopped

        deadline = time.monotonic() + max(timeout_s, 0.0)
        while cls._persisted_process_matches(job) and time.monotonic() < deadline:
            time.sleep(0.02)
        if cls._persisted_process_matches(job):
            try:
                os.killpg(job.process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return stopped

    @classmethod
    def _persisted_supervisor_matches(cls, job: JobRecord) -> bool:
        pid = job.supervisor_pid
        group_id = job.supervisor_process_group_id
        start_time = job.supervisor_start_time
        if pid is None or group_id is None or start_time is None or os.name == "nt":
            return False
        if cls._read_process_start_time(pid) != start_time:
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
        path = Path(job.log_path).parent / "job.json"
        atomic_write_json(path, job.to_dict())
        self._upsert_index(path, job)

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

    def _load_indexed_job(self, job_id: str) -> JobRecord:
        try:
            with self._index_connection() as connection:
                row = connection.execute(
                    "SELECT source_path FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
        except sqlite3.DatabaseError:
            self._rebuild_index()
            return self._load_indexed_job(job_id)
        if row is None:
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
