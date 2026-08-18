from __future__ import annotations

from pathlib import Path


JAVA_PATH = Path("iiwa/PoseTestBotFullCaptureApplication.java")
DOC_PATH = Path("docs/IIWA_FULL_CAPTURE_APPLICATION.md")
CALIBRATION_JAVA_PATH = Path("iiwa/PoseTestBotNineFrameCalibrationApplication.java")
POSE_STREAM_JAVA_PATH = Path("iiwa/PoseTestBotPoseStreamTask.java")


def test_ordinary_capture_uses_a_distinct_persistent_pose_template_frame() -> None:
    java = JAVA_PATH.read_text()
    calibration_java = CALIBRATION_JAVA_PATH.read_text()

    assert 'POSE_TEMPLATE_BASE_PATH =\n\t\t\t"/PoseTestBot/PoseTemplateBase";' in java
    assert "poseTemplateBase = requiredFrame(POSE_TEMPLATE_BASE_PATH);" in java
    assert "robotinfo.setBase(POSE_TEMPLATE_BASE_PATH);" in java
    assert "new Frame(" not in java
    assert "/HRC_Hub/Template_Base" not in java
    assert 'TEMPLATE_BASE_PATH = "/PoseTestBot/TemplateBase";' in calibration_java
    assert '"/PoseTestBot/PoseTemplateBase";' in calibration_java
    assert "robotinfo.setBase(TEMPLATE_BASE_PATH);" in calibration_java
    assert (
        "poseTemplateBase = requiredFrame(POSE_TEMPLATE_BASE_PATH);" in calibration_java
    )
    assert "command.runId,\n\t\t\t\t\t\tPOSE_TEMPLATE_BASE_PATH);" in (calibration_java)


def test_ordinary_capture_waits_for_start_and_does_not_move_before_it() -> None:
    java = JAVA_PATH.read_text()
    run_body = java.split("public void run()", 1)[1].split(
        "private void runCapture", 1
    )[0]

    assert run_body.index("waitForStartCommand()") < run_body.index(
        "runCapture(command)"
    )
    assert "robot.move(" not in run_body
    assert "robot.getCurrentJointPosition()" not in run_body
    assert "moveToA1Min();" not in run_body
    assert "No robot motion occurs before an accepted" in java
    assert "UDP STOP is not a safety stop" in java


def test_nine_frame_calibration_waits_for_start_before_center_motion() -> None:
    java = CALIBRATION_JAVA_PATH.read_text()
    initialize_body = java.split("public void initialize()", 1)[1].split(
        "private ObjectFrame requiredFrame", 1
    )[0]
    run_body = java.split("public void run()", 1)[1].split(
        "private void runCoverageRaster", 1
    )[0]

    assert "robot.move(" not in initialize_body
    assert run_body.index("waitForStartCommand()") < run_body.index(
        "poseStream.configure("
    )
    assert run_body.index("poseStream.configure(") < run_body.index(
        'moveToCenter("capture start anchor")'
    )
    before_start_wait = run_body[: run_body.index("waitForStartCommand()")]
    assert "moveToCenter(" not in before_start_wait
    assert "moveFromCenter(" not in before_start_wait


def test_capture_uses_commissioned_start_and_end_ptp_frames() -> None:
    java = JAVA_PATH.read_text()
    capture_body = java.split("private void runCapture", 1)[1].split(
        "private void moveToCommissionedFrame", 1
    )[0]

    assert '"/PoseTestBot/CaptureStart"' in java
    assert '"/PoseTestBot/CaptureEnd"' in java
    assert "captureStartFrame = requiredFrame(CAPTURE_START_FRAME_PATH);" in java
    assert "captureEndFrame = requiredFrame(CAPTURE_END_FRAME_PATH);" in java
    assert "robot.move(ptp(targetFrame)" in java
    assert capture_body.index("captureStartFrame") < capture_body.index(
        "robot.getCurrentJointPosition()"
    )
    assert capture_body.index("robot.getCurrentJointPosition()") < capture_body.index(
        "moveToA1Min();"
    )
    assert capture_body.index('poseStream.startMotion("a1_capture_sweep")') < (
        capture_body.index("captureEndFrame")
    )
    assert capture_body.index("captureEndFrame") < capture_body.index(
        "poseStream.finishCapture();"
    )


def test_cartesian_command_is_converted_before_joint_velocity_is_applied() -> None:
    java = JAVA_PATH.read_text()

    assert "cartesianVelocityMps * 1000.0 / orbitRadiusMm" in java
    assert "/ A1_FULL_SPEED_UPPER_BOUND_RAD_S" in java
    assert "Math.toRadians(98.0)" in java
    assert "MAX_CAPTURE_CARTESIAN_VELOCITY_M_S" not in java
    assert "Math.toRadians(3.0)" in java
    assert "Math.min(\n\t\t\t\trequestedAngularVelocityRadS" in java
    assert ".setJointVelocityRel(captureJointVelocityRel)" in java
    assert ".setJointVelocityRel(command.cartesianVelocityMps)" not in java
    assert ".setJointVelocityRel(velocity.doubleValue())" not in java


def test_v1_pose_packets_bind_sequence_run_and_frame_identity() -> None:
    java = JAVA_PATH.read_text()
    pose_stream_java = POSE_STREAM_JAVA_PATH.read_text()

    for field in (
        "schema_version",
        "packet_kind",
        "sequence",
        "sender_monotonic_ns",
        "sender_wall_timestamp_ms",
        "run_id",
        "from_frame",
        "to_frame",
        "sunrise_reference_frame_path",
    ):
        assert f'jsonObject.put("{field}"' in pose_stream_java
    assert 'jsonObject.put("from_frame", "robot_flange")' in pose_stream_java
    assert 'jsonObject.put("to_frame", "template_base")' in pose_stream_java
    assert "END_PACKET_COUNT = 3" in pose_stream_java
    assert "command.runId," in java
    assert "POSE_TEMPLATE_BASE_PATH);" in java


def test_network_and_interrupt_failures_are_observable() -> None:
    java = JAVA_PATH.read_text()
    pose_stream_java = POSE_STREAM_JAVA_PATH.read_text()
    combined = java + pose_stream_java

    assert "catch (SocketException e) {\n\t\t}" not in combined
    assert "catch (IOException e) {\n\t\t}" not in combined
    assert "catch (InterruptedException e) {\n\t\t}" not in combined
    assert "recordSendFailure(" in pose_stream_java
    assert "All end-marker transmissions failed" in pose_stream_java
    assert "Thread.currentThread().interrupt()" in combined
    assert 'throw new IllegalArgumentException("receiver_ip is required")' in java
    assert "DEFAULT_RECEIVER_IP" not in java


def test_full_capture_document_explains_frame_and_static_profile_boundary() -> None:
    document = DOC_PATH.read_text()
    normalized = " ".join(document.split())

    assert "`/PoseTestBot/TemplateBase`" in document
    assert "`/PoseTestBot/PoseTemplateBase`" in document
    assert "`template_base_from_pose_template` is identity" in document
    assert "must not be relabelled" in document
    assert (
        "Host receive/wall timestamps remain the synchronization authority"
        in normalized
    )
    assert "0.03 m/s" in document
    assert "1.00 m/s" in document
    assert "`robot_command.v1`" in document
    assert "3°/s" in document
    assert "Speed alone cannot guarantee blur-free images" in normalized
    assert "cannot interrupt the active A1 motion" in document
