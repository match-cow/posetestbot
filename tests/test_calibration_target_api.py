from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.calibration.posegridgen import posegridgen_capabilities
from posetestbot.calibration.target_library import (
    generate_target_bundle,
    select_target_bundle,
)
from posetestbot.io.artifacts import ARUCO_DETECTIONS
from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_configs_from_values,
    write_run_config_with_manifest,
)
from posetestbot.web.app import create_app
from posetestbot.web.routes import calibration_targets as routes


@dataclass
class FakeJob:
    id: str = "targetjob123"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": "calibration_target",
            "command": [],
            "cwd": None,
            "status": "queued",
            "created_at": "2026-07-16T00:00:00+00:00",
            "started_at": None,
            "ended_at": None,
            "returncode": None,
            "message": None,
            "tail": [],
            "resources": [],
            "parameters": {},
            "log_path": "log.txt",
            "visibility": "operator",
        }


class FakeRunner:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return FakeJob(id=f"targetjob{len(self.submissions)}")


def configuration() -> dict:
    value = copy.deepcopy(posegridgen_capabilities()["defaults"])
    value["page"]["orientation"] = "landscape"
    value["board"].update({"rows": 2, "columns": 2, "marker_size_mm": 25.0})
    value["annotations"] = {
        "show_ruler": False,
        "show_parameters": False,
        "show_frame_legend": False,
    }
    return value


@pytest.fixture
def target_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    library = tmp_path / "library"
    app_root = tmp_path / "app"
    app_root.mkdir()
    run = tmp_path / "runs" / "run"
    write_run_config_with_manifest(
        run,
        create_run_config(
            capture_intent="calibration",
            bop_annotation_mode="none",
            run_root=run,
            sensors=sensor_configs_from_values(
                [
                    {
                        "sensor_type": "realsense_d435",
                        "device_id": "static-1",
                        "display_name": "Static D435",
                        "mounting_mode": "static",
                    }
                ]
            ),
        ),
    )
    bundle = generate_target_bundle(
        display_name="API target",
        configuration=configuration(),
        library_root=library,
    )
    runner = FakeRunner()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", (tmp_path / "runs").as_posix())
    monkeypatch.setattr(routes, "APP_ROOT", app_root)
    monkeypatch.setattr(routes, "job_runner", runner)
    monkeypatch.setattr(routes, "default_target_library_root", lambda: library)
    return create_app().test_client(), runner, run, bundle


def test_status_capabilities_preview_fit_and_pydantic_details(target_client) -> None:
    client, _runner, _run, _bundle = target_client
    assert (
        client.get("/calibration-targets/status").get_json()["generation_available"]
        is True
    )
    capabilities = client.get("/calibration-targets/capabilities")
    assert capabilities.status_code == 200
    capability_payload = capabilities.get_json()
    assert capability_payload["board_types"] == ["aruco"]
    assert capability_payload["paper_sizes_mm"]["A5"] == [148.0, 210.0]
    assert capability_payload["paper_sizes_mm"]["A6"] == [105.0, 148.0]

    preview = client.post("/calibration-targets/preview", json=configuration())
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert len(preview.headers["X-Configuration-Hash"]) == 64

    a6_portrait_with_ruler = configuration()
    a6_portrait_with_ruler["page"] = {
        "paper_size": "A6",
        "orientation": "portrait",
    }
    a6_portrait_with_ruler["board"].update(
        {"rows": 1, "columns": 1, "marker_size_mm": 20.0}
    )
    a6_portrait_with_ruler["annotations"]["show_ruler"] = True
    ruler_rejected = client.post(
        "/calibration-targets/preview",
        json=a6_portrait_with_ruler,
    )
    assert ruler_rejected.status_code == 422
    assert ruler_rejected.get_json()["errors"][0]["path"] == [
        "annotations",
        "ruler",
    ]

    too_large = configuration()
    too_large["board"]["columns"] = 7
    too_large["board"]["rows"] = 7
    too_large["board"]["marker_size_mm"] = 100
    fit = client.post("/calibration-targets/fit", json=too_large)
    assert fit.status_code == 200
    assert fit.get_json()["adjusted"] is True

    invalid = client.post(
        "/calibration-targets/preview",
        json={**configuration(), "unexpected": True},
    )
    assert invalid.status_code == 422
    assert invalid.get_json()["errors"][0]["path"] == ["unexpected"]

    oversized = client.post(
        "/calibration-targets/preview",
        data=b"{" + b" " * (256 * 1024) + b"}",
        content_type="application/json",
    )
    assert oversized.status_code == 413


def test_generate_and_select_use_exact_queued_resources(target_client) -> None:
    client, runner, run, bundle = target_client
    generated = client.post(
        "/calibration-targets/generate",
        json={"display_name": "Queued target", "configuration": configuration()},
    )
    assert generated.status_code == 202
    assert runner.submissions[0]["name"] == "calibration_target_generate"
    assert runner.submissions[0]["resources"] == ["cpu", "disk_io"]
    assert runner.submissions[0]["scope_kind"] == "library"
    assert runner.submissions[0].get("run_root") is None
    assert runner.submissions[0]["command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_calibration_target_generate.py",
    ]

    selected = client.post(
        f"/calibration-targets/bundles/{bundle['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "unknown",
            "mounting_frame": "robot_flange",
        },
    )
    assert selected.status_code == 202
    assert runner.submissions[1]["name"] == "calibration_target_select"
    assert runner.submissions[1]["resources"] == ["disk_io"]
    assert runner.submissions[1]["scope_kind"] == "run"
    assert runner.submissions[1]["run_root"] == run.as_posix()
    assert runner.submissions[1]["command"] == [
        "uv",
        "run",
        "python",
        "scripts/run_calibration_target_select.py",
        run.as_posix(),
        bundle["target_id"],
        "--placement",
        "unknown",
        "--mounting-frame",
        "robot_flange",
    ]
    assert runner.submissions[1]["parameters"]["mounting_frame"] == "robot_flange"

    wrong_mounting = client.post(
        f"/calibration-targets/bundles/{bundle['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "unknown",
            "mounting_frame": "template_base",
        },
    )
    assert wrong_mounting.status_code == 400
    assert "Static-camera calibration requires" in wrong_mounting.get_json()["output"]

    known_robot_mounting = client.post(
        f"/calibration-targets/bundles/{bundle['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "template_base_identity",
            "mounting_frame": "robot_flange",
        },
    )
    assert known_robot_mounting.status_code == 400
    assert "known template-base placement" in known_robot_mounting.get_json()["output"]

    missing_pose = client.post(
        f"/calibration-targets/bundles/{bundle['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "posegridgen_board_to_base",
        },
    )
    assert missing_pose.status_code == 400
    assert "mounting_frame is required" in missing_pose.get_json()["output"]


def test_bundle_listing_downloads_and_traversal_rejection(target_client) -> None:
    client, _runner, run, bundle = target_client
    listing = client.get(
        "/calibration-targets/bundles", query_string={"run_root": run.as_posix()}
    )
    assert listing.status_code == 200
    assert listing.get_json()["bundles"][0]["target_id"] == bundle["target_id"]
    assert listing.get_json()["replacement_blockers"] == []

    preview = client.get(
        f"/calibration-targets/bundles/{bundle['target_id']}/preview.png"
    )
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert preview.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert preview.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert preview.headers["ETag"] == f'"{bundle["configuration_sha256"]}"'
    image = cv2.imdecode(
        np.frombuffer(preview.data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE
    )
    assert image.shape[1] > image.shape[0]
    dark_y, dark_x = np.where(image < 128)
    assert dark_x.min() > 0
    assert dark_y.min() > 0
    assert dark_x.max() < image.shape[1] - 1
    assert dark_y.max() < image.shape[0] - 1

    for artifact, mimetype in (
        ("source", "application/json"),
        ("target", "application/json"),
        ("pdf", "application/pdf"),
    ):
        response = client.get(
            f"/calibration-targets/bundles/{bundle['target_id']}/download/{artifact}"
        )
        assert response.status_code == 200
        assert response.mimetype == mimetype
    assert (
        client.get(
            f"/calibration-targets/bundles/{bundle['target_id']}/download/other"
        ).status_code
        == 404
    )
    assert client.get("/calibration-targets/bundles/../download/pdf").status_code in {
        400,
        404,
    }
    assert client.get("/calibration-targets/bundles/../preview.png").status_code in {
        400,
        404,
    }
    assert client.get("/artifacts").status_code == 404


def test_first_selection_after_raw_capture_fails_before_queueing(target_client) -> None:
    client, runner, run, bundle = target_client
    (run / "raw_robot_ee_poses.json").write_text("{}\n")

    response = client.post(
        f"/calibration-targets/bundles/{bundle['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "unknown",
            "mounting_frame": "robot_flange",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["blockers"] == ["raw_robot_ee_poses.json"]
    assert "must be bound before raw acquisition" in response.get_json()["output"]
    assert runner.submissions == []


def test_bundle_delete_requires_confirmation_and_removes_library_target(
    target_client,
) -> None:
    client, _runner, run, bundle = target_client
    endpoint = f"/calibration-targets/bundles/{bundle['target_id']}"

    unconfirmed = client.delete(endpoint, json={"run_root": run.as_posix()})
    assert unconfirmed.status_code == 400
    assert "confirm must be true" in unconfirmed.get_json()["output"]
    assert Path(bundle["bundle_path"]).is_dir()

    deleted = client.delete(
        endpoint,
        json={"run_root": run.as_posix(), "confirm": True},
    )
    assert deleted.status_code == 200
    assert deleted.get_json() == {
        "status": "deleted",
        "target_id": bundle["target_id"],
        "display_name": "API target",
    }
    assert not Path(bundle["bundle_path"]).exists()
    listing = client.get(
        "/calibration-targets/bundles", query_string={"run_root": run.as_posix()}
    )
    assert listing.get_json()["bundles"] == []


def test_bundle_delete_rejects_target_active_for_selected_run(target_client) -> None:
    client, _runner, run, bundle = target_client
    select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="robot_flange",
        library_root=Path(bundle["bundle_path"]).parent,
    )

    response = client.delete(
        f"/calibration-targets/bundles/{bundle['target_id']}",
        json={"run_root": run.as_posix(), "confirm": True},
    )

    assert response.status_code == 409
    assert response.get_json()["blockers"] == ["run_config.json"]
    assert "active for the selected run" in response.get_json()["output"]
    assert Path(bundle["bundle_path"]).is_dir()


def test_selection_conflict_returns_concrete_blocker_paths(target_client) -> None:
    client, runner, run, bundle = target_client
    select_target_bundle(
        run_root=run,
        target_id=bundle["target_id"],
        placement_mode="unknown",
        mounting_frame="robot_flange",
        library_root=Path(bundle["bundle_path"]).parent,
    )
    sensor = run / "processed" / "synchronized" / "realsense_1"
    sensor.mkdir(parents=True)
    (sensor / ARUCO_DETECTIONS).write_text("{}\n")

    unchanged = client.post(
        f"/calibration-targets/bundles/{bundle['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "unknown",
            "mounting_frame": "robot_flange",
        },
    )

    assert unchanged.status_code == 200
    assert unchanged.get_json()["status"] == "unchanged"
    assert unchanged.get_json()["evidence"]["target_id"] == bundle["target_id"]
    assert runner.submissions == []

    listing = client.get(
        "/calibration-targets/bundles", query_string={"run_root": run.as_posix()}
    )
    assert listing.get_json()["replacement_blockers"] == [
        f"processed/synchronized/realsense_1/{ARUCO_DETECTIONS}"
    ]

    replacement = generate_target_bundle(
        display_name="Replacement target",
        configuration=configuration(),
        library_root=Path(bundle["bundle_path"]).parent,
    )
    response = client.post(
        f"/calibration-targets/bundles/{replacement['target_id']}/select",
        json={
            "run_root": run.as_posix(),
            "placement": "unknown",
            "mounting_frame": "robot_flange",
        },
    )

    assert response.status_code == 409
    assert response.get_json()["blockers"] == [
        f"processed/synchronized/realsense_1/{ARUCO_DETECTIONS}"
    ]
