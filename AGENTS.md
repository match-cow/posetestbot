# PoseTestBot Agent Notes

These notes are for Codex and other coding agents working in this repository.
PoseTestBot is now acquisition-first: capture, calibration, synchronization,
optional GT/mask generation, and BOP dataset export are the repo boundary.
Downstream pose-estimator execution and BOP result conversion belong in a
separate consumer repo. The sole evaluation exception is the **Inspect** page's
run-scoped dataset-validation path: it consumes an already exported,
annotation-bearing BOP dataset plus an immutable standard BOP19 result CSV (or
a deterministic test-only GT perturbation), invokes the pinned official BOP
Toolkit, and writes derived evidence only below `processed/bop_evaluation/`.
It is not an estimator, converter, or acquisition-pipeline stage.
The separate `match-cow/posetestbot-cluster` companion may own SSH transfer,
durable SLURM orchestration, an estimator-driver registry with pinned runtimes
(FoundationPose first), and canonical BOP19 CSV generation. PoseTestBot may
expose only a loopback controller client, browser-safe proxy APIs,
cluster/archive and advertised-estimator status, immutable standard-result
import/download, and Inspect-page handoffs. Cluster credentials, estimator
code, estimator-specific conversion, remote paths, and arbitrary scheduler
arguments must never enter this repository or a browser response.

## Operating Rules

- Use `uv` for Python environment and package management.
- Run scripts as `uv run python ...`.
- Add dependencies with `uv add ...`; do not hand-edit dependency locks unless
  a tool-generated update is impossible.
- The web console's default approved run roots are the repository
  `working_data/` directory and `/mnt/working_data_ssd`. Additional
  `POSETESTBOT_WEB_RUN_ROOTS` entries append to those defaults and must retain
  the same containment checks.
- This host keeps GitHub CLI credentials in the user keyring. A failed
  `gh auth status` inside the sandbox can be a sandbox/keyring visibility false
  negative. Before reporting that GitHub authentication is invalid, rerun the
  same read-only authentication check outside the sandbox; do not ask the
  operator to log in again based only on the sandboxed result.
- Browser UI regressions should use Playwright tests. Keep Playwright in the dev
  dependency group, and install browser binaries only when explicitly requested.
- Production frontend builds and localhost-only Playwright regressions are
  standing-authorized outside the sandbox when sandbox stream-descriptor or
  loopback-socket restrictions prevent them. Keep those commands scoped to
  `bun run build` in `frontend/` and this repository's Playwright pytest files.
  This authorization does not include dependency or browser installation,
  external network services, camera or robot access, or physical capture.
- Keep `INSTALL.md` and `scripts/install.sh` current when dependency lists,
  SDK/runtime expectations, setup commands, or validation checks change.
- Treat `docs/` and `mkdocs.yml` as the source of the published technical
  documentation. Update the relevant pages in the same change whenever a
  repository boundary, architecture, operator workflow, HTTP API contract,
  run-config field, artifact, CLI, setup/runtime requirement, or other
  documented behavior changes. When the Flask route map changes, regenerate
  `docs/reference/http-api-routes.md` with
  `uv run python scripts/generate_http_api_reference.py --write` and verify it
  with the corresponding `--check` command.
- Prefer running or checking `scripts/install.sh` before adding ad hoc setup
  instructions.
- The lab KUKA iiwa is the sole robot profile. Never execute physical capture
  without explicit operator authorization and both execution safety gates.
- During repeated calibration, never send the iiwa UDP `STOP` command. It
  cannot interrupt active motion and exits the waiting calibration program,
  requiring a manual Sunrise application restart.
- Do not add blocking request handlers for long-running or hardware-touching
  work. Queue them through `posetestbot.jobs.runner.LocalJobRunner` and declare
  resources.
- Preserve raw capture data. Synchronization/export work should create derived
  artifacts, usually under `processed/`, rather than renaming or deleting the
  only copy of frames.
- PoseTestBot is a work-in-progress research test setup. When calibration input
  evidence is complete and internally valid, prefer retained results with
  prominent quality warnings over blocking solely on conservative
  production/metrology margins. Missing, corrupt, contradictory, or
  non-reproducible evidence must still fail closed. This tolerance policy never
  weakens physical safety gates, path/containment checks, raw-data preservation,
  or artifact-integrity validation.
- Keep completed status current in `docs/REWRITE_PROGRESS.md` and unfinished
  work in `docs/REWRITE_REMAINING_WORK.md`.
- A name containing `legacy` does not by itself make code removable. Keep the
  compatibility readers and entry points named in the remaining-work plan until
  that plan records a migration and sunset decision.
- Before deleting or renaming a tracked file, search production code, tests,
  docs, packaging manifests, and installer checks for references. Rebuild the
  checked-in frontend with Vite; never hand-edit or selectively retain hashed
  files below `posetestbot/web/static/ui/assets/`.

## Web Interface Design Policy

- The operator console is a desktop-first, information-dense interface for
  supervised lab work. Design and review the primary composition at
  1920 x 1080 and 100% browser zoom; use 1440 x 900 as the minimum normal
  desktop check. The persistent application sidebar, workflow step rail, and
  side-by-side configuration, preview, and evidence panes are the canonical
  experience.
- Prioritize desktop clarity and useful information density. Do not reduce,
  hide, or aggressively stack technical evidence, comparisons, provenance,
  validation results, or required controls merely to make every view resemble
  a phone layout. Do not add phone-specific navigation or touch-first
  interaction unless the operator explicitly requests it.
- Widths below the normal desktop target are best-effort fallbacks, not a
  mobile-support commitment. Navigation, dialogs, safety acknowledgements, and
  primary actions must remain reachable and must not overlap; inherently wide
  tables, matrices, timelines, canvases, and steppers may use explicit local
  scrolling. Prefer local overflow over accidental document-wide overflow, and
  never hide safety state or required actions to accommodate a narrow viewport.
- Prioritize Playwright coverage at desktop viewports. Use narrower viewports
  only for a named reachability, overflow, browser-zoom, safety-control, or
  specifically reported regression contract; mobile visual polish and feature
  parity are not release gates.
- Hover explanations may take advantage of mouse-oriented desktop use, but
  they must also be available by keyboard focus or click. Required and
  safety-critical information must never exist only inside a tooltip.
- Treat the two guided outcomes under **Workflow** as the canonical operator
  path. A supporting page must state where it feeds that path, distinguish
  reusable-library authoring from active-run mutation, and link to the next or
  returning workflow step when the handoff is not obvious.
- Name control scope precisely. In particular, distinguish browser-local
  drafts, global catalogue/library mutations, run-owned snapshots, readiness
  evidence, and physical execution authorization. A successful status request
  must not be presented as hardware readiness: use labels such as
  `configured`, `connected`, `verified`, and `ready` only for their actual
  contracts.
- Keep required instructions and disabled-action reasons visible near the
  affected control. Use `HelpTip` for supplemental definitions and technical
  context, not as the only location for prerequisites, safety state, or the
  next required action.
- Background-job submissions must say that work continues after navigation and
  provide a route to **Jobs**. Long job or evidence histories need filtering
  and bounded progressive disclosure while keeping active and failed work easy
  to find.

## Current Lab Hardware

- 3 Intel RealSense D435-class cameras.
- 1 Luxonis OAK-D Pro.
- 1 Stereolabs ZED 2i.
- KUKA LBR iiwa at `172.31.1.147:30300`.
- Lab receiver IP on the robot subnet: `172.31.1.169`.
- Normal network IP on the same interface: `10.145.8.132`.

Robot status is read-only:

```bash
uv run python scripts/robot_status.py --json
```

Plan physical capture without executing it:

```bash
uv run python scripts/create_run_config.py working_data/test_run
uv run python scripts/run_pipeline_sequence.py working_data/test_run \
  --sequence real_full_capture_validation --plan-only
```

Read-only status commands:

```bash
uv run python scripts/robot_status.py --json
uv run python scripts/sensor_status.py --json
uv run python scripts/sensor_adapters.py --json
uv run python scripts/runtime_status.py --json
```

Runtime status is acquisition-only. It checks BlenderProc for optional GT/mask
rendering and the Stereolabs ZED SDK Python module. Camera visibility remains
owned by sensor status.

## Current Architecture Boundary

Keep or extend these areas:

- `posetestbot.pipeline.capture_plan`,
  `posetestbot.pipeline.capture_plan_preflight`,
  and `posetestbot.pipeline.capture_execution`.
- `posetestbot.sensors.*` adapters, registry, status, discovery, and frame
  writer contracts.
- `posetestbot.sync.non_destructive` and `posetestbot.sync.quality`.
- `posetestbot.calibration.*` profile validation, preflight, observations,
  target import, intrinsic/rectification, frame graph, candidates, explicit
  extrinsic modes, and validation/promotion.
- `scripts/run_aruco_stage.py` and `posetestbot.aruco.coverage` as calibration
  target support.
- BlenderProc preparation/render planning for optional dataset GT/masks.
- `posetestbot.pose_templates.catalog` as the JSON-backed Workpiece Catalogue
  persistence, identity, lifecycle, and metadata portability contract.
- The remaining `posetestbot.pose_templates.*` exact slicing, immutable bundle,
  run-selection, and object-instance preparation contracts.
- `scripts/run_bop_export_stage.py` and `posetestbot.bop.writer`.
- The narrow Inspect-only `posetestbot.bop.evaluation` adapter, its official
  BOP Toolkit runtime bridge, and its run-scoped result/report APIs. It may
  import already compatible BOP19 CSVs or create deterministic test-only
  slight-offset results from GT, but must write only below
  `processed/bop_evaluation/` and must never become a pipeline stage.
- The thin `posetestbot.cluster` loopback client and `/cluster/*` web proxies
  for the separately deployed `posetestbot-cluster` companion. Result import
  must rerun the local standard BOP19 validator, bind the controller and staged
  dataset hashes, and retain immutable provenance below
  `processed/bop_evaluation/results/`.
- Flask operator APIs for jobs, capture status, hardware/sensor/runtime
  status, run config, preflight, calibration, the `/workpieces` catalogue,
  sync quality, Inspect-only BOP evaluation, and pipeline sequence submission.

Do not expand the Inspect-only exception into downstream behavior:

- No FoundationPose/MegaPose/SAM6D estimator code, runtime, stages, or direct
  SSH/SLURM wrappers in PoseTestBot. The typed external-controller client is
  the only estimator-orchestration boundary.
- No BOP19 result CSV conversion stage.
- No general evaluator bridge or evaluation pipeline stage beyond the
  run-scoped official BOP19 metrics described above.
- No legacy accuracy or metric-report export stage.

## Important Artifacts

- Raw robot pose artifact: `raw_robot_ee_poses.json`.
- Optional derived robot-pose cadence evidence:
  `processed/robot_pose_cadence_report.json`.
- Matched robot pose artifact: `match_robot_ee_poses.json`.
- Frame timestamp sidecar: `frame_metadata.jsonl`.
- Run manifest artifact: `dataset_manifest.json`.
- Run configuration artifact: `run_config.json`.
- Run preflight artifact: `run_preflight_report.json`.
- Hardware snapshot artifact: `hardware_status_report.json`.
- Capture artifacts: `capture_plan.json`,
  `capture_plan_preflight_report.json`, `capture_execution_plan.json`,
  `capture_execution_status.json`, `capture_execution_report.json`,
  and `capture_execution_logs/`.
- Derived sync report: `sync_report.json`.
- Run-level sync quality report: `sync_quality_report.json`.
- Calibration artifacts: `calibration_preflight_report.json`,
  `calibration_target.json`, `intrinsic_calibration_profiles.json`,
  attempt-level `intrinsic_comparison.json`,
  per-sensor `aruco_detections.json`, `camera_rectification_report.json`,
  `calibration_observations.json`, `calibration_candidates.json`,
  `calibration_profiles_from_observations.json`,
  `calibration_solver_report.json`, `calibration_profiles_solved.json`,
  `calibration_validation_report.json`, and promoted
  `calibration_profiles.json` (`calibration.v2`).
- Run-owned reusable-calibration selection is recorded in
  `calibration_profile_selection.json`. One or more promoted source runs may
  supply explicit per-sensor profiles. Exact single-source or deterministic
  combined
  `calibration_profiles.json` and `intrinsic_calibration_profiles.json`
  snapshots live below `processed/calibration_inputs/<bundle_sha256>/`; the
  selection manifest binds their hashes, every source bundle, and the
  per-sensor profile mapping so a later source-run change cannot alter the
  dataset run. Selection schema v1 remains loadable; multi-source provenance
  uses `calibration_profile_selection.v2`.
- Intent-level calibration attempts live under
  `processed/calibration/<attempt_id>/` and retain `request.json`,
  `progress.json`, `intrinsic_comparison.json`, `time_offset_search.json`,
  `pnp_candidates.json`, `extrinsic_candidates.json`, `ranking.json`,
  `checks.json`, `candidate_profiles.json`, the selected target bundle, and
  explicit promotion evidence.
- BlenderProc render plan artifact: `blenderproc_render_plan.json`.
- Workpiece Catalogue artifacts: global
  `object_catalog/object_catalog.json`, retained UUID-addressed assets below
  `object_catalog/objects/<uuid>/`, canonical geometry revisions and derived
  `pose_template_orientation_analysis.json` and bounded
  `pose_template_orientation_thumbnail.json` caches below each object's
  `derived/` directory, numbered manifest snapshots below
  `object_catalog/revisions/`, and deletion tombstones in the catalog JSON.
- Pose-template artifacts: global immutable
  `pose_templates/<uuid>/pose_template_bundle.json`, exact
  `pose_template_preview.json`, bounded `pose_template_thumbnail.json`,
  retained deletion tombstones and temporary cleanup trees below
  `pose_templates/.deleted/`,
  run-owned `pose_template_selection.json`, its hidden durable
  `.pose_template_selection.transaction.json` journal while replacement is in
  progress, and `object_instances.json`.
- BOP export artifacts: `bop/bop_export_manifest.json`,
  `bop/posetestbot_bop_frame_map.json`, `bop/test_targets_bop19.json`,
  `bop/models/models_info.json`, pose-template
  `bop/posetestbot_pose_template.json` and
  `bop/posetestbot_instance_map.json`, and optional
  `bop/posetestbot_coco_annotations.json`.
- Optional BOP annotation evidence:
  `processed/bop_annotations/generation_report.json`; pose mode adds
  `scene_gt.json`, while pose-plus-mask mode also adds `scene_gt_info.json`,
  `mask/`, and `mask_visib/` below each exported BOP scene.
- Inspect-only BOP evaluation artifacts: immutable imported or simulated result
  CSVs and `result.json` below
  `processed/bop_evaluation/results/<result_id>/`; immutable requests,
  `progress.json`, resolved-source and dataset-adapter evidence, official
  toolkit output, and `report.json` below
  `processed/bop_evaluation/evaluations/<evaluation_id>/`.

## Workpiece Catalogue Contracts

The persistent catalogue root is normally `working_data/object_catalog/`.
`object_catalog.v1` retains stable UUID and BOP `obj_id` identity while adding
editable `name`, `alias`, `description`, `tags`, `groups`, and `attributes`
metadata. Source CAD, canonical PLY, and optional PNG texture assets live in
each workpiece's UUID directory and are referenced by catalog-relative path,
size, and SHA-256.

Serialize every catalogue mutation across threads and processes, write an
atomic numbered revision before replacing the current manifest, and never
reuse a UUID or BOP `obj_id`. Archive is reversible. Permanent deletion is
available directly for active or archived workpieces after explicit
confirmation and only when no pose-template bundle references it. Fail closed
if any published bundle cannot be validated, serialize bundle publication with
catalogue deletion, commit the tombstone before removing assets, and retain
the tombstone. Record asset-cleanup status and errors in that tombstone; a
repeated confirmed delete of the retired UUID must safely retry pending
cleanup.

Pose-template permanent deletion is likewise available directly for active or
archived global bundles after explicit confirmation. Atomically retire the
UUID from library visibility and retain its tombstone before queueing physical
asset removal through `LocalJobRunner` with disk resources. Existing run-owned
snapshots remain independent. A repeated confirmed delete must safely retry
pending cleanup, and a tombstoned template UUID must never be reused.

Workpiece JSON export/import is metadata-only. The JSON does not embed CAD,
canonical PLY, or texture bytes, and import updates matching local UUIDs while
reporting records whose managed assets are absent as skipped. Preserve or move
the complete managed asset tree separately when binary portability is needed.
Queue CAD inspection/conversion through `LocalJobRunner`; it is CPU/disk work
and must not open cameras or command the robot. Catalogue APIs belong under
`/workpieces`; `/pose-templates` owns immutable template authoring and
selection only.

Treat metre/millimetre correction as a new canonical geometry revision. It
requires an archived workpiece, explicit confirmation/operator provenance, and
an expected revision/hash compare-and-swap. Regenerate from the retained source
at the cumulative source-to-mm scale, preserve every earlier canonical version,
and never rewrite existing pose-template or run snapshots. Stable-orientation
analysis is a reproducible cache bound to the canonical hash and implementation
revision; its compact thumbnail is a separately bounded card-read cache with
the same provenance binding. Do not record either mutable cache as an immutable
catalogue asset.

## Sensor Contracts

`posetestbot.sensors.registry` is the static single source of truth for
supported RGB-D sensor families, display names, SDK module names, capture
scripts, folder prefixes, supported resolutions, and mounting modes. It does
not open hardware. Update it first when adding or renaming a sensor adapter.

`posetestbot.sensors.frame_writer` owns shared capture output:

- legacy `rgb/` and `depth/` PNG files,
- compact `frame_metadata.jsonl` records,
- camera sidecars via `write_legacy_camera_sidecars`.

RealSense, OAK-D Pro, and ZED 2i capture scripts should write frames through
`write_legacy_rgbd_frame` or `write_aligned_rgbd_frame`.

The Devices-page operator alias is a reusable lab default in
`working_data/sensor_aliases.json`. Workflow setup snapshots and may edit the
run-owned `capture.sensors[].operator_alias` in `run_config.json`;
`display_name` remains its effective compatibility label. Later lab-default
changes must not rename an existing run. Capture planning mirrors the alias
into `capture_plan.json` and `dataset_manifest.json`; physical identity and
sensor-folder naming remain bound to sensor type and device ID.

`run_config.v3` owns the exact `capture.synchronization` contract. The only
supported mode is `timestamp_aligned`; every enabled camera records its own
timestamp evidence and is paired non-destructively with the robot pose stream.
Reject other modes, implementations, scopes, roles, group identifiers, or
trigger settings instead of silently coercing them. Preserve partial and
unmatched raw evidence on failure.

When reporting timestamp-aligned synchronization quality, do not treat
camera frames recorded before or after a pose-streamed robot motion interval
as invalid matches, and do not use `matched_frames / total_raw_frames` as a
quality metric when that denominator includes those intentional lead-in or
tail frames. They are preserved raw context, not synchronization failures.
Report useful capture-motion evidence instead: eligible in-motion frames,
matched eligible frames and coverage, missing/fallback timestamps,
nearest-pose-threshold rejections, mean/maximum pose delta against the allowed
limit, pose-packet loss, and any unexplained in-motion exclusion. Mention
pre/post-motion frame counts only when diagnosing capture lifecycle or when
the operator explicitly asks for them; never present them as a dataset-quality
caveat.

## Pipeline Sequences

Current acquisition sequences include:

- `real_full_capture_validation`
- `sync_aruco`
- `sync_aruco_calibration_observations`
- `sync_aruco_calibration_candidates`
- `sync_aruco_calibration_solver`
- `sync_aruco_calibration_validation`
- `sync_to_bop_dry_run`
- `sync_to_bop_calibrated_dry_run`
- `capture_to_bop_dataset_dry_run`
- `aruco_grid_full_calibration`
- `calibrated_capture_to_bop_dataset_dry_run`

Keep `sync_quality` immediately after `sync_run` in reusable sequences unless
there is a clear operator-facing reason to bypass that gate.

## Rewrite Gates

The acquisition-only rewrite gates are:

- `rewrite_full_capture.v1`
- `rewrite_calibration_validation.v1`
- `rewrite_bop_export_readiness.v1`

Run them with:

```bash
uv run python scripts/run_rewrite_gate.py <run> --gate rewrite_full_capture.v1 --write
uv run python scripts/run_rewrite_status.py <run> --write
```

## Validation

Use `uv` for tests:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
git diff --check
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -m playwright \
  tests/test_web_console_playwright.py tests/test_web_preview_playwright.py
UV_CACHE_DIR=/tmp/uv-cache uv run playwright install chromium  # only if browser binaries are missing
```

When published documentation is affected, also run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m mkdocs build --strict
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_github_pages.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -m playwright \
  tests/test_github_pages_playwright.py
```

The default pytest selection excludes the explicitly marked Playwright modules
so a normal `uv sync --all-groups` checkout does not require optional Chromium.
Keep each test tied to a distinct production contract, public boundary, or
failure mode; consolidate cases whose setup and assertions are strictly
subsumed by stronger coverage. A browser screenshot is regression evidence only
when it has a golden/pixel comparison or meaningful UI assertions—successfully
writing an image and checking its dimensions is not sufficient.
