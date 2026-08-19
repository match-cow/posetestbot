#!/usr/bin/env python3
"""Create a versioned PoseTestBot run configuration artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from posetestbot.config import DEFAULT_CAPTURE_VELOCITY_M_S
from posetestbot.pipeline.run_config import (
    BOP_ANNOTATION_MODES,
    CAPTURE_INTENTS,
    create_run_config,
    default_lab_sensors,
    fixed_transform_from_mapping,
    sensor_config_from_token,
    write_run_config_with_manifest,
)
from posetestbot.sensors.contracts import MountingMode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write run_config.json for a PoseTestBot run and record it in "
            "dataset_manifest.json."
        )
    )
    parser.add_argument("run_root", help="Run folder that will own run_config.json.")
    parser.add_argument(
        "--intent",
        required=True,
        choices=tuple(sorted(CAPTURE_INTENTS)),
        help="Explicit acquisition outcome owned by this run.",
    )
    parser.add_argument(
        "--annotation-mode",
        required=True,
        choices=tuple(sorted(BOP_ANNOTATION_MODES)),
        help="Explicit BOP annotation capability for eventual dataset export.",
    )
    parser.add_argument("--run-name", default=None, help="Human-readable run name.")
    parser.add_argument(
        "--fixed-transform-json",
        action="append",
        default=[],
        help=(
            "Typed fixed frame edge as JSON with from, to, "
            "rotation_quaternion_wxyz, and translation_mm. May be repeated."
        ),
    )
    parser.add_argument("--resolution", default="720p")
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument(
        "--velocity",
        type=float,
        default=DEFAULT_CAPTURE_VELOCITY_M_S,
        help=(
            "Requested capture-motion speed in m/s "
            f"(default {DEFAULT_CAPTURE_VELOCITY_M_S:g}; execution is capped "
            "independently)."
        ),
    )
    parser.add_argument(
        "--sensor",
        action="append",
        default=None,
        help=(
            "Sensor entry sensor_type:device_id[:mounting_mode[:display_name[:orientation]]]. "
            "Use orientation inverted/normal for RealSense mounts. "
            "May be repeated. Defaults to the current lab profile."
        ),
    )
    parser.add_argument(
        "--mounting-mode",
        choices=tuple(mode.value for mode in MountingMode),
        default=MountingMode.EYE_IN_HAND.value,
        help="Default mounting mode for --sensor entries and lab defaults.",
    )
    parser.add_argument(
        "--dataset-mode",
        choices=("objectless", "pose_template"),
        default="objectless",
        help="Create an objectless run or one awaiting pose-template selection.",
    )
    parser.add_argument(
        "--calibration-profiles",
        default=None,
        help="Optional calibration profile collection path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    sensors = (
        tuple(
            sensor_config_from_token(
                token,
                default_mounting_mode=args.mounting_mode,
            )
            for token in args.sensor
        )
        if args.sensor
        else default_lab_sensors(mounting_mode=args.mounting_mode)
    )
    config = create_run_config(
        run_root=run_root,
        capture_intent=args.intent,
        bop_annotation_mode=args.annotation_mode,
        run_name=args.run_name,
        resolution=args.resolution,
        fps=args.fps,
        velocity_m_s=args.velocity,
        sensors=sensors,
        dataset_mode=args.dataset_mode,
        calibration_profiles=args.calibration_profiles,
        fixed_transforms=tuple(
            fixed_transform_from_mapping(json.loads(value))
            for value in args.fixed_transform_json
        ),
    )
    path = write_run_config_with_manifest(run_root, config)

    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
