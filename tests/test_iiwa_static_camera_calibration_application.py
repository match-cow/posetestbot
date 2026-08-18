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


def test_static_camera_application_uses_base_and_one_extra_taught_point() -> None:
    java = JAVA_PATH.read_text()
    application_data_paths = set(re.findall(r'"(/PoseTestBot/[^"]+)"', java))

    assert application_data_paths == {
        "/PoseTestBot/PoseTemplateBase",
        "/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle",
    }
    assert (
        "poseTemplateBase = requiredFrame(POSE_TEMPLATE_BASE_PATH);" in java
    )
    assert (
        "calibrationStaticBottomMiddle = requiredFrame(\n"
        "\t\t\t\tCALIBRATION_STATIC_BOTTOM_MIDDLE_PATH);" in java
    )
    assert "robotinfo.setBase(POSE_TEMPLATE_BASE_PATH);" in java
    assert "new Frame(" not in java
    assert re.findall(r"private ObjectFrame ([A-Za-z0-9_]+);", java) == [
        "poseTemplateBase",
        "calibrationStaticBottomMiddle",
    ]


def test_static_camera_grid_stays_at_or_above_bottom_middle_in_base_z() -> None:
    java = JAVA_PATH.read_text()

    half_width_match = re.search(r"GRID_HALF_WIDTH_MM = ([0-9.]+);", java)
    row_spacing_match = re.search(r"GRID_ROW_SPACING_MM = ([0-9.]+);", java)
    center_offset_match = re.search(
        r"PATTERN_CENTER_Z_OFFSET_MM = ([0-9.]+);", java
    )
    relative_limit_match = re.search(
        r"MAX_RELATIVE_TRANSLATION_MM = ([0-9.]+);", java
    )
    bottom_limit_match = re.search(
        r"MAX_BOTTOM_MIDDLE_TRANSLATION_MM = ([0-9.]+);", java
    )
    start_limit_match = re.search(r"MAX_START_TRANSLATION_MM = ([0-9.]+);", java)
    assert half_width_match is not None
    assert row_spacing_match is not None
    assert center_offset_match is not None
    assert relative_limit_match is not None
    assert bottom_limit_match is not None
    assert start_limit_match is not None
    half_width = float(half_width_match.group(1))
    row_spacing = float(row_spacing_match.group(1))
    center_offset = float(center_offset_match.group(1))
    relative_limit = float(relative_limit_match.group(1))
    bottom_limit = float(bottom_limit_match.group(1))
    start_limit = float(start_limit_match.group(1))

    assert half_width == 65.0
    assert row_spacing == center_offset == 50.0
    assert math.hypot(half_width, row_spacing) < relative_limit == 100.0
    assert math.hypot(half_width, 2.0 * row_spacing) < bottom_limit == 125.0
    assert start_limit == 25.0 < bottom_limit
    assert "radiusMm > MAX_RELATIVE_TRANSLATION_MM" in java
    assert "PATTERN_CENTER_Z_OFFSET_MM != GRID_ROW_SPACING_MM" in java
    assert "furthestPointFromBottomMm > MAX_BOTTOM_MIDDLE_TRANSLATION_MM" in java
    assert "zFromBottomMiddleMm < 0.0" in java
    assert "validateProgramEnvelope();" in java
    assert "DEPTH_DITHER_MM" not in java
    assert "runRelativeDepthDither" not in java

    grid_body = java.split("private void runRelativePlanarGrid", 1)[1].split(
        "private void capturePlanarGridLeg", 1
    )[0]
    grid_calls = [
        tuple(value.strip() for value in match)
        for match in re.findall(
            r"capturePlanarGridLeg\(\s*"
            r"([^,]+),\s*([^,]+),\s*"
            r"([^,]+),\s*([^,]+),\s*"
            r'cartVelocityMmS,\s*"([^"]+)"\);',
            grid_body,
        )
    ]
    grid_route = [
        ("0.0", "0.0"),
        ("-GRID_HALF_WIDTH_MM", "0.0"),
        ("-GRID_HALF_WIDTH_MM", "PATTERN_CENTER_Z_OFFSET_MM"),
        ("-GRID_HALF_WIDTH_MM", "2.0 * GRID_ROW_SPACING_MM"),
        ("0.0", "2.0 * GRID_ROW_SPACING_MM"),
        ("GRID_HALF_WIDTH_MM", "2.0 * GRID_ROW_SPACING_MM"),
        ("GRID_HALF_WIDTH_MM", "PATTERN_CENTER_Z_OFFSET_MM"),
        ("GRID_HALF_WIDTH_MM", "0.0"),
        ("0.0", "PATTERN_CENTER_Z_OFFSET_MM"),
    ]
    assert [call[:2] for call in grid_calls] == grid_route[:-1]
    assert [call[2:4] for call in grid_calls] == grid_route[1:]
    assert [call[4] for call in grid_calls] == [
        "grid_bottom_middle_to_bottom_left",
        "grid_bottom_left_to_middle_left",
        "grid_middle_left_to_top_left",
        "grid_top_left_to_top_center",
        "grid_top_center_to_top_right",
        "grid_top_right_to_middle_right",
        "grid_middle_right_to_bottom_right",
        "grid_bottom_right_to_center",
    ]
    grid_helper = java.split("private void capturePlanarGridLeg", 1)[1].split(
        "private void runRelativeOrientationDither", 1
    )[0]
    assert "toXMm - fromXMm, 0.0, toZMm - fromZMm," in grid_helper
    assert grid_helper.count("requireGridPointInsideEnvelope(") == 2
    assert "toYMm" not in grid_helper


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
        capture_body.index("runRelativePlanarGrid(cartVelocityMmS)")
    )
    assert capture_body.index("runRelativePlanarGrid(cartVelocityMmS)") < (
        capture_body.index("runRelativeOrientationDither(cartVelocityMmS)")
    )
    assert capture_body.index("runRelativeOrientationDither(cartVelocityMmS)") < (
        capture_body.index("moveFromPatternCenterToBottomMiddle(cartVelocityMmS)")
    )
    assert capture_body.index(
        "moveFromPatternCenterToBottomMiddle(cartVelocityMmS)"
    ) < capture_body.index("poseStream.finishCapture();")
    assert 'moveToBottomMiddle("capture end anchor confirmation")' not in java
    assert "runRelativeDepthDither" not in java
    assert "robot.getCurrentCartesianPosition(" in java
    assert "radiusMm > MAX_START_TRANSLATION_MM" in java
    assert "moveFromBottomMiddleToPatternCenter" not in java

    bottom_move_body = java.split(
        "private void moveFromPatternCenterToBottomMiddle", 1
    )[1].split("private void runRelativePlanarGrid", 1)[0]
    assert (
        "0.0, PATTERN_CENTER_Z_OFFSET_MM,\n"
        "\t\t\t\t0.0, 0.0,"
        in bottom_move_body
    )
    assert '"pattern_center_to_bottom_middle"' in bottom_move_body


def test_static_camera_relative_motion_and_pose_stream_contracts_are_preserved() -> (
    None
):
    java = JAVA_PATH.read_text()
    translation_body = java.split(
        "private void captureRelativeTranslation", 1
    )[1].split("private void captureRelativeOrientation", 1)[0]
    orientation_body = java.split(
        "private void captureRelativeOrientation", 1
    )[1].split("private void captureRelativeMotion", 1)[0]
    relative_motion_body = java.split(
        "private void captureRelativeMotion", 1
    )[1].split("private void settleAtCurrentPose", 1)[0]

    assert "Transformation.ofDeg(" in java
    assert "linRel(offset, referenceFrame)" in java
    assert "offset, poseTemplateBase, cartVelocityMmS, motionName" in translation_body
    assert "calibrationStaticBottomMiddle," in orientation_body
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
    assert translation_body.index(
        "requireInsideRelativeTranslationEnvelope("
    ) < (
        translation_body.index("captureRelativeMotion(")
    )
    assert "requireInsideRelativeTranslationEnvelope(" not in orientation_body
    assert "poseStream.startMotion(motionName);" in relative_motion_body


def test_static_camera_program_uses_ordered_center_orientation_sweeps() -> None:
    java = JAVA_PATH.read_text()
    orientation_body = java.split("private void runRelativeOrientationDither", 1)[
        1
    ].split("private void captureOrientationLeg", 1)[0]

    assert "DEPTH_DITHER_MM" not in java
    assert "captureDepthLeg" not in java
    orientation_calls = [
        tuple(value.strip() for value in match)
        for match in re.findall(
            r"captureOrientationLeg\(\s*"
            r"([^,]+),\s*([^,]+),\s*([^,]+),\s*"
            r'cartVelocityMmS,\s*"([^"]+)"\);',
            orientation_body,
        )
    ]
    dither = "ORIENTATION_DITHER_DEG"
    orientation_deltas = [
        (f"-{dither}", "0.0", "0.0"),
        (f"2.0 * {dither}", "0.0", "0.0"),
        (f"-{dither}", "0.0", "0.0"),
        ("0.0", f"-{dither}", "0.0"),
        ("0.0", f"2.0 * {dither}", "0.0"),
        ("0.0", f"-{dither}", "0.0"),
        ("0.0", "0.0", f"-{dither}"),
        ("0.0", "0.0", f"2.0 * {dither}"),
        ("0.0", "0.0", f"-{dither}"),
    ]
    assert [call[:3] for call in orientation_calls] == orientation_deltas
    assert [call[3] for call in orientation_calls] == [
        "orientation_alpha_center_to_minus",
        "orientation_alpha_minus_to_plus",
        "orientation_alpha_plus_to_center",
        "orientation_beta_center_to_minus",
        "orientation_beta_minus_to_plus",
        "orientation_beta_plus_to_center",
        "orientation_gamma_center_to_minus",
        "orientation_gamma_minus_to_plus",
        "orientation_gamma_plus_to_center",
    ]
    assert orientation_body.count("2.0 * ORIENTATION_DITHER_DEG") == 3
    assert "ORIENTATION_DITHER_DEG = 10.0" in java
    assert "captureRelativeOrientation(" in java
    assert "-alphaDeg, -betaDeg, -gammaDeg," not in java
