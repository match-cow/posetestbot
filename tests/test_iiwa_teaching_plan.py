from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pytest

from posetestbot.calibration.frame_graph import robot_flange_to_template_base
from posetestbot.calibration.teaching_plan import (
    DEFAULT_TEACHING_PLAN_PATH,
    frames_by_name,
    load_teaching_plan,
    relative_result_transform_matrix,
    seed_pose_radians,
    seed_transform_matrix,
    validate_teaching_plan,
)


JAVA_PATH = Path("iiwa/PoseTestBotNineFrameCalibrationApplication.java")
CHECKLIST_PATH = Path("docs/IIWA_CALIBRATION_TEACHING_CHECKLIST.md")


def test_teaching_plan_has_exact_nine_frame_and_phase_contract() -> None:
    plan = load_teaching_plan()
    frames = frames_by_name(plan)

    assert plan["schema_version"] == "iiwa_calibration_teaching_plan.v2"
    assert plan["base_path"] == "/PoseTestBot/TemplateBase"
    assert plan["motion_point"] == "robot_flange"
    assert plan["phase_anchor"] == "CalibrationCenter"
    assert len(frames) == 9
    assert len({frame["path"] for frame in frames.values()}) == 9
    assert all(frame["seed"] is not None for frame in frames.values())
    assert list(frames).count("CalibrationCenter") == 1
    assert "CalibrationReady" not in frames

    phases = {phase["id"]: phase for phase in plan["phases"]}
    assert list(phases) == ["coverage_raster", "orientation_dither"]
    assert [
        (motion["from"], motion["to"], motion["motion_type"])
        for motion in phases["coverage_raster"]["motions"]
    ] == [
        ("CalibrationCenter", "CalibrationCoverageUpperLeft", "PTP"),
        ("CalibrationCoverageUpperLeft", "CalibrationCoverageUpperCenter", "LIN"),
        ("CalibrationCoverageUpperCenter", "CalibrationCoverageUpperRight", "LIN"),
        ("CalibrationCoverageUpperRight", "CalibrationCoverageMiddleRight", "LIN"),
        ("CalibrationCoverageMiddleRight", "CalibrationCenter", "LIN"),
        ("CalibrationCenter", "CalibrationCoverageMiddleLeft", "LIN"),
        ("CalibrationCoverageMiddleLeft", "CalibrationCoverageLowerLeft", "LIN"),
        ("CalibrationCoverageLowerLeft", "CalibrationCoverageLowerCenter", "LIN"),
        ("CalibrationCoverageLowerCenter", "CalibrationCoverageLowerRight", "LIN"),
        ("CalibrationCoverageLowerRight", "CalibrationCenter", "PTP"),
    ]

    orientation = phases["orientation_dither"]
    assert orientation["anchor_frame"] == "CalibrationCenter"
    assert orientation["reference_frame"] == "CalibrationCenter"
    assert orientation["motion_type"] == "LIN_REL"
    assert [
        (motion["delta"]["A"], motion["delta"]["B"], motion["delta"]["C"])
        for motion in orientation["motions"]
    ] == [
        (-15, 0, 0),
        (30, 0, 0),
        (-15, 0, 0),
        (0, -12, 0),
        (0, 24, 0),
        (0, -12, 0),
        (0, 0, -15),
        (0, 0, 30),
        (0, 0, -15),
    ]
    assert [
        tuple(motion["result_offset"].values()) for motion in orientation["motions"]
    ] == [
        (-15, 0, 0),
        (15, 0, 0),
        (0, 0, 0),
        (0, -12, 0),
        (0, 12, 0),
        (0, 0, 0),
        (0, 0, -15),
        (0, 0, 15),
        (0, 0, 0),
    ]


def test_teaching_plan_rejects_translation_or_inconsistent_relative_results() -> None:
    translated = json.loads(DEFAULT_TEACHING_PLAN_PATH.read_text())
    translated["phases"][1]["motions"][0]["delta"]["X"] = 1
    with pytest.raises(ValueError, match="keep XYZ fixed"):
        validate_teaching_plan(translated)

    inconsistent = json.loads(DEFAULT_TEACHING_PLAN_PATH.read_text())
    inconsistent["phases"][1]["motions"][1]["result_offset"]["A"] = 14
    with pytest.raises(ValueError, match="result_offset is inconsistent"):
        validate_teaching_plan(inconsistent)


def test_manifest_frames_and_relative_deltas_match_java() -> None:
    plan = load_teaching_plan()
    java = JAVA_PATH.read_text()

    for frame in plan["frames"]:
        assert re.search(
            rf'private static final String [A-Z0-9_]+_PATH\s*=\s*"{re.escape(frame["path"])}";',
            java,
        ), frame["name"]
    assert (
        'private static final String TEMPLATE_BASE_PATH = "/PoseTestBot/TemplateBase";'
        in java
    )
    assert "new Frame(" not in java
    assert "/HRC_Hub/Template_Base" not in java
    assert "CALIBRATION_READY_PATH" not in java
    assert "CALIBRATION_DEPTH_" not in java
    assert "CALIBRATION_ORIENTATION_" not in java
    assert "robotinfo.setBase(TEMPLATE_BASE_PATH);" in java
    assert "templateBase = requiredFrame(TEMPLATE_BASE_PATH);" in java
    assert "Transformation.ofDeg(0, 0, 0," in java
    assert "linRel(offset," in java
    assert re.search(
        r"linRel\(offset,\s*calibrationCenter\)\s*\.setCartVelocity\(cartVelocityMmS\)",
        java,
    )

    relative_calls = re.findall(
        r"captureRelativeOrientation\((-?\d+), (-?\d+), (-?\d+), cartVelocityMmS,\s*\n?\s*\"([^\"]+)\"\);",
        java,
    )
    assert relative_calls == [
        (
            str(motion["delta"]["A"]),
            str(motion["delta"]["B"]),
            str(motion["delta"]["C"]),
            motion["capture_label"],
        )
        for motion in plan["phases"][1]["motions"]
    ]
    for phase in plan["phases"]:
        for motion in phase["motions"]:
            if motion["capture_label"] is not None:
                assert f'"{motion["capture_label"]}"' in java


def test_calibration_motion_uses_smooth_capture_and_orientation_limits() -> None:
    java = JAVA_PATH.read_text()

    assert "SETTLE_TIME_MS = 1500" in java
    assert "CAPTURE_VELOCITY_SCALE = 0.60" in java
    assert "REPOSITION_PTP_VEL_REL = 0.08" in java
    assert "ORIENTATION_JOINT_VEL_REL = 0.03" in java
    assert "SMOOTH_MOTION_JOINT_ACCEL_REL = 0.03" in java
    assert "SMOOTH_MOTION_JOINT_JERK_REL = 0.03" in java
    assert "MIN_CART_VEL_MM_S = 8.0" in java
    assert "MAX_CART_VEL_MM_S = 30.0" in java
    assert "requestedMmS * CAPTURE_VELOCITY_SCALE" in java
    assert ".setJointVelocityRel(ORIENTATION_JOINT_VEL_REL)" in java
    assert java.count(".setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)") == 4
    assert java.count(".setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL)") == 4
    assert java.count("settleAtCurrentPose(") == 5
    assert 'poseStream.sendCurrentPose(motionName + "_settled")' in java


def test_printable_checklist_has_one_signoff_row_per_taught_frame() -> None:
    plan = load_teaching_plan()
    checklist = CHECKLIST_PATH.read_text()

    assert (
        "Frame path | Created | Seed entered | Touched | XYZABC read-back | "
        "7 joints/redundancy recorded | Reach/joint/singularity OK | "
        "arm/rig/cable clearance OK | required cameras detect target | reviewer/date"
    ) in checklist
    for frame in plan["frames"]:
        assert checklist.count(f"`{frame['path']}`") == 1
    assert "/PoseTestBot/TemplateBase/CalibrationReady`" not in checklist
    assert "all nine orientation motions are `LIN_REL`" in checklist
    assert "--allow-real-robot" in checklist
    assert "--allow-cameras" in checklist
    assert "UDP stop messages cannot interrupt active motion" in checklist
    assert "requires a manual application restart" in checklist
    assert "For eye-in-hand captures" in checklist
    assert "45% image width and 35% image height" in checklist
    assert "research-stage static eye-to-hand captures" in checklist
    assert "15% width, 20% height, and 3% hull area" in checklist
    assert "at least 6/9 image-centroid cells" in checklist


def test_kuka_seed_degrees_and_relative_results_use_known_center_decode() -> None:
    plan = load_teaching_plan()
    center = frames_by_name(plan)["CalibrationCenter"]
    radians = seed_pose_radians(center)

    assert radians["A"] == pytest.approx(-math.pi / 2)
    assert radians["B"] == pytest.approx(math.pi / 6)
    assert radians["C"] == pytest.approx(math.pi)
    transform = seed_transform_matrix(center)
    assert transform[:3, 3] == pytest.approx([0.0, -285.0, 445.0])
    assert transform[:3, :3] == pytest.approx(
        np.array(
            [
                [0.0, -1.0, 0.0],
                [-math.sqrt(3) / 2, 0.0, 0.5],
                [-0.5, 0.0, -math.sqrt(3) / 2],
            ]
        ),
        abs=1e-12,
    )

    alpha_minus = relative_result_transform_matrix(
        center, plan["phases"][1]["motions"][0]["result_offset"]
    )
    expected = robot_flange_to_template_base(
        {
            "X": 0,
            "Y": -285,
            "Z": 445,
            "A": math.radians(-105),
            "B": math.radians(30),
            "C": math.radians(180),
        }
    )
    assert alpha_minus == pytest.approx(expected)
    assert alpha_minus[:3, 3] == pytest.approx(transform[:3, 3])
