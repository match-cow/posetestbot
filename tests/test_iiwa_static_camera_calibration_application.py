from __future__ import annotations

import math
import re
from pathlib import Path


JAVA_PATH = Path("iiwa/PoseTestBotSingleFrameStaticCameraCalibrationApplication.java")


def test_iiwa_application_names_describe_their_runtime_roles() -> None:
    applications = {path.name for path in Path("iiwa").glob("*Application.java")}

    assert applications == {
        "PoseTestBotFullCaptureApplication.java",
        "PoseTestBotNineFrameCalibrationApplication.java",
        "PoseTestBotSingleFrameStaticCameraCalibrationApplication.java",
    }
    assert Path("iiwa/PoseTestBotPoseStreamTask.java").is_file()
    for path in [
        *(Path("iiwa").glob("*Application.java")),
        Path("iiwa/PoseTestBotPoseStreamTask.java"),
    ]:
        java = path.read_text()
        assert f"public class {path.stem}" in java


def test_iiwa_application_initialization_never_commands_motion() -> None:
    for path in Path("iiwa").glob("*Application.java"):
        java = path.read_text()
        initialize_body = java.split("public void initialize()", 1)[1].split(
            "private ObjectFrame requiredFrame", 1
        )[0]
        assert "robot.move(" not in initialize_body, path.name


def test_static_camera_application_requires_only_the_taught_bottom_middle() -> None:
    java = JAVA_PATH.read_text()
    application_data_paths = set(re.findall(r'"(/PoseTestBot/[^"]+)"', java))

    assert application_data_paths == {
        "/PoseTestBot/PoseTemplateBase",
        "/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle",
    }
    assert (
        "calibrationStaticBottomMiddle = requiredFrame(\n"
        "\t\t\t\tCALIBRATION_STATIC_BOTTOM_MIDDLE_PATH);" in java
    )
    assert "robotinfo.setBase(POSE_TEMPLATE_BASE_PATH);" in java
    assert "new Frame(" not in java
    assert re.findall(r"private ObjectFrame ([A-Za-z0-9_]+);", java) == [
        "calibrationStaticBottomMiddle"
    ]


def test_static_camera_pattern_stays_above_the_taught_bottom_middle() -> None:
    java = JAVA_PATH.read_text()

    half_span_match = re.search(r"GRID_HALF_SPAN_MM = ([0-9.]+);", java)
    depth_match = re.search(r"DEPTH_DITHER_MM = ([0-9.]+);", java)
    center_offset_match = re.search(
        r"PATTERN_CENTER_Z_OFFSET_MM = ([0-9.]+);", java
    )
    limit_match = re.search(r"MAX_CENTER_TRANSLATION_MM = ([0-9.]+);", java)
    bottom_limit_match = re.search(
        r"MAX_BOTTOM_MIDDLE_TRANSLATION_MM = ([0-9.]+);", java
    )
    start_limit_match = re.search(r"MAX_START_TRANSLATION_MM = ([0-9.]+);", java)
    assert half_span_match is not None
    assert depth_match is not None
    assert center_offset_match is not None
    assert limit_match is not None
    assert bottom_limit_match is not None
    assert start_limit_match is not None
    half_span = float(half_span_match.group(1))
    depth = float(depth_match.group(1))
    center_offset = float(center_offset_match.group(1))
    limit = float(limit_match.group(1))
    bottom_limit = float(bottom_limit_match.group(1))
    start_limit = float(start_limit_match.group(1))

    assert half_span == 65.0
    assert math.hypot(half_span, half_span) < limit == 100.0
    assert center_offset == depth == 50.0 < limit
    assert center_offset - depth == 0.0
    assert math.sqrt(2 * half_span**2 + center_offset**2) < bottom_limit == 110.0
    assert center_offset + depth < bottom_limit
    assert start_limit == 25.0 < bottom_limit
    assert "radiusMm > MAX_CENTER_TRANSLATION_MM" in java
    assert "PATTERN_CENTER_Z_OFFSET_MM != DEPTH_DITHER_MM" in java
    assert "furthestPointFromBottomMm > MAX_BOTTOM_MIDDLE_TRANSLATION_MM" in java
    assert "zFromBottomMiddleMm < 0.0" in java
    assert (
        "bottomMiddleRadiusMm\n"
        "\t\t\t\t\t\t> MAX_BOTTOM_MIDDLE_TRANSLATION_MM" in java
    )
    assert "validateProgramEnvelope();" in java

    grid_body = java.split("private void runRelativePlanarGrid", 1)[1].split(
        "private void captureGridPoint", 1
    )[0]
    grid_calls = re.findall(
        r"captureGridPoint\(([^,]+), ([^,]+),\s*"
        r'cartVelocityMmS, "([^"]+)"\);',
        grid_body,
    )
    assert grid_calls == [
        ("-GRID_HALF_SPAN_MM", "GRID_HALF_SPAN_MM", "grid_upper_left"),
        ("0.0", "GRID_HALF_SPAN_MM", "grid_upper_center"),
        ("GRID_HALF_SPAN_MM", "GRID_HALF_SPAN_MM", "grid_upper_right"),
        ("GRID_HALF_SPAN_MM", "0.0", "grid_middle_right"),
        ("GRID_HALF_SPAN_MM", "-GRID_HALF_SPAN_MM", "grid_lower_right"),
        ("0.0", "-GRID_HALF_SPAN_MM", "grid_lower_center"),
        ("-GRID_HALF_SPAN_MM", "-GRID_HALF_SPAN_MM", "grid_lower_left"),
        ("-GRID_HALF_SPAN_MM", "0.0", "grid_middle_left"),
    ]
    assert "captureRelativePose(-xMm, -yMm, 0.0, 0.0, 0.0, 0.0," in java

    depth_body = java.split("private void runRelativeDepthDither", 1)[1].split(
        "private void captureDepthPoint", 1
    )[0]
    assert re.findall(
        r"captureDepthPoint\(([^,]+), cartVelocityMmS,\s*" r'"([^"]+)"\);',
        depth_body,
    ) == [
        ("DEPTH_DITHER_MM", "depth_plus"),
        ("-DEPTH_DITHER_MM", "depth_minus"),
    ]


def test_static_camera_program_waits_for_start_before_any_robot_motion() -> None:
    java = JAVA_PATH.read_text()
    run_body = java.split("public void run()", 1)[1].split(
        "private void runCapture", 1
    )[0]
    capture_body = java.split("private void runCapture", 1)[1].split(
        "private void runRelativePlanarGrid", 1
    )[0]

    assert run_body.index("waitForStartCommand()") < run_body.index(
        "runCapture(command)"
    )
    assert "robot.move(" not in run_body
    assert capture_body.index("requireCurrentPositionNearBottomMiddle();") < (
        capture_body.index("poseStream.configure(")
    )
    assert capture_body.index("poseStream.configure(") < capture_body.index(
        'moveToBottomMiddle("capture start anchor")'
    )
    assert capture_body.index('moveToBottomMiddle("capture start anchor")') < (
        capture_body.index("moveFromBottomMiddleToPatternCenter(cartVelocityMmS)")
    )
    assert capture_body.index(
        "moveFromBottomMiddleToPatternCenter(cartVelocityMmS)"
    ) < (
        capture_body.index("runRelativePlanarGrid(cartVelocityMmS)")
    )
    assert capture_body.index("runRelativePlanarGrid(cartVelocityMmS)") < (
        capture_body.index("runRelativeDepthDither(cartVelocityMmS)")
    )
    assert capture_body.index("runRelativeDepthDither(cartVelocityMmS)") < (
        capture_body.index("runRelativeOrientationDither(cartVelocityMmS)")
    )
    assert capture_body.index("runRelativeOrientationDither(cartVelocityMmS)") < (
        capture_body.index("moveFromPatternCenterToBottomMiddle(cartVelocityMmS)")
    )
    assert capture_body.index(
        "moveFromPatternCenterToBottomMiddle(cartVelocityMmS)"
    ) < (
        capture_body.index(
            'moveToBottomMiddle("capture end anchor confirmation")'
        )
    )
    assert capture_body.index(
        'moveToBottomMiddle("capture end anchor confirmation")'
    ) < (
        capture_body.index("poseStream.finishCapture();")
    )
    assert "robot.getCurrentCartesianPosition(" in java
    assert "radiusMm > MAX_START_TRANSLATION_MM" in java

    center_move_body = java.split(
        "private void moveFromBottomMiddleToPatternCenter", 1
    )[1].split("private void runRelativePlanarGrid", 1)[0]
    assert (
        "captureRelativePose(0.0, 0.0, PATTERN_CENTER_Z_OFFSET_MM,"
        in center_move_body
    )
    assert '"bottom_middle_to_pattern_center"' in center_move_body

    bottom_move_body = java.split(
        "private void moveFromPatternCenterToBottomMiddle", 1
    )[1].split("private void runRelativePlanarGrid", 1)[0]
    assert (
        "captureRelativePose(0.0, 0.0, -PATTERN_CENTER_Z_OFFSET_MM,"
        in bottom_move_body
    )
    assert '"pattern_center_to_bottom_middle"' in bottom_move_body


def test_static_camera_relative_motion_and_pose_stream_contracts_are_preserved() -> (
    None
):
    java = JAVA_PATH.read_text()
    relative_motion_body = java.split("private void captureRelativePose", 1)[1].split(
        "private void settleAtCurrentPose", 1
    )[0]

    assert "Transformation.ofDeg(" in java
    assert "linRel(offset, calibrationStaticBottomMiddle)" in java
    assert "new Frame(" not in java
    assert "command.runId,\n\t\t\t\tPOSE_TEMPLATE_BASE_PATH);" in java
    assert "poseStream.startMotion(motionName);" in java
    assert "sentPoseCount = poseStream.stopMotion();" in java
    assert 'poseStream.sendCurrentPose(motionName + "_settled")' in java
    assert "SETTLE_TIME_MS = 1500" in java
    assert "CAPTURE_VELOCITY_SCALE = 0.60" in java
    assert "RELATIVE_MOTION_JOINT_VEL_REL = 0.03" in java
    assert ".setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)" in java
    assert ".setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL)" in java
    assert "moveAsync(" not in java
    assert ".isFinished()" not in java
    assert relative_motion_body.index(
        "requireInsideRelativeTranslationEnvelope("
    ) < (
        relative_motion_body.index(
            "linRel(offset, calibrationStaticBottomMiddle)"
        )
    )


def test_static_camera_program_adds_depth_and_multi_axis_orientation_diversity() -> (
    None
):
    java = JAVA_PATH.read_text()
    orientation_body = java.split("private void runRelativeOrientationDither", 1)[
        1
    ].split("private void captureOrientationPoint", 1)[0]

    assert "captureDepthPoint(DEPTH_DITHER_MM" in java
    assert "captureDepthPoint(-DEPTH_DITHER_MM" in java
    assert orientation_body.count("captureOrientationPoint(") == 6
    assert "ORIENTATION_DITHER_DEG = 10.0" in java
    assert "-alphaDeg, -betaDeg, -gammaDeg," in java
    assert "captureRelativePose(0.0, 0.0, 0.0," in java
