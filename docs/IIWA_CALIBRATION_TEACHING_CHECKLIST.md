# IIWA nine-frame teaching checklist

Use this worksheet to commission
`iiwa/PoseTestBotNineFrameCalibrationApplication.java` in the exact
Sunrise.Workbench project. The authoritative numeric plan is
`iiwa/calibration_teaching_plan.v2.json`.

This is a teaching aid, not reachability, redundancy, singularity, collision,
or cable-clearance validation. Physical work requires the lab risk assessment,
controller safety functions, explicit authorization, an operator, and a
reviewer. Never send the UDP Stop command during repeated calibration. UDP stop messages cannot interrupt active motion and are not an emergency stop;
while idle they exit the waiting application, which requires a manual application restart.

## Deployment record

| Record | Value |
| --- | --- |
| Controller and robot model | |
| Sunrise.OS version | |
| Workbench project revision | |
| PoseTestBot source revision | |
| Tool/load and payload/CoG | |
| Camera rig and enabled sensor identities | |
| Printed target bundle UUID/hash | |
| Operator / reviewer / date | |

- [ ] Back up and synchronize the Workbench project and Application Data.
- [ ] Add `PoseTestBotPoseStreamTask` through the Workbench background-task
  workflow, configure automatic start, and include exactly one
  `PoseTestBotPoseStreamFunction` provider.
- [ ] Compile all current structured-protocol sources against the installed
  Sunrise.OS API. Older UDP command or pose-packet shapes are unsupported.
- [ ] Verify `/PoseTestBot/TemplateBase` as the waypoint parent and
  `/PoseTestBot/PoseTemplateBase` as the independent pose-stream/dataset frame.

## Teach and read back frames

Create exactly these direct children of `/PoseTestBot/TemplateBase`:

| Frame path | Created | Seed entered | Touched | XYZABC read-back | 7 joints/redundancy recorded | Reach/joint/singularity OK | arm/rig/cable clearance OK | required cameras detect target | reviewer/date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `/PoseTestBot/TemplateBase/CalibrationCoverageUpperLeft` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageUpperCenter` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageUpperRight` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageMiddleRight` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCenter` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageMiddleLeft` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageLowerLeft` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageLowerCenter` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| `/PoseTestBot/TemplateBase/CalibrationCoverageLowerRight` | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |

Use numeric values only as uncommissioned seeds. Manually jog in T1, teach
`CalibrationCenter` first, then progress outward. If a frame is retouched,
invalidate and recommission every connected path.

## Offline simulation

- [ ] Resolve all nine frames and both base frames without substitution.
- [ ] Validate the raster path:
  `Center → UL → UC → UR → MR → Center → ML → LL → LC → LR → Center`.
- [ ] Confirm center transits are PTP and raster legs are LIN.
- [ ] Validate the nine zero-translation orientation legs: A −15°, +30°,
  −15°; B −12°, +24°, −12°; C −15°, +30°, −15°.
- [ ] Confirm all nine orientation motions are `LIN_REL` relative to the taught
  center, leave flange XYZ fixed as intended, and return to the expected pose.
- [ ] Check joint branch, margins, arm/rig/fixture/cable clearance, and required
  camera visibility at every endpoint and swept path.
- [ ] Confirm the cyclic task requests 10 ms `BestEffort`, contains no motion
  command, and exposes send/query failures to the application.

| Offline evidence | Result / location | Reviewer / date |
| --- | --- | --- |
| Compile and frame resolution | | |
| Raster simulation | | |
| Orientation simulation | | |
| Complete path simulation | | |

## Physical T1 commissioning

- [ ] Obtain explicit operator authorization and confirm the reviewed physical
  cell, load, cameras, target, fixtures, and cables.
- [ ] Pass both PoseTestBot execution gates: `--allow-real-robot` and
  `--allow-cameras`.
- [ ] Manually position near center, then single-step the initial PTP anchor at
  reduced override.
- [ ] Single-step the raster transits and each orientation leg while checking
  the actual joint branch, clearance, fixed-origin behavior, and visibility.
- [ ] Verify the configured 3% acceleration/jerk limits, 3% orientation joint
  velocity, 1.5-second dwells, and configured raster velocity without treating
  software limits as safety functions.
- [ ] Confirm each dwell yields sharp stationary frames and `_settled` pose
  samples.
- [ ] Use the controller's approved safety response for any unexpected motion,
  branch, clearance, cable, target, or vibration issue—not UDP Stop.

## Supervised acceptance capture

- [ ] Create a fresh calibration-intent `run_config.v4` and select the exact
  target bundle and mounting frame.
- [ ] Queue preflight and inspect the fixed plan before deliberately submitting
  both fresh physical execution acknowledgements.
- [ ] Run a short trial before the full capture. Require current metadata and
  strict `robot_pose.v1` evidence for every enabled camera.
- [ ] Retain the capture plan, preflight, execution plan/status/report/logs,
  raw evidence, and `processed/robot_pose_cadence_report.json`.
- [ ] Aim for median pose cadence at least 50 Hz, p95 gap at most 25 ms, and
  maximum gap at most 40 ms. Investigate misses without rejecting an otherwise
  valid calibration attempt solely for this conservative cadence target.
- [ ] Create the calibration attempt, inspect timestamp/intrinsic/geometric
  evidence per camera, and explicitly promote only reviewed passing profiles.
- [ ] For every required camera, retain at least 15 accepted views and confirm
  supported field coverage. For eye-in-hand captures, require normalized
  centroid spans of at least 45% image width and 35% image height plus at least
  10% supported centroid-hull area. For research-stage static eye-to-hand captures,
  require 15% width, 20% height, and 3% hull area; treat the 3 × 3
  centroid-cell count as a warning rather than an absolute-position veto.
- [ ] Keep factory projection when compatible. Activate a fitted OpenCV model
  only when factory projection is unusable and training covers at least 6/9 image-centroid cells
  with passing held-out, plausibility, per-view, and RMS checks.

| Final decision | Selection / notes |
| --- | --- |
| Deployed source agreement | ☐ |
| Offline simulation approved | ☐ |
| T1 commissioning approved | ☐ |
| Supervised capture accepted | ☐ |
| Promoted profile IDs | |
| Frames requiring retouch | |
| Operator / reviewer / date | |
