#!/usr/bin/env python3
"""Run transactional BlenderProc rendering as a manifest-tracked stage."""

from __future__ import annotations

import argparse
from pathlib import Path

from posetestbot.blenderproc.rendering import (
    discover_render_jobs,
    run_render_jobs,
    write_render_plan,
)
from posetestbot.io.artifacts import (
    BLENDERPROC_RENDER_PLAN,
    PROCESSED_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.sensor_selection import enabled_sensor_folder_names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render prepared BlenderProc scenes for synchronized sensor folders "
            "and record the stage in dataset_manifest.json."
        )
    )
    parser.add_argument("run_root")
    parser.add_argument(
        "--input-folder",
        default=None,
        help="Defaults to <run_root>/processed/synchronized.",
    )
    parser.add_argument(
        "--render-script",
        default="scripts/blenderproc_render_720p_multi.py",
    )
    parser.add_argument("--subdir", default="blenderproc")
    parser.add_argument("--blenderproc", default="blenderproc")
    parser.add_argument(
        "--annotation-mode",
        choices=("pose", "pose_and_masks"),
        required=True,
        help=(
            "Publish analytic pose GT only, or pose GT for a later official "
            "BOP Toolkit depth-mask step. BlenderProc does not render masks."
        ),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--object-name", action="append", default=None)
    selection.add_argument(
        "--objectless",
        action="store_true",
        help="Write a successful skipped plan without invoking BlenderProc.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate prepared folders and write a plan without rendering.",
    )
    return parser.parse_args()


def synchronized_input_folder(
    run_root: Path, explicit_input_folder: str | None
) -> Path:
    if explicit_input_folder:
        return Path(explicit_input_folder)
    rectified = run_root / PROCESSED_DIR / "rectified"
    if rectified.is_dir():
        return rectified
    return run_root / PROCESSED_DIR / SYNCHRONIZED_DIR


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    input_folder = synchronized_input_folder(run_root, args.input_folder)
    sensor_names = (
        enabled_sensor_folder_names(run_root) if args.input_folder is None else None
    )
    manifest = load_or_create_run_manifest(run_root)
    upsert_stage(manifest, name="blenderproc_render", status="running")
    write_run_manifest(manifest, run_root)
    try:
        jobs = (
            []
            if args.objectless
            else discover_render_jobs(
                input_folder=input_folder,
                render_script=Path(args.render_script),
                subdir=args.subdir,
                blenderproc_executable=args.blenderproc,
                sensor_names=sensor_names,
                annotation_mode=args.annotation_mode,
            )
        )
        plan_path = write_render_plan(
            run_root,
            jobs,
            dry_run=args.dry_run,
            skipped=args.objectless,
            skip_reason="objectless_run" if args.objectless else None,
        )
        artifacts: dict[str, Path] = {BLENDERPROC_RENDER_PLAN: plan_path}
        if args.objectless:
            message = "Skipped BlenderProc rendering for explicit objectless run."
        elif args.dry_run:
            message = f"Dry-run render plan created for {len(jobs)} sensor folder(s)."
        else:
            artifacts.update(run_render_jobs(jobs))
            message = f"Rendered BlenderProc outputs for {len(jobs)} sensor folder(s)."
        upsert_stage(
            manifest,
            name="blenderproc_render",
            status="succeeded",
            artifacts=artifacts,
            run_root=run_root,
            message=message,
        )
        write_run_manifest(manifest, run_root)
    except Exception as exc:
        upsert_stage(
            manifest,
            name="blenderproc_render",
            status="failed",
            message=str(exc),
        )
        write_run_manifest(manifest, run_root)
        raise
    print(message)


if __name__ == "__main__":
    main()
