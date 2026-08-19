# IIWA Ordinary Full-Capture Application

## Status

`iiwa/PoseTestBotFullCaptureApplication.java` is the repository candidate for
an ordinary pose-template capture. It is not evidence of the application or
revision currently deployed on the Sunrise controller. After launch, it waits
without motion for an accepted UDP START command.

Before deploying it, compile and simulate the source in the exact
Sunrise.Workbench controller project, create and verify the Application Data
frame below, and commission both A1 paths in T1 with the installed tool/load,
camera rig, fixtures, and cables. Repository validation does not perform any
robot motion.

## Frame Contract

The calibration motion waypoints and the physical pose template do not have to
share an origin or axis convention. They must not be represented by one
ambiguously retaught frame.

| Use | Persistent Sunrise frame | Repository frame role |
| --- | --- | --- |
| Nine-frame calibration motion waypoints | `/PoseTestBot/TemplateBase` | Motion planning only; not a run transform endpoint |
| Single-frame static-calibration bottom-center point | `/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle` | Absolute PTP anchor and 25 mm start-proximity reference; generated relative motion uses live flange/default-TCP axes |
| Static-calibration and ordinary-capture pose stream | `/PoseTestBot/PoseTemplateBase` | Run `template_base` and static `camera → template_base` result |
| Calibration board geometry, static cameras | Target bundle `aruco_grid` | Unknown rigid `aruco_grid → robot_flange`, estimated as support evidence |
| Calibration board geometry, eye-in-hand cameras | Target bundle `aruco_grid` | Explicit or estimated `aruco_grid → template_base` placement |
| Selected pose-template geometry | Selection `pose_template` | Explicit `pose_template → template_base` placement |

Create `/PoseTestBot/PoseTemplateBase` as a persistent Application Data
`ObjectFrame`; do not construct a numeric replacement at runtime. Teach its
origin and axes against the physical pose-template datum. Record its
relationship to `/PoseTestBot/TemplateBase` as commissioning evidence for the
motion plan. That relationship is not needed by the static-camera solver.
The [single-frame static-camera alternative](IIWA_SINGLE_FRAME_STATIC_CAMERA_CALIBRATION.md)
adds its sole taught bottom-center grid point as a child of that Application
Data frame; it does not change the pose-stream endpoint. That application
resolves `PoseTemplateBase` fail-closed but supplies no persistent frame to its
`LIN_REL` calls: its grid, depth, orientation, and return legs use the live
robot-flange/default-TCP axes. The commissioned convention is +X
image-down/toward the floor, +Y robot-left/image-right, and +Z toward the
primary static camera. The image-plane route therefore uses negative X to move
up and negative Y to move image-left. This is the robot's flange/default motion
frame, not a separately offset Workbench `Tool` TCP.

The motion frame and evidence frame are intentionally distinct. Static
calibration pose packets remain immutable
`robot_flange → template_base` observations in
`/PoseTestBot/PoseTemplateBase`, and the reusable result remains
`camera → PoseTemplateBase`, regardless of which live flange axes generate a
relative calibration leg.

The word `template_base` in run artifacts is a semantic role, not a Sunrise
path. Both static-camera calibration and ordinary dataset capture map that role
to `/PoseTestBot/PoseTemplateBase` and record the absolute path in every v1
pose packet. The nine-frame calibration application can command waypoints
below `/PoseTestBot/TemplateBase` while querying and streaming the flange pose
relative to `/PoseTestBot/PoseTemplateBase`; the motion parent does not define
the solver's output frame. If the selected digital pose template is physically
aligned with the latter frame, `template_base_from_pose_template` is identity.
Otherwise, enter the measured rigid transform during pose-template selection;
never compensate by silently retouching a calibration frame.

An eye-in-hand `camera → robot_flange` calibration remains independent of the
chosen world/reference frame as long as the camera mounting has not changed.
Static-camera calibration instead uses a robot-attached moving grid to solve
`camera → PoseTemplateBase` directly. The jointly estimated
`aruco_grid → robot_flange` value is a nuisance/support transform for closure;
there is no runtime hand-tracking product. A preexisting static profile
expressed against `/PoseTestBot/TemplateBase` must not be relabelled as if it
targeted `/PoseTestBot/PoseTemplateBase`; recalibrate it in the intended
reference. The current BOP path must receive camera, robot-pose, and object
transforms in one consistent dataset `template_base`.

## Command and Motion Contract

The only accepted start command is structured `robot_command.v1`; velocity is
Cartesian metres per second:

```json
{
  "schema_version": "robot_command.v1",
  "command": "start_capture",
  "cartesian_velocity_m_s": 0.01,
  "receiver_ip": "172.31.1.169",
  "receiver_port": 8080,
  "run_id": "12345678-1234-4234-9234-123456789abc"
}
```

The A1-only sweep is a circular flange path, while Sunrise PTP accepts a
relative joint-velocity setting. At the commissioned A1-minimum pose the
application therefore computes

```text
requested_A1_rad_s = requested_cartesian_mm_s / flange_orbit_radius_mm
joint_velocity_rel = requested_A1_rad_s / 98_deg_s
```

The radius is measured from the robot-root Z/A1 axis. KUKA publishes A1 rated
speeds of 98°/s for the LBR iiwa 7 R800 and 85°/s for the LBR iiwa 14 R820.
Using the larger value as the denominator makes the requested Cartesian value
an upper bound on either model. The candidate application accepts the
Cartesian request without a separate 0.03 m/s clamp, then limits the computed
A1 angular velocity to 3°/s. Its final relative joint velocity is therefore
bounded by that A1 limit.

Calibration capture is capped at 0.03 m/s. Object-dataset configuration is
bounded at 1.00 m/s by the current plan and receiver. The separately
acknowledged Dashboard manual motion-test request is also structured and uses
the fixed reviewed test velocity. These software limits are not safety-rated.
Record the exact installed model and verify the actual speed in Workbench/T1.
The product values are
available in KUKA's official
[LBR iiwa 7 R800 data sheet](https://www.kuka.com/-/media/kuka-downloads/imported/8350ff3ca11642998dbdc81dcc2ed44c/0000246832_pl.pdf)
and
[LBR iiwa 14 R820 data sheet](https://www.kuka.com/-/media/kuka-downloads/imported/8350ff3ca11642998dbdc81dcc2ed44c/0000246833_en.pdf).

New run configurations default to 0.01 m/s. Calibration setup permits
0.01–0.03 m/s; object-dataset setup permits 0.01–1.00 m/s and exposes the
requested value again at physical authorization. The 720-second supervisor
envelope accommodates the slowest full A1 sweep while the independent
first-packet and inter-packet timeouts still detect a receiver that never
starts or stops progressing.

Speed alone cannot guarantee blur-free images. Rolling-shutter skew and motion
blur also depend on exposure/readout time, illumination, optics, object
distance, and the camera's auto-exposure behavior. Verify sharpness in the
supervised trial and shorten exposure or improve lighting when needed. The
blocking 8%-relative PTP motions to the taught
`/PoseTestBot/CaptureStart` frame, the A1 sweep start, and the taught
`/PoseTestBot/CaptureEnd` frame occur outside pose streaming; their camera
frames are raw transition evidence, not authoritative synchronized capture
frames.

No motion occurs before an accepted start command. The application then:

1. moves PTP to the taught `/PoseTestBot/CaptureStart` frame;
2. snapshots that frame's non-A1 joint branch;
3. moves slowly to the commissioned A1 minimum;
4. waits 1.5 seconds;
5. performs the A1 sweep to the commissioned maximum while sending poses;
6. waits 1.5 seconds;
7. moves PTP to the taught `/PoseTestBot/CaptureEnd` frame; and
8. sends the terminal marker only after that final blocking PTP completes.

It remains at `/PoseTestBot/CaptureEnd` after the terminal marker. A later
return through `/PoseTestBot/CaptureStart` to A1 minimum happens only after a
new, independently authorized start command.

## UDP Pose Stream

The motion application no longer polls `IMotionContainer.isFinished()` before
every sample. It starts the shared read-only
`PoseTestBotPoseStreamTask`, executes the A1 motion as a blocking PTP, and
stops sampling only after that motion returns. The automatic-compatible cyclic
task requests a 10 ms `BestEffort` period (100 Hz nominal); this is a measured
commissioning target, not a KLI real-time guarantee. See the dedicated
[cadence decision and acceptance procedure](IIWA_POSE_STREAM_CADENCE.md).

The structured command must contain a nonblank receiver address, a port in
1–65535, and the run UUID. Missing fields, wildcard addresses, and non-positive
or non-finite velocities are rejected with controller-log errors.

Every new pose packet contains:

- `schema_version=robot_pose.v1`, packet kind, run ID, and increasing sequence;
- controller monotonic and wall-clock diagnostic timestamps;
- the 10 ms target period, previous sender pose delta, and pose-query duration;
- `robot_flange → template_base` endpoint semantics;
- `sunrise_reference_frame_path=/PoseTestBot/PoseTemplateBase`; and
- KUKA XYZ in millimetres plus A/B/C in radians.

The Python receiver accepts only `robot_pose.v1`. It validates the requested
run UUID, unchanging frame identity, and increasing sequence, retains the full
packet under `source_packet`, and records sequence gaps as estimated UDP loss.
Host receive/wall timestamps remain the synchronization authority; controller
timestamps are diagnostic because the two clocks are not assumed synchronized.

Packet parsing, socket creation, pose sending, terminal retries, and thread
interruptions are logged instead of being silently swallowed. The terminal
packet is still sent three times because UDP delivery is not guaranteed.

## Stop and Safety Contract

The application reads the command socket only while idle. A structured UDP
stop request uses the commissioned `robot_command.v1` wire token:

```json
{
  "schema_version": "robot_command.v1",
  "command": "stop_after_current_motion"
}
```

Despite the protocol token's name, the socket is not open during motion, so a
request cannot interrupt the active A1 motion and may be lost if sent then.
It is not an emergency stop or other safety function; while idle it only exits
the application. The token is pinned to the commissioned Sunrise deployment
and must not be renamed on the host without a coordinated controller rollout.
Use the controller's approved safety response for an unsafe condition.

## Commissioning Minimum

- Record the exact controller, Sunrise.OS/Workbench project, robot model,
  source revision, tool/load, camera rig, operator, reviewer, and date.
- Back up Application Data; create and teach
  `/PoseTestBot/PoseTemplateBase`, `/PoseTestBot/CaptureStart`, and
  `/PoseTestBot/CaptureEnd`; and read back their XYZABC values and parent-frame
  relationships.
- Confirm calibration and ordinary-capture pose streams use
  `/PoseTestBot/PoseTemplateBase`, the selected pose-template placement uses
  that frame, and any preexisting static profile expressed in the motion-only
  `/PoseTestBot/TemplateBase` is rejected rather than relabelled.
- Compile the exact project and resolve `getRootFrame()`, PTP acceleration/jerk
  setters, JSON-simple, the task-function provider, and all three persistent
  frames.
- Create `PoseTestBotPoseStreamTask` through the Workbench background-task
  workflow, configure automatic start, and confirm exactly one
  `PoseTestBotPoseStreamFunction` provider is registered.
- Simulate the PTP path to `/PoseTestBot/CaptureStart`, the repositioning path
  to A1 −169°, the captured path to A1 +169°, and the final PTP path to
  `/PoseTestBot/CaptureEnd`, including every joint/redundancy branch, joint and
  singularity margin, collision clearance, and cable motion.
- With explicit operator authorization, single-step the complete
  PTP/A1/PTP sequence in T1 at reduced override before deploying the source.
- Verify the start-frame plus A1-minimum positioning finishes inside the
  receiver's first-packet timeout and the end-frame PTP finishes inside its
  inter-packet timeout.
- Retain a supervised cadence report and target at least 50 Hz median
  end-to-end motion rate, no more than 25 ms p95 gap, and no more than 40 ms
  maximum gap. Investigate a miss before treating the 10 ms request as
  operationally adequate; cadence alone does not invalidate a calibration.
- During the supervised trial, verify increasing v1 sequences, the recorded
  Sunrise frame path, requested/applied Cartesian and A1 speed logs, exposure
  settings, image sharpness, camera coverage, and a successful receiver
  terminal state.
