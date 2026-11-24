#!/usr/bin/env python3
"""Print the selected PoseTestBot iiwa robot profile."""

from __future__ import annotations

import argparse
import json

from posetestbot.robot.status import collect_robot_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print the configured real iiwa profile without sending UDP "
            "commands to the robot."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write the full robot profile status as JSON.",
    )
    return parser.parse_args()


def print_status(status: dict) -> None:
    selected = status["selected_profile"]
    print("PoseTestBot robot status")
    print(f"Generated: {status['generated_at']}")
    print(f"Selected mode: {selected['mode']}")
    print(f"Robot command target: {selected['robot_ip']}:{selected['command_port']}")
    print(f"Receiver bind target: {selected['receiver_ip']}:{selected['receiver_port']}")
    velocity = status["capture_velocity"]
    print(f"Requested capture velocity: {velocity['requested_m_s']} m/s")
    print(
        "Commanded capture velocity: "
        f"{velocity['commanded_m_s']} m/s "
        f"(host cap {velocity['host_command_cap_m_s']} m/s)"
    )


def main() -> int:
    args = parse_args()
    status = collect_robot_status()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print_status(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
