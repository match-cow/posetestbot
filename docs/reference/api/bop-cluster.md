# BOP and cluster-boundary API

These APIs operate after or beside acquisition. BOP annotation and evaluation
remain run-scoped. Cluster routes proxy a separate loopback controller and do
not import estimator code, credentials, or scheduler arguments into
PoseTestBot.

## Optional BOP annotations

| Method and path | Contract |
| --- | --- |
| `GET /bop/annotations/setup?run_root=…` | Inspect exported dataset and readiness blockers per annotation mode |
| `POST /bop/annotations` | Queue `pose` or `pose_and_mask` generation after mode-specific readiness validation |

The request is:

```json
{
  "run_root": "working_data/example",
  "mode": "pose_and_mask"
}
```

The job writes only below `processed/bop_annotations/` and the BOP scene
directories governed by the annotation/export writer. It declares CPU,
render, and disk resources.

## Inspect-only official evaluation

| Method and path | Contract |
| --- | --- |
| `GET /bop/evaluation/setup?run_root=…` | Inspect annotation-bearing dataset, pinned toolkit status, registered results, and evaluations |
| `POST /bop/evaluation/results` | Multipart import of an already standard BOP19 CSV; locally validates and stores immutable provenance |
| `GET /bop/evaluation/results/<result_id>/download?run_root=…` | Download the retained immutable CSV |
| `POST /bop/evaluations` | Queue official toolkit evaluation of a registered result or deterministic test-only GT perturbation |
| `GET /bop/evaluations/<evaluation_id>/report?run_root=…` | Download the completed derived report |

Result upload requires form fields `run_root`, file field `file` (or `result`),
and optional `display_name`/`method_name`. The filename must be a basename with
`.csv`; the bounded upload is validated again after staging.

Registered-result evaluation request:

```json
{
  "run_root": "working_data/example",
  "source": {
    "kind": "registered_result",
    "result_id": "result-…"
  }
}
```

The only alternative source is `gt_simulation`, intended for deterministic
test validation and explicitly labelled as such. This API is not a pose
estimator, result converter, or acquisition-pipeline stage. All result and
evaluation evidence stays below `processed/bop_evaluation/`.

## External cluster controller proxy

| Method and path | Contract |
| --- | --- |
| `GET /cluster/status` | Return curated controller connectivity, storage, archive, and advertised-estimator status |
| `GET /cluster/controller-service` | Inspect the one fixed configured user-service |
| `POST /cluster/controller-service/<action>` | Queue allow-listed start/stop/restart action; browser cannot name a unit or command |
| `GET /cluster/archives` | List immutable controller-side run archives |
| `POST /cluster/archives` | Submit archive creation for a locally validated run |
| `POST /cluster/archives/<archive_id>/restore` | Submit restore with local identity/active-job checks |
| `GET /cluster/pose-estimation/setup?run_root=…` | Return browser-safe dataset hash, archive readiness, and advertised estimator choices |
| `POST /cluster/pose-estimation/jobs` | Submit a typed controller job using an advertised estimator ID and supported options |
| `GET /cluster/jobs` | List curated external jobs |
| `GET /cluster/jobs/<job_id>` | Inspect one curated external job |
| `POST /cluster/jobs/<job_id>/cancel` | Request controller cancellation |
| `POST /cluster/jobs/<job_id>/import-result` | Revalidate and immutably import a completed standard BOP19 result into the active run |

The proxy is enabled only by server configuration. Returned values are
allow-listed and scrubbed; controller URLs/tokens, SSH data, remote paths,
container commands, and arbitrary scheduler inputs are never accepted from or
returned to the browser. Imported results bind the controller provenance,
staged dataset hash, and local dataset hash.

Archive/storage readiness is independent from estimator runtime readiness. A
run can be archived or restored even when no qualified estimator is advertised.
