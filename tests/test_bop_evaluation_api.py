from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import posetestbot.bop.evaluation as evaluation_module
from posetestbot.bop.evaluation import import_bop_result, inspect_dataset
from posetestbot.web.app import create_app
from tests.test_bop_evaluation import make_tiny_evaluation_run, write_result_csv


@dataclass
class FakeJob:
    id: str
    submission: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.submission["name"],
            "command": self.submission["command"],
            "cwd": str(self.submission.get("cwd") or ""),
            "status": "queued",
            "created_at": "2026-07-26T00:00:00+00:00",
            "started_at": None,
            "ended_at": None,
            "returncode": None,
            "message": None,
            "tail": [],
            "resources": self.submission["resources"],
            "parameters": self.submission["parameters"],
            "log_path": "log.txt",
            "visibility": "operator",
        }


class FakeRunner:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []

    def submit(self, **kwargs: Any) -> FakeJob:
        job = FakeJob(f"bopeval{len(self.submissions) + 1}", kwargs)
        if before_start := kwargs.get("before_start"):
            before_start(job.id)
        self.submissions.append(kwargs)
        return job

    def get(self, job_id: str):
        for index, submission in enumerate(self.submissions, 1):
            if job_id == f"bopeval{index}":
                return SimpleNamespace(
                    **{
                        key: submission[key]
                        for key in ("name", "command", "scope_kind", "parameters")
                    },
                    run_root=str(submission["run_root"]),
                    status="queued",
                )
        raise KeyError(job_id)


def _client_for_run(tmp_path: Path, monkeypatch):
    runs_root = tmp_path / "runs"
    run_root = make_tiny_evaluation_run(runs_root, name="format_check-run")
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    monkeypatch.setenv("POSETESTBOT_WEB_INPUT_ROOTS", tmp_path.as_posix())
    app = create_app()
    runner = FakeRunner()
    rule = next(
        rule
        for rule in app.url_map.iter_rules()
        if rule.rule == "/bop/evaluations" and "POST" in rule.methods
    )
    view_module = sys.modules[app.view_functions[rule.endpoint].__module__]
    monkeypatch.setattr(view_module, "job_runner", runner)
    monkeypatch.setattr(
        view_module,
        "toolkit_status",
        lambda _app_root: {
            "status": "ready",
            "available": True,
            "revision": "fixture",
            "required_revision": "fixture",
            "environment_ready": True,
            "renderer": "vispy",
            "install_command": None,
            "reason": None,
        },
    )
    return app.test_client(), runner, run_root


def _assert_evaluation_submission(submission: dict[str, Any]) -> dict[str, Any]:
    assert submission["name"] == "bop_evaluation"
    assert submission["resources"] == ["cpu", "disk_io"]
    assert submission["scope_kind"] == "run"
    assert submission["run_root"] == Path(submission["parameters"]["run_root"])
    assert submission["command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_bop_evaluation.py",
    ]
    assert submission["command"][4] == "--request"
    request_path = Path(submission["command"][5])
    assert request_path.is_file()
    request_value = json.loads(request_path.read_text())
    assert submission["parameters"]["evaluation_id"] == request_value["evaluation_id"]
    assert submission["parameters"]["request_path"] == request_path.as_posix()
    return request_value


def test_setup_and_result_import_expose_compatible_method_choices(
    tmp_path: Path, monkeypatch
) -> None:
    client, runner, run_root = _client_for_run(tmp_path, monkeypatch)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(
        tmp_path / f"foundationpose_{dataset['dataset_alias']}-test.csv"
    )

    setup = client.get(
        "/bop/evaluation/setup", query_string={"run_root": run_root.as_posix()}
    )

    assert setup.status_code == 200
    assert setup.get_json()["dataset"]["evaluation_ready"] is True
    assert setup.get_json()["dataset"]["dataset_alias"] == dataset["dataset_alias"]
    assert setup.get_json()["results"] == []
    assert setup.get_json()["evaluations"] == []

    imported = client.post(
        "/bop/evaluation/results",
        data={
            "run_root": run_root.as_posix(),
            "display_name": "FoundationPose",
            "file": (
                io.BytesIO(source.read_bytes()),
                source.name,
            ),
        },
        content_type="multipart/form-data",
    )

    assert imported.status_code == 201
    record = imported.get_json()["result"]
    assert record["method_name"] == "FoundationPose"
    assert record["simulated"] is False
    assert runner.submissions == []
    refreshed = client.get(
        "/bop/evaluation/setup", query_string={"run_root": run_root.as_posix()}
    ).get_json()
    assert [item["result_id"] for item in refreshed["results"]] == [record["result_id"]]


def test_real_result_evaluation_is_queued_with_only_cpu_and_disk_resources(
    tmp_path: Path, monkeypatch
) -> None:
    client, runner, run_root = _client_for_run(tmp_path, monkeypatch)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(
        tmp_path / f"megapose_{dataset['dataset_alias']}-test.csv"
    )
    result = import_bop_result(run_root, source, method_name="MegaPose")

    response = client.post(
        "/bop/evaluations",
        json={"run_root": run_root.as_posix(), "source": {"kind": "registered_result", "result_id": result["result_id"]}},
    )

    assert response.status_code == 202
    assert len(runner.submissions) == 1
    request_value = _assert_evaluation_submission(runner.submissions[0])
    assert request_value["result_id"] == result["result_id"]
    assert request_value["simulation"] is None
    payload = response.get_json()
    assert payload["job"]["id"] == "bopeval1"
    assert payload["evaluation"]["evaluation_id"] == request_value["evaluation_id"]


def test_simulated_result_generation_and_evaluation_are_queued_together(
    tmp_path: Path, monkeypatch
) -> None:
    client, runner, run_root = _client_for_run(tmp_path, monkeypatch)
    simulation = {
        "method_name": "GT slight offset",
        "translation_sigma_mm": 1.0,
        "rotation_sigma_deg": 0.25,
        "seed": 42,
        "score": 1.0,
    }

    response = client.post(
        "/bop/evaluations",
        json={"run_root": run_root.as_posix(), "source": {"kind": "gt_simulation", **simulation}},
    )

    assert response.status_code == 202
    assert len(runner.submissions) == 1
    request_value = _assert_evaluation_submission(runner.submissions[0])
    assert request_value["result_id"] is None
    assert request_value["simulation"] == simulation
    payload = response.get_json()
    assert payload["job"]["id"] == "bopeval1"
    assert payload["evaluation"]["simulation"] == simulation


def test_setup_reuses_one_dataset_inspection_and_does_not_rehash_result_csvs(
    tmp_path: Path, monkeypatch
) -> None:
    client, _runner, run_root = _client_for_run(tmp_path, monkeypatch)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(tmp_path / f"method_{dataset['dataset_alias']}-test.csv")
    import_bop_result(run_root, source, method_name="Method")

    rule = next(
        rule
        for rule in client.application.url_map.iter_rules()
        if rule.rule == "/bop/evaluation/setup" and "GET" in rule.methods
    )
    view_module = sys.modules[
        client.application.view_functions[rule.endpoint].__module__
    ]
    original_inspect = view_module.inspect_dataset
    inspections = 0

    def counted_inspection(run):
        nonlocal inspections
        inspections += 1
        return original_inspect(run)

    original_sha256 = evaluation_module._sha256_file

    def no_result_rehash(path: Path) -> str:
        if path.suffix == ".csv":
            raise AssertionError("Setup must not content-hash registered CSVs")
        return original_sha256(path)

    monkeypatch.setattr(view_module, "inspect_dataset", counted_inspection)
    monkeypatch.setattr(evaluation_module, "_sha256_file", no_result_rehash)

    response = client.get(
        "/bop/evaluation/setup",
        query_string={"run_root": run_root.as_posix()},
    )

    assert response.status_code == 200
    assert inspections == 1
    assert len(response.get_json()["results"]) == 1
