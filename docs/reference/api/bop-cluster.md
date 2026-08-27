# BOP and cluster-boundary API

These APIs operate after or beside acquisition. BOP annotation and evaluation
remain run-scoped. Cluster routes proxy a separate loopback controller and do
not import estimator code, credentials, or scheduler arguments into
PoseTestBot.

## Optional BOP annotations

| Method and path | Contract |
| --- | --- |
| `GET /bop/annotations/setup?run_root=…` | Inspect exported dataset and readiness blockers per annotation mode |
| `POST /bop/annotations` | Queue the run-configured `pose` or `pose_and_masks` product after mode-specific readiness validation |

The request is:

```json
{
  "run_root": "working_data/example",
  "mode": "pose_and_masks"
}
```

The job writes only below `processed/bop_annotations/` and the BOP scene
directories governed by the annotation/export writer. It declares CPU,
render, and disk resources.

## Inspect-only official evaluation

| Method and path | Contract |
| --- | --- |
| `GET /bop/evaluation/setup?run_root=…` | Inspect annotation-bearing dataset, pinned toolkit status, registered results, and evaluations |
| `GET /bop/evaluation/results?run_root=…&limit=…` | List bounded browser-safe retained-result summaries |
| `POST /bop/evaluation/results` | Multipart import of an already standard BOP19 CSV; locally validates and stores immutable provenance |
| `GET /bop/evaluation/results/<result_id>/download?run_root=…` | Download the retained immutable CSV |
| `GET /bop/evaluation/results/<result_id>/provenance?run_root=…` | Download sanitized controller provenance when the result was collected externally |
| `GET /bop/evaluation/results/<result_id>/package?run_root=…` | Build a deterministic ZIP with CSV, optional provenance, and a path-free hash/size/schema manifest |
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
estimator, result converter, or acquisition stage. All result and
evaluation evidence stays below `processed/bop_evaluation/`.

Every CSV, provenance, and package download rechecks the retained file against
its recorded hash. A package is produced on demand with fixed ZIP metadata; it
does not mutate the result directory. A manual import contains CSV plus
manifest and reports no provenance member. The console's evaluation history
and report are scoped to the selected result, preventing metrics from another
result from remaining visible.

An imported FoundationPose v2 result may carry a verified sensor scope. In
that case PoseTestBot recomputes the selected target inventory from the local
export before import. Evaluation writes
`selected_test_targets_bop19.json` inside the evaluation directory, passes its
absolute path to the pinned toolkit, restricts the adapter scene IDs, and
labels a proper sensor subset as not directly comparable to a full-dataset
result.

## Read-only pose-result inspection

| Method and path | Contract |
| --- | --- |
| `GET /bop/inspection/setup?run_root=…&result_id=…` | Return compatible retained results, validated objects/scenes, sensor labels, capabilities, limits, and visualization contract |
| `GET /bop/inspection/frames?run_root=…&result_id=…&scene_id=…&filter=…&object_id=…&page=…&page_size=…` | Return a bounded paginated frame inventory and provenance-backed operation filters |
| `GET /bop/inspection/frame?run_root=…&result_id=…&scene_id=…&im_id=…&max_hypotheses=…` | Return camera intrinsics, GT, ranked estimates, score/timing, visibility, optional operation evidence, and safe media URLs |
| `GET /bop/inspection/media/<result_id>/<scene_id>/<im_id>/<kind>?run_root=…` | Serve ID-resolved RGB, or a bounded colorized depth PNG |
| `GET /bop/inspection/masks/<result_id>/<scene_id>/<im_id>/<gt_id>/<kind>?run_root=…` | Serve an ID-resolved full or visible GT mask when present |
| `GET /bop/inspection/models/<result_id>/<obj_id>?run_root=…` | Serve the hash-checked evaluation-model PLY |

Supported frame filters are `all`, `estimated`, `target`,
`missing_estimate`, `registration`, `tracking`, and `reinitialization`.
Operation labels are `unknown` unless sanitized retained provenance proves the
FoundationPose execution contract. These APIs write nothing and accept no
filesystem media/model path from the caller. Result IDs, scene/frame/object/GT
IDs, containment, regular-file status, symlinks, hashes, PNG dimensions, row
counts, hypotheses, page size, and media size are validated before response.

## External cluster controller proxy

| Method and path | Contract |
| --- | --- |
| `GET /cluster/status` | Return curated controller connectivity, storage, archive, and advertised-estimator status |
| `GET /cluster/controller-service` | Inspect the one fixed configured user-service |
| `POST /cluster/controller-service/<action>` | Queue allow-listed start/stop/restart action; browser cannot name a unit or command |
| `GET /cluster/archives` | List immutable-while-retained controller-side run archives |
| `POST /cluster/archives` | Submit archive creation for a locally validated run |
| `POST /cluster/archives/<archive_id>/restore` | Submit restore with local identity/active-job checks |
| `DELETE /cluster/archives/<archive_id>` | Queue permanent deletion of one opaque-ID archive after explicit confirmation and operator attribution |
| `GET /cluster/pose-estimation/setup?run_root=…&estimator_id=…` | Return strict setup v3 with browser-safe dataset identity, sensor sequences, and controller-advertised estimators/settings |
| `POST /cluster/pose-estimation/jobs` | Submit a typed controller job using an advertised estimator ID, profile, and closed estimator settings |
| `GET /cluster/jobs?run_root=…&estimator_id=…&limit=…` | List curated external jobs, optionally filtering on validated internal run identity and estimator before path-safe redaction; with a run, include durable retained-result collection state |
| `GET /cluster/jobs/<job_id>` | Inspect one curated external job |
| `POST /cluster/jobs/<job_id>/cancel` | Request controller cancellation |
| `POST /cluster/jobs/<job_id>/import-result` | Explicitly collect, revalidate, and idempotently retain a completed standard BOP19 result in the active run |

The proxy is enabled only by server configuration. Returned values are
allow-listed and scrubbed; controller URLs/tokens, SSH data, remote paths,
container commands, and arbitrary scheduler inputs are never accepted from or
returned to the browser. Imported results bind the controller provenance,
staged dataset hash, and local dataset hash. FoundationPose v2 import also
requires the payload, public result, downloaded provenance, and independently
recomputed selected-target hash to agree.

The UI labels the import endpoint **Collect result** and never invokes it as a
mount/reload side effect. Collection state is recovered by matching the
controller job's opaque ID against retained result records. Pose Estimation
uses the run- and estimator-filtered response as a bounded job selector. Jobs
keeps its global path-redacted history and separately requests the active run's
filtered history, exposing collect/inspect/evaluate actions only where that
second response proves ownership. Repeated same-run submissions and their
collected immutable result IDs remain distinct. FoundationPose v2
retention includes bounded track segments, fixed registration/tracking
iterations, per-image timings, and failure identities. Retention is capped at
200 recorded segments, 200 failure identities, and 10,000 image timings, with
omission counts retained when source evidence is larger. Free-form failure
messages, remote paths, controller secrets, commands, and scheduler arguments
are discarded rather than exposed.

FoundationPose v2 submission example:

```json
{
  "run_root": "working_data/example",
  "estimator_id": "foundationpose",
  "profile_id": "full",
  "operator": "Lab Operator",
  "estimator_settings": {
    "schema_version": "posetestbot_cluster_job_settings.v1",
    "execution_mode": "continuous_tracking",
    "selected_scene_ids": [1, 3]
  }
}
```

Setup sensor descriptors contain only BOP `scene_id`, a safe sensor identity,
the run-owned alias/display label, mounting mode, counts, and tracking
eligibility/blocker. Source paths and physical scheduler controls are never
included. Omitting `estimator_settings` remains the controller's legacy
compatibility request and means independent registration over all exported
target scenes.

Archive/storage readiness is independent from estimator runtime readiness. A
run can be archived, restored, or its retained archive can be deleted even when
no qualified estimator is advertised. Archive deletion removes only the
controller-owned cluster copy; it neither deletes nor accepts a path to the
local acquisition folder. The request body is closed to `confirm: true` and a
non-empty `operator`, while the proxy supplies its own idempotency key.
Controllers without the current domains-and-estimators status shape fail closed;
PoseTestBot never fabricates a default estimator or resource profile.
