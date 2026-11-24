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

The top-bar run switcher uses the ordered `/ui/runs` index and stores its
selection only in browser-local context. It changes which contained run is used
by run-owned pages and actions; it does not mutate, move, or delete a run.

## Run-folder operations

| Method and path | Contract |
| --- | --- |
| `GET /ui/run-folders` | Load or refresh the bounded run-folder inventory |
| `GET /ui/run-folders/cluster-activity` | Report whether controller activity can be verified and list source runs in current or pending archive/estimator jobs |
| `POST /ui/run-folders/refresh` | Queue a filesystem inventory refresh |
| `POST /ui/run-folders/move` | Queue a move after expected source/destination identity checks |
| `DELETE /ui/run-folders` | Queue confirmed deletion after identity and active-job checks |

Move and delete requests use compare-and-swap identity evidence from the latest
inventory. Stale inventory, changed filesystem identity, active run jobs, or an
out-of-root destination fail closed. Deletion is destructive and requires the
explicit confirmation contract exposed by the console.

An unavailable approved root is listed with its reason. Available roots still
contribute runs to the inventory. When cluster integration is enabled, move and
delete require a complete recent activity check. An active controller job for
the source run, controller outage, or incomplete history blocks submission.

A move is refused before anything is isolated when the run's `run_config.json`
is missing, unparsable, or records another location, because neither the move
nor its rollback could then complete.

`calibration_retention` reports `promoted` or `last_snapshot` when a run
publishes calibration or holds its last verified dataset snapshot. Those runs
require a literal boolean `retain_calibration`. With `true`, the worker makes
the retained copy durable before removing the run. A failed copy aborts
deletion. A `false` choice discards the run-owned evidence. The job result
reports `retained_calibration` (the library entry, or `null`).

`POST /run-config` only writes into a new folder, an existing run folder, or a
folder holding only PoseTestBot pre-configuration files. The path must be a
direct child of an approved root and must pass the reserved-name and symlink
checks. Initial configuration validation runs before creating a new directory.

## Local jobs

| Method and path | Contract |
| --- | --- |
| `GET /jobs` | List local jobs; query filters can include/exclude terminal work |
| `GET /jobs/<job_id>` | Return one job snapshot |
| `GET /jobs/<job_id>/log` | Return the bounded plain-text job log |
| `POST /jobs/<job_id>/cancel` | Request cooperative cancellation when the job contract permits it |

Use `run_root` and `search` to look up a run's jobs. The default page includes
active work and bounds terminal history. Use `next_cursor` to read older
terminal entries. `include_services`, when supplied, must be a valid boolean.

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

Inspect submissions persist their run-owned `job.json` binding before a worker
starts. If submission fails before launch, the runner releases the resource
reservation and cleans up the unstarted job record. Persisted job records keep
the original submission path after a run moves.

Robot Start/Stop uses the purpose-specific `POST /robot/commands` contract.
The local runner is not a general remote scheduler. External archive and
estimator jobs are exposed through the narrow [cluster API](bop-cluster.md).
