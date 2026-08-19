from __future__ import annotations

from pathlib import Path

from posetestbot.pipeline.preflight import (
    _calibration_arrangement_check,
    build_run_preflight,
    run_preflight_queue_summary,
    write_run_preflight_report,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)


def _robot_status() -> dict:
    return {"schema_version": "robot_status.v2", "selected_profile": {"mode": "real"}}


def _write_config(run_root: Path, *, intent: str) -> dict:
    config = create_run_config(
        run_root=run_root,
        capture_intent=intent,
        bop_annotation_mode="none",
        sensors=(
            SensorRunConfig(
                "realsense_d435",
                "123",
                "D435",
                "static" if intent == "calibration" else "eye_in_hand",
            ),
        ),
    )
    write_run_config(run_root, config)
    return config.to_dict()


def test_dataset_preflight_fails_closed_without_promoted_selection(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "dataset"
    _write_config(run_root, intent="dataset")

    report = build_run_preflight(
        run_root,
        include_sensor_status=False,
        include_runtime_status=False,
        collect_robot=_robot_status,
    )

    checks = {item["name"]: item for item in report["checks"]}
    assert report["overall_status"] == "error"
    assert checks["calibration_profile_selection"]["status"] == "error"
    assert (
        "promoted reusable calibration"
        in checks["calibration_profile_selection"]["message"]
    )
    assert checks["sensor_status"]["status"] == "error"
    assert checks["runtime_status"]["status"] == "warning"


def test_calibration_preflight_fails_closed_without_target_selection(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "calibration"
    _write_config(run_root, intent="calibration")

    report = build_run_preflight(
        run_root,
        include_sensor_status=False,
        include_runtime_status=False,
        collect_robot=_robot_status,
    )

    check = next(
        item
        for item in report["checks"]
        if item["name"] == "calibration_target_selection"
    )
    assert report["overall_status"] == "error"
    assert check["status"] == "error"
    assert "selected immutable target" in check["message"]


def test_calibration_arrangement_is_exact_for_static_and_wrist_cameras(
    tmp_path: Path,
) -> None:
    static = create_run_config(
        run_root=tmp_path / "static",
        capture_intent="calibration",
        bop_annotation_mode="none",
        sensors=(SensorRunConfig("realsense_d435", "1", "Static", "static"),),
    ).to_dict()
    wrist = create_run_config(
        run_root=tmp_path / "wrist",
        capture_intent="calibration",
        bop_annotation_mode="none",
        sensors=(SensorRunConfig("realsense_d435", "1", "Wrist", "eye_in_hand"),),
    ).to_dict()

    assert (
        _calibration_arrangement_check(
            static, {"mounting_frame": "robot_flange", "placement_mode": "unknown"}
        )["status"]
        == "ok"
    )
    assert (
        _calibration_arrangement_check(
            wrist, {"mounting_frame": "template_base", "placement_mode": "unknown"}
        )["status"]
        == "ok"
    )
    mismatch = _calibration_arrangement_check(
        static, {"mounting_frame": "template_base", "placement_mode": "unknown"}
    )
    assert mismatch["status"] == "error"
    assert mismatch["details"]["expected_target_mounting_frame"] == "robot_flange"


def test_preflight_counts_only_enabled_exact_sensor_entries(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    config = create_run_config(
        run_root=run_root,
        capture_intent="dataset",
        bop_annotation_mode="none",
        sensors=(
            SensorRunConfig("realsense_d435", "1", "First", enabled=True),
            SensorRunConfig("realsense_d435", "2", "Second", enabled=False),
        ),
    )
    write_run_config(run_root, config)

    report = build_run_preflight(
        run_root,
        include_sensor_status=False,
        include_runtime_status=False,
        collect_robot=_robot_status,
    )
    check = next(item for item in report["checks"] if item["name"] == "run_config")
    assert check["details"] == {
        "intent": "dataset",
        "configured_sensor_count": 2,
        "enabled_sensor_count": 1,
        "sensor_counts": {"realsense_d435": 1},
    }


def test_workflow_preflight_blocks_when_selected_camera_cannot_open(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "blocked-camera"
    _write_config(run_root, intent="calibration")
    blocked_readiness = {
        "schema_version": "selected_sensor_readiness.v1",
        "selected_count": 1,
        "ready_count": 0,
        "all_ready": False,
        "probes": [
            {
                "sensor_type": "realsense_d435",
                "device_id": "123",
                "display_name": "D435",
                "status": "blocked",
                "capture_ready": False,
                "reason": "probe_failed",
                "message": "Selected camera D435 is blocked: device or resource busy.",
                "recorded_output": False,
            }
        ],
    }

    report = build_run_preflight(
        run_root,
        include_runtime_status=False,
        collect_robot=_robot_status,
        collect_sensors=lambda: {"total_connected": 1},
        probe_selected_sensors=lambda _config: blocked_readiness,
    )

    checks = {item["name"]: item for item in report["checks"]}
    assert report["overall_status"] == "error"
    assert checks["sensor_status"]["status"] == "ok"
    assert checks["selected_camera_open:realsense_d435:123"]["status"] == "error"
    assert checks["selected_camera_readiness"]["status"] == "error"
    assert report["selected_sensor_readiness"] == blocked_readiness


def test_preflight_queue_summary_rejects_missing_failed_and_stale_evidence(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    config = _write_config(run_root, intent="dataset")

    assert (
        run_preflight_queue_summary(run_root, config)["queue_blocker"]
        == "missing_preflight"
    )

    failed = {
        "schema_version": "run_preflight.v2",
        "overall_status": "error",
        "config": config,
    }
    write_run_preflight_report(run_root, failed)
    assert (
        run_preflight_queue_summary(run_root, config)["queue_blocker"]
        == "failed_preflight"
    )

    ready = {
        "schema_version": "run_preflight.v2",
        "overall_status": "warning",
        "config": config,
        "selected_sensor_readiness": {
            "schema_version": "selected_sensor_readiness.v1",
            "selected_count": 1,
            "ready_count": 1,
            "all_ready": True,
            "probe_contract": {
                "record": False,
                "frames_per_camera": 1,
                "timeout_s_per_camera": 15.0,
            },
            "probes": [
                {
                    "sensor_type": "realsense_d435",
                    "device_id": "123",
                    "capture_ready": True,
                    "status": "ready",
                    "recorded_output": False,
                }
            ],
        },
    }
    write_run_preflight_report(run_root, ready)
    assert run_preflight_queue_summary(run_root, config)["ready_for_queue"] is True

    changed = {**config, "run_name": "changed"}
    assert (
        run_preflight_queue_summary(run_root, changed)["queue_blocker"]
        == "stale_preflight"
    )

    legacy = {**ready, "schema_version": "run_preflight.v1"}
    write_run_preflight_report(run_root, legacy)
    assert (
        run_preflight_queue_summary(run_root, config)["queue_blocker"]
        == "invalid_preflight"
    )

    missing_active_probe = {
        "schema_version": "run_preflight.v2",
        "overall_status": "ok",
        "config": config,
    }
    write_run_preflight_report(run_root, missing_active_probe)
    assert (
        run_preflight_queue_summary(run_root, config)["queue_blocker"]
        == "invalid_preflight"
    )
