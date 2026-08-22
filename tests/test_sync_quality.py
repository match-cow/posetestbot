from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from posetestbot.io.artifacts import (
    CURRENT_SENSOR_METADATA_ARTIFACTS,
    DATASET_MANIFEST,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    MATCH_ROBOT_EE_POSES,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
    SYNC_QUALITY_REPORT,
    SYNC_REPORT,
)
from posetestbot.io.atomic import replace_directories
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


def file_evidence(path: Path, run_root: Path) -> dict:
    return {
        "path": path.relative_to(run_root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def directory_evidence(folder: Path) -> dict:
    rows = [
        {
            "path": path.relative_to(folder).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != SYNC_REPORT
    ]
    payload = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    ).encode()
    return {
        "algorithm": "sha256",
        "file_count": len(rows),
        "total_size_bytes": sum(row["size_bytes"] for row in rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


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
    schema_version: str = "sync_report.v4",
    calibration_sync: dict | None = None,
) -> Path:
    sensor_id = sensor_name.removeprefix("realsense_")
    effective_sync_delta_ms = (
        calibration_sync["sensor"]["sync_delta_ms"] if calibration_sync else 0
    )
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
    raw_sensor = run_root / sensor_name
    raw_sensor.mkdir(parents=True, exist_ok=True)
    raw_metadata_path = raw_sensor / FRAME_METADATA_JSONL
    raw_metadata_path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": "frame_metadata.v1",
                    "frame_index": index,
                    "frame_id": f"source-{index:06d}.png",
                    "sensor_type": "realsense_d435",
                    "sensor_id": sensor_id,
                    "rgb_path": f"rgb/source-{index:06d}.png",
                    "depth_path": f"depth/source-{index:06d}.png",
                    "sensor_timestamp_ns": index + 1001,
                    "host_received_timestamp_ns": index + 1,
                    "host_wall_timestamp_ns": index + 1001,
                },
                separators=(",", ":"),
            )
            + "\n"
            for index in range(total_frames)
        )
    )
    (raw_sensor / RGB_DIR).mkdir(exist_ok=True)
    (raw_sensor / DEPTH_DIR).mkdir(exist_ok=True)
    for index in range(total_frames):
        source_frame_id = f"source-{index:06d}.png"
        (raw_sensor / RGB_DIR / source_frame_id).write_bytes(
            f"raw-rgb-{index}".encode()
        )
        (raw_sensor / DEPTH_DIR / source_frame_id).write_bytes(
            f"raw-depth-{index}".encode()
        )
    for name in CURRENT_SENSOR_METADATA_ARTIFACTS:
        if name != FRAME_METADATA_JSONL:
            (raw_sensor / name).write_text(f"raw test {name}\n")
    raw_robot_path = run_root / RAW_ROBOT_EE_POSES
    run_id = json.loads((run_root / "run_config.json").read_text())["run_id"]
    raw_robot_path.write_text(
        json.dumps(
            {
                "0": {
                    "motion": "circ_far",
                    "host_received_timestamp_ns": max_delta_ns + 1,
                    "host_wall_timestamp_ns": max_delta_ns + 1001,
                    "pose": {"X": 0, "Y": 0, "Z": 0, "A": 0, "B": 0, "C": 0},
                    "source_packet": {
                        "schema_version": "robot_pose.v1",
                        "packet_kind": "pose",
                        "sequence": 0,
                        "sender_monotonic_ns": 1,
                        "sender_wall_timestamp_ms": 1,
                        "run_id": run_id,
                        "from_frame": "robot_flange",
                        "to_frame": "template_base",
                        "sunrise_reference_frame_path": (
                            "/PoseTestBot/PoseTemplateBase"
                        ),
                        "sequence_delta": 0,
                        "estimated_packets_lost": 0,
                    },
                    "stream_end_source_packet": {
                        "schema_version": "robot_pose.v1",
                        "packet_kind": "end",
                        "sequence": 1,
                        "sender_monotonic_ns": 2,
                        "sender_wall_timestamp_ms": 2,
                        "run_id": run_id,
                        "from_frame": "robot_flange",
                        "to_frame": "template_base",
                        "sunrise_reference_frame_path": (
                            "/PoseTestBot/PoseTemplateBase"
                        ),
                        "sequence_delta": 1,
                        "estimated_packets_lost": 0,
                    },
                }
            }
        )
        + "\n"
    )
    raw_pose_record = json.loads(raw_robot_path.read_text())["0"]
    matched: dict[str, dict] = {}
    derived_metadata: list[dict] = []
    nearest_deltas: list[int] = []
    for index in range(matched_frames):
        frame_id = f"{index:06d}.png"
        selected_frame_timestamp_ns = (
            index + 1 if timestamp_source == "host_received" else index + 1001
        )
        selected_robot_timestamp_ns = (
            max_delta_ns + 1
            if robot_timestamp_source == "host_received"
            else max_delta_ns + 1001
        )
        nearest_delta_ns = (
            selected_robot_timestamp_ns - selected_frame_timestamp_ns
        )
        nearest_deltas.append(nearest_delta_ns)
        (report_path.parent / RGB_DIR).mkdir(exist_ok=True)
        (report_path.parent / DEPTH_DIR).mkdir(exist_ok=True)
        (report_path.parent / RGB_DIR / frame_id).write_bytes(
            (raw_sensor / RGB_DIR / f"source-{index:06d}.png").read_bytes()
        )
        (report_path.parent / DEPTH_DIR / frame_id).write_bytes(
            (raw_sensor / DEPTH_DIR / f"source-{index:06d}.png").read_bytes()
        )
        derived_metadata.append(
            {
                "schema_version": "frame_metadata.v1",
                "frame_index": index,
                "frame_id": frame_id,
                "sensor_type": "realsense_d435",
                "sensor_id": sensor_id,
                "rgb_path": f"rgb/{frame_id}",
                "depth_path": f"depth/{frame_id}",
                "sensor_timestamp_ns": index + 1001,
                "host_received_timestamp_ns": index + 1,
                "host_wall_timestamp_ns": index + 1001,
                "source_frame_index": index,
                "source_frame_id": f"source-{index:06d}.png",
                "source_rgb_path": f"rgb/source-{index:06d}.png",
                "source_depth_path": f"depth/source-{index:06d}.png",
                "sync_timestamp_ns": selected_frame_timestamp_ns,
                "sync_delta_ms": effective_sync_delta_ms,
                "sync_requested_timestamp_source": timestamp_source,
                "sync_timestamp_source": timestamp_source,
                "sync_robot_timestamp_source": robot_timestamp_source,
                "matched_robot_pose_index": 0,
                "nearest_robot_delta_ns": nearest_delta_ns,
                "motion": "circ_far",
            }
        )
        matched[frame_id] = {
            "source_frame_id": f"source-{index:06d}.png",
            "source_rgb": f"rgb/source-{index:06d}.png",
            "source_depth": f"depth/source-{index:06d}.png",
            "image_timestamp_ns": selected_frame_timestamp_ns,
            "timestamp_source": timestamp_source,
            "robot_timestamp_source": robot_timestamp_source,
            "delayed_timestamp_ns": selected_frame_timestamp_ns,
            "robot_timestamp_ns": selected_robot_timestamp_ns,
            "matched_robot_pose_index": 0,
            "nearest_robot_delta_ns": nearest_delta_ns,
            "motion": "circ_far",
            "robot_ee_pose": raw_pose_record["pose"],
            "source_packet": raw_pose_record["source_packet"],
            "synchronized_rgb": (report_path.parent / RGB_DIR / frame_id)
            .relative_to(run_root)
            .as_posix(),
            "synchronized_depth": (report_path.parent / DEPTH_DIR / frame_id)
            .relative_to(run_root)
            .as_posix(),
        }
    derived_metadata_path = report_path.parent / FRAME_METADATA_JSONL
    derived_metadata_path.write_text(
        "".join(
            json.dumps(record, separators=(",", ":")) + "\n"
            for record in derived_metadata
        )
    )
    matched_path = report_path.parent / MATCH_ROBOT_EE_POSES
    matched_path.write_text(json.dumps(matched) + "\n")
    copied_metadata_artifacts = [
        name
        for name in CURRENT_SENSOR_METADATA_ARTIFACTS
        if name != FRAME_METADATA_JSONL
    ]
    for name in copied_metadata_artifacts:
        (report_path.parent / name).write_bytes((raw_sensor / name).read_bytes())
    value = {
        "schema_version": schema_version,
        "sync_generation_id": "test-sync-generation",
        "output_contract": "rgbd_copy",
        "input_evidence": {
            FRAME_METADATA_JSONL: file_evidence(raw_metadata_path, run_root),
            RAW_ROBOT_EE_POSES: file_evidence(raw_robot_path, run_root),
            "sensor_artifact_set": directory_evidence(raw_sensor),
        },
        "output_evidence": {
            FRAME_METADATA_JSONL: file_evidence(derived_metadata_path, run_root),
            MATCH_ROBOT_EE_POSES: file_evidence(matched_path, run_root),
            "artifact_set": directory_evidence(report_path.parent),
        },
        "sensor_folder": raw_sensor.relative_to(run_root).as_posix(),
        "output_folder": report_path.parent.relative_to(run_root).as_posix(),
        "timestamp_source": timestamp_source,
        "requested_timestamp_source": timestamp_source,
        "timestamp_source_counts": {timestamp_source: total_frames},
        "timestamp_fallback_count": 0,
        "timestamp_missing_count": 0,
        "sync_delta_ms": effective_sync_delta_ms,
        "max_nearest_pose_delta_ms": 20.0,
        "nearest_pose_delta_rejection_count": dropped_frames,
        "total_frames": total_frames,
        "matched_frames": matched_frames,
        "dropped_frames": dropped_frames,
        "dropped": [
            {
                "motion": "circ_far",
                "reason": "nearest robot pose delta exceeds threshold",
            }
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
        "copied_metadata_artifacts": copied_metadata_artifacts,
        "motion_intervals": [{"motion": "circ_far", "pose_count": matched_frames}],
        "mean_abs_nearest_pose_delta_ns": (
            sum(abs(delta) for delta in nearest_deltas) / len(nearest_deltas)
            if nearest_deltas
            else None
        ),
        "max_abs_nearest_pose_delta_ns": (
            max(abs(delta) for delta in nearest_deltas) if nearest_deltas else None
        ),
        "required_frame_timestamp_domain": (
            calibration_sync["sensor"]["required_frame_timestamp_domain"]
            if calibration_sync
            else None
        ),
        "timestamp_fallback_allowed": (
            calibration_sync["sensor"]["timestamp_fallback_allowed"]
            if calibration_sync
            else False
        ),
        "calibration_sync": calibration_sync,
    }
    if schema_version == "sync_report.v4":
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


def test_build_sync_quality_report_fails_when_packet_loss_is_unaudited(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    value = json.loads(report_path.read_text())
    value["robot_pose_packet_loss_audited"] = False
    value["robot_pose_packet_loss_count"] = None
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    packet_loss = next(
        check
        for check in report["checks"]
        if check["name"] == "sync_robot_pose_packet_loss"
    )
    assert packet_loss["status"] == "error"
    assert "missing or invalid" in packet_loss["message"]


def test_build_sync_quality_report_rejects_stale_or_incomplete_artifacts(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    source_metadata = run_root / "realsense_123" / FRAME_METADATA_JSONL
    source_metadata.write_text(source_metadata.read_text() + "{}\n")

    stale = build_sync_quality_report(run_root)

    assert stale["overall_status"] == "error"
    load_error = next(
        check
        for check in stale["checks"]
        if check["name"].startswith("sync_report_load:")
    )
    assert "evidence is stale" in load_error["message"]

    # Restore the source evidence, then prove output membership is checked too.
    value = json.loads(report_path.read_text())
    source_metadata.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": "frame_metadata.v1",
                    "frame_index": index,
                    "frame_id": f"source-{index:06d}.png",
                    "sensor_type": "realsense_d435",
                    "sensor_id": "123",
                    "rgb_path": f"rgb/source-{index:06d}.png",
                    "depth_path": f"depth/source-{index:06d}.png",
                    "sensor_timestamp_ns": index + 1001,
                    "host_received_timestamp_ns": index + 1,
                    "host_wall_timestamp_ns": index + 1001,
                },
                separators=(",", ":"),
            )
            + "\n"
            for index in range(10)
        )
    )
    assert (
        file_evidence(source_metadata, run_root)
        == value["input_evidence"][FRAME_METADATA_JSONL]
    )
    (report_path.parent / DEPTH_DIR / "000000.png").unlink()

    incomplete = build_sync_quality_report(run_root)

    assert incomplete["overall_status"] == "error"
    assert any(
        "artifact-set evidence is stale" in check["message"]
        for check in incomplete["checks"]
        if check["status"] == "error"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        f"{RGB_DIR}/source-000000.png",
        f"{DEPTH_DIR}/source-000000.png",
        *[
            name
            for name in CURRENT_SENSOR_METADATA_ARTIFACTS
            if name != FRAME_METADATA_JSONL
        ],
    ],
)
def test_build_sync_quality_report_rejects_changed_raw_sensor_artifacts(
    tmp_path: Path,
    relative_path: str,
) -> None:
    run_root = tmp_path / "run"
    write_sync_report(run_root)
    source = run_root / "realsense_123" / relative_path
    source.write_bytes(source.read_bytes() + b"tampered")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    load_error = next(
        check
        for check in report["checks"]
        if check["name"].startswith("sync_report_load:")
    )
    assert "raw RGB-D and camera-sidecar evidence is stale" in load_error["message"]


def test_build_sync_quality_report_recomputes_nearest_pose_summary(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root, max_delta_ns=10_000_000)
    value = json.loads(report_path.read_text())
    value["mean_abs_nearest_pose_delta_ns"] = 0
    value["max_abs_nearest_pose_delta_ns"] = 0
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    assert any(
        "nearest-pose summary does not match" in check["message"]
        for check in report["checks"]
        if check["status"] == "error"
    )


def test_build_sync_quality_report_rejects_sensor_identity_mismatch(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    raw_metadata_path = run_root / "realsense_123" / FRAME_METADATA_JSONL
    records = [json.loads(line) for line in raw_metadata_path.read_text().splitlines()]
    for record in records:
        record["sensor_id"] = "other-device"
    raw_metadata_path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    )
    value = json.loads(report_path.read_text())
    value["input_evidence"][FRAME_METADATA_JSONL] = file_evidence(
        raw_metadata_path, run_root
    )
    value["input_evidence"]["sensor_artifact_set"] = directory_evidence(
        run_root / "realsense_123"
    )
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    assert any(
        "identity does not match run_config" in check["message"]
        for check in report["checks"]
        if check["status"] == "error"
    )


def test_build_sync_quality_report_rejects_reordered_source_timestamps(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    raw_metadata_path = run_root / "realsense_123" / FRAME_METADATA_JSONL
    records = [json.loads(line) for line in raw_metadata_path.read_text().splitlines()]
    records[1]["host_received_timestamp_ns"] = records[0][
        "host_received_timestamp_ns"
    ]
    raw_metadata_path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    )
    value = json.loads(report_path.read_text())
    value["input_evidence"][FRAME_METADATA_JSONL] = file_evidence(
        raw_metadata_path,
        run_root,
    )
    value["input_evidence"]["sensor_artifact_set"] = directory_evidence(
        run_root / "realsense_123"
    )
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    assert any(
        "timestamps must strictly increase" in check["message"]
        for check in report["checks"]
        if check["status"] == "error"
    )


def test_build_sync_quality_report_binds_copied_frames_to_raw_sources(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    copied_rgb = report_path.parent / RGB_DIR / "000000.png"
    copied_rgb.write_bytes(b"different-but-self-hashed-output")
    value = json.loads(report_path.read_text())
    value["output_evidence"]["artifact_set"] = directory_evidence(
        report_path.parent
    )
    report_path.write_text(json.dumps(value) + "\n")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    assert any(
        "synchronized RGB-D bytes do not match" in check["message"]
        for check in report["checks"]
        if check["status"] == "error"
    )


def test_sync_evidence_survives_downstream_blenderproc_publication(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    sensor_folder = report_path.parent
    blenderproc_staging = sensor_folder / ".blenderproc.test.staging"
    masks_staging = sensor_folder / ".masks.test.staging"
    blenderproc_staging.mkdir()
    masks_staging.mkdir()
    (blenderproc_staging / "frame_contract.json").write_text("{}\n")
    (masks_staging / "000000.png").write_bytes(b"derived-mask")

    replace_directories(
        [
            (blenderproc_staging, sensor_folder / "blenderproc"),
            (masks_staging, sensor_folder / "masks"),
        ]
    )

    assert (sensor_folder / ".posetestbot-directory-replace.lock").is_file()
    report = build_sync_quality_report(run_root)
    assert report["overall_status"] == "ok"


@pytest.mark.parametrize(
    "artifact_name",
    ["unexpected.txt", ".posetestbot-directory-replace.not-a-journal"],
)
def test_sync_evidence_rejects_unowned_root_artifact(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    run_root = tmp_path / "run"
    report_path = write_sync_report(run_root)
    (report_path.parent / artifact_name).write_text("not sync-owned\n")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    assert any(
        "Unexpected artifact in synchronized sensor root" in check["message"]
        for check in report["checks"]
        if check["status"] == "error"
    )


def test_build_sync_quality_report_rejects_missing_or_mixed_sensor_generations(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="none",
            sensors=(
                SensorRunConfig("realsense_d435", "123", "D435 A"),
                SensorRunConfig("realsense_d435", "456", "D435 B"),
            ),
        ),
    )
    write_sync_report(run_root, sensor_name="realsense_123")

    missing = build_sync_quality_report(run_root)

    assert missing["overall_status"] == "error"
    coverage = next(
        check for check in missing["checks"] if check["name"] == "sync_sensor_coverage"
    )
    assert coverage["details"]["missing_sensor_folders"] == ["realsense_456"]

    second = write_sync_report(run_root, sensor_name="realsense_456")
    value = json.loads(second.read_text())
    value["sync_generation_id"] = "different-generation"
    second.write_text(json.dumps(value) + "\n")

    mixed = build_sync_quality_report(run_root)

    generation = next(
        check
        for check in mixed["checks"]
        if check["name"] == "sync_generation_coherence"
    )
    assert generation["status"] == "error"


def test_build_sync_quality_report_rejects_retired_sync_schema(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    write_sync_report(run_root, schema_version="sync_report.v3")

    report = build_sync_quality_report(run_root)

    assert report["overall_status"] == "error"
    assert any(
        "Unsupported sync report schema" in check["message"]
        for check in report["checks"]
        if check["status"] == "error"
    )


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
        timestamp_source="host_received",
        max_delta_ns=90_000_000,
    )

    report = build_sync_quality_report(
        run_root,
        min_match_ratio=0.8,
        max_dropped_frames=2,
        max_nearest_pose_delta_ms=50.0,
        require_timestamp_source="host_wall",
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
            "dropped": [
                {"reason": "outside robot motion intervals"} for _ in range(92)
            ],
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


def test_v4_sync_report_audits_frame_and_robot_timestamp_pair(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    write_sync_report(
        run_root,
        schema_version="sync_report.v4",
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
        schema_version="sync_report.v4",
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
        schema_version="sync_report.v4",
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
