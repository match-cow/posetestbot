# PoseTestBot Clean-Break Streamlining

## Summary

- Reduce PoseTestBot to the two canonical guided outcomes—camera calibration
  and object-dataset acquisition—plus the support pages required by those
  workflows.
- Permanently delete roughly 100 GB of historical run data and transient
  runtime history. Do not archive or migrate it.
- Remove historical schemas, protocol fallbacks, aliases, generic pipeline
  controls, rewrite-era gates, and obsolete calibration implementations.
  Current-only readers must fail closed on old inputs.
- Preserve safety, provenance, immutable libraries, non-destructive handling
  of future captures, optional GT generation, cluster handoffs, and
  Inspect-only BOP evaluation.

## Data Reset

- Before deletion, stop the web service/job runner and recheck that no capture,
  hardware, or background job is active. Resolve every target through
  approved-root containment checks; do not use broad root deletion.
- Permanently remove:
  - Repository runs/remnants: `test20260726_BOPv5`, `test20260725_02`,
    `test20260725_03`, `test20260725_04`, and the
    `calib00_test20260724` symlink.
  - SSD runs: `calib00_test20260724`, `objecttest20260805`,
    `test20260728_CalibMobile`, `test20260805`,
    `staticcalibrationtest20260817v3`, `statictest20260803`,
    `test20260728_CalibStatic`, `testtestv220260817`, and
    `staticcalibrationtest20260817`.
  - The verified PoseTestBot entries and matching metadata currently under
    `/mnt/working_data_ssd/.Trash-1000`.
  - Contents of local job history, monitor sessions, sensor previews/snapshots,
    and catalogue staging; recreate required empty service directories.
  - Ignored caches and generated output such as `site/`, `frontend/dist`,
    pytest/ruff caches, and Python bytecode.
- Preserve and validate before and after deletion:
  - `object_catalog/`, `pose_templates/`, `calibration_targets/`, and
    `sensor_aliases.json`.
  - `/mnt/working_data_ssd/posetestbot_cluster_state`, the companion
    repository, lock files, and filesystem-owned directories.
  - Development/runtime installations including `.venv`,
    `frontend/node_modules`, the pinned BOP Toolkit runtime, and `third_party/`.
- Reset known PoseTestBot browser-storage keys for selected runs, custom
  folders, workflow sessions, and the obsolete robot target. With no runs, the
  UI starts from an unconfigured `working_data/test_run`; Run Folders may
  choose another contained path before Workflow creates a fresh configuration.
- Verify that neither approved root contains a remaining run marker,
  historical run symlink, or PoseTestBot trash entry. The next physical
  dataset must use a newly captured and promoted calibration.

## Implementation Changes

### Canonical workflow surface

- Keep Dashboard, Workflow setup/calibration/dataset, Devices, Cell View,
  Calibration Targets, Workpiece Catalogue, Pose Templates, Run Folders, Jobs,
  Pose Estimation, and BOP Evaluation.
- Keep optional GT/mask generation as an explicitly optional dataset step.
- Delete Advanced Tools, `StageForm`, historical workflow URL aliases, manual
  stage cards, implementation-stage IDs, and generic recommendation/workflow
  metadata.
- Remove the sidebar's “Trusted lab network” card. Retain the actual
  network-exposure warning in deployment documentation.
- Keep required prerequisites, safety acknowledgements, disabled-action
  reasons, state scope, background-job persistence, and Jobs handoffs visible.
  Restrict HelpTips to domain-specific concepts; move deeper explanations to
  published documentation.

### Focused orchestration

- Delete the generic stage/sequence registry, the nine noncanonical sequences,
  `/pipeline/*` discovery/execution machinery, and
  `run_pipeline_sequence.py`.
- Replace them with fixed orchestration recipes:
  - Capture: plan → preflight → execution plan → supervised execution →
    capture-completion validation.
  - Dataset processing: non-destructive sync → sync quality → rectification →
    calibrated BOP export.
  - Calibration: the existing attempt-based solve, review, and explicit
    promotion path.
- Provide one safe plan-only CLI, `scripts/plan_capture.py <run>`, and one fixed
  processing CLI, `scripts/process_dataset.py <run>`. Both share the same
  orchestration libraries as web jobs.
- Keep `LocalJobRunner`, resource locking, capture-plan modules, raw-data
  preservation, job recovery, containment checks, and immutable transaction
  journals.

### Rewrite and calibration cleanup

- Delete rewrite-gate/status modules, scripts, stages, reports,
  recommendations, and schema identifiers.
- Fold unique full-capture checks into capture completion: every enabled sensor
  must have balanced nonempty RGB/depth/current metadata, strict timestamp
  evidence, a nonempty current robot-pose stream, successful child processes,
  and clean resource release.
- Let calibration promotion, sync quality, BOP writing, and BOP evaluation
  remain the authoritative validators for their respective outputs.
- Delete the old observations → candidates → solver → validation pipeline,
  `legacy_static`, its web routes/scripts/root-level artifacts, and unused
  ArUco coverage/manual-stage wrappers.
- Move the transform helpers still used by `attempt_solver` and time-offset
  estimation into a current neutral calibration utility; retain only ArUco
  detection helpers used by calibration attempts.

### Current-only contracts

- Introduce `run_config.v4`, remove the generic `pipeline` section, and accept
  only v4. No migration reader is added.
- Require exact registry sensor identifiers, explicit capture intent, explicit
  BOP annotation mode, current frame metadata and camera sidecars,
  `calibration_profile_selection.v2`, current Cell responses, monitor v2,
  cluster setup v2, and controller-advertised estimators.
- Remove filename-only frame discovery, missing-timestamp fallbacks, short
  sensor aliases, calibration-selection v1, synthetic cluster defaults,
  monitor/cell response fallbacks, clipboard fallback, and run-folder
  compatibility symlinks/alias history.
- Remove legacy arbitrary 6-DoF pose-template loading, old
  PoseTemplateCreator revisions, implicit geometry revisions, and
  oversized-manifest/thumbnail fallbacks; retain stable-orientation authoring
  and the current immutable bundles.
- Do not rename or remove a current schema merely because it ends in `.v1`.
  Keep BOP19/result compatibility, OpenCV projection compatibility,
  calibration-to-camera identity checks, path validation, and
  hardware-availability diagnostics; these are current integrity contracts
  rather than backward compatibility.

### IIWA controls

- Keep both Start and Stop on Dashboard and remove the duplicate Devices
  control card. Devices becomes sensor-focused.
- Remove browser-editable IP/port state and always display/use the sole lab
  profile at `172.31.1.147:30300`.
- Start continues to require fresh robot and camera acknowledgements. Present
  Stop as “Stop / exit idle IIWA program,” with a permanent visible warning
  that it cannot interrupt active motion and is not an emergency stop.
- Use only structured `robot_command.v1` commands and strict `robot_pose.v1`
  packets with run ID and reference-frame provenance. Remove legacy UDP
  commands, protocol switches, inferred run IDs, and pose-packet fallbacks from
  Python and all three Java applications.
- Do not send Stop from capture/calibration orchestration. Before the next
  physical use, compile and deploy the current structured-protocol Sunrise
  applications; an older deployed application is intentionally unsupported.

## Public Interfaces and Documentation

- Keep `GET/POST /run-config`, standard `/jobs` APIs, capture status/stop APIs,
  calibration attempts, reusable libraries, BOP annotation/evaluation, and
  cluster APIs.
- Add these purpose-specific queued APIs:
  - `POST /preflight/jobs` with `run_root`.
  - `POST /capture/jobs` with `run_root`,
    `intent: "calibration" | "dataset"`, and both physical-execution
    acknowledgements.
  - `POST /dataset-processing/jobs` with `run_root`.
  - `POST /robot/commands` with either current Start acknowledgements or
    `confirm_idle_program_exit` for Stop; no target override fields.
- Remove `/pipeline/stages*`, `/pipeline/sequences*`, `/pipeline/workflows`,
  `/pipeline/recommendations`, `/pipeline/run*`, `/pipeline/preflight`,
  `/capture-plan*`, the old
  `/calibration/{preflight,observations,candidates,solver,validation}` routes,
  `/run-command`, and the compatibility `POST /ui/calibrations` alias. Keep
  `GET /ui/calibrations` and `POST /ui/calibrations/select`.
- Completely delete `ConsoleGuide`. Replace the guide and redundant repository
  icon with one clearly labelled external **Documentation** link to
  <https://match-cow.github.io/posetestbot/>.
- Delete rewrite-progress/history and superseded dated proposal/validation
  pages. Transfer the five still-open physical tasks into a concise
  `docs/COMMISSIONING.md`: IIWA deployment/cadence, camera-service acceptance,
  five-sensor capture, RealSense depth-scale recheck, and physical
  pose-template review.
- Update AGENTS, README, INSTALL, architecture, workflow, CLI, API, artifact,
  and run-config documentation to describe only the resulting current system.
  Regenerate and check the HTTP route reference, rebuild MkDocs, and rebuild
  the tracked Vite bundle rather than editing hashed assets.

## Tests and Acceptance

- Replace compatibility tests with strict rejection tests for v3 run configs,
  old calibration selections, legacy robot packets/commands, missing frame
  metadata, old monitor/cluster/cell shapes, removed aliases, and removed
  routes.
- Add end-to-end simulated coverage from empty run roots through v4 creation,
  reusable target selection, calibration attempt/promotion, capture
  planning/completion validation, dataset processing, BOP export, optional
  annotations, and evaluation handoff.
- Verify library validation and selection after the purge, run-folder moves
  without symlinks, path containment, immutable snapshots, tombstones,
  transaction recovery, job resources, and future raw-data preservation.
- Add Playwright checks at 1920×1080 and 1440×900 for the exact documentation
  link, absent Console Guide/Trusted-network/Advanced controls, Dashboard-only
  Start/Stop with warnings, sensor-focused Devices, both guided workflows,
  visible blockers, Jobs handoffs, and no primary-layout overflow.
- Run:
  - `UV_CACHE_DIR=/tmp/uv-cache uv run pytest`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`
  - Frontend typecheck, lint, and `bun run build`
  - `git diff --check`
  - HTTP route-reference generation and `--check`
  - Strict MkDocs build, GitHub Pages tests, and the repository's desktop
    Playwright suites
- No camera access, robot commands, physical capture, dependency installation,
  or Sunrise deployment occurs as part of the repository implementation
  without separate operator authorization.
