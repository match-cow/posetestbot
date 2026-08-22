"""Authoritative completion validation for one supervised physical capture."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from posetestbot.io.artifacts import (
    CAMERA_DATA_JSON,
    CAMERA_JSON,
    CAM_K,
    DEPTH_DIR,
    DEPTH_SCALE,
    FRAME_METADATA_JSONL,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
)
from posetestbot.robot.pose_receiver import (
    POSE_PACKET_SCHEMA_VERSION,
    STREAM_END_SOURCE_PACKET,
)
from posetestbot.sensors.frame_writer import SCHEMA_VERSION as FRAME_METADATA_SCHEMA
from posetestbot.sensors.registry import (
    capture_resolution_image_size,
    is_auto_device_id,
    sensor_folder_name,
)


SCHEMA_VERSION = "capture_completion.v1"


def _check(name: str, ok: bool, message: str, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": "ok" if ok else "error",
        "message": message,
        "details": details,
    }


def _positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _load_current_frame_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing current frame metadata: {path}")
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.endswith("\n"):
                raise ValueError(
                    f"Frame metadata line {line_number} is not committed with a newline"
                )
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Frame metadata line {line_number} is invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(f"Frame metadata line {line_number} must be an object")
            if record.get("schema_version") != FRAME_METADATA_SCHEMA:
                raise ValueError(
                    f"Frame metadata line {line_number} must use {FRAME_METADATA_SCHEMA}"
                )
            for field in ("host_received_timestamp_ns", "host_wall_timestamp_ns"):
                if not _positive_integer(record.get(field)):
                    raise ValueError(
                        f"Frame metadata line {line_number} requires positive {field}"
                    )
                if records and record[field] <= records[-1][field]:
                    raise ValueError(
                        f"Frame metadata line {line_number} {field} must strictly "
                        "increase in capture order"
                    )
            sensor_timestamp = record.get("sensor_timestamp_ns")
            if sensor_timestamp is not None and not _positive_integer(sensor_timestamp):
                raise ValueError(
                    f"Frame metadata line {line_number} requires "
                    "sensor_timestamp_ns to be null or a positive integer"
                )
            if (
                isinstance(record.get("frame_index"), bool)
                or not isinstance(record.get("frame_index"), int)
                or record["frame_index"] < 0
            ):
                raise ValueError(
                    f"Frame metadata line {line_number} requires a non-negative frame_index"
                )
            expected_frame_index = len(records)
            if record["frame_index"] != expected_frame_index:
                raise ValueError(
                    f"Frame metadata line {line_number} frame_index must be "
                    f"{expected_frame_index} for ordered contiguous capture evidence; "
                    f"got {record['frame_index']}"
                )
            for field in ("sensor_id", "frame_id", "rgb_path", "depth_path"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ValueError(
                        f"Frame metadata line {line_number} requires non-empty {field}"
                    )
            frame_id = str(record["frame_id"])
            if Path(frame_id).name != frame_id or not frame_id.endswith(".png"):
                raise ValueError(
                    f"Frame metadata line {line_number} requires a plain .png frame_id"
                )
            records.append(record)
    if not records:
        raise ValueError(f"Frame metadata must contain at least one record: {path}")
    return records


def _decode_png(path: Path) -> tuple[str, tuple[int, int]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"PNG path is not a regular file: {path}")
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError(f"Image is not encoded as PNG: {path}")
            image.verify()
        with Image.open(path) as image:
            image.load()
            return image.mode, image.size
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError(f"PNG decode failed for {path}: {exc}") from exc


def _finite_float_values(value: Any, *, count: int, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{field} must contain exactly {count} values")
    result: list[float] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            raise ValueError(f"{field} must contain only finite numbers")
        result.append(float(item))
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Camera sidecar is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Camera sidecar must be a JSON object: {path}")
    return value


def _distortion_evidence(
    value: Mapping[str, Any],
    *,
    field: str,
) -> tuple[list[float], str, str | None] | None:
    raw = value.get("distortion")
    if raw is None:
        if (
            value.get("distortion_model") is not None
            or value.get("projection_source") is not None
        ):
            raise ValueError(f"{field} has projection provenance without distortion")
        return None
    if not isinstance(raw, list) or len(raw) not in {0, 4, 5, 8, 12, 14}:
        raise ValueError(f"{field}.distortion has an unsupported coefficient count")
    distortion = _finite_float_values(
        raw,
        count=len(raw),
        field=f"{field}.distortion",
    )
    model = value.get("distortion_model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{field}.distortion_model must be a non-empty string")
    projection_source = value.get("projection_source")
    if projection_source is not None and (
        not isinstance(projection_source, str) or not projection_source.strip()
    ):
        raise ValueError(f"{field}.projection_source must be a string or null")
    return distortion, model, projection_source


def _camera_sidecar_evidence(folder: Path) -> tuple[tuple[int, int], float]:
    """Validate sidecar agreement and return image size plus depth scale."""

    camera = _load_json_object(folder / CAMERA_JSON)
    camera_data = _load_json_object(folder / CAMERA_DATA_JSON)
    camera_k = _finite_float_values(
        camera.get("cam_K"), count=9, field=f"{CAMERA_JSON}.cam_K"
    )
    matrix = camera_data.get("K")
    if not isinstance(matrix, list) or len(matrix) != 3:
        raise ValueError(f"{CAMERA_DATA_JSON}.K must be a 3x3 matrix")
    matrix_k: list[float] = []
    for row in matrix:
        matrix_k.extend(
            _finite_float_values(row, count=3, field=f"{CAMERA_DATA_JSON}.K row")
        )

    try:
        cam_k_lines = (folder / CAM_K).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            f"Camera sidecar is unreadable: {folder / CAM_K}: {exc}"
        ) from exc
    if len(cam_k_lines) not in {3, 4}:
        raise ValueError(
            f"{CAM_K} must contain three matrix rows and at most one distortion row"
        )
    text_k: list[float] = []
    for line in cam_k_lines[:3]:
        try:
            row = [float(item) for item in line.split()]
        except ValueError as exc:
            raise ValueError(f"{CAM_K} matrix rows must contain numbers") from exc
        if len(row) != 3 or not all(math.isfinite(item) for item in row):
            raise ValueError(f"{CAM_K} must contain a finite 3x3 matrix")
        text_k.extend(row)
    if not all(
        math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
        for left, right in zip(camera_k, matrix_k, strict=True)
    ) or not all(
        math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
        for left, right in zip(camera_k, text_k, strict=True)
    ):
        raise ValueError(
            f"{CAM_K}, {CAMERA_JSON}, and {CAMERA_DATA_JSON} intrinsics disagree"
        )
    if camera_k[0] <= 0 or camera_k[4] <= 0:
        raise ValueError("Camera intrinsic focal lengths must be positive")
    if not all(
        math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-12)
        for value, expected in zip(
            camera_k[6:9],
            (0.0, 0.0, 1.0),
            strict=True,
        )
    ):
        raise ValueError("Camera intrinsic matrix bottom row must be [0, 0, 1]")

    camera_distortion = _distortion_evidence(camera, field=CAMERA_JSON)
    camera_data_distortion = _distortion_evidence(
        camera_data,
        field=CAMERA_DATA_JSON,
    )
    if camera_distortion != camera_data_distortion:
        raise ValueError(
            f"{CAMERA_JSON} and {CAMERA_DATA_JSON} distortion evidence disagrees"
        )
    if len(cam_k_lines) == 4:
        try:
            text_distortion = [float(item) for item in cam_k_lines[3].split()]
        except ValueError as exc:
            raise ValueError(f"{CAM_K} distortion row must contain numbers") from exc
        if (
            len(text_distortion) not in {4, 5, 8, 12, 14}
            or not all(math.isfinite(item) for item in text_distortion)
            or camera_distortion is None
            or len(text_distortion) != len(camera_distortion[0])
            or any(
                not math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
                for left, right in zip(
                    text_distortion,
                    camera_distortion[0],
                    strict=True,
                )
            )
        ):
            raise ValueError(
                f"{CAM_K}, {CAMERA_JSON}, and {CAMERA_DATA_JSON} distortion "
                "evidence disagrees"
            )

    resolution = camera_data.get("resolution")
    if (
        not isinstance(resolution, list)
        or len(resolution) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in resolution
        )
    ):
        raise ValueError(
            f"{CAMERA_DATA_JSON}.resolution must be [positive height, positive width]"
        )
    sidecar_size = (resolution[1], resolution[0])

    camera_depth_scale = camera.get("depth_scale")
    if (
        isinstance(camera_depth_scale, bool)
        or not isinstance(camera_depth_scale, (int, float))
        or not math.isfinite(float(camera_depth_scale))
        or float(camera_depth_scale) <= 0
    ):
        raise ValueError(f"{CAMERA_JSON}.depth_scale must be finite and positive")
    try:
        depth_scale_values = (folder / DEPTH_SCALE).read_text(encoding="utf-8").split()
        depth_scale = float(depth_scale_values[0])
    except (IndexError, OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{DEPTH_SCALE} must contain one positive number") from exc
    if (
        len(depth_scale_values) != 1
        or not math.isfinite(depth_scale)
        or depth_scale <= 0
        or not math.isclose(
            depth_scale,
            float(camera_depth_scale),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        raise ValueError(f"{DEPTH_SCALE} and {CAMERA_JSON}.depth_scale disagree")
    return sidecar_size, depth_scale


def _validate_frame_pngs(
    folder: Path,
    records: list[Mapping[str, Any]],
) -> tuple[int, tuple[int, int] | None, list[str], list[str]]:
    expected_size: tuple[int, int] | None = None
    rgb_modes: set[str] = set()
    depth_modes: set[str] = set()
    validated = 0
    for index, record in enumerate(records):
        frame_id = str(record["frame_id"])
        rgb_mode, rgb_size = _decode_png(folder / RGB_DIR / frame_id)
        depth_mode, depth_size = _decode_png(folder / DEPTH_DIR / frame_id)
        if rgb_mode not in {"RGB", "RGBA"}:
            raise ValueError(
                f"RGB frame {index} must decode as 8-bit RGB or RGBA; got {rgb_mode}"
            )
        if not depth_mode.startswith("I;16"):
            raise ValueError(
                f"Depth frame {index} must decode as 16-bit grayscale; got {depth_mode}"
            )
        if rgb_size != depth_size:
            raise ValueError(
                f"RGB/depth frame {index} dimensions differ: {rgb_size} != {depth_size}"
            )
        if expected_size is None:
            expected_size = rgb_size
        elif rgb_size != expected_size:
            raise ValueError(
                f"Frame {index} dimensions changed: {rgb_size} != {expected_size}"
            )
        rgb_modes.add(rgb_mode)
        depth_modes.add(depth_mode)
        validated += 1
    return validated, expected_size, sorted(rgb_modes), sorted(depth_modes)


def _sensor_check(
    root: Path,
    sensor: Mapping[str, Any],
    *,
    configured_resolution: str,
    expected_image_size: tuple[int, int],
) -> dict[str, Any]:
    folder_name = sensor_folder_name(
        str(sensor["sensor_type"]), str(sensor["device_id"])
    )
    folder = root / folder_name
    folder_is_regular = folder.is_dir() and not folder.is_symlink()
    rgb_root = folder / RGB_DIR
    depth_root = folder / DEPTH_DIR
    image_directories_ok = (
        folder_is_regular
        and rgb_root.is_dir()
        and not rgb_root.is_symlink()
        and depth_root.is_dir()
        and not depth_root.is_symlink()
    )
    sidecar_names = (CAM_K, DEPTH_SCALE, CAMERA_JSON, CAMERA_DATA_JSON)
    try:
        sidecar_files_present = folder_is_regular and all(
            (folder / name).is_file()
            and not (folder / name).is_symlink()
            and (folder / name).stat().st_size > 0
            for name in sidecar_names
        )
    except OSError:
        sidecar_files_present = False
    sidecar_image_size: tuple[int, int] | None = None
    sidecar_validation_error: str | None = None
    if sidecar_files_present:
        try:
            sidecar_image_size, _depth_scale = _camera_sidecar_evidence(folder)
        except ValueError as exc:
            sidecar_validation_error = str(exc)
    else:
        sidecar_validation_error = "One or more required camera sidecars are missing"
    rgb_files = (
        {
            path.relative_to(folder).as_posix()
            for path in rgb_root.glob("*.png")
            if path.is_file() and not path.is_symlink()
        }
        if image_directories_ok
        else set()
    )
    depth_files = (
        {
            path.relative_to(folder).as_posix()
            for path in depth_root.glob("*.png")
            if path.is_file() and not path.is_symlink()
        }
        if image_directories_ok
        else set()
    )
    try:
        if not folder_is_regular:
            raise ValueError(f"Sensor output is not a regular directory: {folder}")
        if not image_directories_ok:
            raise ValueError(
                f"Sensor RGB/depth outputs must be regular directories: {folder}"
            )
        records = _load_current_frame_records(folder / FRAME_METADATA_JSONL)
        metadata_error = None
    except (OSError, UnicodeError, ValueError) as exc:
        records = []
        metadata_error = str(exc)
    recorded_rgb = {str(record.get("rgb_path")) for record in records}
    recorded_depth = {str(record.get("depth_path")) for record in records}
    frame_ids = [str(record.get("frame_id")) for record in records]
    frame_indices = [record.get("frame_index") for record in records]
    sensor_timestamp_count = sum(
        _positive_integer(record.get("sensor_timestamp_ns")) for record in records
    )
    paths_match_frame_ids = bool(records) and all(
        record.get("rgb_path") == f"{RGB_DIR}/{record['frame_id']}"
        and record.get("depth_path") == f"{DEPTH_DIR}/{record['frame_id']}"
        for record in records
    )
    expected_sensor_id = str(sensor["device_id"])
    identity_ok = bool(records) and all(
        record.get("sensor_type") == sensor["sensor_type"]
        and (
            isinstance(record.get("sensor_id"), str)
            and bool(record["sensor_id"])
            and (
                is_auto_device_id(expected_sensor_id)
                or record.get("sensor_id") == expected_sensor_id
            )
        )
        for record in records
    )
    balanced = (
        image_directories_ok
        and bool(records)
        and len(rgb_files) == len(depth_files) == len(records)
        and recorded_rgb == rgb_files
        and recorded_depth == depth_files
        and len(frame_ids) == len(set(frame_ids))
        and len(frame_indices) == len(set(frame_indices))
        and paths_match_frame_ids
    )
    image_validation_error: str | None = None
    validated_image_pair_count = 0
    image_dimensions: tuple[int, int] | None = None
    rgb_modes: list[str] = []
    depth_modes: list[str] = []
    if balanced:
        try:
            (
                validated_image_pair_count,
                image_dimensions,
                rgb_modes,
                depth_modes,
            ) = _validate_frame_pngs(folder, records)
        except (OSError, ValueError) as exc:
            image_validation_error = str(exc)
    configured_dimensions_ok = image_dimensions == expected_image_size
    sidecar_dimensions_match_config = sidecar_image_size == expected_image_size
    sidecar_dimensions_match_pixels = (
        image_dimensions is not None and sidecar_image_size == image_dimensions
    )
    sidecars_ok = (
        sidecar_files_present
        and sidecar_validation_error is None
        and sidecar_dimensions_match_config
    )
    ok = (
        metadata_error is None
        and sidecars_ok
        and identity_ok
        and balanced
        and image_validation_error is None
        and validated_image_pair_count == len(records)
        and configured_dimensions_ok
        and sidecar_dimensions_match_pixels
    )
    return _check(
        f"sensor:{folder_name}",
        ok,
        (
            f"{folder_name} has balanced current RGB-D metadata and usable capture timestamps."
            if ok
            else f"{folder_name} does not satisfy the current raw-frame contract."
        ),
        folder=folder.as_posix(),
        sensor_output_directory_ok=folder_is_regular,
        image_directories_ok=image_directories_ok,
        rgb_count=len(rgb_files),
        depth_count=len(depth_files),
        metadata_count=len(records),
        sensor_timestamp_count=sensor_timestamp_count,
        sensor_timestamp_missing_count=len(records) - sensor_timestamp_count,
        metadata_error=metadata_error,
        required_sidecars=list(sidecar_names),
        sidecar_files_present=sidecar_files_present,
        sidecars_ok=sidecars_ok,
        sidecar_validation_error=sidecar_validation_error,
        sidecar_image_dimensions=(
            list(sidecar_image_size) if sidecar_image_size is not None else None
        ),
        sidecar_dimensions_match_config=sidecar_dimensions_match_config,
        sidecar_dimensions_match_pixels=sidecar_dimensions_match_pixels,
        configured_resolution=configured_resolution,
        expected_image_dimensions=list(expected_image_size),
        configured_dimensions_ok=configured_dimensions_ok,
        identity_ok=identity_ok,
        paths_match_frame_ids=paths_match_frame_ids,
        balanced=balanced,
        validated_image_pair_count=validated_image_pair_count,
        image_dimensions=(
            list(image_dimensions) if image_dimensions is not None else None
        ),
        rgb_modes=rgb_modes,
        depth_modes=depth_modes,
        image_validation_error=image_validation_error,
    )


def _robot_pose_check(
    root: Path,
    *,
    run_id: str,
    expected_reference_frame_path: str,
) -> dict[str, Any]:
    path = root / RAW_ROBOT_EE_POSES
    error: str | None = None
    records: list[Mapping[str, Any]] = []
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Raw robot pose artifact is not a regular file: {path}")
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or not value:
            raise ValueError("raw robot pose artifact must be a nonempty object")
        indexed_records: list[tuple[int, Mapping[str, Any]]] = []
        for key, record in value.items():
            if not isinstance(record, Mapping):
                raise ValueError("every raw robot pose entry must be an object")
            try:
                pose_index = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError("raw robot pose keys must be numeric") from exc
            indexed_records.append((pose_index, record))
        indexed_records.sort(key=lambda item: item[0])
        if [item[0] for item in indexed_records] != list(range(len(indexed_records))):
            raise ValueError("raw robot pose keys must be contiguous from zero")
        records = [record for _, record in indexed_records]
        previous_sequence: int | None = None
        packet_loss_count = 0
        for index, record in enumerate(records):
            for field in ("host_received_timestamp_ns", "host_wall_timestamp_ns"):
                if not _positive_integer(record.get(field)):
                    raise ValueError(f"robot pose {index} requires positive {field}")
            if (
                not isinstance(record.get("motion"), str)
                or not record["motion"].strip()
            ):
                raise ValueError(f"robot pose {index} requires a non-empty motion")
            pose = record.get("pose")
            if not isinstance(pose, Mapping):
                raise ValueError(f"robot pose {index} is missing pose coordinates")
            for axis in ("X", "Y", "Z", "A", "B", "C"):
                coordinate = pose.get(axis)
                if (
                    isinstance(coordinate, bool)
                    or not isinstance(coordinate, (int, float))
                    or not math.isfinite(float(coordinate))
                ):
                    raise ValueError(
                        f"robot pose {index} coordinate {axis} must be finite"
                    )
            source = record.get("source_packet")
            if not isinstance(source, Mapping):
                raise ValueError(f"robot pose {index} is missing source_packet")
            if source.get("schema_version") != POSE_PACKET_SCHEMA_VERSION:
                raise ValueError(
                    f"robot pose {index} must use {POSE_PACKET_SCHEMA_VERSION}"
                )
            if source.get("packet_kind") != "pose":
                raise ValueError(f"robot pose {index} packet_kind must be pose")
            if source.get("run_id") != run_id:
                raise ValueError(f"robot pose {index} run_id does not match the run")
            sequence = source.get("sequence")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
            ):
                raise ValueError(
                    f"robot pose {index} sequence must be a non-negative integer"
                )
            if previous_sequence is not None and sequence <= previous_sequence:
                raise ValueError(f"robot pose {index} sequence must increase strictly")
            for field in ("sender_monotonic_ns", "sender_wall_timestamp_ms"):
                timestamp = source.get(field)
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, int)
                    or timestamp < 0
                ):
                    raise ValueError(
                        f"robot pose {index} requires non-negative {field}"
                    )
            for field in ("sequence_delta", "estimated_packets_lost"):
                derived = source.get(field)
                if (
                    isinstance(derived, bool)
                    or not isinstance(derived, int)
                    or derived < 0
                ):
                    raise ValueError(
                        f"robot pose {index} requires non-negative {field}"
                    )
            expected_delta = (
                0 if previous_sequence is None else sequence - previous_sequence
            )
            if source["sequence_delta"] != expected_delta:
                raise ValueError(f"robot pose {index} sequence_delta is inconsistent")
            expected_loss = max(0, expected_delta - 1)
            if source["estimated_packets_lost"] != expected_loss:
                raise ValueError(
                    f"robot pose {index} estimated_packets_lost is inconsistent"
                )
            packet_loss_count += expected_loss
            previous_sequence = sequence
            if (
                source.get("from_frame") != "robot_flange"
                or source.get("to_frame") != "template_base"
                or source.get("sunrise_reference_frame_path")
                != expected_reference_frame_path
            ):
                raise ValueError(
                    f"robot pose {index} does not match the configured frame provenance"
                )
            if index < len(records) - 1 and STREAM_END_SOURCE_PACKET in record:
                raise ValueError(
                    "stream-end packet evidence may appear only on the final robot pose"
                )

        terminal = records[-1].get(STREAM_END_SOURCE_PACKET)
        if not isinstance(terminal, Mapping):
            raise ValueError("final robot pose is missing stream-end packet evidence")
        if (
            terminal.get("schema_version") != POSE_PACKET_SCHEMA_VERSION
            or terminal.get("packet_kind") != "end"
            or terminal.get("run_id") != run_id
            or terminal.get("from_frame") != "robot_flange"
            or terminal.get("to_frame") != "template_base"
            or terminal.get("sunrise_reference_frame_path")
            != expected_reference_frame_path
        ):
            raise ValueError("stream-end packet provenance is invalid")
        terminal_sequence = terminal.get("sequence")
        if (
            isinstance(terminal_sequence, bool)
            or not isinstance(terminal_sequence, int)
            or previous_sequence is None
            or terminal_sequence <= previous_sequence
        ):
            raise ValueError("stream-end packet sequence must follow the final pose")
        for field in ("sender_monotonic_ns", "sender_wall_timestamp_ms"):
            timestamp = terminal.get(field)
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp < 0
            ):
                raise ValueError(f"stream-end packet requires non-negative {field}")
        terminal_delta = terminal_sequence - previous_sequence
        terminal_loss = max(0, terminal_delta - 1)
        if (
            terminal.get("sequence_delta") != terminal_delta
            or terminal.get("estimated_packets_lost") != terminal_loss
        ):
            raise ValueError("stream-end packet loss evidence is inconsistent")
        packet_loss_count += terminal_loss
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        error = str(exc)
    ok = error is None and bool(records)
    return _check(
        "robot_pose_stream",
        ok,
        (
            "The raw robot-pose stream is nonempty and uses the current packet contract."
            if ok
            else "The raw robot-pose stream does not satisfy the current packet contract."
        ),
        path=path.as_posix(),
        pose_count=len(records),
        robot_pose_packet_loss_count=(packet_loss_count if error is None else None),
        terminal_packet_loss_audited=error is None,
        error=error,
    )


def _process_check(
    processes: list[Mapping[str, Any]], *, expected_sensor_count: int
) -> dict[str, Any]:
    receiver = [item for item in processes if item.get("role") == "robot_pose_receiver"]
    sensors = [item for item in processes if item.get("role") == "sensor_capture"]
    receiver_ok = len(receiver) == 1 and receiver[0].get("status") == "succeeded"
    completed_sensors = [
        item
        for item in sensors
        if item.get("status") == "succeeded"
        or (
            item.get("status") == "stopped"
            and item.get("termination_reason") == "stopped_after_receiver_exit"
        )
    ]
    clean_retries = [
        item
        for item in sensors
        if item not in completed_sensors
        and item.get("output_mutated") is False
        and item.get("termination_reason")
        in {
            "startup_spawn_failed",
            "startup_exit_retry",
            "startup_readiness_timeout_retry",
        }
    ]
    sensors_ok = len(completed_sensors) == expected_sensor_count and len(sensors) == (
        len(completed_sensors) + len(clean_retries)
    )
    sensors_ok = sensors_ok and all(
        item.get("status") == "succeeded"
        or (
            item.get("status") == "stopped"
            and item.get("termination_reason") == "stopped_after_receiver_exit"
        )
        for item in completed_sensors
    )
    released = bool(processes) and all(
        item.get("status") not in {"starting", "running"} and bool(item.get("ended_at"))
        for item in processes
    )
    ok = receiver_ok and sensors_ok and released
    return _check(
        "child_processes_and_resources",
        ok,
        (
            "Capture children completed and all local resources were released."
            if ok
            else "Capture children did not complete cleanly or remain unreleased."
        ),
        receiver_count=len(receiver),
        sensor_count=len(sensors),
        expected_sensor_count=expected_sensor_count,
        completed_sensor_count=len(completed_sensors),
        clean_retry_count=len(clean_retries),
        receiver_ok=receiver_ok,
        sensors_ok=sensors_ok,
        resources_released=released,
    )


def build_capture_completion(
    run_root: str | Path,
    config: Mapping[str, Any],
    processes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate current raw acquisition evidence after all children have stopped."""

    root = Path(run_root)
    enabled_sensors = [
        sensor
        for sensor in config["capture"]["sensors"]
        if isinstance(sensor, Mapping) and sensor.get("enabled", True) is True
    ]
    configured_resolution = str(config["capture"]["resolution"])
    expected_image_size = capture_resolution_image_size(configured_resolution)
    robot_pose = config["frames"]["robot_pose"]
    checks = [
        _sensor_check(
            root,
            sensor,
            configured_resolution=configured_resolution,
            expected_image_size=expected_image_size,
        )
        for sensor in enabled_sensors
    ]
    checks.append(
        _robot_pose_check(
            root,
            run_id=str(config["run_id"]),
            expected_reference_frame_path=str(
                robot_pose["sunrise_reference_frame_path"]
            ),
        )
    )
    checks.append(_process_check(processes, expected_sensor_count=len(enabled_sensors)))
    errors = [check for check in checks if check["status"] == "error"]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if not errors else "error",
        "enabled_sensor_count": len(enabled_sensors),
        "checks": checks,
        "error_count": len(errors),
    }
