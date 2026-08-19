"""Dataset/run manifest helpers for the PoseTestBot rewrite."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from posetestbot.config import RobotProfile
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
    DATASET_MANIFEST,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
)
from posetestbot.sensors.contracts import MountingMode, SensorType


SCHEMA_VERSION = "dataset_manifest.v1"


@dataclass(frozen=True)
class SensorManifestRecord:
    """A physical sensor participating in a run."""

    sensor_type: str
    device_id: str
    folder: str
    display_name: str
    mounting_mode: str | None = None
    status: str = "configured"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    operator_alias: str | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _relative_to(path: str | Path, root: str | Path) -> str:
    path_obj = Path(path)
    root_obj = Path(root)
    try:
        return path_obj.relative_to(root_obj).as_posix()
    except ValueError:
        return path_obj.as_posix()


def sensor_type_from_folder_name(folder_name: str) -> str | None:
    name = folder_name.lower()
    if name.startswith("realsense"):
        return SensorType.REALSENSE_D435.value
    if name.startswith("luxonis") or name.startswith("oak"):
        return SensorType.OAK_D_PRO.value
    if name.startswith("zed_2i") or name.startswith("zed"):
        return SensorType.ZED_2I.value
    return None


def make_sensor_record(
    *,
    sensor_type: str | SensorType,
    device_id: str,
    folder: str | Path,
    run_root: str | Path,
    display_name: str | None = None,
    mounting_mode: str | MountingMode | None = None,
    status: str = "configured",
    metadata: Mapping[str, Any] | None = None,
    operator_alias: str | None = None,
) -> dict[str, Any]:
    sensor_type_value = sensor_type.value if isinstance(sensor_type, SensorType) else sensor_type
    mounting_mode_value = (
        mounting_mode.value if isinstance(mounting_mode, MountingMode) else mounting_mode
    )
    normalized_operator_alias = (
        operator_alias.strip() if operator_alias is not None else None
    ) or None
    folder_path = Path(folder)
    record = SensorManifestRecord(
        sensor_type=sensor_type_value,
        device_id=device_id,
        folder=_relative_to(folder_path, run_root),
        display_name=(
            normalized_operator_alias
            or display_name
            or f"{sensor_type_value} {device_id}"
        ),
        mounting_mode=mounting_mode_value,
        status=status,
        metadata=metadata or {},
        operator_alias=normalized_operator_alias,
    )
    data = asdict(record)
    if record.operator_alias is None:
        data.pop("operator_alias")
    return data


def discover_sensor_records(run_root: str | Path) -> list[dict[str, Any]]:
    root = Path(run_root)
    records: list[dict[str, Any]] = []

    if not root.exists():
        return records

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        sensor_type = sensor_type_from_folder_name(child.name)
        if sensor_type is None:
            continue
        device_id = child.name.removeprefix("realsense_")
        device_id = device_id.removeprefix("luxonis_")
        device_id = device_id.removeprefix("zed_2i_")
        records.append(
            make_sensor_record(
                sensor_type=sensor_type,
                device_id=device_id or child.name,
                folder=child,
                run_root=root,
                display_name=child.name,
                status="discovered",
                metadata={
                    "has_rgb_dir": (child / RGB_DIR).is_dir(),
                    "has_depth_dir": (child / DEPTH_DIR).is_dir(),
                    "has_frame_metadata": (child / FRAME_METADATA_JSONL).is_file(),
                },
            )
        )
    return records


def create_run_manifest(
    run_root: str | Path,
    *,
    run_name: str | None = None,
    robot_profile: RobotProfile | Mapping[str, Any] | None = None,
    capture_config: Mapping[str, Any] | None = None,
    sensors: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(run_root)
    timestamp = utc_now_iso()
    robot_profile_data = _jsonable(robot_profile) if robot_profile is not None else None

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_name or root.name,
        "run_name": run_name or root.name,
        "root_path": root.as_posix(),
        "created_at": timestamp,
        "updated_at": timestamp,
        "robot_profile": robot_profile_data,
        "capture_config": dict(capture_config or {}),
        "sensors": [dict(sensor) for sensor in sensors or []],
        "artifacts": {},
        "stages": [],
    }


def manifest_path(run_root: str | Path) -> Path:
    return Path(run_root) / DATASET_MANIFEST


def load_run_manifest(run_root: str | Path) -> dict[str, Any]:
    path = manifest_path(run_root)
    with open(path, "r") as f:
        manifest = json.load(f)

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported manifest schema: {manifest.get('schema_version')!r}"
        )
    return manifest


def load_or_create_run_manifest(
    run_root: str | Path,
    *,
    run_name: str | None = None,
    robot_profile: RobotProfile | Mapping[str, Any] | None = None,
    capture_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = manifest_path(run_root)
    if path.exists():
        return load_run_manifest(run_root)
    return create_run_manifest(
        run_root,
        run_name=run_name,
        robot_profile=robot_profile,
        capture_config=capture_config,
        sensors=discover_sensor_records(run_root),
    )


def write_run_manifest(manifest: Mapping[str, Any], run_root: str | Path) -> Path:
    path = manifest_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_data = _jsonable(dict(manifest))
    manifest_data["updated_at"] = utc_now_iso()

    return atomic_write_json(path, manifest_data)


def set_manifest_sensors(
    manifest: dict[str, Any], sensors: list[Mapping[str, Any]]
) -> dict[str, Any]:
    manifest["sensors"] = [dict(sensor) for sensor in sensors]
    return manifest


def set_manifest_artifact(
    manifest: dict[str, Any],
    artifact_name: str,
    artifact_path: str | Path,
    *,
    run_root: str | Path,
) -> dict[str, Any]:
    artifacts = dict(manifest.get("artifacts", {}))
    artifacts[artifact_name] = _relative_to(artifact_path, run_root)
    manifest["artifacts"] = artifacts
    return manifest


def upsert_stage(
    manifest: dict[str, Any],
    *,
    name: str,
    status: str,
    artifacts: Mapping[str, str | Path] | None = None,
    run_root: str | Path | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    stages = list(manifest.get("stages", []))
    existing = next((stage for stage in stages if stage.get("name") == name), None)

    if existing is None:
        existing = {"name": name, "started_at": utc_now_iso()}
        stages.append(existing)

    existing["status"] = status
    existing["updated_at"] = utc_now_iso()
    if status in {"succeeded", "failed", "canceled"}:
        existing["ended_at"] = existing["updated_at"]
    else:
        existing.pop("ended_at", None)
    if message:
        existing["message"] = message
    else:
        existing.pop("message", None)
    if artifacts:
        if run_root is None:
            existing["artifacts"] = {key: str(value) for key, value in artifacts.items()}
        else:
            existing["artifacts"] = {
                key: _relative_to(value, run_root) for key, value in artifacts.items()
            }

    manifest["stages"] = stages
    return manifest


def record_raw_robot_pose_artifact(
    manifest: dict[str, Any], run_root: str | Path
) -> dict[str, Any]:
    artifact_path = Path(run_root) / RAW_ROBOT_EE_POSES
    set_manifest_artifact(
        manifest,
        RAW_ROBOT_EE_POSES,
        artifact_path,
        run_root=run_root,
    )
    return upsert_stage(
        manifest,
        name="robot_pose_capture",
        status="succeeded",
        artifacts={RAW_ROBOT_EE_POSES: artifact_path},
        run_root=run_root,
    )
