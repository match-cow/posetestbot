"""Versioned calibration-target geometry and import contracts."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from posetestbot.io.atomic import atomic_write_json


SCHEMA_VERSION = "calibration_target.v3"
POSEGRIDGEN_SCHEMA_VERSION = "2.0"
SUPPORTED_TARGET_TYPES = ("aruco_grid",)
SUPPORTED_ARUCO_DICTIONARIES = frozenset(
    [
        *(
            f"DICT_{bits}X{bits}_{capacity}"
            for bits in range(4, 8)
            for capacity in (50, 100, 250, 1000)
        ),
        "DICT_ARUCO_ORIGINAL",
        "DICT_APRILTAG_16h5",
        "DICT_APRILTAG_25h9",
        "DICT_APRILTAG_36h10",
        "DICT_APRILTAG_36h11",
        "DICT_ARUCO_MIP_36h12",
    ]
)
DEFAULT_TARGET_SPEC = {
    "schema_version": SCHEMA_VERSION,
    "target_type": "aruco_grid",
    "dictionary": "DICT_5X5_50",
    "grid_size": [4, 3],
    "unit": "mm",
    "frame": {
        "name": "aruco_grid",
        "origin": "physical_outer_board_top_left",
        "axes": {"x": "right", "y": "down", "z": "into_board"},
    },
    "target_bounds": {"x_mm": 0.0, "y_mm": 0.0, "width_mm": 245.0, "height_mm": 180.0},
    "print_compensation": {
        "x_percent": 100.0,
        "y_percent": 100.0,
        "application": "pdf_only",
    },
    "markers": [
        {
            "id": row * 4 + column,
            "corners_mm": [
                [column * 65.0, row * 65.0, 0.0],
                [column * 65.0 + 50.0, row * 65.0, 0.0],
                [column * 65.0 + 50.0, row * 65.0 + 50.0, 0.0],
                [column * 65.0, row * 65.0 + 50.0, 0.0],
            ],
        }
        for row in range(3)
        for column in range(4)
    ],
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _size(value: str | list[Any] | tuple[Any, ...], *, label: str) -> list[int]:
    if isinstance(value, str):
        separator = "x" if "x" in value.lower() else ","
        parts = value.lower().split(separator)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ValueError(f"{label} must be a COLSxROWS string or two-item list")
    if len(parts) != 2:
        raise ValueError(f"{label} must contain exactly two values")
    try:
        parsed = [int(item) for item in parts]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} values must be integers") from exc
    if parsed[0] < 1 or parsed[1] < 1:
        raise ValueError(f"{label} values must be positive")
    return parsed


def _finite_float(value: Any, *, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _positive_float(value: Any, *, label: str) -> float:
    parsed = _finite_float(value, label=label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than 0")
    return parsed


def _validate_dictionary(value: Any) -> str:
    dictionary = str(value or "")
    if dictionary not in SUPPORTED_ARUCO_DICTIONARIES:
        raise ValueError(f"Unsupported ArUco dictionary: {dictionary!r}")
    return dictionary


def _dictionary_capacity(dictionary: str) -> int:
    import cv2

    try:
        dictionary_id = getattr(cv2.aruco, dictionary)
        value = cv2.aruco.getPredefinedDictionary(dictionary_id)
    except (AttributeError, cv2.error) as exc:
        raise ValueError(f"Installed OpenCV does not support {dictionary}") from exc
    return int(value.bytesList.shape[0])


def _normalized_grid_frame() -> dict[str, Any]:
    return {
        "name": "aruco_grid",
        "origin": "physical_outer_board_top_left",
        "axes": {"x": "right", "y": "down", "z": "into_board"},
    }


def _geometry_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": value["target_type"],
        "dictionary": value["dictionary"],
        "unit": value["unit"],
        "frame": value["frame"],
        "target_bounds": value["target_bounds"],
        "print_compensation": value["print_compensation"],
        "markers": value["markers"],
    }


def geometry_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(_geometry_payload(value))).hexdigest()


def target_identity(value: Mapping[str, Any]) -> dict[str, str | None]:
    normalized = normalize_calibration_target_spec(value)
    target_id = normalized.get("target_id")
    return {
        "target_id": str(target_id) if target_id is not None else None,
        "geometry_sha256": (
            str(normalized["geometry_sha256"])
            if normalized.get("geometry_sha256") is not None
            else None
        ),
    }


def validate_target_identity(
    evidence: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    *,
    label: str,
) -> None:
    expected = target_identity(target)
    if expected["geometry_sha256"] is None:
        return
    if not isinstance(evidence, Mapping):
        if expected["target_id"] is None:
            return
        raise ValueError(f"{label} is missing calibration-target provenance")
    if evidence.get("geometry_sha256") is None and expected["target_id"] is None:
        return
    if evidence.get("geometry_sha256") != expected["geometry_sha256"]:
        raise ValueError(f"{label} calibration-target geometry_sha256 mismatch")
    if (
        expected["target_id"] is not None
        and evidence.get("target_id") != expected["target_id"]
    ):
        raise ValueError(f"{label} calibration-target target_id mismatch")


def posegridgen_configuration_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalized_compensation(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    x_percent = _positive_float(source.get("x_percent", 100.0), label="x_percent")
    y_percent = _positive_float(source.get("y_percent", 100.0), label="y_percent")
    if x_percent > 200 or y_percent > 200:
        raise ValueError("Print compensation percentages must not exceed 200")
    application = str(source.get("application", "pdf_only"))
    if application != "pdf_only":
        raise ValueError("Print compensation must apply to the PDF only")
    return {
        "x_percent": x_percent,
        "y_percent": y_percent,
        "application": "pdf_only",
    }


def _normalized_bounds(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("target_bounds must be an object")
    bounds = {
        key: _finite_float(value.get(key), label=f"target_bounds.{key}")
        for key in ("x_mm", "y_mm", "width_mm", "height_mm")
    }
    if bounds["width_mm"] <= 0 or bounds["height_mm"] <= 0:
        raise ValueError("target_bounds width and height must be greater than 0")
    return bounds


def _normalized_markers(value: Any, *, capacity: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("aruco_grid markers must be a non-empty list")
    markers: list[dict[str, Any]] = []
    ids: set[int] = set()
    winding: float | None = None
    plane_z: float | None = None
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"Marker {index} must be an object")
        marker_id = int(item.get("id", -1))
        if marker_id < 0:
            raise ValueError(f"Marker {index} ID must be nonnegative")
        if marker_id in ids:
            raise ValueError(f"Duplicate marker ID: {marker_id}")
        if marker_id >= capacity:
            raise ValueError(
                f"Marker ID {marker_id} exceeds dictionary capacity {capacity}"
            )
        raw_corners = item.get("corners_mm")
        if not isinstance(raw_corners, list) or len(raw_corners) != 4:
            raise ValueError(f"Marker {marker_id} must have exactly four corners_mm")
        corners = np.asarray(raw_corners, dtype=float)
        if corners.shape != (4, 3) or not np.isfinite(corners).all():
            raise ValueError(f"Marker {marker_id} corners must be finite 3D points")
        marker_plane = float(corners[0, 2])
        if not np.allclose(corners[:, 2], marker_plane, atol=1e-9, rtol=0):
            raise ValueError(f"Marker {marker_id} corners must be coplanar")
        if plane_z is None:
            plane_z = marker_plane
        elif not math.isclose(plane_z, marker_plane, abs_tol=1e-9):
            raise ValueError("All marker corners must lie on one plane")
        area = 0.5 * sum(
            corners[i, 0] * corners[(i + 1) % 4, 1]
            - corners[(i + 1) % 4, 0] * corners[i, 1]
            for i in range(4)
        )
        if abs(area) <= 1e-9:
            raise ValueError(f"Marker {marker_id} corners are degenerate")
        minimum_xy = corners[:, :2].min(axis=0)
        maximum_xy = corners[:, :2].max(axis=0)
        expected_xy = np.asarray(
            [
                [minimum_xy[0], minimum_xy[1]],
                [maximum_xy[0], minimum_xy[1]],
                [maximum_xy[0], maximum_xy[1]],
                [minimum_xy[0], maximum_xy[1]],
            ]
        )
        if not np.allclose(corners[:, :2], expected_xy, atol=1e-9, rtol=0):
            raise ValueError(
                f"Marker {marker_id} corners must use consistent winding and be "
                "ordered top-left, top-right, bottom-right, bottom-left"
            )
        current_winding = math.copysign(1.0, area)
        if current_winding < 0:
            raise ValueError(
                f"Marker {marker_id} corners must use consistent winding and be "
                "ordered top-left, top-right, bottom-right, bottom-left"
            )
        if winding is None:
            winding = current_winding
        elif winding != current_winding:
            raise ValueError("Marker corners must use consistent winding")
        ids.add(marker_id)
        markers.append(
            {
                "id": marker_id,
                "corners_mm": [
                    [float(component) for component in point]
                    for point in corners.tolist()
                ],
            }
        )
    return markers


def _validate_bounds_containment(
    markers: Sequence[Mapping[str, Any]], bounds: Mapping[str, float]
) -> None:
    x0, y0 = bounds["x_mm"], bounds["y_mm"]
    x1 = x0 + bounds["width_mm"]
    y1 = y0 + bounds["height_mm"]
    tolerance = 1e-8
    for marker in markers:
        for point in marker["corners_mm"]:
            if not (
                x0 - tolerance <= point[0] <= x1 + tolerance
                and y0 - tolerance <= point[1] <= y1 + tolerance
            ):
                raise ValueError(
                    f"Marker {marker['id']} corner lies outside target_bounds"
                )


def _normalized_placement(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Calibration target placement must be an object")
    if value.get("from") != "aruco_grid" or value.get("to") != "template_base":
        raise ValueError(
            "Calibration target placement must map aruco_grid to template_base"
        )
    quaternion = value.get("rotation_quaternion_wxyz")
    translation = value.get("translation_mm")
    if not isinstance(quaternion, list | tuple) or len(quaternion) != 4:
        raise ValueError(
            "Calibration target placement quaternion must contain four values"
        )
    if not isinstance(translation, list | tuple) or len(translation) != 3:
        raise ValueError(
            "Calibration target placement translation must contain three values"
        )
    normalized_quaternion = [
        _finite_float(item, label="placement.rotation_quaternion_wxyz")
        for item in quaternion
    ]
    normalized_translation = [
        _finite_float(item, label="placement.translation_mm") for item in translation
    ]
    if not math.isclose(
        sum(item * item for item in normalized_quaternion),
        1.0,
        abs_tol=1e-8,
    ):
        raise ValueError("Calibration target placement quaternion must be normalized")
    placement = {
        "from": "aruco_grid",
        "to": "template_base",
        "rotation_quaternion_wxyz": normalized_quaternion,
        "translation_mm": normalized_translation,
    }
    for key in ("source", "source_base_frame_interpretation", "unit"):
        if key in value:
            placement[key] = value[key]
    return placement


def _normalize_v3(value: Mapping[str, Any]) -> dict[str, Any]:
    if str(value.get("target_type", "")) != "aruco_grid":
        raise ValueError("calibration_target.v3 currently supports only aruco_grid")
    if str(value.get("unit", "")) != "mm":
        raise ValueError("Calibration target geometry must use millimetres")
    dictionary = _validate_dictionary(value.get("dictionary"))
    capacity = _dictionary_capacity(dictionary)
    markers = _normalized_markers(value.get("markers"), capacity=capacity)
    bounds = _normalized_bounds(value.get("target_bounds"))
    _validate_bounds_containment(markers, bounds)
    frame = value.get("frame")
    expected_frame = _normalized_grid_frame()
    if not isinstance(frame, Mapping) or dict(frame) != expected_frame:
        raise ValueError(
            "Calibration target frame must use the physical board top-left "
            "aruco_grid convention"
        )
    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target_type": "aruco_grid",
        "dictionary": dictionary,
        "unit": "mm",
        "frame": _normalized_grid_frame(),
        "target_bounds": bounds,
        "print_compensation": _normalized_compensation(value.get("print_compensation")),
        "markers": markers,
    }
    for key in (
        "target_id",
        "display_name",
        "grid_size",
        "generator_source",
        "posegridgen",
        "placement",
        "source_schema_version",
    ):
        if key in value:
            normalized[key] = value[key]
    if "grid_size" in normalized:
        normalized["grid_size"] = _size(normalized["grid_size"], label="grid_size")
        if math.prod(normalized["grid_size"]) != len(markers):
            raise ValueError("Calibration target grid_size does not match marker count")
    if "placement" in normalized:
        normalized["placement"] = _normalized_placement(normalized["placement"])
    posegridgen = normalized.get("posegridgen")
    if posegridgen is not None:
        if not isinstance(posegridgen, Mapping):
            raise ValueError("posegridgen provenance must be an object")
        configuration = posegridgen.get("configuration")
        expected_hash = str(posegridgen.get("configuration_hash", ""))
        if not isinstance(configuration, Mapping):
            raise ValueError("posegridgen.configuration must be an object")
        actual_hash = posegridgen_configuration_sha256(configuration)
        if actual_hash != expected_hash:
            raise ValueError(
                "PoseGridGen configuration hash does not match configuration"
            )
    actual_geometry_hash = geometry_sha256(normalized)
    supplied_hash = value.get("geometry_sha256")
    if supplied_hash is not None and str(supplied_hash) != actual_geometry_hash:
        raise ValueError("Calibration target geometry_sha256 does not match geometry")
    normalized["geometry_sha256"] = actual_geometry_hash
    return normalized


def normalize_calibration_target_spec(
    value: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the current exact calibration-target geometry contract."""

    target = DEFAULT_TARGET_SPEC if value is None else value
    if not isinstance(target, Mapping):
        raise ValueError("Calibration target must be a JSON object")
    if target.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Calibration target schema must be {SCHEMA_VERSION!r}")
    return _normalize_v3(target)


def target_from_posegridgen_manifest(
    source: Mapping[str, Any],
    *,
    target_id: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    if str(source.get("schema_version")) != POSEGRIDGEN_SCHEMA_VERSION:
        raise ValueError("PoseGridGen source schema_version must be '2.0'")
    configuration = source.get("request")
    if not isinstance(configuration, Mapping):
        raise ValueError("PoseGridGen source requires a request object")
    board = configuration.get("board")
    if not isinstance(board, Mapping) or board.get("type") != "aruco":
        raise ValueError("Only PoseGridGen ArUco boards can be imported")
    expected_configuration_hash = posegridgen_configuration_sha256(configuration)
    if source.get("configuration_hash") != expected_configuration_hash:
        raise ValueError("PoseGridGen configuration hash does not match request")
    features = source.get("features")
    if not isinstance(features, list):
        raise ValueError("PoseGridGen source requires features")
    compensation = _normalized_compensation(
        {**dict(configuration.get("print_compensation", {})), "application": "pdf_only"}
    )
    scale = np.asarray(
        [compensation["x_percent"] / 100.0, compensation["y_percent"] / 100.0, 1.0]
    )
    # PoseGridGen features describe the precompensated PDF. The printer is
    # expected to restore the nominal dimensions; PnP needs that physical board.
    markers = []
    for item in features:
        if not isinstance(item, Mapping) or item.get("kind") != "marker":
            continue
        corners = np.asarray(item.get("corners_mm"), dtype=float)
        if corners.shape != (4, 3) or not np.isfinite(corners).all():
            raise ValueError("PoseGridGen marker corners must be finite 4x3 points")
        markers.append(
            {"id": item.get("id"), "corners_mm": (corners / scale).round(9).tolist()}
        )
    target_bounds = source.get("target_bounds")
    if not isinstance(target_bounds, Mapping):
        raise ValueError("PoseGridGen source requires target_bounds")
    target: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target_type": "aruco_grid",
        "dictionary": board.get("dictionary"),
        "grid_size": [board.get("columns"), board.get("rows")],
        "unit": "mm",
        "frame": _normalized_grid_frame(),
        "target_bounds": {
            "x_mm": 0.0,
            "y_mm": 0.0,
            "width_mm": _positive_float(target_bounds.get("width_mm"), label="width_mm")
            / scale[0],
            "height_mm": _positive_float(
                target_bounds.get("height_mm"), label="height_mm"
            )
            / scale[1],
        },
        "print_compensation": compensation,
        "markers": markers,
        "posegridgen": {
            "revision": None,
            "configuration_hash": expected_configuration_hash,
            "configuration": dict(configuration),
        },
    }
    if target_id is not None:
        target["target_id"] = target_id
    if display_name is not None:
        target["display_name"] = display_name
    return _normalize_v3(target)


def load_calibration_target_spec(path: str | Path) -> dict[str, Any]:
    with open(path, "r") as file:
        value = json.load(file)
    if not isinstance(value, Mapping):
        raise ValueError(f"Calibration target spec must be a JSON object: {path}")
    schema = value.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise ValueError(f"Calibration target schema must be {SCHEMA_VERSION!r}")
    return normalize_calibration_target_spec(value)


def write_calibration_target(target: Mapping[str, Any], path: str | Path) -> Path:
    normalized = normalize_calibration_target_spec(target)
    return atomic_write_json(Path(path), normalized)


def opencv_grid_board(target: Mapping[str, Any]):
    """Construct a generic OpenCV Board from authoritative marker corners."""

    import cv2

    normalized = normalize_calibration_target_spec(target)
    if normalized["target_type"] != "aruco_grid":
        raise ValueError("OpenCV Board construction requires an aruco_grid target")
    dictionary_name = normalized["dictionary"]
    try:
        dictionary_id = getattr(cv2.aruco, dictionary_name)
        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        object_points = [
            np.asarray(marker["corners_mm"], dtype=np.float32)
            for marker in normalized["markers"]
        ]
        ids = np.asarray(
            [marker["id"] for marker in normalized["markers"]], dtype=np.int32
        )
        board = cv2.aruco.Board(object_points, dictionary, ids)
    except (AttributeError, cv2.error) as exc:
        raise ValueError(
            "Installed opencv-python lacks the required cv2.aruco.Board APIs"
        ) from exc
    return dictionary, board
