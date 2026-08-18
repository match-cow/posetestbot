"""Fixed user-systemd restart control for the PoseTestBot web service."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
SYSTEMCTL_TIMEOUT_SECONDS = 5.0
RESTART_DELAY_SECONDS = 0.5

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebServiceRestartManager:
    """Inspect and restart one server-owned user service without browser input."""

    service_unit: str
    systemctl_path: str | None = None
    timeout_seconds: float = SYSTEMCTL_TIMEOUT_SECONDS
    restart_delay_seconds: float = RESTART_DELAY_SECONDS

    def __post_init__(self) -> None:
        if SERVICE_UNIT_RE.fullmatch(self.service_unit) is None:
            raise ValueError(
                "POSETESTBOT_WEB_SERVICE_UNIT must be a fixed .service unit name"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("Web service status timeout must be positive")
        if self.restart_delay_seconds < 0:
            raise ValueError("Web service restart delay cannot be negative")
        if self.systemctl_path is None:
            object.__setattr__(self, "systemctl_path", shutil.which("systemctl"))

    def status(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "configured": True,
            "available": False,
            "service_unit": self.service_unit,
            "state": "unavailable",
            "blockers": [],
        }
        if self.systemctl_path is None:
            value["blockers"] = [
                {
                    "code": "systemctl_unavailable",
                    "message": "systemctl is unavailable to the PoseTestBot web process.",
                }
            ]
            return value

        try:
            completed = subprocess.run(
                [
                    self.systemctl_path,
                    "--user",
                    "show",
                    "--no-pager",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=MainPID",
                    self.service_unit,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            value["blockers"] = [
                {
                    "code": "service_status_timeout",
                    "message": "The user service manager did not answer in time.",
                }
            ]
            return value
        except OSError:
            value["blockers"] = [
                {
                    "code": "service_status_unavailable",
                    "message": "The user service manager is unavailable.",
                }
            ]
            return value

        properties: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if "=" not in line:
                continue
            key, item = line.split("=", 1)
            if key in {"LoadState", "ActiveState", "SubState", "MainPID"}:
                properties[key] = item.strip()
        load_state = properties.get("LoadState")
        active_state = properties.get("ActiveState")
        sub_state = properties.get("SubState")
        main_pid = properties.get("MainPID")
        installed = load_state == "loaded"
        active = active_state == "active"
        owns_current_process = main_pid == str(os.getpid())

        if not installed:
            state = "unavailable"
        elif active:
            state = "running"
        elif active_state == "activating":
            state = "starting"
        elif active_state == "deactivating":
            state = "stopping"
        elif active_state == "failed":
            state = "failed"
        elif active_state == "inactive":
            state = "stopped"
        else:
            state = "unknown"

        blockers: list[dict[str, str]] = []
        if not installed:
            blockers.append(
                {
                    "code": "service_unit_not_installed",
                    "message": "The configured PoseTestBot web service is not installed.",
                }
            )
        elif completed.returncode != 0:
            blockers.append(
                {
                    "code": "service_status_unavailable",
                    "message": "The configured web service status could not be read.",
                }
            )
        elif not active:
            blockers.append(
                {
                    "code": "service_not_running",
                    "message": (
                        "The configured web service is not the active managed process."
                    ),
                }
            )
        elif not owns_current_process:
            blockers.append(
                {
                    "code": "service_process_mismatch",
                    "message": (
                        "The configured web service does not own this backend process."
                    ),
                }
            )

        value.update(
            {
                "available": (
                    installed
                    and completed.returncode == 0
                    and active
                    and owns_current_process
                ),
                "state": state,
                "load_state": load_state,
                "active_state": active_state,
                "sub_state": sub_state,
                "blockers": blockers,
            }
        )
        return value

    def schedule_restart(self) -> None:
        """Queue restart after the HTTP response has had time to leave the process."""

        if self.systemctl_path is None:
            raise RuntimeError("systemctl is unavailable to the PoseTestBot web process")

        command = [
            self.systemctl_path,
            "--user",
            "--no-block",
            "restart",
            self.service_unit,
        ]

        def restart() -> None:
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout_seconds,
                )
                if completed.returncode != 0:
                    LOGGER.error(
                        "Managed PoseTestBot web restart request failed: %s",
                        completed.stderr.strip() or f"exit {completed.returncode}",
                    )
            except (OSError, subprocess.TimeoutExpired):
                LOGGER.exception("Managed PoseTestBot web restart request failed")

        timer = threading.Timer(self.restart_delay_seconds, restart)
        timer.daemon = True
        timer.start()
