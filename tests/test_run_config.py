from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from posetestbot.config import DEFAULT_CAPTURE_VELOCITY_M_S
from posetestbot.io.artifacts import DATASET_MANIFEST, RUN_CONFIG
from posetestbot.pipeline.run_config import (
    CAPTURE_SYNCHRONIZATION_SCHEMA_VERSION,
    SCHEMA_VERSION,
    FixedFrameTransform,
    SensorRunConfig,
    create_run_config,
    load_run_config_for_run_root,
    sensor_config_from_mapping,
    sensor_config_from_token,
    sensor_configs_from_status,
    validate_run_config,
    write_run_config,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH


def _create(run_root: Path, **overrides):
    return create_run_config(
        run_root=run_root,
        capture_intent=overrides.pop("capture_intent", "dataset"),
        bop_annotation_mode=overrides.pop("bop_annotation_mode", "none"),
        **overrides,
    )


def test_v4_config_is_explicit_and_has_no_generic_pipeline(tmp_path: Path) -> None:
    data = _create(tmp_path / "run").to_dict()

    assert data["schema_version"] == SCHEMA_VERSION == "run_config.v4"
    assert str(uuid.UUID(data["run_id"])) == data["run_id"]
    assert data["capture"]["intent"] == "dataset"
    assert data["bop"] == {"annotation_mode": "none"}
    assert data["capture"]["velocity_m_s"] == DEFAULT_CAPTURE_VELOCITY_M_S
    assert "pipeline" not in data
    assert data["frames"]["robot_pose"] == {
        "from": "robot_flange",
        "to": "template_base",
        "convention": "kuka_abc_radians",
        "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
    }
    assert data["capture"]["synchronization"] == {
        "schema_version": CAPTURE_SYNCHRONIZATION_SCHEMA_VERSION,
        "mode": "timestamp_aligned",
    }
    assert [item["sensor_type"] for item in data["capture"]["sensors"]] == [
        "realsense_d435",
        "realsense_d435",
        "realsense_d435",
        "oak_d_pro",
        "zed_2i",
    ]


def test_v3_and_retired_pipeline_fields_fail_closed(tmp_path: Path) -> None:
    value = _create(tmp_path / "run").to_dict()
    value["schema_version"] = "run_config.v3"
    with pytest.raises(ValueError, match="run_config.v4"):
        validate_run_config(value)

    value["schema_version"] = SCHEMA_VERSION
    value["pipeline"] = {"sequence_id": "sync_aruco"}
    with pytest.raises(ValueError, match="retired fields: pipeline"):
        validate_run_config(value)


def test_config_requires_exact_lab_robot_and_reference_contract(tmp_path: Path) -> None:
    value = _create(tmp_path / "run").to_dict()
    value["robot_profile"]["robot_ip"] = "127.0.0.1"
    with pytest.raises(ValueError, match="sole lab iiwa profile"):
        validate_run_config(value)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("fps", "6", "capture.fps must be positive"),
        ("fps", True, "capture.fps must be positive"),
        ("resolution", 720, "capture.resolution must be a non-empty string"),
        (
            "velocity_m_s",
            "0.2",
            "capture.velocity_m_s must be a finite positive number",
        ),
        (
            "velocity_m_s",
            True,
            "capture.velocity_m_s must be a finite positive number",
        ),
    ],
)
def test_config_rejects_coerced_capture_scalars(
    tmp_path: Path, field: str, replacement: object, message: str
) -> None:
    value = _create(tmp_path / "run").to_dict()
    value["capture"][field] = replacement

    with pytest.raises(ValueError, match=message):
        validate_run_config(value)


def test_config_rejects_noncurrent_selection_pointer_shapes(tmp_path: Path) -> None:
    value = _create(tmp_path / "run").to_dict()
    value["calibration_profiles"] = "processed/calibration_profiles.json"
    value["intrinsic_calibration_profiles"] = (
        "processed/intrinsic_calibration_profiles.json"
    )
    value["calibration_profile_selection"] = {
        "selection_artifact": "calibration_profile_selection.json",
        "bundle_sha256": "a" * 64,
        "selected_at": "2026-08-18T10:00:00+00:00",
        "source_run": "retired",
    }
    with pytest.raises(ValueError, match="fields must be exactly"):
        validate_run_config(value)

    value = _create(tmp_path / "template", dataset_mode="pose_template").to_dict()
    value["pose_template"] = {
        "template_uuid": "11111111-1111-4111-8111-111111111111",
        "selection_artifact": "pose_template_selection.json",
        "bundle_sha256": "b" * 64,
        "placement_confirmed": "true",
    }
    with pytest.raises(ValueError, match="placement_confirmed must be a boolean"):
        validate_run_config(value)

    value = _create(tmp_path / "target").to_dict()
    value["calibration_target"] = {
        "target_id": "11111111-1111-4111-8111-111111111111",
        "bundle_path": "calibration_targets/11111111-1111-4111-8111-111111111111",
        "source_sha256": "a" * 64,
        "spec_sha256": "b" * 64,
        "pdf_sha256": "c" * 64,
        "configuration_sha256": "d" * 64,
        "geometry_sha256": "e" * 64,
        "placement": {"mode": "unknown"},
    }
    with pytest.raises(ValueError, match="mounting_frame must be"):
        validate_run_config(value)

    value = _create(tmp_path / "run-2").to_dict()
    value["frames"]["robot_pose"]["sunrise_reference_frame_path"] = (
        "/PoseTestBot/TemplateBase"
    )
    with pytest.raises(ValueError, match="canonical"):
        validate_run_config(value)


@pytest.mark.parametrize("velocity", [0.0, -0.1, float("nan")])
def test_config_rejects_nonpositive_or_nonfinite_velocity(
    tmp_path: Path, velocity: float
) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        _create(tmp_path / "run", velocity_m_s=velocity)


def test_calibration_intent_rejects_dataset_and_annotation_modes(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="dataset_mode=objectless"):
        _create(
            tmp_path / "calibration-template",
            capture_intent="calibration",
            dataset_mode="pose_template",
        )
    with pytest.raises(ValueError, match="annotation_mode=none"):
        _create(
            tmp_path / "calibration-gt",
            capture_intent="calibration",
            bop_annotation_mode="pose",
        )


def test_sensor_tokens_require_exact_registry_identifiers() -> None:
    sensor = sensor_config_from_token("oak_d_pro:mxid-1:static:Cell OAK-D Pro")
    assert sensor.sensor_type == "oak_d_pro"
    assert sensor.operator_alias == "Cell OAK-D Pro"

    with pytest.raises(ValueError, match="exact registry identifier"):
        sensor_config_from_token("luxonis:mxid-1:static:Cell OAK-D Pro")
    with pytest.raises(ValueError, match="exact registry identifier"):
        sensor_config_from_token("realsense:123:static:Cell D435")


def test_sensor_mapping_rejects_unknown_fields_and_truthy_booleans() -> None:
    base = {
        "sensor_type": "realsense_d435",
        "device_id": "123",
        "display_name": "D435",
        "mounting_mode": "static",
    }
    with pytest.raises(ValueError, match="unsupported fields: alias"):
        sensor_config_from_mapping({**base, "alias": "old"})
    with pytest.raises(ValueError, match="inverted must be a literal JSON boolean"):
        sensor_config_from_mapping({**base, "inverted": "true"})
    with pytest.raises(ValueError, match="enabled must be a literal JSON boolean"):
        sensor_config_from_mapping({**base, "enabled": 1})


def test_sensor_mapping_preserves_explicit_operator_alias_and_disabled_state() -> None:
    sensor = sensor_config_from_mapping(
        {
            "sensor_type": "realsense_d435",
            "device_id": "123",
            "display_name": "D435",
            "operator_alias": "  Wrist camera  ",
            "mounting_mode": "eye_in_hand",
            "enabled": False,
            "inverted": True,
            "metadata": {"model": "D435"},
        }
    )
    assert sensor.display_name == "Wrist camera"
    assert sensor.operator_alias == "Wrist camera"
    assert sensor.enabled is False
    assert sensor.inverted is True


def test_current_config_rejects_alias_normalization_on_read(tmp_path: Path) -> None:
    value = _create(
        tmp_path / "run",
        sensors=(sensor_config_from_token("realsense_d435:123:static:Wrist camera"),),
    ).to_dict()
    value["capture"]["sensors"][0]["operator_alias"] = " Wrist camera "
    with pytest.raises(ValueError, match="trimmed non-empty string"):
        validate_run_config(value)

    value["capture"]["sensors"][0]["operator_alias"] = "Wrist camera"
    value["capture"]["sensors"][0]["display_name"] = "D435"
    with pytest.raises(ValueError, match="must match operator_alias"):
        validate_run_config(value)


def test_sensor_status_uses_only_current_device_identity() -> None:
    sensors = sensor_configs_from_status(
        {
            "families": [
                {
                    "devices": [
                        {
                            "sensor_type": "realsense_d435",
                            "device_id": "123",
                            "display_name": "Intel RealSense 123",
                            "alias": "Wrist Camera",
                            "effective_display_name": "Wrist Camera",
                            "mounting_mode": "static",
                            "inverted": True,
                            "metadata": {"model": "D435"},
                        }
                    ]
                }
            ]
        }
    )
    assert len(sensors) == 1
    assert sensors[0].to_dict()["operator_alias"] == "Wrist Camera"
    assert sensors[0].metadata == {"model": "D435"}


def test_config_rejects_duplicate_sensor_identity(tmp_path: Path) -> None:
    sensor = SensorRunConfig("realsense_d435", "123", "D435", "static")
    with pytest.raises(ValueError, match="repeat identity"):
        _create(tmp_path / "run", sensors=(sensor, sensor))


def test_run_config_load_is_bound_to_its_current_root(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    write_run_config(run_root, _create(run_root))
    assert load_run_config_for_run_root(run_root)["run_root"] == run_root.as_posix()

    moved = tmp_path / "moved"
    run_root.rename(moved)
    with pytest.raises(ValueError, match="does not match requested run_root"):
        load_run_config_for_run_root(moved)


def test_fixed_frame_edges_remain_typed(tmp_path: Path) -> None:
    data = _create(
        tmp_path / "run",
        fixed_transforms=(
            FixedFrameTransform(
                from_frame="robot_flange",
                to_frame="tcp",
                rotation_quaternion_wxyz=(1.0, 0.0, 0.0, 0.0),
                translation_mm=(0.0, 0.0, 125.0),
                source="tool_measurement",
            ),
        ),
    ).to_dict()
    assert data["frames"]["fixed_transforms"] == [
        {
            "from": "robot_flange",
            "to": "tcp",
            "rotation_quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
            "translation_mm": [0.0, 0.0, 125.0],
            "source": "tool_measurement",
        }
    ]


def test_create_run_config_cli_requires_explicit_outcome(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    missing = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/create_run_config.py",
            (tmp_path / "missing").as_posix(),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert missing.returncode != 0
    assert "--intent" in missing.stderr
    assert "--annotation-mode" in missing.stderr


def test_create_run_config_cli_writes_v4_config_and_manifest(tmp_path: Path) -> None:
    run_root = tmp_path / "run-cli"
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/create_run_config.py",
            run_root.as_posix(),
            "--intent",
            "dataset",
            "--annotation-mode",
            "pose",
            "--sensor",
            "realsense_d435:123:static:Cell RealSense",
        ],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )
    assert f"Wrote {run_root / RUN_CONFIG}" in result.stdout
    config = json.loads((run_root / RUN_CONFIG).read_text())
    assert config["schema_version"] == "run_config.v4"
    assert config["capture"]["intent"] == "dataset"
    assert config["bop"] == {"annotation_mode": "pose"}
    assert "pipeline" not in config
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    stage = next(item for item in manifest["stages"] if item["name"] == "run_config")
    assert stage["status"] == "succeeded"
