"""Linux-aware process supervisor used by :mod:`posetestbot.jobs.runner`.

The supervisor deliberately lives in a different process group from the
workload.  That lets it terminate the complete workload group when its owner
goes away, including when the owner was killed and could not run cleanup.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from posetestbot.io.atomic import atomic_write_json


PARENT_DEATH_SIGNAL = signal.SIGTERM
DEFAULT_TERMINATION_TIMEOUT_S = 5.0


def read_process_start_time(pid: int) -> int | None:
    """Return Linux process start ticks, guarding all persisted PID use."""

    if os.name == "nt":
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields_after_name = stat[stat.rfind(")") + 2 :].split()
        return int(fields_after_name[19])
    except (IndexError, OSError, ValueError):
        return None


def process_matches(pid: int, start_time: int | None) -> bool:
    return start_time is not None and read_process_start_time(pid) == start_time


def install_parent_death_signal(signum: int = PARENT_DEATH_SIGNAL) -> None:
    """Ask Linux to signal this supervisor when its immediate parent dies."""

    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_pdeathsig = 1
    if libc.prctl(pr_set_pdeathsig, int(signum), 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_group(
    group_id: int,
    *,
    timeout_s: float,
    leader: subprocess.Popen[Any] | None = None,
) -> bool:
    """Terminate a workload group, escalating after the shared grace period."""

    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while _group_exists(group_id) and time.monotonic() < deadline:
        if leader is not None:
            leader.poll()
        time.sleep(0.02)
    if _group_exists(group_id):
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def _write_identity(path: Path, value: dict[str, Any], **changes: Any) -> None:
    value.update(changes)
    value["updated_monotonic_ns"] = time.monotonic_ns()
    atomic_write_json(path, value)


def supervise(
    *,
    owner_pid: int,
    owner_start_time: int,
    identity_path: Path,
    cancel_request_path: Path,
    command: list[str],
    termination_timeout_s: float = DEFAULT_TERMINATION_TIMEOUT_S,
) -> int:
    """Run one workload until completion or verified owner loss."""

    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    install_parent_death_signal()

    supervisor_pid = os.getpid()
    supervisor_start_time = read_process_start_time(supervisor_pid)
    identity: dict[str, Any] = {
        "schema_version": "job_process_supervisor.v1",
        "owner_pid": owner_pid,
        "owner_start_time": owner_start_time,
        "supervisor_pid": supervisor_pid,
        "supervisor_process_group_id": (
            os.getpgid(supervisor_pid) if os.name != "nt" else supervisor_pid
        ),
        "supervisor_start_time": supervisor_start_time,
        "workload_pid": None,
        "workload_process_group_id": None,
        "workload_start_time": None,
        "status": "starting",
    }
    _write_identity(identity_path, identity)

    # PR_SET_PDEATHSIG has a documented fork/prctl race.  Verifying both PID
    # and start time immediately after prctl closes that window safely.
    if not process_matches(owner_pid, owner_start_time):
        _write_identity(identity_path, identity, status="owner_missing")
        return 125
    if cancel_request_path.is_file():
        _write_identity(identity_path, identity, status="canceled_before_start")
        return 130

    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(command, start_new_session=(os.name != "nt"))
        workload_group = os.getpgid(process.pid) if os.name != "nt" else process.pid
        workload_start_time = read_process_start_time(process.pid)
        _write_identity(
            identity_path,
            identity,
            status="running",
            workload_pid=process.pid,
            workload_process_group_id=workload_group,
            workload_start_time=workload_start_time,
        )

        owner_check_at = 0.0
        while process.poll() is None:
            now = time.monotonic()
            if stop_requested or cancel_request_path.is_file():
                _write_identity(identity_path, identity, status="stopping")
                terminate_group(
                    workload_group,
                    timeout_s=termination_timeout_s,
                    leader=process,
                )
                break
            if now >= owner_check_at:
                owner_check_at = now + 1.0
                if not process_matches(owner_pid, owner_start_time):
                    _write_identity(identity_path, identity, status="owner_missing")
                    terminate_group(
                        workload_group,
                        timeout_s=termination_timeout_s,
                        leader=process,
                    )
                    break
            time.sleep(0.05)

        returncode = process.wait()
        # A command can exit while leaving descendants behind.  They remain in
        # its dedicated group and are never allowed to outlive the job record.
        if os.name != "nt" and _group_exists(workload_group):
            terminate_group(workload_group, timeout_s=termination_timeout_s)
        _write_identity(
            identity_path,
            identity,
            status="stopped",
            workload_returncode=returncode,
        )
        return int(returncode)
    except BaseException as exc:
        if process is not None and process.poll() is None:
            group_id = os.getpgid(process.pid) if os.name != "nt" else process.pid
            terminate_group(
                group_id,
                timeout_s=termination_timeout_s,
                leader=process,
            )
        _write_identity(
            identity_path,
            identity,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervise one PoseTestBot local job.")
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--owner-start-time", type=int, required=True)
    parser.add_argument("--identity-path", type=Path, required=True)
    parser.add_argument("--cancel-request-path", type=Path, required=True)
    parser.add_argument("--termination-timeout", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a workload command is required after --")
    return args


def main() -> int:
    args = parse_args()
    return supervise(
        owner_pid=args.owner_pid,
        owner_start_time=args.owner_start_time,
        identity_path=args.identity_path,
        cancel_request_path=args.cancel_request_path,
        command=args.command,
        termination_timeout_s=max(0.0, args.termination_timeout),
    )


if __name__ == "__main__":
    raise SystemExit(main())
