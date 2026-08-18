# Operator workflows

**Workflow** has exactly two guided outcomes: calibrate cameras and record an
object dataset. The desktop step rail is the canonical operating surface.
Supporting pages author reusable inputs or inspect evidence; they do not
silently mutate the active run.

Every long-running action is a background job. A submission continues after
navigation and remains visible on **Jobs** with its resources, status, log, and
failure evidence.

## Scope of supporting pages

| Page | Scope | Workflow handoff |
| --- | --- | --- |
| Dashboard | Read-only status plus the sole manual IIWA Start/Stop controls | Return to the active workflow after checking the cell |
| Devices | Reusable sensor aliases, read-only discovery, previews, snapshots | Workflow step 1 snapshots/edit run-owned camera settings |
| Cell View | Run-owned geometry, camera frames, trajectory, and provenance | Review capture or exported dataset evidence |
| Calibration Targets | Global reusable printable target bundles | Select the exact physical grid for calibration step 2 |
| Workpiece Catalogue | Global CAD/geometry metadata and lifecycle | Author inputs before creating a pose template |
| Pose Templates | Global immutable template authoring and run selection | Confirm placement in dataset step 2 |
| Run Folders | Contained run inventory, move/delete, cluster archive | Choose a root before creating configuration |
| Pose Estimation | Browser-safe handoff to advertised external estimators | Requires an appropriate completed BOP export |
| BOP Evaluation | Inspect-only standard-result validation | Requires annotations and an immutable BOP19 CSV |

## Outcome 1: calibrate cameras

### 1. Configure the run and cameras

Choose a fresh contained run root, set calibration intent, and select exact
sensor identities, mounting mode, resolution, frame rate, orientation, and
supervised velocity. Saving writes `run_config.v4`; it does not open hardware
or authorize motion.

All enabled cameras in one attempt must use the supported mounting
arrangement. Robot-mounted cameras observe a grid fixed in
`PoseTemplateBase`; static cameras observe a grid rigidly attached to the
robot flange.

### 2. Choose the printed grid and its mounting

Select the immutable bundle that exactly matches the physical board. The run
records its UUID, hashes, geometry, and mounting frame. Generate a new global
bundle only when the printed target changes.

### 3. Check readiness

Queue the consolidated preflight and resolve every visible blocker. The saved
report identifies the configuration checked. Readiness is evidence, not
execution permission; time-sensitive checks are repeated at capture startup.

### 4. Record calibration images

Mount the selected target as recorded, clear the workcell, and explicitly
acknowledge both camera access and robot execution. The fixed recipe writes its
plan, plan preflight, execution plan, status, logs, and completion report.
Partial evidence is retained if a child fails. Do not send IIWA Stop between
calibration captures.

### 5. Calculate, review, and publish

Create one intent-level attempt for the selected cameras and target. Choose
explicit fixed-zero or automatic time alignment, then inspect intrinsic
comparison, timestamp evidence, PnP/extrinsic candidates, ranking, checks, and
per-camera recommendations.

Compatible factory intrinsics remain the default. An OpenCV fit is activated
only when factory projection is unusable and the fitted model passes all
coverage, held-out, plausibility, and error checks. A lower RMS alone is not a
selection rule.

Promotion is a separate, explicit action with operator provenance. Only
passing selected candidates become reusable `calibration.v2` profiles.
Research-quality warnings remain prominent but do not discard complete,
internally valid evidence solely for missing a conservative metrology target.

## Outcome 2: record an object dataset

### 1. Configure cameras and select calibration

Create a fresh dataset-intent `run_config.v4`. Select a promoted calibration
for every enabled camera with exact sensor identity, resolution, mounting, and
orientation. PoseTestBot copies and hash-binds the combined calibration and
timing policy into `processed/calibration_inputs/<bundle_sha256>/`; later
source-run edits cannot change this dataset.

### 2. Choose the pose template and placement

Select an immutable printable template and confirm that exact physical print
and object arrangement. Measure and record the full
`template_base_from_pose_template` transform. The run snapshots
`pose_template_selection.json` and `object_instances.json`.

Use **Workpiece Catalogue** and **Pose Templates** only when authoring a new
library item; return to this workflow to bind it to the active run.

### 3. Check readiness

Queue preflight after calibration and placement are confirmed. Resolve missing
camera profiles, stale hashes, invalid timing policy, target/template conflict,
storage, status, or path blockers before capture.

### 4. Record the object dataset

Place objects exactly as confirmed, clear the workcell, and submit both fresh
execution acknowledgements. Raw RGB, depth, current frame metadata, and strict
`robot_pose.v1` packets are preserved. Never reuse a prior run folder for a new
physical capture.

### 5. Process frames and create the base BOP export

Queue the one fixed processing job:

```text
non-destructive sync → sync quality → rectification → calibrated BOP export
```

Synchronization uses the selected per-camera timestamp fields, clock-domain
rule, offset, and nearest-pose limit. It cannot be overridden by browser
defaults. Quality measures eligible in-motion coverage; preserved lead-in and
tail frames are not dataset failures. Export revalidates calibration and input
hashes before writing `bop_export_manifest.v5`.

### 6. Optionally add BOP ground truth

The base image/model export is a complete acquisition outcome. If
`bop.annotation_mode` is `pose` or `pose_and_masks`, deliberately queue the
matching optional job after base export. Pose mode adds `scene_gt.json`; the
full mode also renders masks, visible masks, and visibility information.

Only an annotation-bearing dataset can use the Inspect evaluation path. Pose
estimation itself remains in the separate controller/consumer boundary.

## Physical controls

The Dashboard is the only page with IIWA Start and Stop.

- Start requires a configured run and fresh camera and robot acknowledgements.
- “Stop / exit idle IIWA program” requires explicit confirmation. It cannot
  interrupt active motion and is not an emergency stop.
- The target is always the fixed lab profile `172.31.1.147:30300`; the browser
  cannot override IP or port.
- Capture cancellation stops/cleans child processes but never sends IIWA Stop.

See [Safety and authorization](concepts/safety.md) and [Physical
commissioning](COMMISSIONING.md).
