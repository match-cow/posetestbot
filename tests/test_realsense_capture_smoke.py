from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from posetestbot.io.artifacts import (
    DATASET_MANIFEST,
    FRAME_METADATA_JSONL,
    REALSENSE_CAPTURE_SMOKE_REPORT,
)
from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_config_from_token,
    write_run_config,
)
from posetestbot.sensors.contracts import CameraIntrinsics, SensorDeviceInfo, SensorType
from posetestbot.sensors.frame_writer import (
    write_camera_sidecars,
    write_rgbd_frame,
)
from posetestbot.sensors.realsense_smoke import (
    build_realsense_capture_smoke_report,
    write_realsense_capture_smoke_with_manifest,
)
from posetestbot.sensors.realsense import (
    _frame_stem_from_sensor_timestamp_ns,
    _frame_timestamp_ns,
    _realsense_timestamp_metadata,
    _timestamp_domain_name,
)


SERIALS = ("825412070181", "033422071805", "923322072633")


class FakeTimestampDomain:
    def __init__(self, name: str):
        self.name = name


class FakeFrame:
    def __init__(
        self,
        *,
        timestamp_ms: float,
        timestamp_domain: str,
        metadata: dict[str, int],
    ):
        self._timestamp_ms = timestamp_ms
        self._timestamp_domain = FakeTimestampDomain(timestamp_domain)
        self._metadata = metadata

    def get_timestamp(self) -> float:
        return self._timestamp_ms

    def get_frame_timestamp_domain(self) -> FakeTimestampDomain:
        return self._timestamp_domain

    def supports_frame_metadata(self, metadata_key: str) -> bool:
        return metadata_key in self._metadata

    def get_frame_metadata(self, metadata_key: str) -> int:
        return self._metadata[metadata_key]


class FakeFrameMetadataValue:
    backend_timestamp = "backend_timestamp"
    frame_timestamp = "frame_timestamp"
    sensor_timestamp = "sensor_timestamp"
    time_of_arrival = "time_of_arrival"


class FakeRs:
    frame_metadata_value = FakeFrameMetadataValue


def realsense_device(serial: str) -> SensorDeviceInfo:
    return SensorDeviceInfo(
        sensor_type=SensorType.REALSENSE_D435,
        device_id=serial,
        display_name=f"RealSense {serial}",
        metadata={"product_line": "D400"},
    )


def test_realsense_timestamp_helpers_record_sensor_clock_details() -> None:
    color_frame = FakeFrame(
        timestamp_ms=1_701_234_567_890.123,
        timestamp_domain="global_time",
        metadata={
            "backend_timestamp": 11,
            "frame_timestamp": 12,
            "sensor_timestamp": 13,
            "time_of_arrival": 14,
        },
    )
    depth_frame = FakeFrame(
        timestamp_ms=1_701_234_567_891.456,
        timestamp_domain="hardware_clock",
        metadata={
            "frame_timestamp": 22,
            "sensor_timestamp": 23,
        },
    )

    assert _frame_timestamp_ns(color_frame) == 1_701_234_567_890_123_008
    assert (
        _frame_stem_from_sensor_timestamp_ns(_frame_timestamp_ns(color_frame))
        == "1701234567890"
    )
    assert _timestamp_domain_name(depth_frame) == "hardware_clock"

    metadata = _realsense_timestamp_metadata(color_frame, depth_frame, FakeRs)

    assert metadata["color_timestamp_domain"] == "global_time"
    assert metadata["depth_timestamp_domain"] == "hardware_clock"
    assert metadata["color_backend_timestamp"] == 11
    assert metadata["color_time_of_arrival"] == 14
    assert metadata["depth_frame_timestamp"] == 22
    assert metadata["depth_sensor_timestamp"] == 23


def realsense_only_config(run_root: Path):
    return create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=tuple(
            sensor_config_from_token(
                f"realsense_d435:{serial}:static:RealSense {serial}"
            )
            for serial in SERIALS
        ),
    )


def fake_capture(
    output_path,
    *,
    device_id,
    max_frames,
    fps,
    warmup_frames,
    preview,
    record,
    inverted=False,
):
    intrinsics = CameraIntrinsics(
        cam_k=(100.0, 0.0, 2.0, 0.0, 101.0, 2.0, 0.0, 0.0, 1.0),
        width=4,
        height=3,
        depth_scale_to_mm=1.0,
    )
    write_camera_sidecars(output_path, intrinsics)
    first_frame_id = None
    last_frame_id = None
    for index in range(max_frames):
        metadata = write_rgbd_frame(
            output_path,
            rgb_image=np.zeros((3, 4, 3), dtype=np.uint8),
            depth_image=np.ones((3, 4), dtype=np.uint16) * index,
            sensor_type=SensorType.REALSENSE_D435,
            sensor_id=device_id,
            frame_index=index,
            sensor_timestamp_ns=1_000 + index,
            host_received_timestamp_ns=2_000 + index,
            host_wall_timestamp_ns=1_700_000_000_000_000_000 + index * 1_000_000,
            extra_metadata={
                "inverted": bool(inverted),
                "image_rotation_degrees": 180 if inverted else 0,
            },
        )
        first_frame_id = first_frame_id or metadata["frame_id"]
        last_frame_id = metadata["frame_id"]
    return {
        "schema_version": "realsense_capture_summary.v1",
        "status": "succeeded",
        "sensor_id": device_id,
        "frame_count": max_frames,
        "fps": fps,
        "warmup_frames": warmup_frames,
        "preview": preview,
        "record": record,
        "inverted": bool(inverted),
        "image_rotation_degrees": 180 if inverted else 0,
        "first_frame_id": first_frame_id,
        "last_frame_id": last_frame_id,
    }


def test_realsense_capture_smoke_succeeds_with_three_mocked_devices(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    write_run_config(run_root, realsense_only_config(run_root))

    path, report = write_realsense_capture_smoke_with_manifest(
        run_root,
        max_frames=2,
        warmup_frames=1,
        discoverer=lambda: [realsense_device(serial) for serial in SERIALS],
        capture_func=fake_capture,
    )

    assert path == run_root / REALSENSE_CAPTURE_SMOKE_REPORT
    assert report["schema_version"] == "realsense_capture_smoke.v1"
    assert report["status"] == "succeeded"
    assert len(report["captures"]) == 3
    assert [capture["status"] for capture in report["captures"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    for serial in SERIALS:
        metadata = run_root / f"realsense_{serial}" / FRAME_METADATA_JSONL
        assert metadata.is_file()
        assert len(metadata.read_text().splitlines()) == 2

    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage
        for stage in manifest["stages"]
        if stage["name"] == "realsense_capture_smoke"
    )
    assert stage["status"] == "succeeded"
    assert stage["artifacts"][REALSENSE_CAPTURE_SMOKE_REPORT] == (
        REALSENSE_CAPTURE_SMOKE_REPORT
    )
    assert [sensor["status"] for sensor in manifest["sensors"]] == [
        "captured",
        "captured",
        "captured",
    ]
    assert manifest["sensors"][0]["metadata"]["inverted"] is False


def test_realsense_capture_smoke_passes_configured_inversion_to_capture(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-inverted"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=tuple(
            sensor_config_from_token(
                (
                    f"realsense_d435:{serial}:static:RealSense {serial}:inverted"
                    if serial == SERIALS[0]
                    else f"realsense_d435:{serial}:static:RealSense {serial}"
                )
            )
            for serial in SERIALS
        ),
    )
    write_run_config(run_root, config)
    capture_calls = []

    def recording_capture(output_path, *, device_id, **kwargs):
        capture_calls.append({"device_id": device_id, **kwargs})
        return fake_capture(output_path, device_id=device_id, **kwargs)

    path, report = write_realsense_capture_smoke_with_manifest(
        run_root,
        max_frames=1,
        discoverer=lambda: [realsense_device(serial) for serial in SERIALS],
        capture_func=recording_capture,
    )

    assert path == run_root / REALSENSE_CAPTURE_SMOKE_REPORT
    assert report["status"] == "succeeded"
    assert [call["inverted"] for call in capture_calls] == [True, False, False]
    assert report["captures"][0]["inverted"] is True
    assert report["captures"][0]["summary"]["image_rotation_degrees"] == 180
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    assert manifest["sensors"][0]["metadata"]["inverted"] is True
    assert manifest["sensors"][0]["metadata"]["image_rotation_degrees"] == 180


def test_realsense_capture_smoke_fails_for_missing_visible_serial(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-missing"
    write_run_config(run_root, realsense_only_config(run_root))
    capture_calls = []

    report = build_realsense_capture_smoke_report(
        run_root,
        discoverer=lambda: [realsense_device(serial) for serial in SERIALS[:2]],
        capture_func=lambda *args, **kwargs: capture_calls.append((args, kwargs)),
    )

    assert report["status"] == "failed"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks[f"visible_realsense:{SERIALS[2]}"]["status"] == "error"
    assert capture_calls == []


def test_realsense_capture_smoke_refuses_nonempty_output_folder(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-nonempty"
    write_run_config(run_root, realsense_only_config(run_root))
    sensor_folder = run_root / f"realsense_{SERIALS[0]}"
    sensor_folder.mkdir(parents=True)
    (sensor_folder / "old.txt").write_text("do not mix captures")

    report = build_realsense_capture_smoke_report(
        run_root,
        discoverer=lambda: [realsense_device(serial) for serial in SERIALS],
        capture_func=fake_capture,
    )

    checks = {check["name"]: check for check in report["checks"]}
    assert report["status"] == "failed"
    assert checks[f"output_folder:realsense_{SERIALS[0]}"]["status"] == "error"
    assert report["captures"] == []


def test_realsense_capture_smoke_is_independent_of_robot_profile(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-real-robot"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=tuple(
            sensor_config_from_token(
                f"realsense_d435:{serial}:static:RealSense {serial}"
            )
            for serial in SERIALS
        ),
    )
    write_run_config(run_root, config)

    report = build_realsense_capture_smoke_report(
        run_root,
        discoverer=lambda: [realsense_device(serial) for serial in SERIALS],
        capture_func=fake_capture,
    )

    assert report["status"] == "succeeded"
    assert "robot_profile_scope" not in {check["name"] for check in report["checks"]}
    assert len(report["captures"]) == 3


def test_realsense_capture_smoke_records_one_camera_capture_failure(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-capture-failure"
    write_run_config(run_root, realsense_only_config(run_root))

    def failing_capture(output_path, *, device_id, **kwargs):
        if device_id == SERIALS[1]:
            raise RuntimeError("camera busy")
        return fake_capture(output_path, device_id=device_id, **kwargs)

    path, report = write_realsense_capture_smoke_with_manifest(
        run_root,
        max_frames=1,
        discoverer=lambda: [realsense_device(serial) for serial in SERIALS],
        capture_func=failing_capture,
    )

    assert path == run_root / REALSENSE_CAPTURE_SMOKE_REPORT
    assert report["status"] == "failed"
    assert [capture["status"] for capture in report["captures"]] == [
        "succeeded",
        "failed",
    ]
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage
        for stage in manifest["stages"]
        if stage["name"] == "realsense_capture_smoke"
    )
    assert stage["status"] == "failed"


def test_realsense_capture_smoke_cli_writes_failed_report_for_wrong_scope(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-cli"
    repo_root = Path(__file__).resolve().parents[1]
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("oak_d_pro:auto:static:Cell OAK-D Pro"),),
    )
    write_run_config(run_root, config)

    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/run_realsense_capture_smoke.py",
            run_root.as_posix(),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"Wrote {run_root / REALSENSE_CAPTURE_SMOKE_REPORT}" in result.stdout
    report = json.loads((run_root / REALSENSE_CAPTURE_SMOKE_REPORT).read_text())
    assert report["status"] == "failed"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["realsense_only_config"]["status"] == "error"
