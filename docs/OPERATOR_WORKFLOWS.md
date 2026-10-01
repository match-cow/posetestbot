# Operator workflows

**Workflow** has exactly two guided outcomes: calibrate cameras and record an
object dataset. The desktop step rail is the canonical operating surface.
Supporting pages author reusable inputs or inspect evidence; they do not
silently mutate the active run.

Every long-running action is a background job. A submission continues after
navigation and remains visible on **Jobs** with its resources, status, log, and
failure evidence.

The compact **Active acquisition run** control in the top bar switches the
browser-local run context in place, so operators can move between discovered
runs without leaving the current page. Use **Run folders** beside it when
creating a fresh acquisition folder or performing inventory, archive, move, or
delete work. Switching context does not modify either run.

## Scope of supporting pages

| Page | Scope | Workflow handoff |
| --- | --- | --- |
| Dashboard | Read-only status plus the sole manual IIWA Start/Stop controls | Return to the active workflow after checking the cell |
| Devices | Reusable sensor aliases/mounting/orientation defaults, read-only discovery, previews, snapshots | Workflow step 1 exclusively selects cameras and snapshots/edits run-owned settings |
| Cell View | Run-owned geometry, camera frames, trajectory, and provenance | Review capture or exported dataset evidence |
| Calibration Targets | Global reusable printable target bundles | Select the exact physical grid for calibration step 2 |
| Workpiece Catalogue | Global CAD/geometry metadata and lifecycle | Author inputs before creating a pose template |
| Pose Templates | Global immutable template authoring and run selection | Confirm placement in dataset step 2 |
| Run Folders | Contained run inventory, move/delete, and cluster archive copy/restore/delete | Choose a root before creating configuration |
| Pose Estimation | Browser-safe handoff with closed, driver-advertised settings | Requires an appropriate completed BOP export; results return to Inspect |
| Pose Results | Read-only frame-level estimate/GT comparison | Consumes one retained compatible result; links to evaluation or back to the external handoff |
| BOP Evaluation | Inspect-only standard-result validation | Requires annotations and an immutable BOP19 CSV |

## Outcome 1: calibrate cameras

### 1. Configure the run and cameras

Choose a fresh contained run root, set calibration intent, and select exact
sensor identities, mounting mode, resolution, frame rate, orientation, and
supervised velocity. Saving writes `run_config.v4`; it does not open hardware
or authorize motion.

The **Devices** page does not select cameras for a future run. Workflow step 1
lists detected cameras alongside any saved run cameras; **Use for this
recording** is the only camera-membership control, and the choice becomes
durable only when setup is saved.

All enabled cameras in one attempt must use the supported mounting
arrangement. Robot-mounted cameras observe a grid fixed in
`PoseTemplateBase`; static cameras observe a grid rigidly attached to the
robot flange.

### 2. Choose the printed grid and its mounting

Select the immutable bundle that exactly matches the physical board. The run
records its UUID, hashes, geometry, and mounting frame. Generate a new global
bundle only when the printed target changes. When target selection was opened
from this workflow step, a successful selection returns directly to step 3;
the standalone library page remains available for reusable-target authoring.

### 3. Check readiness

Queue the consolidated preflight and resolve every visible blocker. The saved
report identifies the configuration checked. Preflight briefly opens every
enabled selected camera through its configured RGB-D adapter and requires one
frame, but records nothing. SDK enumeration alone is insufficient: a camera
held by a stale or crashed recorder blocks readiness. Readiness is evidence,
not execution permission; the camera-open check is repeated at capture startup.

### 4. Record calibration images

Mount the selected target as recorded, clear the workcell, and use the single
fresh checkbox to authorize both camera access and robot execution. The fixed
recipe rechecks selected-camera availability before writing its plan, plan
preflight, execution plan, status, logs, or raw sensor folders. A blocked camera
therefore leaves the run reusable. After camera startup begins, partial evidence
is retained if a child fails. Do not send IIWA Stop between calibration captures.

### 5. Calculate, review, and publish

Create one intent-level attempt for the selected cameras and target. Choose
explicit fixed-zero or automatic time alignment, then inspect intrinsic
comparison, timestamp evidence, PnP/extrinsic candidates, ranking, checks, and
per-camera recommendations.

For robot-mounted cameras, automatic alignment now measures angular timing from
reversible rotations near the viewing axis. Review the measured delay and its
uncertainty alongside the applied 5 ms grid offset. A small change in spatial
translation residual does not veto a supported angular timing measurement.
If timing remains inconclusive, retained 0 ms means the delay was not identified;
it does not establish that the camera has no delay. Reanalyze the existing
recording in a new attempt after a timing-policy update. See
[automatic time alignment](reference/api/calibration.md#automatic-time-alignment).

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
storage, status, or path blockers before capture. Each enabled selected camera
must also pass the bounded one-frame, non-recording open probe; a merely
enumerated but process-blocked camera fails readiness.

### 4. Record the object dataset

Place objects exactly as confirmed, clear the workcell, and submit the single
fresh acknowledgement covering both execution permissions. Raw RGB, depth,
current frame metadata, and strict `robot_pose.v1` packets are preserved. Never
reuse a prior run folder for a new physical capture.

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

### Inspect: run an external pose estimator

Open **Pose Estimation** only after the active run has an immutable,
annotation-bearing BOP export. Each sensor sequence is described by a safe
sensor identifier, its run-owned alias, mounting mode, BOP scene ID, and frame
and target counts. Source paths and cluster controls never enter the browser.

The estimator's settings are a browser-local draft until submission. For
FoundationPose v2 the advertised default is continuous tracking over every
eligible sensor. Deselect sensors to create an explicitly sensor-scoped job,
or select independent registration for the per-frame baseline. At least one
eligible sensor is required in tracking mode. Submission snapshots the closed
settings into the immutable controller job; changing the draft cannot mutate
that job.

Tracking is camera-local: sensor sequences and object instances do not share
track state. A visible GT mask initializes each continuous segment, a missing
target frame resets it, and a tracker error causes same-frame registration
recovery. These oracle masks are initialization/recovery inputs, so the result
must not be presented as an unconstrained detector benchmark.

Every submission creates a distinct durable controller job, even when several
jobs use the same unchanged run. **Pose Estimation** retains a bounded selector
for the selected estimator's jobs on the active run; changing that selector
changes the exact job shown and never mutates it. The **Jobs** page combines
global path-redacted monitoring with a separate active-run lookup. Only jobs
proven by that lookup to belong to the active run expose result actions; select
another run through the normal run context before managing its results.

When a selected controller job succeeds, use the explicit **Collect result**
action on **Pose Estimation** or **Jobs**.
Collection rechecks controller provenance, the staged and local dataset hashes,
the standard BOP19 CSV, and the result hash before retaining it below
`processed/bop_evaluation/results/`. Collection is idempotent, and its state is
recovered from the retained external job ID after a browser or service restart;
no automatic page effect downloads a result. The retained-result actions expose
the CSV, sanitized controller provenance, a deterministic package, **Pose
Results**, and **BOP Evaluation**. Each successfully collected job receives a
separate immutable result ID, and the result selectors switch among all results
that remain compatible with the unchanged BOP dataset.

**Pose Results** is a read-only x-ray diagnostic. Select the retained result,
operator-labelled sensor scene, and frame; then compare the exact-aspect RGB or
colorized depth image with projected evaluation-model geometry. The default is
a translucent cyan estimated surface and magenta GT wireframe. Estimated and GT
surfaces, wireframes, axes, boxes, full masks, and visible masks are independent
browser-local controls. The overlay does not claim observed-depth occlusion.
Direct result links include the run root, so they keep inspecting the correct
export even if another run is active in the browser. Older links containing
only a result ID are resolved against directly indexed, approved run folders;
missing or ambiguous IDs fail closed. When the linked run differs from the
active operator run, the page shows both scopes and offers **Make linked run
active** before returning to other workflow pages.
Evaluation links also carry the run root. A missing linked result disables
evaluation until a retained result is explicitly selected; it never substitutes
another result's metrics.
Next to the result CSV download, **All-sensor GT JSON** downloads one
`scene_gt_all_sensors.json` collection for the full exported dataset. It maps
each BOP scene ID to its sensor in `sensor_by_scene_id`, and stores poses under
`scene_gt[scene_id][im_id]`. Match those IDs with the result CSV's `scene_id`
and `im_id` for manual comparison. The translation values are in millimetres.
The original per-scene BOP `scene_gt.json` files remain unchanged.
Translation and symmetry-unaware rotation deltas appear only for an unambiguous
single estimate-to-GT association. Repeated identical objects remain visibly
ambiguous. Registration/tracking labels appear only when the retained sanitized
FoundationPose evidence proves them; generic BOP19 results show `unknown`. If
WebGL is unavailable, images, masks, navigation, and numeric evidence remain
usable while geometry controls are visibly disabled.

For a
sensor-scoped FoundationPose v2 result, PoseTestBot independently recomputes
the selected target inventory and its hash before accepting it. **BOP
Evaluation** then writes an immutable filtered target list inside that
evaluation and passes it to the pinned official toolkit. The report is clearly
labelled sensor-scoped and is not directly comparable with a full-dataset
result. External estimator execution, conversion, SSH, and scheduling remain
owned by the companion repository.

**BOP Evaluation** first selects one retained result, then requires an explicit
official-toolkit submission, and finally shows only that result's evaluation
history and aggregate report. Manual standard-CSV import is a secondary
expandable action. Deterministic GT perturbation remains isolated under the
advanced, test-only section and is never estimator-performance evidence.

## Physical controls

The Dashboard is the only page with the IIWA **Start program** and **End
program** controls.

- Start requires a configured run and one fresh acknowledgement covering
  camera/receiver readiness and robot motion. The request still carries the
  separate `allow_cameras` and `allow_real_robot` gates so either missing
  permission fails closed.
- **End program** requires explicit confirmation. Its confirmation dialog
  explains that it cannot interrupt active motion, is not an emergency stop,
  and exits only an idle waiting program.
- The target is always the fixed lab profile `172.31.1.147:30300`; the browser
  cannot override IP or port.
- Capture cancellation signals every local child process group before waiting on
  the shared grace period. Repeated cancellation signals are idempotent and do
  not interrupt cleanup. Descendants are stopped even if their launcher has
  already exited. Cancellation never sends IIWA Stop.

See [Safety and authorization](concepts/safety.md) and [Physical
commissioning](COMMISSIONING.md).
