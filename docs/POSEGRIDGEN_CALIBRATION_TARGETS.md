# PoseGridGen Calibration Targets

PoseTestBot uses the pinned PoseGridGen source checkout to generate printable
ArUco targets. This workflow creates acquisition/calibration inputs only; it
does not command the robot or open cameras.

## Enable generation

PoseGridGen generation requires Python 3.12 and the exact clean submodule at
`third_party/PoseGridGen`:

```bash
bash scripts/install.sh --with-posegridgen
bash scripts/install.sh --check-only --with-posegridgen
uv run posetestbot-web
```

The required revision is
`9e6975901fe096bf65f7b7b599d7b82461d2e67c`. A missing, dirty, mismatched, or
wheel-only checkout disables generation. Existing `calibration_target.v3`
artifacts remain readable in that state. The status and
the direct `/calibration-targets` route explain the concrete failure. Navigation,
saved-bundle browsing, downloads, and run selection remain available even when
generation is disabled.

## Generate and select

Open **Calibration Targets** in the operator console:

1. Choose the ArUco dictionary, rows/columns, marker size, gap, paper (DIN A1
   through A6, Letter, or Legal), orientation, annotations, and independent X/Y
   print compensation. A 100 mm ruler cannot fit within A6 portrait's printable
   width, so disable the ruler or use landscape orientation for that case.
2. Optionally attach a board-to-base pose and use **Fit to page** when needed.
3. Inspect the debounced PNG preview, enter a display name, and queue
   **Generate bundle**.
4. Download and inspect the source JSON, canonical target JSON, and printable
   PDF. Generation does not change the active run.
5. Choose a configured run and select the bundle. The run's homogeneous camera
   mounting group determines the physical board mounting: static cameras use
   `robot_flange`, while eye-in-hand cameras use `template_base`. Then declare
   the supported placement policy for that frame. The guided static path uses
   `unknown`; the fixed-target path may use `unknown`,
   `template_base_identity`, or `posegridgen_board_to_base`.

The simplified **Workflow → Calibration** screen can also select any saved
bundle directly for an immutable calculation attempt. Camera mounting is saved
in step 1, target mounting is saved in step 2, and step 5 derives rather than
overrides the resulting mode. Eye-in-hand estimates a target stationary
relative to `template_base`. Static-camera world calibration keeps the cameras
fixed while the target moves rigidly with `robot_flange`; its internal
`eye_to_hand` equation jointly estimates the primary
`camera -> template_base` transform and the nuisance/support
`aruco_grid -> robot_flange` transform. Here `template_base` is the physical
`/PoseTestBot/PoseTemplateBase` used for object capture. The separate
`/PoseTestBot/TemplateBase` may parent the controller's taught calibration
motion waypoints, but it is not the pose-stream reference or the result frame.
A measured flange-to-grid transform is therefore not required, but the
physical attachment must remain rigid for the entire recording. The moving
grid provides calibration observations; static cameras are not used for robot
hand tracking after calibration. This does not require PoseGridGen to be
available and does not initiate physical capture.

After promotion, **Cell View** keeps a static-camera calibration scene in the
same `PoseTemplateBase` frame used by the printed object template. It renders
each fixed `camera -> template_base` profile directly in that frame and derives
the moving board path by composing the promoted
`aruco_grid -> robot_flange` attachment with every exact recorded
`robot_flange -> template_base` pose. It does not re-anchor the scene or its
display ground plane to the first moving target pose. Fixed-target,
eye-in-hand calibration may still use a target-front presentation because that
target is stationary in `template_base`. If the web frontend is updated before
the serving process is restarted, Cell View preserves the older flange-path
preview and shows a restart notice until the backend supplies the target-path
metadata; this mixed-version state must not crash the view.

`posegridgen_board_to_base` is available only when the source records that
pose. Selection cross-checks PoseGridGen's matrix, translation, and quaternion,
converts metres to millimetres and XYZW to WXYZ, and explicitly treats the base
as `template_base`.

## Artifact contract

Each immutable library entry is stored at:

```text
working_data/calibration_targets/<opaque-uuid>/
  calibration_target_bundle.json
  posegridgen_source.json
  calibration_target.json
  calibration_target.pdf
```

`calibration_target_bundle.v2` records the UUID, display name, creation time,
pinned generator revision, configuration/geometry hashes, and fixed file paths,
media types, sizes, and SHA-256 values. Generation stages every file and
promotes the complete directory atomically. Confirmed deletion is allowed only
for an inactive library bundle. The preceding generator revision
`ad152e369e8d2746d0cf66cb1455f2371b0ec0f0` remains compatible as provenance
for current physical-geometry bundles. This does not make retired target or
bundle schemas readable; existing bundles are never regenerated in place.

Selection copies the unchanged bundle to
`<run>/calibration_targets/<target_id>/`, writes the placement-aware root
`<run>/calibration_target.json`, and adds calibration-target hash/provenance
fields to the run config. New guided selections also record
`placement.mounting_frame` as `robot_flange` or `template_base`. The bundle,
root target, run config, and dataset manifest are promoted together with
rollback on failure. Current readers reject selections without
`mounting_frame`; neither known nor unknown placement implies a physical frame.
Select the target again with an explicit mounting frame before readiness,
attempt creation, or promotion. If retained raw evidence prevents replacement,
create a fresh run rather than weakening the current contract.

Intent-level calculation snapshots the bundle below
`<run>/processed/calibration/<attempt_id>/target_bundle/`. Prior attempts and
raw capture data are never replaced. Only explicit recommendation acceptance
copies the selected evidence and bundle into canonical run artifacts.

Calibration calculation also binds its selected target snapshot to the
per-camera Auto time-alignment evidence; neither target geometry nor raw
timestamps are rewritten. The operator behavior and dataset handoff are
documented in [OPERATOR_WORKFLOWS.md](OPERATOR_WORKFLOWS.md). Physical
deployment and acceptance remain in [COMMISSIONING.md](COMMISSIONING.md).

`calibration_target.v3` makes the physical `corners_mm` for every marker
authoritative. Its frame is `aruco_grid`, with origin
`physical_outer_board_top_left`, +X right, +Y down, and +Z into the page.
`print_compensation.application` is `pdf_only`. PoseGridGen's retained source
features and PDF contain printer precompensation; target import removes that
X/Y scaling exactly once. Consumers use the physical points in a generic
OpenCV `Board`, without applying compensation.

For example, seven columns and five rows of 45 mm markers with 10 mm gaps
produce a physical 375 × 265 mm board. At 100.5% X print compensation the PDF
spans 376.875 × 265 mm, while calibration still uses 375 × 265 mm and square
45 mm markers. Measure both outer marker spans before use: compensation is a
correction for printer shrinkage, not a change to physical marker size.

The old `calibration_target.v2` and `calibration_target_bundle.v1` contracts
encoded PDF dimensions as physical geometry and are retired. Current readers
reject them rather than silently changing hashed evidence. Generate a new
bundle from the retained configuration and select it in a fresh run; preserve
old bundles, attempts, and profiles for comparison. A recorded-data reanalysis
must retain the exact original image/robot bytes and capture UUID, record the
source hashes and measured-size evidence, and write a new attempt and corrected
profile collection in its separate run directory. No new capture is required.

ArUco detection refines marker corners with OpenCV `SUBPIX` before intrinsic
and PnP comparison. Detection evidence records the refinement settings; robust
PnP support checks remain in force.

## Recorded shared-grid refinement

For a completed multi-camera eye-in-hand attempt with an estimated stationary
grid, the advanced CLI can fit every camera and one shared grid pose directly
to the retained subpixel corners. Intrinsics, distortion, and the attempt's
timing offsets stay fixed. This avoids treating an average of independently
estimated grid poses as a jointly optimized grid.

```bash
uv run python scripts/refine_calibration_attempt.py working_data/calibration_run \
  --attempt-id <attempt_id>
```

Review the new retained report under
`processed/calibration/<attempt_id>/reprojection_refinement/<refinement_id>/report.json`.
It records the source hashes, common robust corner support, solver policy,
bounded optimization, per-camera reprojection, reserved intrinsic views, and
three motion-disjoint folds. Each fold refits its hand-eye seed using only the
training motions. Settled frames and reserved intrinsic views are excluded
from the final transform fit. Reserved views are matched by their preparation
source-frame identity, since timing correction may renumber synchronized frames.
At least nine shared motion groups are required.
Every camera must improve in the motion-disjoint mean and the full-recording
median against both the shared and individual seed grid references. Incomplete
or failed evidence cannot be promoted.

Explicitly accept a passing reviewed refinement with:

```bash
uv run python scripts/refine_calibration_attempt.py working_data/calibration_run \
  --attempt-id <attempt_id> --refinement-id <refinement_id> \
  --promote --operator "<reviewer>"
```

Promotion repeats the normal attempt gates, checks every input/report hash,
and reproduces the refinement and audit before the atomic canonical-profile
transaction. Seed rankings and candidate profiles remain unchanged; refined
profiles have distinct identities, a shared companion pose, and explicit
refinement provenance. Their translation/rotation quality fields retain the
seed's leave-one-pose-out results and say so in the quality notes. The refined
optical audit is recorded separately in profile metadata. The web promotion
action continues to accept the ordinary seed bundle; refinement acceptance is
currently CLI-only.

To explicitly replace an already promoted refinement with a different reviewed
passing report, add `--replace-refinement`. The operation first retains the
previous promotion request/status and complete canonical profile/configuration
selection under `promotion_history/<snapshot_id>/`. The new request binds every
retained file's hash, and promotion checks those hashes before replacing the
canonical selection. Ordinary repeated promotion remains rejected.

These checks validate recorded image alignment, not independent absolute pose
accuracy. Timing, intrinsic selection, and corner-support masks are determined
before the motion split. A measured target and a separate physical object-pose
check are still needed to establish absolute accuracy.

## Reselection and preflight

Selecting the same target, placement policy, and physical mounting frame again
is idempotent. A different target, placement, or mounting frame is rejected as
soon as capture status/logs, raw RGB-D/metadata, raw robot poses, a calibration
attempt, promoted profiles, rectification, or BOP output
exists. Those artifacts already encode which target moved relative to the
robot, so relabelling them would change the hand-eye equation. The API returns
the concrete blocker paths; preserve the evidence and create a new run.

Preflight verifies bundle containment, absence of symlinks, file hashes,
canonical target agreement, compatible generator provenance, run-config
hashes, one homogeneous calibration camera group, and agreement between camera
mounting, target mounting, and solver interpretation. Target selection changes
`run_config.json`, so older run-preflight evidence becomes stale automatically.

## Run-owned validation

The `calibration_target_import` stage validates the current run-owned bundle,
root target, hashes, and selection contract. It does not convert older target
formats or infer a target from loose source files:

```bash
uv run python scripts/run_calibration_target_import.py working_data/example_run
```

## API and jobs

The scoped API surface is:

- `GET /calibration-targets/status`
- `GET /calibration-targets/capabilities`
- `POST /calibration-targets/fit`
- `POST /calibration-targets/preview`
- `GET /calibration-targets/bundles`
- `POST /calibration-targets/generate`
- `DELETE /calibration-targets/bundles/<target_id>`
- `POST /calibration-targets/bundles/<target_id>/select`
- `GET /calibration-targets/bundles/<target_id>/download/<source|target|pdf>`

The intent-level calculation façade consumes those saved bundles through:

- `GET /calibration/setup?run_root=...`
- `GET /calibration/attempts?run_root=...`
- `POST /calibration/attempts`
- `GET /calibration/attempts/<attempt_id>?run_root=...`
- `POST /calibration/attempts/<attempt_id>/promote`

Attempt creation records stable sensor keys and queues one `cpu`/`disk_io`
parent job. Promotion is a separate queued transaction and requires passing
recommendations or explicit passing candidate IDs. Failed alternative solver
combinations remain diagnostic evidence and do not invalidate a selected
passing combination. The 15-view target and image-centroid coverage targets
are quality recommendations, not extrinsic-solver admission gates. A smaller
capture or limited image coverage retains its solution when motion observability,
inlier support, and geometric validation pass. The review page displays selected
candidate warnings and their measured values alongside the recommendations;
promoted profiles retain them in `metadata.quality_warnings`. Missing or malformed
coverage input still fails. These extrinsic recommendations do not change the
separate validation required to replace a factory lens model with fitted intrinsics.

Promotion rolls back handled errors, including keyboard interruption.
If the filesystem also prevents rollback, the error identifies a retained
`.calibration-promotion-backup-*` directory. Preserve that directory and repair
the filesystem problem before recovering the originals; do not delete it as
temporary output.

Multi-camera attempts retain the common algorithm bundle
whose independently estimated companion transform is most suitable under the
recorded ranking policy. Pairwise companion disagreement above 10 mm or 5° is
promotable with a preserved quality warning; disagreement above 20 mm or 10°
is contradictory and blocks promotion. The doubled hard limit accounts for two
independently accepted estimates lying at opposite sides of the per-camera
10 mm / 5° residual bound. Missing, malformed, or individually failed selected
candidates still fail closed.

The attempt response includes a derived `promotion_review` under the current
retention policy. This lets an immutable attempt calculated under the former
10 mm / 5° hard cross-camera cutoff be promoted without rewriting its ranking
artifact when its recorded numeric evidence remains below the current hard
limit. Promotion revalidates both the historical record and the current policy
before writing profiles, and stores the warning evidence in each promoted
profile. The parent job has five
operator-visible phases: prepare data, estimate target poses, estimate time
alignment, compare robot-camera solutions, and validate/rank.

Request bodies are capped at 256 KiB. Generation queues `cpu` and `disk_io`;
selection queues `disk_io`. Commands use fixed argument arrays and appear in
the existing Jobs page. Deletion requires `confirm: true`, atomically removes
the library bundle, and rejects the target active for the selected run. No
generic filesystem download endpoint is provided.
