# Installation and first launch

The authoritative full host setup is [`INSTALL.md`](https://github.com/match-cow/PoseTestBot/blob/main/INSTALL.md).
This page contains the minimum software-only path and the documentation build.
Neither path commands the robot or starts physical capture.

## Project environment

PoseTestBot requires Python 3.12 and uses `uv` for dependency management.

```bash
git clone https://github.com/match-cow/PoseTestBot.git
cd PoseTestBot
bash scripts/install.sh
```

The default installer runs `uv sync --all-groups`, verifies the bundled web
console, imports required Python modules, and performs acquisition-runtime and
adapter checks without opening hardware for capture.

To verify an existing environment without changing it:

```bash
bash scripts/install.sh --check-only
```

## Read-only status

```bash
uv run python scripts/robot_status.py --json
uv run python scripts/sensor_status.py --json
uv run python scripts/sensor_adapters.py --json
uv run python scripts/runtime_status.py --json
```

`runtime_status.py` checks acquisition-side optional runtimes such as
BlenderProc and the ZED Python module. Camera visibility belongs to sensor
status.

## Start the operator console

The console has no authentication and exposes deliberate lab controls. Bind it
to loopback for local work:

```bash
POSETESTBOT_WEB_HOST=127.0.0.1 uv run posetestbot-web
```

Open <http://127.0.0.1:5000/>. The default port is `5000`.

## Build this documentation

Documentation dependencies are isolated in the `docs` dependency group:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --only-group docs \
  mkdocs build --strict
```

For a loopback development server with rebuilds:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --only-group docs \
  mkdocs serve --dev-addr 127.0.0.1:8000
```

The generated `site/` directory is ignored. GitHub Actions builds it from the
checked-in Markdown and `mkdocs.yml` before deployment.

## Plan without executing

```bash
uv run python scripts/create_run_config.py working_data/test_run
uv run python scripts/run_pipeline_sequence.py working_data/test_run \
  --sequence real_full_capture_validation --plan-only
```

Planning is safe to run without physical authorization. It does not weaken the
fresh execution gates required by the capture API.
