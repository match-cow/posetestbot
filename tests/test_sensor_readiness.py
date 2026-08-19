from __future__ import annotations

import subprocess

from posetestbot.sensors.readiness import (
    SCHEMA_VERSION,
    probe_selected_sensor_readiness,
    selected_sensor_readiness_checks,
    selected_sensor_readiness_matches_config,
)


def _config() -> dict:
    return {
        "capture": {
            "fps": 6,
            "resolution": "720p",
            "sensors": [
                {
                    "sensor_type": "realsense_d435",
                    "device_id": "123",
                    "display_name": "Table camera",
                    "enabled": True,
                },
                {
                    "sensor_type": "oak_d_pro",
                    "device_id": "auto",
                    "display_name": "OAK camera",
                    "enabled": True,
                },
                {
                    "sensor_type": "zed_2i",
                    "device_id": "456",
                    "display_name": "Disabled ZED",
                    "enabled": False,
                },
            ],
        }
    }


def test_selected_camera_probe_uses_capture_startup_without_recording() -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="capture succeeded\n")

    report = probe_selected_sensor_readiness(
        _config(),
        run_command=fake_run,
        python_executable="/venv/python",
    )

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["selected_count"] == 2
    assert report["ready_count"] == 2
    assert report["all_ready"] is True
    assert len(calls) == 2
    realsense_command, realsense_options = calls[0]
    assert realsense_command[:2] == [
        "/venv/python",
        "scripts/capture_realsense_720p.py",
    ]
    assert "--test" in realsense_command
    assert realsense_command[-2:] == ["--device", "123"]
    assert realsense_options["timeout"] == 15.0
    oak_command, _oak_options = calls[1]
    assert oak_command[1] == "scripts/capture_luxonis_720p.py"
    assert "--device" not in oak_command
    assert all(probe["recorded_output"] is False for probe in report["probes"])
    assert selected_sensor_readiness_matches_config(report, _config()) is True

    changed_config = _config()
    changed_config["capture"]["sensors"][0]["device_id"] = "different"
    assert selected_sensor_readiness_matches_config(report, changed_config) is False


def test_selected_camera_probe_fails_closed_on_busy_exit_and_timeout() -> None:
    call_count = 0

    def fake_run(command: list[str], **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(
                command,
                2,
                stdout="Unable to start RealSense stream: device or resource busy\n",
            )
        raise subprocess.TimeoutExpired(command, 3.0, output="camera did not answer")

    report = probe_selected_sensor_readiness(
        _config(),
        timeout_s=3.0,
        run_command=fake_run,
    )

    assert report["all_ready"] is False
    assert report["ready_count"] == 0
    assert [probe["reason"] for probe in report["probes"]] == [
        "probe_failed",
        "probe_timeout",
    ]
    assert "resource busy" in report["probes"][0]["message"]
    assert "restart the backend" in report["probes"][0]["message"]
    checks = selected_sensor_readiness_checks(report)
    assert checks[-1]["name"] == "selected_camera_readiness"
    assert checks[-1]["status"] == "error"
    assert all(check["status"] == "error" for check in checks)
