from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from posetestbot.web.app import create_app
from posetestbot.web.runtime import (
    WebRuntime,
    WebSettings,
    _current_systemd_service_unit,
)
from posetestbot.web.web_service import WebServiceRestartManager


class FakeRunner:
    def __init__(self, statuses: list[str] | None = None) -> None:
        self.statuses = statuses or []

    def list(self, *, include_services: bool = False):
        assert include_services is True
        return [SimpleNamespace(status=status) for status in self.statuses]


class FakeWebServiceManager:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.schedule_calls = 0

    def status(self):
        return {
            "configured": True,
            "available": self.available,
            "service_unit": "posetestbot-web.service",
            "state": "running" if self.available else "failed",
            "blockers": (
                []
                if self.available
                else [
                    {
                        "code": "service_failed",
                        "message": "The configured web service is not running.",
                    }
                ]
            ),
        }

    def schedule_restart(self) -> None:
        self.schedule_calls += 1


def _runtime(
    tmp_path: Path,
    *,
    manager=None,
    statuses: list[str] | None = None,
) -> WebRuntime:
    settings = WebSettings(
        host="127.0.0.1",
        port=5000,
        debug=False,
        job_root=tmp_path / "jobs",
        web_service_unit=(
            "posetestbot-web.service" if manager is not None else None
        ),
    )
    return WebRuntime(
        settings=settings,
        job_runner=FakeRunner(statuses),  # type: ignore[arg-type]
        web_service_manager=manager,
        instance_id="backend-instance-a",
    )


def test_lifecycle_status_reports_fixed_manager_and_active_local_work(
    tmp_path: Path,
) -> None:
    manager = FakeWebServiceManager()
    app = create_app(
        runtime=_runtime(
            tmp_path,
            manager=manager,
            statuses=["queued", "running", "succeeded", "failed"],
        )
    )

    response = app.test_client().get("/system/lifecycle")
    payload = response.get_json()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert payload == {
        "schema_version": "web_lifecycle.v1",
        "instance_id": "backend-instance-a",
        "backend_restart": manager.status(),
        "active_local_jobs": 2,
    }


def test_backend_restart_requires_exact_confirmation_and_available_manager(
    tmp_path: Path,
) -> None:
    manager = FakeWebServiceManager()
    client = create_app(runtime=_runtime(tmp_path, manager=manager)).test_client()

    not_json = client.post("/system/restart-backend", data="confirm=true")
    unconfirmed = client.post("/system/restart-backend", json={"confirm": False})
    caller_controlled = client.post(
        "/system/restart-backend",
        json={"confirm": True, "service_unit": "other.service"},
    )
    accepted = client.post("/system/restart-backend", json={"confirm": True})

    assert not_json.status_code == 400
    assert unconfirmed.status_code == 400
    assert caller_controlled.status_code == 400
    assert accepted.status_code == 202
    assert accepted.get_json() == {
        "accepted": True,
        "instance_id": "backend-instance-a",
        "retry_after_ms": 750,
    }
    assert manager.schedule_calls == 1

    manager.available = False
    unavailable = client.post("/system/restart-backend", json={"confirm": True})
    assert unavailable.status_code == 409
    assert "not running" in unavailable.get_json()["output"]
    assert manager.schedule_calls == 1


def test_backend_restart_is_disabled_for_unmanaged_server(tmp_path: Path) -> None:
    client = create_app(runtime=_runtime(tmp_path)).test_client()

    status = client.get("/system/lifecycle")
    restart = client.post("/system/restart-backend", json={"confirm": True})

    assert status.get_json()["backend_restart"]["state"] == "unmanaged"
    assert restart.status_code == 409
    assert "managed user-systemd service" in restart.get_json()["output"]


def test_web_service_manager_uses_fixed_user_systemd_commands(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if "show" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "LoadState=loaded\nActiveState=active\nSubState=running\n"
                    f"MainPID={os.getpid()}\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    class ImmediateTimer:
        def __init__(self, delay, callback) -> None:
            assert delay == 0
            self.callback = callback
            self.daemon = False

        def start(self) -> None:
            self.callback()

    monkeypatch.setattr("posetestbot.web.web_service.subprocess.run", run)
    monkeypatch.setattr("posetestbot.web.web_service.threading.Timer", ImmediateTimer)
    manager = WebServiceRestartManager(
        "posetestbot-web.service",
        systemctl_path="/usr/bin/systemctl",
        restart_delay_seconds=0,
    )

    status = manager.status()
    manager.schedule_restart()

    assert status["available"] is True
    assert calls[0] == [
        "/usr/bin/systemctl",
        "--user",
        "show",
        "--no-pager",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=MainPID",
        "posetestbot-web.service",
    ]
    assert calls[1] == [
        "/usr/bin/systemctl",
        "--user",
        "--no-block",
        "restart",
        "posetestbot-web.service",
    ]


def test_web_service_manager_rejects_browser_like_unit_values() -> None:
    with pytest.raises(ValueError, match="fixed .service unit"):
        WebServiceRestartManager("posetestbot-web.service --now")


def test_web_service_manager_refuses_a_unit_owned_by_another_process(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "posetestbot.web.web_service.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=999999999\n"
            ),
            stderr="",
        ),
    )
    manager = WebServiceRestartManager(
        "posetestbot-web.service",
        systemctl_path="/usr/bin/systemctl",
    )

    status = manager.status()

    assert status["available"] is False
    assert status["blockers"] == [
        {
            "code": "service_process_mismatch",
            "message": "The configured web service does not own this backend process.",
        }
    ]


def test_current_systemd_service_unit_comes_from_the_process_cgroup(
    tmp_path: Path,
) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/"
        "app.slice/posetestbot-web.service\n"
    )

    assert _current_systemd_service_unit(cgroup) == "posetestbot-web.service"


def test_current_systemd_service_unit_rejects_unmanaged_and_nested_scopes(
    tmp_path: Path,
) -> None:
    unmanaged = tmp_path / "unmanaged-cgroup"
    unmanaged.write_text("0::/user.slice/user-1000.slice/session-2.scope\n")
    nested = tmp_path / "nested-cgroup"
    nested.write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/"
        "app.slice/posetestbot-web.service/debug-shell.scope\n"
    )

    assert _current_systemd_service_unit(unmanaged) is None
    assert _current_systemd_service_unit(nested) is None
