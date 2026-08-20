# Artifact index

Run artifacts are governed evidence, not loose cache files. Writers validate
inputs, write atomically where required, and update `dataset_manifest.json`.
Raw acquisition evidence is preserved; processing writes derived output.

## Run, preflight, and capture

| Path | Contract |
| --- | --- |
| `run_config.json` | Strict `run_config.v4` intent and hardware/data contract |
| `dataset_manifest.json` | Run-level artifact ledger and provenance bindings |
| `run_preflight_report.json` | Current `run_preflight.v2` configuration/readiness evidence |
| `hardware_status_report.json` | Read-only hardware snapshot |
| `capture_plan.json` | Canonical camera/robot plan |
| `capture_plan_preflight_report.json` | Fresh plan-specific checks |
| `capture_execution_plan.json` | Gated supervised execution plan |
| `capture_execution_status.json` | Live/recoverable execution state |
| `capture_execution_report.json` | Child-process and completion validation |
| `capture_execution_logs/` | Per-child stdout/stderr evidence |

Capture completion requires every enabled sensor to have balanced nonempty
RGB/depth/current metadata, strict timestamp evidence, a nonempty current
robot-pose stream, successful children, and clean resource release.
`run_preflight_report.json` embeds `selected_sensor_readiness.v1`: one bounded,
non-recording configured-stream probe per enabled selected camera. A prior
`run_preflight.v1` report does not authorize capture. The capture worker carries
the successful fresh probe into `capture_plan_preflight_report.json` before
starting the fixed recipe.

## Raw and synchronized evidence

| Path | Contract |
| --- | --- |
| per-sensor `rgb/`, `depth/` | Preserved raw PNG frames |
| per-sensor `frame_metadata.jsonl` | Current timestamp and frame identity records |
| per-sensor `cam_K.txt` | Camera intrinsic matrix consumed by calibration and export |
| per-sensor `depthscale.txt` | Positive raw-depth-to-millimetre scale |
| per-sensor `camera.json`, `camera_data.json` | Current intrinsic and resolution evidence, with distortion/projection provenance when supplied |
| `raw_robot_ee_poses.json` | Strict `robot_pose.v1` packets with run/frame provenance |
| `processed/robot_pose_cadence_report.json` | Optional derived delivery-cadence evidence |
| per-sensor `match_robot_ee_poses.json` | Non-destructive nearest-pose matches |
| `sync_report.json` | Matching decisions and exclusions |
| `sync_quality_report.json` | In-motion coverage, deltas, packet loss, and blockers |
| `processed/rectified/<sensor>/` | Derived rectified RGB-D frames, metadata, and projection sidecars |
| `camera_rectification_report.json` | Rectification source/output and per-sensor provenance |

Lead-in and tail camera frames remain raw context. They are not counted as
failed in-motion matches.

## Calibration

| Path | Contract |
| --- | --- |
| `calibration_target.json` | Run-owned selected target bundle and hashes |
| `calibration_profile_selection.json` | Current v2 per-sensor reusable selection |
| `processed/calibration_inputs/<bundle_sha256>/calibration_profiles.json` | Exact selected extrinsic-profile snapshot |
| `processed/calibration_inputs/<bundle_sha256>/intrinsic_calibration_profiles.json` | Exact selected intrinsic-profile snapshot |
| `processed/calibration/<attempt_id>/request.json` | Immutable attempt intent |
| `processed/calibration/<attempt_id>/progress.json` | Five-phase attempt status |
| `processed/calibration/<attempt_id>/intrinsic_comparison.json` | Factory/OpenCV evidence |
| `processed/calibration/<attempt_id>/time_offset_search.json` | Explicit fixed-zero or automatic timing evidence |
| `processed/calibration/<attempt_id>/pnp_candidates.json` | Current PnP evidence |
| `processed/calibration/<attempt_id>/extrinsic_candidates.json` | Mount-aware transform candidates |
| `processed/calibration/<attempt_id>/ranking.json` | Immutable calculation-time candidate ranking/recommendation; current promotion eligibility is derived without rewriting it |
| `processed/calibration/<attempt_id>/checks.json` | Blocking checks and retained warnings |
| `processed/calibration/<attempt_id>/candidate_profiles.json` | Profiles eligible for review/promotion |
| `calibration_profiles.json` | Explicitly promoted `calibration.v2` profiles, including retained multi-camera consistency warnings |
| `intrinsic_calibration_profiles.json` | Explicitly promoted intrinsic profiles and projection evidence |
| `processed/calibration/camera_ee_transform_from_calibration_profiles.json` | Derived BlenderProc camera transform bound to selected profiles |

There are no root-level preflight/observations/candidates/solver/validation
artifacts from the removed staged calibration implementation.

## Reusable libraries and run snapshots

| Path | Contract |
| --- | --- |
| `object_catalog/object_catalog.json` | Serialized global catalogue and tombstones |
| `object_catalog/objects/<uuid>/` | Retained source, canonical geometry revisions, texture, and bounded derived caches |
| `object_catalog/revisions/` | Numbered atomic catalogue manifests |
| `pose_templates/<uuid>/pose_template_bundle.json` | Immutable published bundle |
| `pose_templates/<uuid>/pose_template_preview.json` | Exact planar preview |
| `pose_templates/<uuid>/pose_template_thumbnail.json` | Bounded card-read cache |
| `pose_template_selection.json` | Run-owned immutable bundle selection |
| `.pose_template_selection.transaction.json` | Durable replacement journal while a selection changes |
| `object_instances.json` | Run-owned object-instance mapping |

## BOP export and optional annotations

| Path | Contract |
| --- | --- |
| `bop/bop_export_manifest.json` | Current `bop_export_manifest.v5` and capability declaration |
| `bop/posetestbot_bop_frame_map.json` | Source-to-BOP frame identity |
| `bop/test_targets_bop19.json` | Standard BOP19 targets |
| `bop/models/models_info.json` | Model dimensions and identity |
| `bop/posetestbot_pose_template.json` | Pose-template provenance |
| `bop/posetestbot_instance_map.json` | Run instance to BOP object mapping |
| `bop/posetestbot_coco_annotations.json` | Optional COCO view of generated annotations |
| `processed/bop_annotations/generation_report.json` | Optional GT/mask generation evidence |
| `blenderproc_render_plan.json` | Transactional optional GT/mask render plan, dry-run, or skip evidence |
| scene `scene_gt.json` | Pose annotations for `pose` or `pose_and_masks` |
| scene `scene_gt_info.json`, `mask/`, `mask_visib/` | Additional mask/visibility product |

## Inspect-only evaluation

| Path | Contract |
| --- | --- |
| `processed/bop_evaluation/results/<result_id>/` | Immutable imported/simulated CSV, validation result, and provenance |
| `processed/bop_evaluation/evaluations/<evaluation_id>/` | Request, progress, dataset adapter, official toolkit output, and report |

Evaluation never mutates raw capture or the exported dataset and is not an
acquisition stage.
