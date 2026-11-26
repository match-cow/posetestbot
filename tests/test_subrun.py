from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from posetestbot.calibration.profile_library import load_calibration_profile_selection
from posetestbot.pipeline import subrun
from posetestbot.pipeline.run_config import (
    create_run_config,
    load_run_config_for_run_root,
    validate_run_config,
    write_run_config,
)
from posetestbot.pose_templates.library import set_template_archive_state
from posetestbot.pose_templates.selection import (
    load_pose_template_selection,
    select_pose_template,
)
from posetestbot.web.app import create_app
from posetestbot.web.routes import subruns as subrun_routes
from tests.test_calibration_profile_library_api import (
    SENSOR_ID,
    _candidate,
    _sensor,
    _write_source,
)
from tests.test_pose_template_selection_atomicity import _selection_fixture


PLACEMENT = [[1, 0, 0, 120.0], [0, 1, 0, -40.0], [0, 0, 1, 5.0], [0, 0, 0, 1]]


@pytest.fixture
def parent_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A configured object-dataset run with calibration and confirmed template."""

    (tmp_path / "templates").mkdir()
    _run, library, template, _second = _selection_fixture(tmp_path / "templates")
    runs = tmp_path / "runs"
    source = runs / "calibration_run"
    parent = runs / "object_A"
    _write_source(source)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs.as_posix())
    client = create_app().test_client()
    sensors = [_sensor().to_dict()]
    bundle = _candidate(
        client.get("/ui/calibrations", query_string={"run_root": parent}).get_json(),
        source,
    )["bundle_sha256"]
    selected = client.post(
        "/ui/calibrations/select",
        json={
            "run_root": parent.as_posix(),
            "source_selections": [
                {
                    "source_run_root": source.as_posix(),
                    "expected_bundle_sha256": bundle,
                    "sensor_keys": [f"realsense_d435:{SENSOR_ID}"],
                }
            ],
            "resolution": "720p",
            "sensors": sensors,
        },
    )
    assert selected.status_code == 201, selected.get_json()
    saved = client.post(
        "/run-config",
        json={
            "run_root": parent.as_posix(),
            "intent": "dataset",
            "resolution": "720p",
            "fps": 15,
            "velocity_m_s": 0.2,
            "sensors": sensors,
            "dataset_mode": "pose_template",
        },
    )
    assert saved.status_code == 201, saved.get_json()
    select_pose_template(
        parent,
        template["template_uuid"],
        placement={"matrix": PLACEMENT},
        confirmed=True,
        operator=None,
        library_root=library,
    )
    return {
        "parent": parent,
        "source": source,
        "library": library,
        "template": template,
        "client": client,
    }


def test_subrun_replays_setup_calibration_and_placement_with_lineage(parent_run):
    parent = parent_run["parent"]
    plan = subrun.inspect_subrun_parent(parent)
    assert plan["default_folder_name"] == "object_A__sub02"
    assert plan["capture"]["fps"] == 15

    created = subrun.create_subrun(
        parent,
        plan["default_folder_name"],
        "  anodised aluminium, diffuse light ",
        template_library_root=parent_run["library"],
    )

    child = Path(created["run_root"])
    assert child.parent == parent.parent
    config = load_run_config_for_run_root(child)
    parent_config = load_run_config_for_run_root(parent)
    assert config["run_id"] != parent_config["run_id"]
    assert config["subrun"]["parent_run_id"] == parent_config["run_id"]
    assert config["subrun"]["variant"] == "anodised aluminium, diffuse light"
    assert config["capture"]["sensors"] == parent_config["capture"]["sensors"]
    assert config["capture"]["fps"] == 15 and config["capture"]["velocity_m_s"] == 0.2
    assert config["bop"]["annotation_mode"] == "none"
    assert (
        load_calibration_profile_selection(child)["source"]["bundle_sha256"]
        == load_calibration_profile_selection(parent)["source"]["bundle_sha256"]
    )
    template = load_pose_template_selection(child)
    assert template["bundle_sha256"] == load_pose_template_selection(parent)["bundle_sha256"]
    assert template["placement_confirmed"] is True
    assert np.allclose(template["template_base_from_pose_template"]["matrix"], PLACEMENT)
    assert config["pose_template"]["placement_confirmed"] is True

    with pytest.raises(ValueError, match="itself a subrun"):
        subrun.inspect_subrun_parent(child)
    assert subrun.inspect_subrun_parent(parent)["default_folder_name"] == "object_A__sub03"


def test_subrun_failures_leave_no_partial_folder(parent_run):
    parent, library = parent_run["parent"], parent_run["library"]
    set_template_archive_state(
        parent_run["template"]["template_uuid"], state="archived", library_root=library
    )
    with pytest.raises(ValueError, match="no longer active"):
        subrun.create_subrun(parent, "object_A__sub02", "matte", template_library_root=library)
    assert not (parent.parent / "object_A__sub02").exists()

    shutil.rmtree(parent_run["source"])
    with pytest.raises(ValueError, match="no longer exists"):
        subrun.create_subrun(parent, "object_A__sub02", "matte", template_library_root=library)
    assert not (parent.parent / "object_A__sub02").exists()

    with pytest.raises(ValueError, match="Describe the variant"):
        subrun.create_subrun(parent, "object_A__sub03", "   ")
    with pytest.raises(ValueError, match="already exists"):
        subrun.subrun_target(parent, "object_A")


def test_only_object_dataset_runs_can_parent_a_subrun(tmp_path: Path) -> None:
    run = tmp_path / "calibration"
    run.mkdir()
    write_run_config(
        run,
        create_run_config(run_root=run, capture_intent="calibration", bop_annotation_mode="none"),
    )
    with pytest.raises(ValueError, match="object-dataset run"):
        subrun.inspect_subrun_parent(run)


def test_run_config_subrun_lineage_is_strict() -> None:
    config = create_run_config(
        run_root="/tmp/child",
        capture_intent="dataset",
        bop_annotation_mode="none",
        dataset_mode="pose_template",
        subrun={
            "parent_run_id": "11111111-1111-4111-8111-111111111111",
            "parent_run_root": "/tmp/parent",
            "variant": "matte",
            "created_at": "2026-11-26T10:00:00+00:00",
        },
    ).to_dict()
    validate_run_config(config)
    for change, message in (
        ({"extra": 1}, "exactly"),
        ({"variant": " padded "}, "trimmed"),
        ({"parent_run_root": "relative/path"}, "absolute"),
        ({"parent_run_id": config["run_id"]}, "another run"),
    ):
        broken = {**config, "subrun": {**config["subrun"], **change}}
        with pytest.raises(ValueError, match=message):
            validate_run_config(broken)
    with pytest.raises(ValueError, match="pose_template dataset"):
        validate_run_config({**config, "dataset_mode": "objectless", "pose_template": None})


def test_subrun_api_describes_parent_and_queues_one_disk_job(parent_run, monkeypatch):
    client, parent = parent_run["client"], parent_run["parent"]
    described = client.get("/runs/subrun", query_string={"run_root": parent.as_posix()})
    assert described.status_code == 200
    assert described.get_json()["default_folder_name"] == "object_A__sub02"

    submissions = []

    class Job:
        id = "job-subrun"

        def to_dict(self):
            return {"id": self.id}

    class Runner:
        def submit(self, **kwargs):
            submissions.append(kwargs)
            return Job()

    monkeypatch.setattr(subrun_routes, "get_job_runner", lambda: Runner())
    missing_variant = client.post(
        "/runs/subrun",
        json={"parent_run_root": parent.as_posix(), "folder_name": "object_A__sub02", "variant": ""},
    )
    assert missing_variant.status_code == 400 and not submissions
    queued = client.post(
        "/runs/subrun",
        json={
            "parent_run_root": parent.as_posix(),
            "folder_name": "object_A__sub02",
            "variant": "matte",
        },
    )
    assert queued.status_code == 202, queued.get_json()
    assert submissions[0]["resources"] == ["disk_io"]
    assert submissions[0]["command"][-6:] == [
        "--parent",
        parent.as_posix(),
        "--folder-name",
        "object_A__sub02",
        "--variant",
        "matte",
    ]
    assert not (parent.parent / "object_A__sub02").exists()
