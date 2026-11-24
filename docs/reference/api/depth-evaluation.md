# Inspect depth evaluation API

These run-scoped APIs retain evidence only below
`processed/bop_evaluation/depth/`. Rendering, evaluation, comparisons, and
reporting run as cancelable `LocalJobRunner` CPU/disk jobs. See
[depth methodology](../../concepts/depth-evaluation.md).

`GET /depth/evaluation/setup?run_root=...` returns
`depth_evaluation_setup.v1`: `dataset.ready`/`blockers`, frozen geometry hashes,
sensor scenes/resolutions, frame count, quality evidence, renderer status,
defaults, and assumptions. Each `dataset.sensors` entry includes `projection`,
`distortion_model`, `distortion`, and `unavailable_reason`. Cameras with nonzero
distortion require a rectified BOP export. Setup stays ready while at least one
camera is compatible. Pose-only exports are accepted without BOP19 visibility
or results. Renderer availability is reported separately.

`POST /depth/evaluations` requires:

```json
{
  "schema_version": "depth_evaluation_submission.v1",
  "run_root": "/mnt/working_data_ssd/study/matte",
  "finish_label": "matte",
  "geometry_hashes": ["<canonical snapshot SHA-256 from setup>"],
  "sensors": ["realsense_d435_123"],
  "settings": {
    "thresholds_mm": [1, 2, 5, 10],
    "erosion_pixels": 2,
    "distance_bin_mm": 100,
    "angle_bin_deg": 10
  }
}
```

Unknown fields/retired schemas fail closed. Labels are trimmed 1–120 character
text. Geometry and compatible sensors are nonempty unique known values. The
request and report freeze the selected `camera_projections`. Thresholds are
increasing unique positive values ≤10000 mm (at most 16); distance bins are
1–10000 mm, angle bins 1–90°, and erosion is fixed at 2 pixels. Settings may be
omitted to freeze defaults. `202 depth_submission.v1` returns `id`, `job_id`,
and a job snapshot. Navigation does not cancel work.

`POST /depth/comparisons` requires:

```json
{
  "schema_version": "depth_comparison_submission.v1",
  "run_root": "/mnt/working_data_ssd/study/matte",
  "evaluations": [
    {"run_root": "/mnt/working_data_ssd/study/matte", "evaluation_id": "depth-eval-0123456789ab"},
    {"run_root": "/mnt/working_data_ssd/study/polished", "evaluation_id": "depth-eval-abcdef012345"}
  ]
}
```

Supply 2–24 unique completed inputs from approved roots. The owner run must be
an input. Settings must match and canonical geometry hashes must intersect.
Older studies without `camera_projections` qualify through their frozen,
hash-verified BOP export manifest. New comparisons record the verified
projection evidence per input.
One run cannot have conflicting finish labels. Empty common bins is a valid
completed comparison with explicit unavailability/unmatched coverage. The
submission response uses the same `202` envelope.

## Retained reads

`<kind>` is exactly `evaluations` or `comparisons`. IDs are
`depth-eval-<12 lowercase hex>` or `depth-compare-<12 lowercase hex>`.
Every read requires the current approved `run_root`, including after a move.
Retained requests preserve their original location and job binding.

| Route | Contract |
| --- | --- |
| `GET /depth/<kind>` | `depth_history.v1`; newest first; `offset=0`, `limit=20` (1–100), optional `search` |
| `GET /depth/<kind>/<id>` or `/status` | `depth_status.v1`; metadata, progress, job ID, completion availability; Jobs owns process truth |
| `GET /depth/<kind>/<id>/report` | `depth_evaluation_report.v1` or `depth_comparison_report.v1` |
| `GET /depth/<kind>/<id>/frames` | `depth_frames.v1`; bounded frame metadata; same pagination bounds; optional nonnegative `scene_id` and comparison `input_id` (0–23 ordinal string) filters |
| `GET /depth/<kind>/<id>/frames/<key>` | Frame identities, per-instance/region metrics and scales |
| `GET /depth/<kind>/<id>/frames/<key>/media/<media>` | PNG: `rgb`, `measured`, `reference`, `error`, `missing`; NPZ: `numeric` |
| `GET /depth/<kind>/<id>/download` | Complete research ZIP: HTML, figures, tables, maps, provenance |

Frame keys join six-digit-padded scene/image IDs with `_`; comparisons prefix
the input ordinal. Arbitrary media paths are rejected. Reports/media/downloads
are unavailable until atomic completion, then hash-checked. Null accuracy
means no valid returns; no rendered pixels has `status: unavailable`.

Damaged records or job bindings appear individually as `invalid`. A queued
record without a job binding reports `abandoned`. The rest of the history
remains readable and only completed publications expose downloads. These
states also apply to depth video history.

Versioned Python contracts live in `posetestbot.bop.depth_metrics`, with
TypeScript counterparts in `frontend/src/features/depth-evaluation/contracts.ts`.

## Retained quality videos

`GET /depth/<kind>/<id>/videos?run_root=...` requires a completed source study
and returns `depth_video_choices.v1`: `encoder.available`/`reason`, camera/finish
`series` (scene/input IDs, sensor, resolution and frame count), and newest-first
export `records` with `total`, `offset`, and `limit`. Pagination bounds are the
same as retained reads. Export records include settings, progress, Jobs
identity, output availability, frame count, duration, and output dimensions.

`POST /depth/videos` freezes the following strict contract:

```json
{
  "schema_version": "depth_video_submission.v1",
  "run_root": "/mnt/working_data_ssd/study/matte",
  "source_kind": "evaluations",
  "source_id": "depth-eval-0123456789ab",
  "scene_id": 1,
  "input_id": null,
  "view": "overview",
  "fps": 15,
  "stride": 1,
  "width": 1280
}
```

`source_kind` is `evaluations` or `comparisons`; source and export share the
approved owner run. `input_id` is null for evaluations and the chosen string
ordinal for comparisons. Choose a scene/input pair from `series`. Views are
`overview` (RGB/measured/reference/error), `measured`, `error`, or `missing`.
`fps` is an integer 1–60; `stride` is an integer 1–120; `width` is 960, 1280, or
1920. Defaults are overview/15/1/1280. The complete camera series is sorted by
image ID then sampled by stride. No input or camera series is interleaved.

`202 depth_video_queued.v1` returns `id` (`depth-video-<12 lowercase hex>`),
`job_id`, and `job`. Work uses cancelable LocalJobRunner CPU/disk resources;
unknown fields, unavailable sources/encoders, and invalid selections fail
without queueing. Video contracts live in `posetestbot.bop.depth_videos`.

All reads below require `run_root`:

| Route | Contract |
| --- | --- |
| `GET /depth/videos/<id>` | `depth_video_status.v1`; state, settings, counts, job identity, duration, output availability |
| `GET /depth/videos/<id>/media` | Hash-verified `video/mp4`, inline, conditional/range requests for playback and seeking |
| `GET /depth/videos/<id>/download` | Same MP4 as attachment |
| `GET /depth/videos/<id>/poster` | Hash-verified PNG preview of the first encoded frame |
| `GET /depth/videos/<id>/manifest` | Completed `depth_video_publication.v1`; request/source hashes, settings/scales, encoding, frame count/duration, dimensions, timing definition, output hash |

Requests/progress/job identity and failed `.work/` remain outside `complete/`.
The MP4 and manifest are published together after source revalidation. No
partial download is available. The completed video remains independent of
later raw-source changes. A video does not modify its source study or ZIP.

Current studies publish renderer maps once in the frame NPZ files. If an
older complete study retains duplicate renderer files, preserve its manifest
and use a fresh study to reclaim space. Verify the replacement before choosing
whether to remove the entire older derived publication.
