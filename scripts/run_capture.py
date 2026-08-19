#!/usr/bin/env python3
"""Internal worker for the supervised canonical capture recipe."""

from __future__ import annotations

import argparse
import json

from posetestbot.pipeline.orchestration import execute_capture
from posetestbot.pipeline.run_config import CAPTURE_INTENTS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    parser.add_argument("--intent", choices=sorted(CAPTURE_INTENTS), required=True)
    parser.add_argument("--allow-cameras", action="store_true", required=True)
    parser.add_argument("--allow-real-robot", action="store_true", required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path, report = execute_capture(
        args.run_root,
        intent=args.intent,
        allow_cameras=args.allow_cameras,
        allow_real_robot=args.allow_real_robot,
    )
    print(f"Wrote {path}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
