"""Private, verified access to the pinned PoseTemplateCreator backend.

Only the small source modules needed for mesh safety, layout, and rendering are
loaded.  In particular, the upstream FastAPI application is never imported.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


POSETEMPLATECREATOR_REVISION = "97ddb9b7b756912deb8c2d2d6dde186b461e5d9d"
POSETEMPLATECREATOR_RELATIVE_PATH = Path("third_party/PoseTemplateCreator")
ADAPTER_VERSION = "posetestbot_posetemplatecreator_adapter.v4"
TEMPLATE_PREVIEW_MAX_VERTICES = 160
TEMPLATE_PREVIEW_MAX_FACES = 256
CATALOG_PREVIEW_MAX_VERTICES = 4_096
CATALOG_PREVIEW_MAX_FACES = 8_192
CATALOG_PREVIEW_TARGET_FACES = 4_096
CATALOG_PREVIEW_SIGNIFICANT_DIGITS = 7
# The complete card payload remains capped at 256 KiB. Reserve enough room for
# catalogue identity, provenance, and the selected stable-orientation matrix.
CATALOG_PREVIEW_MESH_MAX_JSON_BYTES = 224 * 1024
CATALOG_PREVIEW_SPATIAL_RESOLUTIONS = (4, 6, 8, 12, 16, 20, 24, 32, 48, 64, 96)
_PRIVATE_PACKAGE = (
    f"_posetestbot_posetemplatecreator_{POSETEMPLATECREATOR_REVISION[:12]}"
)
_MODULES = ("constants", "models", "mesh", "scene", "render")
_REQUIRED_FILES = tuple(Path("backend") / f"{name}.py" for name in _MODULES)
_LOAD_LOCK = threading.RLock()
_BACKEND_LOCK = threading.RLock()
_CACHE: dict[Path, "PoseTemplateCreatorBackend"] = {}


class PoseTemplateCreatorUnavailable(RuntimeError):
    """The optional pinned source checkout cannot be used safely."""


PreviewMeshPayload = dict[str, list[list[float]] | list[list[int]]]


def _compact_preview_arrays(vertices: Any, faces: Any) -> tuple[np.ndarray, np.ndarray]:
    """Return finite, indexed triangles with unused vertices removed."""

    points = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(faces, dtype=np.int64)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("Preview vertices must be a finite Nx3 array")
    if triangles.ndim != 2 or triangles.shape[1] != 3:
        raise ValueError("Preview faces must be an Nx3 array")
    valid = (
        (triangles >= 0).all(axis=1)
        & (triangles < len(points)).all(axis=1)
        & (triangles[:, 0] != triangles[:, 1])
        & (triangles[:, 1] != triangles[:, 2])
        & (triangles[:, 2] != triangles[:, 0])
    )
    triangles = triangles[valid]
    if not len(triangles):
        raise ValueError("Preview mesh has no valid triangles")
    used = np.unique(triangles.reshape(-1))
    remap = np.full(len(points), -1, dtype=np.int64)
    remap[used] = np.arange(len(used), dtype=np.int64)
    return points[used], remap[triangles]


def _quantized_preview_arrays(
    points: np.ndarray, triangles: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Round relative to model extent, then remove quantization degeneracies."""

    extents = np.ptp(points, axis=0)
    scale = float(np.max(extents))
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Preview mesh has no finite spatial extent")
    decimals = int(
        np.clip(
            CATALOG_PREVIEW_SIGNIFICANT_DIGITS - 1 - math.floor(math.log10(scale)),
            -12,
            15,
        )
    )
    rounded = np.round(points, decimals=decimals)
    unique_points, inverse = np.unique(rounded, axis=0, return_inverse=True)
    mapped = inverse[triangles]
    mapped = mapped[
        (mapped[:, 0] != mapped[:, 1])
        & (mapped[:, 1] != mapped[:, 2])
        & (mapped[:, 2] != mapped[:, 0])
    ]
    if not len(mapped):
        raise ValueError("Quantized preview mesh has no triangles")
    _keys, first_indices = np.unique(np.sort(mapped, axis=1), axis=0, return_index=True)
    mapped = mapped[np.sort(first_indices)]
    vectors = unique_points[mapped[:, 1:]] - unique_points[mapped[:, :1]]
    nondegenerate = np.linalg.norm(
        np.cross(vectors[:, 0], vectors[:, 1]), axis=1
    ) > max(1e-30, scale**2 * 1e-14)
    return _compact_preview_arrays(unique_points, mapped[nondegenerate])


def _bounded_preview_payload(
    points: np.ndarray, triangles: np.ndarray
) -> PreviewMeshPayload | None:
    try:
        rounded, compact_faces = _quantized_preview_arrays(points, triangles)
    except (TypeError, ValueError):
        return None
    if (
        not 3 <= len(rounded) <= CATALOG_PREVIEW_MAX_VERTICES
        or not 1 <= len(compact_faces) <= CATALOG_PREVIEW_MAX_FACES
    ):
        return None
    payload: PreviewMeshPayload = {
        "vertices": rounded.tolist(),
        "faces": compact_faces.tolist(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > CATALOG_PREVIEW_MESH_MAX_JSON_BYTES:
        return None
    return payload


def _cluster_preview_arrays(
    points: np.ndarray, triangles: np.ndarray, *, resolution: int
) -> tuple[np.ndarray, np.ndarray]:
    """Weld nearby vertices on a deterministic per-axis grid."""

    lower = points.min(axis=0)
    extents = np.ptp(points, axis=0)
    safe_extents = np.where(extents > 0, extents, 1.0)
    cells = np.floor((points - lower) / safe_extents * resolution).astype(np.int64)
    cells = np.clip(cells, 0, resolution - 1)
    _unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)
    clustered = np.zeros((int(inverse.max()) + 1, 3), dtype=np.float64)
    np.add.at(clustered, inverse, points)
    counts = np.bincount(inverse)
    clustered /= counts[:, np.newaxis]

    mapped = inverse[triangles]
    mapped = mapped[
        (mapped[:, 0] != mapped[:, 1])
        & (mapped[:, 1] != mapped[:, 2])
        & (mapped[:, 2] != mapped[:, 0])
    ]
    if not len(mapped):
        raise ValueError("Clustered preview mesh has no triangles")
    _keys, first_indices = np.unique(np.sort(mapped, axis=1), axis=0, return_index=True)
    mapped = mapped[np.sort(first_indices)]
    vectors = clustered[mapped[:, 1:]] - clustered[mapped[:, :1]]
    nondegenerate = np.linalg.norm(
        np.cross(vectors[:, 0], vectors[:, 1]), axis=1
    ) > max(1e-18, float(np.max(extents)) ** 2 * 1e-14)
    return _compact_preview_arrays(clustered, mapped[nondegenerate])


def _topology_signature(mesh: Any) -> tuple[int, int]:
    """Return connected-component count and Euler characteristic."""

    return int(mesh.body_count), int(mesh.euler_number)


def _payload_topology(mesh_type: type, payload: PreviewMeshPayload) -> tuple[int, int]:
    candidate = mesh_type(
        vertices=np.asarray(payload["vertices"], dtype=np.float64),
        faces=np.asarray(payload["faces"], dtype=np.int64),
        process=False,
    )
    return _topology_signature(candidate)


def _topology_error(
    source: tuple[int, int], result: tuple[int, int]
) -> tuple[int, int]:
    return abs(source[0] - result[0]), abs(source[1] - result[1])


def _spatial_preview_candidate(
    points: np.ndarray, triangles: np.ndarray
) -> tuple[PreviewMeshPayload, int] | None:
    """Find the highest bounded grid with a capped binary search."""

    resolutions = CATALOG_PREVIEW_SPATIAL_RESOLUTIONS
    cache: dict[int, PreviewMeshPayload | None] = {}

    def candidate(index: int) -> PreviewMeshPayload | None:
        if index not in cache:
            try:
                clustered_points, clustered_triangles = _cluster_preview_arrays(
                    points,
                    triangles,
                    resolution=resolutions[index],
                )
                cache[index] = _bounded_preview_payload(
                    clustered_points, clustered_triangles
                )
            except (TypeError, ValueError):
                cache[index] = None
        return cache[index]

    highest = len(resolutions) - 1
    payload = candidate(highest)
    if payload is not None:
        return payload, resolutions[highest]
    if candidate(0) is None:
        return None

    best = 0
    lower = 1
    upper = highest - 1
    while lower <= upper:
        middle = (lower + upper) // 2
        if candidate(middle) is None:
            upper = middle - 1
        else:
            best = middle
            lower = middle + 1
    best_payload = cache.get(best)
    return (best_payload, resolutions[best]) if best_payload is not None else None


def _recognition_approximation(
    *,
    strategy: str,
    source_mesh: Any,
    welded_vertices: int,
    welded_faces: int,
    source_topology: tuple[int, int] | None,
    payload: PreviewMeshPayload,
    spatial_resolution: int | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    try:
        result_topology = _payload_topology(type(source_mesh), payload)
    except Exception:
        result_topology = None
    topology_preserved = (
        source_topology is not None
        and result_topology is not None
        and source_topology == result_topology
    )
    return {
        "strategy": strategy,
        "implementation_revision": ADAPTER_VERSION,
        "source_vertices": int(len(source_mesh.vertices)),
        "source_faces": int(len(source_mesh.faces)),
        "welded_vertices": int(welded_vertices),
        "welded_faces": int(welded_faces),
        "result_vertices": len(payload["vertices"]),
        "result_faces": len(payload["faces"]),
        "source_components": source_topology[0] if source_topology else None,
        "source_euler_number": source_topology[1] if source_topology else None,
        "result_components": result_topology[0] if result_topology else None,
        "result_euler_number": result_topology[1] if result_topology else None,
        "topology_preserved": topology_preserved,
        "spatial_resolution": spatial_resolution,
        "fallback_reason": fallback_reason,
    }


def _preview_mesh_artifact(
    mesh: Any,
) -> tuple[PreviewMeshPayload | None, dict[str, Any] | None]:
    """Build and describe a recognition-focused, bounded catalogue preview.

    PoseTemplateCreator intentionally returns a tiny convex proxy above
    160 vertices or 256 faces. That is sufficient for printable-layout hints,
    but it removes holes, recesses, handles, and separated mechanical details.
    Catalogue cards have a larger yet still bounded budget. Prefer a candidate
    that retains component/Euler topology, then fall back to the upstream proxy.
    """

    candidate = mesh.copy()
    candidate.merge_vertices()
    points, triangles = _compact_preview_arrays(candidate.vertices, candidate.faces)
    candidate = type(mesh)(vertices=points, faces=triangles, process=False)
    source_topology = _topology_signature(candidate)
    exact = _bounded_preview_payload(points, triangles)
    if exact is not None:
        return exact, _recognition_approximation(
            strategy="welded_source",
            source_mesh=mesh,
            welded_vertices=len(points),
            welded_faces=len(triangles),
            source_topology=source_topology,
            payload=exact,
        )

    decimated: PreviewMeshPayload | None = None
    decimated_topology: tuple[int, int] | None = None
    simplifier_error: str | None = None
    try:
        simplified = candidate.simplify_quadric_decimation(
            face_count=min(CATALOG_PREVIEW_TARGET_FACES, len(triangles) - 1),
            aggression=7,
        )
        simplified_points, simplified_triangles = _compact_preview_arrays(
            simplified.vertices, simplified.faces
        )
        decimated = _bounded_preview_payload(simplified_points, simplified_triangles)
        if decimated is not None:
            decimated_topology = _payload_topology(type(mesh), decimated)
            if decimated_topology == source_topology:
                return decimated, _recognition_approximation(
                    strategy="quadric_decimation",
                    source_mesh=mesh,
                    welded_vertices=len(points),
                    welded_faces=len(triangles),
                    source_topology=source_topology,
                    payload=decimated,
                )
    except Exception as exc:
        simplifier_error = type(exc).__name__

    # Boundary-heavy or disconnected CAD can make edge-collapse decimation
    # erase openings. Compare it with the highest bounded spatial candidate.
    spatial = _spatial_preview_candidate(points, triangles)
    if spatial is not None:
        spatial_payload, resolution = spatial
        spatial_topology = _payload_topology(type(mesh), spatial_payload)
        if (
            decimated is None
            or decimated_topology is None
            or _topology_error(source_topology, spatial_topology)
            < _topology_error(source_topology, decimated_topology)
        ):
            return spatial_payload, _recognition_approximation(
                strategy="spatial_clustering",
                source_mesh=mesh,
                welded_vertices=len(points),
                welded_faces=len(triangles),
                source_topology=source_topology,
                payload=spatial_payload,
                spatial_resolution=resolution,
                fallback_reason=simplifier_error,
            )
    if decimated is not None:
        return decimated, _recognition_approximation(
            strategy="quadric_decimation",
            source_mesh=mesh,
            welded_vertices=len(points),
            welded_faces=len(triangles),
            source_topology=source_topology,
            payload=decimated,
        )
    if spatial is not None:
        spatial_payload, resolution = spatial
        return spatial_payload, _recognition_approximation(
            strategy="spatial_clustering",
            source_mesh=mesh,
            welded_vertices=len(points),
            welded_faces=len(triangles),
            source_topology=source_topology,
            payload=spatial_payload,
            spatial_resolution=resolution,
            fallback_reason=simplifier_error,
        )
    return None, None


def _preview_mesh_payload(mesh: Any) -> PreviewMeshPayload | None:
    """Compatibility helper returning only the bounded recognition mesh."""

    return _preview_mesh_artifact(mesh)[0]


@dataclass(frozen=True)
class PoseTemplateCreatorBackend:
    checkout: Path
    revision: str
    constants: types.ModuleType
    models: types.ModuleType
    mesh: types.ModuleType
    scene: types.ModuleType
    render: types.ModuleType

    def safe_filename(self, filename: str | None) -> str:
        return str(self.mesh.safe_filename(filename))

    def file_format(self, filename: str) -> str:
        return str(self.mesh.file_format(filename))

    def load_mesh(self, filename: str, data: bytes):
        if len(data) > int(self.constants.MAX_UPLOAD_BYTES):
            raise ValueError(
                f"CAD file exceeds the {self.constants.MAX_UPLOAD_BYTES} byte limit"
            )
        safe_name = self.safe_filename(filename)
        extension = self.file_format(safe_name)
        with _BACKEND_LOCK:
            return self.mesh._load_mesh(data, extension)

    def canonical_ply(self, filename: str, data: bytes) -> tuple[bytes, dict[str, Any]]:
        mesh = self.load_mesh(filename, data)
        exported = mesh.export(file_type="ply", encoding="binary_little_endian")
        if isinstance(exported, str):
            exported = exported.encode("utf-8")
        payload = bytes(exported)
        metadata = {
            "vertices": int(len(mesh.vertices)),
            "faces": int(len(mesh.faces)),
            "bounds_mm": np.asarray(mesh.bounds, dtype=float).tolist(),
            "watertight": bool(mesh.is_watertight),
        }
        return payload, metadata

    def provenance(self) -> dict[str, Any]:
        """Return the exact implementation versions behind derived geometry."""

        dependencies: dict[str, str] = {}
        for distribution in (
            "numpy",
            "scipy",
            "trimesh",
            "networkx",
            "fast-simplification",
        ):
            try:
                dependencies[distribution] = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                dependencies[distribution] = "unavailable"
        return {
            "adapter_version": ADAPTER_VERSION,
            "upstream_name": "PoseTemplateCreator",
            "upstream_revision": self.revision,
            "dependencies": dependencies,
        }

    def orientation_artifacts(self, filename: str, data: bytes) -> dict[str, Any]:
        """Extract deterministic stable orientations and one bounded source mesh.

        The public adapter result intentionally uses compact PoseTestBot field
        names while retaining the upstream matrices and contours without
        modification.  The source mesh is expressed in the catalogue model's
        millimetre coordinate frame; each ``source_to_placed`` rigid transform
        grounds that source mesh on the corresponding stable base.
        """

        safe_name = self.safe_filename(filename)
        source_sha256 = hashlib.sha256(data).hexdigest()
        with _BACKEND_LOCK:
            extraction = self.mesh.extract_orientations_with_preview(safe_name, data)
            source_mesh = self.load_mesh(safe_name, data)
            try:
                detailed_preview, approximation = _preview_mesh_artifact(source_mesh)
                recognition_error = None
            except Exception as exc:
                detailed_preview = None
                approximation = None
                recognition_error = type(exc).__name__
        compact_preview = extraction.preview_mesh.model_dump(mode="json")
        if detailed_preview is None:
            try:
                fallback_source = source_mesh.copy()
                fallback_source.merge_vertices()
                fallback_points, fallback_faces = _compact_preview_arrays(
                    fallback_source.vertices, fallback_source.faces
                )
                fallback_topology = _topology_signature(fallback_source)
            except Exception:
                fallback_points = np.asarray(source_mesh.vertices)
                fallback_faces = np.asarray(source_mesh.faces)
                fallback_topology = None
            approximation = _recognition_approximation(
                strategy="convex_proxy",
                source_mesh=source_mesh,
                welded_vertices=len(fallback_points),
                welded_faces=len(fallback_faces),
                source_topology=fallback_topology,
                payload=compact_preview,
                fallback_reason=(
                    recognition_error or "bounded_recognition_candidate_unavailable"
                ),
            )
        orientations: list[dict[str, Any]] = []
        for footprint in extraction.orientations:
            value = footprint.model_dump(mode="json")
            if value["source_sha256"] != source_sha256:
                raise PoseTemplateCreatorUnavailable(
                    "PoseTemplateCreator returned orientation geometry for a different source"
                )
            orientations.append(
                {
                    "label": value["orientation_label"],
                    "probability": value["orientation_probability"],
                    "source_to_placed": value["source_to_placed"],
                    "slice_z_mm": value["slice_z_mm"],
                    "contours": value["contours"],
                }
            )
        return {
            "source_filename": safe_name,
            "source_sha256": source_sha256,
            "orientations": orientations,
            # Preserve PoseTemplateCreator's deliberately tiny layout/editor
            # proxy. Catalogue recognition gets a separate, larger LOD so an
            # immutable template with many workpieces does not multiply a
            # several-thousand-face mesh into its exact preview payload.
            "preview_mesh": compact_preview,
            "recognition_mesh": detailed_preview or compact_preview,
            "recognition_mesh_approximation": approximation,
            "provenance": self.provenance(),
        }

    # A readable alias for callers that do not persist the returned artifacts.
    analyze_orientations = orientation_artifacts

    def posed_contours(
        self, filename: str, data: bytes, matrix: np.ndarray
    ) -> list[list[dict[str, float]]]:
        transform = np.asarray(matrix, dtype=float)
        if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
            raise ValueError("Object pose must be a finite 4x4 matrix")
        mesh = self.load_mesh(filename, data).copy()
        mesh.apply_transform(transform)
        with _BACKEND_LOCK:
            contours = self.mesh._closed_contours(mesh)
        return [
            [
                {"x_mm": float(point.x_mm), "y_mm": float(point.y_mm)}
                for point in contour.points
            ]
            for contour in contours
        ]

    def build_scene(self, request: dict[str, Any]):
        validated = self.models.LayoutRequestV2.model_validate(request)
        with _BACKEND_LOCK:
            return self.scene.build_scene(validated)

    def render_pdf(self, scene: Any) -> bytes:
        with _BACKEND_LOCK:
            return bytes(self.render.render_pdf(scene))


def default_posetemplatecreator_checkout() -> Path:
    configured = os.environ.get("POSETESTBOT_APP_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        source = Path(__file__).resolve().parents[2]
        root = source if (source / "pyproject.toml").is_file() else Path.cwd()
    return root / POSETEMPLATECREATOR_RELATIVE_PATH


def _git(checkout: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", checkout.as_posix(), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PoseTemplateCreatorUnavailable(
            f"Unable to inspect PoseTemplateCreator checkout at {checkout}: {exc}"
        ) from exc
    return result.stdout.strip()


def verify_posetemplatecreator_checkout(
    checkout: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(checkout or default_posetemplatecreator_checkout()).resolve()
    result: dict[str, Any] = {
        "schema_version": "posetemplatecreator_source_status.v1",
        "status": "missing",
        "available": False,
        "checkout": root.as_posix(),
        "required_revision": POSETEMPLATECREATOR_REVISION,
        "revision": None,
        "clean": None,
        "missing_files": [],
        "reason": None,
        "adapter_version": ADAPTER_VERSION,
    }
    if not root.is_dir() or not (root / ".git").exists():
        result["reason"] = (
            "PoseTemplateCreator is missing. Run 'git submodule update --init "
            "third_party/PoseTemplateCreator' or 'bash scripts/install.sh "
            "--with-posetemplatecreator'."
        )
        return result
    missing = [
        item.as_posix() for item in _REQUIRED_FILES if not (root / item).is_file()
    ]
    result["missing_files"] = missing
    if missing:
        result["reason"] = "Required backend files are missing: " + ", ".join(missing)
        return result
    try:
        revision = _git(root, "rev-parse", "HEAD")
        dirty = _git(root, "status", "--porcelain", "--untracked-files=all")
    except PoseTemplateCreatorUnavailable as exc:
        result["reason"] = str(exc)
        return result
    result["revision"] = revision
    result["clean"] = not bool(dirty)
    if revision != POSETEMPLATECREATOR_REVISION:
        result["status"] = "revision_mismatch"
        result["reason"] = (
            f"PoseTemplateCreator revision mismatch: found {revision}, "
            f"required {POSETEMPLATECREATOR_REVISION}."
        )
        return result
    if dirty:
        result["status"] = "dirty"
        result["reason"] = "PoseTemplateCreator checkout has local modifications."
        return result
    result["status"] = "available"
    result["available"] = True
    return result


def _load_module(package_name: str, backend_dir: Path, name: str) -> types.ModuleType:
    full_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, backend_dir / f"{name}.py")
    if spec is None or spec.loader is None:
        raise PoseTemplateCreatorUnavailable(f"Unable to load upstream module {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def load_posetemplatecreator_backend(
    checkout: str | Path | None = None,
) -> PoseTemplateCreatorBackend:
    root = Path(checkout or default_posetemplatecreator_checkout()).resolve()
    with _LOAD_LOCK:
        status = verify_posetemplatecreator_checkout(root)
        if not status["available"]:
            _CACHE.pop(root, None)
            raise PoseTemplateCreatorUnavailable(str(status["reason"]))
        cached = _CACHE.get(root)
        if cached is not None:
            return cached
        suffix = hashlib.sha256(root.as_posix().encode()).hexdigest()[:10]
        package_name = f"{_PRIVATE_PACKAGE}_{suffix}"
        backend_dir = root / "backend"
        package = types.ModuleType(package_name)
        package.__file__ = (backend_dir / "__init__.py").as_posix()
        package.__package__ = package_name
        package.__path__ = [backend_dir.as_posix()]
        sys.modules[package_name] = package
        loaded: dict[str, types.ModuleType] = {}
        # Upstream v2 uses absolute ``backend.*`` imports. Provide those aliases
        # only while loading, then remove them so PoseTestBot never exposes or
        # collides with the upstream web application's package name.
        sentinel = object()
        prior_aliases: dict[str, object] = {
            "backend": sys.modules.get("backend", sentinel)
        }
        sys.modules["backend"] = package
        try:
            for name in _MODULES:
                loaded[name] = _load_module(package_name, backend_dir, name)
                alias = f"backend.{name}"
                prior_aliases[alias] = sys.modules.get(alias, sentinel)
                sys.modules[alias] = loaded[name]
            backend = PoseTemplateCreatorBackend(
                checkout=root,
                revision=POSETEMPLATECREATOR_REVISION,
                **loaded,
            )
            required = (
                (backend.mesh, "_load_mesh"),
                (backend.mesh, "_closed_contours"),
                (backend.mesh, "extract_orientations_with_preview"),
                (backend.scene, "build_scene"),
                (backend.render, "render_pdf"),
            )
            absent = [name for module, name in required if not hasattr(module, name)]
            if absent:
                raise PoseTemplateCreatorUnavailable(
                    "Pinned backend lacks required capabilities: " + ", ".join(absent)
                )
        except Exception as exc:
            for name in tuple(sys.modules):
                if name == package_name or name.startswith(package_name + "."):
                    sys.modules.pop(name, None)
            if isinstance(exc, PoseTemplateCreatorUnavailable):
                raise
            raise PoseTemplateCreatorUnavailable(
                f"Unable to initialize pinned PoseTemplateCreator backend: {exc}"
            ) from exc
        finally:
            for alias, previous in prior_aliases.items():
                if previous is sentinel:
                    sys.modules.pop(alias, None)
                else:
                    sys.modules[alias] = previous  # type: ignore[assignment]
        _CACHE[root] = backend
        return backend


def posetemplatecreator_status(checkout: str | Path | None = None) -> dict[str, Any]:
    status = verify_posetemplatecreator_checkout(checkout)
    if status["available"]:
        try:
            backend = load_posetemplatecreator_backend(checkout)
        except PoseTemplateCreatorUnavailable as exc:
            status.update(status="unavailable", available=False, reason=str(exc))
        else:
            status["capabilities"] = {
                "formats": list(backend.constants.SUPPORTED_FORMATS),
                "page_sizes_mm": dict(backend.constants.PAGE_SIZES_MM),
                "limits": {
                    "cad_bytes": int(backend.constants.MAX_UPLOAD_BYTES),
                    "batch_bytes": int(backend.constants.MAX_BATCH_BYTES),
                    "faces": int(backend.constants.MAX_FACES),
                    "contour_vertices": int(backend.constants.MAX_CONTOUR_VERTICES),
                    "instances": int(backend.constants.MAX_OBJECTS),
                    "orientations_per_object": int(backend.constants.MAX_ORIENTATIONS),
                    "preview_vertices": int(backend.constants.MAX_PREVIEW_VERTICES),
                    "preview_faces": int(backend.constants.MAX_PREVIEW_FACES),
                    "catalog_preview_vertices": CATALOG_PREVIEW_MAX_VERTICES,
                    "catalog_preview_faces": CATALOG_PREVIEW_MAX_FACES,
                },
                "coordinate_convention": (
                    "millimetres; source_to_placed maps catalogue-model coordinates "
                    "to a grounded stable orientation; planar template pose is applied last"
                ),
            }
    return status
