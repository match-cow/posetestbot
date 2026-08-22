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
| `capture_execution_plan.json` | Current `capture_execution_plan.v2`, bound to the complete accepted `run_config.v4` digest |
| `capture_execution_status.json` | Current `capture_execution_status.v2` live/final state |
| `capture_execution_report.json` | Current `capture_execution_report.v2` child-process/config/archive evidence plus embedded `capture_completion.v1`; dataset processing requires `succeeded`/`ok` and rebuilds completion before derived work |
| `capture_execution_logs/<execution_id>/` | Immutable per-attempt plan, status, report, and per-child stdout/stderr; retries use a new identity and retain prior diagnostics |

Capture completion requires every enabled sensor to have balanced nonempty
RGB/depth/current metadata, decoded supported pixels at the configured
dimensions, camera sidecar dimensions/intrinsics/depth scale that agree with
one another and those pixels, positive focal lengths, a canonical intrinsic
bottom row, consistent SDK distortion/projection evidence, strict timestamp
evidence, a nonempty current
robot-pose stream with accepted terminal `end` packet evidence, successful
children, and clean resource release.
Capture authorization freezes the full semantic run configuration. Its SHA-256
is rechecked at every camera/receiver/completion boundary and the accepted
snapshot is used for completion validation; a changed configuration fails while
preserving raw evidence.
`run_preflight_report.json` embeds `selected_sensor_readiness.v1`: one bounded,
non-recording configured-stream probe per enabled selected camera. A prior
`run_preflight.v1` report does not authorize capture. The capture worker carries
the successful fresh probe into `capture_plan_preflight_report.json` before
starting the fixed recipe.

## Raw and synchronized evidence

| Path | Contract |
| --- | --- |
| per-sensor `rgb/`, `depth/` | Preserved raw PNG frames |
| per-sensor `frame_metadata.jsonl` | Current frame identity plus required positive host-receipt/wall timestamps and optional positive sensor/depth timestamps; the writer rejects invalid scalar metadata before creating frame outputs, and processing still enforces the promoted profile's exact timestamp source |
| per-sensor `cam_K.txt` | Camera intrinsic matrix consumed by calibration and export |
| per-sensor `depthscale.txt` | Positive raw-depth-to-millimetre scale |
| per-sensor `camera.json`, `camera_data.json` | Current intrinsic and resolution evidence, with distortion/projection provenance when supplied |
| `.raw_robot_ee_poses.claim.json` | Hidden current v2 receiver reservation bound to the run and owning process; its presence is attempt evidence, not a canonical pose stream |
| `raw_robot_ee_poses.claim.<claim_id>.recovered.json` | Retained inactive reservation when a receiver died before publishing its packet journal; accompanied by zero-pose partial evidence and blocks run reuse |
| `raw_robot_ee_poses.journal.<claim_id>.jsonl` | Exclusively locked append-only `raw_robot_ee_poses_journal.v1` records and durable-prefix commit markers while reception is active |
| `raw_robot_ee_poses.journal.<claim_id>.recovered.jsonl` | Retained inactive journal after recovery of a nonterminal committed prefix |
| `raw_robot_ee_poses.partial.<...>.json` | Readable `raw_robot_ee_poses_partial.v1` materialization of every validated journal-committed pose after cancellation, error, or abrupt loss |
| `raw_robot_ee_poses.json` | Strict `robot_pose.v1` packets with run/frame provenance and consistent `sequence`, `sequence_delta`, and `estimated_packets_lost`; the final pose carries the accepted `stream_end_source_packet` and its terminal loss delta |
| `processed/robot_pose_cadence_report.json` | Optional derived delivery-cadence evidence |
| `processed/.synchronization.lock` | Persistent per-run interprocess lock shared by managed sync, sync quality, and optional BlenderProc prepare/render CLIs; coordination only, not dataset evidence |
| per-sensor `match_robot_ee_poses.json` | Non-destructive nearest-pose matches |
| `processed/synchronized/<sensor>/sync_report.json` | `sync_report.v4` matching decisions, one run-generation ID, exact raw RGB-D/sidecar/robot-pose fingerprints, and exact synchronization-owned output evidence |
| `sync_quality_report.json` | In-motion coverage, deltas, mandatory packet-loss audit, and blockers; missing or malformed packet-loss evidence is an error |
| `processed/rectified/<sensor>/` | Exact enabled-sensor rectified RGB-D output with additive metadata, matched-pose bytes, projection sidecars, and source/output fingerprints |
| `camera_rectification_report.json` | Managed-canonical or explicit diagnostic mode, fixed source/output roots, and per-sensor provenance |

Lead-in and tail camera frames remain raw context. They are not counted as
failed in-motion matches.

The canonical robot-pose artifact is absent during motion. The receiver fsyncs
journal commit markers after at most 32 accepted poses or 250 ms and publishes
`raw_robot_ee_poses.json` atomically only after a terminal commit includes the
accepted `motion=end` packet. Startup recovery never treats the reservation as
pose evidence: it either finalizes a terminal journal or retains the committed
prefix in both partial JSON and a recovered journal. Claim, journal, partial,
and canonical artifacts all freeze the run against configuration replacement
or another physical capture.

The managed synchronization generation requires the exact enabled-sensor set
at its configured direct-child run paths (not nested same-name lookalikes),
serializes synchronization, sync-quality, and optional BlenderProc
prepare/render CLI transactions through one persistent run-level processing
lock, and replaces the owned
`processed/synchronized/` root only after every sensor succeeds. A later sensor
or promotion failure therefore leaves the previous coherent derived generation
intact. Explicit subsets are diagnostic-only and cannot target the canonical
root. Its output fingerprint covers the synchronized RGB-D, current camera
sidecars, derived frame metadata, and matched robot poses. Deliberate downstream
`blenderproc/` and `masks/` namespaces and the persistent
`.posetestbot-directory-replace.lock` are validated but excluded, so
publishing optional annotations does not stale the synchronization generation.
Unexpected root artifacts still fail closed. Rectification and BOP export have
fixed managed roots; diagnostic APIs cannot overwrite arbitrary existing trees.

Directory-generation publication may temporarily create transaction-owned
controls beside the affected directories:
`.posetestbot-directory-replace.<transaction_id>.json` and
`.<destination>.posetestbot-backup.<transaction_id>.<index>`. The journal is
replicated to every participating parent and removed only after rollback or
commit cleanup is durable. A persistent
`.posetestbot-directory-replace.lock` is an interprocess coordination file, not
dataset evidence; readers must ignore it. If a process is terminated mid-rename,
the next directory replacement touching any participating parent performs
deterministic recovery before accepting another staged generation.

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
| `processed/calibration/<attempt_id>/promotion_request.json` | Explicit v2 approval snapshot with size/SHA-256 bindings for every material attempt review input; promotion fails closed if any bound file, intrinsic projection, or target-bundle member is missing or changed |
| `processed/calibration/<attempt_id>/promotion.json` | Durable approval/job state: `approved` is the request-pair commit marker, `queued` requires a bound job ID, and running/final states retain the approved review-input bindings |
| `.calibration_promotion.transaction.json` | Hidden durable multi-artifact promotion journal; `prepared` recovery restores the prior generation, while `committed` recovery retains and verifies the complete new generation before cleanup |
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
