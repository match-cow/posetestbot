"""Transactional non-destructive RGB/aligned-depth rectification."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from posetestbot.calibration.intrinsics import (
    select_intrinsic_profile,
    sensor_intrinsic_identity,
)
from posetestbot.io.atomic import (
    atomic_write_json,
    atomic_write_text,
    replace_directory,
)
from posetestbot.io.artifacts import (
    CAMERA_DATA_JSON,
    CAMERA_RECTIFICATION_REPORT,
    CAM_K,
    DEPTH_DIR,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
    MATCH_ROBOT_EE_POSES,
    PROCESSED_DIR,
    RGB_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.pipeline.sensor_selection import enabled_sensor_folder_names
from posetestbot.sensors.frame_writer import validate_rgbd_images
from posetestbot.sync.non_destructive import load_frame_metadata


SCHEMA_VERSION = "camera_rectification.v1"
PROVENANCE_SCHEMA_VERSION = "rectification_provenance.v2"
FINGERPRINT_SCHEMA_VERSION = "rgbd_camera_artifact_fingerprint.v1"
RECTIFICATION_PROVENANCE = "rectification_provenance.json"
RECTIFIED_DIR = "rectified"
_FINGERPRINT_SIDECARS = (
    CAM_K,
    DEPTH_SCALE,
    CAMERA_DATA_JSON,
    FRAME_METADATA_JSONL,
    MATCH_ROBOT_EE_POSES,
)


def _require_regular_fingerprint_sidecars(sensor_folder: Path) -> None:
    for name in _FINGERPRINT_SIDECARS:
        path = sensor_folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Camera artifact must be a regular file: {path}")


def _fingerprint_file(sensor_folder: Path, relative_path: Path) -> tuple[int, str]:
    path = sensor_folder / relative_path
    resolved_sensor = sensor_folder.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_sensor)
    except ValueError as exc:
        raise ValueError(f"Camera artifact escapes its sensor folder: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Camera artifact must be a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def rgbd_camera_artifact_fingerprint(
    sensor_folder: str | Path,
) -> dict[str, Any]:
    """Fingerprint RGB-D pixels and camera sidecars used by later consumers.

    The compact aggregate requires the current camera, frame-metadata, and
    matched-pose sidecars while deliberately excluding derived render outputs
    and the provenance file itself. It therefore remains stable when
    BlenderProc adds masks/GT later while still detecting changed pixels, frame
    membership, timestamps, robot-pose matches, intrinsics, or depth scale.
    """

    sensor = Path(sensor_folder)
    if sensor.is_symlink() or not sensor.is_dir():
        raise ValueError(f"Sensor folder must be a regular directory: {sensor}")
    rgb_dir = sensor / RGB_DIR
    depth_dir = sensor / DEPTH_DIR
    if rgb_dir.is_symlink() or depth_dir.is_symlink():
        raise ValueError(f"RGB/depth directories must not be symlinks: {sensor}")
    _require_regular_fingerprint_sidecars(sensor)
    pairs = _pairs(sensor)
    relative_paths = [
        relative
        for rgb_path, depth_path in pairs
        for relative in (
            rgb_path.relative_to(sensor),
            depth_path.relative_to(sensor),
        )
    ]
    relative_paths.extend(Path(name) for name in _FINGERPRINT_SIDECARS)
    aggregate = hashlib.sha256()
    total_size = 0
    for relative in sorted(relative_paths, key=lambda item: item.as_posix()):
        size, digest = _fingerprint_file(sensor, relative)
        total_size += size
        aggregate.update(
            json.dumps(
                {
                    "path": relative.as_posix(),
                    "size_bytes": size,
                    "sha256": digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        aggregate.update(b"\n")
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "algorithm": "sha256",
        "contract": "rgb_depth_png_and_camera_sidecars",
        "digest": aggregate.hexdigest(),
        "file_count": len(relative_paths),
        "frame_pair_count": len(pairs),
        "total_size_bytes": total_size,
    }


def validate_rectification_provenance(
    source_sensor: str | Path,
    rectified_sensor: str | Path,
) -> dict[str, Any]:
    """Prove a rectified sensor is current for one exact source sensor."""

    source = Path(source_sensor)
    output = Path(rectified_sensor)
    provenance_path = output / RECTIFICATION_PROVENANCE
    if provenance_path.is_symlink() or not provenance_path.is_file():
        raise FileNotFoundError(
            f"Rectification provenance does not exist: {provenance_path}"
        )
    try:
        value = json.loads(provenance_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid rectification provenance JSON: {provenance_path}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError("Rectification provenance must be a JSON object")
    if value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ValueError(
            "Rectification provenance schema_version must be "
            f"{PROVENANCE_SCHEMA_VERSION}"
        )
    if value.get("projection") != "rectified_alpha0":
        raise ValueError("Rectification provenance projection is unsupported")
    recorded_source = Path(str(value.get("source_sensor_folder") or ""))
    recorded_output = Path(str(value.get("output_sensor_folder") or ""))
    if (
        not recorded_source.is_absolute()
        or recorded_source.resolve() != source.resolve()
    ):
        raise ValueError(
            "Rectification provenance source_sensor_folder does not match the "
            "current synchronized sensor"
        )
    if (
        not recorded_output.is_absolute()
        or recorded_output.resolve() != output.resolve()
    ):
        raise ValueError(
            "Rectification provenance output_sensor_folder does not match the "
            "current rectified sensor"
        )
    source_fingerprint = rgbd_camera_artifact_fingerprint(source)
    output_fingerprint = rgbd_camera_artifact_fingerprint(output)
    if value.get("source_fingerprint") != source_fingerprint:
        raise ValueError(
            "Rectification provenance source fingerprint is stale or mismatched"
        )
    if value.get("output_fingerprint") != output_fingerprint:
        raise ValueError(
            "Rectification provenance output fingerprint is stale or mismatched"
        )
    if value.get("frame_count") != output_fingerprint["frame_pair_count"]:
        raise ValueError("Rectification provenance frame_count is inconsistent")
    return value


def _pairs(sensor_folder: Path) -> list[tuple[Path, Path]]:
    rgb = {path.name: path for path in (sensor_folder / RGB_DIR).glob("*.png")}
    depth = {path.name: path for path in (sensor_folder / DEPTH_DIR).glob("*.png")}
    if not rgb or set(rgb) != set(depth):
        raise ValueError(
            f"RGB/depth filenames must be non-empty and identical: {sensor_folder}"
        )
    return [(rgb[name], depth[name]) for name in sorted(rgb)]


def _read_validated_rgbd_pair(
    rgb_path: Path,
    depth_path: Path,
    *,
    expected_image_size: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    rgb = cv2.imread(rgb_path.as_posix(), cv2.IMREAD_UNCHANGED)
    depth = cv2.imread(depth_path.as_posix(), cv2.IMREAD_UNCHANGED)
    if rgb is None or depth is None:
        raise ValueError(f"Unreadable RGB-D frame pair: {rgb_path.name}")
    try:
        rgb, depth = validate_rgbd_images(rgb, depth)
    except ValueError as exc:
        raise ValueError(
            f"Invalid RGB-D pixel contract for {rgb_path.name}: {exc}"
        ) from exc
    actual_image_size = (int(rgb.shape[1]), int(rgb.shape[0]))
    if actual_image_size != expected_image_size:
        raise ValueError(
            "RGB-D dimensions do not match intrinsic profile: "
            f"{rgb_path.name}; actual={actual_image_size}, "
            f"expected={expected_image_size}"
        )
    return rgb, depth


def _maps(profile: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    native = profile["native"]
    rectified = profile.get("rectified")
    if not isinstance(rectified, Mapping):
        raise ValueError(
            "Intrinsic profile has no OpenCV-compatible rectified projection"
        )
    image_size = tuple(int(item) for item in profile["resolution"])
    native_k = np.asarray(native["cam_K"], dtype=float).reshape(3, 3)
    distortion = np.asarray(native["distortion"], dtype=float).reshape(5)
    rectified_k = np.asarray(rectified["cam_K"], dtype=float).reshape(3, 3)
    map_x, map_y = cv2.initUndistortRectifyMap(
        native_k,
        distortion,
        None,
        rectified_k,
        image_size,
        cv2.CV_32FC1,
    )
    return map_x, map_y, rectified_k


def _plain_png_filename(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or Path(value).name != value
        or Path(value).suffix.lower() != ".png"
        or not Path(value).stem
    ):
        raise ValueError(f"{label} must be a plain PNG filename")
    return value


def _validated_frame_metadata(
    sensor_folder: Path,
    pairs: Sequence[tuple[Path, Path]],
    *,
    expected_sensor_id: str,
    expected_orientation: str,
) -> list[dict[str, Any]]:
    records = load_frame_metadata(sensor_folder)
    expected_frames = {rgb_path.name for rgb_path, _depth_path in pairs}
    metadata_frames: set[str] = set()
    sensor_type: str | None = None
    for index, record in enumerate(records):
        label = f"Frame metadata record {index}"
        frame_id = _plain_png_filename(
            record.get("frame_id"), label=f"{label} frame_id"
        )
        if record.get("rgb_path") != f"{RGB_DIR}/{frame_id}":
            raise ValueError(f"{label} rgb_path must be {RGB_DIR}/{frame_id}")
        if record.get("depth_path") != f"{DEPTH_DIR}/{frame_id}":
            raise ValueError(f"{label} depth_path must be {DEPTH_DIR}/{frame_id}")
        if record.get("sensor_id") != expected_sensor_id:
            raise ValueError(
                f"{label} sensor_id does not match the synchronized sensor"
            )
        orientation = str(record.get("orientation") or "normal")
        if orientation != expected_orientation:
            raise ValueError(
                f"{label} orientation does not match the synchronized sensor"
            )
        current_sensor_type = str(record["sensor_type"])
        if sensor_type is None:
            sensor_type = current_sensor_type
        elif current_sensor_type != sensor_type:
            raise ValueError("Frame metadata sensor_type must be consistent")
        metadata_frames.add(frame_id)
    if metadata_frames != expected_frames:
        missing = sorted(expected_frames - metadata_frames)
        extra = sorted(metadata_frames - expected_frames)
        raise ValueError(
            "Frame metadata must cover exactly the synchronized RGB-D frames; "
            f"missing={missing}, extra={extra}"
        )
    return records


def _validated_matched_robot_poses(
    sensor_folder: Path,
    pairs: Sequence[tuple[Path, Path]],
) -> dict[str, Any]:
    path = sensor_folder / MATCH_ROBOT_EE_POSES
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Matched robot-pose evidence is required: {path}")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid matched robot-pose JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Matched robot-pose evidence must be a JSON object")
    expected_frames = {rgb_path.name for rgb_path, _depth_path in pairs}
    actual_frames = set(value)
    if actual_frames != expected_frames:
        missing = sorted(expected_frames - actual_frames)
        extra = sorted(actual_frames - expected_frames)
        raise ValueError(
            "Matched robot-pose evidence must cover exactly the synchronized "
            f"RGB-D frames; missing={missing}, extra={extra}"
        )
    for frame_id in sorted(expected_frames):
        matched = value[frame_id]
        if not isinstance(matched, Mapping):
            raise ValueError(
                f"Matched robot-pose record for {frame_id} must be an object"
            )
        pose = matched.get("robot_ee_pose")
        if not isinstance(pose, Mapping):
            raise ValueError(
                f"Matched robot-pose record for {frame_id} requires robot_ee_pose"
            )
        for field in ("X", "Y", "Z", "A", "B", "C"):
            raw = pose.get(field)
            if isinstance(raw, bool):
                raise ValueError(
                    f"Matched robot pose {frame_id} field {field} must be finite"
                )
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Matched robot pose {frame_id} field {field} must be finite"
                ) from exc
            if not math.isfinite(number):
                raise ValueError(
                    f"Matched robot pose {frame_id} field {field} must be finite"
                )
    return value


def _write_rectified_metadata(
    records: Sequence[Mapping[str, Any]],
    *,
    source: Path,
    destination: Path,
    profile_id: str,
) -> int:
    derived_records = []
    for value in records:
        record = dict(value)
        record["derivation"] = {
            "operation": "alpha0_camera_rectification",
            "intrinsic_profile_id": profile_id,
            "source_metadata": source.as_posix(),
            "rgb_interpolation": "linear",
            "depth_interpolation": "nearest",
            "invalid_depth_value": 0,
        }
        derived_records.append(record)
    atomic_write_text(
        destination,
        "".join(
            json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
            for record in derived_records
        ),
    )
    return len(derived_records)


def _write_sidecars(
    destination: Path,
    profile: Mapping[str, Any],
    rectified_k: np.ndarray,
    *,
    source_sensor: Path,
) -> None:
    depth_scale = float(profile["depth"]["scale_to_mm"])
    matrix_rows = rectified_k.tolist()
    atomic_write_text(
        destination / CAM_K,
        "".join(
            " ".join(str(float(item)) for item in row) + "\n" for row in matrix_rows
        )
        + "0.0 0.0 0.0 0.0 0.0\n",
    )
    atomic_write_text(destination / DEPTH_SCALE, f"{depth_scale}\n")
    atomic_write_json(
        destination / CAMERA_DATA_JSON,
        {
            "K": matrix_rows,
            "resolution": [
                int(profile["resolution"][1]),
                int(profile["resolution"][0]),
            ],
            "distortion": [0.0] * 5,
            "projection": "rectified_alpha0",
            "valid_roi": profile["rectified"]["valid_roi"],
            "intrinsic_profile_id": profile["profile_id"],
            "source_sensor_folder": source_sensor.as_posix(),
            "depth_alignment": profile["depth"]["alignment"],
        },
    )


def rectify_sensor_folder(
    source_sensor: str | Path,
    destination_sensor: str | Path,
    profile: Mapping[str, Any],
    *,
    provenance_output_sensor: str | Path | None = None,
) -> dict[str, Any]:
    """Rectify a sensor into an empty staging folder."""

    source = Path(source_sensor)
    destination = Path(destination_sensor)
    final_output = (
        Path(provenance_output_sensor)
        if provenance_output_sensor is not None
        else destination
    )
    source_fingerprint = rgbd_camera_artifact_fingerprint(source)
    sensor_id, orientation, image_size = sensor_intrinsic_identity(source)
    expected = (
        str(profile["sensor_id"]),
        str(profile["orientation"]),
        tuple(profile["resolution"]),
    )
    actual = (sensor_id, orientation, image_size)
    if actual != expected:
        raise ValueError(
            "Intrinsic profile serial/resolution/orientation mismatch: "
            f"captured={actual}, profile={expected}"
        )
    pairs = _pairs(source)
    frame_metadata = _validated_frame_metadata(
        source,
        pairs,
        expected_sensor_id=sensor_id,
        expected_orientation=orientation,
    )
    _validated_matched_robot_poses(source, pairs)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / RGB_DIR).mkdir()
    (destination / DEPTH_DIR).mkdir()
    map_x, map_y, rectified_k = _maps(profile)
    for rgb_path, depth_path in pairs:
        rgb, depth = _read_validated_rgbd_pair(
            rgb_path,
            depth_path,
            expected_image_size=image_size,
        )
        rectified_rgb = cv2.remap(
            rgb,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        rectified_depth = cv2.remap(
            depth,
            map_x,
            map_y,
            cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        if not cv2.imwrite(
            (destination / RGB_DIR / rgb_path.name).as_posix(), rectified_rgb
        ):
            raise OSError(f"Failed to write rectified RGB: {rgb_path.name}")
        if not cv2.imwrite(
            (destination / DEPTH_DIR / depth_path.name).as_posix(), rectified_depth
        ):
            raise OSError(f"Failed to write rectified depth: {depth_path.name}")
        _read_validated_rgbd_pair(
            destination / RGB_DIR / rgb_path.name,
            destination / DEPTH_DIR / depth_path.name,
            expected_image_size=image_size,
        )

    shutil.copy2(
        source / MATCH_ROBOT_EE_POSES,
        destination / MATCH_ROBOT_EE_POSES,
    )
    metadata_count = _write_rectified_metadata(
        frame_metadata,
        source=source / FRAME_METADATA_JSONL,
        destination=destination / FRAME_METADATA_JSONL,
        profile_id=str(profile["profile_id"]),
    )
    _write_sidecars(destination, profile, rectified_k, source_sensor=source)
    _validated_frame_metadata(
        destination,
        pairs,
        expected_sensor_id=sensor_id,
        expected_orientation=orientation,
    )
    _validated_matched_robot_poses(destination, pairs)
    if rgbd_camera_artifact_fingerprint(source) != source_fingerprint:
        raise RuntimeError(
            f"Synchronized source changed during rectification: {source}"
        )
    output_fingerprint = rgbd_camera_artifact_fingerprint(destination)
    atomic_write_json(
        destination / RECTIFICATION_PROVENANCE,
        {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "source_sensor_folder": source.resolve().as_posix(),
            "output_sensor_folder": final_output.resolve().as_posix(),
            "intrinsic_profile_id": profile["profile_id"],
            "projection": "rectified_alpha0",
            "rgb_interpolation": "linear",
            "depth_interpolation": "nearest",
            "invalid_depth_value": 0,
            "frame_count": len(pairs),
            "source_fingerprint": source_fingerprint,
            "output_fingerprint": output_fingerprint,
        },
    )
    return {
        "sensor_name": source.name,
        "sensor_id": sensor_id,
        "orientation": orientation,
        "resolution": list(image_size),
        "profile_id": profile["profile_id"],
        "frame_count": len(pairs),
        "metadata_record_count": metadata_count,
        "source": source.as_posix(),
        "output": destination.as_posix(),
        "source_fingerprint": source_fingerprint,
        "output_fingerprint": output_fingerprint,
    }


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    try:
        first_resolved.relative_to(second_resolved)
        return True
    except ValueError:
        pass
    try:
        second_resolved.relative_to(first_resolved)
        return True
    except ValueError:
        return False


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path, *, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symlink components: {current}")


def rectify_run(
    run_root: str | Path,
    profiles: Sequence[Mapping[str, Any]],
    *,
    input_root: str | Path | None = None,
    output_root: str | Path | None = None,
    diagnostic_unmanaged: bool = False,
    overwrite: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Build rectified sensors in one staging tree, then promote atomically.

    The default is the managed dataset contract: canonical synchronized input,
    canonical rectified output, and exactly the enabled run-config sensors.
    Custom roots are retained only for explicit test/diagnostic use and require
    ``diagnostic_unmanaged=True`` plus both roots.
    """

    root = Path(run_root)
    custom_roots = input_root is not None or output_root is not None
    if diagnostic_unmanaged:
        if input_root is None or output_root is None:
            raise ValueError(
                "Diagnostic unmanaged rectification requires both input_root "
                "and output_root"
            )
    elif custom_roots:
        raise ValueError(
            "Custom rectification roots are diagnostic-only; set "
            "diagnostic_unmanaged=True and provide both roots"
        )
    source_root = (
        Path(input_root)
        if diagnostic_unmanaged
        else root / PROCESSED_DIR / SYNCHRONIZED_DIR
    )
    destination_root = (
        Path(output_root)
        if diagnostic_unmanaged
        else root / PROCESSED_DIR / RECTIFIED_DIR
    )
    canonical_destination = root / PROCESSED_DIR / RECTIFIED_DIR
    if (
        diagnostic_unmanaged
        and destination_root.resolve() == canonical_destination.resolve()
    ):
        raise ValueError(
            "Diagnostic unmanaged rectification must not write the canonical "
            "rectified output root"
        )
    if source_root.resolve() == destination_root.resolve():
        raise ValueError("Rectification input and output roots must be different")
    if _paths_overlap(source_root, destination_root):
        raise ValueError("Rectification input and output roots must not overlap")
    if _is_within(root, destination_root):
        raise ValueError("Rectification output must not equal or contain the run root")
    if _is_within(destination_root, root):
        relative_output = destination_root.resolve().relative_to(root.resolve())
        if not relative_output.parts or relative_output.parts[0] != PROCESSED_DIR:
            raise ValueError(
                "Run-contained rectification output must remain below processed/"
            )
    _reject_symlink_components(source_root, label="Rectification input")
    _reject_symlink_components(destination_root, label="Rectification output")
    if not diagnostic_unmanaged:
        processed_root = root / PROCESSED_DIR
        for path in (processed_root, source_root, destination_root):
            if path.is_symlink():
                raise ValueError(
                    f"Managed camera rectification paths must not be symlinks: {path}"
                )
    if destination_root.is_symlink():
        raise ValueError(
            f"Rectified output root must not be a symlink: {destination_root}"
        )
    if diagnostic_unmanaged and overwrite:
        raise ValueError(
            "Diagnostic unmanaged rectification does not overwrite existing "
            "destinations; choose a fresh isolated output_root"
        )
    if destination_root.exists() and not overwrite:
        raise FileExistsError(f"Rectified output already exists: {destination_root}")
    discovered_sensors = (
        [
            path
            for path in sorted(source_root.iterdir())
            if path.is_dir()
            and (path / RGB_DIR).is_dir()
            and (path / DEPTH_DIR).is_dir()
        ]
        if source_root.is_dir()
        else []
    )
    if diagnostic_unmanaged:
        sensors = discovered_sensors
    else:
        enabled_names = enabled_sensor_folder_names(root)
        if not enabled_names:
            raise ValueError("run_config.json has no enabled sensors to rectify")
        if len(enabled_names) != len(set(enabled_names)):
            raise ValueError("run_config.json has duplicate enabled sensor folders")
        discovered_by_name = {sensor.name: sensor for sensor in discovered_sensors}
        missing = [name for name in enabled_names if name not in discovered_by_name]
        if missing:
            raise FileNotFoundError(
                "Canonical synchronized input is missing enabled RGB-D sensor "
                "folder(s): " + ", ".join(missing)
            )
        sensors = [discovered_by_name[name] for name in enabled_names]
    if not sensors:
        raise FileNotFoundError(f"No synchronized RGB-D sensor folders: {source_root}")
    staging = destination_root.with_name(
        f".{destination_root.name}.{uuid.uuid4().hex}.tmp"
    )
    staging.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        for sensor in sensors:
            _require_regular_fingerprint_sidecars(sensor)
            sensor_id, orientation, resolution = sensor_intrinsic_identity(sensor)
            profile = select_intrinsic_profile(
                profiles,
                sensor_id=sensor_id,
                resolution=resolution,
                orientation=orientation,
            )
            records.append(
                rectify_sensor_folder(
                    sensor,
                    staging / sensor.name,
                    profile,
                    provenance_output_sensor=destination_root / sensor.name,
                )
            )
        replace_directory(staging, destination_root)
        for record in records:
            record["output"] = (
                destination_root / str(record["sensor_name"])
            ).as_posix()
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": (
            "diagnostic_unmanaged" if diagnostic_unmanaged else "managed_canonical"
        ),
        "run_root": root.as_posix(),
        "source_root": source_root.as_posix(),
        "output_root": destination_root.as_posix(),
        "projection": "rectified_alpha0",
        "sensor_count": len(records),
        "frame_count": sum(int(item["frame_count"]) for item in records),
        "sensors": records,
    }
    report_root = destination_root if diagnostic_unmanaged else root
    report_path = atomic_write_json(report_root / CAMERA_RECTIFICATION_REPORT, report)
    return report_path, report
