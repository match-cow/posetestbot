# PoseTestBot technical documentation

PoseTestBot acquires synchronized robot and RGB-D evidence, calibrates the
camera system, validates the capture, and exports a BOP dataset. This site is
the technical reference for that repository boundary: operator workflows,
runtime architecture, HTTP APIs, file formats, and safety contracts.

## Repository boundary

PoseTestBot owns:

- capture planning and supervised physical acquisition;
- RealSense, OAK-D Pro, and ZED sensor adapters;
- calibration, timestamp alignment, and synchronization-quality evidence;
- reusable workpiece and pose-template libraries with immutable run snapshots;
- optional GT/mask generation and BOP dataset export; and
- run-scoped Inspect evaluation of an existing BOP dataset and standard BOP19
  result CSV.

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
```

Raw capture data is retained. Synchronization, annotation, export, and
evaluation write derived artifacts instead of replacing the only copy of the
evidence.

## Primary interfaces

| Interface | Contract | Start here |
| --- | --- | --- |
| Operator console | Desktop-first supervised lab workflow | [Operator workflows](OPERATOR_WORKFLOWS.md) |
| Flask HTTP API | Trusted-LAN JSON/file interface used by the console | [API conventions](reference/http-api.md) |
| Command-line scripts | Status and fixed capture/processing recipes | [CLI and scripts](reference/cli.md) |
| Run directory | Immutable inputs and reproducible raw/derived artifacts | [Runs and artifact lineage](concepts/runs-and-artifacts.md) |

## Current hardware profile

- 3 Intel RealSense D435-class cameras
- 1 Luxonis OAK-D Pro
- 1 Stereolabs ZED 2i
- KUKA LBR iiwa at `172.31.1.147:30300`
- lab receiver at `172.31.1.169`

Hardware status is read-only. Physical capture requires explicit operator
authorization and both fresh execution safety gates. A successful status or
preflight response is evidence about a check; it is not permission to move the
robot.

## Fast paths

- Set up a development or lab host: [Installation and first launch](getting-started/installation.md)
- Understand process and trust boundaries: [Architecture and boundaries](concepts/architecture.md)
- Find an HTTP endpoint: [Complete route index](reference/http-api-routes.md)
- Inspect the run file contract: [Artifact index](reference/artifacts.md)
- Review the five operator-run acceptance tasks: [Physical commissioning](COMMISSIONING.md)
