from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from posetestbot.sync import non_destructive as sync_module
from posetestbot.io.artifacts import (
    CAMERA_DATA_JSON,
    CAMERA_JSON,
    CAM_K,
    DATASET_MANIFEST,
    DEPTH_DIR,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
    MATCH_ROBOT_EE_POSES,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
    SYNC_REPORT,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    create_run_config,
    write_run_config,
)
from posetestbot.sync.non_destructive import (
    SyncResult,
    resolve_frame_timestamp,
    resolve_max_nearest_pose_delta_ms,
    resolve_sync_delta_ms,
    resolve_timestamp_pair,
    synchronize_run,
    synchronize_sensor_folder,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def create_sync_fixture(tmp_path: Path) -> tuple[Path, Path]:
    run_root = tmp_path / "run-1"
    config = create_run_config(
        run_root=run_root,
        capture_intent="dataset",
        bop_annotation_mode="none",
        sensors=(SensorRunConfig("realsense_d435", "123", "D435"),),
    )
    write_run_config(run_root, config)
    run_id = config.run_id
    sensor_folder = run_root / "realsense_123"
    rgb_folder = sensor_folder / RGB_DIR
    depth_folder = sensor_folder / DEPTH_DIR
    rgb_folder.mkdir(parents=True)
    depth_folder.mkdir()

    for frame_id in ["1000.png", "1050.png", "1500.png"]:
        (rgb_folder / frame_id).write_bytes(f"rgb:{frame_id}".encode())
        (depth_folder / frame_id).write_bytes(f"depth:{frame_id}".encode())

    (sensor_folder / CAM_K).write_text("1 0 2\n0 3 4\n0 0 1\n")
    (sensor_folder / DEPTH_SCALE).write_text("1.0\n")
    write_json(sensor_folder / CAMERA_JSON, {"cam_K": [1, 0, 2, 0, 3, 4, 0, 0, 1]})
    write_json(
        sensor_folder / CAMERA_DATA_JSON, {"K": [[1, 0, 2], [0, 3, 4], [0, 0, 1]]}
    )

    write_jsonl(
        sensor_folder / FRAME_METADATA_JSONL,
        [
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "123",
                "frame_index": 0,
                "frame_id": "1000.png",
                "rgb_path": "rgb/1000.png",
                "depth_path": "depth/1000.png",
                "sensor_timestamp_ns": 10,
                "host_received_timestamp_ns": 1_000_000_000,
                "host_wall_timestamp_ns": 10_000_000_000,
            },
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "123",
                "frame_index": 1,
                "frame_id": "1050.png",
                "rgb_path": "rgb/1050.png",
                "depth_path": "depth/1050.png",
                "sensor_timestamp_ns": 20,
                "host_received_timestamp_ns": 1_050_000_000,
                "host_wall_timestamp_ns": 10_050_000_000,
            },
            {
                "schema_version": "frame_metadata.v1",
                "sensor_type": "realsense_d435",
                "sensor_id": "123",
                "frame_index": 2,
                "frame_id": "1500.png",
                "rgb_path": "rgb/1500.png",
                "depth_path": "depth/1500.png",
                "sensor_timestamp_ns": 30,
                "host_received_timestamp_ns": 1_500_000_000,
                "host_wall_timestamp_ns": 10_500_000_000,
            },
        ],
    )

    write_json(
        run_root / RAW_ROBOT_EE_POSES,
        {
            "0": {
                "framename": 1000,
                "host_received_timestamp_ns": 1_000_000_000,
                "host_wall_timestamp_ns": 10_000_000_000,
                "motion": "circ_far",
                "source_packet": {
                    "schema_version": "robot_pose.v1",
                    "packet_kind": "pose",
                    "sequence": 0,
                    "run_id": run_id,
                    "from_frame": "robot_flange",
                    "to_frame": "template_base",
                    "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase",
                },
                "pose": {"X": 1, "Y": 2, "Z": 3, "A": 4, "B": 5, "C": 6},
            },
            "1": {
                "framename": 1100,
                "host_received_timestamp_ns": 1_100_000_000,
                "host_wall_timestamp_ns": 10_100_000_000,
                "motion": "circ_far",
                "source_packet": {
                    "schema_version": "robot_pose.v1",
                    "packet_kind": "pose",
                    "sequence": 1,
                    "run_id": run_id,
                    "from_frame": "robot_flange",
                    "to_frame": "template_base",
                    "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase",
                },
                "pose": {"X": 7, "Y": 8, "Z": 9, "A": 10, "B": 11, "C": 12},
            },
            "2": {
                "framename": 2000,
                "host_received_timestamp_ns": 2_000_000_000,
                "host_wall_timestamp_ns": 11_000_000_000,
                "motion": "zoom",
                "source_packet": {
                    "schema_version": "robot_pose.v1",
                    "packet_kind": "pose",
                    "sequence": 2,
                    "run_id": run_id,
                    "from_frame": "robot_flange",
                    "to_frame": "template_base",
                    "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase",
                },
                "pose": {"X": 13, "Y": 14, "Z": 15, "A": 16, "B": 17, "C": 18},
            },
        },
    )
    return run_root, sensor_folder


def test_synchronize_sensor_folder_preserves_raw_frames(tmp_path: Path) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)

    result = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="host_received",
    )

    assert result.total_frames == 3
    assert result.matched_frames == 2
    assert result.dropped_frames == 1
    assert (sensor_folder / RGB_DIR / "1000.png").exists()
    assert (sensor_folder / DEPTH_DIR / "1050.png").exists()

    output_folder = Path(result.output_folder)
    assert (output_folder / RGB_DIR / "000000.png").read_bytes() == b"rgb:1000.png"
    assert (output_folder / DEPTH_DIR / "000001.png").read_bytes() == b"depth:1050.png"
    assert (output_folder / CAM_K).read_text() == "1 0 2\n0 3 4\n0 0 1\n"
    assert (output_folder / DEPTH_SCALE).read_text() == "1.0\n"
    assert (output_folder / CAMERA_JSON).exists()
    assert (output_folder / CAMERA_DATA_JSON).exists()
    assert (output_folder / FRAME_METADATA_JSONL).exists()
    derived_metadata = [
        json.loads(line)
        for line in (output_folder / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    assert [record["frame_id"] for record in derived_metadata] == [
        "000000.png",
        "000001.png",
    ]
    assert derived_metadata[0]["rgb_path"] == "rgb/000000.png"
    assert derived_metadata[0]["source_frame_id"] == "1000.png"
    assert derived_metadata[0]["sync_timestamp_source"] == "host_received"
    matched = json.loads((output_folder / MATCH_ROBOT_EE_POSES).read_text())
    assert list(matched) == ["000000.png", "000001.png"]
    assert matched["000000.png"]["motion"] == "circ_far"
    assert matched["000000.png"]["source_rgb"] == "rgb/1000.png"
    assert matched["000000.png"]["synchronized_rgb"].endswith(
        "processed/synchronized/realsense_123/rgb/000000.png"
    )
    assert abs(matched["000001.png"]["nearest_robot_delta_ns"]) == 50_000_000

    report = json.loads((output_folder / SYNC_REPORT).read_text())
    assert report["schema_version"] == "sync_report.v3"
    assert report["timestamp_pair"] == {
        "frame_timestamp_source": "host_received",
        "requested_frame_timestamp_source": "host_received",
        "robot_timestamp_source": "host_received",
    }
    assert report["timestamp_pair_provenance_audited"] is True
    assert report["matched_frames"] == 2
    assert report["eligible_in_motion_frames"] == 2
    assert report["matched_eligible_frames"] == 2
    assert report["eligible_motion_coverage"] == 1.0
    assert report["outside_motion_interval_frame_count"] == 1
    assert report["in_motion_exclusion_count"] == 0
    assert report["unexplained_in_motion_exclusion_count"] == 0
    assert report["dropped"][0]["frame_id"] == "1500.png"
    assert CAM_K in report["copied_metadata_artifacts"]
    assert FRAME_METADATA_JSONL not in report["copied_metadata_artifacts"]


def test_synchronize_sensor_folder_uses_verified_robot_pose_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    verified_snapshot = json.loads((run_root / RAW_ROBOT_EE_POSES).read_text())
    verified_snapshot["0"]["pose"]["X"] = 999
    monkeypatch.setattr(
        sync_module,
        "load_robot_poses",
        lambda *_args: pytest.fail("verified raw robot poses must not be reopened"),
    )

    result = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="host_received",
        raw_robot_poses=verified_snapshot,
    )

    matched = json.loads(Path(result.matched_poses_path).read_text())
    assert matched["000000.png"]["robot_ee_pose"]["X"] == 999
    assert (
        matched["000000.png"]["source_packet"]
        == (verified_snapshot["0"]["source_packet"])
    )


def test_synchronize_run_robot_pose_override_requires_exactly_one_sensor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    second_folder = run_root / "realsense_456"
    shutil.copytree(sensor_folder, second_folder)
    verified_snapshot = json.loads((run_root / RAW_ROBOT_EE_POSES).read_text())
    monkeypatch.setattr(
        sync_module,
        "load_robot_poses",
        lambda *_args: pytest.fail("run-level override must reach the sensor sync"),
    )

    results = synchronize_run(
        run_root,
        sensor_folders=[sensor_folder],
        output_root=run_root / "processed" / "verified-override",
        sync_delta=0,
        raw_robot_poses=verified_snapshot,
    )

    assert len(results) == 1

    with pytest.raises(ValueError, match="exactly one selected sensor"):
        synchronize_run(
            run_root,
            sensor_folders=[sensor_folder, second_folder],
            raw_robot_poses=verified_snapshot,
        )


def test_synchronize_sensor_folder_replaces_stale_derived_frames(
    tmp_path: Path,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    first = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="host_received",
    )
    output_folder = Path(first.output_folder)
    assert len(list((output_folder / RGB_DIR).glob("*.png"))) == 2

    metadata_records = [
        json.loads(line)
        for line in (sensor_folder / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    write_jsonl(sensor_folder / FRAME_METADATA_JSONL, [metadata_records[0]])

    second = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="host_received",
    )

    assert second.matched_frames == 1
    assert [path.name for path in (output_folder / RGB_DIR).glob("*.png")] == [
        "000000.png"
    ]
    assert [path.name for path in (output_folder / DEPTH_DIR).glob("*.png")] == [
        "000000.png"
    ]


def test_sync_strict_nearest_pose_delta_drops_outlier_before_derived_output(
    tmp_path: Path,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)

    result = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="host_received",
        max_nearest_pose_delta_ms=20.0,
    )

    assert result.total_frames == 3
    assert result.matched_frames == 1
    assert result.dropped_frames == 2
    assert (sensor_folder / RGB_DIR / "1050.png").is_file()
    output_folder = Path(result.output_folder)
    assert [path.name for path in (output_folder / RGB_DIR).glob("*.png")] == [
        "000000.png"
    ]
    matched = json.loads((output_folder / MATCH_ROBOT_EE_POSES).read_text())
    assert list(matched) == ["000000.png"]

    report = json.loads((output_folder / SYNC_REPORT).read_text())
    assert report["max_nearest_pose_delta_ms"] == 20.0
    assert report["nearest_pose_delta_rejection_count"] == 1
    assert report["eligible_in_motion_frames"] == 2
    assert report["matched_eligible_frames"] == 1
    assert report["eligible_motion_coverage"] == 0.5
    assert report["outside_motion_interval_frame_count"] == 1
    assert report["in_motion_exclusion_count"] == 1
    assert report["unexplained_in_motion_exclusion_count"] == 0
    assert report["mean_abs_nearest_pose_delta_ns"] == 0
    assert report["max_abs_nearest_pose_delta_ns"] == 0
    rejected = next(
        item
        for item in report["dropped"]
        if item["reason"] == "nearest robot pose delta exceeds threshold"
    )
    assert rejected == {
        "frame_id": "1050.png",
        "timestamp_ns": 1_050_000_000,
        "timestamp_source": "host_received",
        "robot_timestamp_source": "host_received",
        "delayed_timestamp_ns": 1_050_000_000,
        "motion": "circ_far",
        "matched_robot_pose_index": 0,
        "robot_timestamp_ns": 1_000_000_000,
        "nearest_robot_delta_ns": -50_000_000,
        "abs_nearest_robot_delta_ns": 50_000_000,
        "max_nearest_pose_delta_ms": 20.0,
        "max_nearest_pose_delta_ns": 20_000_000,
        "reason": "nearest robot pose delta exceeds threshold",
    }


def test_sync_rejects_missing_current_host_timestamp(tmp_path: Path) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    records = [
        json.loads(line)
        for line in (sensor_folder / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    records[0].pop("host_received_timestamp_ns")
    write_jsonl(sensor_folder / FRAME_METADATA_JSONL, records)

    with pytest.raises(
        ValueError, match="requires positive host_received_timestamp_ns"
    ):
        synchronize_sensor_folder(
            sensor_folder,
            run_root=run_root,
            sync_delta=0,
            timestamp_source="host_received",
        )


def test_sensor_exposure_timestamp_pairs_explicitly_with_robot_wall_clock(
    tmp_path: Path,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    records = [
        json.loads(line)
        for line in (sensor_folder / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    for record in records:
        record["sensor_timestamp_ns"] = record["host_wall_timestamp_ns"]
        record["color_timestamp_domain"] = "global_time"
    write_jsonl(sensor_folder / FRAME_METADATA_JSONL, records)

    result = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="sensor",
        robot_timestamp_source="host_wall",
    )

    assert result.matched_frames == 2
    output_folder = Path(result.output_folder)
    matched = json.loads((output_folder / MATCH_ROBOT_EE_POSES).read_text())
    assert matched["000000.png"]["image_timestamp_ns"] == 10_000_000_000
    assert matched["000000.png"]["robot_timestamp_ns"] == 10_000_000_000
    assert matched["000000.png"]["robot_timestamp_source"] == "host_wall"
    derived = [
        json.loads(line)
        for line in (output_folder / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    assert derived[0]["sync_timestamp_source"] == "sensor"
    assert derived[0]["sync_robot_timestamp_source"] == "host_wall"
    report = json.loads((output_folder / SYNC_REPORT).read_text())
    assert report["timestamp_pair"] == {
        "frame_timestamp_source": "sensor",
        "requested_frame_timestamp_source": "sensor",
        "robot_timestamp_source": "host_wall",
    }


def test_profile_bound_sync_enforces_domain_fallback_and_provenance(
    tmp_path: Path,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    records = [
        json.loads(line)
        for line in (sensor_folder / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    for record in records:
        record["sensor_timestamp_ns"] = record["host_wall_timestamp_ns"]
        record["color_timestamp_domain"] = "global_time"
    write_jsonl(sensor_folder / FRAME_METADATA_JSONL, records)
    calibration_sync = {
        "schema_version": "calibration_sync_policy.v1",
        "source": "selected_calibration_profile",
        "selection_artifact": "calibration_profile_selection.json",
        "bundle_sha256": "a" * 64,
        "calibration_profiles": {
            "relative_path": "processed/calibration_inputs/a/calibration_profiles.json",
            "sha256": "b" * 64,
        },
        "sensor": {
            "sensor_key": "realsense_d435:123",
            "sensor_folder": "realsense_123",
            "profile_id": "profile-123",
            "sync_delta_ms": 0.0,
            "frame_timestamp_source": "sensor",
            "robot_timestamp_source": "host_wall",
            "required_frame_timestamp_domain": "global_time",
            "timestamp_fallback_allowed": False,
            "max_nearest_pose_delta_ms": 20.0,
        },
    }

    result = synchronize_sensor_folder(
        sensor_folder,
        run_root=run_root,
        sync_delta=0,
        timestamp_source="sensor",
        robot_timestamp_source="host_wall",
        max_nearest_pose_delta_ms=20.0,
        required_frame_timestamp_domain="global_time",
        timestamp_fallback_allowed=False,
        calibration_sync=calibration_sync,
    )

    report = json.loads(Path(result.report_path).read_text())
    assert report["required_frame_timestamp_domain"] == "global_time"
    assert report["timestamp_fallback_allowed"] is False
    assert report["timestamp_fallback_count"] == 0
    assert report["calibration_sync"] == calibration_sync

    records[0]["color_timestamp_domain"] = "hardware_clock"
    write_jsonl(sensor_folder / FRAME_METADATA_JSONL, records)
    with pytest.raises(ValueError, match="required 'global_time'"):
        synchronize_sensor_folder(
            sensor_folder,
            run_root=run_root,
            sync_delta=0,
            timestamp_source="sensor",
            robot_timestamp_source="host_wall",
            required_frame_timestamp_domain="global_time",
            timestamp_fallback_allowed=False,
            calibration_sync=calibration_sync,
        )

    records[0]["color_timestamp_domain"] = "global_time"
    records[0].pop("sensor_timestamp_ns")
    write_jsonl(sensor_folder / FRAME_METADATA_JSONL, records)
    with pytest.raises(ValueError, match="without fallback"):
        synchronize_sensor_folder(
            sensor_folder,
            run_root=run_root,
            sync_delta=0,
            timestamp_source="sensor",
            robot_timestamp_source="host_wall",
            required_frame_timestamp_domain="global_time",
            timestamp_fallback_allowed=False,
            calibration_sync=calibration_sync,
        )


def test_sensor_timestamp_requires_explicit_compatible_robot_clock() -> None:
    with pytest.raises(ValueError, match="requires an explicit"):
        resolve_timestamp_pair("sensor", None)
    with pytest.raises(ValueError, match="unsupported pair"):
        resolve_timestamp_pair("sensor", "host_received")


def test_sync_run_cli_processes_all_discovered_sensors(tmp_path: Path) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    second = run_root / "luxonis_abc"
    shutil.copytree(sensor_folder, second)
    records = [
        json.loads(line)
        for line in (second / FRAME_METADATA_JSONL).read_text().splitlines()
    ]
    for record in records:
        record["sensor_type"] = "oak_d_pro"
        record["sensor_id"] = "abc"
    write_jsonl(second / FRAME_METADATA_JSONL, records)
    write_run_config(
        run_root,
        create_run_config(
            run_root=run_root,
            capture_intent="dataset",
            bop_annotation_mode="none",
            sensors=(
                SensorRunConfig("realsense_d435", "123", "D435"),
                SensorRunConfig("oak_d_pro", "abc", "OAK-D Pro"),
            ),
        ),
    )
    new_run_id = json.loads((run_root / "run_config.json").read_text())["run_id"]
    raw = json.loads((run_root / RAW_ROBOT_EE_POSES).read_text())
    for record in raw.values():
        record["source_packet"]["run_id"] = new_run_id
    write_json(run_root / RAW_ROBOT_EE_POSES, raw)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "sync_run_non_destructive.py"),
            str(run_root),
            "--sync-delta",
            "0",
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )

    assert "Synchronized 2 sensor(s)" in result.stdout

    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stages = {stage["name"]: stage for stage in manifest["stages"]}

    assert stages["sync_run"]["status"] == "succeeded"
    assert stages["sync:realsense_123"]["status"] == "succeeded"
    assert stages["sync:luxonis_abc"]["status"] == "succeeded"
    assert (
        run_root / "processed" / "synchronized" / "luxonis_abc" / MATCH_ROBOT_EE_POSES
    ).exists()


def test_synchronize_run_defaults_to_enabled_run_config_sensors(
    tmp_path: Path,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    disabled_folder = run_root / "realsense_999"
    shutil.copytree(sensor_folder, disabled_folder)
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            run_id=json.loads((run_root / "run_config.json").read_text())["run_id"],
            sensors=(
                SensorRunConfig("realsense_d435", "123", "Enabled"),
                SensorRunConfig("realsense_d435", "999", "Disabled", enabled=False),
            ),
        ),
    )

    default_results = synchronize_run(run_root, sync_delta=0)
    explicit_results = synchronize_run(
        run_root,
        sensor_folders=[disabled_folder],
        output_root=run_root / "processed" / "explicit-disabled",
        sync_delta=0,
    )

    assert [Path(item.sensor_folder).name for item in default_results] == [
        "realsense_123"
    ]
    assert [Path(item.sensor_folder).name for item in explicit_results] == [
        "realsense_999"
    ]


def test_synchronize_run_accepts_only_an_explicit_subset_and_output_root(
    tmp_path: Path,
) -> None:
    run_root, sensor_folder = create_sync_fixture(tmp_path)
    shutil.copytree(sensor_folder, run_root / "luxonis_abc")
    output_root = run_root / "processed" / "calibration" / "attempt" / "sync"

    results = synchronize_run(
        run_root,
        sensor_folders=[sensor_folder.relative_to(run_root)],
        output_root=output_root,
        sync_delta=0,
    )

    assert [Path(item.sensor_folder).name for item in results] == ["realsense_123"]
    assert (output_root / "realsense_123" / MATCH_ROBOT_EE_POSES).is_file()
    assert not (output_root / "luxonis_abc").exists()
    with pytest.raises(ValueError, match="remain below the run root"):
        synchronize_run(run_root, sensor_folders=[tmp_path / "outside"])


def test_run_sync_cli_applies_selected_profile_policy_per_exact_camera(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "sync_run_non_destructive.py"
    )
    spec = importlib.util.spec_from_file_location(
        "test_sync_run_non_destructive_script",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    sync_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync_script)

    run_root = tmp_path / "selected-run"
    run_root.mkdir()
    write_run_config(
        run_root,
        create_run_config(
            capture_intent="dataset",
            bop_annotation_mode="none",
            run_root=run_root,
            sensors=(
                SensorRunConfig(
                    "realsense_d435",
                    "camera-A",
                    "Camera A",
                ),
                SensorRunConfig(
                    "realsense_d435",
                    "camera-B",
                    "Camera B",
                ),
            ),
        ),
    )
    sensors = [
        {
            "sensor_key": f"realsense_d435:{device_id}",
            "sensor_name": f"realsense_{device_id}",
            "sensor_folder": f"realsense_{device_id}",
            "sensor_type": "realsense_d435",
            "device_id": device_id,
            "profile_id": f"profile-{device_id}",
            "robot_pose_time_offset_ms": -delta,
            "sync_delta_ms": delta,
            "frame_timestamp_source": "sensor",
            "robot_timestamp_source": "host_wall",
            "required_frame_timestamp_domain": "global_time",
            "timestamp_fallback_allowed": False,
            "max_nearest_pose_delta_ms": threshold,
            "timing_source": f"processed/calibration/{device_id}/offset.json",
            "timing_policy": "auto_offset",
            "timing_status": "applied",
        }
        for device_id, delta, threshold in (
            ("camera-A", -70.0, 20.0),
            ("camera-B", -85.0, 12.5),
        )
    ]
    policy = {
        "schema_version": "calibration_sync_policy.v1",
        "source": "selected_calibration_profile",
        "selection_artifact": "calibration_profile_selection.json",
        "bundle_sha256": "a" * 64,
        "calibration_profiles": {
            "relative_path": "processed/calibration_inputs/a/calibration_profiles.json",
            "sha256": "b" * 64,
        },
        "sensors": sensors,
    }
    calls: list[dict] = []

    def fake_synchronize_run(root, **kwargs):
        calls.append({"root": Path(root), **kwargs})
        sensor_folder = Path(kwargs["sensor_folders"][0])
        return [
            SyncResult(
                sensor_folder=sensor_folder.as_posix(),
                output_folder=(
                    run_root / "processed" / "synchronized" / sensor_folder.name
                ).as_posix(),
                matched_poses_path=(
                    run_root / f"{sensor_folder.name}-matched.json"
                ).as_posix(),
                report_path=(run_root / f"{sensor_folder.name}-sync.json").as_posix(),
                total_frames=10,
                matched_frames=10,
                dropped_frames=0,
            )
        ]

    monkeypatch.setattr(
        sync_script,
        "parse_args",
        lambda: SimpleNamespace(
            run_root=run_root.as_posix(),
            output_root=None,
            sensor_folder=None,
            sync_delta=None,
            timestamp_source=None,
            robot_timestamp_source=None,
            no_copy=False,
        ),
    )
    monkeypatch.setattr(
        sync_script,
        "resolve_calibration_profile_sync_policy",
        lambda _run_root: policy,
    )
    monkeypatch.setattr(sync_script, "synchronize_run", fake_synchronize_run)

    sync_script.main()

    assert [call["sensor_folders"][0].name for call in calls] == [
        "realsense_camera-A",
        "realsense_camera-B",
    ]
    assert [call["sync_delta"] for call in calls] == [-70.0, -85.0]
    assert [call["max_nearest_pose_delta_ms"] for call in calls] == [20.0, 12.5]
    assert all(call["timestamp_source"] == "sensor" for call in calls)
    assert all(call["robot_timestamp_source"] == "host_wall" for call in calls)
    assert all(call["timestamp_fallback_allowed"] is False for call in calls)
    assert calls[0]["calibration_sync"]["sensor"] == sensors[0]
    assert calls[1]["calibration_sync"]["sensor"] == sensors[1]

    calls.clear()
    monkeypatch.setattr(
        sync_script,
        "parse_args",
        lambda: SimpleNamespace(
            run_root=run_root.as_posix(),
            output_root=None,
            sensor_folder=None,
            sync_delta="0",
            timestamp_source=None,
            robot_timestamp_source=None,
            no_copy=False,
        ),
    )
    with pytest.raises(ValueError, match="remove manual synchronization options"):
        sync_script.main()
    assert calls == []


def test_filename_timestamp_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="host_received, host_wall, or sensor"):
        resolve_frame_timestamp({"frame_id": "1000.png"}, "filename")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "invalid"])
def test_sync_delta_rejects_nonfinite_or_nonnumeric_values(value: object) -> None:
    with pytest.raises(ValueError, match="Synchronization delta"):
        resolve_sync_delta_ms("realsense_123", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, True, "invalid"])
def test_max_nearest_pose_delta_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="Maximum nearest-pose delta"):
        resolve_max_nearest_pose_delta_ms(value)  # type: ignore[arg-type]
