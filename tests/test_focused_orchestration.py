from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from posetestbot.pipeline.capture_completion import build_capture_completion
from posetestbot.pipeline.orchestration import (
    capture_job_recipe,
    dataset_processing_commands,
    dataset_processing_job_recipe,
    execute_capture,
    plan_capture,
    preflight_job_recipe,
    process_dataset,
)
from posetestbot.pipeline.preflight import write_run_preflight_report
from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_config_from_token,
    write_run_config,
)
from posetestbot.sensors.contracts import CameraIntrinsics
from posetestbot.sensors.frame_writer import write_camera_sidecars
from posetestbot.web import route_support
from posetestbot.web.app import create_app


RUN_ID = "11111111-1111-4111-8111-111111111111"
SENSOR_ID = "123"
CALIBRATION_PROFILES = (
    "processed/calibration_inputs/" + "a" * 64 + "/calibration_profiles.json"
)
INTRINSIC_PROFILES = (
    "processed/calibration_inputs/"
    + "a" * 64
    + "/intrinsic_calibration_profiles.json"
)


def _sensor():
    return sensor_config_from_token(
        f"realsense_d435:{SENSOR_ID}:static:Cell RealSense"
    )


def _write_config(run_root: Path, *, intent: str) -> dict:
    selected = intent == "dataset"
    config = create_run_config(
        run_root=run_root,
        run_id=RUN_ID if intent == "calibration" else None,
        capture_intent=intent,
        bop_annotation_mode="none",
        sensors=(_sensor(),),
        calibration_profiles=CALIBRATION_PROFILES if selected else None,
        intrinsic_calibration_profiles=INTRINSIC_PROFILES if selected else None,
        calibration_profile_selection=(
            {
                "selection_artifact": "calibration_profile_selection.json",
                "bundle_sha256": "a" * 64,
                "selected_at": "2026-08-18T10:00:00+00:00",
            }
            if selected
            else None
        ),
    )
    write_run_config(run_root, config)
    return config.to_dict()


def _write_current_capture_evidence(run_root: Path, config: dict) -> None:
    sensor_root = run_root / "realsense_123"
    (sensor_root / "rgb").mkdir(parents=True, exist_ok=True)
    (sensor_root / "depth").mkdir(exist_ok=True)
    if not (sensor_root / "camera_data.json").exists():
        write_camera_sidecars(
            sensor_root,
            CameraIntrinsics(
                cam_k=(100.0, 0.0, 4.0, 0.0, 100.0, 4.0, 0.0, 0.0, 1.0),
                width=8,
                height=8,
                distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                depth_scale_to_mm=1.0,
            ),
        )
    records = []
    for index in range(2):
        rgb_path = f"rgb/{index}.png"
        depth_path = f"depth/{index}.png"
        (sensor_root / rgb_path).write_bytes(b"rgb")
        (sensor_root / depth_path).write_bytes(b"depth")
        records.append(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": SENSOR_ID,
                "frame_index": index,
                "frame_id": f"{index}.png",
                "rgb_path": rgb_path,
                "depth_path": depth_path,
                "sensor_timestamp_ns": index + 1,
                "host_received_timestamp_ns": index + 11,
                "host_wall_timestamp_ns": index + 21,
            }
        )
    (sensor_root / "frame_metadata.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    (run_root / "raw_robot_ee_poses.json").write_text(
        json.dumps(
            {
                "0": {
                    "host_received_timestamp_ns": 1,
                    "host_wall_timestamp_ns": 2,
                    "motion": "circ_far",
                    "pose": {"X": 0, "Y": 0, "Z": 0, "A": 0, "B": 0, "C": 0},
                    "source_packet": {
                        "schema_version": "robot_pose.v1",
                        "packet_kind": "pose",
                        "sequence": 0,
                        "sender_monotonic_ns": 1,
                        "sender_wall_timestamp_ms": 1,
                        "run_id": config["run_id"],
                        "from_frame": "robot_flange",
                        "to_frame": "template_base",
                        "sunrise_reference_frame_path": (
                            "/PoseTestBot/PoseTemplateBase"
                        ),
                        "sequence_delta": 0,
                        "estimated_packets_lost": 0,
                    },
                }
            }
        )
    )


def _completed_processes() -> list[dict]:
    return [
        {
            "role": "sensor_capture",
            "status": "stopped",
            "termination_reason": "stopped_after_receiver_exit",
            "ended_at": "2026-08-18T10:01:00+00:00",
        },
        {
            "role": "robot_pose_receiver",
            "status": "succeeded",
            "termination_reason": "receiver_completed",
            "ended_at": "2026-08-18T10:00:59+00:00",
        },
    ]


def test_plan_and_job_recipes_are_fixed_and_purpose_scoped(tmp_path: Path) -> None:
    run_root = tmp_path / "calibration"
    _write_config(run_root, intent="calibration")

    path, plan = plan_capture(run_root, max_frames=2, warmup_frames=3)

    assert path == run_root / "capture_plan.json"
    assert plan["capture"]["max_frames"] == 2
    assert plan["capture"]["warmup_frames"] == 3
    assert all("--allow-real-robot" not in item["command"] for item in plan["commands"])
    assert preflight_job_recipe(run_root).command == (
        "uv",
        "run",
        "python",
        "scripts/run_preflight.py",
        run_root.as_posix(),
        "--check",
        "--write",
    )
    assert preflight_job_recipe(run_root).resources == ("camera", "disk_io")
    capture = capture_job_recipe(
        run_root,
        intent="calibration",
        allow_cameras=True,
        allow_real_robot=True,
    )
    assert capture.command == (
        "uv",
        "run",
        "python",
        "scripts/run_capture.py",
        run_root.as_posix(),
        "--intent",
        "calibration",
        "--allow-cameras",
        "--allow-real-robot",
    )
    assert capture.resources == ("camera", "disk_io", "robot_command")
    assert capture.parameters["purpose"] == "capture"

    with pytest.raises(ValueError, match="requires allow_cameras=true"):
        capture_job_recipe(
            run_root,
            intent="calibration",
            allow_cameras=False,
            allow_real_robot=True,
        )
    with pytest.raises(ValueError, match="intent must be one of"):
        capture_job_recipe(
            run_root,
            intent="custom-stage",
            allow_cameras=True,
            allow_real_robot=True,
        )


def test_capture_rechecks_camera_before_writing_capture_artifacts(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "blocked-before-plan"
    config = _write_config(run_root, intent="calibration")
    write_run_preflight_report(
        run_root,
        {
            "schema_version": "run_preflight.v2",
            "overall_status": "ok",
            "config": config,
            "selected_sensor_readiness": {
                "schema_version": "selected_sensor_readiness.v1",
                "selected_count": 1,
                "ready_count": 1,
                "all_ready": True,
                "probe_contract": {
                    "record": False,
                    "frames_per_camera": 1,
                    "timeout_s_per_camera": 15.0,
                },
                "probes": [
                    {
                        "sensor_type": "realsense_d435",
                        "device_id": SENSOR_ID,
                        "capture_ready": True,
                        "status": "ready",
                        "recorded_output": False,
                    }
                ],
            },
        },
    )
    blocked_readiness = {
        "schema_version": "selected_sensor_readiness.v1",
        "selected_count": 1,
        "ready_count": 0,
        "all_ready": False,
        "probes": [
            {
                "sensor_type": "realsense_d435",
                "device_id": SENSOR_ID,
                "capture_ready": False,
                "status": "blocked",
                "message": "Selected camera is held by a crashed recorder.",
            }
        ],
    }

    with pytest.raises(ValueError, match="held by a crashed recorder"):
        execute_capture(
            run_root,
            intent="calibration",
            allow_cameras=True,
            allow_real_robot=True,
            probe_selected_sensors=lambda _config: blocked_readiness,
        )

    assert not (run_root / "capture_plan.json").exists()
    assert not (run_root / "capture_plan_preflight_report.json").exists()
    assert not (run_root / "capture_execution_plan.json").exists()
    assert not (run_root / "capture_execution_logs").exists()
    assert not (run_root / f"realsense_{SENSOR_ID}").exists()


def test_capture_completion_requires_only_current_raw_contracts(tmp_path: Path) -> None:
    run_root = tmp_path / "capture"
    config = _write_config(run_root, intent="calibration")
    _write_current_capture_evidence(run_root, config)

    report = build_capture_completion(run_root, config, _completed_processes())

    assert report["schema_version"] == "capture_completion.v1"
    assert report["status"] == "ok"
    assert report["error_count"] == 0

    metadata_path = run_root / "realsense_123" / "frame_metadata.jsonl"
    records = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    del records[0]["sensor_timestamp_ns"]
    metadata_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    report = build_capture_completion(run_root, config, _completed_processes())
    sensor_check = next(
        check for check in report["checks"] if check["name"].startswith("sensor:")
    )

    assert report["status"] == "error"
    assert "requires positive sensor_timestamp_ns" in sensor_check["details"][
        "metadata_error"
    ]

    _write_current_capture_evidence(run_root, config)
    poses = json.loads((run_root / "raw_robot_ee_poses.json").read_text())
    poses["0"]["source_packet"]["schema_version"] = "robot_pose.v0"
    (run_root / "raw_robot_ee_poses.json").write_text(json.dumps(poses))
    report = build_capture_completion(run_root, config, _completed_processes())
    pose_check = next(
        check for check in report["checks"] if check["name"] == "robot_pose_stream"
    )
    assert report["status"] == "error"
    assert "must use robot_pose.v1" in pose_check["details"]["error"]


def test_capture_completion_rejects_mismatched_paths_and_child_count(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "capture"
    config = _write_config(run_root, intent="calibration")
    _write_current_capture_evidence(run_root, config)
    metadata_path = run_root / "realsense_123" / "frame_metadata.jsonl"
    records = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    records[0]["rgb_path"], records[1]["rgb_path"] = (
        records[1]["rgb_path"],
        records[0]["rgb_path"],
    )
    metadata_path.write_text("".join(json.dumps(record) + "\n" for record in records))

    report = build_capture_completion(run_root, config, _completed_processes())
    sensor_check = next(
        check for check in report["checks"] if check["name"].startswith("sensor:")
    )
    assert report["status"] == "error"
    assert sensor_check["details"]["paths_match_frame_ids"] is False

    _write_current_capture_evidence(run_root, config)
    report = build_capture_completion(
        run_root,
        config,
        [*_completed_processes(), _completed_processes()[0]],
    )
    process_check = next(
        check
        for check in report["checks"]
        if check["name"] == "child_processes_and_resources"
    )
    assert report["status"] == "error"
    assert process_check["details"]["expected_sensor_count"] == 1
    assert process_check["details"]["completed_sensor_count"] == 2

    (run_root / "realsense_123" / "camera.json").unlink()
    report = build_capture_completion(run_root, config, _completed_processes())
    sensor_check = next(
        check for check in report["checks"] if check["name"].startswith("sensor:")
    )
    assert report["status"] == "error"
    assert sensor_check["details"]["sidecars_ok"] is False


def test_dataset_processing_is_exactly_four_fail_fast_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "dataset"
    _write_config(run_root, intent="dataset")
    monkeypatch.setattr(
        "posetestbot.pipeline.orchestration.verify_calibration_profile_selection",
        lambda *_args, **_kwargs: {"schema_version": "calibration_profile_selection.v2"},
    )

    commands = dataset_processing_commands(run_root)

    assert commands == (
        (
            "uv",
            "run",
            "python",
            "scripts/sync_run_non_destructive.py",
            run_root.as_posix(),
        ),
        (
            "uv",
            "run",
            "python",
            "scripts/run_sync_quality.py",
            run_root.as_posix(),
        ),
        (
            "uv",
            "run",
            "python",
            "scripts/run_camera_rectification.py",
            run_root.as_posix(),
            "--intrinsic-profiles",
            INTRINSIC_PROFILES,
            "--overwrite",
        ),
        (
            "uv",
            "run",
            "python",
            "scripts/run_bop_export_stage.py",
            run_root.as_posix(),
            "--overwrite",
            "--calibration-profiles",
            CALIBRATION_PROFILES,
            "--annotation-source",
            "none",
            "--annotation-mode",
            "none",
            "--objectless",
        ),
    )
    recipe = dataset_processing_job_recipe(run_root)
    assert recipe.command == (
        "uv",
        "run",
        "python",
        "scripts/process_dataset.py",
        run_root.as_posix(),
    )
    assert recipe.resources == ("cpu", "disk_io")
    assert recipe.parameters["purpose"] == "dataset_processing"

    calls: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        assert kwargs["check"] is True
        if len(calls) == 2:
            raise RuntimeError("quality failed")

    monkeypatch.setattr("posetestbot.pipeline.orchestration.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="quality failed"):
        process_dataset(run_root)
    assert calls == list(commands[:2])


class _FakeRunner:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        job_id = f"job-{len(self.submissions)}"
        return SimpleNamespace(
            id=job_id,
            status="queued",
            to_dict=lambda: {
                "id": job_id,
                "status": "queued",
                "scope_kind": kwargs["scope_kind"],
                "run_root": (
                    Path(kwargs["run_root"]).as_posix()
                    if kwargs["run_root"] is not None
                    else None
                ),
            },
        )


def test_purpose_specific_apis_reject_extra_fields_and_queue_exact_recipes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calibration_root = tmp_path / "calibration"
    dataset_root = tmp_path / "dataset"
    _write_config(calibration_root, intent="calibration")
    _write_config(dataset_root, intent="dataset")
    runner = _FakeRunner()
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    monkeypatch.setattr(route_support, "job_runner", runner)
    monkeypatch.setattr(
        route_support,
        "run_preflight_queue_summary",
        lambda *_args, **_kwargs: {
            "ready_for_queue": True,
            "queue_blocker": None,
        },
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.orchestration.verify_calibration_profile_selection",
        lambda *_args, **_kwargs: {"schema_version": "calibration_profile_selection.v2"},
    )
    client = create_app().test_client()

    for endpoint, payload in (
        ("/preflight/jobs", {"run_root": calibration_root.as_posix(), "stage": "x"}),
        (
            "/capture/jobs",
            {
                "run_root": calibration_root.as_posix(),
                "intent": "calibration",
                "allow_cameras": True,
                "allow_real_robot": True,
                "timeout_s": 1,
            },
        ),
        (
            "/dataset-processing/jobs",
            {"run_root": dataset_root.as_posix(), "commands": []},
        ),
    ):
        response = client.post(endpoint, json=payload)
        assert response.status_code == 400
        assert "Unsupported fields" in response.get_json()["output"]
    assert runner.submissions == []

    assert client.post(
        "/preflight/jobs", json={"run_root": calibration_root.as_posix()}
    ).status_code == 202
    assert client.post(
        "/capture/jobs",
        json={
            "run_root": calibration_root.as_posix(),
            "intent": "calibration",
            "allow_cameras": True,
            "allow_real_robot": True,
        },
    ).status_code == 202
    assert client.post(
        "/dataset-processing/jobs", json={"run_root": dataset_root.as_posix()}
    ).status_code == 202

    target_override = client.post(
        "/robot/commands",
        json={
            "command": "start",
            "run_root": calibration_root.as_posix(),
            "allow_cameras": True,
            "allow_real_robot": True,
            "robot_ip": "192.0.2.1",
        },
    )
    assert target_override.status_code == 400
    assert "robot_ip" in target_override.get_json()["output"]
    assert client.post(
        "/robot/commands",
        json={
            "command": "start",
            "run_root": calibration_root.as_posix(),
            "allow_cameras": True,
            "allow_real_robot": True,
        },
    ).status_code == 202
    assert client.post(
        "/robot/commands",
        json={"command": "stop", "confirm_idle_program_exit": True},
    ).status_code == 202

    assert [item["parameters"]["purpose"] for item in runner.submissions] == [
        "preflight",
        "capture",
        "dataset_processing",
        "robot_command",
        "robot_command",
    ]
    assert runner.submissions[-2]["command"] == [
        "uv",
        "run",
        "python",
        "start_iiwa.py",
        "--run-id",
        RUN_ID,
        "--manual-test-speed",
        "--allow-real-robot",
        "--allow-cameras",
    ]
    assert runner.submissions[-1]["command"] == [
        "uv",
        "run",
        "python",
        "stop_iiwa.py",
    ]
