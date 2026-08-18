# Runs and jobs API

Run endpoints discover approved filesystem-backed runs. Job endpoints expose
durable in-process work submitted through `LocalJobRunner`.

## UI bootstrap and run data

| Method and path | Contract |
| --- | --- |
| `GET /ui/bootstrap` | Return initial run roots, current run selection inputs, storage, and system configuration needed by the console |
| `GET /ui/runs` | Discover runs under approved roots |
| `GET /ui/storage?run_root=…` | Return capacity/threshold evidence for the selected run filesystem |
| `GET /ui/overview?run_root=…` | Summarize workflow steps and artifact evidence for a run |
| `GET /ui/cell-scene?run_root=…` | Build the run's cell visualization data |
| `GET /ui/cell-scene/timeline?run_root=…` | Return bounded frame/timeline metadata |
| `GET /ui/cell-scene/camera-frame?run_root=…` | Return a selected camera frame |

The `/ui/*` prefix means console-facing composition, not unrestricted file
access. All supplied paths and identifiers remain validated.

## Run-folder operations

| Method and path | Contract |
| --- | --- |
| `GET /ui/run-folders` | Load or refresh the bounded run-folder inventory |
| `POST /ui/run-folders/refresh` | Queue a filesystem inventory refresh |
| `POST /ui/run-folders/move` | Queue a move after expected source/destination identity checks |
| `DELETE /ui/run-folders` | Queue confirmed deletion after identity and active-job checks |

Move and delete requests use compare-and-swap identity evidence from the latest
inventory. Stale inventory, changed filesystem identity, active run jobs, or an
out-of-root destination fail closed. Deletion is destructive and requires the
explicit confirmation contract exposed by the console.

## Local jobs

| Method and path | Contract |
| --- | --- |
| `GET /jobs` | List local jobs; query filters can include/exclude terminal work |
| `GET /jobs/<job_id>` | Return one job snapshot |
| `GET /jobs/<job_id>/log` | Return the bounded plain-text job log |
| `POST /jobs/<job_id>/cancel` | Request cooperative cancellation when the job contract permits it |

Queued domain APIs generally return:

```json
{
  "job_id": "…",
  "status": "queued",
  "job": {
    "id": "…",
    "name": "…",
    "status": "queued",
    "resources": ["cpu", "disk_io"]
  }
}
```

Job states include `queued`, `running`, `canceling`, and terminal states such as
`succeeded`, `failed`, or `canceled`. A cancellation response means the request
was recorded; clients should poll until terminal. Committed storage operations
may deliberately set `cancelable: false` and return `409` rather than risk a
half-applied filesystem mutation.

Robot Start/Stop uses the purpose-specific `POST /robot/commands` contract.
The local runner is not a general remote scheduler. External archive and
estimator jobs are exposed through the narrow [cluster API](bop-cluster.md).
