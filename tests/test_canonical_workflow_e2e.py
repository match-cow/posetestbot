from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from posetestbot.bop.evaluation import (
    create_evaluation_request,
    create_simulated_bop_result,
    inspect_dataset,
)
from posetestbot.calibration.profile_library import (
    list_calibration_library,
    select_calibration_profile_snapshot,
    verify_calibration_profile_selection,
)
from posetestbot.io.artifacts import BOP_EXPORT_MANIFEST
from posetestbot.pipeline import orchestration
from posetestbot.pipeline.capture_completion import build_capture_completion
from posetestbot.pipeline.run_config import (
    create_run_config,
    load_run_config_for_run_root,
    sensor_config_from_token,
    write_run_config_with_manifest,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH
from posetestbot.sensors.contracts import CameraIntrinsics
from posetestbot.sensors.frame_writer import write_camera_sidecars
from tests.test_bop_evaluation import make_tiny_evaluation_run
from tests.test_calibration_attempt_promotion import (
    prepare_promoted_calibration_for_workflow,
)


def _write_current_capture(run_root: Path, run_id: str) -> None:
    sensor_root = run_root / "realsense_1"
    (sensor_root / "rgb").mkdir(parents=True)
    (sensor_root / "depth").mkdir()
    write_camera_sidecars(
        sensor_root,
        CameraIntrinsics(
            cam_k=(100.0, 0.0, 4.0, 0.0, 100.0, 4.0, 0.0, 0.0, 1.0),
            width=8,
            height=8,
            distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
            depth_scale_to_mm=1.0,
        ),
        include_distortion_in_cam_k=True,
    )
    metadata = []
    poses = {}
    for index in range(2):
        frame_id = f"{1000 + index * 50}.png"
        (sensor_root / "rgb" / frame_id).write_bytes(b"rgb")
        (sensor_root / "depth" / frame_id).write_bytes(b"depth")
        host_received_ns = 1_000_000_000 + index * 50_000_000
        host_wall_ns = 10_000_000_000 + index * 50_000_000
        metadata.append(
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "1",
                "frame_index": index,
                "frame_id": frame_id,
                "rgb_path": f"rgb/{frame_id}",
                "depth_path": f"depth/{frame_id}",
                "sensor_timestamp_ns": host_wall_ns,
                "host_received_timestamp_ns": host_received_ns,
                "host_wall_timestamp_ns": host_wall_ns,
                "color_timestamp_domain": "global_time",
            }
        )
        poses[str(index)] = {
            "framename": 1000 + index * 50,
            "host_received_timestamp_ns": host_received_ns,
            "host_wall_timestamp_ns": host_wall_ns,
            "frame_delta": 0 if index == 0 else 50,
            "motion": "circ_far",
            "pose": {
                "X": float(index),
                "Y": 0.0,
                "Z": 500.0,
                "A": 0.0,
                "B": 0.0,
                "C": 0.0,
            },
            "source_packet": {
                "schema_version": "robot_pose.v1",
                "packet_kind": "pose",
                "sequence": index,
                "sender_monotonic_ns": host_received_ns,
                "sender_wall_timestamp_ms": host_wall_ns // 1_000_000,
                "run_id": run_id,
                "from_frame": "robot_flange",
                "to_frame": "template_base",
                "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
                "sequence_delta": 0 if index == 0 else 1,
                "estimated_packets_lost": 0,
            },
        }
    (sensor_root / "frame_metadata.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in metadata)
    )
    (run_root / "raw_robot_ee_poses.json").write_text(json.dumps(poses))


def _completed_processes() -> list[dict[str, object]]:
    return [
        {
            "role": "sensor_capture",
            "status": "stopped",
            "termination_reason": "stopped_after_receiver_exit",
            "ended_at": "2026-08-19T08:00:01+00:00",
        },
        {
            "role": "robot_pose_receiver",
            "status": "succeeded",
            "termination_reason": "receiver_completed",
            "ended_at": "2026-08-19T08:00:00+00:00",
        },
    ]


def test_simulated_current_workflows_handoff_from_empty_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise both guided outcomes without opening hardware or running tools."""

    calibration_root = prepare_promoted_calibration_for_workflow(
        tmp_path, monkeypatch
    )
    assert load_run_config_for_run_root(calibration_root)["schema_version"] == (
        "run_config.v4"
    )

    dataset_root = tmp_path / "dataset"
    assert not dataset_root.exists()
    sensor = sensor_config_from_token(
        "realsense_d435:1:eye_in_hand:Workflow D435"
    )
    sensor = replace(sensor, metadata={"image_size": [8, 8]})
    initial = create_run_config(
        run_root=dataset_root,
        capture_intent="dataset",
        bop_annotation_mode="pose_and_masks",
        sensors=(sensor,),
    )
    write_run_config_with_manifest(dataset_root, initial)

    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", tmp_path.as_posix())
    library = list_calibration_library(dataset_root)
    reusable = next(
        candidate
        for candidate in library["calibrations"]
        if candidate["source_run_root"] == calibration_root.as_posix()
    )
    assert reusable["valid"] is True
    assert reusable["compatible"] is True, reusable
    selected = select_calibration_profile_snapshot(
        dataset_root,
        source_run_root=calibration_root,
        expected_bundle_sha256=reusable["bundle_sha256"],
        operator="simulated-workflow",
    )
    profile_id = selected["sensor_profiles"]["realsense_d435:1"]
    selected_sensor = replace(sensor, calibration_profile_id=profile_id)
    selection = selected["selection"]
    configured = create_run_config(
        run_root=dataset_root,
        run_id=initial.run_id,
        capture_intent="dataset",
        bop_annotation_mode="pose_and_masks",
        sensors=(selected_sensor,),
        calibration_profiles=selected["calibration_profiles"],
        intrinsic_calibration_profiles=selected["intrinsic_calibration_profiles"],
        calibration_profile_selection={
            "selection_artifact": "calibration_profile_selection.json",
            "bundle_sha256": selection["source"]["bundle_sha256"],
            "selected_at": selection["selected_at"],
        },
    )
    write_run_config_with_manifest(dataset_root, configured)
    verified_selection = verify_calibration_profile_selection(dataset_root)
    assert verified_selection["schema_version"] == "calibration_profile_selection.v2"

    plan_path, plan = orchestration.plan_capture(dataset_root, max_frames=2)
    assert plan_path.is_file()
    assert plan["capture"]["enabled_sensor_count"] == 1
    _write_current_capture(dataset_root, configured.run_id)
    completion = build_capture_completion(
        dataset_root,
        configured.to_dict(),
        _completed_processes(),
    )
    assert completion["status"] == "ok"

    expected_commands = orchestration.dataset_processing_commands(dataset_root)
    invoked: list[tuple[str, ...]] = []

    def simulate_worker(command, **kwargs) -> None:
        assert kwargs["check"] is True
        invoked.append(tuple(command))

    monkeypatch.setattr(orchestration.subprocess, "run", simulate_worker)
    assert orchestration.process_dataset(dataset_root) == expected_commands
    assert invoked == list(expected_commands)

    # Simulate the fixed processing worker plus the optional annotation worker
    # publishing a valid, annotation-bearing BOP tree in this same run.
    assert make_tiny_evaluation_run(tmp_path, name=dataset_root.name) == dataset_root
    manifest = json.loads((dataset_root / "bop" / BOP_EXPORT_MANIFEST).read_text())
    assert manifest["schema_version"] == "bop_export_manifest.v5"
    assert manifest["annotation_source"] == "blenderproc"
    assert manifest["annotation_state"] == "complete"

    inspection = inspect_dataset(dataset_root)
    assert inspection["status"] == "ready"
    result = create_simulated_bop_result(
        dataset_root,
        method_name="Workflow acceptance",
        seed=19,
    )
    request = create_evaluation_request(
        dataset_root,
        result_id=result["result_id"],
    )
    assert request["schema_version"] == "bop_evaluation_request.v1"
    assert request["dataset_sha256"] == inspection["dataset_sha256"]
    assert request["result_id"] == result["result_id"]
