package application;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketException;
import java.nio.charset.Charset;

import javax.inject.Inject;

import com.kuka.roboticsAPI.applicationModel.RoboticsAPIApplication;
import static com.kuka.roboticsAPI.motionModel.BasicMotions.*;

import com.kuka.roboticsAPI.deviceModel.LBR;
import com.kuka.roboticsAPI.geometricModel.Frame;
import com.kuka.roboticsAPI.geometricModel.ObjectFrame;
import com.kuka.roboticsAPI.geometricModel.math.Transformation;
import com.kuka.roboticsAPI.persistenceModel.templateModel.InfoTemplate;

import org.json.simple.JSONObject;
import org.json.simple.parser.JSONParser;

/**
 * Static-camera calibration application with one taught motion frame.
 *
 * Teach only /PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle at
 * the minimum local-Z extent of the calibration pattern. Its +Z axis must
 * point from that bottom position toward the available workspace. The program
 * moves +50 mm from the taught frame to the generated pattern center before
 * running the 3 x 3 planar grid, depth dither, and center swivel motions. This
 * keeps every generated endpoint at or above the taught local-Z position.
 *
 * IMPORTANT: This source does not establish which application is deployed on
 * the lab controller. Compile and simulate it in the exact Sunrise.Workbench
 * project, then validate the frame, every relative endpoint, swept path,
 * target visibility, joint branch, singularity margin, tool/load, fixture,
 * and cable clearance in T1 before deployment.
 *
 * The UDP command is read only while the application is idle. A UDP STOP
 * cannot interrupt active motion and is not a safety stop.
 */
public class PoseTestBotSingleFrameStaticCameraCalibrationApplication
		extends RoboticsAPIApplication {
	private static final String POSE_TEMPLATE_BASE_PATH =
			"/PoseTestBot/PoseTemplateBase";
	private static final String CALIBRATION_STATIC_BOTTOM_MIDDLE_PATH =
			"/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle";
	private static final String DEFAULT_RECEIVER_IP = "172.31.1.169";
	private static final Charset UTF_8 = Charset.forName("UTF-8");

	private static final int SETTLE_TIME_MS = 1500;
	private static final int COMMAND_BUFFER_BYTES = 4096;
	private static final int ROBOT_PORT = 30300;
	private static final int DEFAULT_RECEIVER_PORT = 8080;
	private static final int SETTLED_PACKET_COUNT = 3;
	private static final int SETTLED_PACKET_INTERVAL_MS = 50;

	private static final double GRID_HALF_SPAN_MM = 65.0;
	private static final double DEPTH_DITHER_MM = 50.0;
	private static final double PATTERN_CENTER_Z_OFFSET_MM = 50.0;
	private static final double ORIENTATION_DITHER_DEG = 10.0;
	private static final double MAX_CENTER_TRANSLATION_MM = 100.0;
	private static final double MAX_BOTTOM_MIDDLE_TRANSLATION_MM = 110.0;
	private static final double MAX_START_TRANSLATION_MM = 25.0;

	private static final double CAPTURE_VELOCITY_SCALE = 0.60;
	private static final double REPOSITION_PTP_VEL_REL = 0.08;
	private static final double RELATIVE_MOTION_JOINT_VEL_REL = 0.03;
	private static final double SMOOTH_MOTION_JOINT_ACCEL_REL = 0.03;
	private static final double SMOOTH_MOTION_JOINT_JERK_REL = 0.03;
	private static final double MIN_CART_VEL_MM_S = 8.0;
	private static final double MAX_CART_VEL_MM_S = 30.0;

	@Inject
	private LBR robot;
	@Inject
	private InfoTemplate robotinfo;

	private ObjectFrame calibrationStaticBottomMiddle;
	private PoseTestBotPoseStreamFunction poseStream;

	@Override
	public void initialize() {
		robot = getContext().getDeviceFromType(LBR.class);
		robotinfo.setBase(POSE_TEMPLATE_BASE_PATH);
		calibrationStaticBottomMiddle = requiredFrame(
				CALIBRATION_STATIC_BOTTOM_MIDDLE_PATH);
		validateProgramEnvelope();

		getLogger().info("Resolved single taught static-camera calibration "
				+ "frame: " + CALIBRATION_STATIC_BOTTOM_MIDDLE_PATH);
		getLogger().info("Configured pose-stream reference: "
				+ POSE_TEMPLATE_BASE_PATH);
	}

	private ObjectFrame requiredFrame(String path) {
		ObjectFrame frame = getApplicationData().getFrame(path);
		if (frame == null) {
			throw new IllegalStateException(
					"Required Application Data frame is missing: " + path);
		}
		return frame;
	}

	private void validateProgramEnvelope() {
		double cornerRadiusMm = Math.sqrt(
				2.0 * GRID_HALF_SPAN_MM * GRID_HALF_SPAN_MM);
		if (cornerRadiusMm > MAX_CENTER_TRANSLATION_MM) {
			throw new IllegalStateException("Relative grid corner radius is "
					+ cornerRadiusMm + " mm; limit is "
					+ MAX_CENTER_TRANSLATION_MM + " mm");
		}
		if (DEPTH_DITHER_MM > MAX_CENTER_TRANSLATION_MM) {
			throw new IllegalStateException("Depth dither exceeds the "
					+ MAX_CENTER_TRANSLATION_MM + " mm center envelope");
		}
		if (PATTERN_CENTER_Z_OFFSET_MM != DEPTH_DITHER_MM) {
			throw new IllegalStateException("Pattern-center local-Z offset must "
					+ "equal the depth half-span so the taught frame remains the "
					+ "bottom of the pattern");
		}
		double gridCornerFromBottomMm = translationRadiusMm(
				GRID_HALF_SPAN_MM,
				GRID_HALF_SPAN_MM,
				PATTERN_CENTER_Z_OFFSET_MM);
		double depthTopFromBottomMm =
				PATTERN_CENTER_Z_OFFSET_MM + DEPTH_DITHER_MM;
		double furthestPointFromBottomMm = Math.max(
				gridCornerFromBottomMm, depthTopFromBottomMm);
		if (furthestPointFromBottomMm > MAX_BOTTOM_MIDDLE_TRANSLATION_MM) {
			throw new IllegalStateException("Generated calibration endpoint is "
					+ furthestPointFromBottomMm
					+ " mm from CalibrationStaticBottomMiddle; limit is "
					+ MAX_BOTTOM_MIDDLE_TRANSLATION_MM + " mm");
		}
		if (MAX_START_TRANSLATION_MM <= 0.0
				|| MAX_START_TRANSLATION_MM
						> MAX_BOTTOM_MIDDLE_TRANSLATION_MM) {
			throw new IllegalStateException("Start-position tolerance must be "
					+ "positive and no greater than the bottom-middle envelope");
		}
	}

	@Override
	public void run() {
		getLogger().warn("Before the first start command, manually position the "
				+ "robot at or near the taught CalibrationStaticBottomMiddle pose "
				+ "with the attached target visible in the intended static camera. "
				+ "This is an operator commissioning requirement, not an enforced "
				+ "safety check.");
		getLogger().warn("UDP STOP is not a safety stop and cannot interrupt "
				+ "active motion. Do not send STOP between repeated calibration "
				+ "captures because it exits this application while idle.");

		try {
			poseStream = getTaskFunction(
					PoseTestBotPoseStreamFunction.class);
		} catch (RuntimeException e) {
			getLogger().error("Required automatic PoseTestBot pose-stream "
					+ "background task is unavailable: " + e);
			return;
		}

		while (true) {
			CaptureCommand command = waitForStartCommand();
			if (command == null) {
				return;
			}

			try {
				runCapture(command);
			} catch (RuntimeException e) {
				getLogger().error("Static-camera calibration motion failed; no "
						+ "successful end marker will be reported: " + e);
				return;
			}
			if (Thread.currentThread().isInterrupted()) {
				getLogger().error("Calibration thread was interrupted; exiting "
						+ "instead of accepting another start command");
				return;
			}
		}
	}

	private void runCapture(CaptureCommand command) {
		requireCurrentPositionNearBottomMiddle();
		poseStream.configure(
				command.receiverAddress.getHostAddress(),
				command.receiverPort,
				command.runId,
				POSE_TEMPLATE_BASE_PATH);
		double cartVelocityMmS = cartVelocityMmS(
				command.cartesianVelocityMps);
		getLogger().info("Starting single-frame static-camera calibration for "
				+ "run " + command.runId + " at " + cartVelocityMmS + " mm/s");

		moveToBottomMiddle("capture start anchor");
		moveFromBottomMiddleToPatternCenter(cartVelocityMmS);
		runRelativePlanarGrid(cartVelocityMmS);
		runRelativeDepthDither(cartVelocityMmS);
		runRelativeOrientationDither(cartVelocityMmS);
		moveFromPatternCenterToBottomMiddle(cartVelocityMmS);
		moveToBottomMiddle("capture end anchor confirmation");

		/* Report success only after the blocking return to the taught anchor. */
		poseStream.finishCapture();
	}

	private void moveFromBottomMiddleToPatternCenter(
			double cartVelocityMmS) {
		requirePatternPointInsideEnvelope(0.0, 0.0, 0.0,
				"pattern_center");
		captureRelativePose(0.0, 0.0, PATTERN_CENTER_Z_OFFSET_MM,
				0.0, 0.0, 0.0,
				cartVelocityMmS, "bottom_middle_to_pattern_center");
	}

	private void moveFromPatternCenterToBottomMiddle(
			double cartVelocityMmS) {
		requirePatternPointInsideEnvelope(
				0.0, 0.0, -PATTERN_CENTER_Z_OFFSET_MM,
				"bottom_middle");
		captureRelativePose(0.0, 0.0, -PATTERN_CENTER_Z_OFFSET_MM,
				0.0, 0.0, 0.0,
				cartVelocityMmS, "pattern_center_to_bottom_middle");
	}

	/**
	 * Visit the eight non-center points of a 3 x 3 grid. Each point is reached
	 * from center and immediately reversed, keeping every individual relative
	 * translation within 100 mm of the generated pattern center.
	 */
	private void runRelativePlanarGrid(double cartVelocityMmS) {
		captureGridPoint(-GRID_HALF_SPAN_MM, GRID_HALF_SPAN_MM,
				cartVelocityMmS, "grid_upper_left");
		captureGridPoint(0.0, GRID_HALF_SPAN_MM,
				cartVelocityMmS, "grid_upper_center");
		captureGridPoint(GRID_HALF_SPAN_MM, GRID_HALF_SPAN_MM,
				cartVelocityMmS, "grid_upper_right");
		captureGridPoint(GRID_HALF_SPAN_MM, 0.0,
				cartVelocityMmS, "grid_middle_right");
		captureGridPoint(GRID_HALF_SPAN_MM, -GRID_HALF_SPAN_MM,
				cartVelocityMmS, "grid_lower_right");
		captureGridPoint(0.0, -GRID_HALF_SPAN_MM,
				cartVelocityMmS, "grid_lower_center");
		captureGridPoint(-GRID_HALF_SPAN_MM, -GRID_HALF_SPAN_MM,
				cartVelocityMmS, "grid_lower_left");
		captureGridPoint(-GRID_HALF_SPAN_MM, 0.0,
				cartVelocityMmS, "grid_middle_left");
	}

	private void captureGridPoint(double xMm, double yMm,
			double cartVelocityMmS, String motionName) {
		requirePatternPointInsideEnvelope(xMm, yMm, 0.0, motionName);
		captureRelativePose(xMm, yMm, 0.0, 0.0, 0.0, 0.0,
				cartVelocityMmS, motionName + "_outbound");
		captureRelativePose(-xMm, -yMm, 0.0, 0.0, 0.0, 0.0,
				cartVelocityMmS, motionName + "_return_center");
	}

	private void runRelativeDepthDither(double cartVelocityMmS) {
		captureDepthPoint(DEPTH_DITHER_MM, cartVelocityMmS,
				"depth_plus");
		captureDepthPoint(-DEPTH_DITHER_MM, cartVelocityMmS,
				"depth_minus");
	}

	private void captureDepthPoint(double zMm, double cartVelocityMmS,
			String motionName) {
		requirePatternPointInsideEnvelope(0.0, 0.0, zMm, motionName);
		captureRelativePose(0.0, 0.0, zMm, 0.0, 0.0, 0.0,
				cartVelocityMmS, motionName + "_outbound");
		captureRelativePose(0.0, 0.0, -zMm, 0.0, 0.0, 0.0,
				cartVelocityMmS, motionName + "_return_center");
	}

	/**
	 * Rotate about all three center-frame axes without translating the flange.
	 * Every +/-10 degree result returns to center before the next result.
	 */
	private void runRelativeOrientationDither(double cartVelocityMmS) {
		captureOrientationPoint(-ORIENTATION_DITHER_DEG, 0.0, 0.0,
				cartVelocityMmS, "orientation_alpha_minus");
		captureOrientationPoint(ORIENTATION_DITHER_DEG, 0.0, 0.0,
				cartVelocityMmS, "orientation_alpha_plus");
		captureOrientationPoint(0.0, -ORIENTATION_DITHER_DEG, 0.0,
				cartVelocityMmS, "orientation_beta_minus");
		captureOrientationPoint(0.0, ORIENTATION_DITHER_DEG, 0.0,
				cartVelocityMmS, "orientation_beta_plus");
		captureOrientationPoint(0.0, 0.0, -ORIENTATION_DITHER_DEG,
				cartVelocityMmS, "orientation_gamma_minus");
		captureOrientationPoint(0.0, 0.0, ORIENTATION_DITHER_DEG,
				cartVelocityMmS, "orientation_gamma_plus");
	}

	private void captureOrientationPoint(double alphaDeg, double betaDeg,
			double gammaDeg, double cartVelocityMmS, String motionName) {
		captureRelativePose(0.0, 0.0, 0.0,
				alphaDeg, betaDeg, gammaDeg,
				cartVelocityMmS, motionName + "_outbound");
		captureRelativePose(0.0, 0.0, 0.0,
				-alphaDeg, -betaDeg, -gammaDeg,
				cartVelocityMmS, motionName + "_return_center");
	}

	private void requirePatternPointInsideEnvelope(double xMm, double yMm,
			double zFromCenterMm, String motionName) {
		double centerRadiusMm = translationRadiusMm(
				xMm, yMm, zFromCenterMm);
		double zFromBottomMiddleMm =
				PATTERN_CENTER_Z_OFFSET_MM + zFromCenterMm;
		double bottomMiddleRadiusMm = translationRadiusMm(
				xMm, yMm, zFromBottomMiddleMm);
		if (Double.isNaN(centerRadiusMm)
				|| Double.isInfinite(centerRadiusMm)
				|| centerRadiusMm > MAX_CENTER_TRANSLATION_MM) {
			throw new IllegalArgumentException("Pattern endpoint "
					+ motionName + " is " + centerRadiusMm
					+ " mm from the generated center; limit is "
					+ MAX_CENTER_TRANSLATION_MM + " mm");
		}
		if (Double.isNaN(zFromBottomMiddleMm)
				|| Double.isInfinite(zFromBottomMiddleMm)
				|| zFromBottomMiddleMm < 0.0) {
			throw new IllegalArgumentException("Pattern endpoint "
					+ motionName + " has invalid bottom-relative Z "
					+ zFromBottomMiddleMm + " mm");
		}
		if (Double.isNaN(bottomMiddleRadiusMm)
				|| Double.isInfinite(bottomMiddleRadiusMm)
				|| bottomMiddleRadiusMm
						> MAX_BOTTOM_MIDDLE_TRANSLATION_MM) {
			throw new IllegalArgumentException("Pattern endpoint "
					+ motionName + " is " + bottomMiddleRadiusMm
					+ " mm from CalibrationStaticBottomMiddle; limit is "
					+ MAX_BOTTOM_MIDDLE_TRANSLATION_MM + " mm");
		}
	}

	private void requireInsideRelativeTranslationEnvelope(
			double xMm, double yMm,
			double zMm, String motionName) {
		double radiusMm = translationRadiusMm(xMm, yMm, zMm);
		if (Double.isNaN(radiusMm) || Double.isInfinite(radiusMm)
				|| radiusMm > MAX_CENTER_TRANSLATION_MM) {
			throw new IllegalArgumentException("Relative endpoint "
					+ motionName + " is " + radiusMm
					+ " mm from the current phase anchor; limit is "
					+ MAX_CENTER_TRANSLATION_MM + " mm");
		}
	}

	private void requireCurrentPositionNearBottomMiddle() {
		Frame currentInBottomMiddle = robot.getCurrentCartesianPosition(
				robot.getFlange(), calibrationStaticBottomMiddle);
		double radiusMm = translationRadiusMm(
				currentInBottomMiddle.getX(),
				currentInBottomMiddle.getY(),
				currentInBottomMiddle.getZ());
		if (Double.isNaN(radiusMm) || Double.isInfinite(radiusMm)
				|| radiusMm > MAX_START_TRANSLATION_MM) {
			throw new IllegalStateException("Current flange is " + radiusMm
					+ " mm from CalibrationStaticBottomMiddle; manually position it "
					+ "within " + MAX_START_TRANSLATION_MM
					+ " mm before START");
		}
		getLogger().info("Accepted near-bottom-middle start position at "
				+ radiusMm + " mm from CalibrationStaticBottomMiddle");
	}

	private double translationRadiusMm(double xMm, double yMm,
			double zMm) {
		return Math.sqrt(xMm * xMm + yMm * yMm + zMm * zMm);
	}

	private void moveToBottomMiddle(String motionName) {
		getLogger().info("PTP to taught CalibrationStaticBottomMiddle: "
				+ motionName);
		robot.move(ptp(calibrationStaticBottomMiddle)
				.setJointVelocityRel(REPOSITION_PTP_VEL_REL)
				.setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)
				.setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL));
		settleAtCurrentPose(motionName);
	}

	private void captureRelativePose(double xMm, double yMm, double zMm,
			double alphaDeg, double betaDeg, double gammaDeg,
			double cartVelocityMmS, String motionName) {
		requireInsideRelativeTranslationEnvelope(
				xMm, yMm, zMm, motionName);
		Transformation offset = Transformation.ofDeg(
				xMm, yMm, zMm, alphaDeg, betaDeg, gammaDeg);
		long sentPoseCount;
		poseStream.startMotion(motionName);
		try {
			robot.move(linRel(offset, calibrationStaticBottomMiddle)
					.setCartVelocity(cartVelocityMmS)
					.setJointVelocityRel(RELATIVE_MOTION_JOINT_VEL_REL)
					.setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)
					.setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL));
		} finally {
			sentPoseCount = poseStream.stopMotion();
		}
		verifyPoseStream(motionName, sentPoseCount);
		settleAtCurrentPose(motionName);
	}

	private void settleAtCurrentPose(String motionName) {
		sleepWithInterruption(SETTLE_TIME_MS, motionName + " settle");
		int successfulSamples = 0;
		for (int i = 0; i < SETTLED_PACKET_COUNT; i++) {
			if (poseStream.sendCurrentPose(motionName + "_settled")) {
				successfulSamples++;
			}
			if (i + 1 < SETTLED_PACKET_COUNT) {
				sleepWithInterruption(
						SETTLED_PACKET_INTERVAL_MS,
						motionName + " settled-packet interval");
			}
		}
		if (successfulSamples == 0) {
			throw new IllegalStateException(
					"Pose stream produced no settled samples for " + motionName);
		}
		verifyPoseStream(motionName + "_settled", successfulSamples);
	}

	private void verifyPoseStream(String motionName, long segmentPoseCount) {
		if (poseStream.getFatalFailureCount() > 0L) {
			throw new IllegalStateException("Pose stream failed during "
					+ motionName + ": " + poseStream.getLastError());
		}
		if (segmentPoseCount <= 0L) {
			throw new IllegalStateException(
					"Pose stream produced no samples during " + motionName);
		}
		if (poseStream.getSendFailureCount() > 0L) {
			getLogger().warn("Pose stream has "
					+ poseStream.getSendFailureCount()
					+ " observable UDP send failure(s); last error: "
					+ poseStream.getLastError());
		}
		getLogger().info("Pose cadence evidence for " + motionName
				+ ": target_period_ms=" + poseStream.getTargetPeriodMs()
				+ ", maximum_pose_delta_ms="
				+ poseStream.getMaximumPoseDeltaNs() / 1000000.0
				+ ", maximum_pose_query_ms="
				+ poseStream.getMaximumPoseQueryDurationNs() / 1000000.0);
	}

	private double cartVelocityMmS(double requestedMps) {
		if (Double.isNaN(requestedMps) || Double.isInfinite(requestedMps)
				|| requestedMps <= 0.0) {
			throw new IllegalArgumentException(
					"Capture velocity must be a finite positive value in m/s");
		}

		double requestedMmS = requestedMps * 1000.0;
		double scaledMmS = requestedMmS * CAPTURE_VELOCITY_SCALE;
		double clampedMmS = Math.max(MIN_CART_VEL_MM_S,
				Math.min(MAX_CART_VEL_MM_S, scaledMmS));
		if (clampedMmS != scaledMmS) {
			getLogger().warn("Requested " + requestedMmS
					+ " mm/s; the calibration speed scale gives "
					+ scaledMmS + " mm/s and the configured bounds clamp "
					+ "this to " + clampedMmS + " mm/s");
		} else {
			getLogger().info("Requested " + requestedMmS
					+ " mm/s; applying calibration speed scale: "
					+ clampedMmS + " mm/s");
		}
		return clampedMmS;
	}

	private CaptureCommand waitForStartCommand() {
		DatagramSocket socket = null;
		try {
			socket = new DatagramSocket(ROBOT_PORT);
			getLogger().info("Waiting for UDP start command on port "
					+ ROBOT_PORT + "...");

			while (true) {
				byte[] receiveData = new byte[COMMAND_BUFFER_BYTES];
				DatagramPacket receivePacket = new DatagramPacket(
						receiveData, receiveData.length);
				try {
					socket.receive(receivePacket);
				} catch (IOException e) {
					getLogger().error("UDP command receive failed: " + e);
					return null;
				}
				try {
					String jsonMessage = new String(
							receivePacket.getData(),
							0,
							receivePacket.getLength(),
							UTF_8);
					JSONObject jsonObject = (JSONObject) new JSONParser().parse(
							jsonMessage);

					if (isStopCommand(jsonObject)) {
						getLogger().warn("UDP stop request received while idle. "
								+ "It cannot interrupt active motion, is not a "
								+ "safety stop, and will only exit this application.");
						return null;
					}

					CaptureCommand command = captureCommand(
							jsonObject, receivePacket);
					if (command != null) {
						getLogger().info("Accepted start command from "
								+ receivePacket.getAddress().getHostAddress()
								+ "; pose receiver target "
								+ command.receiverAddress.getHostAddress()
								+ ":" + command.receiverPort);
						return command;
					}

					getLogger().warn("Ignored UDP command without a supported "
							+ "start or stop request");
				} catch (Exception e) {
					getLogger().error("Rejected UDP command from "
							+ receivePacket.getAddress().getHostAddress()
							+ ": " + e);
				}
			}
		} catch (SocketException e) {
			getLogger().error("Unable to bind UDP command port "
					+ ROBOT_PORT + ": " + e);
			return null;
		} finally {
			if (socket != null) {
				socket.close();
			}
		}
	}

	private CaptureCommand captureCommand(JSONObject jsonObject,
			DatagramPacket receivePacket) throws IOException {
		Double velocity = startValue(jsonObject);
		if (velocity == null) {
			return null;
		}
		if (Double.isNaN(velocity.doubleValue())
				|| Double.isInfinite(velocity.doubleValue())
				|| velocity.doubleValue() <= 0.0) {
			throw new IllegalArgumentException(
					"cartesian_velocity_m_s must be finite and greater than zero");
		}

		String receiverIp = DEFAULT_RECEIVER_IP;
		Object receiverIpValue = jsonObject.get("receiver_ip");
		if (receiverIpValue != null) {
			String requestedReceiverIp = receiverIpValue.toString().trim();
			if (requestedReceiverIp.length() == 0
					|| requestedReceiverIp.equals("0.0.0.0")
					|| requestedReceiverIp.equals("::")) {
				receiverIp = receivePacket.getAddress().getHostAddress();
			} else {
				receiverIp = requestedReceiverIp;
			}
		}

		int receiverPort = DEFAULT_RECEIVER_PORT;
		Object receiverPortValue = jsonObject.get("receiver_port");
		if (receiverPortValue != null) {
			receiverPort = integerValue(
					receiverPortValue, "receiver_port");
		}
		if (receiverPort < 1 || receiverPort > 65535) {
			throw new IllegalArgumentException(
					"receiver_port must be between 1 and 65535");
		}

		String runId = "legacy-" + System.currentTimeMillis();
		Object runIdValue = jsonObject.get("run_id");
		if (runIdValue != null && runIdValue.toString().trim().length() > 0) {
			runId = runIdValue.toString().trim();
		}

		return new CaptureCommand(
				velocity.doubleValue(),
				InetAddress.getByName(receiverIp),
				receiverPort,
				runId);
	}

	private Double startValue(JSONObject jsonObject) {
		Object legacyStartValue = jsonObject.get("start");
		if (legacyStartValue != null) {
			return doubleValue(legacyStartValue);
		}

		if ("start_capture".equals(jsonObject.get("command"))) {
			return doubleValue(jsonObject.get("cartesian_velocity_m_s"));
		}
		return null;
	}

	private Double doubleValue(Object value) {
		if (value == null) {
			return null;
		}
		if (value instanceof Number) {
			return Double.valueOf(((Number) value).doubleValue());
		}
		return Double.valueOf(value.toString());
	}

	private int integerValue(Object value, String name) {
		if (value instanceof Number) {
			double number = ((Number) value).doubleValue();
			if (Double.isNaN(number)
					|| Double.isInfinite(number)
					|| number != Math.rint(number)
					|| number < Integer.MIN_VALUE
					|| number > Integer.MAX_VALUE) {
				throw new IllegalArgumentException(
						name + " must be an integer");
			}
			return (int) number;
		}
		return Integer.parseInt(value.toString());
	}

	private boolean isStopCommand(JSONObject jsonObject) {
		if (Boolean.TRUE.equals(jsonObject.get("stop"))) {
			return true;
		}
		Object command = jsonObject.get("command");
		return "pause_capture".equals(command)
				|| "stop_after_current_motion".equals(command)
				|| "emergency_stop".equals(command);
	}

	private void sleepWithInterruption(int millis, String reason) {
		try {
			Thread.sleep(millis);
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
			throw new IllegalStateException(
					"Interrupted during " + reason, e);
		}
	}

	private static final class CaptureCommand {
		private final double cartesianVelocityMps;
		private final InetAddress receiverAddress;
		private final int receiverPort;
		private final String runId;

		private CaptureCommand(double cartesianVelocityMps,
				InetAddress receiverAddress, int receiverPort, String runId) {
			this.cartesianVelocityMps = cartesianVelocityMps;
			this.receiverAddress = receiverAddress;
			this.receiverPort = receiverPort;
			this.runId = runId;
		}
	}
}
