# HTTP API conventions

The Flask HTTP API is the operator console's backend and a supported local
automation surface. It exposes JSON status/configuration endpoints, queued-job
submissions, and bounded file downloads.

## Base URL and trust model

The default development base URL is:

```text
http://127.0.0.1:5000
```

The server has **no end-user authentication**. It is intended for localhost or
a trusted lab network. Do not publish it directly to the internet. The optional
cluster-controller token remains server-side and is never part of an API
response.

## Content types

- JSON requests use `Content-Type: application/json` and require an object
  unless the endpoint says otherwise.
- Catalogue CAD uploads and BOP result imports use `multipart/form-data`.
- Image, PDF, CSV, JSON-report, and managed-asset endpoints return files.
- Job logs return `text/plain`.

Most successful JSON responses include the domain object plus identifiers such
as `run_root`, `job_id`, `attempt_id`, or `result_id`. Persist those identifiers
instead of parsing the human-readable `output` field.

## Run-root parameter

Run-scoped reads normally accept `run_root` as a query parameter:

```bash
curl --fail-with-body --get http://127.0.0.1:5000/run-config \
  --data-urlencode run_root=working_data/example
```

Mutations normally place it in the JSON body:

```json
{
  "run_root": "working_data/example"
}
```

Relative paths resolve below the default repository `working_data/` root.
Absolute and relative values are canonicalized and must remain below an
approved web run root. See [Architecture and boundaries](../concepts/architecture.md#filesystem-boundary).

## Read, write, and queue semantics

| Pattern | Typical status | Meaning |
| --- | --- | --- |
| `GET` | `200` | Inspect current or computed state; does not authorize robot motion |
| bounded `POST`/`PUT`/`PATCH` | `200` or `201` | Validate and commit metadata/report state |
| queued `POST` | `202` | Accepted by `LocalJobRunner`; follow `job_id` through `/jobs/<job_id>` |
| `DELETE` | `200` or `202` | Retire metadata immediately; physical cleanup may be queued |

Navigation does not cancel a `202` job. Poll the job resource or use the
console's **Jobs** page.

## Errors

The common error envelope is:

```json
{
  "output": "human-readable failure reason"
}
```

Some validation endpoints return structured `errors`, `blockers`, `issues`, or
`preflight` fields in addition to `output`. Clients should branch on the HTTP
status and preserve the full body for diagnosis.

| Status | Usual meaning |
| --- | --- |
| `400` | Invalid JSON, field, path, state transition, or unsupported option |
| `404` | Unknown identifier or required artifact absent |
| `409` | Resource busy, stale compare-and-swap input, failed readiness gate, or conflicting state |
| `413` | Upload exceeds its endpoint limit |
| `422` | Structured calibration-target validation failure |
| `503` | Optional worker/controller/runtime unavailable |

## Boolean and execution-gate rules

Execution acknowledgement fields must be literal JSON booleans. In particular,
`allow_cameras` and `allow_real_robot` reject strings and do not persist as
future authorization. See [Safety and authorization](../concepts/safety.md).

## Safe discovery example

These calls return registered metadata and create a plan-only job; they do not
start a physical capture:

```bash
curl --fail-with-body http://127.0.0.1:5000/pipeline/workflows
curl --fail-with-body http://127.0.0.1:5000/pipeline/sequences
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{
    "run_root": "working_data/example",
    "sequence": "real_full_capture_validation",
    "plan_only": true,
    "options": {}
  }' \
  http://127.0.0.1:5000/pipeline/run-sequence
```

## Reference organization

- [Complete generated route index](http-api-routes.md)
- [System, sensors, and monitoring](api/system-sensors.md)
- [Runs and jobs](api/runs-jobs.md)
- [Capture and pipeline](api/capture-pipeline.md)
- [Calibration](api/calibration.md)
- [Workpieces and pose templates](api/catalogs.md)
- [BOP and cluster boundary](api/bop-cluster.md)

The route index is generated from Flask and tested for drift. Domain pages
describe behavioral contracts and important payloads. Payload schema versions
inside artifacts remain authoritative when a response embeds a persisted
artifact.
