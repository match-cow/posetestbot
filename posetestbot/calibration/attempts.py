"""Intent-level, immutable calibration attempts and explicit promotion."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from posetestbot.aruco.grid import _matched_points, detect_sensor_folder
from posetestbot.calibration.attempt_solver import (
    DEFAULT_MAX_MEAN_ROTATION_DEG,
    DEFAULT_MAX_MEAN_TRANSLATION_MM,
    DEFAULT_MAX_OUTLIER_RATIO,
    DEFAULT_MAX_PNP_ALL_POINT_MEAN_ERROR_PX,
    DEFAULT_MAX_OBSERVATIONS_PER_MOTION,
    DEFAULT_IMAGE_COVERAGE_TAIL_SUPPORT_VIEWS,
    DEFAULT_MIN_INLIERS,
    DEFAULT_MIN_IMAGE_CENTROID_HULL_AREA_RATIO,
    DEFAULT_MIN_IMAGE_CENTROID_X_SPAN_RATIO,
    DEFAULT_MIN_IMAGE_CENTROID_Y_SPAN_RATIO,
    DEFAULT_MIN_PNP_COMMON_INLIERS,
    DEFAULT_MIN_PNP_COMMON_INLIER_RATIO,
    DEFAULT_MIN_PNP_CLUTTER_GRID_COLUMNS,
    DEFAULT_MIN_PNP_CLUTTER_GRID_ROWS,
    DEFAULT_MIN_PNP_CLUTTER_SUPPORTED_MARKERS,
    DEFAULT_MIN_PNP_GRID_COLUMNS,
    DEFAULT_MIN_PNP_GRID_ROWS,
    DEFAULT_MIN_PNP_SUPPORTED_CORNERS_PER_MARKER,
    DEFAULT_MIN_PNP_SUPPORTED_MARKERS,
    DEFAULT_MIN_ROTATION_AXIS_ANGLE_DEG,
    DEFAULT_MIN_ROTATION_AXIS_SINGULAR_RATIO,
    DEFAULT_STATIC_MIN_IMAGE_CENTROID_HULL_AREA_RATIO,
    DEFAULT_STATIC_MIN_IMAGE_CENTROID_X_SPAN_RATIO,
    DEFAULT_STATIC_MIN_IMAGE_CENTROID_Y_SPAN_RATIO,
    EXTRINSIC_METHOD_ORDER,
    PNP_METHOD_ORDER,
    evaluate_extrinsic_candidate,
    rank_candidates,
    solve_planar_pnp_candidates,
    transform_from_record,
    transform_residual,
)
from posetestbot.calibration.intrinsics import (
    DEFAULT_MAX_RMS_PX,
    DEFAULT_MAX_VIEW_ERROR_PX,
    DEFAULT_MIN_ACCEPTED_VIEWS,
    DEFAULT_MIN_COVERAGE_CELLS,
    IntrinsicCalibrationError,
    _view_points,
    calibrate_intrinsic_profile,
    factory_intrinsic_profile,
    load_intrinsic_profile_collection,
    projection_is_opencv_compatible,
    select_intrinsic_profile,
    sensor_intrinsic_identity,
    write_intrinsic_profile_collection,
)
from posetestbot.calibration.profiles import (
    SCHEMA_VERSION as PROFILE_SCHEMA_VERSION,
    CalibrationProfile,
    CalibrationQuality,
    CalibrationStatus,
    CalibrationTargetType,
    RigidTransform,
    TransformFrame,
    load_profile_collection,
    profile_to_dict,
    rectified_intrinsics_from_native,
    write_profile_collection,
)
from posetestbot.calibration.target_library import (
    LIBRARY_DIRECTORY,
    CalibrationTargetConflict,
    default_target_library_root,
    list_target_bundles,
    replacement_blockers,
    validate_run_target_selection,
    validate_target_bundle,
)
from posetestbot.calibration.targets import (
    normalize_calibration_target_spec,
    opencv_grid_board,
    target_identity,
    validate_target_identity,
)
from posetestbot.calibration.time_offset import (
    DEFAULT_MAX_NEAREST_POSE_DELTA_MS,
    DEFAULT_MAX_LOMO_SEARCH_ADJUSTED_SIGN_P_VALUE,
    DEFAULT_POLICY as DEFAULT_SYNCHRONIZATION_POLICY,
    DEFAULT_REFERENCE_PNP_METHOD,
    DEFAULT_WARNING_NEAREST_POSE_DELTA_MS,
    FAILURE_POLICY_WARN_KEEP_ZERO,
    IMPROVEMENT_EVIDENCE_STRATEGY,
    IMPLEMENTATION_REVISION as TIME_OFFSET_IMPLEMENTATION_REVISION,
    LOMO_CONSISTENCY_STRATEGY,
    POLICIES as SYNCHRONIZATION_POLICIES,
    SCHEMA_VERSION as TIME_OFFSET_SEARCH_SCHEMA_VERSION,
    SUPPORTED_IMPLEMENTATION_REVISIONS as TIME_OFFSET_SUPPORTED_REVISIONS,
    apply_sensor_time_offset,
    estimate_sensor_time_offset,
    failed_sensor_result,
    fixed_zero_sensor_result,
    offset_values as time_offset_values,
    search_configuration as time_offset_search_configuration,
    sign_convention as time_offset_sign_convention,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
    ARUCO_DETECTIONS,
    ARUCO_POSE_ESTIMATION,
    CALIBRATION_CANDIDATES,
    CALIBRATION_OBSERVATIONS,
    CALIBRATION_PROFILES,
    CALIBRATION_PROFILES_FROM_OBSERVATIONS,
    CALIBRATION_PROFILES_SOLVED,
    CALIBRATION_SOLVER_REPORT,
    CALIBRATION_TARGET,
    CALIBRATION_VALIDATION_REPORT,
    DATASET_MANIFEST,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    INTRINSIC_COMPARISON,
    INTRINSIC_CALIBRATION_PROFILES,
    MATCH_ROBOT_EE_POSES,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
    RUN_CONFIG,
    SYNC_QUALITY_REPORT,
    TIME_OFFSET_SEARCH,
)
from posetestbot.io.manifest import (
    discover_sensor_records,
    load_or_create_run_manifest,
    upsert_stage,
)
from posetestbot.pipeline.run_config import (
    load_run_config_for_run_root,
    run_config_lock,
    validate_run_config,
)
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    ROBOT_POSE_REFERENCE_SCHEMA_VERSION,
    configured_sunrise_reference_frame_path,
    robot_pose_reference_evidence,
    verified_sunrise_reference_frame_path,
)
from posetestbot.sensors.contracts import CameraIntrinsics, MountingMode, SensorType
from posetestbot.sync.non_destructive import (
    indexed_robot_poses,
    load_frame_metadata,
    load_robot_poses,
    synchronize_run,
)
from posetestbot.sync.quality import build_sync_quality_report


ATTEMPT_SCHEMA_VERSION = "calibration_attempt.v1"
HISTORICAL_REQUEST_SCHEMA_VERSION = "calibration_attempt_request.v1"
REQUEST_SCHEMA_VERSION = "calibration_attempt_request.v2"
READABLE_REQUEST_SCHEMA_VERSIONS = frozenset(
    {HISTORICAL_REQUEST_SCHEMA_VERSION, REQUEST_SCHEMA_VERSION}
)
PROGRESS_SCHEMA_VERSION = "calibration_attempt_progress.v1"
PROMOTION_SCHEMA_VERSION = "calibration_attempt_promotion.v1"
PROMOTION_REQUEST_SCHEMA_VERSION = "calibration_promotion_request.v1"
ATTEMPT_DIRECTORY = Path("processed") / "calibration"
REQUEST_FILE = "request.json"
PROGRESS_FILE = "progress.json"
PNP_CANDIDATES_FILE = "pnp_candidates.json"
EXTRINSIC_CANDIDATES_FILE = "extrinsic_candidates.json"
RANKING_FILE = "ranking.json"
CHECKS_FILE = "checks.json"
CANDIDATE_PROFILES_FILE = "candidate_profiles.json"
PROMOTION_REQUEST_FILE = "promotion_request.json"
PROMOTION_FILE = "promotion.json"
TARGET_BUNDLE_DIRECTORY = "target_bundle"
ATTEMPT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ATTEMPT_MIN_MOTION_POSES = 4
ATTEMPT_MIN_TRANSLATION_SPAN_MM = 20.0
ATTEMPT_MIN_ROTATION_SPAN_DEG = 5.0
ATTEMPT_MIN_TARGET_MARKER_COVERAGE_RATIO = 0.5
ATTEMPT_MIN_TARGET_ROW_COVERAGE_RATIO = 0.6
ATTEMPT_MIN_TARGET_COLUMN_COVERAGE_RATIO = 0.6
ATTEMPT_SYNC_DELTA_MS = 0.0
ATTEMPT_MAX_NEAREST_POSE_DELTA_MS = DEFAULT_MAX_NEAREST_POSE_DELTA_MS
ATTEMPT_WARNING_NEAREST_POSE_DELTA_MS = DEFAULT_WARNING_NEAREST_POSE_DELTA_MS
ATTEMPT_TIMESTAMP_SOURCE = "sensor"
ATTEMPT_ROBOT_TIMESTAMP_SOURCE = "host_wall"
ATTEMPT_REALSENSE_TIMESTAMP_DOMAIN = "global_time"
ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS = 5
ATTEMPT_INTRINSIC_MAX_TRAINING_VIEWS = 45
ATTEMPT_INTRINSIC_MAX_HOLDOUT_VIEWS = 15
ATTEMPT_INTRINSIC_HOLDOUT_FRACTION = 0.10
ATTEMPT_INTRINSIC_TEMPORAL_GUARD_VIEWS = 5
ATTEMPT_INTRINSIC_DESCRIPTOR_CORNER_SCALE = 0.03
ATTEMPT_INTRINSIC_DESCRIPTOR_GUARD_DISTANCE = 1.0
ATTEMPT_INTRINSIC_MIN_ABSOLUTE_IMPROVEMENT_PX = 0.05
ATTEMPT_INTRINSIC_MIN_RELATIVE_IMPROVEMENT = 0.05
ATTEMPT_INTRINSIC_MAX_FOCAL_DELTA_RATIO = 0.10
ATTEMPT_INTRINSIC_MAX_PRINCIPAL_DELTA_RATIO = 0.05
ATTEMPT_INTRINSIC_MAX_ASPECT_DELTA_RATIO = 0.05
JOINT_RANKING_NUMERIC_DECIMALS = 6
PROMOTION_TRANSFORM_TOLERANCE_MM = 1e-6
# Reconstructing a JSON quaternion into a matrix can introduce roughly
# 2e-6 degrees of acos round-off even when both records describe one transform.
PROMOTION_TRANSFORM_TOLERANCE_DEG = 1e-5
DEFAULT_INTRINSICS_POLICY = "compare_factory_opencv"
INTRINSICS_POLICIES = {
    "compare_factory_opencv": (
        "Compare captured factory intrinsics with a gated OpenCV calibration"
    ),
    "reuse_compatible_or_factory": (
        "Reuse an exact compatible profile, otherwise captured factory intrinsics"
    ),
}
PHASES = (
    ("prepare_data", "Prepare data"),
    ("estimate_target_poses", "Estimate target poses"),
    ("estimate_time_offsets", "Estimate time alignment"),
    ("compare_robot_camera_solutions", "Compare robot-camera solutions"),
    ("validate_and_rank", "Validate and rank"),
)


def _image_coverage_thresholds(mode: str) -> dict[str, float | int]:
    """Return mounting-aware field-of-view diversity thresholds."""

    if mode == "eye_to_hand":
        x_span = DEFAULT_STATIC_MIN_IMAGE_CENTROID_X_SPAN_RATIO
        y_span = DEFAULT_STATIC_MIN_IMAGE_CENTROID_Y_SPAN_RATIO
        hull_area = DEFAULT_STATIC_MIN_IMAGE_CENTROID_HULL_AREA_RATIO
    else:
        x_span = DEFAULT_MIN_IMAGE_CENTROID_X_SPAN_RATIO
        y_span = DEFAULT_MIN_IMAGE_CENTROID_Y_SPAN_RATIO
        hull_area = DEFAULT_MIN_IMAGE_CENTROID_HULL_AREA_RATIO
    return {
        "image_coverage_tail_support_views": (
            DEFAULT_IMAGE_COVERAGE_TAIL_SUPPORT_VIEWS
        ),
        "min_image_centroid_x_span_ratio": x_span,
        "min_image_centroid_y_span_ratio": y_span,
        "min_image_centroid_hull_area_ratio": hull_area,
    }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _attempt_timestamp_policy(
    sensors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    per_sensor: dict[str, dict[str, Any]] = {}
    for sensor in sensors:
        sensor_key = str(sensor.get("sensor_key") or sensor.get("folder"))
        if str(sensor.get("sensor_type")) == SensorType.REALSENSE_D435.value:
            selected = {
                "frame_timestamp_source": ATTEMPT_TIMESTAMP_SOURCE,
                "robot_timestamp_source": ATTEMPT_ROBOT_TIMESTAMP_SOURCE,
                "required_frame_timestamp_domain": (ATTEMPT_REALSENSE_TIMESTAMP_DOMAIN),
                "timestamp_fallback_allowed": False,
            }
        else:
            selected = {
                "frame_timestamp_source": "host_received",
                "robot_timestamp_source": "host_received",
                "required_frame_timestamp_domain": None,
                "timestamp_fallback_allowed": False,
            }
        per_sensor[sensor_key] = selected
    frame_sources = {item["frame_timestamp_source"] for item in per_sensor.values()}
    robot_sources = {item["robot_timestamp_source"] for item in per_sensor.values()}
    required_domains = {
        item["required_frame_timestamp_domain"] for item in per_sensor.values()
    }
    return {
        "frame_timestamp_source": (
            next(iter(frame_sources)) if len(frame_sources) == 1 else "per_sensor"
        ),
        "robot_timestamp_source": (
            next(iter(robot_sources)) if len(robot_sources) == 1 else "per_sensor"
        ),
        "required_frame_timestamp_domain": (
            next(iter(required_domains)) if len(required_domains) == 1 else "per_sensor"
        ),
        "timestamp_fallback_allowed": False,
        "per_sensor": per_sensor,
    }


def _timestamp_policy_for_sensor(
    policy: Mapping[str, Any], sensor: Mapping[str, Any]
) -> dict[str, Any]:
    sensor_key = str(sensor.get("sensor_key") or sensor.get("folder"))
    per_sensor = policy.get("per_sensor")
    if isinstance(per_sensor, Mapping):
        selected = per_sensor.get(sensor_key)
        if isinstance(selected, Mapping):
            return dict(selected)
    return {
        key: policy.get(key)
        for key in (
            "frame_timestamp_source",
            "robot_timestamp_source",
            "required_frame_timestamp_domain",
            "timestamp_fallback_allowed",
            "per_sensor",
        )
    }


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _calibration_timestamp_preflight(
    run_root: Path,
    sensors: Sequence[Mapping[str, Any]],
    robot_pose_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fail closed unless selected cameras provide one coherent timebase."""

    policy = _attempt_timestamp_policy(sensors)
    errors: list[str] = []
    evidence: list[dict[str, Any]] = []
    robot_paths: dict[str, Path] = {}
    for sensor in sensors:
        sensor_policy = _timestamp_policy_for_sensor(policy, sensor)
        required_domain = sensor_policy["required_frame_timestamp_domain"]
        if required_domain is None:
            continue
        sensor_key = str(sensor.get("sensor_key") or sensor.get("folder"))
        folder = run_root / str(sensor.get("folder", ""))
        metadata_path = folder / FRAME_METADATA_JSONL
        if not _is_contained(metadata_path, run_root):
            errors.append(f"{sensor_key}: frame metadata escapes the run root")
            continue
        try:
            records = load_frame_metadata(folder)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{sensor_key}: invalid frame timestamp evidence: {exc}")
            continue
        if not records:
            errors.append(f"{sensor_key}: frame timestamp evidence is empty")
            continue
        missing_sensor_timestamp = sum(
            item.get("sensor_timestamp_ns") is None for item in records
        )
        domains: dict[str, int] = {}
        for item in records:
            domain = str(item.get("color_timestamp_domain") or "missing")
            domains[domain] = domains.get(domain, 0) + 1
        if missing_sensor_timestamp:
            errors.append(
                f"{sensor_key}: {missing_sensor_timestamp} frame(s) lack "
                "sensor_timestamp_ns"
            )
        if set(domains) != {required_domain}:
            errors.append(
                f"{sensor_key}: RealSense color timestamps must all use "
                f"{required_domain}; observed {domains}"
            )
        evidence.append(
            {
                "sensor_key": sensor_key,
                "frame_metadata_path": _relative(metadata_path, run_root),
                "frame_count": len(records),
                "sensor_timestamp_missing_count": missing_sensor_timestamp,
                "color_timestamp_domain_counts": domains,
            }
        )
        robot_path = run_root / str(sensor.get("robot_pose_path") or RAW_ROBOT_EE_POSES)
        if not _is_contained(robot_path, run_root):
            errors.append(
                f"{sensor_key}: robot timestamp evidence escapes the run root"
            )
        else:
            robot_paths[_relative(robot_path, run_root)] = robot_path

    robot_evidence: list[dict[str, Any]] = []
    for relative, robot_path in sorted(robot_paths.items()):
        if robot_pose_artifacts is None:
            try:
                poses = _read_json(robot_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"invalid robot timestamp evidence: {exc}")
                continue
        else:
            poses = robot_pose_artifacts.get(relative)
            if not isinstance(poses, Mapping):
                errors.append(
                    "verified robot timestamp evidence does not cover " + relative
                )
                continue
        missing_host_wall = sum(
            not isinstance(item, Mapping) or item.get("host_wall_timestamp_ns") is None
            for item in poses.values()
        )
        if not poses:
            errors.append("robot timestamp evidence is empty")
        if missing_host_wall:
            errors.append(
                f"{relative}: {missing_host_wall} robot "
                "pose(s) lack host_wall_timestamp_ns"
            )
        robot_evidence.append(
            {
                "path": relative,
                "pose_count": len(poses),
                "host_wall_timestamp_missing_count": missing_host_wall,
            }
        )
    if errors:
        raise ValueError(
            "Strict calibration timestamp preflight failed: " + "; ".join(errors)
        )
    return {
        **policy,
        "sensors": evidence,
        "robot_pose_artifacts": robot_evidence,
    }


def validate_attempt_id(attempt_id: str) -> str:
    value = str(attempt_id).strip().lower()
    if not ATTEMPT_ID_PATTERN.fullmatch(value):
        raise ValueError("attempt_id must contain 32 lowercase hexadecimal characters")
    return value


def calibration_attempt_root(run_root: str | Path, attempt_id: str) -> Path:
    return Path(run_root) / ATTEMPT_DIRECTORY / validate_attempt_id(attempt_id)


def _attempt_artifact_reference(attempt_id: str, filename: str) -> str:
    return (ATTEMPT_DIRECTORY / validate_attempt_id(attempt_id) / filename).as_posix()


def _sensor_key(sensor_type: str, device_id: str) -> str:
    return f"{sensor_type}:{device_id}"


def _manifest_sensor_records(root: Path) -> dict[str, dict[str, Any]]:
    path = root / DATASET_MANIFEST
    if not _is_contained(path, root) or not path.is_file():
        return {}
    try:
        manifest = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    result = {}
    for raw in manifest.get("sensors", []):
        if not isinstance(raw, Mapping):
            continue
        folder = str(raw.get("folder", ""))
        if folder:
            result[folder] = dict(raw)
    return result


def discover_calibration_cameras(run_root: str | Path) -> list[dict[str, Any]]:
    """Return captured camera identities without opening hardware."""

    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    configured = [
        dict(item)
        for item in config.get("capture", {}).get("sensors", [])
        if isinstance(item, Mapping)
    ]
    enabled_configured = [
        item for item in configured if item.get("enabled", True) is True
    ]
    manifest_records = _manifest_sensor_records(root)
    cameras: list[dict[str, Any]] = []
    for discovered in discover_sensor_records(root):
        folder = str(discovered["folder"])
        record = {**dict(discovered), **manifest_records.get(folder, {})}
        sensor_type = str(record.get("sensor_type", ""))
        device_id = str(record.get("device_id", ""))
        candidate_folder = root / folder
        contained = _is_contained(candidate_folder, root)
        folder_path = candidate_folder.resolve() if contained else candidate_folder
        matching_config = next(
            (
                item
                for item in configured
                if str(item.get("sensor_type")) == sensor_type
                and str(item.get("device_id")) == device_id
            ),
            None,
        )
        if (
            matching_config is not None
            and matching_config.get("enabled", True) is not True
        ):
            continue
        if matching_config is None:
            same_family = [
                item
                for item in enabled_configured
                if str(item.get("sensor_type")) == sensor_type
            ]
            if len(same_family) == 1:
                matching_config = same_family[0]
                device_id = str(matching_config.get("device_id") or device_id)
        key = _sensor_key(sensor_type, device_id)
        errors = []
        if not sensor_type or not device_id:
            errors.append("missing stable sensor type/device identity")
        if not contained:
            errors.append("captured sensor folder escapes the run root")
        rgb = folder_path / RGB_DIR
        depth = folder_path / DEPTH_DIR
        if contained:
            rgb_contained = _is_contained(rgb, root)
            depth_contained = _is_contained(depth, root)
            if not rgb_contained or not rgb.is_dir() or not any(rgb.glob("*.png")):
                errors.append("missing captured RGB frames")
            if (
                not depth_contained
                or not depth.is_dir()
                or not any(depth.glob("*.png"))
            ):
                errors.append("missing captured depth frames")
            if (
                not _is_contained(folder_path / FRAME_METADATA_JSONL, root)
                or not (folder_path / FRAME_METADATA_JSONL).is_file()
            ):
                errors.append("missing frame timestamp evidence")
        sensor_robot_path = folder_path / RAW_ROBOT_EE_POSES
        robot_path = (
            sensor_robot_path
            if contained
            and _is_contained(sensor_robot_path, root)
            and sensor_robot_path.is_file()
            else root / RAW_ROBOT_EE_POSES
        )
        if not _is_contained(robot_path, root) or not robot_path.is_file():
            errors.append(f"missing {RAW_ROBOT_EE_POSES}")
        else:
            try:
                robot = _read_json(robot_path)
                if not robot:
                    errors.append("robot-pose evidence is empty")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"invalid robot-pose evidence: {exc}")
        cameras.append(
            {
                "sensor_key": key,
                "sensor_type": sensor_type,
                "device_id": device_id,
                "sensor_name": folder_path.name,
                "folder": folder,
                "display_name": str(
                    (matching_config or {}).get("display_name")
                    or record.get("display_name")
                    or folder_path.name
                ),
                "current_mounting_mode": (matching_config or {}).get("mounting_mode"),
                "data_ready": not errors,
                "errors": errors,
                "frame_metadata": (
                    contained
                    and _is_contained(folder_path / FRAME_METADATA_JSONL, root)
                    and (folder_path / FRAME_METADATA_JSONL).is_file()
                ),
                "robot_pose_path": _relative(robot_path, root),
            }
        )
    identity_counts: dict[str, int] = {}
    for camera in cameras:
        sensor_key = str(camera["sensor_key"])
        identity_counts[sensor_key] = identity_counts.get(sensor_key, 0) + 1
    for camera in cameras:
        if identity_counts[str(camera["sensor_key"])] <= 1:
            continue
        camera["errors"] = [
            *camera["errors"],
            "duplicate stable sensor identity",
        ]
        camera["data_ready"] = False
    return cameras


def _saved_targets(run_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "target_id": item.get("target_id"),
            "display_name": item.get("display_name") or item.get("target_id"),
            "created_at": item.get("created_at"),
            "geometry_sha256": item.get("geometry_sha256"),
            "valid": item.get("valid", False),
            "error": item.get("error"),
            "selected": item.get("selected", False),
            "selected_placement": item.get("selected_placement"),
            "target": item.get("target"),
        }
        for item in list_target_bundles(
            library_root=default_target_library_root(), run_root=run_root
        )
    ]


def _robot_pose_reference_from_artifacts(
    robot_pose_artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive one aggregate frame identity from already loaded raw artifacts."""

    evidence_by_path = {
        relative: robot_pose_reference_evidence(raw)
        for relative, raw in sorted(robot_pose_artifacts.items())
    }
    if not evidence_by_path:
        raise ValueError("Calibration attempt has no bound robot-pose artifacts")
    statuses = {str(item.get("status")) for item in evidence_by_path.values()}
    if len(statuses) != 1:
        raise ValueError(
            "Selected cameras mix verified v1 and legacy robot-pose reference evidence"
        )
    status = next(iter(statuses))
    if status == "verified":
        identities = {
            (
                item.get("packet_schema_version"),
                item.get("from"),
                item.get("to"),
                verified_sunrise_reference_frame_path(item),
            )
            for item in evidence_by_path.values()
        }
        if len(identities) != 1:
            raise ValueError(
                "Selected cameras do not share one Sunrise robot-pose reference frame"
            )
        evidence = dict(next(iter(evidence_by_path.values())))
        evidence.pop("pose_count", None)
        evidence["artifacts"] = sorted(evidence_by_path)
        evidence["pose_counts"] = {
            relative: int(item["pose_count"])
            for relative, item in sorted(evidence_by_path.items())
        }
    else:
        reasons = {str(item.get("reason") or "") for item in evidence_by_path.values()}
        if len(reasons) != 1:
            raise ValueError(
                "Selected cameras have inconsistent legacy robot-pose provenance"
            )
        evidence = {
            "schema_version": ROBOT_POSE_REFERENCE_SCHEMA_VERSION,
            "status": "unverified",
            "reason": next(iter(reasons)),
            "artifacts": sorted(evidence_by_path),
        }
    return evidence


def _attempt_robot_pose_reference(
    run_root: Path,
    cameras: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind an attempt to the exact shared robot-pose reference, when recorded."""

    robot_pose_artifacts: dict[str, dict[str, Any]] = {}
    artifact_bindings: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        relative = str(camera.get("robot_pose_path") or "")
        if not relative:
            raise ValueError(
                f"{camera.get('sensor_key')}: robot-pose artifact path is missing"
            )
        if relative not in robot_pose_artifacts:
            artifact_path = run_root / relative
            if not _is_contained(artifact_path, run_root):
                raise ValueError(
                    f"{camera.get('sensor_key')}: robot-pose artifact escapes "
                    "the run root"
                )
            payload = artifact_path.read_bytes()
            try:
                raw = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"Robot-pose artifact must contain valid JSON: {relative}"
                ) from exc
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Robot-pose artifact must contain a JSON object: {relative}"
                )
            robot_pose_artifacts[relative] = raw
            artifact_bindings[relative] = {
                "path": relative,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }

    evidence = _robot_pose_reference_from_artifacts(robot_pose_artifacts)
    evidence["artifact_bindings"] = [
        artifact_bindings[relative] for relative in sorted(artifact_bindings)
    ]

    config = load_run_config_for_run_root(run_root)
    expected_path = configured_sunrise_reference_frame_path(config)
    if expected_path is not None:
        observed_path = verified_sunrise_reference_frame_path(evidence)
        if observed_path is None:
            raise ValueError(
                "Run config declares an exact Sunrise robot-pose reference frame, "
                "but the captured robot poses use legacy packets without that "
                "evidence"
            )
        if observed_path != expected_path:
            raise ValueError(
                "Captured robot-pose Sunrise reference frame does not match run "
                f"config: observed {observed_path!r}, expected {expected_path!r}"
            )
    return evidence


def _validated_robot_pose_artifact_bindings(
    request_value: Mapping[str, Any],
) -> list[dict[str, Any]]:
    evidence = request_value.get("robot_pose_reference")
    if not isinstance(evidence, Mapping):
        if request_value.get("schema_version") == REQUEST_SCHEMA_VERSION:
            raise ValueError(
                "Calibration attempt request lacks robot-pose reference evidence"
            )
        return []
    raw_bindings = evidence.get("artifact_bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ValueError(
            "Calibration attempt robot-pose reference lacks immutable artifact "
            "bindings; create a new attempt"
        )
    bindings: list[dict[str, Any]] = []
    paths: set[str] = set()
    for index, raw_binding in enumerate(raw_bindings):
        if not isinstance(raw_binding, Mapping):
            raise ValueError(
                f"Robot-pose artifact binding {index} must be a JSON object"
            )
        path = raw_binding.get("path")
        size_bytes = raw_binding.get("size_bytes")
        sha256 = raw_binding.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or Path(path).as_posix() != path
            or any(part in {"", ".", ".."} for part in Path(path).parts)
        ):
            raise ValueError(
                f"Robot-pose artifact binding {index} has an invalid run-relative path"
            )
        if path in paths:
            raise ValueError(f"Duplicate robot-pose artifact binding path: {path}")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ValueError(
                f"Robot-pose artifact binding {path} has an invalid size_bytes"
            )
        if size_bytes < 1:
            raise ValueError(
                f"Robot-pose artifact binding {path} must have positive size_bytes"
            )
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(
                f"Robot-pose artifact binding {path} has an invalid sha256"
            )
        paths.add(path)
        bindings.append({"path": path, "size_bytes": size_bytes, "sha256": sha256})

    recorded_artifacts = evidence.get("artifacts")
    if (
        not isinstance(recorded_artifacts, list)
        or not all(isinstance(item, str) for item in recorded_artifacts)
        or recorded_artifacts != sorted(paths)
    ):
        raise ValueError(
            "Robot-pose artifact bindings do not exactly cover the recorded "
            "reference artifacts"
        )
    expected_paths = {
        str(sensor.get("robot_pose_path") or RAW_ROBOT_EE_POSES)
        for sensor in request_value.get("sensors", [])
        if isinstance(sensor, Mapping)
    }
    if expected_paths != paths:
        raise ValueError(
            "Robot-pose artifact bindings do not exactly cover every selected "
            "camera's recorded robot-pose source"
        )
    return sorted(bindings, key=lambda item: str(item["path"]))


def _verify_robot_pose_artifact_bindings(
    run_root: Path,
    request_value: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Load, hash, parse, and semantically verify every bound raw pose artifact.

    Each path is read exactly once.  The returned decoded records are therefore
    the same bytes whose hashes and frame identity were checked, and callers can
    use them without reopening a mutable raw path.
    """

    robot_pose_artifacts: dict[str, dict[str, Any]] = {}
    for binding in _validated_robot_pose_artifact_bindings(request_value):
        relative = str(binding["path"])
        path = run_root / relative
        if not _is_contained(path, run_root):
            raise ValueError(
                f"Bound robot-pose artifact escapes the run root: {relative}"
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                "Bound robot-pose artifact is no longer readable: " + relative
            ) from exc
        actual_size = len(payload)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if (
            actual_size != int(binding["size_bytes"])
            or actual_sha256 != binding["sha256"]
        ):
            raise ValueError(
                "Raw robot-pose artifact changed after calibration attempt "
                f"creation: {relative}"
            )
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"Bound robot-pose artifact must contain valid JSON: {relative}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"Bound robot-pose artifact must contain a JSON object: {relative}"
            )
        robot_pose_artifacts[relative] = raw

    if not robot_pose_artifacts:
        return {}
    recorded = request_value.get("robot_pose_reference")
    if not isinstance(recorded, Mapping):
        raise ValueError("Calibration attempt lacks robot-pose reference evidence")
    recorded_semantics = dict(recorded)
    recorded_semantics.pop("artifact_bindings", None)
    recomputed = _robot_pose_reference_from_artifacts(robot_pose_artifacts)
    recorded_json = json.dumps(
        recorded_semantics,
        sort_keys=True,
        separators=(",", ":"),
    )
    recomputed_json = json.dumps(recomputed, sort_keys=True, separators=(",", ":"))
    if recorded_json != recomputed_json:
        raise ValueError(
            "Calibration attempt robot-pose reference identity or pose counts "
            "do not match the bound raw artifacts"
        )
    return robot_pose_artifacts


def _robot_poses_for_sensor(
    run_root: Path,
    sensor: Mapping[str, Any],
    robot_pose_artifacts: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Resolve one sensor's already verified raw robot-pose records."""

    relative = str(sensor.get("robot_pose_path") or RAW_ROBOT_EE_POSES)
    if robot_pose_artifacts:
        raw = robot_pose_artifacts.get(relative)
        if not isinstance(raw, Mapping):
            raise ValueError(f"Verified robot-pose artifacts do not cover {relative}")
        return raw
    return load_robot_poses(run_root, run_root / str(sensor["folder"]))


def _require_static_pose_template_base_evidence(evidence: Mapping[str, Any]) -> None:
    """Reject a static solve whose flange poses are not in PoseTemplateBase."""

    observed_path = verified_sunrise_reference_frame_path(evidence)
    if observed_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        actual = observed_path if observed_path is not None else "unverified"
        raise ValueError(
            "Static camera calibration requires verified robot_pose.v1 packets "
            "expressed in "
            f"{POSE_TEMPLATE_BASE_SUNRISE_PATH!r}; captured evidence is {actual!r}"
        )


def _require_static_pose_template_base_reference(
    run_root: Path,
    evidence: Mapping[str, Any],
) -> None:
    """Require static-camera calibration to solve in the dataset world frame."""

    config = load_run_config_for_run_root(run_root)
    configured_path = configured_sunrise_reference_frame_path(config)
    if configured_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        actual = configured_path if configured_path is not None else "not configured"
        raise ValueError(
            "Static camera calibration must declare "
            "frames.robot_pose.sunrise_reference_frame_path as "
            f"{POSE_TEMPLATE_BASE_SUNRISE_PATH!r} so the solved camera extrinsic "
            f"is expressed in PoseTemplateBase; found {actual!r}"
        )
    _require_static_pose_template_base_evidence(evidence)


def list_calibration_attempts(run_root: str | Path) -> list[dict[str, Any]]:
    root = Path(run_root)
    parent = root / ATTEMPT_DIRECTORY
    if not parent.is_dir():
        return []
    records = []
    for child in parent.iterdir():
        if not child.is_dir() or not ATTEMPT_ID_PATTERN.fullmatch(child.name):
            continue
        try:
            request_value = _read_json(child / REQUEST_FILE)
            progress = _read_json(child / PROGRESS_FILE)
            _validate_attempt_identity(root, child.name, request_value, progress)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        ranking = (
            _read_json(child / RANKING_FILE)
            if (child / RANKING_FILE).is_file()
            else None
        )
        promotion = (
            _read_json(child / PROMOTION_FILE)
            if (child / PROMOTION_FILE).is_file()
            else None
        )
        records.append(
            {
                "attempt_id": child.name,
                "created_at": request_value.get("created_at"),
                "mode": request_value.get("mode"),
                "sensor_keys": request_value.get("sensor_keys", []),
                "target_id": request_value.get("target_id"),
                "status": progress.get("status"),
                "read_only": (
                    request_value.get("schema_version") != REQUEST_SCHEMA_VERSION
                ),
                "recommended_camera_count": (
                    int(ranking.get("recommended_camera_count", 0)) if ranking else 0
                ),
                "promotion": promotion,
            }
        )
    return sorted(
        records, key=lambda item: str(item.get("created_at", "")), reverse=True
    )


def calibration_setup(run_root: str | Path) -> dict[str, Any]:
    root = Path(run_root)
    cameras = discover_calibration_cameras(root)
    attempts = list_calibration_attempts(root)
    return {
        "schema_version": "calibration_setup.v1",
        "run_root": root.as_posix(),
        "cameras": [item for item in cameras if item["data_ready"]],
        "unavailable_cameras": [item for item in cameras if not item["data_ready"]],
        "saved_targets": _saved_targets(root),
        "modes": [
            {
                "id": "eye_in_hand",
                "label": "Robot-mounted camera (eye-in-hand)",
                "primary_transform": "camera → robot_flange",
                "target_mounting": "stationary relative to template_base",
            },
            {
                "id": "eye_to_hand",
                "label": "Static camera (eye-to-hand)",
                "primary_transform": "camera → template_base",
                "target_mounting": "rigidly attached to robot_flange",
            },
        ],
        "solver": {
            "policies": [
                {
                    "id": "auto_compare",
                    "label": "Auto compare — recommended",
                }
            ],
            "default_policy": "auto_compare",
            "default_pnp_methods": list(PNP_METHOD_ORDER),
            "pnp_methods": list(PNP_METHOD_ORDER),
            "default_extrinsic_methods": list(EXTRINSIC_METHOD_ORDER),
            "extrinsic_methods": list(EXTRINSIC_METHOD_ORDER),
            "intrinsics_policy": DEFAULT_INTRINSICS_POLICY,
            "intrinsics_policies": [
                {"id": policy_id, "label": label}
                for policy_id, label in INTRINSICS_POLICIES.items()
            ],
            "synchronization": {
                "implementation_revision": TIME_OFFSET_IMPLEMENTATION_REVISION,
                "default_policy": "auto_offset",
                "policies": [
                    {
                        "id": "auto_offset",
                        "label": "Auto-estimate robot-pose offset — recommended",
                        "description": (
                            "Estimate effective per-camera latency with "
                            "motion-disjoint cross-validation and "
                            "search-corrected leave-one-motion-out evidence."
                        ),
                    },
                    {
                        "id": "fixed_zero",
                        "label": "Use captured timestamps (0 ms)",
                        "description": (
                            "Pair camera and robot evidence without an inferred "
                            "time offset."
                        ),
                    },
                ],
                "search": time_offset_search_configuration(),
                "sign_convention": time_offset_sign_convention(),
            },
            "thresholds": {
                "min_inliers": 6,
                "min_pnp_common_inliers": DEFAULT_MIN_PNP_COMMON_INLIERS,
                "min_pnp_common_inlier_ratio": (DEFAULT_MIN_PNP_COMMON_INLIER_RATIO),
                "max_pnp_all_point_mean_reprojection_error_px": (
                    DEFAULT_MAX_PNP_ALL_POINT_MEAN_ERROR_PX
                ),
                "min_pnp_supported_markers": (DEFAULT_MIN_PNP_SUPPORTED_MARKERS),
                "min_pnp_supported_corners_per_marker": (
                    DEFAULT_MIN_PNP_SUPPORTED_CORNERS_PER_MARKER
                ),
                "min_pnp_grid_rows": DEFAULT_MIN_PNP_GRID_ROWS,
                "min_pnp_grid_columns": DEFAULT_MIN_PNP_GRID_COLUMNS,
                "min_pnp_clutter_supported_markers": (
                    DEFAULT_MIN_PNP_CLUTTER_SUPPORTED_MARKERS
                ),
                "min_pnp_clutter_grid_rows": DEFAULT_MIN_PNP_CLUTTER_GRID_ROWS,
                "min_pnp_clutter_grid_columns": (
                    DEFAULT_MIN_PNP_CLUTTER_GRID_COLUMNS
                ),
                "min_target_marker_coverage_ratio": (
                    ATTEMPT_MIN_TARGET_MARKER_COVERAGE_RATIO
                ),
                "min_target_row_coverage_ratio": (
                    ATTEMPT_MIN_TARGET_ROW_COVERAGE_RATIO
                ),
                "min_target_column_coverage_ratio": (
                    ATTEMPT_MIN_TARGET_COLUMN_COVERAGE_RATIO
                ),
                "min_accepted_views": DEFAULT_MIN_ACCEPTED_VIEWS,
                "min_coverage_cells": DEFAULT_MIN_COVERAGE_CELLS,
                "max_per_view_reprojection_error_px": DEFAULT_MAX_VIEW_ERROR_PX,
                "max_intrinsic_rms_reprojection_error_px": DEFAULT_MAX_RMS_PX,
                "min_motion_poses": ATTEMPT_MIN_MOTION_POSES,
                "min_translation_span_mm": ATTEMPT_MIN_TRANSLATION_SPAN_MM,
                "min_rotation_span_deg": ATTEMPT_MIN_ROTATION_SPAN_DEG,
                "min_rotation_axis_angle_deg": (DEFAULT_MIN_ROTATION_AXIS_ANGLE_DEG),
                "min_rotation_axis_second_to_first_ratio": (
                    DEFAULT_MIN_ROTATION_AXIS_SINGULAR_RATIO
                ),
                "max_observations_per_motion": (DEFAULT_MAX_OBSERVATIONS_PER_MOTION),
                "max_nearest_pose_delta_ms": (ATTEMPT_MAX_NEAREST_POSE_DELTA_MS),
                "warning_nearest_pose_delta_ms": (
                    ATTEMPT_WARNING_NEAREST_POSE_DELTA_MS
                ),
                "image_coverage_tail_support_views": (
                    DEFAULT_IMAGE_COVERAGE_TAIL_SUPPORT_VIEWS
                ),
                "min_image_centroid_x_span_ratio": (
                    DEFAULT_MIN_IMAGE_CENTROID_X_SPAN_RATIO
                ),
                "min_image_centroid_y_span_ratio": (
                    DEFAULT_MIN_IMAGE_CENTROID_Y_SPAN_RATIO
                ),
                "min_image_centroid_hull_area_ratio": (
                    DEFAULT_MIN_IMAGE_CENTROID_HULL_AREA_RATIO
                ),
                "image_coverage_by_mode": {
                    mode: _image_coverage_thresholds(mode)
                    for mode in ("eye_in_hand", "eye_to_hand")
                },
                "max_mean_translation_mm": 10.0,
                "max_mean_rotation_deg": 5.0,
                "max_outlier_ratio": 0.25,
            },
        },
        "latest_attempt": attempts[0] if attempts else None,
    }


def validate_attempt_request(
    run_root: str | Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(run_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Run root not found: {root}")
    allowed_fields = {
        "run_root",
        "mode",
        "sensor_keys",
        "target_id",
        "synchronization_policy",
    }
    unknown_fields = sorted(set(value) - allowed_fields)
    if unknown_fields:
        raise ValueError(
            "Calibration attempt POST contains unsupported fields: "
            + ", ".join(unknown_fields)
        )
    mode = str(value.get("mode", ""))
    if mode not in {"eye_in_hand", "eye_to_hand"}:
        raise ValueError("mode must be eye_in_hand or eye_to_hand")
    raw_keys = value.get("sensor_keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ValueError("sensor_keys must be a non-empty list")
    sensor_keys = [str(item) for item in raw_keys]
    if len(sensor_keys) != len(set(sensor_keys)):
        raise ValueError("sensor_keys must not contain duplicates")
    cameras = {item["sensor_key"]: item for item in discover_calibration_cameras(root)}
    unknown = sorted(set(sensor_keys) - cameras.keys())
    if unknown:
        raise ValueError("Unknown sensor key(s): " + ", ".join(unknown))
    unavailable = [key for key in sensor_keys if not cameras[key]["data_ready"]]
    if unavailable:
        messages = [
            f"{key}: {', '.join(cameras[key]['errors'])}" for key in unavailable
        ]
        raise ValueError("Selected cameras are not data-ready: " + "; ".join(messages))
    selected_cameras = [cameras[key] for key in sensor_keys]
    expected_mounting_mode = "eye_in_hand" if mode == "eye_in_hand" else "static"
    mounting_mismatches = [
        str(camera["sensor_key"])
        for camera in selected_cameras
        if str(camera.get("current_mounting_mode") or "") != expected_mounting_mode
    ]
    if mounting_mismatches:
        raise ValueError(
            f"{mode} calibration requires cameras configured as "
            f"{expected_mounting_mode}; update run setup or remove: "
            + ", ".join(mounting_mismatches)
        )
    timestamp_policy = _calibration_timestamp_preflight(root, selected_cameras)
    robot_pose_reference = _attempt_robot_pose_reference(root, selected_cameras)
    if mode == "eye_to_hand":
        _require_static_pose_template_base_reference(root, robot_pose_reference)
    target_id = str(value.get("target_id", ""))
    try:
        selected_target = validate_run_target_selection(
            root,
            require_mounting_frame=True,
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "Select the exact printed calibration grid in workflow step 2 before analysis"
        ) from exc
    active_id = str(selected_target["target_id"])
    if active_id != target_id:
        blockers = [
            item
            for item in replacement_blockers(root)
            if not item.startswith(f"{ATTEMPT_DIRECTORY.as_posix()}/")
        ]
        if blockers:
            raise CalibrationTargetConflict(
                "The calibration target conflicts with existing raw acquisition or "
                "target-dependent evidence; create a new run.",
                blockers=blockers,
            )
        raise CalibrationTargetConflict(
            "The requested grid is not the grid selected in workflow step 2; change the run selection first.",
            blockers=[],
        )
    expected_target_mounting_frame = (
        "template_base" if mode == "eye_in_hand" else "robot_flange"
    )
    placement_mode = str(selected_target["placement_mode"])
    selected_mounting_frame = selected_target.get("effective_mounting_frame")
    if placement_mode != "unknown" and mode == "eye_to_hand":
        raise ValueError(
            "Static eye-to-hand calibration requires an unknown target placement "
            "mounted on robot_flange; the selected target has a known "
            "template-base placement"
        )
    if (
        selected_mounting_frame is not None
        and selected_mounting_frame != expected_target_mounting_frame
    ):
        raise ValueError(
            f"{mode} calibration requires the target mounted on "
            f"{expected_target_mounting_frame}; the selected target is mounted on "
            f"{selected_mounting_frame}"
        )
    run_target_library = root / LIBRARY_DIRECTORY
    bundle = validate_target_bundle(
        run_target_library / target_id,
        library_root=run_target_library,
    )
    synchronization_policy = str(value.get("synchronization_policy", "auto_offset"))
    if synchronization_policy not in SYNCHRONIZATION_POLICIES:
        raise ValueError(
            "synchronization_policy must be one of: "
            + ", ".join(SYNCHRONIZATION_POLICIES)
        )
    pnp_methods = list(PNP_METHOD_ORDER)
    extrinsic_methods = list(EXTRINSIC_METHOD_ORDER)
    return {
        "mode": mode,
        "sensor_keys": sensor_keys,
        "sensors": selected_cameras,
        "timestamp_policy": timestamp_policy,
        "target_id": target_id,
        "target": normalize_calibration_target_spec(selected_target["target"]),
        "target_bundle": {
            "target_id": bundle["target_id"],
            "display_name": bundle.get("display_name"),
            "configuration_sha256": bundle["configuration_sha256"],
            "geometry_sha256": bundle["geometry_sha256"],
            "files": bundle["files"],
            "selection": dict(selected_target["selection"]),
            "source_path": bundle["bundle_path"],
        },
        "target_mounting": {
            "from": "aruco_grid",
            "to": expected_target_mounting_frame,
            "state": "estimated",
        },
        "robot_pose_reference": robot_pose_reference,
        "solver_policy": "auto_compare",
        "pnp_methods": pnp_methods,
        "extrinsic_methods": extrinsic_methods,
        "intrinsics_policy": DEFAULT_INTRINSICS_POLICY,
        "synchronization_policy": synchronization_policy,
        "synchronization_search": time_offset_search_configuration(),
        "synchronization_implementation_revision": (
            TIME_OFFSET_IMPLEMENTATION_REVISION
        ),
    }


def _initial_progress(attempt_id: str) -> dict[str, Any]:
    return {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "status": "queued",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "current_phase": None,
        "message": "Calibration attempt queued.",
        "phases": [
            {"id": phase_id, "label": label, "status": "pending"}
            for phase_id, label in PHASES
        ],
    }


def _validate_attempt_identity(
    run_root: Path,
    attempt_id: str,
    request_value: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> None:
    if request_value.get("schema_version") not in READABLE_REQUEST_SCHEMA_VERSIONS:
        raise ValueError("Unsupported calibration attempt request schema")
    if progress.get("schema_version") != PROGRESS_SCHEMA_VERSION:
        raise ValueError("Unsupported calibration attempt progress schema")
    if (
        request_value.get("attempt_id") != attempt_id
        or progress.get("attempt_id") != attempt_id
    ):
        raise ValueError("Calibration attempt identity does not match its directory")
    recorded_root = Path(str(request_value.get("run_root", ""))).resolve()
    if recorded_root != run_root.resolve():
        raise ValueError("Calibration attempt belongs to a different run root")


def _require_current_attempt_request(request_value: Mapping[str, Any]) -> None:
    if request_value.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise ValueError(
            "Historical calibration attempts are read-only; create a new attempt "
            "to calculate or promote with the current timing contract"
        )


def create_calibration_attempt(
    run_root: str | Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(run_root)
    normalized = validate_attempt_request(root, value)
    attempt_id = uuid.uuid4().hex
    destination = calibration_attempt_root(root, attempt_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{attempt_id}.{uuid.uuid4().hex}.tmp"
    staging.mkdir(parents=False, exist_ok=False)
    created_at = utc_now_iso()
    request_value = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "run_root": root.as_posix(),
        "created_at": created_at,
        **normalized,
    }
    source_bundle = Path(str(normalized["target_bundle"]["source_path"]))
    try:
        shutil.copytree(source_bundle, staging / TARGET_BUNDLE_DIRECTORY)
        atomic_write_json(staging / REQUEST_FILE, request_value)
        atomic_write_json(staging / PROGRESS_FILE, _initial_progress(attempt_id))
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return request_value


def _update_progress(
    attempt_root: Path,
    *,
    status: str | None = None,
    phase: str | None = None,
    phase_status: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    progress = _read_json(attempt_root / PROGRESS_FILE)
    recorded_phases = {
        str(item.get("id")): item
        for item in progress.get("phases", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    progress["phases"] = [
        {
            **dict(recorded_phases.get(phase_id, {})),
            "id": phase_id,
            "label": label,
            "status": str(recorded_phases.get(phase_id, {}).get("status") or "pending"),
        }
        for phase_id, label in PHASES
    ]
    if status is not None:
        progress["status"] = status
    if phase is not None:
        progress["current_phase"] = phase
        for item in progress["phases"]:
            if item["id"] == phase and phase_status is not None:
                item["status"] = phase_status
    if message is not None:
        progress["message"] = message
    progress["updated_at"] = utc_now_iso()
    atomic_write_json(attempt_root / PROGRESS_FILE, progress)
    return progress


def record_attempt_job(
    run_root: str | Path,
    attempt_id: str,
    *,
    job_id: str,
    kind: str,
) -> None:
    attempt_root = calibration_attempt_root(run_root, attempt_id)
    if kind == "calculation":
        progress = _read_json(attempt_root / PROGRESS_FILE)
        progress["job_id"] = job_id
        progress["updated_at"] = utc_now_iso()
        atomic_write_json(attempt_root / PROGRESS_FILE, progress)
        return
    if kind == "promotion":
        promotion = _read_json(attempt_root / PROMOTION_FILE)
        promotion["job_id"] = job_id
        atomic_write_json(attempt_root / PROMOTION_FILE, promotion)
        return
    raise ValueError("Calibration attempt job kind must be calculation or promotion")


def record_attempt_job_submission_failure(
    run_root: str | Path,
    attempt_id: str,
    *,
    kind: str,
    error: Exception,
) -> None:
    """Make a synchronous queue failure visible without losing the attempt."""

    attempt_root = calibration_attempt_root(run_root, attempt_id)
    message = f"{type(error).__name__}: {error}"
    if kind == "calculation":
        progress = _read_json(attempt_root / PROGRESS_FILE)
        progress.update(
            {
                "status": "failed",
                "updated_at": utc_now_iso(),
                "message": message,
                "failure_stage": "job_submission",
            }
        )
        atomic_write_json(attempt_root / PROGRESS_FILE, progress)
        return
    if kind == "promotion":
        promotion = _read_json(attempt_root / PROMOTION_FILE)
        promotion.update(
            {
                "status": "failed",
                "ended_at": utc_now_iso(),
                "error": message,
                "failure_stage": "job_submission",
            }
        )
        atomic_write_json(attempt_root / PROMOTION_FILE, promotion)
        return
    raise ValueError("Calibration attempt job kind must be calculation or promotion")


def _intrinsic_deltas(
    factory: Mapping[str, Any],
    manual: Mapping[str, Any],
) -> dict[str, Any]:
    factory_native = factory["native"]
    manual_native = manual["native"]
    factory_k = np.asarray(factory_native["cam_K"], dtype=float).reshape(3, 3)
    manual_k = np.asarray(manual_native["cam_K"], dtype=float).reshape(3, 3)
    matrix_delta = manual_k - factory_k
    factory_model = str(factory_native.get("distortion_model", "brown_conrady"))
    manual_model = str(manual_native.get("distortion_model", "brown_conrady"))
    distortion_comparable = factory_model == manual_model
    distortion_delta: np.ndarray | None = None
    if distortion_comparable:
        distortion_delta = np.asarray(
            manual_native["distortion"], dtype=float
        ) - np.asarray(factory_native["distortion"], dtype=float)
    return {
        "manual_minus_factory_cam_K": matrix_delta.reshape(-1).tolist(),
        "max_abs_cam_K_delta": float(np.max(np.abs(matrix_delta))),
        "focal_length_delta_px": [
            float(manual_k[0, 0] - factory_k[0, 0]),
            float(manual_k[1, 1] - factory_k[1, 1]),
        ],
        "principal_point_delta_px": [
            float(manual_k[0, 2] - factory_k[0, 2]),
            float(manual_k[1, 2] - factory_k[1, 2]),
        ],
        "factory_distortion_model": factory_model,
        "manual_distortion_model": manual_model,
        "distortion_coefficients_comparable": distortion_comparable,
        "manual_minus_factory_distortion": (
            distortion_delta.tolist() if distortion_delta is not None else None
        ),
        "max_abs_distortion_delta": (
            float(np.max(np.abs(distortion_delta)))
            if distortion_delta is not None
            else None
        ),
    }


def _intrinsic_natural_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
        if part
    )


def _intrinsic_descriptor_distance(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> float:
    return float(
        np.linalg.norm(
            np.asarray(first["descriptor"], dtype=float)
            - np.asarray(second["descriptor"], dtype=float)
        )
    )


def _intrinsic_quality_key(record: Mapping[str, Any]) -> tuple[int, float, int]:
    return (
        int(record["matched_corner_count"]),
        float(record["target_hull_area_ratio"]),
        -int(record["chronological_index"]),
    )


def _intrinsic_maximin_selection(
    candidates: Sequence[Mapping[str, Any]],
    count: int,
    *,
    seeds: Sequence[Mapping[str, Any]] = (),
) -> list[Mapping[str, Any]]:
    selected = list(dict.fromkeys(str(item["frame"]) for item in seeds))
    by_name = {str(item["frame"]): item for item in [*seeds, *candidates]}
    while len(selected) < count:
        available = [item for item in candidates if str(item["frame"]) not in selected]
        if not available:
            break
        references = [by_name[name] for name in selected]

        def selection_key(item: Mapping[str, Any]) -> tuple[float, int, float, int]:
            minimum_distance = (
                min(
                    _intrinsic_descriptor_distance(item, reference)
                    for reference in references
                )
                if references
                else math.inf
            )
            return (minimum_distance, *_intrinsic_quality_key(item))

        selected.append(str(max(available, key=selection_key)["frame"]))
    return [by_name[name] for name in selected[:count]]


def _intrinsic_views_are_separated(
    candidate: Mapping[str, Any],
    references: Sequence[Mapping[str, Any]],
    *,
    temporal_guard: int,
    descriptor_guard: float,
) -> bool:
    for reference in references:
        if (
            abs(
                int(candidate["chronological_index"])
                - int(reference["chronological_index"])
            )
            <= temporal_guard
        ):
            return False
        if (
            descriptor_guard > 0.0
            and _intrinsic_descriptor_distance(candidate, reference) < descriptor_guard
        ):
            return False
    return True


def _intrinsic_guarded_holdouts(
    candidates: Sequence[Mapping[str, Any]],
    count: int,
    *,
    protected_training: Sequence[Mapping[str, Any]],
    temporal_guard: int,
    descriptor_guard: float,
) -> list[Mapping[str, Any]]:
    selected: list[Mapping[str, Any]] = []
    while len(selected) < count:
        references = [*protected_training, *selected]
        available = [
            item
            for item in candidates
            if str(item["frame"]) not in {str(value["frame"]) for value in selected}
            and _intrinsic_views_are_separated(
                item,
                references,
                temporal_guard=temporal_guard,
                descriptor_guard=descriptor_guard,
            )
        ]
        if not available:
            break

        def selection_key(item: Mapping[str, Any]) -> tuple[float, int, float, int]:
            minimum_distance = (
                min(
                    _intrinsic_descriptor_distance(item, reference)
                    for reference in references
                )
                if references
                else math.inf
            )
            return (minimum_distance, *_intrinsic_quality_key(item))

        selected.append(max(available, key=selection_key))
    return selected


def _intrinsic_detection_split(
    detections: Mapping[str, Any],
    target: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    raw_frames = detections.get("frames")
    if not isinstance(raw_frames, Mapping):
        raise ValueError("ArUco detections require a frames object")
    frames = {str(name): frame for name, frame in raw_frames.items()}
    if len(frames) != len(raw_frames):
        raise ValueError("ArUco detection frame names must be unique strings")
    image_size = detections.get("image_size")
    if (
        not isinstance(image_size, list)
        or len(image_size) != 2
        or any(float(value) <= 0.0 for value in image_size)
    ):
        raise ValueError(
            "ArUco detections require a positive [width, height] image_size"
        )
    width, height = (float(value) for value in image_size)
    _dictionary, board = opencv_grid_board(target)
    board_points = np.concatenate(
        [
            np.asarray(points, dtype=np.float64).reshape(-1, 3)
            for points in board.getObjPoints()
        ]
    )
    minimum = board_points[:, :2].min(axis=0)
    maximum = board_points[:, :2].max(axis=0)
    board_corners = np.asarray(
        [
            minimum,
            [maximum[0], minimum[1]],
            maximum,
            [minimum[0], maximum[1]],
        ],
        dtype=np.float64,
    ).reshape(1, 4, 2)

    records: list[dict[str, Any]] = []
    unusable_views: list[dict[str, str]] = []
    ordered_names = sorted(frames, key=_intrinsic_natural_key)
    for chronological_index, name in enumerate(ordered_names):
        frame = frames[name]
        if not isinstance(frame, Mapping):
            unusable_views.append({"frame": name, "reason": "invalid_detection_record"})
            continue
        points = _view_points(frame, board)
        if points is None:
            unusable_views.append(
                {"frame": name, "reason": "insufficient_matched_grid_corners"}
            )
            continue
        object_points, image_points = points
        try:
            homography, _mask = cv2.findHomography(
                np.asarray(object_points[:, :2], dtype=np.float64),
                np.asarray(image_points, dtype=np.float64),
                method=0,
            )
            if homography is None:
                raise ValueError("homography fit returned no matrix")
            projected = cv2.perspectiveTransform(
                board_corners,
                np.asarray(homography, dtype=np.float64),
            ).reshape(4, 2)
            if not np.all(np.isfinite(projected)):
                raise ValueError("projected board corners are non-finite")
        except (cv2.error, ValueError, TypeError) as exc:
            unusable_views.append(
                {"frame": name, "reason": f"projective_descriptor_unavailable: {exc}"}
            )
            continue
        centroid = np.asarray(image_points, dtype=float).mean(axis=0)
        hull = cv2.convexHull(np.asarray(image_points, dtype=np.float32))
        normalized_corners = projected / np.asarray([width, height])
        descriptor = (
            normalized_corners.reshape(-1) / ATTEMPT_INTRINSIC_DESCRIPTOR_CORNER_SCALE
        )
        records.append(
            {
                "frame": name,
                "chronological_index": chronological_index,
                "coverage_cell": _coverage_cell(centroid.tolist(), image_size),
                "matched_corner_count": len(image_points),
                "target_hull_area_ratio": float(
                    cv2.contourArea(hull) / (width * height)
                ),
                "projected_board_corners_normalized": (
                    normalized_corners.reshape(-1).astype(float).tolist()
                ),
                "descriptor": descriptor.astype(float).tolist(),
            }
        )

    by_cell: dict[int, list[Mapping[str, Any]]] = {}
    for record in records:
        cell = record["coverage_cell"]
        if cell is not None:
            by_cell.setdefault(int(cell), []).append(record)
    represented_cells = sorted(by_cell)
    protected_training = [
        max(by_cell[cell], key=_intrinsic_quality_key) for cell in represented_cells
    ]
    protected_names = {str(item["frame"]) for item in protected_training}
    holdout_count = min(
        ATTEMPT_INTRINSIC_MAX_HOLDOUT_VIEWS,
        max(
            ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS,
            math.ceil(len(records) * ATTEMPT_INTRINSIC_HOLDOUT_FRACTION),
        ),
        max(0, len(records) - DEFAULT_MIN_ACCEPTED_VIEWS),
    )
    holdout_candidates = [
        item for item in records if str(item["frame"]) not in protected_names
    ]
    minimum_training_count = min(
        DEFAULT_MIN_ACCEPTED_VIEWS,
        max(0, len(records) - holdout_count),
    )
    required_training_cells = min(
        DEFAULT_MIN_COVERAGE_CELLS,
        len(represented_cells),
    )
    descriptor_options = (1.0, 0.75, 0.5, 0.0)
    guard_options = [
        (temporal_guard, descriptor_guard)
        for temporal_guard in range(ATTEMPT_INTRINSIC_TEMPORAL_GUARD_VIEWS, -1, -1)
        for descriptor_guard in descriptor_options
    ]
    guard_options.sort(
        key=lambda item: (
            (ATTEMPT_INTRINSIC_TEMPORAL_GUARD_VIEWS - item[0])
            / max(1, ATTEMPT_INTRINSIC_TEMPORAL_GUARD_VIEWS)
            + ATTEMPT_INTRINSIC_DESCRIPTOR_GUARD_DISTANCE
            - item[1],
            -item[0],
            -item[1],
        )
    )
    selected_holdouts: list[Mapping[str, Any]] = []
    training_pool: list[Mapping[str, Any]] = []
    effective_temporal_guard = 0
    effective_descriptor_guard = 0.0
    for temporal_guard, descriptor_guard in guard_options:
        candidate_holdouts = _intrinsic_guarded_holdouts(
            holdout_candidates,
            holdout_count,
            protected_training=protected_training,
            temporal_guard=temporal_guard,
            descriptor_guard=descriptor_guard,
        )
        if len(candidate_holdouts) != holdout_count:
            continue
        holdout_names = {str(item["frame"]) for item in candidate_holdouts}
        candidate_training_pool = [
            item
            for item in records
            if str(item["frame"]) not in holdout_names
            and _intrinsic_views_are_separated(
                item,
                candidate_holdouts,
                temporal_guard=temporal_guard,
                descriptor_guard=descriptor_guard,
            )
        ]
        available_cells = {
            int(item["coverage_cell"])
            for item in candidate_training_pool
            if item["coverage_cell"] is not None
        }
        if (
            len(candidate_training_pool) < minimum_training_count
            or len(available_cells) < required_training_cells
        ):
            continue
        selected_holdouts = candidate_holdouts
        training_pool = candidate_training_pool
        effective_temporal_guard = temporal_guard
        effective_descriptor_guard = descriptor_guard
        break
    if not training_pool and records:
        raise ValueError(
            "Intrinsic split could not preserve the minimum training/holdout evidence"
        )

    training_seed_names = {
        str(item["frame"]) for item in protected_training if item in training_pool
    }
    training_seeds = [
        item for item in training_pool if str(item["frame"]) in training_seed_names
    ]
    selected_training = _intrinsic_maximin_selection(
        training_pool,
        min(ATTEMPT_INTRINSIC_MAX_TRAINING_VIEWS, len(training_pool)),
        seeds=training_seeds,
    )
    selected_training.sort(key=lambda item: int(item["chronological_index"]))
    selected_holdouts.sort(key=lambda item: int(item["chronological_index"]))
    training_names = [str(item["frame"]) for item in selected_training]
    holdout_names = [str(item["frame"]) for item in selected_holdouts]
    selected_names = set(training_names + holdout_names)

    omitted_views: list[dict[str, Any]] = []
    for record in records:
        name = str(record["frame"])
        if name in selected_names:
            continue
        reasons = []
        if any(
            abs(
                int(record["chronological_index"]) - int(holdout["chronological_index"])
            )
            <= effective_temporal_guard
            for holdout in selected_holdouts
        ):
            reasons.append("holdout_temporal_guard")
        if effective_descriptor_guard > 0.0 and any(
            _intrinsic_descriptor_distance(record, holdout) < effective_descriptor_guard
            for holdout in selected_holdouts
        ):
            reasons.append("holdout_descriptor_guard")
        if not reasons:
            reasons.append("training_diversity_cap")
        omitted_views.append({"frame": name, "reasons": reasons})
    correlated_omissions = sum(
        1
        for item in omitted_views
        if any(str(reason).startswith("holdout_") for reason in item["reasons"])
    )

    def evidence(
        values: Sequence[Mapping[str, Any]],
        split_name: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "frame": item["frame"],
                "split": split_name,
                "chronological_index": item["chronological_index"],
                "coverage_cell": item["coverage_cell"],
                "matched_corner_count": item["matched_corner_count"],
                "target_hull_area_ratio": item["target_hull_area_ratio"],
                "projected_board_corners_normalized": item[
                    "projected_board_corners_normalized"
                ],
            }
            for item in values
        ]

    training_cells = sorted(
        {
            int(item["coverage_cell"])
            for item in selected_training
            if item["coverage_cell"] is not None
        }
    )
    holdout_cells = sorted(
        {
            int(item["coverage_cell"])
            for item in selected_holdouts
            if item["coverage_cell"] is not None
        }
    )
    training = {
        **dict(detections),
        "frames": {name: frames[name] for name in training_names},
    }
    holdout = {
        **dict(detections),
        "frames": {name: frames[name] for name in holdout_names},
    }
    split = {
        "strategy": "deterministic_projective_maximin_guarded_views_v2",
        "usable_view_count": len(records),
        "unusable_view_count": len(unusable_views),
        "unusable_views": unusable_views,
        "selected_usable_view_count": len(selected_names),
        "omitted_usable_view_count": len(omitted_views),
        "omitted_correlated_view_count": correlated_omissions,
        "omitted_views": omitted_views,
        "max_optimization_views": (
            ATTEMPT_INTRINSIC_MAX_TRAINING_VIEWS + ATTEMPT_INTRINSIC_MAX_HOLDOUT_VIEWS
        ),
        "max_training_views": ATTEMPT_INTRINSIC_MAX_TRAINING_VIEWS,
        "max_holdout_views": ATTEMPT_INTRINSIC_MAX_HOLDOUT_VIEWS,
        "training_usable_view_count": len(training_names),
        "heldout_usable_view_count": len(holdout_names),
        "training_views": training_names,
        "heldout_views": holdout_names,
        "represented_coverage_cells": represented_cells,
        "training_coverage_cells": training_cells,
        "heldout_coverage_cells": holdout_cells,
        "selected_view_evidence": [
            *evidence(selected_training, "training"),
            *evidence(selected_holdouts, "holdout"),
        ],
        "descriptor": {
            "method": "planar_homography_projected_board_corners_v1",
            "dimension": 8,
            "normalized_corner_coordinate_scale": (
                ATTEMPT_INTRINSIC_DESCRIPTOR_CORNER_SCALE
            ),
            "factory_intrinsics_used": False,
        },
        "holdout_guard": {
            "requested_temporal_radius_views": (ATTEMPT_INTRINSIC_TEMPORAL_GUARD_VIEWS),
            "effective_temporal_radius_views": effective_temporal_guard,
            "requested_descriptor_distance": (
                ATTEMPT_INTRINSIC_DESCRIPTOR_GUARD_DISTANCE
            ),
            "effective_descriptor_distance": effective_descriptor_guard,
            "relaxed_for_minimum_split_feasibility": (
                effective_temporal_guard < ATTEMPT_INTRINSIC_TEMPORAL_GUARD_VIEWS
                or effective_descriptor_guard
                < ATTEMPT_INTRINSIC_DESCRIPTOR_GUARD_DISTANCE
            ),
        },
        "thresholds": {
            "min_training_views": DEFAULT_MIN_ACCEPTED_VIEWS,
            "min_heldout_views": ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS,
            "min_training_coverage_cells": DEFAULT_MIN_COVERAGE_CELLS,
            "holdout_fraction_before_caps": ATTEMPT_INTRINSIC_HOLDOUT_FRACTION,
        },
    }
    return training, holdout, split


def _intrinsic_holdout_evaluation(
    profile: Mapping[str, Any],
    detections: Mapping[str, Any],
    target: Mapping[str, Any],
) -> dict[str, Any]:
    native = profile.get("native")
    if not isinstance(native, Mapping):
        return {
            "status": "unavailable",
            "comparable": False,
            "reason": "missing_native_projection",
        }
    if not projection_is_opencv_compatible(native):
        return {
            "status": "unavailable",
            "comparable": False,
            "reason": "distortion_model_is_not_forward_opencv_compatible",
            "distortion_model": native.get("distortion_model"),
        }
    matrix = np.asarray(native["cam_K"], dtype=float).reshape(3, 3)
    distortion = np.asarray(native["distortion"], dtype=float).reshape(-1)
    _dictionary, board = opencv_grid_board(target)
    frames = detections.get("frames")
    if not isinstance(frames, Mapping):
        raise ValueError("Held-out detections require a frames object")
    per_view: dict[str, float] = {}
    failures: list[dict[str, str]] = []
    squared_errors: list[float] = []
    for frame_name, frame in sorted(frames.items()):
        if not isinstance(frame, Mapping):
            failures.append(
                {"frame": str(frame_name), "reason": "invalid_detection_record"}
            )
            continue
        points = _view_points(frame, board)
        if points is None:
            failures.append(
                {"frame": str(frame_name), "reason": "insufficient_grid_points"}
            )
            continue
        object_points, image_points = points
        try:
            success, rvec, tvec = cv2.solvePnP(
                np.asarray(object_points, dtype=np.float64),
                np.asarray(image_points, dtype=np.float64),
                matrix,
                distortion,
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
            if not success:
                raise ValueError("solvePnP returned no pose")
            projected, _ = cv2.projectPoints(
                object_points,
                rvec,
                tvec,
                matrix,
                distortion,
            )
            errors = np.linalg.norm(
                projected.reshape(-1, 2) - image_points.reshape(-1, 2),
                axis=1,
            )
            if not np.all(np.isfinite(errors)):
                raise ValueError("non-finite reprojection error")
        except (cv2.error, ValueError, TypeError) as exc:
            failures.append({"frame": str(frame_name), "reason": str(exc)})
            continue
        squared_errors.extend(np.square(errors).astype(float).tolist())
        per_view[str(frame_name)] = float(np.sqrt(np.mean(np.square(errors))))
    rms = float(np.sqrt(np.mean(squared_errors))) if squared_errors else None
    max_view = max(per_view.values(), default=None)
    enough_views = len(per_view) >= ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS
    accepted = (
        enough_views
        and rms is not None
        and rms <= DEFAULT_MAX_RMS_PX
        and max_view is not None
        and max_view <= DEFAULT_MAX_VIEW_ERROR_PX
    )
    return {
        "status": "accepted" if accepted else "rejected",
        "comparable": enough_views and rms is not None,
        "profile_id": profile.get("profile_id"),
        "evaluated_view_count": len(per_view),
        "rms_reprojection_error_px": rms,
        "max_view_reprojection_error_px": max_view,
        "per_view_rms_reprojection_error_px": per_view,
        "failures": failures,
        "thresholds": {
            "min_heldout_views": ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS,
            "max_rms_reprojection_error_px": DEFAULT_MAX_RMS_PX,
            "max_view_reprojection_error_px": DEFAULT_MAX_VIEW_ERROR_PX,
        },
    }


def _manual_intrinsic_plausibility(
    factory: Mapping[str, Any],
    manual: Mapping[str, Any],
) -> dict[str, Any]:
    factory_native = factory["native"]
    manual_native = manual["native"]
    factory_k = np.asarray(factory_native["cam_K"], dtype=float).reshape(3, 3)
    manual_k = np.asarray(manual_native["cam_K"], dtype=float).reshape(3, 3)
    distortion = np.asarray(manual_native["distortion"], dtype=float).reshape(-1)
    width, height = (float(value) for value in manual["resolution"])
    fx, fy, cx, cy = (
        float(manual_k[0, 0]),
        float(manual_k[1, 1]),
        float(manual_k[0, 2]),
        float(manual_k[1, 2]),
    )
    factory_fx, factory_fy = float(factory_k[0, 0]), float(factory_k[1, 1])
    focal_delta_ratio = max(
        abs(fx - factory_fx) / factory_fx,
        abs(fy - factory_fy) / factory_fy,
    )
    principal_delta_ratio = max(
        abs(cx - float(factory_k[0, 2])) / width,
        abs(cy - float(factory_k[1, 2])) / height,
    )
    aspect_delta_ratio = abs((fx / fy) / (factory_fx / factory_fy) - 1.0)
    distortion_limits = np.asarray([1.0, 3.0, 0.05, 0.05, 5.0])
    checks = {
        "finite_parameters": bool(
            np.all(np.isfinite(manual_k)) and np.all(np.isfinite(distortion))
        ),
        "positive_focal_lengths": fx > 0.0 and fy > 0.0,
        "principal_point_inside_image": 0.0 <= cx < width and 0.0 <= cy < height,
        "focal_delta_ratio": focal_delta_ratio
        <= ATTEMPT_INTRINSIC_MAX_FOCAL_DELTA_RATIO,
        "principal_delta_ratio": principal_delta_ratio
        <= ATTEMPT_INTRINSIC_MAX_PRINCIPAL_DELTA_RATIO,
        "pixel_aspect_delta_ratio": aspect_delta_ratio
        <= ATTEMPT_INTRINSIC_MAX_ASPECT_DELTA_RATIO,
        "distortion_magnitude": distortion.size == 5
        and bool(np.all(np.abs(distortion) <= distortion_limits)),
    }
    return {
        "status": "accepted" if all(checks.values()) else "rejected",
        "checks": checks,
        "metrics": {
            "focal_delta_ratio": float(focal_delta_ratio),
            "principal_delta_ratio": float(principal_delta_ratio),
            "pixel_aspect_delta_ratio": float(aspect_delta_ratio),
            "absolute_distortion": np.abs(distortion).astype(float).tolist(),
        },
        "thresholds": {
            "max_focal_delta_ratio": ATTEMPT_INTRINSIC_MAX_FOCAL_DELTA_RATIO,
            "max_principal_delta_ratio": (ATTEMPT_INTRINSIC_MAX_PRINCIPAL_DELTA_RATIO),
            "max_pixel_aspect_delta_ratio": (ATTEMPT_INTRINSIC_MAX_ASPECT_DELTA_RATIO),
            "max_absolute_distortion": distortion_limits.tolist(),
        },
    }


def _intrinsic_projection_evidence(profile: Mapping[str, Any]) -> dict[str, Any]:
    native = profile.get("native")
    compatible = isinstance(native, Mapping) and projection_is_opencv_compatible(native)
    evidence = {
        "profile_id": profile.get("profile_id"),
        "opencv_projection_compatible": compatible,
        "distortion_model": (
            native.get("distortion_model") if isinstance(native, Mapping) else None
        ),
        "reason": (
            None
            if compatible
            else (
                "distortion_model_is_not_forward_opencv_compatible"
                if isinstance(native, Mapping)
                else "missing_native_projection"
            )
        ),
    }
    source = profile.get("source")
    compatibility_basis = (
        source.get("opencv_projection_compatibility_basis")
        if isinstance(source, Mapping)
        else None
    )
    if compatible and compatibility_basis:
        evidence["compatibility_basis"] = compatibility_basis
    return evidence


def _intrinsics_for_sensors(
    run_root: Path,
    attempt_root: Path,
    synchronized: Mapping[str, Path],
    request_value: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    existing_path = run_root / INTRINSIC_CALIBRATION_PROFILES
    existing = (
        load_intrinsic_profile_collection(existing_path)
        if existing_path.is_file()
        else []
    )
    profiles: list[dict[str, Any]] = []
    by_sensor: dict[str, dict[str, Any]] = {}
    unusable_sensor_keys: list[str] = []
    comparisons = {
        "schema_version": "intrinsic_comparison.v1",
        "generated_at": utc_now_iso(),
        "attempt_id": request_value["attempt_id"],
        "policy": request_value["intrinsics_policy"],
        "thresholds": {
            "min_accepted_views": DEFAULT_MIN_ACCEPTED_VIEWS,
            "min_coverage_cells": DEFAULT_MIN_COVERAGE_CELLS,
            "max_per_view_reprojection_error_px": DEFAULT_MAX_VIEW_ERROR_PX,
            "max_rms_reprojection_error_px": DEFAULT_MAX_RMS_PX,
            "min_heldout_views": ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS,
            "max_training_views": ATTEMPT_INTRINSIC_MAX_TRAINING_VIEWS,
            "max_holdout_views": ATTEMPT_INTRINSIC_MAX_HOLDOUT_VIEWS,
            "min_absolute_heldout_improvement_px": (
                ATTEMPT_INTRINSIC_MIN_ABSOLUTE_IMPROVEMENT_PX
            ),
            "min_relative_heldout_improvement": (
                ATTEMPT_INTRINSIC_MIN_RELATIVE_IMPROVEMENT
            ),
        },
        "sensors": [],
    }
    for sensor_key, folder in synchronized.items():
        sensor_id, orientation, resolution = sensor_intrinsic_identity(folder)
        factory = factory_intrinsic_profile(folder)
        factory_projection = _intrinsic_projection_evidence(factory)
        candidates = [factory]
        selected: dict[str, Any] | None = None
        existing_projection: dict[str, Any] | None = None
        unusable_projection: dict[str, Any] | None = None
        manual: dict[str, Any] | None = None
        manual_failure: dict[str, Any] | None = None
        comparison_split: dict[str, Any] | None = None
        factory_evaluation: dict[str, Any] | None = None
        manual_evaluation: dict[str, Any] | None = None
        manual_plausibility: dict[str, Any] | None = None
        selection_gates: dict[str, bool] | None = None
        improvement: dict[str, float] | None = None
        if request_value["intrinsics_policy"] == "compare_factory_opencv":
            detections = detect_sensor_folder(
                folder,
                request_value["target"],
                output_path=folder / ARUCO_DETECTIONS,
            )
            try:
                training_detections, holdout_detections, comparison_split = (
                    _intrinsic_detection_split(
                        detections,
                        request_value["target"],
                    )
                )
                if (
                    comparison_split["heldout_usable_view_count"]
                    < ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS
                    or comparison_split["training_usable_view_count"]
                    < DEFAULT_MIN_ACCEPTED_VIEWS
                ):
                    raise ValueError(
                        "Intrinsic comparison requires at least "
                        f"{DEFAULT_MIN_ACCEPTED_VIEWS} training and "
                        f"{ATTEMPT_INTRINSIC_MIN_HOLDOUT_VIEWS} held-out views"
                    )
                manual = calibrate_intrinsic_profile(
                    folder,
                    training_detections,
                    request_value["target"],
                )
                candidates.append(manual)
                factory_evaluation = _intrinsic_holdout_evaluation(
                    factory,
                    holdout_detections,
                    request_value["target"],
                )
                manual_evaluation = _intrinsic_holdout_evaluation(
                    manual,
                    holdout_detections,
                    request_value["target"],
                )
                manual_plausibility = _manual_intrinsic_plausibility(
                    factory,
                    manual,
                )
            except IntrinsicCalibrationError as exc:
                manual_failure = {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "quality": exc.report,
                }
            except (cv2.error, ValueError, TypeError) as exc:
                manual_failure = {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            if (
                factory_evaluation is not None
                and manual_evaluation is not None
                and factory_evaluation.get("comparable")
                and manual_evaluation.get("comparable")
            ):
                factory_rms = float(factory_evaluation["rms_reprojection_error_px"])
                manual_rms = float(manual_evaluation["rms_reprojection_error_px"])
                absolute_improvement = factory_rms - manual_rms
                relative_improvement = (
                    absolute_improvement / factory_rms if factory_rms > 0.0 else 0.0
                )
                improvement = {
                    "absolute_rms_reprojection_error_px": float(absolute_improvement),
                    "relative_rms_reprojection_error": float(relative_improvement),
                }
            factory_projection_unavailable = not bool(
                factory_projection["opencv_projection_compatible"]
            )
            selection_gates = {
                "manual_training_quality": bool(
                    manual is not None
                    and manual.get("quality", {}).get("status") == "accepted"
                ),
                "manual_parameter_plausibility": bool(
                    manual_plausibility is not None
                    and manual_plausibility.get("status") == "accepted"
                ),
                "manual_heldout_absolute_quality": bool(
                    manual_evaluation is not None
                    and manual_evaluation.get("status") == "accepted"
                ),
                "factory_heldout_comparable": bool(
                    factory_evaluation is not None
                    and factory_evaluation.get("comparable")
                ),
                "minimum_absolute_improvement": bool(
                    improvement is not None
                    and improvement["absolute_rms_reprojection_error_px"]
                    >= ATTEMPT_INTRINSIC_MIN_ABSOLUTE_IMPROVEMENT_PX
                ),
                "minimum_relative_improvement": bool(
                    improvement is not None
                    and improvement["relative_rms_reprojection_error"]
                    >= ATTEMPT_INTRINSIC_MIN_RELATIVE_IMPROVEMENT
                ),
                "factory_projection_unavailable": factory_projection_unavailable,
            }
            manual_proven = (
                selection_gates["manual_training_quality"]
                and selection_gates["manual_parameter_plausibility"]
                and selection_gates["manual_heldout_absolute_quality"]
                and factory_projection_unavailable
            )
            if manual is not None and manual_proven:
                selected = {
                    **manual,
                    "attempt_intrinsics_source": (
                        "opencv_manual_factory_projection_unavailable"
                    ),
                }
                selection_reason = (
                    "manual_opencv_passed_training_heldout_and_plausibility_"
                    "gates_while_factory_projection_was_unavailable"
                )
            else:
                selected = {
                    **factory,
                    "attempt_intrinsics_source": (
                        "factory_unusable_manual_not_accepted"
                        if factory_projection_unavailable
                        else "factory_compatible_default_comparison_only"
                    ),
                }
                selection_reason = (
                    (
                        "factory_projection_unusable_and_manual_opencv_did_"
                        "not_pass_all_activation_gates"
                    )
                    if factory_projection_unavailable
                    else (
                        "compatible_factory_intrinsics_retained_by_policy;_"
                        "manual_opencv_result_is_comparison_only"
                    )
                )
        else:
            try:
                existing_profile = select_intrinsic_profile(
                    existing,
                    sensor_id=sensor_id,
                    orientation=orientation,
                    resolution=resolution,
                )
            except ValueError:
                existing_profile = None
            if existing_profile is not None:
                existing_projection = _intrinsic_projection_evidence(existing_profile)
                existing_candidate = {
                    **existing_profile,
                    "attempt_intrinsics_source": (
                        "compatible_existing"
                        if existing_projection["opencv_projection_compatible"]
                        else "existing_projection_unusable"
                    ),
                }
                candidates.append(existing_candidate)
                if existing_projection["opencv_projection_compatible"]:
                    selected = existing_candidate
                    selection_reason = "exact_compatible_existing_profile"
            if selected is None and factory_projection["opencv_projection_compatible"]:
                selected = {
                    **factory,
                    "attempt_intrinsics_source": (
                        "factory_capture_sidecars_existing_projection_unusable"
                        if existing_profile is not None
                        else "factory_capture_sidecars"
                    ),
                }
                selection_reason = (
                    "exact_existing_profile_projection_unusable;_"
                    "compatible_factory_capture_sidecars_selected"
                    if existing_profile is not None
                    else "no_exact_compatible_existing_profile"
                )
            elif selected is None:
                selection_reason = (
                    "exact_existing_and_factory_projections_are_unusable"
                    if existing_profile is not None
                    else "no_exact_existing_profile_and_factory_projection_is_unusable"
                )

        selected_projection = (
            _intrinsic_projection_evidence(selected) if selected is not None else None
        )
        if (
            selected_projection is None
            or not selected_projection["opencv_projection_compatible"]
        ):
            unusable_projection = {
                "reason": "no_opencv_compatible_intrinsic_projection",
                "factory": factory_projection,
                "existing": existing_projection,
                "selected": selected_projection,
            }
            unusable_sensor_keys.append(sensor_key)
            selected = None
        else:
            profiles.append(selected)
            by_sensor[sensor_key] = selected
        manual_selected = (
            manual is not None
            and selected is not None
            and selected["profile_id"] == manual.get("profile_id")
            and selected.get("attempt_intrinsics_source")
            == "opencv_manual_factory_projection_unavailable"
        )
        selection_status = (
            "unusable"
            if unusable_projection is not None
            else (
                "manual_selected"
                if manual_selected
                else (
                    "existing_selected"
                    if selected is not None
                    and selected.get("attempt_intrinsics_source")
                    == "compatible_existing"
                    else "factory_selected"
                )
            )
        )
        comparisons["sensors"].append(
            {
                "sensor_key": sensor_key,
                "sensor_id": sensor_id,
                "resolution": list(resolution),
                "orientation": orientation,
                "status": selection_status,
                "selected_profile_id": (
                    selected["profile_id"] if selected is not None else None
                ),
                "selection_reason": selection_reason,
                "factory_profile_id": factory["profile_id"],
                "factory_projection": factory_projection,
                "existing_projection": existing_projection,
                "unusable_projection": unusable_projection,
                "manual_profile_id": manual.get("profile_id") if manual else None,
                "manual_failure": manual_failure,
                "comparison_split": comparison_split,
                "factory_heldout_evaluation": factory_evaluation,
                "manual_heldout_evaluation": manual_evaluation,
                "manual_plausibility": manual_plausibility,
                "heldout_improvement": improvement,
                "selection_gates": selection_gates,
                "deltas": (
                    _intrinsic_deltas(factory, manual) if manual is not None else None
                ),
                "candidates": candidates,
            }
        )
    atomic_write_json(attempt_root / INTRINSIC_COMPARISON, comparisons)
    if unusable_sensor_keys:
        raise ValueError(
            "No OpenCV-compatible intrinsic projection is available for: "
            + ", ".join(sorted(unusable_sensor_keys))
            + f"; see {INTRINSIC_COMPARISON} for preserved projection evidence"
        )
    return profiles, by_sensor


def _recorded_time_offset_search(
    request_value: Mapping[str, Any],
) -> dict[str, Any]:
    recorded = request_value.get("synchronization_search")
    return (
        dict(recorded)
        if isinstance(recorded, Mapping)
        else time_offset_search_configuration()
    )


def _recorded_nearest_pose_limits_ms(
    request_value: Mapping[str, Any],
) -> tuple[float, float | None]:
    search = _recorded_time_offset_search(request_value)
    maximum_ms = float(search["max_nearest_pose_delta_ms"])
    warning_value = search.get("warning_nearest_pose_delta_ms")
    warning_ms = float(warning_value) if warning_value is not None else None
    if not math.isfinite(maximum_ms) or maximum_ms <= 0.0:
        raise ValueError(
            "Calibration time-offset max nearest-pose delta must be positive"
        )
    if warning_ms is not None and (
        not math.isfinite(warning_ms) or warning_ms <= 0.0 or warning_ms > maximum_ms
    ):
        raise ValueError(
            "Calibration nearest-pose warning threshold must be positive and no "
            "greater than the maximum"
        )
    return maximum_ms, warning_ms


def _append_nearest_pose_warning_checks(
    sync_quality: dict[str, Any],
    *,
    warning_threshold_ms: float | None,
) -> None:
    if warning_threshold_ms is None:
        return
    checks = sync_quality.get("checks")
    if not isinstance(checks, list):
        checks = []
        sync_quality["checks"] = checks
    sensors = sync_quality.get("sensors")
    if not isinstance(sensors, list):
        return
    threshold_ns = round(warning_threshold_ms * 1_000_000.0)
    for sensor in sensors:
        if not isinstance(sensor, Mapping):
            continue
        sensor_name = str(sensor.get("sensor_name") or "unknown")
        value = sensor.get("max_abs_nearest_pose_delta_ns")
        try:
            maximum_ns = int(value)
        except (TypeError, ValueError):
            continue
        exceeds = maximum_ns > threshold_ns
        checks.append(
            {
                "name": f"calibration_nearest_pose_warning:{sensor_name}",
                "status": "warning" if exceeds else "ok",
                "message": (
                    f"{sensor_name} maximum nearest-pose delta "
                    f"{maximum_ns / 1_000_000.0:.3f} ms "
                    + (
                        f"exceeds the {warning_threshold_ms:g} ms warning level; "
                        "the match remains inside the relaxed hard limit."
                        if exceeds
                        else f"is within the {warning_threshold_ms:g} ms warning level."
                    )
                ),
                "details": {
                    "max_abs_nearest_pose_delta_ns": maximum_ns,
                    "warning_nearest_pose_delta_ms": warning_threshold_ms,
                },
            }
        )


def _refresh_sync_quality_status(sync_quality: dict[str, Any]) -> None:
    checks = sync_quality.get("checks")
    statuses = (
        {str(item.get("status")) for item in checks if isinstance(item, Mapping)}
        if isinstance(checks, list)
        else set()
    )
    sync_quality["overall_status"] = (
        "error" if "error" in statuses else "warning" if "warning" in statuses else "ok"
    )


def _prepare_attempt_data(
    run_root: Path,
    attempt_root: Path,
    request_value: Mapping[str, Any],
    robot_pose_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    if robot_pose_artifacts is None:
        robot_pose_artifacts = _verify_robot_pose_artifact_bindings(
            run_root, request_value
        )
    max_nearest_pose_delta_ms, warning_nearest_pose_delta_ms = (
        _recorded_nearest_pose_limits_ms(request_value)
    )
    timestamp_policy = _calibration_timestamp_preflight(
        run_root,
        request_value["sensors"],
        robot_pose_artifacts or None,
    )
    recorded_timestamp_policy = request_value.get("timestamp_policy")
    if isinstance(recorded_timestamp_policy, Mapping):
        policy_keys = (
            "frame_timestamp_source",
            "robot_timestamp_source",
            "required_frame_timestamp_domain",
            "timestamp_fallback_allowed",
        )
        for key in policy_keys:
            if recorded_timestamp_policy.get(key) != timestamp_policy.get(key):
                raise ValueError(
                    "Recorded calibration timestamp policy no longer matches "
                    f"the selected sensor timebase: {key}"
                )
        recorded_per_sensor = recorded_timestamp_policy.get("per_sensor")
        current_per_sensor = timestamp_policy.get("per_sensor")
        expected_sensor_keys = {
            str(sensor.get("sensor_key") or sensor.get("folder"))
            for sensor in request_value["sensors"]
        }
        if (
            not isinstance(recorded_per_sensor, Mapping)
            or not isinstance(current_per_sensor, Mapping)
            or set(recorded_per_sensor) != expected_sensor_keys
            or set(current_per_sensor) != expected_sensor_keys
        ):
            raise ValueError(
                "Recorded calibration timestamp policy no longer matches the "
                "selected per-sensor timebases"
            )
        for sensor_key in sorted(expected_sensor_keys):
            recorded_sensor_policy = recorded_per_sensor.get(sensor_key)
            current_sensor_policy = current_per_sensor.get(sensor_key)
            if not isinstance(recorded_sensor_policy, Mapping) or not isinstance(
                current_sensor_policy, Mapping
            ):
                raise ValueError(
                    "Recorded calibration timestamp policy lacks per-sensor "
                    f"evidence for {sensor_key}"
                )
            for key in policy_keys:
                if recorded_sensor_policy.get(key) != current_sensor_policy.get(key):
                    raise ValueError(
                        "Recorded calibration timestamp policy no longer matches "
                        f"{sensor_key}: {key}"
                    )
    # Retain the zero-offset image/PnP workspace separately.  The accepted
    # per-sensor offsets are materialized later under processed/synchronized;
    # reusing that folder would delete the detections and source-frame mapping
    # that make the search reproducible.
    output_root = attempt_root / "processed" / "preparation_synchronized"
    synchronized: dict[str, Path] = {}
    sync_reports = []
    selected_by_path = {
        (run_root / str(sensor["folder"])).resolve(): sensor
        for sensor in request_value["sensors"]
    }
    required_frame_sources: dict[str, str] = {}
    required_robot_sources: dict[str, str] = {}
    for sensor_path, sensor in selected_by_path.items():
        sensor_policy = _timestamp_policy_for_sensor(timestamp_policy, sensor)
        sensor_name = str(sensor.get("sensor_name") or sensor_path.name)
        required_frame_sources[sensor_name] = str(
            sensor_policy["frame_timestamp_source"]
        )
        required_robot_sources[sensor_name] = str(
            sensor_policy["robot_timestamp_source"]
        )
        loaded_robot_poses = _robot_poses_for_sensor(
            run_root, sensor, robot_pose_artifacts
        )
        results = synchronize_run(
            run_root,
            sensor_folders=[sensor_path],
            output_root=output_root,
            sync_delta=ATTEMPT_SYNC_DELTA_MS,
            timestamp_source=sensor_policy["frame_timestamp_source"],
            robot_timestamp_source=sensor_policy["robot_timestamp_source"],
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
            raw_robot_poses=loaded_robot_poses,
        )
        for result in results:
            selected_sensor = selected_by_path[Path(result.sensor_folder).resolve()]
            sensor_key = str(selected_sensor["sensor_key"])
            synchronized[sensor_key] = Path(result.output_folder).resolve()
            # synchronize_run mirrors the caller's path style. Normalizing here is
            # essential because build_sync_quality_report interprets explicit
            # relative report paths relative to run_root.
            sync_reports.append(Path(result.report_path).resolve())
    sync_quality = build_sync_quality_report(
        run_root,
        report_paths=sync_reports,
        max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        require_timestamp_source=required_frame_sources,
        require_robot_timestamp_source=required_robot_sources,
    )
    sync_quality["calibration_attempt_policy"] = {
        "sync_delta_ms": ATTEMPT_SYNC_DELTA_MS,
        **timestamp_policy,
        "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
        "warning_nearest_pose_delta_ms": warning_nearest_pose_delta_ms,
        "historical_per_sensor_offsets_allowed": False,
    }
    _append_nearest_pose_warning_checks(
        sync_quality,
        warning_threshold_ms=warning_nearest_pose_delta_ms,
    )
    sensor_summaries = sync_quality.get("sensors")
    sync_delta_checks: list[dict[str, Any]] = []
    if not isinstance(sensor_summaries, list) or len(sensor_summaries) != len(
        synchronized
    ):
        sync_delta_checks.append(
            {
                "name": "calibration_sync_delta_evidence",
                "status": "error",
                "message": "Sync delta evidence is missing for selected cameras.",
                "details": {
                    "expected_sensor_count": len(synchronized),
                    "actual_sensor_count": (
                        len(sensor_summaries)
                        if isinstance(sensor_summaries, list)
                        else 0
                    ),
                },
            }
        )
    else:
        for sensor in sensor_summaries:
            sensor_name = (
                str(sensor.get("sensor_name"))
                if isinstance(sensor, Mapping)
                else "unknown"
            )
            actual_value = (
                sensor.get("sync_delta_ms") if isinstance(sensor, Mapping) else None
            )
            try:
                actual_delta_ms = float(actual_value)
            except (TypeError, ValueError):
                actual_delta_ms = math.nan
            delta_ok = math.isfinite(actual_delta_ms) and math.isclose(
                actual_delta_ms,
                ATTEMPT_SYNC_DELTA_MS,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            sync_delta_checks.append(
                {
                    "name": f"calibration_sync_delta:{sensor_name}",
                    "status": "ok" if delta_ok else "error",
                    "message": (
                        f"{sensor_name} used the required zero sync offset."
                        if delta_ok
                        else (
                            f"{sensor_name} sync offset {actual_value!r} ms "
                            "does not equal the required 0.0 ms."
                        )
                    ),
                    "details": {
                        "actual_sync_delta_ms": actual_value,
                        "required_sync_delta_ms": ATTEMPT_SYNC_DELTA_MS,
                    },
                }
            )
    quality_checks = sync_quality.get("checks")
    if not isinstance(quality_checks, list):
        quality_checks = []
        sync_quality["checks"] = quality_checks
    quality_checks.extend(sync_delta_checks)
    _refresh_sync_quality_status(sync_quality)
    atomic_write_json(attempt_root / SYNC_QUALITY_REPORT, sync_quality)
    checks = quality_checks
    blocking_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and (
            check.get("status") == "error"
            or (
                str(check.get("name", "")).startswith("sync_nearest_pose_delta:")
                and check.get("status") != "ok"
            )
        )
    ]
    timestamp_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and str(check.get("name", "")).startswith("sync_timestamp_source:")
    ]
    robot_timestamp_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and str(check.get("name", "")).startswith("sync_robot_timestamp_source:")
    ]
    nearest_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and str(check.get("name", "")).startswith("sync_nearest_pose_delta:")
    ]
    if (
        blocking_checks
        or len(timestamp_checks) != len(synchronized)
        or len(robot_timestamp_checks) != len(synchronized)
        or len(nearest_checks) != len(synchronized)
    ):
        names = [str(check.get("name")) for check in blocking_checks]
        raise ValueError(
            "Selected-camera synchronization quality failed strict "
            "eye-in-hand policy" + (f": {', '.join(names)}" if names else "")
        )
    profiles, by_sensor = _intrinsics_for_sensors(
        run_root,
        attempt_root,
        synchronized,
        request_value,
    )
    write_intrinsic_profile_collection(
        profiles,
        attempt_root / INTRINSIC_CALIBRATION_PROFILES,
    )
    return synchronized, by_sensor


def _projection(profile: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    native = profile.get("native")
    if not isinstance(native, Mapping):
        raise ValueError("Intrinsic profile has no native projection")
    if not projection_is_opencv_compatible(native):
        raise ValueError(
            "Intrinsic SDK distortion model is not a supported forward OpenCV projection"
        )
    return (
        np.asarray(native["cam_K"], dtype=float).reshape(3, 3),
        np.asarray(native["distortion"], dtype=float).reshape(-1),
    )


def _pose_vectors(
    transform_value: Mapping[str, Any],
) -> tuple[list[float], list[float]]:
    transform = transform_from_record(transform_value)
    rvec, _ = cv2.Rodrigues(transform[:3, :3])
    return (
        np.asarray(rvec, dtype=float).reshape(3).tolist(),
        np.asarray(transform[:3, 3], dtype=float).reshape(3).tolist(),
    )


def _coverage_cell(
    centroid: Any,
    image_size: Any,
) -> int | None:
    if (
        not isinstance(centroid, list)
        or len(centroid) != 2
        or not isinstance(image_size, list)
        or len(image_size) != 2
    ):
        return None
    width, height = (float(value) for value in image_size)
    x, y = (float(value) for value in centroid)
    if (
        width <= 0.0
        or height <= 0.0
        or not all(np.isfinite(value) for value in (x, y, width, height))
    ):
        return None
    column = min(2, max(0, int(x * 3.0 / width)))
    row = min(2, max(0, int(y * 3.0 / height)))
    return column + 3 * row


def _pnp_point_marker_metadata(
    detection: Mapping[str, Any],
    target: Mapping[str, Any],
    point_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    ids = detection.get("ids")
    if not isinstance(ids, list) or not ids:
        raise ValueError("PnP detection requires marker IDs")
    marker_positions = {
        int(marker["id"]): divmod(index, int(target["grid_size"][0]))
        for index, marker in enumerate(target["markers"])
    }
    try:
        point_marker_ids = np.repeat(
            np.asarray([int(value) for value in ids], dtype=np.int64), 4
        )
        point_grid_indices = np.repeat(
            np.asarray(
                [marker_positions[int(value)] for value in ids],
                dtype=np.int64,
            ),
            4,
            axis=0,
        )
    except KeyError as exc:
        raise ValueError(f"Detection includes marker outside target: {exc}") from exc
    if len(point_marker_ids) != point_count:
        raise ValueError("PnP marker metadata does not align with matched points")
    return point_marker_ids, point_grid_indices


def _estimate_target_poses(
    attempt_root: Path,
    request_value: Mapping[str, Any],
    synchronized: Mapping[str, Path],
    intrinsics: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    target = normalize_calibration_target_spec(request_value["target"])
    _dictionary, board = opencv_grid_board(target)
    evidence = {
        "schema_version": "calibration_pnp_candidates.v1",
        "attempt_id": request_value["attempt_id"],
        "target": target_identity(target),
        "methods": list(request_value["pnp_methods"]),
        "sensors": [],
    }
    observations: dict[str, dict[str, list[dict[str, Any]]]] = {}
    sensor_metadata = {
        str(item["sensor_key"]): item for item in request_value["sensors"]
    }
    for sensor_key, folder in synchronized.items():
        detection_path = folder / ARUCO_DETECTIONS
        if detection_path.is_file():
            detections = _read_json(detection_path)
            validate_target_identity(
                detections.get("target"), target, label="ArUco detections"
            )
        else:
            detections = detect_sensor_folder(
                folder,
                target,
                output_path=detection_path,
            )
        matrix, distortion = _projection(intrinsics[sensor_key])
        matched = _read_json(folder / MATCH_ROBOT_EE_POSES)
        frames = []
        method_observations = {method: [] for method in request_value["pnp_methods"]}
        compatibility_output: dict[str, Any] = {}
        for frame_id, detection in sorted(detections.get("frames", {}).items()):
            frame_record: dict[str, Any] = {
                "frame_id": frame_id,
                "source_frame_id": (
                    matched.get(frame_id, {}).get("source_frame_id")
                    if isinstance(matched.get(frame_id), Mapping)
                    else None
                ),
                "marker_count": int(detection.get("marker_count", 0)),
                "image_centroid_px": detection.get("image_centroid_px"),
                "image_size": detections.get("image_size"),
                "image_coverage_cell": _coverage_cell(
                    detection.get("image_centroid_px"),
                    detections.get("image_size"),
                ),
                "status": "rejected",
                "candidates": [],
                "failures": [],
            }
            matched_pose = matched.get(frame_id)
            if not isinstance(matched_pose, Mapping):
                frame_record["failures"].append({"reason": "missing_robot_pose"})
                frames.append(frame_record)
                continue
            points = _matched_points(detection, board)
            if points is None or int(detection.get("marker_count", 0)) < 4:
                frame_record["failures"].append(
                    {"reason": "insufficient_board_markers"}
                )
                frames.append(frame_record)
                continue
            try:
                point_marker_ids, point_grid_indices = _pnp_point_marker_metadata(
                    detection,
                    target,
                    len(points[0]),
                )
                solved = solve_planar_pnp_candidates(
                    points[0],
                    points[1],
                    matrix,
                    distortion,
                    methods=request_value["pnp_methods"],
                    point_marker_ids=point_marker_ids,
                    point_grid_indices=point_grid_indices,
                )
            except (cv2.error, ValueError, TypeError) as exc:
                frame_record["failures"].append({"reason": str(exc)})
                frames.append(frame_record)
                continue
            frame_record.update(
                {
                    "status": "ok" if solved["selected"] else "rejected",
                    "detected_image_centroid_px": detection.get(
                        "image_centroid_px"
                    ),
                    "image_centroid_px": solved[
                        "consensus_image_centroid_px"
                    ],
                    "image_coverage_cell": _coverage_cell(
                        solved["consensus_image_centroid_px"],
                        detections.get("image_size"),
                    ),
                    "common_inlier_indices": solved["common_inlier_indices"],
                    "common_inlier_count": solved["common_inlier_count"],
                    "common_inlier_ratio": solved["common_inlier_ratio"],
                    "raw_common_inlier_ratio": solved[
                        "raw_common_inlier_ratio"
                    ],
                    "common_inlier_ratio_basis": solved[
                        "common_inlier_ratio_basis"
                    ],
                    "correspondence_count": solved["correspondence_count"],
                    "duplicate_marker_clutter_filtered": solved[
                        "duplicate_marker_clutter_filtered"
                    ],
                    "duplicate_marker_ids": solved["duplicate_marker_ids"],
                    "marker_detection_counts": solved[
                        "marker_detection_counts"
                    ],
                    "ignored_clutter_correspondence_count": solved[
                        "ignored_clutter_correspondence_count"
                    ],
                    "supported_marker_ids": solved["supported_marker_ids"],
                    "supported_marker_count": solved["supported_marker_count"],
                    "supported_marker_corner_counts": solved[
                        "supported_marker_corner_counts"
                    ],
                    "supported_grid_rows": solved["supported_grid_rows"],
                    "supported_grid_columns": solved["supported_grid_columns"],
                    "quality_thresholds": solved["thresholds"],
                    "candidates": solved["candidates"],
                    "failures": solved["failures"],
                }
            )
            for method, selected in solved["selected"].items():
                method_observations[method].append(
                    {
                        "observation_id": f"{sensor_key}:{method}:{frame_id}",
                        "frame_id": frame_id,
                        "source_frame_id": matched_pose.get("source_frame_id"),
                        "image_timestamp_ns": matched_pose.get("image_timestamp_ns"),
                        "initial_matched_robot_pose_index": matched_pose.get(
                            "matched_robot_pose_index"
                        ),
                        "initial_robot_timestamp_ns": matched_pose.get(
                            "robot_timestamp_ns"
                        ),
                        "initial_nearest_robot_delta_ns": matched_pose.get(
                            "nearest_robot_delta_ns"
                        ),
                        "motion": matched_pose.get("motion"),
                        "robot_ee_pose": dict(matched_pose["robot_ee_pose"]),
                        "target_to_camera": selected["transform"],
                        "mean_reprojection_error_px": selected[
                            "mean_reprojection_error_px"
                        ],
                        "pnp_common_inlier_count": solved["common_inlier_count"],
                        "pnp_common_inlier_ratio": solved["common_inlier_ratio"],
                        "pnp_raw_common_inlier_ratio": solved[
                            "raw_common_inlier_ratio"
                        ],
                        "pnp_common_inlier_ratio_basis": solved[
                            "common_inlier_ratio_basis"
                        ],
                        "pnp_correspondence_count": solved["correspondence_count"],
                        "pnp_duplicate_marker_clutter_filtered": solved[
                            "duplicate_marker_clutter_filtered"
                        ],
                        "pnp_ignored_clutter_correspondence_count": solved[
                            "ignored_clutter_correspondence_count"
                        ],
                        "pnp_quality_reprojection_scope": selected[
                            "quality_reprojection_scope"
                        ],
                        "pnp_supported_marker_ids": solved["supported_marker_ids"],
                        "pnp_supported_grid_rows": solved["supported_grid_rows"],
                        "pnp_supported_grid_columns": solved["supported_grid_columns"],
                        "all_point_mean_reprojection_error_px": selected[
                            "all_point_mean_reprojection_error_px"
                        ],
                        "image_centroid_px": solved[
                            "consensus_image_centroid_px"
                        ],
                        "detected_image_centroid_px": detection.get(
                            "image_centroid_px"
                        ),
                        "image_size": detections.get("image_size"),
                        "image_coverage_cell": _coverage_cell(
                            solved["consensus_image_centroid_px"],
                            detections.get("image_size"),
                        ),
                    }
                )
            preferred = solved["selected"].get("ITERATIVE") or next(
                iter(solved["selected"].values()), None
            )
            if preferred is not None:
                rvec, tvec = _pose_vectors(preferred["transform"])
                compatibility_output[frame_id] = {
                    **dict(matched_pose),
                    "aruco_pose_estimation": {
                        "schema_version": "aruco_pose_estimation.v2",
                        "rvec": rvec,
                        "tvec": tvec,
                        "len_ids": int(detection.get("marker_count", 0)),
                        "pnp_inlier_indices": solved["common_inlier_indices"],
                        "pnp_inlier_count": solved["common_inlier_count"],
                        "pnp_inlier_ratio": solved["common_inlier_ratio"],
                        "pnp_raw_inlier_ratio": solved[
                            "raw_common_inlier_ratio"
                        ],
                        "pnp_inlier_ratio_basis": solved[
                            "common_inlier_ratio_basis"
                        ],
                        "duplicate_marker_clutter_filtered": solved[
                            "duplicate_marker_clutter_filtered"
                        ],
                        "ignored_clutter_correspondence_count": solved[
                            "ignored_clutter_correspondence_count"
                        ],
                        "mean_reprojection_error_px": preferred[
                            "mean_reprojection_error_px"
                        ],
                        "max_reprojection_error_px": preferred[
                            "max_reprojection_error_px"
                        ],
                        "all_point_mean_reprojection_error_px": preferred[
                            "all_point_mean_reprojection_error_px"
                        ],
                        "target": target_identity(target),
                    },
                }
            frames.append(frame_record)
        target_marker_count = len(target["markers"])
        target_columns, target_rows = (int(value) for value in target["grid_size"])
        accepted_frames = [item for item in frames if item["status"] == "ok"]
        accepted_marker_ids = sorted(
            {
                int(marker_id)
                for item in accepted_frames
                for marker_id in item.get("supported_marker_ids", [])
            }
        )
        accepted_grid_rows = sorted(
            {
                int(row)
                for item in accepted_frames
                for row in item.get("supported_grid_rows", [])
            }
        )
        accepted_grid_columns = sorted(
            {
                int(column)
                for item in accepted_frames
                for column in item.get("supported_grid_columns", [])
            }
        )
        dataset_support_thresholds = {
            "min_target_markers": math.ceil(
                target_marker_count * ATTEMPT_MIN_TARGET_MARKER_COVERAGE_RATIO
            ),
            "min_target_rows": math.ceil(
                target_rows * ATTEMPT_MIN_TARGET_ROW_COVERAGE_RATIO
            ),
            "min_target_columns": math.ceil(
                target_columns * ATTEMPT_MIN_TARGET_COLUMN_COVERAGE_RATIO
            ),
            "min_target_marker_coverage_ratio": (
                ATTEMPT_MIN_TARGET_MARKER_COVERAGE_RATIO
            ),
            "min_target_row_coverage_ratio": (ATTEMPT_MIN_TARGET_ROW_COVERAGE_RATIO),
            "min_target_column_coverage_ratio": (
                ATTEMPT_MIN_TARGET_COLUMN_COVERAGE_RATIO
            ),
        }
        dataset_support_ok = (
            len(accepted_marker_ids) >= dataset_support_thresholds["min_target_markers"]
            and len(accepted_grid_rows) >= dataset_support_thresholds["min_target_rows"]
            and len(accepted_grid_columns)
            >= dataset_support_thresholds["min_target_columns"]
        )
        dataset_marker_support = {
            "status": "ok" if dataset_support_ok else "error",
            "accepted_marker_ids": accepted_marker_ids,
            "accepted_marker_count": len(accepted_marker_ids),
            "accepted_grid_rows": accepted_grid_rows,
            "accepted_grid_columns": accepted_grid_columns,
            "target_marker_count": target_marker_count,
            "target_row_count": target_rows,
            "target_column_count": target_columns,
            "thresholds": dataset_support_thresholds,
        }
        if not dataset_support_ok:
            # Retain frame-level PnP evidence but do not permit a small fixed
            # patch of the board to reach the hand-eye solver.
            method_observations = {
                method: [] for method in request_value["pnp_methods"]
            }
        atomic_write_json(folder / ARUCO_POSE_ESTIMATION, compatibility_output)
        observations[sensor_key] = method_observations
        evidence["sensors"].append(
            {
                **sensor_metadata[sensor_key],
                "frame_count": len(frames),
                "solved_frame_count": sum(
                    1 for item in frames if item["status"] == "ok"
                ),
                "dataset_marker_support": dataset_marker_support,
                "accepted_coverage_cells": sorted(
                    {
                        int(item["image_coverage_cell"])
                        for item in frames
                        if item["status"] == "ok"
                        and item.get("image_coverage_cell") is not None
                    }
                ),
                "frames": frames,
            }
        )
    atomic_write_json(attempt_root / PNP_CANDIDATES_FILE, evidence)
    return evidence, observations


def _calibration_observation_report(
    request_value: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    flat = []
    sensors = []
    sensor_metadata = {
        str(item["sensor_key"]): item for item in request_value["sensors"]
    }
    for sensor_key, by_method in observations.items():
        count = sum(len(items) for items in by_method.values())
        sensors.append(
            {
                **sensor_metadata[sensor_key],
                "observation_count": count,
                "pnp_method_counts": {
                    method: len(items) for method, items in by_method.items()
                },
            }
        )
        for method, items in by_method.items():
            for item in items:
                flat.append(
                    {
                        **dict(item),
                        "sensor_name": sensor_metadata[sensor_key]["sensor_name"],
                        "sensor_type": sensor_metadata[sensor_key]["sensor_type"],
                        "device_id": sensor_metadata[sensor_key]["device_id"],
                        "mounting_mode": (
                            "eye_in_hand"
                            if request_value["mode"] == "eye_in_hand"
                            else "static"
                        ),
                        "pnp_method": method,
                    }
                )
    return {
        "schema_version": "calibration_observations.v1",
        "generated_at": utc_now_iso(),
        "run_root": request_value["run_root"],
        "attempt_id": request_value["attempt_id"],
        "overall_status": "ok" if flat else "error",
        "target": request_value["target"],
        "board": request_value["target"],
        "sensor_count": len(sensors),
        "observation_count": len(flat),
        "sensors": sensors,
        "observations": flat,
        "checks": [],
        "rejected": [],
    }


def _estimate_and_apply_time_offsets(
    run_root: Path,
    attempt_root: Path,
    request_value: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    robot_pose_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    if robot_pose_artifacts is None:
        robot_pose_artifacts = _verify_robot_pose_artifact_bindings(
            run_root, request_value
        )
    policy = str(
        request_value.get(
            "synchronization_policy",
            DEFAULT_SYNCHRONIZATION_POLICY,
        )
    )
    timestamp_policy = request_value.get("timestamp_policy")
    search = request_value.get("synchronization_search")
    search_configuration = (
        dict(search)
        if isinstance(search, Mapping)
        else time_offset_search_configuration()
    )
    implementation_revision = str(
        request_value.get(
            "synchronization_implementation_revision",
            TIME_OFFSET_IMPLEMENTATION_REVISION,
        )
    )
    if implementation_revision not in TIME_OFFSET_SUPPORTED_REVISIONS:
        raise ValueError(
            "Unsupported calibration time-offset implementation revision: "
            f"{implementation_revision}"
        )
    improvement_evidence_strategy = IMPROVEMENT_EVIDENCE_STRATEGY
    failure_policy = FAILURE_POLICY_WARN_KEEP_ZERO
    recorded_failure_policy = search_configuration.get("time_offset_failure_policy")
    if recorded_failure_policy != failure_policy:
        raise ValueError(
            "Current calibration time-offset attempts must record the warning-based "
            "recorded-timing fallback policy"
        )
    max_lomo_search_adjusted_sign_p_value = float(
        search_configuration.get(
            "maximum_leave_one_motion_out_search_adjusted_sign_p_value",
            DEFAULT_MAX_LOMO_SEARCH_ADJUSTED_SIGN_P_VALUE,
        )
    )
    if (
        not math.isfinite(max_lomo_search_adjusted_sign_p_value)
        or not 0.0 < max_lomo_search_adjusted_sign_p_value <= 1.0
    ):
        raise ValueError(
            "Calibration time-offset leave-one-motion-out maximum "
            "search-adjusted sign p-value must be in (0, 1]"
        )
    max_nearest_pose_delta_ms = float(search_configuration["max_nearest_pose_delta_ms"])
    if not math.isfinite(max_nearest_pose_delta_ms) or max_nearest_pose_delta_ms <= 0.0:
        raise ValueError(
            "Calibration time-offset max nearest-pose delta must be positive"
        )
    warning_nearest_pose_delta_ms = float(
        search_configuration.get(
            "warning_nearest_pose_delta_ms",
            min(DEFAULT_WARNING_NEAREST_POSE_DELTA_MS, max_nearest_pose_delta_ms),
        )
    )
    if (
        not math.isfinite(warning_nearest_pose_delta_ms)
        or warning_nearest_pose_delta_ms <= 0.0
        or warning_nearest_pose_delta_ms > max_nearest_pose_delta_ms
    ):
        raise ValueError(
            "Calibration nearest-pose warning threshold must be positive and no "
            "greater than the maximum"
        )
    raw_warning_offset_ms = search_configuration.get(
        "warning_absolute_robot_pose_time_offset_ms"
    )
    warning_abs_offset_ms = (
        float(raw_warning_offset_ms) if raw_warning_offset_ms is not None else None
    )
    if warning_abs_offset_ms is not None and (
        not math.isfinite(warning_abs_offset_ms) or warning_abs_offset_ms <= 0.0
    ):
        raise ValueError(
            "Calibration robot-pose time-offset warning threshold must be positive"
        )
    sensor_metadata = {
        str(item["sensor_key"]): item for item in request_value["sensors"]
    }
    adjusted: dict[str, dict[str, list[dict[str, Any]]]] = {}
    sensor_results: list[dict[str, Any]] = []
    failed: list[str] = []
    for sensor_key in request_value["sensor_keys"]:
        by_method = observations[sensor_key]
        reference_observations = list(by_method.get(DEFAULT_REFERENCE_PNP_METHOD, ()))
        sensor = sensor_metadata[sensor_key]
        if policy == "fixed_zero":
            sensor_result = fixed_zero_sensor_result(
                sensor_key=sensor_key,
                observation_count=max(
                    (len(items) for items in by_method.values()),
                    default=0,
                ),
            )
            adjusted[sensor_key] = {
                method: [dict(item) for item in items]
                for method, items in by_method.items()
            }
        else:
            robot_records: list[dict[str, Any]] = []
            try:
                sensor_policy = _timestamp_policy_for_sensor(
                    timestamp_policy
                    if isinstance(timestamp_policy, Mapping)
                    else _attempt_timestamp_policy(request_value["sensors"]),
                    sensor,
                )
                loaded_robot_poses = _robot_poses_for_sensor(
                    run_root, sensor, robot_pose_artifacts
                )
                robot_records = indexed_robot_poses(
                    loaded_robot_poses,
                    timestamp_source=str(sensor_policy["robot_timestamp_source"]),
                )
                if not reference_observations:
                    raise ValueError(
                        f"{sensor_key}: auto-sync reference observations are missing"
                    )
                sensor_result, _reference_adjusted = estimate_sensor_time_offset(
                    reference_observations,
                    sensor_key=sensor_key,
                    robot_records=robot_records,
                    mode=str(request_value["mode"]),
                    offsets_ms=time_offset_values(
                        float(
                            search_configuration["minimum_robot_pose_time_offset_ms"]
                        ),
                        float(
                            search_configuration["maximum_robot_pose_time_offset_ms"]
                        ),
                        float(search_configuration["step_ms"]),
                    ),
                    methods=tuple(
                        str(item)
                        for item in search_configuration["reference_extrinsic_methods"]
                    ),
                    max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
                    warning_nearest_pose_delta_ms=warning_nearest_pose_delta_ms,
                    warning_abs_offset_ms=warning_abs_offset_ms,
                    max_observations_per_motion=int(
                        search_configuration["max_observations_per_motion"]
                    ),
                    max_search_motions=int(
                        search_configuration["maximum_search_motion_count"]
                    ),
                    min_motions_per_fold=int(
                        search_configuration[
                            "minimum_motion_count_per_cross_validation_fold"
                        ]
                    ),
                    min_absolute_improvement_mm=float(
                        search_configuration[
                            "minimum_absolute_cross_validated_improvement_mm"
                        ]
                    ),
                    min_relative_improvement=float(
                        search_configuration[
                            "minimum_relative_cross_validated_improvement"
                        ]
                    ),
                    max_rotation_degradation_deg=float(
                        search_configuration[
                            "maximum_cross_validated_rotation_degradation_deg"
                        ]
                    ),
                    minimum_offset_stability_ms=float(
                        search_configuration["minimum_offset_stability_ms"]
                    ),
                    improvement_evidence_strategy=improvement_evidence_strategy,
                    failure_policy=failure_policy,
                    max_leave_one_motion_out_search_adjusted_sign_p_value=(
                        max_lomo_search_adjusted_sign_p_value
                    ),
                )
                selected_offset_ms = float(
                    sensor_result["selected_robot_pose_time_offset_ms"]
                )
                adjusted[sensor_key] = {
                    method: apply_sensor_time_offset(
                        items,
                        robot_records=robot_records,
                        robot_pose_time_offset_ms=selected_offset_ms,
                        max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
                    )
                    for method, items in by_method.items()
                }
            except Exception as exc:
                sensor_result = failed_sensor_result(
                    sensor_key=sensor_key,
                    observation_count=len(reference_observations),
                    error=exc,
                )
                adjusted[sensor_key] = {}
            if sensor_result["status"] == "failed":
                failed.append(sensor_key)
        sensor_result["display_name"] = sensor.get("display_name")
        sensor_result["sensor_name"] = sensor.get("sensor_name")
        sensor_results.append(sensor_result)

    warning_sensor_keys = [
        str(item["sensor_key"])
        for item in sensor_results
        if any(
            isinstance(check, Mapping) and check.get("status") == "warning"
            for check in item.get("checks", [])
        )
    ]
    report = {
        "schema_version": TIME_OFFSET_SEARCH_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "attempt_id": request_value["attempt_id"],
        "policy": policy,
        "implementation_revision": implementation_revision,
        "offset_kind": "effective_capture_and_pose_pipeline_latency",
        "sign_convention": time_offset_sign_convention(),
        "search": search_configuration,
        "status": "failed" if failed else "complete",
        "sensor_count": len(sensor_results),
        "failed_sensor_keys": failed,
        "warning_sensor_keys": warning_sensor_keys,
        "warning_sensor_count": len(warning_sensor_keys),
        "sensors": sensor_results,
    }
    atomic_write_json(attempt_root / TIME_OFFSET_SEARCH, report)
    if failed:
        failed_details = []
        for item in sensor_results:
            sensor_key = str(item.get("sensor_key") or "")
            if sensor_key not in failed:
                continue
            error_checks = [
                str(check.get("name"))
                for check in item.get("checks", [])
                if isinstance(check, Mapping) and check.get("status") == "error"
            ]
            failed_details.append(
                sensor_key + (f" ({', '.join(error_checks)})" if error_checks else "")
            )
        message = "Auto-sync input could not be evaluated for: " + ", ".join(
            failed_details
        )
        raise ValueError(message)
    return report, adjusted


def _materialize_authoritative_synchronization(
    run_root: Path,
    attempt_root: Path,
    request_value: Mapping[str, Any],
    time_offset_search: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    robot_pose_artifacts: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[
    dict[str, Path],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    if robot_pose_artifacts is None:
        robot_pose_artifacts = _verify_robot_pose_artifact_bindings(
            run_root, request_value
        )
    timestamp_policy = _calibration_timestamp_preflight(
        run_root,
        request_value["sensors"],
        robot_pose_artifacts or None,
    )
    result_by_sensor = {
        str(item["sensor_key"]): item
        for item in time_offset_search.get("sensors", [])
        if isinstance(item, Mapping) and item.get("sensor_key")
    }
    expected_sensor_keys = {str(item) for item in request_value["sensor_keys"]}
    if set(result_by_sensor) != expected_sensor_keys:
        raise ValueError("Time-offset evidence does not cover every selected sensor")
    try:
        max_nearest_pose_delta_ms = float(
            time_offset_search["search"]["max_nearest_pose_delta_ms"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Time-offset evidence lacks a valid max nearest-pose delta"
        ) from exc
    if not math.isfinite(max_nearest_pose_delta_ms) or max_nearest_pose_delta_ms <= 0.0:
        raise ValueError("Time-offset evidence max nearest-pose delta must be positive")
    raw_warning_nearest_pose_delta_ms = time_offset_search["search"].get(
        "warning_nearest_pose_delta_ms"
    )
    warning_nearest_pose_delta_ms = (
        float(raw_warning_nearest_pose_delta_ms)
        if raw_warning_nearest_pose_delta_ms is not None
        else None
    )
    if warning_nearest_pose_delta_ms is not None and (
        not math.isfinite(warning_nearest_pose_delta_ms)
        or warning_nearest_pose_delta_ms <= 0.0
        or warning_nearest_pose_delta_ms > max_nearest_pose_delta_ms
    ):
        raise ValueError("Time-offset nearest-pose warning threshold is invalid")

    output_root = attempt_root / "processed" / "synchronized"
    synchronized: dict[str, Path] = {}
    sync_reports: list[Path] = []
    required_frame_sources: dict[str, str] = {}
    required_robot_sources: dict[str, str] = {}
    expected_by_sensor_name: dict[str, float] = {}
    for sensor in request_value["sensors"]:
        sensor_key = str(sensor["sensor_key"])
        sensor_path = run_root / str(sensor["folder"])
        sensor_policy = _timestamp_policy_for_sensor(timestamp_policy, sensor)
        sensor_name = str(sensor.get("sensor_name") or sensor_path.name)
        selected_sync_delta_ms = float(
            result_by_sensor[sensor_key]["selected_sync_delta_ms"]
        )
        required_frame_sources[sensor_name] = str(
            sensor_policy["frame_timestamp_source"]
        )
        required_robot_sources[sensor_name] = str(
            sensor_policy["robot_timestamp_source"]
        )
        expected_by_sensor_name[sensor_name] = selected_sync_delta_ms
        loaded_robot_poses = _robot_poses_for_sensor(
            run_root, sensor, robot_pose_artifacts
        )
        results = synchronize_run(
            run_root,
            sensor_folders=[sensor_path],
            output_root=output_root,
            sync_delta=selected_sync_delta_ms,
            timestamp_source=sensor_policy["frame_timestamp_source"],
            robot_timestamp_source=sensor_policy["robot_timestamp_source"],
            copy_files=False,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
            raw_robot_poses=loaded_robot_poses,
        )
        if len(results) != 1:
            raise ValueError(
                f"Authoritative synchronization returned no result for {sensor_key}"
            )
        result = results[0]
        synchronized[sensor_key] = Path(result.output_folder).resolve()
        sync_reports.append(Path(result.report_path).resolve())

    sync_quality = build_sync_quality_report(
        run_root,
        report_paths=sync_reports,
        max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        require_timestamp_source=required_frame_sources,
        require_robot_timestamp_source=required_robot_sources,
    )
    sync_quality["calibration_attempt_policy"] = {
        "purpose": "authoritative_calibration_solver_pairing",
        "synchronization_policy": time_offset_search["policy"],
        "time_offset_search": _attempt_artifact_reference(
            str(request_value["attempt_id"]),
            TIME_OFFSET_SEARCH,
        ),
        "sign_convention": time_offset_search["sign_convention"],
        **timestamp_policy,
        "per_sensor_offsets": {
            sensor_key: {
                "robot_pose_time_offset_ms": float(
                    value["selected_robot_pose_time_offset_ms"]
                ),
                "sync_delta_ms": float(value["selected_sync_delta_ms"]),
                "status": value["status"],
            }
            for sensor_key, value in result_by_sensor.items()
        },
        "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
        "warning_nearest_pose_delta_ms": warning_nearest_pose_delta_ms,
        "historical_per_sensor_offsets_allowed": False,
        "auto_estimated_per_sensor_offsets": (
            time_offset_search["policy"] == "auto_offset"
        ),
        "timing_warning_sensor_keys": list(
            time_offset_search.get("warning_sensor_keys", [])
        ),
        "warning_fallback_sensor_keys": sorted(
            sensor_key
            for sensor_key, value in result_by_sensor.items()
            if value.get("warning_fallback_used") is True
        ),
    }
    checks = sync_quality.get("checks")
    if not isinstance(checks, list):
        checks = []
        sync_quality["checks"] = checks
    _append_nearest_pose_warning_checks(
        sync_quality,
        warning_threshold_ms=warning_nearest_pose_delta_ms,
    )
    summaries = sync_quality.get("sensors")
    observed_names: set[str] = set()
    if isinstance(summaries, list):
        for summary in summaries:
            if not isinstance(summary, Mapping):
                continue
            sensor_name = str(summary.get("sensor_name") or "")
            observed_names.add(sensor_name)
            expected = expected_by_sensor_name.get(sensor_name)
            try:
                actual = float(summary["sync_delta_ms"])
            except (KeyError, TypeError, ValueError):
                actual = None
            matched = (
                expected is not None
                and actual is not None
                and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)
            )
            checks.append(
                {
                    "name": f"calibration_authoritative_sync_delta:{sensor_name}",
                    "status": "ok" if matched else "error",
                    "message": (
                        f"{sensor_name} authoritative sync delta is {actual:g} ms."
                        if matched
                        else (
                            f"{sensor_name} authoritative sync delta "
                            f"{actual!r} does not match {expected!r} ms."
                        )
                    ),
                    "details": {
                        "actual_sync_delta_ms": actual,
                        "expected_sync_delta_ms": expected,
                    },
                }
            )
    missing_names = sorted(set(expected_by_sensor_name) - observed_names)
    for sensor_name in missing_names:
        checks.append(
            {
                "name": f"calibration_authoritative_sync_delta:{sensor_name}",
                "status": "error",
                "message": "Authoritative sync-delta evidence is missing.",
            }
        )
    _refresh_sync_quality_status(sync_quality)
    atomic_write_json(attempt_root / SYNC_QUALITY_REPORT, sync_quality)
    blocking = [
        item
        for item in checks
        if isinstance(item, Mapping)
        and (
            item.get("status") == "error"
            or (
                str(item.get("name", "")).startswith("sync_nearest_pose_delta:")
                and item.get("status") != "ok"
            )
        )
    ]
    if blocking:
        raise ValueError(
            "Authoritative calibration synchronization failed: "
            + ", ".join(str(item.get("name")) for item in blocking)
        )

    remapped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for sensor_key, by_method in observations.items():
        matched = _read_json(synchronized[sensor_key] / MATCH_ROBOT_EE_POSES)
        source_matches: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for final_frame_id, value in matched.items():
            if not isinstance(value, Mapping):
                continue
            source_frame_id = str(value.get("source_frame_id") or "")
            if not source_frame_id:
                continue
            if source_frame_id in source_matches:
                raise ValueError(
                    f"{sensor_key}: duplicate authoritative source frame "
                    f"{source_frame_id}"
                )
            source_matches[source_frame_id] = (final_frame_id, value)
        remapped[sensor_key] = {}
        selected = result_by_sensor[sensor_key]
        for method, items in by_method.items():
            remapped_items = []
            for item in items:
                source_frame_id = str(item.get("source_frame_id") or "")
                final_match = source_matches.get(source_frame_id)
                if final_match is None:
                    continue
                final_frame_id, match = final_match
                if int(item["image_timestamp_ns"]) != int(match["image_timestamp_ns"]):
                    raise ValueError(
                        f"{sensor_key}: authoritative timestamp changed for source "
                        f"frame {source_frame_id}"
                    )
                remapped_items.append(
                    {
                        **dict(item),
                        "observation_id": (f"{sensor_key}:{method}:{final_frame_id}"),
                        "frame_id": final_frame_id,
                        "source_frame_id": source_frame_id,
                        "motion": match["motion"],
                        "robot_ee_pose": dict(match["robot_ee_pose"]),
                        "image_timestamp_ns": match["image_timestamp_ns"],
                        "robot_pose_time_offset_ms": float(
                            selected["selected_robot_pose_time_offset_ms"]
                        ),
                        "sync_delta_ms": float(selected["selected_sync_delta_ms"]),
                        "timestamp_alignment": {
                            "frame_timestamp_ns": match["image_timestamp_ns"],
                            "robot_pose_query_timestamp_ns": match[
                                "delayed_timestamp_ns"
                            ],
                            "robot_pose_time_offset_ms": float(
                                selected["selected_robot_pose_time_offset_ms"]
                            ),
                            "sync_delta_ms": float(selected["selected_sync_delta_ms"]),
                            "matched_robot_pose_index": match[
                                "matched_robot_pose_index"
                            ],
                            "robot_timestamp_ns": match["robot_timestamp_ns"],
                            "nearest_robot_delta_ns": match["nearest_robot_delta_ns"],
                            "source": _attempt_artifact_reference(
                                str(request_value["attempt_id"]),
                                TIME_OFFSET_SEARCH,
                            ),
                        },
                    }
                )
            remapped[sensor_key][method] = remapped_items
    return synchronized, remapped


def _compare_solutions(
    attempt_root: Path,
    request_value: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    *,
    time_offset_search: Mapping[str, Any],
) -> list[dict[str, Any]]:
    coverage_thresholds = _image_coverage_thresholds(str(request_value["mode"]))
    alignment_by_sensor = {
        str(item["sensor_key"]): item
        for item in time_offset_search["sensors"]
        if isinstance(item, Mapping)
    }
    candidates = []
    for sensor_key in request_value["sensor_keys"]:
        for pnp_method in request_value["pnp_methods"]:
            method_observations = observations[sensor_key][pnp_method]
            for extrinsic_method in request_value["extrinsic_methods"]:
                candidate = evaluate_extrinsic_candidate(
                    method_observations,
                    mode=request_value["mode"],
                    pnp_method=pnp_method,
                    extrinsic_method=extrinsic_method,
                    sensor_key=sensor_key,
                    min_accepted_views=DEFAULT_MIN_ACCEPTED_VIEWS,
                    min_coverage_cells=DEFAULT_MIN_COVERAGE_CELLS,
                    image_coverage_tail_support_views=(
                        int(
                            coverage_thresholds[
                                "image_coverage_tail_support_views"
                            ]
                        )
                    ),
                    min_image_centroid_x_span_ratio=(
                        float(
                            coverage_thresholds[
                                "min_image_centroid_x_span_ratio"
                            ]
                        )
                    ),
                    min_image_centroid_y_span_ratio=(
                        float(
                            coverage_thresholds[
                                "min_image_centroid_y_span_ratio"
                            ]
                        )
                    ),
                    min_image_centroid_hull_area_ratio=(
                        float(
                            coverage_thresholds[
                                "min_image_centroid_hull_area_ratio"
                            ]
                        )
                    ),
                    min_motion_poses=ATTEMPT_MIN_MOTION_POSES,
                    min_translation_span_mm=(ATTEMPT_MIN_TRANSLATION_SPAN_MM),
                    min_rotation_span_deg=ATTEMPT_MIN_ROTATION_SPAN_DEG,
                )
                candidate["synchronization"] = {
                    "policy": time_offset_search["policy"],
                    "status": alignment_by_sensor[sensor_key]["status"],
                    "warning_fallback_used": bool(
                        alignment_by_sensor[sensor_key].get("warning_fallback_used")
                    ),
                    "robot_pose_time_offset_ms": float(
                        alignment_by_sensor[sensor_key][
                            "selected_robot_pose_time_offset_ms"
                        ]
                    ),
                    "sync_delta_ms": float(
                        alignment_by_sensor[sensor_key]["selected_sync_delta_ms"]
                    ),
                    "source": _attempt_artifact_reference(
                        str(request_value["attempt_id"]),
                        TIME_OFFSET_SEARCH,
                    ),
                }
                candidates.append(candidate)
    report = {
        "schema_version": "calibration_extrinsic_candidates.v1",
        "generated_at": utc_now_iso(),
        "attempt_id": request_value["attempt_id"],
        "mode": request_value["mode"],
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    atomic_write_json(attempt_root / EXTRINSIC_CANDIDATES_FILE, report)
    return candidates


def _camera_intrinsics(profile: Mapping[str, Any]) -> CameraIntrinsics:
    native = profile["native"]
    return CameraIntrinsics(
        cam_k=tuple(float(item) for item in native["cam_K"]),
        width=int(native["width"]),
        height=int(native["height"]),
        distortion=tuple(float(item) for item in native["distortion"]),
        depth_scale_to_mm=float(profile["depth"]["scale_to_mm"]),
        distortion_model=str(native.get("distortion_model", "brown_conrady")),
        projection_source=str(
            profile.get("attempt_intrinsics_source")
            or profile.get("source", {}).get("camera_projection")
            or "attempt_intrinsic_profile"
        ),
    )


def _candidate_profile(
    candidate: Mapping[str, Any],
    *,
    request_value: Mapping[str, Any],
    sensor: Mapping[str, Any],
    intrinsic_profile: Mapping[str, Any],
) -> CalibrationProfile:
    max_nearest_pose_delta_ms, warning_nearest_pose_delta_ms = (
        _recorded_nearest_pose_limits_ms(request_value)
    )
    transform = candidate["primary_transform"]
    quaternion = tuple(float(item) for item in transform["rotation_quaternion_wxyz"])
    translation = tuple(float(item) for item in transform["translation_mm"])
    mode = str(request_value["mode"])
    if mode == "eye_to_hand":
        reference_evidence = request_value.get("robot_pose_reference")
        if not isinstance(reference_evidence, Mapping):
            raise ValueError(
                "Static candidate materialization requires robot-pose reference "
                "evidence from the immutable attempt request"
            )
        _require_static_pose_template_base_evidence(reference_evidence)
    raw_timestamp_policy = request_value.get("timestamp_policy")
    timestamp_policy = (
        dict(raw_timestamp_policy)
        if isinstance(raw_timestamp_policy, Mapping)
        else _attempt_timestamp_policy(request_value["sensors"])
    )
    sensor_timestamp_policy = _timestamp_policy_for_sensor(timestamp_policy, sensor)
    raw_synchronization = candidate.get("synchronization")
    synchronization = (
        dict(raw_synchronization)
        if isinstance(raw_synchronization, Mapping)
        else {
            "policy": DEFAULT_SYNCHRONIZATION_POLICY,
            "status": "fixed_zero",
            "robot_pose_time_offset_ms": 0.0,
            "sync_delta_ms": ATTEMPT_SYNC_DELTA_MS,
            "source": None,
        }
    )
    selected_sync_delta_ms = float(synchronization["sync_delta_ms"])
    mounting = (
        MountingMode.EYE_IN_HAND if mode == "eye_in_hand" else MountingMode.STATIC
    )
    safe_sensor = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sensor["device_id"]))
    safe_method = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        f"{candidate['pnp_method']}_{candidate['extrinsic_method']}",
    )
    profile_id = (
        f"{safe_sensor}_{mode}_{safe_method}_{str(request_value['attempt_id'])[:8]}"
    )
    intrinsics = _camera_intrinsics(intrinsic_profile)
    return CalibrationProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        profile_id=profile_id,
        sensor_id=str(sensor["device_id"]),
        sensor_type=SensorType(str(sensor["sensor_type"])),
        mounting_mode=mounting,
        rig_position="wrist" if mode == "eye_in_hand" else "static",
        intrinsics=intrinsics,
        rectified_intrinsics=rectified_intrinsics_from_native(intrinsics),
        extrinsics=RigidTransform(
            from_frame=TransformFrame.CAMERA,
            to_frame=(
                TransformFrame.ROBOT_FLANGE
                if mode == "eye_in_hand"
                else TransformFrame.TEMPLATE_BASE
            ),
            rotation_quaternion_wxyz=quaternion,  # type: ignore[arg-type]
            translation_mm=translation,  # type: ignore[arg-type]
        ),
        target_type=CalibrationTargetType.ARUCO_GRID,
        calibration_dataset_id=str(request_value["attempt_id"]),
        sync_delta_ms=selected_sync_delta_ms,
        method=f"auto_compare:{candidate['pnp_method']}+{candidate['extrinsic_method']}",
        status=CalibrationStatus.NEEDS_VALIDATION,
        quality=CalibrationQuality(
            num_observations=int(candidate["observation_count"]),
            num_inliers=int(candidate["inlier_count"]),
            mean_reprojection_error_px=candidate.get("mean_reprojection_error_px"),
            residual_translation_mm=float(
                candidate["held_out_residuals"]["mean_translation_mm"]
            ),
            residual_rotation_deg=float(
                candidate["held_out_residuals"]["mean_rotation_deg"]
            ),
            notes="Deterministic leave-one-pose-out calibration attempt candidate.",
        ),
        metadata={
            "sensor_name": sensor["sensor_name"],
            "sensor_key": sensor["sensor_key"],
            "attempt_id": request_value["attempt_id"],
            "candidate_id": candidate["candidate_id"],
            "solver_policy": request_value["solver_policy"],
            "pnp_method": candidate["pnp_method"],
            "extrinsic_method": candidate["extrinsic_method"],
            "target_id": request_value["target_id"],
            "target_mounting": request_value["target_mounting"],
            "robot_pose_reference": request_value.get(
                "robot_pose_reference",
                {
                    "schema_version": ROBOT_POSE_REFERENCE_SCHEMA_VERSION,
                    "status": "unverified",
                    "reason": "calibration_attempt_predates_reference_provenance",
                },
            ),
            "companion_transform": candidate["companion_transform"],
            "held_out_residuals": candidate["held_out_residuals"],
            "outlier_count": candidate["outlier_count"],
            "outlier_ratio": candidate["outlier_ratio"],
            "intrinsic_profile_id": intrinsic_profile["profile_id"],
            "intrinsics_policy": request_value["intrinsics_policy"],
            "synchronization": {
                **synchronization,
                "sync_delta_ms": selected_sync_delta_ms,
                "timestamp_source": sensor_timestamp_policy["frame_timestamp_source"],
                "frame_timestamp_source": sensor_timestamp_policy[
                    "frame_timestamp_source"
                ],
                "robot_timestamp_source": sensor_timestamp_policy[
                    "robot_timestamp_source"
                ],
                "required_frame_timestamp_domain": sensor_timestamp_policy.get(
                    "required_frame_timestamp_domain"
                ),
                "timestamp_fallback_allowed": False,
                "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
                "warning_nearest_pose_delta_ms": (warning_nearest_pose_delta_ms),
                "warning_fallback_used": bool(
                    synchronization.get("warning_fallback_used")
                ),
                "historical_per_sensor_offsets_allowed": False,
                "auto_estimated_per_sensor_offset": (
                    synchronization.get("policy") == "auto_offset"
                ),
                "sensor_key": sensor["sensor_key"],
                "quality_report": (
                    f"processed/calibration/{request_value['attempt_id']}/"
                    f"{SYNC_QUALITY_REPORT}"
                ),
            },
        },
    )


def _joint_companion_frame(
    request_value: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return the shared estimated companion frame when joint ranking applies."""

    sensor_keys = request_value.get("sensor_keys")
    target_mounting = request_value.get("target_mounting")
    if (
        not isinstance(sensor_keys, Sequence)
        or isinstance(sensor_keys, (str, bytes))
        or len(sensor_keys) < 2
        or not isinstance(target_mounting, Mapping)
        or target_mounting.get("state") != "estimated"
    ):
        return None
    from_frame = str(target_mounting.get("from") or "").strip()
    to_frame = str(target_mounting.get("to") or "").strip()
    if not from_frame or not to_frame:
        return None
    return {"from": from_frame, "to": to_frame, "state": "estimated"}


def _algorithm_pair_sort_key(pair: tuple[str, str]) -> tuple[Any, ...]:
    pnp_order = {name: index for index, name in enumerate(PNP_METHOD_ORDER)}
    extrinsic_order = {name: index for index, name in enumerate(EXTRINSIC_METHOD_ORDER)}
    return (
        pnp_order.get(pair[0], len(pnp_order)),
        extrinsic_order.get(pair[1], len(extrinsic_order)),
        pair[0],
        pair[1],
    )


def _joint_algorithm_pairs(
    request_value: Mapping[str, Any],
    ranked_by_sensor: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[tuple[str, str]]:
    raw_pnp = request_value.get("pnp_methods")
    raw_extrinsic = request_value.get("extrinsic_methods")
    if (
        isinstance(raw_pnp, Sequence)
        and not isinstance(raw_pnp, (str, bytes))
        and raw_pnp
        and isinstance(raw_extrinsic, Sequence)
        and not isinstance(raw_extrinsic, (str, bytes))
        and raw_extrinsic
    ):
        pairs = {
            (str(pnp_method), str(extrinsic_method))
            for pnp_method in raw_pnp
            for extrinsic_method in raw_extrinsic
        }
    else:
        pairs = {
            (str(candidate.get("pnp_method")), str(candidate.get("extrinsic_method")))
            for candidates in ranked_by_sensor.values()
            for candidate in candidates
            if candidate.get("pnp_method") and candidate.get("extrinsic_method")
        }
    return sorted(pairs, key=_algorithm_pair_sort_key)


def _candidate_float(candidate: Mapping[str, Any], key: str) -> float | None:
    try:
        value = float(candidate[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _joint_bundle_record(
    *,
    sensor_keys: Sequence[str],
    pair: tuple[str, str],
    ranked_by_sensor: Mapping[str, Sequence[Mapping[str, Any]]],
    companion_frame: Mapping[str, str],
) -> dict[str, Any]:
    pnp_method, extrinsic_method = pair
    bundle_id = f"{pnp_method}|{extrinsic_method}"
    matches_by_sensor = {
        sensor_key: [
            candidate
            for candidate in ranked_by_sensor.get(sensor_key, [])
            if str(candidate.get("pnp_method")) == pnp_method
            and str(candidate.get("extrinsic_method")) == extrinsic_method
        ]
        for sensor_key in sensor_keys
    }
    candidate_options = {
        sensor_key: sorted(str(item.get("candidate_id")) for item in matches)
        for sensor_key, matches in matches_by_sensor.items()
    }
    selected = {
        sensor_key: matches[0]
        for sensor_key, matches in matches_by_sensor.items()
        if len(matches) == 1
    }
    candidate_ids = {
        sensor_key: str(candidate["candidate_id"])
        for sensor_key, candidate in selected.items()
    }
    passing_count = sum(
        candidate.get("status") == "passing" for candidate in selected.values()
    )
    scores = [_candidate_float(candidate, "score") for candidate in selected.values()]
    scores_valid = len(scores) == len(sensor_keys) and all(
        value is not None for value in scores
    )
    numeric_scores = [float(value) for value in scores if value is not None]
    aggregate_score = sum(numeric_scores) if scores_valid else None
    mean_score = (
        aggregate_score / len(sensor_keys) if aggregate_score is not None else None
    )
    reprojection_values = [
        value
        for candidate in selected.values()
        if (value := _candidate_float(candidate, "mean_reprojection_error_px"))
        is not None
    ]
    mean_reprojection_error_px = (
        sum(reprojection_values) / len(reprojection_values)
        if len(reprojection_values) == len(sensor_keys)
        else None
    )
    total_inlier_count = sum(
        int(candidate.get("inlier_count", 0)) for candidate in selected.values()
    )

    transforms: dict[str, np.ndarray] = {}
    transform_errors: dict[str, str] = {}
    for sensor_key, candidate in selected.items():
        raw_transform = candidate.get("companion_transform")
        if not isinstance(raw_transform, Mapping):
            transform_errors[sensor_key] = "companion transform is missing"
            continue
        actual_frame = {
            "from": str(raw_transform.get("from") or ""),
            "to": str(raw_transform.get("to") or ""),
        }
        expected_frame = {
            "from": companion_frame["from"],
            "to": companion_frame["to"],
        }
        if actual_frame != expected_frame:
            transform_errors[sensor_key] = (
                f"companion frame {actual_frame!r} does not match {expected_frame!r}"
            )
            continue
        try:
            transforms[sensor_key] = transform_from_record(raw_transform)
        except (TypeError, ValueError) as exc:
            transform_errors[sensor_key] = str(exc)

    pairwise_residuals: list[dict[str, Any]] = []
    for left_index, left_sensor_key in enumerate(sensor_keys):
        for right_sensor_key in sensor_keys[left_index + 1 :]:
            if left_sensor_key not in transforms or right_sensor_key not in transforms:
                continue
            residual = transform_residual(
                transforms[left_sensor_key], transforms[right_sensor_key]
            )
            pairwise_residuals.append(
                {
                    "left_sensor_key": left_sensor_key,
                    "right_sensor_key": right_sensor_key,
                    "left_candidate_id": candidate_ids[left_sensor_key],
                    "right_candidate_id": candidate_ids[right_sensor_key],
                    "translation_mm": residual["translation_mm"],
                    "rotation_deg": residual["rotation_deg"],
                    "status": (
                        "ok"
                        if residual["translation_mm"] <= DEFAULT_MAX_MEAN_TRANSLATION_MM
                        and residual["rotation_deg"] <= DEFAULT_MAX_MEAN_ROTATION_DEG
                        else "error"
                    ),
                }
            )
    expected_pair_count = len(sensor_keys) * (len(sensor_keys) - 1) // 2
    max_translation_mm = (
        max(item["translation_mm"] for item in pairwise_residuals)
        if pairwise_residuals
        else None
    )
    max_rotation_deg = (
        max(item["rotation_deg"] for item in pairwise_residuals)
        if pairwise_residuals
        else None
    )
    normalized_companion_closure_score = (
        max_translation_mm / DEFAULT_MAX_MEAN_TRANSLATION_MM
        + max_rotation_deg / DEFAULT_MAX_MEAN_ROTATION_DEG
        if max_translation_mm is not None and max_rotation_deg is not None
        else None
    )
    presence_ok = len(selected) == len(sensor_keys)
    passing_ok = passing_count == len(sensor_keys)
    transform_ok = (
        len(transforms) == len(sensor_keys)
        and len(pairwise_residuals) == expected_pair_count
    )
    translation_ok = (
        transform_ok
        and max_translation_mm is not None
        and max_translation_mm <= DEFAULT_MAX_MEAN_TRANSLATION_MM
    )
    rotation_ok = (
        transform_ok
        and max_rotation_deg is not None
        and max_rotation_deg <= DEFAULT_MAX_MEAN_ROTATION_DEG
    )
    checks = [
        {
            "name": "joint_candidate_presence",
            "status": "ok" if presence_ok else "error",
            "actual": len(selected),
            "threshold": len(sensor_keys),
        },
        {
            "name": "joint_individual_candidate_validation",
            "status": "ok" if passing_ok else "error",
            "actual": passing_count,
            "threshold": len(sensor_keys),
        },
        {
            "name": "joint_individual_score_validity",
            "status": "ok" if scores_valid else "error",
            "actual": len(numeric_scores),
            "threshold": len(sensor_keys),
        },
        {
            "name": "joint_companion_transform_validity",
            "status": "ok" if transform_ok else "error",
            "actual": len(transforms),
            "threshold": len(sensor_keys),
            "errors": transform_errors,
        },
        {
            "name": "joint_companion_translation_consistency",
            "status": "ok" if translation_ok else "error",
            "actual": max_translation_mm,
            "threshold": DEFAULT_MAX_MEAN_TRANSLATION_MM,
            "unit": "mm",
        },
        {
            "name": "joint_companion_rotation_consistency",
            "status": "ok" if rotation_ok else "error",
            "actual": max_rotation_deg,
            "threshold": DEFAULT_MAX_MEAN_ROTATION_DEG,
            "unit": "deg",
        },
    ]
    passing = (
        presence_ok
        and passing_ok
        and scores_valid
        and transform_ok
        and translation_ok
        and rotation_ok
    )
    return {
        "bundle_id": bundle_id,
        "pnp_method": pnp_method,
        "extrinsic_method": extrinsic_method,
        "algorithms": [pnp_method, extrinsic_method],
        "sensor_keys": list(sensor_keys),
        "candidate_ids": candidate_ids,
        "candidate_options": candidate_options,
        "status": "passing" if passing else "failed",
        "aggregate_score": aggregate_score,
        "mean_score": mean_score,
        "mean_reprojection_error_px": mean_reprojection_error_px,
        "total_inlier_count": total_inlier_count,
        "companion_frame": dict(companion_frame),
        "pairwise_companion_residuals": pairwise_residuals,
        "max_pairwise_companion_translation_mm": max_translation_mm,
        "max_pairwise_companion_rotation_deg": max_rotation_deg,
        "normalized_companion_closure_score": (normalized_companion_closure_score),
        "checks": checks,
    }


def _joint_consistency_ranking(
    request_value: Mapping[str, Any],
    ranked_by_sensor: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    companion_frame = _joint_companion_frame(request_value)
    if companion_frame is None:
        return None
    sensor_keys = [str(item) for item in request_value["sensor_keys"]]
    bundles = [
        _joint_bundle_record(
            sensor_keys=sensor_keys,
            pair=pair,
            ranked_by_sensor=ranked_by_sensor,
            companion_frame=companion_frame,
        )
        for pair in _joint_algorithm_pairs(request_value, ranked_by_sensor)
    ]
    passing_mean_scores = [
        float(bundle["mean_score"])
        for bundle in bundles
        if bundle.get("status") == "passing" and bundle.get("mean_score") is not None
    ]
    best_individual_score = min(passing_mean_scores, default=None)
    for bundle in bundles:
        mean_score = bundle.get("mean_score")
        bundle["individual_score_delta_from_best"] = (
            float(mean_score) - best_individual_score
            if mean_score is not None and best_individual_score is not None
            else None
        )

    def ranking_number(value: Any) -> float:
        return (
            round(float(value), JOINT_RANKING_NUMERIC_DECIMALS)
            if value is not None
            else math.inf
        )

    def bundle_sort_key(bundle: Mapping[str, Any]) -> tuple[Any, ...]:
        mean_score = bundle.get("mean_score")
        aggregate_score = bundle.get("aggregate_score")
        closure_score = bundle.get("normalized_companion_closure_score")
        reprojection = bundle.get("mean_reprojection_error_px")
        passing = bundle.get("status") == "passing"
        algorithm_key = _algorithm_pair_sort_key(
            (str(bundle["pnp_method"]), str(bundle["extrinsic_method"]))
        )
        return (
            0 if passing else 1,
            ranking_number(mean_score),
            ranking_number(closure_score),
            ranking_number(aggregate_score),
            ranking_number(reprojection),
            -int(bundle.get("total_inlier_count", 0)),
            *algorithm_key,
            str(bundle["bundle_id"]),
        )

    bundles = [dict(item) for item in sorted(bundles, key=bundle_sort_key)]
    for index, bundle in enumerate(bundles, start=1):
        bundle["rank"] = index
        bundle["recommended"] = index == 1 and bundle["status"] == "passing"
    recommendation = next((bundle for bundle in bundles if bundle["recommended"]), None)
    return {
        "required": True,
        "status": "passing" if recommendation else "failed",
        "sensor_keys": sensor_keys,
        "sensor_count": len(sensor_keys),
        "companion_frame": companion_frame,
        "thresholds": {
            "max_pairwise_companion_translation_mm": (DEFAULT_MAX_MEAN_TRANSLATION_MM),
            "max_pairwise_companion_rotation_deg": DEFAULT_MAX_MEAN_ROTATION_DEG,
        },
        "ranking_policy": {
            "best_individual_score": best_individual_score,
            "normalized_companion_closure_score_definition": (
                "max_pairwise_translation_mm/max_translation_mm + "
                "max_pairwise_rotation_deg/max_rotation_deg"
            ),
            "numeric_round_decimals": JOINT_RANKING_NUMERIC_DECIMALS,
            "ordering": [
                "status",
                "rounded_mean_score",
                "rounded_normalized_companion_closure_score",
                "rounded_aggregate_score",
                "rounded_mean_reprojection_error_px",
                "descending_total_inlier_count",
                "canonical_algorithm_order",
                "bundle_id",
            ],
        },
        "bundle_count": len(bundles),
        "passing_bundle_count": sum(
            bundle["status"] == "passing" for bundle in bundles
        ),
        "recommended_bundle_id": (
            recommendation["bundle_id"] if recommendation else None
        ),
        "recommendation": recommendation,
        "bundles": bundles,
    }


def _validate_and_rank(
    attempt_root: Path,
    request_value: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    intrinsics: Mapping[str, Mapping[str, Any]],
    *,
    time_offset_search: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    coverage_thresholds = _image_coverage_thresholds(str(request_value["mode"]))
    max_nearest_pose_delta_ms, warning_nearest_pose_delta_ms = (
        _recorded_nearest_pose_limits_ms(request_value)
    )
    raw_timestamp_policy = request_value.get("timestamp_policy")
    timestamp_policy = (
        dict(raw_timestamp_policy)
        if isinstance(raw_timestamp_policy, Mapping)
        else _attempt_timestamp_policy(request_value["sensors"])
    )
    if time_offset_search is None:
        time_offset_search = {
            "policy": "fixed_zero",
            "sensors": [
                fixed_zero_sensor_result(
                    sensor_key=str(sensor_key),
                    observation_count=0,
                )
                for sensor_key in request_value["sensor_keys"]
            ],
        }
    sensor_metadata = {
        str(item["sensor_key"]): item for item in request_value["sensors"]
    }
    alignment_by_sensor = {
        str(item["sensor_key"]): item
        for item in time_offset_search["sensors"]
        if isinstance(item, Mapping)
    }
    profiles: list[CalibrationProfile] = []
    results = []
    all_checks = []
    ranked_by_sensor: dict[str, list[dict[str, Any]]] = {}
    for sensor_key in request_value["sensor_keys"]:
        ranked = rank_candidates(
            [item for item in candidates if item["sensor_key"] == sensor_key]
        )
        ranked_by_sensor[str(sensor_key)] = ranked
        for item in ranked:
            if "primary_transform" in item:
                profile = _candidate_profile(
                    item,
                    request_value=request_value,
                    sensor=sensor_metadata[sensor_key],
                    intrinsic_profile=intrinsics[sensor_key],
                )
                profiles.append(profile)
                item["profile_id"] = profile.profile_id
            all_checks.extend(
                {**dict(check), "candidate_id": item["candidate_id"]}
                for check in item.get("checks", [])
            )

    joint_consistency = _joint_consistency_ranking(request_value, ranked_by_sensor)
    if joint_consistency is not None:
        for ranked in ranked_by_sensor.values():
            for item in ranked:
                item["recommended"] = False
                item.pop("joint_bundle_id", None)
                item.pop("recommendation_basis", None)
        joint_recommendation = joint_consistency.get("recommendation")
        if isinstance(joint_recommendation, Mapping):
            selected_candidate_ids = set(
                joint_recommendation.get("candidate_ids", {}).values()
            )
            for ranked in ranked_by_sensor.values():
                for item in ranked:
                    if item["candidate_id"] in selected_candidate_ids:
                        item["recommended"] = True
                        item["joint_bundle_id"] = joint_recommendation["bundle_id"]
                        item["recommendation_basis"] = (
                            "multi_camera_companion_consistency"
                        )
        for bundle in joint_consistency["bundles"]:
            all_checks.extend(
                {
                    **dict(check),
                    "scope": "multi_camera_bundle",
                    "bundle_id": bundle["bundle_id"],
                }
                for check in bundle.get("checks", [])
            )

    for sensor_key in request_value["sensor_keys"]:
        ranked = ranked_by_sensor[str(sensor_key)]
        recommendation = next(
            (item for item in ranked if item.get("recommended")), None
        )
        results.append(
            {
                **sensor_metadata[sensor_key],
                "status": "passing" if recommendation else "failed",
                "recommended_candidate_id": (
                    recommendation["candidate_id"] if recommendation else None
                ),
                "recommended_profile_id": (
                    recommendation.get("profile_id") if recommendation else None
                ),
                "recommendation": recommendation,
                "time_offset_search": alignment_by_sensor[sensor_key],
                "candidates": ranked,
            }
        )
    write_profile_collection(profiles, attempt_root / CANDIDATE_PROFILES_FILE)
    ranking = {
        "schema_version": "calibration_ranking.v1",
        "generated_at": utc_now_iso(),
        "attempt_id": request_value["attempt_id"],
        "mode": request_value["mode"],
        "status": (
            "complete"
            if all(item["status"] == "passing" for item in results)
            else "partial"
            if any(item["status"] == "passing" for item in results)
            else "failed"
        ),
        "recommended_camera_count": sum(
            1 for item in results if item["status"] == "passing"
        ),
        "failed_camera_count": sum(1 for item in results if item["status"] == "failed"),
        "thresholds": {
            "min_inliers": 6,
            "min_accepted_views": DEFAULT_MIN_ACCEPTED_VIEWS,
            "min_pnp_clutter_supported_markers": (
                DEFAULT_MIN_PNP_CLUTTER_SUPPORTED_MARKERS
            ),
            "min_pnp_clutter_grid_rows": DEFAULT_MIN_PNP_CLUTTER_GRID_ROWS,
            "min_pnp_clutter_grid_columns": DEFAULT_MIN_PNP_CLUTTER_GRID_COLUMNS,
            "min_coverage_cells": DEFAULT_MIN_COVERAGE_CELLS,
            "image_coverage_tail_support_views": (
                int(coverage_thresholds["image_coverage_tail_support_views"])
            ),
            "min_image_centroid_x_span_ratio": (
                float(coverage_thresholds["min_image_centroid_x_span_ratio"])
            ),
            "min_image_centroid_y_span_ratio": (
                float(coverage_thresholds["min_image_centroid_y_span_ratio"])
            ),
            "min_image_centroid_hull_area_ratio": (
                float(coverage_thresholds["min_image_centroid_hull_area_ratio"])
            ),
            "max_per_view_reprojection_error_px": (DEFAULT_MAX_VIEW_ERROR_PX),
            "max_intrinsic_rms_reprojection_error_px": DEFAULT_MAX_RMS_PX,
            "min_motion_poses": ATTEMPT_MIN_MOTION_POSES,
            "min_translation_span_mm": ATTEMPT_MIN_TRANSLATION_SPAN_MM,
            "min_rotation_span_deg": ATTEMPT_MIN_ROTATION_SPAN_DEG,
            "min_rotation_axis_angle_deg": (DEFAULT_MIN_ROTATION_AXIS_ANGLE_DEG),
            "min_rotation_axis_second_to_first_ratio": (
                DEFAULT_MIN_ROTATION_AXIS_SINGULAR_RATIO
            ),
            "max_observations_per_motion": (DEFAULT_MAX_OBSERVATIONS_PER_MOTION),
            "max_nearest_pose_delta_ms": max_nearest_pose_delta_ms,
            "warning_nearest_pose_delta_ms": warning_nearest_pose_delta_ms,
            "timestamp_source": timestamp_policy["frame_timestamp_source"],
            "robot_timestamp_source": timestamp_policy["robot_timestamp_source"],
            "synchronization_policy": time_offset_search["policy"],
            "sync_delta_ms": (
                0.0
                if all(
                    math.isclose(
                        float(item["selected_sync_delta_ms"]),
                        0.0,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    for item in time_offset_search["sensors"]
                )
                else "per_sensor"
            ),
            "per_sensor_sync_delta_ms": {
                str(item["sensor_key"]): float(item["selected_sync_delta_ms"])
                for item in time_offset_search["sensors"]
            },
            "max_mean_translation_mm": 10.0,
            "max_mean_rotation_deg": 5.0,
            "max_outlier_ratio": 0.25,
        },
        "results": results,
    }
    if joint_consistency is not None:
        ranking["multi_camera_consistency"] = joint_consistency
    atomic_write_json(attempt_root / RANKING_FILE, ranking)
    atomic_write_json(
        attempt_root / CHECKS_FILE,
        {
            "schema_version": "calibration_attempt_checks.v1",
            "attempt_id": request_value["attempt_id"],
            "checks": all_checks,
        },
    )
    return ranking


def run_calibration_attempt(run_root: str | Path, attempt_id: str) -> dict[str, Any]:
    # Downstream synchronization helpers accept both run-relative sensor names
    # and absolute sensor paths.  Resolve once here so a relative CLI run root
    # cannot be joined to itself when an absolute sensor path is required.
    root = Path(run_root).resolve()
    attempt_root = calibration_attempt_root(root, attempt_id)
    request_value = _read_json(attempt_root / REQUEST_FILE)
    initial_progress = _read_json(attempt_root / PROGRESS_FILE)
    _validate_attempt_identity(root, attempt_id, request_value, initial_progress)
    _require_current_attempt_request(request_value)
    if initial_progress.get("status") != "queued":
        raise ValueError(
            "Calibration attempts are immutable and may only be calculated once"
        )
    try:
        _update_progress(
            attempt_root,
            status="running",
            phase="prepare_data",
            phase_status="running",
            message="Synchronizing the selected camera subset.",
        )
        robot_pose_artifacts = _verify_robot_pose_artifact_bindings(root, request_value)
        synchronized, intrinsics = _prepare_attempt_data(
            root,
            attempt_root,
            request_value,
            robot_pose_artifacts,
        )
        _update_progress(
            attempt_root,
            phase="prepare_data",
            phase_status="complete",
            message="Selected camera data and compatible intrinsics are ready.",
        )
        _update_progress(
            attempt_root,
            phase="estimate_target_poses",
            phase_status="running",
            message="Comparing planar target-pose estimates.",
        )
        _pnp, observations = _estimate_target_poses(
            attempt_root,
            request_value,
            synchronized,
            intrinsics,
        )
        _update_progress(
            attempt_root,
            phase="estimate_target_poses",
            phase_status="complete",
            message="Target poses were estimated with the shared robust mask.",
        )
        _update_progress(
            attempt_root,
            phase="estimate_time_offsets",
            phase_status="running",
            message=(
                "Estimating effective camera-to-robot latency on fixed "
                "motion-disjoint evidence."
            ),
        )
        time_offset_search, adjusted_observations = _estimate_and_apply_time_offsets(
            root,
            attempt_root,
            request_value,
            observations,
            robot_pose_artifacts,
        )
        _authoritative_synchronized, observations = (
            _materialize_authoritative_synchronization(
                root,
                attempt_root,
                request_value,
                time_offset_search,
                adjusted_observations,
                robot_pose_artifacts,
            )
        )
        observation_report = _calibration_observation_report(
            request_value, observations
        )
        observation_report["time_offset_search"] = _attempt_artifact_reference(
            str(request_value["attempt_id"]),
            TIME_OFFSET_SEARCH,
        )
        observation_report["synchronization_policy"] = time_offset_search["policy"]
        atomic_write_json(attempt_root / CALIBRATION_OBSERVATIONS, observation_report)
        timing_warning_count = int(time_offset_search.get("warning_sensor_count", 0))
        _update_progress(
            attempt_root,
            phase="estimate_time_offsets",
            phase_status="complete",
            message=(
                "Authoritative camera/robot time alignment is ready with "
                f"advisory evidence for {timing_warning_count} camera(s)."
                if timing_warning_count
                else "Authoritative camera/robot time alignment is ready."
            ),
        )
        _update_progress(
            attempt_root,
            phase="compare_robot_camera_solutions",
            phase_status="running",
            message="Evaluating every compatible PnP/extrinsic combination.",
        )
        candidates = _compare_solutions(
            attempt_root,
            request_value,
            observations,
            time_offset_search=time_offset_search,
        )
        _update_progress(
            attempt_root,
            phase="compare_robot_camera_solutions",
            phase_status="complete",
            message="Robot-camera solver comparison is complete.",
        )
        _update_progress(
            attempt_root,
            phase="validate_and_rank",
            phase_status="running",
            message="Applying validation gates and deterministic ranking.",
        )
        ranking = _validate_and_rank(
            attempt_root,
            request_value,
            candidates,
            intrinsics,
            time_offset_search=time_offset_search,
        )
        _update_progress(
            attempt_root,
            status="complete",
            phase="validate_and_rank",
            phase_status="complete",
            message=(
                "Calibration calculations are complete with timing warnings and "
                "are awaiting review."
                if timing_warning_count
                else "Calibration calculations are complete and awaiting review."
            ),
        )
        return ranking
    except Exception as exc:
        progress = _read_json(attempt_root / PROGRESS_FILE)
        current = progress.get("current_phase")
        _update_progress(
            attempt_root,
            status="failed",
            phase=str(current) if current else None,
            phase_status="failed" if current else None,
            message=f"{type(exc).__name__}: {exc}",
        )
        raise


def load_calibration_attempt(run_root: str | Path, attempt_id: str) -> dict[str, Any]:
    root = Path(run_root)
    attempt_root = calibration_attempt_root(root, attempt_id)
    if not attempt_root.is_dir():
        raise FileNotFoundError(f"Calibration attempt not found: {attempt_id}")
    request_value = _read_json(attempt_root / REQUEST_FILE)
    progress = _read_json(attempt_root / PROGRESS_FILE)
    _validate_attempt_identity(root, attempt_id, request_value, progress)
    ranking = (
        _read_json(attempt_root / RANKING_FILE)
        if (attempt_root / RANKING_FILE).is_file()
        else None
    )
    promotion = (
        _read_json(attempt_root / PROMOTION_FILE)
        if (attempt_root / PROMOTION_FILE).is_file()
        else None
    )
    intrinsic_comparison = (
        _read_json(attempt_root / INTRINSIC_COMPARISON)
        if (attempt_root / INTRINSIC_COMPARISON).is_file()
        else None
    )
    time_offset_search = (
        _read_json(attempt_root / TIME_OFFSET_SEARCH)
        if (attempt_root / TIME_OFFSET_SEARCH).is_file()
        else None
    )
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "run_root": root.as_posix(),
        "request": request_value,
        "read_only": request_value.get("schema_version") != REQUEST_SCHEMA_VERSION,
        "progress": progress,
        "results": ranking,
        "intrinsic_comparison": intrinsic_comparison,
        "time_offset_search": time_offset_search,
        "promotion": promotion,
        "artifacts": {
            name: _relative(attempt_root / name, root)
            for name in (
                REQUEST_FILE,
                PROGRESS_FILE,
                SYNC_QUALITY_REPORT,
                TIME_OFFSET_SEARCH,
                INTRINSIC_COMPARISON,
                INTRINSIC_CALIBRATION_PROFILES,
                PNP_CANDIDATES_FILE,
                CALIBRATION_OBSERVATIONS,
                EXTRINSIC_CANDIDATES_FILE,
                RANKING_FILE,
                CHECKS_FILE,
                CANDIDATE_PROFILES_FILE,
                PROMOTION_FILE,
            )
            if (attempt_root / name).exists()
        },
    }


def _optional_floats_match(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(left_value)
        and math.isfinite(right_value)
        and math.isclose(left_value, right_value, rel_tol=0.0, abs_tol=1e-9)
    )


def _revalidate_joint_promotion(
    attempt: Mapping[str, Any],
    selections: Mapping[str, str],
    *,
    expected_bundle_id: str | None = None,
) -> dict[str, Any] | None:
    request_value = attempt.get("request")
    if not isinstance(request_value, Mapping):
        raise ValueError("Calibration attempt request evidence is missing")
    companion_frame = _joint_companion_frame(request_value)
    if companion_frame is None:
        if expected_bundle_id is not None:
            raise ValueError(
                "Single-camera promotion unexpectedly names a multi-camera bundle"
            )
        return None

    sensor_keys = [str(item) for item in request_value["sensor_keys"]]
    if set(selections) != set(sensor_keys):
        raise ValueError(
            "Multi-camera promotion must select every jointly ranked sensor"
        )
    ranking = attempt.get("results")
    if not isinstance(ranking, Mapping):
        raise ValueError("Calibration ranking evidence is missing")
    consistency = ranking.get("multi_camera_consistency")
    if not isinstance(consistency, Mapping) or consistency.get("required") is not True:
        raise ValueError("Multi-camera consistency evidence is missing")
    if consistency.get("companion_frame") != companion_frame:
        raise ValueError("Multi-camera companion-frame evidence is inconsistent")
    if [str(item) for item in consistency.get("sensor_keys", [])] != sensor_keys:
        raise ValueError("Multi-camera sensor-order evidence is inconsistent")
    thresholds = consistency.get("thresholds")
    if (
        not isinstance(thresholds, Mapping)
        or not _optional_floats_match(
            thresholds.get("max_pairwise_companion_translation_mm"),
            DEFAULT_MAX_MEAN_TRANSLATION_MM,
        )
        or not _optional_floats_match(
            thresholds.get("max_pairwise_companion_rotation_deg"),
            DEFAULT_MAX_MEAN_ROTATION_DEG,
        )
    ):
        raise ValueError("Multi-camera consistency thresholds are invalid")

    results = {
        str(item.get("sensor_key")): item
        for item in ranking.get("results", [])
        if isinstance(item, Mapping) and item.get("sensor_key") is not None
    }
    selected_candidates: dict[str, Mapping[str, Any]] = {}
    for sensor_key in sensor_keys:
        result = results.get(sensor_key)
        if not isinstance(result, Mapping):
            raise ValueError(f"Multi-camera ranking result is missing for {sensor_key}")
        matches = [
            item
            for item in result.get("candidates", [])
            if isinstance(item, Mapping)
            and str(item.get("candidate_id")) == selections[sensor_key]
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Multi-camera candidate evidence is ambiguous for {sensor_key}"
            )
        selected_candidates[sensor_key] = matches[0]

    algorithm_pairs = {
        (
            str(candidate.get("pnp_method")),
            str(candidate.get("extrinsic_method")),
        )
        for candidate in selected_candidates.values()
    }
    if len(algorithm_pairs) != 1:
        raise ValueError(
            "Multi-camera promotion selections must use one common algorithm bundle"
        )
    pair = next(iter(algorithm_pairs))
    recalculated = _joint_bundle_record(
        sensor_keys=sensor_keys,
        pair=pair,
        ranked_by_sensor={
            sensor_key: [candidate]
            for sensor_key, candidate in selected_candidates.items()
        },
        companion_frame=companion_frame,
    )
    if recalculated["status"] != "passing":
        raise ValueError(
            "Selected multi-camera bundle no longer satisfies consistency gates"
        )

    recorded_matches = [
        bundle
        for bundle in consistency.get("bundles", [])
        if isinstance(bundle, Mapping)
        and bundle.get("candidate_ids") == dict(selections)
    ]
    if len(recorded_matches) != 1:
        raise ValueError("Selections do not match one recorded multi-camera bundle")
    recorded = recorded_matches[0]
    if (
        recorded.get("status") != "passing"
        or recorded.get("bundle_id") != recalculated["bundle_id"]
        or recorded.get("pnp_method") != pair[0]
        or recorded.get("extrinsic_method") != pair[1]
    ):
        raise ValueError("Recorded multi-camera bundle is not promotable")
    if expected_bundle_id is not None and recorded.get("bundle_id") != str(
        expected_bundle_id
    ):
        raise ValueError("Promotion request names a different multi-camera bundle")

    numeric_fields = (
        "aggregate_score",
        "mean_score",
        "max_pairwise_companion_translation_mm",
        "max_pairwise_companion_rotation_deg",
        "normalized_companion_closure_score",
    )
    if any(
        not _optional_floats_match(recorded.get(field), recalculated.get(field))
        for field in numeric_fields
    ):
        raise ValueError("Recorded multi-camera bundle summary is inconsistent")
    recorded_residuals = {
        (
            str(item.get("left_sensor_key")),
            str(item.get("right_sensor_key")),
            str(item.get("left_candidate_id")),
            str(item.get("right_candidate_id")),
        ): item
        for item in recorded.get("pairwise_companion_residuals", [])
        if isinstance(item, Mapping)
    }
    recalculated_residuals = {
        (
            str(item.get("left_sensor_key")),
            str(item.get("right_sensor_key")),
            str(item.get("left_candidate_id")),
            str(item.get("right_candidate_id")),
        ): item
        for item in recalculated["pairwise_companion_residuals"]
    }
    if recorded_residuals.keys() != recalculated_residuals.keys():
        raise ValueError("Recorded multi-camera pairwise evidence is incomplete")
    for key, recalculated_residual in recalculated_residuals.items():
        recorded_residual = recorded_residuals[key]
        if (
            recorded_residual.get("status") != recalculated_residual["status"]
            or not _optional_floats_match(
                recorded_residual.get("translation_mm"),
                recalculated_residual["translation_mm"],
            )
            or not _optional_floats_match(
                recorded_residual.get("rotation_deg"),
                recalculated_residual["rotation_deg"],
            )
        ):
            raise ValueError("Recorded multi-camera pairwise evidence is inconsistent")
    return dict(recorded)


def _promotion_selections(
    attempt: Mapping[str, Any],
    overrides: Mapping[str, Any] | None,
) -> dict[str, str]:
    ranking = attempt.get("results")
    if not isinstance(ranking, Mapping):
        raise ValueError("Calibration calculations are not complete")
    explicit = overrides is not None
    supplied = {str(key): str(value) for key, value in (overrides or {}).items()}
    results = {
        str(item["sensor_key"]): item
        for item in ranking.get("results", [])
        if isinstance(item, Mapping)
    }
    unknown = sorted(set(supplied) - results.keys())
    if unknown:
        raise ValueError("Unknown promotion sensor key(s): " + ", ".join(unknown))
    selected = {}
    for sensor_key, result in results.items():
        candidate_id = (
            supplied.get(sensor_key)
            if explicit
            else result.get("recommended_candidate_id")
        )
        if not candidate_id:
            continue
        candidates = {
            str(item["candidate_id"]): item
            for item in result.get("candidates", [])
            if isinstance(item, Mapping)
        }
        candidate = candidates.get(str(candidate_id))
        if candidate is None:
            raise ValueError(
                f"Candidate {candidate_id!r} does not belong to {sensor_key}"
            )
        if candidate.get("status") != "passing":
            raise ValueError(f"Candidate {candidate_id!r} did not pass validation")
        selected[sensor_key] = str(candidate_id)
    if not selected:
        raise ValueError("No passing camera recommendations are available to promote")
    _revalidate_joint_promotion(attempt, selected)
    return selected


def _validate_promotion_motion_consistency(
    item: Mapping[str, Any],
    *,
    sensor_key: str,
    candidate_offset: float,
    recorded_search: Mapping[str, Any],
    search_grid: Sequence[float],
    check_by_name: Mapping[str, Mapping[str, Any]],
    require_passing: bool = True,
) -> None:
    """Recalculate motion-consistency evidence before promotion."""

    status = str(item.get("status") or "")
    fold_check = check_by_name["cross_validation_fold_materiality"]
    consistency_check = check_by_name["leave_one_motion_out_timing_consistency"]
    evidence = item.get("motion_consistency")

    def effective_check_status(check: Mapping[str, Any]) -> str:
        if (
            check.get("status") == "warning"
            and check.get("original_status") == "error"
        ):
            return "error"
        return str(check.get("status") or "")

    candidate_is_zero = math.isclose(
        candidate_offset,
        0.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    if candidate_is_zero:
        if (
            status != "kept_zero"
            or evidence is not None
            or effective_check_status(fold_check) != "not_needed"
            or effective_check_status(consistency_check) != "not_needed"
        ):
            raise ValueError(
                f"Zero-offset motion-consistency evidence is invalid for {sensor_key}"
            )
        return
    expected_status = "applied" if require_passing else "kept_zero"
    evidence_status = (
        str(evidence.get("status") or "")
        if isinstance(evidence, Mapping)
        else ""
    )
    consistency_status = effective_check_status(consistency_check)
    fold_status = effective_check_status(fold_check)
    if (
        status != expected_status
        or not isinstance(evidence, Mapping)
        or evidence.get("strategy") != LOMO_CONSISTENCY_STRATEGY
        or evidence_status not in {"ok", "error"}
        or evidence.get("candidate_search_adjustment") != "bonferroni"
        or evidence.get("candidate_selection_uses_audited_motions") is not True
        or evidence.get("transform_training_motion_disjoint") is not True
        or consistency_status != evidence_status
        or fold_status not in {"ok", "warning", "error"}
        or (require_passing and evidence_status != "ok")
        or (require_passing and fold_status not in {"ok", "warning"})
    ):
        raise ValueError(
            f"Leave-one-motion-out timing evidence is invalid for {sensor_key}"
        )

    try:
        evidence_candidate = float(evidence["candidate_robot_pose_time_offset_ms"])
        motion_count = int(evidence["motion_count"])
        candidate_hypothesis_count = int(evidence["candidate_search_hypothesis_count"])
        minimum_motion_count = (
            int(recorded_search["minimum_motion_count_per_cross_validation_fold"]) * 3
        )
        maximum_motion_count = int(recorded_search["maximum_search_motion_count"])
        minimum_absolute = float(
            recorded_search["minimum_absolute_cross_validated_improvement_mm"]
        )
        minimum_relative = float(
            recorded_search["minimum_relative_cross_validated_improvement"]
        )
        maximum_adjusted_p = float(
            recorded_search["maximum_leave_one_motion_out_search_adjusted_sign_p_value"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Leave-one-motion-out timing thresholds are invalid for {sensor_key}"
        ) from exc
    expected_hypothesis_count = sum(
        not math.isclose(float(value), 0.0, rel_tol=0.0, abs_tol=1e-9)
        for value in search_grid
    )
    if (
        not math.isclose(
            evidence_candidate,
            candidate_offset,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        or not minimum_motion_count <= motion_count <= maximum_motion_count
        or candidate_hypothesis_count != expected_hypothesis_count
        or not all(
            math.isfinite(value)
            for value in (minimum_absolute, minimum_relative, maximum_adjusted_p)
        )
        or minimum_absolute < 0.0
        or minimum_relative < 0.0
        or not 0.0 < maximum_adjusted_p <= 1.0
    ):
        raise ValueError(
            f"Leave-one-motion-out timing bounds are invalid for {sensor_key}"
        )

    thresholds = evidence.get("thresholds")
    expected_thresholds = {
        "minimum_median_absolute_translation_mm": minimum_absolute,
        "minimum_median_relative_translation": minimum_relative,
        "maximum_search_adjusted_positive_sign_p_value": maximum_adjusted_p,
    }
    if not isinstance(thresholds, Mapping) or dict(thresholds) != expected_thresholds:
        raise ValueError(
            f"Leave-one-motion-out timing thresholds are inconsistent for {sensor_key}"
        )

    expected_methods = tuple(
        str(method) for method in recorded_search["reference_extrinsic_methods"]
    )
    summaries = evidence.get("methods")
    motions = evidence.get("motions")
    if (
        not expected_methods
        or not isinstance(summaries, Mapping)
        or set(summaries) != set(expected_methods)
        or not isinstance(motions, list)
        or len(motions) != motion_count
    ):
        raise ValueError(
            f"Leave-one-motion-out timing coverage is invalid for {sensor_key}"
        )

    improvements: dict[str, list[dict[str, float | None]]] = {
        method: [] for method in expected_methods
    }
    motion_names: set[str] = set()
    for motion in motions:
        if not isinstance(motion, Mapping):
            raise ValueError(
                f"Leave-one-motion-out timing motion is invalid for {sensor_key}"
            )
        motion_name = str(motion.get("motion") or "")
        method_evidence = motion.get("methods")
        try:
            validation_observation_count = int(motion["validation_observation_count"])
            training_motion_count = int(motion["training_motion_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Leave-one-motion-out timing motion is invalid for {sensor_key}"
            ) from exc
        if (
            not motion_name
            or motion_name in motion_names
            or validation_observation_count <= 0
            or training_motion_count != motion_count - 1
            or not isinstance(method_evidence, Mapping)
            or set(method_evidence) != set(expected_methods)
        ):
            raise ValueError(
                f"Leave-one-motion-out timing motion is invalid for {sensor_key}"
            )
        motion_names.add(motion_name)
        for method in expected_methods:
            raw_method_evidence = method_evidence[method]
            if not isinstance(raw_method_evidence, Mapping):
                raise ValueError(
                    f"Leave-one-motion-out timing improvement is invalid for {sensor_key}"
                )
            raw_improvement = raw_method_evidence.get("improvement")
            zero_residuals = raw_method_evidence.get("zero_offset_residuals")
            candidate_residuals = raw_method_evidence.get("candidate_residuals")
            if (
                not isinstance(raw_improvement, Mapping)
                or not isinstance(zero_residuals, Mapping)
                or not isinstance(candidate_residuals, Mapping)
            ):
                raise ValueError(
                    f"Leave-one-motion-out timing improvement is invalid for {sensor_key}"
                )
            try:
                zero_translation = float(zero_residuals["mean_translation_mm"])
                zero_rotation = float(zero_residuals["mean_rotation_deg"])
                candidate_translation = float(
                    candidate_residuals["mean_translation_mm"]
                )
                candidate_rotation = float(candidate_residuals["mean_rotation_deg"])
                raw_relative_translation = raw_improvement[
                    "relative_translation"
                ]
                improvement: dict[str, float | None] = {
                    "absolute_translation_mm": float(
                        raw_improvement["absolute_translation_mm"]
                    ),
                    "relative_translation": (
                        float(raw_relative_translation)
                        if raw_relative_translation is not None
                        else None
                    ),
                    "rotation_change_deg": float(
                        raw_improvement["rotation_change_deg"]
                    ),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Leave-one-motion-out timing improvement is invalid for {sensor_key}"
                ) from exc
            source_values = (
                zero_translation,
                zero_rotation,
                candidate_translation,
                candidate_rotation,
                *(
                    value
                    for value in improvement.values()
                    if value is not None
                ),
            )
            expected_absolute = zero_translation - candidate_translation
            expected_relative = (
                expected_absolute / zero_translation if zero_translation > 0.0 else None
            )
            expected_rotation = candidate_rotation - zero_rotation
            if (
                not all(math.isfinite(value) for value in source_values)
                or not math.isclose(
                    improvement["absolute_translation_mm"],
                    expected_absolute,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or not _optional_floats_match(
                    improvement["relative_translation"], expected_relative
                )
                or not math.isclose(
                    improvement["rotation_change_deg"],
                    expected_rotation,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError(
                    f"Leave-one-motion-out timing improvement is invalid for {sensor_key}"
                )
            improvements[method].append(improvement)

    for method, values in improvements.items():
        positive_count = sum(
            float(value["absolute_translation_mm"]) > 1e-12 for value in values
        )
        material_count = sum(
            float(value["absolute_translation_mm"]) >= minimum_absolute
            and value["relative_translation"] is not None
            and float(value["relative_translation"]) >= minimum_relative
            for value in values
        )
        raw_p = float(
            sum(
                math.comb(motion_count, count)
                for count in range(positive_count, motion_count + 1)
            )
            / (2**motion_count)
        )
        adjusted_p = min(1.0, raw_p * candidate_hypothesis_count)
        medians = {
            "absolute_translation_mm": float(
                np.median(
                    [float(value["absolute_translation_mm"]) for value in values]
                )
            ),
            "relative_translation": (
                float(
                    np.median(
                        [float(value["relative_translation"]) for value in values]
                    )
                )
                if all(value["relative_translation"] is not None for value in values)
                else None
            ),
            "rotation_change_deg": float(
                np.median(
                    [float(value["rotation_change_deg"]) for value in values]
                )
            ),
        }
        method_ok = bool(
            medians["absolute_translation_mm"] >= minimum_absolute
            and medians["relative_translation"] is not None
            and float(medians["relative_translation"]) >= minimum_relative
            and adjusted_p <= maximum_adjusted_p
        )
        expected_method_status = "ok" if method_ok else "error"
        summary = summaries[method]
        if not isinstance(summary, Mapping):
            raise ValueError(
                f"Leave-one-motion-out timing summary is invalid for {sensor_key}"
            )
        recorded_medians = summary.get("median_improvement")
        try:
            summary_values_match = (
                summary.get("status") == expected_method_status
                and int(summary["motion_count"]) == motion_count
                and int(summary["positive_motion_count"]) == positive_count
                and int(summary["material_motion_count"]) == material_count
                and math.isclose(
                    float(summary["positive_sign_p_value"]),
                    raw_p,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                and math.isclose(
                    float(summary["candidate_search_adjusted_positive_sign_p_value"]),
                    adjusted_p,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                and isinstance(recorded_medians, Mapping)
                and math.isclose(
                    float(recorded_medians["absolute_translation_mm"]),
                    float(medians["absolute_translation_mm"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                and _optional_floats_match(
                    recorded_medians["relative_translation"],
                    medians["relative_translation"],
                )
                and math.isclose(
                    float(recorded_medians["rotation_change_deg"]),
                    float(medians["rotation_change_deg"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            )
        except (KeyError, TypeError, ValueError):
            summary_values_match = False
        if not summary_values_match or (require_passing and not method_ok):
            raise ValueError(
                f"Leave-one-motion-out timing summary is inconsistent for {sensor_key}"
            )

    expected_evidence_status = (
        "ok"
        if all(
            isinstance(summary, Mapping) and summary.get("status") == "ok"
            for summary in summaries.values()
        )
        else "error"
    )
    if evidence_status != expected_evidence_status:
        raise ValueError(
            f"Leave-one-motion-out timing status is inconsistent for {sensor_key}"
        )

    expected_check_actual = {
        "motion_count": motion_count,
        "methods": dict(summaries),
    }
    if consistency_check.get("actual") != expected_check_actual:
        raise ValueError(
            f"Leave-one-motion-out timing check is inconsistent for {sensor_key}"
        )


def _promotion_time_offset_evidence(
    attempt: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    request_value = attempt.get("request")
    if not isinstance(request_value, Mapping):
        raise ValueError("Calibration attempt request evidence is missing")
    has_explicit_policy = "synchronization_policy" in request_value
    policy = str(
        request_value.get("synchronization_policy", DEFAULT_SYNCHRONIZATION_POLICY)
    )
    report = attempt.get("time_offset_search")
    if report is None:
        if not has_explicit_policy:
            return {}
        raise ValueError("Calibration time-offset promotion evidence is missing")
    attempt_id = str(request_value.get("attempt_id") or attempt.get("attempt_id") or "")
    expected = {str(item) for item in request_value.get("sensor_keys", [])}
    raw_sensors = report.get("sensors", []) if isinstance(report, Mapping) else []
    recorded_search = request_value.get("synchronization_search")
    recorded_revision = request_value.get("synchronization_implementation_revision")
    if (
        not isinstance(report, Mapping)
        or not isinstance(recorded_search, Mapping)
        or not isinstance(recorded_revision, str)
        or recorded_revision not in TIME_OFFSET_SUPPORTED_REVISIONS
        or report.get("schema_version") != TIME_OFFSET_SEARCH_SCHEMA_VERSION
        or report.get("policy") != policy
        or report.get("status") != "complete"
        or report.get("attempt_id") != attempt_id
        or report.get("implementation_revision") != recorded_revision
        or report.get("offset_kind") != "effective_capture_and_pose_pipeline_latency"
        or report.get("sign_convention") != time_offset_sign_convention()
        or report.get("search") != dict(recorded_search)
        or report.get("failed_sensor_keys") != []
        or report.get("sensor_count") != len(expected)
        or not isinstance(raw_sensors, list)
        or len(raw_sensors) != len(expected)
    ):
        raise ValueError("Calibration time-offset promotion evidence is invalid")
    sensors = {
        str(item.get("sensor_key")): item
        for item in raw_sensors
        if isinstance(item, Mapping) and item.get("sensor_key")
    }
    if set(sensors) != expected:
        raise ValueError(
            "Calibration time-offset promotion evidence does not cover every sensor"
        )
    if recorded_revision == TIME_OFFSET_IMPLEMENTATION_REVISION:
        if (
            recorded_search.get("time_offset_failure_policy")
            != FAILURE_POLICY_WARN_KEEP_ZERO
        ):
            raise ValueError("Calibration time-offset fallback policy is invalid")
        expected_warning_sensor_keys = sorted(
            sensor_key
            for sensor_key, item in sensors.items()
            if any(
                isinstance(check, Mapping) and check.get("status") == "warning"
                for check in item.get("checks", [])
            )
        )
        recorded_warning_sensor_keys = report.get("warning_sensor_keys")
        if (
            not isinstance(recorded_warning_sensor_keys, list)
            or sorted(str(item) for item in recorded_warning_sensor_keys)
            != expected_warning_sensor_keys
            or report.get("warning_sensor_count") != len(expected_warning_sensor_keys)
        ):
            raise ValueError("Calibration time-offset warning evidence is invalid")
    search_grid = time_offset_values(
        float(recorded_search["minimum_robot_pose_time_offset_ms"]),
        float(recorded_search["maximum_robot_pose_time_offset_ms"]),
        float(recorded_search["step_ms"]),
    )
    minimum_offset = min(search_grid)
    maximum_offset = max(search_grid)
    required_auto_checks = {
        "fixed_full_range_observation_set",
        "cross_validation_offset_stability",
        "reference_method_sensitivity",
        "search_optimum_not_at_boundary",
        "cross_validated_translation_improvement",
        "cross_validated_rotation_guard",
        "zero_offset_identifiability",
    }
    required_auto_checks.update(
        {
            "cross_validation_fold_materiality",
            "leave_one_motion_out_timing_consistency",
        }
    )
    for sensor_key, item in sensors.items():
        status = str(item.get("status") or "")
        valid_statuses = (
            {"applied", "kept_zero"} if policy == "auto_offset" else {"fixed_zero"}
        )
        if status not in valid_statuses:
            raise ValueError(
                f"Calibration time-offset evidence is not promotable for {sensor_key}"
            )
        try:
            operator_offset = float(item["selected_robot_pose_time_offset_ms"])
            sync_delta = float(item["selected_sync_delta_ms"])
            candidate_offset = float(item["candidate_robot_pose_time_offset_ms"])
            candidate_sync_delta = (
                float(item["candidate_sync_delta_ms"])
                if policy == "auto_offset"
                else -candidate_offset
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Calibration time-offset evidence is invalid for {sensor_key}"
            ) from exc
        if (
            not math.isfinite(operator_offset)
            or not math.isfinite(sync_delta)
            or not math.isfinite(candidate_offset)
            or not math.isfinite(candidate_sync_delta)
            or not math.isclose(
                sync_delta,
                -operator_offset,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or not math.isclose(
                candidate_sync_delta,
                -candidate_offset,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise ValueError(
                f"Calibration time-offset sign evidence is inconsistent for {sensor_key}"
            )
        checks = item.get("checks")
        if not isinstance(checks, list):
            raise ValueError(
                f"Calibration time-offset checks are missing for {sensor_key}"
            )
        check_by_name = {
            str(check.get("name")): check
            for check in checks
            if isinstance(check, Mapping)
        }
        if policy == "auto_offset":
            if (
                item.get("improvement_evidence_strategy")
                != IMPROVEMENT_EVIDENCE_STRATEGY
            ):
                raise ValueError(
                    "Auto-sync improvement evidence strategy is invalid for "
                    f"{sensor_key}"
                )
            degraded_warning_fallback = bool(
                recorded_revision == TIME_OFFSET_IMPLEMENTATION_REVISION
                and item.get("warning_fallback_used") is True
            )
            if degraded_warning_fallback:
                warning_checks = [
                    check
                    for check in check_by_name.values()
                    if check.get("status") == "warning"
                ]
                converted_blockers = [
                    check
                    for check in warning_checks
                    if check.get("original_status") == "error"
                ]
                candidate_on_grid = any(
                    math.isclose(
                        candidate_offset,
                        value,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    for value in search_grid
                )
                if (
                    status != "kept_zero"
                    or item.get("decision") != "recorded_timing_kept"
                    or item.get("decision_reason")
                    != "ambiguous_auto_offset_kept_recorded_zero"
                    or item.get("evidence_strength") != "degraded"
                    or not math.isclose(
                        operator_offset,
                        0.0,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    or not candidate_on_grid
                    or not required_auto_checks.issubset(check_by_name)
                    or not converted_blockers
                    or any(
                        check.get("fallback")
                        != "recorded timing retained at 0 ms"
                        for check in converted_blockers
                    )
                    or any(
                        check.get("status") == "error"
                        for check in check_by_name.values()
                    )
                ):
                    raise ValueError(
                        "Degraded auto-sync warning fallback is invalid for "
                        f"{sensor_key}"
                    )
                _validate_promotion_motion_consistency(
                    item,
                    sensor_key=sensor_key,
                    candidate_offset=candidate_offset,
                    recorded_search=recorded_search,
                    search_grid=search_grid,
                    check_by_name=check_by_name,
                    require_passing=False,
                )
                continue
            if not required_auto_checks.issubset(check_by_name) or any(
                check.get("status") == "error" for check in check_by_name.values()
            ):
                raise ValueError(
                    f"Auto-sync checks are not promotable for {sensor_key}"
                )
            _validate_promotion_motion_consistency(
                item,
                sensor_key=sensor_key,
                candidate_offset=candidate_offset,
                recorded_search=recorded_search,
                search_grid=search_grid,
                check_by_name=check_by_name,
            )
            if status == "applied":
                if (
                    math.isclose(operator_offset, 0.0, rel_tol=0.0, abs_tol=1e-9)
                    or not math.isclose(
                        candidate_offset,
                        operator_offset,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    or item.get("boundary_hit") is not False
                    or not (minimum_offset < operator_offset < maximum_offset)
                    or not any(
                        math.isclose(
                            operator_offset,
                            value,
                            rel_tol=0.0,
                            abs_tol=1e-9,
                        )
                        for value in search_grid
                    )
                ):
                    raise ValueError(
                        f"Applied auto-sync offset is invalid for {sensor_key}"
                    )
            elif (
                not math.isclose(operator_offset, 0.0, rel_tol=0.0, abs_tol=1e-9)
                or not math.isclose(
                    candidate_offset,
                    0.0,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or check_by_name["zero_offset_identifiability"].get("status") != "ok"
            ):
                raise ValueError(
                    f"Zero-offset auto-sync evidence is invalid for {sensor_key}"
                )
        elif (
            checks
            or not math.isclose(operator_offset, 0.0, rel_tol=0.0, abs_tol=1e-9)
            or not math.isclose(candidate_offset, 0.0, rel_tol=0.0, abs_tol=1e-9)
        ):
            raise ValueError(
                f"Fixed-zero synchronization evidence is invalid for {sensor_key}"
            )
    _promotion_time_offset_artifact_bindings(
        attempt,
        request_value,
        sensors,
    )
    return sensors


def _promotion_time_offset_artifact_bindings(
    attempt: Mapping[str, Any],
    request_value: Mapping[str, Any],
    sensors: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind selected offsets to authoritative sync and solver observations."""

    run_root = Path(str(attempt["run_root"]))
    attempt_id = str(request_value["attempt_id"])
    attempt_root = calibration_attempt_root(run_root, attempt_id)
    source_reference = _attempt_artifact_reference(attempt_id, TIME_OFFSET_SEARCH)
    quality = _read_json(attempt_root / SYNC_QUALITY_REPORT)
    checks = quality.get("checks")
    if (
        quality.get("overall_status") == "error"
        or not isinstance(checks, list)
        or any(
            isinstance(item, Mapping) and item.get("status") == "error"
            for item in checks
        )
    ):
        raise ValueError("Authoritative synchronization quality is not promotable")
    policy = quality.get("calibration_attempt_policy")
    offsets = policy.get("per_sensor_offsets") if isinstance(policy, Mapping) else None
    expected_timing_warning_sensor_keys = sorted(
        sensor_key
        for sensor_key, alignment in sensors.items()
        if any(
            isinstance(check, Mapping) and check.get("status") == "warning"
            for check in alignment.get("checks", [])
        )
    )
    expected_warning_fallback_sensor_keys = sorted(
        sensor_key
        for sensor_key, alignment in sensors.items()
        if alignment.get("warning_fallback_used") is True
    )
    timing_warning_sensor_keys = (
        policy.get("timing_warning_sensor_keys")
        if isinstance(policy, Mapping)
        else None
    )
    warning_fallback_sensor_keys = (
        policy.get("warning_fallback_sensor_keys")
        if isinstance(policy, Mapping)
        else None
    )
    if (
        not isinstance(policy, Mapping)
        or policy.get("synchronization_policy")
        != request_value["synchronization_policy"]
        or policy.get("time_offset_search") != source_reference
        or not isinstance(offsets, Mapping)
        or set(offsets) != set(sensors)
        or not isinstance(timing_warning_sensor_keys, list)
        or sorted(str(item) for item in timing_warning_sensor_keys)
        != expected_timing_warning_sensor_keys
        or not isinstance(warning_fallback_sensor_keys, list)
        or sorted(str(item) for item in warning_fallback_sensor_keys)
        != expected_warning_fallback_sensor_keys
    ):
        raise ValueError("Authoritative synchronization provenance is inconsistent")

    sensor_metadata = {
        str(item["sensor_key"]): item
        for item in request_value.get("sensors", [])
        if isinstance(item, Mapping) and item.get("sensor_key")
    }
    summaries = {
        str(item.get("sensor_name")): item
        for item in quality.get("sensors", [])
        if isinstance(item, Mapping) and item.get("sensor_name")
    }
    for sensor_key, alignment in sensors.items():
        recorded = offsets.get(sensor_key)
        metadata = sensor_metadata.get(sensor_key)
        summary = (
            summaries.get(str(metadata.get("sensor_name")))
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            not isinstance(recorded, Mapping)
            or recorded.get("status") != alignment.get("status")
            or not _optional_floats_match(
                recorded.get("robot_pose_time_offset_ms"),
                alignment.get("selected_robot_pose_time_offset_ms"),
            )
            or not _optional_floats_match(
                recorded.get("sync_delta_ms"),
                alignment.get("selected_sync_delta_ms"),
            )
            or not isinstance(summary, Mapping)
            or not _optional_floats_match(
                summary.get("sync_delta_ms"),
                alignment.get("selected_sync_delta_ms"),
            )
        ):
            raise ValueError(
                f"Authoritative synchronization offset is inconsistent for {sensor_key}"
            )

    observations = _read_json(attempt_root / CALIBRATION_OBSERVATIONS)
    if observations.get("time_offset_search") != source_reference:
        raise ValueError("Calibration observations reference invalid timing evidence")
    identity_to_sensor = {
        (str(item.get("sensor_type")), str(item.get("device_id"))): sensor_key
        for sensor_key, item in sensor_metadata.items()
    }
    observation_counts = {sensor_key: 0 for sensor_key in sensors}
    for observation in observations.get("observations", []):
        if not isinstance(observation, Mapping):
            continue
        sensor_key = identity_to_sensor.get(
            (
                str(observation.get("sensor_type")),
                str(observation.get("device_id")),
            )
        )
        if sensor_key not in sensors:
            continue
        alignment = sensors[sensor_key]
        timestamp_alignment = observation.get("timestamp_alignment")
        if (
            not isinstance(timestamp_alignment, Mapping)
            or timestamp_alignment.get("source") != source_reference
            or not _optional_floats_match(
                observation.get("robot_pose_time_offset_ms"),
                alignment.get("selected_robot_pose_time_offset_ms"),
            )
            or not _optional_floats_match(
                observation.get("sync_delta_ms"),
                alignment.get("selected_sync_delta_ms"),
            )
            or not _optional_floats_match(
                timestamp_alignment.get("robot_pose_time_offset_ms"),
                alignment.get("selected_robot_pose_time_offset_ms"),
            )
            or not _optional_floats_match(
                timestamp_alignment.get("sync_delta_ms"),
                alignment.get("selected_sync_delta_ms"),
            )
        ):
            raise ValueError(
                f"Calibration observation timing is inconsistent for {sensor_key}"
            )
        observation_counts[sensor_key] += 1
    missing = sorted(
        sensor_key for sensor_key, count in observation_counts.items() if count == 0
    )
    if missing:
        raise ValueError(
            "Calibration observation timing evidence is missing for: "
            + ", ".join(missing)
        )


def create_promotion_request(
    run_root: str | Path,
    attempt_id: str,
    *,
    selections: Mapping[str, Any] | None = None,
    operator: str | None = None,
) -> dict[str, Any]:
    root = Path(run_root)
    attempt = load_calibration_attempt(root, attempt_id)
    _require_current_attempt_request(attempt["request"])
    if attempt["progress"].get("status") != "complete":
        raise ValueError("Calibration attempt is not complete")
    prior_promotion = attempt.get("promotion")
    if (
        isinstance(prior_promotion, Mapping)
        and prior_promotion.get("status") != "failed"
    ):
        raise ValueError("Calibration attempt already has promotion evidence")
    _promotion_time_offset_evidence(attempt)
    selected = _promotion_selections(attempt, selections)
    joint_bundle = _revalidate_joint_promotion(attempt, selected)
    value = {
        "schema_version": PROMOTION_REQUEST_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "run_root": root.as_posix(),
        "created_at": utc_now_iso(),
        "operator": str(operator).strip() if operator else None,
        "selections": selected,
        "joint_bundle_id": (
            joint_bundle["bundle_id"] if joint_bundle is not None else None
        ),
        "previous_failure": (
            dict(prior_promotion) if isinstance(prior_promotion, Mapping) else None
        ),
    }
    attempt_root = calibration_attempt_root(root, attempt_id)
    atomic_write_json(attempt_root / PROMOTION_REQUEST_FILE, value)
    atomic_write_json(
        attempt_root / PROMOTION_FILE,
        {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "status": "queued",
            "requested_at": value["created_at"],
            "selections": selected,
            "joint_bundle_id": value["joint_bundle_id"],
            "operator": value["operator"],
        },
    )
    return value


def _profile_slot(profile: CalibrationProfile) -> tuple[str, str]:
    return profile.sensor_type.value, profile.sensor_id


def _validate_promotion_request_identity(
    run_root: Path,
    attempt_id: str,
    promotion_request: Mapping[str, Any],
    promotion_status: Mapping[str, Any],
) -> None:
    if promotion_request.get("schema_version") != PROMOTION_REQUEST_SCHEMA_VERSION:
        raise ValueError("Unsupported calibration promotion request schema")
    if promotion_status.get("schema_version") != PROMOTION_SCHEMA_VERSION:
        raise ValueError("Unsupported calibration promotion status schema")
    if (
        promotion_request.get("attempt_id") != attempt_id
        or promotion_status.get("attempt_id") != attempt_id
    ):
        raise ValueError("Calibration promotion identity does not match its attempt")
    recorded_root = Path(str(promotion_request.get("run_root", ""))).resolve()
    if recorded_root != run_root.resolve():
        raise ValueError("Calibration promotion request belongs to a different run")
    request_selections = promotion_request.get("selections")
    status_selections = promotion_status.get("selections")
    if (
        not isinstance(request_selections, Mapping)
        or not request_selections
        or dict(request_selections) != status_selections
    ):
        raise ValueError(
            "Calibration promotion request/status selections are inconsistent"
        )
    if promotion_request.get("joint_bundle_id") != promotion_status.get(
        "joint_bundle_id"
    ):
        raise ValueError(
            "Calibration promotion request/status bundle identity is inconsistent"
        )


def _promotion_count(value: Any, *, label: str, candidate_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Candidate {candidate_id!r} has invalid {label} evidence")
    return value


def _promotion_ratio(value: Any, *, label: str, candidate_id: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Candidate {candidate_id!r} has invalid {label} evidence")
    try:
        ratio = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Candidate {candidate_id!r} has invalid {label} evidence"
        ) from exc
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise ValueError(f"Candidate {candidate_id!r} has invalid {label} evidence")
    return ratio


def _promotion_outlier_evidence(
    candidate: Mapping[str, Any],
    profile: CalibrationProfile,
    *,
    candidate_id: str,
) -> tuple[float, float]:
    """Revalidate the exact full-input outlier policy used by ranking."""

    validation = candidate.get("full_input_validation")
    if not isinstance(validation, Mapping):
        raise ValueError(
            f"Candidate {candidate_id!r} lacks full-input outlier evidence"
        )
    per_motion = validation.get("per_motion")
    if not isinstance(per_motion, Mapping) or not per_motion:
        raise ValueError(
            f"Candidate {candidate_id!r} lacks per-motion outlier evidence"
        )

    total_observations = 0
    total_inliers = 0
    total_outliers = 0
    motion_ratios: list[float] = []
    repeated_motion_ratios: list[float] = []
    for pose_key, raw_motion in per_motion.items():
        if not isinstance(raw_motion, Mapping):
            raise ValueError(
                f"Candidate {candidate_id!r} has invalid motion {pose_key!r} evidence"
            )
        observation_count = _promotion_count(
            raw_motion.get("observation_count"),
            label=f"motion {pose_key!r} observation_count",
            candidate_id=candidate_id,
        )
        inlier_count = _promotion_count(
            raw_motion.get("inlier_count"),
            label=f"motion {pose_key!r} inlier_count",
            candidate_id=candidate_id,
        )
        outlier_count = _promotion_count(
            raw_motion.get("outlier_count"),
            label=f"motion {pose_key!r} outlier_count",
            candidate_id=candidate_id,
        )
        if observation_count <= 0 or inlier_count + outlier_count != observation_count:
            raise ValueError(
                f"Candidate {candidate_id!r} has inconsistent motion "
                f"{pose_key!r} counts"
            )
        ratio = _promotion_ratio(
            raw_motion.get("outlier_ratio"),
            label=f"motion {pose_key!r} outlier_ratio",
            candidate_id=candidate_id,
        )
        expected_ratio = outlier_count / observation_count
        if not math.isclose(ratio, expected_ratio, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(
                f"Candidate {candidate_id!r} has inconsistent motion "
                f"{pose_key!r} outlier ratio"
            )
        total_observations += observation_count
        total_inliers += inlier_count
        total_outliers += outlier_count
        motion_ratios.append(ratio)
        if observation_count >= 4:
            repeated_motion_ratios.append(ratio)

    balanced_ratio = sum(motion_ratios) / len(motion_ratios)
    repeated_motion_ratio = max(repeated_motion_ratios, default=0.0)
    recorded_balanced_ratio = _promotion_ratio(
        validation.get("motion_balanced_outlier_ratio"),
        label="motion_balanced_outlier_ratio",
        candidate_id=candidate_id,
    )
    recorded_repeated_motion_ratio = _promotion_ratio(
        validation.get("max_repeated_motion_outlier_ratio"),
        label="max_repeated_motion_outlier_ratio",
        candidate_id=candidate_id,
    )
    candidate_ratio = _promotion_ratio(
        candidate.get("outlier_ratio"),
        label="candidate outlier_ratio",
        candidate_id=candidate_id,
    )
    profile_ratio = _promotion_ratio(
        profile.metadata.get("outlier_ratio"),
        label="profile outlier_ratio",
        candidate_id=candidate_id,
    )
    ratios = (
        recorded_balanced_ratio,
        candidate_ratio,
        profile_ratio,
    )
    if any(
        not math.isclose(value, balanced_ratio, rel_tol=0.0, abs_tol=1e-12)
        for value in ratios
    ) or not math.isclose(
        recorded_repeated_motion_ratio,
        repeated_motion_ratio,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            f"Candidate {candidate_id!r} has inconsistent aggregate outlier evidence"
        )

    candidate_observations = _promotion_count(
        candidate.get("observation_count"),
        label="candidate observation_count",
        candidate_id=candidate_id,
    )
    candidate_inliers = _promotion_count(
        candidate.get("inlier_count"),
        label="candidate inlier_count",
        candidate_id=candidate_id,
    )
    candidate_outliers = _promotion_count(
        candidate.get("outlier_count"),
        label="candidate outlier_count",
        candidate_id=candidate_id,
    )
    profile_outliers = _promotion_count(
        profile.metadata.get("outlier_count"),
        label="profile outlier_count",
        candidate_id=candidate_id,
    )
    if (
        candidate_observations != total_observations
        or candidate_inliers != total_inliers
        or candidate_outliers != total_outliers
        or profile.quality.num_observations != total_observations
        or profile.quality.num_inliers != total_inliers
        or profile_outliers != total_outliers
    ):
        raise ValueError(
            f"Candidate {candidate_id!r} has inconsistent full-input outlier counts"
        )
    raw_ratio = _promotion_ratio(
        candidate.get("raw_outlier_ratio"),
        label="raw_outlier_ratio",
        candidate_id=candidate_id,
    )
    if not math.isclose(
        raw_ratio,
        total_outliers / total_observations,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            f"Candidate {candidate_id!r} has inconsistent raw outlier evidence"
        )

    checks = candidate.get("checks")
    if not isinstance(checks, list):
        raise ValueError(f"Candidate {candidate_id!r} lacks validation checks")
    expected_checks = {
        "outlier_ratio": balanced_ratio,
        "full_input_repeated_motion_outlier_ratio": repeated_motion_ratio,
    }
    for check_name, expected_actual in expected_checks.items():
        matches = [
            check
            for check in checks
            if isinstance(check, Mapping) and check.get("name") == check_name
        ]
        if len(matches) != 1 or matches[0].get("status") != "ok":
            raise ValueError(
                f"Candidate {candidate_id!r} lacks passing {check_name} evidence"
            )
        actual = _promotion_ratio(
            matches[0].get("actual"),
            label=f"{check_name} check actual",
            candidate_id=candidate_id,
        )
        threshold = _promotion_ratio(
            matches[0].get("threshold"),
            label=f"{check_name} check threshold",
            candidate_id=candidate_id,
        )
        if not math.isclose(
            actual, expected_actual, rel_tol=0.0, abs_tol=1e-12
        ) or not math.isclose(
            threshold,
            DEFAULT_MAX_OUTLIER_RATIO,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(
                f"Candidate {candidate_id!r} has inconsistent {check_name} check"
            )
    return balanced_ratio, repeated_motion_ratio


def _promotion_transform_records_match(
    ranked: Any,
    profiled: Any,
    *,
    label: str,
    candidate_id: str,
) -> None:
    """Require the profile to preserve the exact ranked transform evidence."""

    if not isinstance(ranked, Mapping) or not isinstance(profiled, Mapping):
        raise ValueError(f"Candidate {candidate_id!r} lacks {label} transform evidence")
    ranked_frames = (str(ranked.get("from")), str(ranked.get("to")))
    profiled_frames = (str(profiled.get("from")), str(profiled.get("to")))
    if ranked_frames != profiled_frames:
        raise ValueError(
            f"Candidate profile {candidate_id!r} has inconsistent {label} frames"
        )
    try:
        residual = transform_residual(
            transform_from_record(ranked),
            transform_from_record(profiled),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Candidate {candidate_id!r} has invalid {label} transform evidence"
        ) from exc
    if (
        residual["translation_mm"] > PROMOTION_TRANSFORM_TOLERANCE_MM
        or residual["rotation_deg"] > PROMOTION_TRANSFORM_TOLERANCE_DEG
    ):
        raise ValueError(
            f"Candidate profile {candidate_id!r} does not match ranked {label} "
            "transform evidence"
        )


def _promotion_transform_evidence(
    candidate: Mapping[str, Any],
    profile: CalibrationProfile,
    *,
    candidate_id: str,
) -> None:
    primary = {
        "from": profile.extrinsics.from_frame.value,
        "to": profile.extrinsics.to_frame.value,
        "rotation_quaternion_wxyz": list(profile.extrinsics.rotation_quaternion_wxyz),
        "translation_mm": list(profile.extrinsics.translation_mm),
    }
    _promotion_transform_records_match(
        candidate.get("primary_transform"),
        primary,
        label="primary",
        candidate_id=candidate_id,
    )
    _promotion_transform_records_match(
        candidate.get("companion_transform"),
        profile.metadata.get("companion_transform"),
        label="companion",
        candidate_id=candidate_id,
    )


def _selected_profiles(
    attempt_root: Path,
    attempt: Mapping[str, Any],
    request_value: Mapping[str, Any],
    promotion_request: Mapping[str, Any],
) -> list[CalibrationProfile]:
    time_offset_by_sensor = _promotion_time_offset_evidence(attempt)
    time_offset_source = _attempt_artifact_reference(
        str(request_value["attempt_id"]),
        TIME_OFFSET_SEARCH,
    )
    joint_bundle = _revalidate_joint_promotion(
        attempt,
        {
            str(sensor_key): str(candidate_id)
            for sensor_key, candidate_id in promotion_request["selections"].items()
        },
        expected_bundle_id=(
            str(promotion_request["joint_bundle_id"])
            if promotion_request.get("joint_bundle_id") is not None
            else None
        ),
    )
    if joint_bundle is not None and promotion_request.get("joint_bundle_id") is None:
        raise ValueError("Promotion request lacks its multi-camera bundle identity")
    profiles = load_profile_collection(attempt_root / CANDIDATE_PROFILES_FILE)
    by_candidate = {
        str(profile.metadata.get("candidate_id")): profile for profile in profiles
    }
    timestamp = utc_now_iso()
    selected = []
    for sensor_key, candidate_id in promotion_request["selections"].items():
        profile = by_candidate.get(str(candidate_id))
        if profile is None:
            raise ValueError(f"Candidate profile not found: {candidate_id}")
        if profile.metadata.get("sensor_key") != sensor_key:
            raise ValueError(
                f"Candidate profile {candidate_id!r} does not belong to {sensor_key}"
            )
        if profile.metadata.get("robot_pose_reference") != request_value.get(
            "robot_pose_reference"
        ):
            raise ValueError(
                f"Candidate profile {candidate_id!r} does not retain the immutable "
                "robot-pose artifact binding from its attempt request"
            )
        result = next(
            item
            for item in attempt["results"]["results"]
            if item["sensor_key"] == sensor_key
        )
        candidate = next(
            item
            for item in result["candidates"]
            if item["candidate_id"] == candidate_id
        )
        if candidate["status"] != "passing":
            raise ValueError(f"Candidate no longer passes validation: {candidate_id}")
        alignment = time_offset_by_sensor.get(str(sensor_key))
        expected_sync_delta_ms = (
            float(alignment["selected_sync_delta_ms"])
            if alignment is not None
            else None
        )
        candidate_synchronization = candidate.get("synchronization")
        profile_synchronization = profile.metadata.get("synchronization")
        if alignment is not None and (
            not isinstance(candidate_synchronization, Mapping)
            or not isinstance(profile_synchronization, Mapping)
            or candidate_synchronization.get("source") != time_offset_source
            or profile_synchronization.get("source") != time_offset_source
            or candidate_synchronization.get("policy")
            != request_value["synchronization_policy"]
            or profile_synchronization.get("policy")
            != request_value["synchronization_policy"]
            or candidate_synchronization.get("status") != alignment.get("status")
            or profile_synchronization.get("status") != alignment.get("status")
            or bool(candidate_synchronization.get("warning_fallback_used"))
            != bool(alignment.get("warning_fallback_used"))
            or bool(profile_synchronization.get("warning_fallback_used"))
            != bool(alignment.get("warning_fallback_used"))
            or not _optional_floats_match(
                candidate_synchronization.get("robot_pose_time_offset_ms"),
                alignment.get("selected_robot_pose_time_offset_ms"),
            )
            or not _optional_floats_match(
                profile_synchronization.get("robot_pose_time_offset_ms"),
                alignment.get("selected_robot_pose_time_offset_ms"),
            )
            or not _optional_floats_match(
                candidate_synchronization.get("sync_delta_ms"),
                expected_sync_delta_ms,
            )
            or not _optional_floats_match(
                profile_synchronization.get("sync_delta_ms"),
                expected_sync_delta_ms,
            )
        ):
            raise ValueError(
                f"Candidate {candidate_id!r} has inconsistent auto-sync provenance"
            )
        if alignment is not None and not _optional_floats_match(
            profile.sync_delta_ms, expected_sync_delta_ms
        ):
            raise ValueError(
                f"Candidate {candidate_id!r} profile sync delta is inconsistent"
            )
        _promotion_transform_evidence(
            candidate,
            profile,
            candidate_id=candidate_id,
        )
        inlier_count = profile.quality.num_inliers
        outlier_ratio, repeated_motion_outlier_ratio = _promotion_outlier_evidence(
            candidate,
            profile,
            candidate_id=candidate_id,
        )
        if (
            inlier_count < DEFAULT_MIN_INLIERS
            or profile.quality.residual_translation_mm is None
            or profile.quality.residual_translation_mm > DEFAULT_MAX_MEAN_TRANSLATION_MM
            or profile.quality.residual_rotation_deg is None
            or profile.quality.residual_rotation_deg > DEFAULT_MAX_MEAN_ROTATION_DEG
            or outlier_ratio > DEFAULT_MAX_OUTLIER_RATIO
            or repeated_motion_outlier_ratio > DEFAULT_MAX_OUTLIER_RATIO
        ):
            raise ValueError(
                f"Candidate no longer satisfies promotion gates: {candidate_id}"
            )
        metadata = dict(profile.metadata)
        metadata.update(
            {
                "promotion_attempt_id": request_value["attempt_id"],
                "promotion_candidate_id": candidate_id,
                "promotion_solver_provenance": {
                    "solver_policy": request_value["solver_policy"],
                    "pnp_method": candidate["pnp_method"],
                    "extrinsic_method": candidate["extrinsic_method"],
                },
                "promotion_synchronization_provenance": (
                    {
                        "source": time_offset_source,
                        "status": alignment["status"],
                        "robot_pose_time_offset_ms": alignment[
                            "selected_robot_pose_time_offset_ms"
                        ],
                        "sync_delta_ms": alignment["selected_sync_delta_ms"],
                    }
                    if alignment is not None
                    else {
                        "source": "historical_fixed_zero",
                        "sync_delta_ms": profile.sync_delta_ms,
                    }
                ),
                "promotion_multi_camera_bundle_id": (
                    joint_bundle["bundle_id"] if joint_bundle is not None else None
                ),
                "promoted_at": timestamp,
                "promoted_by": promotion_request.get("operator"),
            }
        )
        selected.append(
            replace(
                profile,
                status=CalibrationStatus.VALID,
                calibrated_at=timestamp,
                operator=promotion_request.get("operator") or profile.operator,
                metadata=metadata,
            )
        )
    return selected


def _canonical_reports(
    attempt_root: Path,
    attempt: Mapping[str, Any],
    selected_profiles: Sequence[CalibrationProfile],
    promotion_request: Mapping[str, Any],
    *,
    canonical_profile_count: int,
) -> dict[str, dict[str, Any]]:
    extrinsic = _read_json(attempt_root / EXTRINSIC_CANDIDATES_FILE)
    ranking = attempt["results"]
    all_profile_values = [profile_to_dict(profile) for profile in selected_profiles]
    selected_ids = {profile.profile_id for profile in selected_profiles}
    selected_candidate_ids = set(promotion_request["selections"].values())
    selected_candidates = [
        item
        for item in extrinsic["candidates"]
        if item.get("candidate_id") in selected_candidate_ids
    ]
    selected_results = [
        {
            **dict(result),
            "candidates": [
                item
                for item in result.get("candidates", [])
                if item.get("candidate_id") in selected_candidate_ids
            ],
        }
        for result in ranking["results"]
        if result.get("sensor_key") in promotion_request["selections"]
    ]
    time_offset_search = attempt.get("time_offset_search")
    synchronization_summary = (
        {
            "policy": time_offset_search.get("policy"),
            "source": (
                f"processed/calibration/{attempt['attempt_id']}/{TIME_OFFSET_SEARCH}"
            ),
            "sensors": [
                {
                    "sensor_key": item.get("sensor_key"),
                    "status": item.get("status"),
                    "robot_pose_time_offset_ms": item.get(
                        "selected_robot_pose_time_offset_ms"
                    ),
                    "sync_delta_ms": item.get("selected_sync_delta_ms"),
                }
                for item in time_offset_search.get("sensors", [])
                if isinstance(item, Mapping)
                and item.get("sensor_key") in promotion_request["selections"]
            ],
        }
        if isinstance(time_offset_search, Mapping)
        else {
            "policy": "fixed_zero",
            "source": "historical_attempt_without_time_offset_search",
            "sensors": [],
        }
    )
    selected_checks = [
        item
        for item in _read_json(attempt_root / CHECKS_FILE)["checks"]
        if item.get("candidate_id") in selected_candidate_ids
        or (
            promotion_request.get("joint_bundle_id") is not None
            and item.get("bundle_id") == promotion_request.get("joint_bundle_id")
        )
    ]
    candidate_report = {
        "schema_version": "calibration_candidates.v1",
        "generated_at": utc_now_iso(),
        "run_root": attempt["run_root"],
        "attempt_id": attempt["attempt_id"],
        "overall_status": "ok",
        "candidate_count": len(selected_candidates),
        "inlier_count": sum(
            profile.quality.num_inliers for profile in selected_profiles
        ),
        "outlier_count": sum(
            profile.quality.num_observations - profile.quality.num_inliers
            for profile in selected_profiles
        ),
        "profiles": all_profile_values,
        "candidates": selected_candidates,
        "comparisons": selected_results,
        "synchronization": synchronization_summary,
        "checks": selected_checks,
    }
    multi_camera_consistency = ranking.get("multi_camera_consistency")
    if isinstance(multi_camera_consistency, Mapping):
        selected_bundle_id = promotion_request.get("joint_bundle_id")
        selected_bundle = next(
            (
                dict(bundle)
                for bundle in multi_camera_consistency.get("bundles", [])
                if isinstance(bundle, Mapping)
                and bundle.get("bundle_id") == selected_bundle_id
            ),
            None,
        )
        candidate_report["multi_camera_consistency"] = {
            **dict(multi_camera_consistency),
            "bundles": [selected_bundle] if selected_bundle is not None else [],
            "recommendation": selected_bundle,
        }
    solver_report = {
        "schema_version": "calibration_solver.v2",
        "generated_at": utc_now_iso(),
        "run_root": attempt["run_root"],
        "attempt_id": attempt["attempt_id"],
        "overall_status": "ok",
        "mode": attempt["request"]["mode"],
        "profile_count": len(all_profile_values),
        "candidate_count": len(selected_candidates),
        "profiles": all_profile_values,
        "solutions": selected_candidates,
        "comparisons": selected_results,
        "synchronization": synchronization_summary,
        "checks": candidate_report["checks"],
    }
    if "multi_camera_consistency" in candidate_report:
        solver_report["multi_camera_consistency"] = candidate_report[
            "multi_camera_consistency"
        ]
    validation_report = {
        "schema_version": "calibration_validation.v1",
        "generated_at": utc_now_iso(),
        "run_root": attempt["run_root"],
        "attempt_id": attempt["attempt_id"],
        "overall_status": "ok",
        "profile_count": len(selected_profiles),
        "promotable_profile_count": len(selected_profiles),
        "selection": {
            "requested": dict(promotion_request["selections"]),
            "selected_profile_ids": sorted(selected_ids),
            "explicit_selection_required": True,
            "joint_bundle_id": promotion_request.get("joint_bundle_id"),
        },
        "synchronization": synchronization_summary,
        "promotion": {
            "requested": True,
            "promoted": True,
            "path": CALIBRATION_PROFILES,
            "profile_count": canonical_profile_count,
            "promoted_profile_ids": sorted(selected_ids),
        },
        "profiles": [
            {
                "profile_id": profile.profile_id,
                "sensor_id": profile.sensor_id,
                "sensor_type": profile.sensor_type.value,
                "mounting_mode": profile.mounting_mode.value,
                "validation_status": "ok",
                "selected": True,
                "promotable": True,
                "num_observations": profile.quality.num_observations,
                "num_inliers": profile.quality.num_inliers,
                "residual_translation_mm": profile.quality.residual_translation_mm,
                "residual_rotation_deg": profile.quality.residual_rotation_deg,
            }
            for profile in selected_profiles
        ],
        "checks": [],
    }
    return {
        CALIBRATION_CANDIDATES: candidate_report,
        CALIBRATION_SOLVER_REPORT: solver_report,
        CALIBRATION_VALIDATION_REPORT: validation_report,
    }


def _transactional_replace(
    run_root: Path,
    promotions: Sequence[tuple[Path, Path]],
) -> None:
    backup_root = run_root / f".calibration-promotion-backup-{uuid.uuid4().hex}"
    backup_root.mkdir(parents=False, exist_ok=False)
    installed: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    try:
        for index, (source, destination) in enumerate(promotions):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                backup = backup_root / str(index)
                os.replace(destination, backup)
                backups.append((backup, destination))
            os.replace(source, destination)
            installed.append(destination)
    except Exception:
        for destination in reversed(installed):
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
        for backup, destination in reversed(backups):
            os.replace(backup, destination)
        raise
    finally:
        if backup_root.exists():
            shutil.rmtree(backup_root)


def promote_calibration_attempt(
    run_root: str | Path, attempt_id: str
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with run_config_lock(root):
        return _promote_calibration_attempt_locked(root, attempt_id)


def _promote_calibration_attempt_locked(
    run_root: str | Path, attempt_id: str
) -> dict[str, Any]:
    root = Path(run_root)
    attempt_root = calibration_attempt_root(root, attempt_id)
    attempt = load_calibration_attempt(root, attempt_id)
    request_value = attempt["request"]
    _require_current_attempt_request(request_value)
    if request_value.get("mode") == "eye_to_hand":
        reference_evidence = request_value.get("robot_pose_reference")
        if not isinstance(reference_evidence, Mapping):
            raise ValueError(
                "Static calibration promotion requires robot-pose reference "
                "evidence from the immutable attempt request"
            )
        _require_static_pose_template_base_reference(root, reference_evidence)
    _verify_robot_pose_artifact_bindings(root, request_value)
    promotion_request = _read_json(attempt_root / PROMOTION_REQUEST_FILE)
    promotion_path = attempt_root / PROMOTION_FILE
    current = _read_json(promotion_path)
    _validate_promotion_request_identity(
        root,
        attempt_id,
        promotion_request,
        current,
    )
    current.update({"status": "running", "started_at": utc_now_iso()})
    atomic_write_json(promotion_path, current)
    staging = root / f".calibration-promotion-{attempt_id}-{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        current_config = load_run_config_for_run_root(root)
        requested_target = request_value.get("target_bundle", {}).get("selection")
        try:
            current_target = validate_run_target_selection(
                root,
                require_mounting_frame=True,
            )["selection"]
        except (FileNotFoundError, ValueError) as exc:
            blockers = [
                item
                for item in replacement_blockers(root)
                if not item.startswith(f"{ATTEMPT_DIRECTORY.as_posix()}/")
            ]
            raise CalibrationTargetConflict(
                "Canonical calibration-target evidence changed after this attempt was created.",
                blockers=blockers,
            ) from exc
        if not isinstance(requested_target, Mapping) or current_target != dict(
            requested_target
        ):
            blockers = [
                item
                for item in replacement_blockers(root)
                if not item.startswith(f"{ATTEMPT_DIRECTORY.as_posix()}/")
            ]
            raise CalibrationTargetConflict(
                "Canonical calibration-target selection changed after this attempt was created.",
                blockers=blockers,
            )
        selected_profiles = _selected_profiles(
            attempt_root, attempt, request_value, promotion_request
        )
        existing_path = root / CALIBRATION_PROFILES
        existing = (
            load_profile_collection(existing_path) if existing_path.is_file() else []
        )
        promoted_slots = {_profile_slot(profile) for profile in selected_profiles}
        preserved = [
            profile
            for profile in existing
            if _profile_slot(profile) not in promoted_slots
        ]
        merged = [*preserved, *selected_profiles]
        write_profile_collection(merged, staging / CALIBRATION_PROFILES)
        write_profile_collection(
            list(selected_profiles),
            staging / CALIBRATION_PROFILES_FROM_OBSERVATIONS,
        )
        write_profile_collection(
            list(selected_profiles),
            staging / CALIBRATION_PROFILES_SOLVED,
        )
        selected_sensor_keys = set(promotion_request["selections"])
        selected_candidate_ids = set(promotion_request["selections"].values())
        selected_pnp_methods = {}
        for result in attempt["results"]["results"]:
            sensor_key = result.get("sensor_key")
            if sensor_key not in selected_sensor_keys:
                continue
            selected_candidate_id = promotion_request["selections"][sensor_key]
            selected_candidate = next(
                item
                for item in result["candidates"]
                if item["candidate_id"] == selected_candidate_id
            )
            selected_pnp_methods[sensor_key] = selected_candidate["pnp_method"]
        observations_report = _read_json(attempt_root / CALIBRATION_OBSERVATIONS)
        selected_observations = []
        for item in observations_report.get("observations", []):
            if not isinstance(item, Mapping):
                continue
            sensor_key = _sensor_key(
                str(item.get("sensor_type")), str(item.get("device_id"))
            )
            if sensor_key in selected_sensor_keys and item.get(
                "pnp_method"
            ) == selected_pnp_methods.get(sensor_key):
                selected_observations.append(dict(item))
        observations_report["observations"] = selected_observations
        observations_report["observation_count"] = len(selected_observations)
        observations_report["sensors"] = [
            item
            for item in observations_report.get("sensors", [])
            if item.get("sensor_key") in selected_sensor_keys
        ]
        observations_report["sensor_count"] = len(observations_report["sensors"])
        observations_report["promoted_candidate_ids"] = sorted(selected_candidate_ids)
        atomic_write_json(staging / CALIBRATION_OBSERVATIONS, observations_report)
        attempt_intrinsics = load_intrinsic_profile_collection(
            attempt_root / INTRINSIC_CALIBRATION_PROFILES
        )
        existing_intrinsics_path = root / INTRINSIC_CALIBRATION_PROFILES
        existing_intrinsics = (
            load_intrinsic_profile_collection(existing_intrinsics_path)
            if existing_intrinsics_path.is_file()
            else []
        )
        selected_sensor_ids = {profile.sensor_id for profile in selected_profiles}
        promoted_intrinsics = [
            item
            for item in attempt_intrinsics
            if str(item.get("sensor_id")) in selected_sensor_ids
        ]
        promoted_intrinsic_keys = {
            (
                str(item["sensor_id"]),
                tuple(item["resolution"]),
                str(item["orientation"]),
            )
            for item in promoted_intrinsics
        }
        preserved_intrinsics = [
            item
            for item in existing_intrinsics
            if (
                str(item["sensor_id"]),
                tuple(item["resolution"]),
                str(item["orientation"]),
            )
            not in promoted_intrinsic_keys
        ]
        write_intrinsic_profile_collection(
            [*preserved_intrinsics, *promoted_intrinsics],
            staging / INTRINSIC_CALIBRATION_PROFILES,
        )
        reports = _canonical_reports(
            attempt_root,
            attempt,
            selected_profiles,
            promotion_request,
            canonical_profile_count=len(merged),
        )
        for filename, report in reports.items():
            atomic_write_json(staging / filename, report)
        target = dict(request_value["target"])
        atomic_write_json(staging / CALIBRATION_TARGET, target)
        updated_config = dict(current_config)
        updated_config["calibration_target"] = request_value["target_bundle"][
            "selection"
        ]
        capture = dict(updated_config["capture"])
        sensors = []
        selected_by_identity = {
            (profile.sensor_type.value, profile.sensor_id): profile
            for profile in selected_profiles
        }
        for raw_sensor in capture["sensors"]:
            sensor = dict(raw_sensor)
            profile = selected_by_identity.get(
                (str(sensor.get("sensor_type")), str(sensor.get("device_id")))
            )
            if profile is not None:
                sensor["mounting_mode"] = profile.mounting_mode.value
                sensor["calibration_profile_id"] = profile.profile_id
            sensors.append(sensor)
        capture["sensors"] = sensors
        updated_config["capture"] = capture
        updated_config["calibration_profiles"] = CALIBRATION_PROFILES
        validate_run_config(updated_config)
        atomic_write_json(staging / RUN_CONFIG, updated_config)
        manifest = load_or_create_run_manifest(root)
        canonical_artifacts = {
            filename: root / filename
            for filename in (
                CALIBRATION_TARGET,
                INTRINSIC_CALIBRATION_PROFILES,
                CALIBRATION_OBSERVATIONS,
                CALIBRATION_CANDIDATES,
                CALIBRATION_PROFILES_FROM_OBSERVATIONS,
                CALIBRATION_SOLVER_REPORT,
                CALIBRATION_PROFILES_SOLVED,
                CALIBRATION_VALIDATION_REPORT,
                CALIBRATION_PROFILES,
                RUN_CONFIG,
            )
        }
        upsert_stage(
            manifest,
            name="calibration_attempt_promotion",
            status="succeeded",
            artifacts=canonical_artifacts,
            run_root=root,
            message=f"Promoted calibration attempt {attempt_id}.",
        )
        atomic_write_json(staging / DATASET_MANIFEST, manifest)
        bundle_stage = staging / TARGET_BUNDLE_DIRECTORY
        shutil.copytree(attempt_root / TARGET_BUNDLE_DIRECTORY, bundle_stage)
        promotions = [
            (staging / filename, root / filename)
            for filename in (
                CALIBRATION_TARGET,
                INTRINSIC_CALIBRATION_PROFILES,
                CALIBRATION_OBSERVATIONS,
                CALIBRATION_CANDIDATES,
                CALIBRATION_PROFILES_FROM_OBSERVATIONS,
                CALIBRATION_SOLVER_REPORT,
                CALIBRATION_PROFILES_SOLVED,
                CALIBRATION_VALIDATION_REPORT,
                CALIBRATION_PROFILES,
                RUN_CONFIG,
                DATASET_MANIFEST,
            )
        ]
        promotions.append(
            (
                bundle_stage,
                root / LIBRARY_DIRECTORY / str(request_value["target_id"]),
            )
        )
        _transactional_replace(root, promotions)
        promoted = {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "status": "promoted",
            "requested_at": promotion_request["created_at"],
            "promoted_at": utc_now_iso(),
            "operator": promotion_request.get("operator"),
            "selections": dict(promotion_request["selections"]),
            "joint_bundle_id": promotion_request.get("joint_bundle_id"),
            "promoted_profile_ids": [
                profile.profile_id for profile in selected_profiles
            ],
            "preserved_profile_ids": [profile.profile_id for profile in preserved],
            "canonical_artifacts": sorted(canonical_artifacts),
        }
        atomic_write_json(promotion_path, promoted)
        return promoted
    except Exception as exc:
        failed = _read_json(promotion_path)
        failed.update(
            {
                "status": "failed",
                "ended_at": utc_now_iso(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        atomic_write_json(promotion_path, failed)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
