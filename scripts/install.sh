#!/usr/bin/env bash
set -Eeuo pipefail

CHECK_ONLY=false
WITH_SYSTEM_PACKAGES=false
WITH_BLENDERPROC=false
WITH_PLAYWRIGHT_BROWSERS=false
WITH_WEB_BUILD=false
WITH_POSEGRIDGEN=false
WITH_POSETEMPLATECREATOR=false
WITH_BOP_TOOLKIT=false
SKIP_RUNTIME_CHECKS=false

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

usage() {
  cat <<'EOF'
PoseTestBot installer

Usage:
  bash scripts/install.sh [options]

Options:
  --check-only             Verify the current environment without installing.
  --with-system-packages   Install common Ubuntu packages for local lab use.
  --with-blenderproc       Install BlenderProc as a uv tool when missing.
  --with-playwright-browsers
                           Install Chromium for Playwright browser UI tests.
  --with-web-build         Reinstall locked Bun packages and rebuild the bundled UI.
  --with-posegridgen       Initialize and verify the pinned PoseGridGen submodule.
  --with-posetemplatecreator
                           Initialize and verify the pinned PoseTemplateCreator submodule.
  --with-bop-toolkit       Initialize the pinned BOP Toolkit and sync its isolated uv runtime.
  --skip-runtime-checks    Skip runtime and sensor adapter verification.
  -h, --help               Show this help text.

Default behavior is a safe project bootstrap:
  - ensure uv is available,
  - run uv sync --all-groups,
  - run lightweight PoseTestBot readiness checks.

Vendor SDKs such as the Stereolabs ZED SDK are reported by the checks but are
not downloaded or installed by this script.
EOF
}

log() {
  printf '\n[install] %s\n' "$*"
}

warn() {
  printf '\n[install:warning] %s\n' "$*" >&2
}

die() {
  printf '\n[install:error] %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run() {
  printf '[install] +'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

run_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    run "$@"
  elif command_exists sudo; then
    run sudo "$@"
  else
    die "sudo is required for --with-system-packages, but sudo is not available."
  fi
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --check-only)
        CHECK_ONLY=true
        ;;
      --with-system-packages)
        WITH_SYSTEM_PACKAGES=true
        ;;
      --with-blenderproc)
        WITH_BLENDERPROC=true
        ;;
      --with-playwright-browsers)
        WITH_PLAYWRIGHT_BROWSERS=true
        ;;
      --with-web-build)
        WITH_WEB_BUILD=true
        ;;
      --with-posegridgen)
        WITH_POSEGRIDGEN=true
        ;;
      --with-posetemplatecreator)
        WITH_POSETEMPLATECREATOR=true
        ;;
      --with-bop-toolkit)
        WITH_BOP_TOOLKIT=true
        ;;
      --skip-runtime-checks)
        SKIP_RUNTIME_CHECKS=true
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
    shift
  done
}

verify_repo_root() {
  [[ -f "${REPO_ROOT}/pyproject.toml" ]] || die "pyproject.toml was not found at ${REPO_ROOT}."
  [[ -f "${REPO_ROOT}/AGENTS.md" ]] || die "AGENTS.md was not found at ${REPO_ROOT}."
  cd "${REPO_ROOT}"
  log "Repository: ${REPO_ROOT}"
}

configure_uv_cache() {
  if [[ -z "${UV_CACHE_DIR:-}" ]]; then
    export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
  fi
  mkdir -p "${UV_CACHE_DIR}" || die "Unable to create UV_CACHE_DIR at ${UV_CACHE_DIR}."
  log "UV cache: ${UV_CACHE_DIR}"
}

ensure_uv() {
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

  if command_exists uv; then
    run uv --version
    return
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    die "uv is not installed. Install uv or run this script without --check-only."
  fi

  log "uv was not found; installing with the official Astral installer."
  if command_exists curl; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command_exists wget; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    die "Install curl or wget first so the uv installer can be downloaded."
  fi

  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  command_exists uv || die "uv installation finished, but uv is still not on PATH."
  run uv --version
}

install_system_packages() {
  if [[ "${WITH_SYSTEM_PACKAGES}" != true ]]; then
    return 0
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    warn "--check-only was provided; skipping Ubuntu package installation."
    return
  fi

  command_exists apt-get || die "--with-system-packages currently supports Ubuntu/Debian hosts with apt-get."

  local packages=(
    build-essential
    ca-certificates
    curl
    git
    libgl1
    libglib2.0-0
    libusb-1.0-0
    libusb-1.0-0-dev
    pkg-config
    udev
    v4l-utils
  )

  log "Installing common Ubuntu packages for PoseTestBot lab use."
  run_sudo apt-get update
  run_sudo apt-get install -y "${packages[@]}"
  warn "Camera vendor udev rules may still be required for RealSense, OAK-D Pro, and ZED devices."
}

sync_python_environment() {
  if [[ "${CHECK_ONLY}" == true ]]; then
    if [[ ! -d ".venv" ]]; then
      warn ".venv is missing. Run 'bash scripts/install.sh' to create the uv environment."
    fi
    log "Skipping uv sync because --check-only was provided."
    return
  fi

  log "Synchronizing the uv Python environment."
  run uv sync --all-groups
}

install_posegridgen() {
  if [[ "${WITH_POSEGRIDGEN}" != true ]]; then
    return 0
  fi
  command_exists git || die "git is required for --with-posegridgen."
  local checkout="${REPO_ROOT}/third_party/PoseGridGen"
  local revision="9e6975901fe096bf65f7b7b599d7b82461d2e67c"
  if [[ "${CHECK_ONLY}" != true ]]; then
    log "Initializing the pinned PoseGridGen source checkout."
    run git submodule update --init --checkout third_party/PoseGridGen
  fi
  [[ -d "${checkout}" ]] || die "PoseGridGen checkout is missing; rerun without --check-only."
  [[ -f "${checkout}/backend/models.py" ]] || die "PoseGridGen backend files are missing."
  local actual_revision
  actual_revision="$(git -C "${checkout}" rev-parse HEAD)"
  [[ "${actual_revision}" == "${revision}" ]] || die "PoseGridGen is at ${actual_revision}; required ${revision}."
  [[ -z "$(git -C "${checkout}" status --porcelain --untracked-files=all)" ]] || die "PoseGridGen checkout is dirty."
  log "PoseGridGen checkout is clean and pinned to ${revision}."
}

install_posetemplatecreator() {
  if [[ "${WITH_POSETEMPLATECREATOR}" != true ]]; then
    return 0
  fi
  command_exists git || die "git is required for --with-posetemplatecreator."
  local checkout="${REPO_ROOT}/third_party/PoseTemplateCreator"
  local revision="97ddb9b7b756912deb8c2d2d6dde186b461e5d9d"
  if [[ "${CHECK_ONLY}" != true ]]; then
    log "Initializing the pinned PoseTemplateCreator source checkout."
    run git submodule update --init --checkout third_party/PoseTemplateCreator
  fi
  [[ -d "${checkout}" ]] || die "PoseTemplateCreator checkout is missing; rerun without --check-only."
  [[ -f "${checkout}/backend/mesh.py" ]] || die "PoseTemplateCreator backend files are missing."
  local actual_revision
  actual_revision="$(git -C "${checkout}" rev-parse HEAD)"
  [[ "${actual_revision}" == "${revision}" ]] || die "PoseTemplateCreator is at ${actual_revision}; required ${revision}."
  [[ -z "$(git -C "${checkout}" status --porcelain --untracked-files=all)" ]] || die "PoseTemplateCreator checkout is dirty."
  log "PoseTemplateCreator checkout is clean and pinned to ${revision}."
}

install_bop_toolkit() {
  if [[ "${WITH_BOP_TOOLKIT}" != true ]]; then
    return 0
  fi
  command_exists git || die "git is required for --with-bop-toolkit."
  local checkout="${REPO_ROOT}/third_party/bop_toolkit"
  local runtime="${REPO_ROOT}/tools/bop_toolkit_runtime"
  local runtime_python="${runtime}/.venv/bin/python"
  local revision="cea62d651c7e395b2e1962b9749e4e89693c6ac4"
  if [[ "${CHECK_ONLY}" != true ]]; then
    log "Initializing the pinned BOP Toolkit source checkout."
    run git submodule update --init --checkout third_party/bop_toolkit
  fi
  [[ -d "${checkout}" ]] || die "BOP Toolkit checkout is missing; rerun without --check-only."
  [[ -f "${checkout}/scripts/eval_calc_errors.py" ]] || die "BOP Toolkit error-evaluation script is missing."
  [[ -f "${checkout}/scripts/eval_calc_scores.py" ]] || die "BOP Toolkit score-evaluation script is missing."
  [[ -f "${runtime}/pyproject.toml" ]] || die "BOP Toolkit runtime project is missing."
  [[ -f "${runtime}/uv.lock" ]] || die "BOP Toolkit runtime lock is missing."
  local actual_revision
  actual_revision="$(git -C "${checkout}" rev-parse HEAD)"
  [[ "${actual_revision}" == "${revision}" ]] || die "BOP Toolkit is at ${actual_revision}; required ${revision}."
  [[ -z "$(git -C "${checkout}" status --porcelain --untracked-files=all)" ]] || die "BOP Toolkit checkout is dirty."
  log "BOP Toolkit checkout is clean and pinned to ${revision}."

  if [[ "${CHECK_ONLY}" != true ]]; then
    log "Synchronizing the isolated BOP Toolkit uv environment."
    run uv sync --project "${runtime}" --frozen
  fi
  [[ -x "${runtime_python}" ]] || die "BOP Toolkit runtime is missing; rerun without --check-only."

  log "Checking the isolated BOP Toolkit runtime."
  run "${runtime_python}" -c '
from pathlib import Path

import bop_toolkit_lib
import vispy
from bop_toolkit_lib import dataset_params, inout

expected = (Path.cwd() / "third_party" / "bop_toolkit").resolve()
actual = Path(bop_toolkit_lib.__file__).resolve()
if expected not in actual.parents:
    raise SystemExit(f"bop_toolkit_lib loaded from {actual}, expected {expected}")
print(f"BOP Toolkit runtime OK ({actual})")
'
}

install_blenderproc() {
  if [[ "${WITH_BLENDERPROC}" != true ]]; then
    return 0
  fi

  export PATH="${HOME}/.local/bin:${PATH}"

  if command_exists blenderproc; then
    local blenderproc_version
    blenderproc_version="$(blenderproc -v 2>/dev/null || true)"
    if [[ "${blenderproc_version}" == "2.8.0" ]]; then
      log "BlenderProc 2.8.0 already on PATH: $(command -v blenderproc)"
      return
    fi
    if [[ "${CHECK_ONLY}" == true ]]; then
      warn "BlenderProc ${blenderproc_version:-unknown} is on PATH; version 2.8.0 is required."
      return
    fi
    warn "Replacing BlenderProc ${blenderproc_version:-unknown} with required version 2.8.0."
    run uv tool install --force blenderproc==2.8.0
    return
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    warn "BlenderProc is not on PATH. Run without --check-only and include --with-blenderproc to install it."
    return
  fi

  log "Installing BlenderProc as a uv tool."
  run uv tool install blenderproc==2.8.0
  export PATH="${HOME}/.local/bin:${PATH}"
  command_exists blenderproc || warn "BlenderProc was installed but is not on PATH; check uv's tool install output."
}

install_playwright_browsers() {
  if [[ "${WITH_PLAYWRIGHT_BROWSERS}" != true ]]; then
    return 0
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    warn "Playwright browser installation requested, but --check-only was provided."
    return
  fi

  log "Installing Chromium for Playwright browser UI tests."
  run uv run playwright install chromium
}

build_web_console() {
  if [[ "${WITH_WEB_BUILD}" != true ]]; then
    return 0
  fi
  if [[ "${CHECK_ONLY}" == true ]]; then
    warn "Web build requested, but --check-only was provided; verifying bundled assets only."
    return 0
  fi
  command_exists bun || die "Bun is required by --with-web-build. Install Bun, then retry."
  log "Installing the locked frontend dependencies and rebuilding the operator console."
  run bun install --cwd frontend --frozen-lockfile
  run bun run --cwd frontend build
}

verify_web_console() {
  local ui_root="${REPO_ROOT}/posetestbot/web/static/ui"
  local cell_asset="${REPO_ROOT}/posetestbot/web/static/cell/template_HRI_LBR_all_center_v2.svg"
  local cluster_service_example="${REPO_ROOT}/deploy/systemd/posetestbot-cluster.service.example"
  local web_service_example="${REPO_ROOT}/deploy/systemd/posetestbot-web.service.example"
  [[ -f "${ui_root}/index.html" ]] || die "Bundled web UI is missing ${ui_root}/index.html. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/*.js" >/dev/null || die "Bundled web UI has no JavaScript asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/*.css" >/dev/null || die "Bundled web UI has no CSS asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/cell-page-*.js" >/dev/null || die "Bundled web UI has no lazy Cell asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/calibration-targets-page-*.js" >/dev/null || die "Bundled web UI has no lazy Calibration Targets asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/workpieces-page-*.js" >/dev/null || die "Bundled web UI has no lazy Workpiece Catalogue asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/pose-templates-page-*.js" >/dev/null || die "Bundled web UI has no lazy Pose Templates asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/bop-evaluation-page-*.js" >/dev/null || die "Bundled web UI has no lazy BOP Evaluation asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/pose-estimation-page-*.js" >/dev/null || die "Bundled web UI has no lazy Pose Estimation asset. Run scripts/install.sh --with-web-build."
  compgen -G "${ui_root}/assets/run-folders-page-*.js" >/dev/null || die "Bundled web UI has no lazy Run folders asset. Run scripts/install.sh --with-web-build."
  [[ -f "${cell_asset}" ]] || die "Bundled Cell template is missing ${cell_asset}."
  [[ -f "${cluster_service_example}" ]] || die "Cluster controller user-service example is missing ${cluster_service_example}."
  [[ -f "${web_service_example}" ]] || die "Web console user-service example is missing ${web_service_example}."
  log "Bundled operator-console assets are present."
}

uv_python() {
  if [[ "${CHECK_ONLY}" == true ]]; then
    run uv run --no-sync python "$@"
  else
    run uv run python "$@"
  fi
}

run_import_smoke() {
  local smoke_code='
import importlib
import sys

modules = [
    "aiohttp",
    "aioice",
    "aiortc",
    "av",
    "cv2",
    "pyrealsense2",
    "fast_simplification",
    "flask",
    "depthai",
    "matplotlib",
    "networkx",
    "numpy",
    "PIL",
    "pydantic",
    "reportlab",
    "scipy",
    "pytransform3d",
    "trimesh",
    "posetestbot.web.app",
    "posetestbot.cluster.client",
    "posetestbot.cluster.controller_service",
]

failures = []
if not (sys.version_info.major == 3 and sys.version_info.minor == 12):
    failures.append(f"python: expected 3.12, found {sys.version.split()[0]}")
for module in modules:
    try:
        importlib.import_module(module)
    except Exception as exc:
        failures.append(f"{module}: {type(exc).__name__}: {exc}")

try:
    import cv2
    if not all(hasattr(cv2.aruco, name) for name in ("Board", "ArucoDetector")):
        failures.append("cv2: required cv2.aruco.Board/ArucoDetector APIs are missing")
except Exception:
    pass

if failures:
    print("Required Python import smoke failed:", file=sys.stderr)
    for failure in failures:
        print(f"- {failure}", file=sys.stderr)
    raise SystemExit(1)

print("Required Python imports OK")
'
  uv_python -c "${smoke_code}"
}

run_readiness_checks() {
  if [[ "${SKIP_RUNTIME_CHECKS}" == true ]]; then
    warn "Skipping runtime and adapter checks because --skip-runtime-checks was provided."
    return
  fi

  log "Checking required Python imports."
  run_import_smoke

  if [[ "${WITH_POSEGRIDGEN}" == true ]]; then
    log "Checking the pinned PoseGridGen backend and renderer capabilities."
    uv_python -c '
from posetestbot.calibration.posegridgen import posegridgen_status

status = posegridgen_status()
if not status["available"] or not status["renderer_compatible"]:
    raise SystemExit(status.get("reason") or "PoseGridGen renderer is unavailable")
print(f"PoseGridGen renderer OK ({status['"'"'revision'"'"']})")
'
  fi

  if [[ "${WITH_POSETEMPLATECREATOR}" == true ]]; then
    log "Checking the pinned PoseTemplateCreator backend capabilities."
    uv_python -c '
from posetestbot.pose_templates.adapter import posetemplatecreator_status

status = posetemplatecreator_status()
if not status["available"]:
    raise SystemExit(status.get("reason") or "PoseTemplateCreator is unavailable")
print(f"PoseTemplateCreator OK ({status['"'"'revision'"'"']})")
'
  fi

  log "Checking acquisition runtime visibility."
  uv_python scripts/runtime_status.py --json

  log "Checking registered sensor adapters without opening hardware."
  uv_python scripts/sensor_adapters.py --json
}

print_followup_notes() {
  cat <<'EOF'

[install] Notes:
- PoseTestBot targets only the real lab iiwa; installation and readiness checks never command it.
- ZED 2i support requires the Stereolabs ZED SDK and pyzed.sl Python bindings outside ordinary uv/PyPI setup.
- Camera discovery may require USB permissions and vendor udev rules on the lab host.
- For physical sensor visibility, run:
    uv run python scripts/sensor_status.py --json
EOF
}

main() {
  parse_args "$@"
  verify_repo_root
  configure_uv_cache
  ensure_uv
  install_posegridgen
  install_posetemplatecreator
  install_system_packages
  sync_python_environment
  install_bop_toolkit
  build_web_console
  verify_web_console
  install_blenderproc
  install_playwright_browsers
  run_readiness_checks
  print_followup_notes
}

main "$@"
