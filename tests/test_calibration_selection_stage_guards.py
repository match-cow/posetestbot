from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from posetestbot.io.artifacts import (
    CALIBRATION_PROFILE_SELECTION,
    DATASET_MANIFEST,
)
from posetestbot.pipeline.run_config import create_run_config, write_run_config
from scripts.run_camera_rectification import _intrinsic_profiles_path


def test_rectification_defaults_to_run_config_intrinsic_snapshot(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "dataset"
    intrinsic_path = (
        "processed/calibration_inputs/"
        + "a" * 64
        + "/intrinsic_calibration_profiles.json"
    )
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            intrinsic_calibration_profiles=intrinsic_path,
        ),
    )

    assert _intrinsic_profiles_path(run_root, None) == run_root / intrinsic_path
    assert _intrinsic_profiles_path(run_root, "explicit.json") == (
        run_root / "explicit.json"
    )


@pytest.mark.parametrize(
    ("stage_name", "script", "profile_flag", "profile_path", "extra_args"),
    [
        (
            "camera_rectification",
            "scripts/run_camera_rectification.py",
            "--intrinsic-profiles",
            "processed/calibration_inputs/missing/intrinsic_calibration_profiles.json",
            (),
        ),
        (
            "blenderproc_prepare",
            "scripts/run_blenderproc_prepare_stage.py",
            "--calibration-profiles",
            "processed/calibration_inputs/missing/calibration_profiles.json",
            ("--annotation-mode", "pose", "--objectless"),
        ),
        (
            "bop_export",
            "scripts/run_bop_export_stage.py",
            "--calibration-profiles",
            "processed/calibration_inputs/missing/calibration_profiles.json",
            ("--annotation-mode", "none", "--objectless", "--no-model-export"),
        ),
    ],
)
def test_selected_calibration_stage_fails_closed_without_selection_manifest(
    tmp_path: Path,
    stage_name: str,
    script: str,
    profile_flag: str,
    profile_path: str,
    extra_args: tuple[str, ...],
) -> None:
    run_root = tmp_path / stage_name
    calibration_path = "processed/calibration_inputs/missing/calibration_profiles.json"
    intrinsic_path = (
        "processed/calibration_inputs/missing/intrinsic_calibration_profiles.json"
    )
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            calibration_profiles=calibration_path,
            intrinsic_calibration_profiles=intrinsic_path,
            calibration_profile_selection={
                "selection_artifact": CALIBRATION_PROFILE_SELECTION,
                "bundle_sha256": "0" * 64,
                "selected_at": "2026-08-18T10:00:00+00:00",
            },
        ),
    )
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            script,
            run_root.as_posix(),
            profile_flag,
            profile_path,
            *extra_args,
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert CALIBRATION_PROFILE_SELECTION in result.stderr
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(item for item in manifest["stages"] if item["name"] == stage_name)
    assert stage["status"] == "failed"
