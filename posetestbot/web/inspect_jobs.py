"""Submission and retained job bindings for run-owned Inspect work."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from posetestbot.bop.inspection import _required_plain_file
from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.runner import RUN_SCOPE, JobRecord, LocalJobRunner
from posetestbot.web.paths import APP_ROOT


def submit_inspect_job(
    runner: LocalJobRunner,
    *,
    root: Path,
    folder: Path,
    name: str,
    script: str,
    parameters: dict,
    args: tuple[str, ...] = (),
) -> JobRecord:
    def bind_job(job_id: str) -> None:
        atomic_write_json(folder / "job.json", {"job_id": job_id})

    try:
        return runner.submit(
            name=name,
            command=[
                "uv",
                "run",
                "python",
                script,
                *args,
                "--request",
                (folder / "request.json").as_posix(),
            ],
            cwd=APP_ROOT,
            resources=["cpu", "disk_io"],
            scope_kind=RUN_SCOPE,
            run_root=root,
            parameters=parameters,
            before_start=bind_job,
        )
    except BaseException:
        shutil.rmtree(folder)
        raise


def read_inspect_job_id(folder: Path) -> str:
    path = _required_plain_file(
        folder / "job.json", root=folder, label="Inspect job binding"
    )
    binding = json.loads(path.read_text())
    job_id = binding.get("job_id") if isinstance(binding, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("Inspect job binding is invalid")
    return job_id


def bound_inspect_job(
    runner: LocalJobRunner,
    *,
    root: Path,
    folder: Path,
    recorded_root: str,
    job_id: str,
    name: str,
    id_parameter: str,
) -> JobRecord:
    job = runner.get(job_id)
    original_request = Path(recorded_root) / folder.relative_to(root) / "request.json"
    if (
        job.run_root != recorded_root
        or job.scope_kind != RUN_SCOPE
        or job.name != name
        or job.parameters.get(id_parameter) != folder.name
        or job.command[-2:] != ["--request", original_request.as_posix()]
    ):
        raise ValueError("Inspect job identity does not match its frozen request")
    return job


def reconcile_inspect_job(
    runner: LocalJobRunner,
    *,
    root: Path,
    path: Path,
    item: dict,
    recorded_root,
    name: str,
    id_parameter: str,
    availability: str,
    state_key: str = "state",
    abandoned_when_unbound: bool = True,
) -> dict:
    """Preserve neighboring records when a job binding is missing or invalid."""
    if item[state_key] == "invalid":
        return item
    pending = not item[availability] and item[state_key] not in {
        "failed",
        "canceled",
        "abandoned",
    }
    binding = path / "job.json"
    try:
        if binding.exists() or binding.is_symlink():
            job_id = read_inspect_job_id(path)
            job = bound_inspect_job(
                runner,
                root=root,
                folder=path,
                recorded_root=recorded_root(),
                job_id=job_id,
                name=name,
                id_parameter=id_parameter,
            )
            item["job_id"] = job_id
            if pending and job.status in {"failed", "canceled", "succeeded"}:
                item[state_key] = "failed" if job.status == "succeeded" else job.status
                item["progress"] = {
                    "state": item[state_key],
                    "error": job.message
                    or "The job ended without publishing its result.",
                }
        elif abandoned_when_unbound and pending and item[state_key] == "queued":
            item[state_key] = "abandoned"
            item["progress"] = {
                "state": "abandoned",
                "error": "No job was bound to this request.",
            }
    except KeyError:
        if pending:
            item[state_key] = "abandoned"
            item["progress"] = {
                "state": "abandoned",
                "error": "The bound job record is unavailable.",
            }
    except (OSError, ValueError) as exc:
        item.update(
            {
                state_key: "invalid",
                availability: False,
                "progress": {"state": "invalid", "error": str(exc)},
            }
        )
    return item
