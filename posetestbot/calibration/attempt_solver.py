"""Deterministic multi-method solving for intent-level calibration attempts.

This module owns the current intent-level calculation path. It
operates on explicit observations and returns JSON-ready evidence so an attempt
can be kept immutable until an operator promotes a selected result.
"""

from __future__ import annotations

import math
from itertools import combinations
from statistics import mean, median
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from pytransform3d import transformations as pt

from posetestbot.calibration.transforms import (
    average_transform,
    invert_transform,
    is_finite_transform,
    residual_summary,
    robot_ee_to_reference,
    transform_from_record,
    transform_record,
    transform_residual,
)


PNP_METHODS: dict[str, int] = {
    "IPPE": cv2.SOLVEPNP_IPPE,
    "ITERATIVE": cv2.SOLVEPNP_ITERATIVE,
    "SQPNP": cv2.SOLVEPNP_SQPNP,
}
HAND_EYE_METHODS: dict[str, int] = {
    "tsai": cv2.CALIB_HAND_EYE_TSAI,
    "park": cv2.CALIB_HAND_EYE_PARK,
    "horaud": cv2.CALIB_HAND_EYE_HORAUD,
    "andreff": cv2.CALIB_HAND_EYE_ANDREFF,
    "daniilidis": cv2.CALIB_HAND_EYE_DANIILIDIS,
}
ROBOT_WORLD_HAND_EYE_METHODS: dict[str, int] = {
    "shah": cv2.CALIB_ROBOT_WORLD_HAND_EYE_SHAH,
    "li": cv2.CALIB_ROBOT_WORLD_HAND_EYE_LI,
}
EXTRINSIC_METHOD_ORDER = (
    "tsai",
    "park",
    "horaud",
    "andreff",
    "daniilidis",
    "shah",
    "li",
)
PNP_METHOD_ORDER = ("IPPE", "ITERATIVE", "SQPNP")

DEFAULT_MIN_INLIERS = 6
DEFAULT_MAX_MEAN_TRANSLATION_MM = 10.0
DEFAULT_MAX_MEAN_ROTATION_DEG = 5.0
DEFAULT_MAX_OUTLIER_RATIO = 0.25
DEFAULT_MIN_PNP_COMMON_INLIERS = 12
DEFAULT_MIN_PNP_COMMON_INLIER_RATIO = 0.5
DEFAULT_MAX_PNP_ALL_POINT_MEAN_ERROR_PX = 3.0
DEFAULT_MIN_PNP_SUPPORTED_MARKERS = 4
DEFAULT_MIN_PNP_SUPPORTED_CORNERS_PER_MARKER = 3
DEFAULT_MIN_PNP_GRID_ROWS = 2
DEFAULT_MIN_PNP_GRID_COLUMNS = 2
DEFAULT_MIN_PNP_CLUTTER_SUPPORTED_MARKERS = 8
DEFAULT_MIN_PNP_CLUTTER_GRID_ROWS = 3
DEFAULT_MIN_PNP_CLUTTER_GRID_COLUMNS = 3
DEFAULT_MIN_ROTATION_AXIS_ANGLE_DEG = 2.0
DEFAULT_MIN_ROTATION_AXIS_SINGULAR_RATIO = 0.15
DEFAULT_MAX_OBSERVATIONS_PER_MOTION = 5
DEFAULT_IMAGE_COVERAGE_TAIL_SUPPORT_VIEWS = 5
DEFAULT_MIN_IMAGE_CENTROID_X_SPAN_RATIO = 0.45
DEFAULT_MIN_IMAGE_CENTROID_Y_SPAN_RATIO = 0.35
DEFAULT_MIN_IMAGE_CENTROID_HULL_AREA_RATIO = 0.10
DEFAULT_STATIC_MIN_IMAGE_CENTROID_X_SPAN_RATIO = 0.15
DEFAULT_STATIC_MIN_IMAGE_CENTROID_Y_SPAN_RATIO = 0.20
DEFAULT_STATIC_MIN_IMAGE_CENTROID_HULL_AREA_RATIO = 0.03


def _transform(rotation: Any, translation: Any) -> np.ndarray:
    result = pt.transform_from(
        np.asarray(rotation, dtype=float).reshape(3, 3),
        np.asarray(translation, dtype=float).reshape(3),
    )
    if not is_finite_transform(result):
        raise ValueError("solver produced a non-finite transform")
    return result


def _motion_balanced_validation(
    observations: Sequence[Mapping[str, Any]],
    residuals: Sequence[Mapping[str, float]],
    *,
    max_translation_mm: float,
    max_rotation_deg: float,
) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, float]]] = {}
    for observation, residual in zip(observations, residuals, strict=True):
        grouped.setdefault(_pose_key(observation), []).append(residual)
    per_motion: dict[str, dict[str, Any]] = {}
    for pose_key, values in grouped.items():
        mask = [
            item["translation_mm"] <= max_translation_mm
            and item["rotation_deg"] <= max_rotation_deg
            for item in values
        ]
        per_motion[pose_key] = {
            "observation_count": len(values),
            "inlier_count": sum(mask),
            "outlier_count": len(mask) - sum(mask),
            "outlier_ratio": (len(mask) - sum(mask)) / len(mask),
            "residuals": residual_summary(values),
        }
    balanced_outlier_ratio = float(
        mean(item["outlier_ratio"] for item in per_motion.values())
    )
    repeated_motion_ratios = [
        float(item["outlier_ratio"])
        for item in per_motion.values()
        if int(item["observation_count"]) >= 4
    ]
    return {
        "per_motion": per_motion,
        "motion_balanced_mean_translation_mm": float(
            mean(
                item["residuals"]["mean_translation_mm"] for item in per_motion.values()
            )
        ),
        "motion_balanced_mean_rotation_deg": float(
            mean(item["residuals"]["mean_rotation_deg"] for item in per_motion.values())
        ),
        "motion_balanced_outlier_ratio": balanced_outlier_ratio,
        "max_repeated_motion_outlier_ratio": max(repeated_motion_ratios, default=0.0),
    }


def _common_pnp_inliers(
    object_points: np.ndarray,
    image_points: np.ndarray,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> np.ndarray:
    """Return a robust mask without using a degenerate planar PnP sample.

    OpenCV's PnP RANSAC uses a minimal PnP kernel even when the requested
    final method is iterative.  That kernel can be unstable for a coplanar
    calibration board and return a tiny consensus set despite an accurate
    direct planar fit.  A plane-to-image homography is the appropriate robust
    model here.  Image points are undistorted back into pixel coordinates so
    the RANSAC threshold retains its four-pixel meaning.
    """

    centered_objects = object_points - np.mean(object_points, axis=0)
    _left, singular_values, plane_axes = np.linalg.svd(
        centered_objects,
        full_matrices=False,
    )
    rank_tolerance = (
        np.finfo(np.float64).eps
        * max(centered_objects.shape)
        * max(float(singular_values[0]), 1.0)
    )
    if float(singular_values[1]) <= rank_tolerance:
        raise ValueError("robust planar PnP requires non-collinear object points")
    planarity_tolerance = max(
        1e-6,
        float(singular_values[0]) * 1e-6,
    )
    if float(singular_values[2]) > planarity_tolerance:
        raise ValueError("robust planar PnP requires coplanar object points")
    plane_points = centered_objects @ plane_axes[:2].T

    undistorted_pixels = cv2.undistortPoints(
        image_points.reshape(-1, 1, 2),
        camera_matrix,
        distortion if distortion.size else None,
        P=camera_matrix,
    ).reshape(-1, 2)
    if not np.all(np.isfinite(undistorted_pixels)):
        raise ValueError("robust planar PnP received non-finite image points")
    homography, inliers = cv2.findHomography(
        plane_points,
        undistorted_pixels,
        method=cv2.RANSAC,
        ransacReprojThreshold=4.0,
        maxIters=2000,
        confidence=0.999,
    )
    if homography is None or inliers is None or not np.all(np.isfinite(homography)):
        raise ValueError("robust planar PnP could not find a common inlier set")
    indices = np.flatnonzero(np.asarray(inliers, dtype=np.uint8).reshape(-1))
    if len(indices) < 4:
        raise ValueError("robust planar PnP found fewer than four common inliers")
    return indices


def solve_planar_pnp_candidates(
    object_points: Any,
    image_points: Any,
    camera_matrix: Any,
    distortion: Any,
    *,
    methods: Sequence[str] = PNP_METHOD_ORDER,
    min_common_inliers: int = DEFAULT_MIN_PNP_COMMON_INLIERS,
    min_common_inlier_ratio: float = DEFAULT_MIN_PNP_COMMON_INLIER_RATIO,
    max_all_point_mean_error_px: float = (DEFAULT_MAX_PNP_ALL_POINT_MEAN_ERROR_PX),
    point_marker_ids: Any | None = None,
    point_grid_indices: Any | None = None,
    min_supported_markers: int = DEFAULT_MIN_PNP_SUPPORTED_MARKERS,
    min_supported_corners_per_marker: int = (
        DEFAULT_MIN_PNP_SUPPORTED_CORNERS_PER_MARKER
    ),
    min_grid_rows: int = DEFAULT_MIN_PNP_GRID_ROWS,
    min_grid_columns: int = DEFAULT_MIN_PNP_GRID_COLUMNS,
    min_clutter_supported_markers: int = (DEFAULT_MIN_PNP_CLUTTER_SUPPORTED_MARKERS),
    min_clutter_grid_rows: int = DEFAULT_MIN_PNP_CLUTTER_GRID_ROWS,
    min_clutter_grid_columns: int = DEFAULT_MIN_PNP_CLUTTER_GRID_COLUMNS,
) -> dict[str, Any]:
    """Compare supported planar PnP algorithms with one robust point mask."""

    object_array = np.asarray(object_points, dtype=np.float64).reshape(-1, 3)
    image_array = np.asarray(image_points, dtype=np.float64).reshape(-1, 2)
    matrix = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
    distortion_array = np.asarray(distortion, dtype=np.float64).reshape(-1)
    if len(object_array) != len(image_array) or len(object_array) < 4:
        raise ValueError("PnP requires at least four paired object/image points")
    if not all(method in PNP_METHODS for method in methods):
        raise ValueError("Unsupported PnP method subset")
    if min_common_inliers < 4:
        raise ValueError("PnP minimum common inliers must be at least four")
    if not 0.0 < min_common_inlier_ratio <= 1.0:
        raise ValueError("PnP minimum common inlier ratio must be in (0, 1]")
    if max_all_point_mean_error_px <= 0.0:
        raise ValueError("PnP whole-board reprojection threshold must be positive")
    if (point_marker_ids is None) != (point_grid_indices is None):
        raise ValueError("PnP marker IDs and grid indices must be provided together")
    marker_ids: np.ndarray | None = None
    grid_indices: np.ndarray | None = None
    if point_marker_ids is not None:
        marker_ids = np.asarray(point_marker_ids, dtype=np.int64).reshape(-1)
        grid_indices = np.asarray(point_grid_indices, dtype=np.int64).reshape(-1, 2)
        if len(marker_ids) != len(object_array) or len(grid_indices) != len(
            object_array
        ):
            raise ValueError(
                "PnP marker/grid metadata must align with point correspondences"
            )
        if min_supported_markers < 1 or min_supported_corners_per_marker < 1:
            raise ValueError("PnP marker support thresholds must be positive")
        if min_grid_rows < 1 or min_grid_columns < 1:
            raise ValueError("PnP grid span thresholds must be positive")
        if (
            min_clutter_supported_markers < min_supported_markers
            or min_clutter_grid_rows < min_grid_rows
            or min_clutter_grid_columns < min_grid_columns
        ):
            raise ValueError(
                "PnP clutter-consensus thresholds must be no weaker than the "
                "ordinary spatial-support thresholds"
            )
    common_indices = _common_pnp_inliers(
        object_array, image_array, matrix, distortion_array
    )
    raw_common_inlier_ratio = float(len(common_indices) / len(object_array))
    supported_marker_ids: list[int] = []
    supported_grid_rows: list[int] = []
    supported_grid_columns: list[int] = []
    marker_corner_counts: dict[str, int] = {}
    spatial_support_available = marker_ids is not None and grid_indices is not None
    marker_detection_counts: dict[str, int] = {}
    duplicate_marker_ids: list[int] = []
    unique_marker_correspondence_capacity = len(object_array)
    duplicate_marker_clutter = False
    if spatial_support_available:
        assert marker_ids is not None
        assert grid_indices is not None
        marker_point_counts = {
            marker_id: int(np.count_nonzero(marker_ids == marker_id))
            for marker_id in sorted({int(value) for value in marker_ids})
        }
        if any(count % 4 != 0 for count in marker_point_counts.values()):
            raise ValueError(
                "PnP marker metadata does not contain four-corner detections"
            )
        marker_detection_counts = {
            str(marker_id): count // 4
            for marker_id, count in marker_point_counts.items()
        }
        duplicate_marker_ids = [
            marker_id for marker_id, count in marker_point_counts.items() if count > 4
        ]
        duplicate_marker_clutter = bool(duplicate_marker_ids)
        unique_marker_correspondence_capacity = 4 * len(marker_point_counts)
        for marker_id in sorted({int(value) for value in marker_ids[common_indices]}):
            count = int(np.count_nonzero(marker_ids[common_indices] == marker_id))
            marker_corner_counts[str(marker_id)] = count
            if count >= min_supported_corners_per_marker:
                supported_marker_ids.append(marker_id)
        supported_mask = np.isin(marker_ids, supported_marker_ids)
        supported_grid_rows = sorted(
            {int(value) for value in grid_indices[supported_mask, 0]}
        )
        supported_grid_columns = sorted(
            {int(value) for value in grid_indices[supported_mask, 1]}
        )
    spatial_support_ok = not spatial_support_available or (
        len(supported_marker_ids) >= min_supported_markers
        and len(supported_grid_rows) >= min_grid_rows
        and len(supported_grid_columns) >= min_grid_columns
    )
    clutter_consensus_support_ok = not duplicate_marker_clutter or (
        len(supported_marker_ids) >= min_clutter_supported_markers
        and len(supported_grid_rows) >= min_clutter_grid_rows
        and len(supported_grid_columns) >= min_clutter_grid_columns
    )
    common_inlier_ratio_basis = (
        "unique_marker_correspondence_capacity"
        if duplicate_marker_clutter
        else "all_detected_correspondences"
    )
    common_inlier_ratio = float(
        min(
            1.0,
            len(common_indices) / max(1, unique_marker_correspondence_capacity),
        )
        if duplicate_marker_clutter
        else raw_common_inlier_ratio
    )
    consensus_image_centroid_px = (
        np.mean(image_array[common_indices], axis=0).astype(float).tolist()
    )
    support_evidence = {
        "correspondence_count": int(len(object_array)),
        "common_inlier_count": int(len(common_indices)),
        "common_inlier_ratio": common_inlier_ratio,
        "raw_common_inlier_ratio": raw_common_inlier_ratio,
        "common_inlier_ratio_basis": common_inlier_ratio_basis,
        "unique_marker_correspondence_capacity": int(
            unique_marker_correspondence_capacity
        ),
        "duplicate_marker_clutter_filtered": duplicate_marker_clutter,
        "duplicate_marker_ids": duplicate_marker_ids,
        "marker_detection_counts": marker_detection_counts,
        "ignored_clutter_correspondence_count": (
            int(len(object_array) - len(common_indices))
            if duplicate_marker_clutter
            else 0
        ),
        "consensus_image_centroid_px": consensus_image_centroid_px,
        "spatial_support_available": spatial_support_available,
        "supported_marker_ids": supported_marker_ids,
        "supported_marker_count": len(supported_marker_ids),
        "supported_marker_corner_counts": marker_corner_counts,
        "supported_grid_rows": supported_grid_rows,
        "supported_grid_columns": supported_grid_columns,
        "thresholds": {
            "min_common_inliers": int(min_common_inliers),
            "min_common_inlier_ratio": float(min_common_inlier_ratio),
            "max_all_point_mean_reprojection_error_px": float(
                max_all_point_mean_error_px
            ),
            "min_supported_markers": int(min_supported_markers),
            "min_supported_corners_per_marker": int(min_supported_corners_per_marker),
            "min_grid_rows": int(min_grid_rows),
            "min_grid_columns": int(min_grid_columns),
            "min_clutter_supported_markers": int(min_clutter_supported_markers),
            "min_clutter_grid_rows": int(min_clutter_grid_rows),
            "min_clutter_grid_columns": int(min_clutter_grid_columns),
        },
    }
    if (
        len(common_indices) < min_common_inliers
        or common_inlier_ratio < min_common_inlier_ratio
        or not spatial_support_ok
        or not clutter_consensus_support_ok
    ):
        if not clutter_consensus_support_ok:
            reason = "insufficient_duplicate_marker_clutter_consensus"
        elif len(common_indices) < min_common_inliers or (
            common_inlier_ratio < min_common_inlier_ratio
        ):
            reason = "insufficient_common_pnp_support"
        else:
            reason = "insufficient_spatial_pnp_support"
        return {
            "common_inlier_indices": common_indices.astype(int).tolist(),
            **support_evidence,
            "candidates": [],
            "selected": {},
            "failures": [
                {
                    "reason": reason,
                    **support_evidence,
                }
            ],
        }
    inlier_objects = object_array[common_indices]
    inlier_images = image_array[common_indices]
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for method in methods:
        try:
            result = cv2.solvePnPGeneric(
                inlier_objects,
                inlier_images,
                matrix,
                distortion_array,
                flags=PNP_METHODS[method],
            )
            success = bool(result[0])
            rvecs = result[1] if len(result) > 1 else ()
            tvecs = result[2] if len(result) > 2 else ()
            if not success or not rvecs or not tvecs:
                raise ValueError("no pose hypotheses")
            method_candidates = []
            for hypothesis_index, (raw_rvec, raw_tvec) in enumerate(
                zip(rvecs, tvecs, strict=True)
            ):
                rvec = np.asarray(raw_rvec, dtype=np.float64).reshape(3, 1)
                tvec = np.asarray(raw_tvec, dtype=np.float64).reshape(3, 1)
                rvec, tvec = cv2.solvePnPRefineLM(
                    inlier_objects,
                    inlier_images,
                    matrix,
                    distortion_array,
                    rvec,
                    tvec,
                )
                rotation, _ = cv2.Rodrigues(rvec)
                pose = _transform(rotation, tvec)
                depths = (rotation @ object_array.T + tvec).T[:, 2]
                if not np.all(np.isfinite(depths)):
                    raise ValueError("non-finite camera depths")
                if float(np.min(depths)) <= 0.0:
                    failures.append(
                        {
                            "method": method,
                            "hypothesis": str(hypothesis_index),
                            "reason": "non_cheiral_pose",
                        }
                    )
                    continue
                projected, _ = cv2.projectPoints(
                    object_array,
                    rvec,
                    tvec,
                    matrix,
                    distortion_array,
                )
                errors = np.linalg.norm(
                    projected.reshape(-1, 2) - image_array,
                    axis=1,
                )
                if not np.all(np.isfinite(errors)):
                    raise ValueError("non-finite reprojection errors")
                quality_indices = (
                    common_indices
                    if duplicate_marker_clutter
                    else np.arange(len(object_array), dtype=np.int64)
                )
                quality_mean_reprojection_error_px = float(
                    np.mean(errors[quality_indices])
                )
                item = {
                    "method": method,
                    "hypothesis": hypothesis_index,
                    "selected_for_method": False,
                    "refinement": "solvePnPRefineLM",
                    "common_inlier_indices": common_indices.astype(int).tolist(),
                    "common_inlier_count": int(len(common_indices)),
                    "mean_reprojection_error_px": float(
                        np.mean(errors[common_indices])
                    ),
                    "max_reprojection_error_px": float(np.max(errors[common_indices])),
                    "all_point_mean_reprojection_error_px": float(np.mean(errors)),
                    "all_point_max_reprojection_error_px": float(np.max(errors)),
                    "quality_reprojection_scope": (
                        "homography_consensus_target_instance"
                        if duplicate_marker_clutter
                        else "all_detected_correspondences"
                    ),
                    "quality_mean_reprojection_error_px": (
                        quality_mean_reprojection_error_px
                    ),
                    "duplicate_marker_clutter_filtered": (duplicate_marker_clutter),
                    "ignored_clutter_correspondence_count": support_evidence[
                        "ignored_clutter_correspondence_count"
                    ],
                    "transform": transform_record(
                        pose,
                        from_frame="aruco_grid",
                        to_frame="camera",
                    ),
                }
                item["quality_status"] = (
                    "accepted"
                    if quality_mean_reprojection_error_px <= max_all_point_mean_error_px
                    else "rejected"
                )
                method_candidates.append(item)
            if not method_candidates:
                raise ValueError("all pose hypotheses were non-cheiral")
            method_candidates.sort(
                key=lambda item: (
                    item["mean_reprojection_error_px"],
                    item["hypothesis"],
                )
            )
            accepted_candidates = [
                item
                for item in method_candidates
                if item["quality_status"] == "accepted"
            ]
            if accepted_candidates:
                accepted_candidates[0]["selected_for_method"] = True
            else:
                best = method_candidates[0]
                failures.append(
                    {
                        "method": method,
                        "reason": (
                            "target_instance_consensus_reprojection_error"
                            if duplicate_marker_clutter
                            else "whole_board_reprojection_error"
                        ),
                        "quality_reprojection_scope": best[
                            "quality_reprojection_scope"
                        ],
                        "quality_mean_reprojection_error_px": best[
                            "quality_mean_reprojection_error_px"
                        ],
                        "all_point_mean_reprojection_error_px": best[
                            "all_point_mean_reprojection_error_px"
                        ],
                        "max_all_point_mean_reprojection_error_px": float(
                            max_all_point_mean_error_px
                        ),
                    }
                )
            candidates.extend(method_candidates)
        except (cv2.error, ValueError, TypeError) as exc:
            failures.append({"method": method, "reason": str(exc)})

    selected = {
        item["method"]: item for item in candidates if item["selected_for_method"]
    }
    return {
        "common_inlier_indices": common_indices.astype(int).tolist(),
        **support_evidence,
        "candidates": candidates,
        "selected": selected,
        "failures": failures,
    }


def _observation_transforms(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    robot: list[np.ndarray] = []
    target_camera: list[np.ndarray] = []
    for observation in observations:
        robot_pose = observation.get("robot_ee_pose")
        target_pose = observation.get("target_to_camera")
        if not isinstance(robot_pose, Mapping) or not isinstance(target_pose, Mapping):
            raise ValueError("observation requires robot and target-camera transforms")
        robot.append(robot_ee_to_reference(robot_pose))
        target_camera.append(transform_from_record(target_pose))
    return robot, target_camera


def _calibrate_hand_eye(
    robot: Sequence[np.ndarray],
    target_camera: Sequence[np.ndarray],
    *,
    mode: str,
    method: str,
) -> tuple[np.ndarray, np.ndarray]:
    if mode == "eye_in_hand":
        gripper_to_base = list(robot)
    elif mode == "eye_to_hand":
        gripper_to_base = [invert_transform(item) for item in robot]
    else:
        raise ValueError("mode must be eye_in_hand or eye_to_hand")
    rotation, translation = cv2.calibrateHandEye(
        [item[:3, :3] for item in gripper_to_base],
        [item[:3, 3] for item in gripper_to_base],
        [item[:3, :3] for item in target_camera],
        [item[:3, 3] for item in target_camera],
        method=HAND_EYE_METHODS[method],
    )
    primary = _transform(rotation, translation)
    if mode == "eye_in_hand":
        companions = [
            flange_to_base @ primary @ target_to_camera
            for flange_to_base, target_to_camera in zip(
                robot, target_camera, strict=True
            )
        ]
    else:
        companions = [
            invert_transform(flange_to_base) @ primary @ target_to_camera
            for flange_to_base, target_to_camera in zip(
                robot, target_camera, strict=True
            )
        ]
    return primary, average_transform(companions)


def _calibrate_robot_world_hand_eye(
    robot: Sequence[np.ndarray],
    target_camera: Sequence[np.ndarray],
    *,
    mode: str,
    method: str,
) -> tuple[np.ndarray, np.ndarray]:
    base_to_flange = [invert_transform(item) for item in robot]
    if mode == "eye_in_hand":
        world_to_camera = list(target_camera)
    elif mode == "eye_to_hand":
        # Relabel the fixed camera as OpenCV's world and the moving target as
        # OpenCV's camera.  Inverting both returned constants then gives the
        # requested camera->base and target->flange transforms.
        world_to_camera = [invert_transform(item) for item in target_camera]
    else:
        raise ValueError("mode must be eye_in_hand or eye_to_hand")
    base_to_world_r, base_to_world_t, gripper_to_camera_r, gripper_to_camera_t = (
        cv2.calibrateRobotWorldHandEye(
            [item[:3, :3] for item in world_to_camera],
            [item[:3, 3] for item in world_to_camera],
            [item[:3, :3] for item in base_to_flange],
            [item[:3, 3] for item in base_to_flange],
            method=ROBOT_WORLD_HAND_EYE_METHODS[method],
        )
    )
    base_to_world = _transform(base_to_world_r, base_to_world_t)
    gripper_to_camera = _transform(gripper_to_camera_r, gripper_to_camera_t)
    if mode == "eye_in_hand":
        return invert_transform(gripper_to_camera), invert_transform(base_to_world)
    return invert_transform(base_to_world), invert_transform(gripper_to_camera)


def solve_extrinsic(
    observations: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    method: str,
) -> tuple[np.ndarray, np.ndarray]:
    if len(observations) < 3:
        raise ValueError("extrinsic calibration requires at least three observations")
    robot, target_camera = _observation_transforms(observations)
    if method in HAND_EYE_METHODS:
        return _calibrate_hand_eye(
            robot,
            target_camera,
            mode=mode,
            method=method,
        )
    if method in ROBOT_WORLD_HAND_EYE_METHODS:
        return _calibrate_robot_world_hand_eye(
            robot,
            target_camera,
            mode=mode,
            method=method,
        )
    raise ValueError(f"Unsupported extrinsic method: {method}")


def _companion_estimate(
    observation: Mapping[str, Any],
    primary: np.ndarray,
    *,
    mode: str,
) -> np.ndarray:
    robot, target_camera = _observation_transforms([observation])
    if mode == "eye_in_hand":
        return robot[0] @ primary @ target_camera[0]
    return invert_transform(robot[0]) @ primary @ target_camera[0]


def _companion_estimates(
    observations: Sequence[Mapping[str, Any]],
    primary: np.ndarray,
    *,
    mode: str,
) -> list[np.ndarray]:
    return [
        _companion_estimate(observation, primary, mode=mode)
        for observation in observations
    ]


def _consensus_companion(
    estimates: Sequence[np.ndarray],
    *,
    max_translation_mm: float,
    max_rotation_deg: float,
) -> np.ndarray:
    """Return a deterministic medoid-refined companion transform.

    A single bad target pose can substantially move the arithmetic transform
    average.  Selecting a transform medoid first and averaging only its closure
    inliers keeps the subsequent leave-one-pose-out evaluation robust while
    preserving the existing mean-transform convention for clean evidence.
    """

    if not estimates:
        raise ValueError("companion-transform consensus requires observations")

    def medoid_key(index: int) -> tuple[float, float, int]:
        residuals = [
            transform_residual(estimates[index], candidate) for candidate in estimates
        ]
        normalized = [
            item["translation_mm"] / max_translation_mm
            + item["rotation_deg"] / max_rotation_deg
            for item in residuals
        ]
        return float(median(normalized)), float(mean(normalized)), index

    medoid_index = min(range(len(estimates)), key=medoid_key)
    medoid_transform = estimates[medoid_index]
    retained = [
        candidate
        for candidate in estimates
        if (
            (residual := transform_residual(medoid_transform, candidate))[
                "translation_mm"
            ]
            <= max_translation_mm
            and residual["rotation_deg"] <= max_rotation_deg
        )
    ]
    return average_transform(retained or [medoid_transform])


def _closure_residuals(
    observations: Sequence[Mapping[str, Any]],
    primary: np.ndarray,
    companion: np.ndarray,
    *,
    mode: str,
) -> list[dict[str, float]]:
    return [
        transform_residual(
            _companion_estimate(observation, primary, mode=mode),
            companion,
        )
        for observation in observations
    ]


def solve_extrinsic_consensus(
    observations: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    method: str,
    max_translation_mm: float = DEFAULT_MAX_MEAN_TRANSLATION_MM,
    max_rotation_deg: float = DEFAULT_MAX_MEAN_ROTATION_DEG,
) -> tuple[np.ndarray, np.ndarray]:
    """Solve an extrinsic transform with the production robust companion model."""

    primary, _unused_companion = solve_extrinsic(
        observations,
        mode=mode,
        method=method,
    )
    companion = _consensus_companion(
        _companion_estimates(observations, primary, mode=mode),
        max_translation_mm=max_translation_mm,
        max_rotation_deg=max_rotation_deg,
    )
    return primary, companion


def _pose_training_sets(
    observations: Sequence[Mapping[str, Any]],
) -> list[tuple[str, ...]]:
    """Build deterministic robust seed sets without combinatorial growth."""

    pose_keys = sorted({_pose_key(item) for item in observations})
    sets = {tuple(pose_keys)}
    sets.update(
        tuple(candidate for candidate in pose_keys if candidate != held_out)
        for held_out in pose_keys
    )
    sample_size = min(6, len(pose_keys) - 1)
    if len(pose_keys) >= 8 and sample_size >= 4:
        combination_count = math.comb(len(pose_keys), sample_size)
        if combination_count <= 64:
            sets.update(combinations(pose_keys, sample_size))
        else:
            generator = np.random.default_rng(0xCA11B)
            attempts = 0
            while len([item for item in sets if len(item) == sample_size]) < 64:
                indices = tuple(
                    sorted(
                        int(item)
                        for item in generator.choice(
                            len(pose_keys), size=sample_size, replace=False
                        )
                    )
                )
                sets.add(tuple(pose_keys[index] for index in indices))
                attempts += 1
                if attempts >= 1024:
                    break
    return sorted(sets, key=lambda item: (-len(item), item))


def _robust_extrinsic_seed(
    observations: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    method: str,
    max_translation_mm: float,
    max_rotation_deg: float,
) -> tuple[np.ndarray, np.ndarray, list[bool]]:
    seeds = []
    for pose_subset in _pose_training_sets(observations):
        training = [item for item in observations if _pose_key(item) in pose_subset]
        try:
            primary, _unused_companion = solve_extrinsic(
                training,
                mode=mode,
                method=method,
            )
            companion = _consensus_companion(
                _companion_estimates(observations, primary, mode=mode),
                max_translation_mm=max_translation_mm,
                max_rotation_deg=max_rotation_deg,
            )
            residuals = _closure_residuals(
                observations,
                primary,
                companion,
                mode=mode,
            )
        except (cv2.error, ValueError, TypeError, np.linalg.LinAlgError):
            continue
        mask = [
            item["translation_mm"] <= max_translation_mm
            and item["rotation_deg"] <= max_rotation_deg
            for item in residuals
        ]
        inlier_residuals = [
            item for item, keep in zip(residuals, mask, strict=True) if keep
        ]
        summary = residual_summary(inlier_residuals)
        seeds.append(
            (
                (
                    -sum(mask),
                    summary["median_translation_mm"] / max_translation_mm
                    + summary["median_rotation_deg"] / max_rotation_deg,
                    summary["mean_translation_mm"],
                    summary["mean_rotation_deg"],
                    pose_subset,
                ),
                primary,
                companion,
                mask,
            )
        )
    if not seeds:
        raise ValueError("extrinsic solver could not form a finite closure model")
    _key, primary, companion, mask = min(seeds, key=lambda item: item[0])
    return primary, companion, mask


def _pose_key(observation: Mapping[str, Any]) -> str:
    value = observation.get("motion")
    return str(value) if value not in {None, ""} else str(observation.get("frame_id"))


def continuous_image_coverage_evidence(
    observations: Sequence[Mapping[str, Any]],
    *,
    tail_support_views: int = DEFAULT_IMAGE_COVERAGE_TAIL_SUPPORT_VIEWS,
) -> dict[str, Any]:
    """Measure partition-independent, repeatedly supported centroid coverage."""

    if tail_support_views < 1:
        raise ValueError("image-coverage tail support must be positive")
    normalized_points: list[np.ndarray] = []
    image_size: tuple[float, float] | None = None
    for observation in observations:
        centroid = observation.get("image_centroid_px")
        size = observation.get("image_size")
        if (
            not isinstance(centroid, Sequence)
            or isinstance(centroid, (str, bytes))
            or len(centroid) != 2
            or not isinstance(size, Sequence)
            or isinstance(size, (str, bytes))
            or len(size) != 2
        ):
            raise ValueError(
                "continuous image-centroid coverage requires centroid and image size"
            )
        point = np.asarray(centroid, dtype=float).reshape(2)
        dimensions = np.asarray(size, dtype=float).reshape(2)
        if (
            not np.all(np.isfinite(point))
            or not np.all(np.isfinite(dimensions))
            or np.any(dimensions <= 0.0)
            or np.any(point < 0.0)
            or np.any(point > dimensions)
        ):
            raise ValueError("continuous image-centroid coverage is non-finite")
        current_size = (float(dimensions[0]), float(dimensions[1]))
        if image_size is None:
            image_size = current_size
        elif not all(
            math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)
            for left, right in zip(image_size, current_size, strict=True)
        ):
            raise ValueError("continuous image-centroid coverage mixes image sizes")
        normalized_points.append(point / dimensions)
    minimum_view_count = 2 * tail_support_views
    if len(normalized_points) < minimum_view_count:
        raise ValueError(
            "continuous image-centroid coverage requires at least "
            f"{minimum_view_count} views"
        )
    points = np.asarray(normalized_points, dtype=float)
    ordered = np.sort(points, axis=0)
    supported_minimum = ordered[tail_support_views - 1]
    supported_maximum = ordered[-tail_support_views]
    supported_span = supported_maximum - supported_minimum
    clipped = np.clip(points, supported_minimum, supported_maximum)
    hull_area_ratio = float(cv2.contourArea(cv2.convexHull(clipped.astype(np.float32))))
    return {
        "strategy": "supported_normalized_centroid_hull.v1",
        "observation_count": len(normalized_points),
        "image_size": list(image_size or ()),
        "tail_support_views": tail_support_views,
        "supported_minimum_normalized_xy": supported_minimum.astype(float).tolist(),
        "supported_maximum_normalized_xy": supported_maximum.astype(float).tolist(),
        "supported_span_ratio_xy": supported_span.astype(float).tolist(),
        "supported_convex_hull_area_ratio": hull_area_ratio,
    }


def _balanced_motion_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    max_per_motion: int = DEFAULT_MAX_OBSERVATIONS_PER_MOTION,
) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    if max_per_motion < 1:
        raise ValueError("maximum observations per motion must be positive")
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, observation in enumerate(observations):
        grouped.setdefault(_pose_key(observation), []).append((index, observation))
    selected: list[tuple[int, Mapping[str, Any]]] = []
    per_motion: dict[str, dict[str, int]] = {}
    for pose_key, values in grouped.items():
        if len(values) <= max_per_motion:
            indices = list(range(len(values)))
        elif max_per_motion == 1:
            indices = [len(values) // 2]
        else:
            indices = sorted(
                {
                    round(index * (len(values) - 1) / (max_per_motion - 1))
                    for index in range(max_per_motion)
                }
            )
        selected.extend(values[index] for index in indices)
        per_motion[pose_key] = {
            "available": len(values),
            "selected": len(indices),
        }
    selected.sort(key=lambda item: item[0])
    return [item for _index, item in selected], {
        "strategy": "evenly_spaced_per_motion_v1",
        "max_observations_per_motion": max_per_motion,
        "input_observation_count": len(observations),
        "solver_observation_count": len(selected),
        "per_motion": per_motion,
    }


def _observability_check(
    observations: Sequence[Mapping[str, Any]],
    *,
    min_pose_count: int = 4,
    min_translation_span_mm: float = 1e-3,
    min_rotation_span_deg: float = 1e-3,
    min_rotation_axis_angle_deg: float = DEFAULT_MIN_ROTATION_AXIS_ANGLE_DEG,
    min_rotation_axis_singular_ratio: float = (
        DEFAULT_MIN_ROTATION_AXIS_SINGULAR_RATIO
    ),
) -> dict[str, Any]:
    pose_keys = {_pose_key(item) for item in observations}
    if len(pose_keys) < min_pose_count:
        raise ValueError(
            "leave-one-pose-out validation requires at least "
            f"{min_pose_count} distinct motion poses; found {len(pose_keys)}"
        )
    if min_rotation_axis_angle_deg <= 0.0:
        raise ValueError("rotation-axis minimum angle must be positive")
    if not 0.0 < min_rotation_axis_singular_ratio <= 1.0:
        raise ValueError("rotation-axis singular ratio must be in (0, 1]")
    robot, _target = _observation_transforms(observations)
    # The caller balances frames per motion before this check.  Retaining the
    # evenly spaced samples (instead of only a motion endpoint) captures axis
    # excitation that occurs within a sweep without letting long sweeps
    # dominate short orientation-dither motions.
    representative_robot = robot
    relative_rotation: list[float] = []
    relative_translation: list[float] = []
    rotation_axes: list[np.ndarray] = []
    for left, right in combinations(representative_robot, 2):
        residual = transform_residual(left, right)
        relative_rotation.append(residual["rotation_deg"])
        relative_translation.append(residual["translation_mm"])
        relative_rvec, _ = cv2.Rodrigues(left[:3, :3].T @ right[:3, :3])
        angle_rad = float(np.linalg.norm(relative_rvec))
        if math.degrees(angle_rad) >= min_rotation_axis_angle_deg:
            rotation_axes.append(relative_rvec.reshape(3) / angle_rad)
    rotation_span = max(relative_rotation, default=0.0)
    translation_span = max(relative_translation, default=0.0)
    if rotation_span < min_rotation_span_deg:
        raise ValueError(
            "degenerate robot motion: rotation span "
            f"{rotation_span:.3f} deg is below {min_rotation_span_deg:.3f} deg"
        )
    if translation_span < min_translation_span_mm:
        raise ValueError(
            "degenerate robot motion: translation span "
            f"{translation_span:.3f} mm is below {min_translation_span_mm:.3f} mm"
        )
    if len(rotation_axes) < 2:
        raise ValueError(
            "degenerate robot motion: fewer than two relative rotations meet "
            f"the {min_rotation_axis_angle_deg:.3f} deg axis-analysis threshold"
        )
    singular_values = np.linalg.svd(
        np.asarray(rotation_axes, dtype=float), compute_uv=False
    )
    singular_ratio = (
        float(singular_values[1] / singular_values[0])
        if singular_values.size >= 2 and singular_values[0] > 0.0
        else 0.0
    )
    if singular_ratio < min_rotation_axis_singular_ratio:
        raise ValueError(
            "degenerate robot motion: rotation-axis second/first singular "
            f"ratio {singular_ratio:.3f} is below "
            f"{min_rotation_axis_singular_ratio:.3f}"
        )
    return {
        "distinct_motion_pose_count": len(pose_keys),
        "translation_span_mm": float(translation_span),
        "rotation_span_deg": float(rotation_span),
        "rotation_axis_sample_count": len(rotation_axes),
        "rotation_axis_singular_values": singular_values.astype(float).tolist(),
        "rotation_axis_second_to_first_ratio": singular_ratio,
        "rotation_axis_minimum_angle_deg": float(min_rotation_axis_angle_deg),
        "rotation_axis_minimum_singular_ratio": float(min_rotation_axis_singular_ratio),
    }


def evaluate_extrinsic_candidate(
    observations: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    pnp_method: str,
    extrinsic_method: str,
    sensor_key: str,
    min_inliers: int = DEFAULT_MIN_INLIERS,
    max_mean_translation_mm: float = DEFAULT_MAX_MEAN_TRANSLATION_MM,
    max_mean_rotation_deg: float = DEFAULT_MAX_MEAN_ROTATION_DEG,
    max_outlier_ratio: float = DEFAULT_MAX_OUTLIER_RATIO,
    min_accepted_views: int = 0,
    min_coverage_cells: int = 0,
    image_coverage_tail_support_views: int = (
        DEFAULT_IMAGE_COVERAGE_TAIL_SUPPORT_VIEWS
    ),
    min_image_centroid_x_span_ratio: float = 0.0,
    min_image_centroid_y_span_ratio: float = 0.0,
    min_image_centroid_hull_area_ratio: float = 0.0,
    min_motion_poses: int = 4,
    min_translation_span_mm: float = 1e-3,
    min_rotation_span_deg: float = 1e-3,
) -> dict[str, Any]:
    """Evaluate one PnP/extrinsic pair with deterministic leave-one-pose-out."""

    candidate_id = f"{sensor_key}|{pnp_method}|{extrinsic_method}"
    input_observations = list(observations)
    input_observation_count = len(input_observations)
    balance_evidence: dict[str, Any] | None = None
    continuous_coverage: dict[str, Any] | None = None
    try:
        if max_mean_translation_mm <= 0 or max_mean_rotation_deg <= 0:
            raise ValueError("residual thresholds must be greater than zero")
        if not 0 <= max_outlier_ratio <= 1:
            raise ValueError("max_outlier_ratio must be between zero and one")
        if any(
            not 0.0 <= value <= 1.0
            for value in (
                min_image_centroid_x_span_ratio,
                min_image_centroid_y_span_ratio,
                min_image_centroid_hull_area_ratio,
            )
        ):
            raise ValueError(
                "continuous image-centroid coverage thresholds must be in [0, 1]"
            )
        accepted_views = {
            str(item.get("frame_id"))
            for item in observations
            if item.get("frame_id") not in {None, ""}
        }
        if len(accepted_views) < min_accepted_views:
            raise ValueError(
                f"accepted view count {len(accepted_views)} is below "
                f"required {min_accepted_views}"
            )
        coverage_cells = {
            int(item["image_coverage_cell"])
            for item in observations
            if item.get("image_coverage_cell") is not None
        }
        continuous_coverage_required = any(
            value > 0.0
            for value in (
                min_image_centroid_x_span_ratio,
                min_image_centroid_y_span_ratio,
                min_image_centroid_hull_area_ratio,
            )
        )
        if continuous_coverage_required:
            continuous_coverage = continuous_image_coverage_evidence(
                input_observations,
                tail_support_views=image_coverage_tail_support_views,
            )
            span_x, span_y = (
                float(value) for value in continuous_coverage["supported_span_ratio_xy"]
            )
            hull_area_ratio = float(
                continuous_coverage["supported_convex_hull_area_ratio"]
            )
            if (
                span_x < min_image_centroid_x_span_ratio
                or span_y < min_image_centroid_y_span_ratio
                or hull_area_ratio < min_image_centroid_hull_area_ratio
            ):
                raise ValueError(
                    "continuous image-centroid coverage is below required "
                    "field-of-view diversity: "
                    f"x span {span_x:.3f}/{min_image_centroid_x_span_ratio:.3f}, "
                    f"y span {span_y:.3f}/{min_image_centroid_y_span_ratio:.3f}, "
                    "hull area "
                    f"{hull_area_ratio:.3f}/"
                    f"{min_image_centroid_hull_area_ratio:.3f}"
                )
        elif len(coverage_cells) < min_coverage_cells:
            raise ValueError(
                f"image-centroid coverage {len(coverage_cells)}/9 is below "
                f"required {min_coverage_cells}/9"
            )
        observations, balance_evidence = _balanced_motion_observations(
            input_observations
        )
        observability = _observability_check(
            observations,
            min_pose_count=min_motion_poses,
            min_translation_span_mm=min_translation_span_mm,
            min_rotation_span_deg=min_rotation_span_deg,
        )
        primary, companion, inlier_mask = _robust_extrinsic_seed(
            observations,
            mode=mode,
            method=extrinsic_method,
            max_translation_mm=max_mean_translation_mm,
            max_rotation_deg=max_mean_rotation_deg,
        )
        for _iteration in range(8):
            fit_observations = [
                item
                for item, keep in zip(observations, inlier_mask, strict=True)
                if keep
            ]
            if len(fit_observations) < 3:
                fit_observations = list(observations)
            primary, _unused_companion = solve_extrinsic(
                fit_observations,
                mode=mode,
                method=extrinsic_method,
            )
            companion = _consensus_companion(
                _companion_estimates(fit_observations, primary, mode=mode),
                max_translation_mm=max_mean_translation_mm,
                max_rotation_deg=max_mean_rotation_deg,
            )
            full_residuals = _closure_residuals(
                observations,
                primary,
                companion,
                mode=mode,
            )
            next_mask = [
                item["translation_mm"] <= max_mean_translation_mm
                and item["rotation_deg"] <= max_mean_rotation_deg
                for item in full_residuals
            ]
            if next_mask == inlier_mask:
                break
            inlier_mask = next_mask

        inlier_observations = [
            item for item, keep in zip(observations, inlier_mask, strict=True) if keep
        ]
        post_pruning_observability = _observability_check(
            inlier_observations,
            min_pose_count=min_motion_poses,
            min_translation_span_mm=min_translation_span_mm,
            min_rotation_span_deg=min_rotation_span_deg,
        )
        inlier_pose_keys = sorted({_pose_key(item) for item in inlier_observations})
        held_out_records: list[dict[str, Any]] = []
        for pose_key in inlier_pose_keys:
            train = [
                item for item in inlier_observations if _pose_key(item) != pose_key
            ]
            holdout = [
                item for item in inlier_observations if _pose_key(item) == pose_key
            ]
            fold_primary, fold_companion = solve_extrinsic(
                train,
                mode=mode,
                method=extrinsic_method,
            )
            for observation in holdout:
                estimate = _companion_estimate(observation, fold_primary, mode=mode)
                residual = transform_residual(estimate, fold_companion)
                held_out_records.append(
                    {
                        "pose": pose_key,
                        "frame_id": observation.get("frame_id"),
                        "validation_split": "leave_one_pose_out_inlier",
                        **residual,
                    }
                )
        for observation, keep, residual in zip(
            observations, inlier_mask, full_residuals, strict=True
        ):
            if keep:
                continue
            held_out_records.append(
                {
                    "pose": _pose_key(observation),
                    "frame_id": observation.get("frame_id"),
                    "validation_split": "rejected_closure_outlier",
                    **residual,
                }
            )
        held_out_summary = residual_summary(held_out_records)
        solver_inlier_count = sum(inlier_mask)
        solver_outlier_count = len(inlier_mask) - solver_inlier_count
        input_residuals = _closure_residuals(
            input_observations,
            primary,
            companion,
            mode=mode,
        )
        input_inlier_mask = [
            item["translation_mm"] <= max_mean_translation_mm
            and item["rotation_deg"] <= max_mean_rotation_deg
            for item in input_residuals
        ]
        inlier_count = sum(input_inlier_mask)
        outlier_count = len(input_inlier_mask) - inlier_count
        raw_outlier_ratio = (
            outlier_count / len(input_inlier_mask) if input_inlier_mask else 1.0
        )
        input_validation = _motion_balanced_validation(
            input_observations,
            input_residuals,
            max_translation_mm=max_mean_translation_mm,
            max_rotation_deg=max_mean_rotation_deg,
        )
        outlier_ratio = input_validation["motion_balanced_outlier_ratio"]
        full_summary = residual_summary(full_residuals)
        input_summary = residual_summary(input_residuals)
        reprojection_values = [
            float(item.get("mean_reprojection_error_px", 0.0))
            for item in input_observations
        ]
        clutter_filtered_view_count = sum(
            bool(item.get("pnp_duplicate_marker_clutter_filtered"))
            for item in input_observations
        )
        ignored_clutter_correspondence_count = sum(
            int(item.get("pnp_ignored_clutter_correspondence_count", 0))
            for item in input_observations
        )
        passing = (
            solver_inlier_count >= min_inliers
            and held_out_summary["mean_translation_mm"] <= max_mean_translation_mm
            and held_out_summary["mean_rotation_deg"] <= max_mean_rotation_deg
            and input_validation["motion_balanced_mean_translation_mm"]
            <= max_mean_translation_mm
            and input_validation["motion_balanced_mean_rotation_deg"]
            <= max_mean_rotation_deg
            and outlier_ratio <= max_outlier_ratio
            and input_validation["max_repeated_motion_outlier_ratio"]
            <= max_outlier_ratio
        )
        score = (
            held_out_summary["median_translation_mm"] / max_mean_translation_mm
            + held_out_summary["median_rotation_deg"] / max_mean_rotation_deg
        )
        primary_frames = (
            ("camera", "robot_flange")
            if mode == "eye_in_hand"
            else ("camera", "template_base")
        )
        companion_frames = (
            ("aruco_grid", "template_base")
            if mode == "eye_in_hand"
            else ("aruco_grid", "robot_flange")
        )
        checks = [
            {
                "name": "accepted_views",
                "status": "ok",
                "actual": len(accepted_views),
                "threshold": min_accepted_views,
            },
            *(
                [
                    {
                        "name": "duplicate_marker_clutter_filtered",
                        "status": "warning",
                        "actual": {
                            "affected_view_count": clutter_filtered_view_count,
                            "accepted_view_count": len(accepted_views),
                            "ignored_correspondence_count": (
                                ignored_clutter_correspondence_count
                            ),
                        },
                        "threshold": {
                            "minimum_supported_markers_per_affected_view": (
                                DEFAULT_MIN_PNP_CLUTTER_SUPPORTED_MARKERS
                            ),
                            "minimum_grid_rows_per_affected_view": (
                                DEFAULT_MIN_PNP_CLUTTER_GRID_ROWS
                            ),
                            "minimum_grid_columns_per_affected_view": (
                                DEFAULT_MIN_PNP_CLUTTER_GRID_COLUMNS
                            ),
                        },
                    }
                ]
                if clutter_filtered_view_count
                else []
            ),
            {
                "name": "image_centroid_coverage",
                "status": (
                    "ok" if len(coverage_cells) >= min_coverage_cells else "warning"
                ),
                "actual": len(coverage_cells),
                "threshold": min_coverage_cells,
                "cells": sorted(coverage_cells),
            },
            *(
                [
                    {
                        "name": "continuous_image_centroid_coverage",
                        "status": "ok",
                        "actual": continuous_coverage,
                        "threshold": {
                            "minimum_x_span_ratio": (min_image_centroid_x_span_ratio),
                            "minimum_y_span_ratio": (min_image_centroid_y_span_ratio),
                            "minimum_convex_hull_area_ratio": (
                                min_image_centroid_hull_area_ratio
                            ),
                            "tail_support_views": (image_coverage_tail_support_views),
                        },
                    }
                ]
                if continuous_coverage is not None
                else []
            ),
            {
                "name": "motion_pose_diversity",
                "status": "ok",
                "actual": observability["distinct_motion_pose_count"],
                "threshold": min_motion_poses,
            },
            {
                "name": "translation_diversity",
                "status": "ok",
                "actual": observability["translation_span_mm"],
                "threshold": min_translation_span_mm,
                "unit": "mm",
            },
            {
                "name": "rotation_diversity",
                "status": "ok",
                "actual": observability["rotation_span_deg"],
                "threshold": min_rotation_span_deg,
                "unit": "deg",
            },
            {
                "name": "rotation_axis_observability",
                "status": "ok",
                "actual": observability["rotation_axis_second_to_first_ratio"],
                "threshold": observability["rotation_axis_minimum_singular_ratio"],
            },
            {
                "name": "post_pruning_motion_pose_diversity",
                "status": "ok",
                "actual": post_pruning_observability["distinct_motion_pose_count"],
                "threshold": min_motion_poses,
            },
            {
                "name": "post_pruning_rotation_axis_observability",
                "status": "ok",
                "actual": post_pruning_observability[
                    "rotation_axis_second_to_first_ratio"
                ],
                "threshold": post_pruning_observability[
                    "rotation_axis_minimum_singular_ratio"
                ],
            },
            {
                "name": "minimum_inliers",
                "status": ("ok" if solver_inlier_count >= min_inliers else "error"),
                "actual": solver_inlier_count,
                "threshold": min_inliers,
            },
            {
                "name": "mean_translation_residual",
                "status": (
                    "ok"
                    if held_out_summary["mean_translation_mm"]
                    <= max_mean_translation_mm
                    else "error"
                ),
                "actual": held_out_summary["mean_translation_mm"],
                "threshold": max_mean_translation_mm,
                "unit": "mm",
            },
            {
                "name": "mean_rotation_residual",
                "status": (
                    "ok"
                    if held_out_summary["mean_rotation_deg"] <= max_mean_rotation_deg
                    else "error"
                ),
                "actual": held_out_summary["mean_rotation_deg"],
                "threshold": max_mean_rotation_deg,
                "unit": "deg",
            },
            {
                "name": "outlier_ratio",
                "status": "ok" if outlier_ratio <= max_outlier_ratio else "error",
                "actual": outlier_ratio,
                "threshold": max_outlier_ratio,
            },
            {
                "name": "full_input_motion_balanced_translation_residual",
                "status": (
                    "ok"
                    if input_validation["motion_balanced_mean_translation_mm"]
                    <= max_mean_translation_mm
                    else "error"
                ),
                "actual": input_validation["motion_balanced_mean_translation_mm"],
                "threshold": max_mean_translation_mm,
                "unit": "mm",
            },
            {
                "name": "full_input_motion_balanced_rotation_residual",
                "status": (
                    "ok"
                    if input_validation["motion_balanced_mean_rotation_deg"]
                    <= max_mean_rotation_deg
                    else "error"
                ),
                "actual": input_validation["motion_balanced_mean_rotation_deg"],
                "threshold": max_mean_rotation_deg,
                "unit": "deg",
            },
            {
                "name": "full_input_repeated_motion_outlier_ratio",
                "status": (
                    "ok"
                    if input_validation["max_repeated_motion_outlier_ratio"]
                    <= max_outlier_ratio
                    else "error"
                ),
                "actual": input_validation["max_repeated_motion_outlier_ratio"],
                "threshold": max_outlier_ratio,
            },
        ]
        return {
            "candidate_id": candidate_id,
            "sensor_key": sensor_key,
            "pnp_method": pnp_method,
            "extrinsic_method": extrinsic_method,
            "algorithms": [pnp_method, extrinsic_method],
            "status": "passing" if passing else "failed",
            "validation_state": "passed" if passing else "failed",
            "score": float(score),
            "observation_count": input_observation_count,
            "input_observation_count": input_observation_count,
            "solver_observation_count": len(observations),
            "solver_inlier_count": solver_inlier_count,
            "solver_outlier_count": solver_outlier_count,
            "inlier_count": inlier_count,
            "outlier_count": outlier_count,
            "outlier_ratio": float(outlier_ratio),
            "raw_outlier_ratio": float(raw_outlier_ratio),
            "mean_reprojection_error_px": (
                float(mean(reprojection_values)) if reprojection_values else None
            ),
            "observation_quality": {
                "accepted_view_count": len(accepted_views),
                "coverage_cells": sorted(coverage_cells),
                "continuous_image_coverage": continuous_coverage,
                "pre_pruning_observability": observability,
                "post_pruning_observability": post_pruning_observability,
                "motion_balancing": balance_evidence,
                **post_pruning_observability,
            },
            "primary_transform": transform_record(
                primary,
                from_frame=primary_frames[0],
                to_frame=primary_frames[1],
            ),
            "companion_transform": transform_record(
                companion,
                from_frame=companion_frames[0],
                to_frame=companion_frames[1],
            ),
            "held_out_residuals": held_out_summary,
            "fit_residuals": full_summary,
            "full_input_residuals": input_summary,
            "full_input_validation": input_validation,
            "leave_one_pose_out": held_out_records,
            "checks": checks,
        }
    except (cv2.error, ValueError, TypeError, np.linalg.LinAlgError) as exc:
        return {
            "candidate_id": candidate_id,
            "sensor_key": sensor_key,
            "pnp_method": pnp_method,
            "extrinsic_method": extrinsic_method,
            "algorithms": [pnp_method, extrinsic_method],
            "status": "error",
            "validation_state": "failed",
            "score": None,
            "observation_count": input_observation_count,
            "input_observation_count": input_observation_count,
            "solver_observation_count": (
                int(balance_evidence["solver_observation_count"])
                if balance_evidence is not None
                else input_observation_count
            ),
            "inlier_count": 0,
            "outlier_count": input_observation_count,
            "outlier_ratio": 1.0,
            "error": str(exc),
            "motion_balancing": balance_evidence,
            "acceptance_thresholds": {
                "min_accepted_views": min_accepted_views,
                "min_coverage_cells": min_coverage_cells,
                "image_coverage_tail_support_views": (
                    image_coverage_tail_support_views
                ),
                "min_image_centroid_x_span_ratio": (min_image_centroid_x_span_ratio),
                "min_image_centroid_y_span_ratio": (min_image_centroid_y_span_ratio),
                "min_image_centroid_hull_area_ratio": (
                    min_image_centroid_hull_area_ratio
                ),
                "min_motion_poses": min_motion_poses,
                "min_translation_span_mm": min_translation_span_mm,
                "min_rotation_span_deg": min_rotation_span_deg,
            },
            "checks": [
                {
                    "name": "solver",
                    "status": "error",
                    "message": str(exc),
                }
            ],
        }


RANKING_NUMERIC_DECIMALS = 6


def rank_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rank passing candidates, followed by deterministic failed evidence."""

    pnp_order = {name: index for index, name in enumerate(PNP_METHOD_ORDER)}
    extrinsic_order = {name: index for index, name in enumerate(EXTRINSIC_METHOD_ORDER)}

    def key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        passing = item.get("status") == "passing"
        score = float(item.get("score")) if item.get("score") is not None else math.inf
        reprojection = (
            float(item.get("mean_reprojection_error_px"))
            if item.get("mean_reprojection_error_px") is not None
            else math.inf
        )
        return (
            0 if passing else 1,
            round(score, RANKING_NUMERIC_DECIMALS),
            round(reprojection, RANKING_NUMERIC_DECIMALS),
            -int(item.get("inlier_count", 0)),
            pnp_order.get(str(item.get("pnp_method")), len(pnp_order)),
            extrinsic_order.get(
                str(item.get("extrinsic_method")), len(extrinsic_order)
            ),
            str(item.get("candidate_id")),
        )

    ranked = [dict(item) for item in sorted(candidates, key=key)]
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
        item["recommended"] = index == 1 and item.get("status") == "passing"
    return ranked
