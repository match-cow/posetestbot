import json
import sys

import pytest

from posetestbot.bop import depth_evaluation as depth
from posetestbot.jobs.runner import LocalJobRunner
from posetestbot.web import inspect_jobs
from posetestbot.web.app import create_app
from posetestbot.web.routes import depth_evaluation as routes
from tests.test_bop_evaluation_api import FakeRunner
from tests.test_depth_evaluation import analytic_reference, make_depth_run, submission


@pytest.fixture
def depth_client(tmp_path, monkeypatch):
    root = make_depth_run(tmp_path)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", str(tmp_path))
    runner = FakeRunner()
    monkeypatch.setattr(routes, "job_runner", runner)
    monkeypatch.setattr(
        depth,
        "renderer_status",
        lambda: {
            "available": True,
            "version": "2.8.0",
            "required_version": "2.8.0",
            "device": "CPU",
            "reason": None,
        },
    )
    return create_app().test_client(), runner, root


def test_depth_submission_queues_only_cpu_disk_and_gates_partial_downloads(
    depth_client,
):
    client, runner, root = depth_client
    response = client.get(
        "/depth/evaluation/setup", query_string={"run_root": str(root)}
    )
    assert response.status_code == 200 and response.json["dataset"]["ready"]
    request = submission(root, label="polished")
    response = client.post("/depth/evaluations", json=request)
    assert response.status_code == 202
    record_id = response.json["id"]
    job = runner.submissions[0]
    assert job["resources"] == ["cpu", "disk_io"] and job["scope_kind"] == "run"
    assert job["command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_depth_evaluation.py",
    ]
    path = depth.folder(root, "evaluations", record_id)
    assert (
        json.loads((path / "job.json").read_text())["job_id"] == response.json["job_id"]
    )
    frozen = json.loads((path / "request.json").read_text())
    assert frozen["submission"]["finish_label"] == "polished"
    query = {"run_root": str(root)}
    assert (
        client.get(
            f"/depth/evaluations/{record_id}/download", query_string=query
        ).status_code
        == 404
    )
    report = depth.run_evaluation(
        path / "request.json", render_reference=analytic_reference
    )
    job_id = json.loads((path / "job.json").read_text())["job_id"]
    moved = root.rename(root.with_name("moved #?%"))
    root.symlink_to(moved, target_is_directory=True)
    query = {"run_root": str(moved)}
    history = client.get("/depth/evaluations", query_string=query)
    assert history.status_code == 200 and history.json["records"][0]["job_id"] == job_id
    assert history.json["records"][0]["report_available"]
    with pytest.raises(ValueError, match="run-owned record"):
        depth.run_evaluation(
            depth.folder(moved, "evaluations", record_id) / "request.json"
        )
    for suffix, mimetype in (
        ("report", "application/json"),
        ("frames", "application/json"),
        ("frames/000001_000000", "application/json"),
        ("frames/000001_000000/media/error", "image/png"),
        ("frames/000001_000000/media/numeric", "application/octet-stream"),
        ("download", "application/zip"),
    ):
        response = client.get(
            f"/depth/evaluations/{record_id}/{suffix}", query_string=query
        )
        assert response.status_code == 200 and response.mimetype == mimetype
        if suffix == "report":
            assert response.json == report and response.json["run_root"] == str(root)
    response = client.get(
        f"/depth/evaluations/{record_id}/frames",
        query_string={**query, "limit": 1, "offset": 1},
    )
    assert response.json["frames"] == [] and response.json["total"] == 1
    assert (
        client.get(
            f"/depth/evaluations/{record_id}/frames",
            query_string={**query, "limit": 101},
        ).status_code
        == 400
    )
    assert (
        client.get(
            f"/depth/evaluations/{record_id}/frames/000001_000000/media/secrets",
            query_string=query,
        ).status_code
        == 404
    )


def test_depth_binding_write_failure_prevents_launch_and_removes_unqueued_request(
    depth_client,
    tmp_path,
    monkeypatch,
):
    client, _, root = depth_client
    runner = LocalJobRunner(tmp_path / "jobs")
    monkeypatch.setattr(routes, "job_runner", runner)

    def fail_binding(_path, _value):
        raise OSError("Injected job binding write failure")

    monkeypatch.setattr(inspect_jobs, "atomic_write_json", fail_binding)
    response = client.post("/depth/evaluations", json=submission(root))
    assert (
        response.status_code == 400
        and "binding write failure" in response.json["output"]
    )
    assert not list((root / depth.BASE / "evaluations").iterdir())
    assert runner.list() == [] and runner.resource_holders() == {}
    retry = runner.submit(
        name="retry",
        command=[sys.executable, "-c", "pass"],
        scope_kind="global",
        resources=["cpu", "disk_io"],
    )
    assert runner.wait(retry.id, timeout=5).status == "succeeded"


def test_depth_history_isolates_invalid_and_abandoned_job_bindings(depth_client):
    client, _, root = depth_client
    record_ids = {}
    for label in ("invalid binding", "abandoned request", "queued normally"):
        response = client.post("/depth/evaluations", json=submission(root, label=label))
        assert response.status_code == 202
        record_ids[label] = response.json["id"]

    invalid = depth.folder(root, "evaluations", record_ids["invalid binding"])
    (invalid / "job.json").write_text("{corrupt")
    abandoned = depth.folder(root, "evaluations", record_ids["abandoned request"])
    (abandoned / "job.json").unlink()

    response = client.get("/depth/evaluations", query_string={"run_root": str(root)})
    assert response.status_code == 200
    assert {record["id"]: record["state"] for record in response.json["records"]} == {
        record_ids["invalid binding"]: "invalid",
        record_ids["abandoned request"]: "abandoned",
        record_ids["queued normally"]: "queued",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"finish_label": " "},
        {"schema_version": "depth_evaluation_submission.v0"},
        {"settings": {"erosion_pixels": 0}},
        {"sensors": ["unknown"]},
        {"geometry_hashes": ["a" * 64]},
        {"estimator": "forbidden"},
    ],
)
def test_depth_rejects_invalid_contracts_without_jobs(depth_client, patch):
    client, runner, root = depth_client
    response = client.post("/depth/evaluations", json={**submission(root), **patch})
    assert response.status_code == 400
    assert runner.submissions == []


def test_depth_api_rejects_unapproved_comparison_inputs_and_path_escapes(depth_client):
    client, runner, root = depth_client
    response = client.post(
        "/depth/comparisons",
        json={
            "schema_version": "depth_comparison_submission.v1",
            "run_root": str(root),
            "evaluations": [
                {"run_root": "/etc", "evaluation_id": "depth-eval-0123456789ab"},
                {"run_root": str(root), "evaluation_id": "depth-eval-abcdef012345"},
            ],
        },
    )
    assert response.status_code == 400 and not runner.submissions
    assert (
        client.get(
            "/depth/evaluation/setup", query_string={"run_root": "/etc"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/depth/evaluations/not-a-valid-id/download",
            query_string={"run_root": str(root)},
        ).status_code
        == 404
    )


def test_depth_quality_csv_and_frame_series_are_completion_gated(depth_client):
    client, _runner, root = depth_client
    response = client.post("/depth/evaluations", json=submission(root))
    record_id = response.json["id"]
    query = {"run_root": str(root)}
    for suffix in ("download/csv", "frame-series"):
        assert (
            client.get(f"/depth/evaluations/{record_id}/{suffix}", query_string=query)
            .status_code
            == 404
        )
    depth.run_evaluation(
        depth.folder(root, "evaluations", record_id) / "request.json",
        render_reference=analytic_reference,
    )

    csv_response = client.get(
        f"/depth/evaluations/{record_id}/download/csv", query_string=query
    )
    assert csv_response.status_code == 200
    assert csv_response.mimetype == "text/csv"
    assert f"{record_id}-depth-quality.csv" in csv_response.headers["Content-Disposition"]
    header = csv_response.data.decode().splitlines()[0].split(",")
    assert header[:3] == ["level", "input_id", "finish_label"]
    assert "rmse_mm" in header and "completeness" in header
    series = client.get(f"/depth/evaluations/{record_id}/frame-series", query_string=query)
    assert series.status_code == 200 and series.json["available"] is True
    assert series.json["rows"]
