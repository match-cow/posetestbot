from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from posetestbot.io.artifacts import DATASET_MANIFEST, SYNC_QUALITY_REPORT, SYNC_REPORT
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)
from posetestbot.sync.quality import (
    build_sync_quality_report,
    calibration_sync_provenance,
    discover_sync_reports,
    verify_profile_bound_sync_evidence,
    write_sync_quality_report_with_manifest,
)


def write_sync_report(
    run_root: Path,
    *,
    sensor_name: str = "realsense_123",
    total_frames: int = 10,
    matched_frames: int = 8,
    dropped_frames: int = 2,
    timestamp_source: str = "host_received",
    robot_timestamp_source: str = "host_received",
    max_delta_ns: int = 10_000_000,
    schema_version: str = "sync_report.v3",
    calibration_sync: dict | None = None,
) -> Path:
    if not (run_root / "run_config.json").is_file():
        write_run_config(
            run_root,
            create_run_config(
                run_root=run_root,
                capture_intent="dataset",
                bop_annotation_mode="none",
                sensors=(SensorRunConfig("realsense_d435", "123", "D435"),),
            ),
        )
    report_path = run_root / "processed" / "synchronized" / sensor_name / SYNC_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": schema_version,
        "sensor_folder": str(run_root / sensor_name),
        "output_folder": str(report_path.parent),
        "timestamp_source": timestamp_source,
        "requested_timestamp_source": timestamp_source,
        "timestamp_source_counts": {timestamp_source: total_frames},
        "timestamp_fallback_count": 0,
        "timestamp_missing_count": 0,
        "sync_delta_ms": 0,
        "max_nearest_pose_delta_ms": 20.0,
        "nearest_pose_delta_rejection_count": dropped_frames,
        "total_frames": total_frames,
        "matched_frames": matched_frames,
        "dropped_frames": dropped_frames,
        "dropped": [
            {"motion": "circ_far", "reason": "nearest pose delta exceeded"}
            for _ in range(dropped_frames)
        ],
        "outside_motion_interval_frame_count": 0,
        "eligible_in_motion_frames": total_frames,
        "matched_eligible_frames": matched_frames,
        "eligible_motion_coverage": (
            matched_frames / total_frames if total_frames else 0.0
        ),
        "in_motion_exclusion_count": dropped_frames,
        "unexplained_in_motion_exclusion_count": 0,
        "incompatible_timestamp_pair_count": 0,
        "robot_pose_packet_loss_audited": True,
        "robot_pose_packet_loss_count": 0,
        "motion_intervals": [{"motion": "circ_far", "pose_count": matched_frames}],
        "mean_abs_nearest_pose_delta_ns": 5_000_000,
        "max_abs_nearest_pose_delta_ns": max_delta_ns,
        "required_frame_timestamp_domain": (
            calibration_sync["sensor"]["required_frame_timestamp_domain"]
            if calibration_sync
            else None
        ),
        "timestamp_fallback_allowed": (
            calibration_sync["sensor"]["timestamp_fallback_allowed"]
            if calibration_sync
            else True
        ),
        "calibration_sync": calibration_sync,
    }
    if schema_version == "sync_report.v3":
        value.update(
            {
                "frame_timestamp_source": timestamp_source,
                "requested_frame_timestamp_source": timestamp_source,
                "robot_timestamp_source": robot_timestamp_source,
                "timestamp_pair": {
                    "frame_timestamp_source": timestamp_source,
                    "requested_frame_timestamp_source": timestamp_source,
                    "robot_timestamp_source": robot_timestamp_source,
                },
                "timestamp_pair_provenance_audited": True,
            }
        )
    report_path.write_text(json.dumps(value) + "\n")
    return report_path


def calibration_sync_policy() -> dict:
    return {
        "schema_version": "calibration_sync_policy.v1",
        "source": "selected_calibration_profile",
        "selection_artifact": "calibration_profile_selection.json",
        "bundle_sha256": "a" * 64,
        "calibration_profiles": {
            "relative_path": (
                "processed/calibration_inputs/selected/calibration_profiles.json"
            ),
            "sha256": "b" * 64,
        },
        "sensors": [
            {
                "sensor_key": "realsense_d435:123",
                "sensor_name": "realsense_123",
                "sensor_folder": "realsense_123",
                "sensor_type": "realsense_d435",
                "device_id": "123",
                "profile_id": "profile-123",
                "robot_pose_time_offset_ms": 7.5,
                "sync_delta_ms": -7.5,
                "frame_timestamp_source": "host_received",
                "robot_timestamp_source": "host_received",
                "required_frame_timestamp_domain": None,
                "timestamp_fallback_allowed": False,
                "max_nearest_pose_delta_ms": 20.0,
                "timing_source": (
                    "processed/calibration/attempt/time_offset_search.json"
                ),
                "timing_policy": "auto_offset",
                "timing_status": "applied",
            }
        ],
    }


def test_build_sync_quality_report_summarizes_sync_reports(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)

    report = build_sync_quality_report(
        run_root,
        min_match_ratio=0.5,
        max_dropped_frames=3,
        max_nearest_pose_delta_ms=20.0,
        require_timestamp_source="host_received",
    )

    assert discover_sync_reports(run_root) == [report_path]
    assert report["schema_version"] == "sync_quality_report.v2"
    assert report["overall_status"] == "ok"
    assert report["sensor_count"] == 1
    assert report["matched_frames"] == 8
    assert report["total_frames"] == 10
    assert report["eligible_in_motion_frames"] == 10
    assert report["matched_eligible_frames"] == 8
    assert report["overall_match_ratio"] == 0.8
    assert report["match_ratio_denominator"] == "eligible_in_motion_frames"
    assert report["sensors"][0]["sensor_name"] == "realsense_123"
    assert report["sensors"][0]["max_nearest_pose_delta_ms"] == 20.0
    assert report["sensors"][0]["nearest_pose_delta_rejection_count"] == 2
    assert {check["status"] for check in report["checks"]} == {"ok"}


def test_build_sync_quality_report_accepts_relative_run_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    run_root = Path("run")
    write_sync_report(run_root)

    report = build_sync_quality_report(run_root, min_match_ratio=0.5)

    assert report["overall_status"] == "ok"
    assert report["sensor_count"] == 1
    assert report["sensors"][0]["report_path"] == (
        "processed/synchronized/realsense_123/sync_report.json"
    )


def test_build_sync_quality_report_warns_on_quality_thresholds(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    write_sync_report(
        run_root,
        matched_frames=4,
        dropped_frames=6,
        timestamp_source="filename",
        max_delta_ns=90_000_000,
    )

    report = build_sync_quality_report(
        run_root,
        min_match_ratio=0.8,
        max_dropped_frames=2,
        max_nearest_pose_delta_ms=50.0,
        require_timestamp_source="host_received",
    )

    warnings = {
        check["name"] for check in report["checks"] if check["status"] == "warning"
    }
    assert report["overall_status"] == "error"
    assert warnings == {
        "sync_eligible_motion_coverage:realsense_123",
        "sync_in_motion_exclusions:realsense_123",
        "sync_nearest_pose_delta:realsense_123",
    }
    timestamp_check = next(
        check
        for check in report["checks"]
        if check["name"] == "sync_timestamp_source:realsense_123"
    )
    assert timestamp_check["status"] == "error"


def test_quality_ignores_preserved_frames_outside_robot_motion(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(
        run_root,
        total_frames=100,
        matched_frames=8,
        dropped_frames=92,
    )
    value = json.loads(report_path.read_text())
    value.update(
        {
            "outside_motion_interval_frame_count": 92,
            "eligible_in_motion_frames": 8,
            "matched_eligible_frames": 8,
            "eligible_motion_coverage": 1.0,
            "in_motion_exclusion_count": 0,
            "nearest_pose_delta_rejection_count": 0,
        }
    )
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(run_root, min_match_ratio=0.8)

    assert report["overall_status"] == "ok"
    assert report["total_frames"] == 100
    assert report["outside_motion_interval_frame_count"] == 92
    assert report["eligible_in_motion_frames"] == 8
    assert report["matched_eligible_frames"] == 8
    assert report["overall_eligible_motion_coverage"] == 1.0
    coverage = next(
        check
        for check in report["checks"]
        if check["name"] == "sync_eligible_motion_coverage:realsense_123"
    )
    assert coverage["status"] == "ok"
    assert coverage["details"]["denominator"] == "eligible_in_motion_frames"


def test_v3_sync_report_audits_frame_and_robot_timestamp_pair(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    write_sync_report(
        run_root,
        schema_version="sync_report.v3",
        timestamp_source="sensor",
        robot_timestamp_source="host_wall",
    )

    report = build_sync_quality_report(
        run_root,
        require_timestamp_source="sensor",
        require_robot_timestamp_source="host_wall",
    )

    assert report["overall_status"] == "ok"
    assert report["sensors"][0]["timestamp_pair_provenance_audited"] is True
    assert report["sensors"][0]["timestamp_pair"] == {
        "frame_timestamp_source": "sensor",
        "requested_frame_timestamp_source": "sensor",
        "robot_timestamp_source": "host_wall",
    }
    check = next(
        item
        for item in report["checks"]
        if item["name"] == "sync_robot_timestamp_source:realsense_123"
    )
    assert check["status"] == "ok"


def test_profile_bound_quality_requires_exact_timing_and_coverage(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    policy = calibration_sync_policy()
    sensor = policy["sensors"][0]
    provenance = calibration_sync_provenance(policy, sensor)
    report_path = write_sync_report(
        run_root,
        schema_version="sync_report.v3",
        calibration_sync=provenance,
    )
    value = json.loads(report_path.read_text())
    value.update(
        {
            "sync_delta_ms": -7.5,
            "max_nearest_pose_delta_ms": 20.0,
        }
    )
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(
        run_root,
        max_nearest_pose_delta_ms={"realsense_123": 20.0},
        require_timestamp_source={"realsense_123": "host_received"},
        require_robot_timestamp_source={"realsense_123": "host_received"},
        calibration_sync_policy=policy,
    )

    assert report["overall_status"] == "ok"
    assert report["calibration_sync_policy"] == policy
    assert (
        next(
            check
            for check in report["checks"]
            if check["name"] == "sync_calibration_timing:realsense_123"
        )["status"]
        == "ok"
    )
    assert (
        next(
            check
            for check in report["checks"]
            if check["name"] == "sync_calibration_timing_coverage"
        )["status"]
        == "ok"
    )

    value["sync_delta_ms"] = 0.0
    report_path.write_text(json.dumps(value) + "\n")
    mismatched = build_sync_quality_report(
        run_root,
        max_nearest_pose_delta_ms={"realsense_123": 20.0},
        require_timestamp_source={"realsense_123": "host_received"},
        require_robot_timestamp_source={"realsense_123": "host_received"},
        calibration_sync_policy=policy,
    )
    assert mismatched["overall_status"] == "error"


def test_profile_bound_evidence_is_rebuilt_before_downstream_use(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    policy = calibration_sync_policy()
    sensor = policy["sensors"][0]
    report_path = write_sync_report(
        run_root,
        schema_version="sync_report.v3",
        calibration_sync=calibration_sync_provenance(policy, sensor),
    )
    value = json.loads(report_path.read_text())
    value["sync_delta_ms"] = -7.5
    report_path.write_text(json.dumps(value) + "\n")
    write_sync_quality_report_with_manifest(
        run_root,
        min_match_ratio=0.5,
        max_nearest_pose_delta_ms={"realsense_123": 20.0},
        require_timestamp_source={"realsense_123": "host_received"},
        require_robot_timestamp_source={"realsense_123": "host_received"},
        calibration_sync_policy=policy,
    )

    verified = verify_profile_bound_sync_evidence(run_root, policy)
    assert verified["bundle_sha256"] == "a" * 64
    assert verified["sensor_count"] == 1

    value["calibration_sync"]["sensor"]["profile_id"] = "tampered"
    report_path.write_text(json.dumps(value) + "\n")
    with pytest.raises(ValueError, match="failed"):
        verify_profile_bound_sync_evidence(run_root, policy)


def test_build_sync_quality_report_errors_without_sync_reports(
    tmp_path: Path,
) -> None:
    report = build_sync_quality_report(tmp_path / "run")

    assert report["overall_status"] == "error"
    assert report["sensor_count"] == 0
    assert report["checks"][0]["name"] == "sync_reports_present"


def test_write_sync_quality_report_updates_manifest(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    write_sync_report(run_root)

    path, report = write_sync_quality_report_with_manifest(
        run_root,
        min_match_ratio=0.5,
    )

    assert path == run_root / SYNC_QUALITY_REPORT
    assert report["overall_status"] == "ok"
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(
        stage for stage in manifest["stages"] if stage["name"] == "sync_quality"
    )
    assert stage["status"] == "succeeded"
    assert stage["artifacts"][SYNC_QUALITY_REPORT] == SYNC_QUALITY_REPORT


def test_sync_quality_cli_writes_manifest_report(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    write_sync_report(run_root)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(repo_root / "scripts" / "run_sync_quality.py"),
            str(run_root),
            "--min-match-ratio",
            "0.5",
            "--max-dropped-frames",
            "3",
            "--require-timestamp-source",
            "host_received",
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert (
        "Sync quality: ok (8/10 eligible in-motion frames synchronized, 1 sensors)"
    ) in result.stdout
    assert (run_root / SYNC_QUALITY_REPORT).is_file()
