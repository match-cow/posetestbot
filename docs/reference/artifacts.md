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
| `bop/posetestbot_bop_frame_map.json` | Current v3 source-to-BOP frame and exported sensor-scene identity |
| `bop/test_targets_bop19.json` | Standard BOP19 targets |
| `bop/models/models_info.json` | Model dimensions and identity |
| `bop/posetestbot_pose_template.json` | Pose-template provenance |
| `bop/posetestbot_instance_map.json` | Current v1 exact `(scene_id, im_id, gt_id)` to run-instance UUID mapping |
| `bop/posetestbot_coco_annotations.json` | Optional COCO view of generated annotations |
| `processed/bop_annotations/generation_report.json` | Optional GT/mask generation evidence |
| `blenderproc_render_plan.json` | Transactional optional GT/mask render plan, dry-run, or skip evidence |
| scene `scene_gt.json` | Pose annotations for `pose` or `pose_and_masks` |
| scene `scene_gt_info.json`, `mask/`, `mask_visib/` | Additional mask/visibility product |

## Inspect-only evaluation

| Path | Contract |
| --- | --- |
| `processed/bop_evaluation/results/<result_id>/result.json` | Immutable result identity, hashes, dataset binding, optional external-job identity, and bounded FoundationPose execution summary |
| `processed/bop_evaluation/results/<result_id>/*.csv` | Immutable validated standard BOP19 estimates |
| `processed/bop_evaluation/results/<result_id>/controller-provenance.json` | Optional sanitized, hash-bound controller/runtime and bounded execution evidence; manual imports do not fabricate it |
| `processed/bop_evaluation/evaluations/<evaluation_id>/` | Request, progress, dataset adapter, official toolkit output, and report |
| `processed/bop_evaluation/evaluations/<evaluation_id>/selected_test_targets_bop19.json` | Immutable locally recomputed target list used for that evaluation; filtered to verified selected sensor scenes when applicable |
| `processed/bop_evaluation/evaluations/<evaluation_id>/robot_consistency_inputs.json` | Immutable robot-consistency snapshot: original export evidence hashes, resolved camera-to-template-base transforms, and stable instance identities |
| `processed/bop_evaluation/evaluations/<evaluation_id>/robot_consistency.json` | IPD MVD/ADD in mm, matching coverage, unavailable reasons, all sensor/instance scores and per-view errors; bound to dataset/result/input hashes |
| `processed/bop_evaluation/comparisons/<comparison_id>/request.json` | Immutable offline Inspect comparison sources and exact run-local hashes; output belongs to one evaluated input run |
| `processed/bop_evaluation/comparisons/<comparison_id>/comparison.json`, `frames.csv` | GT/RC errors, rejected/missing coverage, correlations, threshold diagnostics, repeated-trajectory differences and embedded selected image evidence |
| `processed/bop_evaluation/comparisons/<comparison_id>/index.html`, `consistency_vs_gt.png`, `consistency_vs_gt.svg`, `manifest.json` | Offline interactive desktop comparison, standalone scientific figures and publication hashes; no source dataset changes |
| `processed/bop_evaluation/visualizations/<export_id>/request.json` | `bop_inspection_export.v1`: frozen layer/background/filter/FPS settings, ordered frame IDs, dataset/result identities, and source file identities |
| `processed/bop_evaluation/visualizations/<export_id>/job.json`, `progress.json` | Local job identity and rendering counts/state/errors; Jobs recovers the export URL |
| `processed/bop_evaluation/visualizations/<export_id>/manifest.json`, `output.json` | Content hashes for sources and completed output, source/output dimensions, frame count, encoding and duration; download integrity binding |
| `processed/bop_evaluation/visualizations/<export_id>/<export_id>.zip` or `.mp4` | Atomically published PNG archive or H.264 video; failed/canceled partials cannot be downloaded |

Evaluation never mutates raw capture or the exported dataset and is not an
acquisition stage. Result ZIP packages are generated deterministically on
demand after rechecking retained hashes; they are downloads, not additional
stored run artifacts. Pose Results writes visualizations only on explicit
export. ZIP entries are numerically ordered `<scene_id:06d>/<im_id:06d>.png`
plus `manifest.json`. Video carries the same rendered frames, without labels
or controls, and retains its manifest beside the MP4. Completed exports remain
downloadable after later source changes, using their retained integrity hashes.
Each request runs once (`.started` records worker ownership); partial files from
a force-killed worker may remain hidden on disk but are never downloadable.
