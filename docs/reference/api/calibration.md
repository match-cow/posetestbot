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
