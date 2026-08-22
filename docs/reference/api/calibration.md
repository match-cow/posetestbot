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

The fixed current planar-PnP comparison uses IPPE and SQPnP. Iterative PnP is
not generated as a third default candidate: after the common LM refinement it
was a duplicate initializer on the repository corpus, not independent evidence.
The API does not accept caller-selected solver lists. Retained attempt evidence
can still identify an Iterative result for diagnosis; new attempts do not
produce one.

`GET /calibration/attempts/<attempt_id>` also derives a non-artifact
`promotion_review` from the immutable candidate evidence. Alternative solver
failures are diagnostic and do not block a complete selected bundle. For a
complete, internally consistent per-camera candidate, mean closure residuals
above 10 mm or 5° are retained as prominent quality warnings; values above the
20 mm or 10° hard self-consistency ceiling remain contradictory and fail
closed. For a multi-camera bundle, pairwise disagreement between the
independently estimated common companion transforms uses the same advisory and
hard limits. A response status of `promotable_with_warnings` enables explicit
promotion while preserving the exact warning evidence in the promoted profiles.
The promotion transaction revalidates historical attempts under their recorded
policy before applying this current retention rule; it does not rewrite
`ranking.json`.

Creating a promotion request records a `calibration_promotion_request.v2`
approval snapshot. Its `review_input_bindings` list size- and SHA-256-binds the
complete attempt intent/status, synchronization and timing evidence, intrinsic,
PnP, observation and extrinsic evidence, ranking/checks/candidate profiles, and
the attempt-owned target bundle. Approval writes the request first and an
`approved` status as its durable commit marker while holding the run mutation
lock. Only successful queue submission plus durable job-ID binding changes that
status to `queued`; an unbound approval cannot execute. Submission or binding
failure records a terminal failure, and a submitted job whose binding fails is
canceled. The queued promotion verifies the same binding set before changing
promotion status, verifies the exact intrinsic projection and staged target
bundle, and rechecks all source bindings before transaction commit. It fails
closed if evidence is missing or changed. Promotion requests using the retired
v1 schema are rejected and are not migrated in place; a fresh v2 approval may
be recorded only through the normal failed-promotion retry flow.

Promotion publishes its root profiles, target, run configuration, manifest,
library state, and promotion evidence through one recoverable transaction. The
hidden `.calibration_promotion.transaction.json` hash-binds staged, prior, and
installed content. Recovery of a `prepared` transaction restores the complete
prior generation; recovery of a `committed` transaction verifies and retains
the complete new generation before removing backups. Attempt reads and later
promotion starts perform this recovery, while missing or tampered transaction
evidence fails closed. Staged files and directories are fsynced before journal
publication, and installation and rollback use no-clobber renames so a raced
path is preserved rather than overwritten.

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

## Reusable profile selection

| Method and path | Contract |
| --- | --- |
| `GET /ui/calibrations?run_root=…` | List compatible promoted calibration sources and current selection |
| `POST /ui/calibrations/select` | Build an immutable single- or multi-source run snapshot |

Multi-source selection uses `calibration_profile_selection.v2`, binds every
source bundle and per-sensor mapping, and records hashes of the exact combined
profile snapshots below `processed/calibration_inputs/<bundle_sha256>/`.
