"""Discovery and immutable selection of promoted calibration profile bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from posetestbot.calibration.intrinsics import (
    SCHEMA_VERSION as INTRINSIC_SCHEMA_VERSION,
    projection_is_opencv_compatible,
    validate_intrinsic_profile,
)
from posetestbot.calibration.profiles import (
    CalibrationProfile,
    CalibrationStatus,
    SCHEMA_VERSION as CALIBRATION_SCHEMA_VERSION,
    profile_from_dict,
    profile_to_dict,
    rectified_projection_from_native,
    validate_profile_collection,
)
from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json
from posetestbot.io.artifacts import (
    BLENDERPROC_RENDER_PLAN,
    BOP_DIR,
    CALIBRATION_PROFILES,
    CALIBRATION_PROFILE_SELECTION,
    CAMERA_RECTIFICATION_REPORT,
    DEPTH_DIR,
    INTRINSIC_CALIBRATION_PROFILES,
    MATCH_ROBOT_EE_POSES,
    MASKS_DIR,
    PROCESSED_DIR,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
    RUN_CONFIG,
    SYNC_QUALITY_REPORT,
    SYNC_REPORT,
    SYNCHRONIZED_DIR,
)
from posetestbot.pipeline.run_config import (
    SensorRunConfig,
    load_run_config_for_run_root,
    run_config_lock,
    sensor_config_from_mapping,
)
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    configured_sunrise_reference_frame_path,
    normalize_sunrise_reference_frame_path,
    robot_pose_reference_evidence,
    verified_sunrise_reference_frame_path,
)
from posetestbot.sensors.registry import get_sensor_adapter, sensor_folder_name


LIBRARY_SCHEMA_VERSION = "calibration_library.v1"
SELECTION_SCHEMA_VERSION = "calibration_profile_selection.v2"
COMPOSITE_SELECTION_SCHEMA_VERSION = SELECTION_SCHEMA_VERSION
SUPPORTED_SELECTION_SCHEMA_VERSIONS = {SELECTION_SCHEMA_VERSION}
SNAPSHOT_PARENT = Path("processed") / "calibration_inputs"
MAX_PROFILE_ARTIFACT_BYTES = 16 * 1024 * 1024
RESOLUTION_IMAGE_SIZES = {
    "720p": (1280, 720),
    "360p": (672, 376),
}


class CalibrationSelectionConflict(ValueError):
    """A stale or incompatible selection request with structured UI issues."""

    def __init__(self, message: str, issues: Sequence[Mapping[str, Any]]):
        super().__init__(message)
        self.issues = [dict(issue) for issue in issues]


@dataclass(frozen=True)
class _LoadedBundle:
    calibration_bytes: bytes
    intrinsic_bytes: bytes
    calibration_sha256: str
    intrinsic_sha256: str
    bundle_sha256: str
    calibration_schema_version: str
    intrinsic_schema_version: str
    profiles: tuple[CalibrationProfile, ...]
    intrinsic_profiles: tuple[dict[str, Any], ...]


def _issue(code: str, message: str, *, sensor_key: str | None = None) -> dict[str, str]:
    value = {"code": code, "message": message}
    if sensor_key is not None:
        value["sensor_key"] = sensor_key
    return value


def _safe_error(prefix: str, error: Exception) -> str:
    detail = f"{type(error).__name__}: {error}"
    if len(detail) > 500:
        detail = detail[:497] + "..."
    return f"{prefix}: {detail}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bundle_sha256(calibration_sha256: str, intrinsic_sha256: str) -> str:
    payload = (
        f"{CALIBRATION_PROFILES}:{calibration_sha256}\n"
        f"{INTRINSIC_CALIBRATION_PROFILES}:{intrinsic_sha256}\n"
    ).encode("ascii")
    return _sha256(payload)


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _bundle_from_selected_profiles(
    profiles: Sequence[CalibrationProfile],
    intrinsic_profiles: Sequence[Mapping[str, Any]],
) -> _LoadedBundle:
    calibration_bytes = _canonical_json_bytes(
        {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "profiles": [profile_to_dict(profile) for profile in profiles],
        }
    )
    intrinsic_bytes = _canonical_json_bytes(
        {
            "schema_version": INTRINSIC_SCHEMA_VERSION,
            "profiles": [dict(profile) for profile in intrinsic_profiles],
        }
    )
    calibration_schema, parsed_profiles = _parse_calibration_profiles(calibration_bytes)
    intrinsic_schema, parsed_intrinsics = _parse_intrinsic_profiles(intrinsic_bytes)
    calibration_digest = _sha256(calibration_bytes)
    intrinsic_digest = _sha256(intrinsic_bytes)
    return _LoadedBundle(
        calibration_bytes=calibration_bytes,
        intrinsic_bytes=intrinsic_bytes,
        calibration_sha256=calibration_digest,
        intrinsic_sha256=intrinsic_digest,
        bundle_sha256=_bundle_sha256(calibration_digest, intrinsic_digest),
        calibration_schema_version=calibration_schema,
        intrinsic_schema_version=intrinsic_schema,
        profiles=parsed_profiles,
        intrinsic_profiles=parsed_intrinsics,
    )


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(
            f"Artifact is missing, unreadable, or a symbolic link: {path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Artifact must be a regular file: {path}")
        if metadata.st_size > MAX_PROFILE_ARTIFACT_BYTES:
            raise ValueError(
                f"Artifact exceeds {MAX_PROFILE_ARTIFACT_BYTES} bytes: {path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(MAX_PROFILE_ARTIFACT_BYTES + 1)
    finally:
        os.close(descriptor)


def _json_object(payload: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _parse_calibration_profiles(
    payload: bytes,
) -> tuple[str, tuple[CalibrationProfile, ...]]:
    value = _json_object(payload, label=CALIBRATION_PROFILES)
    schema = str(value.get("schema_version"))
    if schema != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("Calibration collection schema must be calibration.v2")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("Calibration collection profiles must be a list")
    if any(not isinstance(item, Mapping) for item in raw_profiles):
        raise ValueError("Calibration collection profiles must be objects")
    profiles = tuple(profile_from_dict(item) for item in raw_profiles)
    validate_profile_collection(profiles)
    return schema, profiles


def _parse_intrinsic_profiles(
    payload: bytes,
) -> tuple[str, tuple[dict[str, Any], ...]]:
    value = _json_object(payload, label=INTRINSIC_CALIBRATION_PROFILES)
    schema = str(value.get("schema_version"))
    if schema != INTRINSIC_SCHEMA_VERSION:
        raise ValueError(
            f"Intrinsic collection schema must be {INTRINSIC_SCHEMA_VERSION!r}"
        )
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("Intrinsic collection profiles must be a list")
    profiles: list[dict[str, Any]] = []
    keys: list[tuple[str, tuple[int, ...], str]] = []
    for item in raw_profiles:
        if not isinstance(item, Mapping):
            raise ValueError("Intrinsic collection profiles must be objects")
        validate_intrinsic_profile(item)
        profile = dict(item)
        profiles.append(profile)
        keys.append(
            (
                str(profile.get("sensor_id")),
                tuple(int(value) for value in profile.get("resolution", [])),
                str(profile.get("orientation")),
            )
        )
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Intrinsic collection has duplicate serial/resolution/orientation profiles"
        )
    return schema, tuple(profiles)


def _load_bundle(source_run_root: Path) -> _LoadedBundle:
    calibration_bytes = _read_regular_file(source_run_root / CALIBRATION_PROFILES)
    intrinsic_bytes = _read_regular_file(
        source_run_root / INTRINSIC_CALIBRATION_PROFILES
    )
    calibration_schema, profiles = _parse_calibration_profiles(calibration_bytes)
    intrinsic_schema, intrinsic_profiles = _parse_intrinsic_profiles(intrinsic_bytes)
    calibration_digest = _sha256(calibration_bytes)
    intrinsic_digest = _sha256(intrinsic_bytes)
    return _LoadedBundle(
        calibration_bytes=calibration_bytes,
        intrinsic_bytes=intrinsic_bytes,
        calibration_sha256=calibration_digest,
        intrinsic_sha256=intrinsic_digest,
        bundle_sha256=_bundle_sha256(calibration_digest, intrinsic_digest),
        calibration_schema_version=calibration_schema,
        intrinsic_schema_version=intrinsic_schema,
        profiles=profiles,
        intrinsic_profiles=intrinsic_profiles,
    )


def _source_run_name(source_run_root: Path) -> str:
    try:
        value = _json_object(
            _read_regular_file(source_run_root / RUN_CONFIG), label=RUN_CONFIG
        )
        name = value.get("run_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, ValueError):
        pass
    return source_run_root.name


def _profile_summary(profile: CalibrationProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "sensor_id": profile.sensor_id,
        "sensor_type": profile.sensor_type.value,
        "mounting_mode": profile.mounting_mode.value,
        "status": profile.status.value,
        "resolution": [profile.intrinsics.width, profile.intrinsics.height],
        "intrinsic_profile_id": (
            str(profile.metadata["intrinsic_profile_id"])
            if profile.metadata.get("intrinsic_profile_id")
            else None
        ),
        "created_at": profile.calibrated_at,
    }


def _intrinsic_summary(profile: Mapping[str, Any]) -> dict[str, Any]:
    source = profile.get("source")
    return {
        "profile_id": str(profile.get("profile_id", "")),
        "sensor_id": str(profile.get("sensor_id", "")),
        "resolution": [int(item) for item in profile.get("resolution", [])],
        "orientation": str(profile.get("orientation", "")),
        "method": (
            str(source.get("mode"))
            if isinstance(source, Mapping) and source.get("mode") is not None
            else None
        ),
    }


def _configured_image_size(
    sensor: Mapping[str, Any], resolution: str
) -> tuple[int, int] | None:
    metadata = sensor.get("metadata")
    if isinstance(metadata, Mapping):
        explicit = metadata.get("image_size") or metadata.get("resolution")
        if (
            isinstance(explicit, list | tuple)
            and len(explicit) == 2
            and all(type(item) is int and item > 0 for item in explicit)
        ):
            return int(explicit[0]), int(explicit[1])
    return RESOLUTION_IMAGE_SIZES.get(resolution)


def _float_values(value: Any, *, count: int) -> tuple[float, ...] | None:
    if not isinstance(value, list | tuple) or len(value) != count:
        return None
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None


def _projection_dimensions(value: Mapping[str, Any]) -> tuple[int, int] | None:
    try:
        dimensions = int(value.get("width", 0)), int(value.get("height", 0))
    except (TypeError, ValueError):
        return None
    return dimensions if dimensions[0] > 0 and dimensions[1] > 0 else None


def _profile_intrinsic_equivalence_issues(
    profile: CalibrationProfile,
    intrinsic: Mapping[str, Any],
    *,
    sensor_key: str,
) -> list[dict[str, str]]:
    """Require both profile files to describe one byte-for-byte numeric projection."""

    mismatches: list[str] = []
    native = intrinsic.get("native")
    if not isinstance(native, Mapping):
        mismatches.append("native projection")
    else:
        if _float_values(native.get("cam_K"), count=9) != tuple(
            float(item) for item in profile.intrinsics.cam_k
        ):
            mismatches.append("native cam_K")
        if _float_values(native.get("distortion"), count=5) != tuple(
            float(item) for item in profile.intrinsics.distortion
        ):
            mismatches.append("native distortion")
        if str(native.get("distortion_model", "brown_conrady")) != str(
            profile.intrinsics.distortion_model
        ):
            mismatches.append("native distortion model")
        if _projection_dimensions(native) != (
            profile.intrinsics.width,
            profile.intrinsics.height,
        ):
            mismatches.append("native dimensions")

    if tuple(int(item) for item in intrinsic.get("resolution", [])) != (
        profile.intrinsics.width,
        profile.intrinsics.height,
    ):
        mismatches.append("collection resolution")

    full_rectified = profile.rectified_intrinsics
    full_roi = profile.rectified_valid_roi
    if full_rectified is not None and full_roi is None:
        _derived, full_roi = rectified_projection_from_native(profile.intrinsics)
    intrinsic_rectified = intrinsic.get("rectified")
    if full_rectified is None or not isinstance(intrinsic_rectified, Mapping):
        mismatches.append("rectified projection availability")
    else:
        if _float_values(intrinsic_rectified.get("cam_K"), count=9) != tuple(
            float(item) for item in full_rectified.cam_k
        ):
            mismatches.append("rectified cam_K")
        if _float_values(intrinsic_rectified.get("distortion"), count=5) != tuple(
            float(item) for item in full_rectified.distortion
        ):
            mismatches.append("rectified distortion")
        if str(intrinsic_rectified.get("distortion_model", "brown_conrady")) != str(
            full_rectified.distortion_model
        ):
            mismatches.append("rectified distortion model")
        if _projection_dimensions(intrinsic_rectified) != (
            full_rectified.width,
            full_rectified.height,
        ):
            mismatches.append("rectified dimensions")
        intrinsic_roi = intrinsic_rectified.get("valid_roi")
        if _float_values(intrinsic_roi, count=4) != (
            tuple(float(item) for item in full_roi) if full_roi is not None else None
        ):
            mismatches.append("rectified valid ROI")

    depth = intrinsic.get("depth")
    try:
        intrinsic_depth_scale = (
            float(depth.get("scale_to_mm")) if isinstance(depth, Mapping) else None
        )
    except (TypeError, ValueError):
        intrinsic_depth_scale = None
    if intrinsic_depth_scale != float(profile.intrinsics.depth_scale_to_mm):
        mismatches.append("depth scale")

    if not mismatches:
        return []
    return [
        _issue(
            "calibration_intrinsic_projection_mismatch",
            f"Calibration profile {profile.profile_id} and intrinsic profile "
            f"{intrinsic.get('profile_id')} disagree on: {', '.join(mismatches)}.",
            sensor_key=sensor_key,
        )
    ]


def _select_profile(
    bundle: _LoadedBundle,
    sensor: Mapping[str, Any],
    resolution: str,
    expected_sunrise_reference_frame_path: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    sensor_type = str(sensor["sensor_type"])
    device_id = str(sensor["device_id"])
    mounting_mode = str(sensor["mounting_mode"])
    sensor_key = f"{sensor_type}:{device_id}"
    image_size = _configured_image_size(sensor, resolution)
    identity_profiles = [
        profile
        for profile in bundle.profiles
        if profile.sensor_type.value == sensor_type and profile.sensor_id == device_id
    ]
    if not identity_profiles:
        return None, [
            _issue(
                "sensor_identity_not_calibrated",
                f"No calibration profile matches {sensor_key}.",
                sensor_key=sensor_key,
            )
        ]

    valid_profiles = [
        profile
        for profile in identity_profiles
        if profile.status == CalibrationStatus.VALID
    ]
    if not valid_profiles:
        return None, [
            _issue(
                "profile_not_valid",
                f"Calibration profiles for {sensor_key} are not marked valid.",
                sensor_key=sensor_key,
            )
        ]

    mounted_profiles = [
        profile
        for profile in valid_profiles
        if profile.mounting_mode.value == mounting_mode
    ]
    if not mounted_profiles:
        return None, [
            _issue(
                "mounting_mode_mismatch",
                f"{sensor_key} is configured as {mounting_mode}, but its valid calibration uses another mounting mode.",
                sensor_key=sensor_key,
            )
        ]

    resolution_profiles = [
        profile
        for profile in mounted_profiles
        if image_size is None
        or (profile.intrinsics.width, profile.intrinsics.height) == image_size
    ]
    if not resolution_profiles:
        return None, [
            _issue(
                "resolution_mismatch",
                f"No valid {sensor_key} profile matches configured resolution {resolution} ({image_size}).",
                sensor_key=sensor_key,
            )
        ]
    if len(resolution_profiles) != 1:
        ids = ", ".join(sorted(profile.profile_id for profile in resolution_profiles))
        return None, [
            _issue(
                "ambiguous_calibration_profile",
                f"More than one valid profile matches {sensor_key}: {ids}.",
                sensor_key=sensor_key,
            )
        ]
    profile = resolution_profiles[0]
    if profile.mounting_mode.value == "static":
        if expected_sunrise_reference_frame_path is None:
            return None, [
                _issue(
                    "destination_robot_pose_reference_unconfigured",
                    "Static calibration reuse requires "
                    "frames.robot_pose.sunrise_reference_frame_path in the "
                    "destination run config. Set it to the exact absolute Sunrise "
                    "Application Data path expected in robot_pose.v1 packets.",
                    sensor_key=sensor_key,
                )
            ]
        if expected_sunrise_reference_frame_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
            return None, [
                _issue(
                    "destination_pose_template_base_reference_required",
                    "Static cameras in an object dataset must use robot poses "
                    f"expressed in {POSE_TEMPLATE_BASE_SUNRISE_PATH}; the "
                    "configured Sunrise reference is "
                    f"{expected_sunrise_reference_frame_path}.",
                    sensor_key=sensor_key,
                )
            ]
        try:
            observed_reference_path = verified_sunrise_reference_frame_path(
                profile.metadata.get("robot_pose_reference")
            )
        except ValueError as exc:
            return None, [
                _issue(
                    "static_calibration_reference_invalid",
                    f"Static calibration profile {profile.profile_id} has invalid "
                    f"robot-pose reference provenance: {exc}.",
                    sensor_key=sensor_key,
                )
            ]
        if observed_reference_path is None:
            return None, [
                _issue(
                    "static_calibration_reference_unverified",
                    f"Static calibration profile {profile.profile_id} does not "
                    "contain verified robot_pose.v1 Sunrise reference-frame "
                    "provenance. Recalibrate with v1 pose packets; retired static "
                    "profiles are not reusable in dataset runs.",
                    sensor_key=sensor_key,
                )
            ]
        if observed_reference_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
            return None, [
                _issue(
                    "static_calibration_not_in_pose_template_base",
                    f"Static calibration profile {profile.profile_id} is "
                    f"expressed in Sunrise frame {observed_reference_path}, not "
                    f"the dataset world frame {POSE_TEMPLATE_BASE_SUNRISE_PATH}. "
                    "The historical profile remains evidence but cannot be "
                    "relabeled as camera-to-PoseTemplateBase; recalibrate in the "
                    "canonical frame.",
                    sensor_key=sensor_key,
                )
            ]
        if observed_reference_path != expected_sunrise_reference_frame_path:
            return None, [
                _issue(
                    "static_calibration_reference_mismatch",
                    f"Static calibration profile {profile.profile_id} is expressed "
                    f"in Sunrise frame {observed_reference_path}, but the "
                    "destination robot pose stream expects "
                    f"{expected_sunrise_reference_frame_path}. Re-express and "
                    "validate the profile or recalibrate in the destination frame.",
                    sensor_key=sensor_key,
                )
            ]
    if profile.rectified_intrinsics is None:
        return None, [
            _issue(
                "rectified_projection_unavailable",
                f"Calibration profile {profile.profile_id} has no usable rectified projection.",
                sensor_key=sensor_key,
            )
        ]

    expected_orientation = "inverted" if sensor.get("inverted") is True else "normal"
    expected_intrinsic_id = profile.metadata.get("intrinsic_profile_id")
    intrinsic_matches = [
        item
        for item in bundle.intrinsic_profiles
        if str(item.get("sensor_id")) == profile.sensor_id
        and tuple(int(value) for value in item.get("resolution", []))
        == (profile.intrinsics.width, profile.intrinsics.height)
        and str(item.get("orientation")) == expected_orientation
        and (
            not expected_intrinsic_id
            or str(item.get("profile_id")) == str(expected_intrinsic_id)
        )
    ]
    if not intrinsic_matches:
        return None, [
            _issue(
                "intrinsic_profile_mismatch",
                f"No intrinsic profile for {sensor_key} matches serial, resolution, and {expected_orientation} orientation.",
                sensor_key=sensor_key,
            )
        ]
    if len(intrinsic_matches) != 1:
        return None, [
            _issue(
                "ambiguous_intrinsic_profile",
                f"More than one intrinsic profile matches {sensor_key}.",
                sensor_key=sensor_key,
            )
        ]
    intrinsic = intrinsic_matches[0]
    native = intrinsic.get("native")
    if not isinstance(native, Mapping) or not projection_is_opencv_compatible(native):
        return None, [
            _issue(
                "intrinsic_projection_not_opencv_compatible",
                f"Intrinsic profile {intrinsic.get('profile_id')} cannot be used for OpenCV rectification.",
                sensor_key=sensor_key,
            )
        ]
    if not isinstance(intrinsic.get("rectified"), Mapping):
        return None, [
            _issue(
                "intrinsic_rectification_unavailable",
                f"Intrinsic profile {intrinsic.get('profile_id')} has no rectified projection.",
                sensor_key=sensor_key,
            )
        ]

    equivalence_issues = _profile_intrinsic_equivalence_issues(
        profile, intrinsic, sensor_key=sensor_key
    )
    if equivalence_issues:
        return None, equivalence_issues

    return {
        "sensor_key": sensor_key,
        "sensor_type": sensor_type,
        "device_id": device_id,
        "calibrated_sensor_id": profile.sensor_id,
        "mounting_mode": mounting_mode,
        "resolution": resolution,
        "image_size": [profile.intrinsics.width, profile.intrinsics.height],
        "orientation": expected_orientation,
        "profile_id": profile.profile_id,
        "intrinsic_profile_id": str(intrinsic["profile_id"]),
    }, []


def _compatibility(
    bundle: _LoadedBundle,
    setup: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    mapping: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for sensor in setup["sensors"]:
        if sensor.get("enabled", True) is not True:
            continue
        selected, sensor_issues = _select_profile(
            bundle,
            sensor,
            str(setup["resolution"]),
            expected_sunrise_reference_frame_path=setup.get(
                "robot_pose_sunrise_reference_frame_path"
            ),
        )
        issues.extend(sensor_issues)
        if selected is not None:
            mapping.append(selected)
    return mapping, issues


def _normalize_setup(
    run_root: Path,
    *,
    sensors: Any = None,
    resolution: Any = None,
    require_available: bool,
) -> dict[str, Any] | None:
    current: Mapping[str, Any] | None = None
    try:
        current = load_run_config_for_run_root(run_root)
    except FileNotFoundError:
        current = None

    if sensors is None:
        if current is None:
            if require_available:
                raise ValueError(
                    "sensors are required until the destination run config has been saved"
                )
            return None
        capture = current.get("capture")
        sensors = capture.get("sensors") if isinstance(capture, Mapping) else None
    if not isinstance(sensors, list | tuple) or not sensors:
        raise ValueError("sensors must be a non-empty list")

    normalized_sensors: list[dict[str, Any]] = []
    for item in sensors:
        if not isinstance(item, Mapping):
            raise ValueError("Each sensor must be a JSON object")
        normalized_sensors.append(sensor_config_from_mapping(item).to_dict())
    if not any(sensor["enabled"] is True for sensor in normalized_sensors):
        raise ValueError("At least one sensor must be enabled")
    keys = [
        (sensor["sensor_type"], sensor["device_id"]) for sensor in normalized_sensors
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("Sensor type/device identity entries must be unique")

    if resolution is None:
        if current is not None and isinstance(current.get("capture"), Mapping):
            resolution = current["capture"].get("resolution")
        else:
            resolution = "720p"
    if not isinstance(resolution, str) or not resolution.strip():
        raise ValueError("resolution must be a non-empty string")
    resolution = resolution.strip()
    for sensor in normalized_sensors:
        if sensor["enabled"] is not True:
            continue
        adapter = get_sensor_adapter(sensor["sensor_type"])
        if resolution not in adapter.supported_resolutions:
            supported = ", ".join(adapter.supported_resolutions)
            raise ValueError(
                f"{adapter.display_name} supports {supported}, not {resolution!r}"
            )
    if current is not None:
        configured_path = configured_sunrise_reference_frame_path(current)
        if configured_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
            raise ValueError(
                "The destination run must use the canonical PoseTemplateBase "
                "Sunrise reference frame"
            )
    return {
        "resolution": resolution,
        "sensors": normalized_sensors,
        "robot_pose_sunrise_reference_frame_path": (POSE_TEMPLATE_BASE_SUNRISE_PATH),
    }


def _setup_identity(
    setup: Any,
) -> tuple[str, tuple[tuple[str, str, str, bool], ...], str | None] | None:
    """Return the calibration-relevant identity of one intended setup.

    Operator aliases, display labels, disabled catalogue entries, and the
    derived ``calibration_profile_id`` do not change calibration geometry.
    Enabled camera membership, physical mounting, orientation, and capture
    resolution do.
    """

    if not isinstance(setup, Mapping):
        return None
    resolution = setup.get("resolution")
    sensors = setup.get("sensors")
    if not isinstance(resolution, str) or not isinstance(sensors, list | tuple):
        return None
    identity: list[tuple[str, str, str, bool]] = []
    for sensor in sensors:
        if not isinstance(sensor, Mapping):
            return None
        if sensor.get("enabled", True) is not True:
            continue
        sensor_type = sensor.get("sensor_type")
        device_id = sensor.get("device_id")
        mounting_mode = sensor.get("mounting_mode")
        inverted = sensor.get("inverted", False)
        if (
            not isinstance(sensor_type, str)
            or not isinstance(device_id, str)
            or not isinstance(mounting_mode, str)
            or not isinstance(inverted, bool)
        ):
            return None
        identity.append((sensor_type, device_id, mounting_mode, inverted))
    raw_reference_path = setup.get("robot_pose_sunrise_reference_frame_path")
    try:
        reference_path = normalize_sunrise_reference_frame_path(raw_reference_path)
    except ValueError:
        return None
    if reference_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        return None
    return resolution, tuple(sorted(identity)), reference_path


def _mapping_identity(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    result: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        sensor_key = raw.get("sensor_key")
        if not isinstance(sensor_key, str) or not sensor_key or sensor_key in result:
            return None
        result[sensor_key] = dict(raw)
    return result


def _bundle_source_artifacts(bundle: _LoadedBundle) -> dict[str, dict[str, Any]]:
    return {
        "calibration_profiles": {
            "relative_path": CALIBRATION_PROFILES,
            "sha256": bundle.calibration_sha256,
            "size_bytes": len(bundle.calibration_bytes),
            "schema_version": bundle.calibration_schema_version,
        },
        "intrinsic_calibration_profiles": {
            "relative_path": INTRINSIC_CALIBRATION_PROFILES,
            "sha256": bundle.intrinsic_sha256,
            "size_bytes": len(bundle.intrinsic_bytes),
            "schema_version": bundle.intrinsic_schema_version,
        },
    }


def _selection_matches_intent(
    selection: Mapping[str, Any],
    *,
    schema_version: str,
    setup: Mapping[str, Any],
    mapping: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Decide idempotence from the complete selection intent, not just bytes."""

    expected_mapping = [dict(item) for item in mapping]
    if (
        selection.get("schema_version") != schema_version
        or _setup_identity(selection.get("intended_setup")) != _setup_identity(setup)
        or _mapping_identity(selection.get("sensor_profile_mapping"))
        != _mapping_identity(expected_mapping)
        or selection.get("sensor_profiles")
        != {item["sensor_key"]: item["profile_id"] for item in expected_mapping}
        or selection.get("source") != dict(source)
    ):
        return False
    if sources is None:
        return "sources" not in selection
    return selection.get("sources") == [dict(item) for item in sources]


def _empty_artifact_summary(filename: str) -> dict[str, Any]:
    return {
        "relative_path": filename,
        "sha256": None,
        "size_bytes": None,
        "schema_version": None,
    }


def _inspect_source(
    source_run_root: Path,
    setup: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], _LoadedBundle | None]:
    calibration_summary = _empty_artifact_summary(CALIBRATION_PROFILES)
    calibration_summary.update({"valid_profile_count": 0, "profiles": []})
    intrinsic_summary = _empty_artifact_summary(INTRINSIC_CALIBRATION_PROFILES)
    intrinsic_summary.update({"profile_count": 0, "profiles": []})
    record: dict[str, Any] = {
        "source_run_root": source_run_root.as_posix(),
        "source_run_name": _source_run_name(source_run_root),
        "bundle_sha256": None,
        "valid": False,
        "compatible": False,
        "issues": [],
        "calibration_profiles": calibration_summary,
        "intrinsic_calibration_profiles": intrinsic_summary,
        "sensor_profile_mapping": [],
        "sensor_profiles": {},
    }
    try:
        bundle = _load_bundle(source_run_root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record["issues"].append(
            _issue(
                "invalid_calibration_bundle",
                _safe_error("Calibration bundle is not usable", exc),
            )
        )
        return record, None

    valid_profiles = [
        profile
        for profile in bundle.profiles
        if profile.status == CalibrationStatus.VALID
    ]
    calibration_summary.update(
        {
            "sha256": bundle.calibration_sha256,
            "size_bytes": len(bundle.calibration_bytes),
            "schema_version": bundle.calibration_schema_version,
            "valid_profile_count": len(valid_profiles),
            "profiles": [_profile_summary(profile) for profile in bundle.profiles],
        }
    )
    intrinsic_summary.update(
        {
            "sha256": bundle.intrinsic_sha256,
            "size_bytes": len(bundle.intrinsic_bytes),
            "schema_version": bundle.intrinsic_schema_version,
            "profile_count": len(bundle.intrinsic_profiles),
            "profiles": [
                _intrinsic_summary(profile) for profile in bundle.intrinsic_profiles
            ],
        }
    )
    record["bundle_sha256"] = bundle.bundle_sha256
    if not valid_profiles:
        record["issues"].append(
            _issue(
                "no_valid_calibration_profiles",
                "The calibration collection contains no profiles marked valid.",
            )
        )
        return record, bundle
    record["valid"] = True
    if setup is None:
        record["issues"].append(
            _issue(
                "destination_setup_required",
                "Save camera settings or provide sensors and resolution before selecting this calibration.",
            )
        )
        return record, bundle

    mapping, compatibility_issues = _compatibility(bundle, setup)
    record["sensor_profile_mapping"] = mapping
    record["sensor_profiles"] = {
        item["sensor_key"]: item["profile_id"] for item in mapping
    }
    record["issues"].extend(compatibility_issues)
    record["compatible"] = not compatibility_issues
    return record, bundle


def _is_direct_run_path(path: Path) -> bool:
    from posetestbot.web.security import web_run_roots

    return any(path.parent == root.resolve() for root in web_run_roots())


def _resolve_direct_run(value: str | Path, *, must_exist: bool) -> Path:
    from posetestbot.web.security import resolve_web_run_root

    resolved = resolve_web_run_root(value)
    if not _is_direct_run_path(resolved):
        raise ValueError("Run must be a direct child of an allowed run root")
    if must_exist:
        directory_entry = resolved.parent / resolved.name
        if directory_entry.is_symlink() or not directory_entry.is_dir():
            raise ValueError("Run must be an existing non-symbolic-link directory")
        if directory_entry.resolve() != resolved:
            raise ValueError("Run directory resolution changed unexpectedly")
    return resolved


def _selection_path(run_root: Path) -> Path:
    return run_root / CALIBRATION_PROFILE_SELECTION


def _safe_snapshot_path(run_root: Path, relative: Any, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"{label} must be a run-relative path")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"{label} must be a run-relative path")
    path = run_root
    for part in raw.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"{label} must not contain symbolic links")
    try:
        path.resolve().relative_to(run_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the destination run") from exc
    return path


def _validate_composite_selection_provenance(value: Mapping[str, Any]) -> None:
    source = value.get("source")
    sources = value.get("sources")
    if not isinstance(source, Mapping) or source.get("kind") != "composite":
        raise ValueError("Composite calibration selection source.kind is invalid")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Composite calibration selection sources are required")
    source_count = source.get("source_count")
    if type(source_count) is not int or source_count != len(sources):
        raise ValueError(
            "Composite calibration selection source_count does not match sources"
        )
    expected_source_name = (
        "Combined calibration from "
        f"{source_count} source run{'s' if source_count != 1 else ''}"
    )
    if source.get("run_name") != expected_source_name:
        raise ValueError("Composite calibration selection source run_name is invalid")

    seen_roots: set[str] = set()
    seen_sensor_keys: set[str] = set()
    provenance_mapping: list[Mapping[str, Any]] = []
    for index, item in enumerate(sources):
        if not isinstance(item, Mapping):
            raise ValueError(f"Composite calibration source {index} must be an object")
        run_root = item.get("run_root")
        if not isinstance(run_root, str) or not Path(run_root).is_absolute():
            raise ValueError(
                f"Composite calibration source {index} run_root must be absolute"
            )
        source_path = Path(run_root)
        if (
            run_root != source_path.as_posix()
            or ".." in source_path.parts
            or not source_path.name
        ):
            raise ValueError(
                f"Composite calibration source {index} run_root is not canonical"
            )
        if run_root in seen_roots:
            raise ValueError("Composite calibration source run roots must be unique")
        seen_roots.add(run_root)
        run_name = item.get("run_name")
        if (
            not isinstance(run_name, str)
            or not run_name.strip()
            or run_name != run_name.strip()
        ):
            raise ValueError(
                f"Composite calibration source {index} run_name is invalid"
            )
        digest = item.get("bundle_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                f"Composite calibration source {index} bundle_sha256 is invalid"
            )
        selected_sensor_keys = item.get("selected_sensor_keys")
        if (
            not isinstance(selected_sensor_keys, list)
            or not selected_sensor_keys
            or any(
                not isinstance(sensor_key, str) or not sensor_key
                for sensor_key in selected_sensor_keys
            )
        ):
            raise ValueError(
                f"Composite calibration source {index} selected_sensor_keys are invalid"
            )
        if len(selected_sensor_keys) != len(set(selected_sensor_keys)):
            raise ValueError(
                f"Composite calibration source {index} repeats a sensor assignment"
            )
        overlap = seen_sensor_keys.intersection(selected_sensor_keys)
        if overlap:
            raise ValueError(
                "Composite calibration sensor assignments overlap: "
                + ", ".join(sorted(overlap))
            )
        seen_sensor_keys.update(selected_sensor_keys)
        mapping = item.get("sensor_profile_mapping")
        if not isinstance(mapping, list) or any(
            not isinstance(entry, Mapping) for entry in mapping
        ):
            raise ValueError(
                f"Composite calibration source {index} sensor mapping is invalid"
            )
        if {entry.get("sensor_key") for entry in mapping} != set(selected_sensor_keys):
            raise ValueError(
                f"Composite calibration source {index} mapping does not match its assignments"
            )
        provenance_mapping.extend(mapping)
        artifact_hashes: dict[str, str] = {}
        for artifact_key in (
            "calibration_profiles",
            "intrinsic_calibration_profiles",
        ):
            artifact = item.get(artifact_key)
            if not isinstance(artifact, Mapping):
                raise ValueError(
                    f"Composite calibration source {index}.{artifact_key} is required"
                )
            artifact_digest = artifact.get("sha256")
            if (
                not isinstance(artifact_digest, str)
                or len(artifact_digest) != 64
                or any(
                    character not in "0123456789abcdef" for character in artifact_digest
                )
            ):
                raise ValueError(
                    f"Composite calibration source {index}.{artifact_key} hash is invalid"
                )
            artifact_hashes[artifact_key] = artifact_digest
        observed_bundle_digest = _bundle_sha256(
            artifact_hashes["calibration_profiles"],
            artifact_hashes["intrinsic_calibration_profiles"],
        )
        if digest != observed_bundle_digest:
            raise ValueError(
                f"Composite calibration source {index} bundle_sha256 does not "
                "match its artifact hashes"
            )

    selection_mapping = value.get("sensor_profile_mapping")
    if not isinstance(selection_mapping, list) or any(
        not isinstance(entry, Mapping) for entry in selection_mapping
    ):
        raise ValueError("Composite calibration selection sensor mapping is invalid")
    mapping_by_key = {
        str(entry.get("sensor_key")): dict(entry) for entry in selection_mapping
    }
    provenance_by_key = {
        str(entry.get("sensor_key")): dict(entry) for entry in provenance_mapping
    }
    if (
        len(mapping_by_key) != len(selection_mapping)
        or len(provenance_by_key) != len(provenance_mapping)
        or mapping_by_key != provenance_by_key
        or set(mapping_by_key) != seen_sensor_keys
    ):
        raise ValueError(
            "Composite calibration source provenance does not match the selection mapping"
        )


def load_calibration_profile_selection(
    run_root: str | Path,
    *,
    verify_snapshots: bool = True,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    value = _json_object(
        _read_regular_file(_selection_path(root)),
        label=CALIBRATION_PROFILE_SELECTION,
    )
    schema_version = value.get("schema_version")
    if schema_version not in SUPPORTED_SELECTION_SCHEMA_VERSIONS:
        raise ValueError(
            "Calibration selection schema must be one of "
            f"{sorted(SUPPORTED_SELECTION_SCHEMA_VERSIONS)!r}"
        )
    source = value.get("source")
    snapshot = value.get("snapshot")
    if not isinstance(source, Mapping) or not isinstance(snapshot, Mapping):
        raise ValueError("Calibration selection source and snapshot are required")
    bundle_digest = str(source.get("bundle_sha256", ""))
    if len(bundle_digest) != 64 or any(
        character not in "0123456789abcdef" for character in bundle_digest
    ):
        raise ValueError("Calibration selection bundle_sha256 is invalid")
    if schema_version == COMPOSITE_SELECTION_SCHEMA_VERSION:
        _validate_composite_selection_provenance(value)
    if not verify_snapshots:
        return dict(value)

    expected_directory = SNAPSHOT_PARENT / bundle_digest
    if snapshot.get("directory") != expected_directory.as_posix():
        raise ValueError(
            "Calibration selection snapshot directory does not match its bundle hash"
        )
    observed_hashes: dict[str, str] = {}
    filenames = {
        "calibration_profiles": CALIBRATION_PROFILES,
        "intrinsic_calibration_profiles": INTRINSIC_CALIBRATION_PROFILES,
    }
    for key, filename in filenames.items():
        artifact = snapshot.get(key)
        source_artifact = source.get(key)
        if not isinstance(artifact, Mapping):
            raise ValueError(f"Calibration selection snapshot.{key} is required")
        if not isinstance(source_artifact, Mapping):
            raise ValueError(f"Calibration selection source.{key} is required")
        expected_relative = expected_directory / filename
        if artifact.get("relative_path") != expected_relative.as_posix():
            raise ValueError(
                f"Calibration selection snapshot path identity changed: {key}"
            )
        if source_artifact.get("relative_path") != filename:
            raise ValueError(
                f"Calibration selection source path identity is invalid: {key}"
            )
        path = _safe_snapshot_path(
            root, artifact.get("relative_path"), label=f"snapshot.{key}.relative_path"
        )
        payload = _read_regular_file(path)
        digest = _sha256(payload)
        if digest != artifact.get("sha256"):
            raise ValueError(f"Calibration selection snapshot hash changed: {key}")
        if source_artifact.get("sha256") != digest:
            raise ValueError(
                f"Calibration selection source and snapshot hashes disagree: {key}"
            )
        if artifact.get("size_bytes") != len(payload) or source_artifact.get(
            "size_bytes"
        ) != len(payload):
            raise ValueError(
                f"Calibration selection source and snapshot sizes disagree: {key}"
            )
        if artifact.get("schema_version") != source_artifact.get("schema_version"):
            raise ValueError(
                f"Calibration selection source and snapshot schemas disagree: {key}"
            )
        observed_hashes[key] = digest
    observed_bundle = _bundle_sha256(
        observed_hashes["calibration_profiles"],
        observed_hashes["intrinsic_calibration_profiles"],
    )
    if observed_bundle != bundle_digest:
        raise ValueError(
            "Calibration selection bundle hash does not match its snapshots"
        )
    return dict(value)


def _resolved_input_path(run_root: Path, value: str | Path, *, label: str) -> Path:
    raw = Path(value)
    path = raw if raw.is_absolute() else run_root / raw
    resolved = path.resolve()
    try:
        resolved.relative_to(run_root)
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside the selected run") from exc
    return resolved


def _verify_static_profile_robot_pose_reference(
    run_root: Path,
    bundle: _LoadedBundle,
    setup: Mapping[str, Any],
    mapping: Sequence[Mapping[str, Any]],
) -> None:
    """Verify existing destination raw poses against selected static profiles."""

    profiles_by_id = {profile.profile_id: profile for profile in bundle.profiles}
    static_profile_ids = {
        str(item.get("profile_id"))
        for item in mapping
        if (
            profiles_by_id.get(str(item.get("profile_id"))) is not None
            and profiles_by_id[str(item.get("profile_id"))].mounting_mode.value
            == "static"
        )
    }
    if not static_profile_ids:
        return

    raw_expected_path = setup.get("robot_pose_sunrise_reference_frame_path")
    if raw_expected_path is None:
        raise ValueError(
            "Static calibration selection has no destination Sunrise robot-pose "
            "reference-frame expectation"
        )
    expected_path = normalize_sunrise_reference_frame_path(raw_expected_path)
    if expected_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        raise ValueError(
            "Static calibration selection must use the canonical dataset world "
            f"frame {POSE_TEMPLATE_BASE_SUNRISE_PATH!r}; found {expected_path!r}"
        )
    for profile_id in sorted(static_profile_ids):
        profile = profiles_by_id[profile_id]
        profile_path = verified_sunrise_reference_frame_path(
            profile.metadata.get("robot_pose_reference")
        )
        if profile_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
            actual = profile_path if profile_path is not None else "unverified"
            raise ValueError(
                f"Selected static calibration profile {profile_id} is not "
                "expressed in the canonical PoseTemplateBase frame: "
                f"{actual!r}"
            )

    candidates = {run_root / RAW_ROBOT_EE_POSES}
    sensors = setup.get("sensors")
    if isinstance(sensors, Sequence) and not isinstance(sensors, (str, bytes)):
        for raw_sensor in sensors:
            if not isinstance(raw_sensor, Mapping):
                continue
            sensor = sensor_config_from_mapping(raw_sensor)
            if sensor.enabled is not True:
                continue
            candidates.add(
                run_root
                / sensor_folder_name(sensor.sensor_type, sensor.device_id)
                / RAW_ROBOT_EE_POSES
            )

    observed_artifacts = [path for path in sorted(candidates) if os.path.lexists(path)]
    for path in observed_artifacts:
        raw = _json_object(
            _read_regular_file(path),
            label=path.relative_to(run_root).as_posix(),
        )
        evidence = robot_pose_reference_evidence(raw)
        observed_path = verified_sunrise_reference_frame_path(evidence)
        if observed_path is None:
            raise ValueError(
                "Destination raw robot poses omit robot_pose.v1 Sunrise "
                "reference-frame provenance required by static calibration: "
                f"{path.relative_to(run_root).as_posix()}"
            )
        if observed_path != expected_path:
            raise ValueError(
                "Destination raw robot-pose Sunrise reference frame does not "
                "match the selected static calibration: "
                f"{path.relative_to(run_root).as_posix()} records "
                f"{observed_path!r}, expected {expected_path!r}"
            )


def verify_calibration_profile_selection(
    run_root: str | Path,
    *,
    expected_calibration_profiles: str | Path | None = None,
    expected_intrinsic_calibration_profiles: str | Path | None = None,
    expected_bundle_sha256: str | None = None,
    verify_run_config: bool = True,
) -> dict[str, Any]:
    """Revalidate immutable selection identity, bytes, pairing, and run-config binding."""

    root = Path(run_root).resolve()
    selection = load_calibration_profile_selection(root)
    source = selection["source"]
    snapshot = selection["snapshot"]
    bundle_digest = str(source["bundle_sha256"])
    if expected_bundle_sha256 is not None and expected_bundle_sha256 != bundle_digest:
        raise ValueError(
            "Calibration selection bundle does not match the expected bundle SHA-256"
        )
    calibration_relative = str(snapshot["calibration_profiles"]["relative_path"])
    intrinsic_relative = str(
        snapshot["intrinsic_calibration_profiles"]["relative_path"]
    )
    calibration_path = _safe_snapshot_path(
        root, calibration_relative, label="snapshot.calibration_profiles.relative_path"
    )
    intrinsic_path = _safe_snapshot_path(
        root,
        intrinsic_relative,
        label="snapshot.intrinsic_calibration_profiles.relative_path",
    )
    if (
        expected_calibration_profiles is not None
        and _resolved_input_path(
            root,
            expected_calibration_profiles,
            label="expected_calibration_profiles",
        )
        != calibration_path.resolve()
    ):
        raise ValueError(
            "Configured calibration_profiles path is not the selected immutable snapshot"
        )
    if (
        expected_intrinsic_calibration_profiles is not None
        and _resolved_input_path(
            root,
            expected_intrinsic_calibration_profiles,
            label="expected_intrinsic_calibration_profiles",
        )
        != intrinsic_path.resolve()
    ):
        raise ValueError(
            "Configured intrinsic_calibration_profiles path is not the selected immutable snapshot"
        )

    bundle = _load_bundle(calibration_path.parent)
    if bundle.bundle_sha256 != bundle_digest:
        raise ValueError("Selected calibration bundle identity changed")
    if bundle.calibration_schema_version != snapshot["calibration_profiles"].get(
        "schema_version"
    ) or bundle.intrinsic_schema_version != snapshot[
        "intrinsic_calibration_profiles"
    ].get("schema_version"):
        raise ValueError("Selected calibration snapshot schema identity changed")

    setup = selection.get("intended_setup")
    if not isinstance(setup, Mapping):
        raise ValueError("Calibration selection intended_setup is missing")
    mapping, issues = _compatibility(bundle, setup)
    if issues:
        raise ValueError(
            "Selected calibration snapshot is no longer internally compatible: "
            + "; ".join(issue["message"] for issue in issues)
        )
    if selection.get("sensor_profile_mapping") != mapping:
        raise ValueError("Calibration selection sensor profile mapping changed")
    expected_sensor_profiles = {
        item["sensor_key"]: item["profile_id"] for item in mapping
    }
    if selection.get("sensor_profiles") != expected_sensor_profiles:
        raise ValueError("Calibration selection sensor profile lookup changed")
    _verify_static_profile_robot_pose_reference(root, bundle, setup, mapping)

    config: Mapping[str, Any] | None = None
    if verify_run_config:
        try:
            config = load_run_config_for_run_root(root)
        except FileNotFoundError:
            config = None
    if verify_run_config and config is not None:
        current_setup = _normalize_setup(root, require_available=True)
        if _setup_identity(setup) != _setup_identity(current_setup):
            raise ValueError(
                "Run config camera setup or Sunrise robot-pose reference no longer "
                "matches the calibration selection"
            )
        pointer = config.get("calibration_profile_selection")
        if not isinstance(pointer, Mapping):
            raise ValueError(
                "Run config does not bind its calibration inputs to the selection manifest"
            )
        if (
            pointer.get("selection_artifact") != CALIBRATION_PROFILE_SELECTION
            or str(pointer.get("bundle_sha256", "")) != bundle_digest
        ):
            raise ValueError("Run config calibration selection pointer is stale")
        if (
            _resolved_input_path(
                root,
                str(config.get("calibration_profiles", "")),
                label="run_config.calibration_profiles",
            )
            != calibration_path.resolve()
        ):
            raise ValueError(
                "Run config calibration_profiles path is not the selected snapshot"
            )
        if (
            _resolved_input_path(
                root,
                str(config.get("intrinsic_calibration_profiles", "")),
                label="run_config.intrinsic_calibration_profiles",
            )
            != intrinsic_path.resolve()
        ):
            raise ValueError(
                "Run config intrinsic_calibration_profiles path is not the selected snapshot"
            )
    return selection


def selected_calibration_profile_ids_by_sensor_folder(
    run_root: str | Path,
    *,
    selection: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve the exact selected profile ID for each enabled capture folder.

    Supplying ``selection`` avoids rereading an artifact that the caller has
    just verified. The current run config is still checked so downstream
    stages cannot silently reinterpret a selection after camera membership or
    mounting changes.
    """

    root = Path(run_root).resolve()
    if selection is None:
        selection = verify_calibration_profile_selection(root)
    config = load_run_config_for_run_root(root)
    capture = config.get("capture")
    if not isinstance(capture, Mapping) or not isinstance(capture.get("sensors"), list):
        raise ValueError("Run config capture sensors are missing")
    current_setup = {
        "resolution": capture.get("resolution"),
        "sensors": capture["sensors"],
        "robot_pose_sunrise_reference_frame_path": (POSE_TEMPLATE_BASE_SUNRISE_PATH),
    }
    reference_path = configured_sunrise_reference_frame_path(config)
    if reference_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        raise ValueError(
            "Run config does not use the canonical PoseTemplateBase Sunrise frame"
        )
    if _setup_identity(selection.get("intended_setup")) != _setup_identity(
        current_setup
    ):
        raise ValueError(
            "Run config camera setup no longer matches the calibration selection"
        )
    mapping_by_key = _mapping_identity(selection.get("sensor_profile_mapping"))
    if mapping_by_key is None:
        raise ValueError("Calibration selection sensor profile mapping is invalid")

    result: dict[str, str] = {}
    enabled_sensor_keys: set[str] = set()
    for raw_sensor in capture["sensors"]:
        sensor = sensor_config_from_mapping(raw_sensor)
        if sensor.enabled is not True:
            continue
        sensor_key = f"{sensor.sensor_type}:{sensor.device_id}"
        enabled_sensor_keys.add(sensor_key)
        selected = mapping_by_key.get(sensor_key)
        if selected is None:
            raise ValueError(
                f"Calibration selection does not map enabled sensor {sensor_key}"
            )
        profile_id = selected.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError(
                f"Calibration selection profile ID is invalid for {sensor_key}"
            )
        if sensor.calibration_profile_id != profile_id:
            raise ValueError(
                f"Run config sensor {sensor_key} is not bound to profile {profile_id}"
            )
        folder = sensor_folder_name(sensor.sensor_type, sensor.device_id)
        if folder in result:
            raise ValueError(f"Run config repeats capture folder {folder}")
        result[folder] = profile_id
    if set(mapping_by_key) != enabled_sensor_keys:
        raise ValueError(
            "Calibration selection does not map the current enabled sensors exactly"
        )
    return result


def _selected_for_library(run_root: Path) -> dict[str, Any] | None:
    try:
        selected = verify_calibration_profile_selection(run_root)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        if len(detail) > 300:
            detail = detail[:297] + "..."
        return {
            "valid": False,
            "issues": [
                _issue(
                    "invalid_current_selection",
                    f"Current calibration selection is invalid: {detail}",
                )
            ],
        }
    return {**selected, "valid": True, "issues": []}


def list_calibration_library(run_root: str | Path) -> dict[str, Any]:
    """List promoted calibration artifacts and compatibility for one destination."""

    from posetestbot.web.security import web_run_roots

    destination = _resolve_direct_run(run_root, must_exist=False)
    setup = _normalize_setup(destination, require_available=False)
    records: list[dict[str, Any]] = []
    for allowed_root in web_run_roots():
        if not allowed_root.is_dir():
            continue
        try:
            children = sorted(allowed_root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
                source = child.resolve()
                if source == destination or source.parent != allowed_root.resolve():
                    continue
                if not any(
                    os.path.lexists(source / filename)
                    for filename in (
                        CALIBRATION_PROFILES,
                        INTRINSIC_CALIBRATION_PROFILES,
                    )
                ):
                    continue
                record, _bundle = _inspect_source(source, setup)
                records.append(record)
            except OSError:
                continue
    records.sort(key=lambda item: (item["source_run_name"], item["source_run_root"]))
    return {
        "schema_version": LIBRARY_SCHEMA_VERSION,
        "run_root": destination.as_posix(),
        "selected": _selected_for_library(destination),
        "replacement_blockers": calibration_selection_replacement_blockers(destination),
        "calibrations": records,
    }


def _ensure_snapshot_parent(run_root: Path) -> Path:
    current = run_root
    for part in SNAPSHOT_PARENT.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"Calibration snapshot directory must not be a symlink: {current}"
            )
        if current.exists():
            if not current.is_dir():
                raise ValueError(
                    f"Calibration snapshot path must be a directory: {current}"
                )
        else:
            current.mkdir()
        try:
            current.resolve().relative_to(run_root.resolve())
        except ValueError as exc:
            raise ValueError("Calibration snapshot directory escapes the run") from exc
    return current


def _write_snapshot(run_root: Path, bundle: _LoadedBundle) -> tuple[Path, Path]:
    parent = _ensure_snapshot_parent(run_root)
    destination = parent / bundle.bundle_sha256
    calibration_path = destination / CALIBRATION_PROFILES
    intrinsic_path = destination / INTRINSIC_CALIBRATION_PROFILES
    if destination.is_symlink():
        raise ValueError("Calibration snapshot bundle directory must not be a symlink")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("Calibration snapshot bundle path must be a directory")
        if _read_regular_file(calibration_path) != bundle.calibration_bytes:
            raise ValueError(
                "Existing calibration snapshot bytes do not match bundle hash"
            )
        if _read_regular_file(intrinsic_path) != bundle.intrinsic_bytes:
            raise ValueError(
                "Existing intrinsic snapshot bytes do not match bundle hash"
            )
        os.chmod(calibration_path, 0o444)
        os.chmod(intrinsic_path, 0o444)
        os.chmod(destination, 0o555)
        return calibration_path, intrinsic_path

    staging = parent / f".{bundle.bundle_sha256}.{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        atomic_write_bytes(staging / CALIBRATION_PROFILES, bundle.calibration_bytes)
        atomic_write_bytes(
            staging / INTRINSIC_CALIBRATION_PROFILES, bundle.intrinsic_bytes
        )
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    os.chmod(calibration_path, 0o444)
    os.chmod(intrinsic_path, 0o444)
    os.chmod(destination, 0o555)
    return calibration_path, intrinsic_path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _directory_has_material_files(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.is_dir():
        return False
    try:
        return any(item.is_file() for item in path.rglob("*"))
    except OSError:
        return True


def calibration_selection_replacement_blockers(run_root: str | Path) -> list[str]:
    """Return material capture/derivation paths that make replacement unsafe."""

    root = Path(run_root).resolve()
    candidates = (
        root / RAW_ROBOT_EE_POSES,
        root / MATCH_ROBOT_EE_POSES,
        root / SYNC_REPORT,
        root / SYNC_QUALITY_REPORT,
        root / CAMERA_RECTIFICATION_REPORT,
        root / BLENDERPROC_RENDER_PLAN,
    )
    blockers = [
        path.relative_to(root).as_posix() for path in candidates if path.is_file()
    ]
    material_directories = (
        root / PROCESSED_DIR / SYNCHRONIZED_DIR,
        root / PROCESSED_DIR / "rectified",
        root / BOP_DIR,
    )
    blockers.extend(
        path.relative_to(root).as_posix()
        for path in material_directories
        if _directory_has_material_files(path)
    )
    try:
        children = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if child.is_symlink():
            blockers.append(child.relative_to(root).as_posix())
            continue
        if not child.is_dir() or child.name == PROCESSED_DIR:
            continue
        if any(
            _directory_has_material_files(child / folder)
            for folder in (RGB_DIR, DEPTH_DIR, MASKS_DIR)
        ):
            blockers.append(child.relative_to(root).as_posix())
    return sorted(set(blockers))


def _selection_response(
    selection: Mapping[str, Any],
    *,
    idempotent: bool,
) -> dict[str, Any]:
    snapshot = selection["snapshot"]
    calibration_relative = str(snapshot["calibration_profiles"]["relative_path"])
    intrinsic_relative = str(
        snapshot["intrinsic_calibration_profiles"]["relative_path"]
    )
    mapping = [dict(item) for item in selection["sensor_profile_mapping"]]
    sensor_profiles = dict(selection["sensor_profiles"])
    return {
        "schema_version": str(selection["schema_version"]),
        "selection": dict(selection),
        "calibration_profiles": calibration_relative,
        "intrinsic_calibration_profiles": intrinsic_relative,
        "sensor_profile_mapping": mapping,
        "sensor_profiles": sensor_profiles,
        "processing_inputs": {
            "calibration_profiles": calibration_relative,
            "intrinsic_calibration_profiles": intrinsic_relative,
        },
        "idempotent": idempotent,
    }


def select_calibration_profile_snapshot(
    run_root: str | Path,
    *,
    source_run_root: str | Path,
    expected_bundle_sha256: str,
    sensors: Any = None,
    resolution: Any = None,
    operator: Any = None,
    expected_current_bundle_sha256: Any = None,
    confirm_replace: Any = False,
) -> dict[str, Any]:
    """Validate a promoted source and snapshot both profile collections."""

    destination = _resolve_direct_run(run_root, must_exist=False)
    setup = _normalize_setup(
        destination,
        sensors=sensors,
        resolution=resolution,
        require_available=True,
    )
    assert setup is not None
    sensor_keys = sorted(
        f"{sensor['sensor_type']}:{sensor['device_id']}"
        for sensor in setup["sensors"]
        if sensor.get("enabled", True) is True
    )
    return select_calibration_profile_composite_snapshot(
        run_root,
        source_selections=[
            {
                "source_run_root": str(source_run_root),
                "expected_bundle_sha256": expected_bundle_sha256,
                "sensor_keys": sensor_keys,
            }
        ],
        sensors=sensors,
        resolution=resolution,
        operator=operator,
        expected_current_bundle_sha256=expected_current_bundle_sha256,
        confirm_replace=confirm_replace,
    )


def select_calibration_profile_composite_snapshot(
    run_root: str | Path,
    *,
    source_selections: Any,
    sensors: Any = None,
    resolution: Any = None,
    operator: Any = None,
    expected_current_bundle_sha256: Any = None,
    confirm_replace: Any = False,
) -> dict[str, Any]:
    """Compose an immutable destination bundle from explicit per-sensor sources."""

    destination = _resolve_direct_run(run_root, must_exist=False)
    if not isinstance(source_selections, list | tuple) or not source_selections:
        raise ValueError("source_selections must be a non-empty list")
    if operator is None:
        operator = "web_operator"
    if (
        not isinstance(operator, str)
        or not operator.strip()
        or len(operator.strip()) > 200
    ):
        raise ValueError(
            "operator must be a non-empty string of at most 200 characters"
        )
    if expected_current_bundle_sha256 is not None and (
        not isinstance(expected_current_bundle_sha256, str)
        or len(expected_current_bundle_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_current_bundle_sha256
        )
    ):
        raise ValueError(
            "expected_current_bundle_sha256 must be a lowercase SHA-256 digest or null"
        )
    if not isinstance(confirm_replace, bool):
        raise ValueError("confirm_replace must be a literal JSON boolean")

    setup = _normalize_setup(
        destination,
        sensors=sensors,
        resolution=resolution,
        require_available=True,
    )
    assert setup is not None
    enabled_sensors = [
        sensor for sensor in setup["sensors"] if sensor.get("enabled", True) is True
    ]
    sensors_by_key = {
        f"{sensor['sensor_type']}:{sensor['device_id']}": sensor
        for sensor in enabled_sensors
    }

    specifications: list[dict[str, Any]] = []
    sources_seen: set[Path] = set()
    assigned_sources: dict[str, Path] = {}
    for index, raw in enumerate(source_selections):
        if not isinstance(raw, Mapping):
            raise ValueError(f"source_selections[{index}] must be a JSON object")
        source_run_root = raw.get("source_run_root")
        if not isinstance(source_run_root, str) or not source_run_root.strip():
            raise ValueError(f"source_selections[{index}].source_run_root is required")
        source = _resolve_direct_run(source_run_root, must_exist=True)
        if source == destination:
            raise ValueError("Calibration sources must be different from run_root")
        if source in sources_seen:
            raise ValueError("Each calibration source run may appear only once")
        sources_seen.add(source)
        expected_digest = raw.get("expected_bundle_sha256")
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise ValueError(
                f"source_selections[{index}].expected_bundle_sha256 must be a lowercase SHA-256 digest"
            )
        sensor_keys = raw.get("sensor_keys")
        if (
            not isinstance(sensor_keys, list | tuple)
            or not sensor_keys
            or any(not isinstance(key, str) or not key for key in sensor_keys)
        ):
            raise ValueError(
                f"source_selections[{index}].sensor_keys must be a non-empty list"
            )
        if len(sensor_keys) != len(set(sensor_keys)):
            raise ValueError(
                f"source_selections[{index}].sensor_keys contains duplicates"
            )
        for sensor_key in sensor_keys:
            if sensor_key not in sensors_by_key:
                raise ValueError(
                    f"Calibration source assignment references an unknown or disabled sensor: {sensor_key}"
                )
            if sensor_key in assigned_sources:
                raise ValueError(
                    f"More than one calibration source is assigned to {sensor_key}"
                )
            assigned_sources[sensor_key] = source
        specifications.append(
            {
                "source": source,
                "expected_bundle_sha256": expected_digest,
                "sensor_keys": list(sensor_keys),
            }
        )

    missing_sensor_keys = set(sensors_by_key).difference(assigned_sources)
    if missing_sensor_keys:
        raise CalibrationSelectionConflict(
            "Every enabled camera requires an explicit calibration source",
            [
                _issue(
                    "calibration_source_assignment_missing",
                    f"Select a calibration source for {sensor_key}.",
                    sensor_key=sensor_key,
                )
                for sensor_key in sorted(missing_sensor_keys)
            ],
        )

    loaded_sources: dict[Path, tuple[dict[str, Any], _LoadedBundle]] = {}
    for specification in sorted(
        specifications, key=lambda item: item["source"].as_posix()
    ):
        source = specification["source"]
        with run_config_lock(source):
            record, bundle = _inspect_source(source, setup)
        if bundle is None or not record["valid"]:
            raise CalibrationSelectionConflict(
                "A selected calibration source is invalid",
                record["issues"],
            )
        if bundle.bundle_sha256 != specification["expected_bundle_sha256"]:
            raise CalibrationSelectionConflict(
                "A calibration changed after it was listed; refresh and select it again",
                [
                    _issue(
                        "stale_calibration_bundle",
                        f"The source calibration hashes changed for {source.as_posix()}.",
                    )
                ],
            )
        loaded_sources[source] = (record, bundle)

    selected_profiles: list[CalibrationProfile] = []
    selected_intrinsics: list[Mapping[str, Any]] = []
    requested_mapping: list[dict[str, Any]] = []
    for sensor_key in sorted(sensors_by_key):
        sensor = sensors_by_key[sensor_key]
        source = assigned_sources[sensor_key]
        _record, bundle = loaded_sources[source]
        mapping, issues = _select_profile(
            bundle,
            sensor,
            str(setup["resolution"]),
            expected_sunrise_reference_frame_path=setup.get(
                "robot_pose_sunrise_reference_frame_path"
            ),
        )
        if mapping is None or issues:
            raise CalibrationSelectionConflict(
                f"The selected source cannot calibrate {sensor_key}",
                issues,
            )
        profile = next(
            (
                item
                for item in bundle.profiles
                if item.profile_id == mapping["profile_id"]
            ),
            None,
        )
        intrinsic = next(
            (
                item
                for item in bundle.intrinsic_profiles
                if str(item.get("profile_id")) == mapping["intrinsic_profile_id"]
            ),
            None,
        )
        if profile is None or intrinsic is None:
            raise CalibrationSelectionConflict(
                "A selected calibration source changed internally",
                [
                    _issue(
                        "calibration_source_mapping_invalid",
                        f"The selected profile pair for {sensor_key} is unavailable.",
                        sensor_key=sensor_key,
                    )
                ],
            )
        requested_mapping.append(mapping)
        selected_profiles.append(profile)
        selected_intrinsics.append(intrinsic)

    if len(specifications) == 1:
        bundle = loaded_sources[specifications[0]["source"]][1]
    else:
        try:
            bundle = _bundle_from_selected_profiles(
                selected_profiles, selected_intrinsics
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CalibrationSelectionConflict(
                "The selected calibration profiles cannot form one deterministic bundle",
                [
                    _issue(
                        "composite_calibration_conflict",
                        _safe_error("The selected profile collections conflict", exc),
                    )
                ],
            ) from exc

    requested_mapping_by_key = {item["sensor_key"]: item for item in requested_mapping}
    source_provenance: list[dict[str, Any]] = []
    for specification in sorted(
        specifications, key=lambda item: item["source"].as_posix()
    ):
        source = specification["source"]
        record, source_bundle = loaded_sources[source]
        selected_sensor_keys = sorted(
            sensor_key
            for sensor_key in sensors_by_key
            if assigned_sources[sensor_key] == source
        )
        source_provenance.append(
            {
                "run_root": source.as_posix(),
                "run_name": record["source_run_name"],
                "bundle_sha256": source_bundle.bundle_sha256,
                "selected_sensor_keys": selected_sensor_keys,
                "sensor_profile_mapping": [
                    requested_mapping_by_key[sensor_key]
                    for sensor_key in selected_sensor_keys
                ],
                "calibration_profiles": {
                    "relative_path": CALIBRATION_PROFILES,
                    "sha256": source_bundle.calibration_sha256,
                    "size_bytes": len(source_bundle.calibration_bytes),
                    "schema_version": source_bundle.calibration_schema_version,
                },
                "intrinsic_calibration_profiles": {
                    "relative_path": INTRINSIC_CALIBRATION_PROFILES,
                    "sha256": source_bundle.intrinsic_sha256,
                    "size_bytes": len(source_bundle.intrinsic_bytes),
                    "schema_version": source_bundle.intrinsic_schema_version,
                },
            }
        )

    with run_config_lock(destination) as locked_destination:
        locked_setup = _normalize_setup(
            locked_destination,
            sensors=sensors,
            resolution=resolution,
            require_available=True,
        )
        assert locked_setup is not None
        locked_mapping, locked_issues = _compatibility(bundle, locked_setup)
        if locked_issues:
            raise CalibrationSelectionConflict(
                "The combined calibration became incompatible with the destination camera setup",
                locked_issues,
            )
        locked_mapping_by_key = {item["sensor_key"]: item for item in locked_mapping}
        if locked_mapping_by_key != requested_mapping_by_key:
            raise CalibrationSelectionConflict(
                "The destination camera setup changed while calibration sources were selected",
                [
                    _issue(
                        "stale_destination_camera_setup",
                        "Refresh the calibration library and assign sources again.",
                    )
                ],
            )
        for provenance in source_provenance:
            selected_sensor_keys = provenance["selected_sensor_keys"]
            provenance["sensor_profile_mapping"] = [
                locked_mapping_by_key[sensor_key] for sensor_key in selected_sensor_keys
            ]
        selection_source = {
            "kind": "composite",
            "run_name": (
                "Combined calibration from "
                f"{len(source_provenance)} source run"
                f"{'s' if len(source_provenance) != 1 else ''}"
            ),
            "source_count": len(source_provenance),
            "bundle_sha256": bundle.bundle_sha256,
            **_bundle_source_artifacts(bundle),
        }
        try:
            current = verify_calibration_profile_selection(
                locked_destination,
                verify_run_config=False,
            )
        except FileNotFoundError:
            current = None
        except (OSError, ValueError) as exc:
            raise CalibrationSelectionConflict(
                "The current calibration selection is invalid and cannot be replaced safely",
                [
                    _issue(
                        "invalid_current_selection",
                        _safe_error("Current calibration selection is invalid", exc),
                    )
                ],
            ) from exc
        current_bundle = (
            str(current["source"]["bundle_sha256"]) if current is not None else None
        )
        if current is not None and _selection_matches_intent(
            current,
            schema_version=COMPOSITE_SELECTION_SCHEMA_VERSION,
            setup=locked_setup,
            mapping=locked_mapping,
            source=selection_source,
            sources=source_provenance,
        ):
            return _selection_response(current, idempotent=True)
        if current is None and expected_current_bundle_sha256 is not None:
            raise CalibrationSelectionConflict(
                "The expected current calibration no longer exists",
                [
                    _issue(
                        "current_selection_missing",
                        "No current calibration selection exists for the supplied compare-and-swap hash.",
                    )
                ],
            )
        if current is not None:
            if expected_current_bundle_sha256 != current_bundle:
                raise CalibrationSelectionConflict(
                    "The current calibration changed; refresh before replacing it",
                    [
                        _issue(
                            "stale_current_calibration_bundle",
                            "expected_current_bundle_sha256 does not match the active selection.",
                        )
                    ],
                )
            if confirm_replace is not True:
                raise CalibrationSelectionConflict(
                    "Replacing the active calibration requires explicit confirmation",
                    [
                        _issue(
                            "calibration_replacement_confirmation_required",
                            "Set confirm_replace to literal true after reviewing the replacement.",
                        )
                    ],
                )
            blockers = calibration_selection_replacement_blockers(locked_destination)
            if blockers:
                raise CalibrationSelectionConflict(
                    "Calibration cannot be replaced after capture or derived dataset material exists",
                    [
                        {
                            "code": "calibration_replacement_blocked",
                            "message": (
                                "Create a new run to use another calibration; this run already "
                                "contains capture or derived dataset material."
                            ),
                            "blockers": blockers,
                        }
                    ],
                )

        calibration_path, intrinsic_path = _write_snapshot(locked_destination, bundle)
        calibration_relative = _relative(calibration_path, locked_destination)
        intrinsic_relative = _relative(intrinsic_path, locked_destination)
        selected_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        mapping = list(locked_mapping)
        sensor_profiles = {item["sensor_key"]: item["profile_id"] for item in mapping}
        selection = {
            "schema_version": COMPOSITE_SELECTION_SCHEMA_VERSION,
            "selected_at": selected_at,
            "operator": operator.strip(),
            "source": selection_source,
            "sources": source_provenance,
            "snapshot": {
                "directory": _relative(calibration_path.parent, locked_destination),
                "calibration_profiles": {
                    "relative_path": calibration_relative,
                    "sha256": bundle.calibration_sha256,
                    "size_bytes": len(bundle.calibration_bytes),
                    "schema_version": bundle.calibration_schema_version,
                },
                "intrinsic_calibration_profiles": {
                    "relative_path": intrinsic_relative,
                    "sha256": bundle.intrinsic_sha256,
                    "size_bytes": len(bundle.intrinsic_bytes),
                    "schema_version": bundle.intrinsic_schema_version,
                },
            },
            "intended_setup": locked_setup,
            "sensor_profile_mapping": mapping,
            "sensor_profiles": sensor_profiles,
        }
        atomic_write_json(_selection_path(locked_destination), selection)
    return _selection_response(selection, idempotent=False)


def selected_calibration_run_config_defaults(
    run_root: str | Path,
    *,
    sensors: Sequence[SensorRunConfig],
    resolution: str,
    requested_calibration_profiles: str | None,
    infer_when_omitted: bool,
    expected_bundle_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a verified selection when saving the destination run config."""

    root = Path(run_root).resolve()
    requested_is_managed_snapshot = False
    if requested_calibration_profiles:
        requested_path = Path(requested_calibration_profiles)
        if not requested_path.is_absolute():
            requested_path = root / requested_path
        try:
            requested_relative = requested_path.resolve().relative_to(root)
            requested_is_managed_snapshot = (
                requested_relative.parts[: len(SNAPSHOT_PARENT.parts)]
                == SNAPSHOT_PARENT.parts
            )
        except ValueError:
            requested_is_managed_snapshot = False
    try:
        selection = verify_calibration_profile_selection(
            root,
            expected_bundle_sha256=expected_bundle_sha256,
            verify_run_config=False,
        )
    except FileNotFoundError:
        if expected_bundle_sha256 is not None or requested_is_managed_snapshot:
            raise CalibrationSelectionConflict(
                "The expected calibration selection no longer exists",
                [
                    _issue(
                        "current_selection_missing",
                        "Select the calibration again before saving the run config.",
                    )
                ],
            )
        return None
    except ValueError as exc:
        if expected_bundle_sha256 is not None:
            raise CalibrationSelectionConflict(
                "The selected calibration changed before the run config was saved",
                [
                    _issue(
                        "stale_calibration_selection",
                        _safe_error("Calibration selection verification failed", exc),
                    )
                ],
            ) from exc
        raise
    snapshot = selection["snapshot"]
    calibration_relative = str(snapshot["calibration_profiles"]["relative_path"])
    intrinsic_relative = str(
        snapshot["intrinsic_calibration_profiles"]["relative_path"]
    )
    selected_path = (root / calibration_relative).resolve()
    if requested_calibration_profiles:
        requested_path = Path(requested_calibration_profiles)
        if not requested_path.is_absolute():
            requested_path = root / requested_path
        if requested_path.resolve() != selected_path:
            if expected_bundle_sha256 is not None or requested_is_managed_snapshot:
                raise CalibrationSelectionConflict(
                    "The requested calibration path is not the expected selection",
                    [
                        _issue(
                            "calibration_selection_path_mismatch",
                            "Save the immutable path returned by the calibration selection request.",
                        )
                    ],
                )
            return None
    elif not infer_when_omitted:
        if expected_bundle_sha256 is not None:
            raise CalibrationSelectionConflict(
                "The expected calibration snapshot path was omitted",
                [
                    _issue(
                        "calibration_selection_path_missing",
                        "Send the calibration_profiles path returned by calibration selection.",
                    )
                ],
            )
        return None

    bundle = _load_bundle(root / Path(calibration_relative).parent)
    setup = {
        "resolution": resolution,
        "sensors": [sensor.to_dict() for sensor in sensors],
        "robot_pose_sunrise_reference_frame_path": (POSE_TEMPLATE_BASE_SUNRISE_PATH),
    }
    if _setup_identity(selection.get("intended_setup")) != _setup_identity(setup):
        raise CalibrationSelectionConflict(
            "The saved camera setup no longer matches the selected calibration",
            [
                _issue(
                    "calibration_selection_setup_mismatch",
                    "Reselect calibration for the exact enabled cameras, mounting modes, orientation, and resolution before saving.",
                )
            ],
        )
    mapping, issues = _compatibility(bundle, setup)
    if issues:
        raise ValueError(
            "Selected calibration is incompatible with the saved camera setup: "
            + "; ".join(issue["message"] for issue in issues)
        )
    if _mapping_identity(selection.get("sensor_profile_mapping")) != _mapping_identity(
        mapping
    ):
        raise CalibrationSelectionConflict(
            "The saved camera-to-profile mapping no longer matches the selection",
            [
                _issue(
                    "calibration_selection_mapping_mismatch",
                    "Reselect calibration so every enabled camera is bound to the reviewed profile.",
                )
            ],
        )
    return {
        "calibration_profiles": calibration_relative,
        "intrinsic_calibration_profiles": intrinsic_relative,
        "calibration_profile_selection": {
            "selection_artifact": CALIBRATION_PROFILE_SELECTION,
            "bundle_sha256": selection["source"]["bundle_sha256"],
            "selected_at": selection["selected_at"],
        },
        "sensor_profile_mapping": mapping,
    }
