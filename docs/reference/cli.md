# CLI and scripts

Run repository Python through `uv run python`. The operator console queues the
same fixed recipes through `LocalJobRunner`; the scripts are useful for
software checks and supervised terminal operation.

## Console and read-only status

```bash
uv run posetestbot-web
uv run python scripts/robot_status.py --json
uv run python scripts/sensor_status.py --json
uv run python scripts/sensor_adapters.py --json
uv run python scripts/runtime_status.py --json
```

Status commands do not authorize capture or robot motion.

## Run creation and fixed orchestration

| Command | Purpose |
| --- | --- |
| `scripts/create_run_config.py <run> --intent … --annotation-mode …` | Create strict `run_config.v4` and update the run manifest |
| `scripts/plan_capture.py <run> [--json]` | Write the canonical non-executing capture plan |
| `scripts/run_preflight.py <run> --check --write` | Write readiness evidence, including one non-recorded frame from each selected camera, without authorizing robot execution |
| `scripts/run_capture.py <run> --intent … --allow-cameras --allow-real-robot` | Internal/supervised worker for the one physical capture recipe |
| `scripts/process_dataset.py <run>` | Run sync → quality → rectification → calibrated base BOP export |

Example safe setup:

```bash
uv run python scripts/create_run_config.py working_data/test_run \
  --intent dataset --annotation-mode none
uv run python scripts/plan_capture.py working_data/test_run --json
```

There is no stage or sequence registry and no caller-supplied stage list.
`run_capture.py` touches cameras and the robot and must only be used with
explicit operator authorization. Its two flags are fresh acknowledgements,
not saved configuration.

The lower-level capture-plan, preflight, execution-plan, execution, sync,
quality, rectification, and BOP scripts remain implementation workers for the
fixed recipes. They are not alternative operator workflows.

## Calibration and reusable inputs

| Command | Purpose |
| --- | --- |
| `run_calibration_target_generate.py` | Generate a reusable target bundle |
| `run_calibration_target_import.py` | Import a pinned target bundle |
| `run_calibration_target_select.py` | Snapshot a target into a run |
| `run_calibration_attempt.py` | Execute one intent-level calibration attempt |
| `validate_calibration_profiles.py` | Validate current calibration profiles |
| `run_pose_template_orientation_analysis.py` | Analyze stable orientations for one canonical workpiece revision |
| `run_pose_template_preview.py` | Produce an exact bounded template preview |
| `run_pose_template_generate.py` | Publish an immutable pose-template bundle |
| `run_pose_template_select.py` | Snapshot a bundle into a dataset run |

Calibration review and promotion are explicit workflow/API operations. The
removed observations/candidates/solver/validation stage chain has no CLI
entry points.

## Optional annotations and Inspect evaluation

| Command | Purpose |
| --- | --- |
| `run_blenderproc_prepare_stage.py` | Prepare the explicit optional render inputs |
| `run_blenderproc_render_stage.py` | Render requested GT/masks through BlenderProc |
| `run_bop_annotations.py` | Generate the run-configured optional annotation product |
| `run_bop_evaluation.py` | Run the narrow official BOP19 evaluation adapter |

The base export is produced by `process_dataset.py`. Optional annotation is a
separate, deliberate step. Evaluation consumes an existing annotation-bearing
BOP dataset and immutable standard BOP19 CSV; it is not acquisition or
estimation.

## Documentation and validation

```bash
uv run python scripts/generate_http_api_reference.py --write
uv run python scripts/generate_http_api_reference.py --check
uv run python -m mkdocs build --strict
uv run pytest tests/test_github_pages.py
```
