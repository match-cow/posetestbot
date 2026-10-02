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

`GET /calibration/attempts/<attempt_id>` also derives a non-artifact
`promotion_review` from the immutable candidate evidence. Alternative solver
failures are diagnostic and do not block a complete selected bundle. For a
multi-camera bundle, pairwise disagreement between the independently estimated
common companion transforms is advisory above 10 mm or 5° and fails closed
above 20 mm or 10°. A response status of `promotable_with_warnings` enables
explicit promotion while preserving the exact warning evidence in the promoted
profiles. The promotion transaction revalidates historical attempts under
their recorded policy before applying this current retention rule; it does not
rewrite `ranking.json`.

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
| `GET /ui/calibrations?run_root=…` | List compatible promoted calibration sources and current selection |
| `POST /ui/calibrations/select` | Build an immutable single- or multi-source run snapshot |

Multi-source selection uses `calibration_profile_selection.v2`, binds every
source bundle and per-sensor mapping, and records hashes of the exact combined
profile snapshots below `processed/calibration_inputs/<bundle_sha256>/`.
