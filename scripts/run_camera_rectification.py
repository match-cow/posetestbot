#!/usr/bin/env python3
"""Rectify synchronized RGB/aligned-depth data without modifying source frames."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.calibration.intrinsics import load_intrinsic_profile_collection
from posetestbot.calibration.rectification import rectify_run
from posetestbot.io.artifacts import (
    CALIBRATION_PROFILE_SELECTION,
    CAMERA_RECTIFICATION_REPORT,
    INTRINSIC_CALIBRATION_PROFILES,
)
from posetestbot.io.manifest import load_or_create_run_manifest, upsert_stage, write_run_manifest
from posetestbot.pipeline.run_config import load_run_config_for_run_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    parser.add_argument("--intrinsic-profiles")
    parser.add_argument("--input-root")
    parser.add_argument("--output-root")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _selected_calibration_configured(run_root: Path) -> bool:
    try:
        config = load_run_config_for_run_root(run_root)
    except FileNotFoundError:
        config = {}
    return (
        config.get("calibration_profile_selection") is not None
        or (run_root / CALIBRATION_PROFILE_SELECTION).exists()
    )


def _run_input_path(run_root: Path, value: str | None, default: str) -> Path:
    path = Path(value) if value else Path(default)
    return path if path.is_absolute() else run_root / path


def _intrinsic_profiles_path(run_root: Path, cli_value: str | None) -> Path:
    if cli_value is not None:
        return _run_input_path(run_root, cli_value, INTRINSIC_CALIBRATION_PROFILES)
    try:
        config = load_run_config_for_run_root(run_root)
    except FileNotFoundError:
        config = {}
    return _run_input_path(
        run_root,
        config.get("intrinsic_calibration_profiles"),
        INTRINSIC_CALIBRATION_PROFILES,
    )


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    profiles_path = _intrinsic_profiles_path(run_root, args.intrinsic_profiles)
    manifest = load_or_create_run_manifest(run_root)
    upsert_stage(manifest, name="camera_rectification", status="running")
    write_run_manifest(manifest, run_root)
    try:
        if _selected_calibration_configured(run_root):
            from posetestbot.calibration.profile_library import (
                verify_calibration_profile_selection,
            )

            verify_calibration_profile_selection(
                run_root,
                expected_intrinsic_calibration_profiles=profiles_path,
            )
            from posetestbot.sync.calibration_policy import (
                resolve_calibration_profile_sync_policy,
            )
            from posetestbot.sync.quality import (
                verify_profile_bound_sync_evidence,
            )

            calibration_sync_policy = resolve_calibration_profile_sync_policy(run_root)
            if calibration_sync_policy is None:
                raise ValueError(
                    "Selected calibration is not bound to a synchronization policy"
                )
            verify_profile_bound_sync_evidence(
                run_root,
                calibration_sync_policy,
            )
        profiles = load_intrinsic_profile_collection(profiles_path)
        report_path, report = rectify_run(
            run_root,
            profiles,
            input_root=args.input_root,
            output_root=args.output_root,
            overwrite=args.overwrite,
        )
        upsert_stage(
            manifest,
            name="camera_rectification",
            status="succeeded",
            artifacts={
                CAMERA_RECTIFICATION_REPORT: report_path,
                "rectified": Path(report["output_root"]),
            },
            run_root=run_root,
        )
        write_run_manifest(manifest, run_root)
    except Exception as exc:
        upsert_stage(manifest, name="camera_rectification", status="failed", message=str(exc))
        write_run_manifest(manifest, run_root)
        raise
    print(f"Wrote {report_path}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
