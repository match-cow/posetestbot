# Calibration API

Calibration has two calculation surfaces: reusable target bundles and
intent-level attempts. Promotion writes explicit evidence; it does not silently
replace a reusable profile.

## Calibration targets

| Method and path | Contract |
| --- | --- |
| `GET /calibration-targets/status` | Inspect pinned PoseGridGen source/runtime status and bundle availability |
| `GET /calibration-targets/capabilities` | Return supported target-generation options and limits |
| `POST /calibration-targets/fit` | Validate dimensions/options and report page fit; validation failures may return `422` |
| `POST /calibration-targets/preview` | Render a bounded preview from a validated request |
| `POST /calibration-targets/generate` | Queue PDF/spec/preview bundle generation |
| `GET /calibration-targets/bundles` | List reusable bundles |
| `GET /calibration-targets/bundles/<target_id>/preview.png` | Return the stored preview |
| `GET /calibration-targets/bundles/<target_id>/download/<artifact>` | Download an allow-listed bundle artifact |
| `POST /calibration-targets/bundles/<target_id>/select` | Snapshot/select a target for a run after conflict checks |
| `DELETE /calibration-targets/bundles/<target_id>` | Remove a permitted reusable bundle after reference checks |

Generation is CPU/disk work and returns `202`. See
[Calibration target generation](../../POSEGRIDGEN_CALIBRATION_TARGETS.md) for
the full target and selection contract.

## Intent-level attempts

| Method and path | Contract |
| --- | --- |
| `GET /calibration/setup?run_root=…` | Return current target, sensor, evidence, and attempt readiness |
| `GET /calibration/attempts?run_root=…` | List retained attempt summaries |
| `POST /calibration/attempts` | Validate an intent and queue calculation |
| `GET /calibration/attempts/<attempt_id>?run_root=…` | Return request, progress, ranking/checks, candidates, and linked jobs |
| `POST /calibration/attempts/<attempt_id>/promote` | Queue explicit selected-candidate promotion with operator provenance |

An attempt retains `request.json`, `progress.json`, intermediate search and
candidate files, ranking/check evidence, selected target, candidate profiles,
and promotion evidence below `processed/calibration/<attempt_id>/`.
The queue binds the job ID before the worker runs. Status and listing reconcile
a terminal calculation job with its saved progress. Promotion uses a durable
journal to recover an interrupted multi-file publication. Failed replacement
restores the previous promoted evidence and exposes the failure for review.

`GET /calibration/attempts/<attempt_id>` also derives a non-artifact
`promotion_review` from the immutable candidate evidence. Alternative solver
failures are diagnostic and do not block a complete selected bundle. For a
multi-camera bundle, pairwise disagreement between the independently estimated
common companion transforms is advisory above 10 mm or 5° and fails closed
above 20 mm or 10°. A response status of `promotable_with_warnings` enables
explicit promotion while preserving the exact warning evidence in the promoted
profiles. The promotion transaction reproduces the stored ranking under its
recorded policy before applying the current retention rule. Accepted immutable
rankings from the initial joint-consistency policy may omit its revision. The
reader interprets that one published shape using its original v1 thresholds.

Captured sensor type and identity must agree with `run_config.json`. Each
promoted geometry profile requires its exact matching intrinsic profile.
Current profiles record the first and last host receipt times of their camera
evidence. The console reports that capture age and the publication date
separately. Older profiles without capture dates display an unknown age.

OAK-D Pro RGB requests device undistortion. Current camera sidecars identify
the pinhole projection as `depthai_device_undistorted`. A raw factory model
with nonzero coefficients beyond the supported forward model requires the
gated fitted projection before use.

Typical submission shape:

```json
{
  "run_root": "working_data/calibration_run",
  "mode": "eye_in_hand",
  "sensor_keys": ["realsense_d435:825412070181"],
  "target_id": "…",
  "synchronization_policy": "auto_offset"
}
```

The exact accepted fields and candidate modes are returned by
`/calibration/setup`; clients should use that setup payload rather than assume
a mode is available.

Optional shared-grid reprojection refinement is an advanced recorded-data CLI
operation, described in [Calibration targets](../../POSEGRIDGEN_CALIBRATION_TARGETS.md#recorded-shared-grid-refinement).
It retains the attempt's ranking and seed profiles. Explicit CLI acceptance
adds a hash-bound `reprojection_refinement` to promotion request/status evidence
and reproduces its inputs, optimization, and motion-disjoint audit inside the
ordinary promotion transaction. Browser promotion submits the ordinary seed
selection; no refinement creation or acceptance HTTP route is exposed.

## Automatic time alignment

The current implementation is `constant_latency_nearest_pose_optical_spin.v6`.
Create a new calculation attempt with `synchronization_policy: auto_offset` to
analyze an existing recording after restarting the backend. Existing attempts
remain immutable; older timing revisions are not migrated or reinterpreted.

For `eye_in_hand`, timing first looks for a common group of at least three
single-axis rotations at the same flange location, including opposite
directions. Each motion needs at least 5° of rotation and 20 usable IPPE views;
flange translation must stay within 0.5 mm and the camera rotation axis must
align with the optical axis by at least 0.85. Selection uses geometry before
testing offsets. Every selected frame needs valid robot samples across the full
±300 ms search range, with no interpolation across a gap above 40 ms.

The estimator aligns camera and robot angular trajectories with a shared
angular mapping across forward and reverse motions. Its uncertainty interval
envelops the 95% percentile interval from 200 resamples of 0.5 s frame blocks,
estimates with each motion omitted, and linear/quadratic/cubic mapping estimates,
then expands by half the median robot sample period. Applying an offset requires
an interval that excludes zero, stays inside the search range, and is no wider
than `max(20 ms, 2 × median robot sample period)`. The measured continuous estimate
is rounded to the nearest 5 ms grid value for authoritative nearest-pose pairing.
Raw timestamps and poses remain unchanged.

`time_offset_search.json` retains each sensor's `rotational_timing` configuration,
status, source frame identities, angular search curve, measured offset,
`confidence_interval_ms`, motion-omission estimates, and mapping sensitivity.
An accepted measurement uses `improvement_evidence_strategy:
optical_axis_spin_block_bootstrap.v1`; spatial translation materiality is then
diagnostic and does not veto an independently identified delay. The spatial
rotation-degradation guard and final residual/reprojection checks still apply.
Promotion reproduces the angular measurement from retained IPPE observations
and the request's hash-bound raw robot poses before accepting it.

When this motion evidence is unavailable or inconclusive, the existing
motion-disjoint spatial search and search-corrected leave-one-motion-out checks
remain the fallback. Weak evidence retains 0 ms with a visible warning; 0 ms is
not a claim that the camera has no delay. Camera-to-SDK arrival time is a separate
delivery measurement and must not be substituted for image-to-robot alignment.

Angular motion as a timing signal is motivated by
[Furrer et al., *Evaluation of Combined Time-Offset Estimation and Hand-Eye Calibration on Robotic Datasets*](https://tisl.cs.utoronto.ca/publication/201709-fsr-hand_eye_calibration/fsr17-hand_eye_calibration.pdf).
The restricted optical-axis selection and uncertainty procedure above are
PoseTestBot's implementation.

## Reusable profile selection

| Method and path | Contract |
| --- | --- |
| `GET /ui/calibrations?run_root=…` | List compatible promoted calibration sources and current selection, newest first |
| `POST /ui/calibrations/select` | Build an immutable single- or multi-source run snapshot |
| `GET /ui/calibrations/library` | List every calibration on disk with its age, cameras, location, and the runs that use it |
| `POST /ui/calibrations/library/retain` | Copy a calibration a run still holds into the retained library |
| `DELETE /ui/calibrations/library/<bundle_sha256>` | Permanently delete one retained library entry |

Multi-source selection uses `calibration_profile_selection.v2`, binds every
source bundle and per-sensor mapping, and records hashes of the exact combined
profile snapshots below `processed/calibration_inputs/<bundle_sha256>/`.

### Sources and the retained library

A selectable source is either a calibration run that still publishes its
promoted `calibration_profiles.json`, or an entry in the retained calibration
library. Each listed source reports `origin` (`run` or `library`),
`calibrated_at`, and `source_run_root`; for a library entry `source_run_root`
is the entry directory, and that value is what `source_selections[]` sends
back. A selection made from a library entry records the entry directory as the
source `run_root` and adds a `retained` object with the entry's `retained_at`
and original `origin`. An entry whose bundle a present run still publishes is
listed once, as the run.

Publication also retains a verified library copy with reason `promotion`.
If that copy fails, promoted profiles remain usable from their run and
Calibrations offers **Keep in library** to retry.

`GET /ui/calibrations/library` is independent of any destination run. Each
calibration has an `availability`:

| Value | Meaning | `selectable` |
| --- | --- | --- |
| `run` | Promoted in a calibration run folder that still exists | yes |
| `library` | Kept in the retained library; its run folder may be gone | yes |
| `snapshot` | Exists only as the snapshot inside dataset runs that selected it | no, until retained |

`used_by` lists runs whose verified selection references the bundle.
`unusable` lists run or library files that failed integrity or schema
validation; they are reported, never offered.

Set `POSETESTBOT_CALIBRATION_AGE_WARNING_DAYS` to a positive integer on the
backend to enable a warning per dated camera profile. The warning preserves
selectability and prompts a mount check. Omit the setting to leave it disabled.

`POST /ui/calibrations/library/retain` takes `run_root` and `bundle_sha256`.
The run must hold that exact bundle, either promoted at its root or as its
verified selection snapshot; a mismatch returns `409`. Retention is idempotent
(`already_retained`). `DELETE` requires `{"confirm": true}`. Deleting an entry
never changes a dataset run: each run keeps its own snapshot.

The library is local storage outside cluster run archives. Include the entire
`working_data/calibration_library/` tree in the lab's filesystem backup. Restore
each digest directory with its three files together while the backend is
stopped. On the next read, PoseTestBot verifies the digests and file identities.
