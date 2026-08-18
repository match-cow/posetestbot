"""Compose browser-ready cell scenes with pytransform3d as frame authority."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import cv2
import numpy as np
from pytransform3d import rotations as pr
from pytransform3d import transformations as pt
from pytransform3d.transform_manager import TransformManager

from posetestbot.calibration.profiles import (
    CalibrationProfile,
    load_profile_collection,
    select_valid_profile_for_sensor,
)
from posetestbot.calibration.static_reuse import (
    verify_static_profile_destination_reference,
)
from posetestbot.calibration.targets import DEFAULT_TARGET_SPEC
from posetestbot.io.artifacts import (
    BOP_DIR,
    BOP_EXPORT_MANIFEST,
    CALIBRATION_TARGET,
    CALIBRATION_PROFILE_SELECTION,
    DEPTH_DIR,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
    MATCH_ROBOT_EE_POSES,
    PROCESSED_DIR,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.pose_templates.selection import load_pose_template_selection
from posetestbot.sensors.contracts import MountingMode, SensorType
from posetestbot.sensors.registry import sensor_folder_name

SCENE_SCHEMA_VERSION = "cell_scene.v1"
TIMELINE_SCHEMA_VERSION = "cell_timeline.v1"
MAX_TIMELINE_PAGE = 2_000
MAX_PREVIEW_POSES = 200
DEPTH_PREVIEW_MIN_MM = 200.0
DEPTH_PREVIEW_MAX_MM = 3_000.0
CALIBRATION_ATTEMPT_DIRECTORY = Path("processed") / "calibration"
POSE_TEMPLATE_BUNDLE = "pose_template_bundle.json"
POSE_TEMPLATE_PREVIEW = "pose_template_preview.json"
TARGET_FRONT_PRESENTATION = np.diag([1.0, -1.0, -1.0, 1.0])


def _matrix(quaternion: Any, translation: Any) -> np.ndarray:
    q = np.asarray(quaternion, dtype=float)
    t = np.asarray(translation, dtype=float)
    if q.shape != (4,) or t.shape != (3,) or not np.all(np.isfinite([*q, *t])):
        raise ValueError("Transform requires finite quaternion[4] and translation[3]")
    norm = float(np.linalg.norm(q))
    if not math.isclose(norm, 1.0, abs_tol=1e-3):
        raise ValueError("Transform quaternion must be normalized")
    return pt.transform_from(pr.matrix_from_quaternion(q), t)


def _transform_dict(matrix: np.ndarray, parent: str) -> dict[str, Any]:
    return {
        "semantics": "entity_to_parent",
        "parent_frame": parent,
        "translation_mm": matrix[:3, 3].tolist(),
        "rotation_quaternion_wxyz": pr.quaternion_from_matrix(matrix[:3, :3]).tolist(),
    }


def _identity(parent: str | None = None) -> dict[str, Any]:
    return {
        "semantics": "entity_to_parent",
        "parent_frame": parent,
        "translation_mm": [0.0, 0.0, 0.0],
        "rotation_quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
    }


def _canonical_grid_frame(target: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the supported grid frame, including the legacy v1 default."""

    if target.get("target_type") != "aruco_grid":
        return None
    raw = target.get("frame", DEFAULT_TARGET_SPEC["frame"])
    if not isinstance(raw, Mapping):
        return None
    axes = raw.get("axes")
    if not isinstance(axes, Mapping):
        return None
    frame = {
        "name": raw.get("name"),
        "origin": raw.get("origin"),
        "axes": {
            "x": axes.get("x"),
            "y": axes.get("y"),
            "z": axes.get("z"),
        },
    }
    expected = DEFAULT_TARGET_SPEC["frame"]
    return frame if frame == expected else None


def _reference_presentation(reference_frame: str) -> dict[str, Any]:
    matrix = np.eye(4)
    return {
        "mode": "reference_z_up",
        "presentation_only": True,
        "source_frame": reference_frame,
        "anchor_frame": reference_frame,
        "display_up_axis": "+Z",
        "matrix": matrix.tolist(),
        "transform": _transform_dict(matrix, "display"),
        "target_frame": None,
    }


def _target_front_presentation(
    target_to_reference: np.ndarray,
    *,
    reference_frame: str,
    target_frame: Mapping[str, Any],
) -> dict[str, Any]:
    """Map a right/down/into grid frame to a conventional right-handed Z-up view."""

    reference_to_display = TARGET_FRONT_PRESENTATION @ pt.invert_transform(
        target_to_reference
    )
    return {
        "mode": "calibration_target_front",
        "presentation_only": True,
        "source_frame": reference_frame,
        "anchor_frame": "calibration_target",
        "display_up_axis": "+Z",
        "source_front_axis": "-Z",
        "matrix": reference_to_display.tolist(),
        "transform": _transform_dict(reference_to_display, "display"),
        "target_frame": dict(target_frame),
    }


def _entity(
    entity_id: str,
    entity_type: str,
    label: str,
    *,
    transform: dict[str, Any] | None,
    status: str,
    provenance: Mapping[str, Any],
    reason: str | None = None,
    geometry: Mapping[str, Any] | None = None,
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        "id": entity_id,
        "type": entity_type,
        "label": label,
        "status": status,
        "transform": transform,
        "unresolved_reason": reason,
        "geometry": dict(geometry or {}),
        "provenance": dict(provenance),
    }
    if calibration is not None:
        value["calibration"] = dict(calibration)
    return value


def _kuka_pose(value: Mapping[str, Any]) -> np.ndarray:
    try:
        translation = [float(value[key]) for key in ("X", "Y", "Z")]
        euler = [float(value[key]) for key in ("C", "B", "A")]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("KUKA pose requires finite X/Y/Z/A/B/C values") from exc
    if not np.all(np.isfinite([*translation, *euler])):
        raise ValueError("KUKA pose contains non-finite values")
    return pt.transform_from(pr.matrix_from_euler(euler, 0, 1, 2, True), translation)


def _read_mapping(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, Mapping):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _matched_timeline(sensor_folder: Path) -> list[dict[str, Any]]:
    values = _read_mapping(sensor_folder / MATCH_ROBOT_EE_POSES)
    poses: list[dict[str, Any]] = []
    for filename, record in values.items():
        if not isinstance(record, Mapping) or not isinstance(
            record.get("robot_ee_pose"), Mapping
        ):
            raise ValueError(f"Invalid matched robot pose record {filename!r}")
        try:
            frame_index = int(Path(str(filename)).stem)
        except ValueError as exc:
            raise ValueError(
                f"Matched pose frame must have a numeric stem: {filename!r}"
            ) from exc
        poses.append(
            {
                "frame_index": frame_index,
                "frame_id": str(filename),
                "timestamp_ns": record.get("frame_timestamp_ns")
                or record.get("timestamp_ns"),
                "motion": record.get("motion"),
                "matrix": _kuka_pose(record["robot_ee_pose"]),
            }
        )
    return sorted(poses, key=lambda item: (item["frame_index"], item["frame_id"]))


def _raw_timeline(path: Path) -> list[dict[str, Any]]:
    values = _read_mapping(path)
    poses: list[dict[str, Any]] = []
    for key, record in values.items():
        if not isinstance(record, Mapping):
            raise ValueError(f"Invalid raw robot pose record {key!r}")
        pose = record.get("pose") or record.get("robot_ee_pose")
        if not isinstance(pose, Mapping):
            raise ValueError(f"Raw robot pose {key!r} lacks pose coordinates")
        try:
            frame_index = int(key)
        except ValueError:
            frame_index = len(poses)
        poses.append(
            {
                "frame_index": frame_index,
                "frame_id": str(record.get("framename") or key),
                "timestamp_ns": record.get("host_received_timestamp_ns")
                or record.get("host_wall_timestamp_ns"),
                "motion": record.get("motion"),
                "matrix": _kuka_pose(pose),
            }
        )
    return sorted(poses, key=lambda item: item["frame_index"])


def _stored_image_rotation_degrees(sensor_folder: Path) -> int | None:
    metadata_path = sensor_folder / FRAME_METADATA_JSONL
    if not metadata_path.is_file():
        return None
    try:
        with metadata_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, Mapping):
                    continue
                raw = record.get("image_rotation_degrees")
                if raw is None:
                    continue
                rotation = int(raw)
                if rotation in {0, 180}:
                    return rotation
                return None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _image_presentation(
    sensor: Mapping[str, Any], sensor_folder: Path
) -> dict[str, Any]:
    inverted = sensor.get("inverted") is True
    stored_rotation = _stored_image_rotation_degrees(sensor_folder)
    display_rotation = 180 if inverted and stored_rotation != 180 else 0
    if not inverted:
        correction = "not_required"
    elif stored_rotation == 180:
        correction = "capture"
    else:
        correction = "viewer"
    return {
        "configured_inverted": inverted,
        "stored_rotation_degrees": stored_rotation,
        "display_rotation_degrees": display_rotation,
        "correction": correction,
    }


def _depth_scale_to_mm(sensor_folder: Path) -> float | None:
    path = sensor_folder / DEPTH_SCALE
    try:
        value = float(path.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _synchronized_source_contexts(
    run_root: Path,
    config: Mapping[str, Any],
    *,
    timeline_id: str | None = None,
) -> list[dict[str, Any]]:
    candidates = [
        run_root / PROCESSED_DIR / "rectified",
        run_root / PROCESSED_DIR / SYNCHRONIZED_DIR,
    ]
    input_root = next((path for path in candidates if path.is_dir()), None)
    enabled_sensors: list[tuple[str, Mapping[str, Any]]] = []
    for sensor in config.get("capture", {}).get("sensors", []):
        if not isinstance(sensor, Mapping) or sensor.get("enabled", True) is not True:
            continue
        enabled_sensors.append(
            (
                sensor_folder_name(
                    str(sensor.get("sensor_type", "")),
                    str(sensor.get("device_id", "")),
                ),
                sensor,
            )
        )
    enabled_names = [name for name, _sensor in enabled_sensors]
    folders_by_name = (
        {
            path.name: path
            for path in input_root.iterdir()
            if path.is_dir() and path.name in set(enabled_names)
        }
        if input_root is not None
        else {}
    )
    sources: list[dict[str, Any]] = []
    for name, sensor in enabled_sensors:
        source_id = f"sensor:{name}"
        if timeline_id is not None and source_id != timeline_id:
            continue
        folder = folders_by_name.get(name)
        if folder is None:
            continue
        path = folder / MATCH_ROBOT_EE_POSES
        if path.is_file():
            sources.append(
                {
                    "id": source_id,
                    "label": name,
                    "source": path,
                    "kind": "synchronized",
                    "run_root": run_root,
                    "frame_directories": {
                        "rgb": folder / RGB_DIR,
                        "depth": folder / DEPTH_DIR,
                    },
                    "depth_scale_to_mm": _depth_scale_to_mm(folder),
                    "camera": {
                        "sensor_folder": name,
                        "sensor_type": str(sensor.get("sensor_type", "")),
                        "device_id": str(sensor.get("device_id", "")),
                        "display_name": str(sensor.get("display_name") or name),
                        "mounting_mode": str(sensor.get("mounting_mode", "")),
                        "inverted": sensor.get("inverted") is True,
                        "image_presentation": _image_presentation(sensor, folder),
                    },
                }
            )
    return sources


def _timeline_sources(
    run_root: Path, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    sources = _synchronized_source_contexts(run_root, config)
    if sources:
        for source in sources:
            source["poses"] = _matched_timeline(Path(source["source"]).parent)
        return sources
    enabled_names = [
        sensor_folder_name(
            str(sensor.get("sensor_type", "")),
            str(sensor.get("device_id", "")),
        )
        for sensor in config.get("capture", {}).get("sensors", [])
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is True
    ]
    raw_candidates = [run_root / RAW_ROBOT_EE_POSES]
    raw_candidates.extend(
        run_root / name / RAW_ROBOT_EE_POSES for name in enabled_names
    )
    raw = next((path for path in raw_candidates if path.is_file()), None)
    if raw is not None:
        return [
            {
                "id": "raw:robot",
                "label": "Raw robot poses",
                "source": raw,
                "kind": "raw",
                "poses": _raw_timeline(raw),
                "run_root": run_root,
                "frame_directories": {},
                "depth_scale_to_mm": None,
                "camera": None,
            }
        ]
    return []


def _camera_frame_path(
    source: Mapping[str, Any],
    frame_id: str,
    *,
    modality: str,
) -> Path:
    if modality not in {"rgb", "depth"}:
        raise ValueError("modality must be rgb or depth")
    raw_directories = source.get("frame_directories")
    raw_run_root = source.get("run_root")
    raw_directory = (
        raw_directories.get(modality) if isinstance(raw_directories, Mapping) else None
    )
    if not isinstance(raw_directory, Path) or not isinstance(raw_run_root, Path):
        raise FileNotFoundError(
            f"{modality.upper()} frames are not available for this timeline"
        )

    relative_frame = Path(frame_id)
    if (
        not frame_id
        or relative_frame.name != frame_id
        or relative_frame.suffix.lower() != ".png"
    ):
        raise ValueError("Camera frame identifiers must be plain PNG filenames")

    run_root = raw_run_root.resolve(strict=True)
    frame_directory = raw_directory.resolve(strict=True)
    frame_directory.relative_to(run_root)
    frame_path = (frame_directory / relative_frame).resolve(strict=True)
    frame_path.relative_to(frame_directory)
    if not frame_path.is_file():
        raise FileNotFoundError(f"Camera frame is not a file: {frame_id}")
    return frame_path


def _timeline_camera_frame_path(
    source: Mapping[str, Any],
    pose: Mapping[str, Any],
    *,
    modality: str,
) -> Path:
    return _camera_frame_path(
        source,
        str(pose.get("frame_id", "")),
        modality=modality,
    )


def _modality_metadata(source: Mapping[str, Any], modality: str) -> dict[str, Any]:
    directories = source.get("frame_directories")
    directory = directories.get(modality) if isinstance(directories, Mapping) else None
    available = False
    if (
        isinstance(directory, Path)
        and directory.is_dir()
        and (modality != "depth" or isinstance(source.get("depth_scale_to_mm"), float))
    ):
        for pose in source["poses"]:
            try:
                _timeline_camera_frame_path(source, pose, modality=modality)
            except (FileNotFoundError, OSError, ValueError):
                continue
            available = True
            break
    return {
        "available": available,
        "kind": modality,
        "media_type": "image/png",
        "source": directory.as_posix() if isinstance(directory, Path) else None,
    }


def _camera_frame_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    rgb = _modality_metadata(source, "rgb")
    depth = _modality_metadata(source, "depth")
    depth.update(
        {
            "depth_scale_to_mm": source.get("depth_scale_to_mm"),
            "visualization": "turbo_near_warm_fixed_range",
            "preview_min_depth_mm": DEPTH_PREVIEW_MIN_MM,
            "preview_max_depth_mm": DEPTH_PREVIEW_MAX_MM,
            "invalid_depth_value": 0,
        }
    )
    return {
        "available": rgb["available"] or depth["available"],
        "rgb": rgb,
        "depth": depth,
    }


def _timeline_metadata(source: Mapping[str, Any], *, default: bool) -> dict[str, Any]:
    poses = source["poses"]
    return {
        "id": source["id"],
        "label": source["label"],
        "kind": source["kind"],
        "frame_count": len(poses),
        "default": default,
        "exact": True,
        "interpolation": "none",
        "page_limit": MAX_TIMELINE_PAGE,
        "source": Path(source["source"]).as_posix(),
        "camera": dict(source["camera"])
        if isinstance(source.get("camera"), Mapping)
        else None,
        "camera_frames": _camera_frame_metadata(source),
    }


def _pose_payload(
    item: Mapping[str, Any],
    index: int,
    *,
    entity_to_robot_flange: np.ndarray | None = None,
) -> dict[str, Any]:
    matrix = item["matrix"]
    if entity_to_robot_flange is not None:
        matrix = pt.concat(entity_to_robot_flange, matrix)
    return {
        "index": index,
        "frame_index": item["frame_index"],
        "frame_id": item["frame_id"],
        "timestamp_ns": item["timestamp_ns"],
        "motion": item["motion"],
        "transform": _transform_dict(matrix, "template_base"),
    }


def _preview(
    poses: list[dict[str, Any]],
    *,
    entity_to_robot_flange: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    if len(poses) <= MAX_PREVIEW_POSES:
        indices = list(range(len(poses)))
    else:
        indices = sorted(
            {
                round(i * (len(poses) - 1) / (MAX_PREVIEW_POSES - 1))
                for i in range(MAX_PREVIEW_POSES)
            }
        )
    return [
        _pose_payload(
            poses[index],
            index,
            entity_to_robot_flange=entity_to_robot_flange,
        )
        for index in indices
    ]


def _profiles(
    run_root: Path,
    config: Mapping[str, Any],
    warnings: list[dict[str, str]],
) -> tuple[list[CalibrationProfile], Path]:
    value = config.get("calibration_profiles")
    if isinstance(value, str) and value:
        raw = Path(value)
        try:
            path = _resolve_profile_collection_path(run_root, raw)
        except ValueError as exc:
            warnings.append(
                {"code": "invalid_calibration_profiles", "message": str(exc)}
            )
            return [], raw
    else:
        path = (run_root / "calibration_profiles.json").resolve()
    if not path.is_file():
        warnings.append(
            {
                "code": "missing_calibration_profiles",
                "message": (
                    "No calibration profile collection is available at "
                    f"{path}; cameras remain unresolved."
                ),
            }
        )
        return [], path
    try:
        if (
            config.get("calibration_profile_selection") is not None
            or (run_root / CALIBRATION_PROFILE_SELECTION).exists()
        ):
            from posetestbot.calibration.profile_library import (
                verify_calibration_profile_selection,
            )

            verify_calibration_profile_selection(
                run_root,
                expected_calibration_profiles=path,
            )
        return load_profile_collection(path), path
    except (OSError, ValueError) as exc:
        warnings.append({"code": "invalid_calibration_profiles", "message": str(exc)})
        return [], path


def _resolve_profile_collection_path(run_root: Path, raw: Path) -> Path:
    """Resolve an explicit collection path without guessing between two files."""

    if raw.is_absolute():
        return raw.resolve()

    roots = (run_root.resolve(), Path.cwd().resolve())
    candidates: list[Path] = []
    for base in roots:
        candidate = (base / raw).resolve()
        if candidate == base or base not in candidate.parents:
            continue
        if candidate.is_file() and candidate not in candidates:
            candidates.append(candidate)

    if len(candidates) > 1:
        locations = ", ".join(path.as_posix() for path in candidates)
        raise ValueError(
            f"Ambiguous calibration profile path {raw.as_posix()!r}; "
            f"it resolves to multiple files: {locations}"
        )
    if candidates:
        return candidates[0]

    # Prefer the run-relative spelling for a useful missing-file diagnostic. The
    # cwd-relative spelling is considered above whenever it actually exists.
    return (run_root.resolve() / raw).resolve()


def _profile_for_sensor(
    profiles: list[CalibrationProfile],
    sensor: Mapping[str, Any],
    sensor_name: str,
) -> CalibrationProfile:
    mounting_mode = MountingMode(str(sensor.get("mounting_mode") or "eye_in_hand"))
    sensor_type = SensorType(str(sensor.get("sensor_type")))
    device_id = str(sensor.get("device_id", ""))
    exact_profiles = [
        profile
        for profile in profiles
        if profile.sensor_type == sensor_type and profile.sensor_id == device_id
    ]
    configured_profile_id = sensor.get("calibration_profile_id")
    if configured_profile_id:
        matches = [
            profile
            for profile in profiles
            if profile.profile_id == str(configured_profile_id)
        ]
        if not matches:
            raise KeyError(
                f"Configured profile {configured_profile_id!r} does not exist"
            )
        profile = matches[0]
        if profile.sensor_type != sensor_type or profile.sensor_id != device_id:
            raise KeyError(
                f"Configured profile {configured_profile_id!r} does not match "
                f"sensor identity {sensor_type.value}:{device_id}"
            )
        # Reuse the canonical identity/status matcher on the exact pinned profile.
        # This prevents a pin for another camera or mounting mode from being rendered.
        return select_valid_profile_for_sensor(
            [profile],
            sensor_name,
            mounting_mode=mounting_mode,
        )
    return select_valid_profile_for_sensor(
        exact_profiles,
        sensor_name,
        mounting_mode=mounting_mode,
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: Any) -> int | None:
    parsed = _optional_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _optional_calibration_transform(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        from_frame = str(value["from"])
        to_frame = str(value["to"])
        transform = _matrix(
            value["rotation_quaternion_wxyz"],
            value["translation_mm"],
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not from_frame or not to_frame:
        return None
    return {
        "from": from_frame,
        "to": to_frame,
        "matrix": np.asarray(transform, dtype=float).tolist(),
        "rotation_quaternion_wxyz": pr.quaternion_from_matrix(
            transform[:3, :3]
        ).tolist(),
        "translation_mm": transform[:3, 3].tolist(),
    }


def _calibration_evidence(
    profile: CalibrationProfile,
    transform: np.ndarray,
    profile_path: Path,
) -> dict[str, Any]:
    metadata = profile.metadata
    solver = metadata.get("promotion_solver_provenance")
    held_out = metadata.get("held_out_residuals")
    return {
        "profile_id": profile.profile_id,
        "schema_version": profile.schema_version,
        "status": profile.status.value,
        "mounting_mode": profile.mounting_mode.value,
        "rig_position": profile.rig_position,
        "extrinsics": {
            "from": profile.extrinsics.from_frame.value,
            "to": profile.extrinsics.to_frame.value,
            "matrix": np.asarray(transform, dtype=float).tolist(),
            "rotation_quaternion_wxyz": list(
                profile.extrinsics.rotation_quaternion_wxyz
            ),
            "translation_mm": list(profile.extrinsics.translation_mm),
        },
        "companion_transform": _optional_calibration_transform(
            metadata.get("companion_transform")
        ),
        "quality": {
            "num_observations": profile.quality.num_observations,
            "num_inliers": profile.quality.num_inliers,
            "mean_reprojection_error_px": _optional_float(
                profile.quality.mean_reprojection_error_px
            ),
            "max_reprojection_error_px": _optional_float(
                profile.quality.max_reprojection_error_px
            ),
            "residual_translation_mm": _optional_float(
                profile.quality.residual_translation_mm
            ),
            "residual_rotation_deg": _optional_float(
                profile.quality.residual_rotation_deg
            ),
            "outlier_count": _optional_int(metadata.get("outlier_count")),
            "outlier_ratio": _optional_float(metadata.get("outlier_ratio")),
            "held_out_residuals": dict(held_out)
            if isinstance(held_out, Mapping)
            else None,
            "notes": profile.quality.notes,
        },
        "evidence": {
            "profile_source": profile_path.as_posix(),
            "method": profile.method,
            "calibration_dataset_id": profile.calibration_dataset_id,
            "target_type": profile.target_type.value,
            "target_id": metadata.get("target_id"),
            "calibrated_at": profile.calibrated_at,
            "operator": profile.operator,
            "sync_delta_ms": _optional_float(profile.sync_delta_ms),
            "promotion_attempt_id": metadata.get("promotion_attempt_id"),
            "promotion_candidate_id": metadata.get("promotion_candidate_id"),
            "promotion_multi_camera_bundle_id": metadata.get(
                "promotion_multi_camera_bundle_id"
            ),
            "promotion_solver_provenance": (
                dict(solver) if isinstance(solver, Mapping) else None
            ),
            "promoted_at": metadata.get("promoted_at"),
            "promoted_by": metadata.get("promoted_by"),
            "intrinsic_profile_id": metadata.get("intrinsic_profile_id"),
        },
    }


def _bop_export_provenance(
    run_root: Path,
    warnings: list[dict[str, str]],
    dataset_mode: str,
    pose_template_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = run_root / BOP_DIR / BOP_EXPORT_MANIFEST
    provenance: dict[str, Any] = {
        "status": "not_exported",
        "manifest_path": path.as_posix(),
        "dataset_mode_matches": None,
    }
    if not path.is_file():
        return provenance
    try:
        manifest = _read_mapping(path)
        if pose_template_selection is not None:
            exported_template = manifest.get("pose_template")
            matches = (
                manifest.get("dataset_mode") == "pose_template"
                and isinstance(exported_template, Mapping)
                and exported_template.get("template_uuid")
                == pose_template_selection.get("template_uuid")
                and exported_template.get("bundle_sha256")
                == pose_template_selection.get("bundle_sha256")
            )
            provenance.update(
                {
                    "status": "current" if matches else "stale",
                    "manifest_schema_version": manifest.get("schema_version"),
                    "pose_template_matches": matches,
                    "template_uuid": pose_template_selection.get("template_uuid"),
                }
            )
            if not matches:
                warnings.append(
                    {
                        "code": "stale_bop_pose_template_provenance",
                        "message": "The BOP export does not match the selected immutable pose template.",
                    }
                )
            return provenance
        matches = manifest.get("dataset_mode") == dataset_mode == "objectless"
        provenance.update(
            {
                "status": "current" if matches else "stale",
                "manifest_schema_version": manifest.get("schema_version"),
                "dataset_mode_matches": matches,
            }
        )
        if provenance["status"] == "stale":
            warnings.append(
                {
                    "code": "stale_bop_export_provenance",
                    "message": "The BOP export dataset mode does not match this objectless run.",
                }
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        provenance.update({"status": "invalid", "error": str(exc)})
        warnings.append(
            {
                "code": "invalid_bop_export_provenance",
                "message": f"Cannot validate BOP export provenance: {exc}",
            }
        )
    return provenance


def _sensor_key(sensor: Mapping[str, Any]) -> str:
    family = str(sensor.get("sensor_type", "sensor"))
    if family == "realsense_d435":
        family = "realsense"
    elif family == "oak_d_pro":
        family = "luxonis"
    elif family == "zed_2i":
        family = "zed_2i"
    return f"{family}_{sensor.get('device_id', 'unknown')}"


def _configured_fixed_frames(config: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("from"))
        for item in config.get("frames", {}).get("fixed_transforms", [])
        if isinstance(item, Mapping)
    }


def _calibration_attempt_target(run_root: Path) -> dict[str, Any] | None:
    """Return the newest run-local attempt target without treating it as promoted."""

    attempts_root = run_root / CALIBRATION_ATTEMPT_DIRECTORY
    if not attempts_root.is_dir():
        return None
    candidates: list[dict[str, Any]] = []
    for attempt_root in attempts_root.iterdir():
        request_path = attempt_root / "request.json"
        progress_path = attempt_root / "progress.json"
        target_path = attempt_root / "target_bundle" / CALIBRATION_TARGET
        if not (
            attempt_root.is_dir()
            and request_path.is_file()
            and progress_path.is_file()
            and target_path.is_file()
        ):
            continue
        try:
            request_value = _read_mapping(request_path)
            progress = _read_mapping(progress_path)
            target = _read_mapping(target_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if request_value.get("attempt_id") != attempt_root.name:
            continue
        candidates.append(
            {
                "sort_key": str(request_value.get("created_at") or ""),
                "target": target,
                "target_path": target_path,
                "pdf_path": attempt_root / "target_bundle" / "calibration_target.pdf",
                "mounting_frame": request_value.get("target_mounting", {}).get("to"),
                "provenance": {
                    "source": target_path.as_posix(),
                    "selection_source": "latest_run_calibration_attempt",
                    "attempt_id": attempt_root.name,
                    "attempt_status": progress.get("status"),
                    "placement_state": request_value.get("target_mounting", {}).get(
                        "state"
                    ),
                },
            }
        )
    return max(candidates, key=lambda item: item["sort_key"], default=None)


def _calibration_target_context(
    run_root: Path, config: Mapping[str, Any]
) -> dict[str, Any] | None:
    target_path = run_root / CALIBRATION_TARGET
    configured = config.get("calibration_target")
    if target_path.is_file():
        target = _read_mapping(target_path)
        pdf_path = None
        provenance: dict[str, Any] = {
            "source": target_path.as_posix(),
            "selection_source": "promoted_run_artifact",
        }
        placement = target.get("placement")
        mounting_frame = None
        if isinstance(configured, Mapping):
            bundle_path = Path(str(configured.get("bundle_path", "")))
            if not bundle_path.is_absolute() and ".." not in bundle_path.parts:
                candidate = run_root / bundle_path / "calibration_target.pdf"
                if candidate.is_file():
                    pdf_path = candidate
            configured_placement = configured.get("placement")
            if isinstance(configured_placement, Mapping):
                mounting_frame = configured_placement.get("mounting_frame")
            if not isinstance(placement, Mapping) and isinstance(
                configured_placement, Mapping
            ):
                transform = configured_placement.get("transform")
                if isinstance(transform, Mapping):
                    placement = transform
            provenance["target_id"] = configured.get("target_id")
        return {
            "target": target,
            "target_path": target_path,
            "pdf_path": pdf_path,
            "placement": placement,
            "mounting_frame": mounting_frame,
            "provenance": provenance,
        }
    return _calibration_attempt_target(run_root)


def cell_calibration_target_pdf_path(run_root: str | Path) -> Path:
    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    context = _calibration_target_context(root, config)
    if context is None or not isinstance(context.get("pdf_path"), Path):
        raise FileNotFoundError("No calibration-target PDF is available for this run")
    path = context["pdf_path"]
    if not path.is_file():
        raise FileNotFoundError("No calibration-target PDF is available for this run")
    path.resolve(strict=True).relative_to(root.resolve())
    return path


def _profile_target_placement(
    profiles: list[CalibrationProfile], target: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    target_id = target.get("target_id")
    for profile in sorted(profiles, key=lambda item: item.profile_id):
        if target_id and profile.metadata.get("target_id") != target_id:
            continue
        companion = profile.metadata.get("companion_transform")
        if not isinstance(companion, Mapping):
            continue
        if companion.get("from") != "aruco_grid":
            continue
        return companion, {
            "placement_source": "promoted_calibration_profile_companion",
            "placement_profile_id": profile.profile_id,
        }
    return None, None


def _target_geometry(
    target: Mapping[str, Any], *, pdf_url: str | None
) -> dict[str, Any]:
    board = target.get("posegridgen", {}).get("configuration", {}).get("board", {})
    marker_length = target.get("marker_length") or board.get("marker_size_mm")
    marker_separation = target.get("marker_separation") or board.get("separation_mm")
    markers = target.get("markers")
    frame = _canonical_grid_frame(target)
    return {
        "kind": "calibration_target",
        "target_type": target.get("target_type"),
        "target_id": target.get("target_id"),
        "display_name": target.get("display_name"),
        "geometry_sha256": target.get("geometry_sha256"),
        "frame": frame,
        "target_bounds": target.get("target_bounds"),
        "grid_size": target.get("grid_size"),
        "marker_length_mm": marker_length,
        "marker_separation_mm": marker_separation,
        "square_length_mm": target.get("square_length"),
        "markers": list(markers) if isinstance(markers, list) else [],
        "pdf_url": pdf_url,
    }


def _pose_template_footprint(
    run_root: Path, selection: Mapping[str, Any]
) -> dict[str, Any]:
    snapshot = run_root / str(selection["bundle_snapshot"])
    bundle = _read_mapping(snapshot / POSE_TEMPLATE_BUNDLE)
    preview = _read_mapping(snapshot / POSE_TEMPLATE_PREVIEW)
    configuration = bundle.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Pose-template bundle has no printable configuration")
    page = preview.get("page")
    instances = preview.get("instances")
    if not isinstance(page, Mapping) or not isinstance(instances, list):
        raise ValueError("Pose-template bundle has no exact printable preview")
    contours = []
    for item in instances:
        if not isinstance(item, Mapping):
            continue
        item_contours = item.get("compensated_contours")
        if not isinstance(item_contours, list):
            continue
        contours.append(
            {
                "instance_uuid": item.get("instance_uuid"),
                "contours": item_contours,
            }
        )
    return {
        "kind": "pose_template_footprint",
        "display_name": bundle.get("display_name"),
        "page": dict(page),
        "page_configuration": dict(configuration.get("page", {})),
        "contours": contours,
        "template_uuid": selection.get("template_uuid"),
        "bundle_sha256": selection.get("bundle_sha256"),
    }


def build_cell_scene(run_root: str | Path) -> dict[str, Any]:
    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    warnings: list[dict[str, str]] = []
    manager = TransformManager()
    presentation = _reference_presentation("template_base")
    trajectory_entity_id = "robot_flange"
    trajectory_label = "Robot flange"
    trajectory_derivation = "recorded_robot_flange_to_template_base"
    trajectory_entity_to_robot_flange: np.ndarray | None = None
    fixed_sources: dict[str, Mapping[str, Any]] = {}
    for edge in config.get("frames", {}).get("fixed_transforms", []):
        try:
            matrix = _matrix(edge["rotation_quaternion_wxyz"], edge["translation_mm"])
            manager.add_transform(str(edge["from"]), str(edge["to"]), matrix)
            fixed_sources[str(edge["from"])] = edge
        except (KeyError, ValueError) as exc:
            warnings.append({"code": "invalid_fixed_transform", "message": str(exc)})

    timelines = _timeline_sources(root, config)
    first_pose = (
        timelines[0]["poses"][0]["matrix"]
        if timelines and timelines[0]["poses"]
        else None
    )
    entities = [
        _entity(
            "template_base",
            "reference_frame",
            "PoseTemplateBase",
            transform=_identity(None),
            status="planned",
            provenance={"source": "run_config.frames.dataset_reference_frame"},
            geometry={"kind": "axes", "size_mm": 100},
        ),
    ]

    configured_fixed_frames = _configured_fixed_frames(config)
    for frame, label, kind in (
        ("physical_robot_base", "Physical robot base", "robot_base"),
        ("tcp", "Robot TCP", "tcp"),
    ):
        parent = "robot_flange" if frame == "tcp" else "template_base"
        try:
            transform = manager.get_transform(frame, parent)
        except KeyError:
            entities.append(
                _entity(
                    frame,
                    kind,
                    label,
                    transform=None,
                    status=(
                        "unresolved"
                        if frame in configured_fixed_frames
                        else "not_configured"
                    ),
                    reason=(
                        f"No fixed transform resolves {frame} to {parent}"
                        if frame in configured_fixed_frames
                        else None
                    ),
                    provenance={"source": "run_config.frames.fixed_transforms"},
                    geometry={"kind": kind},
                )
            )
        else:
            entities.append(
                _entity(
                    frame,
                    kind,
                    label,
                    transform=_transform_dict(transform, parent),
                    status="planned",
                    provenance={
                        "source": fixed_sources.get(frame, {}).get(
                            "source", "run_config.frames.fixed_transforms"
                        )
                    },
                    geometry={"kind": kind},
                )
            )

    if first_pose is not None:
        manager.add_transform("robot_flange", "template_base", first_pose)
        first_pose = manager.get_transform("robot_flange", "template_base")
    entities.append(
        _entity(
            "robot_flange",
            "robot_flange",
            "Robot flange",
            transform=_transform_dict(first_pose, "template_base")
            if first_pose is not None
            else None,
            status="recorded" if first_pose is not None else "unresolved",
            reason=None
            if first_pose is not None
            else "No synchronized or raw flange pose timeline is available",
            provenance={
                "source": Path(timelines[0]["source"]).as_posix() if timelines else None
            },
            geometry={"kind": "flange_proxy"},
        )
    )

    profiles, profile_path = _profiles(root, config, warnings)
    enabled_sensors = [
        sensor
        for sensor in config.get("capture", {}).get("sensors", [])
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is True
    ]
    resolved_profiles: dict[str, CalibrationProfile] = {}
    profile_errors: dict[str, str] = {}
    for sensor in enabled_sensors:
        key = _sensor_key(sensor)
        try:
            resolved_profiles[key] = _profile_for_sensor(profiles, sensor, key)
        except (KeyError, ValueError) as exc:
            profile_errors[key] = str(exc)

    matched_pose_paths = {
        str(camera["sensor_folder"]): Path(source["source"])
        for source in timelines
        if source.get("kind") == "synchronized"
        and isinstance((camera := source.get("camera")), Mapping)
        and isinstance(camera.get("sensor_folder"), str)
    }
    reference_error: str | None = None
    try:
        verify_static_profile_destination_reference(
            root,
            config,
            resolved_profiles.values(),
            matched_robot_pose_paths_by_sensor_name=matched_pose_paths,
        )
    except ValueError as exc:
        reference_error = str(exc)
        warnings.append(
            {
                "code": "invalid_calibration_world_reference",
                "message": reference_error,
            }
        )

    selected_profiles: list[CalibrationProfile] = []
    for sensor in enabled_sensors:
        key = _sensor_key(sensor)
        label = str(sensor.get("display_name") or key)
        try:
            if key in profile_errors:
                raise ValueError(profile_errors[key])
            if reference_error is not None:
                raise ValueError(reference_error)
            profile = resolved_profiles[key]
            selected_profiles.append(profile)
            parent = (
                "robot_flange"
                if profile.mounting_mode.value == "eye_in_hand"
                else "template_base"
            )
            transform = _matrix(
                profile.extrinsics.rotation_quaternion_wxyz,
                profile.extrinsics.translation_mm,
            )
            manager.add_transform(f"camera:{key}", parent, transform)
            resolved = manager.get_transform(f"camera:{key}", parent)
            intrinsics = profile.rectified_intrinsics or profile.intrinsics
            geometry = {
                "kind": "camera_frustum",
                "width": intrinsics.width,
                "height": intrinsics.height,
                "fx": intrinsics.cam_k[0],
                "fy": intrinsics.cam_k[4],
                "cx": intrinsics.cam_k[2],
                "cy": intrinsics.cam_k[5],
                "depth_mm": 180,
            }
            entities.append(
                _entity(
                    f"camera:{key}",
                    "camera",
                    label,
                    transform=_transform_dict(resolved, parent),
                    status="planned",
                    provenance={
                        "source": profile_path.as_posix(),
                        "profile_id": profile.profile_id,
                        "schema_version": profile.schema_version,
                    },
                    geometry=geometry,
                    calibration=_calibration_evidence(
                        profile,
                        resolved,
                        profile_path,
                    ),
                )
            )
        except (KeyError, ValueError) as exc:
            entities.append(
                _entity(
                    f"camera:{key}",
                    "camera",
                    label,
                    transform=None,
                    status="unresolved",
                    reason=f"No valid calibration profile: {exc}",
                    provenance={"source": profile_path.as_posix()},
                    geometry={"kind": "camera_frustum"},
                )
            )

    encoded_root = quote(root.as_posix(), safe="")
    pose_selection = None
    has_context_surface = False
    if config.get("dataset_mode") == "pose_template":
        try:
            pose_selection = load_pose_template_selection(root)
            template_provenance = {
                "schema_version": "pose_template_selection.v1",
                "template_uuid": pose_selection["template_uuid"],
                "bundle_sha256": pose_selection["bundle_sha256"],
                "instance_count": len(pose_selection["instances"]),
            }
            footprint = _pose_template_footprint(root, pose_selection)
            footprint_transform = np.asarray(
                pose_selection["template_base_from_pose_template"]["matrix"],
                dtype=float,
            )
            entities.append(
                _entity(
                    "pose_template_footprint",
                    "template",
                    str(footprint.get("display_name") or "Object footprint template"),
                    transform=_transform_dict(footprint_transform, "template_base"),
                    status="planned",
                    provenance={
                        **template_provenance,
                        "source": (
                            root
                            / str(pose_selection["bundle_snapshot"])
                            / POSE_TEMPLATE_PREVIEW
                        ).as_posix(),
                        "placement_confirmed": pose_selection.get(
                            "placement_confirmed"
                        ),
                    },
                    geometry=footprint,
                )
            )
            has_context_surface = True
            for item in pose_selection["instances"]:
                instance_uuid = item["instance_uuid"]
                transform = np.asarray(
                    item["template_base_from_object"]["matrix"], dtype=float
                )
                geometry = {
                    "kind": "mesh",
                    "obj_id": item["obj_id"],
                    "mesh_url": f"/ui/cell-pose-template-assets/{instance_uuid}/mesh?run_root={encoded_root}",
                    "texture_url": (
                        f"/ui/cell-pose-template-assets/{instance_uuid}/texture?run_root={encoded_root}"
                        if "texture" in item["assets"]
                        else None
                    ),
                }
                entities.append(
                    _entity(
                        f"object:{instance_uuid}",
                        "object",
                        item["name"],
                        transform=_transform_dict(transform, "template_base"),
                        status="planned",
                        provenance={
                            "instance_uuid": instance_uuid,
                            "catalog_uuid": item["catalog_uuid"],
                            "obj_id": item["obj_id"],
                            **template_provenance,
                        },
                        geometry=geometry,
                    )
                )
        except (OSError, ValueError) as exc:
            template_provenance = {"schema_version": "pose_template_selection.v1"}
            warnings.append(
                {"code": "invalid_pose_template_selection", "message": str(exc)}
            )
    else:
        template_provenance = None

    try:
        target_context = _calibration_target_context(root, config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        target_context = None
        entities.append(
            _entity(
                "calibration_target",
                "calibration_target",
                "Calibration target",
                transform=None,
                status="unresolved",
                reason=str(exc),
                provenance={"source": (root / CALIBRATION_TARGET).as_posix()},
                geometry={"kind": "calibration_target"},
            )
        )
    if target_context is not None:
        try:
            target = target_context["target"]
            placement = target_context.get("placement")
            placement_provenance = None
            if not isinstance(placement, Mapping):
                placement, placement_provenance = _profile_target_placement(
                    selected_profiles, target
                )
            placement_known = isinstance(placement, Mapping)
            if not placement_known:
                placement = {
                    "rotation_quaternion_wxyz": [1, 0, 0, 0],
                    "translation_mm": [0, 0, 0],
                    "to": "template_base",
                }
            matrix = _matrix(
                placement["rotation_quaternion_wxyz"], placement["translation_mm"]
            )
            parent = str(placement.get("to", "template_base"))
            moving_target_intent = (
                parent == "robot_flange"
                or target_context.get("mounting_frame") == "robot_flange"
            )
            moving_target_resolved = placement_known and parent == "robot_flange"
            manager.add_transform("calibration_target", parent, matrix)
            matrix = manager.get_transform("calibration_target", parent)
            target_frame = _canonical_grid_frame(target)
            if target_frame is not None and not moving_target_intent:
                try:
                    target_to_reference = manager.get_transform(
                        "calibration_target", "template_base"
                    )
                except KeyError:
                    warnings.append(
                        {
                            "code": "unresolved_target_presentation",
                            "message": (
                                "Calibration target cannot be resolved to template_base; "
                                "the Cell view retained its reference Z-up presentation."
                            ),
                        }
                    )
                else:
                    presentation = _target_front_presentation(
                        target_to_reference,
                        reference_frame="template_base",
                        target_frame=target_frame,
                    )
            if moving_target_resolved:
                trajectory_entity_id = "calibration_target"
                trajectory_label = "Calibration target"
                trajectory_derivation = (
                    "promoted_calibration_target_to_robot_flange_composed_with_"
                    "recorded_robot_flange_to_template_base"
                )
                trajectory_entity_to_robot_flange = matrix
            pdf_path = target_context.get("pdf_path")
            geometry = _target_geometry(
                target,
                pdf_url=(
                    f"/ui/cell-calibration-target-pdf?run_root={encoded_root}"
                    if isinstance(pdf_path, Path) and pdf_path.is_file()
                    else None
                ),
            )
            geometry["placement_known"] = placement_known
            label = str(target.get("display_name") or "Calibration target")
            if not placement_known:
                label += " (reference placement)"
            entities.append(
                _entity(
                    "calibration_target",
                    "calibration_target",
                    label,
                    transform=_transform_dict(matrix, parent),
                    status="planned" if placement_known else "reference",
                    provenance={
                        **target_context["provenance"],
                        **(placement_provenance or {}),
                        "placement_known": placement_known,
                    },
                    geometry=geometry,
                )
            )
            has_context_surface = True
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            entities.append(
                _entity(
                    "calibration_target",
                    "calibration_target",
                    "Calibration target",
                    transform=None,
                    status="unresolved",
                    reason=str(exc),
                    provenance=dict(target_context["provenance"]),
                    geometry={"kind": "calibration_target"},
                )
            )

    if not has_context_surface:
        entities.append(
            _entity(
                "hri_template",
                "template",
                "HRI cell template",
                transform=_identity("template_base"),
                status="reference",
                provenance={"source": "packaged_hri_template"},
                geometry={
                    "kind": "svg_plane",
                    "width_mm": 420,
                    "height_mm": 297,
                    "asset_url": "/assets/cell/template_HRI_LBR_all_center_v2.svg",
                    "mapping": "center=template_base;right=+X;down=+Y",
                },
            )
        )

    timeline_meta = [
        _timeline_metadata(item, default=index == 0)
        for index, item in enumerate(timelines)
    ]
    bop_export_provenance = _bop_export_provenance(
        root, warnings, str(config.get("dataset_mode", "objectless")), pose_selection
    )
    return {
        "schema_version": SCENE_SCHEMA_VERSION,
        "coordinate_system": {
            "units": "millimetres",
            "handedness": "right",
            "up_axis": (
                presentation["source_front_axis"]
                if presentation["mode"] == "calibration_target_front"
                else "+Z"
            ),
            "reference_frame": "template_base",
            "reference_frame_label": "PoseTemplateBase",
            "sunrise_reference_frame_path": config.get("frames", {})
            .get("robot_pose", {})
            .get("sunrise_reference_frame_path"),
            "transform_semantics": "entity_to_parent",
            "presentation": presentation,
        },
        "run_root": root.as_posix(),
        "entities": entities,
        "warnings": warnings,
        "timelines": timeline_meta,
        "default_timeline_id": timeline_meta[0]["id"] if timeline_meta else None,
        "trajectory": {
            "entity_id": trajectory_entity_id,
            "label": trajectory_label,
            "reference_frame": "template_base",
            "reference_frame_label": "PoseTemplateBase",
            "source_timeline_id": timeline_meta[0]["id"] if timeline_meta else None,
            "derivation": trajectory_derivation,
        },
        "trajectory_preview": (
            _preview(
                timelines[0]["poses"],
                entity_to_robot_flange=trajectory_entity_to_robot_flange,
            )
            if timelines
            else []
        ),
        "object_selection": {
            "objectless": config.get("dataset_mode") == "objectless",
            "dataset_mode": config.get("dataset_mode", "objectless"),
            "instance_count": len(pose_selection["instances"])
            if pose_selection is not None
            else 0,
            "pose_template": template_provenance
            if pose_selection is not None
            else None,
            "bop_export": bop_export_provenance,
        },
    }


def cell_timeline_page(
    run_root: str | Path,
    timeline_id: str,
    *,
    offset: int = 0,
    limit: int = MAX_TIMELINE_PAGE,
) -> dict[str, Any]:
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, MAX_TIMELINE_PAGE)
    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    sources = {source["id"]: source for source in _timeline_sources(root, config)}
    if timeline_id not in sources:
        raise KeyError(f"Unknown timeline_id: {timeline_id}")
    source = sources[timeline_id]
    poses = source["poses"]
    page = poses[offset : offset + limit]
    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "timeline": _timeline_metadata(source, default=False),
        "offset": offset,
        "limit": limit,
        "total": len(poses),
        "next_offset": offset + len(page) if offset + len(page) < len(poses) else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "poses": [
            _pose_payload(item, offset + index) for index, item in enumerate(page)
        ],
    }


def _resolve_camera_frame(
    run_root: str | Path,
    timeline_id: str,
    *,
    timeline_index: int | None,
    frame_id: str | None,
    modality: str = "rgb",
) -> tuple[Mapping[str, Any], Path]:
    if timeline_index is None and frame_id is None:
        raise ValueError("timeline_index or frame_id is required")
    if timeline_index is not None and timeline_index < 0:
        raise ValueError("timeline_index must be greater than or equal to 0")
    if modality not in {"rgb", "depth"}:
        raise ValueError("modality must be rgb or depth")
    root = Path(run_root)
    config = load_run_config_for_run_root(root)

    if frame_id is not None:
        sources = {
            source["id"]: source
            for source in _synchronized_source_contexts(
                root,
                config,
                timeline_id=timeline_id,
            )
        }
        if timeline_id not in sources:
            raise KeyError(f"Unknown timeline_id: {timeline_id}")
        source = sources[timeline_id]
        try:
            return source, _camera_frame_path(
                source,
                frame_id,
                modality=modality,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"{modality.upper()} frame is unavailable: {frame_id}"
            ) from exc

    sources = {source["id"]: source for source in _timeline_sources(root, config)}
    if timeline_id not in sources:
        raise KeyError(f"Unknown timeline_id: {timeline_id}")
    source = sources[timeline_id]
    poses = source["poses"]
    assert timeline_index is not None
    if timeline_index >= len(poses):
        raise KeyError(f"Unknown timeline_index: {timeline_index}")
    try:
        return (
            source,
            _timeline_camera_frame_path(
                source,
                poses[timeline_index],
                modality=modality,
            ),
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{modality.upper()} frame is unavailable at timeline index "
            f"{timeline_index}"
        ) from exc


def cell_camera_frame_path(
    run_root: str | Path,
    timeline_id: str,
    timeline_index: int | None = None,
    *,
    frame_id: str | None = None,
    modality: str = "rgb",
) -> Path:
    _source, path = _resolve_camera_frame(
        run_root,
        timeline_id,
        timeline_index=timeline_index,
        frame_id=frame_id,
        modality=modality,
    )
    return path


def cell_depth_frame_preview_png(
    run_root: str | Path,
    timeline_id: str,
    timeline_index: int | None = None,
    *,
    frame_id: str | None = None,
) -> bytes:
    source, path = _resolve_camera_frame(
        run_root,
        timeline_id,
        timeline_index=timeline_index,
        frame_id=frame_id,
        modality="depth",
    )
    scale_to_mm = source.get("depth_scale_to_mm")
    if not isinstance(scale_to_mm, float):
        raise FileNotFoundError("Depth scale is unavailable for this camera timeline")
    depth = cv2.imread(path.as_posix(), cv2.IMREAD_UNCHANGED)
    if depth is None or depth.dtype != np.uint16 or depth.ndim != 2:
        raise ValueError(f"Depth frame must be a single-channel uint16 PNG: {path}")

    depth_mm = depth.astype(np.float32) * scale_to_mm
    valid = depth > 0
    normalized = np.clip(
        (depth_mm - DEPTH_PREVIEW_MIN_MM)
        / (DEPTH_PREVIEW_MAX_MM - DEPTH_PREVIEW_MIN_MM),
        0.0,
        1.0,
    )
    # Near points are warm and far points are cool; zero-depth pixels stay black.
    color_index = np.rint((1.0 - normalized) * 255.0).astype(np.uint8)
    preview = cv2.applyColorMap(color_index, cv2.COLORMAP_TURBO)
    preview[~valid] = 0
    encoded, png = cv2.imencode(".png", preview)
    if not encoded:
        raise OSError(f"Failed to encode depth preview: {path}")
    return png.tobytes()
