"""Transactional non-destructive RGB/aligned-depth rectification."""

from __future__ import annotations

import hashlib
import json
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
from posetestbot.pipeline.sensor_selection import filter_enabled_sensor_folders
from posetestbot.io.derived import validate_derived_output


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


def _fingerprint_file(sensor_folder: Path, relative_path: Path) -> tuple[int, str]:
    path = sensor_folder / relative_path
    resolved_sensor = sensor_folder.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_sensor)
    except ValueError as exc:
        raise ValueError(
            f"Camera artifact escapes its sensor folder: {path}"
        ) from exc
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

    The compact aggregate deliberately excludes derived render outputs and the
    provenance file itself.  It therefore remains stable when BlenderProc adds
    masks/GT later while still detecting changed pixels, frame membership,
    timestamps, robot-pose matches, intrinsics, or depth scale.
    """

    sensor = Path(sensor_folder)
    if sensor.is_symlink() or not sensor.is_dir():
        raise ValueError(f"Sensor folder must be a regular directory: {sensor}")
    rgb_dir = sensor / RGB_DIR
    depth_dir = sensor / DEPTH_DIR
    if rgb_dir.is_symlink() or depth_dir.is_symlink():
        raise ValueError(f"RGB/depth directories must not be symlinks: {sensor}")
    pairs = _pairs(sensor)
    relative_paths = [
        relative
        for rgb_path, depth_path in pairs
        for relative in (
            rgb_path.relative_to(sensor),
            depth_path.relative_to(sensor),
        )
    ]
    relative_paths.extend(
        Path(name) for name in _FINGERPRINT_SIDECARS if (sensor / name).is_file()
    )
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


def _copy_metadata(source: Path, destination: Path, profile_id: str) -> int:
    if not source.is_file():
        return 0
    records = []
    for line_number, line in enumerate(source.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError(f"Frame metadata line {line_number} must be an object")
        record = dict(value)
        record["derivation"] = {
            "operation": "alpha0_camera_rectification",
            "intrinsic_profile_id": profile_id,
            "source_metadata": source.as_posix(),
            "rgb_interpolation": "linear",
            "depth_interpolation": "nearest",
            "invalid_depth_value": 0,
        }
        records.append(record)
    atomic_write_text(
        destination,
        "".join(
            json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
            for record in records
        ),
    )
    return len(records)


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
    validate_derived_output(source.parent, destination, sources=(source,))
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
    destination.mkdir(parents=True, exist_ok=False)
    (destination / RGB_DIR).mkdir()
    (destination / DEPTH_DIR).mkdir()
    map_x, map_y, rectified_k = _maps(profile)
    for rgb_path, depth_path in pairs:
        rgb = cv2.imread(rgb_path.as_posix(), cv2.IMREAD_UNCHANGED)
        depth = cv2.imread(depth_path.as_posix(), cv2.IMREAD_UNCHANGED)
        if rgb is None or depth is None:
            raise ValueError(f"Unreadable RGB-D frame pair: {rgb_path.name}")
        if rgb.shape[:2] != depth.shape or (rgb.shape[1], rgb.shape[0]) != image_size:
            raise ValueError(
                f"RGB-D dimensions do not match intrinsic profile: {rgb_path.name}"
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

    for artifact in (MATCH_ROBOT_EE_POSES,):
        source_path = source / artifact
        if source_path.is_file():
            shutil.copy2(source_path, destination / artifact)
    metadata_count = _copy_metadata(
        source / FRAME_METADATA_JSONL,
        destination / FRAME_METADATA_JSONL,
        str(profile["profile_id"]),
    )
    _write_sidecars(destination, profile, rectified_k, source_sensor=source)
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


def rectify_run(
    run_root: str | Path,
    profiles: Sequence[Mapping[str, Any]],
    *,
    input_root: str | Path | None = None,
    output_root: str | Path | None = None,
    overwrite: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Build every rectified sensor in one staging tree, then promote it atomically."""

    root = Path(run_root)
    source_root = (
        Path(input_root) if input_root else root / PROCESSED_DIR / SYNCHRONIZED_DIR
    )
    destination_root = (
        Path(output_root) if output_root else root / PROCESSED_DIR / RECTIFIED_DIR
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
    sensors = (
        filter_enabled_sensor_folders(root, discovered_sensors)
        if input_root is None
        else discovered_sensors
    )
    if not sensors:
        raise FileNotFoundError(f"No synchronized RGB-D sensor folders: {source_root}")
    validate_derived_output(root, destination_root, sources=sensors)
    staging = destination_root.with_name(
        f".{destination_root.name}.{uuid.uuid4().hex}.tmp"
    )
    staging.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        for sensor in sensors:
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
        "run_root": root.as_posix(),
        "source_root": source_root.as_posix(),
        "output_root": destination_root.as_posix(),
        "projection": "rectified_alpha0",
        "sensor_count": len(records),
        "frame_count": sum(int(item["frame_count"]) for item in records),
        "sensors": records,
    }
    report_path = atomic_write_json(root / CAMERA_RECTIFICATION_REPORT, report)
    return report_path, report
