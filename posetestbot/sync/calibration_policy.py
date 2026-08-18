"""Resolve hash-bound synchronization policy from a selected calibration.

The resolver binds current promoted calibration timing to dataset sync.
When a run config has no explicit ``calibration_profile_selection`` pointer it
returns ``None`` and does not infer profile use from files that happen to exist.
When a selection is present, every enabled sensor and every timing field is
validated against the immutable run-owned snapshot before any policy is
returned.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from posetestbot.calibration.profile_library import (
    verify_calibration_profile_selection,
)
from posetestbot.calibration.profiles import (
    SCHEMA_VERSION as CALIBRATION_SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationStatus,
    profile_from_dict,
    validate_profile_collection,
)
from posetestbot.io.artifacts import CALIBRATION_PROFILE_SELECTION
from posetestbot.pipeline.run_config import (
    load_run_config_for_run_root,
    normalize_inverted,
    normalize_mounting_mode,
    normalize_sensor_enabled,
    normalize_sensor_type,
)
from posetestbot.sensors.registry import sensor_folder_name
from posetestbot.sync.non_destructive import (
    resolve_max_nearest_pose_delta_ms,
    resolve_timestamp_pair,
)


SCHEMA_VERSION = "calibration_sync_policy.v1"
SUPPORTED_OFFSET_POLICIES = frozenset({"auto_offset", "fixed_zero"})
AUTO_OFFSET_STATUSES = frozenset({"applied", "kept_zero"})


def _finite_float(value: Any, *, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _provenance_path(value: Any, *, label: str) -> str:
    text = _required_text(value, label=label)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be a run-relative path")
    if path.as_posix() != text or "." in path.parts:
        raise ValueError(f"{label} must use one canonical run-relative path")
    return text


def _sensor_identity(sensor: Mapping[str, Any]) -> tuple[str, str, str]:
    sensor_type = normalize_sensor_type(str(sensor.get("sensor_type", ""))).value
    device_id = _required_text(
        sensor.get("device_id"),
        label="Run config sensor device_id",
    )
    return sensor_type, device_id, f"{sensor_type}:{device_id}"


def _enabled_sensor_map(
    sensors: Any,
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(sensors, Sequence) or isinstance(sensors, (str, bytes)):
        raise ValueError(f"{label} sensors must be a list")
    enabled: dict[str, dict[str, Any]] = {}
    folders: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for index, raw in enumerate(sensors):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} sensor {index} must be an object")
        sensor_type, device_id, sensor_key = _sensor_identity(raw)
        identity = (sensor_type, device_id)
        if identity in identities:
            raise ValueError(f"{label} contains duplicate sensor identity {sensor_key}")
        identities.add(identity)
        if not normalize_sensor_enabled(raw.get("enabled", True)):
            continue
        folder = sensor_folder_name(sensor_type, device_id)
        if folder in folders:
            raise ValueError(f"{label} contains duplicate sensor folder {folder}")
        folders.add(folder)
        enabled[sensor_key] = {
            "sensor_key": sensor_key,
            "sensor_type": sensor_type,
            "device_id": device_id,
            "mounting_mode": normalize_mounting_mode(
                str(raw.get("mounting_mode", ""))
            ).value,
            "inverted": normalize_inverted(raw.get("inverted", False)),
            "calibration_profile_id": raw.get("calibration_profile_id"),
            "sensor_folder": folder,
        }
    if not enabled:
        raise ValueError(f"{label} must contain at least one enabled sensor")
    return enabled


def _load_hash_bound_profiles(
    run_root: Path,
    selection: Mapping[str, Any],
) -> list[CalibrationProfile]:
    artifact = selection["snapshot"]["calibration_profiles"]
    relative_path = _provenance_path(
        artifact.get("relative_path"),
        label="Calibration snapshot profile path",
    )
    path = run_root / relative_path
    payload = path.read_bytes()
    observed_sha256 = hashlib.sha256(payload).hexdigest()
    if observed_sha256 != artifact.get("sha256"):
        raise ValueError("Calibration snapshot profile hash changed during resolution")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Calibration snapshot profiles contain invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("Calibration snapshot profile collection must be an object")
    if value.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        raise ValueError(
            "Calibration snapshot profile collection schema is unsupported"
        )
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("Calibration snapshot profiles must be a list")
    profiles = [
        profile_from_dict(item) for item in raw_profiles if isinstance(item, Mapping)
    ]
    if len(profiles) != len(raw_profiles):
        raise ValueError("Calibration snapshot contains a non-object profile")
    validate_profile_collection(profiles)
    return profiles


def _validate_current_setup(
    config: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Mapping[str, Any]]]:
    capture = config.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("Run config capture settings are missing")
    current = _enabled_sensor_map(
        capture.get("sensors"),
        label="Current run config",
    )
    intended_setup = selection.get("intended_setup")
    if not isinstance(intended_setup, Mapping):
        raise ValueError("Calibration selection intended setup is missing")
    if intended_setup.get("resolution") != capture.get("resolution"):
        raise ValueError(
            "Current run resolution no longer matches the calibration selection"
        )
    intended = _enabled_sensor_map(
        intended_setup.get("sensors"),
        label="Calibration selection intended setup",
    )
    if set(current) != set(intended):
        raise ValueError(
            "Current enabled sensors no longer match the calibration selection"
        )
    for sensor_key in current:
        for field in ("sensor_type", "device_id", "mounting_mode", "inverted"):
            if current[sensor_key][field] != intended[sensor_key][field]:
                raise ValueError(
                    f"Current sensor {sensor_key} changed {field} after calibration "
                    "selection"
                )

    raw_mapping = selection.get("sensor_profile_mapping")
    if not isinstance(raw_mapping, list):
        raise ValueError("Calibration selection sensor profile mapping is missing")
    mapping: dict[str, Mapping[str, Any]] = {}
    for item in raw_mapping:
        if not isinstance(item, Mapping):
            raise ValueError(
                "Calibration selection sensor mapping must contain objects"
            )
        sensor_key = _required_text(
            item.get("sensor_key"),
            label="Calibration selection mapping sensor_key",
        )
        if sensor_key in mapping:
            raise ValueError(
                f"Calibration selection duplicates sensor mapping {sensor_key}"
            )
        mapping[sensor_key] = item
    if set(mapping) != set(current):
        raise ValueError(
            "Calibration selection does not map every current enabled sensor exactly"
        )
    return current, mapping


def _profile_timing_policy(
    profile: CalibrationProfile,
    *,
    sensor: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    sensor_key = str(sensor["sensor_key"])
    if profile.status != CalibrationStatus.VALID:
        raise ValueError(f"Selected calibration profile is not valid for {sensor_key}")
    if (
        profile.sensor_type.value != sensor["sensor_type"]
        or profile.sensor_id != sensor["device_id"]
        or profile.mounting_mode.value != sensor["mounting_mode"]
    ):
        raise ValueError(
            f"Selected calibration profile identity is inconsistent for {sensor_key}"
        )
    if (
        mapping.get("sensor_type") != sensor["sensor_type"]
        or str(mapping.get("device_id")) != sensor["device_id"]
        or mapping.get("mounting_mode") != sensor["mounting_mode"]
        or mapping.get("profile_id") != profile.profile_id
    ):
        raise ValueError(
            f"Calibration selection mapping is inconsistent for {sensor_key}"
        )

    sync_delta_ms = _finite_float(
        profile.sync_delta_ms,
        label=f"Selected profile {profile.profile_id} sync_delta_ms",
    )
    synchronization = profile.metadata.get("synchronization")
    if not isinstance(synchronization, Mapping):
        raise ValueError(
            f"Selected profile {profile.profile_id} lacks synchronization provenance"
        )
    metadata_delta_ms = _finite_float(
        synchronization.get("sync_delta_ms"),
        label=(
            f"Selected profile {profile.profile_id} "
            "metadata.synchronization.sync_delta_ms"
        ),
    )
    if not math.isclose(
        sync_delta_ms,
        metadata_delta_ms,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError(
            f"Selected profile {profile.profile_id} has contradictory sync deltas"
        )
    if synchronization.get("sensor_key") != sensor_key:
        raise ValueError(
            f"Selected profile {profile.profile_id} timing sensor identity changed"
        )
    metadata_sensor_key = profile.metadata.get("sensor_key")
    if metadata_sensor_key is not None and metadata_sensor_key != sensor_key:
        raise ValueError(
            f"Selected profile {profile.profile_id} metadata sensor identity changed"
        )
    sensor_name = _required_text(
        profile.metadata.get("sensor_name"),
        label=f"Selected profile {profile.profile_id} sensor_name",
    )
    if sensor_name != sensor["sensor_folder"]:
        raise ValueError(
            f"Selected profile {profile.profile_id} sensor folder identity changed"
        )

    timestamp_source = _required_text(
        synchronization.get("timestamp_source"),
        label=f"Selected profile {profile.profile_id} timestamp_source",
    )
    frame_timestamp_source = _required_text(
        synchronization.get("frame_timestamp_source"),
        label=f"Selected profile {profile.profile_id} frame_timestamp_source",
    )
    if timestamp_source != frame_timestamp_source:
        raise ValueError(
            f"Selected profile {profile.profile_id} has contradictory frame "
            "timestamp sources"
        )
    robot_timestamp_source = _required_text(
        synchronization.get("robot_timestamp_source"),
        label=f"Selected profile {profile.profile_id} robot_timestamp_source",
    )
    timestamp_source, robot_timestamp_source = resolve_timestamp_pair(
        frame_timestamp_source,
        robot_timestamp_source,
    )
    max_nearest_pose_delta_ms = resolve_max_nearest_pose_delta_ms(
        synchronization.get("max_nearest_pose_delta_ms")
    )
    if max_nearest_pose_delta_ms is None:
        raise ValueError(
            f"Selected profile {profile.profile_id} lacks max-nearest-pose timing"
        )
    if synchronization.get("timestamp_fallback_allowed") is not False:
        raise ValueError(
            f"Selected profile {profile.profile_id} must forbid timestamp fallback"
        )
    required_domain = synchronization.get("required_frame_timestamp_domain")
    if required_domain is not None and (
        not isinstance(required_domain, str) or not required_domain.strip()
    ):
        raise ValueError(
            f"Selected profile {profile.profile_id} timestamp domain is invalid"
        )
    if timestamp_source == "sensor" and required_domain is None:
        raise ValueError(
            f"Selected profile {profile.profile_id} lacks its sensor timestamp domain"
        )
    _provenance_path(
        synchronization.get("quality_report"),
        label=f"Selected profile {profile.profile_id} timing quality report",
    )

    offset_policy = synchronization.get("policy")
    offset_status = synchronization.get("status")
    robot_pose_time_offset_ms = synchronization.get("robot_pose_time_offset_ms")
    if offset_policy not in SUPPORTED_OFFSET_POLICIES:
        raise ValueError(
            f"Selected profile {profile.profile_id} has invalid offset policy"
        )
    valid_statuses = (
        AUTO_OFFSET_STATUSES
        if offset_policy == "auto_offset"
        else frozenset({"fixed_zero"})
    )
    if offset_status not in valid_statuses:
        raise ValueError(
            f"Selected profile {profile.profile_id} has invalid offset status"
        )
    robot_pose_time_offset_ms = _finite_float(
        robot_pose_time_offset_ms,
        label=(f"Selected profile {profile.profile_id} robot_pose_time_offset_ms"),
    )
    if not math.isclose(
        sync_delta_ms,
        -robot_pose_time_offset_ms,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError(
            f"Selected profile {profile.profile_id} has contradictory offset signs"
        )
    offset_is_zero = math.isclose(
        robot_pose_time_offset_ms,
        0.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    if (
        (offset_policy == "fixed_zero" and not offset_is_zero)
        or (offset_status in {"fixed_zero", "kept_zero"} and not offset_is_zero)
        or (offset_status == "applied" and offset_is_zero)
    ):
        raise ValueError(
            f"Selected profile {profile.profile_id} offset decision is contradictory"
        )
    offset_source = _provenance_path(
        synchronization.get("source"),
        label=f"Selected profile {profile.profile_id} offset source",
    )
    auto_estimated = synchronization.get("auto_estimated_per_sensor_offset")
    if not isinstance(auto_estimated, bool) or auto_estimated != (
        offset_policy == "auto_offset"
    ):
        raise ValueError(
            f"Selected profile {profile.profile_id} offset policy provenance "
            "is inconsistent"
        )
    promotion = profile.metadata.get("promotion_synchronization_provenance")
    if promotion is not None:
        if not isinstance(promotion, Mapping):
            raise ValueError(
                f"Selected profile {profile.profile_id} promotion timing "
                "provenance is invalid"
            )
        if (
            promotion.get("source") != offset_source
            or promotion.get("status") != offset_status
            or not math.isclose(
                _finite_float(
                    promotion.get("sync_delta_ms"),
                    label=(
                        f"Selected profile {profile.profile_id} promoted sync_delta_ms"
                    ),
                ),
                sync_delta_ms,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or not math.isclose(
                _finite_float(
                    promotion.get("robot_pose_time_offset_ms"),
                    label=(
                        f"Selected profile {profile.profile_id} promoted "
                        "robot_pose_time_offset_ms"
                    ),
                ),
                robot_pose_time_offset_ms,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise ValueError(
                f"Selected profile {profile.profile_id} promotion timing "
                "provenance is inconsistent"
            )

    return {
        "sensor_key": sensor_key,
        "sensor_name": sensor_name,
        "sensor_folder": sensor["sensor_folder"],
        "sensor_type": sensor["sensor_type"],
        "device_id": sensor["device_id"],
        "profile_id": profile.profile_id,
        "robot_pose_time_offset_ms": robot_pose_time_offset_ms,
        "sync_delta_ms": sync_delta_ms,
        "frame_timestamp_source": timestamp_source,
        "robot_timestamp_source": robot_timestamp_source,
        "required_frame_timestamp_domain": required_domain,
        "timestamp_fallback_allowed": False,
        "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
        "timing_source": offset_source,
        "timing_policy": offset_policy,
        "timing_status": offset_status,
    }


def resolve_calibration_profile_sync_policy(
    run_root: str | Path,
) -> dict[str, Any] | None:
    """Resolve exact per-sensor synchronization from an explicit selection.

    ``None`` means the run config did not request a managed calibration
    selection. A present pointer is never ignored or downgraded.
    """

    root = Path(run_root).resolve()
    config = load_run_config_for_run_root(root)
    pointer = config.get("calibration_profile_selection")
    if pointer is None:
        return None
    if not isinstance(pointer, Mapping):
        raise ValueError("Run config calibration profile selection is invalid")
    selection = verify_calibration_profile_selection(
        root,
        expected_calibration_profiles=config.get("calibration_profiles"),
        expected_intrinsic_calibration_profiles=config.get(
            "intrinsic_calibration_profiles"
        ),
        expected_bundle_sha256=str(pointer.get("bundle_sha256", "")),
        verify_run_config=False,
    )
    # Preserve the established, sensor-specific drift diagnostics before the
    # broader run-config binding check also compares robot-pose provenance.
    current, mapping = _validate_current_setup(config, selection)
    selection = verify_calibration_profile_selection(
        root,
        expected_calibration_profiles=config.get("calibration_profiles"),
        expected_intrinsic_calibration_profiles=config.get(
            "intrinsic_calibration_profiles"
        ),
        expected_bundle_sha256=str(pointer.get("bundle_sha256", "")),
        verify_run_config=True,
    )
    profiles = _load_hash_bound_profiles(root, selection)
    profiles_by_id = {profile.profile_id: profile for profile in profiles}

    sensors: list[dict[str, Any]] = []
    for sensor_key in sorted(current):
        selected_profile_id = current[sensor_key].get("calibration_profile_id")
        if not isinstance(selected_profile_id, str) or not selected_profile_id:
            raise ValueError(
                f"Current enabled sensor {sensor_key} lacks a selected profile"
            )
        if mapping[sensor_key].get("profile_id") != selected_profile_id:
            raise ValueError(
                f"Current enabled sensor {sensor_key} profile selection is stale"
            )
        profile = profiles_by_id.get(selected_profile_id)
        if profile is None:
            raise ValueError(
                f"Selected profile {selected_profile_id!r} is missing for {sensor_key}"
            )
        sensors.append(
            _profile_timing_policy(
                profile,
                sensor=current[sensor_key],
                mapping=mapping[sensor_key],
            )
        )

    snapshot = selection["snapshot"]["calibration_profiles"]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "selected_calibration_profile",
        "selection_artifact": CALIBRATION_PROFILE_SELECTION,
        "bundle_sha256": selection["source"]["bundle_sha256"],
        "calibration_profiles": {
            "relative_path": snapshot["relative_path"],
            "sha256": snapshot["sha256"],
        },
        "sensors": sensors,
    }
