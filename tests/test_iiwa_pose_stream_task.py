from __future__ import annotations

from pathlib import Path


TASK_PATH = Path("iiwa/PoseTestBotPoseStreamTask.java")
INTERFACE_PATH = Path("iiwa/PoseTestBotPoseStreamFunction.java")
ORDINARY_PATH = Path("iiwa/PoseTestBotFullCaptureApplication.java")
CALIBRATION_PATH = Path("iiwa/PoseTestBotNineFrameCalibrationApplication.java")
STATIC_CALIBRATION_PATH = Path(
    "iiwa/PoseTestBotSingleFrameStaticCameraCalibrationApplication.java"
)


def test_pose_stream_is_an_automatic_compatible_read_only_cyclic_task() -> None:
    task = TASK_PATH.read_text()
    interface = INTERFACE_PATH.read_text()

    assert "extends RoboticsAPICyclicBackgroundTask" in task
    assert "@TaskFunctionProvider" in task
    assert "implements PoseTestBotPoseStreamFunction" in task
    assert "TARGET_PERIOD_MS = 10" in task
    assert "CycleBehavior.BestEffort" in task
    assert "initializeCyclic(0, TARGET_PERIOD_MS" in task
    assert "robot.getCurrentCartesianPosition(" in task
    assert "robot.move(" not in task
    assert "moveAsync(" not in task
    assert "IMotionContainer" not in task
    assert " move(" not in interface
    assert " moveAsync(" not in interface


def test_motion_apps_do_not_poll_completion_in_the_sampling_hot_path() -> None:
    ordinary = ORDINARY_PATH.read_text()
    calibration = CALIBRATION_PATH.read_text()
    static_calibration = STATIC_CALIBRATION_PATH.read_text()

    for java in (ordinary, calibration, static_calibration):
        assert "getTaskFunction(" in java
        assert "poseStream.startMotion(" in java
        assert "poseStream.stopMotion()" in java
        assert "poseStream.finishCapture()" in java
        assert "moveAsync(" not in java
        assert ".isFinished()" not in java
    assert "robot.move(ptp(jointTarget(A1_MAX_RAD))" in ordinary
    assert "robot.move(lin(target)" in calibration
    assert "robot.move(linRel(offset, calibrationCenter)" in calibration
    assert "robot.move(linRel(offset)" in static_calibration
    assert "linRel(offset," not in static_calibration
    assert "poseTemplateBase = requiredFrame(POSE_TEMPLATE_BASE_PATH);" in (
        static_calibration
    )


def test_v1_packets_carry_target_and_measured_sender_cadence() -> None:
    task = TASK_PATH.read_text()

    assert 'jsonObject.put("sender_target_period_ms"' in task
    assert 'jsonObject.put("sender_previous_pose_delta_ns"' in task
    assert 'jsonObject.put("sender_pose_query_duration_ns"' in task
    assert "previousPoseStartedNs" in task
    assert "maximumPoseDeltaNs" in task
    assert "maximumPoseQueryDurationNs" in task


def test_cyclic_exceptions_are_contained_and_exposed_to_the_motion_app() -> None:
    task = TASK_PATH.read_text()
    ordinary = ORDINARY_PATH.read_text()
    calibration = CALIBRATION_PATH.read_text()
    static_calibration = STATIC_CALIBRATION_PATH.read_text()

    assert "catch (RuntimeException e) {" in task
    assert 'recordFatalFailure("cyclic pose acquisition", e);' in task
    assert "streaming = false;" in task
    assert "getFatalFailureCount()" in ordinary
    assert "getFatalFailureCount()" in calibration
    assert "getFatalFailureCount()" in static_calibration
    assert "segmentPoseCount <= 0L" in ordinary
    assert "segmentPoseCount <= 0L" in calibration
    assert "segmentPoseCount <= 0L" in static_calibration
