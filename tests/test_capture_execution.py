from __future__ import annotations

import json

import os

import signal

import subprocess

import sys

import time

from dataclasses import replace

from pathlib import Path

import pytest

from PIL import Image

import posetestbot.pipeline.capture_execution as capture_execution_module
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

from posetestbot.pipeline.capture_completion import _sensor_check

from posetestbot.pipeline.capture_execution import (
    CaptureExecutionPermissionError,
    _process_identity_is_live,
    _process_start_time,
    _snapshot_process_tree,
    _terminate_process_tree,
    build_capture_execution_plan,
    run_capture_execution,
)

from posetestbot.pipeline.run_config import (
    create_run_config,
    run_config_sha256,
    sensor_config_from_token,
    write_run_config,
)
from posetestbot.sensors.contracts import CameraIntrinsics
from posetestbot.sensors.frame_writer import write_camera_sidecars


CAPTURE_IMAGE_SIZE = (1280, 720)


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
    def __init__(self, command: list[str], log_file, readiness_advance=None):
        self.command = command
        self.returncode = None
        self.log_file = log_file
        self.pid = 23456
        self.readiness_advance = readiness_advance
        self.poll_count = 0
        self.log_file.write("fake persistent background started\n")

    def poll(self):
        self.poll_count += 1
        if self.poll_count >= 3 and self.readiness_advance is not None:
            readiness_advance = self.readiness_advance
            self.readiness_advance = None
            readiness_advance()
        return self.returncode

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(self.command, timeout)


class FakeSignalProcess(FakePersistentProcess):
    def wait(self, timeout=None):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler should interrupt receiver wait")


class FakeCameraExitWhileReceiverRuns(FakePersistentProcess):
    def __init__(
        self,
        command: list[str],
        log_file,
        state: dict,
        returncode: int,
        readiness_advance=None,
    ):
        super().__init__(command, log_file, readiness_advance)
        self.state = state
        self.exit_returncode = returncode

    def poll(self):
        super().poll()
        if self.state.get("receiver_started"):
            self.returncode = self.exit_returncode
        return self.returncode


class FakeCameraExitAfterReceiver(FakePersistentProcess):
    def __init__(
        self,
        command: list[str],
        log_file,
        returncode: int,
        readiness_advance=None,
    ):
        super().__init__(command, log_file, readiness_advance)
        self.exit_returncode = returncode

    def wait(self, timeout=None):
        self.returncode = self.exit_returncode
        return self.returncode


class FakeMotionEndReceiver(FakeBackgroundProcess):
    def __init__(self, command: list[str], log_file, state: dict):
        super().__init__(command, log_file)
        self.state = state

    def wait(self, timeout=None):
        self.state["motion_end"] = True
        return super().wait(timeout=timeout)


def fake_sensor_status() -> dict:
    return {
        "schema_version": "sensor_status.v1",
        "families": [
            {
                "sensor_type": "realsense_d435",
                "sdk_available": True,
                "devices": [
                    {
                        "sensor_type": "realsense_d435",
                        "device_id": "123",
                        "display_name": "RealSense 123",
                        "connected": True,
                        "capture_ready": True,
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
                width=CAPTURE_IMAGE_SIZE[0],
                height=CAPTURE_IMAGE_SIZE[1],
                distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                depth_scale_to_mm=1.0,
            ),
        )
    for index in range(record_count):
        Image.new("RGB", CAPTURE_IMAGE_SIZE, color=(index, 0, 0)).save(
            sensor_folder / "rgb" / f"{index}.png"
        )
        Image.new("I;16", CAPTURE_IMAGE_SIZE, color=index + 1).save(
            sensor_folder / "depth" / f"{index}.png"
        )
    monotonic_base_ns = time.monotonic_ns() - record_count
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
            "host_received_timestamp_ns": monotonic_base_ns + index,
            "host_wall_timestamp_ns": time.time_ns(),
        }
        for index in range(record_count)
    ]
    (sensor_folder / FRAME_METADATA_JSONL).write_text(
        "".join(f"{json.dumps(record)}\n" for record in records)
    )


def test_capture_completion_decodes_every_rgb_depth_png(tmp_path: Path) -> None:
    run_root, config = configured_run(tmp_path, "png-validation")
    mark_sensor_ready(run_root)
    sensor = config["capture"]["sensors"][0]

    valid = _sensor_check(
        run_root,
        sensor,
        configured_resolution="720p",
        expected_image_size=CAPTURE_IMAGE_SIZE,
    )
    (run_root / "realsense_123" / "depth" / "1.png").write_bytes(b"not a png")
    corrupt = _sensor_check(
        run_root,
        sensor,
        configured_resolution="720p",
        expected_image_size=CAPTURE_IMAGE_SIZE,
    )

    assert valid["status"] == "ok"
    assert valid["details"]["validated_image_pair_count"] == 3
    assert valid["details"]["image_dimensions"] == list(CAPTURE_IMAGE_SIZE)
    assert corrupt["status"] == "error"
    assert corrupt["details"]["balanced"] is True
    assert "PNG decode failed" in corrupt["details"]["image_validation_error"]


@pytest.mark.parametrize(
    "frame_indices",
    (
        [0, 1, 3, 4],
        [1, 0, 2, 3],
    ),
)
def test_capture_completion_requires_ordered_contiguous_frame_indices(
    tmp_path: Path,
    frame_indices: list[int],
) -> None:
    run_root, config = configured_run(tmp_path, "frame-index-integrity")
    mark_sensor_ready(run_root, record_count=4)
    metadata_path = run_root / "realsense_123" / FRAME_METADATA_JSONL
    records = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    for record, frame_index in zip(records, frame_indices, strict=True):
        record["frame_index"] = frame_index
    metadata_path.write_text("".join(f"{json.dumps(record)}\n" for record in records))

    result = _sensor_check(
        run_root,
        config["capture"]["sensors"][0],
        configured_resolution="720p",
        expected_image_size=CAPTURE_IMAGE_SIZE,
    )

    assert result["status"] == "error"
    assert "ordered contiguous capture evidence" in result["details"]["metadata_error"]


@pytest.mark.parametrize(
    "timestamp_field",
    ("host_received_timestamp_ns", "host_wall_timestamp_ns"),
)
def test_capture_completion_requires_strictly_increasing_host_timestamps(
    tmp_path: Path,
    timestamp_field: str,
) -> None:
    run_root, config = configured_run(tmp_path, f"frame-{timestamp_field}")
    mark_sensor_ready(run_root, record_count=3)
    metadata_path = run_root / "realsense_123" / FRAME_METADATA_JSONL
    records = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    records[1][timestamp_field] = records[0][timestamp_field]
    metadata_path.write_text("".join(f"{json.dumps(record)}\n" for record in records))

    result = _sensor_check(
        run_root,
        config["capture"]["sensors"][0],
        configured_resolution="720p",
        expected_image_size=CAPTURE_IMAGE_SIZE,
    )

    assert result["status"] == "error"
    assert (
        f"{timestamp_field} must strictly increase in capture order"
        in result["details"]["metadata_error"]
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

    assert plan["schema_version"] == "capture_execution_plan.v2"
    assert plan["run_config_sha256"] == run_config_sha256(config.to_dict())
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


def test_capture_execution_rejects_a_concurrent_supervisor_without_mutation(
    tmp_path: Path,
) -> None:
    run_root, _config = configured_run(tmp_path, "concurrent-supervisor")
    before = filesystem_snapshot(run_root)

    with capture_execution_module._exclusive_capture_execution(run_root):
        with pytest.raises(RuntimeError, match="supervisor is already active"):
            run_capture_execution(
                run_root,
                allow_cameras=True,
                allow_real_robot=True,
                collect_sensors=lambda: (_ for _ in ()).throw(
                    AssertionError("concurrency rejection must precede discovery")
                ),
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


def test_capture_execution_rejects_auto_device_before_discovery_or_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "auto-device"
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                sensor_config_from_token("oak_d_pro:auto:eye_in_hand:Cell OAK-D"),
            ),
        ),
    )
    before = filesystem_snapshot(run_root)

    def forbidden_discovery():
        raise AssertionError("auto identity rejection must precede sensor discovery")

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("auto identity rejection must precede process startup")
        ),
    )

    with pytest.raises(ValueError, match="concrete device_id.*replace auto"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=forbidden_discovery,
        )

    assert filesystem_snapshot(run_root) == before
    assert not (run_root / CAPTURE_PLAN).exists()
    assert not (run_root / CAPTURE_EXECUTION_PLAN).exists()
    assert not (run_root / CAPTURE_EXECUTION_STATUS).exists()
    assert not (run_root / CAPTURE_EXECUTION_REPORT).exists()
    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


def test_capture_execution_rejects_relative_outputs_outside_child_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    run_root = Path("relative-run")
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)

    with pytest.raises(ValueError, match="Sensor output folder escapes the run root"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=lambda: (_ for _ in ()).throw(
                AssertionError("path rejection must precede discovery")
            ),
        )

    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


def test_capture_execution_rejects_symlinked_sensor_output_before_discovery(
    tmp_path: Path,
) -> None:
    run_root, _config = configured_run(tmp_path, "symlinked-sensor-output")
    output_path = run_root / "realsense_123"
    output_path.symlink_to(tmp_path / "outside-sensor-output", target_is_directory=True)

    with pytest.raises(ValueError, match="Sensor output folder escapes the run root"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=lambda: (_ for _ in ()).throw(
                AssertionError("path rejection must precede discovery")
            ),
        )

    assert output_path.is_symlink()
    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


def test_capture_execution_rechecks_sensor_path_after_discovery_before_artifacts(
    tmp_path: Path,
) -> None:
    run_root, _config = configured_run(tmp_path, "sensor-path-changed-by-discovery")
    output_path = run_root / "realsense_123"

    def discovery_then_path_escape():
        output_path.symlink_to(
            tmp_path / "outside-discovery-output",
            target_is_directory=True,
        )
        return fake_sensor_status()

    with pytest.raises(ValueError, match="Sensor output folder escapes the run root"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=discovery_then_path_escape,
        )

    assert output_path.is_symlink()
    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()
    assert not (run_root / CAPTURE_EXECUTION_PLAN).exists()
    assert not (run_root / CAPTURE_EXECUTION_STATUS).exists()
    assert not (run_root / CAPTURE_EXECUTION_REPORT).exists()


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


def test_capture_execution_rejects_reordered_persisted_commands_before_discovery(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "reordered-plan"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(
            sensor_config_from_token("realsense_d435:123:static:First"),
            sensor_config_from_token("realsense_d435:456:static:Second"),
        ),
    )
    write_run_config(run_root, config)
    persisted = build_capture_plan(config.to_dict()).to_dict()
    persisted["commands"][0], persisted["commands"][1] = (
        persisted["commands"][1],
        persisted["commands"][0],
    )
    (run_root / CAPTURE_PLAN).write_text(json.dumps(persisted))
    before = filesystem_snapshot(run_root)

    with pytest.raises(ValueError, match="exactly match the canonical commands"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=lambda: (_ for _ in ()).throw(
                AssertionError("stale order rejection must precede discovery")
            ),
        )

    assert filesystem_snapshot(run_root) == before


def test_capture_execution_rejects_symlinked_persisted_plan_before_discovery(
    tmp_path: Path,
) -> None:
    run_root, config = configured_run(tmp_path, "symlinked-plan")
    external_plan = tmp_path / "external-capture-plan.json"
    external_plan.write_text(json.dumps(build_capture_plan(config).to_dict()))
    (run_root / CAPTURE_PLAN).symlink_to(external_plan)

    with pytest.raises(ValueError, match="not a regular file"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=lambda: (_ for _ in ()).throw(
                AssertionError("symlink rejection must precede discovery")
            ),
        )

    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


def test_capture_execution_status_reader_rejects_symlink(tmp_path: Path) -> None:
    run_root, _config = configured_run(tmp_path, "symlinked-status")
    external_status = tmp_path / "external-capture-status.json"
    external_status.write_text(
        json.dumps({"schema_version": "capture_execution_status.v2"})
    )
    (run_root / CAPTURE_EXECUTION_STATUS).symlink_to(external_status)

    with pytest.raises(ValueError, match="must be a regular file"):
        capture_execution_module.load_capture_execution_status(run_root)


def test_capture_execution_recovers_pose_journal_before_unused_output_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, config = configured_run(tmp_path, "recovered-pose-journal")
    recoveries: list[tuple[Path, str]] = []

    def fake_recover(root, *, expected_run_id):
        recoveries.append((Path(root), expected_run_id))
        (Path(root) / RAW_ROBOT_EE_POSES).write_text(json.dumps({"0": {}}))
        return []

    monkeypatch.setattr(
        capture_execution_module,
        "recover_pose_journals",
        fake_recover,
    )
    monkeypatch.setattr(
        capture_execution_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovered raw evidence must block every child")
        ),
    )

    with pytest.raises(FileExistsError, match="unused raw output paths"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
        )

    assert recoveries == [(run_root, config["run_id"])]
    assert (run_root / RAW_ROBOT_EE_POSES).is_file()
    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


def test_capture_execution_blocks_recovered_partial_pose_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "recovered-partial-journal")

    def fake_recover(root, *, expected_run_id):
        del expected_run_id
        recovered = (
            Path(root)
            / f"raw_robot_ee_poses.journal.{'1' * 32}.recovered.jsonl"
        )
        recovered.write_text('{"durable_pose_prefix": true}\n')
        (Path(root) / "raw_robot_ee_poses.partial.1.test.json").write_text(
            '{"received_pose_count": 1}\n'
        )
        return [{"status": "partial", "journal": recovered}]

    monkeypatch.setattr(
        capture_execution_module,
        "recover_pose_journals",
        fake_recover,
    )

    with pytest.raises(FileExistsError, match="recovered.jsonl"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=lambda: (_ for _ in ()).throw(
                AssertionError("partial recovery must block discovery")
            ),
        )

    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()
    assert list(run_root.glob("raw_robot_ee_poses.partial.*.json"))


def test_capture_execution_blocks_retained_claim_only_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "recovered-claim-only")

    def fake_recover(root, *, expected_run_id):
        del expected_run_id
        recovered = (
            Path(root)
            / f"raw_robot_ee_poses.claim.{'1' * 32}.recovered.json"
        )
        recovered.write_text('{"status": "reserved"}\n')
        return [{"status": "claim_recovered", "claim": recovered}]

    monkeypatch.setattr(
        capture_execution_module,
        "recover_pose_journals",
        fake_recover,
    )

    with pytest.raises(FileExistsError, match="claim.*recovered"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=lambda: (_ for _ in ()).throw(
                AssertionError("claim recovery must block discovery")
            ),
        )

    assert not (run_root / CAPTURE_EXECUTION_LOGS_DIR).exists()


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
    child_session_flags: list[bool] = []
    config_checkpoints: list[str] = []
    original_config_check = capture_execution_module._assert_run_config_digest

    def recording_config_check(run_root_value, expected_sha256, *, phase):
        config_checkpoints.append(phase)
        return original_config_check(
            run_root_value,
            expected_sha256,
            phase=phase,
        )

    def fake_popen(command, **kwargs):
        child_session_flags.append(kwargs["start_new_session"])
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
                            "stream_end_source_packet": {
                                "schema_version": "robot_pose.v1",
                                "packet_kind": "end",
                                "sequence": 1,
                                "sender_monotonic_ns": 2,
                                "sender_wall_timestamp_ms": 2,
                                "run_id": config.run_id,
                                "from_frame": "robot_flange",
                                "to_frame": "template_base",
                                "sunrise_reference_frame_path": (
                                    "/PoseTestBot/PoseTemplateBase"
                                ),
                                "sequence_delta": 1,
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
            return FakePersistentProcess(
                list(command),
                kwargs["stdout"],
                lambda: mark_sensor_ready(run_root, record_count=4),
            )
        return FakeBackgroundProcess(list(command), kwargs["stdout"])

    def fake_terminate(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            terminated_commands.append(list(process.command))
            process.returncode = -15
            process.log_file.write("fake supervisor stopped process\n")
        return set()

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", lambda _: None
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_trees",
        fake_terminate,
    )
    monkeypatch.setattr(
        capture_execution_module,
        "_assert_run_config_digest",
        recording_config_check,
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
    assert report["schema_version"] == "capture_execution_report.v2"
    assert report["status"] == "succeeded"
    assert report["run_config_sha256"] == run_config_sha256(config.to_dict())
    assert config_checkpoints == [
        "before execution evidence publication",
        "immediately before camera child 1 startup attempt 1",
        "immediately before robot receiver START",
        "after robot receiver completion",
        "before capture completion validation",
        "after capture completion validation",
    ]
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
    assert child_session_flags == [False, False]
    archive_root = run_root / report["execution_archive"]
    assert archive_root.parent == run_root / CAPTURE_EXECUTION_LOGS_DIR
    assert (
        json.loads((archive_root / CAPTURE_EXECUTION_PLAN).read_text())
        == report["capture_execution_plan"]
    )
    assert json.loads((archive_root / CAPTURE_EXECUTION_REPORT).read_text()) == report
    persisted_status = json.loads((run_root / CAPTURE_EXECUTION_STATUS).read_text())
    assert persisted_status["receive_idle_timeout_s"] == 7
    assert json.loads((archive_root / CAPTURE_EXECUTION_STATUS).read_text()) == (
        persisted_status
    )
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


def test_capture_execution_rejects_run_config_change_before_receiver_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "config-changed-before-start")
    receiver_commands: list[list[str]] = []

    def mutate_config_after_readiness() -> None:
        mark_sensor_ready(run_root, record_count=4)
        value = json.loads((run_root / "run_config.json").read_text())
        value["run_name"] = "mutated after authorization"
        (run_root / "run_config.json").write_text(json.dumps(value))

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            receiver_commands.append(list(command))
            raise AssertionError("receiver must not start from a changed config")
        mark_sensor_ready(run_root, record_count=3)
        return FakePersistentProcess(
            list(command),
            kwargs["stdout"],
            mutate_config_after_readiness,
        )

    def fake_terminate(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            process.returncode = -signal.SIGTERM
        return set()

    monkeypatch.setattr(capture_execution_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        capture_execution_module, "_terminate_process_trees", fake_terminate
    )
    monkeypatch.setattr(capture_execution_module.time, "sleep", lambda _delay: None)

    with pytest.raises(
        RuntimeError, match="changed after capture authorization.*before robot receiver"
    ):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=1,
        )

    assert receiver_commands == []
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "failed"
    assert (run_root / "realsense_123" / FRAME_METADATA_JSONL).is_file()
    assert (
        run_config_sha256(json.loads((run_root / "run_config.json").read_text()))
        != report["run_config_sha256"]
    )


def test_capture_execution_rejects_run_config_change_during_receiver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "config-changed-during-motion")

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            value = json.loads((run_root / "run_config.json").read_text())
            value["run_name"] = "mutated during receiver"
            (run_root / "run_config.json").write_text(json.dumps(value))
            return FakeBackgroundProcess(list(command), kwargs["stdout"])
        mark_sensor_ready(run_root, record_count=3)
        return FakePersistentProcess(
            list(command),
            kwargs["stdout"],
            lambda: mark_sensor_ready(run_root, record_count=4),
        )

    def fake_terminate(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            process.returncode = -signal.SIGTERM
        return set()

    monkeypatch.setattr(capture_execution_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        capture_execution_module, "_terminate_process_trees", fake_terminate
    )
    monkeypatch.setattr(capture_execution_module.time, "sleep", lambda _delay: None)

    with pytest.raises(
        RuntimeError, match="changed after capture authorization.*receiver completion"
    ):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=1,
        )

    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "failed"
    receiver = next(
        process
        for process in report["processes"]
        if process["role"] == "robot_pose_receiver"
    )
    assert receiver["status"] == "succeeded"


def test_capture_execution_finalizes_failure_report_when_run_config_becomes_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "config-corrupted-during-motion")

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            (run_root / "run_config.json").write_text("{not-json\n")
            return FakeBackgroundProcess(list(command), kwargs["stdout"])
        mark_sensor_ready(run_root, record_count=3)
        return FakePersistentProcess(
            list(command),
            kwargs["stdout"],
            lambda: mark_sensor_ready(run_root, record_count=4),
        )

    def fake_terminate(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            process.returncode = -signal.SIGTERM
        return set()

    monkeypatch.setattr(capture_execution_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        capture_execution_module, "_terminate_process_trees", fake_terminate
    )
    monkeypatch.setattr(capture_execution_module.time, "sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="became missing, unreadable, or invalid"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=1,
        )

    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "failed"
    assert "unreadable, or invalid" in report["message"]
    archived_report = (
        run_root / report["execution_archive"] / CAPTURE_EXECUTION_REPORT
    )
    assert json.loads(archived_report.read_text()) == report


def test_capture_execution_retry_retains_prior_attempt_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "retained-retry-diagnostics")

    def fail_spawn(*_args, **_kwargs):
        raise OSError("synthetic camera spawn failure")

    monkeypatch.setattr(capture_execution_module.subprocess, "Popen", fail_spawn)

    with pytest.raises(RuntimeError, match="exhausted 1 startup attempt"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            camera_startup_attempts=1,
        )
    archive_root = run_root / CAPTURE_EXECUTION_LOGS_DIR
    first_archive = next(archive_root.iterdir())
    first_snapshot = filesystem_snapshot(first_archive)
    first_report = json.loads((first_archive / CAPTURE_EXECUTION_REPORT).read_text())

    with pytest.raises(RuntimeError, match="exhausted 1 startup attempt"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            camera_startup_attempts=1,
        )

    archives = sorted(path for path in archive_root.iterdir() if path.is_dir())
    assert len(archives) == 2
    assert filesystem_snapshot(first_archive) == first_snapshot
    current_report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert current_report["execution_id"] != first_report["execution_id"]
    assert current_report["status"] == first_report["status"] == "failed"


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


def test_capture_execution_does_not_retry_after_incomplete_startup_termination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-survivor-no-retry")
    camera_spawn_count = 0
    receiver_commands: list[list[str]] = []

    def fake_popen(command, **kwargs):
        nonlocal camera_spawn_count
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            receiver_commands.append(list(command))
            raise AssertionError("receiver must not start after a camera survivor")
        camera_spawn_count += 1
        return FakePersistentProcess(list(command), kwargs["stdout"])

    def incomplete_termination(process, *, timeout_s, expected_start_time=None):
        del process, timeout_s, expected_start_time
        return True

    def cleanup_termination(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            process.returncode = -signal.SIGKILL
        return set()

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_tree",
        incomplete_termination,
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_trees",
        cleanup_termination,
    )

    with pytest.raises(RuntimeError, match="could not be fully terminated"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=0,
            camera_startup_attempts=2,
            camera_startup_retry_delay_s=0,
        )

    assert camera_spawn_count == 1
    assert receiver_commands == []
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    camera = next(
        process
        for process in report["processes"]
        if process["role"] == "sensor_capture"
    )
    assert camera["termination_reason"] == "startup_termination_incomplete"


def test_capture_execution_rechecks_output_after_startup_retry_delay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-late-output-no-retry")
    output_path = run_root / "realsense_123"
    camera_spawn_count = 0

    def fake_popen(command, **kwargs):
        nonlocal camera_spawn_count
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            raise AssertionError("receiver must not start after late raw output")
        camera_spawn_count += 1
        return FakePersistentProcess(list(command), kwargs["stdout"])

    def complete_termination(process, *, timeout_s, expected_start_time=None):
        del timeout_s, expected_start_time
        process.returncode = -signal.SIGTERM
        return False

    def publish_after_delay(_delay_s):
        output_path.mkdir(parents=True, exist_ok=False)

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_tree",
        complete_termination,
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", publish_after_delay
    )

    with pytest.raises(RuntimeError, match="after startup termination"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=0,
            camera_startup_attempts=2,
            camera_startup_retry_delay_s=0.1,
        )

    assert camera_spawn_count == 1
    assert output_path.is_dir()
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    camera = next(
        process
        for process in report["processes"]
        if process["role"] == "sensor_capture"
    )
    assert camera["output_mutated"] is True
    assert camera["termination_reason"] == "startup_late_output_no_retry"


def test_capture_execution_rechecks_symlink_containment_before_retry_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-late-symlink-no-retry")
    output_path = run_root / "realsense_123"
    camera_spawn_count = 0

    def fail_camera_spawn(command, **_kwargs):
        nonlocal camera_spawn_count
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            raise AssertionError("receiver must not start after a path escape")
        camera_spawn_count += 1
        raise OSError("synthetic initial camera spawn failure")

    def publish_symlink_after_delay(_delay_s):
        output_path.symlink_to(
            tmp_path / "outside-late-sensor-output",
            target_is_directory=True,
        )

    monkeypatch.setattr(
        capture_execution_module.subprocess,
        "Popen",
        fail_camera_spawn,
    )
    monkeypatch.setattr(
        capture_execution_module.time,
        "sleep",
        publish_symlink_after_delay,
    )

    with pytest.raises(RuntimeError, match="Sensor output folder escapes the run root"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            camera_startup_attempts=2,
            camera_startup_retry_delay_s=0.1,
        )

    assert camera_spawn_count == 1
    assert output_path.is_symlink()
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "failed"


@pytest.mark.parametrize("stale_record", [False, True])
def test_capture_execution_requires_ready_camera_to_advance_with_recent_metadata(
    tmp_path: Path,
    monkeypatch,
    stale_record: bool,
) -> None:
    run_root, _config = configured_run(tmp_path, "camera-stalled-before-start")
    receiver_commands: list[list[str]] = []

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            receiver_commands.append(list(command))
            raise AssertionError("receiver must not start for a stalled camera")
        mark_sensor_ready(run_root, record_count=3)

        def age_all_records() -> None:
            metadata_path = run_root / "realsense_123" / FRAME_METADATA_JSONL
            records = [
                json.loads(line) for line in metadata_path.read_text().splitlines()
            ]
            for index, record in enumerate(records, start=1):
                record["host_received_timestamp_ns"] = index
            metadata_path.write_text(
                "".join(f"{json.dumps(record)}\n" for record in records)
            )

        if stale_record:
            age_all_records()

        def publish_stale_record():
            mark_sensor_ready(run_root, record_count=4)
            age_all_records()

        return FakePersistentProcess(
            list(command),
            kwargs["stdout"],
            publish_stale_record if stale_record else None,
        )

    def cleanup_termination(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            process.returncode = -signal.SIGTERM
        return set()

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_trees",
        cleanup_termination,
    )

    with pytest.raises(RuntimeError, match="did not advance with recent committed"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=fake_sensor_status,
            startup_wait_s=0,
        )

    assert receiver_commands == []
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    assert report["status"] == "failed"
    expected_detail = "not recent" if stale_record else "did not advance"
    assert expected_detail in report["message"]


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

    def fake_terminate(process, *, timeout_s, expected_start_time=None):
        del timeout_s, expected_start_time
        process.returncode = -15
        return False

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_tree",
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
        return FakeCameraExitAfterReceiver(
            list(command),
            kwargs["stdout"],
            9,
            lambda: mark_sensor_ready(run_root, record_count=4),
        )

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
            process = FakePersistentProcess(
                list(command),
                kwargs["stdout"],
                lambda: mark_sensor_ready(run_root, record_count=4),
            )
        spawned.append(process)
        return process

    def fake_terminate(processes, *, timeout_s):
        del timeout_s
        for process, _start_time in processes:
            process.returncode = -signal.SIGTERM
            terminated.append(process)
        return set()

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.time.sleep", lambda _: None
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_trees",
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


def test_camera_exit_during_motion_is_deferred_until_receiver_end(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_root = tmp_path / "camera-failure-during-motion"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(
            sensor_config_from_token("realsense_d435:123:static:Primary"),
            sensor_config_from_token("realsense_d435:456:static:Backup"),
        ),
    )
    write_run_config(run_root, config)
    state = {"receiver_started": False, "motion_end": False}
    stopped_after_motion: list[FakePersistentProcess] = []

    def sensor_status() -> dict:
        value = fake_sensor_status()
        value["families"][0]["devices"].append(
            {
                "sensor_type": "realsense_d435",
                "device_id": "456",
                "display_name": "RealSense 456",
                "connected": True,
                "capture_ready": True,
            }
        )
        return value

    def fake_popen(command, **kwargs):
        if any(item.endswith("pose_receiver_udp_json.py") for item in command):
            state["receiver_started"] = True
            (run_root / RAW_ROBOT_EE_POSES).write_text(
                json.dumps({"0": {"motion": "circ_far", "pose": {"X": 1}}})
            )
            return FakeMotionEndReceiver(list(command), kwargs["stdout"], state)

        device_id = command[command.index("--device") + 1]
        mark_sensor_ready(run_root, device_id=device_id)
        if device_id == "123":
            return FakeCameraExitWhileReceiverRuns(
                list(command),
                kwargs["stdout"],
                state,
                9,
                lambda: mark_sensor_ready(
                    run_root,
                    device_id=device_id,
                    record_count=4,
                ),
            )
        return FakePersistentProcess(
            list(command),
            kwargs["stdout"],
            lambda: mark_sensor_ready(
                run_root,
                device_id=device_id,
                record_count=4,
            ),
        )

    def fake_terminate(processes, *, timeout_s):
        del timeout_s
        assert state["motion_end"] is True
        for process, _start_time in processes:
            process.returncode = -signal.SIGTERM
            stopped_after_motion.append(process)
        return set()

    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "posetestbot.pipeline.capture_execution._terminate_process_trees",
        fake_terminate,
    )

    with pytest.raises(RuntimeError, match="continue through motion=end"):
        run_capture_execution(
            run_root,
            allow_cameras=True,
            allow_real_robot=True,
            collect_sensors=sensor_status,
            timeout_s=5,
        )

    assert state == {"receiver_started": True, "motion_end": True}
    assert len(stopped_after_motion) == 1
    assert (run_root / RAW_ROBOT_EE_POSES).is_file()
    report = json.loads((run_root / CAPTURE_EXECUTION_REPORT).read_text())
    processes = report["processes"]
    receiver = next(item for item in processes if item["role"] == "robot_pose_receiver")
    cameras = [item for item in processes if item["role"] == "sensor_capture"]
    assert receiver["status"] == "succeeded"
    assert {item["termination_reason"] for item in cameras} == {
        "camera_exited_while_receiver_active",
        "stopped_after_receiver_exit",
    }


def test_verified_process_tree_terminator_stops_stubborn_descendant(
    tmp_path: Path,
) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("verified procfs process identities are Linux-specific")

    ready_path = tmp_path / "descendant-ready.txt"
    descendant = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    worker = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        ["uv", "run", "--no-sync", "python", "-u", "-c", worker],
        cwd=Path(__file__).resolve().parents[1],
    )
    identities = {}
    try:
        deadline = time.monotonic() + 5
        while not ready_path.is_file() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"uv wrapper exited early with {process.returncode}"
                )
            time.sleep(0.02)
        assert ready_path.is_file()
        start_time = _process_start_time(process.pid)
        identities = _snapshot_process_tree(
            process.pid,
            expected_start_time=start_time,
        )
        assert len(identities) >= 3

        survived = _terminate_process_tree(
            process,
            timeout_s=1.0,
            expected_start_time=start_time,
        )

        assert survived is False
        assert process.poll() is not None
        assert not any(
            _process_identity_is_live(identity) for identity in identities.values()
        )
    finally:
        for identity in identities.values():
            if _process_identity_is_live(identity):
                os.kill(identity.pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
