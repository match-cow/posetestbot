"""Luxonis OAK-D Pro capture helpers using DepthAI v3."""

from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from posetestbot.sensors.contracts import CameraIntrinsics, SensorType
from posetestbot.sensors.frame_writer import (
    ensure_rgbd_folders,
    sync_frame_metadata,
    write_camera_sidecars,
    write_rgbd_frame,
)


CAPTURE_SUMMARY_SCHEMA_VERSION = "oak_d_pro_capture_summary.v1"
DEPTHAI_REQUIRED_MAJOR = 3
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_RGB_DEPTH_DELTA_NS = 30_000_000
TIMESTAMP_SOURCE = "depthai_host_synced"


class OAKDProCaptureError(RuntimeError):
    """Raised when an OAK-D Pro capture cannot be started or completed."""


class OAKDProPreviewStream:
    """Non-blocking DepthAI v3 RGB preview with a one-frame host queue."""

    def __init__(
        self,
        *,
        device_id: str | None = None,
        fps: int = 6,
        width: int = 640,
        height: int = 480,
        dai_module: Any | None = None,
    ) -> None:
        if fps <= 0 or width <= 0 or height <= 0:
            raise ValueError("OAK-D Pro preview dimensions and fps must be positive")
        self.dai = dai_module or _import_depthai()
        version = getattr(self.dai, "__version__", None)
        if not depthai_version_supported(version):
            raise OAKDProCaptureError(
                f"DepthAI v3 is required for OAK-D Pro preview; found "
                f"depthai {version or 'unknown'}."
            )
        self.device: Any | None = None
        self.pipeline_context: Any | None = None
        self.pipeline: Any | None = None
        self.queue: Any | None = None
        self.device_id = device_id
        self.resolved_device_id = device_id or "default"
        self.closed = False
        try:
            self.device = _open_device(self.dai, device_id)
            self.resolved_device_id = _device_id_from_device(self.device, device_id)
            self.pipeline_context = self.dai.Pipeline(self.device)
            enter = getattr(self.pipeline_context, "__enter__", None)
            self.pipeline = enter() if callable(enter) else self.pipeline_context
            color = self.pipeline.create(self.dai.node.Camera)
            color.build(self.dai.CameraBoardSocket.CAM_A, sensorFps=float(fps))
            _apply_lens_position(self.device, self.dai, color)
            output = color.requestOutput(
                (int(width), int(height)),
                self.dai.ImgFrame.Type.BGR888i,
                self.dai.ImgResizeMode.STRETCH,
                float(fps),
                True,
            )
            self.queue = output.createOutputQueue()
            set_blocking = getattr(self.queue, "setBlocking", None)
            if callable(set_blocking):
                set_blocking(False)
            set_max_size = getattr(self.queue, "setMaxSize", None)
            if callable(set_max_size):
                set_max_size(1)
            self.pipeline.start()
        except Exception as exc:
            self.close()
            if isinstance(exc, OAKDProCaptureError):
                raise
            raise OAKDProCaptureError(
                f"Unable to start OAK-D Pro RGB preview: {type(exc).__name__}: {exc}"
            ) from exc

    @property
    def selected_source(self) -> dict[str, Any]:
        return {
            "kind": "depthai",
            "device_id": self.resolved_device_id,
            "queue_blocking": False,
            "queue_max_size": 1,
        }

    def try_get_frame(self) -> Any | None:
        if self.closed or self.queue is None:
            return None
        packets: list[Any] = []
        try_get_all = getattr(self.queue, "tryGetAll", None)
        if callable(try_get_all):
            packets = [packet for packet in try_get_all() if packet is not None]
        else:
            try_get = getattr(self.queue, "tryGet", None)
            packet = try_get() if callable(try_get) else None
            if packet is not None:
                packets = [packet]
        if not packets:
            return None
        return packets[-1].getCvFrame()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.pipeline is not None:
            try:
                self.pipeline.stop()
                self.pipeline.wait()
            except Exception:
                pass
        if self.pipeline_context is not None:
            exit_context = getattr(self.pipeline_context, "__exit__", None)
            if callable(exit_context):
                try:
                    exit_context(None, None, None)
                except Exception:
                    pass
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass

    def __enter__(self) -> "OAKDProPreviewStream":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def _version_major(version: str | None) -> int | None:
    if not version:
        return None
    match = re.match(r"\s*(\d+)", str(version))
    return int(match.group(1)) if match else None


def depthai_version_supported(version: str | None) -> bool:
    return _version_major(version) == DEPTHAI_REQUIRED_MAJOR


def depthai_version_status(version: str | None) -> dict[str, Any]:
    supported = depthai_version_supported(version)
    return {
        "version": version,
        "required": ">=3,<4",
        "supported": supported,
    }


def _import_depthai() -> Any:
    try:
        import depthai as dai
    except ImportError as exc:
        raise OAKDProCaptureError(
            "depthai is not importable in the current uv environment."
        ) from exc

    version = getattr(dai, "__version__", None)
    if not depthai_version_supported(version):
        found = version or "unknown"
        raise OAKDProCaptureError(
            f"DepthAI v3 is required for OAK-D Pro capture; found depthai {found}."
        )
    return dai


def depthai_timedelta_ns(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1_000_000_000)
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        return int(total_seconds() * 1_000_000_000)
    return None


def dai_timestamp_ns(packet: Any, *, device_clock: bool = False) -> int | None:
    if packet is None:
        return None
    try:
        timestamp = (
            packet.getTimestampDevice() if device_clock else packet.getTimestamp()
        )
    except Exception:
        return None
    return depthai_timedelta_ns(timestamp)


def dai_sequence_num(packet: Any) -> int | None:
    if packet is None:
        return None
    try:
        return int(packet.getSequenceNum())
    except Exception:
        return None


def estimate_capture_wall_timestamp_ns(
    *,
    frame_depthai_timestamp_ns: int | None,
    depthai_now_ns: int | None,
    host_wall_received_timestamp_ns: int,
) -> int:
    """Estimate frame wall-clock capture time from DepthAI's host-synced clock."""

    if frame_depthai_timestamp_ns is None or depthai_now_ns is None:
        return int(host_wall_received_timestamp_ns)
    receive_age_ns = int(depthai_now_ns) - int(frame_depthai_timestamp_ns)
    return int(host_wall_received_timestamp_ns) - receive_age_ns


def rgb_depth_timestamp_delta_ns(rgb_packet: Any, depth_packet: Any) -> int | None:
    rgb_timestamp_ns = dai_timestamp_ns(rgb_packet, device_clock=False)
    depth_timestamp_ns = dai_timestamp_ns(depth_packet, device_clock=False)
    if rgb_timestamp_ns is None or depth_timestamp_ns is None:
        return None
    return int(depth_timestamp_ns) - int(rgb_timestamp_ns)


def rgb_depth_pair_is_usable(
    rgb_packet: Any,
    depth_packet: Any,
    *,
    max_delta_ns: int = DEFAULT_RGB_DEPTH_DELTA_NS,
) -> bool:
    delta_ns = rgb_depth_timestamp_delta_ns(rgb_packet, depth_packet)
    if delta_ns is None:
        return False
    return abs(delta_ns) <= max_delta_ns


def camera_intrinsics_from_matrix(
    matrix: Any,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    depth_scale_to_mm: float = 1.0,
    distortion: tuple[float, ...] = (),
) -> CameraIntrinsics:
    values = np.asarray(matrix, dtype=float).reshape(-1)
    if values.size != 9:
        raise ValueError(f"Expected a 3x3 camera matrix, got {values.size} values")
    return CameraIntrinsics(
        cam_k=tuple(float(value) for value in values),
        width=int(width),
        height=int(height),
        distortion=tuple(float(value) for value in distortion),
        depth_scale_to_mm=float(depth_scale_to_mm),
    )


def _camera_intrinsics_from_depthai(
    device: Any,
    dai: Any,
    *,
    width: int,
    height: int,
) -> CameraIntrinsics:
    try:
        calibration = device.readCalibration()
        matrix = calibration.getCameraIntrinsics(
            dai.CameraBoardSocket.CAM_A, width, height
        )
        try:
            distortion = tuple(
                float(value)
                for value in calibration.getDistortionCoefficients(
                    dai.CameraBoardSocket.CAM_A
                )
            )
        except Exception:
            distortion = ()
    except Exception as exc:
        raise OAKDProCaptureError(
            f"Unable to read OAK-D Pro RGB calibration: {type(exc).__name__}: {exc}"
        ) from exc
    return camera_intrinsics_from_matrix(
        matrix,
        width=width,
        height=height,
        depth_scale_to_mm=1.0,
        distortion=distortion,
    )


def _device_id_from_device(device: Any, requested_device_id: str | None) -> str:
    for method_name in ("getMxId", "getDeviceId"):
        try:
            value = getattr(device, method_name)()
        except Exception:
            continue
        if value:
            return str(value)
    return requested_device_id or "default"


def write_oak_d_pro_rgbd_frame(
    output_path: str | Path,
    *,
    rgb_packet: Any,
    depth_packet: Any,
    sensor_id: str,
    frame_index: int,
    host_received_timestamp_ns: int,
    host_wall_received_timestamp_ns: int,
    depthai_now_ns: int | None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one DepthAI RGB/depth pair through the current folder contract."""

    rgb_timestamp_ns = dai_timestamp_ns(rgb_packet, device_clock=False)
    depth_timestamp_ns = dai_timestamp_ns(depth_packet, device_clock=False)
    rgb_device_timestamp_ns = dai_timestamp_ns(rgb_packet, device_clock=True)
    depth_device_timestamp_ns = dai_timestamp_ns(depth_packet, device_clock=True)
    capture_wall_timestamp_ns = estimate_capture_wall_timestamp_ns(
        frame_depthai_timestamp_ns=rgb_timestamp_ns,
        depthai_now_ns=depthai_now_ns,
        host_wall_received_timestamp_ns=host_wall_received_timestamp_ns,
    )
    delta_ns = (
        int(depth_timestamp_ns) - int(rgb_timestamp_ns)
        if rgb_timestamp_ns is not None and depth_timestamp_ns is not None
        else None
    )
    metadata: dict[str, Any] = {
        "timestamp_source": TIMESTAMP_SOURCE,
        "device_timestamp_ns": rgb_device_timestamp_ns,
        "depth_device_timestamp_ns": depth_device_timestamp_ns,
        "capture_wall_timestamp_ns": capture_wall_timestamp_ns,
        "host_wall_received_timestamp_ns": int(host_wall_received_timestamp_ns),
        "depthai_clock_now_ns": depthai_now_ns,
        "rgb_sequence_num": dai_sequence_num(rgb_packet),
        "depth_sequence_num": dai_sequence_num(depth_packet),
        "rgb_depth_timestamp_delta_ns": delta_ns,
    }
    if extra_metadata:
        metadata.update(dict(extra_metadata))

    return write_rgbd_frame(
        output_path,
        rgb_image=rgb_packet.getCvFrame(),
        depth_image=depth_packet.getFrame(),
        sensor_type=SensorType.OAK_D_PRO,
        sensor_id=sensor_id,
        frame_index=frame_index,
        sensor_timestamp_ns=rgb_timestamp_ns,
        depth_sensor_timestamp_ns=depth_timestamp_ns,
        host_received_timestamp_ns=host_received_timestamp_ns,
        host_wall_timestamp_ns=capture_wall_timestamp_ns,
        frame_stem=str(capture_wall_timestamp_ns // 1_000_000),
        extra_metadata=metadata,
    )


def _cv2_for_preview(cv2_module: Any | None) -> Any:
    if cv2_module is not None:
        return cv2_module
    try:
        import cv2
    except ImportError as exc:
        raise OAKDProCaptureError(
            "OpenCV is required for --preview but cv2 is not importable."
        ) from exc
    return cv2


def _open_device(dai: Any, device_id: str | None) -> Any:
    if not device_id:
        try:
            return dai.Device()
        except Exception as exc:
            raise OAKDProCaptureError(
                f"Unable to open OAK-D Pro device: {type(exc).__name__}: {exc}"
            ) from exc
    try:
        return dai.Device(device_id)
    except TypeError:
        try:
            return dai.Device(dai.DeviceInfo(device_id))
        except Exception as exc:
            raise OAKDProCaptureError(
                f"Unable to open OAK-D Pro device {device_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    except Exception as exc:
        raise OAKDProCaptureError(
            f"Unable to open OAK-D Pro device {device_id}: {type(exc).__name__}: {exc}"
        ) from exc


def _apply_lens_position(device: Any, dai: Any, color_camera: Any) -> None:
    try:
        calibration = device.readCalibration()
        lens_position = calibration.getLensPosition(dai.CameraBoardSocket.CAM_A)
    except Exception:
        return
    if lens_position is None:
        return
    try:
        color_camera.initialControl.setManualFocus(int(lens_position))
    except Exception:
        return


def _build_pipeline_outputs(
    pipeline: Any,
    device: Any,
    dai: Any,
    *,
    fps: int,
    width: int,
    height: int,
    max_rgb_depth_delta_ns: int,
) -> Any:
    color = pipeline.create(dai.node.Camera)
    left = pipeline.create(dai.node.Camera)
    right = pipeline.create(dai.node.Camera)
    stereo = pipeline.create(dai.node.StereoDepth)
    sync = pipeline.create(dai.node.Sync)

    color.build(dai.CameraBoardSocket.CAM_A, sensorFps=float(fps))
    left.build(dai.CameraBoardSocket.CAM_B, sensorFps=float(fps))
    right.build(dai.CameraBoardSocket.CAM_C, sensorFps=float(fps))
    _apply_lens_position(device, dai, color)

    stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.DENSITY)
    stereo.setLeftRightCheck(True)
    stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
    stereo.setOutputSize(width, height)

    rgb_output = color.requestOutput(
        (width, height),
        dai.ImgFrame.Type.BGR888i,
        dai.ImgResizeMode.STRETCH,
        float(fps),
        True,
    )
    left.requestOutput(
        (width, height),
        dai.ImgFrame.Type.GRAY8,
        dai.ImgResizeMode.STRETCH,
        float(fps),
    ).link(stereo.left)
    right.requestOutput(
        (width, height),
        dai.ImgFrame.Type.GRAY8,
        dai.ImgResizeMode.STRETCH,
        float(fps),
    ).link(stereo.right)
    rgb_output.link(stereo.inputAlignTo)

    sync.setRunOnHost(True)
    sync.setSyncThreshold(timedelta(microseconds=max_rgb_depth_delta_ns // 1_000))
    rgb_output.link(sync.inputs["rgb"])
    stereo.depth.link(sync.inputs["depth"])
    return sync.out.createOutputQueue()


def _frames_from_sync_message(message_group: Any) -> tuple[Any | None, Any | None]:
    try:
        return message_group["rgb"], message_group["depth"]
    except Exception:
        pass

    try:
        return message_group.get("rgb"), message_group.get("depth")
    except Exception:
        return None, None


def capture_oak_d_pro_rgbd(
    output_path: str | Path | None,
    *,
    device_id: str | None = None,
    fps: int = 6,
    max_frames: int = 0,
    warmup_frames: int = 0,
    preview: bool = False,
    record: bool = True,
    max_rgb_depth_delta_ns: int = DEFAULT_RGB_DEPTH_DELTA_NS,
    dai_module: Any | None = None,
    cv2_module: Any | None = None,
) -> dict[str, Any]:
    """Capture aligned OAK-D Pro RGB-D frames into the current folder contract."""

    if fps <= 0:
        raise ValueError("fps must be positive")
    if max_frames < 0:
        raise ValueError("max_frames must be greater than or equal to 0")
    if warmup_frames < 0:
        raise ValueError("warmup_frames must be greater than or equal to 0")
    if max_rgb_depth_delta_ns < 0:
        raise ValueError("max_rgb_depth_delta_ns must be greater than or equal to 0")
    if record and output_path is None:
        raise ValueError("output_path is required when record=True")

    output = Path(output_path) if output_path is not None else None
    dai = dai_module or _import_depthai()
    version = getattr(dai, "__version__", None)
    if not depthai_version_supported(version):
        found = version or "unknown"
        raise OAKDProCaptureError(
            f"DepthAI v3 is required for OAK-D Pro capture; found depthai {found}."
        )
    cv2_preview = _cv2_for_preview(cv2_module) if preview else None

    device = _open_device(dai, device_id)
    metadata_records: list[dict[str, Any]] = []
    sidecar_paths: dict[str, str] = {}
    captured_frames = 0
    valid_frames_seen = 0
    rejected_pairs = 0
    started_at_ns = time.time_ns()
    resolved_device_id = _device_id_from_device(device, device_id)

    try:
        if record and output is not None:
            ensure_rgbd_folders(output)

        with dai.Pipeline(device) as pipeline:
            output_queue = _build_pipeline_outputs(
                pipeline,
                device,
                dai,
                fps=fps,
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                max_rgb_depth_delta_ns=max_rgb_depth_delta_ns,
            )
            if record and output is not None:
                written = write_camera_sidecars(
                    output,
                    _camera_intrinsics_from_depthai(
                        device,
                        dai,
                        width=DEFAULT_WIDTH,
                        height=DEFAULT_HEIGHT,
                    ),
                )
                sidecar_paths = {key: path.name for key, path in written.items()}

            pipeline.start()
            while pipeline.isRunning() and (
                max_frames <= 0 or captured_frames < max_frames
            ):
                message_group = output_queue.get()
                host_wall_received_timestamp_ns = time.time_ns()
                host_received_timestamp_ns = time.monotonic_ns()
                depthai_now_ns = depthai_timedelta_ns(dai.Clock.now())
                rgb_packet, depth_packet = _frames_from_sync_message(message_group)
                if rgb_packet is None or depth_packet is None:
                    rejected_pairs += 1
                    continue
                if not rgb_depth_pair_is_usable(
                    rgb_packet,
                    depth_packet,
                    max_delta_ns=max_rgb_depth_delta_ns,
                ):
                    rejected_pairs += 1
                    continue

                rgb_image = rgb_packet.getCvFrame()
                key = -1
                if cv2_preview is not None:
                    cv2_preview.imshow("OAK-D Pro Capture RGB aligned", rgb_image)
                    key = cv2_preview.waitKey(1)

                if valid_frames_seen < warmup_frames:
                    valid_frames_seen += 1
                    if key & 0xFF == ord("q") or key == 27:
                        break
                    continue

                if record and output is not None:
                    metadata = write_oak_d_pro_rgbd_frame(
                        output,
                        rgb_packet=rgb_packet,
                        depth_packet=depth_packet,
                        sensor_id=resolved_device_id,
                        frame_index=captured_frames,
                        host_received_timestamp_ns=host_received_timestamp_ns,
                        host_wall_received_timestamp_ns=host_wall_received_timestamp_ns,
                        depthai_now_ns=depthai_now_ns,
                    )
                    metadata_records.append(metadata)

                captured_frames += 1
                valid_frames_seen += 1
                if key & 0xFF == ord("q") or key == 27:
                    break
            try:
                pipeline.stop()
                pipeline.wait()
            except Exception:
                pass
    except OAKDProCaptureError:
        raise
    except Exception as exc:
        raise OAKDProCaptureError(
            f"Unable to capture OAK-D Pro RGB-D frames: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if cv2_preview is not None:
            try:
                cv2_preview.destroyAllWindows()
            except Exception:
                pass
        try:
            device.close()
        except Exception:
            pass
        if record and output is not None:
            sync_frame_metadata(output)

    first_metadata = metadata_records[0] if metadata_records else {}
    last_metadata = metadata_records[-1] if metadata_records else {}
    return {
        "schema_version": CAPTURE_SUMMARY_SCHEMA_VERSION,
        "status": "succeeded",
        "sensor_type": SensorType.OAK_D_PRO.value,
        "sensor_id": resolved_device_id,
        "requested_device_id": device_id,
        "display_name": f"OAK-D Pro {resolved_device_id}".strip(),
        "depthai": depthai_version_status(version),
        "output_path": output.as_posix() if output is not None else None,
        "record": record,
        "preview": preview,
        "fps": fps,
        "max_frames": max_frames,
        "warmup_frames": warmup_frames,
        "max_rgb_depth_delta_ns": max_rgb_depth_delta_ns,
        "frame_count": captured_frames,
        "rejected_pairs": rejected_pairs,
        "sidecars": sidecar_paths,
        "first_frame_id": first_metadata.get("frame_id"),
        "last_frame_id": last_metadata.get("frame_id"),
        "first_sensor_timestamp_ns": first_metadata.get("sensor_timestamp_ns"),
        "last_sensor_timestamp_ns": last_metadata.get("sensor_timestamp_ns"),
        "started_at_ns": started_at_ns,
        "ended_at_ns": time.time_ns(),
    }


def summary_to_json(summary: Mapping[str, Any]) -> str:
    return json.dumps(dict(summary), indent=2, sort_keys=True)
