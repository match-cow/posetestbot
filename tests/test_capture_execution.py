from __future__ import annotations

import json

import os

import signal

import subprocess

from dataclasses import replace

from pathlib import Path

import pytest

from posetestbot.io.artifacts import (
    CAPTURE_EXECUTION_LOGS_DIR,
    CAPTURE_EXECUTION_PLAN,
    CAPTURE_EXECUTION_REPORT,
    CAPTURE_EXECUTION_STATUS,
    CAPTURE_PLAN,
    DATASET_MANIFEST,
    FRAME_METADATA_JSONL,
    RAW_ROBOT_EE_POSES,
)

from posetestbot.pipeline.capture_plan import build_capture_plan, write_capture_plan

from posetestbot.pipeline.capture_execution import (
    CaptureExecutionPermissionError,
    build_capture_execution_plan,
    run_capture_execution,
)

from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_config_from_token,
    write_run_config,
)
from posetestbot.sensors.contracts import CameraIntrinsics
from posetestbot.sensors.frame_writer import write_camera_sidecars


class FakeBackgroundProcess:
    def __init__(self, command: list[str], log_file):
        self.command = command
        self.returncode = None
        self.log_file = log_file
        self.pid = 12345
        self.log_file.write("fake background started\n")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        self.log_file.write("fake background finished\n")
        return 0


class FakePersistentProcess:
    def __init__(self, command: list[str], log_file):
        self.command = command
        self.returncode = None
        self.log_file = log_file
        self.pid = 23456
        self.log_file.write("fake persistent background started\n")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(self.command, timeout)


class FakeSignalProcess(FakePersistentProcess):
    def wait(self, timeout=None):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler should interrupt receiver wait")


class FakeCameraExitWhileReceiverRuns(FakePersistentProcess):
    def __init__(self, command: list[str], log_file, state: dict, returncode: int):
        super().__init__(command, log_file)
        self.state = state
        self.exit_returncode = returncode

    def poll(self):
        if self.state.get("receiver_started"):
            self.returncode = self.exit_returncode
        return self.returncode


class FakeCameraExitAfterReceiver(FakePersistentProcess):
    def __init__(self, command: list[str], log_file, returncode: int):
        super().__init__(command, log_file)
        self.exit_returncode = returncode

    def wait(self, timeout=None):
        self.returncode = self.exit_returncode
        return self.returncode


def fake_sensor_status() -> dict:
    return {
        "schema_version": "sensor_status.v1",
        "families": [
            {
                "sensor_type": "realsense_d435",
                "sdk_available": True,
                "devices": [
                    {
                        "device_id": "123",
                        "display_name": "RealSense 123",
                        "connected": True,
                    }
                ],
                "error": None,
            }
        ],
        "overall_status": "ok",
        "checks": [],
    }


def filesystem_snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = (
            "dir" if path.is_dir() else "file",
            None if path.is_dir() else path.read_bytes(),
        )
    return snapshot


def configured_run(tmp_path: Path, name: str = "run") -> tuple[Path, dict]:
    run_root = tmp_path / name
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)
    return run_root, config.to_dict()


def mark_sensor_ready(
    run_root: Path,
    *,
    device_id: str = "123",
    record_count: int = 3,
) -> None:
    sensor_folder = run_root / f"realsense_{device_id}"
    sensor_folder.mkdir(parents=True, exist_ok=True)
    (sensor_folder / "rgb").mkdir(exist_ok=True)
    (sensor_folder / "depth").mkdir(exist_ok=True)
    if not (sensor_folder / "camera_data.json").exists():
        write_camera_sidecars(
            sensor_folder,
            CameraIntrinsics(
                cam_k=(100.0, 0.0, 4.0, 0.0, 100.0, 4.0, 0.0, 0.0, 1.0),
                width=8,
                height=8,
                distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                depth_scale_to_mm=1.0,
            ),
        )
    for index in range(record_count):
        (sensor_folder / "rgb" / f"{index}.png").write_bytes(b"rgb")
        (sensor_folder / "depth" / f"{index}.png").write_bytes(b"depth")
    records = [
        {
            "schema_version": "frame_metadata.v1",
            "sensor_type": "realsense_d435",
            "sensor_id": device_id,
            "frame_index": index,
            "frame_id": f"{index}.png",
            "rgb_path": f"rgb/{index}.png",
            "depth_path": f"depth/{index}.png",
            "sensor_timestamp_ns": index + 1,
            "host_received_timestamp_ns": index + 1,
            "host_wall_timestamp_ns": index + 1,
        }
        for index in range(record_count)
    ]
    (sensor_folder / FRAME_METADATA_JSONL).write_text(
        "".join(f"{json.dumps(record)}\n" for record in records)
    )


def test_capture_execution_plan_selects_full_capture_roles(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)

    plan = build_capture_execution_plan(
        run_root,
        allow_cameras=True,
        allow_real_robot=True,
        collect_sensors=fake_sensor_status,
    )

    assert plan["schema_version"] == "capture_execution_plan.v1"
    assert plan["status"] == "ok"
    assert plan["mode"] == "full"
    assert plan["ready_to_execute"] is True
    assert plan["preflight_status"] == "ok"
    assert plan["selected_roles"] == ["sensor_capture", "robot_pose_receiver"]
    assert [command["role"] for command in plan["selected_commands"]] == [
        "sensor_capture",
        "robot_pose_receiver",
    ]
    assert plan["skipped_commands"] == []
    assert plan["selected_resources"] == ["camera", "disk_io", "robot_command"]
    gates = {gate["name"]: gate for gate in plan["gates"]}
    assert gates["camera_permission"]["status"] == "ok"
    assert gates["capture_plan_preflight"]["status"] == "ok"


def test_capture_execution_plan_blocks_until_both_permissions_are_allowed(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)

    plan = build_capture_execution_plan(
        run_root,
        include_sensor_status=False,
    )

    assert plan["status"] == "error"
    assert plan["ready_to_execute"] is False
    assert [command["role"] for command in plan["selected_commands"]] == [
        "sensor_capture",
        "robot_pose_receiver",
    ]
    gates = {gate["name"]: gate for gate in plan["gates"]}
    assert gates["camera_permission"]["status"] == "error"
    assert gates["real_robot_permission"]["status"] == "error"


@pytest.mark.parametrize(
    ("allow_cameras", "allow_real_robot", "blocked_gate"),
    [
        (True, False, "real_robot_permission"),
    ],
)
def test_capture_execution_plan_blocks_when_either_permission_is_absent(
    tmp_path: Path,
    allow_cameras: bool,
    allow_real_robot: bool,
    blocked_gate: str,
) -> None:
    run_root = tmp_path / blocked_gate
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),
            ),
        ),
    )

    plan = build_capture_execution_plan(
        run_root,
        allow_cameras=allow_cameras,
        allow_real_robot=allow_real_robot,
        collect_sensors=fake_sensor_status,
    )

    gates = {gate["name"]: gate for gate in plan["gates"]}
    assert plan["ready_to_execute"] is False
    assert gates[blocked_gate]["status"] == "error"


@pytest.mark.parametrize(
    ("allow_cameras", "allow_real_robot"),
    [
        (1, True),
    ],
)
def test_capture_execution_rejects_nonliteral_gates_before_any_mutation(
    tmp_path: Path,
    monkeypatch,
    allow_cameras,
    allow_real_robot,
) -> None:
    run_root, _config = configured_run(tmp_path, "strict-boundary")
    manifest_path = run_root / DATASET_MANIFEST
    manifest_path.write_text('{"sentinel": true}\n')
    before = filesystem_snapshot(run_root)

    def forbidden_discovery():
        raise AssertionError("permission rejection must precede sensor discovery")

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("permission rejection must precede process startup")
        ),
    )

    with pytest.raises(CaptureExecutionPermissionError, match="fresh strict"):
        run_capture_execution(
            run_root,
            allow_cameras=allow_cameras,
            allow_real_robot=allow_real_robot,
            collect_sensors=forbidden_discovery,
        )

    assert filesystem_snapshot(run_root) == before


@pytest.mark.parametrize("blocker", ["raw_pose"])
def test_capture_execution_rejects_existing_raw_outputs_before_discovery_or_mutation(
    tmp_path: Path,
    monkeypatch,
    blocker: str,
) -> None:
    run_root, config = configured_run(tmp_path, f"existing-{blocker}")
    canonical_plan = build_capture_plan(config)
    if blocker == "raw_pose":
        (run_root / RAW_ROBOT_EE_POSES).write_text('{"preserve": true}\n')
    else:
        sensor_command = next(
            command
            for command in canonical_plan.commands
            if command.role == "sensor_capture"
        )
        assert sensor_command.output_folder is not None
        Path(sensor_command.output_folder).mkdir(parents=True)
    before = filesystem_snapshot(run_root)

    def forbidden_discovery():
        raise AssertionError("raw output rejection must precede sensor discovery")

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("raw output rejection must precede process startup")
        ),
    )

    with pytest.raises(FileExistsError, match="unused raw output paths"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=forbidden_discovery,
        )

    assert filesystem_snapshot(run_root) == before
    assert not (run_root / CAPTURE_EXECUTION_PLAN).exists()
    assert not (run_root / CAPTURE_EXECUTION_STATUS).exists()
    assert not (run_root / CAPTURE_EXECUTION_REPORT).exists()
    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


def test_capture_execution_validates_live_sensor_preflight_before_supervisor_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "sensor-preflight-first")
    before = filesystem_snapshot(run_root)

    def failed_discovery():
        raise RuntimeError("sensor discovery failed before acceptance")

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed preflight must not start a process")
        ),
    )

    with pytest.raises(RuntimeError, match="sensor discovery failed"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=failed_discovery,
        )

    assert filesystem_snapshot(run_root) == before
    assert not (run_root / CAPTURE_EXECUTION_PLAN).exists()
    assert not (run_root / CAPTURE_PLAN).exists()
    assert not (run_root / CAPTURE_EXECUTION_STATUS).exists()
    assert not (run_root / CAPTURE_EXECUTION_REPORT).exists()
    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()
    assert not (run_root / DATASET_MANIFEST).exists()


def test_capture_execution_rejects_stale_plan_after_camera_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "disabled-after-plan"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(
            sensor_config_from_token("realsense_d435:working:eye_in_hand:Working"),
            sensor_config_from_token("realsense_d435:offline:eye_in_hand:Offline"),
        ),
    )
    write_run_config(run_root, config)
    write_capture_plan(run_root, build_capture_plan(config.to_dict()))

    updated_sensors = (
        config.capture.sensors[0],
        replace(config.capture.sensors[1], enabled=False),
    )
    updated_config = replace(
        config,
        capture=replace(config.capture, sensors=updated_sensors),
    )
    write_run_config(run_root, updated_config)
    before = filesystem_snapshot(run_root)

    def forbidden_discovery():
        raise AssertionError("stale-plan rejection must precede sensor discovery")

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale capture plan must not start a process")
        ),
    )

    with pytest.raises(ValueError, match="exactly match the canonical commands"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=forbidden_discovery,
        )

    assert filesystem_snapshot(run_root) == before


def test_capture_execution_full_mode_stops_sensor_process_after_receiver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "run-full-execute"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)
    background_commands: list[list[str]] = []
    receiver_commands: list[list[str]] = []
    terminated_commands: list[list[str]] = []

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            receiver_commands.append(list(command))
            (run_root / RAW_ROBOT_EE_POSES).write_text(
                json.dumps(
                    {
                        "0": {
                            "host_received_timestamp_ns": 1,
                            "host_wall_timestamp_ns": 2,
                            "motion": "circ_far",
                            "source_packet": {
                                "schema_version": "robot_pose.v1",
                                "packet_kind": "pose",
                                "sequence": 0,
                                "sender_monotonic_ns": 1,
                                "sender_wall_timestamp_ms": 1,
                                "run_id": config.run_id,
                                "from_frame": "robot_flange",
                                "to_frame": "template_base",
                                "sunrise_reference_frame_path": (
                                    "/PoseTestBot/PoseTemplateBase"
                                ),
                                "sequence_delta": 0,
                                "estimated_packets_lost": 0,
                            },
                            "pose": {
                                "X": 1,
                                "Y": 2,
                                "Z": 3,
                                "A": 0,
                                "B": 0,
                                "C": 0,
                            },
                        }
                    }
                )
            )
            return FakeBackgroundProcess(list(command), kwargs["stdout"])
        background_commands.append(list(command))
        if any(item.endswith("capture_realsense_720p.py") for item in command):
            mark_sensor_ready(run_root)
            return FakePersistentProcess(list(command), kwargs["stdout"])
        return FakeBackgroundProcess(list(command), kwargs["stdout"])

    def fake_terminate(process, *, timeout_s):
        terminated_commands.append(list(process.command))
        process.returncode = -15
        process.log_file.write("fake supervisor stopped process\n")

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", lambda _: None
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_group",
        fake_terminate,
    )

    report_path, report = run_capture_execution(
        run_root,
        allow_cameras=True,
        allow_real_robot=True,
        collect_sensors=fake_sensor_status,
        timeout_s=5,
        receive_start_timeout_s=11,
        receive_idle_timeout_s=7,
    )

    assert report_path == run_root / CAPTURE_EXECUTION_REPORT
    assert report["status"] == "succeeded"
    assert report["completion"]["schema_version"] == "capture_completion.v1"
    assert report["completion"]["status"] == "ok"
    assert report["completion"]["enabled_sensor_count"] == 1
    assert report["completion"]["error_count"] == 0
    assert {check["name"] for check in report["completion"]["checks"]} == {
        "sensor:realsense_123",
        "robot_pose_stream",
        "child_processes_and_resources",
    }
    assert report["mode"] == "full"
    assert report["capture_execution_plan"]["selected_roles"] == [
        "sensor_capture",
        "robot_pose_receiver",
    ]
    processes = {process["role"]: process for process in report["processes"]}
    assert processes["sensor_capture"]["status"] == "stopped"
    assert processes["sensor_capture"]["pid"] == 23456
    assert processes["sensor_capture"]["started_at"]
    assert processes["sensor_capture"]["ended_at"]
    assert processes["sensor_capture"]["elapsed_s"] >= 0
    assert processes["sensor_capture"]["termination_reason"] == (
        "stopped_after_receiver_exit"
    )
    assert processes["robot_pose_receiver"]["termination_reason"] == (
        "receiver_completed"
    )
    assert "--allow-cameras" in processes["robot_pose_receiver"]["command"]
    assert "--allow-real-robot" in processes["robot_pose_receiver"]["command"]
    assert terminated_commands
    assert any(
        any(item.endswith("capture_realsense_720p.py") for item in command)
        for command in terminated_commands
    )
    assert any(
        any(item.endswith("capture_realsense_720p.py") for item in command)
        for command in background_commands
    )
    assert receiver_commands[0][:4] == [
        "uv",
        "run",
        "python",
        "scripts/pose_receiver_udp_json.py",
    ]
    assert "--allow-cameras" in receiver_commands[0]
    assert "--allow-real-robot" in receiver_commands[0]
    assert (
        receiver_commands[0][
            receiver_commands[0].index("--receive-start-timeout-s") + 1
        ]
        == "11"
    )
    assert (
        receiver_commands[0][receiver_commands[0].index("--receive-idle-timeout-s") + 1]
        == "7"
    )
    assert report["receive_start_timeout_s"] == 11
    assert report["receive_idle_timeout_s"] == 7
    persisted_status = json.loads((run_root / CAPTURE_EXECUTION_STATUS).read_text())
    assert persisted_status["receive_idle_timeout_s"] == 7
    planned_receiver = next(
        command
        for command in report["capture_execution_plan"]["selected_commands"]
        if command["role"] == "robot_pose_receiver"
    )
    assert "--allow-cameras" not in planned_receiver["command"]
    assert "--allow-real-robot" not in planned_receiver["command"]
    persisted_plan = json.loads((run_root / CAPTURE_PLAN).read_text())
    persisted_receiver = next(
        command
        for command in persisted_plan["commands"]
        if command["role"] == "robot_pose_receiver"
    )
    assert "--allow-cameras" not in persisted_receiver["command"]
    assert "--allow-real-robot" not in persisted_receiver["command"]


def test_capture_execution_does_not_retry_after_partial_sensor_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-partial-no-retry")
    camera_spawn_count = 0
    receiver_commands: list[list[str]] = []

    def fake_popen(command, **kwargs):
        nonlocal camera_spawn_count
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            receiver_commands.append(list(command))
            raise AssertionError("receiver must not start after partial camera output")
        camera_spawn_count += 1
        mark_sensor_ready(run_root, record_count=1)
        process = FakePersistentProcess(list(command), kwargs["stdout"])
        process.returncode = 9
        return process

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", lambda _: None
    )

    with pytest.raises(RuntimeError, match="preserving partial raw evidence"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            camera_startup_retry_delay_s=0,
        )

    assert camera_spawn_count == 1
    assert receiver_commands == []
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    camera_processes = [
        process
        for process in report["processes"]
        if process["role"] == "sensor_capture"
    ]
    assert len(camera_processes) == 1
    assert camera_processes[0]["startup_attempt"] == 1
    assert camera_processes[0]["readiness_record_count"] == 1
    assert camera_processes[0]["output_mutated"] is True
    assert camera_processes[0]["termination_reason"] == (
        "startup_partial_output_no_retry"
    )
    assert (run_root / "realsense_123" / FRAME_METADATA_JSONL).is_file()


def test_capture_execution_never_starts_receiver_without_first_frame_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-not-ready")
    receiver_commands: list[list[str]] = []
    camera_processes: list[FakePersistentProcess] = []

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            receiver_commands.append(list(command))
            raise AssertionError("receiver must not start before camera readiness")
        process = FakePersistentProcess(list(command), kwargs["stdout"])
        camera_processes.append(process)
        return process

    def fake_terminate(process, *, timeout_s):
        del timeout_s
        process.returncode = -15

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_group",
        fake_terminate,
    )

    with pytest.raises(
        RuntimeError, match="readiness deadline expired before robot START"
    ):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=0,
            camera_startup_attempts=1,
        )

    assert receiver_commands == []
    assert len(camera_processes) == 1
    assert camera_processes[0].returncode == -15
    assert not (run_root / RAW_ROBOT_EE_POSES).exists()
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "failed"
    assert FRAME_METADATA_JSONL in report["message"]


def test_capture_execution_fails_on_nonzero_camera_exit_after_receiver(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-fails-after-receiver")

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            (run_root / RAW_ROBOT_EE_POSES).write_text(
                json.dumps({"0": {"motion": "circ_far", "pose": {"X": 1}}})
            )
            return FakeBackgroundProcess(list(command), kwargs["stdout"])
        mark_sensor_ready(run_root)
        return FakeCameraExitAfterReceiver(list(command), kwargs["stdout"], 9)

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", lambda _: None
    )

    with pytest.raises(RuntimeError, match="failure after receiver completion"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            timeout_s=5,
        )

    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    camera = next(
        process
        for process in report["processes"]
        if process["role"] == "sensor_capture"
    )
    assert report["status"] == "failed"
    assert camera["returncode"] == 9
    assert camera["status"] == "failed"


def test_capture_execution_sigterm_cancels_every_spawned_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "run-canceled"
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),
            ),
        ),
    )
    spawned: list[FakePersistentProcess] = []
    terminated: list[FakePersistentProcess] = []

    def fake_popen(command, **kwargs):
        process: FakePersistentProcess
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            process = FakeSignalProcess(list(command), kwargs["stdout"])
        else:
            mark_sensor_ready(run_root)
            process = FakePersistentProcess(list(command), kwargs["stdout"])
        spawned.append(process)
        return process

    def fake_terminate(process, *, timeout_s):
        del timeout_s
        process.returncode = -signal.SIGTERM
        terminated.append(process)

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", lambda _: None
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_group",
        fake_terminate,
    )

    with pytest.raises(RuntimeError, match="canceled by SIGTERM"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            timeout_s=5,
        )

    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "canceled"
    assert "SIGTERM" in report["message"]
    assert len(spawned) == 2
    assert terminated == spawned
    assert all(process["status"] == "terminated" for process in report["processes"])
    persisted = json.loads((run_root / CAPTURE_EXECUTION_STATUS).read_text())
    assert persisted["status"] == "canceled"
    assert persisted["active_process_count"] == 0
