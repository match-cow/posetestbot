#!/usr/bin/env python3
"""Run the fixed non-destructive calibrated dataset-processing recipe."""

from __future__ import annotations

import argparse

from posetestbot.pipeline.orchestration import process_dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    commands = process_dataset(args.run_root)
    print(f"Completed {len(commands)} fixed dataset-processing steps.")


if __name__ == "__main__":
    main()
