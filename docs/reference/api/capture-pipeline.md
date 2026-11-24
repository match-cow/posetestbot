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

The job actively opens each enabled camera selected by `run_config.v4`, starts
its configured RGB-D stream, requires one frame, and closes it without recording
or creating a raw sensor folder. A selected camera that is merely enumerated but
still held by a stale/crashed process makes preflight fail.

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

`intent` must match `run_config.json`. Queueing requires a current
`run_preflight.v2` report with status `ok` or `warning` and active evidence for
every selected camera. The queue records the approved configuration SHA-256 in
the worker arguments and job parameters. The fixed recipe builds a capture
plan, runs capture-plan preflight, writes the execution plan, supervises capture,
and validates completion. Cancel requests cannot interrupt active IIWA motion
and never send the idle-program exit command.

Before writing `capture_plan.json`, the worker repeats the bounded
selected-camera open probe and checks the configuration SHA-256 again. The
supervisor checks it under the run-config lock and publishes `starting` before
releasing that lock. A changed configuration requires a fresh preflight and a
new capture submission.

## Dataset processing

`POST /dataset-processing/jobs` accepts `run_root` and queues the immutable
recipe:

```text
non-destructive sync → sync quality → RGB-D rectification → calibrated base BOP export
```

It requires dataset intent and a valid current reusable-calibration selection.
It never rewrites raw capture. Optional pose or mask generation is deliberately
separate under `/bop/annotations`.

## Synchronization quality

The dataset-processing job writes the report. `GET /sync/quality?run_root=…`
loads the saved evidence. Coverage uses eligible in-motion frames. The report
includes pose delta, pose packet loss, missing timestamp evidence, and
unexplained in-motion exclusions. Pose sequence gaps at the beginning and end
of the streamed interval count toward packet loss.

For manual synchronization, `--sync-delta` defaults to 0 ms. A JSON mapping
must specify a delta for every sensor folder being synchronized. With
`--timestamp-source host_wall`, the sidecar value represents userspace receipt
of the frame for all supported cameras. OAK also retains an estimated capture
time separately.
