"""Decode the iiwa robot-pose stream into a rigid transform."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from pytransform3d import rotations as pr
from pytransform3d import transformations as pt


def robot_flange_to_template_base(robot_pose: Mapping[str, Any]) -> np.ndarray:
    """Decode the iiwa KUKA A/B/C-radian stream with explicit endpoints."""

    try:
        translation = np.asarray([float(robot_pose[key]) for key in ("X", "Y", "Z")])
        euler_cba = np.asarray([float(robot_pose[key]) for key in ("C", "B", "A")])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Robot pose must contain finite X,Y,Z,A,B,C values") from exc
    if not np.all(np.isfinite([*translation, *euler_cba])):
        raise ValueError("Robot pose must contain finite X,Y,Z,A,B,C values")
    return pt.transform_from(pr.matrix_from_euler(euler_cba, 0, 1, 2, True), translation)
