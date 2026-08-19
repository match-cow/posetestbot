> [!WARNING]
> The repository states used for the CMS and HRI publications are preserved in
> the `CMS` and `HRI` branches, respectively.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="posetestbot/web/static/cow_dark.png">
    <source media="(prefers-color-scheme: light)" srcset="posetestbot/web/static/cow_light.png">
    <img src="posetestbot/web/static/cow_light.png" alt="PoseTestBot cow logo" width="96">
  </picture>
</p>

<h1 align="center">PoseTestBot</h1>

<p align="center">
  <strong>From supervised multi-camera capture to traceable BOP datasets.</strong>
</p>

<p align="center">
  <a href="https://match-cow.github.io/posetestbot/"><strong>Technical documentation →</strong></a><br>
  <sub>Architecture, operator workflows, HTTP APIs, schemas, artifacts, and command reference.</sub>
</p>

PoseTestBot is an acquisition-first system for building 6D object-pose
datasets from robot-mounted and static RGB-D cameras. It brings camera
calibration, robot-aware capture, synchronization, optional ground-truth
generation, and BOP export into one operator-guided workflow.

The goal is not simply to collect frames. It is to produce a dataset whose
origin can be understood and reproduced: raw evidence stays untouched, every
dataset-derived result belongs to its run, and exports remain bound to the exact
calibration, timing, geometry, and object inputs that created them.

## The Workflow

The desktop console guides two outcomes:

1. **Camera calibration** — configure the camera rig and target, supervise a
   capture, run one intent-level calibration attempt, review its evidence, and
   explicitly promote reusable profiles.
2. **Object dataset** — snapshot a calibration and pose template into a run,
   supervise physical capture, synchronize and validate the data, then export
   a BOP dataset.

```text
reusable inputs → run snapshot → fixed supervised capture recipe
                                   │
                                   └→ fixed sync → quality → rectify → BOP
                                                                    │
                                                                    └→ optional GT/masks
```

Calibration targets feed calibration runs. Workpieces feed immutable pose
templates. Dataset runs snapshot the selected calibration and pose template,
so later library edits cannot silently change an existing dataset.
Long-running work is queued, recoverable, and visible in **Jobs** after
navigation.

## What a Run Produces

- preserved RGB-D frames, timestamps, and robot poses;
- calibration, synchronization, and quality evidence;
- optional object-pose ground truth, masks, and visibility evidence; and
- a standard BOP dataset with camera parameters, object models, targets, and
  compact PoseTestBot provenance.

The base export is useful without annotations. Pose ground truth can be added
when object placement is known, while the full annotation mode adds masks and
visibility data for evaluation-ready datasets.

## Core Principles

- **Raw data is evidence.** Processing writes derived artifacts instead of
  rewriting or deleting the only copy of a capture.
- **A run is self-contained.** Configuration and reusable inputs are
  snapshotted and hash-bound to their outputs.
- **Hardware action is explicit.** Readiness checks never authorize motion or
  start physical capture.
- **Failures stay inspectable.** Partial captures, logs, reports, and job state
  are retained for diagnosis.
- **The output is estimator-agnostic.** Pose-estimation methods consume the
  exported dataset elsewhere.

## Repository Boundary

PoseTestBot's acquisition boundary ends at a validated BOP dataset. It does
not contain or execute pose-estimator code and does not convert proprietary
estimator output.

The **Inspect → BOP Evaluation** page is intentionally limited to dataset
validation. It can apply the pinned official BOP19 metrics to an already
compatible result CSV, or to a clearly labelled deterministic test result
derived from ground truth. Evaluation evidence remains run-scoped and is never
an acquisition stage.

The optional **Inspect → Pose Estimation** page is a thin integration with the
separate [`match-cow/posetestbot-cluster`](https://github.com/match-cow/posetestbot-cluster)
companion. That loopback-only controller owns SSH credentials,
immutable-while-retained run archives, BIGWORK staging, durable SLURM state,
an estimator-driver registry,
qualified private runtimes, and standard BOP19 CSV generation. Archive and
restore, plus confirmed archive deletion, remain usable without any pose
method. Estimators are available only when the controller advertises a
qualified current driver. PoseTestBot
revalidates the active run,
proxies browser-safe requests, imports a completed CSV through its existing
BOP19 validator, and retains external-job/container/input/output provenance.
It never becomes an acquisition stage. The browser never receives a controller
token or cluster credential. Private runtimes, licenses, remote paths, and
scheduler details remain companion-owned.
The Dashboard reports storage/archive readiness separately from qualified
estimator readiness. It may also inspect, start, and stop one fixed server-configured
user-systemd controller unit. Those lifecycle actions are queued local jobs;
the browser cannot provide a command, unit name, environment value, or
credential. The controller card links to the **Run folders** page's top-level
**Cluster storage** archive/restore/delete panel, and **Pose Estimation**
provides the same return handoff. Permanent archive deletion is a confirmed,
operator-attributed controller job and never deletes the local acquisition
folder.

Deployment and qualification instructions live in the companion repository.

## Lab Context and Safety

The current cell uses a KUKA LBR iiwa with three Intel RealSense D435-class
cameras, one Luxonis OAK-D Pro, and one Stereolabs ZED 2i. Physical capture
requires an operator, explicit authorization, and both execution safety gates.

The iiwa UDP `STOP` command is not a safety stop: it cannot interrupt active
motion and exits the waiting calibration application. Do not use it between
calibration captures.

## Run the Console

PoseTestBot uses Python 3.12 and `uv`. Follow the
[installation guide](INSTALL.md) for SDK and optional-tool setup, then start
the operator console with:

```bash
uv run posetestbot-web
```

The console is unauthenticated and can expose deliberate robot controls. Run
it only on the trusted lab network, or bind it to localhost for local use.

## Documentation

- [Technical documentation](https://match-cow.github.io/posetestbot/)
- [GitHub Pages maintenance](docs/GITHUB_PAGES.md)
- [Operator workflows](docs/OPERATOR_WORKFLOWS.md)
- [Installation and runtime requirements](INSTALL.md)
- [Workpiece Catalogue](docs/WORKPIECE_CATALOGUE.md)
- [Pose templates and object ground truth](docs/POSETEMPLATECREATOR_OBJECT_GT.md)
- [Physical commissioning](docs/COMMISSIONING.md)
- [Contributor and safety rules](AGENTS.md)
