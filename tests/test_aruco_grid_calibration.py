from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.calibration.intrinsics import (
    IntrinsicCalibrationError,
    calibrate_intrinsic_profile,
    factory_intrinsic_profile,
    select_intrinsic_profile,
)
from posetestbot.calibration.targets import (
    normalize_calibration_target_spec,
    opencv_grid_board,
)


def sensor_fixture(folder: Path) -> None:
    (folder / "rgb").mkdir(parents=True)
    (folder / "cam_K.txt").write_text(
        "580 0 320\n0 585 240\n0 0 1\n0.02 -0.01 0.001 -0.001 0.003\n"
    )
    (folder / "depthscale.txt").write_text("1.0\n")
    (folder / "camera_data.json").write_text(
        json.dumps(
            {"K": [[580, 0, 320], [0, 585, 240], [0, 0, 1]], "resolution": [480, 640]}
        )
    )
    (folder / "frame_metadata.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "SERIAL-1",
                "frame_index": 0,
                "frame_id": "000000.png",
                "rgb_path": "rgb/000000.png",
                "depth_path": "depth/000000.png",
                "sensor_timestamp_ns": 1,
                "host_received_timestamp_ns": 2,
                "host_wall_timestamp_ns": 3,
                "orientation": "normal",
            }
        )
        + "\n"
    )


def synthetic_detections(
    target: dict, *, centered: bool = False
) -> tuple[dict, dict[str, np.ndarray]]:
    _dictionary, board = opencv_grid_board(target)
    ids = board.getIds().reshape(-1).astype(int).tolist()
    objects = [
        np.asarray(item, dtype=np.float32).reshape(4, 3)
        for item in board.getObjPoints()
    ]
    true_k = np.array([[600.0, 0.0, 320.0], [0.0, 605.0, 240.0], [0.0, 0.0, 1.0]])
    distortion = np.array([0.02, -0.01, 0.001, -0.001, 0.003])
    frames = {}
    poses = {}
    offsets = (
        [(0.0, 0.0)] * 18
        if centered
        else [
            (x, y)
            for y in (-130.0, 0.0, 130.0)
            for x in (-190.0, 0.0, 190.0)
            for _repeat in range(2)
        ]
    )
    for index, (tx, ty) in enumerate(offsets):
        rvec = np.array(
            [
                (-0.22, 0.08, 0.26)[index % 3],
                (-0.18, 0.20, 0.05)[(index // 3) % 3],
                -0.08 + 0.025 * (index % 7),
            ]
        )
        tvec = np.array([tx, ty, 620.0 + 8.0 * (index % 5)])
        corners = [
            cv2.projectPoints(points, rvec, tvec, true_k, distortion)[0]
            .reshape(4, 2)
            .tolist()
            for points in objects
        ]
        name = f"{index:06d}.png"
        frames[name] = {"ids": ids, "corners": corners, "marker_count": len(ids)}
        poses[name] = {"rvec": rvec, "tvec": tvec}
    return {
        "schema_version": "aruco_detections.v1",
        "image_size": [640, 480],
        "frames": frames,
    }, poses


def current_target() -> dict:
    markers = []
    for marker_id in range(6):
        row, column = divmod(marker_id, 3)
        x = column * 40.0
        y = row * 40.0
        markers.append(
            {
                "id": marker_id,
                "corners_mm": [
                    [x, y, 0.0],
                    [x + 30.0, y, 0.0],
                    [x + 30.0, y + 30.0, 0.0],
                    [x, y + 30.0, 0.0],
                ],
            }
        )
    return normalize_calibration_target_spec(
        {
            "schema_version": "calibration_target.v2",
            "target_type": "aruco_grid",
            "dictionary": "DICT_5X5_50",
            "grid_size": [3, 2],
            "unit": "mm",
            "frame": {
                "name": "aruco_grid",
                "origin": "compensated_outer_board_top_left",
                "axes": {"x": "right", "y": "down", "z": "into_board"},
            },
            "target_bounds": {
                "x_mm": 0.0,
                "y_mm": 0.0,
                "width_mm": 110.0,
                "height_mm": 70.0,
            },
            "print_compensation": {
                "x_percent": 100.0,
                "y_percent": 100.0,
                "application": "already_applied",
            },
            "markers": markers,
        }
    )


def test_synthetic_intrinsic_recovery(tmp_path: Path) -> None:
    sensor = tmp_path / "realsense_SERIAL-1"
    sensor_fixture(sensor)
    target = current_target()
    detections, _poses = synthetic_detections(target)

    profile = calibrate_intrinsic_profile(sensor, detections, target)

    assert profile["schema_version"] == "intrinsic_calibration.v1"
    assert profile["quality"]["accepted_view_count"] == 18
    assert len(profile["quality"]["coverage_cells"]) >= 6
    assert profile["quality"]["rms_reprojection_error_px"] < 0.1
    assert np.allclose(
        np.asarray(profile["native"]["cam_K"]).reshape(3, 3),
        np.array([[600.0, 0.0, 320.0], [0.0, 605.0, 240.0], [0.0, 0.0, 1.0]]),
        atol=1.0,
    )
    assert profile["rectified"]["distortion"] == [0.0] * 5
    assert profile["depth"]["alignment"]["recalibrated"] is False


def test_intrinsic_coverage_failure_reports_rejected_audit(tmp_path: Path) -> None:
    sensor = tmp_path / "realsense_SERIAL-1"
    sensor_fixture(sensor)
    target = current_target()
    detections, _poses = synthetic_detections(target, centered=True)

    with pytest.raises(IntrinsicCalibrationError) as captured:
        calibrate_intrinsic_profile(sensor, detections, target)

    assert captured.value.report["status"] == "rejected"
    assert "coverage" in captured.value.report["reason"]
    assert captured.value.report["accepted_views"]


def test_factory_profile_and_exact_identity_selection(tmp_path: Path) -> None:
    sensor = tmp_path / "realsense_SERIAL-1"
    sensor_fixture(sensor)
    profile = factory_intrinsic_profile(sensor)

    selected = select_intrinsic_profile(
        [profile], sensor_id="SERIAL-1", resolution=(640, 480), orientation="normal"
    )

    assert selected["source"]["mode"] == "factory"
    assert selected["depth"]["scale_source"] == "factory_sdk"
    with pytest.raises(ValueError, match="exactly one"):
        select_intrinsic_profile(
            [profile],
            sensor_id="SERIAL-1",
            resolution=(1280, 720),
            orientation="normal",
        )
