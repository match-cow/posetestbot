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

For a configured run, the Dashboard selects its journey from
`run_config.json` → `capture.intent`. `dataset_mode` describes dataset content;
it does not choose between the calibration and object-dataset workflows.

## Scope of supporting pages

| Page | Scope | Workflow handoff |
| --- | --- | --- |
| Dashboard | Acquisition status, managed monitor/controller lifecycle, background work, storage, and run evidence; no physical robot commands | Follow the run-intent journey back to the required workflow step |
| Devices | Reusable sensor aliases/mounting/orientation defaults, read-only discovery, previews, snapshots | Workflow step 1 exclusively selects cameras and snapshots/edits run-owned settings |
| Cell View | Run-owned geometry, camera frames, trajectory, and provenance | Review capture or exported dataset evidence |
| Calibration Targets | Global reusable printable target bundles | Select the exact physical grid for calibration step 2 |
| Workpiece Catalogue | Global CAD/geometry metadata and lifecycle | Author inputs before creating a pose template |
| Pose Templates | Global immutable template authoring and run selection | Confirm placement in dataset step 2 |
| Run Folders | Contained run inventory, move/delete, and cluster archive copy/restore/delete | Choose a root before creating configuration |
| Pose Estimation | Browser-safe handoff to advertised external estimators | Requires a verified `pose_and_masks` BOP export; review dataset step 6 first |
| BOP Evaluation | Inspect-only standard-result validation | Requires that same verified visibility contract and an immutable BOP19 CSV |

Dashboard, Devices, and Workflow step 1 all fail closed when sensor discovery
cannot be loaded. An error is shown as unavailable—not as an empty lab or a
ready camera set—and Workflow setup remains unsavable until discovery succeeds.

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
reuse a prior run folder for a new physical capture. Capture binds the complete
validated run configuration and rechecks it before each camera child, before
robot `START`, after motion, and on both sides of completion validation. A
second concurrent supervisor for the run is rejected. Every attempt retains
its plan, status, report, and logs under
`capture_execution_logs/<execution_id>/`; a safe no-output retry does not erase
the earlier failure evidence.

While motion is active, robot packets are durably accumulated in
`raw_robot_ee_poses.journal.<claim_id>.jsonl`; the canonical
`raw_robot_ee_poses.json` appears only after the accepted `motion=end` packet is
committed. If the receiver or host stops abruptly, the next startup recovers a
terminal journal atomically or retains the committed prefix as
`raw_robot_ee_poses.partial.<...>.json` plus a `.recovered.jsonl` journal. Do
not delete that evidence or retry physical capture in the same run; inspect the
failed/canceled attempt and create a new run. A receiver killed before its
journal appears is likewise recovered as a retained `.recovered.json` claim and
zero-pose partial report, never as a silently reusable reservation.

### 5. Process frames and create the base BOP export

Queue the one fixed processing job:

```text
non-destructive sync → sync quality → rectification → calibrated BOP export
```

Submission fails before queueing unless `capture_execution_report.json` is a
current `capture_execution_report.v2` success with `status: succeeded`, its
configuration/plan/archive bindings agree, and its embedded
`capture_completion.v1` has `status: ok`. PoseTestBot rebuilds that completion
check against the current run configuration, process records, and raw capture
evidence both when the job is submitted and again before its first derived
command. Missing, corrupt, changed, or incomplete capture evidence therefore
cannot feed synchronization or export.

Synchronization uses the selected per-camera timestamp fields, clock-domain
rule, offset, and nearest-pose limit. It cannot be overridden by browser
defaults. Every retained current `robot_pose.v1` packet must provide
non-negative `sequence`, `sequence_delta`, and `estimated_packets_lost` fields;
the deltas must match both packet loss and stream order, and the final pose must
retain the accepted terminal `end` packet and its loss delta. Missing,
malformed, or inconsistent packet-loss/stream-closure evidence fails
synchronization, and sync quality emits an error instead of omitting that
check. One `sync_report.v4` generation binds the exact raw RGB-D, camera
sidecars, robot poses, and exact synchronization-owned outputs for every enabled
sensor; managed sync, sync-quality, and optional BlenderProc prepare/render CLI
transactions serialize per run, require that exact sensor set, and replace the
canonical synchronized root together or leave the previous generation intact.
Later `blenderproc/` and `masks/` publication and the persistent
`.posetestbot-directory-replace.lock` do not stale that fingerprint, while unexpected
root artifacts fail closed. Explicit sensor subsets are diagnostic-only.
Quality measures eligible in-motion coverage; preserved lead-in and tail
frames are not dataset failures. Rectification and export use only their fixed
run-owned roots and revalidate calibration and input hashes before writing
`bop_export_manifest.v5`.

### 6. Optionally add BOP ground truth

The base image/model export is a complete acquisition outcome. If
`bop.annotation_mode` is `pose` or `pose_and_masks`, deliberately queue the
matching optional job after base export. Pose mode adds `scene_gt.json`; the
full mode also renders masks, visible masks, and visibility information.

Inspect evaluation and the Pose Estimation handoff require the verified
`pose_and_masks` product: `bop_export_manifest.v5` must declare complete
BlenderProc annotations and `capabilities.bop19_evaluation: true`, while every
exported scene supplies matching `scene_gt.json` and `scene_gt_info.json` plus
complete `mask/` and `mask_visib/` visibility evidence. Pose-only
`scene_gt.json` is not evaluation-ready. Pose estimation itself remains in the
separate controller/consumer boundary.

## Physical controls

The Dashboard has no manual IIWA Start/End controls. Physical recording starts
only from the relevant **Workflow** capture step after current run preflight,
camera-open checks, and one fresh acknowledgement carrying both
`allow_cameras` and `allow_real_robot`. Capture cancellation stops and cleans
child processes but never sends the IIWA UDP idle-exit command.

The purpose-specific `POST /robot/commands` API remains available for explicit
commissioning or diagnostic clients; it is not the canonical browser capture
path. Its `start` operation queues real robot motion without the guided capture
recipe. Its historical `stop` operation is not a motion stop and cannot
interrupt active motion: it exits only the waiting Sunrise application and
requires a manual restart. Never issue it during repeated calibration.

See [Safety and authorization](concepts/safety.md) and [Physical
commissioning](COMMISSIONING.md).
