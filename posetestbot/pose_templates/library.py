"""Exact full-pose previews and immutable pose-template bundles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import fcntl
import numpy as np

from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json
from posetestbot.pose_templates.adapter import (
    ADAPTER_VERSION,
    POSETEMPLATECREATOR_REVISION,
    load_posetemplatecreator_backend,
)
from posetestbot.pose_templates.catalog import (
    _mutation_lock,
    _sha256,
    default_catalog_root,
    default_working_data_root,
    get_catalog_object,
    load_catalog,
    utc_now_iso,
)
from posetestbot.pose_templates.orientations import (
    ensure_catalog_orientation_analysis,
    select_orientation,
)
from posetestbot.pose_templates.transforms import (
    matrix_from_xyz_rpy,
    transform_record,
    validate_rigid_matrix,
)


BUNDLE_SCHEMA_VERSION = "pose_template_bundle.v1"
PREVIEW_SCHEMA_VERSION = "pose_template_preview.v1"
THUMBNAIL_SCHEMA_VERSION = "pose_template_thumbnail.v1"
LIBRARY_DIRECTORY = "pose_templates"
BUNDLE_MANIFEST = "pose_template_bundle.json"
TEMPLATE_PDF = "pose_template.pdf"
PREVIEW_JSON = "pose_template_preview.json"
THUMBNAIL_JSON = "pose_template_thumbnail.json"
ARCHIVE_STATE = "archive_state.json"
DELETION_DIRECTORY = ".deleted"
DELETION_TOMBSTONE_SCHEMA_VERSION = "pose_template_deletion_tombstone.v1"
DELETION_TOMBSTONE_MAX_JSON_BYTES = 64 * 1024
MAX_INSTANCES = 200
THUMBNAIL_MAX_CONTOURS = MAX_INSTANCES * 2
THUMBNAIL_MAX_POINTS = 4096
THUMBNAIL_MAX_POINTS_PER_CONTOUR = 48
THUMBNAIL_MAX_JSON_BYTES = 2 * 1024 * 1024
ARCHIVE_STATE_MAX_JSON_BYTES = 64 * 1024
CARD_MANIFEST_MAX_JSON_BYTES = 4 * 1024 * 1024
SUPPORTED_SOURCE_REVISIONS = {POSETEMPLATECREATOR_REVISION}
_LOCK = threading.RLock()
_LIBRARY_LOCK_STATE = threading.local()


@contextmanager
def template_library_lock(library_root: str | Path):
    """Serialize lifecycle changes and snapshots across worker processes."""

    with _LOCK:
        library = Path(library_root).resolve()
        library.mkdir(parents=True, exist_ok=True)
        lock_path = library / ".pose_template_library.lock"
        held = getattr(_LIBRARY_LOCK_STATE, "locks", None)
        if held is None:
            held = {}
            _LIBRARY_LOCK_STATE.locks = held
        depth = int(held.get(lock_path, 0))
        if depth:
            held[lock_path] = depth + 1
            try:
                yield
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
                    yield
                finally:
                    held.pop(lock_path, None)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def default_template_library_root() -> Path:
    return default_working_data_root() / LIBRARY_DIRECTORY


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _uuid(value: Any, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _bundle_file(bundle_dir: Path, relative_value: Any, *, label: str) -> Path:
    """Resolve a regular bundle file without following any internal symlink."""

    relative = Path(str(relative_value))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} path must be bundle-relative")
    cursor = bundle_dir
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"{label} path must not contain symlinks")
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(bundle_dir.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"{label} path escapes or is missing from the bundle") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _bundle_directory(library: Path, template_uuid: str) -> Path:
    """Resolve one UUID-addressed bundle directory without following symlinks."""

    library = library.resolve()
    bundle = library / _uuid(template_uuid, label="template_uuid")
    if bundle.is_symlink() or not bundle.is_dir():
        raise FileNotFoundError(f"Pose-template bundle does not exist: {bundle}")
    resolved = bundle.resolve(strict=True)
    try:
        resolved.relative_to(library)
    except ValueError as exc:
        raise ValueError("Pose-template bundle escapes library") from exc
    return resolved


def _read_bounded_json(path: Path, *, maximum_bytes: int, label: str) -> Any:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ValueError(f"{label} exceeds its {maximum_bytes}-byte limit")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _deletion_paths(library: Path, template_uuid: str) -> tuple[Path, Path, Path]:
    opaque_id = _uuid(template_uuid, label="template_uuid")
    deletion_root = library.resolve() / DELETION_DIRECTORY
    return (
        deletion_root,
        deletion_root / f"{opaque_id}.json",
        deletion_root / f"{opaque_id}.assets",
    )


def _assert_template_uuid_not_retired(library: Path, template_uuid: str) -> None:
    deletion_root, tombstone_path, _cleanup_path = _deletion_paths(
        library, template_uuid
    )
    if deletion_root.is_symlink():
        raise ValueError("Pose-template deletion records must not be symlinked")
    if tombstone_path.is_symlink() or tombstone_path.exists():
        raise ValueError(
            f"Pose-template UUID has been permanently retired: {template_uuid}"
        )


def _load_archive_state_lightweight(bundle_dir: Path) -> dict[str, Any]:
    archive_path = _bundle_file(bundle_dir, ARCHIVE_STATE, label="Bundle archive state")
    archive = _read_bounded_json(
        archive_path,
        maximum_bytes=ARCHIVE_STATE_MAX_JSON_BYTES,
        label="Bundle archive state",
    )
    if not isinstance(archive, Mapping) or archive.get("state") not in {
        "active",
        "archived",
    }:
        raise ValueError("Pose-template archive state is invalid")
    return dict(archive)


def _declared_bundle_files(manifest: Mapping[str, Any]) -> set[str]:
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("Pose-template bundle file metadata is invalid")
    declared = {BUNDLE_MANIFEST, ARCHIVE_STATE}
    for name in ("pdf", "preview"):
        value = files.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Bundle {name} path is invalid")
        declared.add(value)
    thumbnail = files.get("thumbnail")
    if thumbnail is not None:
        if not isinstance(thumbnail, str) or not thumbnail:
            raise ValueError("Bundle thumbnail path is invalid")
        declared.add(thumbnail)
    assets = files.get("assets")
    if not isinstance(assets, Mapping) or not assets:
        raise ValueError("Pose-template bundle asset metadata is invalid")
    for instance_files in assets.values():
        if not isinstance(instance_files, Mapping) or not instance_files:
            raise ValueError("Pose-template instance asset metadata is invalid")
        for record in instance_files.values():
            if not isinstance(record, Mapping):
                raise ValueError("Pose-template asset record is invalid")
            value = record.get("path")
            if not isinstance(value, str) or not value:
                raise ValueError("Pose-template asset path is invalid")
            declared.add(value)
    return declared


def _validate_bundle_tree(bundle_dir: Path, declared_files: set[str]) -> None:
    """Reject undeclared entries and every symlink in an immutable bundle."""

    root = bundle_dir.resolve()
    normalized_files: set[str] = set()
    expected_directories: set[str] = set()
    for value in declared_files:
        path = _bundle_file(bundle_dir, value, label="Bundle declared file")
        relative = path.relative_to(root).as_posix()
        normalized_files.add(relative)
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError("Pose-template bundle must not contain symlinks")
            if relative not in expected_directories:
                raise ValueError(
                    f"Pose-template bundle contains an undeclared directory: {relative}"
                )
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError("Pose-template bundle must not contain symlinks")
            if relative not in normalized_files:
                raise ValueError(
                    f"Pose-template bundle contains an undeclared file: {relative}"
                )


def _finite_pose(
    value: Mapping[str, Any], keys: tuple[str, ...], *, label: str
) -> dict[str, float]:
    pose = {key: float(value.get(key, 0.0)) for key in keys}
    if not np.isfinite(list(pose.values())).all():
        raise ValueError(f"{label} must be finite")
    return pose


def _points(contours: list[Any]) -> list[list[dict[str, float]]]:
    """Normalize current upstream ContourV2 values."""

    result: list[list[dict[str, float]]] = []
    for contour in contours:
        if not isinstance(contour, Mapping):
            raise ValueError("Stable orientation contour must be a ContourV2 object")
        raw_points = contour.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 3:
            raise ValueError("Stable orientation contains an invalid closed contour")
        points = [
            {"x_mm": float(point["x_mm"]), "y_mm": float(point["y_mm"])}
            for point in raw_points
        ]
        if not np.isfinite(
            [coordinate for point in points for coordinate in point.values()]
        ).all():
            raise ValueError("Stable orientation contour must be finite")
        result.append(points)
    if not result:
        raise ValueError("Stable orientation does not contain a closed contour")
    return result


def _preview_points(contours: Any) -> list[list[dict[str, float]]]:
    """Validate the exact point-list contour shape stored in current previews."""

    if not isinstance(contours, list):
        raise ValueError("Template preview contours must be a list")
    result: list[list[dict[str, float]]] = []
    for contour in contours:
        if not isinstance(contour, list) or len(contour) < 3:
            raise ValueError("Template preview contains an invalid closed contour")
        points: list[dict[str, float]] = []
        for point in contour:
            if not isinstance(point, Mapping):
                raise ValueError("Template preview contour point must be an object")
            try:
                normalized = {
                    "x_mm": float(point["x_mm"]),
                    "y_mm": float(point["y_mm"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Template preview contour point must contain numeric x_mm/y_mm"
                ) from exc
            if not np.isfinite(list(normalized.values())).all():
                raise ValueError("Template preview contour must be finite")
            points.append(normalized)
        result.append(points)
    if not result:
        raise ValueError("Template preview does not contain a closed contour")
    return result


def _transform_planar_contours(
    contours: list[Any], planar: np.ndarray
) -> list[list[dict[str, float]]]:
    transformed: list[list[dict[str, float]]] = []
    for contour in _points(contours):
        output: list[dict[str, float]] = []
        for point in contour:
            placed = planar @ np.asarray(
                [point["x_mm"], point["y_mm"], 0.0, 1.0], dtype=float
            )
            output.append({"x_mm": float(placed[0]), "y_mm": float(placed[1])})
        transformed.append(output)
    return transformed


def _pdf_compensated_contours(
    contours: list[list[dict[str, float]]],
    *,
    page_width_mm: float,
    page_height_mm: float,
    origin_margin_mm: float,
    scale_x: float,
    scale_y: float,
) -> list[list[dict[str, float]]]:
    """Mirror upstream's page-centred printer compensation exactly.

    PoseTemplateCreator scales all meaningful template content about the
    physical page centre while leaving the ISO MediaBox/page boundary nominal.
    Points here remain expressed relative to the printable-work-area origin.
    """

    center_x = page_width_mm / 2.0
    center_y = page_height_mm / 2.0
    return [
        [
            {
                "x_mm": center_x
                + scale_x * (origin_margin_mm + point["x_mm"] - center_x)
                - origin_margin_mm,
                "y_mm": center_y
                + scale_y * (origin_margin_mm + point["y_mm"] - center_y)
                - origin_margin_mm,
            }
            for point in contour
        ]
        for contour in contours
    ]


def _contour_bounds(
    contours: list[list[dict[str, float]]],
) -> dict[str, float]:
    points = [point for contour in contours for point in contour]
    if not points:
        raise ValueError("Pose-template contour set must not be empty")
    return {
        "min_x_mm": min(point["x_mm"] for point in points),
        "min_y_mm": min(point["y_mm"] for point in points),
        "max_x_mm": max(point["x_mm"] for point in points),
        "max_y_mm": max(point["y_mm"] for point in points),
    }


def _contour_area(contour: list[dict[str, float]]) -> float:
    """Return absolute signed-polygon area for stable primary selection."""

    return abs(
        sum(
            point["x_mm"] * contour[(index + 1) % len(contour)]["y_mm"]
            - contour[(index + 1) % len(contour)]["x_mm"] * point["y_mm"]
            for index, point in enumerate(contour)
        )
        / 2.0
    )


def _decimate_contour(
    contour: list[dict[str, float]], limit: int
) -> list[dict[str, float]]:
    """Evenly sample a closed polygon without duplicating its first point."""

    if len(contour) <= limit:
        return contour
    indices = [(index * len(contour)) // limit for index in range(limit)]
    return [contour[index] for index in indices]


def build_template_thumbnail(
    preview: Mapping[str, Any], *, template_uuid: str | None = None
) -> dict[str, Any]:
    """Derive the bounded footprint used by library and run-selection cards.

    Each instance contributes its largest compensated contour before any
    secondary contour is considered. Secondary contours (including holes) are
    then admitted round-robin by their source order. Oversized contours are
    evenly decimated. This preserves useful object identity for all supported
    instances while keeping a single card response structurally bounded.
    """

    if preview.get("schema_version") != PREVIEW_SCHEMA_VERSION:
        raise ValueError(f"Template preview schema must be {PREVIEW_SCHEMA_VERSION}")
    source_instances = preview.get("instances")
    if not isinstance(source_instances, list) or not source_instances:
        raise ValueError("Template preview must contain at least one instance")
    if len(source_instances) > MAX_INSTANCES:
        raise ValueError(
            f"Template thumbnail supports at most {MAX_INSTANCES} instances"
        )

    prepared: list[tuple[Mapping[str, Any], list[list[dict[str, float]]], int]] = []
    source_contour_count = 0
    source_point_count = 0
    for item in source_instances:
        if not isinstance(item, Mapping):
            raise ValueError("Template preview instance must be an object")
        contours = _preview_points(item.get("compensated_contours"))
        primary_index = max(
            range(len(contours)),
            key=lambda index: (_contour_area(contours[index]), -index),
        )
        prepared.append((item, contours, primary_index))
        source_contour_count += len(contours)
        source_point_count += sum(len(contour) for contour in contours)

    primary_limit = min(
        THUMBNAIL_MAX_POINTS_PER_CONTOUR,
        max(3, THUMBNAIL_MAX_POINTS // len(prepared)),
    )
    remaining_points = THUMBNAIL_MAX_POINTS
    output_instances: list[dict[str, Any]] = []
    included_source_indices: list[list[int]] = []
    for item, contours, primary_index in prepared:
        primary = _decimate_contour(
            contours[primary_index], min(primary_limit, remaining_points)
        )
        remaining_points -= len(primary)
        included_source_indices.append([primary_index])
        catalog = item.get("catalog", {})
        catalog_summary = {
            key: catalog[key]
            for key in ("catalog_uuid", "name", "obj_id")
            if isinstance(catalog, Mapping) and key in catalog
        }
        output_instances.append(
            {
                "instance_uuid": _uuid(
                    item.get("instance_uuid"), label="instance_uuid"
                ),
                "catalog_uuid": catalog_summary.get("catalog_uuid"),
                "orientation_id": item.get("orientation_id"),
                "catalog": catalog_summary,
                "compensated_contours": [primary],
                "primary_contour_source_index": primary_index,
            }
        )

    secondary_indices = [
        [index for index in range(len(contours)) if index != primary_index]
        for _, contours, primary_index in prepared
    ]
    maximum_secondary_depth = max(
        (len(value) for value in secondary_indices), default=0
    )
    for depth in range(maximum_secondary_depth):
        for instance_index, (_, contours, _) in enumerate(prepared):
            if (
                depth >= len(secondary_indices[instance_index])
                or sum(len(value) for value in included_source_indices)
                >= THUMBNAIL_MAX_CONTOURS
                or remaining_points < 3
            ):
                continue
            source_index = secondary_indices[instance_index][depth]
            contour = contours[source_index]
            bounded = _decimate_contour(
                contour,
                min(THUMBNAIL_MAX_POINTS_PER_CONTOUR, remaining_points),
            )
            output_instances[instance_index]["compensated_contours"].append(bounded)
            included_source_indices[instance_index].append(source_index)
            remaining_points -= len(bounded)

    included_contour_count = sum(
        len(item["compensated_contours"]) for item in output_instances
    )
    included_point_count = sum(
        len(contour)
        for item in output_instances
        for contour in item["compensated_contours"]
    )
    for index, item in enumerate(output_instances):
        source_contours = prepared[index][1]
        item_contours = item["compensated_contours"]
        item["approximation"] = {
            "truncated": (
                len(item_contours) != len(source_contours)
                or sum(len(contour) for contour in item_contours)
                != sum(len(contour) for contour in source_contours)
            ),
            "source_contours": len(source_contours),
            "included_contours": len(item_contours),
            "source_points": sum(len(contour) for contour in source_contours),
            "included_points": sum(len(contour) for contour in item_contours),
        }

    configuration = preview.get("configuration", {})
    if not isinstance(configuration, Mapping):
        configuration = {}
    page_configuration = configuration.get("page", {})
    if not isinstance(page_configuration, Mapping):
        page_configuration = {}
    print_compensation = configuration.get("print_compensation", {})
    if not isinstance(print_compensation, Mapping):
        print_compensation = {}
    page = preview.get("page", {})
    if not isinstance(page, Mapping):
        page = {}
    truncated = (
        included_contour_count != source_contour_count
        or included_point_count != source_point_count
    )
    result = {
        "schema_version": THUMBNAIL_SCHEMA_VERSION,
        "template_uuid": _uuid(template_uuid, label="template_uuid")
        if template_uuid
        else None,
        "valid": bool(preview.get("valid", False)),
        "display_name": configuration.get("display_name"),
        "description": configuration.get("description"),
        "source": preview.get("source"),
        "page": {
            "width_mm": float(page.get("width_mm")),
            "height_mm": float(page.get("height_mm")),
        },
        "configuration": {
            "page": {
                "size": page_configuration.get("size"),
                "orientation": page_configuration.get("orientation"),
                "origin_from_lower_left_mm": list(
                    page_configuration.get("origin_from_lower_left_mm", [15.0, 15.0])
                ),
                "print_compensation_origin": page_configuration.get(
                    "print_compensation_origin", "page_center"
                ),
            },
            "print_compensation": {
                "x_scale": float(print_compensation.get("x_scale", 1.0)),
                "y_scale": float(print_compensation.get("y_scale", 1.0)),
            },
        },
        "instances": output_instances,
        "approximation": {
            "approximate": truncated,
            "truncated": truncated,
            "strategy": "largest-primary-then-round-robin-even-decimation",
            "source_contours": source_contour_count,
            "included_contours": included_contour_count,
            "source_points": source_point_count,
            "included_points": included_point_count,
            "limits": {
                "instances": MAX_INSTANCES,
                "contours": THUMBNAIL_MAX_CONTOURS,
                "points": THUMBNAIL_MAX_POINTS,
                "points_per_contour": THUMBNAIL_MAX_POINTS_PER_CONTOUR,
            },
        },
    }
    return _validate_template_thumbnail_content(result, template_uuid=template_uuid)


def _validate_template_thumbnail_content(
    value: Any, *, template_uuid: str | None
) -> dict[str, Any]:
    """Enforce the endpoint's bounded structure, not only its file hash."""

    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != THUMBNAIL_SCHEMA_VERSION
    ):
        raise ValueError(
            f"Template thumbnail schema must be {THUMBNAIL_SCHEMA_VERSION}"
        )
    if template_uuid and value.get("template_uuid") != _uuid(
        template_uuid, label="template_uuid"
    ):
        raise ValueError("Template thumbnail UUID does not match its bundle")
    instances = value.get("instances")
    if not isinstance(instances, list) or not 1 <= len(instances) <= MAX_INSTANCES:
        raise ValueError(
            f"Template thumbnail must contain 1 to {MAX_INSTANCES} instances"
        )
    contour_count = 0
    point_count = 0
    for item in instances:
        if not isinstance(item, Mapping):
            raise ValueError("Template thumbnail instance must be an object")
        _uuid(item.get("instance_uuid"), label="instance_uuid")
        contours = item.get("compensated_contours")
        if not isinstance(contours, list) or not contours:
            raise ValueError("Template thumbnail must retain one contour per instance")
        contour_count += len(contours)
        for contour in contours:
            if (
                not isinstance(contour, list)
                or not 3 <= len(contour) <= THUMBNAIL_MAX_POINTS_PER_CONTOUR
            ):
                raise ValueError("Template thumbnail contains an invalid contour")
            try:
                coordinates = [
                    float(point[axis])
                    for point in contour
                    if isinstance(point, Mapping)
                    for axis in ("x_mm", "y_mm")
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "Template thumbnail contour must contain numeric points"
                ) from exc
            if (
                len(coordinates) != len(contour) * 2
                or not np.isfinite(coordinates).all()
            ):
                raise ValueError(
                    "Template thumbnail contour must contain finite points"
                )
            point_count += len(contour)
    if contour_count > THUMBNAIL_MAX_CONTOURS:
        raise ValueError("Template thumbnail exceeds its contour limit")
    if point_count > THUMBNAIL_MAX_POINTS:
        raise ValueError("Template thumbnail exceeds its point limit")
    approximation = value.get("approximation")
    if not isinstance(approximation, Mapping):
        raise ValueError("Template thumbnail approximation evidence is missing")
    limits = approximation.get("limits", {})
    expected_limits = {
        "instances": MAX_INSTANCES,
        "contours": THUMBNAIL_MAX_CONTOURS,
        "points": THUMBNAIL_MAX_POINTS,
        "points_per_contour": THUMBNAIL_MAX_POINTS_PER_CONTOUR,
    }
    if limits != expected_limits:
        raise ValueError("Template thumbnail limits are missing or inconsistent")
    if (
        approximation.get("included_contours") != contour_count
        or approximation.get("included_points") != point_count
    ):
        raise ValueError("Template thumbnail included counts are inconsistent")
    page = value.get("page", {})
    configuration = value.get("configuration", {})
    page_configuration = (
        configuration.get("page", {}) if isinstance(configuration, Mapping) else {}
    )
    compensation = (
        configuration.get("print_compensation", {})
        if isinstance(configuration, Mapping)
        else {}
    )
    origin = page_configuration.get("origin_from_lower_left_mm", [])
    numbers = [
        page.get("width_mm") if isinstance(page, Mapping) else None,
        page.get("height_mm") if isinstance(page, Mapping) else None,
        compensation.get("x_scale") if isinstance(compensation, Mapping) else None,
        compensation.get("y_scale") if isinstance(compensation, Mapping) else None,
        *(origin if isinstance(origin, list) else []),
    ]
    try:
        numeric = [float(number) for number in numbers]
    except (TypeError, ValueError) as exc:
        raise ValueError("Template thumbnail page metadata must be numeric") from exc
    if len(origin) != 2 or len(numeric) != 6 or not np.isfinite(numeric).all():
        raise ValueError("Template thumbnail page metadata must be finite")
    if numeric[0] <= 0 or numeric[1] <= 0 or numeric[2] <= 0 or numeric[3] <= 0:
        raise ValueError(
            "Template thumbnail page dimensions and scales must be positive"
        )
    return dict(value)


def _normalize_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    name = str(value.get("display_name", "")).strip()
    if not name or len(name) > 120:
        raise ValueError("Template display_name must contain 1 to 120 characters")
    description = value.get("description")
    if description is not None and len(str(description)) > 2000:
        raise ValueError("Template description must not exceed 2000 characters")
    page = value.get("page", {})
    if not isinstance(page, Mapping):
        raise ValueError("Template page must be an object")
    size = str(page.get("size", "A3"))
    orientation = str(page.get("orientation", "landscape"))
    if size not in {"A0", "A1", "A2", "A3", "A4"}:
        raise ValueError("Template page size must be A0, A1, A2, A3, or A4")
    if orientation not in {"portrait", "landscape"}:
        raise ValueError("Template orientation must be portrait or landscape")
    compensation = value.get("print_compensation", {})
    if not isinstance(compensation, Mapping):
        raise ValueError("print_compensation must be an object")
    scale_x = float(compensation.get("x_scale", 1.0))
    scale_y = float(compensation.get("y_scale", 1.0))
    if (
        not np.isfinite([scale_x, scale_y]).all()
        or not (0.5 <= scale_x <= 1.5)
        or not (0.5 <= scale_y <= 1.5)
    ):
        raise ValueError(
            "Print compensation factors must be finite and between 0.5 and 1.5"
        )
    instances = value.get("instances", [])
    if not isinstance(instances, list) or not instances:
        raise ValueError("Template must contain at least one object instance")
    if len(instances) > MAX_INSTANCES:
        raise ValueError(f"Template may contain at most {MAX_INSTANCES} instances")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(instances):
        if not isinstance(item, Mapping):
            raise ValueError(f"Template instance {index} must be an object")
        if not item.get("instance_uuid"):
            raise ValueError(
                f"Template instance {index} requires an immutable instance_uuid"
            )
        instance_uuid = _uuid(item.get("instance_uuid"), label="instance_uuid")
        if instance_uuid in seen:
            raise ValueError("Template instance UUIDs must be unique")
        seen.add(instance_uuid)
        catalog_uuid = _uuid(item.get("catalog_uuid"), label="catalog_uuid")
        pose = item.get("pose")
        if not isinstance(pose, Mapping):
            raise ValueError(f"Template instance {index} pose must be an object")
        orientation_id = str(item.get("orientation_id", "")).strip()
        if not orientation_id or len(orientation_id) > 128:
            raise ValueError(
                f"Template instance {index} requires a current orientation_id"
            )
        pose_value = _finite_pose(
            pose,
            ("x_mm", "y_mm", "rotation_deg"),
            label=f"Template instance {index} planar pose",
        )
        normalized.append(
            {
                "instance_uuid": instance_uuid,
                "catalog_uuid": catalog_uuid,
                "orientation_id": orientation_id,
                "placement_mode": "stable_orientation",
                "pose": pose_value,
            }
        )
    normalized.sort(key=lambda item: item["instance_uuid"])
    return {
        "display_name": name,
        "description": str(description).strip() if description else None,
        "page": {
            "size": size,
            "orientation": orientation,
            "origin_from_lower_left_mm": [15.0, 15.0],
            "print_compensation_origin": "page_center",
            "page_boundary_scaled": False,
            "template_content_scaled": True,
        },
        "print_compensation": {"x_scale": scale_x, "y_scale": scale_y},
        "instances": normalized,
    }


def build_template_preview(
    configuration: Mapping[str, Any], *, catalog_root: str | Path | None = None
) -> dict[str, Any]:
    """Build exact full-pose slice geometry without committing a template."""
    config = _normalize_configuration(configuration)
    catalog = load_catalog(catalog_root, verify_assets=False)
    records = {item["catalog_uuid"]: item for item in catalog["objects"]}
    root = Path(catalog["catalog_root"])
    backend = load_posetemplatecreator_backend()
    page_width, page_height = backend.constants.page_dimensions_mm(
        config["page"]["size"], config["page"]["orientation"]
    )
    origin_margin = float(backend.constants.ORIGIN_MARGIN_MM)
    scale_x = config["print_compensation"]["x_scale"]
    scale_y = config["print_compensation"]["y_scale"]
    preview_instances: list[dict[str, Any]] = []
    preview_meshes: dict[str, Any] = {}
    analyses: dict[str, dict[str, Any]] = {}
    layout_objects: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    compensation_issues: list[dict[str, Any]] = []
    for item in config["instances"]:
        record = records.get(item["catalog_uuid"])
        if record is None:
            raise ValueError(f"Unknown catalog object: {item['catalog_uuid']}")
        if record["state"] != "active":
            raise ValueError(f"Archived object cannot be added: {record['name']}")
        pose = item["pose"]
        try:
            analysis = analyses.get(item["catalog_uuid"])
            if analysis is None:
                analysis = ensure_catalog_orientation_analysis(
                    item["catalog_uuid"], catalog_root=root
                )
                analyses[item["catalog_uuid"]] = analysis
            orientation = select_orientation(analysis, item["orientation_id"])
            planar = matrix_from_xyz_rpy(
                x_mm=pose["x_mm"],
                y_mm=pose["y_mm"],
                z_mm=0.0,
                roll_deg=0.0,
                pitch_deg=0.0,
                yaw_deg=pose["rotation_deg"],
            )
            source_to_placed = validate_rigid_matrix(
                orientation["source_to_placed"], label="source_to_placed"
            )
            matrix = planar @ source_to_placed
            nominal = _transform_planar_contours(orientation["contours"], planar)
            layout_contours = _points(orientation["contours"])
            layout_pose = pose
            orientation_snapshot: dict[str, Any] | None = {
                "orientation_id": orientation["orientation_id"],
                "label": orientation["label"],
                "rank": orientation["rank"],
                "probability": orientation["probability"],
                "slice_z_mm": orientation["slice_z_mm"],
                "source_to_placed": orientation["source_to_placed"],
                "analysis_schema_version": analysis["schema_version"],
                "analysis_generated_at": analysis["generated_at"],
                "analysis_provenance": analysis["provenance"],
            }
            preview_meshes.setdefault(
                record["canonical_ply_sha256"], analysis["preview_mesh"]
            )
            layout_label = str(orientation["label"])
            layout_slice_z = float(orientation["slice_z_mm"])
            layout_source_to_placed = orientation["source_to_placed"]
        except Exception as exc:
            detail = getattr(exc, "message", str(exc))
            code = getattr(exc, "code", "invalid_intersection")
            errors.append(
                {
                    "instance_uuid": item["instance_uuid"],
                    "code": code,
                    "message": detail,
                }
            )
            continue
        compensated = _pdf_compensated_contours(
            nominal,
            page_width_mm=page_width,
            page_height_mm=page_height,
            origin_margin_mm=origin_margin,
            scale_x=scale_x,
            scale_y=scale_y,
        )
        compensated_page_bounds = _contour_bounds(
            [
                [
                    {
                        "x_mm": origin_margin + point["x_mm"],
                        "y_mm": origin_margin + point["y_mm"],
                    }
                    for point in contour
                ]
                for contour in compensated
            ]
        )
        epsilon = 1e-7
        if (
            compensated_page_bounds["min_x_mm"] < -epsilon
            or compensated_page_bounds["min_y_mm"] < -epsilon
            or compensated_page_bounds["max_x_mm"] > page_width + epsilon
            or compensated_page_bounds["max_y_mm"] > page_height + epsilon
        ):
            compensation_issues.append(
                {
                    "instance_uuid": item["instance_uuid"],
                    "code": "compensated_outside_page",
                    "message": (
                        f"Object '{record['name']}' leaves the physical page after "
                        "page-centred print compensation. Move it inward or reduce "
                        "the X/Y print percentage."
                    ),
                    "bounds": compensated_page_bounds,
                }
            )
        selected_revision = next(
            (
                revision
                for revision in record.get("geometry_revisions", [])
                if revision.get("revision") == record.get("geometry_revision")
            ),
            None,
        )
        instance = {
            **item,
            "catalog": {
                "catalog_uuid": record["catalog_uuid"],
                "obj_id": record["obj_id"],
                "name": record["name"],
                "canonical_ply_sha256": record["canonical_ply_sha256"],
                "texture_sha256": record.get("texture_sha256"),
                "geometry_revision": record["geometry_revision"],
                "source_to_mm_scale": record["source_to_mm_scale"],
                "geometry_revision_created_at": (
                    selected_revision.get("created_at")
                    if isinstance(selected_revision, Mapping)
                    else record.get("created_at")
                ),
                "geometry_revision_operation": (
                    selected_revision.get("operation")
                    if isinstance(selected_revision, Mapping)
                    else None
                ),
            },
            "pose_template_from_object": transform_record(
                matrix, parent="pose_template", child=f"object:{item['instance_uuid']}"
            ),
            "orientation": orientation_snapshot,
            "preview_mesh_sha256": (
                record["canonical_ply_sha256"]
                if item["placement_mode"] == "stable_orientation"
                else None
            ),
            "nominal_contours": nominal,
            "compensated_contours": compensated,
            "compensated_page_bounds_mm": compensated_page_bounds,
            "nominal_geometry_sha256": _hash_json(nominal),
            "compensated_geometry_sha256": _hash_json(compensated),
        }
        preview_instances.append(instance)
        layout_objects.append(
            {
                "id": item["instance_uuid"],
                "name": record["name"],
                "source_filename": "canonical.ply",
                "source_sha256": record["canonical_ply_sha256"],
                "orientation_label": layout_label,
                "slice_z_mm": layout_slice_z,
                "source_to_placed": layout_source_to_placed,
                "contours": [{"points": contour} for contour in layout_contours],
                "pose": layout_pose,
            }
        )
    if errors:
        return {
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "valid": False,
            "configuration": config,
            "configuration_sha256": _hash_json(config),
            "instances": preview_instances,
            "preview_meshes": preview_meshes,
            "errors": errors,
            "source": {
                "revision": POSETEMPLATECREATOR_REVISION,
                "adapter_version": ADAPTER_VERSION,
            },
        }
    request = {
        "schema_version": "2.0",
        "template_name": config["display_name"],
        "paper_size": config["page"]["size"],
        "orientation": config["page"]["orientation"],
        "pdf_scale_x_percent": scale_x * 100.0,
        "pdf_scale_y_percent": scale_y * 100.0,
        "objects": layout_objects,
    }
    scene = backend.build_scene(request)
    validation = backend.scene.validation_from_scene(scene).model_dump(mode="json")
    issues = [
        {
            "instance_uuid": str(issue["object_id"]),
            "code": issue["code"],
            "message": issue["message"],
            "bounds": issue["bounds"],
        }
        for issue in validation["issues"]
    ]
    issues.extend(compensation_issues)
    valid = bool(scene.valid) and not compensation_issues
    return {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "valid": valid,
        "configuration": config,
        "configuration_sha256": _hash_json(config),
        "page": validation["page"],
        "instances": preview_instances,
        "preview_meshes": preview_meshes,
        "fit": {
            "valid": valid,
            "objects": validation["objects"],
            "issues": issues,
        },
        "errors": issues,
        "print_geometry_sha256": _hash_json(
            [item["compensated_contours"] for item in preview_instances]
        ),
        "nominal_geometry_sha256": _hash_json(
            [item["nominal_contours"] for item in preview_instances]
        ),
        "source": {
            "revision": POSETEMPLATECREATOR_REVISION,
            "adapter_version": ADAPTER_VERSION,
        },
        "_layout_request": request,
    }


def _assert_bundle_catalog_snapshot_current(
    snapshot_instances: list[dict[str, Any]], *, catalog_root: Path
) -> None:
    """Fail publication if a referenced live geometry changed during staging."""

    catalog = load_catalog(catalog_root, verify_assets=False)
    current_by_uuid = {
        item["catalog_uuid"]: item for item in catalog.get("objects", [])
    }
    for item in snapshot_instances:
        snapshot = item["catalog"]
        current = current_by_uuid.get(snapshot["catalog_uuid"])
        if current is None:
            raise ValueError(
                "Cannot publish pose template because a workpiece was deleted: "
                f"{snapshot['catalog_uuid']}"
            )
        if current.get("state") != "active":
            raise ValueError(
                f"Cannot publish pose template with archived object: {current['name']}"
            )
        for field in (
            "canonical_ply_sha256",
            "texture_sha256",
            "geometry_revision",
        ):
            if current.get(field) != snapshot.get(field):
                raise ValueError(
                    "Cannot publish pose template because catalogue geometry changed "
                    f"while it was being generated: {current['name']}"
                )


def _generate_template_bundle(
    configuration: Mapping[str, Any],
    *,
    catalog_root: str | Path | None = None,
    library_root: str | Path | None = None,
    template_uuid: str | None = None,
    cloned_from: str | None = None,
) -> dict[str, Any]:
    opaque_id = _uuid(template_uuid or uuid.uuid4(), label="template_uuid")
    library = Path(library_root or default_template_library_root())
    _assert_template_uuid_not_retired(library, opaque_id)
    destination = library / opaque_id
    if destination.exists():
        raise ValueError(f"Pose template already exists: {opaque_id}")
    preview = build_template_preview(configuration, catalog_root=catalog_root)
    if not preview["valid"]:
        raise ValueError(
            "Pose template is invalid: "
            + "; ".join(item["message"] for item in preview["errors"])
        )
    stage = library / f".{opaque_id}.{uuid.uuid4().hex}.tmp"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        catalog = load_catalog(catalog_root)
        catalog_base = Path(catalog["catalog_root"])
        snapshot_root = stage / "assets"
        file_records: dict[str, Any] = {}
        snapshot_instances: list[dict[str, Any]] = []
        for item in preview["instances"]:
            record = get_catalog_object(item["catalog_uuid"], catalog_root=catalog_base)
            instance_dir = snapshot_root / item["instance_uuid"]
            instance_dir.mkdir(parents=True)
            canonical_source = catalog_base / record["assets"]["canonical_ply"]["path"]
            canonical_target = instance_dir / "canonical.ply"
            shutil.copyfile(canonical_source, canonical_target)
            files = {
                "canonical_ply": {
                    "path": canonical_target.relative_to(stage).as_posix(),
                    "sha256": _sha256(canonical_target),
                    "size_bytes": canonical_target.stat().st_size,
                }
            }
            texture_record = record["assets"].get("texture")
            if texture_record:
                texture_target = instance_dir / "texture.png"
                shutil.copyfile(catalog_base / texture_record["path"], texture_target)
                files["texture"] = {
                    "path": texture_target.relative_to(stage).as_posix(),
                    "sha256": _sha256(texture_target),
                    "size_bytes": texture_target.stat().st_size,
                }
            file_records[item["instance_uuid"]] = files
            snapshot_instances.append(
                {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"nominal_contours", "compensated_contours"}
                    },
                    "assets": files,
                }
            )
        serializable_preview = {
            key: value for key, value in preview.items() if not key.startswith("_")
        }
        atomic_write_json(stage / PREVIEW_JSON, serializable_preview)
        created = utc_now_iso()
        thumbnail = build_template_thumbnail(
            serializable_preview, template_uuid=opaque_id
        )
        thumbnail.update(
            created_at=created,
            updated_at=created,
            cloned_from=(
                _uuid(cloned_from, label="cloned_from") if cloned_from else None
            ),
        )
        atomic_write_json(stage / THUMBNAIL_JSON, thumbnail)
        backend = load_posetemplatecreator_backend()
        scene = backend.build_scene(preview["_layout_request"])
        atomic_write_bytes(stage / TEMPLATE_PDF, backend.render_pdf(scene))
        catalog_snapshot = {
            "schema_version": catalog["schema_version"],
            "version": catalog["version"],
            "objects": [item["catalog"] for item in snapshot_instances],
        }
        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "template_uuid": opaque_id,
            "display_name": preview["configuration"]["display_name"],
            "description": preview["configuration"]["description"],
            "created_at": created,
            "updated_at": created,
            "cloned_from": _uuid(cloned_from, label="cloned_from")
            if cloned_from
            else None,
            "page": preview["configuration"]["page"],
            "layout": {
                "nominal": {"units": "mm", "frame": "pose_template"},
                "compensated": preview["configuration"]["print_compensation"],
            },
            "print_compensation": preview["configuration"]["print_compensation"],
            "instances": snapshot_instances,
            "catalog_snapshot": catalog_snapshot,
            "configuration": preview["configuration"],
            "hashes": {
                "catalog": _hash_json(catalog_snapshot),
                "configuration": preview["configuration_sha256"],
                "nominal_geometry": preview["nominal_geometry_sha256"],
                "compensated_geometry": preview["print_geometry_sha256"],
                "pdf": _sha256(stage / TEMPLATE_PDF),
                "preview": _sha256(stage / PREVIEW_JSON),
                "thumbnail": _sha256(stage / THUMBNAIL_JSON),
                "assets": _hash_json(file_records),
            },
            "files": {
                "pdf": TEMPLATE_PDF,
                "preview": PREVIEW_JSON,
                "thumbnail": THUMBNAIL_JSON,
                "assets": file_records,
            },
            "source": {
                "name": "PoseTemplateCreator",
                "revision": POSETEMPLATECREATOR_REVISION,
                "adapter_version": ADAPTER_VERSION,
            },
        }
        manifest["bundle_sha256"] = _hash_json(manifest)
        atomic_write_json(stage / BUNDLE_MANIFEST, manifest)
        atomic_write_json(
            stage / ARCHIVE_STATE,
            {
                "schema_version": "pose_template_archive_state.v1",
                "state": "active",
                "updated_at": created,
            },
        )
        validate_template_bundle(stage, library_root=library, allow_staging=True)
        root = Path(catalog_root or default_catalog_root())
        # Heavy orientation analysis, slicing, rendering, and asset copying stay
        # outside the catalogue mutation lock. Publication briefly rechecks the
        # exact geometry identities under the same lock used by correction and
        # deletion, so a stale stage can never become a published bundle.
        with _mutation_lock(root):
            _assert_bundle_catalog_snapshot_current(
                snapshot_instances, catalog_root=root
            )
            with template_library_lock(library):
                _assert_template_uuid_not_retired(library, opaque_id)
                if destination.exists() or destination.is_symlink():
                    raise ValueError(f"Pose template already exists: {opaque_id}")
                os.replace(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return validate_template_bundle(destination, library_root=library)


def generate_template_bundle(
    configuration: Mapping[str, Any],
    *,
    catalog_root: str | Path | None = None,
    library_root: str | Path | None = None,
    template_uuid: str | None = None,
    cloned_from: str | None = None,
) -> dict[str, Any]:
    """Stage heavy work unlocked, then publish against a locked live snapshot."""

    root = Path(catalog_root or default_catalog_root())
    return _generate_template_bundle(
        configuration,
        catalog_root=root,
        library_root=library_root,
        template_uuid=template_uuid,
        cloned_from=cloned_from,
    )


def _load_template_bundle_metadata_unlocked(
    template_uuid: str, *, library_root: str | Path | None = None
) -> dict[str, Any]:
    """Validate bounded manifest identity for non-authoritative card reads.

    This verifies the manifest's self-hash, UUID, supported producer, thumbnail
    declaration, and mutable archive state. It deliberately does not read or
    hash the PDF, full preview, or copied mesh assets. Selection, catalogue
    deletion, and explicit whole-bundle audits must continue to use
    :func:`validate_template_bundle`.
    """

    library = Path(library_root or default_template_library_root())
    opaque_id = _uuid(template_uuid, label="template_uuid")
    bundle_dir = _bundle_directory(library, opaque_id)
    manifest_path = _bundle_file(bundle_dir, BUNDLE_MANIFEST, label="Bundle manifest")
    if manifest_path.stat().st_size > CARD_MANIFEST_MAX_JSON_BYTES:
        raise ValueError("Pose-template manifest exceeds the current size limit")
    manifest = _read_bounded_json(
        manifest_path,
        maximum_bytes=CARD_MANIFEST_MAX_JSON_BYTES,
        label="Bundle manifest",
    )
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError(f"Bundle schema must be {BUNDLE_SCHEMA_VERSION}")
    if _uuid(manifest.get("template_uuid"), label="template_uuid") != opaque_id:
        raise ValueError("Bundle directory does not match template_uuid")
    expected_bundle_hash = manifest.get("bundle_sha256")
    unhashed = {key: value for key, value in manifest.items() if key != "bundle_sha256"}
    if _hash_json(unhashed) != expected_bundle_hash:
        raise ValueError("Pose-template bundle manifest hash mismatch")
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Pose-template bundle source metadata is invalid")
    source_revision = source.get("revision")
    if source_revision not in SUPPORTED_SOURCE_REVISIONS:
        raise ValueError("Pose-template bundle has an unsupported upstream revision")
    instances = manifest.get("instances")
    if not isinstance(instances, list) or not 1 <= len(instances) <= MAX_INSTANCES:
        raise ValueError("Pose-template bundle instance metadata is invalid")
    files = manifest.get("files")
    hashes = manifest.get("hashes")
    if not isinstance(files, Mapping) or not isinstance(hashes, Mapping):
        raise ValueError("Pose-template bundle file metadata is invalid")
    thumbnail_file = files.get("thumbnail")
    thumbnail_hash = hashes.get("thumbnail")
    if thumbnail_file != THUMBNAIL_JSON or not isinstance(thumbnail_hash, str):
        raise ValueError("Current bundle must declare its bounded thumbnail")
    if (
        not isinstance(thumbnail_hash, str)
        or len(thumbnail_hash) != 64
        or any(character not in "0123456789abcdef" for character in thumbnail_hash)
    ):
        raise ValueError("Bundle thumbnail hash is invalid")
    _validate_bundle_tree(bundle_dir, _declared_bundle_files(manifest))
    archive = _load_archive_state_lightweight(bundle_dir)
    return {**manifest, "archive": archive, "bundle_path": bundle_dir.as_posix()}


def load_template_bundle_metadata(
    template_uuid: str, *, library_root: str | Path | None = None
) -> dict[str, Any]:
    library = Path(library_root or default_template_library_root())
    with template_library_lock(library):
        return _load_template_bundle_metadata_unlocked(
            template_uuid, library_root=library
        )


def _validate_template_bundle_unlocked(
    bundle_path: str | Path,
    *,
    library_root: str | Path | None = None,
    allow_staging: bool = False,
) -> dict[str, Any]:
    bundle_dir = Path(bundle_path)
    library = Path(library_root or bundle_dir.parent).resolve()
    resolved = bundle_dir.resolve()
    try:
        resolved.relative_to(library)
    except ValueError as exc:
        raise ValueError("Pose-template bundle escapes library") from exc
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise FileNotFoundError(f"Pose-template bundle does not exist: {bundle_dir}")
    manifest_path = _bundle_file(bundle_dir, BUNDLE_MANIFEST, label="Bundle manifest")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError(f"Bundle schema must be {BUNDLE_SCHEMA_VERSION}")
    template_uuid = _uuid(manifest.get("template_uuid"), label="template_uuid")
    if not allow_staging and bundle_dir.name != template_uuid:
        raise ValueError("Bundle directory does not match template_uuid")
    expected_bundle_hash = manifest.get("bundle_sha256")
    unhashed = {key: value for key, value in manifest.items() if key != "bundle_sha256"}
    if _hash_json(unhashed) != expected_bundle_hash:
        raise ValueError("Pose-template bundle manifest hash mismatch")
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Pose-template bundle source metadata is invalid")
    source_revision = source.get("revision")
    if source_revision not in SUPPORTED_SOURCE_REVISIONS:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_REVISIONS))
        raise ValueError(
            "Pose-template bundle has an unsupported upstream revision; "
            f"supported revisions are {supported}"
        )
    files = manifest.get("files", {})
    for name, digest_key in (("pdf", "pdf"), ("preview", "preview")):
        path = _bundle_file(bundle_dir, files.get(name, ""), label=f"Bundle {name}")
        if _sha256(path) != manifest["hashes"][digest_key]:
            raise ValueError(f"Bundle {name} is missing or was modified")
    thumbnail_file = files.get("thumbnail")
    thumbnail_hash = manifest.get("hashes", {}).get("thumbnail")
    if thumbnail_file != THUMBNAIL_JSON or not isinstance(thumbnail_hash, str):
        raise ValueError("Current bundle must declare its bounded thumbnail")
    thumbnail_path = _bundle_file(bundle_dir, thumbnail_file, label="Bundle thumbnail")
    if _sha256(thumbnail_path) != thumbnail_hash:
        raise ValueError("Bundle thumbnail is missing or was modified")
    _validate_template_thumbnail_content(
        _read_bounded_json(
            thumbnail_path,
            maximum_bytes=THUMBNAIL_MAX_JSON_BYTES,
            label="Bundle thumbnail",
        ),
        template_uuid=template_uuid,
    )
    for instance_files in files.get("assets", {}).values():
        for record in instance_files.values():
            path = _bundle_file(bundle_dir, record["path"], label="Bundle asset")
            if path.stat().st_size != int(record["size_bytes"]):
                raise ValueError("Bundle asset is missing or has the wrong size")
            if _sha256(path) != record["sha256"]:
                raise ValueError("Bundle asset hash mismatch")
    archive_path = _bundle_file(bundle_dir, ARCHIVE_STATE, label="Bundle archive state")
    with open(archive_path, "r", encoding="utf-8") as handle:
        archive = json.load(handle)
    if archive.get("state") not in {"active", "archived"}:
        raise ValueError("Pose-template archive state is invalid")
    _validate_bundle_tree(bundle_dir, _declared_bundle_files(manifest))
    return {**manifest, "archive": archive, "bundle_path": resolved.as_posix()}


def validate_template_bundle(
    bundle_path: str | Path,
    *,
    library_root: str | Path | None = None,
    allow_staging: bool = False,
) -> dict[str, Any]:
    library = Path(library_root or Path(bundle_path).parent)
    with template_library_lock(library):
        return _validate_template_bundle_unlocked(
            bundle_path,
            library_root=library,
            allow_staging=allow_staging,
        )


def _load_template_bundle_detail_unlocked(
    template_uuid: str, *, library_root: Path
) -> dict[str, Any]:
    """Load bounded, self-authenticating current bundle metadata."""

    opaque_id = _uuid(template_uuid, label="template_uuid")
    return _load_template_bundle_metadata_unlocked(opaque_id, library_root=library_root)


def load_template_bundle_detail(
    template_uuid: str, *, library_root: str | Path | None = None
) -> dict[str, Any]:
    """Load synchronous detail without hashing unrelated immutable artifacts."""

    library = Path(library_root or default_template_library_root())
    with template_library_lock(library):
        bundle = _load_template_bundle_detail_unlocked(
            template_uuid, library_root=library
        )
        return bundle


def _declared_sha256(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} hash is invalid")
    return value


def _verify_declared_bundle_file(
    bundle: Mapping[str, Any],
    relative_value: Any,
    digest_value: Any,
    *,
    label: str,
    size_bytes: Any | None = None,
) -> Path:
    path = _bundle_file(Path(bundle["bundle_path"]), relative_value, label=label)
    if size_bytes is not None:
        try:
            expected_size = int(size_bytes)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} size is invalid") from exc
        if expected_size < 0 or path.stat().st_size != expected_size:
            raise ValueError(f"{label} is missing or has the wrong size")
    expected_digest = _declared_sha256(digest_value, label=label)
    if _sha256(path) != expected_digest:
        raise ValueError(f"{label} is missing or was modified")
    return path


def load_template_bundle_preview(
    template_uuid: str, *, library_root: str | Path | None = None
) -> Any:
    """Load the one hash-verified full preview requested by the operator."""

    library = Path(library_root or default_template_library_root())
    with template_library_lock(library):
        bundle = _load_template_bundle_detail_unlocked(
            template_uuid, library_root=library
        )
        files = bundle.get("files")
        hashes = bundle.get("hashes")
        if not isinstance(files, Mapping) or not isinstance(hashes, Mapping):
            raise ValueError("Pose-template bundle file metadata is invalid")
        preview_path = _verify_declared_bundle_file(
            bundle,
            files.get("preview", ""),
            hashes.get("preview"),
            label="Bundle preview",
        )
        with open(preview_path, "r", encoding="utf-8") as handle:
            return json.load(handle)


def resolve_template_bundle_asset(
    template_uuid: str,
    instance_uuid: str,
    kind: str,
    *,
    library_root: str | Path | None = None,
) -> Path:
    """Resolve and verify only one requested immutable instance artifact."""

    library = Path(library_root or default_template_library_root())
    with template_library_lock(library):
        bundle = _load_template_bundle_detail_unlocked(
            template_uuid, library_root=library
        )
        files = bundle.get("files")
        assets = files.get("assets") if isinstance(files, Mapping) else None
        instance_files = (
            assets.get(instance_uuid) if isinstance(assets, Mapping) else None
        )
        if not isinstance(instance_files, Mapping) or kind not in instance_files:
            raise KeyError("Unknown template instance asset")
        record = instance_files[kind]
        if not isinstance(record, Mapping):
            raise ValueError("Pose-template asset record is invalid")
        return _verify_declared_bundle_file(
            bundle,
            record.get("path", ""),
            record.get("sha256"),
            label="Bundle asset",
            size_bytes=record.get("size_bytes"),
        )


def resolve_template_bundle_download(
    template_uuid: str,
    kind: str,
    *,
    library_root: str | Path | None = None,
) -> Path:
    """Resolve a verified PDF or self-hashed manifest for download."""

    if kind not in {"pdf", "manifest"}:
        raise KeyError("Unknown template download")
    library = Path(library_root or default_template_library_root())
    with template_library_lock(library):
        bundle = _load_template_bundle_detail_unlocked(
            template_uuid, library_root=library
        )
        bundle_dir = Path(bundle["bundle_path"])
        if kind == "manifest":
            return _bundle_file(bundle_dir, BUNDLE_MANIFEST, label="Bundle manifest")
        files = bundle.get("files")
        hashes = bundle.get("hashes")
        if not isinstance(files, Mapping) or not isinstance(hashes, Mapping):
            raise ValueError("Pose-template bundle file metadata is invalid")
        return _verify_declared_bundle_file(
            bundle,
            files.get("pdf", ""),
            hashes.get("pdf"),
            label="Bundle pdf",
        )


def load_template_thumbnail(
    template_uuid: str, *, library_root: str | Path | None = None
) -> dict[str, Any]:
    """Load the hash-verified bounded thumbnail from a current bundle."""

    library = Path(library_root or default_template_library_root())
    opaque_id = _uuid(template_uuid, label="template_uuid")
    bundle_dir = _bundle_directory(library, opaque_id)
    metadata = load_template_bundle_metadata(opaque_id, library_root=library)
    thumbnail_name = metadata.get("files", {}).get("thumbnail")
    if thumbnail_name != THUMBNAIL_JSON:
        raise ValueError("Current bundle does not declare its bounded thumbnail")
    thumbnail_path = _bundle_file(bundle_dir, thumbnail_name, label="Bundle thumbnail")
    if _sha256(thumbnail_path) != metadata["hashes"]["thumbnail"]:
        raise ValueError("Bundle thumbnail is missing or was modified")
    thumbnail = _read_bounded_json(
        thumbnail_path,
        maximum_bytes=THUMBNAIL_MAX_JSON_BYTES,
        label="Bundle thumbnail",
    )
    return _validate_template_thumbnail_content(thumbnail, template_uuid=opaque_id)


def list_template_bundles(
    library_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    library = Path(library_root or default_template_library_root())
    if not library.is_dir():
        return []
    bundles = []
    for child in sorted(library.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            try:
                bundles.append(validate_template_bundle(child, library_root=library))
            except (OSError, ValueError):
                continue
    return bundles


def template_bundle_summary(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return card metadata without exact contours, assets, or preview meshes."""

    instances = []
    for item in bundle.get("instances", []):
        if not isinstance(item, Mapping):
            continue
        catalog = item.get("catalog", {})
        instances.append(
            {
                "instance_uuid": item.get("instance_uuid"),
                "catalog_uuid": item.get("catalog_uuid"),
                "orientation_id": item.get("orientation_id"),
                "catalog": {
                    key: catalog[key]
                    for key in ("catalog_uuid", "name", "obj_id", "geometry_revision")
                    if isinstance(catalog, Mapping) and key in catalog
                },
            }
        )
    configuration = bundle.get("configuration", {})
    if not isinstance(configuration, Mapping):
        configuration = {}
    return {
        "schema_version": bundle.get("schema_version"),
        "template_uuid": bundle.get("template_uuid"),
        "display_name": bundle.get("display_name"),
        "description": bundle.get("description"),
        "created_at": bundle.get("created_at"),
        "updated_at": bundle.get("updated_at"),
        "cloned_from": bundle.get("cloned_from"),
        "bundle_sha256": bundle.get("bundle_sha256"),
        "page": bundle.get("page"),
        "print_compensation": bundle.get("print_compensation"),
        "configuration": {
            "page": configuration.get("page"),
            "print_compensation": configuration.get("print_compensation"),
        },
        "instance_count": len(instances),
        "instances": instances,
        "thumbnail": {
            "schema_version": THUMBNAIL_SCHEMA_VERSION,
            "stored": bool(bundle.get("files", {}).get("thumbnail"))
            if isinstance(bundle.get("files"), Mapping)
            else False,
        },
        "source": bundle.get("source"),
        "archive": bundle.get("archive"),
    }


def list_template_bundle_summaries(
    library_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List current bounded card metadata without hashing unrelated assets."""

    library = Path(library_root or default_template_library_root())
    if not library.is_dir() or library.is_symlink():
        return []
    summaries = []
    for child in sorted(library.iterdir()):
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
            continue
        try:
            opaque_id = _uuid(child.name, label="template_uuid")
            bundle = load_template_bundle_metadata(opaque_id, library_root=library)
            summaries.append(template_bundle_summary(bundle))
        except (OSError, ValueError):
            continue
    return summaries


def set_template_archive_state(
    template_uuid: str, *, state: str, library_root: str | Path | None = None
) -> dict[str, Any]:
    if state not in {"active", "archived"}:
        raise ValueError("Template state must be active or archived")
    library = Path(library_root or default_template_library_root())
    with template_library_lock(library):
        bundle = _load_template_bundle_detail_unlocked(
            template_uuid, library_root=library
        )
        archive = {
            "schema_version": "pose_template_archive_state.v1",
            "state": state,
            "updated_at": utc_now_iso(),
        }
        archive_path = _bundle_file(
            Path(bundle["bundle_path"]),
            ARCHIVE_STATE,
            label="Bundle archive state",
        )
        atomic_write_json(
            archive_path,
            archive,
        )
        return {**bundle, "archive": archive}


def _load_template_deletion_tombstone(
    tombstone_path: Path, *, template_uuid: str, cleanup_path: Path
) -> dict[str, Any]:
    value = _read_bounded_json(
        tombstone_path,
        maximum_bytes=DELETION_TOMBSTONE_MAX_JSON_BYTES,
        label="Pose-template deletion tombstone",
    )
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != DELETION_TOMBSTONE_SCHEMA_VERSION
        or value.get("template_uuid") != template_uuid
    ):
        raise ValueError("Pose-template deletion tombstone is invalid")
    cleanup = value.get("asset_cleanup")
    expected_path = cleanup_path.name
    if (
        not isinstance(cleanup, Mapping)
        or cleanup.get("path") != expected_path
        or cleanup.get("status") not in {"pending", "complete"}
    ):
        raise ValueError("Pose-template deletion cleanup record is invalid")
    return dict(value)


def _template_deletion_result(
    tombstone: Mapping[str, Any], *, already_deleted: bool
) -> dict[str, Any]:
    cleanup = tombstone["asset_cleanup"]
    return {
        **tombstone,
        "schema_version": "pose_template_library_delete.v1",
        "status": (
            "deleted" if cleanup["status"] == "complete" else "deleted_cleanup_pending"
        ),
        "already_deleted": already_deleted,
    }


def delete_template_bundle(
    template_uuid: str,
    *,
    library_root: str | Path | None = None,
    cleanup_assets: bool = True,
) -> dict[str, Any]:
    """Permanently retire one active or archived global library bundle.

    Run selections own complete copied snapshots and remain valid. The live
    UUID directory is atomically moved out of the library before cleanup, and a
    retained tombstone prevents identity reuse and makes failed cleanup
    retryable. Web handlers can set ``cleanup_assets=False`` and queue the
    potentially slow removal through :class:`LocalJobRunner`.
    """

    library = Path(library_root or default_template_library_root())
    opaque_id = _uuid(template_uuid, label="template_uuid")
    with template_library_lock(library):
        deletion_root, tombstone_path, cleanup_path = _deletion_paths(
            library, opaque_id
        )
        if deletion_root.is_symlink():
            raise ValueError("Pose-template deletion records must not be symlinked")
        deletion_root.mkdir(parents=True, exist_ok=True)
        if tombstone_path.is_symlink() or cleanup_path.is_symlink():
            raise ValueError("Pose-template deletion paths must not be symlinked")

        destination = library.resolve() / opaque_id
        already_deleted = not destination.exists()
        if destination.is_symlink():
            raise ValueError("Pose-template bundle must not be a symlink")

        if destination.exists():
            if cleanup_path.exists():
                raise ValueError(
                    "Pose-template deletion has both live and pending asset trees"
                )
            bundle = _load_template_bundle_detail_unlocked(
                opaque_id, library_root=library
            )
            if tombstone_path.exists():
                tombstone = _load_template_deletion_tombstone(
                    tombstone_path,
                    template_uuid=opaque_id,
                    cleanup_path=cleanup_path,
                )
                if (
                    tombstone["asset_cleanup"]["status"] != "pending"
                    or tombstone.get("display_name") != bundle["display_name"]
                    or tombstone.get("bundle_sha256") != bundle["bundle_sha256"]
                    or tombstone.get("state") != bundle["archive"]["state"]
                ):
                    raise ValueError(
                        "Pose-template deletion tombstone does not match the live bundle"
                    )
            else:
                deleted_at = utc_now_iso()
                tombstone = {
                    "schema_version": DELETION_TOMBSTONE_SCHEMA_VERSION,
                    "template_uuid": opaque_id,
                    "display_name": bundle["display_name"],
                    "bundle_sha256": bundle["bundle_sha256"],
                    "state": bundle["archive"]["state"],
                    "deleted_at": deleted_at,
                    "asset_cleanup": {
                        "status": "pending",
                        "path": cleanup_path.name,
                        "last_attempt_at": None,
                        "last_error": None,
                    },
                }
                atomic_write_json(tombstone_path, tombstone)
            os.replace(destination, cleanup_path)
        elif tombstone_path.exists():
            tombstone = _load_template_deletion_tombstone(
                tombstone_path,
                template_uuid=opaque_id,
                cleanup_path=cleanup_path,
            )
        else:
            raise KeyError(f"Unknown pose-template bundle: {opaque_id}")

        cleanup = dict(tombstone["asset_cleanup"])
        if not cleanup_assets or cleanup["status"] == "complete":
            return _template_deletion_result(tombstone, already_deleted=already_deleted)
        cleanup.update(last_attempt_at=utc_now_iso(), last_error=None)
        tombstone["asset_cleanup"] = cleanup
        atomic_write_json(tombstone_path, tombstone)

    cleanup_error: str | None = None
    try:
        if cleanup_path.exists():
            shutil.rmtree(cleanup_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_error = (f"{type(exc).__name__}: {exc}")[:2_000]

    with template_library_lock(library):
        tombstone = _load_template_deletion_tombstone(
            tombstone_path,
            template_uuid=opaque_id,
            cleanup_path=cleanup_path,
        )
        cleanup = dict(tombstone["asset_cleanup"])
        # A concurrent retry may already have completed the cleanup. Never
        # downgrade that durable result because this attempt observed a race.
        if cleanup["status"] != "complete":
            if cleanup_error is None:
                cleanup.update(status="complete", last_error=None)
            else:
                cleanup.update(status="pending", last_error=cleanup_error)
            tombstone["asset_cleanup"] = cleanup
            atomic_write_json(tombstone_path, tombstone)
        return _template_deletion_result(tombstone, already_deleted=already_deleted)


def record_template_cleanup_submission_failure(
    template_uuid: str,
    error: Exception,
    *,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Persist why retired bundle assets could not be queued for cleanup."""

    library = Path(library_root or default_template_library_root())
    opaque_id = _uuid(template_uuid, label="template_uuid")
    with template_library_lock(library):
        _deletion_root, tombstone_path, cleanup_path = _deletion_paths(
            library, opaque_id
        )
        tombstone = _load_template_deletion_tombstone(
            tombstone_path,
            template_uuid=opaque_id,
            cleanup_path=cleanup_path,
        )
        cleanup = dict(tombstone["asset_cleanup"])
        if cleanup["status"] != "complete":
            cleanup.update(
                last_attempt_at=utc_now_iso(),
                last_error=(
                    f"Cleanup job could not be queued: {type(error).__name__}: {error}"
                )[:2_000],
            )
            tombstone["asset_cleanup"] = cleanup
            atomic_write_json(tombstone_path, tombstone)
        return _template_deletion_result(tombstone, already_deleted=True)


def clone_template_configuration(
    template_uuid: str,
    *,
    library_root: str | Path | None = None,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    library = Path(library_root or default_template_library_root())
    bundle = load_template_bundle_detail(template_uuid, library_root=library)
    configuration_value = bundle.get("configuration")
    if not isinstance(configuration_value, Mapping):
        raise ValueError("Pose-template configuration metadata is invalid")
    configuration_hash = bundle.get("hashes", {}).get("configuration")
    if configuration_hash is not None and (
        _declared_sha256(configuration_hash, label="Bundle configuration")
        != _hash_json(configuration_value)
    ):
        raise ValueError("Pose-template configuration hash mismatch")
    catalog = load_catalog(catalog_root, verify_assets=False)
    current_by_uuid = {
        item["catalog_uuid"]: item for item in catalog.get("objects", [])
    }
    snapshot_by_instance = {
        item["instance_uuid"]: item
        for item in bundle.get("instances", [])
        if isinstance(item, Mapping) and item.get("instance_uuid")
    }
    configuration = json.loads(json.dumps(configuration_value))
    display_name = configuration.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("Pose-template display name is invalid")
    configuration["display_name"] = f"{display_name} (copy)"
    configuration_instances = configuration.get("instances")
    if not isinstance(configuration_instances, list) or not configuration_instances:
        raise ValueError("Pose-template configuration instances are invalid")
    for item in configuration_instances:
        if not isinstance(item, dict):
            raise ValueError("Pose-template configuration instance is invalid")
        catalog_uuid = item.get("catalog_uuid")
        instance_uuid = item.get("instance_uuid")
        current = current_by_uuid.get(catalog_uuid)
        snapshot = snapshot_by_instance.get(instance_uuid)
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("catalog_uuid") != catalog_uuid
        ):
            raise ValueError("Pose-template configuration snapshot is invalid")
        snapshot_catalog = snapshot.get("catalog", {}) if snapshot else {}
        if current is None:
            raise ValueError(
                "Cannot clone this template because a referenced catalogue "
                f"workpiece is unavailable: {catalog_uuid}"
            )
        if current.get("state") != "active":
            raise ValueError(
                "Cannot clone this template while workpiece "
                f"{current.get('name', catalog_uuid)!r} is archived"
            )
        if snapshot_catalog.get("canonical_ply_sha256") != current.get(
            "canonical_ply_sha256"
        ):
            raise ValueError(
                "Cannot clone this template because workpiece "
                f"{current.get('name', catalog_uuid)!r} now uses a "
                "different geometry revision. Create a new template and "
                "review its stable orientation."
            )
        item["instance_uuid"] = str(uuid.uuid4())
    return configuration
