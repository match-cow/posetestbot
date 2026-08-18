#!/usr/bin/env python3

import argparse
import sys

from posetestbot.config import (
    MANUAL_TEST_COMMAND_VELOCITY_M_S,
    MAX_CAPTURE_COMMAND_VELOCITY_M_S,
    robot_profile,
)
from posetestbot.robot.udp import send_start


def send_start_message(
    *,
    capture_vel: float | None,
    run_id: str,
    manual_test_speed: bool = False,
    allow_real_robot: bool = False,
    allow_cameras: bool = False,
) -> bool:
    """Send a capture-start message to the configured iiwa controller."""

    if allow_real_robot is not True or allow_cameras is not True:
        print(
            "Starting the iiwa requires fresh --allow-real-robot and "
            "--allow-cameras acknowledgements."
        )
        return False

    profile = robot_profile().with_overrides(
        cartesian_velocity_m_s=(
            MANUAL_TEST_COMMAND_VELOCITY_M_S if manual_test_speed else capture_vel
        ),
    )

    try:
        start_message = send_start(
            profile,
            run_id=run_id,
            maximum_velocity_m_s=(
                MANUAL_TEST_COMMAND_VELOCITY_M_S
                if manual_test_speed
                else MAX_CAPTURE_COMMAND_VELOCITY_M_S
            ),
        )
        print(f"Sent start message to {profile.robot_ip}:{profile.command_port}")
        print(f"Message: {start_message}")
        return True
    except OSError as exc:
        print(f"Socket error: {exc}")
        return False
    except Exception as exc:
        print(f"Error sending start message: {exc}")
        return False


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Send start message to iiwa via UDP")
    parser.add_argument(
        "--capture_vel",
        type=float,
        default=None,
        help=(
            "Override the requested Cartesian capture velocity in m/s. "
            "The transmitted numeric value is capped at 0.03."
        ),
    )
    parser.add_argument(
        "--manual-test-speed",
        action="store_true",
        help=(
            "Transmit the dedicated manual motion-test request of 0.1 m/s. "
            "This does not change run-owned acquisition speeds."
        ),
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Immutable run UUID included with robot_command.v1.",
    )
    parser.add_argument(
        "--allow-real-robot",
        action="store_true",
        help="Fresh acknowledgement that this invocation may start robot motion.",
    )
    parser.add_argument(
        "--allow-cameras",
        action="store_true",
        help="Fresh acknowledgement that camera acquisition is authorized.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()
    selected_profile = robot_profile().with_overrides(
        cartesian_velocity_m_s=args.capture_vel,
    )

    if args.verbose:
        print(
            "Target robot: "
            f"{selected_profile.mode} "
            f"{selected_profile.robot_ip}:{selected_profile.command_port}"
        )

    success = send_start_message(
        capture_vel=selected_profile.cartesian_velocity_m_s,
        run_id=args.run_id,
        manual_test_speed=args.manual_test_speed,
        allow_real_robot=args.allow_real_robot,
        allow_cameras=args.allow_cameras,
    )

    if success:
        print("Start message sent successfully.")
        sys.exit(0)
    else:
        print("Failed to send start message.")
        sys.exit(1)


if __name__ == "__main__":
    main()
