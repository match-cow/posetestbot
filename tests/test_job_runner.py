from __future__ import annotations

import json

import os

import signal

import subprocess

import sys

import time

from multiprocessing import get_context

from pathlib import Path

import pytest

from posetestbot.jobs import runner as runner_module
from posetestbot.jobs.runner import (
    CANCELED,
    CANCELING,
    FAILED,
    QUEUED,
    SUCCEEDED,
    LocalJobRunner,
    ResourceBusyError,
    SERVICE_VISIBILITY,
)


def _race_resource_submission(
    job_root: str,
    ready_path: str,
    start_path: str,
    result_path: str,
    resource: str,
) -> None:
    runner = LocalJobRunner(Path(job_root))
    Path(ready_path).write_text("ready\n")
    deadline = time.monotonic() + 10
    while not Path(start_path).is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        job = runner.submit(
            name="resource-race",
            scope_kind="global",
            command=[sys.executable, "-c", "import time; time.sleep(2)"],
            resources=[resource],
        )
    except ResourceBusyError:
        Path(result_path).write_text("blocked\n")
        return
    except Exception as exc:  # pragma: no cover - diagnostic for child failures
        Path(result_path).write_text(f"error:{type(exc).__name__}:{exc}\n")
        return
    Path(result_path).write_text("accepted\n")
    runner.wait(job.id, timeout=5)


def test_uv_job_resolves_supported_user_install_when_service_path_omits_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv_executable = tmp_path / ".local" / "bin" / "uv"
    uv_executable.parent.mkdir(parents=True)
    uv_executable.write_text("#!/bin/sh\n")
    uv_executable.chmod(0o700)
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: None)

    command = ["uv", "run", "python", "worker.py"]

    assert runner_module._resolve_supervised_command(command, home=tmp_path) == [
        uv_executable.as_posix(),
        "run",
        "python",
        "worker.py",
    ]
    assert command == ["uv", "run", "python", "worker.py"]


def _write_terminal_job(
    job_root: Path,
    *,
    job_id: str,
    created_at: str,
    name: str | None = None,
    status: str = SUCCEEDED,
    scope_kind: str | None = "global",
    run_root: str | None = None,
    parameters: dict | None = None,
) -> bytes:
    job_dir = job_root / job_id
    job_dir.mkdir(parents=True)
    value = {
        "id": job_id,
        "name": name or job_id,
        "command": [sys.executable, "-c", "pass"],
        "cwd": None,
        "status": status,
        "created_at": created_at,
        "ended_at": created_at,
        "log_path": (job_dir / "log.txt").as_posix(),
        "parameters": parameters or {},
    }
    if scope_kind is not None:
        value["scope_kind"] = scope_kind
        value["run_root"] = run_root
    encoded = json.dumps(value, indent=2).encode()
    (job_dir / "job.json").write_bytes(encoded)
    (job_dir / "log.txt").write_text(job_id)
    return encoded


def test_local_job_runner_requires_valid_explicit_scope(tmp_path: Path) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    command = [sys.executable, "-c", "pass"]

    with pytest.raises(TypeError, match="scope_kind"):
        runner.submit(name="missing", command=command)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="new job"):
        runner.submit(name="unknown", command=command, scope_kind="unknown")
    with pytest.raises(ValueError, match="run_root is required"):
        runner.submit(name="run", command=command, scope_kind="run")
    with pytest.raises(ValueError, match="only valid"):
        runner.submit(
            name="global",
            command=command,
            scope_kind="global",
            run_root=tmp_path,
        )
    with pytest.raises(ValueError, match="hierarchy segments"):
        runner.submit(
            name="invalid-resource",
            command=command,
            scope_kind="global",
            resources=["camera::oak_d_pro"],
        )

    job = runner.submit(
        name="run",
        command=command,
        scope_kind="run",
        run_root=tmp_path / "dataset",
        parameters={"run_root": "kept-for-command-provenance"},
    )
    finished = runner.wait(job.id, timeout=5)
    assert finished.scope_kind == "run"
    assert finished.run_root == (tmp_path / "dataset").resolve().as_posix()
    assert finished.parameters["run_root"] == "kept-for-command-provenance"


def test_job_index_rebuilds_without_rewriting_or_pruning_history(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    expected = {
        "first": _write_terminal_job(
            job_root,
            job_id="first",
            created_at="2026-07-20T00:00:00+00:00",
        ),
        "second": _write_terminal_job(
            job_root,
            job_id="second",
            created_at="2026-07-21T00:00:00+00:00",
        ),
    }
    runner = LocalJobRunner(job_root)
    assert runner.index_path.is_file()
    assert [job.id for job in runner.list()] == ["second", "first"]

    runner.index_path.write_bytes(b"not a sqlite database")
    recovered = LocalJobRunner(job_root)

    assert [job.id for job in recovered.list()] == ["second", "first"]
    assert {path.name for path in job_root.iterdir() if path.is_dir()} == set(expected)
    for job_id, original in expected.items():
        assert (job_root / job_id / "job.json").read_bytes() == original


def test_job_index_rejects_pre_scope_history(tmp_path: Path) -> None:
    job_root = tmp_path / "jobs"
    _write_terminal_job(
        job_root,
        job_id="pre-scope",
        created_at="2026-07-19T00:00:00+00:00",
        scope_kind=None,
    )

    runner = LocalJobRunner(job_root)

    assert runner.list() == []
    with pytest.raises(KeyError, match="Unknown job"):
        runner.get("pre-scope")


def test_job_history_cursor_is_stable_when_newer_history_arrives(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    for job_id, day in (("one", 1), ("two", 2), ("three", 3)):
        _write_terminal_job(
            job_root,
            job_id=job_id,
            created_at=f"2026-07-{day:02d}T00:00:00+00:00",
        )
    runner = LocalJobRunner(job_root)

    first = runner.list_page(limit=2)
    assert [job.id for job in first.jobs] == ["three", "two"]
    assert first.next_cursor is not None

    _write_terminal_job(
        job_root,
        job_id="newer",
        created_at="2026-07-04T00:00:00+00:00",
    )
    second = runner.list_page(limit=2, cursor=first.next_cursor)

    assert [job.id for job in second.jobs] == ["one"]
    assert second.next_cursor is None


def test_local_job_runner_captures_successful_command(tmp_path: Path) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")

    job = runner.submit(
        name="echo",
        scope_kind="global",
        command=[sys.executable, "-c", "print('hello from job')"],
        parameters={"purpose": "unit-test"},
    )
    finished = runner.wait(job.id, timeout=5)

    assert finished.status == SUCCEEDED
    assert finished.returncode == 0
    assert finished.parameters == {"purpose": "unit-test"}
    assert "hello from job" in runner.log_text(job.id)
    assert (tmp_path / "jobs" / job.id / "job.json").is_file()


def test_local_job_runner_records_failed_command(tmp_path: Path) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")

    job = runner.submit(
        name="fail",
        scope_kind="global",
        command=[sys.executable, "-c", "print('nope'); raise SystemExit(7)"],
    )
    finished = runner.wait(job.id, timeout=5)

    assert finished.status == FAILED
    assert finished.returncode == 7
    assert "nope" in runner.log_text(job.id)


def test_worker_thread_start_failure_releases_durable_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")

    def fail_start(_thread: object) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(runner_module.threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        runner.submit(
            name="never-started",
            scope_kind="global",
            command=[sys.executable, "-c", "print('must not run')"],
            resources=["camera"],
        )

    jobs = runner.list()
    assert len(jobs) == 1
    assert jobs[0].status == FAILED
    assert "worker thread could not start" in (jobs[0].message or "")
    assert runner.resource_holders() == {}


def test_internal_runner_failure_stops_work_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    original_refresh = runner._refresh_supervisor_identity
    calls = 0

    def fail_first_refresh(job_id: str, *, wait_s: float = 0.0) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("identity refresh failed")
        original_refresh(job_id, wait_s=wait_s)

    monkeypatch.setattr(runner, "_refresh_supervisor_identity", fail_first_refresh)
    job = runner.submit(
        name="runner-fault",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["camera"],
    )

    finished = runner.wait(job.id, timeout=10)

    assert finished.status == FAILED
    assert "identity refresh failed" in (finished.message or "")
    assert runner.resource_holders() == {}


def test_supervisor_sigkill_does_not_leave_workload_or_release_early(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Process-group recovery uses Linux process metadata")
    runner = LocalJobRunner(tmp_path / "jobs")
    job = runner.submit(
        name="supervisor-crash",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["robot"],
    )
    deadline = time.monotonic() + 5
    record = runner.get(job.id)
    while (
        record.supervisor_pid is None or record.process_pid is None
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
        record = runner.get(job.id)
    assert record.supervisor_pid is not None
    assert record.process_pid is not None
    workload_pid = record.process_pid

    os.kill(record.supervisor_pid, signal.SIGKILL)
    finished = runner.wait(job.id, timeout=8)

    assert finished.status == FAILED
    assert runner.resource_holders() == {}
    deadline = time.monotonic() + 5
    while (
        LocalJobRunner._read_process_start_time(workload_pid) is not None
        and not LocalJobRunner._process_is_zombie(workload_pid)
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert LocalJobRunner._read_process_start_time(
        workload_pid
    ) is None or LocalJobRunner._process_is_zombie(workload_pid)


def test_internal_runner_failure_retains_claim_until_process_stop_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    original_refresh = runner._refresh_supervisor_identity
    original_terminate = runner._terminate_process_group
    refresh_calls = 0
    allow_termination = False

    def fail_first_refresh(job_id: str, *, wait_s: float = 0.0) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            raise RuntimeError("identity refresh failed")
        original_refresh(job_id, wait_s=wait_s)

    def conditional_terminate(
        process: subprocess.Popen,
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        if not allow_termination:
            return False
        return original_terminate(process, timeout_s=timeout_s)

    monkeypatch.setattr(runner, "_refresh_supervisor_identity", fail_first_refresh)
    monkeypatch.setattr(runner, "_terminate_process_group", conditional_terminate)
    job = runner.submit(
        name="unverified-runner-fault",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["camera"],
    )

    with runner._lock:
        worker = runner._threads[job.id]
    worker.join(timeout=5)
    retained = runner.get(job.id)

    assert retained.status not in {FAILED, SUCCEEDED, CANCELED}
    assert "claim is retained" in (retained.message or "")
    assert runner.resource_holders() == {"camera": job.id}

    allow_termination = True
    assert runner.resource_holders() == {}
    assert runner.get(job.id).status == FAILED


def test_thread_start_terminal_persistence_is_retried_before_claim_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    real_atomic_write_json = runner_module.atomic_write_json
    failed_terminal_write = False

    def fail_one_terminal_write(path: Path, value: object, **kwargs: object) -> Path:
        nonlocal failed_terminal_write
        if (
            not failed_terminal_write
            and Path(path).name == "job.json"
            and isinstance(value, dict)
            and value.get("status") == FAILED
        ):
            failed_terminal_write = True
            raise OSError("temporary job-state storage fault")
        return real_atomic_write_json(path, value, **kwargs)

    def fail_start(_thread: object) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(runner_module, "atomic_write_json", fail_one_terminal_write)
    monkeypatch.setattr(runner_module.threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        runner.submit(
            name="never-started",
            scope_kind="global",
            command=[sys.executable, "-c", "print('must not run')"],
            resources=["camera"],
        )

    assert failed_terminal_write
    assert runner.resource_holders() == {}
    assert runner.list()[0].status == FAILED


def test_rebuildable_index_failure_does_not_change_job_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")

    def fail_index_update(_path: Path, _job: object) -> None:
        raise OSError("derived index unavailable")

    monkeypatch.setattr(runner, "_upsert_index", fail_index_update)
    job = runner.submit(
        name="index-fault",
        scope_kind="global",
        command=[sys.executable, "-c", "print('authoritative state survives')"],
        resources=["disk_io"],
    )

    finished = runner.wait(job.id, timeout=5)

    assert finished.status == SUCCEEDED
    assert runner.resource_holders() == {}
    assert "authoritative state survives" in runner.log_text(job.id)


def test_local_job_runner_bounds_large_unbroken_output(tmp_path: Path) -> None:
    runner = LocalJobRunner(
        tmp_path / "jobs",
        max_log_bytes=4096,
        max_tail_line_chars=128,
    )

    job = runner.submit(
        name="large-output",
        scope_kind="global",
        command=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 100_000)",
        ],
    )
    finished = runner.wait(job.id, timeout=5)

    assert finished.status == SUCCEEDED
    log_path = Path(finished.log_path)
    assert log_path.stat().st_size <= 4096
    assert "job log truncated" in runner.log_text(job.id)
    assert all(len(line) <= 150 for line in finished.tail)
    assert any("line truncated" in line for line in finished.tail)
    assert (log_path.parent / "job.json").stat().st_size < 10_000


def test_local_job_runner_can_cancel_running_command(tmp_path: Path) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")

    job = runner.submit(
        name="sleep",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
    )
    deadline = time.time() + 5
    while runner.get(job.id).started_at is None and time.time() < deadline:
        time.sleep(0.01)

    canceled = runner.cancel(job.id)
    finished = runner.wait(job.id, timeout=5)

    assert canceled.status in {CANCELING, CANCELED}
    assert finished.status == CANCELED


def test_shutdown_still_stops_work_when_cancel_request_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    job = runner.submit(
        name="shutdown-storage-fault",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["robot"],
    )
    deadline = time.monotonic() + 5
    while runner.get(job.id).process_pid is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.get(job.id).process_pid is not None
    real_atomic_write_json = runner_module.atomic_write_json

    def fail_cancel_request(path: Path, value: object, **kwargs: object) -> Path:
        if Path(path).name == runner_module.JOB_CANCEL_REQUEST_FILENAME:
            raise OSError("cancel-request storage unavailable")
        return real_atomic_write_json(path, value, **kwargs)

    monkeypatch.setattr(runner_module, "atomic_write_json", fail_cancel_request)

    runner.shutdown(timeout=2.0)

    assert runner.wait(job.id, timeout=5).status == CANCELED
    assert runner.resource_holders() == {}


def test_local_job_runner_cancels_child_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child_survived.txt"
    runner = LocalJobRunner(tmp_path / "jobs")
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import pathlib, time; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('alive')\"]); "
        "time.sleep(10)"
    )

    job = runner.submit(
        name="parent",
        command=[sys.executable, "-c", script],
        scope_kind="global",
    )
    deadline = time.time() + 5
    while runner.get(job.id).started_at is None and time.time() < deadline:
        time.sleep(0.01)

    runner.cancel(job.id)
    finished = runner.wait(job.id, timeout=5)
    time.sleep(1.2)

    assert finished.status == CANCELED
    assert not marker.exists()


def test_local_job_runner_marks_interrupted_jobs_failed_on_reload(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    job_dir = job_root / "orphaned"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "id": "orphaned",
                "name": "sleep",
                "command": [sys.executable, "-c", "import time; time.sleep(10)"],
                "cwd": None,
                "status": QUEUED,
                "created_at": "2026-06-16T00:00:00+00:00",
                "log_path": (job_dir / "log.txt").as_posix(),
                "resources": ["robot"],
                "parameters": {"capture": True},
                "scope_kind": "global",
            }
        )
    )

    reloaded = LocalJobRunner(job_root)
    loaded = reloaded.get("orphaned")

    assert loaded.status == FAILED
    assert loaded.message == "Job runner restarted before this job completed."
    assert loaded.parameters == {"capture": True}


def test_local_job_runner_stops_verified_orphaned_process_group_on_reload(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Process-group recovery uses Linux process metadata")

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    process_start_time = LocalJobRunner._read_process_start_time(process.pid)
    if process_start_time is None:
        process.kill()
        process.wait()
        pytest.skip("Linux /proc process start metadata is unavailable")

    job_root = tmp_path / "jobs"
    job_dir = job_root / "orphaned"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "id": "orphaned",
                "name": "sleep",
                "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                "cwd": None,
                "status": "running",
                "created_at": "2026-07-10T00:00:00+00:00",
                "log_path": (job_dir / "log.txt").as_posix(),
                "process_pid": process.pid,
                "process_group_id": os.getpgid(process.pid),
                "process_start_time": process_start_time,
                "runner_pid": 999_999_999,
                "runner_start_time": 1,
                "scope_kind": "global",
            }
        )
    )

    try:
        reloaded = LocalJobRunner(job_root)
        loaded = reloaded.get("orphaned")
        process.wait(timeout=5)

        assert loaded.status == FAILED
        assert "orphaned process group was stopped" in loaded.message
        assert process.returncode == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_orphan_recovery_stops_descendants_after_workload_leader_exits(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Process-group recovery uses Linux process metadata")

    child_path = tmp_path / "child.pid"
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, subprocess, sys, time; "
                "child=subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(30)']); "
                f"pathlib.Path({str(child_path)!r}).write_text(str(child.pid)); "
                "time.sleep(0.2)"
            ),
        ],
        start_new_session=True,
    )
    leader_start_time = LocalJobRunner._read_process_start_time(leader.pid)
    if leader_start_time is None:
        leader.kill()
        leader.wait()
        pytest.skip("Linux /proc process start metadata is unavailable")
    deadline = time.monotonic() + 5
    while not child_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_path.is_file()
    child_pid = int(child_path.read_text())
    group_id = os.getpgid(leader.pid)
    leader.wait(timeout=5)
    assert LocalJobRunner._dedicated_group_has_live_members(group_id)

    job_root = tmp_path / "jobs"
    job_id = "c" * 12
    job_dir = job_root / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "name": "orphaned-descendant",
                "command": [sys.executable, "-c", "pass"],
                "cwd": None,
                "status": "running",
                "created_at": "2026-08-23T00:00:00+00:00",
                "log_path": (job_dir / "log.txt").as_posix(),
                "process_pid": leader.pid,
                "process_group_id": group_id,
                "process_start_time": leader_start_time,
                "runner_pid": 999_999_999,
                "runner_start_time": 1,
                "scope_kind": "global",
            }
        )
    )

    try:
        recovered = LocalJobRunner(job_root)
        assert recovered.get(job_id).status == FAILED
        deadline = time.monotonic() + 5
        while (
            LocalJobRunner._dedicated_group_has_live_members(group_id)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert not LocalJobRunner._dedicated_group_has_live_members(group_id)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_local_job_runner_rejects_busy_resources(tmp_path: Path) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")

    job = runner.submit(
        name="sleep",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        resources=["robot"],
    )
    try:
        with pytest.raises(ResourceBusyError, match="robot held by job"):
            runner.submit(
                name="other",
                scope_kind="global",
                command=[sys.executable, "-c", "print('blocked')"],
                resources=["robot"],
            )
        assert runner.resource_holders()["robot"] == job.id
    finally:
        runner.cancel(job.id)


def test_separate_long_lived_runners_share_resource_claims(tmp_path: Path) -> None:
    job_root = tmp_path / "jobs"
    first_runner = LocalJobRunner(job_root)
    second_runner = LocalJobRunner(job_root)

    first = first_runner.submit(
        name="first",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        resources=["camera:realsense_d435:123"],
    )
    try:
        with pytest.raises(ResourceBusyError, match="camera conflicts with"):
            second_runner.submit(
                name="second",
                scope_kind="global",
                command=[sys.executable, "-c", "print('must not start')"],
                resources=["camera"],
            )
        assert second_runner.resource_holders()["camera:realsense_d435:123"] == first.id
    finally:
        first_runner.cancel(first.id)
        first_runner.wait(first.id, timeout=5)


def test_separate_runner_cancellation_is_monotonic_and_stops_owner_work(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    owner = LocalJobRunner(job_root)
    job = owner.submit(
        name="foreign-cancel",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["robot"],
    )
    deadline = time.monotonic() + 5
    while owner.get(job.id).process_pid is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert owner.get(job.id).process_pid is not None

    other = LocalJobRunner(job_root)
    requested = other.cancel(job.id)
    finished = owner.wait(job.id, timeout=8)

    assert requested.status in {CANCELING, CANCELED}
    assert finished.status == CANCELED
    assert LocalJobRunner(job_root).get(job.id).status == CANCELED
    assert owner.resource_holders() == {}
    assert other.resource_holders() == {}


def test_long_lived_foreign_runner_refreshes_terminal_owner_state(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    owner = LocalJobRunner(job_root)
    job = owner.submit(
        name="foreign-refresh",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(1)"],
        resources=["disk_io"],
    )
    other = LocalJobRunner(job_root)
    assert other.get(job.id).status in {QUEUED, "running"}

    assert owner.wait(job.id, timeout=5).status == SUCCEEDED

    assert other.get(job.id).status == SUCCEEDED
    assert [item.id for item in other.list()].count(job.id) == 1


def test_resource_claim_check_and_publish_is_interprocess_atomic(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    start_path = tmp_path / "start"
    context = get_context("spawn")
    processes = []
    result_paths = []
    for index, resource in enumerate(("camera", "camera:oak_d_pro")):
        ready_path = tmp_path / f"ready-{index}"
        result_path = tmp_path / f"result-{index}"
        process = context.Process(
            target=_race_resource_submission,
            args=(
                job_root.as_posix(),
                ready_path.as_posix(),
                start_path.as_posix(),
                result_path.as_posix(),
                resource,
            ),
        )
        process.start()
        processes.append((process, ready_path))
        result_paths.append(result_path)
    try:
        deadline = time.monotonic() + 10
        while (
            not all(path.is_file() for _process, path in processes)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert all(path.is_file() for _process, path in processes)
        start_path.write_text("go\n")
        for process, _ready_path in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(path.read_text().strip() for path in result_paths) == [
            "accepted",
            "blocked",
        ]
    finally:
        for process, _ready_path in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)


def test_resource_claim_scan_fails_closed_on_corrupt_persisted_job(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    runner = LocalJobRunner(job_root)
    corrupt_root = job_root / ("f" * 12)
    corrupt_root.mkdir()
    (corrupt_root / "job.json").write_text("{not valid json\n")

    with pytest.raises(RuntimeError, match="Cannot verify persisted resource claim"):
        runner.submit(
            name="must-not-bypass-corrupt-claim",
            scope_kind="global",
            command=[sys.executable, "-c", "print('must not start')"],
            resources=["camera"],
        )

    assert sorted(path.name for path in job_root.iterdir() if path.is_dir()) == [
        "f" * 12
    ]

    independent = runner.submit(
        name="no-resource-allocation",
        scope_kind="global",
        command=[sys.executable, "-c", "pass"],
    )
    assert runner.wait(independent.id, timeout=5).status == SUCCEEDED


def test_resource_lock_replacement_while_waiting_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if runner_module.fcntl is None:
        pytest.skip("Interprocess file locking is unavailable")
    runner = LocalJobRunner(tmp_path / "jobs")
    real_flock = runner_module.fcntl.flock
    replaced = False

    def replace_lock_after_acquire(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if operation == runner_module.fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path = runner.job_root / runner_module.RESOURCE_LOCK_FILENAME
            lock_path.unlink()
            lock_path.write_text("replacement\n")

    monkeypatch.setattr(runner_module.fcntl, "flock", replace_lock_after_acquire)

    with pytest.raises(RuntimeError, match="lock identity changed"):
        runner.resource_holders()


def test_job_state_directory_replacement_while_waiting_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if runner_module.fcntl is None:
        pytest.skip("Interprocess file locking is unavailable")
    runner = LocalJobRunner(tmp_path / "jobs")
    job_dir = runner.job_root / ("a" * 12)
    job_dir.mkdir()
    displaced = runner.job_root / "displaced"
    replacement = runner.job_root / "replacement"
    replacement.mkdir()
    real_flock = runner_module.fcntl.flock
    replaced = False

    def replace_directory_after_acquire(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if operation == runner_module.fcntl.LOCK_EX and not replaced:
            replaced = True
            job_dir.rename(displaced)
            replacement.rename(job_dir)

    monkeypatch.setattr(runner_module.fcntl, "flock", replace_directory_after_acquire)

    with pytest.raises(RuntimeError, match="identity (changed|disappeared)"):
        with runner._job_state_transaction(job_dir):
            pass


def test_active_claim_with_corrupt_supervisor_identity_fails_closed(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "jobs"
    job_id = "b" * 12
    job_dir = job_root / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "name": "unverifiable-owner",
                "command": [sys.executable, "-c", "import time; time.sleep(30)"],
                "cwd": None,
                "status": "running",
                "created_at": "2026-08-23T00:00:00+00:00",
                "log_path": (job_dir / "log.txt").as_posix(),
                "resources": ["camera"],
                "runner_pid": 999_999_999,
                "runner_start_time": 1,
                "scope_kind": "global",
            }
        )
    )
    (job_dir / "supervisor.json").write_text("{not-json\n")
    runner = LocalJobRunner(job_root)

    with pytest.raises(RuntimeError, match="Cannot verify persisted resource claim"):
        runner.resource_holders()

    assert runner.get(job_id).status == "running"


def test_local_job_runner_applies_hierarchical_resource_conflicts(
    tmp_path: Path,
) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    preview = runner.submit(
        name="preview",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        resources=["camera:realsense_d435:123"],
    )
    try:
        with pytest.raises(ResourceBusyError, match="camera conflicts with"):
            runner.submit(
                name="capture",
                scope_kind="global",
                command=[sys.executable, "-c", "print('blocked')"],
                resources=["camera"],
            )
        other = runner.submit(
            name="other-preview",
            scope_kind="global",
            command=[sys.executable, "-c", "print('allowed')"],
            resources=["camera:realsense_d435:456"],
        )
        assert runner.wait(other.id, timeout=5).status == SUCCEEDED
    finally:
        runner.cancel(preview.id)
        runner.wait(preview.id, timeout=5)


def test_service_visibility_filters_public_jobs_and_resources(tmp_path: Path) -> None:
    runner = LocalJobRunner(tmp_path / "jobs")
    service = runner.submit(
        name="managed-monitor",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["monitoring_camera:0c45:2283"],
        visibility=SERVICE_VISIBILITY,
    )
    operator = runner.submit(
        name="operator-job",
        scope_kind="global",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        resources=["disk_io"],
    )
    try:
        assert [job.id for job in runner.list(include_services=False)] == [operator.id]
        assert runner.resource_holders() == {"disk_io": operator.id}
        assert runner.resource_holders(include_services=True) == {
            "disk_io": operator.id,
            "monitoring_camera:0c45:2283": service.id,
        }
    finally:
        runner.shutdown()


def test_supervisor_stops_workload_descendants_after_owner_sigkill(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Linux parent-death signaling is required")

    ready_path = tmp_path / "owner_ready.json"
    child_ready = tmp_path / "child_pid.txt"
    survived = tmp_path / "descendant_survived.txt"
    job_root = tmp_path / "jobs"
    descendant = (
        "import pathlib, time; time.sleep(3); "
        f"pathlib.Path({str(survived)!r}).write_text('alive'); time.sleep(30)"
    )
    workload = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        f"pathlib.Path({str(child_ready)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    owner = (
        "import json, pathlib, sys, time; "
        "from posetestbot.jobs.runner import LocalJobRunner; "
        f"runner=LocalJobRunner(pathlib.Path({str(job_root)!r})); "
        f"job=runner.submit(name='parent-death', command=[sys.executable, '-c', {workload!r}], scope_kind='global'); "
        f"child=pathlib.Path({str(child_ready)!r}); "
        "deadline=time.time()+5; "
        "\nwhile (runner.get(job.id).process_pid is None or not child.exists()) and time.time()<deadline: time.sleep(0.02)\n"
        "record=runner.get(job.id); "
        f"pathlib.Path({str(ready_path)!r}).write_text(json.dumps({{'supervisor_pid':record.supervisor_pid,'workload_pid':record.process_pid,'child_pid':int(child.read_text())}})); "
        "time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", owner])
    try:
        deadline = time.time() + 8
        while not ready_path.is_file() and time.time() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"owner exited early with {process.returncode}")
            time.sleep(0.02)
        identities = json.loads(ready_path.read_text())
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)

        deadline = time.time() + 7
        while time.time() < deadline:
            if all(
                LocalJobRunner._read_process_start_time(int(pid)) is None
                for pid in identities.values()
            ):
                break
            time.sleep(0.05)
        time.sleep(3.2)

        assert not survived.exists()
        assert all(
            LocalJobRunner._read_process_start_time(int(pid)) is None
            for pid in identities.values()
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for pid in (
            json.loads(ready_path.read_text()).values() if ready_path.is_file() else []
        ):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
