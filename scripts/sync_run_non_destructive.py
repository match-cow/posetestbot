#!/usr/bin/env python3
"""Synchronize every discovered sensor folder in a run non-destructively."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.io.artifacts import RUN_CONFIG
from posetestbot.sync.calibration_policy import (
    resolve_calibration_profile_sync_policy,
)
from posetestbot.sync.non_destructive import (
    sync_result_artifacts,
    synchronize_run,
)
from posetestbot.sync.quality import calibration_sync_provenance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize every raw sensor folder in a run into "
            "processed/synchronized without modifying raw frames."
        )
    )
    parser.add_argument("run_root", help="Run root containing sensor folders.")
    parser.add_argument(
        "--output-root",
        default=None,
        help="Derived sync output root. Defaults to <run-root>/processed/synchronized.",
    )
    parser.add_argument(
        "--sensor-folder",
        action="append",
        default=None,
        help=(
            "Explicit run-contained raw sensor folder. Repeat to synchronize a "
            "subset; omit to preserve run-wide discovery."
        ),
    )
    parser.add_argument(
        "--sync-delta",
        default=None,
        help="Sync delta in ms, or a JSON file mapping sensor types to ms.",
    )
    parser.add_argument(
        "--timestamp-source",
        choices=("host_received", "host_wall", "sensor"),
        default=None,
        help=(
            "Manual timestamp source. Runs with a selected calibration always "
            "use its immutable per-camera timestamp policy."
        ),
    )
    parser.add_argument(
        "--robot-timestamp-source",
        choices=("host_received", "host_wall"),
        default=None,
        help=(
            "Robot-pose timestamp source. Required for sensor timestamps; "
            "inferred only for matching host clock sources."
        ),
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Write metadata only without copying rgb/depth frames.",
    )
    return parser.parse_args()


def load_sync_delta(value: str | None):
    if value is None:
        return None

    path = Path(value)
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)

    return float(value)


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    sync_delta = load_sync_delta(args.sync_delta)
    calibration_sync_policy = (
        resolve_calibration_profile_sync_policy(run_root)
        if (run_root / RUN_CONFIG).is_file()
        else None
    )
    if calibration_sync_policy is not None:
        manual_overrides = [
            flag
            for flag, value in (
                ("--sync-delta", args.sync_delta),
                ("--timestamp-source", args.timestamp_source),
                ("--robot-timestamp-source", args.robot_timestamp_source),
                ("--sensor-folder", args.sensor_folder),
                ("--output-root", args.output_root),
            )
            if value is not None
        ]
        if manual_overrides:
            raise ValueError(
                "Runs with a selected calibration use its hash-bound per-camera "
                "timing; remove manual synchronization options: "
                + ", ".join(manual_overrides)
            )

    manifest = load_or_create_run_manifest(run_root)
    upsert_stage(manifest, name="sync_run", status="running")
    write_run_manifest(manifest, run_root)

    try:
        if calibration_sync_policy is None:
            results = synchronize_run(
                run_root,
                sensor_folders=args.sensor_folder,
                output_root=args.output_root,
                sync_delta=sync_delta,
                timestamp_source=args.timestamp_source or "host_received",
                robot_timestamp_source=args.robot_timestamp_source,
                copy_files=not args.no_copy,
            )
        else:
            results = []
            for sensor in calibration_sync_policy["sensors"]:
                results.extend(
                    synchronize_run(
                        run_root,
                        sensor_folders=[run_root / sensor["sensor_folder"]],
                        sync_delta=sensor["sync_delta_ms"],
                        timestamp_source=sensor["frame_timestamp_source"],
                        robot_timestamp_source=sensor["robot_timestamp_source"],
                        copy_files=not args.no_copy,
                        max_nearest_pose_delta_ms=sensor["max_nearest_pose_delta_ms"],
                        required_frame_timestamp_domain=sensor[
                            "required_frame_timestamp_domain"
                        ],
                        timestamp_fallback_allowed=sensor["timestamp_fallback_allowed"],
                        calibration_sync=calibration_sync_provenance(
                            calibration_sync_policy,
                            sensor,
                        ),
                    )
                )
    except Exception as exc:
        upsert_stage(
            manifest,
            name="sync_run",
            status="failed",
            message=str(exc),
        )
        write_run_manifest(manifest, run_root)
        raise

    for result in results:
        sensor_name = Path(result.sensor_folder).name
        upsert_stage(
            manifest,
            name=f"sync:{sensor_name}",
            status="succeeded",
            artifacts=sync_result_artifacts(result),
            run_root=run_root,
            message=(
                f"Wrote {result.matched_frames} synchronized in-motion "
                "frame-pose match(es). Raw frames remain preserved."
            ),
        )

    matched_frames = sum(result.matched_frames for result in results)
    upsert_stage(
        manifest,
        name="sync_run",
        status="succeeded",
        run_root=run_root,
        message=(
            f"Synchronized {len(results)} sensor(s): wrote "
            f"{matched_frames} in-motion frame-pose match(es)."
            + (
                " Applied hash-bound per-camera calibration timing."
                if calibration_sync_policy is not None
                else ""
            )
        ),
    )
    write_run_manifest(manifest, run_root)

    print(
        f"Synchronized {len(results)} sensor(s): wrote "
        f"{matched_frames} in-motion frame-pose match(es)."
    )


if __name__ == "__main__":
    main()
