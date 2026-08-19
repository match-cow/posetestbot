# Physical commissioning

The five tasks below require an authorized operator on the lab system and
retained, reviewable evidence. They are not performed by repository tests or
installation commands.

Never treat a successful status request as permission to move the robot. Use
the fixed lab profile, inspect the generated plan, and require one fresh
operator acknowledgement covering both camera and robot gates for capture.
The IIWA Stop control only exits an idle waiting application; it cannot
interrupt motion and is not an emergency stop.

## 1. IIWA deployment and pose cadence

- Compile the three current Sunrise applications against the installed
  Sunrise.OS API, including the single-frame application's no-reference
  `linRel(Transformation)` overload, and deploy the shared
  structured-command/pose-stream code.
- Verify `/PoseTestBot/TemplateBase`, `/PoseTestBot/PoseTemplateBase`, the nine
  calibration frames, the ordinary-capture start/end frames, and
  `/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle` in Workbench.
  For that single-frame anchor, verify live flange/default-TCP +X is
  image-down/toward the floor, +Y is robot-left/image-right, and +Z is toward
  the primary camera; confirm that the route uses negative X to move up and
  negative Y to move image-left, and that no separately offset Workbench `Tool`
  TCP changes the motion contract. Keep
  `PoseTemplateBase` unchanged as the pose-stream and static-result frame.
- Simulate every path, including all 22 single-frame relative legs and their
  complete swept volumes, then commission each endpoint and swept path by
  explicitly authorized T1 single-stepping at reduced override under the lab
  safety process. Do not send the UDP Stop command during calibration.
- Retain the deployed revision, frame read-back, path review, and
  `processed/robot_pose_cadence_report.json`. Review a cadence warning rather
  than discarding otherwise complete calibration evidence solely for missing a
  conservative cadence target.

See [IIWA full capture](IIWA_FULL_CAPTURE_APPLICATION.md), [single-frame
static calibration](IIWA_SINGLE_FRAME_STATIC_CAMERA_CALIBRATION.md), and
[pose-stream cadence](IIWA_POSE_STREAM_CADENCE.md).

## 2. Camera-service acceptance

- On the operator-ready host, exercise the room monitor, all three RealSense
  cameras, the OAK-D Pro, and the ZED 2i through the console.
- Confirm the fixed UGREEN room-monitor view is upright after its 180° stream
  correction.
- Verify previews and RGB/depth snapshots from each selected device through
  the lab LAN and the approved remote-access path.
- Repeat clean shutdown, forced termination, service restart, and stale-worker
  recovery checks. Retain timestamped logs and status evidence without sending
  a robot command.

## 3. Five-sensor capture

- Verify the ZED SDK and all five configured sensors with the read-only status
  commands, then create a fresh `run_config.v4` dataset run.
- Run `uv run python scripts/plan_capture.py <run> --json` and review the fixed
  plan and preflight evidence.
- With explicit authorization and the combined execution acknowledgement,
  perform a short supervised trial before the full capture.
- Require balanced nonempty RGB/depth/current-metadata tuples for every enabled
  sensor, a nonempty current robot-pose stream, successful child processes,
  and clean resource release. Preserve all raw evidence and capture reports.

## 4. RealSense metric depth-scale recheck

- After cable or firmware maintenance, repeat the metric-depth check for
  RealSense `923322072633`.
- Record raw measurements, device/firmware/cable identity, environmental
  conditions, factory scale, alignment evidence, and the operator conclusion.
- Do not silently change calibration or depth scale from a status observation;
  investigate and record any discrepancy first.

## 5. Physical pose-template review

- Import the intended CAD and texture assets through the Workpiece Catalogue
  and confirm canonical millimetre dimensions against the physical parts.
- Compare compact and exact previews for identifying features, then publish an
  immutable printable pose-template bundle from reviewed active revisions.
- Verify the printed arrangement and the full
  `template_base_from_pose_template` transform for a fresh dataset run.
- Retain the selected bundle, object-instance snapshot, print, photographs,
  measurements, and reviewer sign-off as run provenance.
