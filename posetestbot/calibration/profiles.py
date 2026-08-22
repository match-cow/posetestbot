"""Typed calibration profile contract for current sensor calibration."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

import cv2
import numpy as np

from posetestbot.calibration.intrinsics import projection_is_opencv_compatible
from posetestbot.io.atomic import atomic_write_json
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    verified_sunrise_reference_frame_path,
)
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType
from posetestbot.sensors.registry import SENSOR_ADAPTERS

SCHEMA_VERSION = "calibration.v2"
QUATERNION_NORM_TOLERANCE = 1e-3


class CalibrationTargetType(StrEnum):
    CHARUCO = "charuco"
    ARUCO_GRID = "aruco_grid"
    CHECKERBOARD = "checkerboard"
    UNKNOWN = "unknown"


class CalibrationStatus(StrEnum):
    VALID = "valid"
    NEEDS_VALIDATION = "needs_validation"
    DEPRECATED = "deprecated"
    FAILED = "failed"


class TransformFrame(StrEnum):
    CAMERA = "camera"
    ROBOT_FLANGE = "robot_flange"
    TEMPLATE_BASE = "template_base"
    ARUCO_GRID = "aruco_grid"
    TCP = "tcp"
    PHYSICAL_ROBOT_BASE = "physical_robot_base"


@dataclass(frozen=True)
class RigidTransform:
    """Rigid transform with quaternion order matching the baseline: w, x, y, z."""

    from_frame: TransformFrame
    to_frame: TransformFrame
    rotation_quaternion_wxyz: tuple[float, float, float, float]
    translation_mm: tuple[float, float, float]

    def validate_for_mounting_mode(self, mounting_mode: MountingMode) -> None:
        if self.from_frame != TransformFrame.CAMERA:
            raise ValueError("Calibration extrinsics must use from_frame='camera'")
        if mounting_mode == MountingMode.EYE_IN_HAND:
            expected = TransformFrame.ROBOT_FLANGE
        else:
            expected = TransformFrame.TEMPLATE_BASE
        if self.to_frame != expected:
            raise ValueError(
                f"{mounting_mode.value} calibration must transform camera to "
                f"{expected.value}"
            )
        if len(self.rotation_quaternion_wxyz) != 4:
            raise ValueError("rotation_quaternion_wxyz must have 4 values")
        if len(self.translation_mm) != 3:
            raise ValueError("translation_mm must have 3 values")
        values = (*self.rotation_quaternion_wxyz, *self.translation_mm)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Calibration extrinsics must contain only finite values")
        quaternion_norm = math.sqrt(
            sum(float(value) ** 2 for value in self.rotation_quaternion_wxyz)
        )
        if abs(quaternion_norm - 1.0) > QUATERNION_NORM_TOLERANCE:
            raise ValueError(
                "rotation_quaternion_wxyz must be normalized to unit length"
            )


@dataclass(frozen=True)
class CalibrationQuality:
    num_observations: int = 0
    num_inliers: int = 0
    mean_reprojection_error_px: float | None = None
    max_reprojection_error_px: float | None = None
    residual_translation_mm: float | None = None
    residual_rotation_deg: float | None = None
    notes: str | None = None

    def validate(self) -> None:
        if self.num_observations < 0:
            raise ValueError("num_observations cannot be negative")
        if self.num_inliers < 0:
            raise ValueError("num_inliers cannot be negative")
        if self.num_inliers > self.num_observations:
            raise ValueError("num_inliers cannot exceed num_observations")
        for name in (
            "mean_reprojection_error_px",
            "max_reprojection_error_px",
            "residual_translation_mm",
            "residual_rotation_deg",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"quality.{name} must be finite and nonnegative")


@dataclass(frozen=True)
class CalibrationProfile:
    schema_version: str
    profile_id: str
    sensor_id: str
    sensor_type: SensorType
    mounting_mode: MountingMode
    rig_position: str
    intrinsics: CameraIntrinsics
    extrinsics: RigidTransform
    rectified_intrinsics: CameraIntrinsics | None = None
    rectified_valid_roi: tuple[int, int, int, int] | None = None
    target_type: CalibrationTargetType = CalibrationTargetType.UNKNOWN
    calibration_dataset_id: str | None = None
    method: str | None = None
    status: CalibrationStatus = CalibrationStatus.NEEDS_VALIDATION
    quality: CalibrationQuality = field(default_factory=CalibrationQuality)
    operator: str | None = None
    calibrated_at: str | None = None
    sync_delta_ms: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported calibration schema: {self.schema_version!r}")
        if not self.profile_id:
            raise ValueError("profile_id is required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.profile_id):
            raise ValueError(
                "profile_id may only contain letters, numbers, underscore, dot, or dash"
            )
        if not self.sensor_id:
            raise ValueError("sensor_id is required")
        if not self.rig_position:
            raise ValueError("rig_position is required")

        def validate_camera_intrinsics(
            intrinsics: CameraIntrinsics,
            label: str,
        ) -> None:
            if len(intrinsics.cam_k) != 9:
                raise ValueError(f"{label}.cam_k must have 9 values")
            if len(intrinsics.distortion) != 5:
                raise ValueError(f"{label}.distortion must have exactly 5 values")
            values = (*intrinsics.cam_k, *intrinsics.distortion)
            if not all(math.isfinite(float(value)) for value in values):
                raise ValueError(f"{label} must contain only finite values")
            if (
                not math.isfinite(float(intrinsics.depth_scale_to_mm))
                or intrinsics.depth_scale_to_mm <= 0
            ):
                raise ValueError(
                    f"{label}.depth_scale_to_mm must be finite and positive"
                )
            if intrinsics.width <= 0 or intrinsics.height <= 0:
                raise ValueError(f"{label} width and height must be positive")
            if intrinsics.cam_k[0] <= 0 or intrinsics.cam_k[4] <= 0:
                raise ValueError(f"{label} focal lengths fx and fy must be positive")
            if not all(
                math.isclose(float(value), expected, abs_tol=1e-9)
                for value, expected in zip(
                    intrinsics.cam_k[6:9],
                    (0.0, 0.0, 1.0),
                    strict=True,
                )
            ):
                raise ValueError(f"{label}.cam_k bottom row must be [0, 0, 1]")
            if not intrinsics.distortion_model.strip():
                raise ValueError(f"{label}.distortion_model is required")

        validate_camera_intrinsics(self.intrinsics, "intrinsics")
        if self.rectified_intrinsics is not None:
            rectified = self.rectified_intrinsics
            validate_camera_intrinsics(rectified, "rectified_intrinsics")
            if (
                rectified.width != self.intrinsics.width
                or rectified.height != self.intrinsics.height
            ):
                raise ValueError(
                    "rectified intrinsics must preserve native output resolution"
                )
            if any(
                not math.isclose(float(value), 0.0, abs_tol=1e-12)
                for value in rectified.distortion
            ):
                raise ValueError("rectified intrinsics must have zero distortion")
            if not math.isclose(
                float(rectified.depth_scale_to_mm),
                float(self.intrinsics.depth_scale_to_mm),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError(
                    "rectified intrinsics must preserve native depth scale"
                )
        if self.rectified_valid_roi is not None:
            if len(self.rectified_valid_roi) != 4 or any(
                int(value) < 0 for value in self.rectified_valid_roi
            ):
                raise ValueError(
                    "rectified valid ROI must contain four nonnegative integers"
                )
            x, y, width, height = self.rectified_valid_roi
            if width <= 0 or height <= 0:
                raise ValueError(
                    "rectified valid ROI width and height must be positive"
                )
            if x + width > self.intrinsics.width or y + height > self.intrinsics.height:
                raise ValueError(
                    "rectified valid ROI must fit within output resolution"
                )
        if self.sync_delta_ms is not None and (
            isinstance(self.sync_delta_ms, bool)
            or not isinstance(self.sync_delta_ms, int | float)
            or not math.isfinite(float(self.sync_delta_ms))
        ):
            raise ValueError("sync_delta_ms must be a finite number")
        self.extrinsics.validate_for_mounting_mode(self.mounting_mode)
        self.quality.validate()
        if self.status == CalibrationStatus.VALID and self.quality.num_inliers <= 0:
            raise ValueError(
                "valid calibration profiles must record at least one inlier"
            )


def _enum_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    return value


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return _enum_value(value)


def _intrinsics_to_dict(intrinsics: CameraIntrinsics) -> dict[str, Any]:
    distortion = [float(item) for item in intrinsics.distortion[:5]]
    distortion.extend([0.0] * (5 - len(distortion)))
    return {
        "cam_K": list(intrinsics.cam_k),
        "width": intrinsics.width,
        "height": intrinsics.height,
        "distortion_model": intrinsics.distortion_model,
        "distortion": distortion,
        "depth_scale_to_mm": intrinsics.depth_scale_to_mm,
        "projection_source": intrinsics.projection_source,
    }


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ValueError(f"{label} fields are invalid: " + "; ".join(details))


def _strict_string(value: Any, *, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string" + (" or null" if nullable else ""))
    return value


def _strict_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _strict_number(
    value: Any,
    *,
    label: str,
    nullable: bool = False,
) -> int | float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a number" + (" or null" if nullable else ""))
    if not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")
    return value


def _strict_number_sequence(
    value: Any,
    *,
    label: str,
    length: int,
) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label} must contain exactly {length} numbers")
    return tuple(
        float(_strict_number(item, label=f"{label}[{index}]"))
        for index, item in enumerate(value)
    )


def _intrinsics_from_dict(
    value: Mapping[str, Any],
    *,
    label: str,
    rectified: bool = False,
) -> CameraIntrinsics:
    expected = {
        "cam_K",
        "width",
        "height",
        "distortion_model",
        "distortion",
        "depth_scale_to_mm",
        "projection_source",
    }
    if rectified:
        expected.update({"alpha", "valid_roi"})
    _require_exact_keys(value, expected, label=label)
    projection_source = _strict_string(
        value["projection_source"],
        label=f"{label}.projection_source",
        nullable=True,
    )
    if rectified:
        alpha = _strict_number(value["alpha"], label=f"{label}.alpha")
        if not math.isclose(float(alpha), 0.0, abs_tol=1e-12):
            raise ValueError(f"{label}.alpha must be 0")
        valid_roi = value["valid_roi"]
        if not isinstance(valid_roi, list) or len(valid_roi) != 4:
            raise ValueError(f"{label}.valid_roi must contain exactly 4 integers")
        for index, item in enumerate(valid_roi):
            _strict_integer(item, label=f"{label}.valid_roi[{index}]")
    return CameraIntrinsics(
        cam_k=_strict_number_sequence(value["cam_K"], label=f"{label}.cam_K", length=9),
        width=_strict_integer(value["width"], label=f"{label}.width"),
        height=_strict_integer(value["height"], label=f"{label}.height"),
        distortion=_strict_number_sequence(
            value["distortion"], label=f"{label}.distortion", length=5
        ),
        depth_scale_to_mm=float(
            _strict_number(
                value["depth_scale_to_mm"], label=f"{label}.depth_scale_to_mm"
            )
        ),
        distortion_model=str(
            _strict_string(value["distortion_model"], label=f"{label}.distortion_model")
        ),
        projection_source=projection_source,
    )


def rectified_projection_from_native(
    intrinsics: CameraIntrinsics,
) -> tuple[CameraIntrinsics | None, tuple[int, int, int, int] | None]:
    if intrinsics.width <= 0 or intrinsics.height <= 0:
        return None, None
    if not projection_is_opencv_compatible(
        {
            "distortion_model": intrinsics.distortion_model,
            "distortion": list(intrinsics.distortion),
        }
    ):
        return None, None
    matrix = np.asarray(intrinsics.cam_k, dtype=float).reshape(3, 3)
    distortion = np.zeros(5, dtype=float)
    source = np.asarray(intrinsics.distortion, dtype=float).reshape(-1)
    distortion[: min(5, source.size)] = source[:5]
    rectified, roi = cv2.getOptimalNewCameraMatrix(
        matrix,
        distortion,
        (intrinsics.width, intrinsics.height),
        0.0,
        (intrinsics.width, intrinsics.height),
    )
    return (
        CameraIntrinsics(
            cam_k=tuple(float(item) for item in rectified.reshape(-1)),
            width=intrinsics.width,
            height=intrinsics.height,
            distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
            depth_scale_to_mm=intrinsics.depth_scale_to_mm,
            distortion_model="brown_conrady",
            projection_source=(
                f"rectified_alpha0_from:{intrinsics.projection_source}"
                if intrinsics.projection_source
                else "rectified_alpha0"
            ),
        ),
        tuple(int(item) for item in roi),
    )


def rectified_intrinsics_from_native(
    intrinsics: CameraIntrinsics,
) -> CameraIntrinsics | None:
    return rectified_projection_from_native(intrinsics)[0]


def _transform_frame(value: Any) -> TransformFrame:
    return TransformFrame(str(value))


def profile_to_dict(profile: CalibrationProfile) -> dict[str, Any]:
    profile.validate()
    derived_rectified, derived_roi = rectified_projection_from_native(
        profile.intrinsics
    )
    rectified_intrinsics = profile.rectified_intrinsics or derived_rectified
    rectified_roi = profile.rectified_valid_roi or derived_roi
    rectified_value = (
        _intrinsics_to_dict(rectified_intrinsics)
        if rectified_intrinsics is not None
        else None
    )
    if rectified_value is not None:
        rectified_value.update(
            {"alpha": 0.0, "valid_roi": list(rectified_roi or (0, 0, 0, 0))}
        )
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "sensor_id": profile.sensor_id,
        "sensor_type": profile.sensor_type.value,
        "mounting_mode": profile.mounting_mode.value,
        "rig_position": profile.rig_position,
        "intrinsics": {
            "native": _intrinsics_to_dict(profile.intrinsics),
            "rectified": rectified_value,
        },
        "extrinsics": {
            "from": profile.extrinsics.from_frame.value,
            "to": profile.extrinsics.to_frame.value,
            "rotation_quaternion_wxyz": list(
                profile.extrinsics.rotation_quaternion_wxyz
            ),
            "translation_mm": list(profile.extrinsics.translation_mm),
        },
        "target_type": profile.target_type.value,
        "calibration_dataset_id": profile.calibration_dataset_id,
        "method": profile.method,
        "status": profile.status.value,
        "quality": _jsonable(profile.quality),
        "operator": profile.operator,
        "calibrated_at": profile.calibrated_at,
        "sync_delta_ms": profile.sync_delta_ms,
        "metadata": dict(profile.metadata),
        "projection_provenance": dict(
            profile.metadata.get(
                "projection_provenance",
                {
                    "native": "captured_or_calibrated_color_projection",
                    "rectified": "opencv_alpha0_same_resolution",
                    "depth_scale": "factory_sdk_not_recalibrated",
                    "depth_alignment": "capture_adapter_sdk_depth_to_color",
                },
            )
        ),
    }


def profile_from_dict(value: Mapping[str, Any]) -> CalibrationProfile:
    if not isinstance(value, Mapping):
        raise ValueError("Calibration profile must be an object")
    _require_exact_keys(
        value,
        {
            "schema_version",
            "profile_id",
            "sensor_id",
            "sensor_type",
            "mounting_mode",
            "rig_position",
            "intrinsics",
            "extrinsics",
            "target_type",
            "calibration_dataset_id",
            "method",
            "status",
            "quality",
            "operator",
            "calibrated_at",
            "sync_delta_ms",
            "metadata",
            "projection_provenance",
        },
        label="calibration.v2 profile",
    )
    source_schema = _strict_string(value["schema_version"], label="schema_version")
    if source_schema != SCHEMA_VERSION:
        raise ValueError(f"Unsupported calibration schema: {source_schema!r}")
    raw_intrinsics = value["intrinsics"]
    if not isinstance(raw_intrinsics, Mapping):
        raise ValueError("Calibration intrinsics must be an object")
    _require_exact_keys(
        raw_intrinsics,
        {"native", "rectified"},
        label="calibration.v2 intrinsics",
    )
    intrinsics = raw_intrinsics.get("native")
    rectified_intrinsics = raw_intrinsics.get("rectified")
    if not isinstance(intrinsics, Mapping):
        raise ValueError("calibration.v2 intrinsics.native must be an object")
    extrinsics = value["extrinsics"]
    if not isinstance(extrinsics, Mapping):
        raise ValueError("calibration.v2 extrinsics must be an object")
    _require_exact_keys(
        extrinsics,
        {"from", "to", "rotation_quaternion_wxyz", "translation_mm"},
        label="calibration.v2 extrinsics",
    )
    quality = value["quality"]
    if not isinstance(quality, Mapping):
        raise ValueError("calibration.v2 quality must be an object")
    _require_exact_keys(
        quality,
        {
            "num_observations",
            "num_inliers",
            "mean_reprojection_error_px",
            "max_reprojection_error_px",
            "residual_translation_mm",
            "residual_rotation_deg",
            "notes",
        },
        label="calibration.v2 quality",
    )
    metadata = value["metadata"]
    if not isinstance(metadata, Mapping):
        raise ValueError("calibration.v2 metadata must be an object")
    projection_provenance = value["projection_provenance"]
    if not isinstance(projection_provenance, Mapping):
        raise ValueError("calibration.v2 projection_provenance must be an object")

    native_intrinsics = _intrinsics_from_dict(
        intrinsics,
        label="intrinsics.native",
    )
    normalized_rectified = (
        _intrinsics_from_dict(
            rectified_intrinsics,
            label="intrinsics.rectified",
            rectified=True,
        )
        if isinstance(rectified_intrinsics, Mapping)
        else None
    )
    if rectified_intrinsics is not None and not isinstance(
        rectified_intrinsics, Mapping
    ):
        raise ValueError("intrinsics.rectified must be an object or null")
    rectified_roi = (
        tuple(rectified_intrinsics["valid_roi"])
        if isinstance(rectified_intrinsics, Mapping)
        else None
    )
    sync_delta_ms = _strict_number(
        value["sync_delta_ms"],
        label="sync_delta_ms",
        nullable=True,
    )
    profile = CalibrationProfile(
        schema_version=SCHEMA_VERSION,
        profile_id=str(_strict_string(value["profile_id"], label="profile_id")),
        sensor_id=str(_strict_string(value["sensor_id"], label="sensor_id")),
        sensor_type=SensorType(
            _strict_string(value["sensor_type"], label="sensor_type")
        ),
        mounting_mode=MountingMode(
            _strict_string(value["mounting_mode"], label="mounting_mode")
        ),
        rig_position=str(_strict_string(value["rig_position"], label="rig_position")),
        intrinsics=native_intrinsics,
        rectified_intrinsics=normalized_rectified,
        rectified_valid_roi=rectified_roi,
        extrinsics=RigidTransform(
            from_frame=TransformFrame(
                _strict_string(extrinsics["from"], label="extrinsics.from")
            ),
            to_frame=TransformFrame(
                _strict_string(extrinsics["to"], label="extrinsics.to")
            ),
            rotation_quaternion_wxyz=_strict_number_sequence(
                extrinsics["rotation_quaternion_wxyz"],
                label="extrinsics.rotation_quaternion_wxyz",
                length=4,
            ),
            translation_mm=_strict_number_sequence(
                extrinsics["translation_mm"],
                label="extrinsics.translation_mm",
                length=3,
            ),
        ),
        target_type=CalibrationTargetType(
            _strict_string(value["target_type"], label="target_type")
        ),
        calibration_dataset_id=_strict_string(
            value["calibration_dataset_id"],
            label="calibration_dataset_id",
            nullable=True,
        ),
        method=_strict_string(value["method"], label="method", nullable=True),
        status=CalibrationStatus(_strict_string(value["status"], label="status")),
        quality=CalibrationQuality(
            num_observations=_strict_integer(
                quality["num_observations"], label="quality.num_observations"
            ),
            num_inliers=_strict_integer(
                quality["num_inliers"], label="quality.num_inliers"
            ),
            mean_reprojection_error_px=_strict_number(
                quality["mean_reprojection_error_px"],
                label="quality.mean_reprojection_error_px",
                nullable=True,
            ),
            max_reprojection_error_px=_strict_number(
                quality["max_reprojection_error_px"],
                label="quality.max_reprojection_error_px",
                nullable=True,
            ),
            residual_translation_mm=_strict_number(
                quality["residual_translation_mm"],
                label="quality.residual_translation_mm",
                nullable=True,
            ),
            residual_rotation_deg=_strict_number(
                quality["residual_rotation_deg"],
                label="quality.residual_rotation_deg",
                nullable=True,
            ),
            notes=_strict_string(
                quality["notes"], label="quality.notes", nullable=True
            ),
        ),
        operator=_strict_string(value["operator"], label="operator", nullable=True),
        calibrated_at=_strict_string(
            value["calibrated_at"], label="calibrated_at", nullable=True
        ),
        sync_delta_ms=float(sync_delta_ms) if sync_delta_ms is not None else None,
        metadata={
            **dict(metadata),
            "projection_provenance": dict(projection_provenance),
        },
    )
    profile.validate()
    return profile


def load_profile(path: str | Path) -> CalibrationProfile:
    with open(path, "r") as f:
        return profile_from_dict(json.load(f))


def write_profile(profile: CalibrationProfile, path: str | Path) -> Path:
    profile.validate()
    path = Path(path)
    return atomic_write_json(path, profile_to_dict(profile))


def write_profile_collection(
    profiles: list[CalibrationProfile], path: str | Path
) -> Path:
    path = Path(path)
    validate_profile_collection(profiles)
    return atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "profiles": [profile_to_dict(profile) for profile in profiles],
        },
    )


def load_profile_collection(path: str | Path) -> list[CalibrationProfile]:
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, Mapping):
        raise ValueError("Calibration profile collection must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Unsupported calibration collection schema: {value!r}")
    raw_profiles = value.get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise ValueError("Calibration profile collection profiles must be a list")
    profiles = [profile_from_dict(item) for item in raw_profiles]
    validate_profile_collection(profiles)
    return profiles


def validate_profile_collection(profiles: Iterable[CalibrationProfile]) -> None:
    profile_list = list(profiles)
    seen_ids: set[str] = set()
    valid_slots: dict[tuple[SensorType, str, MountingMode, str], str] = {}
    for profile in profile_list:
        profile.validate()
        if profile.profile_id in seen_ids:
            raise ValueError(f"Duplicate calibration profile_id: {profile.profile_id}")
        seen_ids.add(profile.profile_id)
        if profile.status != CalibrationStatus.VALID:
            continue
        slot = (
            profile.sensor_type,
            profile.sensor_id,
            profile.mounting_mode,
            profile.rig_position,
        )
        existing = valid_slots.get(slot)
        if existing is not None:
            raise ValueError(
                "Multiple valid calibration profiles occupy the same sensor/mount/rig "
                f"slot: {existing}, {profile.profile_id}"
            )
        valid_slots[slot] = profile.profile_id


def sensor_identity_from_folder_name(sensor_name: str) -> tuple[SensorType | None, str]:
    exact_key = sensor_name.split(":", 1)
    if len(exact_key) == 2 and exact_key[1]:
        try:
            return SensorType(exact_key[0]), exact_key[1]
        except ValueError:
            return None, sensor_name
    for sensor_type, adapter in SENSOR_ADAPTERS.items():
        prefix = f"{adapter.folder_prefix}_"
        if sensor_name.startswith(prefix) and len(sensor_name) > len(prefix):
            return sensor_type, sensor_name[len(prefix) :]
    return None, sensor_name


def _profile_match_score(profile: CalibrationProfile, sensor_name: str) -> int | None:
    sensor_type, device_id = sensor_identity_from_folder_name(sensor_name)
    if sensor_type is None or profile.sensor_type != sensor_type:
        return None
    return 100 if profile.sensor_id == device_id else None


def select_profile_for_sensor(
    profiles: Iterable[CalibrationProfile],
    sensor_name: str,
    *,
    mounting_mode: MountingMode | None = None,
    required_statuses: set[CalibrationStatus] | None = None,
) -> CalibrationProfile:
    matches = []
    for profile in profiles:
        if mounting_mode is not None and profile.mounting_mode != mounting_mode:
            continue
        if required_statuses is not None and profile.status not in required_statuses:
            continue
        score = _profile_match_score(profile, sensor_name)
        if score is not None:
            matches.append((score, profile))

    if not matches:
        mode = f" {mounting_mode.value}" if mounting_mode else ""
        raise KeyError(f"No{mode} calibration profile matches {sensor_name!r}")

    matches.sort(key=lambda item: item[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        profile_ids = ", ".join(profile.profile_id for _, profile in matches)
        raise ValueError(
            f"Ambiguous calibration profiles for {sensor_name!r}: {profile_ids}"
        )
    return matches[0][1]


def select_valid_profile_for_sensor(
    profiles: Iterable[CalibrationProfile],
    sensor_name: str,
    *,
    mounting_mode: MountingMode | None = None,
    profile_id: str | None = None,
) -> CalibrationProfile:
    profile_list = list(profiles)
    if profile_id is not None:
        exact_matches = [
            profile for profile in profile_list if profile.profile_id == profile_id
        ]
        if not exact_matches:
            raise KeyError(
                f"Selected calibration profile {profile_id!r} is unavailable for {sensor_name!r}"
            )
        if len(exact_matches) != 1:
            raise ValueError(f"Duplicate selected calibration profile_id: {profile_id}")
        profile_list = exact_matches
    profile = select_profile_for_sensor(
        profile_list,
        sensor_name,
        mounting_mode=mounting_mode,
        required_statuses={CalibrationStatus.VALID},
    )
    require_static_profile_pose_template_base(profile)
    return profile


def require_static_profile_pose_template_base(
    profile: CalibrationProfile,
) -> None:
    """Require a reusable static profile to be proven in PoseTemplateBase."""

    if profile.mounting_mode != MountingMode.STATIC:
        return
    try:
        observed_path = verified_sunrise_reference_frame_path(
            profile.metadata.get("robot_pose_reference")
        )
    except ValueError as exc:
        raise ValueError(
            f"Static calibration profile {profile.profile_id} has invalid "
            f"robot-pose reference provenance: {exc}"
        ) from exc
    if observed_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        actual = observed_path if observed_path is not None else "unverified"
        raise ValueError(
            f"Static calibration profile {profile.profile_id} cannot be reused: "
            "camera-to-template_base must be backed by verified robot_pose.v1 "
            f"poses in {POSE_TEMPLATE_BASE_SUNRISE_PATH!r}; found {actual!r}. "
            "Create and promote a current guided calibration."
        )


def blenderproc_camera_transform_from_profile(
    profile: CalibrationProfile,
) -> dict[str, object]:
    """Return a BlenderProc-prep transform entry for eye-in-hand or static cameras."""

    profile.validate()
    return {
        "quaternion": list(profile.extrinsics.rotation_quaternion_wxyz),
        "position": list(profile.extrinsics.translation_mm),
        "mounting_mode": profile.mounting_mode.value,
        "from": profile.extrinsics.from_frame.value,
        "to": profile.extrinsics.to_frame.value,
        "profile_id": profile.profile_id,
    }


def blenderproc_camera_transform_map_from_profiles(
    profiles: Iterable[CalibrationProfile],
    sensor_names: Iterable[str],
    *,
    profile_ids_by_sensor_name: Mapping[str, str] | None = None,
    mounting_modes_by_sensor_name: Mapping[str, MountingMode] | None = None,
) -> dict[str, dict[str, object]]:
    profile_list = list(profiles)
    transform_map = {}
    for sensor_name in sensor_names:
        profile_id = None
        if profile_ids_by_sensor_name is not None:
            try:
                profile_id = profile_ids_by_sensor_name[sensor_name]
            except KeyError as exc:
                raise KeyError(
                    f"Calibration selection has no profile for {sensor_name!r}"
                ) from exc
        mounting_mode = None
        if mounting_modes_by_sensor_name is not None:
            try:
                mounting_mode = mounting_modes_by_sensor_name[sensor_name]
            except KeyError as exc:
                raise KeyError(
                    f"Run configuration has no mounting mode for {sensor_name!r}"
                ) from exc
        profile = select_valid_profile_for_sensor(
            profile_list,
            sensor_name,
            mounting_mode=mounting_mode,
            profile_id=profile_id,
        )
        transform_map[sensor_name] = blenderproc_camera_transform_from_profile(profile)
    return transform_map
