from __future__ import annotations

import json
import signal
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

import scripts.capture_luxonis_720p as oak_capture_cli

from posetestbot.io.artifacts import DEPTH_DIR, FRAME_METADATA_JSONL, RGB_DIR
from posetestbot.sensors.oak_d_pro import (
    OAKDProPreviewStream,
    OAKDProCaptureError,
    TIMESTAMP_SOURCE,
    _next_sync_message,
    camera_intrinsics_from_matrix,
    capture_oak_d_pro_rgbd,
    dai_timestamp_ns,
    estimate_capture_wall_timestamp_ns,
    rgb_depth_pair_is_usable,
    rgb_depth_timestamp_delta_ns,
    write_oak_d_pro_rgbd_frame,
)


class FakeDepthAIFrame:
    def __init__(
        self,
        *,
        timestamp_ns: int,
        device_timestamp_ns: int,
        sequence_num: int,
        rgb_image=None,
        depth_image=None,
    ):
        self.timestamp_ns = timestamp_ns
        self.device_timestamp_ns = device_timestamp_ns
        self.sequence_num = sequence_num
        self.rgb_image = rgb_image
        self.depth_image = depth_image

    def getTimestamp(self) -> timedelta:
        return timedelta(microseconds=self.timestamp_ns // 1_000)

    def getTimestampDevice(self) -> timedelta:
        return timedelta(microseconds=self.device_timestamp_ns // 1_000)

    def getSequenceNum(self) -> int:
        return self.sequence_num

    def getCvFrame(self):
        return self.rgb_image

    def getFrame(self):
        return self.depth_image


class FakeDaiNoDevice:
    __version__ = "3.7.1"

    class Device:
        def __init__(self):
            raise RuntimeError("No available devices")


def test_depthai_timestamp_helpers_use_host_synced_clock() -> None:
    rgb = FakeDepthAIFrame(
        timestamp_ns=4_900_123_000,
        device_timestamp_ns=9_000_000_000,
        sequence_num=7,
    )
    depth = FakeDepthAIFrame(
        timestamp_ns=4_902_123_000,
        device_timestamp_ns=9_002_000_000,
        sequence_num=8,
    )

    assert dai_timestamp_ns(rgb) == 4_900_123_000
    assert dai_timestamp_ns(rgb, device_clock=True) == 9_000_000_000
    assert rgb_depth_timestamp_delta_ns(rgb, depth) == 2_000_000
    assert rgb_depth_pair_is_usable(rgb, depth, max_delta_ns=2_000_000) is True
    assert rgb_depth_pair_is_usable(rgb, depth, max_delta_ns=1_999_999) is False

    host_wall_received_ns = 1_700_000_000_010_000_000
    assert (
        estimate_capture_wall_timestamp_ns(
            frame_depthai_timestamp_ns=4_900_123_000,
            depthai_now_ns=5_000_123_000,
            host_wall_received_timestamp_ns=host_wall_received_ns,
        )
        == host_wall_received_ns - 100_000_000
    )


def test_write_oak_d_pro_rgbd_frame_uses_capture_wall_timestamp(
    tmp_path: Path,
) -> None:
    rgb_image = np.zeros((3, 4, 3), dtype=np.uint8)
    rgb_image[:, :, 1] = 128
    depth_image = np.ones((3, 4), dtype=np.uint16) * 42
    rgb = FakeDepthAIFrame(
        timestamp_ns=4_900_123_000,
        device_timestamp_ns=9_000_000_000,
        sequence_num=7,
        rgb_image=rgb_image,
    )
    depth = FakeDepthAIFrame(
        timestamp_ns=4_901_123_000,
        device_timestamp_ns=9_001_000_000,
        sequence_num=8,
        depth_image=depth_image,
    )
    host_wall_received_ns = 1_700_000_000_010_000_000
    capture_wall_ns = host_wall_received_ns - 100_000_000

    metadata = write_oak_d_pro_rgbd_frame(
        tmp_path,
        rgb_packet=rgb,
        depth_packet=depth,
        sensor_id="mxid-1",
        frame_index=3,
        host_received_timestamp_ns=123,
        host_wall_received_timestamp_ns=host_wall_received_ns,
        depthai_now_ns=5_000_123_000,
    )

    assert metadata["frame_id"] == f"{capture_wall_ns // 1_000_000}.png"
    assert metadata["sensor_type"] == "oak_d_pro"
    assert metadata["sensor_id"] == "mxid-1"
    assert metadata["sensor_timestamp_ns"] == 4_900_123_000
    assert metadata["depth_sensor_timestamp_ns"] == 4_901_123_000
    assert metadata["device_timestamp_ns"] == 9_000_000_000
    assert metadata["depth_device_timestamp_ns"] == 9_001_000_000
    assert metadata["capture_wall_timestamp_ns"] == capture_wall_ns
    assert metadata["host_wall_timestamp_ns"] == capture_wall_ns
    assert metadata["host_wall_received_timestamp_ns"] == host_wall_received_ns
    assert metadata["host_received_timestamp_ns"] == 123
    assert metadata["rgb_sequence_num"] == 7
    assert metadata["depth_sequence_num"] == 8
    assert metadata["rgb_depth_timestamp_delta_ns"] == 1_000_000
    assert metadata["timestamp_source"] == TIMESTAMP_SOURCE

    rgb_path = tmp_path / RGB_DIR / metadata["frame_id"]
    depth_path = tmp_path / DEPTH_DIR / metadata["frame_id"]
    assert rgb_path.is_file()
    assert depth_path.is_file()
    assert cv2.imread(rgb_path.as_posix()).shape == (3, 4, 3)
    assert cv2.imread(depth_path.as_posix(), cv2.IMREAD_UNCHANGED).dtype == np.uint16
    assert json.loads((tmp_path / FRAME_METADATA_JSONL).read_text()) == metadata


def test_camera_intrinsics_from_matrix_validates_shape() -> None:
    intrinsics = camera_intrinsics_from_matrix(
        np.array([[100.0, 0.0, 2.0], [0.0, 101.0, 3.0], [0.0, 0.0, 1.0]]),
        width=4,
        height=3,
        depth_scale_to_mm=1.0,
    )

    assert intrinsics.width == 4
    assert intrinsics.height == 3
    assert intrinsics.cam_k == (
        100.0,
        0.0,
        2.0,
        0.0,
        101.0,
        3.0,
        0.0,
        0.0,
        1.0,
    )


def test_capture_wraps_depthai_device_open_failures(tmp_path: Path) -> None:
    try:
        capture_oak_d_pro_rgbd(tmp_path, max_frames=1, dai_module=FakeDaiNoDevice)
    except OAKDProCaptureError as exc:
        assert "Unable to open OAK-D Pro device" in str(exc)
        assert "No available devices" in str(exc)
    else:
        raise AssertionError("device-open failure was not wrapped")


def test_supervised_oak_queue_poll_stops_without_blocking_get() -> None:
    class Queue:
        def __init__(self) -> None:
            self.try_calls = 0
            self.get_calls = 0

        def tryGet(self):
            self.try_calls += 1
            return None

        def get(self):
            self.get_calls += 1
            raise AssertionError("supervised capture must not enter blocking get")

    queue = Queue()
    stop_checks = 0

    def stop_requested() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 2

    message = _next_sync_message(
        queue,
        stop_requested=stop_requested,
        pipeline_running=lambda: True,
    )

    assert message is None
    assert queue.try_calls == 1
    assert queue.get_calls == 0


def test_oak_cli_sigterm_handler_sets_adapter_stop_callback(monkeypatch) -> None:
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_capture(_output_path, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert kwargs["stop_requested"]() is True
        return {
            "status": "succeeded",
            "sensor_id": "oak-1",
            "frame_count": 0,
            "rejected_pairs": 0,
            "preview": False,
        }

    monkeypatch.setattr(
        oak_capture_cli,
        "parse_args",
        lambda: SimpleNamespace(
            test=False,
            output_path="unused",
            device="oak-1",
            fps=6,
            max_frames=0,
            warmup_frames=0,
            preview=False,
            max_rgb_depth_delta_ms=30.0,
            print_json=False,
        ),
    )
    monkeypatch.setattr(oak_capture_cli, "capture_oak_d_pro_rgbd", fake_capture)

    assert oak_capture_cli.main() == 0
    assert signal.getsignal(signal.SIGTERM) == previous_sigterm


def test_oak_preview_uses_nonblocking_latest_frame_queue_and_closes() -> None:
    frames = [np.zeros((2, 3, 3), dtype=np.uint8), np.ones((2, 3, 3), dtype=np.uint8)]

    class Packet:
        def __init__(self, frame):
            self.frame = frame

        def getCvFrame(self):
            return self.frame

    class Queue:
        def __init__(self):
            self.blocking = None
            self.max_size = None

        def setBlocking(self, value):
            self.blocking = value

        def setMaxSize(self, value):
            self.max_size = value

        def tryGetAll(self):
            return [Packet(frames[0]), Packet(frames[1])]

    class Output:
        def __init__(self, queue):
            self.queue = queue

        def createOutputQueue(self):
            return self.queue

    class CameraNode:
        class InitialControl:
            def setManualFocus(self, _value):
                pass

        def __init__(self, queue):
            self.queue = queue
            self.initialControl = self.InitialControl()

        def build(self, *_args, **_kwargs):
            pass

        def requestOutput(self, *_args):
            return Output(self.queue)

    class Device:
        def __init__(self, _device_id=None):
            self.closed = False

        def getMxId(self):
            return "oak-1"

        def readCalibration(self):
            raise RuntimeError("no calibration in preview double")

        def close(self):
            self.closed = True

    class Pipeline:
        def __init__(self, device):
            self.device = device
            self.queue = Queue()
            self.started = False
            self.stopped = False
            self.exited = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.exited = True

        def create(self, _node):
            return CameraNode(self.queue)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def wait(self):
            pass

    class FakeDai:
        __version__ = "3.7.1"

        class node:
            Camera = object()

        class CameraBoardSocket:
            CAM_A = object()

        class ImgFrame:
            class Type:
                BGR888i = object()

        class ImgResizeMode:
            STRETCH = object()

    FakeDai.Device = Device
    FakeDai.Pipeline = Pipeline

    stream = OAKDProPreviewStream(device_id="oak-1", dai_module=FakeDai)
    pipeline = stream.pipeline
    device = stream.device

    latest = stream.try_get_frame()
    stream.close()

    assert np.array_equal(latest, frames[1])
    assert pipeline.queue.blocking is False
    assert pipeline.queue.max_size == 1
    assert pipeline.started is True
    assert pipeline.stopped is True
    assert pipeline.exited is True
    assert device.closed is True
