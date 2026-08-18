from __future__ import annotations

import json
from pathlib import Path

import pytest

from posetestbot.io.artifacts import (
    CAPTURE_PLAN,
    CAPTURE_PLAN_PREFLIGHT_REPORT,
    DATASET_MANIFEST,
)
from posetestbot.pipeline.capture_plan import write_capture_plan_with_manifest
from posetestbot.pipeline.capture_plan_preflight import (
    build_capture_plan_preflight,
    write_capture_plan_preflight_with_manifest,
)
from posetestbot.pipeline.run_config import (
    create_run_config,
    sensor_config_from_token,
    write_run_config,
)


def fake_sensor_status() -> dict:
    return {
        "schema_version": "sensor_status.v1",
        "generated_at": "2026-06-16T00:00:00+00:00",
        "total_connected": 2,
        "all_expected_connected": True,
        "families": [
            {
                "sensor_type": "realsense_d435",
                "display_name": "Intel RealSense D435",
                "sdk_module": "pyrealsense2",
                "sdk_available": True,
                "connected_count": 1,
                "devices": [
                    {
                        "sensor_type": "realsense_d435",
                        "device_id": "123",
                        "display_name": "RealSense 123",
                        "connected": True,
                        "metadata": {},
                    }
                ],
            },
            {
                "sensor_type": "oak_d_pro",
                "display_name": "Luxonis OAK-D Pro",
                "sdk_module": "depthai",
                "sdk_available": True,
                "connected_count": 1,
                "devices": [
                    {
                        "sensor_type": "oak_d_pro",
                        "device_id": "mxid-1",
                        "display_name": "OAK-D Pro",
                        "connected": True,
                        "metadata": {},
                    }
                ],
            },
            {
                "sensor_type": "zed_2i",
                "display_name": "Stereolabs ZED 2i",
                "sdk_module": "pyzed.sl",
                "sdk_available": True,
                "connected_count": 0,
                "devices": [],
            },
        ],
    }


def test_capture_plan_preflight_reports_ok_for_mocked_connected_sensors(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(
            sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),
            sensor_config_from_token("oak_d_pro:auto:static:Cell OAK-D Pro"),
        ),
    )
    write_run_config(run_root, config)
    write_capture_plan_with_manifest(run_root, config.to_dict())

    report = build_capture_plan_preflight(
        run_root,
        allow_real_robot=True,
        collect_sensors=fake_sensor_status,
    )

    assert report["schema_version"] == "capture_plan_preflight.v1"
    assert report["overall_status"] == "ok"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["real_robot_permission"]["status"] == "ok"
    assert checks["sensor_adapter:realsense_d435:123"]["status"] == "ok"
    assert checks["sensor_adapter:oak_d_pro:auto"]["status"] == "ok"
    assert checks["sensor_output_folder:realsense_123"]["status"] == "ok"
    assert checks["sensor:realsense_d435:123"]["status"] == "ok"
    assert checks["sensor:oak_d_pro:auto"]["status"] == "ok"
    assert checks["capture_plan_build"]["status"] == "ok"
    assert report["capture_plan"]["schema_version"] == "capture_plan.v1"


def test_capture_plan_preflight_includes_sensor_diagnostics_on_failures(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-diagnostics"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(
            sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),
            sensor_config_from_token("zed_2i:auto:static:Cell ZED 2i"),
        ),
    )
    write_run_config(run_root, config)
    write_capture_plan_with_manifest(run_root, config.to_dict())

    def diagnostic_sensor_status() -> dict:
        return {
            "schema_version": "sensor_status.v1",
            "generated_at": "2026-06-16T00:00:00+00:00",
            "total_connected": 0,
            "all_expected_connected": False,
            "families": [
                {
                    "sensor_type": "realsense_d435",
                    "display_name": "Intel RealSense D435",
                    "sdk_module": "pyrealsense2",
                    "sdk_available": True,
                    "connected_count": 0,
                    "devices": [],
                    "error": "RuntimeError: could not initialize udev monitor",
                    "diagnostics": [
                        {
                            "code": "discovery_error",
                            "severity": "error",
                            "message": "RealSense discovery failed.",
                            "hints": ["Check USB/udev access."],
                        }
                    ],
                },
                {
                    "sensor_type": "zed_2i",
                    "display_name": "Stereolabs ZED 2i",
                    "sdk_module": "pyzed.sl",
                    "sdk_available": False,
                    "connected_count": 0,
                    "devices": [],
                    "error": None,
                    "diagnostics": [
                        {
                            "code": "sdk_unavailable",
                            "severity": "warning",
                            "message": "pyzed.sl is not importable.",
                            "hints": ["Install the ZED SDK."],
                        }
                    ],
                },
            ],
        }

    report = build_capture_plan_preflight(
        run_root,
        collect_sensors=diagnostic_sensor_status,
    )

    checks = {check["name"]: check for check in report["checks"]}
    realsense_check = checks["sensor:realsense_d435:123"]
    assert realsense_check["status"] == "error"
    assert realsense_check["details"]["diagnostics"][0]["code"] == "discovery_error"
    assert "udev" in realsense_check["details"]["diagnostics"][0]["hints"][0]
    zed_check = checks["sensor:zed_2i:auto"]
    assert zed_check["status"] == "error"
    assert zed_check["details"]["diagnostics"][0]["code"] == "sdk_unavailable"


def test_capture_plan_preflight_blocks_realsense_usb2_fallback(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-realsense-usb2"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)
    write_capture_plan_with_manifest(run_root, config.to_dict())

    def usb2_sensor_status() -> dict:
        transport_diagnostic = {
            "code": "realsense_usb_below_superspeed",
            "severity": "error",
            "message": "RealSense 123 is connected below SuperSpeed.",
            "devices": [
                {
                    "device_id": "123",
                    "reason": "usb_connection_below_superspeed",
                    "usb_type_descriptor": "2.1",
                    "usb_major": 2,
                }
            ],
        }
        return {
            "schema_version": "sensor_status.v1",
            "generated_at": "2026-07-21T00:00:00+00:00",
            "total_connected": 1,
            "total_capture_ready": 0,
            "all_expected_connected": False,
            "families": [
                {
                    "sensor_type": "realsense_d435",
                    "display_name": "Intel RealSense D435",
                    "sdk_module": "pyrealsense2",
                    "sdk_available": True,
                    "connected_count": 1,
                    "capture_ready_count": 0,
                    "devices": [
                        {
                            "sensor_type": "realsense_d435",
                            "device_id": "123",
                            "display_name": "RealSense 123",
                            "connected": True,
                            # Simulate an older status producer that has
                            # transport metadata but no capture_ready flag.
                            "metadata": {"usb_type_descriptor": "2.1"},
                        }
                    ],
                    "diagnostics": [transport_diagnostic],
                }
            ],
        }

    report = build_capture_plan_preflight(
        run_root,
        allow_real_robot=True,
        collect_sensors=usb2_sensor_status,
    )

    check = next(
        item for item in report["checks"] if item["name"] == "sensor:realsense_d435:123"
    )
    assert report["overall_status"] == "error"
    assert check["status"] == "error"
    assert check["message"] == "Configured device 123 is not capture-ready."
    assert check["details"]["connected_devices"] == []
    assert check["details"]["diagnostics"][0]["code"] == (
        "realsense_usb_below_superspeed"
    )


def test_capture_plan_preflight_errors_for_real_robot_without_override(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)

    report = build_capture_plan_preflight(
        run_root,
        include_sensor_status=False,
        write_plan_if_missing=False,
    )

    assert report["overall_status"] == "error"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["real_robot_permission"]["status"] == "error"
    assert "robot_controller_command" not in checks


def test_capture_plan_preflight_reports_unsupported_resolution_without_throwing(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-bad-resolution"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
        resolution="360p",
    )
    write_run_config(run_root, config)

    report = build_capture_plan_preflight(
        run_root,
        include_sensor_status=False,
        write_plan_if_missing=False,
    )

    assert report["overall_status"] == "error"
    assert report["capture_plan"] is None
    assert report["capture_plan_build_error"]
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["sensor_adapter:realsense_d435:123"]["status"] == "error"
    assert checks["capture_plan_build"]["status"] == "error"
    assert "supported: 720p" in checks["sensor_adapter:realsense_d435:123"]["message"]
    assert not (run_root / CAPTURE_PLAN).exists()


def test_capture_plan_preflight_blocks_nonempty_sensor_folder(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-existing-folder"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)
    sensor_folder = run_root / "realsense_123"
    sensor_folder.mkdir()
    (sensor_folder / "old_frame.png").write_text("placeholder")

    report = build_capture_plan_preflight(
        run_root,
        include_sensor_status=False,
        write_plan_if_missing=False,
    )

    assert report["overall_status"] == "error"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["sensor_output_folder:realsense_123"]["status"] == "error"
    assert checks["sensor_output_folder:realsense_123"]["details"]["child_count"] == 1


def test_capture_plan_preflight_blocks_even_empty_sensor_folder(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-existing-empty-folder"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)
    (run_root / "realsense_123").mkdir()

    report = build_capture_plan_preflight(
        run_root,
        include_sensor_status=False,
        write_plan_if_missing=False,
    )

    checks = {check["name"]: check for check in report["checks"]}
    output_check = checks["sensor_output_folder:realsense_123"]
    assert report["overall_status"] == "error"
    assert output_check["status"] == "error"
    assert output_check["details"]["child_count"] == 0
    assert "Use a new run root" in output_check["message"]


def test_capture_plan_preflight_blocks_existing_raw_robot_pose_artifact(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-existing-poses"
    config = create_run_config(
        capture_intent="dataset", bop_annotation_mode="none", run_root=run_root
    )
    write_run_config(run_root, config)
    (run_root / "raw_robot_ee_poses.json").write_text("{}\n")

    report = build_capture_plan_preflight(
        run_root,
        include_sensor_status=False,
        write_plan_if_missing=False,
    )

    checks = {check["name"]: check for check in report["checks"]}
    assert report["overall_status"] == "error"
    assert checks["raw_robot_pose_output"]["status"] == "error"
    assert "Use a new run root" in checks["raw_robot_pose_output"]["message"]


def test_capture_plan_preflight_errors_for_duplicate_output_folder(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-duplicate-folder"
    with pytest.raises(ValueError, match="repeat identity"):
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                sensor_config_from_token("zed_2i:auto:static:Cell ZED 2i A"),
                sensor_config_from_token("zed_2i:auto:static:Cell ZED 2i B"),
            ),
        )


def test_capture_plan_preflight_writes_report_and_manifest(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)

    path, report = write_capture_plan_preflight_with_manifest(
        run_root,
        include_sensor_status=False,
        allow_real_robot=True,
    )

    assert path == run_root / CAPTURE_PLAN_PREFLIGHT_REPORT
    assert report["overall_status"] == "warning"
    assert (run_root / CAPTURE_PLAN).is_file()
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage
        for stage in manifest["stages"]
        if stage["name"] == "capture_plan_preflight"
    )
    assert stage["status"] == "succeeded"
    assert stage["artifacts"][CAPTURE_PLAN_PREFLIGHT_REPORT] == (
        CAPTURE_PLAN_PREFLIGHT_REPORT
    )
    assert stage["artifacts"][CAPTURE_PLAN] == CAPTURE_PLAN
    assert manifest["robot_profile"]["mode"] == "real"
    assert manifest["capture_config"]["fps"] == 6


def test_capture_plan_preflight_accepts_persisted_plan_build_options(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run-with-plan-options"
    config = create_run_config(
        capture_intent="dataset",
        bop_annotation_mode="none",
        run_root=run_root,
        sensors=(sensor_config_from_token("realsense_d435:123:static:Cell RealSense"),),
    )
    write_run_config(run_root, config)
    write_capture_plan_with_manifest(
        run_root,
        config.to_dict(),
        max_frames=12,
        warmup_frames=30,
    )

    report = build_capture_plan_preflight(
        run_root,
        include_sensor_status=False,
        allow_real_robot=True,
    )

    checks = {check["name"]: check for check in report["checks"]}
    assert report["overall_status"] == "warning"
    assert checks["capture_plan_current_config"]["status"] == "ok"
    assert report["capture_plan"]["capture"]["max_frames"] == 12
    assert report["capture_plan"]["capture"]["warmup_frames"] == 30
