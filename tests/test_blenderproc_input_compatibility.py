from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from posetestbot.blenderproc.preparation import (
    camera_transform_for_sensor,
    read_camera_parameters,
)
from posetestbot.io.artifacts import CAM_K


def test_blenderproc_read_camera_parameters_allows_missing_distortion(
    tmp_path: Path,
) -> None:
    sensor_folder = tmp_path / "processed" / "synchronized" / "realsense_123"
    sensor_folder.mkdir(parents=True)
    (sensor_folder / CAM_K).write_text("1 0 2\n0 3 4\n0 0 1\n")

    cam_matrix, dist_coefficients = read_camera_parameters(sensor_folder)

    np.testing.assert_allclose(cam_matrix, np.array([[1, 0, 2], [0, 3, 4], [0, 0, 1]]))
    np.testing.assert_allclose(dist_coefficients, np.zeros((5, 1)))


def test_blenderproc_camera_transform_lookup_requires_exact_sensor_key() -> None:
    transforms = {
        "realsense": {"position": [1, 2, 3]},
        "luxonis": {"position": [4, 5, 6]},
        "zed_2i": {"position": [7, 8, 9]},
    }

    with pytest.raises(KeyError, match="No exact camera transform"):
        camera_transform_for_sensor(transforms, "realsense_123")

    transforms["realsense_123"] = {"position": [10, 20, 30]}
    assert camera_transform_for_sensor(transforms, "realsense_123") == {
        "position": [10, 20, 30]
    }
