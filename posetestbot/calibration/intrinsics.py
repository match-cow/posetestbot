"""Native and alpha=0 rectified camera intrinsic calibration profiles."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from posetestbot.calibration.targets import (
    opencv_grid_board,
    target_identity,
    validate_target_identity,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
    CAM_K,
    CAMERA_DATA_JSON,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
)


SCHEMA_VERSION = "intrinsic_calibration.v1"
DEFAULT_MIN_ACCEPTED_VIEWS = 15
DEFAULT_MIN_COVERAGE_CELLS = 6
DEFAULT_MAX_VIEW_ERROR_PX = 3.0
DEFAULT_MAX_RMS_PX = 1.5
OPENCV_FORWARD_DISTORTION_MODELS = frozenset(
    {"none", "brown_conrady", "modified_brown_conrady"}
)
ZERO_DISTORTION_PINHOLE_EQUIVALENT_MODELS = frozenset({"inverse_brown_conrady"})


class IntrinsicCalibrationError(ValueError):
    """Quality-gate failure carrying the rejected-view audit."""

    def __init__(self, message: str, report: Mapping[str, Any]):
        super().__init__(message)
        self.report = dict(report)


def _read_json(path: Path) -> Any:
    with open(path, "r") as file:
        return json.load(file)


def _camera_matrix(path: Path) -> tuple[np.ndarray, np.ndarray]:
    rows = [line.split() for line in path.read_text().splitlines() if line.strip()]
    if len(rows) < 3 or any(len(row) != 3 for row in rows[:3]):
        raise ValueError(f"Camera matrix must start with three 3-value rows: {path}")
    matrix = np.asarray(rows[:3], dtype=np.float64)
    distortion = np.asarray(rows[3], dtype=np.float64) if len(rows) > 3 else np.zeros(5)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"Camera matrix must be finite: {path}")
    if distortion.size not in {0, 4, 5, 8, 12, 14} or not np.all(
        np.isfinite(distortion)
    ):
        raise ValueError(f"Camera distortion is invalid: {path}")
    padded = np.zeros(5, dtype=np.float64)
    padded[: min(5, distortion.size)] = distortion.reshape(-1)[:5]
    return matrix, padded


def _sensor_metadata(sensor_folder: Path) -> tuple[str, str, tuple[int, int]]:
    camera_data_path = sensor_folder / CAMERA_DATA_JSON
    if not camera_data_path.is_file():
        raise FileNotFoundError(f"Missing current camera sidecar: {camera_data_path}")
    camera_data = _read_json(camera_data_path)
    if not isinstance(camera_data, Mapping):
        raise ValueError("camera_data.json must be an object")
    resolution = camera_data.get("resolution")
    if not isinstance(resolution, list) or len(resolution) != 2:
        raise ValueError("camera_data.json requires a two-value resolution")
    height, width = int(resolution[0]), int(resolution[1])
    if height <= 0 or width <= 0:
        raise ValueError("camera_data.json resolution must be positive")
    metadata_path = sensor_folder / FRAME_METADATA_JSONL
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Missing current frame metadata: {metadata_path}")
    records = [line for line in metadata_path.read_text().splitlines() if line.strip()]
    if not records:
        raise ValueError("frame_metadata.jsonl must contain at least one record")
    record = json.loads(records[0])
    if (
        not isinstance(record, Mapping)
        or record.get("schema_version") != "frame_metadata.v1"
    ):
        raise ValueError("frame_metadata.jsonl does not contain current frame metadata")
    sensor_id = str(record.get("sensor_id") or "")
    orientation = str(record.get("orientation") or "normal")
    if not sensor_id:
        raise ValueError("frame metadata requires sensor_id")
    if orientation not in {"normal", "inverted"}:
        raise ValueError(f"Unsupported sensor orientation: {orientation!r}")
    return sensor_id, orientation, (width, height)


def sensor_intrinsic_identity(
    sensor_folder: str | Path,
) -> tuple[str, str, tuple[int, int]]:
    """Return serial, orientation, and (width, height) from captured sidecars."""

    return _sensor_metadata(Path(sensor_folder))


def _rectified_projection(
    native_k: np.ndarray,
    distortion: np.ndarray,
    image_size: tuple[int, int],
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    matrix, roi = cv2.getOptimalNewCameraMatrix(
        native_k,
        distortion,
        image_size,
        0.0,
        image_size,
        centerPrincipalPoint=False,
    )
    return np.asarray(matrix, dtype=np.float64), tuple(int(item) for item in roi)


def _projection_dict(
    matrix: np.ndarray,
    distortion: np.ndarray,
    image_size: tuple[int, int],
    *,
    distortion_model: str = "brown_conrady",
    valid_roi: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    value = {
        "cam_K": matrix.reshape(-1).astype(float).tolist(),
        "width": image_size[0],
        "height": image_size[1],
        "distortion_model": distortion_model,
        "distortion": distortion.reshape(-1).astype(float).tolist(),
    }
    if valid_roi is not None:
        value.update({"alpha": 0.0, "valid_roi": list(valid_roi)})
    return value


def projection_is_opencv_compatible(projection: Mapping[str, Any]) -> bool:
    """Whether coefficients can be used as a forward OpenCV projection.

    A nonzero inverse-distortion model must never be passed to OpenCV as a
    forward model.  Exactly zero coefficients are the model-invariant
    exception: both forward and inverse mappings reduce to the same pinhole
    projection.
    """

    model = str(projection.get("distortion_model", "brown_conrady"))
    if model in OPENCV_FORWARD_DISTORTION_MODELS:
        return True
    try:
        distortion = np.asarray(projection.get("distortion"), dtype=float).reshape(-1)
    except (TypeError, ValueError):
        return False
    return bool(
        model in ZERO_DISTORTION_PINHOLE_EQUIVALENT_MODELS
        and distortion.size > 0
        and np.all(np.isfinite(distortion))
        and np.all(distortion == 0.0)
    )


def _captured_distortion(
    folder: Path,
    cam_k_distortion: np.ndarray,
) -> tuple[np.ndarray, str, str | None]:
    camera_data_path = folder / CAMERA_DATA_JSON
    if not camera_data_path.is_file():
        raise FileNotFoundError(f"Missing current camera sidecar: {camera_data_path}")
    camera_data = _read_json(camera_data_path)
    if not isinstance(camera_data, Mapping):
        raise ValueError("camera_data.json must be an object")
    raw = camera_data.get("distortion")
    if isinstance(raw, list):
        values = np.asarray(raw, dtype=np.float64).reshape(-1)
        if values.size not in {0, 4, 5, 8, 12, 14} or not np.all(np.isfinite(values)):
            raise ValueError("Captured SDK distortion coefficients are invalid")
        distortion = np.zeros(5, dtype=np.float64)
        distortion[: min(5, values.size)] = values[:5]
    else:
        distortion = cam_k_distortion
    model = str(camera_data.get("distortion_model") or "brown_conrady")
    if model == "none" and np.any(np.abs(distortion) > 0.0):
        raise ValueError("Captured distortion model 'none' has nonzero coefficients")
    projection_source = camera_data.get("projection_source")
    return (
        distortion,
        model,
        (str(projection_source) if projection_source is not None else None),
    )


def _profile_id(
    sensor_id: str, image_size: tuple[int, int], orientation: str, mode: str
) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", sensor_id).strip("_")
    return f"{safe_id}_{image_size[0]}x{image_size[1]}_{orientation}_{mode}"


def factory_intrinsic_profile(sensor_folder: str | Path) -> dict[str, Any]:
    """Wrap captured SDK color intrinsics without claiming depth recalibration."""

    folder = Path(sensor_folder)
    matrix, cam_k_distortion = _camera_matrix(folder / CAM_K)
    distortion, distortion_model, projection_source = _captured_distortion(
        folder, cam_k_distortion
    )
    sensor_id, orientation, image_size = _sensor_metadata(folder)
    depth_scale_path = folder / DEPTH_SCALE
    if not depth_scale_path.is_file():
        raise FileNotFoundError(
            f"Missing current depth-scale sidecar: {depth_scale_path}"
        )
    depth_scale = float(depth_scale_path.read_text().strip())
    if not math.isfinite(depth_scale) or depth_scale <= 0:
        raise ValueError("Factory depth scale must be finite and positive")
    forward_model_compatible = distortion_model in OPENCV_FORWARD_DISTORTION_MODELS
    zero_distortion_model_invariant = bool(
        distortion_model in ZERO_DISTORTION_PINHOLE_EQUIVALENT_MODELS
        and np.all(distortion == 0.0)
    )
    opencv_compatible = forward_model_compatible or zero_distortion_model_invariant
    if opencv_compatible:
        rectified_k, roi = _rectified_projection(matrix, distortion, image_size)
        rectified: dict[str, Any] | None = _projection_dict(
            rectified_k,
            np.zeros(5),
            image_size,
            valid_roi=roi,
        )
    else:
        # Preserve nonzero inverse/non-pinhole coefficients as evidence, but
        # never feed them into OpenCV's forward Brown-Conrady projection.
        rectified = None
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": _profile_id(sensor_id, image_size, orientation, "factory"),
        "sensor_id": sensor_id,
        "sensor_name": folder.name,
        "resolution": list(image_size),
        "orientation": orientation,
        "native": _projection_dict(
            matrix,
            distortion,
            image_size,
            distortion_model=distortion_model,
        ),
        "rectified": rectified,
        "source": {
            "mode": "factory",
            "camera_projection": "captured_sdk_color_intrinsics",
            "sidecars_complete": True,
            "sdk_distortion_model": distortion_model,
            "sdk_projection_source": projection_source,
            "opencv_projection_compatible": opencv_compatible,
            "opencv_projection_compatibility_basis": (
                "forward_opencv_distortion_model"
                if forward_model_compatible
                else (
                    "exact_zero_distortion_is_model_invariant"
                    if zero_distortion_model_invariant
                    else None
                )
            ),
            "rectification_available": opencv_compatible,
            "rectification_unavailable_reason": (
                None
                if opencv_compatible
                else "sdk_distortion_model_is_not_forward_opencv_compatible"
            ),
        },
        "depth": {
            "scale_to_mm": depth_scale,
            "scale_source": "factory_sdk",
            "alignment": {
                "projection": "depth_aligned_to_color",
                "source": "capture_adapter_sdk",
                "recalibrated": False,
            },
        },
        "quality": {
            "status": "factory",
            "accepted_view_count": 0,
            "coverage_cells": [],
            "rms_reprojection_error_px": None,
            "rejected_views": [],
        },
    }


def _detection_frames(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frames = value.get("frames")
    if not isinstance(frames, Mapping):
        raise ValueError("aruco_detections.json must contain a frames object")
    return frames


def _view_points(
    frame: Mapping[str, Any],
    board: Any,
) -> tuple[np.ndarray, np.ndarray] | None:
    ids = frame.get("ids")
    corners = frame.get("corners")
    if (
        not isinstance(ids, list)
        or not isinstance(corners, list)
        or len(ids) != len(corners)
        or not ids
    ):
        return None
    corner_arrays = [
        np.asarray(item, dtype=np.float32).reshape(1, 4, 2) for item in corners
    ]
    object_points, image_points = board.matchImagePoints(
        corner_arrays,
        np.asarray(ids, dtype=np.int32).reshape(-1, 1),
    )
    if object_points is None or image_points is None or len(object_points) < 4:
        return None
    return (
        np.asarray(object_points, dtype=np.float32).reshape(-1, 3),
        np.asarray(image_points, dtype=np.float32).reshape(-1, 2),
    )


def _calibrate(
    object_points: Sequence[np.ndarray],
    image_points: Sequence[np.ndarray],
    image_size: tuple[int, int],
    seed_k: np.ndarray,
    seed_distortion: np.ndarray,
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    result = cv2.calibrateCameraExtended(
        list(object_points),
        list(image_points),
        image_size,
        seed_k.copy(),
        seed_distortion.copy(),
        flags=cv2.CALIB_USE_INTRINSIC_GUESS,
    )
    rms, matrix, distortion = result[:3]
    per_view_errors = np.asarray(result[-1], dtype=float).reshape(-1)
    return (
        float(rms),
        np.asarray(matrix),
        np.asarray(distortion).reshape(-1)[:5],
        per_view_errors,
    )


def calibrate_intrinsic_profile(
    sensor_folder: str | Path,
    detections: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    min_accepted_views: int = DEFAULT_MIN_ACCEPTED_VIEWS,
    min_coverage_cells: int = DEFAULT_MIN_COVERAGE_CELLS,
    max_view_error_px: float = DEFAULT_MAX_VIEW_ERROR_PX,
    max_rms_px: float = DEFAULT_MAX_RMS_PX,
) -> dict[str, Any]:
    """Calibrate color projection from matched GridBoard corners with hard gates."""

    if min_accepted_views < 1 or not 1 <= min_coverage_cells <= 9:
        raise ValueError("Intrinsic view/coverage thresholds are invalid")
    folder = Path(sensor_folder)
    factory = factory_intrinsic_profile(folder)
    seed_k = np.asarray(factory["native"]["cam_K"], dtype=float).reshape(3, 3)
    seed_distortion = (
        np.asarray(factory["native"]["distortion"], dtype=float)
        if projection_is_opencv_compatible(factory["native"])
        else np.zeros(5, dtype=float)
    )
    image_size = tuple(int(item) for item in factory["resolution"])
    _dictionary, board = opencv_grid_board(target)
    validate_target_identity(detections.get("target"), target, label="ArUco detections")

    accepted_names: list[str] = []
    objects: list[np.ndarray] = []
    images: list[np.ndarray] = []
    rejected: list[dict[str, Any]] = []
    frames = _detection_frames(detections)
    for frame_name, raw_frame in sorted(frames.items()):
        if not isinstance(raw_frame, Mapping):
            rejected.append({"frame": frame_name, "reason": "invalid_detection_record"})
            continue
        points = _view_points(raw_frame, board)
        if points is None:
            rejected.append(
                {"frame": frame_name, "reason": "insufficient_matched_grid_corners"}
            )
            continue
        objects.append(points[0])
        images.append(points[1])
        accepted_names.append(str(frame_name))

    if len(objects) < min_accepted_views:
        message = f"Intrinsic calibration has {len(objects)} usable views; requires {min_accepted_views}"
        raise IntrinsicCalibrationError(
            message,
            {
                "status": "rejected",
                "reason": message,
                "accepted_views": accepted_names,
                "rejected_views": rejected,
            },
        )
    rms, matrix, distortion, errors = _calibrate(
        objects, images, image_size, seed_k, seed_distortion
    )
    keep = errors <= max_view_error_px
    for name, error, accepted in zip(accepted_names, errors, keep, strict=True):
        if not accepted:
            rejected.append(
                {
                    "frame": name,
                    "reason": "per_view_reprojection_error",
                    "error_px": float(error),
                }
            )
    if not np.all(keep):
        objects = [
            value for value, accepted in zip(objects, keep, strict=True) if accepted
        ]
        images = [
            value for value, accepted in zip(images, keep, strict=True) if accepted
        ]
        accepted_names = [
            value
            for value, accepted in zip(accepted_names, keep, strict=True)
            if accepted
        ]
        if len(objects) < min_accepted_views:
            message = (
                f"Intrinsic calibration has {len(objects)} accepted views after "
                f"reprojection filtering; requires {min_accepted_views}"
            )
            raise IntrinsicCalibrationError(
                message,
                {
                    "status": "rejected",
                    "reason": message,
                    "accepted_views": accepted_names,
                    "rejected_views": rejected,
                },
            )
        rms, matrix, distortion, errors = _calibrate(
            objects, images, image_size, matrix, distortion
        )

    coverage_cells = sorted(
        {
            min(2, int(float(np.mean(points[:, 0])) * 3 / image_size[0]))
            + 3 * min(2, int(float(np.mean(points[:, 1])) * 3 / image_size[1]))
            for points in images
        }
    )
    gate_failures = []
    if len(coverage_cells) < min_coverage_cells:
        gate_failures.append(
            f"coverage {len(coverage_cells)}/9 is below {min_coverage_cells}/9"
        )
    if rms > max_rms_px:
        gate_failures.append(f"final RMS {rms:.3f}px exceeds {max_rms_px:.3f}px")
    if np.any(errors > max_view_error_px):
        gate_failures.append("refined per-view reprojection error exceeds threshold")
    if gate_failures:
        message = "Intrinsic calibration quality gate failed: " + "; ".join(
            gate_failures
        )
        raise IntrinsicCalibrationError(
            message,
            {
                "status": "rejected",
                "reason": message,
                "accepted_views": accepted_names,
                "coverage_cells": coverage_cells,
                "rms_reprojection_error_px": rms,
                "per_view_reprojection_error_px": {
                    name: float(error)
                    for name, error in zip(accepted_names, errors, strict=True)
                },
                "rejected_views": rejected,
            },
        )

    rectified_k, roi = _rectified_projection(matrix, distortion, image_size)
    target_source = target.get("generator_source", {})
    return {
        **factory,
        "profile_id": _profile_id(
            factory["sensor_id"], image_size, factory["orientation"], "aruco"
        ),
        "native": _projection_dict(
            matrix, distortion, image_size, distortion_model="brown_conrady"
        ),
        "rectified": _projection_dict(
            rectified_k, np.zeros(5), image_size, valid_roi=roi
        ),
        "source": {
            "mode": "calibrate",
            "algorithm": "cv2.calibrateCameraExtended",
            "seed": factory["profile_id"],
            "seed_distortion_used": projection_is_opencv_compatible(factory["native"]),
            "target_schema_version": target.get("schema_version"),
            "target_sha256": target_source.get("sha256")
            if isinstance(target_source, Mapping)
            else None,
            "target": target_identity(target),
        },
        "quality": {
            "status": "accepted",
            "accepted_view_count": len(accepted_names),
            "accepted_views": accepted_names,
            "coverage_cells": coverage_cells,
            "rms_reprojection_error_px": rms,
            "per_view_reprojection_error_px": {
                name: float(error)
                for name, error in zip(accepted_names, errors, strict=True)
            },
            "rejected_views": rejected,
            "thresholds": {
                "min_accepted_views": min_accepted_views,
                "min_coverage_cells": min_coverage_cells,
                "max_view_error_px": max_view_error_px,
                "max_rms_px": max_rms_px,
            },
        },
    }


def validate_intrinsic_profile(profile: Mapping[str, Any]) -> None:
    if profile.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Intrinsic schema must be {SCHEMA_VERSION!r}")
    resolution = profile.get("resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(int(item) <= 0 for item in resolution)
    ):
        raise ValueError("Intrinsic resolution must be [width, height]")
    if profile.get("orientation") not in {"normal", "inverted"}:
        raise ValueError("Intrinsic orientation must be normal or inverted")
    for projection_name in ("native",):
        projection = profile.get(projection_name)
        if not isinstance(projection, Mapping):
            raise ValueError(f"Intrinsic {projection_name} projection is required")
        matrix = projection.get("cam_K")
        if (
            not isinstance(matrix, list)
            or len(matrix) != 9
            or not all(math.isfinite(float(item)) for item in matrix)
        ):
            raise ValueError(
                f"Intrinsic {projection_name}.cam_K must contain 9 finite values"
            )
        distortion = projection.get("distortion")
        if (
            not isinstance(distortion, list)
            or len(distortion) != 5
            or not all(math.isfinite(float(item)) for item in distortion)
        ):
            raise ValueError(
                f"Intrinsic {projection_name}.distortion must contain 5 finite values"
            )
    rectified = profile.get("rectified")
    if rectified is None:
        source = profile.get("source")
        if (
            not isinstance(source, Mapping)
            or source.get("rectification_available") is not False
        ):
            raise ValueError(
                "Missing rectified projection requires explicit unavailable provenance"
            )
        return
    if not isinstance(rectified, Mapping):
        raise ValueError("Intrinsic rectified projection must be an object or null")
    matrix = rectified.get("cam_K")
    if (
        not isinstance(matrix, list)
        or len(matrix) != 9
        or not all(math.isfinite(float(item)) for item in matrix)
    ):
        raise ValueError("Intrinsic rectified.cam_K must contain 9 finite values")
    distortion = rectified.get("distortion")
    if (
        not isinstance(distortion, list)
        or len(distortion) != 5
        or not all(math.isfinite(float(item)) for item in distortion)
    ):
        raise ValueError("Intrinsic rectified.distortion must contain 5 finite values")
    if any(float(item) != 0.0 for item in distortion):
        raise ValueError("Rectified distortion must be zero")
    roi = rectified.get("valid_roi")
    if not isinstance(roi, list) or len(roi) != 4 or any(int(item) < 0 for item in roi):
        raise ValueError("Rectified valid_roi must contain four nonnegative integers")
    x, y, width, height = (int(item) for item in roi)
    if x + width > int(resolution[0]) or y + height > int(resolution[1]):
        raise ValueError("Rectified valid_roi must fit output resolution")


def write_intrinsic_profile_collection(
    profiles: Sequence[Mapping[str, Any]], path: str | Path
) -> Path:
    values = [dict(profile) for profile in profiles]
    for profile in values:
        validate_intrinsic_profile(profile)
    keys = [
        (item["sensor_id"], tuple(item["resolution"]), item["orientation"])
        for item in values
    ]
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Intrinsic collection has duplicate serial/resolution/orientation profiles"
        )
    return atomic_write_json(
        Path(path), {"schema_version": SCHEMA_VERSION, "profiles": values}
    )


def load_intrinsic_profile_collection(path: str | Path) -> list[dict[str, Any]]:
    value = _read_json(Path(path))
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Intrinsic collection schema must be {SCHEMA_VERSION!r}")
    profiles = value.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("Intrinsic collection profiles must be a list")
    for profile in profiles:
        if not isinstance(profile, Mapping):
            raise ValueError("Intrinsic profiles must be objects")
        validate_intrinsic_profile(profile)
    return [dict(item) for item in profiles]


def select_intrinsic_profile(
    profiles: Sequence[Mapping[str, Any]],
    *,
    sensor_id: str,
    resolution: tuple[int, int],
    orientation: str,
) -> dict[str, Any]:
    matches = [
        dict(profile)
        for profile in profiles
        if str(profile.get("sensor_id")) == sensor_id
        and tuple(profile.get("resolution", [])) == resolution
        and str(profile.get("orientation")) == orientation
    ]
    if len(matches) != 1:
        raise ValueError(
            "Expected exactly one intrinsic profile matching "
            f"serial={sensor_id}, resolution={resolution}, orientation={orientation}; found {len(matches)}"
        )
    return matches[0]
