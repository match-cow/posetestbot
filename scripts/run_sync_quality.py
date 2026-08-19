#!/usr/bin/env python3
"""Aggregate non-destructive synchronization quality for a run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.io.artifacts import RUN_CONFIG
from posetestbot.sync.calibration_policy import (
    resolve_calibration_profile_sync_policy,
)
from posetestbot.sync.quality import (
    build_sync_quality_report,
    write_sync_quality_report_with_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check synchronized sync_report.json files for eligible in-motion "
            "coverage, timestamp evidence, and nearest-pose deltas."
        )
    )
    parser.add_argument("run_root", help="Run folder containing processed sync output.")
    parser.add_argument(
        "--min-match-ratio",
        type=float,
        default=0.8,
        help=(
            "Warn when a sensor's synchronized/eligible-in-motion coverage is "
            "below this value."
        ),
    )
    parser.add_argument(
        "--max-dropped-frames",
        type=int,
        help="Warn when a sensor excludes more than this many in-motion frames.",
    )
    parser.add_argument(
        "--max-nearest-pose-delta-ms",
        type=float,
        default=None,
        help=(
            "Manual nearest-pose threshold. Selected calibration runs always "
            "use their immutable per-camera thresholds."
        ),
    )
    parser.add_argument(
        "--no-nearest-pose-threshold",
        action="store_true",
        help="Do not check nearest robot-pose delta.",
    )
    parser.add_argument(
        "--require-timestamp-source",
        choices=("host_received", "host_wall", "sensor"),
        help="Warn when a sync report used a different timestamp source.",
    )
    parser.add_argument(
        "--require-robot-timestamp-source",
        choices=("host_received", "host_wall"),
        help="Error when a sync report cannot prove this robot timestamp source.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print a report without writing sync_quality_report.json.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON report.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    calibration_sync_policy = (
        resolve_calibration_profile_sync_policy(run_root)
        if (run_root / RUN_CONFIG).is_file()
        else None
    )
    if calibration_sync_policy is not None:
        manual_overrides = [
            flag
            for flag, active in (
                (
                    "--max-nearest-pose-delta-ms",
                    args.max_nearest_pose_delta_ms is not None,
                ),
                ("--no-nearest-pose-threshold", args.no_nearest_pose_threshold),
                (
                    "--require-timestamp-source",
                    args.require_timestamp_source is not None,
                ),
                (
                    "--require-robot-timestamp-source",
                    args.require_robot_timestamp_source is not None,
                ),
            )
            if active
        ]
        if manual_overrides:
            raise ValueError(
                "Runs with a selected calibration verify its hash-bound "
                "per-camera timing; remove manual timing options: "
                + ", ".join(manual_overrides)
            )
        sensors = calibration_sync_policy["sensors"]
        max_delta = {
            sensor["sensor_folder"]: sensor["max_nearest_pose_delta_ms"]
            for sensor in sensors
        }
        required_frame_sources = {
            sensor["sensor_folder"]: sensor["frame_timestamp_source"]
            for sensor in sensors
        }
        required_robot_sources = {
            sensor["sensor_folder"]: sensor["robot_timestamp_source"]
            for sensor in sensors
        }
    else:
        max_delta = (
            None
            if args.no_nearest_pose_threshold
            else (
                args.max_nearest_pose_delta_ms
                if args.max_nearest_pose_delta_ms is not None
                else 50.0
            )
        )
        required_frame_sources = args.require_timestamp_source
        required_robot_sources = args.require_robot_timestamp_source
    report_args = {
        "min_match_ratio": args.min_match_ratio,
        "max_dropped_frames": args.max_dropped_frames,
        "max_nearest_pose_delta_ms": max_delta,
        "require_timestamp_source": required_frame_sources,
        "require_robot_timestamp_source": required_robot_sources,
        "calibration_sync_policy": calibration_sync_policy,
    }

    if args.no_write:
        report = build_sync_quality_report(run_root, **report_args)
        path = None
    else:
        path, report = write_sync_quality_report_with_manifest(
            run_root,
            **report_args,
        )

    if path is not None:
        print(f"Wrote {path}")
    print(
        "Sync quality: "
        f"{report['overall_status']} "
        f"({report['matched_eligible_frames']}/"
        f"{report['eligible_in_motion_frames']} eligible in-motion frames "
        "synchronized, "
        f"{report['sensor_count']} sensors)"
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))

    if report["overall_status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
