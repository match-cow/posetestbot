from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from posetestbot.config import (
    DEFAULT_CAPTURE_VELOCITY_M_S,
    DEFAULT_RECEIVER_PORT,
    DEFAULT_ROBOT_PORT,
    LAB_ROBOT_IP,
    LAB_ROBOT_RECEIVER_IP,
    MAX_CAPTURE_COMMAND_VELOCITY_M_S,
    RobotProfile,
    robot_profile,
)
from posetestbot.robot import udp


RUN_ID = "11111111-1111-4111-8111-111111111111"


def test_robot_profile_is_the_fixed_lab_target_even_when_old_env_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSETESTBOT_ROBOT_IP", "192.0.2.10")
    monkeypatch.setenv("POSETESTBOT_ROBOT_PORT", "12345")
    monkeypatch.setenv("POSETESTBOT_RECEIVER_IP", "192.0.2.11")

    profile = robot_profile()

    assert profile.mode == "real"
    assert profile.robot_ip == LAB_ROBOT_IP
    assert profile.command_port == DEFAULT_ROBOT_PORT
    assert profile.receiver_ip == LAB_ROBOT_RECEIVER_IP
    assert profile.receiver_port == DEFAULT_RECEIVER_PORT
    assert profile.cartesian_velocity_m_s == DEFAULT_CAPTURE_VELOCITY_M_S


def test_robot_udp_exposes_only_structured_current_commands() -> None:
    assert udp.structured_start_command(0.02, RUN_ID) == {
        "schema_version": "robot_command.v1",
        "command": "start_capture",
        "cartesian_velocity_m_s": 0.02,
        "run_id": RUN_ID,
    }
    assert udp.structured_stop_command() == {
        "schema_version": "robot_command.v1",
        "command": "stop_after_current_motion",
    }
    assert not hasattr(udp, "legacy_start_command")
    assert not hasattr(udp, "legacy_stop_command")


def test_structured_stop_matches_the_commissioned_sunrise_applications() -> None:
    assert udp.IDLE_EXIT_COMMAND == "stop_after_current_motion"
    for path in Path("iiwa").glob("*Application.java"):
        java = path.read_text()
        assert (
            'private static final String IDLE_EXIT_COMMAND =\n'
            '\t\t\t"stop_after_current_motion";' in java
        ), path.name
        assert "&& IDLE_EXIT_COMMAND.equals(jsonObject.get(\"command\"));" in java
        assert '"exit_idle_program"' not in java


def test_send_stop_sends_the_commissioned_structured_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[dict, str, int]] = []
    monkeypatch.setattr(
        udp,
        "send_udp_json",
        lambda message, ip, port: sent.append((message, ip, port)),
    )
    profile = robot_profile()

    message = udp.send_stop(profile)

    assert message == {
        "schema_version": "robot_command.v1",
        "command": "stop_after_current_motion",
    }
    assert sent == [(message, LAB_ROBOT_IP, DEFAULT_ROBOT_PORT)]


@pytest.mark.parametrize("run_id", ["run-1", str(uuid.uuid4()).upper(), ""])
def test_structured_start_rejects_noncanonical_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError, match="canonical UUID"):
        udp.structured_start_command(0.02, run_id)


def test_send_start_caps_velocity_and_sends_structured_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[dict, str, int]] = []
    monkeypatch.setattr(
        udp,
        "send_udp_json",
        lambda message, ip, port: sent.append((message, ip, port)),
    )
    profile = RobotProfile(
        mode="real",
        robot_ip=LAB_ROBOT_IP,
        command_port=DEFAULT_ROBOT_PORT,
        receiver_ip=LAB_ROBOT_RECEIVER_IP,
        receiver_port=DEFAULT_RECEIVER_PORT,
        cartesian_velocity_m_s=0.2,
    )

    message = udp.send_start(profile, run_id=RUN_ID)

    assert message == {
        "schema_version": "robot_command.v1",
        "command": "start_capture",
        "cartesian_velocity_m_s": MAX_CAPTURE_COMMAND_VELOCITY_M_S,
        "run_id": RUN_ID,
        "receiver_ip": LAB_ROBOT_RECEIVER_IP,
        "receiver_port": DEFAULT_RECEIVER_PORT,
    }
    assert sent == [(message, LAB_ROBOT_IP, DEFAULT_ROBOT_PORT)]


def test_direct_start_cli_requires_fresh_acknowledgements_before_udp() -> None:
    result = subprocess.run(
        ["uv", "run", "python", "start_iiwa.py", "--run-id", RUN_ID],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        env={**os.environ, "UV_CACHE_DIR": "/tmp/uv-cache"},
    )

    assert result.returncode == 1
    assert "fresh --allow-real-robot and --allow-cameras" in result.stdout


@pytest.mark.parametrize(
    ("script", "arguments", "retired_flag"),
    [
        (
            "start_iiwa.py",
            ["--run-id", RUN_ID, "--ip_robot", "192.0.2.10"],
            "--ip_robot",
        ),
        ("stop_iiwa.py", ["--ip_robot", "192.0.2.10"], "--ip_robot"),
        (
            "scripts/pose_receiver_udp_json.py",
            ["/tmp/unused-pose-output", "--run-id", RUN_ID, "--protocol", "legacy"],
            "--protocol",
        ),
        (
            "scripts/pose_receiver_udp_json.py",
            ["/tmp/unused-pose-output", "--run-id", RUN_ID, "--robot_mode", "real"],
            "--robot_mode",
        ),
        (
            "scripts/run_capture.py",
            [
                "/tmp/unused-capture-run",
                "--intent",
                "dataset",
                "--allow-cameras",
                "--allow-real-robot",
                "--mode",
                "full",
            ],
            "--mode",
        ),
    ],
)
def test_robot_and_execution_clis_reject_retired_target_or_protocol_flags(
    script: str,
    arguments: list[str],
    retired_flag: str,
) -> None:
    result = subprocess.run(
        ["uv", "run", "python", script, *arguments],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        env={**os.environ, "UV_CACHE_DIR": "/tmp/uv-cache"},
    )

    assert result.returncode != 0
    assert retired_flag in result.stderr
    assert "unrecognized arguments" in result.stderr
