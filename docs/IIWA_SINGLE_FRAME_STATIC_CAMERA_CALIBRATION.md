# IIWA Single-Frame Static-Camera Calibration Application

## Outcome and Status

`iiwa/PoseTestBotSingleFrameStaticCameraCalibrationApplication.java` is the
repository alternative for calibrating a fixed camera with a calibration
target rigidly mounted on the robot. It requires one additional taught motion
frame and generates the remaining poses as bounded relative motions.

This source is not deployment or physical-commissioning evidence. It must be
added to the exact Sunrise.Workbench project, compiled against the installed
Sunrise.OS API, simulated, commissioned in T1, and selected on the controller
by an authorized operator. Repository validation never commands the robot.

## One-Frame Contract

The existing persistent `/PoseTestBot/PoseTemplateBase` remains the application
base, pose-stream reference, and destination frame of the reusable static
`camera -> PoseTemplateBase` result. Create and teach only this additional
motion frame:

```text
/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle
```

Teach `CalibrationStaticBottomMiddle` at the minimum local-Z endpoint that the
calibration target may occupy. Orient the frame so its +Z axis points from this
bottom position toward the available workspace. The generated pattern center
is 50 mm along that +Z axis. At that center, the robot-carried target must be
centered and fully visible in the intended static camera, with useful border
margin for translation and rotation. The program does not construct numeric
absolute frames at runtime.

All translations are expressed in the taught bottom-middle frame's axes.
“Bottom” in this contract means the minimum local-Z endpoint, not necessarily
the bottom of a camera image or negative Z of another Sunrise frame. “Upper”,
“lower”, “left”, “depth plus”, and “depth minus” are route labels, not promises
about camera-image direction or camera distance. For the ceiling-mounted cell,
commissioning must verify that taught-frame +Z actually points away from the
restricted negative-Z workspace before any motion is enabled.

## Relative Motion Plan

The program first moves 50 mm in local +Z from the taught bottom-middle frame
to the generated center. The planar grid retains 65 mm X/Y half-spans around
that center, and the depth dither retains ±50 mm Z travel around it. Thus the
complete pattern occupies local Z = 0…100 mm and never generates an endpoint
below the taught frame.

| Generated point | X from taught bottom | Y from taught bottom | Z from taught bottom | Distance from taught bottom |
| --- | ---: | ---: | ---: | ---: |
| Pattern center | 0 | 0 | +50 mm | 50 mm |
| Upper left | -65 mm | +65 mm | +50 mm | 104.6 mm |
| Upper center | 0 | +65 mm | +50 mm | 82.0 mm |
| Upper right | +65 mm | +65 mm | +50 mm | 104.6 mm |
| Middle right | +65 mm | 0 | +50 mm | 82.0 mm |
| Lower right | +65 mm | -65 mm | +50 mm | 104.6 mm |
| Lower center | 0 | -65 mm | +50 mm | 82.0 mm |
| Lower left | -65 mm | -65 mm | +50 mm | 104.6 mm |
| Middle left | -65 mm | 0 | +50 mm | 82.0 mm |
| Depth plus | 0 | 0 | +100 mm | 100 mm |
| Depth minus / taught bottom | 0 | 0 | 0 | 0 |

Each grid or depth result is a blocking `LIN_REL` from the generated center and
is followed by its exact inverse `LIN_REL` back to that center. Every individual
relative translation remains within the 100 mm center limit, and every
generated endpoint remains within the separate 110 mm radius around the taught
bottom-middle frame. The program then visits independent -10° and +10° results
about A, B, and C at zero translation from the generated center, returning to
center after each orientation.

These bounds apply only to the flange origin's translation. They do not bound
every point of the robot, tool, mounted target, or cables. Orientation can sweep
target corners outside those spheres, so the complete robot-and-target swept
volume still requires collision and clearance validation.

## Runtime Sequence

Launching the application causes no robot motion. It resolves the shared
`PoseTestBotPoseStreamTask`, binds UDP port 30300, and waits for an accepted
positive-velocity START command. After START it:

1. queries the current flange pose without motion and rejects the run unless it
   is already within 25 mm of `CalibrationStaticBottomMiddle`;
2. configures pose streaming in `/PoseTestBot/PoseTemplateBase`;
3. moves PTP to the taught `CalibrationStaticBottomMiddle` anchor;
4. moves `LIN_REL` +50 mm in the taught frame's Z axis to the generated center;
5. visits and returns from all eight planar grid points;
6. visits and returns from the two depth points;
7. visits and returns from the six orientation results at the generated center;
8. moves `LIN_REL` -50 mm in taught-frame Z back to the bottom-middle anchor;
9. performs a final blocking PTP confirmation at that absolute taught frame;
   and
10. sends the successful terminal marker only after that return completes.

Relative motions use 60% of the requested Cartesian speed, clamped to
8–30 mm/s, plus 3% relative joint velocity, acceleration, and jerk limits.
Bottom-middle PTP motions use 8% relative joint velocity and the same 3%
acceleration/jerk limits. Every leg has a 1.5-second dwell followed by three
settled pose samples. These are motion-quality settings, not safety-rated
limits.

The UDP socket is read only while idle. `STOP` cannot interrupt active motion,
is not a safety stop, and exits the waiting application. Never send it between
repeated calibration captures; use a new START after the prior capture has
completed.

## Workbench and T1 Commissioning

- Back up Application Data and record the exact controller, Sunrise.OS,
  Workbench project, repository revision, tool/load, target, operator,
  reviewer, and date.
- Rename/import the repository classes consistently in Workbench:
  `PoseTestBotFullCaptureApplication`,
  `PoseTestBotNineFrameCalibrationApplication`,
  `PoseTestBotSingleFrameStaticCameraCalibrationApplication`, and
  `PoseTestBotPoseStreamTask`. A repository rename does not update Workbench
  application or automatic-background-task metadata.
- Register `PoseTestBotPoseStreamTask` as the one automatic cyclic provider of
  `PoseTestBotPoseStreamFunction`, then compile all five Java sources.
- Read back the exact parent and bottom-middle paths and the taught frame's
  XYZABC/joint branch. Confirm local +Z points from the taught minimum-Z target
  position toward the generated center and available workspace. Confirm the
  target is rigidly mounted and its attachment does not change during the
  recording.
- Before START, manually position the flange within 25 mm of the taught
  bottom-middle frame.
  The program rejects a farther start without moving, but that proximity check
  does not prove the initial PTP joint-space path is collision-free.
- Simulate the bottom-to-center `LIN_REL`, every PTP and other `LIN_REL`
  endpoint, and every swept path. Confirm no generated endpoint crosses below
  local Z = 0. Check reachability, joint/redundancy branch, singularity margin,
  fixtures, the complete mounted target, arm, and cable clearance—not only the
  flange-origin envelope.
- Verify all grid/depth/orientation results keep the complete target detectable
  with useful image coverage in every selected static camera.
- With explicit operator authorization, single-step the entire sequence in T1
  at reduced pendant override before a supervised capture. Do not infer
  readiness from a successful status request or repository test.
- Retain v1 pose-stream identity/cadence evidence and the normal calibration
  solver, validation, and promotion artifacts. The resulting primary profile
  remains `camera -> PoseTemplateBase`; the estimated
  `aruco_grid -> robot_flange` attachment is supporting evidence, not a runtime
  hand-tracking product.

The installed Sunrise.OS Javadoc and exact Workbench project are authoritative
for the available `linRel(Transformation, ObjectFrame)` overload and motion
parameter setters.
