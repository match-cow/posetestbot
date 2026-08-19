"""Single-pass ArUco GridBoard detection helpers for calibration attempts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from posetestbot.calibration.targets import (
    normalize_calibration_target_spec,
    opencv_grid_board,
    target_identity,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import ARUCO_DETECTIONS, RGB_DIR


DETECTION_SCHEMA_VERSION = "aruco_detections.v1"


def _image_paths(sensor_folder: Path) -> list[Path]:
    rgb = sensor_folder / RGB_DIR
    if not rgb.is_dir():
        raise FileNotFoundError(f"Missing synchronized RGB folder: {rgb}")
    paths = sorted(
        path
        for path in rgb.iterdir()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if not paths:
        raise FileNotFoundError(f"No RGB frames found: {rgb}")
    return paths


def _target_provenance(target: Mapping[str, Any]) -> dict[str, Any]:
    source = target.get("generator_source")
    return {
        **target_identity(target),
        "schema_version": target.get("schema_version"),
        "dictionary": target.get("dictionary"),
        "grid_size": target.get("grid_size"),
        "marker_length_mm": target.get("marker_length"),
        "marker_separation_mm": target.get("marker_separation"),
        "generator_sha256": (
            source.get("sha256") if isinstance(source, Mapping) else None
        ),
    }


def detect_sensor_folder(
    sensor_folder: str | Path,
    target: Mapping[str, Any],
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Detect target markers once from native synchronized color frames."""

    folder = Path(sensor_folder)
    normalized = normalize_calibration_target_spec(target)
    dictionary, _board = opencv_grid_board(normalized)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    allowed_ids = {int(marker["id"]) for marker in normalized["markers"]}
    frames: dict[str, Any] = {}
    image_size: list[int] | None = None
    for image_path in _image_paths(folder):
        image = cv2.imread(image_path.as_posix(), cv2.IMREAD_COLOR)
        if image is None:
            frames[image_path.name] = {
                "ids": [],
                "corners": [],
                "rejected_reason": "unreadable_image",
            }
            continue
        height, width = image.shape[:2]
        current_size = [width, height]
        if image_size is None:
            image_size = current_size
        elif image_size != current_size:
            raise ValueError(
                f"Mixed RGB resolutions in {folder}: {image_size} and {current_size}"
            )
        corners, ids, rejected = detector.detectMarkers(image)
        matched: list[tuple[int, np.ndarray]] = []
        if ids is not None:
            matched = [
                (int(marker_id), np.asarray(corner, dtype=float).reshape(4, 2))
                for marker_id, corner in zip(ids.reshape(-1), corners, strict=True)
                if int(marker_id) in allowed_ids
            ]
        matched.sort(key=lambda item: item[0])
        all_points = (
            np.concatenate([item[1] for item in matched], axis=0) if matched else None
        )
        frames[image_path.name] = {
            "ids": [item[0] for item in matched],
            "corners": [item[1].tolist() for item in matched],
            "marker_count": len(matched),
            "image_centroid_px": (
                all_points.mean(axis=0).tolist() if all_points is not None else None
            ),
            "rejected_candidate_count": len(rejected),
        }
    report = {
        "schema_version": DETECTION_SCHEMA_VERSION,
        "sensor_name": folder.name,
        "source_projection": "synchronized_native_rgb",
        "image_size": image_size,
        "target": _target_provenance(normalized),
        "frame_count": len(frames),
        "detected_frame_count": sum(
            1 for frame in frames.values() if frame["marker_count"] > 0
        ),
        "frames": frames,
    }
    atomic_write_json(
        Path(output_path) if output_path else folder / ARUCO_DETECTIONS,
        report,
    )
    return report


def _matched_points(
    frame: Mapping[str, Any], board: Any
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return current marker correspondences for attempt-owned PnP solving."""

    ids = frame.get("ids")
    corners = frame.get("corners")
    if (
        not isinstance(ids, list)
        or not isinstance(corners, list)
        or not ids
        or len(ids) != len(corners)
    ):
        return None
    object_points, image_points = board.matchImagePoints(
        [np.asarray(corner, dtype=np.float32).reshape(1, 4, 2) for corner in corners],
        np.asarray(ids, dtype=np.int32).reshape(-1, 1),
    )
    if object_points is None or len(object_points) < 4:
        return None
    return (
        np.asarray(object_points).reshape(-1, 3),
        np.asarray(image_points).reshape(-1, 2),
    )
