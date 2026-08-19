from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from posetestbot.blenderproc.rendering import (
    ANALYTIC_IMPLEMENTATION_REVISION,
    WORKSPACE_RENDER_SCRIPT,
    discover_render_jobs,
    run_render_jobs,
)
from posetestbot.io.artifacts import (
    BLENDERPROC_RENDER_PLAN,
    DATASET_MANIFEST,
    MASKS_DIR,
    MATCH_ROBOT_EE_POSES,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def create_prepared_render_fixture(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "run-1"
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="pose_and_masks",
            sensors=(SensorRunConfig("realsense_d435", "123", "D435"),),
        ),
    )
    bproc_folder = (
        run_root / "processed" / "synchronized" / "realsense_123" / "blenderproc"
    )
    objects_folder = bproc_folder / "objects"
    objects_folder.mkdir(parents=True)
    matched_poses = bproc_folder.parent / MATCH_ROBOT_EE_POSES
    write_json(
        matched_poses,
        {
            "000000.png": {
                "robot_ee_pose": {
                    "X": 0.0,
                    "Y": 0.0,
                    "Z": 0.0,
                    "A": 0.0,
                    "B": 0.0,
                    "C": 0.0,
                }
            }
        },
    )
    instance = {
        "instance_uuid": "11111111-1111-4111-8111-111111111111",
        "catalog_uuid": "22222222-2222-4222-8222-222222222222",
        "obj_id": 1,
        "name": "cube",
        "mesh": "cube.ply",
        "transform": "cube.npy",
        "texture": None,
    }
    write_json(
        bproc_folder / "objects.json",
        {
            "schema_version": "blenderproc_object_instances.v1",
            "template_uuid": "33333333-3333-4333-8333-333333333333",
            "bundle_sha256": "a" * 64,
            "instances": [instance],
        },
    )
    np.save(
        bproc_folder / "camera_matrix.npy",
        np.array([[50.0, 0.0, 40.0], [0.0, 50.0, 30.0], [0.0, 0.0, 1.0]]),
    )
    np.save(bproc_folder / "camera_poses.npy", np.eye(4)[None, :, :])
    write_json(
        bproc_folder / "frame_contract.json",
        {
            "schema_version": "blenderproc_frame_contract.v1",
            "annotation_mode": "pose_and_masks",
            "projection": "native",
            "resolution": {"width": 80, "height": 60},
            "source_artifact_sha256": {
                MATCH_ROBOT_EE_POSES: hashlib.sha256(
                    matched_poses.read_bytes()
                ).hexdigest()
            },
            "frames": [
                {
                    "output_image_id": 0,
                    "source_frame_id": 0,
                    "source_filename": "000000.png",
                }
            ],
        },
    )
    (objects_folder / "cube.ply").write_text(
        "ply\nformat ascii 1.0\nelement vertex 0\nend_header\n"
    )
    np.save(objects_folder / "cube.npy", np.eye(4))
    return run_root, bproc_folder


def test_blenderproc_render_stage_dry_run_writes_plan_and_manifest(
    tmp_path: Path,
) -> None:
    run_root, _ = create_prepared_render_fixture(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_render_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose_and_masks",
            "--dry-run",
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Dry-run render plan created for 1 sensor folder" in result.stdout

    plan = json.loads((run_root / BLENDERPROC_RENDER_PLAN).read_text())
    assert plan["schema_version"] == "blenderproc_render_plan.v1"
    assert plan["dry_run"] is True
    assert plan["jobs"][0]["sensor_name"] == "realsense_123"
    assert plan["jobs"][0]["command"][:2] == ["blenderproc", "run"]
    assert plan["jobs"][0]["annotation_mode"] == "pose_and_masks"
    assert plan["jobs"][0]["resolution"] == [80, 60]
    assert plan["jobs"][0]["analytic_implementation_revision"] == (
        ANALYTIC_IMPLEMENTATION_REVISION
    )
    assert len(plan["jobs"][0]["render_script_sha256"]) == 64

    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage for stage in manifest["stages"] if stage["name"] == "blenderproc_render"
    )
    assert stage["status"] == "succeeded"
    assert stage["artifacts"][BLENDERPROC_RENDER_PLAN] == BLENDERPROC_RENDER_PLAN


def test_blenderproc_entrypoint_starts_with_runtime_import() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "blenderproc_render_720p_multi.py"
    )

    assert (
        script.read_text(encoding="utf-8")
        .splitlines()[0]
        .startswith("import blenderproc as bproc")
    )


def test_blenderproc_render_default_ignores_disabled_stale_sensor_folder(
    tmp_path: Path,
) -> None:
    run_root, enabled_prepared = create_prepared_render_fixture(tmp_path)
    synchronized = run_root / "processed" / "synchronized"
    disabled_sensor = synchronized / "realsense_999"
    shutil.copytree(enabled_prepared, disabled_sensor / "blenderproc")
    shutil.copy2(
        enabled_prepared.parent / MATCH_ROBOT_EE_POSES,
        disabled_sensor / MATCH_ROBOT_EE_POSES,
    )
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="pose_and_masks",
            run_root=run_root,
            sensors=(
                SensorRunConfig("realsense_d435", "123", "Enabled"),
                SensorRunConfig("realsense_d435", "999", "Disabled", enabled=False),
            ),
        ),
    )
    repo_root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        str(repo_root / "scripts" / "run_blenderproc_render_stage.py"),
        str(run_root),
        "--annotation-mode",
        "pose_and_masks",
        "--dry-run",
    ]

    subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    default_plan = json.loads((run_root / BLENDERPROC_RENDER_PLAN).read_text())
    assert [item["sensor_name"] for item in default_plan["jobs"]] == ["realsense_123"]

    subprocess.run(
        [*command, "--input-folder", str(synchronized)],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    explicit_plan = json.loads((run_root / BLENDERPROC_RENDER_PLAN).read_text())
    assert [item["sensor_name"] for item in explicit_plan["jobs"]] == [
        "realsense_123",
        "realsense_999",
    ]


def write_fake_render_output(workspace: Path) -> None:
    scene = workspace / "train_pbr" / "000000"
    scene.mkdir(parents=True, exist_ok=True)
    objects = json.loads((workspace / "objects.json").read_text())
    contract = json.loads((workspace / "frame_contract.json").read_text())
    instance = objects["instances"][0]
    write_json(
        scene / "scene_gt.json",
        {
            "0": [
                {
                    "obj_id": 1,
                    "cam_R_m2c": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    "cam_t_m2c": [0.0, 0.0, 500.0],
                }
            ]
        },
    )
    write_json(
        scene / "posetestbot_render_instances.json",
        {
            "schema_version": "posetestbot_render_instances.v1",
            "blenderproc_version": "2.8.0",
            "supported_blenderproc_version": "2.8.0",
            "annotation_mode": contract["annotation_mode"],
            "identity_contract": "bop_gt_index_matches_loaded_instance_order.v1",
            "instances": objects["instances"],
            "frame_bindings": contract["frames"],
            "frames": {
                "0": [
                    {
                        "gt_id": 0,
                        "obj_id": 1,
                        "instance_uuid": instance["instance_uuid"],
                        "catalog_uuid": instance["catalog_uuid"],
                    }
                ]
            },
        },
    )
    write_json(
        scene / "posetestbot_gt_provenance.json",
        {
            "schema_version": "posetestbot_gt_provenance.v1",
            "blenderproc_version": "2.8.0",
            "annotation_mode": contract["annotation_mode"],
            "pose_contract": "analytic_model_to_opencv_camera_rigid_transform.v1",
            "frame_bindings": contract["frames"],
            "source_artifact_sha256": contract["source_artifact_sha256"],
            "analytic_implementation": {
                "revision": ANALYTIC_IMPLEMENTATION_REVISION,
                "script_sha256": hashlib.sha256(
                    (workspace / WORKSPACE_RENDER_SCRIPT).read_bytes()
                ).hexdigest(),
            },
            "resolution": contract["resolution"],
        },
    )


@pytest.mark.parametrize("annotation_mode", ["pose", "pose_and_masks"])
def test_render_jobs_promote_pose_evidence_and_clear_stale_masks(
    tmp_path: Path,
    annotation_mode: str,
) -> None:
    run_root, bproc_folder = create_prepared_render_fixture(tmp_path)
    contract_path = bproc_folder / "frame_contract.json"
    contract = json.loads(contract_path.read_text())
    contract["annotation_mode"] = annotation_mode
    write_json(contract_path, contract)
    sensor_folder = bproc_folder.parent
    (sensor_folder / MASKS_DIR).mkdir()
    (sensor_folder / MASKS_DIR / "old.txt").write_text("old")
    (bproc_folder / "output").mkdir()
    (bproc_folder / "output" / "old.txt").write_text("old")
    jobs = discover_render_jobs(
        input_folder=run_root / "processed" / "synchronized",
        render_script=Path(__file__).resolve().parents[1]
        / "scripts"
        / "blenderproc_render_720p_multi.py",
        subdir="blenderproc",
        blenderproc_executable="blenderproc",
        annotation_mode=annotation_mode,
    )

    def fake_runner(command: list[str], *, check: bool) -> None:
        assert check is True
        write_fake_render_output(Path(command[-3]))

    artifacts = run_render_jobs(jobs, command_runner=fake_runner)

    assert not (sensor_folder / MASKS_DIR).exists()
    assert (bproc_folder / "output" / "scene_gt.json").is_file()
    assert (bproc_folder / "output" / "posetestbot_render_instances.json").is_file()
    assert (bproc_folder / "output" / "posetestbot_gt_provenance.json").is_file()
    assert not (bproc_folder / "output" / "scene_gt_info.json").exists()
    assert not (bproc_folder / "output" / "mask").exists()
    assert not (bproc_folder / "output" / "mask_visib").exists()
    assert not (bproc_folder / "output" / "old.txt").exists()
    assert artifacts["realsense_123:blenderproc_output"] == bproc_folder / "output"


def test_render_failure_preserves_every_previous_sensor_output(tmp_path: Path) -> None:
    run_root, first_prepared = create_prepared_render_fixture(tmp_path)
    synchronized = run_root / "processed" / "synchronized"
    second_prepared = synchronized / "zed_2i_456" / "blenderproc"
    shutil.copytree(first_prepared, second_prepared)
    shutil.copy2(
        first_prepared.parent / MATCH_ROBOT_EE_POSES,
        second_prepared.parent / MATCH_ROBOT_EE_POSES,
    )
    for prepared in (first_prepared, second_prepared):
        sensor = prepared.parent
        (sensor / MASKS_DIR).mkdir()
        (sensor / MASKS_DIR / "previous.txt").write_text(sensor.name)
        (prepared / "output").mkdir()
        (prepared / "output" / "previous.txt").write_text(sensor.name)
    jobs = discover_render_jobs(
        input_folder=synchronized,
        render_script=Path(__file__).resolve().parents[1]
        / "scripts"
        / "blenderproc_render_720p_multi.py",
        subdir="blenderproc",
        blenderproc_executable="blenderproc",
        annotation_mode="pose_and_masks",
    )
    calls = 0

    def failing_runner(command: list[str], *, check: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.CalledProcessError(1, command)
        write_fake_render_output(Path(command[-3]))

    with pytest.raises(subprocess.CalledProcessError):
        run_render_jobs(jobs, command_runner=failing_runner)

    for prepared in (first_prepared, second_prepared):
        sensor = prepared.parent
        assert (sensor / MASKS_DIR / "previous.txt").read_text() == sensor.name
        assert (prepared / "output" / "previous.txt").read_text() == sensor.name
    assert not list(synchronized.rglob("*.staging"))
    assert not list(synchronized.rglob("*.work"))


def test_render_rejects_blenderproc_masks_or_gt_info(tmp_path: Path) -> None:
    run_root, prepared = create_prepared_render_fixture(tmp_path)
    (prepared / "output").mkdir()
    (prepared / "output" / "previous.txt").write_text("keep")
    jobs = discover_render_jobs(
        input_folder=run_root / "processed" / "synchronized",
        render_script=Path(__file__).resolve().parents[1]
        / "scripts"
        / "blenderproc_render_720p_multi.py",
        subdir="blenderproc",
        blenderproc_executable="blenderproc",
        annotation_mode="pose_and_masks",
    )

    def invalid_runner(command: list[str], *, check: bool) -> None:
        assert check is True
        workspace = Path(command[-3])
        write_fake_render_output(workspace)
        write_json(
            workspace / "train_pbr" / "000000" / "scene_gt_info.json",
            {"0": [{}]},
        )

    with pytest.raises(
        ValueError,
        match="must not contain renderer-owned",
    ):
        run_render_jobs(jobs, command_runner=invalid_runner)

    assert (prepared / "output" / "previous.txt").read_text() == "keep"


def test_render_rejects_same_key_matched_pose_value_mutation(
    tmp_path: Path,
) -> None:
    run_root, prepared = create_prepared_render_fixture(tmp_path)
    (prepared / "output").mkdir()
    (prepared / "output" / "previous.txt").write_text("keep")
    jobs = discover_render_jobs(
        input_folder=run_root / "processed" / "synchronized",
        render_script=Path(__file__).resolve().parents[1]
        / "scripts"
        / "blenderproc_render_720p_multi.py",
        subdir="blenderproc",
        blenderproc_executable="blenderproc",
        annotation_mode="pose_and_masks",
    )
    matched_path = prepared.parent / MATCH_ROBOT_EE_POSES
    matched = json.loads(matched_path.read_text())
    matched["000000.png"]["robot_ee_pose"]["X"] = 0.125
    write_json(matched_path, matched)
    called = False

    def unexpected_runner(command: list[str], *, check: bool) -> None:
        nonlocal called
        called = True

    with pytest.raises(
        ValueError,
        match="changed after BlenderProc preparation",
    ):
        run_render_jobs(jobs, command_runner=unexpected_runner)

    assert called is False
    assert (prepared / "output" / "previous.txt").read_text() == "keep"


def test_objectless_render_skips_runtime_and_input_validation(tmp_path: Path) -> None:
    run_root = tmp_path / "objectless"
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="pose_and_masks",
            sensors=(SensorRunConfig("realsense_d435", "123", "D435"),),
        ),
    )
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_render_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose_and_masks",
            "--objectless",
            "--render-script",
            str(tmp_path / "missing.py"),
            "--blenderproc",
            "definitely-missing",
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    plan = json.loads((run_root / BLENDERPROC_RENDER_PLAN).read_text())
    assert plan["skipped"] is True
    assert plan["skip_reason"] == "objectless_run"
    assert plan["jobs"] == []
    assert "Skipped BlenderProc" in result.stdout


def test_blenderproc_render_prefers_rectified_sensor_tree(tmp_path: Path) -> None:
    run_root, synchronized_prepared = create_prepared_render_fixture(tmp_path)
    synchronized_sensor = synchronized_prepared.parent
    rectified_sensor = run_root / "processed" / "rectified" / synchronized_sensor.name
    shutil.copytree(synchronized_sensor, rectified_sensor)
    repo_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_render_stage.py"),
            str(run_root),
            "--annotation-mode",
            "pose_and_masks",
            "--dry-run",
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    plan = json.loads((run_root / BLENDERPROC_RENDER_PLAN).read_text())
    assert plan["jobs"][0]["sensor_folder"] == rectified_sensor.as_posix()


def test_render_rejects_mode_different_from_preparation(tmp_path: Path) -> None:
    run_root, _ = create_prepared_render_fixture(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_blenderproc_render_stage.py"),
            str(run_root),
            "--dry-run",
            "--annotation-mode",
            "pose",
        ],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "annotation mode does not match" in result.stderr


def test_analytic_scene_gt_uses_model_to_opencv_camera_transform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_blenderproc = types.ModuleType("blenderproc")
    fake_blenderproc.__version__ = "2.8.0"
    monkeypatch.setitem(sys.modules, "blenderproc", fake_blenderproc)
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "blenderproc_render_720p_multi.py"
    )
    spec = importlib.util.spec_from_file_location("test_bproc_render_script", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validated_blenderproc_version() == "2.8.0"

    objects = tmp_path / "objects"
    objects.mkdir()
    template_from_object = np.eye(4)
    template_from_object[:3, 3] = [1.1, 2.2, 3.3]
    np.save(objects / "instance.npy", template_from_object)
    template_from_camera = np.array(
        [
            [0.0, -1.0, 0.0, 1.0],
            [1.0, 0.0, 0.0, 2.0],
            [0.0, 0.0, 1.0, 3.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )
    instances = [
        {
            "instance_uuid": "11111111-1111-4111-8111-111111111111",
            "obj_id": 7,
            "transform": "instance.npy",
        }
    ]

    scene_gt = module.build_analytic_scene_gt(
        template_from_camera[None, :, :],
        objects.as_posix(),
        instances,
    )

    annotation = scene_gt["0"][0]
    np.testing.assert_allclose(
        np.asarray(annotation["cam_R_m2c"]).reshape(3, 3),
        np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]),
        atol=1e-12,
    )
    np.testing.assert_allclose(
        annotation["cam_t_m2c"],
        [200.0, -100.0, 300.0],
        atol=1e-9,
    )
    assert annotation["obj_id"] == 7
