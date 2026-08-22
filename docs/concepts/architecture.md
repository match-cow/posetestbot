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
| Run configuration and fixed orchestration | `posetestbot.pipeline.run_config`, `.orchestration`, `.capture_*` | Run root |
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
resources; physical capture also claims robot and disk resources. Claims are
serialized against persisted jobs under an interprocess lock, including
hierarchical parent/child conflicts, so overlapping service processes cannot
double-allocate physical resources. A browser navigation does not cancel
submitted work.

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

Complete derived directory generations are published through a durable
rollback journal. Replacements are limited to real sibling directories,
overlapping parent directories are locked across processes, and every
participating parent receives the same transaction record before the first
destination is moved. Recovery on the next replacement restores the complete
prior generation unless every new directory was installed and the commit
decision was made durable; after that decision it retains the complete new
generation. Journal entries bind the exact parent and old/new directory
identities, so a symlink or unrelated directory appearing at a staging,
destination, or backup path fails closed instead of being moved or deleted.

## Acquisition boundary

The acquisition boundary ends at a validated BOP dataset. The following do not belong in
this repository:

- FoundationPose, MegaPose, SAM6D, or another estimator runtime;
- estimator-specific input or result conversion;
- direct SSH or SLURM wrappers;
- a general evaluation stage; or
- cluster secrets and remote filesystem configuration.

The narrow exception is Inspect-only official BOP19 evaluation of an immutable
compatible result against a verified `pose_and_masks` export. That export must
declare complete BlenderProc annotations and BOP19 evaluation capability in
`bop_export_manifest.v5`, with matching per-scene `scene_gt.json`,
`scene_gt_info.json`, and complete full/visible instance-mask evidence below
`mask/` and `mask_visib/`. Pose-only ground truth is not evaluation-ready.
Evaluation writes derived evidence only below
`processed/bop_evaluation/`.
