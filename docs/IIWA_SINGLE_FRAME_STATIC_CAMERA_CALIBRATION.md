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

Teach `CalibrationStaticBottomMiddle` as the bottom-center point of the 3 × 3
calibration grid. Its flange position is the lowest permitted Z value in the
parent `PoseTemplateBase` coordinate system. The other bottom-row points are
65 mm in parent-frame -X and +X; no generated grid point may have a
`PoseTemplateBase.Z` below the taught point.

The program resolves the existing `PoseTemplateBase` `ObjectFrame` and
expresses every generated translation in that parent frame—not in the taught
child frame's axes. Parent X supplies left/center/right, parent Y remains fixed,
and parent Z supplies bottom/middle/top. The generated center is
`(X=0, Y=0, Z=+50 mm)` relative to the taught point. At that center, the
robot-carried target must be centered and fully visible in the intended static
camera, with useful border margin for translation and rotation.

## Relative Motion Plan

Starting at the taught bottom-middle point, the 3 × 3 grid occupies
`PoseTemplateBase.X = -65, 0, +65 mm`, fixed
`PoseTemplateBase.Y`, and bottom-relative
`PoseTemplateBase.Z = 0, +50, +100 mm`.

| Generated point | X from taught bottom | Y from taught bottom | Z from taught bottom | Distance from taught bottom |
| --- | ---: | ---: | ---: | ---: |
| Bottom left | -65 mm | 0 | 0 | 65 mm |
| Bottom middle (taught) | 0 | 0 | 0 | 0 |
| Bottom right | +65 mm | 0 | 0 | 65 mm |
| Middle left | -65 mm | 0 | +50 mm | 82.0 mm |
| Pattern center | 0 | 0 | +50 mm | 50 mm |
| Middle right | +65 mm | 0 | +50 mm | 82.0 mm |
| Top left | -65 mm | 0 | +100 mm | 119.3 mm |
| Top center | 0 | 0 | +100 mm | 100 mm |
| Top right | +65 mm | 0 | +100 mm | 119.3 mm |

The grid phase follows this deterministic perimeter-to-center order:

```text
Bottom-middle → bottom-left → middle-left → top-left → top-center
              → top-right → middle-right → bottom-right → center
```

Each leg is a blocking `LIN_REL` delta from the preceding point; the robot no
longer makes a center-return spoke after every grid observation. The final
bottom-right-to-center diagonal is about 82.0 mm; all other grid legs are 50 or
65 mm. Every grid endpoint is checked against the taught bottom before its
relative delta is issued, and a negative bottom-relative Z is rejected.

There is no separate depth phase. After the grid route reaches the generated
center, orientation follows
`center → -10° → +10° → center` independently for A, B, and C. The direct
negative-to-positive leg is a 20° relative rotation. Translation remains zero
throughout all orientation legs.

The generated sequence contains 18 relative legs: eight grid legs, nine
orientation legs, and center-to-bottom. Every individual relative translation
remains within the 100 mm leg limit, and every generated endpoint remains
within 125 mm of the taught bottom-middle point.

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
4. follows the eight-leg X/Z grid route from that bottom-middle point through
   every other grid point and ends at its generated center;
5. sweeps each A/B/C axis from -10° through +10° and back to center in
   three legs per axis;
6. moves `LIN_REL` -50 mm in `PoseTemplateBase.Z` back to—not below—the
   taught bottom-middle point; and
7. sends the successful terminal marker only after that blocking return
   completes.

Grid translations use `PoseTemplateBase` as their explicit `LIN_REL`
reference. The zero-translation orientation legs use the taught bottom-middle
frame's orientation axes while the flange is at the generated center. Relative
motions use 60% of the requested Cartesian speed, clamped to 8–30 mm/s, plus
3% relative joint velocity, acceleration, and jerk limits. The initial
bottom-middle PTP uses 8% relative joint velocity and the same 3%
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
- Read back the exact parent and bottom-middle paths, the parent
  `PoseTemplateBase` axes, and the taught frame's XYZABC/joint branch. Confirm
  the taught flange position is the lowest allowed parent-frame Z, parent -X
  and +X are the intended left/right grid directions, and parent +Z leads from
  the bottom row to the middle and top rows. The taught child frame's local
  axes do not define grid translation. Confirm the target is rigidly mounted
  and its attachment does not change during the recording.
- Before START, manually position the flange within 25 mm of the taught
  bottom-middle frame.
  The program rejects a farther start without moving, but that proximity check
  does not prove the initial PTP joint-space path is collision-free.
- Simulate every PTP and `LIN_REL` endpoint and swept path. Verify the
  complete generated route keeps
  `PoseTemplateBase.Z >= CalibrationStaticBottomMiddle.Z`; pay particular
  attention to the initial PTP because a Cartesian endpoint contract does not
  constrain its joint-space swept path. Recommission the direct grid legs and
  20° negative-to-positive orientation legs. Check reachability,
  joint/redundancy branch, singularity margin, fixtures, the complete mounted
  target, arm, and cable clearance—not only the flange-origin envelope.
- Verify all grid/orientation results keep the complete target detectable
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
