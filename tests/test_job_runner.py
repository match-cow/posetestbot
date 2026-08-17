from __future__ import annotations

import json

import os

import signal

import subprocess

import sys

import time

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
