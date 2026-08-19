from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

import posetestbot.sensors.frame_writer as frame_writer

from posetestbot.io.artifacts import (
    CAMERA_DATA_JSON,
    CAMERA_JSON,
    CAM_K,
    DEPTH_DIR,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
    RGB_DIR,
)
from posetestbot.sensors.contracts import AlignedRgbdFrame, CameraIntrinsics, SensorType
from posetestbot.sensors.frame_writer import (
    append_frame_metadata,
    frame_stem_from_host_wall_ns,
    sync_frame_metadata,
    write_aligned_rgbd_frame,
    write_camera_sidecars,
    write_rgbd_frame,
)


def read_metadata(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (path / FRAME_METADATA_JSONL).read_text().splitlines()
    ]


def test_write_rgbd_frame_creates_images_and_metadata(
    tmp_path: Path,
) -> None:
    rgb = np.zeros((2, 3, 3), dtype=np.uint8)
    rgb[:, :, 1] = 128
    depth = np.ones((2, 3), dtype=np.uint16) * 42

    metadata = write_rgbd_frame(
        tmp_path,
        rgb_image=rgb,
        depth_image=depth,
        sensor_type=SensorType.REALSENSE_D435,
        sensor_id="123",
        frame_index=7,
        sensor_timestamp_ns=111,
        depth_sensor_timestamp_ns=222,
        host_received_timestamp_ns=333,
        host_wall_timestamp_ns=1_701_234_567_890_123_456,
        extra_metadata={"color_frame_number": 44},
    )

    assert metadata["frame_id"] == "1701234567890.png"
    assert (tmp_path / RGB_DIR / metadata["frame_id"]).is_file()
    assert (tmp_path / DEPTH_DIR / metadata["frame_id"]).is_file()
    assert cv2.imread((tmp_path / RGB_DIR / metadata["frame_id"]).as_posix()).shape == (
        2,
        3,
        3,
    )
    assert (
        cv2.imread(
            (tmp_path / DEPTH_DIR / metadata["frame_id"]).as_posix(),
            cv2.IMREAD_UNCHANGED,
        ).dtype
        == np.uint16
    )

    records = read_metadata(tmp_path)
    assert records == [metadata]
    assert records[0]["sensor_type"] == "realsense_d435"
    assert records[0]["depth_sensor_timestamp_ns"] == 222
    assert records[0]["color_frame_number"] == 44


def test_write_aligned_rgbd_frame_uses_contract_fields(tmp_path: Path) -> None:
    frame = AlignedRgbdFrame(
        sensor_id="mxid-1",
        sensor_type=SensorType.OAK_D_PRO,
        frame_index=2,
        sensor_timestamp_ns=10,
        host_received_timestamp_ns=20,
        rgb_image=np.zeros((2, 2, 3), dtype=np.uint8),
        depth_image_aligned_to_rgb=np.zeros((2, 2), dtype=np.uint16),
        intrinsics=CameraIntrinsics(
            cam_k=(1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0),
            width=2,
            height=2,
        ),
        exposure_metadata={"exposure_us": 500},
    )

    metadata = write_aligned_rgbd_frame(
        tmp_path,
        frame,
        host_wall_timestamp_ns=123_456_789_000_000,
        frame_stem="000002",
        extra_metadata={"sequence_num": 5},
    )

    assert metadata["frame_id"] == "000002.png"
    assert metadata["sensor_type"] == "oak_d_pro"
    assert metadata["sensor_id"] == "mxid-1"
    assert metadata["sensor_timestamp_ns"] == 10
    assert metadata["host_received_timestamp_ns"] == 20
    assert metadata["exposure_us"] == 500
    assert metadata["sequence_num"] == 5
    assert read_metadata(tmp_path) == [metadata]


def test_frame_stem_from_host_wall_ns_uses_milliseconds() -> None:
    assert frame_stem_from_host_wall_ns(1_234_567_890) == "1235"


def test_frame_metadata_defers_fsync_until_capture_shutdown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fsync_calls: list[int] = []
    monkeypatch.setattr(
        frame_writer.os,
        "fsync",
        lambda file_descriptor: fsync_calls.append(file_descriptor),
    )

    metadata_path = append_frame_metadata(tmp_path, {"frame_index": 0})

    assert json.loads(metadata_path.read_text()) == {"frame_index": 0}
    assert fsync_calls == []
    assert sync_frame_metadata(tmp_path) == metadata_path
    assert len(fsync_calls) == 1


def test_sync_frame_metadata_is_a_noop_before_the_first_frame(
    tmp_path: Path,
) -> None:
    assert sync_frame_metadata(tmp_path) is None


def test_write_camera_sidecars_writes_numeric_calibration_formats(
    tmp_path: Path,
) -> None:
    intrinsics = CameraIntrinsics(
        cam_k=(100.0, 0.0, 50.0, 0.0, 101.0, 51.0, 0.0, 0.0, 1.0),
        width=1280,
        height=720,
        depth_scale_to_mm=0.25,
    )

    paths = write_camera_sidecars(tmp_path, intrinsics)

    assert paths[CAM_K] == tmp_path / CAM_K
    assert (tmp_path / CAM_K).read_text().splitlines() == [
        "100.0 0.0 50.0",
        "0.0 101.0 51.0",
        "0.0 0.0 1.0",
    ]
    assert (tmp_path / DEPTH_SCALE).read_text() == "0.25\n"
    assert json.loads((tmp_path / CAMERA_JSON).read_text()) == {
        "cam_K": [100.0, 0.0, 50.0, 0.0, 101.0, 51.0, 0.0, 0.0, 1.0],
        "depth_scale": 0.25,
    }
    assert json.loads((tmp_path / CAMERA_DATA_JSON).read_text()) == {
        "K": [[100.0, 0.0, 50.0], [0.0, 101.0, 51.0], [0.0, 0.0, 1.0]],
        "resolution": [720, 1280],
    }


def test_frame_writer_refuses_collision_and_invalid_rgbd(tmp_path: Path) -> None:
    kwargs = {
        "rgb_image": np.zeros((2, 3, 3), dtype=np.uint8),
        "depth_image": np.zeros((2, 3), dtype=np.uint16),
        "sensor_type": SensorType.REALSENSE_D435,
        "sensor_id": "123",
        "frame_index": 0,
        "sensor_timestamp_ns": 1,
        "host_received_timestamp_ns": 2,
        "host_wall_timestamp_ns": 3_000_000,
        "frame_stem": "000000",
    }
    write_rgbd_frame(tmp_path, **kwargs)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        write_rgbd_frame(tmp_path, **kwargs)

    invalid = dict(kwargs)
    invalid["frame_stem"] = "000001"
    invalid["depth_image"] = np.zeros((3, 2), dtype=np.uint16)
    with pytest.raises(ValueError, match="dimensions must match"):
        write_rgbd_frame(tmp_path, **invalid)


def test_frame_writer_rolls_back_committed_files_on_control_flow_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def interrupt_metadata(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(frame_writer, "append_frame_metadata", interrupt_metadata)

    with pytest.raises(KeyboardInterrupt):
        write_rgbd_frame(
            tmp_path,
            rgb_image=np.zeros((2, 3, 3), dtype=np.uint8),
            depth_image=np.zeros((2, 3), dtype=np.uint16),
            sensor_type=SensorType.REALSENSE_D435,
            sensor_id="123",
            frame_index=0,
            sensor_timestamp_ns=1,
            host_received_timestamp_ns=2,
            frame_stem="000000",
        )

    assert not list((tmp_path / RGB_DIR).iterdir())
    assert not list((tmp_path / DEPTH_DIR).iterdir())
    assert not (tmp_path / FRAME_METADATA_JSONL).exists()


def test_camera_sidecars_refuse_overwrite(tmp_path: Path) -> None:
    intrinsics = CameraIntrinsics(
        cam_k=(100.0, 0.0, 50.0, 0.0, 101.0, 51.0, 0.0, 0.0, 1.0),
        width=1280,
        height=720,
        depth_scale_to_mm=1.0,
    )
    write_camera_sidecars(tmp_path, intrinsics)

    with pytest.raises(FileExistsError, match="camera sidecar"):
        write_camera_sidecars(tmp_path, intrinsics)
