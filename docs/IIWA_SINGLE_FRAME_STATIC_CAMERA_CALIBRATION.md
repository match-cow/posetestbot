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

The existing persistent `/PoseTestBot/PoseTemplateBase` remains the configured
application base, the `robot_flange -> template_base` pose-stream reference,
and the destination frame of the reusable static
`camera -> PoseTemplateBase` result. Create and teach only this additional
absolute PTP anchor:

```text
/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle
```

Teach `CalibrationStaticBottomMiddle` with the target straight in front of the
primary static camera and approximately bottom-center in its image. Teach the
flange orientation so its live/default motion axes move the target as follows:

- TCP `+X`: image-down, toward the floor;
- TCP `+Y`: robot-left and image-right; and
- TCP `+Z`: toward the camera.

The image-plane route therefore uses negative TCP X to move up from the taught
bottom row and negative TCP Y to move left in the image.

The application issues `robot.move(linRel(offset))`, so every generated
translation and rotation follows the live robot-flange/default-TCP axes. It
does not pass `PoseTemplateBase` or the taught anchor as a `LIN_REL` reference.
This is the flange/default motion frame, not a separately offset Workbench
`Tool` TCP; the commissioned setup assumes no nonzero Tool TCP is being used to
introduce another motion origin or axis convention.

The generated pattern center is TCP `(X=-50, Y=0, Z=0 mm)` relative to the
taught point. It should place the complete robot-carried target near the image
center with enough border and depth margin for every translation and rotation.
Every other selected static camera must also retain useful target visibility
through the complete sequence. `PoseTemplateBase` is still resolved
fail-closed, but its origin and axes do not define these relative motions.

## Relative Motion Plan

Starting at the taught bottom-middle point, the image-plane grid uses the
following bottom-relative TCP coordinates:

| Generated point | TCP X from taught bottom | TCP Y from taught bottom | TCP Z from taught bottom | Distance from taught bottom |
| --- | ---: | ---: | ---: | ---: |
| Bottom middle (taught) | 0 | 0 | 0 | 0 |
| Bottom left | 0 | -65 mm | 0 | 65 mm |
| Middle left | -50 mm | -65 mm | 0 | 82.0 mm |
| Top left | -100 mm | -65 mm | 0 | 119.3 mm |
| Top center | -100 mm | 0 | 0 | 100 mm |
| Top right | -100 mm | +65 mm | 0 | 119.3 mm |
| Middle right | -50 mm | +65 mm | 0 | 82.0 mm |
| Bottom right | 0 | +65 mm | 0 | 65 mm |
| Pattern center | -50 mm | 0 | 0 | 50 mm |

The grid phase follows this deterministic perimeter-to-center order:

```text
Bottom-middle → bottom-left → middle-left → top-left → top-center
              → top-right → middle-right → bottom-right → center
```

In coordinate form, the grid route is exactly:

```text
(0,0,0) → (0,-65,0) → (-50,-65,0) → (-100,-65,0)
        → (-100,0,0) → (-100,+65,0) → (-50,+65,0)
        → (0,+65,0) → (-50,0,0) mm
```

Each leg is a blocking `LIN_REL` delta from the preceding point. The final
bottom-right-to-center diagonal is about 82.0 mm; all other image-plane grid
legs are 50 or 65 mm.

From center, four blocking TCP-Z legs add two depth observations and return to
center between them:

```text
(-50,0,0) → (-50,0,+50) toward camera → (-50,0,0)
           → (-50,0,-50) away from camera → (-50,0,0) mm
```

After those depth legs, orientation follows
`center → -10° → +10° → center` independently for A, B, and C. The direct
negative-to-positive leg is a 20° relative rotation. Translation remains zero
throughout all orientation legs.

The generated sequence contains 22 relative legs: eight grid legs, four depth
legs, nine orientation legs, and one center-to-bottom-middle leg. Before a
translation is issued, both endpoints must contain finite bottom-relative TCP
X/Y/Z values, bottom-relative TCP X must be non-positive, the individual
translation must be at most 100 mm, and each endpoint must be at most 125 mm
from the taught anchor. Negative TCP Z is deliberately permitted for the
away-from-camera observation. A positive bottom-relative TCP X endpoint is
rejected because it would move image-down past the taught bottom row toward the
floor. These are coordinate-envelope checks; the parent `PoseTemplateBase.Z`
axis is not a physical floor.

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
4. follows the eight-leg TCP-X/Y image-plane grid route from bottom-middle through
   every other grid point and ends at its generated center;
5. performs the four TCP-Z depth legs, settling at toward, center, away, and
   center;
6. sweeps each live TCP A/B/C axis from -10° through +10° and back to center in
   three legs per axis;
7. moves `LIN_REL` +50 mm in live TCP X from center back to—but not past—the taught
   bottom-middle point; and
8. sends the successful terminal marker only after that blocking return
   completes.

All relative legs use the no-reference `linRel(Transformation)` overload and
therefore the live flange/default-TCP motion frame. `PoseTemplateBase` remains
unchanged as the pose-stream and solver-result frame. Relative motions use 60%
of the requested Cartesian speed, clamped to 8–30 mm/s, plus
3% relative joint velocity, acceleration, and jerk limits. The initial
bottom-middle PTP uses 8% relative joint velocity and the same 3%
acceleration/jerk limits. The PTP anchor and every relative leg have a
1.5-second dwell followed by three settled pose samples. These are
motion-quality settings, not safety-rated limits.

The UDP socket is read only while idle. `STOP` uses the structured
`robot_command.v1` command token `stop_after_current_motion`; this exact wire
value is pinned to the commissioned Sunrise deployment. Despite that token's
name, `STOP` cannot interrupt active motion, is not a safety stop, and may be
lost if sent while the socket is closed. While idle it exits the waiting
application. Never send it between repeated calibration captures; use a new
START after the prior capture has completed.

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
- Read back the exact parent and bottom-middle paths and the taught anchor's
  XYZABC/joint branch. With the target straight in front of the primary camera
  at approximately image bottom-center, verify that live flange/default-TCP +X
  is image-down/toward the floor, +Y is robot-left/image-right, and +Z is toward
  the camera. Confirm the
  application commands the robot/default flange motion frame and that no
  separately offset Workbench `Tool` TCP changes this contract. Confirm the
  target is rigidly mounted and its attachment does not change during the
  recording.
- Before START, manually position the flange within 25 mm of the taught
  bottom-middle frame.
  The program rejects a farther start without moving, but that proximity check
  does not prove the initial PTP joint-space path is collision-free.
- Compile against the installed Sunrise.OS API and verify that the
  no-reference `linRel(Transformation)` overload and all motion setters resolve
  exactly as used. Then simulate the absolute PTP and all 22 relative endpoints
  and swept paths. Check the four depth legs, the direct image-plane grid legs,
  the +50 mm TCP-X return to the taught anchor, and the 20°
  negative-to-positive orientation legs.
  Pay particular attention to the initial PTP because a Cartesian endpoint
  contract does not constrain its joint-space swept path. Check reachability,
  joint/redundancy branch, singularity margin, fixtures, the complete mounted
  target, arm, and cable clearance—not only the flange-origin envelope.
- Verify all grid, depth, and orientation results keep the complete target
  detectable with useful image coverage in every selected static camera.
- With explicit operator authorization, single-step the entire sequence in T1
  at reduced pendant override, checking every endpoint and swept path before a
  supervised capture or deployment. Do not infer readiness from a successful
  status request or repository test.
- Retain `robot_pose.v1` identity/cadence evidence and the current attempt and
  explicit promotion artifacts. The resulting primary profile
  remains `camera -> PoseTemplateBase`; the estimated
  `aruco_grid -> robot_flange` attachment is supporting evidence, not a runtime
  hand-tracking product.

The installed Sunrise.OS Javadoc and exact Workbench project are authoritative
for the no-reference `linRel(Transformation)` overload, its live
flange/default-TCP semantics, and the motion-parameter setters.
