# Capture and pipeline API

The pipeline API exposes declared stages/sequences, run configuration,
preflight evidence, plan generation, and queued execution. Physical execution
is intentionally separated from metadata discovery and planning.

## Registry and recommendations

| Method and path | Contract |
| --- | --- |
| `GET /pipeline/workflows` | Return the two canonical guided operator outcomes |
| `GET /pipeline/stages` | List registered acquisition stages and parameter/resource metadata |
| `GET /pipeline/stages/<stage_id>` | Inspect one stage |
| `GET /pipeline/sequences` | List registered stage sequences |
| `GET /pipeline/sequences/<sequence_id>` | Inspect one sequence and ordering |
| `GET /pipeline/recommendations?run_root=…` | Derive next-action recommendations from run artifacts |

Unknown stage/sequence identifiers return `404`. The registry is the source of
truth; clients should discover it instead of embedding an assumed list.

## Run configuration and preflight

| Method and path | Contract |
| --- | --- |
| `GET /run-config?run_root=…` | Load validated `run_config.v3`, its sequence plan, preflight summary, and camera contract |
| `POST /run-config` | Validate and atomically write run configuration and manifest evidence |
| `GET /pipeline/preflight?run_root=…` | Build current run preflight without writing it |
| `POST /pipeline/preflight` | Write `run_preflight_report.json`; can include sensor/runtime status |

See [Run configuration](../run-config.md) for fields. Preflight is readiness
evidence; it does not grant physical execution.

## Capture plan and gates

| Method and path | Contract |
| --- | --- |
| `GET /capture-plan?run_root=…` | Load `capture_plan.json` |
| `POST /capture-plan` | Build and persist the plan from run config; optional `max_frames` bounds the plan |
| `GET /capture-plan/preflight?run_root=…` | Compute capture-plan preflight without writing |
| `POST /capture-plan/preflight` | Write preflight; requires literal `allow_real_robot: true` |
| `GET /capture-plan/execution?run_root=…` | Load `capture_execution_plan.json` |
| `POST /capture-plan/execution` | Write execution plan; requires literal `allow_cameras: true` and `allow_real_robot: true` |
| `GET /capture/status?run_root=…` | Read `capture_execution_status.json` |
| `GET /capture/jobs?run_root=…` | List capture jobs and current resource holders |
| `POST /capture/jobs/<job_id>/stop` | Cancel a recognized capture job through the job runner |

Example execution-plan request (this is a real physical authorization surface):

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{
    "run_root": "working_data/example",
    "allow_cameras": true,
    "allow_real_robot": true,
    "include_sensors": true
  }' \
  http://127.0.0.1:5000/capture-plan/execution
```

Do not send this request unless the operator has authorized the current
physical run. `mode` and `robot_mode` are retired and rejected.

## Queueing pipeline work

| Method and path | Contract |
| --- | --- |
| `POST /pipeline/run` | Validate and queue one registered stage |
| `POST /pipeline/run-sequence` | Validate and queue one registered sequence; supports explicit `plan_only` |
| `POST /pipeline/run-config` | Queue the sequence selected in `run_config.json` after preflight checks |

`/pipeline/run-config` refuses a non-plan-only sequence containing physical
capture. Clients must use the dedicated gated capture action so both fresh
execution acknowledgements are present. Missing, failed, stale, or invalid
preflight evidence returns `409` unless a specifically supported software-only
override is supplied.

## Synchronization quality

`GET /sync/quality?run_root=…` computes current quality without writing;
`POST /sync/quality` writes `sync_quality_report.json`. The metric denominator
is eligible in-motion frames, not all preserved raw frames. See
[Runs and artifact lineage](../../concepts/runs-and-artifacts.md#synchronization-semantics).
