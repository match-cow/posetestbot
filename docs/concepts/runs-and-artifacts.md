# Runs and artifact lineage

A run directory is the reproducibility boundary. It owns configuration,
selected reusable inputs, raw evidence, derived processing evidence, and the
exported dataset.

## Storage roots

The web console always approves these roots:

- `<repository>/working_data/`
- `/mnt/working_data_ssd`

`POSETESTBOT_WEB_RUN_ROOTS` appends additional approved roots; it does not
replace the defaults. Every API path still passes the same containment checks.

## Data classes

| Class | Examples | Mutation policy |
| --- | --- | --- |
| Run intent | `run_config.json`, `capture_plan.json` | Replaced only through validated writers; manifest records the change |
| Raw evidence | RGB/depth PNGs, `frame_metadata.jsonl`, `raw_robot_ee_poses.json` | Preserve; never rename/delete the only copy during sync or export |
| Immutable input snapshot | calibration selection/bundle, pose-template selection, object instances | Hash-bound to the run so later library changes cannot alter it |
| Derived evidence | sync, calibration, render, and validation reports | Reproducible output, normally below `processed/` |
| Dataset export | `bop/` | Bound to selected frames, calibration, models, annotations, and provenance |
| Inspect evaluation | `processed/bop_evaluation/` | Immutable inputs/results plus derived official-toolkit evidence |

## Manifest contract

`dataset_manifest.json` is the run-level artifact ledger. Domain writers update
it when they create or replace governed artifacts. Consumers should validate
the recorded path, size/hash where present, schema version, and upstream input
binding rather than treating file existence as sufficient evidence.

## Reusable inputs become run-owned

Global catalogues are authoring surfaces. A dataset run does not hold a live
pointer to mutable global state.

- Calibration selection produces exact combined profile snapshots below
  `processed/calibration_inputs/<bundle_sha256>/` and a selection manifest.
- Pose-template selection creates a run-owned `pose_template_selection.json`
  and `object_instances.json`.
- Existing run snapshots remain valid when a global workpiece or template is
  archived or permanently deleted.

## Synchronization semantics

The only supported capture synchronization mode is `timestamp_aligned`.
Every camera records its own timestamp evidence and is paired
non-destructively with the robot pose stream.

Quality is evaluated over frames eligible during the robot motion interval.
Lead-in and tail frames are retained raw context; they are not failed matches
and do not belong in a `matched / total raw` quality ratio. Relevant evidence
includes in-motion coverage, timestamp fallback/missing counts, nearest-pose
threshold rejections, pose delta, packet loss, and unexplained exclusions.

See the [artifact index](../reference/artifacts.md) for canonical filenames.
