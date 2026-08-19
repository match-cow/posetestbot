"""Current-only run configuration for PoseTestBot acquisition workflows."""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import fcntl

from posetestbot.io.atomic import atomic_write_json
from posetestbot.config import (
    DEFAULT_CAPTURE_VELOCITY_M_S,
    RobotProfile,
    robot_profile,
)
from posetestbot.io.artifacts import CALIBRATION_PROFILE_SELECTION, RUN_CONFIG
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
)
from posetestbot.sensors.contracts import MountingMode, SensorType


SCHEMA_VERSION = "run_config.v4"
SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION}
CAPTURE_SYNCHRONIZATION_SCHEMA_VERSION = "capture_synchronization.v1"
CAPTURE_SYNCHRONIZATION_MODES = {"timestamp_aligned"}
CAPTURE_INTENTS = {"calibration", "dataset"}
BOP_ANNOTATION_MODES = {"none", "pose", "pose_and_masks"}
DATASET_MODES = {"objectless", "pose_template"}
RUN_CONFIG_LOCK = ".run_config.lock"

_RUN_CONFIG_LOCK = threading.RLock()
_RUN_CONFIG_LOCK_STATE = threading.local()

LAB_REALSENSE_SERIALS = (
    "825412070181",
    "033422071805",
    "923322072633",
)


@contextmanager
def run_config_lock(run_root: str | Path):
    """Serialize run-config transactions across threads and processes.

    Pose-template selection performs a read-modify-write of ``run_config.json``.
    Keeping that transaction on the same lock as ordinary config replacement
    prevents either writer from observing or promoting an intermediate version.
    """

    with _RUN_CONFIG_LOCK:
        root = Path(run_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / RUN_CONFIG_LOCK
        held = getattr(_RUN_CONFIG_LOCK_STATE, "locks", None)
        if held is None:
            held = {}
            _RUN_CONFIG_LOCK_STATE.locks = held
        depth = int(held.get(lock_path, 0))
        if depth:
            held[lock_path] = depth + 1
            try:
                yield root
            finally:
                held[lock_path] -= 1
            return
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "a+b", closefd=False) as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                held[lock_path] = 1
                try:
                    yield root
                finally:
                    held.pop(lock_path, None)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class SensorRunConfig:
    """One intended sensor participant in a run."""

    sensor_type: str
    device_id: str
    display_name: str
    mounting_mode: str = MountingMode.EYE_IN_HAND.value
    enabled: bool = True
    calibration_profile_id: str | None = None
    inverted: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    operator_alias: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        operator_alias = (
            self.operator_alias.strip() if self.operator_alias is not None else None
        )
        if not operator_alias:
            data.pop("operator_alias")
        else:
            data["operator_alias"] = operator_alias
            data["display_name"] = operator_alias
        return data


@dataclass(frozen=True)
class CaptureSynchronizationConfig:
    """Cross-camera acquisition timing requested for one run."""

    schema_version: str = CAPTURE_SYNCHRONIZATION_SCHEMA_VERSION
    mode: str = "timestamp_aligned"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class CaptureRunConfig:
    """Capture defaults shared by hardware adapters."""

    intent: str
    resolution: str = "720p"
    fps: int = 6
    velocity_m_s: float = DEFAULT_CAPTURE_VELOCITY_M_S
    sensors: tuple[SensorRunConfig, ...] = ()
    synchronization: CaptureSynchronizationConfig = field(
        default_factory=CaptureSynchronizationConfig
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sensors"] = [sensor.to_dict() for sensor in self.sensors]
        data["synchronization"] = self.synchronization.to_dict()
        return data


@dataclass(frozen=True)
class BopRunConfig:
    """Explicit BOP annotation capability requested for a dataset run."""

    annotation_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {"annotation_mode": self.annotation_mode}


@dataclass(frozen=True)
class FixedFrameTransform:
    """One operator-supplied fixed edge in the run frame graph."""

    from_frame: str
    to_frame: str
    rotation_quaternion_wxyz: tuple[float, float, float, float]
    translation_mm: tuple[float, float, float]
    source: str = "operator_configured"

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_frame,
            "to": self.to_frame,
            "rotation_quaternion_wxyz": list(self.rotation_quaternion_wxyz),
            "translation_mm": list(self.translation_mm),
            "source": self.source,
        }


@dataclass(frozen=True)
class RunFramesConfig:
    robot_pose: Mapping[str, str] = field(
        default_factory=lambda: {
            "from": "robot_flange",
            "to": "template_base",
            "convention": "kuka_abc_radians",
        }
    )
    dataset_reference_frame: str = "template_base"
    fixed_transforms: tuple[FixedFrameTransform, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "robot_pose": dict(self.robot_pose),
            "dataset_reference_frame": self.dataset_reference_frame,
            "fixed_transforms": [item.to_dict() for item in self.fixed_transforms],
        }


@dataclass(frozen=True)
class PoseTestBotRunConfig:
    """Top-level versioned run configuration artifact."""

    schema_version: str
    run_id: str
    run_name: str
    run_root: str
    robot_profile: RobotProfile
    capture: CaptureRunConfig
    frames: RunFramesConfig = field(default_factory=RunFramesConfig)
    dataset_mode: str = "objectless"
    pose_template: Mapping[str, Any] | None = None
    calibration_profiles: str | None = None
    intrinsic_calibration_profiles: str | None = None
    calibration_profile_selection: Mapping[str, Any] | None = None
    calibration_target: Mapping[str, Any] | None = None
    bop: BopRunConfig = field(default_factory=lambda: BopRunConfig("none"))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "run_name": self.run_name,
            "run_root": self.run_root,
            "robot_profile": asdict(self.robot_profile),
            "capture": self.capture.to_dict(),
            "frames": self.frames.to_dict(),
            "calibration_profiles": self.calibration_profiles,
            "calibration_target": (
                dict(self.calibration_target)
                if self.calibration_target is not None
                else None
            ),
            "bop": self.bop.to_dict(),
        }
        if self.intrinsic_calibration_profiles is not None:
            result["intrinsic_calibration_profiles"] = (
                self.intrinsic_calibration_profiles
            )
        if self.calibration_profile_selection is not None:
            result["calibration_profile_selection"] = dict(
                self.calibration_profile_selection
            )
        result["dataset_mode"] = self.dataset_mode
        result["pose_template"] = (
            dict(self.pose_template) if self.pose_template is not None else None
        )
        return result


def normalize_sensor_type(value: str) -> SensorType:
    try:
        return SensorType(value.strip())
    except ValueError as exc:
        choices = ", ".join(sensor.value for sensor in SensorType)
        raise ValueError(
            f"Unknown sensor type {value!r}; use an exact registry identifier: {choices}"
        ) from exc


def normalize_mounting_mode(value: str) -> MountingMode:
    key = value.strip().lower().replace("-", "_")
    try:
        return MountingMode(key)
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in MountingMode)
        raise ValueError(
            f"Unknown mounting mode {value!r}; use one of: {choices}"
        ) from exc


def normalize_inverted(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    key = str(value).strip().lower().replace("-", "_")
    if key in {"1", "true", "yes", "y", "inverted", "upside_down"}:
        return True
    if key in {"0", "false", "no", "n", "normal", "upright", ""}:
        return False
    raise ValueError(
        "Unknown sensor orientation "
        f"{value!r}; use inverted, normal, true, false, 1, or 0"
    )


def normalize_sensor_enabled(value: Any) -> bool:
    """Return a sensor participation flag without truthy-string coercion."""

    if isinstance(value, bool):
        return value
    raise ValueError("Sensor enabled must be a literal JSON boolean")


def normalize_operator_alias(value: Any) -> str | None:
    """Return a trimmed, optional operator-facing camera alias."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Sensor operator_alias must be a string or null")
    return value.strip() or None


def capture_synchronization_from_mapping(
    value: Mapping[str, Any] | CaptureSynchronizationConfig | None,
) -> CaptureSynchronizationConfig:
    """Normalize one strict camera synchronization policy."""

    if value is None:
        return CaptureSynchronizationConfig()
    if isinstance(value, CaptureSynchronizationConfig):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise ValueError("capture.synchronization must be a JSON object")
    schema_version = str(value.get("schema_version", ""))
    if schema_version != CAPTURE_SYNCHRONIZATION_SCHEMA_VERSION:
        raise ValueError(
            "capture.synchronization.schema_version must be "
            f"{CAPTURE_SYNCHRONIZATION_SCHEMA_VERSION}"
        )
    base_keys = {"schema_version", "mode"}
    unexpected = sorted(set(value) - base_keys)
    if unexpected:
        raise ValueError(
            "timestamp_aligned capture synchronization does not accept: "
            + ", ".join(unexpected)
        )
    if value.get("mode") != "timestamp_aligned":
        raise ValueError(
            "capture.synchronization.mode must be exactly timestamp_aligned"
        )
    return CaptureSynchronizationConfig()


def validate_capture_synchronization(
    synchronization: Mapping[str, Any] | CaptureSynchronizationConfig | None,
    sensors: list[Mapping[str, Any]] | tuple[SensorRunConfig, ...],
) -> CaptureSynchronizationConfig:
    """Validate synchronization against the exact enabled camera set."""

    del sensors
    return capture_synchronization_from_mapping(synchronization)


def _validate_sensor_orientation(sensor_type: SensorType | str, inverted: bool) -> None:
    normalized = (
        sensor_type if isinstance(sensor_type, SensorType) else SensorType(sensor_type)
    )
    if inverted and normalized != SensorType.REALSENSE_D435:
        raise ValueError("Sensor inverted=true is only supported for RealSense D435")


def _required_mounting_mode(
    value: Any,
    *,
    default_mounting_mode: str | None,
) -> str:
    """Resolve an explicitly authored camera mount without guessing one."""

    candidate = value
    if candidate is None or not str(candidate).strip():
        candidate = default_mounting_mode
    if candidate is None or not str(candidate).strip():
        raise ValueError(
            "Sensor mounting_mode is required; set it per sensor or provide "
            "an explicit default mounting mode"
        )
    return normalize_mounting_mode(str(candidate)).value


def sensor_config_from_token(
    token: str,
    *,
    default_mounting_mode: str | None = None,
) -> SensorRunConfig:
    """Parse ``sensor_type:device_id[:mounting_mode[:display_name[:orientation]]]``."""

    parts = token.split(":")
    if len(parts) < 2 or len(parts) > 5:
        raise ValueError(
            "Sensor entries must look like "
            "sensor_type:device_id[:mounting_mode[:display_name[:orientation]]]"
        )
    sensor_type = normalize_sensor_type(parts[0])
    device_id = parts[1].strip()
    if not device_id:
        raise ValueError("Sensor device_id must not be empty")
    mounting_mode = _required_mounting_mode(
        parts[2] if len(parts) >= 3 else None,
        default_mounting_mode=default_mounting_mode,
    )
    operator_alias = parts[3].strip() if len(parts) >= 4 and parts[3].strip() else None
    display_name = operator_alias or f"{sensor_type.value}:{device_id}"
    inverted = normalize_inverted(parts[4]) if len(parts) == 5 else False
    _validate_sensor_orientation(sensor_type, inverted)
    return SensorRunConfig(
        sensor_type=sensor_type.value,
        device_id=device_id,
        display_name=display_name,
        mounting_mode=mounting_mode,
        inverted=inverted,
        operator_alias=operator_alias,
    )


def sensor_config_from_mapping(
    value: Mapping[str, Any],
    *,
    default_mounting_mode: str | None = None,
) -> SensorRunConfig:
    allowed_fields = {
        "sensor_type",
        "device_id",
        "display_name",
        "mounting_mode",
        "enabled",
        "calibration_profile_id",
        "inverted",
        "metadata",
        "operator_alias",
    }
    unexpected = sorted(set(value) - allowed_fields)
    if unexpected:
        raise ValueError(
            "Sensor entry contains unsupported fields: " + ", ".join(unexpected)
        )
    sensor_type = normalize_sensor_type(str(value.get("sensor_type", "")))
    device_id = str(value.get("device_id", "")).strip()
    if not device_id:
        raise ValueError("Sensor device_id must not be empty")
    mounting_mode = _required_mounting_mode(
        value.get("mounting_mode"),
        default_mounting_mode=default_mounting_mode,
    )
    operator_alias = normalize_operator_alias(value.get("operator_alias"))
    display_name = str(
        operator_alias
        or value.get("display_name")
        or f"{sensor_type.value}:{device_id}"
    ).strip()
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("Sensor metadata must be a JSON object")
    calibration_profile_id = value.get("calibration_profile_id")
    if calibration_profile_id is not None:
        calibration_profile_id = str(calibration_profile_id)
    inverted = value.get("inverted", False)
    if not isinstance(inverted, bool):
        raise ValueError("Sensor inverted must be a literal JSON boolean")
    _validate_sensor_orientation(sensor_type, inverted)
    return SensorRunConfig(
        sensor_type=sensor_type.value,
        device_id=device_id,
        display_name=display_name,
        mounting_mode=mounting_mode,
        enabled=normalize_sensor_enabled(value.get("enabled", True)),
        calibration_profile_id=calibration_profile_id,
        inverted=inverted,
        metadata=dict(metadata),
        operator_alias=operator_alias,
    )


def sensor_configs_from_values(
    values: list[Any] | None,
    *,
    default_mounting_mode: str | None = None,
) -> tuple[SensorRunConfig, ...]:
    normalized_default = (
        normalize_mounting_mode(default_mounting_mode).value
        if default_mounting_mode is not None
        else None
    )
    if values is None:
        mode = _required_mounting_mode(
            None,
            default_mounting_mode=normalized_default,
        )
        return default_lab_sensors(mounting_mode=mode)
    if not values:
        raise ValueError("At least one sensor entry is required")
    sensors = []
    for value in values:
        if isinstance(value, str):
            sensors.append(
                sensor_config_from_token(
                    value,
                    default_mounting_mode=normalized_default,
                )
            )
        elif isinstance(value, Mapping):
            sensors.append(
                sensor_config_from_mapping(
                    value,
                    default_mounting_mode=normalized_default,
                )
            )
        else:
            raise ValueError("Sensor entries must be strings or JSON objects")
    return tuple(sensors)


def sensor_configs_from_status(
    sensor_status: Mapping[str, Any],
    *,
    default_mounting_mode: str | None = None,
) -> tuple[SensorRunConfig, ...]:
    """Build run-config sensor entries from discovered status devices."""

    sensors: list[SensorRunConfig] = []
    mode = (
        normalize_mounting_mode(default_mounting_mode).value
        if default_mounting_mode is not None
        else None
    )
    for family in sensor_status.get("families", []):
        if not isinstance(family, Mapping):
            continue
        for device in family.get("devices", []):
            if not isinstance(device, Mapping) or not device.get("connected", True):
                continue
            sensor_type = normalize_sensor_type(str(device.get("sensor_type", "")))
            device_id = str(device.get("device_id", "")).strip()
            if not device_id:
                continue
            operator_alias = normalize_operator_alias(device.get("alias"))
            effective_display_name = str(
                device.get("effective_display_name") or ""
            ).strip()
            discovered_display_name = str(device.get("display_name") or "").strip()
            if (
                operator_alias is None
                and effective_display_name
                and effective_display_name != discovered_display_name
            ):
                operator_alias = effective_display_name
            display_name = str(
                operator_alias
                or effective_display_name
                or discovered_display_name
                or f"{sensor_type.value}:{device_id}"
            )
            mounting_mode = _required_mounting_mode(
                device.get("mounting_mode"),
                default_mounting_mode=mode,
            )
            inverted = normalize_inverted(device.get("inverted", False))
            _validate_sensor_orientation(sensor_type, inverted)
            metadata = device.get("metadata", {})
            if not isinstance(metadata, Mapping):
                metadata = {}
            sensors.append(
                SensorRunConfig(
                    sensor_type=sensor_type.value,
                    device_id=device_id,
                    display_name=display_name,
                    mounting_mode=mounting_mode,
                    inverted=inverted,
                    metadata=dict(metadata),
                    operator_alias=operator_alias,
                )
            )
    return tuple(sensors)


def default_lab_sensors(
    *,
    mounting_mode: str = MountingMode.EYE_IN_HAND.value,
) -> tuple[SensorRunConfig, ...]:
    mode = normalize_mounting_mode(mounting_mode).value
    sensors = [
        SensorRunConfig(
            sensor_type=SensorType.REALSENSE_D435.value,
            device_id=serial,
            display_name=f"RealSense D435 {serial}",
            mounting_mode=mode,
            metadata={"lab_profile": "current_posetestbot"},
        )
        for serial in LAB_REALSENSE_SERIALS
    ]
    sensors.extend(
        [
            SensorRunConfig(
                sensor_type=SensorType.OAK_D_PRO.value,
                device_id="auto",
                display_name="Luxonis OAK-D Pro",
                mounting_mode=mode,
                metadata={"lab_profile": "current_posetestbot"},
            ),
            SensorRunConfig(
                sensor_type=SensorType.ZED_2I.value,
                device_id="auto",
                display_name="Stereolabs ZED 2i",
                mounting_mode=mode,
                metadata={"lab_profile": "current_posetestbot"},
            ),
        ]
    )
    return tuple(sensors)


def create_run_config(
    *,
    run_root: str | Path,
    capture_intent: str,
    bop_annotation_mode: str,
    run_id: str | None = None,
    run_name: str | None = None,
    resolution: str = "720p",
    fps: int = 6,
    velocity_m_s: float = DEFAULT_CAPTURE_VELOCITY_M_S,
    sensors: tuple[SensorRunConfig, ...] | None = None,
    dataset_mode: str | None = None,
    pose_template: Mapping[str, Any] | None = None,
    calibration_profiles: str | None = None,
    intrinsic_calibration_profiles: str | None = None,
    calibration_profile_selection: Mapping[str, Any] | None = None,
    calibration_target: Mapping[str, Any] | None = None,
    fixed_transforms: tuple[FixedFrameTransform, ...] = (),
    synchronization: (Mapping[str, Any] | CaptureSynchronizationConfig | None) = None,
) -> PoseTestBotRunConfig:
    run_root_path = Path(run_root)
    if capture_intent not in CAPTURE_INTENTS:
        raise ValueError(
            "capture_intent must be one of: " + ", ".join(sorted(CAPTURE_INTENTS))
        )
    if bop_annotation_mode not in BOP_ANNOTATION_MODES:
        raise ValueError(
            "bop_annotation_mode must be one of: "
            + ", ".join(sorted(BOP_ANNOTATION_MODES))
        )
    sensor_configs = sensors if sensors is not None else default_lab_sensors()
    inferred_mode = dataset_mode or "objectless"
    if inferred_mode not in DATASET_MODES:
        raise ValueError(
            "dataset_mode must be one of: " + ", ".join(sorted(DATASET_MODES))
        )
    synchronization_config = validate_capture_synchronization(
        synchronization,
        sensor_configs,
    )
    robot_pose = {
        "from": "robot_flange",
        "to": "template_base",
        "convention": "kuka_abc_radians",
    }
    robot_pose["sunrise_reference_frame_path"] = POSE_TEMPLATE_BASE_SUNRISE_PATH
    config = PoseTestBotRunConfig(
        schema_version=SCHEMA_VERSION,
        run_id=str(uuid.UUID(run_id)) if run_id is not None else str(uuid.uuid4()),
        run_name=run_name or run_root_path.name,
        run_root=run_root_path.as_posix(),
        robot_profile=robot_profile(),
        capture=CaptureRunConfig(
            intent=capture_intent,
            resolution=resolution,
            fps=fps,
            velocity_m_s=velocity_m_s,
            sensors=tuple(sensor_configs),
            synchronization=synchronization_config,
        ),
        frames=RunFramesConfig(
            robot_pose=robot_pose,
            fixed_transforms=fixed_transforms,
        ),
        dataset_mode=inferred_mode,
        pose_template=dict(pose_template) if pose_template is not None else None,
        calibration_profiles=calibration_profiles,
        intrinsic_calibration_profiles=intrinsic_calibration_profiles,
        calibration_profile_selection=(
            dict(calibration_profile_selection)
            if calibration_profile_selection is not None
            else None
        ),
        calibration_target=(
            dict(calibration_target) if calibration_target is not None else None
        ),
        bop=BopRunConfig(annotation_mode=bop_annotation_mode),
    )
    validate_run_config(config.to_dict())
    return config


def fixed_transform_from_mapping(value: Mapping[str, Any]) -> FixedFrameTransform:
    quaternion = value.get("rotation_quaternion_wxyz")
    translation = value.get("translation_mm")
    if not isinstance(quaternion, (list, tuple)) or len(quaternion) != 4:
        raise ValueError("Fixed transform rotation_quaternion_wxyz must have 4 values")
    if not isinstance(translation, (list, tuple)) or len(translation) != 3:
        raise ValueError("Fixed transform translation_mm must have 3 values")
    return FixedFrameTransform(
        from_frame=str(value.get("from", "")),
        to_frame=str(value.get("to", "")),
        rotation_quaternion_wxyz=tuple(float(item) for item in quaternion),
        translation_mm=tuple(float(item) for item in translation),
        source=str(value.get("source") or "operator_configured"),
    )


def validate_run_config(value: Mapping[str, Any]) -> None:
    schema = value.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise ValueError(f"Run config schema_version must be {SCHEMA_VERSION}")
    retired_fields = sorted(
        {"object_folder", "selected_objects", "pipeline"} & value.keys()
    )
    if retired_fields:
        raise ValueError(
            "Run config contains retired fields: " + ", ".join(retired_fields)
        )
    allowed_top_level = {
        "schema_version",
        "run_id",
        "run_name",
        "run_root",
        "robot_profile",
        "capture",
        "frames",
        "dataset_mode",
        "pose_template",
        "calibration_profiles",
        "intrinsic_calibration_profiles",
        "calibration_profile_selection",
        "calibration_target",
        "bop",
    }
    unexpected = sorted(set(value) - allowed_top_level)
    if unexpected:
        raise ValueError(
            "Run config contains unsupported fields: " + ", ".join(unexpected)
        )
    required_top_level = {
        "schema_version",
        "run_id",
        "run_name",
        "run_root",
        "robot_profile",
        "capture",
        "frames",
        "dataset_mode",
        "pose_template",
        "calibration_profiles",
        "calibration_target",
        "bop",
    }
    missing_top_level = sorted(required_top_level - value.keys())
    if missing_top_level:
        raise ValueError(
            "Run config is missing required fields: " + ", ".join(missing_top_level)
        )
    if not isinstance(value.get("run_name"), str) or not value["run_name"].strip():
        raise ValueError("Run config run_name must be a non-empty string")
    if not isinstance(value.get("run_root"), str) or not value["run_root"].strip():
        raise ValueError("Run config run_root must be a non-empty path")
    try:
        normalized_run_id = str(uuid.UUID(str(value.get("run_id"))))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Run config run_id must be a canonical UUID") from exc
    if value.get("run_id") != normalized_run_id:
        raise ValueError("Run config run_id must be a canonical UUID")
    dataset_mode = value.get("dataset_mode")
    if dataset_mode not in DATASET_MODES:
        raise ValueError(
            "Run config dataset_mode must be one of: "
            + ", ".join(sorted(DATASET_MODES))
        )
    pose_template = value.get("pose_template")
    if pose_template is not None:
        if not isinstance(pose_template, Mapping):
            raise ValueError("Run config pose_template must be an object or null")
        expected_pose_template_fields = {
            "template_uuid",
            "selection_artifact",
            "bundle_sha256",
            "placement_confirmed",
        }
        if set(pose_template) != expected_pose_template_fields:
            raise ValueError(
                "Run config pose_template fields must be exactly: "
                + ", ".join(sorted(expected_pose_template_fields))
            )
        try:
            template_uuid = str(uuid.UUID(str(pose_template.get("template_uuid"))))
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                "Run config pose_template.template_uuid must be a canonical UUID"
            ) from exc
        if pose_template.get("template_uuid") != template_uuid:
            raise ValueError(
                "Run config pose_template.template_uuid must be a canonical UUID"
            )
        if pose_template.get("selection_artifact") != "pose_template_selection.json":
            raise ValueError(
                "Run config pose_template.selection_artifact must be "
                "pose_template_selection.json"
            )
        template_digest = pose_template.get("bundle_sha256")
        if (
            not isinstance(template_digest, str)
            or len(template_digest) != 64
            or any(character not in "0123456789abcdef" for character in template_digest)
        ):
            raise ValueError(
                "Run config pose_template.bundle_sha256 must be a SHA-256 digest"
            )
        if not isinstance(pose_template.get("placement_confirmed"), bool):
            raise ValueError(
                "Run config pose_template.placement_confirmed must be a boolean"
            )

    robot = value.get("robot_profile")
    if not isinstance(robot, Mapping):
        raise ValueError("Run config robot_profile must be an object")
    if robot.get("mode") != "real":
        raise ValueError("Run config robot_profile.mode must be 'real'")
    expected_robot = asdict(robot_profile())
    if dict(robot) != expected_robot:
        raise ValueError(
            "Run config robot_profile must match the sole lab iiwa profile"
        )

    capture = value.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("Run config capture must be an object")
    expected_capture_fields = {
        "intent",
        "resolution",
        "fps",
        "velocity_m_s",
        "sensors",
        "synchronization",
    }
    if set(capture) != expected_capture_fields:
        raise ValueError(
            "Run config capture fields must be exactly: "
            + ", ".join(sorted(expected_capture_fields))
        )
    if capture.get("intent") not in CAPTURE_INTENTS:
        raise ValueError(
            "Run config capture.intent must be one of: "
            + ", ".join(sorted(CAPTURE_INTENTS))
        )
    fps = capture.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise ValueError("Run config capture.fps must be positive")
    if (
        not isinstance(capture.get("resolution"), str)
        or not capture["resolution"].strip()
    ):
        raise ValueError("Run config capture.resolution must be a non-empty string")
    velocity_m_s = capture.get("velocity_m_s")
    if (
        isinstance(velocity_m_s, bool)
        or not isinstance(velocity_m_s, (int, float))
        or not math.isfinite(velocity_m_s)
        or velocity_m_s <= 0.0
    ):
        raise ValueError(
            "Run config capture.velocity_m_s must be a finite positive number"
        )
    calibration_target = value.get("calibration_target")
    calibration_profiles = value.get("calibration_profiles")
    if calibration_profiles is not None and (
        not isinstance(calibration_profiles, str) or not calibration_profiles.strip()
    ):
        raise ValueError(
            "Run config calibration_profiles must be a non-empty path or null"
        )
    intrinsic_profiles = value.get("intrinsic_calibration_profiles")
    if intrinsic_profiles is not None and (
        not isinstance(intrinsic_profiles, str) or not intrinsic_profiles.strip()
    ):
        raise ValueError(
            "Run config intrinsic_calibration_profiles must be a non-empty path or null"
        )
    calibration_selection = value.get("calibration_profile_selection")
    if calibration_selection is not None:
        if not isinstance(calibration_selection, Mapping):
            raise ValueError(
                "Run config calibration_profile_selection must be an object or null"
            )
        expected_selection_fields = {
            "selection_artifact",
            "bundle_sha256",
            "selected_at",
        }
        if set(calibration_selection) != expected_selection_fields:
            raise ValueError(
                "Run config calibration_profile_selection fields must be exactly: "
                + ", ".join(sorted(expected_selection_fields))
            )
        if (
            calibration_selection.get("selection_artifact")
            != CALIBRATION_PROFILE_SELECTION
        ):
            raise ValueError(
                "Run config calibration_profile_selection.selection_artifact must be "
                f"{CALIBRATION_PROFILE_SELECTION}"
            )
        digest = calibration_selection.get("bundle_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                "Run config calibration_profile_selection.bundle_sha256 must be a SHA-256 digest"
            )
        if (
            not isinstance(calibration_selection.get("selected_at"), str)
            or not calibration_selection["selected_at"].strip()
        ):
            raise ValueError(
                "Run config calibration_profile_selection.selected_at must be a non-empty string"
            )
        if not isinstance(value.get("calibration_profiles"), str) or not isinstance(
            intrinsic_profiles, str
        ):
            raise ValueError(
                "Run config calibration selection requires both calibration profile paths"
            )
    if calibration_target is not None:
        if not isinstance(calibration_target, Mapping):
            raise ValueError("Run config calibration_target must be an object or null")
        required_target_fields = {
            "target_id",
            "bundle_path",
            "source_sha256",
            "spec_sha256",
            "pdf_sha256",
            "configuration_sha256",
            "geometry_sha256",
            "placement",
        }
        unexpected_target_fields = sorted(
            calibration_target.keys() - required_target_fields
        )
        if unexpected_target_fields:
            raise ValueError(
                "Run config calibration_target contains unsupported fields: "
                + ", ".join(unexpected_target_fields)
            )
        missing_target_fields = sorted(
            required_target_fields - calibration_target.keys()
        )
        if missing_target_fields:
            raise ValueError(
                "Run config calibration_target is missing: "
                + ", ".join(missing_target_fields)
            )
        try:
            target_id = str(uuid.UUID(str(calibration_target.get("target_id"))))
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                "Run config calibration_target.target_id must be a canonical UUID"
            ) from exc
        if calibration_target.get("target_id") != target_id:
            raise ValueError(
                "Run config calibration_target.target_id must be a canonical UUID"
            )
        bundle_path_value = calibration_target.get("bundle_path")
        if not isinstance(bundle_path_value, str) or not bundle_path_value.strip():
            raise ValueError(
                "Run config calibration_target.bundle_path must be a non-empty path"
            )
        bundle_path = Path(bundle_path_value)
        if bundle_path.is_absolute() or ".." in bundle_path.parts:
            raise ValueError(
                "Run config calibration_target.bundle_path must be run-relative"
            )
        for hash_key in (
            "source_sha256",
            "spec_sha256",
            "pdf_sha256",
            "configuration_sha256",
            "geometry_sha256",
        ):
            digest = calibration_target.get(hash_key)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(
                    f"Run config calibration_target.{hash_key} must be a SHA-256 digest"
                )
        placement = calibration_target.get("placement")
        if not isinstance(placement, Mapping) or placement.get("mode") not in {
            "unknown",
            "template_base_identity",
            "posegridgen_board_to_base",
        }:
            raise ValueError("Run config calibration_target placement mode is invalid")
        mounting_frame = placement.get("mounting_frame")
        if mounting_frame not in {
            "robot_flange",
            "template_base",
        }:
            raise ValueError(
                "Run config calibration_target placement mounting_frame must be "
                "robot_flange or template_base"
            )
        if placement.get("mode") != "unknown" and mounting_frame != "template_base":
            raise ValueError(
                "Run config known calibration-target placement requires "
                "mounting_frame=template_base"
            )
    if value["dataset_mode"] == "objectless" and value.get("pose_template") is not None:
        raise ValueError("Objectless run config cannot reference a pose template")
    if (
        value["dataset_mode"] != "pose_template"
        and value.get("pose_template") is not None
    ):
        raise ValueError(
            "Only pose_template dataset mode may reference a pose template"
        )

    frames = value.get("frames")
    if not isinstance(frames, Mapping):
        raise ValueError("Run config frames must be an object")
    if frames is not None:
        expected_frame_fields = {
            "robot_pose",
            "dataset_reference_frame",
            "fixed_transforms",
        }
        if set(frames) != expected_frame_fields:
            raise ValueError(
                "Run config frames fields must be exactly: "
                + ", ".join(sorted(expected_frame_fields))
            )
        robot_pose = frames.get("robot_pose")
        if (
            not isinstance(robot_pose, Mapping)
            or robot_pose.get("from") != "robot_flange"
            or robot_pose.get("to") != "template_base"
        ):
            raise ValueError(
                "Run config frames.robot_pose must map robot_flange to template_base"
            )
        if robot_pose.get("convention") != "kuka_abc_radians":
            raise ValueError(
                "Run config robot pose convention must be kuka_abc_radians"
            )
        expected_robot_pose = {
            "from": "robot_flange",
            "to": "template_base",
            "convention": "kuka_abc_radians",
            "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
        }
        if dict(robot_pose) != expected_robot_pose:
            raise ValueError(
                "Run config frames.robot_pose must use the canonical "
                "robot_flange-to-PoseTemplateBase contract"
            )
        if frames.get("dataset_reference_frame") != "template_base":
            raise ValueError("Run config dataset_reference_frame must be template_base")
        fixed_transforms = frames.get("fixed_transforms", [])
        if not isinstance(fixed_transforms, list):
            raise ValueError("Run config frames.fixed_transforms must be a list")
        for index, transform in enumerate(fixed_transforms):
            if not isinstance(transform, Mapping):
                raise ValueError(f"Fixed transform {index} must be an object")
            expected_transform_fields = {
                "from",
                "to",
                "rotation_quaternion_wxyz",
                "translation_mm",
                "source",
            }
            if set(transform) != expected_transform_fields:
                raise ValueError(
                    f"Fixed transform {index} fields do not match the current contract"
                )
            if (
                not isinstance(transform.get("from"), str)
                or not transform["from"]
                or not isinstance(transform.get("to"), str)
                or not transform["to"]
            ):
                raise ValueError(f"Fixed transform {index} requires from/to endpoints")
            if (
                not isinstance(transform.get("source"), str)
                or not transform["source"].strip()
            ):
                raise ValueError(
                    f"Fixed transform {index} source must be a non-empty string"
                )
            quaternion = transform.get("rotation_quaternion_wxyz")
            translation = transform.get("translation_mm")
            if not isinstance(quaternion, list) or len(quaternion) != 4:
                raise ValueError(
                    f"Fixed transform {index} quaternion must have 4 values"
                )
            if not isinstance(translation, list) or len(translation) != 3:
                raise ValueError(
                    f"Fixed transform {index} translation must have 3 values"
                )
            raw_values = [*quaternion, *translation]
            if any(
                isinstance(item, bool) or not isinstance(item, (int, float))
                for item in raw_values
            ):
                raise ValueError(f"Fixed transform {index} must be numeric")
            values = [float(item) for item in raw_values]
            if not all(math.isfinite(item) for item in values):
                raise ValueError(f"Fixed transform {index} must be finite")
            if not math.isclose(
                sum(float(item) ** 2 for item in quaternion), 1.0, abs_tol=1e-3
            ):
                raise ValueError(
                    f"Fixed transform {index} quaternion must be normalized"
                )

    sensors = capture.get("sensors")
    if not isinstance(sensors, list) or not sensors:
        raise ValueError("Run config capture.sensors must be a non-empty list")
    enabled_sensor_count = 0
    sensor_identities: set[tuple[str, str]] = set()
    expected_sensor_fields = {
        "sensor_type",
        "device_id",
        "display_name",
        "mounting_mode",
        "enabled",
        "calibration_profile_id",
        "inverted",
        "metadata",
    }
    for index, sensor in enumerate(sensors):
        if not isinstance(sensor, Mapping):
            raise ValueError(f"Run config sensor {index} must be an object")
        optional_sensor_fields = {"operator_alias"}
        if not expected_sensor_fields.issubset(sensor) or not set(sensor).issubset(
            expected_sensor_fields | optional_sensor_fields
        ):
            raise ValueError(
                f"Run config sensor {index} fields do not match the current contract"
            )
        if not isinstance(sensor.get("sensor_type"), str):
            raise ValueError(f"Run config sensor {index} sensor_type must be a string")
        sensor_type = normalize_sensor_type(sensor["sensor_type"])
        if not isinstance(sensor.get("mounting_mode"), str):
            raise ValueError(
                f"Run config sensor {index} mounting_mode must be a string"
            )
        normalize_mounting_mode(sensor["mounting_mode"])
        if (
            not isinstance(sensor.get("device_id"), str)
            or not sensor["device_id"].strip()
        ):
            raise ValueError(f"Run config sensor {index} device_id must not be empty")
        identity = (sensor_type.value, str(sensor["device_id"]).strip())
        if identity in sensor_identities:
            raise ValueError(
                "Run config capture sensors repeat identity " + ":".join(identity)
            )
        sensor_identities.add(identity)
        if (
            not isinstance(sensor.get("display_name"), str)
            or not sensor["display_name"].strip()
        ):
            raise ValueError(
                f"Run config sensor {index} display_name must be a non-empty string"
            )
        if not isinstance(sensor.get("metadata"), Mapping):
            raise ValueError(
                f"Run config sensor {index} metadata must be a JSON object"
            )
        calibration_profile_id = sensor.get("calibration_profile_id")
        if calibration_profile_id is not None and (
            not isinstance(calibration_profile_id, str)
            or not calibration_profile_id.strip()
        ):
            raise ValueError(
                f"Run config sensor {index} calibration_profile_id must be a non-empty string or null"
            )
        if "operator_alias" in sensor:
            operator_alias = sensor.get("operator_alias")
            if (
                not isinstance(operator_alias, str)
                or not operator_alias.strip()
                or operator_alias != operator_alias.strip()
            ):
                raise ValueError(
                    f"Run config sensor {index} operator_alias must be a trimmed non-empty string"
                )
            if sensor["display_name"] != operator_alias:
                raise ValueError(
                    f"Run config sensor {index} display_name must match operator_alias"
                )
        try:
            enabled = normalize_sensor_enabled(sensor.get("enabled", True))
        except ValueError as exc:
            raise ValueError(
                f"Run config sensor {index} enabled must be a boolean"
            ) from exc
        enabled_sensor_count += int(enabled)
        inverted = sensor.get("inverted")
        if not isinstance(inverted, bool):
            raise ValueError(f"Run config sensor {index} inverted must be a boolean")
        _validate_sensor_orientation(sensor_type, inverted)
    if enabled_sensor_count == 0:
        raise ValueError("Run config must enable at least one capture sensor")
    synchronization = capture.get("synchronization")
    if synchronization is None:
        raise ValueError("run_config.v4 requires capture.synchronization")
    validate_capture_synchronization(synchronization, sensors)
    bop = value.get("bop")
    if not isinstance(bop, Mapping):
        raise ValueError("Run config bop must be an object")
    if set(bop) != {"annotation_mode"}:
        raise ValueError("Run config bop accepts only annotation_mode")
    if bop.get("annotation_mode") not in BOP_ANNOTATION_MODES:
        raise ValueError(
            "Run config bop.annotation_mode must be one of: "
            + ", ".join(sorted(BOP_ANNOTATION_MODES))
        )
    if capture.get("intent") == "calibration" and dataset_mode != "objectless":
        raise ValueError("Calibration capture intent requires dataset_mode=objectless")
    if capture.get("intent") == "calibration" and bop.get("annotation_mode") != "none":
        raise ValueError("Calibration capture intent requires bop.annotation_mode=none")


def write_run_config(run_root: str | Path, config: PoseTestBotRunConfig) -> Path:
    with run_config_lock(run_root) as root:
        return atomic_write_json(root / RUN_CONFIG, config.to_dict())


def write_run_config_with_manifest(
    run_root: str | Path,
    config: PoseTestBotRunConfig,
) -> Path:
    run_root_path = Path(run_root)
    path = write_run_config(run_root_path, config)
    manifest = load_or_create_run_manifest(run_root_path)
    upsert_stage(
        manifest,
        name="run_config",
        status="succeeded",
        artifacts={RUN_CONFIG: path},
        run_root=run_root_path,
        message=(
            "Created run config for "
            f"{len(config.capture.sensors)} sensor(s), "
            f"{config.capture.intent} capture intent, and the fixed lab iiwa profile."
        ),
    )
    write_run_manifest(manifest, run_root_path)
    return path


def load_run_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Run config must be a JSON object: {path}")
    validate_run_config(value)
    return value


def load_run_config_for_run_root(run_root: str | Path) -> dict[str, Any]:
    run_root_path = Path(run_root)
    config = load_run_config(run_root_path / RUN_CONFIG)
    config_run_root = Path(str(config["run_root"])).resolve()
    if config_run_root != run_root_path.resolve():
        raise ValueError(
            "Run config run_root does not match requested run_root: "
            f"{config['run_root']} != {run_root_path.as_posix()}"
        )
    return config
