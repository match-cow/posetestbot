# Acquisition Rewrite Remaining Work

Last reviewed: 2026-08-04

This is the only repository-owned list of unfinished rewrite work. The
software-only rewrite is complete. Exactly five operator-run physical
milestones remain; supporting implementation notes and completed validation
belong in [REWRITE_PROGRESS.md](REWRITE_PROGRESS.md) and Git history.

## Boundary

PoseTestBot ends at a validated BOP dataset. Capture, calibration,
synchronization, optional BlenderProc GT/mask generation, pose-template
provenance, and BOP export are in scope. Estimator execution and standard BOP19
result generation belong in the separate `match-cow/posetestbot-cluster`
companion. PoseTestBot contains only its typed loopback client/proxy, storage
and job presentation, fixed server-configured local controller-service
status/start/stop, and immutable standard-result import/download. Controller
credentials and arbitrary commands remain unavailable to the browser. The sole
evaluation exception is
the Inspect-only, run-scoped official BOP19 validation path: it consumes a
completed annotation-bearing export and an already compatible standard result
CSV, or generates a deterministic test-only slight GT perturbation, and writes
derived evidence only below `processed/bop_evaluation/`. It is not a pipeline
stage.

The lab iiwa is the sole robot profile. Preserve raw capture evidence. Every
physical action requires explicit operator authorization; robot-and-camera
capture requires both execution gates. During repeated calibration, never send
the iiwa UDP `STOP` command.

## Accepted Baseline

The acquisition, synchronization, calibration, pose-template, optional
annotation, BOP export, Inspect-only evaluation, background-job, Flask API, and
packaged operator-console contracts are implemented and covered by the
non-hardware validation recorded in
[REWRITE_PROGRESS.md](REWRITE_PROGRESS.md).

The earlier nine-frame calibration Sunrise deployment is operationally
accepted. The operator attests that the controller class then named
`PoseTestBot_CalibrationVarianceProposal` compiled in Workbench, its nine
persistent frames were taught, the program was physically commissioned, and
the guided captures completed successfully. Its renamed repository counterpart
is `PoseTestBotNineFrameCalibrationApplication`; that historical acceptance
does not establish deployment of the renamed/high-rate source. The repository
also retains three completed guided calibration runs and promoted attempt
`268c897e1baf49e7bd78a434a4569b99`; its common `IPPE + Shah` profiles pass
`rewrite_calibration_validation.v1` at 3/3.

The exact Workbench project, controller revision record, and completed
frame-teaching worksheet are not available to copy into this repository. That
provenance must not be reconstructed or implied. Operator attestation plus the
retained capture and calibration artifacts closes the calibration-program
rewrite milestone. The teaching checklist remains a reusable procedure for
future recommissioning or cell changes, not an unfinished rewrite gate.

That acceptance applies to the attested earlier controller revision. The
repository's 2026-08-03 high-rate pose-stream source is a prospective
improvement and has not been deployed or physically measured. It does not
invalidate retained calibration evidence, but it must be compiled and
recommissioned before replacing the accepted controller program.

The ordinary full-capture Sunrise application is separate from the accepted
calibration program and remains open under milestone 1.
The additional `PoseTestBotSingleFrameStaticCameraCalibrationApplication` is a
repository candidate with one taught bottom-center grid point and a generated
center for its bounded parent-frame X/Z grid and swivel motions. It does not
inherit the nine-frame program's historical physical acceptance.

## 1 — IIWA Controller Commissioning and Cadence Rollout

Follow
[IIWA_FULL_CAPTURE_APPLICATION.md](IIWA_FULL_CAPTURE_APPLICATION.md) and the
[single-frame static-camera calibration contract](IIWA_SINGLE_FRAME_STATIC_CAMERA_CALIBRATION.md).
The gating dataset outcome remains ordinary pose-template capture, not the
accepted earlier nine-frame calibration revision. The shared high-rate task is
coupled to this controller commissioning work and does not add a sixth rewrite
outcome.

- [ ] In the exact Workbench project, create the shared
  `PoseTestBotPoseStreamTask` as an automatic cyclic background task, include
  its task-function interface, and compile the full-capture, nine-frame, and
  single-frame calibration application revisions against the installed
  Sunrise.OS API.
- [ ] For the single-frame static-camera alternative, create and read back only
  `/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle` beyond the
  existing `/PoseTestBot/PoseTemplateBase`. Verify its flange position is the
  lowest allowed Z in the parent coordinate system, parent X supplies the
  intended left/right columns, and parent +Z supplies the middle/top rows.
  Simulate and T1-commission the parent-frame X/Z grid from the taught point to
  the generated center, every orientation leg, and the return to the taught
  point. Verify the 100 mm relative-leg and 125 mm taught-point envelopes, the
  absence of any
  generated point below the taught parent-frame Z, the read-only 25 mm
  start-proximity rejection, and target visibility plus swept
  target/arm/cable clearance. Confirm retained static-camera target centroids
  clear the research-stage 15% image-width, 20% image-height, and 3%
  supported-hull minima; retain the measured coverage and pursue wider motion
  where the cell permits it rather than treating those minima as an ideal
  calibration trajectory.
- [ ] Before replacing the accepted calibration deployment, revalidate its
  unchanged motion/frame contract and retain a supervised cadence report.
  Target at least 50 Hz median host receive rate, no more than 25 ms p95 gap,
  and no more than 40 ms maximum in-motion gap; retain and investigate any
  miss without treating cadence alone as a calibration-attempt failure. Follow
  [IIWA_POSE_STREAM_CADENCE.md](IIWA_POSE_STREAM_CADENCE.md); never send UDP
  `STOP` between repeated calibration captures.
- [ ] After the high-rate calibration revision is deployed and target
  visibility is improved, create a fresh run for the three exact static D435
  cameras. Bind the printed grid to `robot_flange` with unknown attachment,
  perform one explicitly authorized supervised capture, and retain the
  three-camera static-world attempt (internally the eye-to-hand equation).
  Keep other printed grids that reuse the same marker dictionary outside the
  camera views where practical. The solver can retain one strong 8-marker,
  3-row, 3-column planar instance with a warning, but clutter filtering is
  reviewable fallback evidence rather than the preferred capture arrangement.
  Require every robot pose packet to use
  `sunrise_reference_frame_path=/PoseTestBot/PoseTemplateBase`, passing
  `camera -> PoseTemplateBase` profiles, and mutually consistent estimated
  `aruco_grid -> robot_flange` support evidence before promotion. The latter is
  a nuisance transform, not a runtime hand-tracking output. Do not reinterpret
  an older run whose saved camera mounting or pose reference is wrong.

- [ ] Identify and record the exact Sunrise application deployed for ordinary
  capture. `iiwa/PoseTestBotFullCaptureApplication.java` remains a repository
  candidate, not proof of the deployed application or revision.
- [ ] Compile and simulate the exact ordinary-capture controller project before
  deploying it or reconciling the repository source with the deployed source.
- [ ] Create, teach, and read back the distinct persistent frames
  `/PoseTestBot/PoseTemplateBase`, `/PoseTestBot/CaptureStart`, and
  `/PoseTestBot/CaptureEnd`. Retain `/PoseTestBot/TemplateBase` only as the
  nine-frame calibration application's commissioned motion-waypoint parent;
  the single-frame alternative anchors its relative motions below
  `/PoseTestBot/PoseTemplateBase`. Confirm that the calibration pose-stream
  task queries the flange relative to `/PoseTestBot/PoseTemplateBase`; the
  static solver does not require a measured transform between the waypoint
  parent and result frame.
  The software now retains and compares exact v1
  `sunrise_reference_frame_path` values and refuses legacy, undeclared, or
  mismatched static-profile reuse. Equal path strings are not evidence that a
  persistent frame was never retaught, so this commissioning/read-back item
  remains required.
- [ ] Commission the complete PTP/A1/PTP path in the installed cell, including
  joint branch, singularity, clearance, payload, camera-rig, cable, dwell,
  speed, and final-pose checks.
- [ ] Verify that the selected pose-template placement, robot pose stream, and
  every selected camera profile are expressed in one ordinary dataset
  `template_base` reference.
- [ ] Retain the controller identity, Workbench/offline evidence, frame
  read-backs, T1 sign-off, operator/reviewer/date, and supervised trial
  evidence. Any robot-and-camera trial requires explicit authorization and
  both execution gates.

## 2 — Camera-Service Acceptance

This milestone must not send a robot command or start an acquisition pipeline.

- [ ] On the operator-ready lab host, run the UGREEN WebRTC monitor, all three
  RealSense previews, and the OAK-D Pro preview concurrently for at least
  30 seconds.
- [ ] Exercise the console through `10.145.8.132` and the current Tailscale
  address with two simultaneous UGREEN peers. Require all applicable media and
  frame counters to continue advancing.
- [ ] Capture and validate RGB/depth snapshots from the three RealSense cameras
  and OAK-D Pro while the service matrix is active.
- [ ] Repeat across graceful web-server `SIGTERM`, forced `SIGKILL`, restart,
  and an individual monitor-worker crash. Require owned processes and device
  handles to clear within five seconds, then require supported streams to
  restart.
- [ ] Retain a timestamped report and logs below
  `working_data/web_camera_acceptance/`, including device identities,
  endpoints, counters, process identities, release timing, and failures.

ZED live preview is not implemented and is not required here. ZED capture is
covered by milestone 3.

## 3 — Current Five-Sensor Capture

This depends on milestones 1 and 2 and on an operator-ready robot/camera cell.

- [ ] Install and verify `pyzed.sl` on the lab host, then verify all three
  configured RealSense serials, OAK-D Pro, and ZED 2i are visible with the
  intended identities, run-owned alias snapshots, mounts, orientations,
  resolutions, and frame rates.
- [ ] Create a fresh run root at the reviewed capture velocity. Never reuse a
  root containing raw camera frames or raw robot poses.
- [ ] Generate and inspect `real_full_capture_validation` in plan-only mode,
  including receiver routing, sensor identities, resources, startup order,
  output folders, timeouts, and overwrite blockers.
- [ ] With explicit operator authorization and both execution gates, run a
  short supervised trial and then the deliberate full capture.
- [ ] Require balanced RGB/depth/metadata tuples for every selected sensor,
  nonempty robot poses, clean process/device release, acceptable sync quality,
  and a 10/10 `rewrite_full_capture.v1` result.
- [ ] Preserve the plan, preflight, execution status/report/logs, hardware
  snapshot, raw folders, synchronization reports, and rewrite-gate report as
  acceptance evidence.

This milestone uses the supported timestamp-aligned five-sensor contract.

## 4 — RealSense Metric Depth-Scale Recheck

- [ ] After the planned cable and firmware maintenance opportunity, repeat the
  controlled aligned-depth measurements for RealSense `923322072633` at
  multiple known distances. Saved checks showed a range-dependent scale
  anomaly; the factory depth scale and alignment have not been recalibrated.
- [ ] Record raw measurements, device/firmware/cable identity, environmental
  setup, expected distances, fitted error, and the resulting accept,
  service/recalibrate, or exclude decision. Do not silently correct captured
  depth or overwrite earlier evidence.

## 5 — Physical Pose-Template Review

- [ ] Import and classify the intended real CAD and texture assets through
  **Workpiece Catalogue**.
- [ ] Compare compact and exact interactive previews and millimetre dimensions
  against the physical workpieces. Confirm representative holes, ports,
  handles, recesses, axes, and stable-orientation choices.
- [ ] Generate an immutable printable pose template from the reviewed active
  workpieces, print and measure it, and verify the intended object identities
  and placements.
- [ ] Record and verify the full
  `template_base_from_pose_template` transform for a fresh pose-template run,
  preserving the catalogue revision, geometry hashes, template bundle,
  measurements, photographs or review evidence, and operator/reviewer/date.

Physical duplicate-instance repetition is not required to close this
milestone; the implemented multiple-instance contracts and synthetic
regressions remain supported.

## Retained Contracts, Not Open Tasks

- Current acquisition inputs are intentionally narrow: `run_config.v3`,
  `calibration.v2`, `calibration_target.v2`, and `sync_report.v3`. Retired
  config/profile/sync/target migrations and the old standalone entry points are
  not repository compatibility surfaces. Historical calibration-attempt
  evidence remains inspectable but cannot be rerun or promoted.
- `docs/IIWA_CALIBRATION_TEACHING_CHECKLIST.md` remains the operational
  reference for future calibration-program recommissioning. Its blank
  worksheet fields are not evidence gaps for this accepted rewrite.
- All supported acquisition uses timestamp alignment. PoseTestBot does not
  configure or qualify hardware triggering, maintain cross-camera exposure
  groups, or export multiview synchronization claims.
- The retained real BOP v5 dataset and its 11/11 readiness evidence supersede
  any requirement to regenerate the older v4 derived export.
- External estimator execution, SSH transfer, durable SLURM state, and standard
  BOP19 result generation belong to `posetestbot-cluster`. Its archive domain
  is estimator-independent; FoundationPose and future methods are trusted
  companion drivers behind a generic manifest/API. The thin controller proxy,
  estimator-job logs/cancellation, archive copy/restore, and immutable
  standard-result import/download are retained Inspect contracts here, not
  acquisition-pipeline stages. Remote-source deletion is not exposed.
- Production-bundle size tuning is optional engineering work, not a rewrite
  gate.
- Catalogue JSON portability remains intentionally metadata-only. Managed
  binary asset trees are preserved or moved as filesystem data; a one-file
  binary catalogue format is not planned.
- Job folders and logs are retained indefinitely. The rebuildable SQLite
  index, server-side pagination, and exact-ID loading are the completed
  non-destructive history contract; pruning is not planned.

## Completion

The rewrite is complete when all five milestones above have retained,
reviewable physical evidence and the intended real dataset passes its
applicable acquisition rewrite gates. Software regressions must continue to
follow the repository validation commands in `AGENTS.md` and must never open
cameras, contact the robot, or record physical data.

Articulated iiwa rendering remains deferred until approved robot geometry,
joint-state evidence, and transforms exist. Estimator runtimes, result
conversion, general evaluator bridges, evaluation pipeline stages, and legacy
metric-report exports remain outside this repository.
