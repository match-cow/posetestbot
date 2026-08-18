"""Web-process settings and background-job ownership.

Route modules resolve the runner from the active Flask application. The
module-level proxy keeps blueprint imports independent of process-wide state.
"""

from __future__ import annotations

import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import current_app, has_app_context

from posetestbot.jobs.runner import LocalJobRunner
from posetestbot.web.paths import APP_ROOT


RUNTIME_EXTENSION_KEY = "posetestbot.web_runtime"
CLUSTER_ENV_FILE_VARIABLE = "POSETESTBOT_CLUSTER_ENV_FILE"
CLUSTER_SHARED_ENV_NAMES = {
    "POSETESTBOT_CLUSTER_API_TOKEN",
    "POSETESTBOT_CLUSTER_HOST",
    "POSETESTBOT_CLUSTER_PORT",
}
SYSTEMD_SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _current_systemd_service_unit(
    cgroup_path: Path = Path("/proc/self/cgroup"),
) -> str | None:
    """Return the systemd service that owns this process, when directly managed."""

    try:
        lines = cgroup_path.read_text().splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        component = fields[2].rstrip("/").rsplit("/", 1)[-1]
        if SYSTEMD_SERVICE_UNIT_RE.fullmatch(component) is not None:
            return component
    return None


def _cluster_env_file(path_value: str | None) -> tuple[Path | None, dict[str, str]]:
    if not path_value:
        return None, {}
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{CLUSTER_ENV_FILE_VARIABLE} must be an absolute path")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("Controller .env file is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Controller .env must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Controller .env must have mode 0600")

    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("Controller .env file cannot be read") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid controller .env line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None:
            raise ValueError(f"Invalid controller .env name on line {line_number}")
        if key in CLUSTER_SHARED_ENV_NAMES:
            values.setdefault(key, value.strip())
    return path.resolve(), values


def _cluster_url_from_values(values: dict[str, str]) -> str:
    host = values.get("POSETESTBOT_CLUSTER_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Controller .env host must be loopback-only")
    try:
        port = int(values.get("POSETESTBOT_CLUSTER_PORT", "8765"))
    except ValueError as exc:
        raise ValueError("Controller .env port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Controller .env port must be between 1 and 65535")
    formatted_host = f"[{host}]" if host == "::1" else host
    return f"http://{formatted_host}:{port}"


@dataclass(frozen=True)
class WebSettings:
    host: str
    port: int
    debug: bool
    job_root: Path
    cluster_url: str = "http://127.0.0.1:8765"
    cluster_token: str | None = None
    cluster_enabled: bool = False
    cluster_env_file: Path | None = None
    cluster_service_unit: str | None = None
    web_service_unit: str | None = None

    @classmethod
    def from_environment(cls) -> WebSettings:
        cluster_env_path, cluster_env = _cluster_env_file(
            os.environ.get(CLUSTER_ENV_FILE_VARIABLE)
        )
        cluster_url = os.environ.get("POSETESTBOT_CLUSTER_URL") or (
            _cluster_url_from_values(cluster_env)
            if cluster_env_path is not None
            else "http://127.0.0.1:8765"
        )
        cluster_token = (
            os.environ.get("POSETESTBOT_CLUSTER_API_TOKEN")
            or cluster_env.get("POSETESTBOT_CLUSTER_API_TOKEN")
            or None
        )
        return cls(
            host=os.environ.get("POSETESTBOT_WEB_HOST", "0.0.0.0"),
            port=int(os.environ.get("POSETESTBOT_WEB_PORT", "5000")),
            debug=_env_bool("POSETESTBOT_WEB_DEBUG", default=False),
            job_root=APP_ROOT / "working_data" / "jobs",
            cluster_url=cluster_url,
            cluster_token=cluster_token,
            cluster_enabled=_env_bool(
                "POSETESTBOT_CLUSTER_ENABLED",
                default=cluster_env_path is not None,
            ),
            cluster_env_file=cluster_env_path,
            cluster_service_unit=(
                os.environ.get("POSETESTBOT_CLUSTER_SERVICE_UNIT") or None
            ),
            web_service_unit=(
                os.environ.get("POSETESTBOT_WEB_SERVICE_UNIT")
                or _current_systemd_service_unit()
            ),
        )


@dataclass
class WebRuntime:
    settings: WebSettings
    job_runner: LocalJobRunner
    cluster_client: Any | None = None
    cluster_service_manager: Any | None = None
    web_service_manager: Any | None = None
    instance_id: str = ""

    def __post_init__(self) -> None:
        if not self.instance_id:
            self.instance_id = uuid.uuid4().hex


_default_runtime: WebRuntime | None = None
_default_runtime_lock = threading.Lock()


def create_web_runtime(
    *,
    settings: WebSettings | None = None,
    job_runner: LocalJobRunner | None = None,
) -> WebRuntime:
    selected_settings = settings or WebSettings.from_environment()
    cluster_client = None
    if selected_settings.cluster_enabled and selected_settings.cluster_token:
        from posetestbot.cluster.client import ClusterControllerClient

        cluster_client = ClusterControllerClient(
            selected_settings.cluster_url,
            selected_settings.cluster_token,
        )
    cluster_service_manager = None
    if selected_settings.cluster_service_unit:
        from posetestbot.cluster.controller_service import SystemdUserServiceManager

        cluster_service_manager = SystemdUserServiceManager(
            selected_settings.cluster_service_unit
        )
    web_service_manager = None
    if selected_settings.web_service_unit:
        from posetestbot.web.web_service import WebServiceRestartManager

        web_service_manager = WebServiceRestartManager(
            selected_settings.web_service_unit
        )
    return WebRuntime(
        settings=selected_settings,
        job_runner=job_runner or LocalJobRunner(selected_settings.job_root),
        cluster_client=cluster_client,
        cluster_service_manager=cluster_service_manager,
        web_service_manager=web_service_manager,
    )


def default_web_runtime() -> WebRuntime:
    global _default_runtime
    with _default_runtime_lock:
        if _default_runtime is None:
            _default_runtime = create_web_runtime()
        return _default_runtime


def get_web_runtime() -> WebRuntime:
    if has_app_context():
        runtime = current_app.extensions.get(RUNTIME_EXTENSION_KEY)
        if runtime is not None:
            return runtime
    return default_web_runtime()


def get_job_runner() -> LocalJobRunner:
    return get_web_runtime().job_runner


def get_cluster_client():
    runtime = get_web_runtime()
    if runtime.cluster_client is None:
        raise RuntimeError("Cluster controller token is not configured")
    return runtime.cluster_client


def get_cluster_service_manager():
    runtime = get_web_runtime()
    if runtime.cluster_service_manager is None:
        raise RuntimeError("Cluster controller service management is not configured")
    return runtime.cluster_service_manager


class _CurrentJobRunnerProxy:
    """Resolve job-runner operations through the active application runtime."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_job_runner(), name)


job_runner = _CurrentJobRunnerProxy()
