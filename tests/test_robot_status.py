from __future__ import annotations

from posetestbot.config import (
    DEFAULT_CAPTURE_VELOCITY_M_S,
    LAB_NORMAL_NETWORK_IP,
    LAB_ROBOT_IP,
    LAB_ROBOT_RECEIVER_IP,
    MAX_CAPTURE_COMMAND_VELOCITY_M_S,
)
from posetestbot.robot import status as robot_status


def test_collect_robot_status_reports_only_the_fixed_real_profile(monkeypatch) -> None:
    monkeypatch.setenv("POSETESTBOT_ROBOT_IP", "192.0.2.10")
    monkeypatch.setenv("POSETESTBOT_RECEIVER_IP", "192.0.2.11")

    status = robot_status.collect_robot_status()

    assert status["schema_version"] == robot_status.SCHEMA_VERSION
    assert status["selected_profile"]["mode"] == "real"
    assert status["selected_profile"]["robot_ip"] == LAB_ROBOT_IP
    assert status["selected_profile"]["receiver_ip"] == LAB_ROBOT_RECEIVER_IP
    assert status["normal_network_ip"] == LAB_NORMAL_NETWORK_IP
    assert "env_overrides" not in status
    assert status["command_protocols"] == ["robot_command.v1"]
    assert status["capture_velocity"] == {
        "requested_m_s": DEFAULT_CAPTURE_VELOCITY_M_S,
        "commanded_m_s": DEFAULT_CAPTURE_VELOCITY_M_S,
        "host_command_cap_m_s": MAX_CAPTURE_COMMAND_VELOCITY_M_S,
    }
