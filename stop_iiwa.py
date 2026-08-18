#!/usr/bin/env python3

import argparse
import sys

from posetestbot.config import robot_profile
from posetestbot.robot.udp import send_stop


def send_stop_message() -> bool:
    """Request that the idle iiwa application exit its command loop."""

    profile = robot_profile()

    try:
        stop_message = send_stop(profile)
        print(f"Sent stop message to {profile.robot_ip}:{profile.command_port}")
        print(f"Message: {stop_message}")
        return True
    except OSError as exc:
        print(f"Socket error: {exc}")
        return False
    except Exception as exc:
        print(f"Error sending stop message: {exc}")
        return False


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Request exit of the idle PoseTestBot iiwa program via UDP"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()
    selected_profile = robot_profile()

    if args.verbose:
        print(
            "Target robot: "
            f"{selected_profile.mode} "
            f"{selected_profile.robot_ip}:{selected_profile.command_port}"
        )

    success = send_stop_message()

    if success:
        print("Stop message sent successfully.")
        sys.exit(0)
    else:
        print("Failed to send stop message.")
        sys.exit(1)


if __name__ == "__main__":
    main()
