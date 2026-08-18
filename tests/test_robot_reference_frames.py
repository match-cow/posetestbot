from __future__ import annotations

import copy

import pytest

from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    robot_pose_reference_evidence,
    verified_sunrise_reference_frame_path,
)


RUN_ID = "11111111-1111-4111-8111-111111111111"


def _record(*, sequence: int = 0) -> dict:
    return {
        "motion": "pose_0",
        "pose": {"X": 0},
        "source_packet": {
            "schema_version": "robot_pose.v1",
            "packet_kind": "pose",
            "run_id": RUN_ID,
            "sequence": sequence,
            "from_frame": "robot_flange",
            "to_frame": "template_base",
            "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
        },
    }


def test_current_pose_reference_evidence_binds_run_and_frame_identity() -> None:
    evidence = robot_pose_reference_evidence(
        {"0": _record(sequence=0), "1": _record(sequence=1)}
    )

    assert evidence == {
        "schema_version": "robot_pose_reference.v1",
        "status": "verified",
        "packet_schema_version": "robot_pose.v1",
        "from": "robot_flange",
        "to": "template_base",
        "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
        "run_id": RUN_ID,
        "pose_count": 2,
    }
    assert (
        verified_sunrise_reference_frame_path(evidence)
        == POSE_TEMPLATE_BASE_SUNRISE_PATH
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda record: record.pop("source_packet"), "must retain its robot_pose.v1"),
        (
            lambda record: record["source_packet"].update({"run_id": "run-1"}),
            "invalid run_id",
        ),
        (
            lambda record: record["source_packet"].update(
                {"sunrise_reference_frame_path": "/PoseTestBot/TemplateBase"}
            ),
            "does not use",
        ),
    ],
)
def test_old_or_malformed_pose_packets_fail_closed(mutation, message: str) -> None:
    record = copy.deepcopy(_record())
    mutation(record)
    with pytest.raises(ValueError, match=message):
        robot_pose_reference_evidence({"0": record})
