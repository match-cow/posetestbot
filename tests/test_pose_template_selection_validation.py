from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest
import trimesh

from posetestbot.pose_templates.catalog import import_catalog_object
from posetestbot.pose_templates.library import generate_template_bundle
from posetestbot.pose_templates.orientations import analyze_catalog_orientations
from posetestbot.pipeline.run_config import create_run_config, write_run_config
from posetestbot.pose_templates.selection import (
    load_pose_template_selection,
    prepare_object_instances,
    select_pose_template,
)
from posetestbot.web.app import create_app
from posetestbot.web.routes import pose_templates as routes


def _selection_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    cad = tmp_path / "box.stl"
    cad.write_bytes(trimesh.creation.box(extents=(20, 10, 8)).export(file_type="stl"))
    catalog = tmp_path / "catalog"
    workpiece = import_catalog_object(name="Box", cad_path=cad, catalog_root=catalog)
    orientation_id = analyze_catalog_orientations(
        workpiece["catalog_uuid"], catalog_root=catalog
    )["orientations"][0]["orientation_id"]
    library = tmp_path / "library"
    bundle = generate_template_bundle(
        {
            "display_name": "Selection validation",
            "instances": [
                {
                    "instance_uuid": "11111111-1111-4111-8111-111111111111",
                    "catalog_uuid": workpiece["catalog_uuid"],
                    "orientation_id": orientation_id,
                    "pose": {
                        "x_mm": 40,
                        "y_mm": 40,
                        "rotation_deg": 0,
                    },
                }
            ],
        },
        catalog_root=catalog,
        library_root=library,
    )
    run = tmp_path / "run"
    run.mkdir()
    write_run_config(
        run,
        create_run_config(
            run_root=run,
            capture_intent="dataset",
            bop_annotation_mode="none",
            dataset_mode="pose_template",
        ),
    )
    select_pose_template(
        run,
        bundle["template_uuid"],
        placement={"matrix": np.eye(4).tolist()},
        confirmed=True,
        operator="first-operator",
        library_root=library,
    )
    return run, library, bundle


def _set_path(
    value: dict[str, Any], path: tuple[str | int, ...], replacement: Any
) -> None:
    target: Any = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement


def test_selection_loader_rejects_tampered_trusted_fields(tmp_path: Path) -> None:
    run, _library, _bundle = _selection_fixture(tmp_path)
    selection_path = run / "pose_template_selection.json"
    pristine = json.loads(selection_path.read_text())
    instance = pristine["instances"][0]
    mutations: list[tuple[str, str, Callable[[dict[str, Any]], None]]] = [
        (
            "confirmation type",
            "placement_confirmed must be a boolean",
            lambda value: _set_path(value, ("placement_confirmed",), "false"),
        ),
        (
            "operator type",
            "operator provenance must be a non-empty string",
            lambda value: _set_path(value, ("operator",), ["operator"]),
        ),
        (
            "selected timestamp",
            "selected_at provenance must be ISO-8601",
            lambda value: _set_path(value, ("selected_at",), "yesterday"),
        ),
        (
            "snapshot location",
            "bundle snapshot must be processed/pose_template_selection",
            lambda value: _set_path(
                value, ("bundle_snapshot",), "processed/a-different-bundle"
            ),
        ),
        (
            "configuration hash",
            "snapshot configuration hash mismatch",
            lambda value: _set_path(value, ("configuration_sha256",), "0" * 64),
        ),
        (
            "upstream source",
            "snapshot source mismatch",
            lambda value: _set_path(
                value, ("source", "adapter_version"), "tampered-adapter"
            ),
        ),
        (
            "print compensation",
            "snapshot print_compensation mismatch",
            lambda value: _set_path(value, ("print_compensation", "x_scale"), 1.02),
        ),
        (
            "catalog snapshot",
            "snapshot catalog_snapshot mismatch",
            lambda value: _set_path(
                value, ("catalog_snapshot", "objects", 0, "name"), "Other"
            ),
        ),
        (
            "placement frame",
            "template_base_from_pose_template frame semantics mismatch",
            lambda value: _set_path(
                value,
                ("template_base_from_pose_template", "child_frame"),
                "other",
            ),
        ),
        (
            "instance UUID",
            "resolved instance UUID mismatch",
            lambda value: _set_path(
                value,
                ("instances", 0, "instance_uuid"),
                "22222222-2222-4222-8222-222222222222",
            ),
        ),
        (
            "catalog UUID",
            "resolved instance catalog UUID mismatch",
            lambda value: _set_path(
                value,
                ("instances", 0, "catalog_uuid"),
                "33333333-3333-4333-8333-333333333333",
            ),
        ),
        (
            "BOP object ID type",
            "resolved instance obj_id mismatch",
            lambda value: _set_path(
                value, ("instances", 0, "obj_id"), str(instance["obj_id"])
            ),
        ),
        (
            "workpiece name",
            "resolved instance name mismatch",
            lambda value: _set_path(value, ("instances", 0, "name"), "Other"),
        ),
        (
            "asset path",
            "resolved instance assets mismatch",
            lambda value: _set_path(
                value,
                ("instances", 0, "assets", "canonical_ply", "path"),
                "../outside.ply",
            ),
        ),
        (
            "nominal transform",
            "resolved pose_template_from_object duplicated values mismatch",
            lambda value: _set_path(
                value,
                (
                    "instances",
                    0,
                    "pose_template_from_object",
                    "translation_mm",
                    0,
                ),
                instance["pose_template_from_object"]["translation_mm"][0] + 1,
            ),
        ),
        (
            "resolved transform frame",
            "resolved template_base_from_object frame semantics mismatch",
            lambda value: _set_path(
                value,
                ("instances", 0, "template_base_from_object", "parent_frame"),
                "other",
            ),
        ),
    ]

    for _label, message, mutate in mutations:
        tampered = copy.deepcopy(pristine)
        mutate(tampered)
        selection_path.write_text(json.dumps(tampered))
        with pytest.raises(ValueError, match=message):
            load_pose_template_selection(run)

    selection_path.write_text(json.dumps(pristine))
    assert prepare_object_instances(run)["instances"][0]["name"] == "Box"


def test_select_pose_template_rejects_non_boolean_confirmation_at_public_boundary(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ValueError, match="confirmed must be a boolean"):
        select_pose_template(
            run,
            "11111111-1111-4111-8111-111111111111",
            placement={"matrix": np.eye(4).tolist()},
            confirmed="false",  # type: ignore[arg-type]
            operator="pytest",
            library_root=tmp_path / "library",
        )
    with pytest.raises(
        ValueError, match="operator provenance must be a non-empty string"
    ):
        select_pose_template(
            run,
            "11111111-1111-4111-8111-111111111111",
            placement={"matrix": np.eye(4).tolist()},
            confirmed=True,
            operator=None,  # type: ignore[arg-type]
            library_root=tmp_path / "library",
        )


def test_selection_http_routes_reject_string_false_before_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setattr(routes, "REQUEST_ROOT", tmp_path / "requests")

    class Runner:
        submissions = 0

        def submit(self, **_kwargs: Any) -> None:
            self.submissions += 1

    runner = Runner()
    monkeypatch.setattr(routes, "job_runner", runner)
    response = (
        create_app()
        .test_client()
        .post(
            "/pose-templates/runs/selection",
            json={
                "run_root": run.as_posix(),
                "template_uuid": "11111111-1111-4111-8111-111111111111",
                "placement": {"matrix": np.eye(4).tolist()},
                "confirmed": "false",
                "operator": "pytest",
            },
        )
    )

    assert response.status_code == 400
    assert response.get_json()["output"] == "confirmed must be a boolean"
    assert runner.submissions == 0
    assert not (tmp_path / "requests").exists()


def test_selection_request_script_rejects_string_false_before_core_call(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "run_root": (tmp_path / "run").as_posix(),
                "template_uuid": "11111111-1111-4111-8111-111111111111",
                "placement": {"matrix": np.eye(4).tolist()},
                "confirmed": "false",
                "operator": "pytest",
            }
        )
    )
    completed = subprocess.run(
        [
            sys.executable,
            (
                Path(__file__).parents[1] / "scripts/run_pose_template_select.py"
            ).as_posix(),
            "--request",
            request_path.as_posix(),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "confirmed must be a boolean" in completed.stderr


def test_same_selection_from_new_operator_updates_provenance(tmp_path: Path) -> None:
    run, library, bundle = _selection_fixture(tmp_path)

    selected = select_pose_template(
        run,
        bundle["template_uuid"],
        placement={"matrix": np.eye(4).tolist()},
        confirmed=True,
        operator="second-operator",
        library_root=library,
    )

    assert selected["operator"] == "second-operator"
