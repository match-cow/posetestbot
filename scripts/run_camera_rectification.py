#!/usr/bin/env python3
"""Rectify synchronized RGB/aligned-depth data without modifying source frames."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.calibration.intrinsics import load_intrinsic_profile_collection
from posetestbot.calibration.rectification import RECTIFIED_DIR, rectify_run
from posetestbot.io.artifacts import (
    CALIBRATION_PROFILE_SELECTION,
    CAMERA_RECTIFICATION_REPORT,
    INTRINSIC_CALIBRATION_PROFILES,
    PROCESSED_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    parser.add_argument("--intrinsic-profiles")
    parser.add_argument(
        "--input-root",
        help=(
            "Compatibility path assertion; managed rectification accepts only "
            "the run's canonical processed/synchronized root"
        ),
    )
    parser.add_argument(
        "--output-root",
        help=(
            "Compatibility path assertion; managed rectification accepts only "
            "the run's canonical processed/rectified root"
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--diagnostic-unmanaged",
        action="store_true",
        help=(
            "Allow a canonical-root diagnostic run without a run-owned "
            "calibration_profile_selection.v2. This is not the managed dataset "
            "processing path."
        ),
    )
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


def _require_managed_rectification_roots(
    run_root: Path,
    *,
    input_root: str | None,
    output_root: str | None,
) -> None:
    """Reject custom roots at the managed dataset-stage boundary."""

    root = run_root.resolve()
    processed_root = root / PROCESSED_DIR
    canonical_input = processed_root / SYNCHRONIZED_DIR
    canonical_output = processed_root / RECTIFIED_DIR
    requested_input = _run_input_path(
        root,
        input_root,
        f"{PROCESSED_DIR}/{SYNCHRONIZED_DIR}",
    )
    requested_output = _run_input_path(
        root,
        output_root,
        f"{PROCESSED_DIR}/{RECTIFIED_DIR}",
    )
    if requested_input.resolve() != canonical_input.resolve():
        raise ValueError(
            "Managed camera rectification requires the canonical synchronized "
            f"input root: {canonical_input}"
        )
    if requested_output.resolve() != canonical_output.resolve():
        raise ValueError(
            "Managed camera rectification requires the canonical rectified "
            f"output root: {canonical_output}"
        )
    for path in (processed_root, canonical_input, canonical_output):
        if path.is_symlink():
            raise ValueError(
                f"Managed camera rectification paths must not be symlinks: {path}"
            )


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    _require_managed_rectification_roots(
        run_root,
        input_root=args.input_root,
        output_root=args.output_root,
    )
    load_run_config_for_run_root(run_root)
    selected_calibration_configured = _selected_calibration_configured(run_root)
    if not selected_calibration_configured and not args.diagnostic_unmanaged:
        raise ValueError(
            "Managed camera rectification requires a run-owned "
            "calibration_profile_selection.v2; use --diagnostic-unmanaged only "
            "for isolated software diagnostics."
        )
    profiles_path = _intrinsic_profiles_path(run_root, args.intrinsic_profiles)
    manifest = load_or_create_run_manifest(run_root)
    upsert_stage(manifest, name="camera_rectification", status="running")
    write_run_manifest(manifest, run_root)
    try:
        if selected_calibration_configured:
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
        upsert_stage(
            manifest, name="camera_rectification", status="failed", message=str(exc)
        )
        write_run_manifest(manifest, run_root)
        raise
    print(f"Wrote {report_path}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
