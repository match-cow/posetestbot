#!/usr/bin/env python3
"""Generate one of the two run-scoped BOP ground-truth products."""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from posetestbot.jobs.cancellation import raise_on_first_cancel_signal
from posetestbot.pipeline.derived_work import derived_stage_lock
from posetestbot.bop.annotations import (
    ANNOTATION_REPORT,
    inspect_annotation_setup,
    selected_calibration_profiles,
    validate_annotation_mode,
)
from posetestbot.io.atomic import atomic_write_json


APP_ROOT = Path(__file__).resolve().parents[1]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate pose-only or pose-plus-mask BOP ground truth from the "
            "run-owned pose template, robot poses, and selected calibration."
        )
    )
    parser.add_argument("run_root")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("pose", "pose_and_masks"),
        help=(
            "'pose' writes scene_gt.json only; 'pose_and_masks' additionally "
            "writes exact BOP masks, visibility masks, and scene_gt_info.json."
        ),
    )
    return parser.parse_args()


def build_annotation_commands(
    *,
    run_root: Path,
    calibration_profiles: Path,
    mode: str,
) -> list[list[str]]:
    """Build the exact three-stage command contract for a selected product."""

    mode = validate_annotation_mode(mode)
    return [
        [
            "uv",
            "run",
            "python",
            "scripts/run_blenderproc_prepare_stage.py",
            run_root.as_posix(),
            "--calibration-profiles",
            calibration_profiles.as_posix(),
            "--annotation-mode",
            mode,
        ],
        [
            "uv",
            "run",
            "python",
            "scripts/run_blenderproc_render_stage.py",
            run_root.as_posix(),
            "--annotation-mode",
            mode,
        ],
        [
            "uv",
            "run",
            "python",
            "scripts/run_bop_export_stage.py",
            run_root.as_posix(),
            "--calibration-profiles",
            calibration_profiles.as_posix(),
            "--annotation-source",
            "blenderproc",
            "--annotation-mode",
            mode,
            "--overwrite",
        ],
    ]


def _write_report(
    path: Path,
    *,
    run_root: Path,
    mode: str,
    status: str,
    commands: list[list[str]],
    started_at: str,
    message: str,
    failed_command: list[str] | None = None,
) -> None:
    value: dict[str, Any] = {
        "schema_version": "bop_annotation_generation_report.v1",
        "run_root": run_root.as_posix(),
        "mode": mode,
        "status": status,
        "started_at": started_at,
        "updated_at": _utc_now_iso(),
        "commands": commands,
        "message": message,
    }
    if status in {"succeeded", "failed", "canceled"}:
        value["completed_at"] = value["updated_at"]
    if failed_command is not None:
        value["failed_command"] = failed_command
    atomic_write_json(path, value)


def main() -> None:
    args = parse_args()
    with derived_stage_lock(Path(args.run_root), "bop-annotations"):
        _run(args)


def _run(args: argparse.Namespace) -> None:
    run_root = Path(args.run_root).resolve()
    mode = validate_annotation_mode(args.mode)
    calibration_profiles = selected_calibration_profiles(run_root)

    setup = inspect_annotation_setup(run_root, app_root=APP_ROOT)
    readiness = setup["readiness_by_mode"][mode]
    if not readiness["ready"]:
        messages = "; ".join(str(item["message"]) for item in readiness["blockers"])
        raise ValueError(f"Ground-truth generation is not ready: {messages}")

    commands = build_annotation_commands(
        run_root=run_root,
        calibration_profiles=calibration_profiles,
        mode=mode,
    )
    report_path = run_root / ANNOTATION_REPORT
    started_at = _utc_now_iso()
    command = None
    try:
        _write_report(
            report_path,
            run_root=run_root,
            mode=mode,
            status="running",
            commands=commands,
            started_at=started_at,
            message="Ground-truth generation is running.",
        )
        for command in commands:
            subprocess.run(command, cwd=APP_ROOT, check=True)
    except BaseException as exc:
        _write_report(
            report_path,
            run_root=run_root,
            mode=mode,
            status="canceled" if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else "failed",
            commands=commands,
            started_at=started_at,
            message=str(exc),
            failed_command=command,
        )
        raise

    _write_report(
        report_path,
        run_root=run_root,
        mode=mode,
        status="succeeded",
        commands=commands,
        started_at=started_at,
        message=(
            "Pose GT is ready."
            if mode == "pose"
            else "Pose GT, exact instance masks, visible masks, and ROI evidence are ready."
        ),
    )
    print(f"Generated {mode} BOP ground truth for {run_root}.")


if __name__ == "__main__":
    raise_on_first_cancel_signal(KeyboardInterrupt("Derived job canceled"))
    main()
