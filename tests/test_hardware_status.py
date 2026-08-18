from __future__ import annotations

import json
from pathlib import Path

from posetestbot.io.artifacts import DATASET_MANIFEST, HARDWARE_STATUS_REPORT
from posetestbot.pipeline.hardware_status import (
    build_hardware_status_report,
    write_hardware_status_report_with_manifest,
)
from posetestbot.pipeline.run_config import create_run_config, write_run_config


def fake_robot_status() -> dict:
    return {
        "schema_version": "robot_status.v2",
        "selected_profile": {
            "mode": "real",
            "robot_ip": "172.31.1.147",
            "command_port": 30300,
            "receiver_ip": "172.31.1.169",
            "receiver_port": 8080,
        },
    }


def fake_sensor_status() -> dict:
    return {
        "schema_version": "sensor_status.v1",
        "total_connected": 4,
        "all_expected_connected": False,
        "expected_counts_requested": True,
        "families": [
            {
                "sensor_type": "realsense_d435",
                "display_name": "Intel RealSense D435",
                "sdk_module": "pyrealsense2",
                "sdk_available": True,
                "expected_count": 3,
                "connected_count": 3,
                "meets_expected": True,
                "devices": [],
                "error": None,
            },
            {
                "sensor_type": "oak_d_pro",
                "display_name": "Luxonis OAK-D Pro",
                "sdk_module": "depthai",
                "sdk_available": False,
                "expected_count": 1,
                "connected_count": 0,
                "meets_expected": False,
                "devices": [],
                "error": None,
            },
        ],
    }


def fake_runtime_status() -> dict:
    return {
        "schema_version": "runtime_status.v1",
        "runtime_count": 2,
        "available_count": 1,
        "all_available": False,
        "runtimes": [
            {
                "runtime_id": "blenderproc",
                "display_name": "BlenderProc",
                "category": "renderer",
                "required_for": "rendering",
                "available": True,
                "checks": [],
            },
            {
                "runtime_id": "zed_sdk_python",
                "display_name": "Stereolabs ZED SDK Python",
                "category": "camera_sdk",
                "required_for": "ZED 2i capture",
                "available": False,
                "checks": [],
            },
        ],
    }


def test_hardware_status_report_combines_robot_sensor_runtime_status(
    tmp_path: Path,
) -> None:
    report = build_hardware_status_report(
        tmp_path / "run",
        collect_robot=fake_robot_status,
        collect_sensors=fake_sensor_status,
        collect_runtimes=fake_runtime_status,
    )

    assert report["schema_version"] == "hardware_status_report.v1"
    assert report["overall_status"] == "warning"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["robot_profile"]["status"] == "ok"
    assert checks["sensor_status"]["status"] == "warning"
    assert checks["sensor:oak_d_pro"]["status"] == "warning"
    assert checks["runtime:zed_sdk_python"]["status"] == "warning"
    assert report["sensor_status"]["total_connected"] == 4


def test_hardware_status_report_writes_manifest_stage(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    path, report = write_hardware_status_report_with_manifest(
        run_root,
        collect_robot=fake_robot_status,
        collect_sensors=fake_sensor_status,
        collect_runtimes=fake_runtime_status,
    )

    assert path == run_root / HARDWARE_STATUS_REPORT
    assert report["overall_status"] == "warning"
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage for stage in manifest["stages"] if stage["name"] == "hardware_status"
    )
    assert stage["status"] == "succeeded"
    assert stage["artifacts"][HARDWARE_STATUS_REPORT] == HARDWARE_STATUS_REPORT
    assert manifest["robot_profile"]["mode"] == "real"


def test_hardware_status_report_uses_run_config_robot_profile(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "real-run"
    config = create_run_config(
        capture_intent="dataset", bop_annotation_mode="none", run_root=run_root
    )
    write_run_config(run_root, config)

    report = build_hardware_status_report(
        run_root,
        include_sensor_status=False,
        include_runtime_status=False,
    )

    selected = report["robot_status"]["selected_profile"]
    assert selected["mode"] == "real"
    assert selected["robot_ip"] == "172.31.1.147"
    assert report["checks"][0]["details"]["selected_mode"] == "real"
