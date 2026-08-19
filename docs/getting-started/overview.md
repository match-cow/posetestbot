# System overview

PoseTestBot is a Python 3.12 application with a Flask backend, a bundled React
operator console, a local background-job runner, and filesystem-backed run and
catalogue state. The normal operating unit is a **run directory**, not a
database record.

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
8. Optionally generate run-scoped GT/masks or validate a standard BOP19 result
   through the Inspect-only evaluation path.

Long-running submissions return a job identifier. They continue when the
browser navigates away and remain inspectable through `/jobs` and the console's
**Jobs** page.

## What to read next

- [Operator workflows](../OPERATOR_WORKFLOWS.md) for the two guided outcomes.
- [Architecture and boundaries](../concepts/architecture.md) for ownership and
  process separation.
- [Run configuration](../reference/run-config.md) for `run_config.v4`.
- [API conventions](../reference/http-api.md) for HTTP usage.
