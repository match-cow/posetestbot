"""Testable Stereolabs ZED 2i aligned RGB-D capture adapter."""

from __future__ import annotations

import json
import time
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

CAPTURE_SUMMARY_SCHEMA_VERSION = "zed_2i_capture_summary.v1"
SUPPORTED_RESOLUTIONS = ("720p", "360p")


class ZED2iCaptureError(RuntimeError):
    """Raised when ZED 2i capture cannot start or complete safely."""


def _import_zed() -> Any:
    try:
        import pyzed.sl as sl
    except ImportError as exc:
        raise ZED2iCaptureError(
            "Stereolabs ZED SDK Python API is not importable. Install the ZED SDK "
            "and its pyzed module in the current uv environment."
        ) from exc
    return sl


def resolution_from_name(sl: Any, resolution: str) -> Any:
    if resolution == "720p":
        return sl.RESOLUTION.HD720
    if resolution == "360p":
        return sl.RESOLUTION.VGA
    raise ValueError(
        f"resolution must be one of {', '.join(SUPPORTED_RESOLUTIONS)}; got {resolution!r}"
    )


def _cv2_for_preview(cv2_module: Any | None) -> Any:
    if cv2_module is not None:
        return cv2_module
    try:
        import cv2
    except ImportError as exc:
        raise ZED2iCaptureError(
            "OpenCV is required for ZED preview but cv2 is not importable."
        ) from exc
    return cv2


def _camera_info(zed: Any) -> Any:
    try:
        return zed.get_camera_information()
    except Exception as exc:
        raise ZED2iCaptureError(
            f"Unable to read ZED camera information: {type(exc).__name__}: {exc}"
        ) from exc


def _resolved_serial(camera_info: Any, requested_device_id: str | None) -> str:
    for owner in (
        camera_info,
        getattr(camera_info, "camera_configuration", None),
    ):
        value = getattr(owner, "serial_number", None) if owner is not None else None
        if value not in {None, "", 0}:
            return str(value)
    return requested_device_id or "default"


def camera_intrinsics_from_zed(camera_info: Any) -> CameraIntrinsics:
    try:
        configuration = camera_info.camera_configuration
        left = configuration.calibration_parameters.left_cam
        resolution = configuration.resolution
        distortion = tuple(float(value) for value in getattr(left, "disto", ()))
        return CameraIntrinsics(
            cam_k=(
                float(left.fx),
                0.0,
                float(left.cx),
                0.0,
                float(left.fy),
                float(left.cy),
                0.0,
                0.0,
                1.0,
            ),
            width=int(resolution.width),
            height=int(resolution.height),
            distortion=distortion,
            depth_scale_to_mm=1.0,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ZED2iCaptureError(f"Invalid ZED camera calibration: {exc}") from exc


def _timestamp_ns(zed: Any, sl: Any) -> int | None:
    try:
        return int(zed.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds())
    except Exception:
        return None


def _rgb_bgr(image: Any, cv2_module: Any) -> np.ndarray:
    rgb = np.asarray(image.get_data())
    if rgb.ndim != 3 or rgb.shape[2] not in {3, 4}:
        raise ZED2iCaptureError(f"ZED left image has invalid shape: {rgb.shape}")
    if rgb.dtype != np.uint8:
        raise ZED2iCaptureError(f"ZED left image must be uint8; got {rgb.dtype}")
    if rgb.shape[2] == 4:
        rgb = cv2_module.cvtColor(rgb, cv2_module.COLOR_BGRA2BGR)
    return np.ascontiguousarray(rgb)


def _depth_uint16(depth: Any) -> np.ndarray:
    values = np.asarray(depth.get_data())
    if values.ndim != 2:
        raise ZED2iCaptureError(f"ZED depth image has invalid shape: {values.shape}")
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    return np.ascontiguousarray(
        np.clip(values, 0, np.iinfo(np.uint16).max).astype(np.uint16)
    )


def capture_zed_2i_rgbd(
    output_path: str | Path | None,
    *,
    device_id: str | None = None,
    fps: int = 30,
    max_frames: int = 0,
    warmup_frames: int = 0,
    resolution: str = "720p",
    preview: bool = False,
    record: bool = True,
    sl_module: Any | None = None,
    cv2_module: Any | None = None,
) -> dict[str, Any]:
    """Capture left RGB and aligned millimetre depth through shared writers."""

    if fps <= 0:
        raise ValueError("fps must be positive")
    if max_frames < 0:
        raise ValueError("max_frames must be greater than or equal to 0")
    if warmup_frames < 0:
        raise ValueError("warmup_frames must be greater than or equal to 0")
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise ValueError(
            f"resolution must be one of {', '.join(SUPPORTED_RESOLUTIONS)}"
        )
    if record and output_path is None:
        raise ValueError("output_path is required when record=True")
    requested_serial: int | None = None
    if device_id:
        try:
            requested_serial = int(device_id)
        except ValueError as exc:
            raise ValueError("ZED device_id must be a numeric serial number") from exc
        if requested_serial <= 0:
            raise ValueError("ZED device_id must be a positive serial number")

    output = Path(output_path) if output_path is not None else None
    sl = sl_module or _import_zed()
    cv2 = _cv2_for_preview(cv2_module) if preview else (cv2_module or __import__("cv2"))
    init_parameters = sl.InitParameters()
    init_parameters.camera_resolution = resolution_from_name(sl, resolution)
    init_parameters.camera_fps = fps
    init_parameters.coordinate_units = sl.UNIT.MILLIMETER
    init_parameters.depth_mode = sl.DEPTH_MODE.NEURAL
    if requested_serial is not None:
        try:
            init_parameters.set_from_serial_number(requested_serial)
        except Exception as exc:
            raise ZED2iCaptureError(
                f"Unable to select ZED serial {device_id}: {type(exc).__name__}: {exc}"
            ) from exc

    zed = sl.Camera()
    metadata_records: list[dict[str, Any]] = []
    sidecar_paths: dict[str, str] = {}
    captured_frames = 0
    valid_frames_seen = 0
    grab_failures = 0
    last_frame_stem_ms: int | None = None
    started_at_ns = time.time_ns()
    resolved_device_id = device_id or "default"
    try:
        status = zed.open(init_parameters)
        if status != sl.ERROR_CODE.SUCCESS:
            raise ZED2iCaptureError(f"Could not open ZED camera: {status}")
        camera_info = _camera_info(zed)
        resolved_device_id = _resolved_serial(camera_info, device_id)
        intrinsics = camera_intrinsics_from_zed(camera_info)
        if record and output is not None:
            ensure_rgbd_folders(output)
            written = write_camera_sidecars(output, intrinsics)
            sidecar_paths = {key: path.name for key, path in written.items()}

        runtime_parameters = sl.RuntimeParameters()
        image = sl.Mat()
        depth = sl.Mat()
        while max_frames <= 0 or captured_frames < max_frames:
            if zed.grab(runtime_parameters) != sl.ERROR_CODE.SUCCESS:
                grab_failures += 1
                continue
            host_wall_timestamp_ns = time.time_ns()
            host_received_timestamp_ns = time.monotonic_ns()
            zed.retrieve_image(image, sl.VIEW.LEFT)
            zed.retrieve_measure(depth, sl.MEASURE.DEPTH)
            rgb_image = _rgb_bgr(image, cv2)
            depth_image = _depth_uint16(depth)
            if rgb_image.shape[:2] != depth_image.shape:
                raise ZED2iCaptureError(
                    "ZED RGB and depth dimensions differ: "
                    f"{rgb_image.shape[:2]} != {depth_image.shape}"
                )
            valid_frames_seen += 1
            if valid_frames_seen <= warmup_frames:
                continue
            key = -1
            if preview:
                cv2.imshow("ZED 2i Capture RGB aligned", rgb_image)
                key = cv2.waitKey(1)
            if record and output is not None:
                sensor_timestamp_ns = _timestamp_ns(zed, sl)
                frame_stem_ms = host_wall_timestamp_ns // 1_000_000
                if last_frame_stem_ms is not None:
                    frame_stem_ms = max(frame_stem_ms, last_frame_stem_ms + 1)
                metadata = write_rgbd_frame(
                    output,
                    rgb_image=rgb_image,
                    depth_image=depth_image,
                    sensor_type=SensorType.ZED_2I,
                    sensor_id=resolved_device_id,
                    frame_index=captured_frames,
                    sensor_timestamp_ns=sensor_timestamp_ns,
                    host_received_timestamp_ns=host_received_timestamp_ns,
                    host_wall_timestamp_ns=host_wall_timestamp_ns,
                    frame_stem=str(frame_stem_ms),
                    extra_metadata={
                        "timestamp_source": "zed_image_and_host",
                        "requested_device_id": device_id,
                        "resolution": resolution,
                    },
                )
                metadata_records.append(metadata)
                last_frame_stem_ms = frame_stem_ms
            captured_frames += 1
            if key & 0xFF == ord("q") or key == 27:
                break
    except ZED2iCaptureError:
        raise
    except Exception as exc:
        raise ZED2iCaptureError(
            f"Unable to capture ZED 2i RGB-D frames: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            zed.close()
        except Exception:
            pass
        if preview:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
        if record and output is not None:
            sync_frame_metadata(output)

    first = metadata_records[0] if metadata_records else {}
    last = metadata_records[-1] if metadata_records else {}
    return {
        "schema_version": CAPTURE_SUMMARY_SCHEMA_VERSION,
        "status": "succeeded",
        "sensor_type": SensorType.ZED_2I.value,
        "sensor_id": resolved_device_id,
        "requested_device_id": device_id,
        "display_name": f"ZED 2i {resolved_device_id}",
        "output_path": output.as_posix() if output is not None else None,
        "record": record,
        "preview": preview,
        "fps": fps,
        "max_frames": max_frames,
        "warmup_frames": warmup_frames,
        "resolution": resolution,
        "frame_count": captured_frames,
        "valid_frames_seen": valid_frames_seen,
        "grab_failures": grab_failures,
        "sidecars": sidecar_paths,
        "first_frame_id": first.get("frame_id"),
        "last_frame_id": last.get("frame_id"),
        "first_sensor_timestamp_ns": first.get("sensor_timestamp_ns"),
        "last_sensor_timestamp_ns": last.get("sensor_timestamp_ns"),
        "started_at_ns": started_at_ns,
        "ended_at_ns": time.time_ns(),
    }


def summary_to_json(summary: Mapping[str, Any]) -> str:
    return json.dumps(dict(summary), indent=2, sort_keys=True)
