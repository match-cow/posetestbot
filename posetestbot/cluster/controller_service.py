"""Fixed user-systemd lifecycle control for the loopback cluster companion."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
SYSTEMCTL_TIMEOUT_SECONDS = 5.0


class ClusterControllerServiceError(RuntimeError):
    """Raised when the configured local service cannot be inspected or controlled."""


@dataclass(frozen=True)
class SystemdUserServiceManager:
    """Inspect and control one server-configured user service without shell input."""

    service_unit: str
    systemctl_path: str | None = None
    timeout_seconds: float = SYSTEMCTL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if SERVICE_UNIT_RE.fullmatch(self.service_unit) is None:
            raise ValueError(
                "POSETESTBOT_CLUSTER_SERVICE_UNIT must be a fixed .service unit name"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("Controller service timeout must be positive")
        if self.systemctl_path is None:
            object.__setattr__(self, "systemctl_path", shutil.which("systemctl"))

    def _base_status(self) -> dict[str, Any]:
        return {
            "managed": True,
            "service_unit": self.service_unit,
            "unit_installed": False,
            "state": "unavailable",
            "active": False,
            "can_start": False,
            "can_stop": False,
            "load_state": None,
            "active_state": None,
            "sub_state": None,
            "unit_file_state": None,
            "blockers": [],
        }

    def status(self) -> dict[str, Any]:
        value = self._base_status()
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
                    "--property=UnitFileState",
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
            if key in {"LoadState", "ActiveState", "SubState", "UnitFileState"}:
                properties[key] = item.strip()
        load_state = properties.get("LoadState")
        active_state = properties.get("ActiveState")
        sub_state = properties.get("SubState")
        unit_file_state = properties.get("UnitFileState")
        installed = load_state == "loaded"

        if not installed:
            state = "unavailable"
        elif active_state == "active":
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
                    "message": "The configured user service unit is not installed.",
                }
            )
        elif completed.returncode != 0:
            blockers.append(
                {
                    "code": "service_status_unavailable",
                    "message": "The configured user service status could not be read.",
                }
            )
        elif state == "failed":
            blockers.append(
                {
                    "code": "service_failed",
                    "message": (
                        "The controller service failed. Inspect its user-systemd "
                        "journal before retrying."
                    ),
                }
            )

        value.update(
            {
                "unit_installed": installed,
                "state": state,
                "active": active_state == "active",
                "can_start": installed
                and completed.returncode == 0
                and active_state in {"inactive", "failed"},
                "can_stop": installed
                and completed.returncode == 0
                and active_state in {"active", "activating"},
                "load_state": load_state,
                "active_state": active_state,
                "sub_state": sub_state,
                "unit_file_state": unit_file_state,
                "blockers": blockers,
            }
        )
        return value

    def command(self, action: str) -> list[str]:
        if action not in {"start", "stop"}:
            raise ValueError("Controller service action must be start or stop")
        if self.systemctl_path is None:
            raise ClusterControllerServiceError(
                "systemctl is unavailable to the PoseTestBot web process"
            )
        return [
            self.systemctl_path,
            "--user",
            "--no-block",
            action,
            self.service_unit,
        ]
