# Runs and jobs API

Run endpoints discover approved filesystem-backed runs. Job endpoints expose
durable local process-backed work submitted through `LocalJobRunner`.

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

The top-bar run switcher uses the ordered `/ui/runs` index and stores its
selection only in browser-local context. It changes which contained run is used
by run-owned pages and actions; it does not mutate, move, or delete a run.

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

Declared resource ownership is shared across every runner instance using the
same job root. Submission takes an interprocess transaction, reloads persisted
active owners, applies hierarchical conflicts such as `camera` versus
`camera:oak_d_pro`, and publishes the new queued record before releasing the
claim lock. PID/start-time identity permits a later runner to reclaim an owner
that actually exited; two service processes cannot both accept the same camera
or robot resource. Malformed, symlinked, identity-mismatched, or path-escaping
persisted active-claim evidence blocks new resource allocation instead of being
silently ignored. The root claim lock and per-job state locks bind the opened
directory and lock-file inodes again after acquisition, so replacing a pathname
while a process waits for the lock fails closed.

Cancellation writes a durable `cancel_request.json` under the job directory.
The supervisor observes that request before workload launch and while the
workload runs; per-job interprocess state serialization makes cancellation a
monotonic transition that another runner cannot overwrite with a later
`running`, `succeeded`, or `failed` update. Failure to start the local worker
thread is persisted as a terminal job before the resource is released. An
unexpected runner fault after the supervisor starts first verifies that the
supervisor and workload groups stopped, then persists a terminal failure and
releases the claim. If process termination or authoritative terminal
persistence cannot be verified, the active claim remains fail-closed and is
retried by subsequent runner operations. `job.json` is authoritative; the
SQLite history index is a rebuildable view, so an index-write fault cannot
change a completed command's outcome or retain its resource claim.

`POST /dataset-processing/jobs` is stricter than this generic response shape:
before accepting the job, it revalidates a succeeded
`capture_execution_report.v2`, its exact run-configuration and per-execution
archive bindings, and its embedded `capture_completion.v1` with `status: ok`.
This request-time gate stays bounded and does not decode the run's image set.
The queued worker repeats the provenance gate and rebuilds the complete capture
result against current raw evidence, including every RGB/depth PNG, before any
derived stage begins.

Manual robot motion-start and idle-program-exit requests use the purpose-specific
`POST /robot/commands` contract. They are not exposed as Dashboard quick
controls and are not the canonical supervised-capture path; the idle-program
exit cannot stop motion. The local runner is not a general remote scheduler.
External archive and estimator jobs are exposed through the narrow [cluster
API](bop-cluster.md).
