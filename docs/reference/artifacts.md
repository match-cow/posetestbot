# Artifact index

Canonical artifact names are interfaces between acquisition stages, the web
console, validation gates, and downstream BOP consumers. Paths below are
relative to a run unless stated otherwise.

## Run and capture

| Artifact | Role |
| --- | --- |
| `dataset_manifest.json` | Run artifact ledger and provenance |
| `run_config.json` | Validated `run_config.v3` intent |
| `run_preflight_report.json` | Run-level readiness evidence |
| `hardware_status_report.json` | Run-owned hardware/runtime snapshot |
| `capture_plan.json` | Planned camera/robot capture contract |
| `capture_plan_preflight_report.json` | Fresh capture-plan readiness evidence |
| `capture_execution_plan.json` | Gated execution plan |
| `capture_execution_status.json` | Current durable execution state |
| `capture_execution_report.json` | Terminal capture evidence |
| `capture_execution_logs/` | Per-process capture logs |

## Raw and synchronized evidence

| Artifact | Role |
| --- | --- |
| per-sensor `rgb/`, `depth/` | Preserved legacy-compatible raw frame PNGs |
| `frame_metadata.jsonl` | Compact frame timestamp and acquisition sidecar |
| `raw_robot_ee_poses.json` | Original robot pose stream |
| `processed/robot_pose_cadence_report.json` | Optional derived cadence evidence |
| `match_robot_ee_poses.json` | Frame-matched robot poses |
| `sync_report.json` | Non-destructive pairing report |
| `sync_quality_report.json` | In-motion timestamp-alignment quality (`v2`) |

Lead-in/tail frames remain raw context. Their presence is not a synchronization
failure and is not included in the eligible in-motion coverage denominator.

## Calibration

| Artifact | Role |
| --- | --- |
| `calibration_preflight_report.json` | Calibration input/readiness evidence |
| `calibration_target.json` | Selected run-owned target |
| `intrinsic_calibration_profiles.json` | Current exact intrinsic profiles |
| `aruco_detections.json` | Per-sensor target detections |
| `camera_rectification_report.json` | Rectification results/provenance |
| `calibration_observations.json` | Normalized solver observations |
| `calibration_candidates.json` | Candidate transforms/residuals |
| `calibration_profiles_from_observations.json` | Candidate profile projection |
| `calibration_solver_report.json` | Solver method and evidence |
| `calibration_profiles_solved.json` | Solver-produced profiles |
| `calibration_validation_report.json` | Validation checks/warnings/blockers |
| `calibration_profiles.json` | Promoted `calibration.v2` profiles |
| `calibration_profile_selection.json` | Immutable source bundle and per-sensor selection binding |
| `processed/calibration_inputs/<bundle_sha256>/` | Exact combined profile snapshots |
| `processed/calibration/<attempt_id>/` | Intent request, progress, search, candidates, ranking, checks, profiles, and promotion evidence |

## Reusable workpieces and templates

These are global, normally below `working_data/`, rather than run-relative.

| Artifact | Role |
| --- | --- |
| `object_catalog/object_catalog.json` | Serialized `object_catalog.v1` manifest and tombstones |
| `object_catalog/objects/<uuid>/` | Retained source CAD, canonical revisions, and optional texture |
| `object_catalog/revisions/` | Numbered atomic catalogue manifests |
| object `derived/pose_template_orientation_analysis.json` | Reproducible stable-orientation cache |
| object `derived/pose_template_orientation_thumbnail.json` | Bounded card-read cache |
| `pose_templates/<uuid>/pose_template_bundle.json` | Immutable published template bundle |
| template `pose_template_preview.json` | Exact slicing preview |
| template `pose_template_thumbnail.json` | Bounded library-card cache |
| `pose_templates/.deleted/` | Retained template deletion cleanup trees |

Run-owned template artifacts are `pose_template_selection.json`, the hidden
`.pose_template_selection.transaction.json` journal while replacement is in
progress, and `object_instances.json`.

## BOP export and annotations

| Artifact | Role |
| --- | --- |
| `bop/bop_export_manifest.json` | Export inputs, outputs, hashes, and annotation mode |
| `bop/posetestbot_bop_frame_map.json` | PoseTestBot-to-BOP frame identity |
| `bop/test_targets_bop19.json` | Standard target list |
| `bop/models/models_info.json` | BOP object dimensions/diameters |
| `bop/posetestbot_pose_template.json` | Pose-template provenance |
| `bop/posetestbot_instance_map.json` | Run instance to BOP object/scene identity |
| `bop/posetestbot_coco_annotations.json` | Optional compact COCO annotations |
| `processed/bop_annotations/generation_report.json` | Optional GT/mask generation evidence |
| BOP scene `scene_gt.json` | Object poses in pose modes |
| BOP scene `scene_gt_info.json`, `mask/`, `mask_visib/` | Pose-plus-mask evidence |

## Inspect-only evaluation

| Path | Role |
| --- | --- |
| `processed/bop_evaluation/results/<result_id>/` | Immutable imported/simulated CSV, validation result, and provenance |
| `processed/bop_evaluation/evaluations/<evaluation_id>/` | Immutable request, progress, resolved inputs, official toolkit output, and report |

Evaluation never writes into raw capture data and is not a pipeline stage.

## Rewrite gates

The acquisition boundary is summarized by:

- `rewrite_full_capture.v1`
- `rewrite_calibration_validation.v1`
- `rewrite_bop_export_readiness.v1`

```bash
uv run python scripts/run_rewrite_gate.py working_data/example \
  --gate rewrite_full_capture.v1 --write
uv run python scripts/run_rewrite_status.py working_data/example --write
```
