from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from posetestbot.bop.evaluation import inspect_dataset, list_results
from posetestbot.cluster.client import ClusterClientError
from posetestbot.web.app import create_app
from posetestbot.web.runtime import WebRuntime, WebSettings
from tests.test_bop_evaluation import make_tiny_evaluation_run, write_result_csv


class FakeRunner:
    def __init__(self, root: Path):
        self.job_root = root
        self.submissions: list[dict[str, Any]] = []

    def list(self, *, include_services: bool = True):
        return []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        job_id = f"service-{len(self.submissions)}"

        class FakeJob:
            id = job_id

            def to_dict(self):
                return {
                    "id": self.id,
                    "name": kwargs["name"],
                    "command": kwargs["command"],
                    "status": "queued",
                    "resources": kwargs["resources"],
                    "scope_kind": kwargs["scope_kind"],
                    "parameters": kwargs["parameters"],
                }

        return FakeJob()


def _status() -> dict[str, Any]:
    return {
        "schema_version": "posetestbot_cluster_status.v1",
        "ready": True,
        "connection": {"ready": True},
        "features": {
            "archive_read": True,
            "archive_mutation": True,
            "pose_estimation": True,
        },
        "feature_blockers": {"archive": [], "estimation": []},
        "runtime": {
            "runtime_id": "foundationpose-a1b694b8",
            "foundationpose_revision": "a1b694b83e633c2cb6115b9063d940a687759392",
            "bop_toolkit_revision": "cea62d651c7e395b2e1962b9749e4e89693c6ac4",
            "sif_sha256": "1" * 64,
            "weights_sha256": "2" * 64,
            "weights_files_sha256": "4" * 64,
            "qualification_manifest_sha256": "5" * 64,
            "foundationpose_license": "NVIDIA Source Code License",
            "foundationpose_license_sha256": "3" * 64,
            "qualified": True,
            "ready": True,
        },
        "profiles": [
            {
                "profile_id": "smoke",
                "enabled": True,
                "partition": "gpu",
                "gres": "gpu:1",
                "cpus": 4,
                "memory": "24G",
                "walltime": "00:20:00",
                "max_targets": 2,
            }
        ],
    }


class FakeController:
    def __init__(self):
        self.pose_payload: dict[str, Any] | None = None
        self.pose_key: str | None = None
        self.job_value: dict[str, Any] | None = None
        self.result_source: Path | None = None
        self.provenance_source: Path | None = None
        self.archive_value: dict[str, Any] | None = None
        self.archive_payload: dict[str, Any] | None = None
        self.archive_key: str | None = None
        self.restore_payload: dict[str, Any] | None = None
        self.restore_key: str | None = None
        self.cancel_key: str | None = None

    def status(self):
        return _status()

    def create_pose_job(self, payload, *, idempotency_key: str):
        self.pose_payload = dict(payload)
        self.pose_key = idempotency_key
        job_id = f"pose-{uuid.UUID('12345678-1234-4234-9234-123456789abc')}"
        return {
            "job": {
                "schema_version": "posetestbot_cluster_job.v1",
                "job_id": job_id,
                "state": "preparing",
                "status": "preparing",
                "payload": dict(payload),
            }
        }

    def pose_jobs(self, **_kwargs):
        return {"jobs": [self.job_value] if self.job_value else [], "next_cursor": None}

    def job(self, _job_id: str):
        if self.job_value is None:
            raise KeyError("missing fixture job")
        return {"job": self.job_value}

    def download_artifact(
        self, _job_id, artifact, destination, *, max_bytes=128 * 1024 * 1024
    ):
        source = (
            self.result_source if artifact == "result.csv" else self.provenance_source
        )
        assert source is not None
        assert source.stat().st_size <= max_bytes
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def archives(self, *, verify_archive_id=None):
        if verify_archive_id is not None and self.archive_value is None:
            raise KeyError("missing fixture archive")
        return {
            "archives": [self.archive_value] if self.archive_value else [],
            "verified_archive": self.archive_value if verify_archive_id else None,
        }

    def create_archive(self, payload, *, idempotency_key: str):
        self.archive_payload = dict(payload)
        self.archive_key = idempotency_key
        return {"archive": self.archive_value}

    def restore_archive(self, _archive_id, payload, *, idempotency_key: str):
        self.restore_payload = dict(payload)
        self.restore_key = idempotency_key
        return {"job": self.job_value}

    def cancel_job(self, _job_id, *, idempotency_key: str):
        self.cancel_key = idempotency_key
        return {"job": self.job_value}

    def job_log(self, _job_id):
        return "controller log\nremote /secret/work\nAuthorization: Bearer fixture\n"


class GenericController(FakeController):
    def __init__(self):
        super().__init__()
        self.estimation_payload: dict[str, Any] | None = None
        self.estimation_key: str | None = None

    def status(self):
        profile = {
            "profile_id": "smoke",
            "enabled": True,
            "partition": "gpu",
            "gres": "gpu:1",
            "cpus": 4,
            "memory": "24G",
            "walltime": "00:20:00",
            "max_targets": 2,
        }
        return {
            "schema_version": "posetestbot_cluster_status.v1",
            "ready": True,
            "connection": {"ready": True},
            "features": {
                "archive_read": True,
                "archive_mutation": True,
                "estimation_submission": True,
            },
            "feature_blockers": {
                "archive": [],
                "estimation": [],
                "estimation_submission": [],
            },
            "domains": {
                "storage": {
                    "ready": True,
                    "read": True,
                    "mutation": True,
                    "blockers": [],
                },
                "scheduler": {"ready": True, "blockers": []},
            },
            "estimators": [
                {
                    "estimator_id": "megapose",
                    "driver_id": "megapose.v1",
                    "display_name": "MegaPose",
                    "installed": True,
                    "configured": True,
                    "enabled": True,
                    "ready": True,
                    "blockers": [],
                    "readiness_blockers": [],
                    "input_contracts": ["posetestbot.bop.v5.pose_and_masks"],
                    "output_contract": "bop19.csv.v1",
                    "runtime": {
                        "estimator_id": "megapose",
                        "driver_id": "megapose.v1",
                        "runtime_id": "megapose-fixture",
                        "container": {
                            "filename": "megapose.sif",
                            "sha256": "6" * 64,
                        },
                        "assets": {
                            "weights": {
                                "filename": "weights.json",
                                "sha256": "7" * 64,
                            },
                            "bad": {
                                "filename": "/secret/weights.json",
                                "sha256": "8" * 64,
                            },
                        },
                        "source_revisions": {
                            "megapose": "abcdef0123456789",
                            "private": "/secret/checkout",
                        },
                        "licenses": [
                            {"name": "Fixture license", "sha256": "9" * 64}
                        ],
                        "input_contracts": [
                            "posetestbot.bop.v5.pose_and_masks"
                        ],
                        "output_contract": "bop19.csv.v1",
                        "qualified_resource_profiles": ["smoke"],
                        "qualification_manifest_sha256": "a" * 64,
                        "qualified": True,
                        "ready": True,
                        "qualification_blockers": [],
                    },
                    "profiles": [profile],
                }
            ],
            "runtime": {},
            "profiles": [],
            "blockers": [],
        }

    def create_estimation_job(self, payload, *, idempotency_key: str):
        self.estimation_payload = dict(payload)
        self.estimation_key = idempotency_key
        job_id = f"pose-{uuid.UUID('12345678-1234-4234-9234-123456789abc')}"
        return {
            "job": {
                "schema_version": "posetestbot_cluster_job.v1",
                "job_id": job_id,
                "kind": "estimation",
                "state": "preparing",
                "status": "preparing",
                "payload": dict(payload),
            }
        }

    def estimation_jobs(self, **_kwargs):
        return {
            "jobs": [self.job_value] if self.job_value else [],
            "next_cursor": None,
        }


class ArchiveOnlyController(GenericController):
    def status(self):
        status = super().status()
        status["ready"] = False
        status["connection"] = {"ready": False}
        status["domains"]["scheduler"] = {
            "ready": False,
            "blockers": ["The LUIS login host is not connected."],
        }
        status["estimators"][0]["ready"] = False
        status["estimators"][0]["readiness_blockers"] = [
            "The LUIS login host is not connected."
        ]
        status["blockers"] = ["The LUIS login host is not connected."]
        return status


class OfflineController(FakeController):
    def status(self):
        raise ClusterClientError("Cluster controller is unavailable")


class BlockedController(FakeController):
    def status(self):
        status = _status()
        status["ready"] = False
        status["connection"] = {"ready": False}
        status["blockers"] = [
            "PROJECT quota cannot currently be verified.",
            {
                "code": "login_host_unavailable",
                "message": "The LUIS login host did not answer.",
            },
        ]
        return status


def _pose_ready_run(root: Path) -> Path:
    run = make_tiny_evaluation_run(root, name="pose-ready")
    scene = run / "bop" / "test" / "000001"
    (scene / "mask_visib").mkdir()
    assert cv2.imwrite(
        (scene / "mask_visib" / "000000_000000.png").as_posix(),
        np.full((8, 8), 255, dtype=np.uint8),
    )
    manifest_path = run / "bop" / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["annotation_mode"] = "pose_and_masks"
    manifest["capabilities"].update(
        {"gt_masks_full": True, "gt_masks_visible": True, "gt_visibility_info": True}
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return run


def _app(
    tmp_path: Path,
    controller,
    *,
    enabled: bool = True,
    service_manager=None,
    cluster_env_file: Path | None = None,
):
    runs_root = tmp_path / "runs"
    runs_root.mkdir(exist_ok=True)
    settings = WebSettings(
        host="127.0.0.1",
        port=5000,
        debug=False,
        job_root=tmp_path / "jobs",
        cluster_url="http://127.0.0.1:8765",
        cluster_token="x" * 32,
        cluster_enabled=enabled,
        cluster_env_file=cluster_env_file,
    )
    runner = FakeRunner(settings.job_root)
    runtime = WebRuntime(settings, runner, controller, service_manager)
    return create_app(runtime=runtime), runs_root


class FakeControllerServiceManager:
    def __init__(self) -> None:
        self.state = "stopped"
        self.commands: list[str] = []

    def status(self):
        active = self.state == "running"
        return {
            "managed": True,
            "service_unit": "posetestbot-cluster.service",
            "unit_installed": True,
            "state": self.state,
            "active": active,
            "can_start": not active,
            "can_stop": active,
            "load_state": "loaded",
            "active_state": "active" if active else "inactive",
            "sub_state": "running" if active else "dead",
            "unit_file_state": "disabled",
            "blockers": [],
            "private_path": "/must/not/reach/browser",
        }

    def command(self, action: str):
        self.commands.append(action)
        return [
            "/usr/bin/systemctl",
            "--user",
            "--no-block",
            action,
            "posetestbot-cluster.service",
        ]


def test_cluster_controller_service_status_and_actions_are_server_owned(
    tmp_path: Path,
) -> None:
    manager = FakeControllerServiceManager()
    app, _runs_root = _app(
        tmp_path,
        FakeController(),
        enabled=False,
        service_manager=manager,
        cluster_env_file=tmp_path / "private" / "controller.env",
    )
    client = app.test_client()

    status = client.get("/cluster/controller-service")
    rejected = client.post(
        "/cluster/controller-service/start", json={"confirm": False}
    )
    caller_controlled = client.post(
        "/cluster/controller-service/start",
        json={"confirm": True, "service_unit": "caller-controlled.service"},
    )
    started = client.post(
        "/cluster/controller-service/start", json={"confirm": True}
    )

    assert status.status_code == 200
    assert status.get_json()["state"] == "stopped"
    assert status.get_json()["integration"] == {
        "enabled": False,
        "controller_configured": True,
        "environment_file_configured": True,
    }
    assert "private_path" not in status.get_data(as_text=True)
    assert "controller.env" not in status.get_data(as_text=True)
    assert "xxxxxxxx" not in status.get_data(as_text=True)
    assert rejected.status_code == 400
    assert caller_controlled.status_code == 400
    assert started.status_code == 202
    assert started.get_json()["job"]["command"][-1] == (
        "posetestbot-cluster.service"
    )
    assert "caller-controlled" not in started.get_data(as_text=True)
    assert manager.commands == ["start"]

    manager.state = "running"
    stopped = client.post(
        "/cluster/controller-service/stop", json={"confirm": True}
    )

    assert stopped.status_code == 202
    assert stopped.get_json()["job"]["scope_kind"] == "global"
    assert stopped.get_json()["job"]["resources"] == [
        "cluster_controller_service"
    ]
    assert manager.commands == ["start", "stop"]


def test_cluster_controller_service_is_explicitly_unmanaged_by_default(
    tmp_path: Path,
) -> None:
    app, _runs_root = _app(tmp_path, FakeController())
    client = app.test_client()

    status = client.get("/cluster/controller-service")
    action = client.post(
        "/cluster/controller-service/start", json={"confirm": True}
    )

    assert status.status_code == 200
    assert status.get_json()["state"] == "unmanaged"
    assert status.get_json()["can_start"] is False
    assert action.status_code == 409


def test_pose_setup_submission_is_server_revalidated_and_loopback_proxied(
    tmp_path: Path, monkeypatch
) -> None:
    controller = FakeController()
    app, runs_root = _app(tmp_path, controller)
    run = _pose_ready_run(runs_root)
    (run / "run_config.json").write_text("{}\n")
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    client = app.test_client()

    setup = client.get(
        "/cluster/pose-estimation/setup", query_string={"run_root": run.as_posix()}
    )
    assert setup.status_code == 200
    assert setup.get_json()["ready"] is True
    assert setup.get_json()["annotation_mode"] == "pose_and_masks"
    assert setup.get_json()["oracle_mask_contract"] == "bop_mask_visib_gt_instance.v1"

    submitted = client.post(
        "/cluster/pose-estimation/jobs",
        json={
            "run_root": run.as_posix(),
            "profile_id": "smoke",
            "operator": "Fixture Operator",
            "dataset_sha256": "caller-must-not-control-this",
        },
    )
    assert submitted.status_code == 202
    dataset = inspect_dataset(run, include_depth_content=True)
    assert controller.pose_payload == {
        "run_root": run.as_posix(),
        "dataset_alias": dataset["dataset_alias"],
        "dataset_sha256": dataset["dataset_sha256"],
        "profile_id": "smoke",
        "operator": "Fixture Operator",
    }
    assert controller.pose_key is not None
    assert controller.pose_key.startswith("pose-submit:")


def test_generic_estimator_is_discovered_selected_and_submitted(
    tmp_path: Path, monkeypatch
) -> None:
    controller = GenericController()
    app, runs_root = _app(tmp_path, controller)
    run = _pose_ready_run(runs_root)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    client = app.test_client()

    setup_response = client.get(
        "/cluster/pose-estimation/setup",
        query_string={
            "run_root": run.as_posix(),
            "estimator_id": "megapose",
        },
    )

    assert setup_response.status_code == 200
    setup = setup_response.get_json()
    assert setup["schema_version"] == "cluster_estimation_setup.v2"
    assert setup["estimator_id"] == "megapose"
    assert setup["estimator"]["display_name"] == "MegaPose"
    assert setup["ready"] is True
    serialized = setup_response.get_data(as_text=True)
    assert "/secret" not in serialized
    assert "bad" not in setup["runtime"]["assets"]
    assert "private" not in setup["runtime"]["source_revisions"]

    submitted = client.post(
        "/cluster/pose-estimation/jobs",
        json={
            "run_root": run.as_posix(),
            "estimator_id": "megapose",
            "profile_id": "smoke",
            "operator": "Fixture Operator",
        },
    )

    assert submitted.status_code == 202
    dataset = inspect_dataset(run, include_depth_content=True)
    assert controller.estimation_payload == {
        "estimator_id": "megapose",
        "run_root": run.as_posix(),
        "dataset_alias": dataset["dataset_alias"],
        "dataset_sha256": dataset["dataset_sha256"],
        "profile_id": "smoke",
        "operator": "Fixture Operator",
    }
    assert controller.estimation_key is not None
    assert controller.estimation_key.startswith("estimation-submit:")


def test_archive_storage_status_is_independent_of_estimator_readiness(
    tmp_path: Path
) -> None:
    controller = ArchiveOnlyController()
    app, _runs_root = _app(tmp_path, controller)

    response = app.test_client().get("/cluster/archives")

    assert response.status_code == 200
    assert response.get_json()["storage"] == {
        "ready": True,
        "read": True,
        "mutation": True,
        "blockers": [],
    }


def test_pose_setup_exposes_controller_outage_and_containment_blockers(
    tmp_path: Path, monkeypatch
) -> None:
    app, runs_root = _app(tmp_path, OfflineController())
    run = _pose_ready_run(runs_root)
    outside = _pose_ready_run(tmp_path / "outside")
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    client = app.test_client()

    setup = client.get(
        "/cluster/pose-estimation/setup", query_string={"run_root": run.as_posix()}
    )
    assert setup.status_code == 200
    assert setup.get_json()["ready"] is False
    assert any(
        item["code"] == "controller_unavailable"
        for item in setup.get_json()["blockers"]
    )

    escaped = client.get(
        "/cluster/pose-estimation/setup",
        query_string={"run_root": outside.as_posix()},
    )
    assert escaped.status_code == 400
    assert "allowed root" in escaped.get_json()["output"]


def test_pose_setup_preserves_structured_controller_readiness_blockers(
    tmp_path: Path, monkeypatch
) -> None:
    app, runs_root = _app(tmp_path, BlockedController())
    run = _pose_ready_run(runs_root)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    client = app.test_client()

    response = client.get(
        "/cluster/pose-estimation/setup", query_string={"run_root": run.as_posix()}
    )

    assert response.status_code == 200
    setup = response.get_json()
    assert setup["ready"] is False
    assert {(item["code"], item["message"]) for item in setup["blockers"]} >= {
        (
            "controller_blocker_1",
            "PROJECT quota cannot currently be verified.",
        ),
        ("login_host_unavailable", "The LUIS login host did not answer."),
    }


def _successful_external_job(
    controller: FakeController, run: Path, tmp_path: Path
) -> str:
    dataset = inspect_dataset(run)
    job_id = "pose-12345678-1234-4234-9234-123456789abc"
    result = write_result_csv(
        tmp_path / f"foundationpose_{dataset['dataset_alias']}-test_{job_id}.csv"
    )
    result_hash = hashlib.sha256(result.read_bytes()).hexdigest()
    provenance = {
        "schema_version": "posetestbot_cluster_collected_result.v1",
        "job_id": job_id,
        "method": "foundationpose",
        "dataset_sha256": dataset["dataset_sha256"],
        "oracle_mask_contract": "bop_mask_visib_gt_instance.v1",
        "score_contract": "constant_1.0_no_detection_confidence",
        "execution_contract": "independent_register_per_target_no_tracking.v1",
        "units": {
            "bop_model": "millimetres",
            "bop_depth": "millimetres",
            "foundationpose": "metres",
            "result_translation": "millimetres",
        },
        "runtime": _status()["runtime"],
        "input_manifest_sha256": "3" * 64,
        "input_hashes": {"rgb": "4" * 64, "depth": "5" * 64},
        "bop_content_sha256": "6" * 64,
        "output_hashes": {result.name: result_hash},
        "project_copy": {
            "state": "verified",
            "artifact_sha256": {result.name: result_hash},
        },
        "estimate_count": 1,
        "failure_count": 0,
        "collected_at": "2026-08-04T12:00:00+00:00",
        "remote_work_dir": f"/secret/project/results/{job_id}",
        "scheduler": {"command": "sbatch --secret=/secret/token"},
        "external_job": {
            "provider": "posetestbot-cluster",
            "job_id": job_id,
            "slurm_job_id": "81234",
        },
        "result": {
            "filename": result.name,
            "sha256": result_hash,
            "size_bytes": result.stat().st_size,
        },
    }
    provenance_path = tmp_path / "controller-provenance.json"
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    controller.result_source = result
    controller.provenance_source = provenance_path
    controller.job_value = {
        "schema_version": "posetestbot_cluster_job.v1",
        "job_id": job_id,
        "kind": "pose-estimation",
        "state": "succeeded",
        "status": "succeeded",
        "payload": {"run_root": run.as_posix()},
        "result": {
            "filename": result.name,
            "sha256": result_hash,
            "provenance_sha256": hashlib.sha256(
                provenance_path.read_bytes()
            ).hexdigest(),
            "dataset_sha256": dataset["dataset_sha256"],
            "estimate_count": 1,
            "failure_count": 0,
        },
        "terminal": True,
    }
    return job_id


def _successful_generic_external_job(
    controller: GenericController, run: Path, tmp_path: Path
) -> str:
    dataset = inspect_dataset(run)
    job_id = "pose-22345678-1234-4234-9234-123456789abc"
    result = write_result_csv(
        tmp_path / f"megapose_{dataset['dataset_alias']}-test_{job_id}.csv"
    )
    result_hash = hashlib.sha256(result.read_bytes()).hexdigest()
    runtime = {
        "estimator_id": "megapose",
        "driver_id": "megapose.v1",
        "runtime_id": "megapose-fixture",
        "container": {"filename": "megapose.sif", "sha256": "1" * 64},
        "assets": {
            "weights": {"filename": "weights.json", "sha256": "2" * 64}
        },
        "source_revisions": {"megapose": "abcdef0123456789"},
        "build_provenance": {"base_image_digest": f"sha256:{'3' * 64}"},
        "licenses": [{"name": "Fixture license", "sha256": "4" * 64}],
        "input_contracts": ["posetestbot.bop.v5.pose_and_masks"],
        "output_contract": "bop19.csv.v1",
        "qualified_resource_profiles": ["smoke"],
        "qualification_manifest_sha256": "5" * 64,
        "qualified": True,
        "ready": True,
        "qualification_blockers": [],
        "private_path": "/secret/runtime",
    }
    estimator = {
        "estimator_id": "megapose",
        "driver_id": "megapose.v1",
        "runtime_id": "megapose-fixture",
        "input_contracts": ["posetestbot.bop.v5.pose_and_masks"],
        "output_contract": "bop19.csv.v1",
    }
    provenance = {
        "schema_version": "posetestbot_cluster_collected_result.v1",
        "job_id": job_id,
        "dataset_sha256": dataset["dataset_sha256"],
        "bop_content_sha256": "6" * 64,
        "input_manifest_sha256": "7" * 64,
        "input_hashes": {"rgb": "8" * 64, "depth": "9" * 64},
        "runtime": runtime,
        "estimator": estimator,
        "external_job": {
            "provider": "posetestbot-cluster",
            "job_id": job_id,
            "slurm_job_id": "91234",
            "estimator_id": "megapose",
            "driver_id": "megapose.v1",
            "runtime_id": "megapose-fixture",
        },
        "result": {
            "filename": result.name,
            "sha256": result_hash,
            "size_bytes": result.stat().st_size,
        },
        "output_hashes": {result.name: result_hash},
        "project_copy": {
            "state": "verified",
            "artifact_sha256": {result.name: result_hash},
        },
        "estimate_count": 1,
        "failure_count": 0,
        "collected_at": "2026-08-06T12:00:00+00:00",
        "remote_work_dir": "/secret/project/results",
    }
    provenance_path = tmp_path / "generic-controller-provenance.json"
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    controller.result_source = result
    controller.provenance_source = provenance_path
    controller.job_value = {
        "schema_version": "posetestbot_cluster_job.v1",
        "job_id": job_id,
        "kind": "estimation",
        "state": "succeeded",
        "status": "succeeded",
        "payload": {
            "run_root": run.as_posix(),
            "estimator_id": "megapose",
            "driver_id": "megapose.v1",
            "runtime_id": "megapose-fixture",
        },
        "result": {
            "filename": result.name,
            "sha256": result_hash,
            "provenance_sha256": hashlib.sha256(
                provenance_path.read_bytes()
            ).hexdigest(),
            "dataset_sha256": dataset["dataset_sha256"],
            "estimator_id": "megapose",
            "runtime_id": "megapose-fixture",
            "estimate_count": 1,
            "failure_count": 0,
        },
        "terminal": True,
    }
    return job_id


def test_external_result_import_is_idempotent_and_historical_download_survives_drift(
    tmp_path: Path, monkeypatch
) -> None:
    controller = FakeController()
    app, runs_root = _app(tmp_path, controller)
    run = _pose_ready_run(runs_root)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    job_id = _successful_external_job(controller, run, tmp_path)
    client = app.test_client()

    first = client.post(
        f"/cluster/jobs/{job_id}/import-result", json={"run_root": run.as_posix()}
    )
    second = client.post(
        f"/cluster/jobs/{job_id}/import-result", json={"run_root": run.as_posix()}
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert (
        first.get_json()["result"]["result_id"]
        == second.get_json()["result"]["result_id"]
    )
    assert second.get_json()["created"] is False
    records = list_results(run)
    assert len(records) == 1
    assert records[0]["source_kind"] == "external_controller"
    assert records[0]["external_job"]["slurm_job_id"] == "81234"
    stored = json.loads((run / records[0]["controller_provenance_path"]).read_text())
    assert stored["schema_version"] == "posetestbot_external_result_provenance.v1"
    assert "project_copy" not in stored and "scheduler" not in stored
    assert "/secret" not in json.dumps(stored)

    manifest = run / "bop" / "bop_export_manifest.json"
    manifest.write_text(manifest.read_text() + " ")
    result_id = records[0]["result_id"]
    download = client.get(
        f"/bop/evaluation/results/{result_id}/download",
        query_string={"run_root": run.as_posix()},
    )
    assert download.status_code == 200
    assert hashlib.sha256(download.data).hexdigest() == records[0]["sha256"]


def test_generic_external_result_import_retains_neutral_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    controller = GenericController()
    app, runs_root = _app(tmp_path, controller)
    run = _pose_ready_run(runs_root)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    job_id = _successful_generic_external_job(controller, run, tmp_path)

    response = app.test_client().post(
        f"/cluster/jobs/{job_id}/import-result",
        json={"run_root": run.as_posix()},
    )

    assert response.status_code == 201
    [record] = list_results(run)
    assert record["method"] == "megapose"
    assert record["method_name"] == "Megapose (cluster)"
    stored = json.loads((run / record["controller_provenance_path"]).read_text())
    assert stored["method"] == "megapose"
    assert stored["external_job"]["driver_id"] == "megapose.v1"
    assert stored["runtime"]["container"]["filename"] == "megapose.sif"
    assert stored["runtime"]["output_contract"] == "bop19.csv.v1"
    assert "/secret" not in json.dumps(stored)


def test_external_result_import_refuses_dataset_drift_without_losing_remote_result(
    tmp_path: Path, monkeypatch
) -> None:
    controller = FakeController()
    app, runs_root = _app(tmp_path, controller)
    run = _pose_ready_run(runs_root)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    job_id = _successful_external_job(controller, run, tmp_path)
    manifest = run / "bop" / "bop_export_manifest.json"
    manifest.write_text(manifest.read_text() + " ")

    response = app.test_client().post(
        f"/cluster/jobs/{job_id}/import-result", json={"run_root": run.as_posix()}
    )
    assert response.status_code == 409
    assert "changed after this cluster job was staged" in response.get_json()["output"]
    assert list_results(run) == []
    assert controller.result_source is not None and controller.result_source.is_file()


def test_cluster_jobs_logs_cancel_and_archive_copy_restore_use_server_keys(
    tmp_path: Path, monkeypatch
) -> None:
    controller = FakeController()
    app, runs_root = _app(tmp_path, controller)
    run = _pose_ready_run(runs_root)
    (run / "run_config.json").write_text("{}\n")
    identity = {"device": run.stat().st_dev, "inode": run.stat().st_ino}
    archive_id = "archive-12345678-1234-4234-9234-123456789abc"
    job_id = "pose-12345678-1234-4234-9234-123456789abc"
    controller.archive_value = {
        "archive_id": archive_id,
        "state": "succeeded",
        "status": "succeeded",
        "source_run_root": run.as_posix(),
        "source_identity": identity,
        "verified": True,
        "remote_path": "/secret/project/archive",
    }
    controller.job_value = {
        "job_id": job_id,
        "kind": "pose-estimation",
        "state": "running",
        "status": "running",
        "payload": {"run_root": run.as_posix(), "remote_path": "/secret/work"},
        "error": "remote failure at /secret/work",
        "log_available": True,
        "terminal": False,
    }
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    client = app.test_client()

    created = client.post(
        "/cluster/archives",
        headers={"Idempotency-Key": "browser-controlled"},
        json={
            "run_root": run.as_posix(),
            "expected_identity": identity,
            "operator": "Fixture Operator",
        },
    )
    assert created.status_code == 202, created.get_json()
    assert controller.archive_payload == {
        "run_root": run.as_posix(),
        "operator": "Fixture Operator",
    }
    assert controller.archive_key is not None
    assert controller.archive_key.startswith("archive-copy:")
    assert controller.archive_key != "browser-controlled"
    assert "remote_path" not in created.get_json()["archive"]

    listed = client.get("/cluster/archives")
    assert listed.status_code == 200
    assert listed.get_json()["integration"] == {"enabled": True}
    assert listed.get_json()["archives"][0]["archive_id"] == archive_id

    restored = client.post(
        f"/cluster/archives/{archive_id}/restore",
        headers={"Idempotency-Key": "browser-controlled"},
        json={
            "destination_root": runs_root.as_posix(),
            "destination_name": "restored-run",
            "operator": "Fixture Operator",
        },
    )
    assert restored.status_code == 202
    assert controller.restore_payload == {
        "destination_root": runs_root.as_posix(),
        "destination_name": "restored-run",
        "operator": "Fixture Operator",
    }
    assert controller.restore_key is not None
    assert controller.restore_key.startswith("archive-restore:")

    job = client.get(f"/cluster/jobs/{job_id}", query_string={"include_log": "1"})
    assert job.status_code == 200
    assert job.get_json()["log"] == (
        "controller log\nremote [controller path]\n[redacted controller detail]\n"
    )
    assert job.get_json()["job"]["error"] == "remote failure at [controller path]"
    assert "remote_path" not in job.get_json()["job"]["payload"]

    canceled = client.post(
        f"/cluster/jobs/{job_id}/cancel",
        headers={"Idempotency-Key": "browser-controlled"},
    )
    assert canceled.status_code == 202
    assert controller.cancel_key is not None
    assert controller.cancel_key.startswith("job-cancel:")
