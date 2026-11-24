# System overview

This section is the technical reference. For task-oriented instructions,
start with the [Quickstart](../quickstart.md) and the user guide.

PoseTestBot is a Python 3.12 application with a Flask backend, a bundled React
operator console, a local background-job runner, and filesystem-backed run and
catalogue state. The normal operating unit is a **run directory**, not a
database record.

## Repository boundary

PoseTestBot owns:

- capture planning and supervised physical acquisition;
- RealSense, OAK-D Pro, and ZED sensor adapters;
- calibration, timestamp alignment, and synchronization-quality evidence;
- reusable workpiece and pose-template libraries with immutable run snapshots;
- optional GT/mask generation and BOP dataset export; and
- run-scoped frame inspection, requested visualization downloads, and official evaluation of an existing
  BOP dataset and immutable standard BOP19 result CSV.

Pose estimators, estimator-specific conversion, SSH credentials, and SLURM
orchestration are outside this repository. The optional cluster integration is
a loopback-only client for the separate `posetestbot-cluster` controller.

## Data flow

```text
global libraries                run-owned evidence
----------------              -----------------------------------------------
sensor aliases      ────────▶  run_config.json
calibration bundles ────────▶  immutable calibration snapshot
workpiece geometry  ────────▶  immutable pose-template selection
                                      │
                                      ▼
                              supervised RGB-D + robot capture
                                      │
                                      ▼
                              non-destructive synchronization
                                      │
                                      ▼
                              sync and calibration quality gates
                                      │
                                      ▼
                              BOP export ──▶ optional GT and masks
                                      │
                                      ▼
                            retained BOP19 result
                                      │
                         ┌────────────┴────────────┐
                         ▼                         ▼
                  pose/GT inspection       official evaluation
```

Raw capture data is retained. Synchronization, annotation, export, and
evaluation write derived artifacts instead of replacing the only copy of the
evidence.

## Primary interfaces

| Interface | Contract | Start here |
| --- | --- | --- |
| Operator console | Desktop-first supervised lab workflow | [Operator workflows](../OPERATOR_WORKFLOWS.md) |
| Flask HTTP API | Trusted-LAN JSON/file interface used by the console | [API conventions](../reference/http-api.md) |
| Command-line scripts | Status and fixed capture/processing recipes | [CLI and scripts](../reference/cli.md) |
| Run directory | Immutable inputs and reproducible raw/derived artifacts | [Runs and artifact lineage](../concepts/runs-and-artifacts.md) |

## Current hardware profile

- RGB-D cameras: Intel RealSense D435 is the current main camera. Adapters
  for the Luxonis OAK-D Pro and Stereolabs ZED 2i are also supported.
- KUKA LBR iiwa at `172.31.1.147:30300`
- lab receiver at `172.31.1.169`

Hardware status is read-only. Physical capture requires explicit operator
authorization and both fresh execution safety gates. A successful status or
preflight response is evidence about a check; it is not permission to move the
robot.

## Runtime components

| Component | Location | Responsibility |
| --- | --- | --- |
| Flask application | `posetestbot.web.app` | Serves the console and browser-safe HTTP API |
| Operator console | `frontend/`, built into `posetestbot/web/static/ui/` | Guided desktop workflow and evidence inspection |
| Local job runner | `posetestbot.jobs.runner.LocalJobRunner` | Runs long or hardware-touching work outside request handlers |
| Fixed orchestration | `posetestbot.pipeline.orchestration` | Owns the only capture and dataset-processing recipes |
| Sensor registry | `posetestbot.sensors.registry` | Static supported-device metadata; does not open hardware |
| Run storage | `working_data/`, `/mnt/working_data_ssd` | Raw evidence, immutable inputs, derived artifacts, and exports |
| Global libraries | `working_data/object_catalog/`, `working_data/pose_templates/` | Reusable workpieces and immutable pose-template bundles |
| Cluster client | `posetestbot.cluster` | Optional loopback boundary to the separately deployed controller |

## Normal operating sequence

1. Create or select a run and write `run_config.json`.
2. Snapshot reusable calibration and pose-template inputs into that run.
3. Queue preflight, then inspect the fixed capture plan and readiness evidence.
4. Obtain explicit operator authorization and submit both execution gates.
5. Capture independent timestamp evidence from every enabled camera and the
   robot pose stream.
6. Synchronize non-destructively and evaluate in-motion coverage and timing.
7. Export the selected frames, calibration, object models, and provenance as a
   BOP dataset.
8. Optionally generate run-scoped GT/masks, explicitly collect a standard
   BOP19 result from the external controller, inspect frame-level pose/GT
   overlays, and validate that selected result through the Inspect-only
   official evaluation path.

Long-running submissions return a job identifier. They continue when the
browser navigates away and remain inspectable through `/jobs` and the console's
**Jobs** page.

## What to read next

- [Operator workflows](../OPERATOR_WORKFLOWS.md) for the two guided outcomes.
- [Architecture and boundaries](../concepts/architecture.md) for ownership and
  process separation.
- [Run configuration](../reference/run-config.md) for `run_config.v4`.
- [API conventions](../reference/http-api.md) for HTTP usage.
