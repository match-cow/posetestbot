"""Neutral rigid-transform helpers for current calibration attempts."""

from __future__ import annotations

import math
from statistics import mean, median
from typing import Any, Mapping, Sequence

import numpy as np
from pytransform3d import rotations as pr
from pytransform3d import transformations as pt


def is_finite_transform(value: np.ndarray) -> bool:
    """Return whether ``value`` is one finite homogeneous transform."""

    return value.shape == (4, 4) and bool(np.all(np.isfinite(value)))


def invert_transform(value: np.ndarray) -> np.ndarray:
    """Invert one finite homogeneous transform."""

    if not is_finite_transform(value):
        raise ValueError("cannot invert a non-finite transform")
    return pt.invert_transform(value)


def transform_record(
    value: np.ndarray,
    *,
    from_frame: str,
    to_frame: str,
) -> dict[str, Any]:
    """Serialize one finite transform with explicit frame endpoints."""

    if not is_finite_transform(value):
        raise ValueError("transform must be finite")
    x, y, z, qw, qx, qy, qz = pt.pq_from_transform(value)
    return {
        "from": from_frame,
        "to": to_frame,
        "matrix": np.asarray(value, dtype=float).tolist(),
        "rotation_quaternion_wxyz": [
            float(qw),
            float(qx),
            float(qy),
            float(qz),
        ],
        "translation_mm": [float(x), float(y), float(z)],
    }


def transform_from_record(value: Mapping[str, Any]) -> np.ndarray:
    """Load one finite transform from its current JSON representation."""

    matrix = value.get("matrix")
    if matrix is not None:
        result = np.asarray(matrix, dtype=float)
        if not is_finite_transform(result):
            raise ValueError("recorded transform matrix is invalid")
        return result
    quaternion = np.asarray(value.get("rotation_quaternion_wxyz"), dtype=float)
    translation = np.asarray(value.get("translation_mm"), dtype=float)
    if quaternion.shape != (4,) or translation.shape != (3,):
        raise ValueError("recorded transform requires quaternion and translation")
    result = pt.transform_from_pq(
        np.asarray([*translation.tolist(), *quaternion.tolist()], dtype=float)
    )
    if not is_finite_transform(result):
        raise ValueError("recorded transform is non-finite")
    return result


def transform_residual(left: np.ndarray, right: np.ndarray) -> dict[str, float]:
    """Return translation and rotation separation between two transforms."""

    translation_mm = float(np.linalg.norm(left[:3, 3] - right[:3, 3]))
    delta = left[:3, :3].T @ right[:3, :3]
    cosine = max(-1.0, min(1.0, (float(np.trace(delta)) - 1.0) / 2.0))
    return {
        "translation_mm": translation_mm,
        "rotation_deg": math.degrees(math.acos(cosine)),
    }


def residual_summary(records: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Summarize a collection of transform residuals."""

    if not records:
        return {
            "mean_translation_mm": 0.0,
            "median_translation_mm": 0.0,
            "max_translation_mm": 0.0,
            "mean_rotation_deg": 0.0,
            "median_rotation_deg": 0.0,
            "max_rotation_deg": 0.0,
        }
    translations = [float(item["translation_mm"]) for item in records]
    rotations = [float(item["rotation_deg"]) for item in records]
    return {
        "mean_translation_mm": float(mean(translations)),
        "median_translation_mm": float(median(translations)),
        "max_translation_mm": float(max(translations)),
        "mean_rotation_deg": float(mean(rotations)),
        "median_rotation_deg": float(median(rotations)),
        "max_rotation_deg": float(max(rotations)),
    }


def robot_ee_to_reference(robot_ee_pose: Mapping[str, Any]) -> np.ndarray:
    """Convert one current KUKA XYZ/ABC pose to a homogeneous transform."""

    try:
        rotation = pr.matrix_from_euler(
            np.array(
                [
                    float(robot_ee_pose["C"]),
                    float(robot_ee_pose["B"]),
                    float(robot_ee_pose["A"]),
                ]
            ),
            0,
            1,
            2,
            True,
        )
        translation = np.array(
            [
                float(robot_ee_pose["X"]),
                float(robot_ee_pose["Y"]),
                float(robot_ee_pose["Z"]),
            ],
            dtype=float,
        )
    except KeyError as exc:
        raise ValueError("robot_ee_pose must include X, Y, Z, A, B, C") from exc
    return pt.transform_from(rotation, translation)


def _average_quaternions(quaternions: np.ndarray) -> np.ndarray:
    accumulator = np.zeros((4, 4), dtype=float)
    for quaternion in quaternions:
        normalized = np.array(quaternion, dtype=float)
        if normalized[0] < 0:
            normalized = -normalized
        accumulator += np.outer(normalized, normalized)
    accumulator /= len(quaternions)
    eigenvalues, eigenvectors = np.linalg.eigh(accumulator)
    return eigenvectors[:, np.argmax(eigenvalues)]


def average_transform(transforms: Sequence[np.ndarray]) -> np.ndarray:
    """Average transform translation and unit-quaternion rotation."""

    if not transforms:
        raise ValueError("At least one transform is required")
    translations = []
    quaternions = []
    for transform in transforms:
        x, y, z, qw, qx, qy, qz = pt.pq_from_transform(transform)
        translations.append([x, y, z])
        quaternions.append([qw, qx, qy, qz])
    translation = np.mean(np.asarray(translations, dtype=float), axis=0)
    quaternion = _average_quaternions(np.asarray(quaternions, dtype=float))
    return pt.transform_from(pr.matrix_from_quaternion(quaternion), translation)
