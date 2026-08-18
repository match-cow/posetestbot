# Capture and focused orchestration API

PoseTestBot exposes purpose-specific operations for the two guided outcomes.
There is no stage/sequence discovery API, arbitrary stage submission, or
caller-selected composition.

## Run configuration

| Route | Contract |
| --- | --- |
| `GET /run-config?run_root=…` | Load strict `run_config.v4`, current preflight summary, and camera-contract mutability |
| `POST /run-config` | Create or replace current intent/configuration and update the run manifest |

The write body requires `run_root`, `intent`, and `annotation_mode`. It may
include current sensor, resolution, rate, velocity, mounting, synchronization,
dataset-mode, and reusable-calibration selection fields. Unknown fields and
saved execution gates are rejected.

## Queued preflight

`POST /preflight/jobs` accepts only a contained configured `run_root` and
queues the canonical preflight with camera and disk resources:

```json
{"run_root": "working_data/example"}
```

A `202` response includes `job_id` and the job snapshot. Work continues after
navigation and remains visible through `/jobs` and **Jobs**.

## Supervised capture

| Route | Contract |
| --- | --- |
| `GET /capture/jobs?run_root=…` | List capture jobs and current execution evidence |
| `POST /capture/jobs` | Queue the fixed physical capture recipe |
| `GET /capture/status?run_root=…` | Read `capture_execution_status.json` |
| `POST /capture/jobs/<job_id>/stop` | Cancel/clean up a capture job without sending IIWA Stop |

The submission body is exact and both booleans must be literal `true`:

```json
{
  "run_root": "working_data/example",
  "intent": "dataset",
  "allow_cameras": true,
  "allow_real_robot": true
}
```

`intent` must match `run_config.json`, and a fresh successful run preflight is
required. The server runs one recipe: plan → capture-plan preflight → execution
plan → supervised execution → capture-completion validation. Cancel requests
cannot interrupt active IIWA motion and never send the idle-program exit
command.

## Dataset processing

`POST /dataset-processing/jobs` accepts `run_root` and queues the immutable
recipe:

```text
non-destructive sync → sync quality → RGB-D rectification → calibrated base BOP export
```

It requires dataset intent and a valid current reusable-calibration selection.
It never rewrites raw capture. Optional pose or mask generation is deliberately
separate under `/bop/annotations`.

## Removed surfaces

`/pipeline/*`, `/capture-plan*`, and `/run-command` are not registered. Clients
must use the purpose-specific routes above and `/robot/commands`.

## Synchronization quality

`GET /sync/quality?run_root=…` loads existing evidence. `POST /sync/quality`
writes the current report after strict synchronized inputs exist. Quality is
based on eligible in-motion frames, pose delta, packet loss, timestamp
evidence, and unexplained exclusions—not lead-in/tail frames.
