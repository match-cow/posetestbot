# Installation and first launch

Inspect's [IPD robot-consistency metric](../concepts/robot-consistency.md) uses
the existing NumPy, SciPy, pytransform3d, and trimesh dependencies; no additional
IPD runtime is installed. The installer smoke-checks its module, and the shared
BOP evaluation job requires the pinned official toolkit.

Start here to install PoseTestBot, launch the console, and enable MP4 downloads.
Run the commands below from the repository root. These setup commands do not
command the robot or start physical capture. Optional camera SDKs, BlenderProc,
and BOP Toolkit have detailed instructions in the
[lab SDK and runtime setup guide](https://github.com/match-cow/PoseTestBot/blob/main/INSTALL.md).

## Project environment

PoseTestBot requires Python 3.12 and uses `uv` for dependency management.

```bash
git clone https://github.com/match-cow/PoseTestBot.git
cd PoseTestBot
bash scripts/install.sh
```

The default installer runs `uv sync --all-groups --locked`, verifies the bundled web
console, imports required Python modules, and performs acquisition-runtime and
adapter checks without opening hardware for capture.

The committed lockfile includes the patched application dependencies; an
out-of-date lockfile fails installation instead of silently resolving different
versions. Rerun the installer after updating the checkout. Current minimums are
Flask 3.1.3, aiohttp 3.14.3, and Pillow 12.3.

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

## Pose Results MP4 exports

**Inspect → Pose Results → Create & download MP4** requires FFmpeg with the
`libx264` encoder. On Ubuntu/Debian, install the system packages and check the
environment:

```bash
bash scripts/install.sh --with-system-packages
bash scripts/install.sh --check-only
```

The system-package step uses `sudo` and installs the project's lab prerequisites,
including `ffmpeg` and `ffprobe`. The check reports whether `libx264` is available.
Return to Pose Results after installation to enable MP4 exports.

Missing FFmpeg disables MP4 with a visible explanation. **Download images (.zip)**
works with the project's Python environment. This optional renderer does not
affect acquisition runtime readiness.

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
uv run python scripts/create_run_config.py working_data/test_run \
  --intent dataset --annotation-mode none
uv run python scripts/plan_capture.py working_data/test_run --json
```

Planning is safe to run without physical authorization. It does not weaken the
fresh execution gates required by the capture API.
