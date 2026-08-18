package application;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
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
 * Nine-frame ArUco calibration capture with more image-space and orientation
 * variance than PoseTestBotFullCaptureApplication's single-axis A1 sweep.
 *
 * IMPORTANT: This repository revision is enabled for lab validation, but the
 * exact deployed controller application and revision must be independently
 * recorded. Revalidate every frame and connecting motion in Sunrise.Workbench
 * and T1 whenever the frames, tool, cables, target, cameras, or safety setup
 * change.
 *
 * The nine raster targets are persistent Application Data ObjectFrames directly
 * below /PoseTestBot/TemplateBase. Numeric values in the repository teaching
 * manifest are uncommissioned Workbench seeds only. The A/B/C orientation
 * dither is implemented as program-owned relative rotations from the taught
 * CalibrationCenter; no numeric absolute target is created at runtime.
 */
public class PoseTestBotNineFrameCalibrationApplication
		extends RoboticsAPIApplication {

	/* Motion waypoints stay under the commissioned calibration teaching frame. */
	private static final String TEMPLATE_BASE_PATH = "/PoseTestBot/TemplateBase";
	/* Static-camera extrinsics are solved in the ordinary dataset world frame. */
	private static final String POSE_TEMPLATE_BASE_PATH =
			"/PoseTestBot/PoseTemplateBase";
	private static final String CALIBRATION_COVERAGE_UPPER_LEFT_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageUpperLeft";
	private static final String CALIBRATION_COVERAGE_UPPER_CENTER_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageUpperCenter";
	private static final String CALIBRATION_COVERAGE_UPPER_RIGHT_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageUpperRight";
	private static final String CALIBRATION_COVERAGE_MIDDLE_RIGHT_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageMiddleRight";
	private static final String CALIBRATION_CENTER_PATH = "/PoseTestBot/TemplateBase/CalibrationCenter";
	private static final String CALIBRATION_COVERAGE_MIDDLE_LEFT_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageMiddleLeft";
	private static final String CALIBRATION_COVERAGE_LOWER_LEFT_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageLowerLeft";
	private static final String CALIBRATION_COVERAGE_LOWER_CENTER_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageLowerCenter";
	private static final String CALIBRATION_COVERAGE_LOWER_RIGHT_PATH = "/PoseTestBot/TemplateBase/CalibrationCoverageLowerRight";
	private static final Charset UTF_8 = Charset.forName("UTF-8");

	/* Commission one phase at a time before enabling both together. */
	private static final boolean RUN_COVERAGE_RASTER = true;
	private static final boolean RUN_ORIENTATION_DITHER = true;

	private static final int SETTLE_TIME_MS = 1500;
	private static final int ROBOT_PORT = 30300;
	private static final int SETTLED_PACKET_COUNT = 3;
	private static final int SETTLED_PACKET_INTERVAL_MS = 50;

	/* Keep capture motion below the requested run velocity to limit blur. */
	private static final double CAPTURE_VELOCITY_SCALE = 0.60;
	private static final double REPOSITION_PTP_VEL_REL = 0.08;
	private static final double ORIENTATION_JOINT_VEL_REL = 0.03;
	private static final double SMOOTH_MOTION_JOINT_ACCEL_REL = 0.03;
	private static final double SMOOTH_MOTION_JOINT_JERK_REL = 0.03;
	private static final double MIN_CART_VEL_MM_S = 8.0;
	private static final double MAX_CART_VEL_MM_S = 30.0;

	@Inject
	private LBR robot;
	@Inject
	private InfoTemplate robotinfo;

	private ObjectFrame templateBase;
	private ObjectFrame poseTemplateBase;
	private ObjectFrame coverageUpperLeft;
	private ObjectFrame coverageUpperCenter;
	private ObjectFrame coverageUpperRight;
	private ObjectFrame coverageMiddleRight;
	private ObjectFrame calibrationCenter;
	private ObjectFrame coverageMiddleLeft;
	private ObjectFrame coverageLowerLeft;
	private ObjectFrame coverageLowerCenter;
	private ObjectFrame coverageLowerRight;
	private PoseTestBotPoseStreamFunction poseStream;

	@Override
	public void initialize() {
		robot = getContext().getDeviceFromType(LBR.class);
		robotinfo.setBase(TEMPLATE_BASE_PATH);
		templateBase = requiredFrame(TEMPLATE_BASE_PATH);
		poseTemplateBase = requiredFrame(POSE_TEMPLATE_BASE_PATH);
		coverageUpperLeft = requiredFrame(CALIBRATION_COVERAGE_UPPER_LEFT_PATH);
		coverageUpperCenter = requiredFrame(CALIBRATION_COVERAGE_UPPER_CENTER_PATH);
		coverageUpperRight = requiredFrame(CALIBRATION_COVERAGE_UPPER_RIGHT_PATH);
		coverageMiddleRight = requiredFrame(CALIBRATION_COVERAGE_MIDDLE_RIGHT_PATH);
		calibrationCenter = requiredFrame(CALIBRATION_CENTER_PATH);
		coverageMiddleLeft = requiredFrame(CALIBRATION_COVERAGE_MIDDLE_LEFT_PATH);
		coverageLowerLeft = requiredFrame(CALIBRATION_COVERAGE_LOWER_LEFT_PATH);
		coverageLowerCenter = requiredFrame(CALIBRATION_COVERAGE_LOWER_CENTER_PATH);
		coverageLowerRight = requiredFrame(CALIBRATION_COVERAGE_LOWER_RIGHT_PATH);

		getLogger().info("Resolved TemplateBase and all nine taught grid frames: "
				+ robotinfo.getBase());
		getLogger().info("Resolved static-camera calibration output reference: "
				+ poseTemplateBase);
	}

	private ObjectFrame requiredFrame(String path) {
		ObjectFrame frame = getApplicationData().getFrame(path);
		if (frame == null) {
			throw new IllegalStateException(
					"Required Application Data frame is missing: " + path);
		}
		return frame;
	}

	@Override
	public void run() {
		getLogger().warn("Before the first start command, manually position the "
				+ "robot at or near the taught CalibrationCenter pose. This is an "
				+ "operator commissioning requirement, not an enforced safety check.");

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
				poseStream.configure(
						command.receiverIp,
						command.receiverPort,
						command.runId,
						POSE_TEMPLATE_BASE_PATH);
				double cartVelocityMmS = cartVelocityMmS(
						command.cartesianVelocityMps);
				getLogger().info("Starting calibration variance capture for run "
						+ command.runId + " at " + cartVelocityMmS + " mm/s");
				moveToCenter("capture start anchor");

				if (RUN_COVERAGE_RASTER) {
					runCoverageRaster(cartVelocityMmS);
				}
				if (RUN_ORIENTATION_DITHER) {
					runOrientationDither(cartVelocityMmS);
				}

				poseStream.finishCapture();
			} catch (RuntimeException e) {
				getLogger().error("Calibration capture failed; no successful "
						+ "end marker will be reported: " + e);
				return;
			}
			sleep(SETTLE_TIME_MS);
		}
	}

	/**
	 * Raster translation is the primary image-centroid coverage mechanism.
	 * The +/-160 mm lateral offsets and roughly +/-90 mm vertical offsets are
	 * intended to cross the thirds of a 1280x720 image without re-aiming the
	 * camera perfectly at the board on every waypoint.
	 */
	private void runCoverageRaster(double cartVelocityMmS) {
		moveFromCenter(coverageUpperLeft, "coverage raster");
		captureLinear(coverageUpperCenter, cartVelocityMmS,
				"coverage_upper_left_to_center");
		captureLinear(coverageUpperRight, cartVelocityMmS,
				"coverage_upper_center_to_right");
		captureLinear(coverageMiddleRight, cartVelocityMmS,
				"coverage_upper_to_middle_right");
		captureLinear(calibrationCenter, cartVelocityMmS,
				"coverage_middle_right_to_center");
		captureLinear(coverageMiddleLeft, cartVelocityMmS,
				"coverage_middle_center_to_left");
		captureLinear(coverageLowerLeft, cartVelocityMmS,
				"coverage_middle_to_lower_left");
		captureLinear(coverageLowerCenter, cartVelocityMmS,
				"coverage_lower_left_to_center");
		captureLinear(coverageLowerRight, cartVelocityMmS,
				"coverage_lower_center_to_right");
		moveToCenter("coverage raster return");
	}

	/**
	 * Adds rotation-axis diversity for intrinsic and hand-eye observability.
	 * These are intentionally modest +/-15 degree A/C and +/-12 degree B
	 * offsets. Which change maps to image yaw, pitch, or roll depends on the
	 * actual flange-to-camera mounting transform and must be verified visually.
	 */
	private void runOrientationDither(double cartVelocityMmS) {
		captureRelativeOrientation(-15, 0, 0, cartVelocityMmS,
				"orientation_alpha_minus_15");
		captureRelativeOrientation(30, 0, 0, cartVelocityMmS,
				"orientation_alpha_plus_15");
		captureRelativeOrientation(-15, 0, 0, cartVelocityMmS,
				"orientation_alpha_return_center");
		captureRelativeOrientation(0, -12, 0, cartVelocityMmS,
				"orientation_beta_minus_12");
		captureRelativeOrientation(0, 24, 0, cartVelocityMmS,
				"orientation_beta_plus_12");
		captureRelativeOrientation(0, -12, 0, cartVelocityMmS,
				"orientation_beta_return_center");
		captureRelativeOrientation(0, 0, -15, cartVelocityMmS,
				"orientation_gamma_minus_15");
		captureRelativeOrientation(0, 0, 30, cartVelocityMmS,
				"orientation_gamma_plus_15");
		captureRelativeOrientation(0, 0, -15, cartVelocityMmS,
				"orientation_gamma_return_center");
	}

	private void moveFromCenter(ObjectFrame target, String phaseName) {
		getLogger().info("PTP from taught CalibrationCenter into " + phaseName);
		robot.move(ptp(target)
				.setJointVelocityRel(REPOSITION_PTP_VEL_REL)
				.setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)
				.setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL));
		settleAtCurrentPose(phaseName);
	}

	private void moveToCenter(String motionName) {
		getLogger().info("PTP to taught CalibrationCenter: " + motionName);
		robot.move(ptp(calibrationCenter)
				.setJointVelocityRel(REPOSITION_PTP_VEL_REL)
				.setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)
				.setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL));
		settleAtCurrentPose(motionName);
	}

	private void captureLinear(ObjectFrame target, double cartVelocityMmS,
			String motionName) {
		long sentPoseCount;
		poseStream.startMotion(motionName);
		try {
			robot.move(lin(target)
					.setCartVelocity(cartVelocityMmS)
					.setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)
					.setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL));
		} finally {
			sentPoseCount = poseStream.stopMotion();
		}
		verifyPoseStream(motionName, sentPoseCount);
		settleAtCurrentPose(motionName);
	}

	private void captureRelativeOrientation(double alphaDeg, double betaDeg,
			double gammaDeg, double cartVelocityMmS, String motionName) {
		Transformation offset = Transformation.ofDeg(0, 0, 0,
				alphaDeg, betaDeg, gammaDeg);
		long sentPoseCount;
		poseStream.startMotion(motionName);
		try {
			robot.move(linRel(offset, calibrationCenter)
					.setCartVelocity(cartVelocityMmS)
					.setJointVelocityRel(ORIENTATION_JOINT_VEL_REL)
					.setJointAccelerationRel(SMOOTH_MOTION_JOINT_ACCEL_REL)
					.setJointJerkRel(SMOOTH_MOTION_JOINT_JERK_REL));
		} finally {
			sentPoseCount = poseStream.stopMotion();
		}
		verifyPoseStream(motionName, sentPoseCount);
		settleAtCurrentPose(motionName);
	}

	private void settleAtCurrentPose(String motionName) {
		sleep(SETTLE_TIME_MS);
		int successfulSamples = 0;
		for (int i = 0; i < SETTLED_PACKET_COUNT; i++) {
			if (poseStream.sendCurrentPose(motionName + "_settled")) {
				successfulSamples++;
			}
			if (i + 1 < SETTLED_PACKET_COUNT) {
				sleep(SETTLED_PACKET_INTERVAL_MS);
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
		while (true) {
			DatagramSocket socket = null;

			try {
				socket = new DatagramSocket(ROBOT_PORT);
				getLogger().info("Waiting for UDP start command...");

				byte[] receiveData = new byte[1024];
				DatagramPacket receivePacket = new DatagramPacket(
						receiveData, receiveData.length);
				socket.receive(receivePacket);

				String jsonMessage = new String(receivePacket.getData(), 0,
						receivePacket.getLength(), UTF_8);
				JSONObject jsonObject = (JSONObject) new JSONParser().parse(
						jsonMessage);

				CaptureCommand command = captureCommand(jsonObject);
				if (command != null) {
					getLogger().info("Pose receiver target: "
							+ command.receiverIp + ":"
							+ command.receiverPort);
					return command;
				}

				if (isStopCommand(jsonObject)) {
					getLogger().info("Stop message received. Ending program.");
					return null;
				}
			} catch (Exception e) {
				getLogger().error("UDP command error: " + e);
			} finally {
				if (socket != null) {
					socket.close();
				}
			}
		}
	}

	private Double startValue(JSONObject jsonObject) {
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

	private CaptureCommand captureCommand(JSONObject jsonObject) {
		if (!"robot_command.v1".equals(jsonObject.get("schema_version"))) {
			throw new IllegalArgumentException(
					"schema_version must be robot_command.v1");
		}
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

		Object receiverIpValue = jsonObject.get("receiver_ip");
		if (receiverIpValue == null
				|| receiverIpValue.toString().trim().length() == 0) {
			throw new IllegalArgumentException("receiver_ip is required");
		}
		String requestedReceiverIp = receiverIpValue.toString().trim();

		Object receiverPortValue = jsonObject.get("receiver_port");
		if (receiverPortValue == null) {
			throw new IllegalArgumentException("receiver_port is required");
		}
		int requestedReceiverPort = integerValue(
				receiverPortValue, "receiver_port");
		if (requestedReceiverPort < 1 || requestedReceiverPort > 65535) {
			throw new IllegalArgumentException(
					"receiver_port must be between 1 and 65535");
		}

		Object runIdValue = jsonObject.get("run_id");
		if (runIdValue == null
				|| runIdValue.toString().trim().length() == 0) {
			throw new IllegalArgumentException("run_id is required");
		}
		String requestedRunId = runIdValue.toString().trim();
		return new CaptureCommand(
				velocity.doubleValue(),
				requestedReceiverIp,
				requestedReceiverPort,
				requestedRunId);
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
		return "robot_command.v1".equals(jsonObject.get("schema_version"))
				&& "exit_idle_program".equals(jsonObject.get("command"));
	}

	private void sleep(int millis) {
		try {
			Thread.sleep(millis);
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
		}
	}

	private static final class CaptureCommand {
		private final double cartesianVelocityMps;
		private final String receiverIp;
		private final int receiverPort;
		private final String runId;

		private CaptureCommand(double cartesianVelocityMps,
				String receiverIp, int receiverPort, String runId) {
			this.cartesianVelocityMps = cartesianVelocityMps;
			this.receiverIp = receiverIp;
			this.receiverPort = receiverPort;
			this.runId = runId;
		}
	}
}
