# Architecture and boundaries

PoseTestBot is deliberately acquisition-first. Ownership boundaries are part
of the data-integrity and credential-isolation model, not merely deployment
choices.

## Component topology

```text
browser
  │ same-origin HTTP
  ▼
Flask operator API ─────▶ LocalJobRunner ─────▶ acquisition/calibration scripts
  │                              │
  │                              └────────────▶ run-owned logs and artifacts
  │
  ├────────▶ approved local run roots
  │            working_data/
  │            /mnt/working_data_ssd
  │
  └────────▶ loopback controller client (optional)
                    │ authenticated server-side request
                    ▼
             posetestbot-cluster companion
             SSH / archives / SLURM / estimators / BOP19 result creation
```

The browser never receives the controller token, cluster credential, remote
path, container command, or scheduler argument. PoseTestBot only returns a
curated browser-safe controller view.

## Backend modules

| Area | Primary modules | Persistence |
| --- | --- | --- |
| Run configuration and planning | `posetestbot.pipeline.*` | Run root |
| Sensor acquisition | `posetestbot.sensors.*` | Raw sensor directories and sidecars |
| Robot integration | `posetestbot.robot.*`, `iiwa/` applications | Raw robot pose stream |
| Synchronization | `posetestbot.sync.non_destructive`, `.quality` | Derived synchronized frames and reports |
| Calibration | `posetestbot.calibration.*` | Attempt evidence, promoted profiles, immutable input snapshots |
| Workpieces | `posetestbot.pose_templates.catalog` | Global JSON catalogue and managed assets |
| Pose templates | remaining `posetestbot.pose_templates.*` | Immutable global bundles and run snapshots |
| BOP export | `posetestbot.bop.writer` | `bop/` below the run |
| Inspect evaluation | `posetestbot.bop.evaluation` | `processed/bop_evaluation/` only |
| Web interface | `posetestbot.web.routes.*` | Delegates mutations to domain code or queued jobs |

## Request and job boundary

HTTP handlers may validate, inspect, or perform bounded metadata mutations.
Long-running, CPU/disk-heavy, or hardware-touching work is submitted to
`LocalJobRunner` with declared resources. The response generally uses HTTP
`202` and includes `job_id` plus a job snapshot.

Resource declarations serialize incompatible work. Camera jobs claim camera
resources; physical capture also claims robot and disk resources. A browser
navigation does not cancel submitted work.

## Filesystem boundary

Web paths are normalized and checked in `posetestbot.web.security`.

- Run paths are confined to the repository `working_data/`,
  `/mnt/working_data_ssd`, and explicitly appended
  `POSETESTBOT_WEB_RUN_ROOTS` entries.
- Run/output parameters remain below the selected run.
- Repository-scoped inputs remain below the repository.
- Extra input roots are opt-in through `POSETESTBOT_WEB_INPUT_ROOTS`.

Relative paths are resolved by their declared scope before containment is
checked. API clients must not rely on `..`, symlinks, or absolute paths to
escape these roots.

## Acquisition boundary

The pipeline ends at a validated BOP dataset. The following do not belong in
this repository:

- FoundationPose, MegaPose, SAM6D, or another estimator runtime;
- estimator-specific input or result conversion;
- direct SSH or SLURM wrappers;
- a general evaluation pipeline stage; or
- cluster secrets and remote filesystem configuration.

The narrow exception is Inspect-only official BOP19 evaluation of an already
exported annotation-bearing dataset and immutable compatible result. It writes
derived evidence only below `processed/bop_evaluation/`.
