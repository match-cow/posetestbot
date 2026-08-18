#!/usr/bin/env python3
"""Write the canonical non-executing capture plan for a configured run."""

from __future__ import annotations

import argparse
import json

from posetestbot.pipeline.orchestration import plan_capture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--warmup-frames", type=int)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path, plan = plan_capture(
        args.run_root,
        max_frames=args.max_frames,
        warmup_frames=args.warmup_frames,
    )
    print(f"Wrote {path}")
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
