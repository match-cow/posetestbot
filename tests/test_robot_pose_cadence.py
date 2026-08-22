from __future__ import annotations

import json
from pathlib import Path

from posetestbot.robot.cadence import (
    CADENCE_REPORT_SCHEMA_VERSION,
    DEFAULT_REPORT_PATH,
    analyze_robot_pose_cadence,
    analyze_run_robot_pose_cadence,
)


def pose_record(
    index: int,
    *,
    motion: str,
    host_ns: int,
    sender_ns: int | None = None,
    sequence: int | None = None,
) -> tuple[str, dict[str, object]]:
    record: dict[str, object] = {
        "motion": motion,
        "host_received_timestamp_ns": host_ns,
        "pose": {"X": 0, "Y": 0, "Z": 0, "A": 0, "B": 0, "C": 0},
    }
    if sender_ns is not None and sequence is not None:
        record["source_packet"] = {
            "sender_monotonic_ns": sender_ns,
            "sender_target_period_ms": 10,
            "sequence": sequence,
            "estimated_packets_lost": 0,
        }
    return str(index), record


def test_cadence_passes_regular_100_hz_motion_and_excludes_settled_pairs() -> None:
    raw_poses = dict(
        [
            pose_record(0, motion="move_a", host_ns=0, sender_ns=0, sequence=0),
            pose_record(
                1,
                motion="move_a",
                host_ns=10_000_000,
                sender_ns=10_000_000,
                sequence=1,
            ),
            pose_record(
                2,
                motion="move_a",
                host_ns=20_000_000,
                sender_ns=20_000_000,
                sequence=2,
            ),
            pose_record(
                3,
                motion="move_a_settled",
                host_ns=70_000_000,
                sender_ns=70_000_000,
                sequence=3,
            ),
            pose_record(
                4,
                motion="move_a_settled",
                host_ns=120_000_000,
                sender_ns=120_000_000,
                sequence=4,
            ),
            pose_record(
                5,
                motion="move_b",
                host_ns=200_000_000,
                sender_ns=200_000_000,
                sequence=5,
            ),
            pose_record(
                6,
                motion="move_b",
                host_ns=210_000_000,
                sender_ns=210_000_000,
                sequence=6,
            ),
        ]
    )

    report = analyze_robot_pose_cadence(raw_poses)

    assert report["schema_version"] == CADENCE_REPORT_SCHEMA_VERSION
    assert report["status"] == "passed"
    assert report["host_receive_cadence"]["interval_count"] == 3
    assert report["host_receive_cadence"]["motion_segment_count"] == 2
    assert report["host_receive_cadence"]["median_rate_hz"] == 100.0
    assert report["sender_cadence"]["available"] is True
    assert report["sender_target_period_ms"] == 10


def test_cadence_fails_historical_roughly_16_hz_stream() -> None:
    raw_poses = dict(
        pose_record(index, motion="move", host_ns=index * 61_000_000)
        for index in range(8)
    )

    report = analyze_robot_pose_cadence(raw_poses)

    assert report["status"] == "failed"
    assert report["host_receive_cadence"]["median_rate_hz"] == 1000 / 61
    assert report["host_receive_cadence"]["maximum_gap_ms"] == 61
    assert report["sender_cadence"]["available"] is False
    assert {check["name"] for check in report["checks"] if not check["passed"]} == {
        "minimum_median_rate_hz",
        "maximum_p95_gap_ms",
        "maximum_gap_ms",
    }


def test_cadence_includes_packet_loss_observed_at_stream_end() -> None:
    raw_poses = dict(
        pose_record(index, motion="move", host_ns=index * 10_000_000)
        for index in range(4)
    )
    raw_poses["3"]["stream_end_source_packet"] = {"estimated_packets_lost": 2}

    report = analyze_robot_pose_cadence(raw_poses)

    assert report["estimated_packets_lost"] == 2


def test_write_creates_only_derived_report_below_processed(tmp_path: Path) -> None:
    raw_poses = dict(
        pose_record(index, motion="move", host_ns=index * 10_000_000)
        for index in range(4)
    )
    (tmp_path / "raw_robot_ee_poses.json").write_text(json.dumps(raw_poses))

    report, report_path = analyze_run_robot_pose_cadence(tmp_path, write=True)

    assert report["status"] == "passed"
    assert report_path == tmp_path / DEFAULT_REPORT_PATH
    assert json.loads(report_path.read_text())["source_path"] == (
        "raw_robot_ee_poses.json"
    )
    assert json.loads((tmp_path / "raw_robot_ee_poses.json").read_text()) == raw_poses
