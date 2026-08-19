"""Shared RGB-D frame writing helpers for capture adapters."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from posetestbot.io.atomic import atomic_write_json, atomic_write_text
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


SCHEMA_VERSION = "frame_metadata.v1"


def ensure_rgbd_folders(output_path: str | Path) -> Path:
    """Create the current RGB-D capture folder shape."""

    output = Path(output_path)
    (output / RGB_DIR).mkdir(parents=True, exist_ok=True)
    (output / DEPTH_DIR).mkdir(parents=True, exist_ok=True)
    return output


def append_frame_metadata(output_path: str | Path, metadata: Mapping[str, Any]) -> Path:
    """Append and expose one compact JSONL metadata record for a captured frame.

    Closing the handle keeps the complete record visible to the live capture
    supervisor without imposing a storage durability barrier on every frame.
    Capture adapters call :func:`sync_frame_metadata` once during shutdown.
    """

    output = Path(output_path)
    metadata_path = output / FRAME_METADATA_JSONL
    line = json.dumps(dict(metadata), separators=(",", ":"), allow_nan=False) + "\n"
    previous_size = metadata_path.stat().st_size if metadata_path.exists() else 0
    try:
        with open(metadata_path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
    # Preserve the last complete JSONL record even when capture is interrupted.
    except BaseException:
        if metadata_path.exists():
            with open(metadata_path, "r+b") as handle:
                handle.truncate(previous_size)
        raise
    return metadata_path


def sync_frame_metadata(output_path: str | Path) -> Path | None:
    """Apply the deferred durability barrier to committed frame metadata."""

    metadata_path = Path(output_path) / FRAME_METADATA_JSONL
    if not metadata_path.is_file():
        return None
    with open(metadata_path, "a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    return metadata_path


def frame_stem_from_host_wall_ns(host_wall_timestamp_ns: int) -> str:
    """Return the millisecond timestamp filename stem."""

    return str(int(round(host_wall_timestamp_ns / 1_000_000)))


def _sensor_type_value(sensor_type: SensorType | str) -> str:
    return (
        sensor_type.value if isinstance(sensor_type, SensorType) else str(sensor_type)
    )


def _write_png(path: Path, image: Any) -> None:
    if not cv2.imwrite(path.as_posix(), image):
        raise OSError(f"Failed to write image: {path}")


def _validate_rgbd_images(
    rgb_image: Any, depth_image: Any
) -> tuple[np.ndarray, np.ndarray]:
    rgb = np.asarray(rgb_image)
    depth = np.asarray(depth_image)
    if rgb.ndim != 3 or rgb.shape[2] not in {3, 4}:
        raise ValueError("rgb_image must have shape (height, width, 3|4)")
    if rgb.dtype != np.uint8:
        raise ValueError("rgb_image must use uint8 pixels")
    if depth.ndim != 2:
        raise ValueError("depth_image must have shape (height, width)")
    if depth.dtype != np.uint16:
        raise ValueError("depth_image must use uint16 pixels")
    if rgb.shape[:2] != depth.shape:
        raise ValueError("RGB and depth image dimensions must match")
    if not rgb.size or not depth.size:
        raise ValueError("RGB and depth images must not be empty")
    return rgb, depth


def _temporary_png_path(path: Path) -> Path:
    return path.with_name(f".{path.stem}.{uuid.uuid4().hex}.png")


def write_camera_sidecars(
    output_path: str | Path,
    intrinsics: CameraIntrinsics,
    *,
    include_distortion_in_cam_k: bool = False,
) -> dict[str, Path]:
    """Write current camera sidecars shared by calibration and BOP export."""

    output = Path(output_path)
    output.mkdir(parents=True, exist_ok=True)
    if intrinsics.width <= 0 or intrinsics.height <= 0:
        raise ValueError("Camera intrinsic width and height must be positive")
    if len(intrinsics.cam_k) != 9 or not all(
        np.isfinite(value) for value in intrinsics.cam_k
    ):
        raise ValueError("Camera intrinsic matrix must contain 9 finite values")
    if (
        not np.isfinite(intrinsics.depth_scale_to_mm)
        or intrinsics.depth_scale_to_mm <= 0
    ):
        raise ValueError("Camera depth scale must be finite and positive")
    cam_k = [float(value) for value in intrinsics.cam_k]
    matrix_rows = intrinsics.as_matrix_rows()

    cam_k_path = output / CAM_K
    depth_scale_path = output / DEPTH_SCALE
    camera_json_path = output / CAMERA_JSON
    camera_data_path = output / CAMERA_DATA_JSON
    paths = [cam_k_path, depth_scale_path, camera_json_path, camera_data_path]
    existing = [path for path in paths if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite camera sidecar(s): "
            + ", ".join(path.as_posix() for path in existing)
        )

    cam_k_text = "".join(f"{row[0]} {row[1]} {row[2]}\n" for row in matrix_rows)
    if include_distortion_in_cam_k and intrinsics.distortion:
        cam_k_text += (
            " ".join(str(float(value)) for value in intrinsics.distortion) + "\n"
        )
    camera_payload: dict[str, Any] = {
        "cam_K": cam_k,
        "depth_scale": float(intrinsics.depth_scale_to_mm),
    }
    camera_data_payload: dict[str, Any] = {
        "K": [[float(value) for value in row] for row in matrix_rows],
        "resolution": [int(intrinsics.height), int(intrinsics.width)],
    }
    if intrinsics.distortion or intrinsics.projection_source is not None:
        distortion_evidence = {
            "distortion": [float(value) for value in intrinsics.distortion],
            "distortion_model": intrinsics.distortion_model,
            "projection_source": intrinsics.projection_source,
        }
        camera_payload.update(distortion_evidence)
        camera_data_payload.update(distortion_evidence)

    created: list[Path] = []
    try:
        created.append(atomic_write_text(cam_k_path, cam_k_text))
        created.append(
            atomic_write_text(
                depth_scale_path, f"{float(intrinsics.depth_scale_to_mm)}\n"
            )
        )
        created.append(
            atomic_write_json(
                camera_json_path,
                camera_payload,
                indent=4,
                sort_keys=False,
            )
        )
        created.append(
            atomic_write_json(
                camera_data_path,
                camera_data_payload,
                indent=None,
                sort_keys=False,
            )
        )
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    return {
        CAM_K: cam_k_path,
        DEPTH_SCALE: depth_scale_path,
        CAMERA_JSON: camera_json_path,
        CAMERA_DATA_JSON: camera_data_path,
    }


def write_rgbd_frame(
    output_path: str | Path,
    *,
    rgb_image: Any,
    depth_image: Any,
    sensor_type: SensorType | str,
    sensor_id: str,
    frame_index: int,
    sensor_timestamp_ns: int | None,
    host_received_timestamp_ns: int,
    host_wall_timestamp_ns: int | None = None,
    depth_sensor_timestamp_ns: int | None = None,
    frame_stem: str | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one RGB-D pair and append its metadata sidecar record."""

    rgb, depth = _validate_rgbd_images(rgb_image, depth_image)
    if frame_index < 0:
        raise ValueError("frame_index must be greater than or equal to 0")
    if not sensor_id:
        raise ValueError("sensor_id must not be empty")
    output = ensure_rgbd_folders(output_path)
    wall_timestamp = (
        host_wall_timestamp_ns if host_wall_timestamp_ns is not None else time.time_ns()
    )
    stem = frame_stem or frame_stem_from_host_wall_ns(wall_timestamp)
    if not stem.isdigit():
        raise ValueError("frame_stem must contain only digits")
    frame_id = f"{stem}.png"
    rgb_path = output / RGB_DIR / frame_id
    depth_path = output / DEPTH_DIR / frame_id
    metadata_path = output / FRAME_METADATA_JSONL
    if rgb_path.exists() or depth_path.exists():
        raise FileExistsError(f"Refusing to overwrite RGB-D frame {frame_id}")

    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sensor_type": _sensor_type_value(sensor_type),
        "sensor_id": sensor_id,
        "frame_index": int(frame_index),
        "frame_id": frame_id,
        "rgb_path": f"{RGB_DIR}/{frame_id}",
        "depth_path": f"{DEPTH_DIR}/{frame_id}",
        "sensor_timestamp_ns": sensor_timestamp_ns,
        "host_received_timestamp_ns": int(host_received_timestamp_ns),
        "host_wall_timestamp_ns": int(wall_timestamp),
    }
    if depth_sensor_timestamp_ns is not None:
        metadata["depth_sensor_timestamp_ns"] = int(depth_sensor_timestamp_ns)
    if extra_metadata:
        overlaps = sorted(set(extra_metadata) & set(metadata))
        if overlaps:
            raise ValueError(
                "extra_metadata may not override core field(s): " + ", ".join(overlaps)
            )
        metadata.update(dict(extra_metadata))

    rgb_temporary = _temporary_png_path(rgb_path)
    depth_temporary = _temporary_png_path(depth_path)
    created: list[Path] = []
    try:
        _write_png(rgb_temporary, rgb)
        _write_png(depth_temporary, depth)
        os.replace(rgb_temporary, rgb_path)
        created.append(rgb_path)
        os.replace(depth_temporary, depth_path)
        created.append(depth_path)
        append_frame_metadata(output, metadata)
    # Cleanup must also run for control-flow exceptions such as KeyboardInterrupt.
    # Capture processes may receive a shutdown signal while committing a frame;
    # leaving only one member of the RGB/depth/metadata tuple makes the raw run
    # fail its integrity gate.
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        rgb_temporary.unlink(missing_ok=True)
        depth_temporary.unlink(missing_ok=True)

    if not metadata_path.is_file():
        raise OSError(f"Frame metadata was not written: {metadata_path}")
    return metadata


def write_aligned_rgbd_frame(
    output_path: str | Path,
    frame: AlignedRgbdFrame,
    *,
    host_wall_timestamp_ns: int | None = None,
    frame_stem: str | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write an `AlignedRgbdFrame` through the current capture contract."""

    metadata = dict(frame.exposure_metadata)
    if extra_metadata:
        metadata.update(dict(extra_metadata))
    return write_rgbd_frame(
        output_path,
        rgb_image=frame.rgb_image,
        depth_image=frame.depth_image_aligned_to_rgb,
        sensor_type=frame.sensor_type,
        sensor_id=frame.sensor_id,
        frame_index=frame.frame_index,
        sensor_timestamp_ns=frame.sensor_timestamp_ns,
        host_received_timestamp_ns=frame.host_received_timestamp_ns,
        host_wall_timestamp_ns=host_wall_timestamp_ns,
        frame_stem=frame_stem,
        extra_metadata=metadata,
    )
