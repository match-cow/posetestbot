#!/usr/bin/env python3
"""Write readiness evidence for a configured canonical capture workflow."""

from __future__ import annotations

import argparse
import json

from posetestbot.pipeline.preflight import (
    build_run_preflight,
    write_run_preflight_with_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Load run_config.json and summarize robot, sensor, runtime, and "
            "workflow-input readiness without starting physical capture."
        )
    )
    parser.add_argument("run_root", help="Run root containing run_config.json.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write the full preflight summary as JSON.",
    )
    parser.add_argument(
        "--no-sensors",
        action="store_true",
        help="Skip live sensor discovery.",
    )
    parser.add_argument(
        "--no-runtimes",
        action="store_true",
        help="Skip external runtime readiness checks.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 2 when preflight status is error.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write run_preflight_report.json and record a manifest stage.",
    )
    return parser.parse_args()


def print_summary(preflight: dict) -> None:
    print("PoseTestBot run preflight")
    print(f"Generated: {preflight['generated_at']}")
    print(f"Run root: {preflight['run_root']}")
    print(f"Status: {preflight['overall_status']}")
    for check in preflight["checks"]:
        print(f"- {check['status'].upper()} {check['name']}: {check['message']}")


def main() -> int:
    args = parse_args()
    if args.write:
        path, preflight = write_run_preflight_with_manifest(
            args.run_root,
            include_sensor_status=not args.no_sensors,
            include_runtime_status=not args.no_runtimes,
        )
    else:
        path = None
        preflight = build_run_preflight(
            args.run_root,
            include_sensor_status=not args.no_sensors,
            include_runtime_status=not args.no_runtimes,
        )
    if args.json:
        print(json.dumps(preflight, indent=2, sort_keys=True))
    else:
        print_summary(preflight)
        if path is not None:
            print(f"Wrote: {path}")
    if args.check and preflight["overall_status"] == "error":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
