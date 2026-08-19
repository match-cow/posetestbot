"""UDP helpers for the sole structured iiwa command protocol."""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

from posetestbot.config import (
    MAX_CAPTURE_COMMAND_VELOCITY_M_S,
    RobotProfile,
    bounded_capture_velocity_m_s,
)


# This is the commissioned robot_command.v1 wire token. Keep it aligned with
# every Sunrise application; changing it requires a coordinated controller
# deployment, not just a host-side rename.
IDLE_EXIT_COMMAND = "stop_after_current_motion"


def _advertised_receiver_ip(receiver_ip: str) -> str | None:
    normalized = receiver_ip.strip()
    if normalized in {"", "0.0.0.0", "::"}:
        return None
    return normalized


def structured_start_command(
    cartesian_velocity_m_s: float,
    run_id: str,
    *,
    receiver_ip: str | None = None,
    receiver_port: int | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "schema_version": "robot_command.v1",
        "command": "start_capture",
        "cartesian_velocity_m_s": cartesian_velocity_m_s,
    }
    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("run_id must be a canonical UUID") from exc
    if run_id != canonical_run_id:
        raise ValueError("run_id must be a canonical UUID")
    command["run_id"] = run_id
    if receiver_ip:
        command["receiver_ip"] = receiver_ip
    if receiver_port is not None:
        command["receiver_port"] = receiver_port
    return command


def structured_stop_command() -> dict[str, str]:
    return {
        "schema_version": "robot_command.v1",
        "command": IDLE_EXIT_COMMAND,
    }


def send_udp_json(message: dict[str, Any], ip: str, port: int) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(payload, (ip, port))


def send_start(
    profile: RobotProfile,
    *,
    run_id: str,
    maximum_velocity_m_s: float = MAX_CAPTURE_COMMAND_VELOCITY_M_S,
) -> dict[str, Any]:
    receiver_ip = _advertised_receiver_ip(profile.receiver_ip)
    command_velocity_m_s = bounded_capture_velocity_m_s(
        profile.cartesian_velocity_m_s,
        maximum_velocity_m_s=maximum_velocity_m_s,
    )
    message = structured_start_command(
        command_velocity_m_s,
        run_id,
        receiver_ip=receiver_ip,
        receiver_port=profile.receiver_port,
    )

    send_udp_json(message, profile.robot_ip, profile.command_port)
    return message


def send_stop(
    profile: RobotProfile,
) -> dict[str, Any]:
    message = structured_stop_command()
    send_udp_json(message, profile.robot_ip, profile.command_port)
    return message
