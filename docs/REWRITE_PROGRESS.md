# Rewrite Progress

Last updated: 2026-08-18

PoseTestBot is acquisition-first. Its repository boundary is real capture,
calibration, non-destructive synchronization, optional GT/mask generation,
pose-template provenance, and BOP dataset export. Downstream estimators and
result conversion remain excluded. The sole evaluator exception is the
Inspect-only, run-scoped official BOP19 dataset-validation path described
below; it consumes completed exports and is not a pipeline stage.

## Current State

The code rewrite is implemented across:

- real-only capture planning, preflight, supervised execution, and raw-evidence
  protection;
- RealSense, OAK-D Pro, and ZED 2i sensor adapters and status contracts, with
  run-scoped aliases and enable/disable selection that preserve disabled camera
  metadata;
- timestamp-only transactional synchronization and sync-quality reporting,
  including strict hash-bound reuse of each selected calibration profile's
  saved timing policy;
- PoseGridGen targets, including immutable-library card previews rendered from
  stored marker geometry, and attempt-scoped factory-vs-OpenCV intrinsic
  evidence, whole-board PnP support gates, global-sensor-time RealSense
  synchronization, calibration-attempt-only constant effective-latency search,
  common-bundle multi-camera extrinsic ranking, and explicit promotion;
- the dedicated Workpiece Catalogue page and `/workpieces` API, backed by the
  existing JSON/UUID asset store with editable classification, previews,
  guarded lifecycle, revisioned metre/mm correction, metadata portability, and
  pose-template integration;
- the updated PoseTemplateCreator stable-orientation workflow, with bounded
  isometric previews, exact planar layout/fit validation, immutable printable
  templates sourced from active workpieces, preview-rich run selection,
  per-instance GT, and Cell provenance;
- BlenderProc 2.8.0 preparation/render identity checks and transactional,
  selectable pose-only or pose-plus-mask ground truth, and
  annotation-capability-explicit BOP v5 export;
- Inspect-only standard-result registration, deterministic GT-derived test
  fixtures, and official BOP19 metric evaluation below
  `processed/bop_evaluation/`; and
- the packaged React operator console, managed jobs/services, and scoped Flask
  APIs.

## 2026-08-18 Technical Documentation and HTTP Reference

The earlier single-page public guide was replaced by a Material for MkDocs
technical site built from the authoritative `docs/` tree. The new hierarchy
documents process and credential boundaries, run/artifact lineage, safety and
execution authorization, `run_config.v3`, canonical artifacts, CLI entry
points, and domain-specific HTTP behavior. It keeps all existing operator,
calibration, workpiece, template, IIWA, validation, and project-status guides
reachable through persistent multi-page navigation and full-text search.

`scripts/generate_http_api_reference.py` now renders every non-static Flask
rule into `docs/reference/http-api-routes.md`. Its check mode prevents route-map
changes from silently leaving the published reference stale. Hand-authored API
pages explain trust/containment, JSON and multipart conventions, job semantics,
errors, physical capture gates, calibration state, catalogue lifecycle, BOP
evaluation, and the browser-safe external-controller boundary.

The Pages workflow builds with the locked docs-only uv dependency group and
strict MkDocs validation before uploading generated `site/`. The generated
directory is ignored rather than maintained by hand. Navigation deliberately
uses ordinary static document links, with browser coverage for desktop sidebar,
narrow-width drawer, history, direct routes, and client-side search. No camera
or robot command is involved in documentation validation.

Validation passed all 719 default non-Playwright tests (including five focused
documentation source/build contracts), three dedicated Chromium navigation and
search regressions, a strict MkDocs build, the installer's docs-only check path,
Ruff on the generator/tests, and `git diff --check`. The final 1920×1080 site
was visually reviewed with the full technical navigation, content table, and
table of contents visible. No camera or robot command was executed.

## 2026-08-18 Complete Static-Calibration Change Review

A complete cross-layer review covered the corrected parent-frame X/Z Sunrise
route and its commissioning docs, mounting-aware field-coverage thresholds,
duplicate-ID multi-grid consensus, v5 recorded-timing fallback, calibration
promotion, RealSense duplicate-color handling, run-folder cleanup, managed
console restart controls, and the rebuilt operator UI. Retained evidence from
static attempts `d70cfd359c2e4661a336e4cdaadcabe8` and
`8e88c57af23444de8d531672bce9b93f` was re-read without mutation; both still
satisfy their recorded current contracts and remain deliberately unpromoted.

The review found and fixed three concrete consistency defects. Promotion of a
v5 degraded timing fallback now recalculates its saved leave-one-motion-out
residual improvements, method summaries, significance correction, statuses,
candidate/synchronizer sign relationship, and authoritative quality-report
warning bindings instead of trusting warning labels. RealSense duplicate-color
skips now keep preview Q/Escape handling reachable even if a stream repeatedly
reuses the same aligned color frame. Operator documentation now consistently
describes the parent `PoseTemplateBase` X/Z route, mode-specific coverage, v5
fallback limits, and the distinct failed-discovery versus successful-replay
attempt IDs.

Validation passed 714 default non-Playwright tests, all 20 desktop Playwright
tests, Ruff on the affected Python modules and tests, frontend TypeScript build
and ESLint, the production Vite build, the non-mutating installer check, real
retained-artifact promotion revalidation, and `git diff --check`. The build
retains Vite's advisory large-chunk warning for the existing PLY loader. No
camera or robot command was executed, no raw evidence was changed, and no
calibration result was promoted.

## 2026-08-17 Managed Console Restart Controls

The persistent top-right console header now exposes one explicit **Restart**
dialog with Frontend, Backend, and Both actions. Frontend restart reloads only
the current browser tab. Backend restart derives its fixed unit name from the
serving process's own Linux systemd control group; its strict JSON endpoint
cannot accept a browser-provided unit name, checks that the exact user-systemd
unit is loaded, active, and owns the serving process, and schedules restart
after returning HTTP 202. An optional environment override supports unusual
managed layouts but is not required for the standard service. The browser
waits for the backend instance identity to change before reporting
reconnection or reloading for the combined action.

Backend actions require a visible interruption acknowledgement and report the
current count of active process-owned local jobs. The dialog explains that
captures, previews, and local jobs stop during graceful backend shutdown while
durable remote cluster jobs remain independent. The deployed web-service
process cgroup owns the fixed service-unit identity; unmanaged Flask/Vite
development sessions fail closed for backend restart while retaining frontend
reload.

Live verification covered the already-installed standard user service, whose
unit intentionally has no restart-specific environment setting. After a
zero-active-job controlled restart, the backend derived
`posetestbot-web.service`, matched its new systemd `MainPID`, and advertised
managed restart as available with no blockers.

Validation passed 17 focused Flask/runtime/UI tests, frontend type checking and
lint, the production Vite build, the focused 1920 × 1080 Playwright regression,
Ruff on the affected Python files, and `git diff --check`. No camera or robot
command was executed.

## 2026-08-17 Research-Stage Static Calibration Tolerance

Static-camera (`eye_to_hand`) extrinsic attempts now use research-stage field
coverage minima of 15% image width, 20% image height, and 3% supported
centroid-hull area, with the existing five-view tail support. Robot-mounted
camera attempts retain the established 45% / 35% / 10% minima. The measured
19.6% / 26.4% / 4.72% coverage from static attempt
`980574a9dd524ee2b1997dea6b87c0d3` therefore clears the revised static gate
while remaining explicit evidence for later review.

Current Auto time alignment still applies an inferred offset only when its
motion-disjoint and leave-one-motion-out evidence is strong and consistent.
When a completed search is weak, ambiguous, inconsistent, or boundary-limited,
it now keeps the recorded 0 ms pairing, converts the failed identification
checks into prominent warnings, and continues to robot-camera geometry
validation. Missing, corrupt, or unevaluable timestamp/robot-pose input remains
blocking. This is revision
`constant_latency_nearest_pose_motion_lomo_warn_keep_zero.v5`; prior attempts
remain immutable inspection evidence.

Fresh derived-only replay `d70cfd359c2e4661a336e4cdaadcabe8` on the same
preserved recording completed with one passing `IPPE + Park` recommendation.
It retained 0 ms after recording the weak +30 ms candidate as a degraded
warning, accepted all 539 observations, and reported 1.492 mm / 0.818° mean
held-out residual with 0.768 px mean reprojection error. Its measured 19.60% /
26.42% / 4.72% field coverage passed the recorded 15% / 20% / 3% static
thresholds. The result remains unpromoted and awaits operator review.

Static replay `4fb4e50240ec425682ae6e22fe0b869a` exposed a separate
target-detection defect: every image also contained other printed grids that
reused marker IDs. The old whole-image inlier ratio rejected all 743 frames
even though one coherent target instance retained 12–20 markers with
subpixel-level fit. Planar PnP now recognizes duplicate-ID multi-grid clutter,
requires the retained instance to span at least 8 markers, 3 rows, and 3
columns, measures pose quality and centroid coverage only on that consensus,
and retains the discarded correspondences as a prominent warning. Weak target
fragments remain blocking. Optional OpenCV intrinsic comparison remains
non-blocking when a compatible factory lens model is selected.

The new immutable fixed-zero replay
`8e88c57af23444de8d531672bce9b93f` completed with a passing
`IPPE + Shah` recommendation: 743/743 accepted observations, 0.859 px mean
retained-grid reprojection error, 0.940 mm / 0.672° mean held-out residual, and
25.03% / 22.59% / 5.39% supported field coverage. It explicitly recorded and
applied a 0 ms robot-pose offset. All 743 views record duplicate-grid filtering
and 49,905 ignored off-instance corner correspondences as reviewable warning
evidence. The result remains unpromoted and awaits operator review.

Validation passed all 700 default non-Playwright tests, five focused desktop
Playwright calibration regressions, the production frontend build, Ruff on the
affected Python modules/tests, promotion revalidation of the real fallback
artifact, and `git diff --check`. No camera or robot command was executed.

The duplicate-grid follow-up additionally passed 48 focused calibration tests,
two desktop Playwright regressions, the production frontend build, Ruff on the
affected Python files, and `git diff --check`. Its replay used preserved raw
evidence only; no camera or robot command was executed.

## 2026-08-17 RealSense Repeated-Frame Capture Hardening

RealSense alignment may emit a new depth frameset while reusing the preceding
color frame. Because the color timestamp names the authoritative aligned RGB-D
pair, that SDK behavior previously reached the no-overwrite guard and aborted
an otherwise active supervised capture. The adapter now skips repeated color
frame identities and timestamp-derived filename stems, waits for the next
unique color observation, and reports the skipped count in its capture summary.
The writer still refuses any genuine overwrite of pre-existing raw evidence.

The final calibration-analysis view now derives the recorded physical
arrangement from the run-owned camera configuration, independently of whether
captured RGB-D, timestamp, and robot-pose evidence is data-ready. A failed or
incomplete capture therefore reports its missing evidence without falsely
claiming that Workflow step 1 has no configured camera group.

Validation passed all 698 default non-Playwright tests, the focused desktop
Playwright camera-arrangement regression, the production frontend build, and
`git diff --check`. No camera or robot command was executed.

## 2026-08-17 Corrected Bottom-Middle X/Z Calibration Grid

The single-frame static-camera candidate now implements
`CalibrationStaticBottomMiddle` as the name specifies: it is the bottom-center
point of the 3 × 3 grid and the lowest permitted flange Z in the
`PoseTemplateBase` coordinate system. The previous candidate incorrectly used
the child frame's local axes for an X/Y grid plus a depth phase. That
interpretation could move in the wrong physical directions and did not encode
the required parent-frame minimum-Z contract.

The application now resolves `PoseTemplateBase` explicitly and uses its X/Z
axes for every generated translation. X supplies -65/0/+65 mm columns, Y stays
fixed, and bottom-relative Z supplies 0/+50/+100 mm rows. Starting at the taught
bottom-middle point, the ordered grid route visits every other grid point once
and ends at the generated center before each A/B/C
`center → minus → plus → center` orientation sweep. The separate depth phase
and redundant final PTP confirmation are removed. Endpoint guards reject any
negative bottom-relative Z; relative legs remain below 100 mm and all grid
points remain within 125 mm of the taught point.

These corrected paths remain an uncommissioned source candidate and require
Workbench simulation plus supervised T1 recommissioning. Repository work did
not command the robot or open a camera.

Validation passed all 27 IIWA source/teaching contracts and the complete
default non-hardware suite at 696 passed / 18 deselected, plus Ruff on the
affected source-contract tests and `git diff --check`.

## 2026-08-17 Durable User-Service Worker Resolution

Queued `uv run` workers now resolve the supported per-user uv installation even
when a systemd user manager supplies only its default system PATH. The web
service example also requires the absolute uv binary directory on PATH, and the
installer rejects a packaged service example that loses that requirement. This
prevents host or user-service restarts from disabling the Dashboard UGREEN
monitor and other managed jobs before their worker process starts.

## 2026-08-06 Cluster Operations Discoverability and Durable Web Service

Cluster operations are now visible before the operator reaches downstream pose
estimation. The Dashboard places the fixed companion service control in its
first readiness row, fully inside the canonical 1920×1080 viewport, and gives
cluster archive/restore the primary handoff. **Run folders** renders its
**Cluster storage** panel directly below the page header—even while local
inventory is loading or unavailable—and **Pose Estimation** links back to it.
Storage readiness and archive transfer remain independent of estimator
qualification.

The deployment examples now cover both the companion and the PoseTestBot web
console with enabled user-systemd operation and `Restart=always`. The install
guide documents intentional rebuild/restart deployment boundaries instead of a
source watcher that could interrupt process-owned jobs. The installer verifies
that both service examples ship with the bundled console.

Validation completed with 695 default tests, 18 desktop Playwright tests,
frontend typecheck and lint, the production Vite build, installer check-only,
shell syntax, and diff checks.

## 2026-08-06 Independent Cluster Archive and Estimator Registry

The separate `posetestbot-cluster` companion now keeps one credential and
durability boundary while splitting its capabilities internally. Archive copy,
restore, and prepare-move have an independent server policy/readiness domain
and no longer depend on FoundationPose qualification. Estimator execution uses
a closed driver registry, an exact estimator-neutral runtime manifest, and a
generic v2 job API; FoundationPose is the first driver and its v1 API/runtime
manifest remain compatible. New methods can be added only as trusted companion
drivers with pinned qualification and provenance validation—not as browser
commands or PoseTestBot stages.

PoseTestBot's loopback client/proxy now discovers the companion's estimator
catalogue, selects only an advertised estimator ID and qualified resource
profile, and retains browser-safe generic estimator/runtime provenance. The
Pose Estimation page exposes that selection. Run folders uses the independent
storage domain, so verified archive/restore remains available while estimation
is disabled or unqualified. The Dashboard reports storage and estimator
readiness separately, and its fixed service start/stop warning covers both
archive and estimator work. Companion policy stays in the shared mode-0600
`.env`; PoseTestBot reads only loopback host/port/token.

Validation completed with 50 companion tests, 695 default PoseTestBot tests,
17 desktop Playwright tests, Ruff and frontend lint checks, both Python package
builds, the production Vite build, and the installer check-only path.

All FoundationPose deployment instructions are now consolidated in the
companion's canonical `docs/FOUNDATIONPOSE_CLUSTER_SETUP.md`; the superseded
LUIS-only runbook and duplicated install steps were removed.

## 2026-08-06 Active Run Selection Clarity

The operator console now treats the top bar as persistent read-only run context
instead of a dropdown that attempted to hold every acquisition. It shows the
saved run display name, exact active folder, and configuration state, with one
clear **Change** route to **Run folders**. The run index exposes the saved
display name separately from the filesystem leaf so the two scopes remain
visible without implying that metadata renames storage.

**Run folders** now leads with the active acquisition, explains the one-folder-
per-acquisition and multi-object-per-template boundary, provides the new sibling
folder form, and offers a bounded searchable, root-filterable, sortable chooser
for existing runs before its detailed storage-management evidence. Existing
move/delete identity checks and active-run guards remain unchanged. Workflow's
field is now labeled **Run display name (optional)** and states that leaving it
blank adopts the folder name.

The detailed **Storage inventory and actions** table has an independent search
over display names, folders, paths, objects, sensors, and evidence, plus a
bounded 720 px scrolling region with a sticky column header. The global active-
run block and its **Change run** control now share an explicit 56 px height and
are vertically centered inside the 72 px desktop top bar; Playwright asserts
equal control heights and equal top/bottom spacing at the normal desktop
viewport.

## 2026-08-06 Superseded Bottom-Anchor Interpretation

This dated implementation introduced
`/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle` but incorrectly
treated the taught child frame's local Z as a depth anchor for an X/Y grid.
That route is superseded by the corrected 2026-08-17 parent-frame X/Z contract
above and must not be commissioned.

## 2026-08-04 Research Simplification

The acquisition contract now accepts only `run_config.v3` with exact
`timestamp_aligned` synchronization. Hardware-trigger configuration,
qualification, continuous metadata-watchdog, complete-view grouping, BOP
frame-set bindings, related scripts/APIs/UI, and their dedicated tests were
removed. Raw camera frames and timestamp evidence remain preserved; processing
produces independent per-camera frame-to-pose matches and never claims
simultaneous cross-camera exposure.

Current data formats are `calibration.v2`, `calibration_target.v2`, and
`sync_report.v3`. The v1/v2 run-config and sync readers, calibration-v1
migration, ArUcoGridGen fallback importer, standalone single-folder sync and
object-instance entry points, `web_interface.py`, and compatibility catalogue
routes were retired. Historical calibration attempts remain readable for
scientific inspection, but a retired request/timing revision cannot be rerun or
promoted.

Calibration attempt creation now accepts only the run, mode, selected sensors,
target, and timing policy. Intrinsic/solver choices and thresholds are
server-owned. Automatic timing fails closed for weak, ambiguous, inconsistent,
boundary, or unevaluable evidence; zero milliseconds passes only when the
search identifies it, while `fixed_zero` remains a separate explicit policy.
Candidate ordering uses one deterministic six-decimal ranking tuple without
tolerance bands.

The external cluster boundary supports the original
`POSETESTBOT_CLUSTER_ENABLED`/URL/token settings and a server-only mode-0600
companion-env-file source. PoseTestBot retains browser-safe status, fixed
user-service lifecycle control, pose-job submission/logs/cancellation,
immutable result import/download, and archive copy/restore; idempotency keys are
server-generated. Imported controller evidence is validated and rewritten as a
compact allowlisted provenance record; raw scheduler accounting, failure text,
copy details, remote paths, credentials, and unknown fields are not retained.
Remote-source deletion and two-phase move preparation are not exposed.

Final non-hardware validation collected 702 tests: 686 default contracts and
16 desktop Playwright journeys passed, with 34,997 Python test-source lines.
Ruff, frontend typecheck/lint, the production Vite build, installer check-only,
shell syntax, package build, and diff checks passed. Read-only evidence checks
remain ready at 11/11 for `working_data/test20260726_BOPv5` and 3/3 for
`/mnt/working_data_ssd/calib00_test20260724`. No physical hardware or real
cluster controller was exercised.

## 2026-08-05 Managed Cluster Controller Lifecycle

The Dashboard now distinguishes the fixed local controller service state from
authenticated loopback connectivity and full cluster readiness. An explicitly
configured user-systemd unit can be started or stopped from the browser; both
actions require a literal confirmation, are queued through `LocalJobRunner` as
global work with a dedicated resource, and execute only a server-built
`systemctl --user --no-block` argument vector. A caller cannot choose the unit,
command, environment, working directory, or scheduler arguments. Stop presents
an interruption/reconciliation warning and lifecycle progress remains visible
in Jobs.

`POSETESTBOT_CLUSTER_ENV_FILE` can reuse the companion's absolute mode-0600
`.env` at web-process startup. Only the shared token and loopback host/port are
selected; the path and all SSH/cluster settings remain outside browser
responses. The existing explicit variables take precedence, and browser-side
credential configuration remains forbidden. The checked-in user-service
example and installer smoke checks keep this optional deployment contract
visible without installing the companion or its estimator runtime in
PoseTestBot.

Validation for this change passed 692 default pytest contracts and all 17
desktop Playwright journeys, plus Ruff, frontend typecheck/lint, the production
Vite build, package build, installer check-only, shell syntax, and diff checks.
The systemd adapter itself was exercised with fixed-command/status fixtures. One
credential-redacted read-only check against the existing running user service
verified shared-env authentication and reported both LUIS hosts/quota ready.
It also confirmed the current companion remains read-only, has no configured
runtime manifest, exposes no enabled GPU profile, and therefore keeps pose
estimation disabled. No real service or cluster job was mutated, and no camera
or robot was opened or commanded.

## 2026-08-04 Descriptive IIWA Applications and Single-Frame Static Calibration

The repository Sunrise sources now use role-specific Java names:
`PoseTestBotFullCaptureApplication`,
`PoseTestBotNineFrameCalibrationApplication`,
`PoseTestBotSingleFrameStaticCameraCalibrationApplication`, and the shared
`PoseTestBotPoseStreamTask`. The rename is repository-side only; Workbench
application/background-task metadata and any deployed controller selection
must be updated and recorded independently.

The new static-camera alternative required one additional taught frame at
`/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle`. After an accepted
START it anchored there and generated the other motions. Its original
center-relative X/Y/depth interpretation was later found not to match the
bottom-center grid meaning and is superseded by the 2026-08-17
`PoseTemplateBase` X/Z route. Before the initial PTP, the retained read-only
pose check rejects START unless the flange is already within 25 mm of the
taught point.

All motion applications now start by resolving their shared pose-stream
provider and waiting without robot motion for an accepted UDP START. The former
repository-only offline-validation Boolean and its test assumptions were
removed. This does not replace Workbench compile, offline path simulation,
frame read-back, T1 commissioning, deployment identity, or physical execution
authorization. No robot, camera, or lab service was accessed while making this
repository change.

Repository validation passed all 25 focused iiwa source-contract tests and the
complete default non-hardware suite at 1,107 passed / 66 deselected. Ruff checks
for the affected Python tests and `git diff --check` also passed. The host does
not contain the authoritative Sunrise.Workbench project/classpath, so the
five-source Workbench compile and every controller motion check remain
operator-run commissioning work.

## 2026-08-03 Run Creation and Mixed Calibration Reuse

The active-run dialog now represents the server's direct-child storage
contract explicitly: the operator chooses one approved storage root and one
run-folder name. Each acquisition therefore gets a distinct sibling folder,
while the editable run name inside Workflow remains clearly identified as
metadata. This removes the former full-path field that reopened the current
folder by default and made creating several runs under one storage root easy to
miss.

The Devices-to-run camera-settings path now has explicit persistence at both
scopes. Devices aliases use a nearby per-camera save action; mounting and
supported orientation selectors immediately write their reusable defaults.
Every write adopts the server-returned file state, preserves disconnected
records, and bypasses stale HTTP caches. Failed immediate saves visibly revert
instead of leaving a false applied state. Desktop regressions change alias,
robot-mounted/static mount, and normal/inverted orientation and prove all three
survive a full reload. The page now labels those fields as reusable lab
defaults and separates the browser-local next-run selection from the durable
run handoff.

Existing runs intentionally retain their own settings. Workflow step 1 exposes
editable per-camera **Operator alias for this run**, **Mounting for this run**,
and **Image orientation for this run** controls and writes them to
`capture.sensors[]`. A mount or orientation change clears an incompatible
per-camera profile and forces object-dataset setup to select compatible
calibration evidence rather than silently reusing the old interpretation.
Once capture status/logs, raw RGB-D/metadata, or raw robot poses exist, the
camera identity, membership, mount, orientation, resolution, FPS, and
synchronization contract and exact Sunrise robot-pose reference path are
read-only; an alias-only correction remains safe.

Static calibration now has one explicit end-to-end arrangement. A homogeneous
static group selects a robot-carried target and records
`placement.mounting_frame=robot_flange` with unknown attachment. The attempt
uses the internal eye-to-hand equation without a second editable mounting
choice, publishes `camera -> template_base` as its primary result, and jointly
estimates `aruco_grid -> robot_flange` only as nuisance/support evidence. The
robot-carried grid supplies multi-pose excitation; static cameras are not used
to track the hand during object capture. The semantic `template_base` result is
the physical `/PoseTestBot/PoseTemplateBase`. `/PoseTestBot/TemplateBase` is
retained only as the calibration controller's motion-waypoint parent. A
homogeneous eye-in-hand group records a fixed `template_base` target instead.
Mixed calibration recordings are rejected
with guidance to use separate runs. Preflight, attempt creation, and promotion
recheck the saved camera/target frames, while promotion preserves the exact
target selection rather than replacing it with a generic unknown placement.
Legacy target selections without the new frame field remain loadable through
the compatibility reader, but an unknown legacy placement is not inferred from
mutable camera setup. Readiness, attempt creation, and promotion require an
explicit target reselection.

Dataset setup now assigns a promoted calibration source per enabled camera.
One source may still cover the complete rig, while static and robot-mounted
cameras can be drawn from separate calibration runs. The server revalidates
every explicit assignment and source hash, creates a deterministic combined
camera/intrinsic collection when needed, and publishes it through the existing
read-only snapshot and replacement/CAS gates. The new
`calibration_profile_selection.v2` record retains every source bundle and its
assigned sensors; existing v1 selections remain loadable and unchanged.
Source changes after publication cannot alter the combined run-owned bytes.
Idempotence now compares the complete intended setup, exact sensor-to-profile
mapping, and source provenance rather than bundle bytes alone. Managed BOP and
BlenderProc preparation resolve every camera through that immutable mapping,
so a composite collection cannot fall back to a heuristic profile choice.
Static-profile reuse now also fails closed on physical-reference ambiguity.
Calibration attempts extract the exact absolute Sunrise reference path from a
coherent `robot_pose.v1` stream and retain `robot_pose_reference.v1` evidence in
profile metadata without changing the `calibration.v2` schema. Workflow step 1
visibly requires the expected path and stores it in dataset run configs at
`frames.robot_pose.sunrise_reference_frame_path`; selection, immutable
verification, preflight, and any existing destination raw pose stream require
an exact match. New static calibration and dataset runs use
`/PoseTestBot/PoseTemplateBase`, making their world-frame products directly
compatible. Legacy/no-path static profiles and preexisting profiles captured
relative to `/PoseTestBot/TemplateBase` remain readable but cannot be silently
selected or relabelled for this dataset role. Eye-in-hand profiles remain
base-independent. This path contract is software provenance only and does not
replace commissioning evidence that the persistent Sunrise frame was not
retaught.
Attempt requests now hash- and size-bind every raw robot-pose artifact and
rederive exact frame identity and per-artifact pose counts from those same
bytes. Preparation, automatic offset estimation, and authoritative
synchronization share that one verified in-memory snapshot, while synchronized
matches retain their original `robot_pose.v1` `source_packet`. Mixed static and
eye-in-hand BOP, BlenderProc, and Cell consumers verify those actual matched
packets before using the static world frame.
All 1,099 default tests and all 66 desktop Playwright regressions pass,
including the alias/mount/orientation default reload, run-owned camera-setting,
reference-frame provenance, three-static-camera arrangement, legacy fail-closed,
and compatible-profile replacement contracts. Ruff, frontend type checking,
the Vite production build, and `git diff --check` also pass. Read-only status
commands were used; no camera was opened, robot command was sent, or physical
capture was run.

## 2026-08-03 DIN A5/A6 Calibration Targets

The pinned PoseGridGen checkout now includes DIN A5 (148 × 210 mm) and A6
(105 × 148 mm). Both sizes flow through the existing capability-driven paper
selector, preview, fit, immutable JSON/PDF bundle, selection, and calibration
contracts without adding another target path or changing marker geometry.
PoseGridGen rejects the 100 mm ruler on A6 portrait because it cannot fit inside
the printer-safe width; the operator can disable the ruler or use landscape.

Previously generated immutable bundles from the `ad152e369e` pin remain valid
and selectable with their exact original hashes and provenance. New generation
requires the current `9e6975901f` checkout; no saved bundle is rewritten or
silently rerendered during the pin transition.

Focused renderer/bundle, Flask API, and desktop Playwright regressions pass,
including both new selector options and their physical page dimensions. The
complete 1,029-test default suite also passes without hardware access.

## 2026-08-03 IIWA High-Rate Pose-Stream Source

The retained 2026-07-28 calibration run was audited at 1,375 within-motion
intervals: 61.042 ms median (16.382 Hz), 62.779 ms p95, and 79.253 ms maximum.
That confirms the deployed stream was far below the Java loop's nominal 10 ms
sleep and repeatedly outside the former 20 ms nearest-pose calibration limit.
Attempt `ef9ffe4619104c4fb804dd71ab6e153c` consequently found zero motion groups
whose frames remained matchable across its complete time-offset search and
failed during `time_offset_search_execution`; target detection itself had
succeeded for 335 frames.

Both repository iiwa motion applications now use blocking motions with a
separate read-only `RoboticsAPICyclicBackgroundTask` requesting a 10 ms
`BestEffort` period. This removes `IMotionContainer.isFinished()` from the
sampling hot path. The shared task owns sequenced `robot_pose.v1` delivery,
retains target/delta/query-duration evidence, contains cyclic runtime failures,
and exposes them to the motion application. The receiver validates and retains
the new optional evidence. A non-destructive cadence reporter applies the
commissioning targets of 50 Hz median, 25 ms p95 gap, and 40 ms maximum gap.

The current calibration timing revision searches -300 to +300 ms, warns when a
passing candidate exceeds ±150 ms, retains nearest-pose matches through 150 ms,
and warns above 20 ms. Weak, ambiguous, boundary, inconsistent, or otherwise
unevaluable offset-search evidence fails closed before extrinsic geometry.
Automatic 0 ms timing passes only when all required identification and
stability checks support it. Missing/corrupt robot-pose evidence also fails;
reprojection, held-out residual, outlier, coverage, motion-diversity, and
multi-camera closure gates still govern a current attempt's promotion.

A historical v3 attempt on the same retained run,
`6a02e06bacbf4242b20ca50baa6bca8c`, completed all calculation phases with
exit status 0. It retained recorded timing for all 514 observations, reported
the weak +20 ms candidate and 33.363 ms maximum nearest-pose delta as degraded
warnings, and reached geometric validation. It produced no recommendation
because the unchanged continuous image-centroid coverage gates failed (x span
0.435/0.450, y span 0.318/0.350, hull area 0.088/0.100), not because of timing.
That immutable result remains inspection evidence only; the retired timing
rule cannot be rerun or promoted.

The four Java sources compile against a public Sunrise 1.15.1 API set, and all
1,029 default (non-Playwright) tests pass without hardware access. This is not
lab deployment evidence: exact Workbench background-task registration,
installed-API compilation, controller deployment, path recommissioning, and
supervised cadence measurement remain operator work under remaining milestone
1. No robot command, UDP `STOP`, camera access, or physical capture was
performed.

## 2026-07-29 Cell View Frame and Surface Rendering

Cell View now renders the robot base, flange, and TCP proxies on their local Z
axes. The flange proxy extends behind its recorded mounting-face origin and
shows a local RGB coordinate frame. Every calibrated camera likewise shows its
unchanged OpenCV frame (+X right, +Y down, +Z optical-forward) and has a solid
housing and lens in addition to the calibrated frustum, making direct scene
selection substantially easier without changing any stored transform.

The display-only reference grid is now a shallow platform recessed below the
evidence plane. Printable HRI and pose-template sheets have explicit thickness,
while pose contours and calibration-target ink are separated from their paper
faces. This removes coplanar depth fighting between the grid, white paper, and
black target squares while preserving all canonical frame origins and recorded
placements. Focused desktop Playwright image assertions cover the camera hit
target, flange direction, RGB axes, clean paper regions, target markers, and
pose-template contours. The Cell scene backend contracts, frontend type check,
lint, and production build also pass; no camera or robot was opened or
commanded.

## 2026-07-29 Run Folder Management

The Inspect navigation now includes a dedicated **Run folders** page for
auditing and managing acquisition runs across the exact server-approved run
roots. Its inventory makes each run's recursively measured size prominent and
summarizes the saved sensor setup, pose-template objects, and durable evidence
without opening cameras, contacting the robot, or mutating run artifacts.

Deletion and cross-root moves are explicit background disk jobs. The active
run remains protected until the operator switches context; deleting an
inactive run requires one fresh confirmation, while moving one requires an
available destination among the configured roots and refuses collisions.
Every request binds the discovered folder identity so a stale page cannot act
on a replacement directory. Moves also bind the inventory-time identity of the
selected destination root, so an unmounted or replaced SSD cannot silently
redirect data into the directory underneath its mountpoint. A stale or
refreshing inventory disables both actions. Discovery and mutation preserve the
existing allowed-root containment and symlink defenses, and fail before
isolation when a run contains a nested filesystem or same-device bind mount.

A completed move leaves a compatibility symlink at the former location. This
keeps immutable artifacts whose recorded provenance contains the original
absolute run path resolvable while the canonical data tree resides below the
new approved root. The page explains that contract directly, keeps queued work
visible after navigation, links to **Jobs**, and refreshes the inventory after
the background operation finishes.

Fsynced transaction journals make storage work recoverable across worker or
host interruption. Inventory recovery rolls back an uncommitted move, finishes
a committed and content-verified move, or resumes a confirmed partial deletion
without ever republishing a partially deleted tree. Unexpected path occupancy,
root replacement, or content mismatch is preserved as a visible maintenance
condition with retained-byte evidence; further mutations remain blocked.
Inventory/recovery and mutation jobs are non-cancelable because a refresh may
cross one of those already confirmed recovery boundaries.

The focused Flask and desktop Playwright contracts cover inventory size and
contents, active-run protection, one-confirm deletion, move request identity,
destination-root identity, crash recovery and idempotence, compatibility-link
guidance, background-job handoff, stale-inventory blocking, and table-local
overflow.
Validation is software-only; it does not open a camera, contact the robot,
start a lab service, or perform physical capture.

## 2026-07-27 Run-Owned Camera Alias Persistence

Camera labels now have an explicit scope and durable acquisition provenance.
The **Devices** page calls its value the reusable **Default operator alias**,
anchors its JSON store to the application root, and overlays visible-camera
edits onto the complete saved map so a disconnected camera is not forgotten.
Workflow step 1 separately exposes **Operator alias for this run** for every
configured camera. New runs inherit the current lab default, while existing
runs hydrate their saved value and cannot be silently renamed by later sensor
discovery or a global default edit.

`run_config.v3` accepts the additive optional
`capture.sensors[].operator_alias` field and keeps `display_name` as its
effective compatibility label. Capture planning carries both into
`capture_plan.json` and `dataset_manifest.json`; sensor folders and hardware
contracts remain keyed only by sensor type and device ID. The sequence runner
now reloads child-process manifest updates before recording parent completion,
fixing the stale parent write that previously discarded planned sensor records
from completed or failed multi-stage runs.

Validation completed with all 988 default pytest contracts and all 54
desktop/preview Playwright contracts passing. Ruff, frontend type checking and
lint, the production Vite rebuild, and `git diff --check` also passed. No
camera, robot, lab service, or physical capture was accessed.

## 2026-07-27 Software Completion and Physical-Plan Reconciliation

All remaining software-only rewrite maintenance is complete. The live
remaining-work document now contains only five operator-run physical
milestones: ordinary-capture controller commissioning, camera-service
acceptance, current five-sensor capture, the RealSense metric depth-scale
recheck, and physical pose-template review.

The earlier nine-frame calibration Sunrise deployment is operationally
accepted. The operator confirms that the controller class then named
`PoseTestBot_CalibrationVarianceProposal` compiled in Workbench, all nine
persistent frames were taught, that application was commissioned, and the
guided captures succeeded. The renamed repository counterpart is now
`PoseTestBotNineFrameCalibrationApplication`; the historical acceptance does
not by itself establish that the renamed/high-rate source was deployed. The
repository independently retains the three completed guided runs and
explicitly promoted attempt
`268c897e1baf49e7bd78a434a4569b99`; its common `IPPE + Shah` result passes the
calibration-validation rewrite gate at 3/3. The exact Workbench project,
controller revision record, and completed frame-teaching worksheet were not
available to copy into the repository, so no provenance was fabricated. The
teaching checklist remains a future recommissioning reference rather than an
open rewrite gate. The separate ordinary-capture Sunrise application remains
uncommissioned and is one of the five physical milestones.

The completed web and job-history maintenance includes:

- `WebSettings`, `WebRuntime`, and `LocalJobRunner` ownership in the dedicated
  `posetestbot.web.runtime` module, with application-factory injection for
  isolated tests;
- focused jobs/commands, system-status, capture, pipeline/configuration,
  calibration-stage, and sync-quality blueprints, followed by removal of
  `posetestbot/web/legacy.py` only after production and test imports were
  eliminated and packaging was verified;
- authoritative top-level `scope_kind` and `run_root` job provenance. New jobs
  explicitly select `run`, `library`, or `global`; insufficient historical
  provenance loads as `unknown`;
- a non-destructive, rebuildable SQLite index below the job root. Canonical
  job directories, `job.json` records, and logs remain the source of truth and
  are never pruned. Startup recovers nonterminal work only, while terminal
  history is loaded by exact ID or indexed page;
- opaque-cursor `/jobs` pagination with bounded limits, server-side text,
  status, scope, and run filtering, totals, status counts, first-page active
  inclusion, and the retained `jobs` and `resources` response members; and
- server-backed incremental Jobs history, authoritative active-run,
  other-run, reusable-library, lab-wide, and legacy-unknown labels, explicit
  typed status tones, and visible keyboard-accessible reasons beside
  persistently disabled actions.

Pipeline, calibration, annotation, evaluation, and run-selection jobs are
run-owned; catalogue, target, and template authoring jobs are library-owned;
previews, snapshots, monitoring, and manual robot commands are global.
Existing parameter dictionaries remain command provenance but are no longer
used to guess UI scope.

The current-format saved-data readers remain supported. The Research
Simplification milestone above supersedes this earlier compatibility decision:
`web_interface.py`, the direct non-destructive sync CLI, retired schema readers,
and the former catalogue aliases are no longer production surfaces.

Non-hardware acceptance for this pass completed with:

- installer shell syntax and the full installer check-only path;
- Ruff and 984 default pytest contracts;
- frontend type checking, lint, and a production Vite build;
- all 52 packaged Playwright contracts;
- a wheel and sdist containing the exact current 45-file packaged UI plus all
  new runtime/blueprint modules and no removed legacy module;
- an isolated installed-wheel smoke covering 128 Flask rules, the Workpiece
  Catalogue and retained pose-template catalogue APIs, generation/selection
  routes, Jobs pagination, and served hashed assets; and
- `git diff --check`.

No camera stream, snapshot, hardware status probe, robot command, or recording
was started during this work. The installer reported optional `pyzed.sl` as
unavailable, as expected on this development host.

## 2026-07-27 Active Run Folder and Console Usability Pass

The persistent top bar now uses one neutral, muted selector for the
**Active run folder**. Inside that control, a folder icon, scope label, exact
monospaced path value, adjacent statement that all run-owned pages and actions
use the folder, and inline **Change** affordance replace the former unlabeled
select and detached chevron-only action. Existing, custom, and new/unlisted
folders remain choices within the same control without making run selection
compete visually with the page's primary action.

The folder-entry dialog now names the operation as an active-context switch,
states that the folder controls the entire operator workflow rather than only
an output destination, and asks the operator to confirm the intended
acquisition run. It also makes clear that a new folder begins unconfigured and
that changing context does not copy setup or evidence from the prior run.

The accompanying consistency and usability pass:

- shows untouched future workflow steps as **Not started** instead of implying
  that they failed, shows only the selected step's panel while preserving
  unsaved drafts and live job tracking in previously visited steps, and labels
  the dataset journey as five required steps plus one optional annotation step;
- makes dataset step 5 own synchronization, quality verification,
  calibration validation, rectification, and the base BOP export, while step 6
  owns optional pose or pose-plus-mask ground-truth evidence and completes only
  when that annotation output is verified;
- consistently calls the printed object arrangement a **pose template**, uses
  **Cell View** in navigation and the page heading, and distinguishes running,
  verified, and both spellings of canceled status;
- returns Cell View and Jobs to the selected run's remembered workflow
  position, sends the Devices handoff directly to the active journey's setup
  step, and uses guided setup as the fallback; and
- keeps queued readiness, capture, processing, annotation, target-selection,
  and pose-template-selection work visible after navigation with precise run
  scope, routes to **Jobs**, and fail-closed duplicate prevention. Switching
  the active folder resets run-setup drafts before the new folder can be
  saved, and the Dashboard room monitor opens its camera only after an
  explicit **Start monitor** action.

All 51 packaged Playwright contracts (39 console and 12 preview) passed,
including focused assertions for the visible desktop affordance, strengthened
dialog, active-folder persistence and reset, step-draft preservation,
background-job recovery, workflow handoffs, optional-step state transitions,
explicit monitor start, and desktop overflow behavior. The default suite also
passed all 969 non-browser contracts. Frontend type checking, lint, and the
production Vite build passed. The packaged UI was visually inspected at 1920 ×
1080 and 1440 × 900, with an additional narrow reachability check at 900 × 900.
No camera or robot was opened or commanded.

## 2026-07-27 Dashboard Acquisition Operations

The Dashboard now prioritizes live acquisition supervision instead of
duplicating pipeline recommendations. The workcell WebRTC monitor occupies the
larger side of the primary desktop row; the former **Recommended next action**
card is removed because the persistent workflow return and evidence strip
already own guided navigation. A page visit only reads monitor status; it does
not open the room camera. **Start monitor** is the explicit camera-opening
action, while an already running monitor can reconnect and remains visible in
**Jobs**.

The adjacent **Job activity** panel polls operator jobs every second, keeps
every queued/running/canceling job immediately visible in a bounded local
scroll area, and retains the three latest failed jobs with their messages.
Capture jobs still keep the separate prominent stop-control banner. A new
five-second `/ui/storage` status reports free, used, and total capacity for the
filesystem containing the selected run. Its warning and critical reserves are
the smaller of 500/100 GiB and 15%/5% of filesystem capacity, so a nearly empty
500 GB acquisition SSD is not permanently mislabeled while larger disks retain
an absolute capture reserve.

The Dashboard workflow overview now uses the same canonical guided-step
metadata as Workflow instead of collapsing backend artifact sections into an
approximate five-tile sequence. Saved `dataset_mode=objectless` runs show the
five camera-calibration steps, while saved `dataset_mode=pose_template` runs
show all six object-dataset steps and identify the reused calibration as an
input to dataset step 1. Each tile links to its exact guided `?step=` route;
completion still comes from run-owned configuration and validated durable
artifacts. An unconfigured run shows the two-outcome workflow chooser instead
of being presented as calibration by default. Packaged Playwright coverage
locks the five-step calibration and six-step dataset variants.

Validation passed seven focused Flask/UI tests, four affected desktop
Playwright contracts at up to 1920 × 1080, frontend type checking and lint, the
production Vite build, Ruff, and diff checks. The real
`/mnt/working_data_ssd` mount was queried read-only and reported healthy with
about 95% free. No camera or robot was opened or commanded.

## 2026-07-26 Current Workflow Return

The operator console now retains one browser-local guided-workflow return point
per selected run. After an operator opens camera calibration or object-dataset
recording, the persistent desktop sidebar identifies that journey, its exact
viewed step, the step number, and the workflow rail status. Active capture and
dataset-processing jobs override the cached rail status with live queued,
running, stopping, finished, or failed state as applicable.

Both the primary **Workflow** navigation item and the sidebar resume action
return directly to the saved `?step=` route after visits to Dashboard, Cell, or
other supporting pages and after a browser reload. **Choose another workflow**
remains a separate explicit action, so fast return does not remove access to
the outcome chooser. Return points are isolated by run path: changing the
selected run never presents another run's step as the current one. This
browser-local navigation state does not mutate run-owned artifacts, imply
hardware readiness, or authorize physical execution.

Validation passed all 35 operator-console Playwright tests, including the new
1440 × 900 navigation/reload/live-recording/run-isolation regression, and all
43 non-browser web-interface tests. Frontend type checking, lint, the
production Vite build, and diff checks also passed. No camera or robot was
opened or commanded.

## 2026-07-26 Guided BOP Ground-Truth Products

The canonical **Workflow → Object dataset → Add optional BOP ground-truth
evidence** step offers two explicit, run-scoped annotation products after the
required base image/model export is verified. **Plain pose ground truth**
derives every instance's
OpenCV model-to-camera rotation and millimetre translation through immutable
object geometry, pose-template instance/placement transforms, matched robot
poses, and the selected calibration snapshot. It writes standard
`scene_gt.json` plus identity/provenance evidence, deliberately omits
visibility data, and remains marked non-evaluable.

**Pose + object masks and ROI** begins with the same BlenderProc 2.8.0
scene/pose validation, then invokes the pinned official BOP Toolkit renderer.
It compares rendered object depth with captured depth using BOP19 visibility
semantics and the 15 mm tolerance, writes full-frame binary `mask/` and
`mask_visib/` PNGs, and produces official `scene_gt_info.json` pixel counts,
visibility fractions, and `bbox_obj`/`bbox_visib` ROI. Only this complete mode
rebuilds the visibility-filtered BOP19 targets and advertises evaluation
readiness.

One `LocalJobRunner` job owns preparation, BlenderProc pose derivation, and
transactional BOP re-export. Its mode and run remain discoverable through
persistent Jobs history after navigation. Readiness fails closed on an
unconfirmed template placement, invalid calibration snapshot, mismatched
RGB/depth/robot-pose frame keys, stale base export, missing BlenderProc, or—for
the full product—the missing pinned toolkit runtime. Raw frames, robot poses,
template selection, and calibration snapshots are read-only.

The retained `working_data/test20260726_BOPv5` run now contains the completed
`pose_and_masks` product. BlenderProc 2.8.0 validated both calibrated camera
scenes and generated 1,621 rigid per-instance poses (810 + 811 frames). The
pinned clean BOP Toolkit then generated 1,621 full masks, 1,621 visible masks,
and both GT-info files. Every instance is above the 0.1 visibility target
threshold (minimum visibility fractions 0.8593 and 0.8143 by scene), so the
1,621 BOP19 target rows reconcile exactly. Durable pose/mask hashes verify,
the export advertises BOP19 evaluation capability, and
`rewrite_bop_export_readiness.v1` passes 11/11 checks. Representative
RGB/full-mask/visible-mask/ROI evidence was inspected without changing raw
capture data.

## 2026-07-26 Inspect-only Official BOP19 Evaluation

The **Inspect → BOP Evaluation** page now validates a selected run's completed,
annotation-bearing BOP v5 export without mutating `bop/`, raw capture,
synchronization, calibration, or GT evidence. It accepts canonical BOP19 result
CSVs, validates their filename dataset/split identity, exact columns, pose
values, target coverage, size, and hash, then retains independently selectable
method/results. Before real estimator output exists, an explicitly test-only
mode deterministically perturbs GT translation and rotation by small bounded
offsets and registers the generated file through the same validation contract.
The UI never presents those simulated values as estimator performance.

Evaluation is a CPU/disk `LocalJobRunner` job and continues after navigation.
It invokes official BOP Toolkit commit
`cea62d651c7e395b2e1962b9749e4e89693c6ac4` in the isolated locked
`tools/bop_toolkit_runtime` environment. A runtime-only generic-dataset adapter
keeps the pinned submodule clean while directing its standard error/score
scripts at the selected export. Reports expose overall BOP19 Average Recall,
AR VSD, AR MSSD, AR MSPD, timing, and immutable dataset, result, adapter,
renderer, and toolkit provenance. History keeps different methods, result
runs, and simulated fixtures separately selectable.

The completed real v5 annotation product was exercised through this path with
the deterministic 1 mm translation / 0.25° rotation, seed-42 fixture. The
official toolkit accepted all 1,621 estimates and reported overall BOP19 AR
0.9628, AR VSD 0.8885, AR MSSD 1.0000, and AR MSPD 1.0000. Evaluation
`evaluation-640602f34b4e` retains the exact dataset, depth-content, result,
adapter, renderer, command, and toolkit hashes below
`processed/bop_evaluation/`; these values are format-validation evidence, not
pose-estimator performance.

Final validation passed 160 focused GT/export/evaluation/web tests, 43 pipeline
registry/sequence tests, all 50 packaged Playwright contracts, frontend type
checking and lint, the production Vite build, Ruff, and diff checks. The final
default suite reports 969 passed and 50 deselected. At that checkpoint, both
tracked iiwa applications still used repository-local inerting guards and the
tests asserted that state. The 2026-08-04 iiwa update above supersedes those
guards with an operational no-motion-before-START contract. No
validation command altered or executed either robot application.

The feature is deliberately not an estimator wrapper, proprietary-result
converter, general evaluator bridge, or pipeline sequence. Imported/simulated
results live below `processed/bop_evaluation/results/<result_id>/`; requests,
progress, resolved input, adapter configuration, official toolkit output, and
final reports live below
`processed/bop_evaluation/evaluations/<evaluation_id>/`. The optional installer
flag `--with-bop-toolkit` initializes the pinned checkout and synchronizes this
separate NumPy-below-2 toolkit environment without changing PoseTestBot's
NumPy-2 main environment.

## 2026-08-04 External Cluster Storage and FoundationPose Companion

Implemented the separate `match-cow/posetestbot-cluster` companion boundary.
The companion is a loopback-only authenticated FastAPI controller with
SQLite-journaled archive, restore, transfer, SLURM, collection, cancellation,
and restart-reconciliation states. It pins the LUIS transfer/login host split,
generated PROJECT/BIGWORK layouts, strict host-key and unattended-key policy,
server-owned GPU profiles, a FoundationPose SIF/weights/runtime qualification
contract, seven-day failed-stage retention, and exact recorded-ID
cancellation. Full-run archives reject links, special files, nested mounts,
device crossings, traversal, and changing sources; verified restore extracts
manifest-listed regular files only. FoundationPose consumes only complete BOP
v5 `pose_and_masks` exports, uses `mask_visib` as an explicit oracle GT mask,
runs independent per-target registration without tracking, preserves partial
failures, and emits a pinned-loader-validated standard BOP19 CSV.

PoseTestBot gained only the thin external boundary: typed loopback calls,
`/cluster/*` proxies behind one enable switch, visible readiness blockers,
durable pose-job/archive presentation, and immutable result import. **Inspect →
Pose Estimation** shows the dataset/runtime/profile/oracle-mask contract and
states that work continues through **Jobs**. Run folders can create archive
copies and restore verified remote-only runs; PoseTestBot does not prepare an
archive move or request source deletion. Imported controller CSVs are
revalidated locally and retain compact allowlisted scientific provenance plus
the digest of the original controller evidence while browser responses expose
only essential provider/job/scheduler identity. Intact
historical CSVs remain downloadable after dataset drift while evaluation
correctly remains incompatible.

No FoundationPose code, SSH credential, arbitrary remote path/SLURM argument,
estimator converter, or pipeline stage entered PoseTestBot. The browser sees
neither the controller token, credentials, nor remote paths. The production Vite
bundle includes the new desktop pages, and focused controller/proxy/archive/
result-import regressions cover authentication, containment, idempotency,
restart state, exact cancellation, stale-dataset refusal, retention, and
historical download integrity.

Final non-hardware validation passed all 30 companion-controller tests, its
Ruff and lock checks, and reproducible wheel/source builds whose source
archive includes the offline runtime and qualification assets. PoseTestBot's
full default suite passed 1,115 tests; a final 32-test cluster/result-import
focus passed after the archive-drift hardening; and all 68 packaged desktop
Playwright contracts passed at the required viewports. Frontend lint, the
production Vite build, installer check-only validation, shell syntax, Ruff,
and diff checks also passed. No command contacted LUIS, opened a camera, or
commanded the robot.

A follow-up aligned the runtime deployment with the lab's early-stage research
workflow. The human/controller FoundationPose license-approval boolean and CLI
acknowledgement were removed end to end, including the browser presentation.
The companion now retains the exact license file from its pinned FoundationPose
commit, copies it into the SIF, and records its SHA-256 as ordinary runtime
provenance. The companion remains MIT-licensed without relicensing
FoundationPose. Its documented build path now stages a private context through
the transfer host, builds with Apptainer `--fakeroot` on a LUIS login node, and
stores the unpublished SIF under the fixed BIGWORK runtime root. Focused
follow-up validation passed 35 companion tests and Ruff, 29 PoseTestBot cluster
and BOP-evaluation tests, both pose-estimation Playwright contracts, frontend
lint, and the production Vite build. No command built a SIF, submitted a SLURM
job, opened a camera, or commanded the robot.

## 2026-07-26 Object-Dataset Research Speed Range

The guided **Record an object dataset** setup now accepts requested
capture speeds from 0.01–1.00 m/s and preserves existing run-owned values in
that range. Calibration setup remains at 0.01–0.03 m/s. Dataset requests above
the conservative 0.03 m/s legacy range use `robot_command.v1`; the canonical
plan and receiver pass them through up to 1.00 m/s and reject an extended cap
with the legacy protocol.

The ordinary-capture Sunrise candidate no longer adds a Cartesian 0.03 m/s
clamp, while retaining its independent 3°/s A1 angular-speed cap. Setup and
physical-authorization copy show the requested value, the structured-app
requirement, the A1 cap, and the warning that speed alone cannot guarantee
sharp frames because exposure time and lighting still matter.

Focused validation passed 39 capture-plan, receiver, UDP, and Java conversion
tests; two desktop Playwright workflow regressions; Ruff; frontend lint and
type checking; the production Vite build; and diff checks. No camera or robot
was opened or commanded.

## 2026-07-26 BOP Toolkit Compatibility and Clean Export Contract

The retained object capture `working_data/test20260725_04` was audited against
official BOP Toolkit commit `cea62d651c7e395b2e1962b9749e4e89693c6ac4`.
Its two `test/00000x` scene layouts, contiguous RGB/depth names, 16-bit depth,
per-image intrinsics/depth scale, model ID, and `models_info.json` geometry are
structurally BOP-scenewise. All 811 exported frames per camera are inside the
recorded `a1_capture_sweep`; synchronization correctly kept 712 and 667
pre/post-motion raw frames out of the BOP scenes.

The v4 export was nevertheless not directly consumable: the canonical binary
PLY had an importer-specific `property ushort stl` face attribute, and the
official BOP Toolkit `load_ply` reader lost face alignment and rejected it.
The v5 exporter now derives conservative ASCII triangular PLY files with
vertex normals, optional supported UV/color fields, and an optional copied PNG
texture. It writes the estimator model under `models/` and a texture-free
evaluation copy under `models_eval/`, because the official metric scripts
request the `_eval` model type. Both preserve source vertices, faces, object
coordinates, and millimetre units while leaving the immutable catalogue
geometry untouched.

A temporary one-frame v5 sample built from the retained run's actual rectified
RGB-D pair and Greifer snapshot was accepted by the official generic scenewise
reader. It reported RGB/depth present and GT/masks absent, loaded the camera and
populated target row, and loaded both 3,684-vertex / 1,228-face model copies.

The subsequent real object run `working_data/test20260726_BOPv5` completed
through job `40cf447b9486` with return code 0 and passed
`rewrite_bop_export_readiness.v1` (11/11 checks). Its clean v5 export contains
two scenes with 810 and 811 paired RGB-D frames, 1,621 matching BOP19 target
rows, and one Greifer model in both `models/` and `models_eval/`. It contains no
placeholder GT, GT-info, mask, or visible-mask files. A read-only quality
recalculation also confirmed 1,621/1,621 eligible in-motion frames synchronized;
the 1,363 other raw frames were intentional outside-motion context, not
synchronization failures. The same official toolkit revision loaded both
scenes' first and last calibrated RGB-D samples, all camera rows and targets,
and both 3,684-vertex / 1,228-face model copies directly from this full export.

`bop_export_manifest.v5` also makes annotation capability explicit.
Annotation-free output contains RGB-D scenes, standard `scene_camera.json`,
selected models, compact portable provenance, and a populated BOP19 target list
derived from the confirmed pose-template object counts. It omits placeholder
GT, GT-info, masks, and GT instance maps. Pose-only mode adds `scene_gt.json`
and identity/provenance without claiming evaluation readiness. The complete
pose-plus-mask mode adds official visibility and mask evidence and is the only
mode marked ready for BOP19 evaluation. Export metadata no longer embeds
absolute run paths, per-frame camera JSON no longer repeats PoseTestBot
calibration payloads, and the manifest retains only calibration profiles used
by exported scenes.

For new annotation-bearing exports, `test_targets_bop19.json` now counts only
GT instances whose `scene_gt_info.json` `visib_fract` is at least 0.1, matching
the official BOP19 localization target policy. Inspect evaluation cross-checks
that inventory against GT-info. An older export whose target counts include
less-visible instances remains inspectable, but receives an explicit warning:
its scores are valid for its exported target list and are not
leaderboard-comparable.

The active gates are:

- `rewrite_full_capture.v1`
- `rewrite_calibration_validation.v1`
- `rewrite_bop_export_readiness.v1`

The guided dataset-processing step now follows its persistent local job after
submission and after navigation. It shows queued, running, canceling, failed,
successful-but-not-yet-verified, and verified states; marks the workflow rail
as running; identifies the next unverified outcome; refreshes durable evidence
when the job exits; and links directly to Jobs for the live log and
cancellation. The Jobs log drawer has separate copy controls for complete
process output and structured job context/metadata. The overview also treats a
missing BlenderProc plan as optional for an annotation-free export, so a
verified image/model BOP dataset is no longer mislabeled `in_progress`.

Timestamp-aligned quality now grades synchronized coverage only against camera
frames eligible inside robot motion intervals. Preserved lead-in/tail frames
remain raw lifecycle context and no longer create a false low-match warning.
Reports separately retain eligible/matched coverage, missing or fallback
timestamps, nearest-pose rejections, unexplained in-motion exclusions, and
audited robot-pose packet loss; CLI summaries no longer present all raw frames
as a dataset-quality denominator.

Calibration-target reuse is explicit in the operator console. A saved target is
a global reusable library entry and can be selected by every fresh calibration
run, including after cameras move. Once target-dependent evidence exists, only
that run-owned target/placement snapshot is locked: reviewing the active target
is read-only, replacement controls explain the fresh-run requirement before
submission, and an exact repeat selection is an idempotent success rather than
a queued mutation. Existing raw and derived calibration evidence remains
untouched.

Workflow pose-template selection now uses each bundle's exact immutable
canonical PLY assets for the selected 3D detail instead of the compact
stable-orientation proxy. The scene initially frames the objects rather than
the entire printed sheet, retains source vertex colours and open-surface
visibility, and maps numbered scene markers to a persistent object index with
the workpiece name, BOP object ID, dimensions, face count, and orientation.
Operators can focus one instance, refit all objects, or return to the sheet
overview; the bounded proxy remains only as a per-object loading/error
fallback.

The Cell view now composes the run's actual context surface: exact compensated
pose-template footprint contours for object-bearing runs, the selected or
latest run-local calibration-attempt board for calibration runs, and the
packaged HRI sheet only as a fallback. Promoted board placement is recovered
from calibration-profile companion transforms; boards without promoted
placement remain visibly marked as reference overlays. Optional robot-base and
TCP frames are reported as not configured instead of unresolved, while cameras
still fail closed when this run has no matching promoted profile. Calibration
target scenes retain the canonical top-left, +X-right, +Y-down, +Z-into-board
frame and use a presentation-only right-handed target alignment, so cameras on
the printed/front negative-Z side appear above the grid without changing any
stored pose or promoted calibration transform. The page now gives the 3D scene
its own full-width inspection surface, keeps its pose source and slider directly
below it, places retained camera evidence in a separate full-width section
below the scene, and moves visibility, selection provenance, and the component
list into a final evidence row. Operators can select multiple named camera
timelines and compare them side by side as RGB, fixed-range colourized metric
depth, or both. Each tile applies the shared slider ordinal to that camera's own
exact matched timeline without interpolation; visible copy explicitly states
that cross-camera views are timestamp aligned, not simultaneous exposures.
Inverted RealSense mounts use capture metadata to avoid
double rotation: already corrected stored frames are shown directly, while
older frames lacking that evidence receive the configured 180-degree display
correction. Depth previews use a stable 200–3000 mm near-warm/far-cool scale,
keep zero/invalid pixels black, and never modify the retained uint16 PNG.

Historical run `working_data/hot_full_capture_fixed_20260710_1351` passes the
full-capture gate at 10/10 for three RealSense cameras. The current five-sensor
profile, combined camera-service lifecycle acceptance, and real BOP v4 export
still require operator-run acceptance. That historical run used an older 10 ×
7 / 70-marker board and insufficient hand-eye motion diversity; it is a
preserved negative baseline, not `calib00` calibration evidence.

The `calib00` guided campaign completed three independent physical acquisition
and calibration journeys with all three eye-in-hand RealSense cameras.
Historical attempts `d909d13cc5944a068e8a2ec13eeedd32`,
`3106a2b80b87444db0ac26de89bc01b3`, and
`f1e990d3424a48ed95b266f7bf134838` each produced one complete common bundle.
Maximum within-run stationary-companion closure was 7.847 mm / 1.115°; the
8.642 mm / 1.099° maximum cross-run difference remains a method-confounded
diagnostic requiring a controlled repeat. The validation record is
[EYE_IN_HAND_CALIBRATION_VALIDATION_20260723.md](EYE_IN_HAND_CALIBRATION_VALIDATION_20260723.md).
At campaign completion each run passed `rewrite_full_capture.v1` at 10/10 and
`rewrite_calibration_validation.v1` at 3/3. The later top-level reusable
profile collections were retired because they predate required time-alignment
provenance; raw captures and immutable attempt evidence remain.

Calibration attempts default to per-camera Auto time alignment and retain
the complete bounded search and decision in
`time_offset_search.json`. A retrospective replay selected +70 to +75 ms,
+80 to +85 ms, and +45 to +55 ms for the three cameras and reduced
cross-validated stationary-target translation residual by 14–38% versus 0 ms.
This is effective-latency tuning from robot motion, not hardware-clock proof,
and it was not promoted back into the historical attempts.

The search-corrected motion audit was introduced in historical revision
`constant_latency_nearest_pose_motion_lomo_cv.v2`. It keeps the interior
optimum, aggregate materiality, fold-optimum stability, reference-method
sensitivity, and rotation gates, then refits the transform with every motion
held out in turn. The default now requires at least 12 motions, four per fold;
both Shah and Li must pass 0.25 mm / 10% median materiality and a one-sided
positive-motion sign test Bonferroni-corrected over the complete nonzero offset
grid at 0.05. Per-fold materiality remains recorded as a warning rather than
allowing one arbitrary three-fold partition to veto stronger motion-level
evidence. Promotion recalculates the saved per-motion improvements, medians,
sign probability, and search correction instead of trusting saved `ok` labels.
The current research-stage revision retains that strong-evidence path for
applying a nonzero offset. A completed but weak, ambiguous, inconsistent, or
boundary-limited search keeps recorded 0 ms with degraded warning evidence;
missing, corrupt, or unevaluable input still blocks. An automatic 0 ms result
is identified without degradation only when all required folds support zero.
Earlier attempts retain their immutable evidence for inspection only; they
cannot be replayed or promoted.

Real retained-data attempt `1c6b0c9d00dc49ce8d0c14c18d43336b` completed
under v2 with all three required D435 cameras. It selected +70 ms, +85 ms, and
+45 ms; both Shah and Li passed the search-corrected leave-one-motion-out
audit for every camera. The common `IPPE + Shah` recommendation has maximum
three-camera companion closure of 3.172 mm / 0.450°, and 15 common bundles
pass.

An operator retry, `e588682f5ad64e9aaf8ed39e7b02c623`, revealed that the
still-running web backend had not been restarted after an implementation
change. Its immutable historical evidence remains inspectable but is not a
current rerun or promotion source. The setup API reports its loaded timing
revision; the packaged workflow blocks Auto attempt creation when that value is
missing or differs from the revision expected by the UI, identifies the
required backend restart, and labels historical attempts accurately.

Fresh-process attempt `268c897e1baf49e7bd78a434a4569b99` repeated the exact
recordings under v2 and reproduced all three offsets, residuals, the common
`IPPE + Shah` recommendation, and 3.172 mm / 0.450° closure. Its three
recommended profiles were explicitly promoted with saved sync deltas -70 ms,
-85 ms, and -45 ms. The run's canonical `calibration_profiles.json` is now
`calibration.v2`, all three profiles are valid, and
`rewrite_calibration_validation.v1` is ready at 3/3.

Extrinsic image-coverage acceptance is partition-independent. Eye-in-hand
attempts require five-view-supported normalized centroid spans of at least 45%
x and 35% y plus at least 10% supported convex-hull area. Research-stage static
eye-to-hand attempts use 15% / 20% / 3%; the actual mode-specific thresholds
are recorded in ranking evidence. The 3 × 3 cell count remains visible as a
diagnostic warning and remains the separate manual-intrinsic gate. The strict
eye-in-hand policy allowed the real `033422071805` evidence to be judged by its
measured 53.1% / 43.3% spans and 13.25% hull area instead of failing solely
because those views landed in five arbitrary cells.

Object-dataset synchronization now applies the exact per-camera timing from the
selected run-owned calibration snapshot and rejects overrides. Sync quality,
rectification, and BOP export recheck camera coverage, values, profile identity,
bundle hash, and timestamp provenance. The guided page exposes the policy and
blocks readiness when it cannot be verified.

The campaign also closed three defects in the guided page: a brand-new run no
longer stalls on a missing config, recorded calibration cameras refresh after
capture, and the physical action now submits the canonical
`real_full_capture_validation` sequence rather than bypassing its hardware
snapshot and preflight artifacts. The calculation card documents the observed
10–20 minute duration and persisted background-job behavior. The
full-capture gate accepts the immutable pre-START preflight embedded in an
execution plan when the standalone report is absent, and rejects mismatched
embedded status.

## 2026-07-25 Object-Dataset Capture Validation

The guided physical-capture request and the canonical
`real_full_capture_validation` sequence require each camera to publish three
valid committed metadata records before robot reception starts. The
authorization dialog shows the per-camera startup deadline alongside the
independent robot packet timeouts. Frame metadata becomes visible to the
supervisor after every complete JSONL record, while the storage durability
barrier is deferred from the per-frame hot path to one `fsync` during each
adapter's shutdown.

Authorized retry `working_data/test20260725_03` reused the exact verified
calibration and pose-template snapshots from the failed object-dataset attempt
in a fresh run root. Its two enabled D435 cameras completed 1,503 and 1,480
balanced RGB/depth/metadata tuples, with maximum host-receipt gaps of 0.347055
and 0.339562 seconds. The iiwa receiver committed 12,226 poses, both camera
processes exited cleanly after the receiver, capture execution succeeded with
the raw evidence preserved, and `rewrite_full_capture.v1` is ready at 9/9. This
validates the current two-camera object-dataset capture path; it does not close
the separate five-sensor acceptance milestone.

## 2026-07-25 Annotation-Free Real BOP Export (v4 checkpoint)

At this checkpoint, the guided object-dataset processing sequence no longer made required BOP
export depend on BlenderProc preparation or rendering. Its required path is
now synchronization, sync quality, calibration preflight, rectification, and
transactional BOP export. The v4 exporter recorded an explicit
`annotation_source` contract: `none` wrote explicit empty annotation rows in
`scene_gt.json` and `scene_gt_info.json`, an empty `test_targets_bop19.json`
list, and an empty instance map while still exporting calibrated RGB-D scenes,
canonical models, pose-template provenance, and frame maps. `blenderproc`
remains an explicit opt-in source for the separate optional GT/mask path.
Required workflow retries atomically replace only the derived rectification
and BOP trees.

Retained real run `working_data/test20260725_04` exercised the corrected path
without camera or robot access. Synchronization reproduced 1,622 matched
frames across the two enabled D435 cameras, calibration preflight passed 2/2,
and rectification/export wrote two 811-frame BOP scenes plus canonical
`Greifer` model `obj_000004.ply`. The export manifest is
`bop_export_manifest.v4`, declares `dataset_mode=pose_template` and
`annotation_source=none`, validated 2 scenes / 1,622 frames / 1 model /
0 rendered targets, and passes `rewrite_bop_export_readiness.v1` at 11/11.
The original raw folders remain at 1,523 and 1,478 balanced RGB/depth frames.
The 2026-07-26 compatibility audit above supersedes the v4 empty-placeholder
and byte-for-byte model-copy contracts.

## 2026-07-24 Manual IIWA Command Confirmation

The Dashboard and Devices manual IIWA dialogs now use one fresh confirmation
per START or STOP request. The single START control visibly retains target
identity, real-robot motion authorization, and camera/pose-receiver readiness;
checking it sends both independent backend execution gates. STOP retains its
single target confirmation and the visible warning that UDP STOP cannot
interrupt active motion and exits the waiting calibration application.

## 2026-07-24 Ordinary IIWA Capture Candidate Hardening

The repository's still-unconfirmed ordinary Sunrise candidate now uses the
persistent `/PoseTestBot/PoseTemplateBase` instead of the historical HRC
reference. Static-camera calibration pose streaming uses that same result
frame; only the calibration motion waypoints remain below the separate
`/PoseTestBot/TemplateBase`. At that checkpoint the candidate was held pending
exact Sunrise.Workbench compilation, frame/path simulation, T1 commissioning,
and deployed-application identification. The accompanying operator note
explains how the moving calibration board, pose-template, and dataset reference
transforms remain explicit. Preexisting wrong-frame static profiles remain
non-portable and are not relabelled.

The candidate no longer moves before START or returns after its end marker. It
converts `cartesian_velocity_m_s` into a bounded relative A1 speed from the
measured flange orbit radius, defaults pose delivery to `172.31.1.169`, logs
command/send/interrupt failures, and emits sequenced `robot_pose.v1` packets
with run and Sunrise-frame identity. The hardened receiver remains compatible
with legacy packets while retaining and validating the new metadata and
recording sequence gaps as UDP-loss evidence.

The candidate source also requires taught `/PoseTestBot/CaptureStart` and
`/PoseTestBot/CaptureEnd` Application Data frames. After an accepted START it
moves PTP to the start frame before sampling the A1 sweep's non-A1 joint
branch, and it completes the end-frame PTP before emitting the terminal
marker. Teaching and commissioning those frames and all three intervening
paths remain operator work.

The workflow speed control now states that ordinary full capture is an A1
joint PTP whose tangential flange-speed request is converted by Sunrise. New
runs default to 0.01 m/s and the UI permits 0.01–0.03 m/s. Run-owned
acquisition commands, candidate Cartesian input, and candidate A1 angular
motion are independently capped at 0.03, 0.03 m/s, and 3°/s. The separately
acknowledged Dashboard/Devices manual motion-test command requests 0.1 m/s,
ten times its former 0.01 m/s request, and shows that value in its confirmation
dialog and job provenance. The deployed Sunrise application may apply a lower
controller-side cap. The calibration application retains 8–30 mm/s
raster/relative motion and 3% orientation joint speed. The supervisor envelope
is now 720 seconds so a 0.01 m/s sweep is not terminated by the old five-minute
total limit. Operator copy explicitly notes that speed alone cannot guarantee
sharp images because exposure/readout time and lighting remain camera-dependent.

Repository-only validation passed all 878 default tests, the focused desktop
Playwright speed/capture-gate regression, Ruff, frontend type checking and
lint, the Vite production build, and diff checks. This host has no
Sunrise/JSON-simple controller classpath, so the exact Workbench compile,
simulation, deployed identity, frame teaching, T1 checks, and physical trial
remain explicitly unfinished.

## 2026-07-23 Operator Console Streamlining

The complete packaged operator console received a desktop-first usability and
process audit:

- grouped navigation into **Operate**, **Prepare**, and **Inspect**, moved the
  canonical **Workflow** beside the Dashboard, aligned navigation labels with
  page titles, and added an always-available console guide that explains the
  two outcomes, safety boundary, and core evidence terms;
- added compact workflow handoffs to Devices, Calibration Targets, Workpiece
  Catalogue, Pose Templates, Cell, and Jobs so each supporting page names its
  scope, its durable output, and the next guided step;
- distinguished a configured robot profile from a verified or ready robot,
  exposed dashboard API failures, clarified that device labels and mounts are
  saved separately from the browser-local run draft, and added keyboard- and
  click-accessible help for technical calibration and placement controls;
- made the workflow resume the first running or incomplete required step,
  reflected the viewed step in the URL and step rail without skipping the
  journey title on initial resume, reset document scroll when moving between
  pages, reset the run-path dialog to the current run on every open, and allowed
  Enter to submit it;
- made long job histories operable with search, state filters, active-first
  ordering, 20-row progressive disclosure, clearer timing and resource-lock
  evidence, guarded repeated cancellation, and visible log-load failures;
- improved normal-desktop and narrower reachability across the utility pages
  without hiding evidence or converting the console into a phone-first layout;
  and
- replaced the sprawling README with a short acquisition boundary, five-step
  console start, page-handoff table, safety summary, outputs, validation
  commands, and focused documentation index. Development guidance now records
  the same handoff, status-language, visible-instruction, and bounded-history
  contracts in `AGENTS.md`.

This pass changed only software and documentation. Validation did not send a
robot command, authorize motion, execute physical capture, or satisfy any
physical acceptance item in
[REWRITE_REMAINING_WORK.md](REWRITE_REMAINING_WORK.md). The visual audit loaded
the console's ordinary local status and monitor surfaces.

Validation completed on 2026-07-23:

- all 860 non-browser pytest tests and all 42 explicitly marked desktop
  Playwright tests passed;
- Ruff, frontend type checking and lint, the production Vite build, installer
  check-only path, shell syntax, and `git diff --check` passed;
- the packaged console was visually inspected at 1920 × 1080 and 1440 × 900,
  including every primary page, both guided journeys, the global guide,
  handoffs, long job history, and cross-page scroll restoration; and
- no robot command, physical capture, or rewrite acceptance gate was executed.

## 2026-07-24 Pose-Template Orientation Preview Fidelity

- The stable-orientation chooser now renders the topology-aware recognition
  mesh (up to 4,096 vertices and 8,192 faces) instead of the deliberately tiny
  160-vertex/256-face printable-layout proxy. The selected-instance inspection
  uses the same higher-fidelity surface, preserving holes, recesses, handles,
  and separated mechanical features that identify a workpiece.
- Orientation comparisons now use a wider two-column desktop layout with
  larger rasterized 3D views, explicit displayed/source face counts, and a
  visible warning if only the proxy fallback is available. Desktop Playwright
  coverage proves that the chooser and selected-instance inspection consume
  the recognition mesh while catalogue cards retain their bounded card mesh.

## 2026-07-24 Workpiece Preview Runtime/Cache Recovery

- Bounded Workpiece Catalogue and Pose Templates cards now distinguish an
  implementation-revision mismatch from an ordinary missing/stale preview and
  visibly direct the operator to restart PoseTestBot and reload. The backend's
  exact validation error remains available on the card for diagnosis.
- The Catalogue's top-level **Refresh** action now invalidates both ranked
  orientation and bounded-thumbnail queries, so a restarted runtime or newly
  generated cache recovers without leaving React Query's earlier failure on
  screen. Desktop Playwright coverage exercises the mismatch on both pages and
  verifies recovery through that refresh action.
- Workpiece deletion is now directly available from both active and archived
  states while retaining explicit confirmation, reference blockers, stable
  UUID/BOP-ID tombstones, and retryable asset cleanup. Pose-template library
  cards also expose confirmed permanent deletion for active or archived
  versions; deletion atomically retires the global bundle, retains a UUID
  tombstone, queues physical asset cleanup as a disk job visible under
  **Jobs**, and leaves run-owned copied snapshots unchanged.

## 2026-07-23 Workpiece Recognition Preview Fidelity

- The selected Workpiece Catalogue detail now loads the hash-versioned exact
  canonical PLY in its authored orientation. Vertex colours and source normals
  are retained when present, missing normals are generated, and open CAD is
  rendered double-sided. Selected-object recognition no longer depends on
  stable-pose analysis or its compact convex proxy.
- Catalogue cards now use a separate recognition-focused LOD: indexed geometry
  is welded and, when it does not fit, deterministic quadric and spatial
  candidates are compared by component/Euler topology before the legacy convex
  safety proxy. The chosen strategy, counts, topology signatures, and fallback
  reason are retained with the cache. The card remains bounded to 4,096
  vertices, 8,192 faces, and 256 KiB, while immutable pose-template previews
  keep their smaller interaction-oriented tier.
- Dense card meshes render through one Canvas2D surface instead of thousands of
  SVG nodes and are loaded only near the viewport. A keyboard-accessible
  `LOD`/`Approx`/`Proxy` badge reports source and displayed face counts and any
  topology loss, and the catalogue exposes an explicit queued **Refresh card
  preview** action for stale caches.
- The current DGS-108 example improved from a 118-vertex / 232-face convex card
  proxy to a 2,194-vertex / 4,636-face recognition LOD (125,838-byte thumbnail)
  that retains the welded source's Euler value of -124; its selected detail
  uses the full 245,508-vertex / 81,836-face canonical model.
- Added hollow/perforated-part, relative-quantization, cache-bound, and
  deterministic-spatial regressions so topology loss is detected and labelled,
  plus desktop browser coverage for exact selected-model loading, hash
  cache-busting, analysis-free rendering, dense Canvas2D cards, accessible LOD
  evidence, and queued card refresh.

Validation completed on 2026-07-23:

- 720 non-browser pytest tests and all 40 explicitly marked Playwright tests
  passed;
- Ruff, frontend type checking and lint, the production frontend build, shell
  syntax, installer check-only, and `git diff --check` passed; and
- no camera, robot, lab service, or physical capture was accessed.

## 2026-07-22 Guided Operator Workflows

Implementation of the outcome-oriented operator workflow architecture is
complete:

- replaced the generic seven-phase primary navigation with two guided journeys:
  a five-step required camera-calibration spine and a six-step required
  object-dataset spine, with persistent artifact-backed status and visual
  dependencies;
- kept optional target/template authoring, advanced calibration evidence, and
  BlenderProc GT/mask work visibly outside the required spine, while retaining
  individual stage forms only under **Advanced tools** for diagnostics and
  recovery;
- collapsed operator preflight into one visible readiness facade per journey,
  with human-readable missing/stale/failed/invalid states, while preserving the
  separate two-acknowledgement capture dialog and fresh startup checks at the
  physical execution boundary;
- made a prior promoted calibration a required object-dataset input, with
  per-camera compatibility checks, a hash-bound
  `calibration_profile_selection.json`, and exact run-owned profile snapshots
  below `processed/calibration_inputs/<bundle_sha256>/`; snapshot pairs are
  re-hashed at readiness and immediately before rectification, BlenderProc
  preparation, or BOP export, while selection replacement is confirmation/CAS
  gated and blocked after capture or derived dataset material exists;
- added keyboard-accessible contextual help and explicit explanations for
  camera mounting modes, template placement, synchronization, BOP output, and
  Factory SDK versus OpenCV intrinsics. Compatible factory projection remains
  selected by policy; OpenCV activates only as the fully gated fallback when
  factory projection is unusable;
- bound calibration analysis to the step-2 run-owned grid and the step-1 camera
  mounting identities, split mixed static/robot-mounted selections into
  separate attempts, and reject contradictory mode or target submissions at
  the API boundary;
- made guided progress depend on schema/status-validated evidence rather than
  file existence, use the run-level sync-quality report as the aggregate over
  per-camera sync reports, and refresh progress while queued work completes;
  and
- added the versioned `operator_workflows.v1` description endpoint while
  retaining old workflow URLs as redirects into the corresponding guided step.

This was a software-only workflow and documentation change. It did not open a
camera, contact the robot, authorize motion, or complete any outstanding
operator-run acceptance item in
[REWRITE_REMAINING_WORK.md](REWRITE_REMAINING_WORK.md).

Repository-wide software validation of this redesign completed on 2026-07-22:

- 677 non-browser pytest tests and all 33 explicitly marked Playwright tests
  passed (710 total);
- Ruff, frontend type checking and lint, the production Vite build, and
  `git diff --check` passed; and
- the browser suite covered both numbered journeys, responsive navigation,
  one visible readiness action, calibration selection/replacement CAS, fresh
  capture gates, Factory/OpenCV guidance, automatic progress refresh, and the
  consolidated dataset-processing action.

## 2026-07-22 Workpiece Catalogue

Implementation of the dedicated **Workpiece Catalogue** feature is complete:

- added the navigation entry and `/workpieces` page below Calibration Targets
  and above Pose Templates, with upload, detail/edit, search, tag/group/state
  filtering, compact isometric and interactive previews, usage evidence,
  archive/restore/delete, revisioned metre/mm correction, and JSON
  import/export;
- retained PLY/STL/OBJ source CAD, canonical PLY, optional PNG texture, hashes,
  and editable `name`, `alias`, `description`, `tags`, `groups`, and
  `attributes` in `working_data/object_catalog/` without adding a database;
- added a selected-object interactive client-side mesh view plus bounded static
  isometric card previews without multiplying WebGL contexts. Catalogue cards
  use a separate at-most-256-KiB, canonical-hash-bound orientation thumbnail;
  only the selected editor path reads ranked orientations and exact contours;
- serialized Flask/worker mutations with cross-process locking and atomic
  numbered revisions, and made permanent deletion require explicit
  confirmation, zero pose-template references, and a fully valid published
  template library while retaining never-reused UUID/BOP-ID tombstones;
- serialized immutable template publication against catalogue deletion, made
  deletion commit its tombstone before removing assets, contained queued-worker
  cleanup to managed request UUID directories, capped streamed multipart and
  JSON requests even when Content-Length is absent, and bounded both persisted
  job logs and per-line API tails. Upload and unit-correction workers clean
  request folders on failure as well as success, and submissions prune stale
  folders older than 24 hours without touching active jobs. Tombstones retain
  retryable asset-cleanup status/error evidence if post-commit removal fails;
- kept JSON portability intentionally metadata-only: it never embeds CAD or
  texture bytes, remains exportable for metadata recovery after asset damage,
  and reports locally absent or corrupt UUID assets as skipped;
- moved new pose-template selection to active workpieces from the same
  catalogue while preserving legacy catalogue APIs and immutable bundle/run
  snapshots. The template editor now filters catalogue metadata, presents
  ranked stable grounded orientations beside exact base contours, supports
  direct planar drag/rotation, and enables generation only for an exact current
  server preview. Library and run selection use hash-verified bounded footprint
  cards (with explicit simplification evidence), while the selected version
  loads its full immutable interactive 3D scene. Pre-thumbnail bundles derive
  the bounded card in memory without mutation. Physical-placement confirmation
  clears after any template or transform change; preview submission retries
  transient resource conflicts and discards stale configuration results. New
  manifests omit duplicate raw contours while the hash-verified exact preview
  retains them. Metadata/card reads are bounded, and preview/PDF/individual-
  asset requests hash only their requested declared artifact; strict whole-
  bundle validation remains mandatory for run selection and catalogue delete;
- updated the PoseTemplateCreator pin from `450747b` to `97ddb9b`, retained old
  bundle/six-DoF draft readability, and records canonical geometry revision,
  unit scale, stable-orientation provenance, and the composed
  `Txy * Rz * source_to_placed` transform in new immutable bundles;
- made unit correction archive/confirmation/operator/CAS-gated, regenerate
  from retained source at the cumulative scale, preserve all canonical
  revisions, tolerate optional stable-orientation cache failure, and leave
  every existing template/run snapshot untouched; and
- made run selection a locked, staged transaction across the copied bundle,
  selection record, and run config, with strict record-to-bundle validation,
  live validation, complete rollback on ordinary promotion failure, and a
  durable journal that rolls back or finishes cleanup after process loss.
  Every production run-config writer shares the same per-run cross-process
  lock, recovery rejects symlinked/non-directory ancestors, and exact orphaned
  selection staging names are pruned without touching unrelated hidden files.
  Selection snapshotting is serialized against archive, all published bundle
  trees reject undeclared files and symlinks, and expensive template analysis,
  slicing, PDF rendering, and asset copying occur outside the short catalogue
  publication lock before exact geometry identities are rechecked.

Repository-wide software validation of this addition completed on 2026-07-22:

- 657 non-browser pytest tests, 17 operator-console Playwright tests, and 12
  preview Playwright tests passed (686 total);
- the Workpiece Catalogue browser coverage exercised one orbitable bounded 3D
  detail canvas plus static isometric cards, metadata validation, filters,
  queued upload, import, archive/restore, confirmed deletion, and reference
  conflicts without fetching the full canonical PLY;
- an additional focused Playwright case forced a transient preview-resource
  409 and passed after the automatic retry path;
- Ruff, frontend type checking, frontend lint, the production frontend build,
  wheel/source packaging, `git diff --check`, shell syntax validation, and the
  installer check-only path passed.

No camera, robot, lab service, or physical capture was accessed during this
feature validation. Network access was limited to reading/fetching the named
GitHub upstream. BlenderProc and `pyzed.sl` remain optional unavailable
runtimes on this host, as reported by the successful installer check-only path.

## 2026-07-22 Housekeeping and Evidence Reconciliation

- Reconciled the README, iiwa commissioning documents, current-state summary,
  and remaining-work plan with the retained three-camera `calib00` repeat. The
  earlier two-camera promotion remains historical evidence instead of being
  presented as the current result.
- Advanced the checked-in PoseTemplateCreator gitlink from `450747b` to the
  already-required `97ddb9b`; code, installer, documentation, and submodule now
  agree on one usable revision.
- Removed dead helpers and constants left by retired downstream/fake-mode
  paths, the unused `tqdm` dependency, an unreachable frontend separator and
  its Radix dependency, the replaced cow image, a byte-identical duplicate HRI
  SVG, and the orphaned fixed sync-offset sample. Explicitly retained the
  compatibility readers and entry points whose sunset remains undecided.
- Replaced the last operator-facing static-object-registry recommendation with
  the immutable pose-template/objectless contract and removed misleading
  estimator wording from the frame-writer regression name.
- Moved license-file declarations to the current project metadata contract,
  required a compatible setuptools build backend, and kept the installer import
  smoke aligned with the actual direct dependencies.
- Marked localhost Playwright coverage explicitly. The default pytest run now
  works without optional Chromium; browser validation remains a separate,
  documented `-m playwright` command.
- Added agent guidance for compatibility-aware deletion, reference searches,
  and Vite-owned hashed assets.
- Audited the complete test suite by production contract. Removed nine
  collected cases: one exact robot-profile subset, two catalogue lifecycle/v1
  cases superseded by stronger revision/tombstone coverage, a tautological USB
  forwarding case, three duplicate Flask route/shell checks, and a screenshot
  case that asserted only image dimensions. Preserved the unique assertions in
  the stronger tests, and retained all hardware-safety, compatibility-reader,
  transaction/race, and acquisition-boundary coverage.

Validation after this pass completed on 2026-07-22:

- 650 default pytest tests and all 28 explicitly marked Playwright tests passed
  (678 total);
- the focused maintenance/redundancy sets, Ruff, frontend type checking and lint,
  the production frontend build, shell syntax, installer check-only with both
  pinned submodules, and `git diff --check` passed; and
- an isolated wheel/sdist build completed without the prior setuptools
  deprecation warning and contained the current Python, frontend, static, and
  license/notice assets.

No camera, robot, lab service, or physical capture was accessed during this
housekeeping pass. BlenderProc and `pyzed.sl` remain optional unavailable
runtimes on this host.

## 2026-07-21 Audit and Cleanup

- At that checkpoint, confirmed that no estimator, evaluator, BOP-result
  conversion, or metric implementation remained in tracked production code.
  The later narrow Inspect-only official BOP19 exception is documented above.
- Removed obsolete duplicate launch/ArUco/Sunrise files, definition-only
  helpers/constants, stale completed plans, generated build debris, and the
  misleading downstream-compatibility test name.
- Fixed calibration-workflow lint and run-switch state leakage.
- Required both execution gates for manual IIWA start requests while leaving
  Stop available without motion-start gates.
- Made live-preview capability explicit and disabled unsupported ZED preview
  controls before a doomed background job can be queued.
- Reduced the iiwa calibration capture profile to 60% of requested Cartesian
  speed (8–45 mm/s), lowered repositioning and central orientation speeds,
  applied 3% acceleration/jerk limits to every motion, and added a 1.5-second
  vibration dwell after every leg.
- Verified the pinned PoseGridGen and PoseTemplateCreator checkouts and
  packaged pose-template/backend/UI contents.
- Retired the static object registry, bundled sample models, legacy run-setup
  selector, Cell registry preview/assets, and BlenderProc/BOP fallback paths;
  object-bearing runs now flow only through immutable pose-template bundles.
- Hardened every production IIWA START entry point with fresh robot-and-camera
  acknowledgements, made the UDP pose receiver refuse prior raw evidence before
  network I/O, added finite first-packet/inter-packet timeouts with terminal
  manifest states and unique partial evidence, and kept reusable sequence and
  capture plans free of execution authorization.
- Hardened eye-in-hand attempts so RealSense SDK `global_time` sensor exposure
  timestamps pair with robot host-wall timestamps, with no timestamp-source
  fallback. The original contract used a 20 ms maximum nearest-pose delta;
  current v3 uses a 20 ms warning and 150 ms hard boundary. The explicit
  fixed-zero baseline remains available, and new guided attempts can apply the
  evidence-gated per-camera auto offset described in Current State. Added
  spatial/campaign target support, rotation-axis rank checks before and after
  pruning, per-motion balanced fitting, and full-input validation.
- Made `inverse_brown_conrady` forward-OpenCV-compatible only for finite,
  exact-zero coefficients. Compatible factory projection remains selected;
  factory/manual comparison and rejection evidence remains immutable, and a
  gated manual profile is required when factory projection is unusable.
- Added deterministic multi-camera ranking over complete same-PnP/same-
  extrinsic bundles. Every individual candidate must pass, pairwise stationary
  companion closure must remain within 10 mm / 5°, and bundles within 0.01 of
  the best normalized mean individual score are ordered by normalized closure.
  Six-decimal normalized comparison suppresses physically meaningless solver
  dust before canonical method tie-breaking. Ranking/promotion fail closed when
  no complete common bundle passes.
- Added a Run Setup camera enable control. Disabled cameras retain identity,
  mounting/orientation metadata, and profile selection but are excluded from
  capture/preflight, calibration, Cell, and rewrite-gate expectations.
- Distinguished physically detected sensors from SDK-addressable,
  capture-ready sensors. USB-descriptor-only RealSense records remain visible
  as diagnostic evidence but no longer satisfy expected counts, preflight, or
  hardware-snapshot selection. SDK-enumerated RealSense devices with a known
  USB major version below 3 are likewise blocked before capture; status reports
  the affected serial and transport descriptor. Optional SDK-recommended
  firmware metadata is retained as warning-only troubleshooting evidence and
  never drives an automatic firmware change.
- During a pre-fix web-route diagnostic, a string-valued gate request
  accidentally queued possible IIWA START job `0a4ec1902719`. Its local
  workload returned code 1 and retained no send confirmation, so delivery is
  unverified and more likely did not occur. It produced no camera frames or raw
  robot-pose artifact, and no `STOP` was sent. The route now rejects non-boolean
  execution gates before normalization.

For the 2026-07-21 baseline, software validation completed. The two-camera
physical `calib00` capture produced an explicitly promoted common bundle and
passing historical calibration-validation evidence:

- 564 non-browser pytest tests, 15 operator-console Playwright tests, and 12
  preview Playwright tests passed (591 total);
- Ruff, frontend type checking, frontend lint, the production frontend build,
  and `git diff --check` passed; and
- the installer check-only path, wheel/sdist build, packaged-asset audit, and
  installed-wheel Flask smoke passed. The production build's approximately
  937 kB lazy Cell chunk remains the optional P5 performance item.

## Remaining Work

All unfinished tasks, dependencies, safety constraints, and exit criteria now
live in [REWRITE_REMAINING_WORK.md](REWRITE_REMAINING_WORK.md). Keep that file
and this short status snapshot current; completed plan documents are available
through Git history.
