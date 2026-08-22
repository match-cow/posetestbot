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
saved execution gates are rejected. Once a capture execution attempt has
published its plan, status, log, report, robot poses, or sensor evidence, the
run configuration is frozen; use a new run instead of rewriting its acquisition
contract.

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

`intent` must match `run_config.json`, and a fresh successful run preflight is
required. Only `run_preflight.v2`, with active evidence matching every currently
selected camera, can authorize queueing. The server runs one recipe: plan →
capture-plan preflight → execution plan → supervised execution →
capture-completion validation. Cancel requests cannot interrupt active IIWA
motion and never send the idle-program exit command.

Before the recipe writes its first capture artifact, the capture worker repeats
the bounded selected-camera open probe. Failure rejects capture without creating
`capture_plan.json`, execution metadata/logs, or raw camera output, so the same
run remains usable after the camera/backend issue is corrected.

After that open probe succeeds, the worker takes one adjacent SDK/device-status
snapshot and reuses it while it deterministically rebuilds the plan, preflight,
and execution plan. The supervisor still requires each real recorder to publish
three committed frame records before robot motion; repeated discovery calls do
not add readiness evidence and are avoided because they can destabilize USB/SDK
state immediately before acquisition. A device-status record participates only
when both current `connected` and `capture_ready` fields are literally `true`;
missing flags are malformed evidence, not implied readiness.

The accepted execution plan is `capture_execution_plan.v2` and hash-binds the
entire validated `run_config.v4`. The supervisor rechecks that digest before
every camera child, immediately before robot receiver `START`, after receiver
completion, and before and after completion validation. A change preserves any
raw camera or robot evidence but fails the execution; completion is evaluated
only against the accepted configuration snapshot. A nonblocking run-directory
lock rejects a second concurrent supervisor. Each attempt receives a unique
immutable directory below `capture_execution_logs/<execution_id>/` containing
its exact plan, final status, report, and child logs, so a later no-output retry
cannot overwrite earlier diagnostics. The root plan/status/report remain the
current attempt for the console. Sensor outputs must be absent canonical direct
children of the run root, with no symlink traversal; containment and absence are
rechecked immediately before every camera child spawn, including retries.

During robot motion, the receiver owns the hidden
`.raw_robot_ee_poses.claim.json` v2 reservation, bound to the run and receiver
process, and appends accepted packets to a current-schema
`raw_robot_ee_poses.journal.<claim_id>.jsonl`. One exclusive lifecycle lock
serializes claim creation with startup recovery. Commit markers are flushed and
fsynced after at most 32 poses or 250 ms. The canonical
`raw_robot_ee_poses.json` remains absent until an accepted `motion=end` packet is
included in a terminal durable commit, after which publication is atomic and
no-overwrite. Startup recovery refuses a journal still locked by a live
receiver. An inactive terminal journal finalizes the canonical artifact;
otherwise recovery materializes every validated committed packet in
`raw_robot_ee_poses.partial.<...>.json` and retains the journal as
`raw_robot_ee_poses.journal.<claim_id>.recovered.jsonl`. Any such recovered raw
evidence blocks reuse of the run for a second physical capture. If the process
dies after publishing its claim but before publishing the journal, recovery
retains `raw_robot_ee_poses.claim.<claim_id>.recovered.json` and a zero-pose
partial report instead of leaving an unowned reservation or silently retrying.

## Dataset processing

`POST /dataset-processing/jobs` accepts `run_root` and queues the immutable
recipe:

```text
non-destructive sync → sync quality → RGB-D rectification → calibrated base BOP export
```

It requires dataset intent, a valid current reusable-calibration selection, and
`capture_execution_report.json` with schema `capture_execution_report.v2` and
top-level `status: succeeded`. Its run-configuration digest, embedded accepted
plan/snapshot, current plan/status, and per-execution archive must agree exactly.
The embedded `capture_completion.v1` must have `status: ok`. Queue-time
validation is bounded to configuration and capture/archive provenance so the
Flask request never scans a large image set. Before its first derived command,
the queued worker repeats that gate and rebuilds capture completion against the
current raw evidence. Missing, unreadable, changed, or incomplete capture
evidence fails closed. The recipe never rewrites raw capture. Optional pose or
mask generation is deliberately separate under `/bop/annotations`.

Capture completion decodes every referenced PNG, requires supported RGB/RGBA
and unsigned-16-bit depth pixels at the configured dimensions, and validates
exact frame membership. Host-receipt and host-wall timestamps must both be
positive and strictly increase in capture order. Sensor, RGB, depth, and raw
robot-pose paths must be regular non-symlink evidence. The dimensions recorded
in `camera_data.json` must
match those pixels and the configured resolution, while `cam_K.txt`,
`camera.json`, and `camera_data.json` must agree on intrinsics and any recorded
distortion/projection provenance. Focal lengths must be positive and the
intrinsic bottom row must be `[0, 0, 1]`; consistently corrupt copies are not
accepted merely because they agree. The two depth-scale sidecars must agree.
Current SDK coefficient sets of 4, 5, 8, 12, or 14 values remain valid; the
five-term calibration snapshot is derived explicitly later. The shared writer
validates scalar frame identity and positive timestamp metadata before creating
frame outputs. The final retained robot pose must also carry the accepted
protocol `end` packet, including its terminal sequence/loss delta; the absence
of that stream-closure evidence is not a successful capture.

## Removed surfaces

`/pipeline/*`, `/capture-plan*`, and `/run-command` are not registered. Clients
must use the purpose-specific routes above and `/robot/commands`.

## Synchronization quality

`GET /sync/quality?run_root=…` loads existing evidence. `POST /sync/quality`
writes the current report after strict synchronized inputs exist. Quality is
based on eligible in-motion frames, pose delta, packet loss, timestamp
evidence, and unexplained exclusions—not lead-in/tail frames. Synchronization
accepts only current `robot_pose.v1` source packets with non-negative integer
`sequence`, `sequence_delta`, and `estimated_packets_lost` fields whose values
agree with one another and with stream order. Missing, malformed, or
inconsistent packet-loss evidence fails synchronization; sync quality reports
an error rather than silently omitting the packet-loss audit.

Managed synchronization reads raw enabled-sensor folders and writes one
transactional generation only below `<run>/processed/synchronized`. Each
`sync_report.v4` shares that generation ID and hash-binds the exact raw RGB,
depth, metadata/sidecar, and robot-pose inputs plus the complete derived sensor
tree. Every managed input must be the configured canonical sensor directory
directly below the run root; an empty selection, subset, or nested same-name
lookalike fails exact coverage. Input mutation during copying aborts
publication, and one coherent generation is required downstream. Custom roots
and pairing-only output are library-only diagnostic contracts and cannot
replace the managed result.

Managed rectification reads only `<run>/processed/synchronized`, writes only
`<run>/processed/rectified`, and requires exact enabled-sensor coverage,
metadata/matched-pose membership, finite poses, and source/output fingerprints.
Library diagnostics require `diagnostic_unmanaged=True`, explicit isolated
roots, no overlap, and a fresh output destination. Managed BOP export likewise
accepts only the canonical synchronized or rectified input root and writes only
`<run>/bop`; legacy path flags are canonical assertions, not arbitrary output
selection.
