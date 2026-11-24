"""Stable filesystem anchors for source-checkout and installed web use."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_app_root() -> Path:
    explicit = os.environ.get("POSETESTBOT_APP_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    source_candidate = Path(__file__).resolve().parents[2]
    if (source_candidate / "pyproject.toml").is_file():
        return source_candidate
    return Path.cwd().resolve()


APP_ROOT = resolve_app_root()


def working_data_root() -> Path:
    configured = os.environ.get("POSETESTBOT_WORKING_DATA_ROOT")
    return Path(configured).expanduser().resolve() if configured else APP_ROOT / "working_data"
