"""Bounded, non-recording readiness probes for run-selected cameras."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from posetestbot.sensors.contracts import SensorType
from posetestbot.sensors.registry import get_sensor_adapter, is_auto_device_id


SCHEMA_VERSION = "selected_sensor_readiness.v1"
DEFAULT_PROBE_TIMEOUT_S = 15.0
MAX_PROBE_OUTPUT_CHARS = 4_000

ProbeRunner = Callable[..., subprocess.CompletedProcess[str]]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _enabled_sensors(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    capture = config.get("capture")
    sensors = capture.get("sensors") if isinstance(capture, Mapping) else None
    if not isinstance(sensors, list):
        return []
    return [
        sensor
        for sensor in sensors
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is True
    ]


def _selected_sensor_identities(
    config: Mapping[str, Any],
) -> list[tuple[str, str]]:
    return sorted(
        (
            str(sensor.get("sensor_type") or ""),
            str(sensor.get("device_id") or "").strip(),
        )
        for sensor in _enabled_sensors(config)
    )


def selected_sensor_readiness_matches_config(
    report: Mapping[str, Any] | None,
    config: Mapping[str, Any],
) -> bool:
    """Return whether report proves every current selected camera is openable."""

    if not isinstance(report, Mapping) or report.get("schema_version") != SCHEMA_VERSION:
        return False
    expected = _selected_sensor_identities(config)
    raw_probes = report.get("probes")
    if not expected or not isinstance(raw_probes, list) or len(raw_probes) != len(expected):
        return False
    if any(not isinstance(probe, Mapping) for probe in raw_probes):
        return False
    probes = [probe for probe in raw_probes if isinstance(probe, Mapping)]
    actual = sorted(
        (
            str(probe.get("sensor_type") or ""),
            str(probe.get("device_id") or "").strip(),
        )
        for probe in probes
    )
    contract = report.get("probe_contract")
    return (
        actual == expected
        and report.get("selected_count") == len(expected)
        and report.get("ready_count") == len(expected)
        and report.get("all_ready") is True
        and isinstance(contract, Mapping)
        and contract.get("record") is False
        and contract.get("frames_per_camera") == 1
        and all(
            probe.get("status") == "ready"
            and probe.get("capture_ready") is True
            and probe.get("recorded_output") is False
            for probe in probes
        )
    )


def build_sensor_readiness_probe_command(
    sensor: Mapping[str, Any],
    *,
    fps: int,
    resolution: str,
    python_executable: str = sys.executable,
) -> list[str]:
    """Build the existing adapter's one-frame, no-write capture command."""

    sensor_type = SensorType(str(sensor.get("sensor_type") or ""))
    device_id = str(sensor.get("device_id") or "").strip()
    if not device_id:
        raise ValueError("Selected sensor device_id must not be empty")
    adapter = get_sensor_adapter(sensor_type)
    command = [
        python_executable,
        adapter.capture_script,
    ]
    if sensor_type == SensorType.ZED_2I:
        # The ZED CLI currently requires its positional output argument even in
        # --test mode. record=False guarantees that this path is never opened.
        command.append(os.devnull)
    command.extend(
        [
            "--test",
            "--fps",
            str(fps),
            "--max_frames",
            "1",
        ]
    )
    if not is_auto_device_id(device_id):
        command.extend(["--device", device_id])
    if sensor_type == SensorType.ZED_2I:
        command.extend(["--resolution", resolution])
    return command


def _bounded_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text[-MAX_PROBE_OUTPUT_CHARS:]


def _blocked_message(sensor_name: str, reason: str) -> str:
    return (
        f"Selected camera {sensor_name} is blocked: {reason}. Stop any stale "
        "preview or capture job; if no job owns the camera, restart the backend "
        "before rechecking readiness."
    )


def probe_selected_sensor_readiness(
    config: Mapping[str, Any],
    *,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    run_command: ProbeRunner = subprocess.run,
    python_executable: str = sys.executable,
) -> dict[str, Any]:
    """Open every enabled selected camera for one frame without recording.

    SDK enumeration alone does not prove that another process has released a
    camera. Each adapter therefore executes its normal RGB-D startup path with
    ``--test --max_frames 1``. The child is bounded so a wedged SDK cannot hold
    the readiness job indefinitely, and no path below the run root is passed to
    the adapter.
    """

    if timeout_s <= 0:
        raise ValueError("timeout_s must be greater than 0")
    capture = config.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("Run configuration capture must be an object")
    fps = capture.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise ValueError("Run configuration capture.fps must be a positive integer")
    resolution = str(capture.get("resolution") or "").strip()
    if not resolution:
        raise ValueError("Run configuration capture.resolution must not be empty")

    probes: list[dict[str, Any]] = []
    for sensor in _enabled_sensors(config):
        sensor_type = str(sensor.get("sensor_type") or "")
        device_id = str(sensor.get("device_id") or "").strip()
        sensor_name = str(
            sensor.get("operator_alias")
            or sensor.get("display_name")
            or f"{sensor_type}:{device_id}"
        )
        command: Sequence[str] = ()
        started = time.monotonic()
        try:
            command = build_sensor_readiness_probe_command(
                sensor,
                fps=fps,
                resolution=resolution,
                python_executable=python_executable,
            )
            completed = run_command(
                list(command),
                cwd=_repo_root(),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
            )
            returncode = int(completed.returncode)
            output_tail = _bounded_output(completed.stdout)
            if returncode == 0:
                status = "ready"
                reason = None
                message = (
                    f"Selected camera {sensor_name} opened its configured RGB-D "
                    "stream and delivered a frame without recording output."
                )
            else:
                status = "blocked"
                reason = "probe_failed"
                detail = output_tail.strip().splitlines()
                summary = (
                    detail[-1] if detail else f"probe exited with status {returncode}"
                )
                message = _blocked_message(sensor_name, summary)
        except subprocess.TimeoutExpired as exc:
            returncode = None
            output_tail = _bounded_output(exc.stdout)
            status = "blocked"
            reason = "probe_timeout"
            message = _blocked_message(
                sensor_name,
                f"the non-recording open probe exceeded {timeout_s:g} seconds",
            )
        except Exception as exc:
            returncode = None
            output_tail = ""
            status = "blocked"
            reason = "probe_error"
            message = _blocked_message(
                sensor_name,
                f"{type(exc).__name__}: {exc}",
            )
        probes.append(
            {
                "sensor_type": sensor_type,
                "device_id": device_id,
                "display_name": sensor_name,
                "status": status,
                "capture_ready": status == "ready",
                "reason": reason,
                "message": message,
                "elapsed_s": time.monotonic() - started,
                "timeout_s": timeout_s,
                "returncode": returncode,
                "command": list(command),
                "output_tail": output_tail,
                "recorded_output": False,
            }
        )

    ready_count = sum(probe["capture_ready"] is True for probe in probes)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_count": len(probes),
        "ready_count": ready_count,
        "all_ready": bool(probes) and ready_count == len(probes),
        "probe_contract": {
            "record": False,
            "frames_per_camera": 1,
            "timeout_s_per_camera": timeout_s,
        },
        "probes": probes,
    }


def selected_sensor_readiness_checks(
    report: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Translate a selected-camera probe report into preflight checks."""

    if (
        not isinstance(report, Mapping)
        or report.get("schema_version") != SCHEMA_VERSION
    ):
        return [
            {
                "name": "selected_camera_readiness",
                "status": "error",
                "message": "Selected-camera readiness evidence is missing or invalid.",
                "details": {},
            }
        ]
    raw_probes = report.get("probes")
    probes = raw_probes if isinstance(raw_probes, list) else []
    checks: list[dict[str, Any]] = []
    for index, raw_probe in enumerate(probes):
        probe = dict(raw_probe) if isinstance(raw_probe, Mapping) else {}
        sensor_type = str(probe.get("sensor_type") or "unknown")
        device_id = str(probe.get("device_id") or index)
        ready = probe.get("capture_ready") is True and probe.get("status") == "ready"
        checks.append(
            {
                "name": f"selected_camera_open:{sensor_type}:{device_id}",
                "status": "ok" if ready else "error",
                "message": str(
                    probe.get("message")
                    or f"Selected camera {sensor_type}:{device_id} has no readiness result."
                ),
                "details": probe,
            }
        )
    all_ready = (
        report.get("all_ready") is True
        and bool(probes)
        and all(check["status"] == "ok" for check in checks)
        and (
            config is None
            or selected_sensor_readiness_matches_config(report, config)
        )
    )
    checks.append(
        {
            "name": "selected_camera_readiness",
            "status": "ok" if all_ready else "error",
            "message": (
                f"All {len(probes)} selected camera(s) can open and deliver a frame."
                if all_ready
                else "One or more selected cameras cannot currently start capture."
            ),
            "details": {
                "selected_count": len(probes),
                "ready_count": sum(check["status"] == "ok" for check in checks),
                "recorded_output": False,
            },
        }
    )
    return checks
