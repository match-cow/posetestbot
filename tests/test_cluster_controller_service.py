from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from posetestbot.cluster.controller_service import SystemdUserServiceManager
from posetestbot.web.runtime import WebSettings


def _completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_systemd_user_service_status_and_fixed_commands(monkeypatch) -> None:
    responses = [
        _completed(
            "LoadState=loaded\n"
            "ActiveState=inactive\n"
            "SubState=dead\n"
            "UnitFileState=disabled\n"
        ),
        _completed(
            "LoadState=loaded\n"
            "ActiveState=active\n"
            "SubState=running\n"
            "UnitFileState=enabled\n"
        ),
    ]
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return responses.pop(0)

    monkeypatch.setattr(
        "posetestbot.cluster.controller_service.subprocess.run", fake_run
    )
    manager = SystemdUserServiceManager(
        "posetestbot-cluster.service", systemctl_path="/usr/bin/systemctl"
    )

    stopped = manager.status()
    running = manager.status()

    assert stopped["state"] == "stopped"
    assert stopped["can_start"] is True
    assert stopped["can_stop"] is False
    assert running["state"] == "running"
    assert running["active"] is True
    assert running["can_start"] is False
    assert running["can_stop"] is True
    assert manager.command("start") == [
        "/usr/bin/systemctl",
        "--user",
        "--no-block",
        "start",
        "posetestbot-cluster.service",
    ]
    assert manager.command("stop")[-2:] == ["stop", "posetestbot-cluster.service"]
    assert all(call[-1] == "posetestbot-cluster.service" for call in calls)


def test_systemd_user_service_fails_closed_for_missing_or_unsafe_unit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "posetestbot.cluster.controller_service.subprocess.run",
        lambda *_args, **_kwargs: _completed(
            "LoadState=not-found\nActiveState=inactive\nSubState=dead\n",
            returncode=1,
        ),
    )
    manager = SystemdUserServiceManager(
        "posetestbot-cluster.service", systemctl_path="/usr/bin/systemctl"
    )

    status = manager.status()

    assert status["state"] == "unavailable"
    assert status["unit_installed"] is False
    assert status["can_start"] is False
    assert status["blockers"] == [
        {
            "code": "service_unit_not_installed",
            "message": "The configured user service unit is not installed.",
        }
    ]
    with pytest.raises(ValueError, match="fixed .service unit"):
        SystemdUserServiceManager(
            "../../attacker.service", systemctl_path="/usr/bin/systemctl"
        )


def test_web_settings_reuse_mode_0600_controller_env_without_exposing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "controller.env"
    env_file.write_text(
        "POSETESTBOT_CLUSTER_API_TOKEN=" + "t" * 40 + "\n"
        "POSETESTBOT_CLUSTER_HOST=localhost\n"
        "POSETESTBOT_CLUSTER_PORT=9876\n"
        "POSETESTBOT_CLUSTER_SSH_KEY=/private/controller-key\n"
    )
    env_file.chmod(0o600)
    for name in (
        "POSETESTBOT_CLUSTER_API_TOKEN",
        "POSETESTBOT_CLUSTER_URL",
        "POSETESTBOT_CLUSTER_ENABLED",
        "POSETESTBOT_CLUSTER_SERVICE_UNIT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSETESTBOT_CLUSTER_ENV_FILE", env_file.as_posix())
    monkeypatch.setenv(
        "POSETESTBOT_CLUSTER_SERVICE_UNIT", "posetestbot-cluster.service"
    )

    settings = WebSettings.from_environment()

    assert settings.cluster_enabled is True
    assert settings.cluster_url == "http://localhost:9876"
    assert settings.cluster_token == "t" * 40
    assert settings.cluster_env_file == env_file
    assert settings.cluster_service_unit == "posetestbot-cluster.service"
    assert not hasattr(settings, "cluster_ssh_key")


def test_web_settings_reject_controller_env_with_group_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "controller.env"
    env_file.write_text("POSETESTBOT_CLUSTER_API_TOKEN=" + "t" * 40 + "\n")
    env_file.chmod(0o640)
    monkeypatch.setenv("POSETESTBOT_CLUSTER_ENV_FILE", env_file.as_posix())

    with pytest.raises(ValueError, match="mode 0600"):
        WebSettings.from_environment()
