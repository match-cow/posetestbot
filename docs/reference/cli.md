# CLI and scripts

Run repository scripts through `uv` from the repository root:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/<script>.py --help
```

## Installed service entry point

```bash
POSETESTBOT_WEB_HOST=127.0.0.1 \
POSETESTBOT_WEB_PORT=5000 \
uv run posetestbot-web
```

The web server owns a `LocalJobRunner` and releases its jobs during process
shutdown. It is unauthenticated; bind only to localhost or a trusted lab
network.

## Read-only status

| Command | Contract |
| --- | --- |
| `scripts/robot_status.py --json` | Read-only iiwa status; never authorizes motion |
| `scripts/sensor_status.py --json` | Current supported camera visibility |
| `scripts/sensor_adapters.py --json` | Static adapter registry; no hardware open |
| `scripts/runtime_status.py --json` | Acquisition-side optional runtime visibility |

## Run planning and orchestration

| Command | Contract |
| --- | --- |
| `scripts/create_run_config.py <run>` | Create validated `run_config.v3` |
| `scripts/run_preflight.py <run>` | Build/write run preflight evidence |
| `scripts/run_pipeline_sequence.py <run> --sequence <id> [--plan-only]` | Execute or plan a registered sequence |
| `scripts/run_capture_plan_stage.py <run>` | Write capture plan |
| `scripts/run_capture_plan_preflight.py <run>` | Write capture-plan preflight under its gate contract |
| `scripts/run_capture_execution_plan.py <run>` | Write gated execution plan; physical authorization rules apply |
| `scripts/run_capture_execution_stage.py <run>` | Execute supervised physical capture; do not run without authorization |

Plan-only example:

```bash
uv run python scripts/create_run_config.py working_data/test_run
uv run python scripts/run_pipeline_sequence.py working_data/test_run \
  --sequence real_full_capture_validation --plan-only
```

## Synchronization and quality

| Command | Contract |
| --- | --- |
| `scripts/sync_run_non_destructive.py <run>` | Create synchronized derived output without moving/deleting raw frames |
| `scripts/run_sync_quality.py <run>` | Compute/write in-motion timing quality |
| `scripts/report_robot_pose_cadence.py <run>` | Write optional pose cadence evidence |

Keep `sync_quality` immediately after `sync_run` in reusable pipeline
sequences unless an operator-facing contract explicitly bypasses it.

## Calibration

| Command family | Contract |
| --- | --- |
| `run_calibration_target_{generate,import,select}.py` | Manage reusable and run-selected target bundles |
| `run_aruco_{coverage,detection,pose,stage}.py` | Target detection/coverage support |
| `run_intrinsic_calibration_stage.py` | Estimate intrinsic profiles |
| `run_camera_rectification.py` | Derive rectification evidence |
| `run_calibration_{preflight,observations,candidates,solver,validation}.py` | Compatibility stage/report writers |
| `run_calibration_attempt.py` | Execute an intent-level attempt or explicit promotion |
| `validate_calibration_profiles.py` | Validate profile schema/frame contracts |

## Workpieces, templates, and BOP

| Command family | Contract |
| --- | --- |
| `run_object_catalog_import.py` | Import catalogue metadata/assets under catalogue rules |
| `run_workpiece_unit_correction.py` | Create a confirmed canonical geometry revision |
| `run_pose_template_{preview,generate,select}.py` | Validate, publish, and snapshot immutable templates |
| `run_pose_template_orientation_analysis.py` | Build reproducible orientation cache |
| `run_blenderproc_{prepare,render}_stage.py` | Prepare/render optional GT/mask inputs |
| `run_bop_export_stage.py` | Export the run as a BOP dataset |
| `run_bop_annotations.py` | Generate optional run-scoped pose/mask evidence |
| `run_bop_evaluation.py --request <path>` | Execute one immutable Inspect-only official-toolkit request |

No script in this repository should run an external pose estimator or convert
estimator-specific output.

## Documentation and validation

```bash
uv run python scripts/generate_http_api_reference.py --check
uv run --frozen --only-group docs mkdocs build --strict
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
git diff --check
```

The generated route reference must be refreshed with `--write` whenever the
Flask route map changes.
