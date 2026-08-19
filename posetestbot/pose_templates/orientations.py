"""Stable-orientation analysis and hash-bound catalogue cache artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from pathlib import Path
from typing import Any, Mapping

from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json
from posetestbot.pose_templates.adapter import (
    ADAPTER_VERSION,
    CATALOG_PREVIEW_MAX_FACES,
    CATALOG_PREVIEW_MESH_MAX_JSON_BYTES,
    CATALOG_PREVIEW_MAX_VERTICES,
    POSETEMPLATECREATOR_REVISION,
    TEMPLATE_PREVIEW_MAX_FACES,
    TEMPLATE_PREVIEW_MAX_VERTICES,
    PoseTemplateCreatorBackend,
    load_posetemplatecreator_backend,
)
from posetestbot.pose_templates.catalog import (
    _mutation_lock,
    get_catalog_object,
    utc_now_iso,
)
from posetestbot.pose_templates.transforms import validate_rigid_matrix


ORIENTATION_ANALYSIS_SCHEMA_VERSION = "pose_template_orientation_analysis.v1"
ORIENTATION_THUMBNAIL_SCHEMA_VERSION = "pose_template_orientation_thumbnail.v1"
ORIENTATION_CACHE_FILENAME = "pose_template_orientation_analysis.json"
ORIENTATION_THUMBNAIL_FILENAME = "pose_template_orientation_thumbnail.json"
ORIENTATION_THUMBNAIL_MAX_BYTES = 256 * 1024


class OrientationAnalysisStaleError(ValueError):
    """A cache exists but does not describe the current canonical geometry."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uuid(value: Any, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _catalog_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value[key]
        for key in (
            "obj_id",
            "name",
            "alias",
            "groups",
            "tags",
            "state",
            "geometry_revision",
            "source_to_mm_scale",
        )
        if key in value
    }


def _thumbnail_catalog_summary(value: Any) -> dict[str, Any]:
    """Keep mutable card identity useful without consuming the mesh budget."""

    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in ("obj_id", "name") if key in value}


def _orientation_id(source_sha256: str, orientation: Mapping[str, Any]) -> str:
    identity = {
        "source_sha256": source_sha256,
        "source_to_placed": orientation["source_to_placed"],
        "slice_z_mm": orientation["slice_z_mm"],
        "contours": orientation["contours"],
    }
    return "orientation-" + hashlib.sha256(_canonical_json(identity)).hexdigest()[:24]


def orientation_cache_path(
    catalog_uuid: str,
    *,
    catalog_root: str | Path | None = None,
    canonical_path: str | Path | None = None,
) -> Path:
    """Resolve the derived cache beside a workpiece's canonical PLY."""

    opaque_id = _uuid(catalog_uuid, label="catalog_uuid")
    if canonical_path is None:
        record = get_catalog_object(
            opaque_id, catalog_root=catalog_root, verify_assets=False
        )
        root = Path(record["catalog_root"])
        source = root / record["assets"]["canonical_ply"]["path"]
    else:
        source = Path(canonical_path)
        root = Path(catalog_root).resolve() if catalog_root is not None else None
    resolved = source.resolve()
    if root is not None:
        try:
            resolved.relative_to(root.resolve() / "objects" / opaque_id)
        except ValueError as exc:
            raise ValueError(
                "Canonical PLY is outside the managed workpiece directory"
            ) from exc
    return resolved.with_name(ORIENTATION_CACHE_FILENAME)


def build_orientation_analysis(
    catalog_uuid: str,
    canonical_path: str | Path,
    canonical_sha256: str,
    *,
    catalog_root: str | Path | None = None,
    catalog_metadata: Mapping[str, Any] | None = None,
    backend: PoseTemplateCreatorBackend | None = None,
) -> dict[str, Any]:
    """Analyze an explicit canonical snapshot without consulting the manifest.

    This pure geometry boundary is also suitable while catalogue code stages a
    replacement snapshot: callers supply the UUID, file, and expected digest.
    Persistence and current-manifest checks are deliberately separate.
    """

    opaque_id = _uuid(catalog_uuid, label="catalog_uuid")
    source = Path(canonical_path).resolve()
    orientation_cache_path(
        opaque_id, catalog_root=catalog_root, canonical_path=source
    )  # validates containment when a root is supplied
    if not source.is_file() or source.is_symlink():
        raise FileNotFoundError(f"Canonical PLY does not exist: {source}")
    expected = str(canonical_sha256).lower()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("canonical_sha256 must be a lowercase SHA-256 digest")
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError("Canonical PLY hash does not match canonical_sha256")
    implementation = backend or load_posetemplatecreator_backend()
    extracted = implementation.orientation_artifacts("canonical.ply", payload)
    if extracted["source_sha256"] != expected:
        raise ValueError(
            "Orientation analysis source hash does not match canonical PLY"
        )

    orientations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for rank, raw in enumerate(extracted["orientations"], start=1):
        item = {
            "label": str(raw["label"]),
            "probability": float(raw["probability"]),
            "source_to_placed": raw["source_to_placed"],
            "slice_z_mm": float(raw["slice_z_mm"]),
            "contours": raw["contours"],
        }
        orientation_id = _orientation_id(expected, item)
        if orientation_id in seen_ids:
            raise ValueError(
                "PoseTemplateCreator returned duplicate stable orientations"
            )
        seen_ids.add(orientation_id)
        orientations.append(
            {
                "orientation_id": orientation_id,
                "rank": rank,
                **item,
            }
        )

    catalog = _catalog_summary(catalog_metadata or {})
    return {
        "schema_version": ORIENTATION_ANALYSIS_SCHEMA_VERSION,
        "catalog_uuid": opaque_id,
        "catalog": catalog,
        "source": {
            "filename": "canonical.ply",
            "canonical_ply_sha256": expected,
            "units": "mm",
            "coordinate_frame": "catalog_object",
        },
        "generated_at": utc_now_iso(),
        "provenance": extracted["provenance"],
        "preview_mesh": extracted["preview_mesh"],
        "recognition_mesh": extracted.get(
            "recognition_mesh", extracted["preview_mesh"]
        ),
        "recognition_mesh_approximation": extracted["recognition_mesh_approximation"],
        "orientations": orientations,
    }


def _validate_preview_mesh(
    value: Any,
    *,
    label: str,
    max_vertices: int = CATALOG_PREVIEW_MAX_VERTICES,
    max_faces: int = CATALOG_PREVIEW_MAX_FACES,
    max_json_bytes: int | None = None,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    vertices = value.get("vertices")
    faces = value.get("faces")
    if not isinstance(vertices, list) or not 3 <= len(vertices) <= max_vertices:
        raise ValueError(f"{label} vertices are invalid")
    if not isinstance(faces, list) or not 1 <= len(faces) <= max_faces:
        raise ValueError(f"{label} faces are invalid")
    for vertex in vertices:
        if (
            not isinstance(vertex, list)
            or len(vertex) != 3
            or not all(math.isfinite(float(number)) for number in vertex)
        ):
            raise ValueError(f"{label} vertex is invalid")
    for face in faces:
        if (
            not isinstance(face, list)
            or len(face) != 3
            or len(set(face)) != 3
            or not all(
                isinstance(index, int) and 0 <= index < len(vertices) for index in face
            )
        ):
            raise ValueError(f"{label} face is invalid")
    if (
        max_json_bytes is not None
        and len(_canonical_json({"vertices": vertices, "faces": faces}))
        > max_json_bytes
    ):
        raise ValueError(f"{label} exceeds its bounded JSON size")


def _validate_recognition_approximation(
    value: Any,
    *,
    mesh: Any,
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if value.get("strategy") not in {
        "welded_source",
        "quadric_decimation",
        "spatial_clustering",
        "convex_proxy",
    }:
        raise ValueError(f"{label} strategy is invalid")
    if value.get("implementation_revision") != ADAPTER_VERSION:
        raise ValueError(f"{label} implementation revision is invalid")
    if not isinstance(mesh, Mapping):
        raise ValueError(f"{label} mesh is invalid")
    counts = {
        "source_vertices": (1, None),
        "source_faces": (1, None),
        "welded_vertices": (3, None),
        "welded_faces": (1, None),
        "result_vertices": (3, CATALOG_PREVIEW_MAX_VERTICES),
        "result_faces": (1, CATALOG_PREVIEW_MAX_FACES),
    }
    for key, (minimum, maximum) in counts.items():
        count = value.get(key)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < minimum
            or (maximum is not None and count > maximum)
        ):
            raise ValueError(f"{label} {key} is invalid")
    if value["result_vertices"] != len(mesh.get("vertices", [])) or value[
        "result_faces"
    ] != len(mesh.get("faces", [])):
        raise ValueError(f"{label} result counts do not match its mesh")
    for key in (
        "source_components",
        "source_euler_number",
        "result_components",
        "result_euler_number",
    ):
        metric = value.get(key)
        if metric is not None and (
            not isinstance(metric, int) or isinstance(metric, bool)
        ):
            raise ValueError(f"{label} {key} is invalid")
    if not isinstance(value.get("topology_preserved"), bool):
        raise ValueError(f"{label} topology_preserved is invalid")
    resolution = value.get("spatial_resolution")
    if resolution is not None and (
        not isinstance(resolution, int)
        or isinstance(resolution, bool)
        or resolution < 2
    ):
        raise ValueError(f"{label} spatial_resolution is invalid")
    reason = value.get("fallback_reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 120):
        raise ValueError(f"{label} fallback_reason is invalid")


def _validate_analysis(
    value: Mapping[str, Any],
    *,
    catalog_uuid: str,
    canonical_sha256: str,
) -> dict[str, Any]:
    if value.get("schema_version") != ORIENTATION_ANALYSIS_SCHEMA_VERSION:
        raise OrientationAnalysisStaleError(
            f"Orientation cache schema must be {ORIENTATION_ANALYSIS_SCHEMA_VERSION}"
        )
    opaque_id = _uuid(value.get("catalog_uuid"), label="catalog_uuid")
    if opaque_id != _uuid(catalog_uuid, label="catalog_uuid"):
        raise OrientationAnalysisStaleError(
            "Orientation cache belongs to another workpiece"
        )
    source = value.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("canonical_ply_sha256") != canonical_sha256
    ):
        raise OrientationAnalysisStaleError(
            "Orientation cache is stale because the canonical geometry changed"
        )
    provenance = value.get("provenance")
    if not isinstance(provenance, Mapping) or (
        provenance.get("upstream_revision") != POSETEMPLATECREATOR_REVISION
        or provenance.get("adapter_version") != ADAPTER_VERSION
    ):
        raise OrientationAnalysisStaleError(
            "Orientation cache was produced by an unsupported implementation revision"
        )
    _validate_preview_mesh(
        value.get("preview_mesh"),
        label="Orientation cache preview_mesh",
        max_vertices=TEMPLATE_PREVIEW_MAX_VERTICES,
        max_faces=TEMPLATE_PREVIEW_MAX_FACES,
    )
    recognition_mesh = value.get("recognition_mesh")
    _validate_preview_mesh(
        recognition_mesh,
        label="Orientation cache recognition_mesh",
        max_json_bytes=CATALOG_PREVIEW_MESH_MAX_JSON_BYTES,
    )
    _validate_recognition_approximation(
        value.get("recognition_mesh_approximation"),
        mesh=recognition_mesh,
        label="Orientation cache recognition_mesh_approximation",
    )

    orientations = value.get("orientations")
    if not isinstance(orientations, list) or not orientations:
        raise ValueError("Orientation cache must contain at least one orientation")
    seen: set[str] = set()
    for item in orientations:
        if not isinstance(item, Mapping):
            raise ValueError("Orientation cache entries must be objects")
        expected_id = _orientation_id(canonical_sha256, item)
        if item.get("orientation_id") != expected_id or expected_id in seen:
            raise ValueError("Orientation cache contains an invalid orientation_id")
        seen.add(expected_id)
        validate_rigid_matrix(item.get("source_to_placed"), label="source_to_placed")
        probability = float(item.get("probability"))
        slice_z = float(item.get("slice_z_mm"))
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("Orientation probability must be between zero and one")
        if not math.isfinite(slice_z) or slice_z < 0.0:
            raise ValueError("Orientation slice_z_mm must be non-negative")
        contours = item.get("contours")
        if not isinstance(contours, list) or not contours:
            raise ValueError("Orientation must contain closed contours")
    return dict(value)


def build_orientation_thumbnail(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the contour-free, implementation-bound workpiece card payload."""

    orientations = analysis.get("orientations")
    if not isinstance(orientations, list) or not orientations:
        raise ValueError("Orientation analysis does not contain an orientation")
    first = orientations[0]
    if not isinstance(first, Mapping):
        raise ValueError("Orientation analysis entry is invalid")
    thumbnail = {
        "schema_version": ORIENTATION_THUMBNAIL_SCHEMA_VERSION,
        "catalog_uuid": analysis.get("catalog_uuid"),
        "catalog": _thumbnail_catalog_summary(analysis.get("catalog")),
        "source": analysis.get("source"),
        "provenance": analysis.get("provenance"),
        "preview_mesh": analysis.get("recognition_mesh", analysis.get("preview_mesh")),
        "recognition_mesh_approximation": analysis.get(
            "recognition_mesh_approximation"
        ),
        "orientation": {
            key: first[key]
            for key in (
                "orientation_id",
                "label",
                "rank",
                "probability",
                "slice_z_mm",
                "source_to_placed",
            )
        },
    }
    if len(_canonical_json(thumbnail) + b"\n") > ORIENTATION_THUMBNAIL_MAX_BYTES:
        raise ValueError("Orientation thumbnail exceeds its bounded size limit")
    return thumbnail


def write_orientation_thumbnail(
    path: str | Path, analysis: Mapping[str, Any]
) -> dict[str, Any]:
    """Write the bounded thumbnail with compact deterministic JSON encoding."""

    thumbnail = build_orientation_thumbnail(analysis)
    payload = _canonical_json(thumbnail) + b"\n"
    if len(payload) > ORIENTATION_THUMBNAIL_MAX_BYTES:
        raise ValueError("Orientation thumbnail exceeds its bounded size limit")
    atomic_write_bytes(path, payload)
    return thumbnail


def _validate_orientation_thumbnail(
    value: Any, *, catalog_uuid: str, canonical_sha256: str
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != ORIENTATION_THUMBNAIL_SCHEMA_VERSION
    ):
        raise ValueError(
            f"Orientation thumbnail schema must be {ORIENTATION_THUMBNAIL_SCHEMA_VERSION}"
        )
    if _uuid(value.get("catalog_uuid"), label="catalog_uuid") != _uuid(
        catalog_uuid, label="catalog_uuid"
    ):
        raise OrientationAnalysisStaleError(
            "Orientation thumbnail belongs to another workpiece"
        )
    source = value.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("canonical_ply_sha256") != canonical_sha256
    ):
        raise OrientationAnalysisStaleError(
            "Orientation thumbnail is stale because canonical geometry changed"
        )
    provenance = value.get("provenance")
    if not isinstance(provenance, Mapping) or (
        provenance.get("upstream_revision") != POSETEMPLATECREATOR_REVISION
        or provenance.get("adapter_version") != ADAPTER_VERSION
    ):
        raise OrientationAnalysisStaleError(
            "Orientation thumbnail was produced by an unsupported implementation revision"
        )
    preview_mesh = value.get("preview_mesh")
    _validate_preview_mesh(
        preview_mesh,
        label="Orientation thumbnail preview_mesh",
        max_json_bytes=CATALOG_PREVIEW_MESH_MAX_JSON_BYTES,
    )
    _validate_recognition_approximation(
        value.get("recognition_mesh_approximation"),
        mesh=preview_mesh,
        label="Orientation thumbnail recognition_mesh_approximation",
    )
    orientation = value.get("orientation")
    if not isinstance(orientation, Mapping):
        raise ValueError("Orientation thumbnail selection is invalid")
    orientation_id = orientation.get("orientation_id")
    if (
        not isinstance(orientation_id, str)
        or not orientation_id.startswith("orientation-")
        or len(orientation_id) != len("orientation-") + 24
    ):
        raise ValueError("Orientation thumbnail orientation_id is invalid")
    validate_rigid_matrix(orientation.get("source_to_placed"), label="source_to_placed")
    probability = float(orientation.get("probability"))
    slice_z = float(orientation.get("slice_z_mm"))
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("Orientation thumbnail probability is invalid")
    if not math.isfinite(slice_z) or slice_z < 0:
        raise ValueError("Orientation thumbnail slice is invalid")
    return dict(value)


def load_catalog_orientation_analysis(
    catalog_uuid: str, *, catalog_root: str | Path | None = None
) -> dict[str, Any]:
    """Load a cache only if its source file and catalogue identity still match."""

    record = get_catalog_object(
        catalog_uuid, catalog_root=catalog_root, verify_assets=False
    )
    root = Path(record["catalog_root"])
    canonical = root / record["assets"]["canonical_ply"]["path"]
    expected = str(record["canonical_ply_sha256"])
    if _sha256_path(canonical) != expected:
        raise OrientationAnalysisStaleError(
            "Canonical PLY no longer matches the catalogue manifest"
        )
    cache = orientation_cache_path(
        record["catalog_uuid"], catalog_root=root, canonical_path=canonical
    )
    with open(cache, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError("Orientation cache must contain a JSON object")
    validated = _validate_analysis(
        value,
        catalog_uuid=record["catalog_uuid"],
        canonical_sha256=expected,
    )
    # Catalogue labels and lifecycle state are intentionally mutable. Keep the
    # persisted geometry cache hash-bound, but never return stale display data.
    validated["catalog"] = _catalog_summary(record)
    return validated


def analyze_catalog_orientations(
    catalog_uuid: str,
    *,
    catalog_root: str | Path | None = None,
    backend: PoseTemplateCreatorBackend | None = None,
) -> dict[str, Any]:
    """Analyze current catalogue geometry and atomically replace its cache."""

    record = get_catalog_object(
        catalog_uuid, catalog_root=catalog_root, verify_assets=False
    )
    root = Path(record["catalog_root"])
    canonical = root / record["assets"]["canonical_ply"]["path"]
    expected = str(record["canonical_ply_sha256"])
    analysis = build_orientation_analysis(
        record["catalog_uuid"],
        canonical,
        expected,
        catalog_root=root,
        catalog_metadata=record,
        backend=backend,
    )
    # Fail before replacing either cache if the new paired card artifact cannot
    # satisfy its count, provenance, or byte contract.
    build_orientation_thumbnail(analysis)
    # Publication shares the catalogue's process/thread lock with unit correction
    # and permanent deletion. The relatively expensive analysis stays outside the
    # lock; once it is ready, revalidate the exact current geometry and publish in
    # one short critical section. This prevents a worker that started before a
    # deletion from recreating a removed object's derived directory afterwards.
    with _mutation_lock(root):
        current = get_catalog_object(
            record["catalog_uuid"], catalog_root=root, verify_assets=False
        )
        current_path = root / current["assets"]["canonical_ply"]["path"]
        if (
            current["canonical_ply_sha256"] != expected
            or _sha256_path(current_path) != expected
        ):
            raise OrientationAnalysisStaleError(
                "Canonical geometry changed while stable orientations were being analyzed"
            )
        cache = orientation_cache_path(
            record["catalog_uuid"], catalog_root=root, canonical_path=current_path
        )
        atomic_write_json(cache, analysis)
        write_orientation_thumbnail(
            cache.with_name(ORIENTATION_THUMBNAIL_FILENAME), analysis
        )
    return load_catalog_orientation_analysis(record["catalog_uuid"], catalog_root=root)


def ensure_catalog_orientation_analysis(
    catalog_uuid: str, *, catalog_root: str | Path | None = None
) -> dict[str, Any]:
    """Load a current cache, computing it on demand inside an existing job."""

    try:
        return load_catalog_orientation_analysis(
            catalog_uuid, catalog_root=catalog_root
        )
    except (FileNotFoundError, OrientationAnalysisStaleError):
        return analyze_catalog_orientations(catalog_uuid, catalog_root=catalog_root)


def load_catalog_orientation_thumbnail(
    catalog_uuid: str, *, catalog_root: str | Path | None = None
) -> dict[str, Any]:
    """Return the bounded card payload without the potentially large contours."""

    record = get_catalog_object(
        catalog_uuid, catalog_root=catalog_root, verify_assets=False
    )
    root = Path(record["catalog_root"])
    canonical = root / record["assets"]["canonical_ply"]["path"]
    expected = str(record["canonical_ply_sha256"])
    cache = orientation_cache_path(
        record["catalog_uuid"], catalog_root=root, canonical_path=canonical
    )
    thumbnail_path = cache.with_name(ORIENTATION_THUMBNAIL_FILENAME)
    if thumbnail_path.stat().st_size > ORIENTATION_THUMBNAIL_MAX_BYTES:
        raise ValueError("Orientation thumbnail exceeds its bounded size limit")
    with open(thumbnail_path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    validated = _validate_orientation_thumbnail(
        value,
        catalog_uuid=record["catalog_uuid"],
        canonical_sha256=expected,
    )
    # Mutable catalogue labels must never be served from a stale geometry cache.
    validated["catalog"] = _thumbnail_catalog_summary(record)
    if len(_canonical_json(validated) + b"\n") > ORIENTATION_THUMBNAIL_MAX_BYTES:
        raise ValueError("Orientation thumbnail exceeds its bounded size limit")
    return validated


def select_orientation(
    analysis: Mapping[str, Any], orientation_id: str
) -> dict[str, Any]:
    for item in analysis.get("orientations", []):
        if item.get("orientation_id") == orientation_id:
            return dict(item)
    raise KeyError(f"Unknown stable orientation: {orientation_id}")
