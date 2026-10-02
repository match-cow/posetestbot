from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from posetestbot.bop import evaluation, robot_consistency as rc
from posetestbot.calibration.profiles import TransformFrame, profile_to_dict
from posetestbot.sensors.contracts import MountingMode
from tests.test_bop_evaluation import write_result_csv
from tests.test_bop_inspection import make_inspection_run
from tests.test_calibration_profiles import static_profile


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pose(rotation=None, translation=(0, 0, 0)) -> np.ndarray:
    pose = np.eye(4)
    pose[:3, :3] = np.eye(3) if rotation is None else rotation
    pose[:3, 3] = translation
    return pose


def make_robot_consistency_run(tmp_path: Path) -> Path:
    run = make_inspection_run(tmp_path, two_frames=True)
    bop = run / "bop"
    scene = bop / "test/000001"
    source = run / "processed/synchronized/realsense_fixture"
    profile = static_profile()
    profile = replace(
        profile,
        mounting_mode=MountingMode.EYE_IN_HAND,
        extrinsics=replace(
            profile.extrinsics,
            to_frame=TransformFrame.ROBOT_FLANGE,
            translation_mm=(10, 20, 30),
            rotation_quaternion_wxyz=(np.sqrt(0.5), 0, 0, np.sqrt(0.5)),
        ),
    )
    # Independent transforms include nonzero hand-eye translation/rotation,
    # flange rotation and sparse raw frame IDs reindexed to BOP image IDs.
    hand_eye = _pose(Rotation.from_euler("z", np.pi / 2).as_matrix(), (10, 20, 30))
    references = [
        _pose(translation=(100, 200, 300)) @ hand_eye,
        _pose(Rotation.from_euler("z", 0.3).as_matrix(), (130, 220, 310)) @ hand_eye,
    ]
    model_to_base = references[0] @ _pose(translation=(0, 0, 500))
    gt = {}
    for index, reference in enumerate(references):
        model_to_camera = np.linalg.inv(reference) @ model_to_base
        gt[str(index)] = [
            {
                "obj_id": 1,
                "cam_R_m2c": model_to_camera[:3, :3].reshape(-1).tolist(),
                "cam_t_m2c": model_to_camera[:3, 3].tolist(),
            }
        ]
    _json(scene / "scene_gt.json", gt)
    matched_path = source / "match_robot_ee_poses.json"
    _json(
        matched_path,
        {
            "000007.png": {"robot_ee_pose": dict(X=100, Y=200, Z=300, A=0, B=0, C=0)},
            "000042.png": {"robot_ee_pose": dict(X=130, Y=220, Z=310, A=0.3, B=0, C=0)},
        },
    )
    prepared_path = source / "blenderproc/camera_poses.npy"
    prepared_path.parent.mkdir()
    prepared = np.asarray(references)
    prepared[:, :3, 3] /= 1000
    np.save(prepared_path, prepared)
    provenance_path = scene / "posetestbot_gt_provenance.json"
    _json(
        provenance_path,
        {
            "schema_version": "posetestbot_gt_provenance.v1",
            "translation_unit": "mm",
            "coordinate_frames": {
                "camera_pose_input": "template_base_from_opencv_camera"
            },
            "source_artifact_sha256": {
                "match_robot_ee_poses.json": _hash(matched_path)
            },
            "input_sha256": {"camera_poses.npy": _hash(prepared_path)},
            "frame_bindings": [
                {
                    "output_image_id": index,
                    "source_frame_id": frame_id,
                    "source_filename": f"{frame_id:06d}.png",
                }
                for index, frame_id in enumerate((7, 42))
            ],
        },
    )
    manifest_path = bop / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    export = manifest["exports"][0]
    export.update(
        calibration_profile_id=profile.profile_id,
        projection="native",
        input_sensor_folder=source.relative_to(run).as_posix(),
        annotation_provenance={"sha256": _hash(provenance_path)},
    )
    manifest["calibration_profiles"] = [profile_to_dict(profile)]
    _json(manifest_path, manifest)
    _json(
        bop / "posetestbot_bop_frame_map.json",
        {
            "schema_version": "posetestbot_bop_frame_map.v3",
            "scenes": {
                "1": {
                    **{
                        key: export[key]
                        for key in (
                            "sensor_name",
                            "split",
                            "scene_folder",
                            "projection",
                            "input_sensor_folder",
                        )
                    },
                    "frames": {
                        str(index): {"source_rgb": f"rgb/{frame_id:06d}.png"}
                        for index, frame_id in enumerate((7, 42))
                    },
                }
            },
        },
    )
    return run


def _inputs(run: Path):
    manifest = json.loads((run / "bop/bop_export_manifest.json").read_text())
    targets = json.loads((run / "bop/test_targets_bop19.json").read_text())
    return manifest, targets, rc.freeze_inputs(run, manifest, targets)


def _result(run: Path, inputs: dict, offsets=(0, 4)) -> Path:
    gt = json.loads((run / "bop/test/000001/scene_gt.json").read_text())
    rows = []
    for frame, offset in zip(inputs["scenes"][0]["frames"], offsets, strict=True):
        im_id = frame["im_id"]
        annotation = gt[str(im_id)][0]
        reference = np.asarray(frame["camera_to_reference_mm"])
        prediction = _pose(
            np.asarray(annotation["cam_R_m2c"]).reshape(3, 3), annotation["cam_t_m2c"]
        )
        # Add error along a fixed reference-frame axis, independent of camera.
        prediction[:3, 3] += reference[:3, :3].T @ [offset, 0, 0]
        rows.append(
            dict(
                scene_id=1,
                im_id=im_id,
                obj_id=1,
                score=1,
                R=" ".join(map(str, prediction[:3, :3].reshape(-1))),
                t=" ".join(map(str, prediction[:3, 3])),
                time=-1,
            )
        )
    dataset = evaluation.inspect_dataset(run)
    return write_result_csv(
        run.parent / f"metric_{dataset['dataset_alias']}-test.csv", rows=rows
    )


def test_ipd_arithmetic_mean_and_exact_vertex_metrics() -> None:
    references = np.asarray([np.eye(4), _pose(translation=(10, 20, 30))])
    rotations = [np.eye(3), Rotation.from_euler("z", np.pi / 2).as_matrix()]
    predictions = np.asarray(
        [
            np.linalg.inv(reference) @ _pose(rotation)
            for reference, rotation in zip(references, rotations, strict=True)
        ]
    )
    vertices = np.asarray([[0, 0, 0], [10, 0, 0], [0, 20, 0]])
    result = rc.measure_track(references, predictions, vertices, {})
    # IPD averages matrices elementwise, retaining rotation shrinkage. A rigid
    # rotation average would give different distances and fails this contract.
    assert result["mvd_mm"] == pytest.approx(np.sqrt(200))
    assert result["add_mm"] == pytest.approx((np.sqrt(50) + np.sqrt(200)) / 3)
    mean = np.asarray(result["reference_mean_model_to_template_base_mm"])
    assert np.linalg.det(mean[:3, :3]) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "symmetry", ["discrete", "continuous", "offset", "sphere", "mixed"]
)
def test_symmetry_equivalent_estimates_have_zero_consistency(symmetry: str) -> None:
    rotation = Rotation.from_euler("z", np.pi).as_matrix()
    offset = np.asarray([12, 5, 0]) if symmetry == "offset" else np.zeros(3)
    equivalent = _pose(rotation, offset - rotation @ offset)
    if symmetry == "discrete":
        info = {
            "symmetries_discrete": [
                np.eye(4).reshape(-1).tolist(),
                equivalent.reshape(-1).tolist(),
            ]
        }
    else:
        info = {
            "symmetries_continuous": [{"axis": [0, 0, 1], "offset": offset.tolist()}]
        }
        if symmetry == "sphere":
            info["symmetries_continuous"].append(
                {"axis": [1, 0, 0], "offset": [0, 0, 0]}
            )
        if symmetry == "mixed":
            info["symmetries_discrete"] = [
                _pose(Rotation.from_euler("x", np.pi).as_matrix()).reshape(-1).tolist()
            ]
    result = rc.measure_track(
        np.asarray([np.eye(4), np.eye(4)]),
        np.asarray([np.eye(4), equivalent]),
        np.asarray([[0, 0, 0], [10, 20, 5]]),
        info,
    )
    assert result["mvd_mm"] == pytest.approx(0, abs=1e-10)
    assert result["add_mm"] == pytest.approx(0, abs=1e-10)


def test_mixed_symmetry_preserves_ipd_nonrigid_mean_reduction() -> None:
    rotation = Rotation.from_euler("x", [30, 80], degrees=True).as_matrix().mean(axis=0)
    info = {
        "symmetries_continuous": [{"axis": [0, 0, 1], "offset": [0, 0, 0]}],
        "symmetries_discrete": [
            _pose(Rotation.from_euler("x", np.pi).as_matrix()).reshape(-1).tolist()
        ],
    }
    reduced = rc.canonical_pose(_pose(rotation), info)
    sine = (np.sin(np.deg2rad(30)) + np.sin(np.deg2rad(80))) / 2
    cosine = (np.cos(np.deg2rad(30)) + np.cos(np.deg2rad(80))) / 2
    diagonal = 1 - sine * sine / (1 + cosine)
    assert reduced[:3, :3] == pytest.approx(
        np.asarray([[1, 0, 0], [0, diagonal, -sine], [0, sine, diagonal]])
    )
    assert np.linalg.det(reduced[:3, :3]) != pytest.approx(1)


def test_redundant_discrete_spin_does_not_create_continuous_axis_flip() -> None:
    pose = _pose(Rotation.from_euler("x", 130, degrees=True).as_matrix())
    info = {"symmetries_continuous": [{"axis": [0, 0, 1], "offset": [0, 0, 0]}]}
    expected = rc.canonical_pose(pose, info)
    info["symmetries_discrete"] = [
        _pose(Rotation.from_euler("z", np.pi).as_matrix()).reshape(-1).tolist()
    ]
    assert rc.canonical_pose(pose, info) == pytest.approx(expected)


def test_export_robot_transforms_sparse_frame_binding_and_mm_scores(
    tmp_path: Path,
) -> None:
    run = make_robot_consistency_run(tmp_path)
    manifest, targets, inputs = _inputs(run)
    before = {path: _hash(path) for path in run.rglob("*") if path.is_file()}
    report = rc.evaluate(run, _result(run, inputs), manifest, targets, inputs)
    assert report["status"] == "available"
    assert report["coverage"] == 1
    assert report["evaluated_track_count"] == 1
    assert {
        metric["id"]: metric["value"] for metric in report["metrics"]
    } == pytest.approx({"robot_consistency_mvd": 2, "robot_consistency_add": 2})
    assert report["tracks"][0]["matched_frames"] == 2
    assert [row["im_id"] for row in report["tracks"][0]["frame_errors"]] == [0, 1]
    assert all(_hash(path) == digest for path, digest in before.items())


def test_consistent_bias_is_not_ground_truth_accuracy(tmp_path: Path) -> None:
    run = make_robot_consistency_run(tmp_path)
    manifest, targets, inputs = _inputs(run)
    report = rc.evaluate(
        run, _result(run, inputs, offsets=(20, 20)), manifest, targets, inputs
    )
    assert report["metrics"][0]["value"] == pytest.approx(0, abs=1e-10)
    assert report["coverage"] == 1


def test_unordered_repeated_instances_use_one_to_one_matching_and_macro_average(
    tmp_path: Path,
) -> None:
    run = make_robot_consistency_run(tmp_path)
    _, _, original_inputs = _inputs(run)
    scene = run / "bop/test/000001"
    gt = json.loads((scene / "scene_gt.json").read_text())
    info = json.loads((scene / "scene_gt_info.json").read_text())
    instance_path = run / "bop/posetestbot_instance_map.json"
    instance_map = json.loads(instance_path.read_text())
    for frame in original_inputs["scenes"][0]["frames"]:
        im_id = frame["im_id"]
        reference = np.asarray(frame["camera_to_reference_mm"])
        extra = dict(gt[str(im_id)][0])
        extra["cam_t_m2c"] = (
            np.asarray(extra["cam_t_m2c"]) + reference[:3, :3].T @ [80, 0, 0]
        ).tolist()
        gt[str(im_id)].append(extra)
        info[str(im_id)].append(dict(info[str(im_id)][0]))
        instance_map["instances"].append(
            {
                "scene_id": 1,
                "im_id": im_id,
                "gt_id": 1,
                "obj_id": 1,
                "instance_uuid": "22222222-2222-4222-8222-222222222222",
            }
        )
    _json(scene / "scene_gt.json", gt)
    _json(scene / "scene_gt_info.json", info)
    _json(instance_path, instance_map)
    _json(
        run / "bop/test_targets_bop19.json",
        [
            {"scene_id": 1, "im_id": im_id, "obj_id": 1, "inst_count": 2}
            for im_id in (0, 1)
        ],
    )
    manifest, targets, inputs = _inputs(run)
    first_result = _result(run, inputs)
    import csv

    with first_result.open() as handle:
        rows = list(csv.DictReader(handle))
    for im_id in (0, 1):
        annotation = gt[str(im_id)][1]
        rows.insert(
            0,
            {
                **rows[-1],
                "im_id": im_id,
                "score": 0.01,
                "R": " ".join(map(str, annotation["cam_R_m2c"])),
                "t": " ".join(map(str, annotation["cam_t_m2c"])),
            },
        )
    rows.append({**rows[0], "score": 100, "t": "1000 1000 1000"})
    path = write_result_csv(first_result, rows=rows)
    report = rc.evaluate(run, path, manifest, targets, inputs)
    assert report["evaluated_track_count"] == 2
    assert report["eligible_frames"] == report["matched_frames"] == 4
    assert report["unmatched_predictions"] == 1
    assert sorted(track["mvd_mm"] for track in report["tracks"]) == pytest.approx(
        [0, 2], abs=1e-10
    )
    assert report["metrics"][0]["value"] == pytest.approx(1)


@pytest.mark.parametrize(
    "missing", ["calibration", "matched", "prepared", "instance_map", "static"]
)
def test_missing_prerequisites_are_unavailable_without_fabricating_metrics(
    tmp_path: Path, missing: str
) -> None:
    run = make_robot_consistency_run(tmp_path)
    manifest, targets, original = _inputs(run)
    result_path = _result(run, original)
    if missing == "calibration":
        manifest.pop("calibration_profiles")
    elif missing == "static":
        manifest["calibration_profiles"] = [profile_to_dict(static_profile())]
    else:
        relative = {
            "matched": "processed/synchronized/realsense_fixture/match_robot_ee_poses.json",
            "prepared": "processed/synchronized/realsense_fixture/blenderproc/camera_poses.npy",
            "instance_map": "bop/posetestbot_instance_map.json",
        }[missing]
        (run / relative).unlink()
    inputs = rc.freeze_inputs(run, manifest, targets)
    report = rc.evaluate(run, result_path, manifest, targets, inputs)
    assert report["status"] == "unavailable"
    assert report["metrics"] == []
    assert report["coverage"] is None
    assert report["excluded_scenes"][0]["reason"]


def test_rejected_or_missing_predictions_do_not_create_a_zero_score(
    tmp_path: Path,
) -> None:
    run = make_robot_consistency_run(tmp_path)
    manifest, targets, inputs = _inputs(run)
    report = rc.evaluate(
        run, _result(run, inputs, offsets=(0, 101)), manifest, targets, inputs
    )
    assert report["status"] == "unavailable"
    assert report["metrics"] == []
    assert report["coverage"] == 0.5
    assert report["unmatched_predictions"] == 1
    assert report["tracks"][0]["matched_frames"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "matched_pose",
        "frame_map",
        "instance_map",
        "prepared_pose",
        "symlink",
        "frame_binding",
        "calibration",
        "moving_object",
    ],
)
def test_corrupt_changed_or_unsafe_robot_evidence_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    run = make_robot_consistency_run(tmp_path)
    manifest, targets, inputs = _inputs(run)
    source = run / "processed/synchronized/realsense_fixture"
    paths = {
        "matched_pose": source / "match_robot_ee_poses.json",
        "frame_map": run / "bop/posetestbot_bop_frame_map.json",
        "instance_map": run / "bop/posetestbot_instance_map.json",
        "prepared_pose": source / "blenderproc/camera_poses.npy",
    }
    if mutation in paths:
        paths[mutation].write_bytes(paths[mutation].read_bytes() + b" ")
        with pytest.raises(ValueError, match="changed"):
            rc.verify_sources(run, inputs)
        return
    elif mutation == "symlink":
        path = source / "match_robot_ee_poses.json"
        outside = tmp_path / "outside.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif mutation == "frame_binding":
        path = run / "bop/posetestbot_bop_frame_map.json"
        data = json.loads(path.read_text())
        data["scenes"]["1"]["frames"]["1"]["source_rgb"] = "rgb/000007.png"
        _json(path, data)
    elif mutation == "calibration":
        manifest["calibration_profiles"][0]["extrinsics"]["translation_mm"][0] += 1
    else:
        path = run / "bop/test/000001/scene_gt.json"
        data = json.loads(path.read_text())
        data["1"][0]["cam_t_m2c"][0] += 1
        _json(path, data)
    with pytest.raises(ValueError):
        rc.freeze_inputs(run, manifest, targets)


def test_queued_evaluation_publishes_bop_and_robot_consistency_with_frozen_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    run = make_robot_consistency_run(tmp_path)
    _, _, inputs = _inputs(run)
    result = evaluation.import_bop_result(run, _result(run, inputs))
    request = evaluation.create_evaluation_request(run, result_id=result["result_id"])
    request_path = evaluation.evaluation_request_path(run, request["evaluation_id"])
    frozen = request_path.parent / rc.INPUTS_FILENAME
    assert frozen.stat().st_mode & 0o222 == 0
    assert _hash(frozen) == request["robot_consistency"]["inputs_sha256"]
    monkeypatch.setattr(evaluation, "toolkit_status", lambda _: {"available": True})

    def official_fixture(command, **kwargs):
        assert command[command.index("--num-workers") + 1]
        scores = (
            Path(command[command.index("--eval-path") + 1])
            / Path(command[command.index("--result-filename") + 1]).stem
            / "scores_bop19.json"
        )
        _json(
            scores,
            {
                "bop19_average_recall": 0.8,
                "bop19_average_recall_vsd": 0.7,
                "bop19_average_recall_mssd": 0.8,
                "bop19_average_recall_mspd": 0.9,
                "bop19_average_time_per_image": -1,
            },
        )

    monkeypatch.setattr(evaluation.subprocess, "run", official_fixture)
    report = evaluation.run_evaluation_request(request_path, app_root=tmp_path)
    assert len(report["metrics"]) == 7
    assert report["official_scores"]["bop19_average_recall"] == 0.8
    assert report["robot_consistency"]["status"] == "available"
    assert report["robot_consistency"]["metrics"][0]["value"] == pytest.approx(2)
    retained = json.loads((request_path.parent / rc.REPORT_FILENAME).read_text())
    assert retained["dataset_sha256"] == request["dataset_sha256"]
    assert retained["result_sha256"] == result["sha256"]
    assert retained["inputs_sha256"] == _hash(frozen)
    assert retained["tracks"][0]["frame_errors"]
    assert "frame_errors" not in report["robot_consistency"]["tracks"][0]
    assert (
        evaluation.list_evaluations(run)[0]["robot_consistency"]
        == report["robot_consistency"]
    )


@pytest.mark.parametrize("changed", ["source", "frozen_snapshot"])
def test_worker_rejects_changed_robot_inputs_before_invoking_the_toolkit(
    tmp_path: Path, monkeypatch, changed: str
) -> None:
    run = make_robot_consistency_run(tmp_path)
    request = evaluation.create_evaluation_request(
        run, simulation={"translation_sigma_mm": 0, "rotation_sigma_deg": 0, "seed": 42}
    )
    request_path = evaluation.evaluation_request_path(run, request["evaluation_id"])
    if changed == "source":
        path = (
            run / "processed/synchronized/realsense_fixture/match_robot_ee_poses.json"
        )
    else:
        path = request_path.parent / rc.INPUTS_FILENAME
        path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b" ")
    calls = []
    monkeypatch.setattr(
        evaluation.subprocess, "run", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(ValueError, match="changed|integrity"):
        evaluation.run_evaluation_request(request_path, app_root=tmp_path)
    assert calls == []
    assert not (request_path.parent / "report.json").exists()
    assert (
        json.loads((request_path.parent / "progress.json").read_text())["status"]
        == "failed"
    )


def test_robot_snapshot_and_scores_respect_selected_sensor_scenes(
    tmp_path: Path,
) -> None:
    run = make_robot_consistency_run(tmp_path)
    manifest, targets, _ = _inputs(run)
    manifest["exports"].append(
        {
            **manifest["exports"][0],
            "scene_id": 2,
            "scene_folder": "test/000002",
            "input_sensor_folder": "../outside",
        }
    )
    # An unselected scene must not contribute frames, identities or estimates
    # or cause its unrelated robot-input folder to be opened.
    snapshot = rc.freeze_inputs(run, manifest, targets)
    assert [scene["scene_id"] for scene in snapshot["scenes"]] == [1]
    report = rc.evaluate(run, _result(run, snapshot), manifest, targets, snapshot)
    assert {track["scene_id"] for track in report["tracks"]} == {1}
    assert report["matched_frames"] == 2
