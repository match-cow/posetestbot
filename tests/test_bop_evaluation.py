from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from posetestbot.bop import evaluation as bop_evaluation
from posetestbot.bop.evaluation import (
    create_evaluation_request,
    create_simulated_bop_result,
    import_bop_result,
    inspect_dataset,
    list_evaluations,
    list_results,
    result_package_bytes,
    result_file_path,
    validate_bop_result_csv,
)


RESULT_HEADER = ["scene_id", "im_id", "obj_id", "score", "R", "t", "time"]
IDENTITY_R = "1 0 0 0 1 0 0 0 1"
GT_T = "0 0 500"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _write_model(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "ply",
                "format ascii 1.0",
                "element vertex 8",
                "property float x",
                "property float y",
                "property float z",
                "element face 12",
                "property list uchar int vertex_indices",
                "end_header",
                "-10 -10 -10",
                "10 -10 -10",
                "10 10 -10",
                "-10 10 -10",
                "-10 -10 10",
                "10 -10 10",
                "10 10 10",
                "-10 10 10",
                "3 0 2 1",
                "3 0 3 2",
                "3 4 5 6",
                "3 4 6 7",
                "3 0 1 5",
                "3 0 5 4",
                "3 1 2 6",
                "3 1 6 5",
                "3 2 3 7",
                "3 2 7 6",
                "3 3 0 4",
                "3 3 4 7",
                "",
            )
        )
    )


def make_tiny_evaluation_run(tmp_path: Path, *, name: str = "unsafe_run-name") -> Path:
    """Create the smallest annotation-bearing PoseTestBot BOP v5 export."""

    run_root = tmp_path / name
    bop_root = run_root / "bop"
    scene_root = bop_root / "test" / "000001"
    (scene_root / "rgb").mkdir(parents=True)
    (scene_root / "depth").mkdir()
    assert cv2.imwrite(
        (scene_root / "rgb" / "000000.png").as_posix(),
        np.zeros((8, 8, 3), dtype=np.uint8),
    )
    assert cv2.imwrite(
        (scene_root / "depth" / "000000.png").as_posix(),
        np.full((8, 8), 500, dtype=np.uint16),
    )
    _write_json(
        scene_root / "scene_camera.json",
        {
            "0": {
                "cam_K": [100.0, 0.0, 4.0, 0.0, 100.0, 4.0, 0.0, 0.0, 1.0],
                "depth_scale": 1.0,
            }
        },
    )
    _write_json(
        scene_root / "scene_gt.json",
        {
            "0": [
                {
                    "cam_R_m2c": [
                        1.0,
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                    ],
                    "cam_t_m2c": [0.0, 0.0, 500.0],
                    "obj_id": 1,
                }
            ]
        },
    )
    _write_json(
        scene_root / "scene_gt_info.json",
        {
            "0": [
                {
                    "bbox_obj": [1, 1, 5, 5],
                    "bbox_visib": [1, 1, 5, 5],
                    "px_count_all": 25,
                    "px_count_valid": 25,
                    "px_count_visib": 25,
                    "visib_fract": 1.0,
                }
            ]
        },
    )
    targets_path = bop_root / "test_targets_bop19.json"
    _write_json(
        targets_path,
        [{"scene_id": 1, "im_id": 0, "obj_id": 1, "inst_count": 1}],
    )
    models_info = {
        "1": {
            "diameter": 34.641016,
            "min_x": -10.0,
            "min_y": -10.0,
            "min_z": -10.0,
            "size_x": 20.0,
            "size_y": 20.0,
            "size_z": 0.0,
        }
    }
    for folder in ("models", "models_eval"):
        _write_json(bop_root / folder / "models_info.json", models_info)
        _write_model(bop_root / folder / "obj_000001.ply")

    validation = {
        "status": "ok",
        "scene_count": 1,
        "frame_count": 1,
        "model_count": 1,
        "annotation_count": 1,
        "target_count": 1,
        "capabilities": {
            "bop_scenewise_rgbd": True,
            "pose_estimation_input": True,
            "gt_annotations": True,
            "bop19_evaluation": True,
        },
    }
    _write_json(
        bop_root / "dataset_info.json",
        {
            "schema_version": "posetestbot_bop_dataset_info.v1",
            "name": name,
            "bop_format": "scenewise",
            "splits": ["test"],
            "scene_count": 1,
            "sensors": ["fixture"],
            "generated_at": "2026-07-26T00:00:00+00:00",
        },
    )
    _write_json(
        bop_root / "bop_export_manifest.json",
        {
            "schema_version": "bop_export_manifest.v5",
            "format": "bop-scenewise",
            "layout": "<split>/<scene_id>",
            "dataset_root": ".",
            "exports": [
                {
                    "sensor_name": "fixture",
                    "scene_id": 1,
                    "split": "test",
                    "scene_folder": "test/000001",
                    "rgb_count": 1,
                    "depth_count": 1,
                    "artifacts": {
                        "scene_camera": "test/000001/scene_camera.json",
                        "scene_gt": "test/000001/scene_gt.json",
                        "scene_gt_info": "test/000001/scene_gt_info.json",
                    },
                    "annotation_source": "blenderproc",
                }
            ],
            "object_models": [
                {
                    "object_name": "fixture",
                    "obj_id": 1,
                    "source_path": "fixture.ply",
                    "bop_path": "models/obj_000001.ply",
                    "bop_eval_path": "models_eval/obj_000001.ply",
                    "texture_path": None,
                }
            ],
            "objectless": False,
            "dataset_mode": "pose_template",
            "annotation_source": "blenderproc",
            "annotation_state": "complete",
            "capabilities": validation["capabilities"],
            "stable_id_mapping": {"fixture": 1},
            "targets_path": targets_path.relative_to(bop_root).as_posix(),
            "dataset_info_path": "dataset_info.json",
            "validation": validation,
        },
    )
    return run_root


def write_result_csv(
    path: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    header: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header or RESULT_HEADER)
        writer.writeheader()
        for row in rows or [
            {
                "scene_id": 1,
                "im_id": 0,
                "obj_id": 1,
                "score": 1.0,
                "R": IDENTITY_R,
                "t": GT_T,
                "time": -1,
            }
        ]:
            writer.writerow(row)
    return path


def _result_path(run_root: Path, record: dict[str, Any]) -> Path:
    value = record.get("path", record.get("result_path"))
    assert isinstance(value, str) and value
    path = Path(value)
    return path if path.is_absolute() else run_root / path


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_inspection_derives_a_safe_stable_alias_and_confirms_real_evidence(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)

    first = inspect_dataset(run_root)
    second = inspect_dataset(run_root)

    assert first["evaluation_ready"] is True
    assert first["blockers"] == []
    assert first["dataset_alias"] == second["dataset_alias"]
    assert re.fullmatch(r"[a-z][a-z0-9]*", first["dataset_alias"])
    assert "_" not in first["dataset_alias"]
    assert "-" not in first["dataset_alias"]
    assert first["dataset_sha256"] == second["dataset_sha256"]
    assert first["counts"] == {
        "scenes": 1,
        "images": 1,
        "objects": 1,
        "targets": 1,
        "annotations": 1,
    }


def test_valid_bop19_result_reports_the_parsed_method_and_row_count(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    path = write_result_csv(
        tmp_path / f"foundationpose_{dataset['dataset_alias']}-test.csv"
    )

    validation = validate_bop_result_csv(path, dataset=dataset)

    assert validation["status"] == "ok"
    assert validation["method"] == "foundationpose"
    assert validation["dataset_alias"] == dataset["dataset_alias"]
    assert validation["row_count"] == 1
    assert validation["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"scene_id": 2}, "scene"),
        ({"R": "1 0 0 0 1 0 0 0"}, "R"),
    ],
)
def test_result_validation_checks_shape_numbers_and_target_membership(
    tmp_path: Path, mutation: dict[str, Any], message: str
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    row = {
        "scene_id": 1,
        "im_id": 0,
        "obj_id": 1,
        "score": 1.0,
        "R": IDENTITY_R,
        "t": GT_T,
        "time": -1,
    }
    row.update(mutation)
    path = write_result_csv(
        tmp_path / f"method_{dataset['dataset_alias']}-test.csv", rows=[row]
    )

    with pytest.raises(ValueError, match=message):
        validate_bop_result_csv(path, dataset=dataset)


def test_result_validation_requires_the_exact_bop19_header(tmp_path: Path) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    path = write_result_csv(
        tmp_path / f"method_{dataset['dataset_alias']}-test.csv",
        header=[*RESULT_HEADER, "extra"],
        rows=[
            {
                "scene_id": 1,
                "im_id": 0,
                "obj_id": 1,
                "score": 1.0,
                "R": IDENTITY_R,
                "t": GT_T,
                "time": -1,
                "extra": "",
            }
        ],
    )

    with pytest.raises(ValueError, match="header"):
        validate_bop_result_csv(path, dataset=dataset)


def test_result_validation_enforces_per_image_timing_and_dataset_filename(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    rows = [
        {
            "scene_id": 1,
            "im_id": 0,
            "obj_id": 1,
            "score": score,
            "R": IDENTITY_R,
            "t": GT_T,
            "time": runtime,
        }
        for score, runtime in ((1.0, 0.01), (0.9, 0.02))
    ]
    timing_path = write_result_csv(
        tmp_path / f"method_{dataset['dataset_alias']}-test.csv", rows=rows
    )
    with pytest.raises(ValueError, match="time"):
        validate_bop_result_csv(timing_path, dataset=dataset)

    wrong_dataset_path = write_result_csv(tmp_path / "method_otherdataset-test.csv")
    with pytest.raises(ValueError, match="dataset"):
        validate_bop_result_csv(wrong_dataset_path, dataset=dataset)


def test_result_validation_rejects_split_types_and_csv_quoting(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    split_type = write_result_csv(
        tmp_path / f"method_{dataset['dataset_alias']}-test-rendered.csv"
    )
    with pytest.raises(ValueError, match="split_type"):
        validate_bop_result_csv(split_type, dataset=dataset)

    quoted = tmp_path / f"method_{dataset['dataset_alias']}-test.csv"
    quoted.write_text(
        ",".join(RESULT_HEADER) + "\n" + f'"1",0,1,1.0,{IDENTITY_R},{GT_T},-1\n'
    )
    with pytest.raises(ValueError, match="quot"):
        validate_bop_result_csv(quoted, dataset=dataset)


def test_result_import_is_immutable_hashed_and_listed(tmp_path: Path) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(
        tmp_path / f"realpose_{dataset['dataset_alias']}-test.csv"
    )
    source_bytes = source.read_bytes()

    record = import_bop_result(run_root, source, method_name="Real Pose")

    stored = _result_path(run_root, record)
    assert stored != source
    assert stored.is_file()
    assert stored.read_bytes() == source_bytes
    assert stored.stat().st_mode & stat.S_IWUSR == 0
    assert source.read_bytes() == source_bytes
    assert record["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert record["dataset_sha256"] == dataset["dataset_sha256"]
    assert record["method_name"] == "Real Pose"
    assert record["simulated"] is False
    assert [item["result_id"] for item in list_results(run_root)] == [
        record["result_id"]
    ]


def test_manual_result_package_is_deterministic_path_free_and_hash_checked(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(
        tmp_path / f"method_{dataset['dataset_alias']}-test.csv"
    )
    result = import_bop_result(run_root, source, method_name="Manual method")

    first, first_manifest = result_package_bytes(run_root, result["result_id"])
    second, second_manifest = result_package_bytes(run_root, result["result_id"])

    assert first == second
    assert first_manifest == second_manifest
    assert first_manifest["schema_version"] == "bop_result_package_manifest.v1"
    assert first_manifest["provenance"] is None
    assert first_manifest["external_job"] is None
    assert first_manifest["dataset"] == {
        "dataset_alias": dataset["dataset_alias"],
        "split": "test",
        "sha256": dataset["dataset_sha256"],
    }
    assert run_root.as_posix() not in json.dumps(first_manifest)
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == sorted([source.name, "manifest.json"])
        assert archive.read(source.name) == source.read_bytes()
        assert json.loads(archive.read("manifest.json")) == first_manifest

    stored = result_file_path(run_root, result["result_id"])
    stored.chmod(0o600)
    stored.write_bytes(stored.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="size|integrity"):
        result_package_bytes(run_root, result["result_id"])


def test_foundationpose_failure_retention_strips_messages_and_paths(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    inventory = bop_evaluation._dataset_inventory(run_root)
    evidence = bop_evaluation._foundationpose_execution_evidence(
        {
            "oracle_mask_contract": "bop_mask_visib_gt_instance.v1",
            "score_contract": "constant_1.0_no_detection_confidence",
            "execution_contract": "independent_register_per_target.v2",
            "track_segment_count": 0,
            "recorded_track_segment_count": 0,
            "omitted_track_segment_count": 0,
            "track_segments": [],
            "failures": [
                {
                    "scene_id": 1,
                    "im_id": 0,
                    "obj_id": 1,
                    "gt_id": 0,
                    "instance_uuid": "11111111-1111-4111-8111-111111111111",
                    "operation": "registration",
                    "error_type": "RuntimeError",
                    "message": "weights missing below /secret/cluster/path",
                    "remote_path": "/secret/cluster/path",
                }
            ],
            "image_timings_seconds": {"1/0": 0.125},
        },
        inventory=inventory,
        execution_mode="independent_registration",
        counts={
            "registration_count": 1,
            "tracking_count": 0,
            "reinitialization_count": 0,
            "selected_target_count": 1,
            "processed_target_count": 1,
            "selected_scope_excluded_target_count": 0,
            "profile_excluded_target_count": 0,
            "estimate_count": 0,
            "failure_count": 1,
        },
    )

    assert evidence["failure_identities"] == [
        {
            "scene_id": 1,
            "im_id": 0,
            "obj_id": 1,
            "gt_id": 0,
            "instance_uuid": "11111111-1111-4111-8111-111111111111",
            "operation": "registration",
            "error_type": "RuntimeError",
        }
    ]
    assert "/secret" not in json.dumps(evidence)


def test_foundationpose_image_timing_evidence_is_deterministically_bounded() -> None:
    frame_count = 10_001
    evidence = bop_evaluation._foundationpose_execution_evidence(
        {
            "oracle_mask_contract": "bop_mask_visib_gt_instance.v1",
            "score_contract": "constant_1.0_no_detection_confidence",
            "execution_contract": "independent_register_per_target.v2",
            "track_segment_count": 0,
            "recorded_track_segment_count": 0,
            "omitted_track_segment_count": 0,
            "track_segments": [],
            "failures": [],
            "image_timings_seconds": {
                f"1/{im_id}": im_id / 10_000 for im_id in range(frame_count)
            },
        },
        inventory={
            "manifest": {
                "exports": [{"scene_id": 1, "sensor_name": "fixture"}]
            },
            "target_rows": {
                (1, im_id, 1): {"inst_count": 1}
                for im_id in range(frame_count)
            },
            "object_ids": {1},
        },
        execution_mode="independent_registration",
        counts={
            "registration_count": frame_count,
            "tracking_count": 0,
            "reinitialization_count": 0,
            "selected_target_count": frame_count,
            "processed_target_count": frame_count,
            "selected_scope_excluded_target_count": 0,
            "profile_excluded_target_count": 0,
            "estimate_count": frame_count,
            "failure_count": 0,
        },
    )

    assert evidence["recorded_image_timing_count"] == 10_000
    assert evidence["omitted_image_timing_count"] == 1
    assert list(evidence["image_timings_seconds"])[-1] == "1/9999"


def test_public_result_descriptor_never_copies_unsafe_extension_values() -> None:
    public = bop_evaluation.public_result_descriptor(
        {
            "result_id": "result-aaaaaaaaaaaa",
            "external_job": {
                "provider": "posetestbot-cluster",
                "job_id": "/secret/remote/job",
                "slurm_job_id": "/secret/slurm",
            },
            "sensor_scope": {
                "schema_version": "bop_result_sensor_scope.v1",
                "scope_kind": "selected_bop_sensor_scenes",
                "execution_mode": "continuous_tracking",
                "selected_scene_ids": ["/secret/scene"],
                "eligible_scene_ids": [1],
                "selected_target_inventory_sha256": "a" * 64,
                "selected_target_count": 1,
                "full_dataset_target_count": 1,
                "excluded_target_count": 0,
                "is_sensor_scoped": False,
            },
            "tracking": {
                "oracle_mask_contract": "/secret/mask",
                "registration_count": 1,
            },
            "controller_provenance_sha256": "/secret/provenance",
        }
    )

    assert "external_job" not in public
    assert "sensor_scope" not in public
    assert public["tracking"] == {"registration_count": 1}
    assert public["provenance_available"] is False
    assert "/secret" not in json.dumps(public)


def test_registered_result_metadata_cannot_escape_its_immutable_folder(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(tmp_path / f"method_{dataset['dataset_alias']}-test.csv")
    result = import_bop_result(run_root, source, method_name="Method")
    record_path = (
        run_root
        / "processed"
        / "bop_evaluation"
        / "results"
        / result["result_id"]
        / "result.json"
    )
    record = json.loads(record_path.read_text())
    record["path"] = "../../../../../bop/dataset_info.json"
    record["result_path"] = record["path"]
    record_path.chmod(0o600)
    _write_json(record_path, record)

    [listed] = list_results(run_root)

    assert listed["compatible"] is False
    assert listed["blockers"][0]["code"] == "result_path_invalid"
    with pytest.raises(ValueError, match="compatible"):
        result_file_path(run_root, result["result_id"])


def test_simulation_is_deterministic_and_never_mutates_ground_truth(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    gt_path = run_root / "bop" / "test" / "000001" / "scene_gt.json"
    original_gt = gt_path.read_bytes()

    first = create_simulated_bop_result(
        run_root,
        method_name="simone",
        translation_sigma_mm=1.0,
        rotation_sigma_deg=0.25,
        seed=1234,
    )
    second = create_simulated_bop_result(
        run_root,
        method_name="simtwo",
        translation_sigma_mm=1.0,
        rotation_sigma_deg=0.25,
        seed=1234,
    )

    assert _read_rows(_result_path(run_root, first)) == _read_rows(
        _result_path(run_root, second)
    )
    assert gt_path.read_bytes() == original_gt
    assert first["simulated"] is True
    assert first["simulation"] == {
        "translation_sigma_mm": 1.0,
        "rotation_sigma_deg": 0.25,
        "seed": 1234,
    }


def test_zero_offset_simulation_is_gt_equivalent(tmp_path: Path) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)

    record = create_simulated_bop_result(
        run_root,
        method_name="gtsmoke",
        translation_sigma_mm=0.0,
        rotation_sigma_deg=0.0,
        seed=7,
    )
    [row] = _read_rows(_result_path(run_root, record))

    assert [float(value) for value in row["R"].split()] == pytest.approx(
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    )
    assert [float(value) for value in row["t"].split()] == pytest.approx(
        [0.0, 0.0, 500.0]
    )
    assert float(row["time"]) == -1.0


@pytest.mark.parametrize(
    ("artifact", "mutate", "message"),
    [
        (
            "scene_gt.json",
            lambda value: value["0"][0].__setitem__("cam_R_m2c", [1.0] * 8),
            "cam_R_m2c",
        ),
    ],
)
def test_inspection_blocks_malformed_camera_and_ground_truth_shapes(
    tmp_path: Path,
    artifact: str,
    mutate: Any,
    message: str,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    path = run_root / "bop" / "test" / "000001" / artifact
    value = json.loads(path.read_text())
    mutate(value)
    _write_json(path, value)

    inspection = inspect_dataset(run_root)

    assert inspection["evaluation_ready"] is False
    assert any(message in blocker for blocker in inspection["blockers"])


def test_inspection_rejects_escaped_targets_and_symlinked_or_mismatched_depth(
    tmp_path: Path,
) -> None:
    escaped_run = make_tiny_evaluation_run(tmp_path, name="escaped")
    manifest_path = escaped_run / "bop" / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["targets_path"] = "../../outside-targets.json"
    _write_json(manifest_path, manifest)
    escaped = inspect_dataset(escaped_run)
    assert escaped["evaluation_ready"] is False
    assert any("remain below" in blocker for blocker in escaped["blockers"])

    symlink_run = make_tiny_evaluation_run(tmp_path, name="symlinked")
    depth = symlink_run / "bop" / "test" / "000001" / "depth" / "000000.png"
    external_depth = tmp_path / "external-depth.png"
    external_depth.write_bytes(depth.read_bytes())
    depth.unlink()
    depth.symlink_to(external_depth)
    symlinked = inspect_dataset(symlink_run)
    assert symlinked["evaluation_ready"] is False
    assert any("depth" in blocker.lower() for blocker in symlinked["blockers"])

    mismatch_run = make_tiny_evaluation_run(tmp_path, name="mismatched")
    mismatch_depth = mismatch_run / "bop" / "test" / "000001" / "depth" / "000000.png"
    assert cv2.imwrite(
        mismatch_depth.as_posix(),
        np.full((4, 8), 500, dtype=np.uint16),
    )
    mismatched = inspect_dataset(mismatch_run)
    assert mismatched["evaluation_ready"] is False
    assert any("dimensions" in blocker for blocker in mismatched["blockers"])


def test_worker_depth_content_hash_binds_every_target_depth_image(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    before = inspect_dataset(run_root, include_depth_content=True)
    depth = run_root / "bop" / "test" / "000001" / "depth" / "000000.png"
    assert cv2.imwrite(
        depth.as_posix(),
        np.full((8, 8), 501, dtype=np.uint16),
    )

    after = inspect_dataset(run_root, include_depth_content=True)

    assert before["depth_content_hashed"] is True
    assert isinstance(before["dataset_content_sha256"], str)
    assert before["dataset_alias"] == after["dataset_alias"]
    assert before["dataset_content_sha256"] != after["dataset_content_sha256"]


def test_evaluation_request_rejects_a_result_bound_to_an_old_dataset_hash(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    dataset = inspect_dataset(run_root)
    source = write_result_csv(tmp_path / f"method_{dataset['dataset_alias']}-test.csv")
    result = import_bop_result(run_root, source, method_name="Method")
    scene_gt = run_root / "bop" / "test" / "000001" / "scene_gt.json"
    gt = json.loads(scene_gt.read_text())
    gt["0"][0]["cam_t_m2c"][0] = 11.0
    _write_json(scene_gt, gt)

    with pytest.raises(ValueError, match="dataset.*(changed|hash)|hash.*dataset"):
        create_evaluation_request(run_root, result_id=result["result_id"])

    assert list_evaluations(run_root) == []


def test_annotation_free_export_reports_blockers_and_disables_simulation(
    tmp_path: Path,
) -> None:
    run_root = make_tiny_evaluation_run(tmp_path)
    bop_root = run_root / "bop"
    scene_root = bop_root / "test" / "000001"
    (scene_root / "scene_gt.json").unlink()
    (scene_root / "scene_gt_info.json").unlink()
    manifest_path = bop_root / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["annotation_source"] = "none"
    manifest["annotation_state"] = "absent"
    manifest["exports"][0]["annotation_source"] = "none"
    manifest["exports"][0]["artifacts"].pop("scene_gt")
    manifest["exports"][0]["artifacts"].pop("scene_gt_info")
    manifest["capabilities"]["gt_annotations"] = False
    manifest["capabilities"]["bop19_evaluation"] = False
    manifest["validation"]["annotation_count"] = 0
    manifest["validation"]["capabilities"]["gt_annotations"] = False
    manifest["validation"]["capabilities"]["bop19_evaluation"] = False
    _write_json(manifest_path, manifest)

    inspection = inspect_dataset(run_root)

    assert inspection["evaluation_ready"] is False
    assert inspection["counts"]["annotations"] == 0
    assert any(
        "ground truth" in blocker.lower() or "annotation" in blocker.lower()
        for blocker in inspection["blockers"]
    )
    with pytest.raises(ValueError, match="ground truth|annotation"):
        create_simulated_bop_result(
            run_root,
            method_name="unavailable",
            translation_sigma_mm=1.0,
            rotation_sigma_deg=0.25,
            seed=1,
        )
