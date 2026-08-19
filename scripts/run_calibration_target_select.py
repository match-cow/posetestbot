#!/usr/bin/env python3
"""Select an immutable calibration-target bundle for a configured run."""

from __future__ import annotations

import argparse
import json

from posetestbot.calibration.target_library import select_target_bundle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root")
    parser.add_argument("target_id")
    parser.add_argument(
        "--placement",
        required=True,
        choices=(
            "unknown",
            "template_base_identity",
            "posegridgen_board_to_base",
        ),
    )
    parser.add_argument(
        "--mounting-frame",
        required=True,
        choices=("robot_flange", "template_base"),
    )
    parser.add_argument("--library-root")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = select_target_bundle(
        run_root=args.run_root,
        target_id=args.target_id,
        placement_mode=args.placement,
        mounting_frame=args.mounting_frame,
        library_root=args.library_root,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
