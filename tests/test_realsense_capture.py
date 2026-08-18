from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import cv2
import numpy as np

from posetestbot.io.artifacts import (
    CAM_K,
    CAMERA_DATA_JSON,
    CAMERA_JSON,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    RGB_DIR,
)
from posetestbot.calibration.intrinsics import (
    factory_intrinsic_profile,
    projection_is_opencv_compatible,
)
from posetestbot.sensors.frame_writer import write_legacy_camera_sidecars
from posetestbot.sensors.contracts import SensorType
from posetestbot.sensors.discovery import (
    _parse_realsense_lsusb_devices,
    discover_realsense_d435,
)
from posetestbot.sensors.realsense import (
    _intrinsics_for_orientation,
    camera_intrinsics_from_realsense,
    capture_realsense_rgbd,
)


class FakeIntrinsics:
    fx = 600.0
    fy = 601.0
    ppx = 320.0
    ppy = 240.0
    width = 1280
    height = 720
    coeffs = (0.1, -0.02, 0.003, -0.004, 0.005)
    model = "distortion.brown_conrady"


class FakeDepthIntrinsics(FakeIntrinsics):
    fx = 111.0
    fy = 112.0
    coeffs = (0.9, 0.8, 0.7, 0.6, 0.5)
    model = "distortion.inverse_brown_conrady"


class FakeFrameProfile:
    def __init__(self, *, color: bool):
        self.intrinsics = FakeIntrinsics() if color else FakeDepthIntrinsics()

    def as_video_stream_profile(self):
        return self


class FakeFrame:
    def __init__(self, index: int, *, color: bool):
        self.index = index
        self.profile = FakeFrameProfile(color=color)
        self.color = color

    def get_data(self):
        if self.color:
            image = np.zeros((3, 4, 3), dtype=np.uint8)
            image[:, :, 0] = np.arange(12, dtype=np.uint8).reshape(3, 4)
            image[:, :, 1] = self.index
            return image
        return np.arange(12, dtype=np.uint16).reshape(3, 4) + self.index * 100

    def get_timestamp(self):
        return float(self.index * 10)

    def get_frame_number(self):
        return self.index


class FakeFrames:
    def __init__(self, index: int):
        self.index = index

    def get_depth_frame(self):
        return FakeFrame(self.index, color=False)

    def get_color_frame(self):
        return FakeFrame(self.index, color=True)


class FakeDepthSensor:
    def get_depth_scale(self):
        return 0.001


class FakeSensor:
    def __init__(self, name: str):
        self.name = name

    def get_info(self, key):
        if key == "name":
            return self.name
        return ""


class FakeDevice:
    def __init__(self, serial: str = "123"):
        self.serial = serial
        self.sensors = [FakeSensor("RGB Camera")]

    def get_info(self, key):
        values = {
            "serial_number": self.serial,
            "name": "Intel RealSense D435i",
            "product_line": "D400",
        }
        return values[key]

    def first_depth_sensor(self):
        return FakeDepthSensor()


class FakeProfile:
    def __init__(self, device: FakeDevice):
        self.device = device

    def get_device(self):
        return self.device


class FakeConfig:
    def __init__(self, device: FakeDevice):
        self.device = device
        self.enabled_device = None
        self.streams = []

    def enable_device(self, serial: str):
        self.enabled_device = serial

    def resolve(self, _pipeline_wrapper):
        return FakeProfile(self.device)

    def enable_stream(self, *args):
        self.streams.append(args)


class FakePipeline:
    def __init__(self, device: FakeDevice):
        self.device = device
        self.index = 0
        self.started = False
        self.stopped = False

    def start(self, _config):
        self.started = True
        return FakeProfile(self.device)

    def wait_for_frames(self):
        self.index += 1
        return FakeFrames(self.index)

    def stop(self):
        self.stopped = True


class FakeAlign:
    def __init__(self, _stream):
        pass

    def process(self, frames):
        return frames


class FakeRS:
    camera_info = SimpleNamespace(
        serial_number="serial_number",
        name="name",
        product_line="product_line",
    )
    stream = SimpleNamespace(depth="depth", color="color")
    format = SimpleNamespace(z16="z16", bgr8="bgr8")

    def __init__(self, serial: str = "123"):
        self.device = FakeDevice(serial)
        self.pipeline_instance = None
        self.config_instance = None

    def pipeline(self):
        self.pipeline_instance = FakePipeline(self.device)
        return self.pipeline_instance

    def config(self):
        self.config_instance = FakeConfig(self.device)
        return self.config_instance

    def pipeline_wrapper(self, pipeline):
        return pipeline

    def align(self, stream):
        return FakeAlign(stream)


class PreviewSpy:
    def __init__(self):
        self.imshow_calls = 0
        self.wait_key_calls = 0
        self.destroy_calls = 0

    def imshow(self, *_args):
        self.imshow_calls += 1

    def waitKey(self, *_args):
        self.wait_key_calls += 1
        return -1

    def destroyAllWindows(self):
        self.destroy_calls += 1


def test_capture_realsense_rgbd_writes_frames_without_preview(tmp_path) -> None:
    fake_rs = FakeRS("825412070181")
    preview = PreviewSpy()

    summary = capture_realsense_rgbd(
        tmp_path,
        device_id="825412070181",
        fps=6,
        max_frames=2,
        warmup_frames=1,
        preview=False,
        rs_module=fake_rs,
        cv2_module=preview,
    )

    assert summary["schema_version"] == "realsense_capture_summary.v1"
    assert summary["sensor_id"] == "825412070181"
    assert summary["product_line"] == "D400"
    assert summary["frame_count"] == 2
    assert summary["skipped_duplicate_color_frame_count"] == 0
    assert summary["preview"] is False
    assert preview.imshow_calls == 0
    assert fake_rs.pipeline_instance.stopped is True
    assert fake_rs.config_instance.enabled_device == "825412070181"
    assert len(list((tmp_path / RGB_DIR).glob("*.png"))) == 2
    assert len(list((tmp_path / DEPTH_DIR).glob("*.png"))) == 2
    assert (tmp_path / CAMERA_JSON).is_file()
    camera_data = json.loads((tmp_path / CAMERA_DATA_JSON).read_text())
    assert camera_data["K"][0][0] == 600.0
    assert camera_data["distortion"] == [0.1, -0.02, 0.003, -0.004, 0.005]
    assert camera_data["distortion_model"] == "brown_conrady"
    assert camera_data["projection_source"] == "realsense_sdk_color_stream"
    assert len((tmp_path / "cam_K.txt").read_text().splitlines()) == 4
    records = [
        json.loads(line)
        for line in (tmp_path / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    assert [record["frame_index"] for record in records] == [0, 1]
    assert records[0]["sensor_id"] == "825412070181"
    assert records[0]["inverted"] is False
    assert records[0]["image_rotation_degrees"] == 0


def test_capture_realsense_rgbd_skips_reused_aligned_color_frame(
    tmp_path,
) -> None:
    class RepeatedColorFrames(FakeFrames):
        def __init__(self, depth_index: int, color_index: int):
            super().__init__(depth_index)
            self.color_index = color_index

        def get_color_frame(self):
            return FakeFrame(self.color_index, color=True)

    class RepeatedColorPipeline(FakePipeline):
        def wait_for_frames(self):
            self.index += 1
            color_index = 1 if self.index <= 2 else 2
            return RepeatedColorFrames(self.index, color_index)

    class RepeatedColorRS(FakeRS):
        def pipeline(self):
            self.pipeline_instance = RepeatedColorPipeline(self.device)
            return self.pipeline_instance

    fake_rs = RepeatedColorRS("233522079721")
    summary = capture_realsense_rgbd(
        tmp_path,
        device_id="233522079721",
        fps=6,
        max_frames=2,
        rs_module=fake_rs,
    )

    records = [
        json.loads(line)
        for line in (tmp_path / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    assert summary["status"] == "succeeded"
    assert summary["frame_count"] == 2
    assert summary["skipped_duplicate_color_frame_count"] == 1
    assert fake_rs.pipeline_instance.index == 3
    assert [record["frame_index"] for record in records] == [0, 1]
    assert [record["color_frame_number"] for record in records] == [1, 2]
    assert [record["depth_frame_number"] for record in records] == [1, 3]
    assert [record["frame_id"] for record in records] == ["10.png", "20.png"]


def test_capture_realsense_rgbd_duplicate_keeps_preview_stop_reachable(
    tmp_path,
) -> None:
    class RepeatedColorFrames(FakeFrames):
        def get_color_frame(self):
            return FakeFrame(1, color=True)

    class RepeatedColorPipeline(FakePipeline):
        def wait_for_frames(self):
            self.index += 1
            return RepeatedColorFrames(self.index)

    class RepeatedColorRS(FakeRS):
        def pipeline(self):
            self.pipeline_instance = RepeatedColorPipeline(self.device)
            return self.pipeline_instance

    class QuitOnDuplicatePreview(PreviewSpy):
        def waitKey(self, *_args):
            self.wait_key_calls += 1
            return ord("q") if self.wait_key_calls == 2 else -1

    fake_rs = RepeatedColorRS("233522079721")
    preview = QuitOnDuplicatePreview()
    summary = capture_realsense_rgbd(
        tmp_path,
        device_id="233522079721",
        fps=6,
        max_frames=2,
        preview=True,
        rs_module=fake_rs,
        cv2_module=preview,
    )

    assert summary["frame_count"] == 1
    assert summary["skipped_duplicate_color_frame_count"] == 1
    assert fake_rs.pipeline_instance.index == 2
    assert preview.imshow_calls == 2
    assert preview.wait_key_calls == 2
    assert preview.destroy_calls == 1


def test_host_received_timestamp_is_sampled_before_alignment(
    tmp_path,
    monkeypatch,
) -> None:
    state = {"phase": "created"}

    class PhasePipeline(FakePipeline):
        def wait_for_frames(self):
            frames = super().wait_for_frames()
            state["phase"] = "sdk_returned"
            return frames

    class PhaseAlign(FakeAlign):
        def process(self, frames):
            state["phase"] = "aligned"
            return super().process(frames)

    class PhaseRS(FakeRS):
        def pipeline(self):
            self.pipeline_instance = PhasePipeline(self.device)
            return self.pipeline_instance

        def align(self, stream):
            return PhaseAlign(stream)

    def monotonic_ns() -> int:
        assert state["phase"] == "sdk_returned"
        state["phase"] = "timestamped"
        return 123_456_789

    monkeypatch.setattr(
        "posetestbot.sensors.realsense.time.monotonic_ns",
        monotonic_ns,
    )

    capture_realsense_rgbd(
        tmp_path,
        max_frames=1,
        rs_module=PhaseRS(),
    )

    record = json.loads((tmp_path / FRAME_METADATA_JSONL).read_text())
    assert record["host_received_timestamp_ns"] == 123_456_789


def test_capture_realsense_rgbd_preview_is_optional(tmp_path) -> None:
    preview = PreviewSpy()

    summary = capture_realsense_rgbd(
        tmp_path,
        device_id="123",
        fps=6,
        max_frames=1,
        preview=True,
        rs_module=FakeRS("123"),
        cv2_module=preview,
    )

    assert summary["frame_count"] == 1
    assert summary["preview"] is True
    assert preview.imshow_calls == 1
    assert preview.wait_key_calls == 1
    assert preview.destroy_calls == 1


def test_capture_realsense_rgbd_honors_graceful_stop_between_frames(tmp_path) -> None:
    fake_rs = FakeRS("123")

    summary = capture_realsense_rgbd(
        tmp_path,
        device_id="123",
        fps=6,
        max_frames=0,
        preview=False,
        stop_requested=lambda: fake_rs.pipeline_instance.index >= 2,
        rs_module=fake_rs,
    )

    assert summary["frame_count"] == 1
    assert fake_rs.pipeline_instance.stopped is True
    assert len(list((tmp_path / RGB_DIR).glob("*.png"))) == 1
    assert len(list((tmp_path / DEPTH_DIR).glob("*.png"))) == 1
    assert len((tmp_path / FRAME_METADATA_JSONL).read_text().splitlines()) == 1


def test_capture_realsense_rgbd_inverted_rotates_frames_and_intrinsics(
    tmp_path,
) -> None:
    summary = capture_realsense_rgbd(
        tmp_path,
        device_id="123",
        fps=6,
        max_frames=1,
        preview=False,
        inverted=True,
        rs_module=FakeRS("123"),
    )

    assert summary["inverted"] is True
    assert summary["image_rotation_degrees"] == 180
    assert summary["orientation"] == "inverted"

    rgb_path = next((tmp_path / RGB_DIR).glob("*.png"))
    depth_path = next((tmp_path / DEPTH_DIR).glob("*.png"))
    written_rgb = cv2.imread(rgb_path.as_posix(), cv2.IMREAD_UNCHANGED)
    written_depth = cv2.imread(depth_path.as_posix(), cv2.IMREAD_UNCHANGED)
    expected_rgb = np.rot90(FakeFrame(1, color=True).get_data(), 2)
    expected_depth = np.rot90(FakeFrame(1, color=False).get_data(), 2)

    assert np.array_equal(written_rgb, expected_rgb)
    assert np.array_equal(written_depth, expected_depth)

    camera = json.loads((tmp_path / CAMERA_JSON).read_text())
    assert camera["cam_K"] == [
        600.0,
        0.0,
        959.0,
        0.0,
        601.0,
        479.0,
        0.0,
        0.0,
        1.0,
    ]
    assert camera["distortion"] == [0.1, -0.02, -0.003, 0.004, 0.005]
    assert camera["distortion_model"] == "brown_conrady"
    assert camera["projection_source"] == ("realsense_sdk_color_stream_rotated_180")
    records = [
        json.loads(line)
        for line in (tmp_path / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    assert records[0]["inverted"] is True
    assert records[0]["image_rotation_degrees"] == 180
    assert records[0]["orientation"] == "inverted"


def test_inverse_sdk_distortion_is_preserved_but_not_misapplied_to_opencv(
    tmp_path,
) -> None:
    sdk_intrinsics = SimpleNamespace(
        fx=600.0,
        fy=601.0,
        ppx=320.0,
        ppy=240.0,
        width=1280,
        height=720,
        coeffs=(0.1, -0.02, 0.003, -0.004, 0.005),
        model="distortion.inverse_brown_conrady",
    )
    native = camera_intrinsics_from_realsense(sdk_intrinsics, 1.0)
    inverted = _intrinsics_for_orientation(native, inverted=True)

    assert inverted.distortion_model == "inverse_brown_conrady"
    assert inverted.distortion == (0.1, -0.02, -0.003, 0.004, 0.005)
    write_legacy_camera_sidecars(tmp_path, inverted)

    assert len((tmp_path / CAM_K).read_text().splitlines()) == 3
    profile = factory_intrinsic_profile(tmp_path)
    assert profile["native"]["distortion"] == [
        0.1,
        -0.02,
        -0.003,
        0.004,
        0.005,
    ]
    assert profile["native"]["distortion_model"] == "inverse_brown_conrady"
    assert profile["source"]["opencv_projection_compatible"] is False
    assert profile["source"]["rectification_available"] is False
    assert profile["rectified"] is None
    assert projection_is_opencv_compatible(profile["native"]) is False


def test_exact_zero_inverse_distortion_is_model_invariant_for_opencv(
    tmp_path,
) -> None:
    sdk_intrinsics = SimpleNamespace(
        fx=600.0,
        fy=601.0,
        ppx=320.0,
        ppy=240.0,
        width=1280,
        height=720,
        coeffs=(0.0, -0.0, 0.0, 0.0, 0.0),
        model="distortion.inverse_brown_conrady",
    )
    native = camera_intrinsics_from_realsense(sdk_intrinsics, 1.0)
    write_legacy_camera_sidecars(tmp_path, native)

    profile = factory_intrinsic_profile(tmp_path)

    assert profile["native"]["distortion_model"] == "inverse_brown_conrady"
    assert profile["native"]["distortion"] == [0.0] * 5
    assert projection_is_opencv_compatible(profile["native"]) is True
    assert profile["source"]["opencv_projection_compatible"] is True
    assert profile["source"]["opencv_projection_compatibility_basis"] == (
        "exact_zero_distortion_is_model_invariant"
    )
    assert profile["source"]["rectification_available"] is True
    assert profile["source"]["rectification_unavailable_reason"] is None
    assert profile["rectified"] is not None
    assert profile["rectified"]["distortion"] == [0.0] * 5
    assert (
        projection_is_opencv_compatible(
            {
                "distortion_model": "kannala_brandt4",
                "distortion": [0.0] * 5,
            }
        )
        is False
    )


def test_discover_realsense_d435_reads_mocked_sdk_devices(monkeypatch) -> None:
    class FakeDiscoveryDevice:
        def get_info(self, key):
            return {
                "serial_number": "rs-1",
                "name": "Intel RealSense D435",
                "product_line": "D400",
            }[key]

    fake_rs = SimpleNamespace(
        camera_info=FakeRS.camera_info,
        context=lambda: SimpleNamespace(query_devices=lambda: [FakeDiscoveryDevice()]),
    )
    monkeypatch.setitem(sys.modules, "pyrealsense2", fake_rs)
    monkeypatch.setattr(
        "posetestbot.sensors.discovery._video_node_metadata_by_serial",
        lambda: {},
    )
    monkeypatch.setattr(
        "posetestbot.sensors.discovery._discover_realsense_from_lsusb",
        lambda: [],
    )

    devices = discover_realsense_d435()

    assert len(devices) == 1
    assert devices[0].sensor_type == SensorType.REALSENSE_D435
    assert devices[0].device_id == "rs-1"
    assert devices[0].metadata["product_line"] == "D400"


def test_discover_realsense_d435_filters_other_realsense_models(monkeypatch) -> None:
    class FakeCameraInfo:
        serial_number = "serial_number"
        name = "name"
        product_line = "product_line"
        product_id = "product_id"

    class FakeDiscoveryDevice:
        def __init__(self, serial: str, name: str, product_id: str):
            self.values = {
                "serial_number": serial,
                "name": name,
                "product_line": "D400",
                "product_id": product_id,
            }

        def supports(self, _key):
            return True

        def get_info(self, key):
            return self.values[key]

    fake_rs = SimpleNamespace(
        camera_info=FakeCameraInfo,
        context=lambda: SimpleNamespace(
            query_devices=lambda: [
                FakeDiscoveryDevice("d435", "Intel RealSense D435", "0B07"),
                FakeDiscoveryDevice("d455", "Intel RealSense D455", "0B5C"),
            ]
        ),
    )
    monkeypatch.setitem(sys.modules, "pyrealsense2", fake_rs)
    monkeypatch.setattr(
        "posetestbot.sensors.discovery._video_node_metadata_by_serial",
        lambda: {},
    )
    monkeypatch.setattr(
        "posetestbot.sensors.discovery._discover_realsense_from_lsusb",
        lambda: [],
    )

    devices = discover_realsense_d435()

    assert [device.device_id for device in devices] == ["d435"]


def test_parse_realsense_lsusb_fallback_reads_d435_and_d435i() -> None:
    devices = _parse_realsense_lsusb_devices(
        """
Bus 003 Device 005: ID 8086:0b07 Intel Corp. RealSense D435
  iProduct                2 Intel(R) RealSense(TM) Depth Camera 435
  iSerial                 3 926223021865
Bus 003 Device 008: ID 8086:0b3a Intel Corp. Intel(R) RealSense(TM) Depth Camera 435i
  iProduct                2 Intel(R) RealSense(TM) Depth Camera 435i
  iSerial                 3 923322072633
"""
    )

    assert [device.device_id for device in devices] == [
        "926223021865",
        "923322072633",
    ]
    assert devices[0].metadata["product_id"] == "0b07"
    assert devices[1].metadata["product_id"] == "0b3a"
