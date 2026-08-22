from __future__ import annotations

import json
import signal
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import scripts.capture_zed_2i as zed_capture_cli

from posetestbot.io.artifacts import CAMERA_JSON, DEPTH_DIR, FRAME_METADATA_JSONL, RGB_DIR
from posetestbot.sensors.zed_2i import ZED2iCaptureError, capture_zed_2i_rgbd


class FakeInitParameters:
    def __init__(self) -> None:
        self.selected_serial = None
        self.camera_resolution = None
        self.camera_fps = None
        self.coordinate_units = None
        self.depth_mode = None

    def set_from_serial_number(self, serial: int) -> None:
        self.selected_serial = serial


class FakeMat:
    def __init__(self) -> None:
        self.data = None

    def get_data(self):
        return self.data


class FakeTimestamp:
    def __init__(self, value: int) -> None:
        self.value = value

    def get_nanoseconds(self) -> int:
        return self.value


class FakeZEDCamera:
    def __init__(self, *, open_status: str = "SUCCESS") -> None:
        self.open_status = open_status
        self.closed = False
        self.grab_index = 0
        self.init_parameters = None

    def open(self, init_parameters):
        self.init_parameters = init_parameters
        return self.open_status

    def get_camera_information(self):
        left = SimpleNamespace(
            fx=100.0,
            fy=101.0,
            cx=2.0,
            cy=1.0,
            disto=[0.1, 0.0, 0.0, 0.0, 0.0],
        )
        return SimpleNamespace(
            serial_number=987654,
            camera_configuration=SimpleNamespace(
                serial_number=987654,
                calibration_parameters=SimpleNamespace(left_cam=left),
                resolution=SimpleNamespace(width=4, height=3),
            ),
        )

    def grab(self, _runtime_parameters):
        self.grab_index += 1
        return "SUCCESS"

    def retrieve_image(self, image, _view):
        value = np.zeros((3, 4, 4), dtype=np.uint8)
        value[:, :, 0] = self.grab_index
        value[:, :, 3] = 255
        image.data = value

    def retrieve_measure(self, depth, _measure):
        value = np.full((3, 4), 100.5 + self.grab_index, dtype=np.float32)
        value[0, 0] = np.nan
        depth.data = value

    def get_timestamp(self, _reference):
        return FakeTimestamp(self.grab_index * 1_000_000)

    def close(self) -> None:
        self.closed = True


class FakeSL:
    RESOLUTION = SimpleNamespace(HD720="HD720", VGA="VGA")
    UNIT = SimpleNamespace(MILLIMETER="MILLIMETER")
    DEPTH_MODE = SimpleNamespace(NEURAL="NEURAL")
    ERROR_CODE = SimpleNamespace(SUCCESS="SUCCESS")
    VIEW = SimpleNamespace(LEFT="LEFT")
    MEASURE = SimpleNamespace(DEPTH="DEPTH")
    TIME_REFERENCE = SimpleNamespace(IMAGE="IMAGE")
    RuntimeParameters = type("RuntimeParameters", (), {})
    Mat = FakeMat
    InitParameters = FakeInitParameters

    def __init__(self, camera: FakeZEDCamera) -> None:
        self.camera = camera

    def Camera(self) -> FakeZEDCamera:
        return self.camera


def test_zed_capture_uses_resolved_serial_and_shared_frame_contract(tmp_path) -> None:
    camera = FakeZEDCamera()
    fake_sl = FakeSL(camera)

    summary = capture_zed_2i_rgbd(
        tmp_path,
        device_id="123456",
        fps=15,
        max_frames=2,
        warmup_frames=1,
        resolution="360p",
        sl_module=fake_sl,
        cv2_module=cv2,
    )

    assert summary["schema_version"] == "zed_2i_capture_summary.v1"
    assert summary["sensor_id"] == "987654"
    assert summary["requested_device_id"] == "123456"
    assert summary["frame_count"] == 2
    assert summary["valid_frames_seen"] == 3
    assert summary["resolution"] == "360p"
    assert camera.init_parameters.selected_serial == 123456
    assert camera.init_parameters.camera_resolution == "VGA"
    assert camera.closed is True
    assert (tmp_path / CAMERA_JSON).is_file()
    rgb_files = sorted((tmp_path / RGB_DIR).glob("*.png"))
    depth_files = sorted((tmp_path / DEPTH_DIR).glob("*.png"))
    assert [path.name for path in rgb_files] == [path.name for path in depth_files]
    assert len({path.stem for path in rgb_files}) == 2
    assert all(path.stem.isdigit() for path in rgb_files)
    depth = cv2.imread(depth_files[0].as_posix(), cv2.IMREAD_UNCHANGED)
    assert depth.dtype == np.uint16
    assert depth[0, 0] == 0
    records = [
        json.loads(line)
        for line in (tmp_path / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    assert [record["sensor_timestamp_ns"] for record in records] == [2_000_000, 3_000_000]
    assert all(record["sensor_id"] == "987654" for record in records)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"fps": 0}, "fps must be positive"),
        ({"max_frames": -1}, "max_frames"),
        ({"warmup_frames": -1}, "warmup_frames"),
        ({"resolution": "1080p"}, "resolution"),
        ({"device_id": "not-a-serial"}, "numeric serial"),
    ],
)
def test_zed_capture_validates_arguments(arguments: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        capture_zed_2i_rgbd(None, record=False, **arguments)


def test_zed_capture_wraps_open_error_and_closes_camera(tmp_path) -> None:
    camera = FakeZEDCamera(open_status="CAMERA_NOT_DETECTED")

    with pytest.raises(ZED2iCaptureError, match="Could not open ZED camera"):
        capture_zed_2i_rgbd(
            tmp_path,
            max_frames=1,
            sl_module=FakeSL(camera),
            cv2_module=cv2,
        )

    assert camera.closed is True
    assert not (tmp_path / RGB_DIR).exists()


def test_zed_capture_honors_supervisor_stop_callback(tmp_path) -> None:
    camera = FakeZEDCamera()

    summary = capture_zed_2i_rgbd(
        tmp_path,
        max_frames=0,
        sl_module=FakeSL(camera),
        cv2_module=cv2,
        stop_requested=lambda: camera.grab_index >= 2,
    )

    assert summary["frame_count"] == 1
    assert camera.grab_index == 2
    assert camera.closed is True


def test_zed_cli_sigterm_handler_sets_adapter_stop_callback(monkeypatch) -> None:
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_capture(_output_path, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert kwargs["stop_requested"]() is True
        return {
            "status": "succeeded",
            "sensor_id": "zed-1",
            "frame_count": 0,
            "preview": False,
        }

    monkeypatch.setattr(
        zed_capture_cli,
        "parse_args",
        lambda: SimpleNamespace(
            output_path="unused",
            test=False,
            device="123",
            fps=30,
            max_frames=0,
            warmup_frames=0,
            resolution="720p",
            preview=False,
            print_json=False,
        ),
    )
    monkeypatch.setattr(zed_capture_cli, "capture_zed_2i_rgbd", fake_capture)

    assert zed_capture_cli.main() == 0
    assert signal.getsignal(signal.SIGTERM) == previous_sigterm
