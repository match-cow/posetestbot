"""Minimal BOP scene writer for synchronized PoseTestBot sensor folders."""

from __future__ import annotations

import json
import hashlib
import math
import re
import shutil
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np
import pytransform3d.rotations as pr
import pytransform3d.transformations as pt
import trimesh

from posetestbot.calibration.profiles import (
    CalibrationProfile,
    CalibrationStatus,
    profile_to_dict,
)
from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json
from posetestbot.io.artifacts import (
    BOP_COCO_ANNOTATIONS,
    BOP_DIR,
    BOP_DATASET_INFO,
    BOP_EXPORT_MANIFEST,
    BOP_FRAME_MAP_JSON,
    BOP_INSTANCE_MAP,
    BOP_POSE_TEMPLATE,
    BOP_TARGETS_BOP19,
    CAM_K,
    DEPTH_DIR,
    DEPTH_SCALE,
    MATCH_ROBOT_EE_POSES,
    MODELS_DIR,
    MODELS_EVAL_DIR,
    RGB_DIR,
)

SCHEMA_VERSION = "bop_export_manifest.v5"
FRAME_MAP_SCHEMA_VERSION = "posetestbot_bop_frame_map.v3"
DATASET_INFO_SCHEMA_VERSION = "posetestbot_bop_dataset_info.v1"
ANNOTATION_SOURCES = frozenset({"none", "blenderproc"})
ANNOTATION_MODES = frozenset({"none", "pose", "pose_and_masks"})
SCENE_GT_INFO_FIELDS = frozenset(
    {
        "bbox_obj",
        "bbox_visib",
        "px_count_all",
        "px_count_valid",
        "px_count_visib",
        "visib_fract",
    }
)


@dataclass(frozen=True)
class BopSceneExport:
    sensor_name: str
    scene_id: int
    split: str
    scene_folder: str
    rgb_count: int
    depth_count: int
    artifacts: dict[str, str]
    calibration_profile_id: str | None = None
    targets: list[dict] | None = None
    frame_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    instance_map: list[dict] = field(default_factory=list)
    projection: str = "native"
    input_sensor_folder: str | None = None
    authoritative_source_sensor_folder: str | None = None
    input_fingerprint_sha256: str | None = None
    authoritative_source_fingerprint_sha256: str | None = None
    annotation_source: str = "none"
    annotation_mode: str = "none"
    annotation_provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BopObjectModel:
    object_name: str
    obj_id: int
    source_path: str
    bop_path: str
    bop_eval_path: str
    texture_path: str | None = None


def read_camera_matrix(path: Path) -> list[float]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing camera matrix file: {path}")
    values = [float(value) for value in path.read_text().split()]
    if len(values) < 9:
        raise ValueError(f"Camera matrix file {path} has fewer than 9 values")
    return values[:9]


def read_depth_scale(path: Path) -> float:
    if not path.is_file():
        raise FileNotFoundError(f"Missing depth-scale sidecar: {path}")
    values = path.read_text().split()
    if not values:
        raise ValueError(f"Depth-scale sidecar is empty: {path}")
    return float(values[0])


def _frame_pairs(sensor_folder: Path) -> list[tuple[Path, Path]]:
    rgb_folder = sensor_folder / RGB_DIR
    depth_folder = sensor_folder / DEPTH_DIR
    if not rgb_folder.is_dir():
        raise FileNotFoundError(f"Missing RGB folder: {rgb_folder}")
    if not depth_folder.is_dir():
        raise FileNotFoundError(f"Missing depth folder: {depth_folder}")

    rgb_by_name = {path.name: path for path in rgb_folder.glob("*.png")}
    depth_by_name = {path.name: path for path in depth_folder.glob("*.png")}
    if not rgb_by_name or not depth_by_name:
        raise FileNotFoundError(
            f"No matching RGB/depth PNG frame pairs in {sensor_folder}"
        )
    if set(rgb_by_name) != set(depth_by_name):
        missing_depth = sorted(set(rgb_by_name) - set(depth_by_name))
        missing_rgb = sorted(set(depth_by_name) - set(rgb_by_name))
        raise ValueError(
            "RGB/depth frame names do not match; "
            f"missing_depth={missing_depth}, missing_rgb={missing_rgb}"
        )
    return [(rgb_by_name[name], depth_by_name[name]) for name in sorted(rgb_by_name)]


def _write_json(path: Path, value: object) -> Path:
    return atomic_write_json(path, value)


def mesh_vertices(path: Path) -> np.ndarray:
    mesh = trimesh.load(path, process=False)
    if isinstance(mesh, trimesh.Scene):
        vertices = [
            np.asarray(geometry.vertices, dtype=float)
            for geometry in mesh.geometry.values()
            if hasattr(geometry, "vertices") and len(geometry.vertices)
        ]
        if not vertices:
            return np.empty((0, 3), dtype=float)
        return np.vstack(vertices)
    if not hasattr(mesh, "vertices"):
        return np.empty((0, 3), dtype=float)
    vertices = np.asarray(mesh.vertices, dtype=float)
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        return np.empty((0, 3), dtype=float)
    return vertices


def exact_vertex_diameter(vertices: np.ndarray, *, chunk_size: int = 512) -> float:
    max_distance_sq = 0.0
    for start in range(0, len(vertices), chunk_size):
        chunk = vertices[start : start + chunk_size]
        distances_sq = np.sum((chunk[:, None, :] - vertices[None, :, :]) ** 2, axis=2)
        max_distance_sq = max(max_distance_sq, float(np.max(distances_sq)))
    return float(np.sqrt(max_distance_sq))


def model_geometry_info(
    path: Path, cached: Mapping[str, object] | None = None
) -> dict[str, object]:
    source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if cached is not None:
        geometry = cached.get("posetestbot_geometry")
        if (
            isinstance(geometry, Mapping)
            and geometry.get("source_sha256") == source_sha256
            and geometry.get("diameter_method") == "exact_convex_hull_vertex_pairwise"
        ):
            required = {
                "diameter",
                "min_x",
                "min_y",
                "min_z",
                "size_x",
                "size_y",
                "size_z",
            }
            if required <= set(cached):
                return {
                    key: cached[key]
                    for key in (*sorted(required), "posetestbot_geometry")
                }
    vertices = mesh_vertices(path)
    vertex_count = int(len(vertices))
    if vertex_count == 0:
        raise ValueError(f"Object model contains no vertices: {path}")

    mins = vertices.min(axis=0)
    maxs = vertices.max(axis=0)
    size = maxs - mins
    if not np.all(np.isfinite(vertices)):
        raise ValueError(f"Object model contains non-finite vertices: {path}")
    hull_vertices = vertices
    if vertex_count > 4:
        try:
            hull_vertices = np.asarray(
                trimesh.Trimesh(vertices=vertices, process=False).convex_hull.vertices,
                dtype=float,
            )
        except Exception as exc:
            raise ValueError(
                f"Unable to compute convex hull for {path}: {exc}"
            ) from exc
    diameter = exact_vertex_diameter(hull_vertices)
    if not math.isfinite(diameter) or diameter <= 0:
        raise ValueError(f"Object model diameter must be finite and positive: {path}")
    return {
        "diameter": diameter,
        "min_x": float(mins[0]),
        "min_y": float(mins[1]),
        "min_z": float(mins[2]),
        "size_x": float(size[0]),
        "size_y": float(size[1]),
        "size_z": float(size[2]),
        "posetestbot_geometry": {
            "diameter_method": "exact_convex_hull_vertex_pairwise",
            "vertex_count": vertex_count,
            "convex_hull_vertex_count": int(len(hull_vertices)),
            "source_sha256": source_sha256,
        },
    }


def write_bop_model_ply(
    source: str | Path,
    destination: str | Path,
    *,
    texture_filename: str | None = None,
) -> Path:
    """Normalize a catalogue mesh to the conservative BOP Toolkit PLY subset.

    Catalogue geometry may legitimately retain importer-specific vertex or face
    attributes. The BOP Toolkit's PLY reader does not consume arbitrary binary
    face properties, so copying such a file byte-for-byte can desynchronize its
    face parser. BOP models are derived artifacts: preserve the exact vertices,
    faces, object frame, and millimetre units while serializing only triangular
    geometry, vertex normals, optional vertex colors, and optional UVs.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    loaded = trimesh.load(source_path, process=False, force="mesh")
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError(f"Object model is not a triangle mesh: {source_path}")

    vertices = np.asarray(loaded.vertices, dtype=np.float64)
    faces = np.asarray(loaded.faces, dtype=np.int64)
    if vertices.ndim != 2 or vertices.shape[1] != 3 or not len(vertices):
        raise ValueError(f"Object model contains no valid vertices: {source_path}")
    if faces.ndim != 2 or faces.shape[1] != 3 or not len(faces):
        raise ValueError(f"Object model must contain triangular faces: {source_path}")
    if not np.all(np.isfinite(vertices)):
        raise ValueError(f"Object model contains non-finite vertices: {source_path}")
    if int(faces.min()) < 0 or int(faces.max()) >= len(vertices):
        raise ValueError(f"Object model contains invalid face indices: {source_path}")

    normalized = trimesh.Trimesh(
        vertices=vertices,
        faces=faces,
        process=False,
    )
    normals = np.asarray(normalized.vertex_normals, dtype=np.float64)
    if normals.shape != vertices.shape or not np.all(np.isfinite(normals)):
        raise ValueError(f"Object model vertex normals are invalid: {source_path}")

    if getattr(loaded.visual, "kind", None) == "vertex" and len(
        loaded.visual.vertex_colors
    ) == len(vertices):
        normalized.visual.vertex_colors = np.asarray(
            loaded.visual.vertex_colors,
            dtype=np.uint8,
        )

    uv = getattr(loaded.visual, "uv", None)
    if uv is None:
        attributes = getattr(loaded, "vertex_attributes", {})
        for first, second in (("texture_u", "texture_v"), ("s", "t")):
            if first in attributes and second in attributes:
                uv = np.column_stack((attributes[first], attributes[second]))
                break
    if uv is not None:
        uv_array = np.asarray(uv, dtype=np.float64)
        if uv_array.shape != (len(vertices), 2) or not np.all(np.isfinite(uv_array)):
            raise ValueError(
                f"Object model texture coordinates are invalid: {source_path}"
            )
        normalized.vertex_attributes["texture_u"] = uv_array[:, 0]
        normalized.vertex_attributes["texture_v"] = uv_array[:, 1]
    elif texture_filename is not None:
        raise ValueError(
            f"Object model has a texture but no usable UV coordinates: {source_path}"
        )

    payload = trimesh.exchange.ply.export_ply(
        normalized,
        encoding="ascii",
        vertex_normal=True,
        include_attributes=uv is not None,
    )
    if texture_filename is not None:
        if Path(texture_filename).name != texture_filename:
            raise ValueError("BOP texture filename must not contain a path")
        marker = b"format ascii 1.0\n"
        if marker not in payload:
            raise ValueError("Unable to add the BOP PLY texture declaration")
        payload = payload.replace(
            marker,
            marker + f"comment TextureFile {texture_filename}\n".encode("utf-8"),
            1,
        )
    return atomic_write_bytes(destination_path, payload)


def validate_bop_model_ply(path: str | Path) -> dict[str, int | bool]:
    """Validate the PLY subset emitted for direct BOP Toolkit consumption."""

    model_path = Path(path)
    with open(model_path, "rb") as handle:
        header = handle.read(1_048_576)
    end = header.find(b"end_header\n")
    if end < 0:
        raise ValueError(f"BOP object model has no bounded PLY header: {model_path}")
    header_text = header[: end + len(b"end_header\n")].decode("ascii")
    lines = header_text.splitlines()
    if "format ascii 1.0" not in lines:
        raise ValueError(f"BOP object model must use ASCII PLY: {model_path}")
    required_normal_properties = {
        "property float nx",
        "property float ny",
        "property float nz",
    }
    if not required_normal_properties <= set(lines):
        raise ValueError(f"BOP object model must include vertex normals: {model_path}")
    if "property list uchar int vertex_indices" not in lines:
        raise ValueError(
            f"BOP object model must use triangular vertex-index faces: {model_path}"
        )
    allowed_vertex_properties = {
        "property float x",
        "property float y",
        "property float z",
        "property float nx",
        "property float ny",
        "property float nz",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "property uchar alpha",
        "property double texture_u",
        "property double texture_v",
    }
    current_element: str | None = None
    vertex_properties: set[str] = set()
    face_properties: set[str] = set()
    for line in lines:
        if line.startswith("element "):
            parts = line.split()
            current_element = parts[1] if len(parts) == 3 else None
            if current_element not in {"vertex", "face"}:
                raise ValueError(
                    f"BOP object model has an unsupported PLY element: {model_path}"
                )
        elif line.startswith("property "):
            if current_element == "vertex":
                vertex_properties.add(line)
            elif current_element == "face":
                face_properties.add(line)
            else:
                raise ValueError(
                    f"BOP object model has a property outside an element: {model_path}"
                )
    if not vertex_properties <= allowed_vertex_properties:
        raise ValueError(
            f"BOP object model has unsupported vertex properties: {model_path}"
        )
    if face_properties != {"property list uchar int vertex_indices"}:
        raise ValueError(
            f"BOP object model has unsupported face properties: {model_path}"
        )

    loaded = trimesh.load(model_path, process=False, force="mesh")
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError(f"BOP object model is not a triangle mesh: {model_path}")
    faces = np.asarray(loaded.faces)
    vertices = np.asarray(loaded.vertices)
    if faces.ndim != 2 or faces.shape[1] != 3 or not len(faces):
        raise ValueError(f"BOP object model faces are invalid: {model_path}")
    if vertices.ndim != 2 or vertices.shape[1] != 3 or not len(vertices):
        raise ValueError(f"BOP object model vertices are invalid: {model_path}")
    return {
        "vertex_count": int(len(vertices)),
        "face_count": int(len(faces)),
        "ascii": True,
        "vertex_normals": True,
    }


def copy_bop_instance_models(
    output_root: str | Path,
    run_root: str | Path,
    object_instances: Mapping[str, object],
    *,
    geometry_cache: Mapping[str, object] | None = None,
) -> list[BopObjectModel]:
    """Export one canonical model for each stable obj_id, never per instance."""
    output = Path(output_root)
    run = Path(run_root).resolve()
    instances = object_instances.get("instances")
    if not isinstance(instances, list):
        raise ValueError("object_instances instances must be a list")
    by_id: dict[int, Mapping[str, object]] = {}
    for item in instances:
        if not isinstance(item, Mapping):
            raise ValueError("object_instances entries must be objects")
        obj_id = int(item["obj_id"])
        if obj_id <= 0:
            raise ValueError("object_instances obj_id must be positive")
        previous = by_id.get(obj_id)
        if previous is not None and previous.get("canonical_ply_sha256") != item.get(
            "canonical_ply_sha256"
        ):
            raise ValueError(
                f"Instances sharing obj_id {obj_id} use different geometry"
            )
        if previous is not None and previous.get("texture_sha256") != item.get(
            "texture_sha256"
        ):
            raise ValueError(
                f"Instances sharing obj_id {obj_id} use different textures"
            )
        by_id.setdefault(obj_id, item)
    models_dir = output / MODELS_DIR
    models_eval_dir = output / MODELS_EVAL_DIR
    models_dir.mkdir(parents=True, exist_ok=True)
    models_eval_dir.mkdir(parents=True, exist_ok=True)
    info: dict[str, dict[str, object]] = {}
    result: list[BopObjectModel] = []
    for obj_id, item in sorted(by_id.items()):
        source = run / str(item["canonical_ply"])
        try:
            source.resolve(strict=True).relative_to(run)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Object instance model escapes run root: {source}"
            ) from exc
        expected_source_sha256 = str(item["canonical_ply_sha256"])
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        if source_sha256 != expected_source_sha256:
            raise ValueError(
                f"Object instance model hash does not match its snapshot: {source}"
            )
        destination = models_dir / f"obj_{obj_id:06d}.ply"
        eval_destination = models_eval_dir / f"obj_{obj_id:06d}.ply"
        texture_value = item.get("texture")
        texture_destination: Path | None = None
        if texture_value is not None:
            texture_source = run / str(texture_value)
            try:
                texture_source.resolve(strict=True).relative_to(run)
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"Object instance texture escapes run root: {texture_source}"
                ) from exc
            expected_texture_sha256 = str(item["texture_sha256"])
            if hashlib.sha256(texture_source.read_bytes()).hexdigest() != (
                expected_texture_sha256
            ):
                raise ValueError(
                    "Object instance texture hash does not match its snapshot: "
                    f"{texture_source}"
                )
            texture_destination = models_dir / f"obj_{obj_id:06d}.png"
            shutil.copy2(texture_source, texture_destination)
        write_bop_model_ply(
            source,
            destination,
            texture_filename=(
                texture_destination.name if texture_destination is not None else None
            ),
        )
        write_bop_model_ply(source, eval_destination)
        validate_bop_model_ply(destination)
        validate_bop_model_ply(eval_destination)
        cached = geometry_cache.get(str(obj_id)) if geometry_cache else None
        geometry = model_geometry_info(
            source, cached if isinstance(cached, Mapping) else None
        )
        info[str(obj_id)] = {
            "source_name": item["name"],
            "catalog_uuid": item["catalog_uuid"],
            "source_path": str(item["canonical_ply"]),
            **geometry,
        }
        result.append(
            BopObjectModel(
                object_name=str(item["name"]),
                obj_id=obj_id,
                source_path=str(item["canonical_ply"]),
                bop_path=destination.relative_to(output).as_posix(),
                bop_eval_path=eval_destination.relative_to(output).as_posix(),
                texture_path=(
                    texture_destination.relative_to(output).as_posix()
                    if texture_destination is not None
                    else None
                ),
            )
        )
    _write_json(models_dir / "models_info.json", info)
    _write_json(models_eval_dir / "models_info.json", info)
    return result


def _load_json_if_present(path: Path) -> object | None:
    if not path.is_file():
        return None
    with open(path, "r") as f:
        return json.load(f)


def blenderproc_output_folder(sensor_folder: Path) -> Path:
    return sensor_folder / "blenderproc" / "output"


def load_blenderproc_scene_json(
    sensor_folder: Path, filename: str
) -> dict[str, object] | None:
    value = _load_json_if_present(blenderproc_output_folder(sensor_folder) / filename)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            f"Expected {filename} in {blenderproc_output_folder(sensor_folder)} "
            "to contain a JSON object"
        )
    return value


def normalize_scene_gt_object_ids(
    scene_gt: Mapping[str, object],
    object_name_to_id: Mapping[str, int] | None = None,
) -> dict[str, object]:
    if object_name_to_id is None:
        return dict(scene_gt)

    normalized: dict[str, object] = {}
    for image_id, image_annotations in scene_gt.items():
        if not isinstance(image_annotations, list):
            normalized[image_id] = image_annotations
            continue
        normalized_annotations = []
        for annotation in image_annotations:
            if not isinstance(annotation, dict):
                normalized_annotations.append(annotation)
                continue
            annotation_copy = dict(annotation)
            obj_id = annotation_copy.get("obj_id")
            if isinstance(obj_id, str) and obj_id in object_name_to_id:
                annotation_copy["obj_id"] = object_name_to_id[obj_id]
            normalized_annotations.append(annotation_copy)
        normalized[image_id] = normalized_annotations
    return normalized


def targets_from_scene_gt(
    scene_gt: Mapping[str, object],
    *,
    scene_id: int,
    scene_gt_info: Mapping[str, object] | None = None,
    min_visibility: float = 0.1,
) -> list[dict[str, int]]:
    """Build BOP19 localization targets from sufficiently visible GT instances.

    The official BOP19 target inventory counts instances whose visible surface
    fraction is at least 10%.  ``scene_gt_info`` remains optional for callers
    loading older annotation sets that do not contain visibility evidence.
    """

    if not math.isfinite(min_visibility) or not 0.0 <= min_visibility <= 1.0:
        raise ValueError("BOP target minimum visibility must be between 0 and 1")
    targets: list[dict[str, int]] = []
    for image_id, image_annotations in sorted(
        scene_gt.items(), key=lambda item: int(item[0])
    ):
        if not isinstance(image_annotations, list):
            continue
        image_infos = scene_gt_info.get(image_id) if scene_gt_info is not None else None
        if scene_gt_info is not None and (
            not isinstance(image_infos, list)
            or len(image_infos) != len(image_annotations)
        ):
            raise ValueError(f"scene_gt_info does not match scene_gt image {image_id}")
        counts: dict[int, int] = {}
        for annotation_index, annotation in enumerate(image_annotations):
            if not isinstance(annotation, dict):
                continue
            if isinstance(image_infos, list):
                info = image_infos[annotation_index]
                if not isinstance(info, Mapping):
                    raise ValueError(
                        f"scene_gt_info[{image_id!r}][{annotation_index}] "
                        "must be an object"
                    )
                try:
                    visibility = float(info["visib_fract"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"scene_gt_info[{image_id!r}][{annotation_index}] "
                        "has no valid visib_fract"
                    ) from exc
                if not math.isfinite(visibility) or not 0.0 <= visibility <= 1.0:
                    raise ValueError(
                        f"scene_gt_info[{image_id!r}][{annotation_index}] "
                        "visib_fract must be between 0 and 1"
                    )
                if visibility < min_visibility:
                    continue
            obj_id = annotation.get("obj_id")
            try:
                obj_id_int = int(obj_id)
            except (TypeError, ValueError):
                continue
            counts[obj_id_int] = counts.get(obj_id_int, 0) + 1
        for obj_id, inst_count in sorted(counts.items()):
            targets.append(
                {
                    "scene_id": scene_id,
                    "im_id": int(image_id),
                    "obj_id": obj_id,
                    "inst_count": inst_count,
                }
            )
    return targets


def targets_from_template_instances(
    template_instances: list[Mapping[str, object]],
    *,
    scene_id: int,
    frame_count: int,
) -> list[dict[str, int]]:
    """Build inference targets from the confirmed physical template inventory."""

    counts: dict[int, int] = {}
    for index, instance in enumerate(template_instances):
        try:
            obj_id = int(instance["obj_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Template instance {index} has an invalid BOP obj_id"
            ) from exc
        if obj_id <= 0:
            raise ValueError(f"Template instance {index} has a non-positive obj_id")
        counts[obj_id] = counts.get(obj_id, 0) + 1
    return [
        {
            "scene_id": scene_id,
            "im_id": image_id,
            "obj_id": obj_id,
            "inst_count": inst_count,
        }
        for image_id in range(frame_count)
        for obj_id, inst_count in sorted(counts.items())
    ]


def mask_filename(image_id: int, annotation_index: int) -> str:
    return f"{image_id:06d}_{annotation_index:06d}.png"


def mask_pixels(path: Path) -> np.ndarray | None:
    if not path.is_file():
        return None
    image = cv2.imread(path.as_posix(), cv2.IMREAD_UNCHANGED)
    if image is None:
        return None
    if image.ndim == 3:
        image = image[:, :, 0]
    return np.asarray(image) > 0


def resolve_annotation_mode(
    annotation_source: str,
    annotation_mode: str,
) -> str:
    """Validate one explicit current GT capability and source pair."""

    if annotation_source not in ANNOTATION_SOURCES:
        raise ValueError(
            "BOP annotation_source must be one of: "
            + ", ".join(sorted(ANNOTATION_SOURCES))
        )
    resolved = annotation_mode
    if resolved not in ANNOTATION_MODES:
        raise ValueError(
            "BOP annotation_mode must be one of: " + ", ".join(sorted(ANNOTATION_MODES))
        )
    expected_source = "none" if resolved == "none" else "blenderproc"
    if annotation_source != expected_source:
        raise ValueError(
            f"BOP annotation_mode {resolved!r} requires annotation_source "
            f"{expected_source!r}"
        )
    return resolved


def _read_rgbd_pair(rgb_path: Path, depth_path: Path) -> tuple[np.ndarray, np.ndarray]:
    rgb = cv2.imread(rgb_path.as_posix(), cv2.IMREAD_UNCHANGED)
    depth = cv2.imread(depth_path.as_posix(), cv2.IMREAD_UNCHANGED)
    if rgb is None:
        raise ValueError(f"RGB PNG is unreadable: {rgb_path}")
    if depth is None:
        raise ValueError(f"Depth PNG is unreadable: {depth_path}")
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] not in {3, 4}:
        raise ValueError(f"RGB image must be uint8 with 3 or 4 channels: {rgb_path}")
    if depth.dtype != np.uint16 or depth.ndim != 2:
        raise ValueError(f"Depth image must be single-channel uint16: {depth_path}")
    if rgb.shape[:2] != depth.shape:
        raise ValueError(f"RGB/depth dimensions do not match: {rgb_path}, {depth_path}")
    return rgb, depth


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Missing BlenderProc GT provenance input: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plain_child_file(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValueError(f"{label} must be a plain filename")
    path = root / value
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"{label} is missing or escapes its prepared folder") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular prepared file")
    return path


def _rigid_rotation(
    values: object,
    *,
    label: str,
    tolerance: float = 1e-4,
) -> np.ndarray:
    try:
        rotation = np.asarray(values, dtype=float).reshape(3, 3)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain a 3x3 rotation") from exc
    if not np.all(np.isfinite(rotation)):
        raise ValueError(f"{label} must be finite")
    orthogonality_error = float(
        np.linalg.norm(rotation.T @ rotation - np.eye(3), ord="fro")
    )
    determinant = float(np.linalg.det(rotation))
    if orthogonality_error > tolerance or abs(determinant - 1.0) > tolerance:
        raise ValueError(f"{label} must be an orthonormal rotation with determinant +1")
    return rotation


def validate_blenderproc_gt_provenance(
    sensor_folder: Path,
    *,
    frame_pairs: list[tuple[Path, Path]],
    annotation_mode: str,
    projection: str,
    cam_k: list[float],
    scene_gt: Mapping[str, object],
    calibration_profile: CalibrationProfile,
) -> dict[str, object]:
    """Verify analytic GT against its exact prepared inputs and source frames."""

    output_folder = blenderproc_output_folder(sensor_folder)
    prepared_folder = output_folder.parent
    provenance = load_blenderproc_scene_json(
        sensor_folder,
        "posetestbot_gt_provenance.json",
    )
    if provenance is None:
        raise FileNotFoundError(
            "BlenderProc GT annotations require "
            f"{output_folder / 'posetestbot_gt_provenance.json'}"
        )
    required_scalars = {
        "schema_version": "posetestbot_gt_provenance.v1",
        "blenderproc_version": "2.8.0",
        "supported_blenderproc_version": "2.8.0",
        "annotation_mode": annotation_mode,
        "pose_contract": "analytic_model_to_opencv_camera_rigid_transform.v1",
        "translation_unit": "mm",
        "rotation_storage": "row_major_3x3",
        "projection": projection,
    }
    mismatches = [
        key
        for key, expected in required_scalars.items()
        if provenance.get(key) != expected
    ]
    if mismatches:
        raise ValueError(
            "Rendered GT provenance has incompatible fields: " + ", ".join(mismatches)
        )
    if provenance.get("coordinate_frames") != {
        "model": "canonical_object_model",
        "camera": "opencv_camera",
        "camera_pose_input": "template_base_from_opencv_camera",
        "object_pose_input": "template_base_from_object",
    }:
        raise ValueError("Rendered GT provenance coordinate-frame contract is invalid")
    render_script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "blenderproc_render_720p_multi.py"
    )
    expected_implementation = {
        "revision": "posetestbot_analytic_bop_gt.v1",
        "script_sha256": _sha256_file(render_script),
    }
    if provenance.get("analytic_implementation") != expected_implementation:
        raise ValueError(
            "Rendered GT provenance analytic implementation is not the current "
            "pinned PoseTestBot implementation"
        )
    if provenance.get("scene_loading") != {
        "objects": "blenderproc.loader.load_obj",
        "camera_intrinsics": "blenderproc.camera.set_intrinsics_from_K_matrix",
        "camera_poses": "blenderproc.camera.add_camera_pose",
        "image_rendering": False,
        "mask_generation": (
            "official_bop_toolkit_depth_step"
            if annotation_mode == "pose_and_masks"
            else "not_requested"
        ),
    }:
        raise ValueError("Rendered GT provenance scene-loading contract is invalid")

    matched_poses = _load_json_if_present(sensor_folder / MATCH_ROBOT_EE_POSES)
    if not isinstance(matched_poses, Mapping):
        raise ValueError(
            "BlenderProc GT frame bindings require matched robot-pose evidence"
        )
    expected_bindings: list[dict[str, object]] = []
    robot_pose_records: list[Mapping[str, object]] = []
    resolution: tuple[int, int] | None = None
    for output_image_id, (rgb_path, depth_path) in enumerate(frame_pairs):
        rgb, _depth = _read_rgbd_pair(rgb_path, depth_path)
        current_resolution = (int(rgb.shape[1]), int(rgb.shape[0]))
        if resolution is None:
            resolution = current_resolution
        elif current_resolution != resolution:
            raise ValueError("BlenderProc GT frames must have one resolution")
        source_filename = rgb_path.name
        try:
            source_frame_id = int(rgb_path.stem)
        except ValueError as exc:
            raise ValueError(
                f"BlenderProc GT source frame must have a numeric stem: {rgb_path}"
            ) from exc
        matched = matched_poses.get(source_filename)
        if not isinstance(matched, Mapping) or not isinstance(
            matched.get("robot_ee_pose"),
            Mapping,
        ):
            raise ValueError(
                "BlenderProc GT source frame lacks matched robot-pose evidence: "
                f"{source_filename}"
            )
        robot_pose_records.append(matched["robot_ee_pose"])
        expected_bindings.append(
            {
                "output_image_id": output_image_id,
                "source_frame_id": source_frame_id,
                "source_filename": source_filename,
            }
        )
    if set(matched_poses) != {
        str(binding["source_filename"]) for binding in expected_bindings
    }:
        raise ValueError(
            "BlenderProc GT frame bindings do not exactly cover matched robot poses"
        )
    assert resolution is not None
    expected_resolution = {"width": resolution[0], "height": resolution[1]}
    expected_source_hashes = {
        MATCH_ROBOT_EE_POSES: _sha256_file(sensor_folder / MATCH_ROBOT_EE_POSES)
    }
    if provenance.get("source_artifact_sha256") != expected_source_hashes:
        raise ValueError(
            "Rendered GT provenance does not match the current matched "
            "robot-pose artifact"
        )
    if provenance.get("resolution") != expected_resolution:
        raise ValueError("Rendered GT provenance resolution does not match RGB-D")
    if provenance.get("frame_bindings") != expected_bindings:
        raise ValueError(
            "Rendered GT provenance frame bindings do not match exported RGB-D "
            "and matched-pose keys"
        )

    frame_contract_path = prepared_folder / "frame_contract.json"
    frame_contract = _load_json_if_present(frame_contract_path)
    if frame_contract != {
        "schema_version": "blenderproc_frame_contract.v1",
        "annotation_mode": annotation_mode,
        "projection": projection,
        "resolution": expected_resolution,
        "source_artifact_sha256": expected_source_hashes,
        "frames": expected_bindings,
    }:
        raise ValueError("Prepared BlenderProc frame contract no longer matches GT")
    input_names = (
        "camera_matrix.npy",
        "camera_poses.npy",
        "frame_contract.json",
        "objects.json",
    )
    expected_input_hashes = {
        name: _sha256_file(
            _plain_child_file(
                prepared_folder,
                name,
                label=f"BlenderProc input {name}",
            )
        )
        for name in input_names
    }
    if provenance.get("input_sha256") != expected_input_hashes:
        raise ValueError("Rendered GT provenance input hashes no longer match")

    camera_matrix = np.load(
        prepared_folder / "camera_matrix.npy",
        allow_pickle=False,
    )
    if (
        camera_matrix.shape != (3, 3)
        or not np.all(np.isfinite(camera_matrix))
        or not np.allclose(
            camera_matrix,
            np.asarray(cam_k, dtype=float).reshape(3, 3),
            rtol=0.0,
            atol=1e-9,
        )
    ):
        raise ValueError(
            "Prepared BlenderProc camera matrix does not match BOP scene camera"
        )
    camera_poses = np.load(
        prepared_folder / "camera_poses.npy",
        allow_pickle=False,
    )
    if camera_poses.shape != (len(frame_pairs), 4, 4) or not np.all(
        np.isfinite(camera_poses)
    ):
        raise ValueError("Prepared BlenderProc camera poses have an invalid shape")
    camera_to_mount = pt.transform_from(
        pr.matrix_from_quaternion(
            np.asarray(
                calibration_profile.extrinsics.rotation_quaternion_wxyz,
                dtype=float,
            )
        ),
        np.asarray(calibration_profile.extrinsics.translation_mm, dtype=float),
    )
    expected_camera_poses = []
    for index, robot_pose in enumerate(robot_pose_records):
        if calibration_profile.mounting_mode.value == "static":
            camera_to_template = camera_to_mount
        else:
            try:
                translation = np.asarray(
                    [float(robot_pose[key]) for key in ("X", "Y", "Z")],
                    dtype=float,
                )
                euler = np.asarray(
                    [float(robot_pose[key]) for key in ("C", "B", "A")],
                    dtype=float,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Matched robot pose {index} is invalid for eye-in-hand GT"
                ) from exc
            if not np.all(np.isfinite(translation)) or not np.all(np.isfinite(euler)):
                raise ValueError(
                    f"Matched robot pose {index} is non-finite for eye-in-hand GT"
                )
            flange_to_template = pt.transform_from(
                pr.matrix_from_euler(euler, 0, 1, 2, True),
                translation,
            )
            camera_to_template = flange_to_template @ camera_to_mount
        camera_pose_metres = camera_to_template.copy()
        camera_pose_metres[:3, 3] /= 1000.0
        expected_camera_poses.append(camera_pose_metres)
    if not np.allclose(
        camera_poses,
        np.asarray(expected_camera_poses),
        rtol=0.0,
        atol=1e-9,
    ):
        raise ValueError(
            "Prepared BlenderProc camera poses do not match current matched robot "
            "poses and the selected calibration extrinsics"
        )
    for index, pose in enumerate(camera_poses):
        _rigid_rotation(
            pose[:3, :3],
            label=f"camera_poses.npy[{index}] rotation",
        )
        if not np.allclose(
            pose[3],
            np.asarray([0.0, 0.0, 0.0, 1.0]),
            rtol=0.0,
            atol=1e-9,
        ):
            raise ValueError(
                f"camera_poses.npy[{index}] is not a homogeneous transform"
            )

    objects = _load_json_if_present(prepared_folder / "objects.json")
    if (
        not isinstance(objects, Mapping)
        or objects.get("schema_version") != "blenderproc_object_instances.v1"
        or not isinstance(objects.get("instances"), list)
    ):
        raise ValueError("Prepared BlenderProc object-instance input is invalid")
    object_records = provenance.get("object_files")
    if not isinstance(object_records, list) or len(object_records) != len(
        objects["instances"]
    ):
        raise ValueError("Rendered GT object-file provenance is incomplete")
    objects_folder = prepared_folder / "objects"
    object_transforms: list[np.ndarray] = []
    for index, (instance, record) in enumerate(
        zip(objects["instances"], object_records, strict=True)
    ):
        if not isinstance(instance, Mapping) or not isinstance(record, Mapping):
            raise ValueError(f"Rendered GT object provenance row {index} is invalid")
        mesh_path = _plain_child_file(
            objects_folder,
            instance.get("mesh"),
            label=f"BlenderProc object {index} mesh",
        )
        transform_path = _plain_child_file(
            objects_folder,
            instance.get("transform"),
            label=f"BlenderProc object {index} transform",
        )
        transform = np.load(transform_path, allow_pickle=False)
        if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
            raise ValueError(
                f"BlenderProc object {index} transform has an invalid shape"
            )
        _rigid_rotation(
            transform[:3, :3],
            label=f"BlenderProc object {index} transform rotation",
        )
        if not np.allclose(
            transform[3],
            np.asarray([0.0, 0.0, 0.0, 1.0]),
            rtol=0.0,
            atol=1e-9,
        ):
            raise ValueError(f"BlenderProc object {index} transform is not homogeneous")
        object_transforms.append(transform)
        expected_record = {
            "instance_uuid": instance.get("instance_uuid"),
            "mesh": instance.get("mesh"),
            "mesh_sha256": _sha256_file(mesh_path),
            "transform": instance.get("transform"),
            "transform_sha256": _sha256_file(transform_path),
        }
        if instance.get("texture"):
            texture_path = _plain_child_file(
                objects_folder,
                instance["texture"],
                label=f"BlenderProc object {index} texture",
            )
            expected_record.update(
                {
                    "texture": instance["texture"],
                    "texture_sha256": _sha256_file(texture_path),
                }
            )
        if dict(record) != expected_record:
            raise ValueError(
                f"Rendered GT object-file provenance row {index} no longer matches"
            )

    expected_scene_keys = {str(index) for index in range(len(camera_poses))}
    if set(scene_gt) != expected_scene_keys:
        raise ValueError("Rendered scene_gt does not cover every provenance frame")
    for image_id, template_from_camera in enumerate(camera_poses):
        annotations = scene_gt[str(image_id)]
        if not isinstance(annotations, list) or len(annotations) != len(
            object_transforms
        ):
            raise ValueError(
                f"Rendered scene_gt frame {image_id} does not cover every object"
            )
        camera_from_template = np.linalg.inv(template_from_camera)
        for gt_id, (annotation, instance, template_from_object) in enumerate(
            zip(
                annotations,
                objects["instances"],
                object_transforms,
                strict=True,
            )
        ):
            if not isinstance(annotation, Mapping):
                raise ValueError(
                    f"Rendered scene_gt frame {image_id}, row {gt_id} is invalid"
                )
            camera_from_object = camera_from_template @ template_from_object
            expected_rotation = camera_from_object[:3, :3].reshape(-1)
            expected_translation = camera_from_object[:3, 3] * 1000.0
            if (
                int(annotation.get("obj_id", -1)) != int(instance["obj_id"])
                or not np.allclose(
                    np.asarray(annotation.get("cam_R_m2c"), dtype=float),
                    expected_rotation,
                    rtol=0.0,
                    atol=1e-9,
                )
                or not np.allclose(
                    np.asarray(annotation.get("cam_t_m2c"), dtype=float),
                    expected_translation,
                    rtol=0.0,
                    atol=1e-8,
                )
            ):
                raise ValueError(
                    "Rendered scene_gt pose does not match the hashed analytic "
                    f"transform chain at frame {image_id}, row {gt_id}"
                )
    return provenance


def _official_bbox_from_mask(mask: np.ndarray) -> list[int]:
    """Return the BOP Toolkit's inclusive-coordinate bounding-box convention."""

    if not np.any(mask):
        return [-1, -1, -1, -1]
    ys, xs = np.where(mask)
    x_min = int(xs.min())
    y_min = int(ys.min())
    width = int(xs.max() - x_min)
    height = int(ys.max() - y_min)
    return [x_min, y_min, width, height]


def validate_scene_gt(
    scene_gt: Mapping[str, object],
    *,
    frame_count: int,
    object_name_to_id: Mapping[str, int] | None,
) -> None:
    expected_keys = {str(index) for index in range(frame_count)}
    actual_keys = {str(key) for key in scene_gt}
    if actual_keys != expected_keys:
        raise ValueError(
            "scene_gt image IDs must exactly match exported frames; "
            f"expected={sorted(expected_keys)}, actual={sorted(actual_keys)}"
        )
    known_ids = (
        set(object_name_to_id.values()) if object_name_to_id is not None else None
    )
    for image_id, image_annotations in scene_gt.items():
        if not isinstance(image_annotations, list):
            raise ValueError(f"scene_gt[{image_id!r}] must be a list")
        for annotation_index, annotation in enumerate(image_annotations):
            if not isinstance(annotation, Mapping):
                raise ValueError(
                    f"scene_gt[{image_id!r}][{annotation_index}] must be an object"
                )
            try:
                obj_id = int(annotation["obj_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid obj_id in scene_gt[{image_id!r}][{annotation_index}]"
                ) from exc
            if obj_id <= 0 or (known_ids is not None and obj_id not in known_ids):
                raise ValueError(f"Unknown BOP obj_id {obj_id} in scene_gt")
            for key, length in (("cam_R_m2c", 9), ("cam_t_m2c", 3)):
                values = annotation.get(key)
                if not isinstance(values, list) or len(values) != length:
                    raise ValueError(
                        f"scene_gt annotation {key} must contain {length} values"
                    )
                if not all(math.isfinite(float(value)) for value in values):
                    raise ValueError(f"scene_gt annotation {key} must be finite")
            _rigid_rotation(
                annotation["cam_R_m2c"],
                label=(f"scene_gt[{image_id!r}][{annotation_index}].cam_R_m2c"),
            )


def _strict_binary_mask(path: Path, *, expected_shape: tuple[int, int]) -> np.ndarray:
    image = cv2.imread(path.as_posix(), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"BOP mask PNG is unreadable: {path}")
    if image.dtype != np.uint8 or image.ndim != 2:
        raise ValueError(f"BOP mask must be a single-channel uint8 PNG: {path}")
    if tuple(image.shape) != expected_shape:
        raise ValueError(
            f"BOP mask dimensions do not match the scene depth image: {path}"
        )
    values = set(int(value) for value in np.unique(image))
    if not values.issubset({0, 255}):
        raise ValueError(f"BOP mask pixels must be exactly binary 0/255: {path}")
    return image == 255


def _validate_scene_gt_info_row(
    info: object,
    *,
    label: str,
    full_mask: np.ndarray,
    visible_mask: np.ndarray,
    depth: np.ndarray,
) -> None:
    if not isinstance(info, Mapping) or set(info) != SCENE_GT_INFO_FIELDS:
        raise ValueError(
            f"{label} must contain exactly the official BOP scene_gt_info fields"
        )
    for key in ("bbox_obj", "bbox_visib"):
        bbox = info[key]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(value) is not int for value in bbox)
        ):
            raise ValueError(f"{label}.{key} must contain exactly four integers")
    for key in ("px_count_all", "px_count_valid", "px_count_visib"):
        value = info[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"{label}.{key} must be a non-negative integer")
    visibility = info["visib_fract"]
    if type(visibility) not in {int, float}:
        raise ValueError(f"{label}.visib_fract must be numeric")
    visibility = float(visibility)
    if not math.isfinite(visibility) or not 0.0 <= visibility <= 1.0:
        raise ValueError(f"{label}.visib_fract must be between 0 and 1")

    if np.any(visible_mask & ~full_mask):
        raise ValueError(f"{label} visible mask must be a subset of the full mask")
    full_count = int(np.count_nonzero(full_mask))
    visible_count = int(np.count_nonzero(visible_mask))
    valid_count = int(np.count_nonzero(full_mask & (depth > 0)))
    if info["px_count_all"] < full_count:
        raise ValueError(
            f"{label}.px_count_all must include the complete rendered silhouette"
        )
    if info["px_count_valid"] != valid_count:
        raise ValueError(f"{label}.px_count_valid does not match mask/depth evidence")
    if info["px_count_visib"] != visible_count:
        raise ValueError(f"{label}.px_count_visib does not match mask evidence")
    expected_fraction = (
        visible_count / float(info["px_count_all"]) if info["px_count_all"] else 0.0
    )
    if not math.isclose(
        visibility,
        expected_fraction,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError(f"{label}.visib_fract does not match official pixel counts")
    if info["bbox_visib"] != _official_bbox_from_mask(visible_mask):
        raise ValueError(f"{label}.bbox_visib does not match the visible mask")
    if visible_count == 0:
        if info["bbox_obj"] != [-1, -1, -1, -1]:
            raise ValueError(f"{label}.bbox_obj must use the official empty sentinel")
    else:
        full_bbox = _official_bbox_from_mask(full_mask)
        object_bbox = info["bbox_obj"]
        if object_bbox[2] < 0 or object_bbox[3] < 0:
            raise ValueError(f"{label}.bbox_obj must have non-negative dimensions")
        if (
            object_bbox[0] > full_bbox[0]
            or object_bbox[1] > full_bbox[1]
            or object_bbox[0] + object_bbox[2] < full_bbox[0] + full_bbox[2]
            or object_bbox[1] + object_bbox[3] < full_bbox[1] + full_bbox[3]
        ):
            raise ValueError(f"{label}.bbox_obj must enclose the in-frame full mask")
        if info["px_count_all"] == full_count and object_bbox != full_bbox:
            raise ValueError(f"{label}.bbox_obj does not match the full mask")
        if info["px_count_all"] > full_count:
            touches_boundary = bool(
                np.any(full_mask[0, :])
                or np.any(full_mask[-1, :])
                or np.any(full_mask[:, 0])
                or np.any(full_mask[:, -1])
            )
            if not touches_boundary:
                raise ValueError(
                    f"{label}.px_count_all indicates truncation but the full "
                    "mask does not touch an image boundary"
                )


def validate_official_scene_annotations(
    scene_folder: str | Path,
) -> dict[str, int]:
    """Validate a complete official BOP Toolkit mask/info bundle."""

    scene_folder = Path(scene_folder)
    scene_gt = _load_json_if_present(scene_folder / "scene_gt.json")
    scene_gt_info = _load_json_if_present(scene_folder / "scene_gt_info.json")
    if not isinstance(scene_gt, Mapping):
        raise ValueError(f"Missing or invalid scene_gt.json: {scene_folder}")
    if not isinstance(scene_gt_info, Mapping) or set(scene_gt_info) != set(scene_gt):
        raise ValueError(
            f"scene_gt_info.json must exactly match scene_gt image IDs: {scene_folder}"
        )

    mask_folder = scene_folder / "mask"
    mask_visib_folder = scene_folder / "mask_visib"
    expected_names: set[str] = set()
    annotation_count = 0
    for image_id, image_annotations in scene_gt.items():
        if not isinstance(image_annotations, list):
            raise ValueError(f"scene_gt[{image_id!r}] must contain a list")
        infos = scene_gt_info[image_id]
        if not isinstance(infos, list) or len(infos) != len(image_annotations):
            raise ValueError(f"scene_gt_info does not match scene_gt image {image_id}")
        depth_path = scene_folder / DEPTH_DIR / f"{int(image_id):06d}.png"
        depth = cv2.imread(depth_path.as_posix(), cv2.IMREAD_UNCHANGED)
        if depth is None or depth.dtype != np.uint16 or depth.ndim != 2:
            raise ValueError(
                f"BOP depth must be a single-channel uint16 PNG: {depth_path}"
            )
        for gt_id, info in enumerate(infos):
            filename = mask_filename(int(image_id), gt_id)
            expected_names.add(filename)
            full = _strict_binary_mask(
                mask_folder / filename,
                expected_shape=tuple(depth.shape),
            )
            visible = _strict_binary_mask(
                mask_visib_folder / filename,
                expected_shape=tuple(depth.shape),
            )
            _validate_scene_gt_info_row(
                info,
                label=f"scene_gt_info[{image_id!r}][{gt_id}]",
                full_mask=full,
                visible_mask=visible,
                depth=depth,
            )
            annotation_count += 1

    for folder, label in (
        (mask_folder, "full"),
        (mask_visib_folder, "visible"),
    ):
        if not folder.is_dir():
            raise ValueError(f"BOP {label} mask directory is missing: {folder}")
        entries = list(folder.iterdir())
        if any(not path.is_file() for path in entries):
            raise ValueError(
                f"BOP {label} mask directory may contain only mask PNG files"
            )
        actual_names = {path.name for path in entries}
        if actual_names != expected_names:
            raise ValueError(
                f"BOP {label} mask filenames must exactly match scene_gt: "
                f"expected={sorted(expected_names)}, actual={sorted(actual_names)}"
            )
    return {
        "annotation_count": annotation_count,
        "mask_count": len(expected_names),
        "visible_mask_count": len(expected_names),
    }


def finalize_official_scene_annotations(
    output_root: str | Path,
    exports: list[BopSceneExport],
) -> list[BopSceneExport]:
    """Bind official mask/info outputs to staged pose exports."""

    output_root = Path(output_root)
    finalized: list[BopSceneExport] = []
    for export in exports:
        if export.annotation_source != "blenderproc":
            raise ValueError("Official BOP masks require BlenderProc pose annotations")
        scene_folder = output_root / export.scene_folder
        validate_official_scene_annotations(scene_folder)
        scene_gt = _load_json_if_present(scene_folder / "scene_gt.json")
        scene_gt_info = _load_json_if_present(scene_folder / "scene_gt_info.json")
        assert isinstance(scene_gt, Mapping)
        assert isinstance(scene_gt_info, Mapping)
        artifacts = dict(export.artifacts)
        artifacts.update(
            {
                "scene_gt": (scene_folder / "scene_gt.json")
                .relative_to(output_root)
                .as_posix(),
                "scene_gt_info": (scene_folder / "scene_gt_info.json")
                .relative_to(output_root)
                .as_posix(),
                "mask": (scene_folder / "mask").relative_to(output_root).as_posix(),
                "mask_visib": (scene_folder / "mask_visib")
                .relative_to(output_root)
                .as_posix(),
            }
        )
        finalized.append(
            replace(
                export,
                artifacts=artifacts,
                targets=targets_from_scene_gt(
                    scene_gt,
                    scene_id=export.scene_id,
                    scene_gt_info=scene_gt_info,
                ),
                annotation_mode="pose_and_masks",
            )
        )
    return finalized


def camera_matrix_from_profile(
    profile: CalibrationProfile | None, *, projection: str = "native"
) -> list[float] | None:
    if profile is None:
        return None
    if projection == "rectified":
        if profile.rectified_intrinsics is None:
            raise ValueError(
                f"Calibration profile {profile.profile_id} has no rectified intrinsics"
            )
        return list(profile.rectified_intrinsics.cam_k)
    return list(profile.intrinsics.cam_k)


def depth_scale_from_profile(profile: CalibrationProfile | None) -> float | None:
    if profile is None:
        return None
    return float(profile.intrinsics.depth_scale_to_mm)


def export_sensor_scene_to_bop(
    sensor_folder: str | Path,
    output_root: str | Path,
    *,
    split: str = "test",
    scene_id: int = 1,
    overwrite: bool = False,
    calibration_profile: CalibrationProfile | None = None,
    object_name_to_id: Mapping[str, int] | None = None,
    template_instances: list[Mapping[str, object]] | None = None,
    source_projection: str | None = None,
    input_sensor_folder: str | None = None,
    authoritative_source_sensor_folder: str | None = None,
    input_fingerprint_sha256: str | None = None,
    authoritative_source_fingerprint_sha256: str | None = None,
    annotation_source: str = "none",
    annotation_mode: str,
) -> BopSceneExport:
    sensor_folder = Path(sensor_folder)
    output_root = Path(output_root)
    sensor_name = sensor_folder.name
    if not re.fullmatch(r"(?:train|val|test)(?:_[A-Za-z0-9.-]+)?", split):
        raise ValueError(f"Invalid BOP split name: {split!r}")
    if scene_id < 0:
        raise ValueError("BOP scene_id must be greater than or equal to 0")
    annotation_mode = resolve_annotation_mode(annotation_source, annotation_mode)
    if calibration_profile is not None:
        calibration_profile.validate()
        if calibration_profile.status != CalibrationStatus.VALID:
            raise ValueError(
                f"Calibration profile {calibration_profile.profile_id} is not valid"
            )
    scene_folder = output_root / split / f"{scene_id:06d}"
    if scene_folder.exists():
        if not overwrite:
            raise FileExistsError(
                f"BOP scene folder already exists: {scene_folder}; pass overwrite=True"
            )
        shutil.rmtree(scene_folder)

    rgb_dest = scene_folder / RGB_DIR
    depth_dest = scene_folder / DEPTH_DIR
    rgb_dest.mkdir(parents=True)
    depth_dest.mkdir(parents=True)

    detected_projection = (
        "rectified"
        if (sensor_folder / "rectification_provenance.json").is_file()
        else "native"
    )
    projection = source_projection or detected_projection
    if projection not in {"native", "rectified"}:
        raise ValueError(f"Unsupported BOP input projection: {projection!r}")
    if source_projection is not None and projection != detected_projection:
        raise ValueError(
            "Declared BOP input projection does not match sensor-folder "
            f"provenance: declared={projection!r}, detected={detected_projection!r}"
        )
    input_sensor_folder_value = (
        input_sensor_folder if input_sensor_folder is not None else sensor_folder.name
    )
    authoritative_source_folder_value = (
        authoritative_source_sensor_folder or input_sensor_folder_value
    )
    source_cam_k = read_camera_matrix(sensor_folder / CAM_K)
    profile_cam_k = camera_matrix_from_profile(
        calibration_profile,
        projection=projection,
    )
    if profile_cam_k is not None and not np.allclose(
        np.asarray(source_cam_k, dtype=float),
        np.asarray(profile_cam_k, dtype=float),
        rtol=0.0,
        atol=1e-6,
    ):
        raise ValueError(
            "BOP input camera matrix does not match the selected calibration "
            f"profile for {projection} projection"
        )
    cam_k = profile_cam_k or source_cam_k
    source_depth_scale_path = sensor_folder / DEPTH_SCALE
    source_depth_scale = read_depth_scale(source_depth_scale_path)
    depth_scale = depth_scale_from_profile(calibration_profile)
    if calibration_profile is not None:
        if not math.isclose(
            float(source_depth_scale),
            float(depth_scale),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "BOP input depth scale does not match the selected calibration profile"
            )
    if depth_scale is None:
        depth_scale = source_depth_scale
    if len(cam_k) != 9 or not all(math.isfinite(float(value)) for value in cam_k):
        raise ValueError("BOP camera matrix must contain 9 finite values")
    if float(cam_k[0]) <= 0 or float(cam_k[4]) <= 0:
        raise ValueError("BOP camera focal lengths must be positive")
    if not math.isfinite(float(depth_scale)) or float(depth_scale) <= 0:
        raise ValueError("BOP depth scale must be finite and positive")
    frame_map: dict[str, dict[str, str | int]] = {}
    scene_camera: dict[str, dict[str, object]] = {}

    frame_pairs = _frame_pairs(sensor_folder)
    for image_id, (rgb_source, depth_source) in enumerate(frame_pairs):
        rgb_image, _depth_image = _read_rgbd_pair(rgb_source, depth_source)
        if calibration_profile is not None:
            expected_intrinsics = (
                calibration_profile.rectified_intrinsics
                if projection == "rectified"
                else calibration_profile.intrinsics
            )
            assert expected_intrinsics is not None
            actual_size = (int(rgb_image.shape[1]), int(rgb_image.shape[0]))
            expected_size = (
                int(expected_intrinsics.width),
                int(expected_intrinsics.height),
            )
            if actual_size != expected_size:
                raise ValueError(
                    "BOP RGB-D dimensions do not match the selected calibration "
                    f"profile: actual={actual_size}, expected={expected_size}"
                )
        image_name = f"{image_id:06d}.png"
        shutil.copy2(rgb_source, rgb_dest / image_name)
        shutil.copy2(depth_source, depth_dest / image_name)
        image_id_key = str(image_id)
        scene_camera[image_id_key] = {
            "cam_K": cam_k,
            "depth_scale": depth_scale,
        }
        source_rgb_relative = rgb_source.relative_to(sensor_folder).as_posix()
        source_depth_relative = depth_source.relative_to(sensor_folder).as_posix()
        frame_map[image_id_key] = {
            "source_rgb": source_rgb_relative,
            "source_depth": source_depth_relative,
            "bop_rgb": f"{RGB_DIR}/{image_name}",
            "bop_depth": f"{DEPTH_DIR}/{image_name}",
        }

    scene_gt: dict[str, object] | None = None
    targets: list[dict[str, int]] = []
    instance_map: list[dict] = []
    gt_provenance: dict[str, object] | None = None
    if annotation_source == "blenderproc":
        scene_gt = load_blenderproc_scene_json(sensor_folder, "scene_gt.json")
        if scene_gt is None:
            raise FileNotFoundError(
                "BlenderProc annotations were requested but scene_gt.json is missing "
                f"below {blenderproc_output_folder(sensor_folder)}"
            )
        scene_gt = normalize_scene_gt_object_ids(scene_gt, object_name_to_id)
        validate_scene_gt(
            scene_gt,
            frame_count=len(frame_pairs),
            object_name_to_id=object_name_to_id,
        )
        if calibration_profile is None:
            raise ValueError(
                "BlenderProc GT export requires a selected valid calibration "
                "profile for every sensor"
            )
        gt_provenance = validate_blenderproc_gt_provenance(
            sensor_folder,
            frame_pairs=frame_pairs,
            annotation_mode=annotation_mode,
            projection=projection,
            cam_k=[float(value) for value in cam_k],
            scene_gt=scene_gt,
            calibration_profile=calibration_profile,
        )
        targets = targets_from_scene_gt(
            scene_gt,
            scene_id=scene_id,
        )
    elif template_instances is not None:
        targets = targets_from_template_instances(
            template_instances,
            scene_id=scene_id,
            frame_count=len(frame_pairs),
        )
    if template_instances is not None and scene_gt is not None:
        assert gt_provenance is not None
        rendered_identity = load_blenderproc_scene_json(
            sensor_folder, "posetestbot_render_instances.json"
        )
        if rendered_identity is None:
            raise FileNotFoundError(
                "Pose-template BOP export requires posetestbot_render_instances.json"
            )
        if (
            rendered_identity.get("schema_version") != "posetestbot_render_instances.v1"
            or rendered_identity.get("blenderproc_version") != "2.8.0"
            or rendered_identity.get("annotation_mode") != annotation_mode
            or rendered_identity.get("identity_contract")
            != "bop_gt_index_matches_loaded_instance_order.v1"
        ):
            raise ValueError(
                "Rendered pose-template instance identity evidence is unsupported"
            )
        rendered_instances = rendered_identity.get("instances")
        expected_instances = [
            {
                "instance_uuid": item["instance_uuid"],
                "catalog_uuid": item["catalog_uuid"],
                "obj_id": int(item["obj_id"]),
                "name": item["name"],
                "mesh": f"{item['instance_uuid']}.ply",
                "transform": f"{item['instance_uuid']}.npy",
                "texture": (
                    f"{item['instance_uuid']}.png" if item.get("texture") else None
                ),
            }
            for item in template_instances
        ]
        if rendered_instances != expected_instances:
            raise ValueError(
                "Rendered instance list does not match object_instances.v1"
            )
        identity_frames = rendered_identity.get("frames")
        if rendered_identity.get("frame_bindings") != gt_provenance.get(
            "frame_bindings"
        ):
            raise ValueError(
                "Rendered instance identity frame bindings do not match GT provenance"
            )
        if not isinstance(identity_frames, Mapping) or set(identity_frames) != set(
            scene_gt
        ):
            raise ValueError("Rendered instance identity frames do not match scene_gt")
        for image_id, annotations in sorted(
            scene_gt.items(), key=lambda item: int(item[0])
        ):
            identities = identity_frames.get(image_id)
            if not isinstance(identities, list) or len(identities) != len(annotations):
                raise ValueError(
                    f"Rendered identity count does not match scene_gt frame {image_id}"
                )
            for gt_index, (annotation, identity) in enumerate(
                zip(annotations, identities, strict=True)
            ):
                if (
                    not isinstance(identity, Mapping)
                    or int(identity.get("gt_id", -1)) != gt_index
                ):
                    raise ValueError(
                        f"Invalid rendered GT identity at frame {image_id}, index {gt_index}"
                    )
                obj_id = int(annotation["obj_id"])
                if int(identity.get("obj_id", -1)) != obj_id:
                    raise ValueError(
                        f"Rendered identity obj_id does not match scene_gt frame {image_id}, index {gt_index}"
                    )
                instance_map.append(
                    {
                        "scene_id": scene_id,
                        "im_id": int(image_id),
                        "gt_id": gt_index,
                        "obj_id": obj_id,
                        "instance_uuid": identity["instance_uuid"],
                        "catalog_uuid": identity["catalog_uuid"],
                    }
                )

    artifacts = {
        "scene_camera": _write_json(scene_folder / "scene_camera.json", scene_camera)
    }
    if scene_gt is not None:
        artifacts["scene_gt"] = _write_json(
            scene_folder / "scene_gt.json",
            scene_gt,
        )
    annotation_provenance: dict[str, object] = {}
    if gt_provenance is not None:
        provenance_source = (
            blenderproc_output_folder(sensor_folder) / "posetestbot_gt_provenance.json"
        )
        provenance_destination = scene_folder / "posetestbot_gt_provenance.json"
        shutil.copy2(provenance_source, provenance_destination)
        if _load_json_if_present(provenance_destination) != gt_provenance:
            raise RuntimeError(
                "BlenderProc GT provenance changed while the BOP scene was exported"
            )
        artifacts["gt_provenance"] = provenance_destination
        annotation_provenance = {
            "artifact": provenance_destination.relative_to(output_root).as_posix(),
            "sha256": _sha256_file(provenance_destination),
            "schema_version": gt_provenance["schema_version"],
            "blenderproc_version": gt_provenance["blenderproc_version"],
            "annotation_mode": gt_provenance["annotation_mode"],
            "pose_contract": gt_provenance["pose_contract"],
            "analytic_implementation": gt_provenance["analytic_implementation"],
            "calibration_profile_id": calibration_profile.profile_id,
            "frame_binding_count": len(gt_provenance["frame_bindings"]),
            "scene_gt_sha256": _sha256_file(scene_folder / "scene_gt.json"),
        }

    return BopSceneExport(
        sensor_name=sensor_name,
        scene_id=scene_id,
        split=split,
        scene_folder=scene_folder.relative_to(output_root).as_posix(),
        rgb_count=len(frame_pairs),
        depth_count=len(frame_pairs),
        artifacts={
            key: path.relative_to(output_root).as_posix()
            for key, path in artifacts.items()
        },
        calibration_profile_id=(
            calibration_profile.profile_id if calibration_profile is not None else None
        ),
        targets=targets,
        frame_map=frame_map,
        instance_map=instance_map,
        projection=projection,
        input_sensor_folder=input_sensor_folder_value,
        authoritative_source_sensor_folder=(authoritative_source_folder_value),
        input_fingerprint_sha256=input_fingerprint_sha256,
        authoritative_source_fingerprint_sha256=(
            authoritative_source_fingerprint_sha256
        ),
        annotation_source=annotation_source,
        annotation_mode=annotation_mode,
        annotation_provenance=annotation_provenance,
    )


def targets_filename(split: str) -> str:
    if split == "test":
        return BOP_TARGETS_BOP19
    return f"{split}_targets_bop19.json"


def write_bop_targets(
    output_root: str | Path, exports: list[BopSceneExport], *, split: str
) -> Path:
    output_root = Path(output_root)
    targets = [
        target
        for export in exports
        for target in export.targets or []
        if export.split == split
    ]
    return _write_json(output_root / targets_filename(split), targets)


def write_bop_frame_map(output_root: str | Path, exports: list[BopSceneExport]) -> Path:
    output_root = Path(output_root)
    scenes = {}
    for export in sorted(exports, key=lambda item: item.scene_id):
        scene = {
            "sensor_name": export.sensor_name,
            "split": export.split,
            "scene_folder": export.scene_folder,
            "projection": export.projection,
            "input_sensor_folder": export.input_sensor_folder,
            "authoritative_source_sensor_folder": (
                export.authoritative_source_sensor_folder
            ),
            "input_fingerprint_sha256": export.input_fingerprint_sha256,
            "authoritative_source_fingerprint_sha256": (
                export.authoritative_source_fingerprint_sha256
            ),
            "frames": export.frame_map,
        }
        scenes[str(export.scene_id)] = {
            key: value for key, value in scene.items() if value is not None
        }
    return _write_json(
        output_root / BOP_FRAME_MAP_JSON,
        {"schema_version": FRAME_MAP_SCHEMA_VERSION, "scenes": scenes},
    )


def write_bop_instance_map(
    output_root: str | Path, exports: list[BopSceneExport]
) -> Path:
    return _write_json(
        Path(output_root) / BOP_INSTANCE_MAP,
        {
            "schema_version": "posetestbot_bop_instance_map.v1",
            "instances": [item for export in exports for item in export.instance_map],
        },
    )


def write_bop_pose_template(
    output_root: str | Path,
    selection: Mapping[str, object],
) -> Path:
    return _write_json(
        Path(output_root) / BOP_POSE_TEMPLATE,
        {
            "schema_version": "posetestbot_pose_template.v1",
            "template_uuid": selection["template_uuid"],
            "bundle_sha256": selection["bundle_sha256"],
            "configuration_sha256": selection["configuration_sha256"],
            "template_base_from_pose_template": selection[
                "template_base_from_pose_template"
            ],
            "print_compensation": selection["print_compensation"],
            "source": selection["source"],
            "catalog_snapshot": selection["catalog_snapshot"],
            "operator": selection["operator"],
            "selected_at": selection["selected_at"],
        },
    )


def write_bop_dataset_info(
    output_root: str | Path,
    exports: list[BopSceneExport],
    *,
    dataset_name: str,
    generated_at: str,
) -> Path:
    output_root = Path(output_root)
    splits = sorted({export.split for export in exports})
    return _write_json(
        output_root / BOP_DATASET_INFO,
        {
            "schema_version": DATASET_INFO_SCHEMA_VERSION,
            "name": dataset_name,
            "description": "BOP-scenewise dataset exported by PoseTestBot",
            "bop_format": "scenewise",
            "splits": splits,
            "scene_count": len(exports),
            "sensors": sorted({export.sensor_name for export in exports}),
            "generated_at": generated_at,
        },
    )


def validate_bop_dataset(
    output_root: str | Path,
    exports: list[BopSceneExport],
    *,
    object_models: list[BopObjectModel] | None = None,
    targets_path: str | Path | None = None,
) -> dict[str, object]:
    output_root = Path(output_root)
    annotation_sources = {export.annotation_source for export in exports}
    if len(annotation_sources) != 1:
        raise ValueError("BOP datasets require scenes with one annotation source")
    annotation_source = next(iter(annotation_sources))
    annotation_modes = {export.annotation_mode for export in exports}
    if len(annotation_modes) != 1:
        raise ValueError("BOP datasets require scenes with one annotation mode")
    annotation_mode = next(iter(annotation_modes))
    resolve_annotation_mode(annotation_source, annotation_mode)
    scene_ids: set[int] = set()
    scene_image_ids: dict[int, set[int]] = {}
    scene_object_ids: dict[tuple[int, int], set[int]] = {}
    annotation_count = 0
    for export in exports:
        if export.scene_id in scene_ids:
            raise ValueError(f"Duplicate BOP scene ID: {export.scene_id}")
        scene_ids.add(export.scene_id)
        scene_folder = output_root / export.scene_folder
        rgb_names = {path.name for path in (scene_folder / RGB_DIR).glob("*.png")}
        depth_names = {path.name for path in (scene_folder / DEPTH_DIR).glob("*.png")}
        if rgb_names != depth_names or len(rgb_names) != export.rgb_count:
            raise ValueError(f"BOP scene frame sets are inconsistent: {scene_folder}")
        image_ids = {int(Path(name).stem) for name in rgb_names}
        scene_image_ids[export.scene_id] = image_ids
        scene_camera = _load_json_if_present(scene_folder / "scene_camera.json")
        expected_keys = {str(image_id) for image_id in image_ids}
        if not isinstance(scene_camera, Mapping) or set(scene_camera) != expected_keys:
            raise ValueError(
                f"scene_camera keys do not match scene images: {scene_folder}"
            )
        scene_gt_path = scene_folder / "scene_gt.json"
        scene_gt_info_path = scene_folder / "scene_gt_info.json"
        mask_path = scene_folder / "mask"
        mask_visib_path = scene_folder / "mask_visib"
        if annotation_mode == "none":
            if export.annotation_provenance:
                raise ValueError("Annotation-free BOP scenes must omit GT provenance")
            if (
                scene_gt_path.exists()
                or scene_gt_info_path.exists()
                or mask_path.exists()
                or mask_visib_path.exists()
            ):
                raise ValueError(
                    "Annotation-free BOP scenes must omit all GT and mask "
                    f"artifacts: {scene_folder}"
                )
            for image_id in image_ids:
                scene_object_ids[(export.scene_id, image_id)] = set()
            continue

        provenance_summary = export.annotation_provenance
        if not isinstance(provenance_summary, Mapping):
            raise ValueError(
                f"Annotated BOP scene has no GT provenance: {scene_folder}"
            )
        provenance_relative = provenance_summary.get("artifact")
        if not isinstance(provenance_relative, str):
            raise ValueError(
                f"Annotated BOP scene has no GT provenance artifact: {scene_folder}"
            )
        provenance_path = output_root / provenance_relative
        try:
            provenance_path.resolve(strict=True).relative_to(output_root.resolve())
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Annotated BOP scene GT provenance is missing or escapes: "
                f"{scene_folder}"
            ) from exc
        if (
            provenance_summary.get("sha256") != _sha256_file(provenance_path)
            or export.artifacts.get("gt_provenance") != provenance_relative
            or provenance_summary.get("scene_gt_sha256") != _sha256_file(scene_gt_path)
        ):
            raise ValueError(
                f"Annotated BOP scene GT provenance hash/path mismatch: {scene_folder}"
            )
        published_provenance = _load_json_if_present(provenance_path)
        published_bindings = (
            published_provenance.get("frame_bindings")
            if isinstance(published_provenance, Mapping)
            else None
        )
        if (
            not isinstance(published_provenance, Mapping)
            or published_provenance.get("schema_version")
            != "posetestbot_gt_provenance.v1"
            or published_provenance.get("annotation_mode") != annotation_mode
            or not isinstance(published_bindings, list)
            or len(published_bindings) != len(image_ids)
        ):
            raise ValueError(
                f"Annotated BOP scene GT provenance is inconsistent: {scene_folder}"
            )

        scene_gt = _load_json_if_present(scene_gt_path)
        if not isinstance(scene_gt, Mapping) or set(scene_gt) != expected_keys:
            raise ValueError(f"scene_gt keys do not match scene images: {scene_folder}")
        assert isinstance(scene_gt, Mapping)
        scene_gt_info = None
        if annotation_mode == "pose":
            if (
                scene_gt_info_path.exists()
                or mask_path.exists()
                or mask_visib_path.exists()
            ):
                raise ValueError(
                    "Pose-only BOP scenes must omit GT visibility and mask "
                    f"artifacts: {scene_folder}"
                )
        else:
            validate_official_scene_annotations(scene_folder)
            scene_gt_info = _load_json_if_present(scene_gt_info_path)
            assert isinstance(scene_gt_info, Mapping)
        for image_id, image_annotations in scene_gt.items():
            if not isinstance(image_annotations, list):
                raise ValueError(f"scene_gt[{image_id!r}] must contain a list")
            if scene_gt_info is not None:
                annotation_infos = scene_gt_info[image_id]
                if not isinstance(annotation_infos, list) or len(
                    annotation_infos
                ) != len(image_annotations):
                    raise ValueError(
                        f"scene_gt_info does not match scene_gt image {image_id}"
                    )
            annotation_count += len(image_annotations)
            object_ids = {
                int(annotation["obj_id"])
                for annotation in image_annotations
                if isinstance(annotation, Mapping) and "obj_id" in annotation
            }
            scene_object_ids[(export.scene_id, int(image_id))] = object_ids

    model_ids = {model.obj_id for model in object_models or []}
    if object_models:
        models_info = _load_json_if_present(
            output_root / MODELS_DIR / "models_info.json"
        )
        models_eval_info = _load_json_if_present(
            output_root / MODELS_EVAL_DIR / "models_info.json"
        )
        if (
            not isinstance(models_info, Mapping)
            or {int(key) for key in models_info} != model_ids
            or models_eval_info != models_info
        ):
            raise ValueError(
                "models/models_info.json and models_eval/models_info.json must "
                "match the exported object models"
            )
        for model in object_models:
            if not (output_root / model.bop_path).is_file():
                raise FileNotFoundError(f"Missing BOP object model: {model.bop_path}")
            if not (output_root / model.bop_eval_path).is_file():
                raise FileNotFoundError(
                    f"Missing BOP evaluation model: {model.bop_eval_path}"
                )
            validate_bop_model_ply(output_root / model.bop_path)
            validate_bop_model_ply(output_root / model.bop_eval_path)
            if (
                model.texture_path is not None
                and not (output_root / model.texture_path).is_file()
            ):
                raise FileNotFoundError(
                    f"Missing BOP object texture: {model.texture_path}"
                )

    expected_targets = [
        target
        for export in exports
        for target in export.targets or []
        if export.split == "test"
    ]
    target_count = 0
    if annotation_mode == "none":
        if model_ids and not expected_targets:
            raise ValueError(
                "Annotation-free object models require confirmed pose-template targets"
            )
        if expected_targets and model_ids and targets_path is None:
            raise ValueError(
                "Annotation-free object datasets require populated BOP19 targets"
            )
        if (not expected_targets or not model_ids) and targets_path is not None:
            raise ValueError(
                "Annotation-free datasets without exported models must omit "
                "BOP19 targets"
            )
    if targets_path is not None:
        targets = _load_json_if_present(Path(targets_path))
        if not isinstance(targets, list):
            raise ValueError("BOP targets must be a JSON list")
        if targets != expected_targets:
            raise ValueError(
                "BOP targets do not exactly match the exported scene target inventory"
            )
        target_count = len(targets)
        for target in targets:
            if not isinstance(target, Mapping):
                raise ValueError("Each BOP target must be a JSON object")
            scene_id = int(target["scene_id"])
            image_id = int(target["im_id"])
            obj_id = int(target["obj_id"])
            if image_id not in scene_image_ids.get(scene_id, set()):
                raise ValueError(f"BOP target references missing scene/image: {target}")
            if annotation_mode != "none" and obj_id not in scene_object_ids.get(
                (scene_id, image_id), set()
            ):
                raise ValueError(
                    f"BOP target references missing object instance: {target}"
                )
            if model_ids and obj_id not in model_ids:
                raise ValueError(f"BOP target references missing model: {target}")

    frame_count = sum(export.rgb_count for export in exports)
    pose_estimation_input = frame_count > 0 and bool(model_ids) and target_count > 0
    evaluation_ready = (
        annotation_mode == "pose_and_masks"
        and annotation_count > 0
        and target_count > 0
        and bool(model_ids)
    )
    return {
        "status": "ok",
        "scene_count": len(exports),
        "frame_count": frame_count,
        "model_count": len(object_models or []),
        "annotation_count": annotation_count,
        "target_count": target_count,
        "capabilities": {
            "bop_scenewise_rgbd": frame_count > 0,
            "pose_estimation_input": pose_estimation_input,
            "gt_annotations": annotation_mode != "none" and annotation_count > 0,
            "gt_poses": annotation_mode != "none" and annotation_count > 0,
            "gt_masks_full": annotation_mode == "pose_and_masks"
            and annotation_count > 0,
            "gt_masks_visible": annotation_mode == "pose_and_masks"
            and annotation_count > 0,
            "gt_visibility_info": annotation_mode == "pose_and_masks"
            and annotation_count > 0,
            "bop19_evaluation": evaluation_ready,
        },
    }


def _image_size(path: Path) -> tuple[int, int] | None:
    image = cv2.imread(path.as_posix(), cv2.IMREAD_UNCHANGED)
    if image is None:
        return None
    height, width = image.shape[:2]
    return int(width), int(height)


def _annotation_bbox(
    annotation: Mapping[str, object],
    annotation_info: object,
) -> list[float]:
    bbox = None
    if isinstance(annotation_info, Mapping):
        bbox = annotation_info.get("bbox_visib") or annotation_info.get("bbox_obj")
    if bbox is None:
        bbox = annotation.get("bbox_visib") or annotation.get("bbox_obj")
    if not isinstance(bbox, list | tuple) or len(bbox) < 4:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        return [float(value) for value in bbox[:4]]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0, 0.0]


def _annotation_area(
    bbox: list[float],
    annotation_info: object,
) -> float:
    if isinstance(annotation_info, Mapping):
        for key in ("px_count_visib", "px_count_valid", "px_count_all"):
            value = annotation_info.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return float(max(0.0, bbox[2]) * max(0.0, bbox[3]))


def _segmentation_from_mask(mask_path: Path) -> tuple[list[list[float]], int]:
    mask = mask_pixels(mask_path)
    if mask is None:
        return [], 0
    contours, _hierarchy = cv2.findContours(
        mask.astype(np.uint8),
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    segmentations: list[list[float]] = []
    for contour in contours:
        if len(contour) < 3:
            continue
        points = contour.reshape(-1, 2)
        segmentations.append(points.astype(float).reshape(-1).tolist())
    return segmentations, int(np.count_nonzero(mask))


def coco_annotations_from_exports(
    output_root: str | Path,
    exports: list[BopSceneExport],
    *,
    split: str,
    object_models: list[BopObjectModel] | None = None,
) -> dict[str, object]:
    output_root = Path(output_root)
    categories_by_id: dict[int, dict[str, object]] = {}
    for model in object_models or []:
        categories_by_id[model.obj_id] = {
            "id": model.obj_id,
            "name": model.object_name,
            "supercategory": "object",
        }

    images: list[dict[str, object]] = []
    annotations: list[dict[str, object]] = []
    annotation_id = 1
    image_id = 1

    for export in sorted(
        (export for export in exports if export.split == split),
        key=lambda export: (export.scene_id, export.sensor_name),
    ):
        scene_folder = output_root / export.scene_folder
        scene_gt = _load_json_if_present(scene_folder / "scene_gt.json")
        scene_gt_info = _load_json_if_present(scene_folder / "scene_gt_info.json")
        if not isinstance(scene_gt, Mapping):
            scene_gt = {}
        if not isinstance(scene_gt_info, Mapping):
            scene_gt_info = {}

        for rgb_path in sorted((scene_folder / RGB_DIR).glob("*.png")):
            try:
                bop_image_id = int(rgb_path.stem)
            except ValueError:
                continue
            size = _image_size(rgb_path)
            width, height = size if size is not None else (0, 0)
            image_record = {
                "id": image_id,
                "file_name": rgb_path.relative_to(output_root).as_posix(),
                "width": width,
                "height": height,
                "posetestbot": {
                    "scene_id": export.scene_id,
                    "im_id": bop_image_id,
                    "sensor_name": export.sensor_name,
                    "split": export.split,
                    "scene_folder": scene_folder.as_posix(),
                },
            }
            images.append(image_record)

            image_key = str(bop_image_id)
            image_annotations = scene_gt.get(image_key, [])
            image_annotation_infos = scene_gt_info.get(image_key, [])
            if not isinstance(image_annotations, list):
                image_id += 1
                continue
            if not isinstance(image_annotation_infos, list):
                image_annotation_infos = []

            for annotation_index, annotation in enumerate(image_annotations):
                if not isinstance(annotation, Mapping):
                    continue
                try:
                    category_id = int(annotation["obj_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                categories_by_id.setdefault(
                    category_id,
                    {
                        "id": category_id,
                        "name": f"obj_{category_id:06d}",
                        "supercategory": "object",
                    },
                )
                annotation_info = (
                    image_annotation_infos[annotation_index]
                    if annotation_index < len(image_annotation_infos)
                    else {}
                )
                bbox = _annotation_bbox(annotation, annotation_info)
                mask_path = (
                    scene_folder
                    / "mask"
                    / mask_filename(
                        bop_image_id,
                        annotation_index,
                    )
                )
                segmentation, mask_area = _segmentation_from_mask(mask_path)
                area = (
                    float(mask_area)
                    if mask_area
                    else _annotation_area(
                        bbox,
                        annotation_info,
                    )
                )
                annotations.append(
                    {
                        "id": annotation_id,
                        "image_id": image_id,
                        "category_id": category_id,
                        "bbox": bbox,
                        "area": area,
                        "iscrowd": 0,
                        "segmentation": segmentation,
                        "posetestbot": {
                            "scene_id": export.scene_id,
                            "im_id": bop_image_id,
                            "annotation_index": annotation_index,
                            "sensor_name": export.sensor_name,
                            "mask_path": (
                                mask_path.relative_to(output_root).as_posix()
                                if mask_path.is_file()
                                else None
                            ),
                            "bop_annotation": dict(annotation),
                            "bop_gt_info": (
                                dict(annotation_info)
                                if isinstance(annotation_info, Mapping)
                                else None
                            ),
                        },
                    }
                )
                annotation_id += 1
            image_id += 1

    return {
        "schema_version": "posetestbot_coco_annotations.v1",
        "info": {
            "description": "PoseTestBot COCO-style annotations derived from BOP export.",
            "source_layout": BOP_DIR,
            "split": split,
        },
        "images": images,
        "annotations": annotations,
        "categories": [categories_by_id[obj_id] for obj_id in sorted(categories_by_id)],
        "posetestbot": {
            "split": split,
            "scene_count": len(
                {export.scene_id for export in exports if export.split == split}
            ),
            "image_count": len(images),
            "annotation_count": len(annotations),
        },
    }


def write_bop_coco_annotations(
    output_root: str | Path,
    exports: list[BopSceneExport],
    *,
    split: str,
    object_models: list[BopObjectModel] | None = None,
) -> Path:
    output_root = Path(output_root)
    return _write_json(
        output_root / BOP_COCO_ANNOTATIONS,
        coco_annotations_from_exports(
            output_root,
            exports,
            split=split,
            object_models=object_models,
        ),
    )


def write_bop_export_manifest(
    output_root: str | Path,
    exports: list[BopSceneExport],
    *,
    calibration_profiles_path: str | Path | None = None,
    calibration_profiles: list[CalibrationProfile] | None = None,
    object_models: list[BopObjectModel] | None = None,
    targets_path: str | Path | None = None,
    coco_annotations_path: str | Path | None = None,
    frame_map_path: str | Path | None = None,
    dataset_info_path: str | Path | None = None,
    validation: Mapping[str, object] | None = None,
    stable_id_mapping: Mapping[str, int] | None = None,
    dataset_mode: str = "objectless",
    pose_template_provenance: Mapping[str, object] | None = None,
    instance_map_path: str | Path | None = None,
    pose_template_path: str | Path | None = None,
    annotation_source: str,
    annotation_mode: str,
    annotation_provenance: Mapping[str, object] | None = None,
) -> Path:
    output_root = Path(output_root)
    manifest_path = output_root / BOP_EXPORT_MANIFEST
    export_annotation_sources = {export.annotation_source for export in exports}
    if annotation_source not in ANNOTATION_SOURCES:
        raise ValueError(
            "BOP annotation_source must be one of: "
            + ", ".join(sorted(ANNOTATION_SOURCES))
        )
    if export_annotation_sources != {annotation_source}:
        raise ValueError(
            "BOP scene annotation sources must exactly match the manifest "
            f"annotation source: {sorted(export_annotation_sources)} != "
            f"{annotation_source!r}"
        )
    export_annotation_modes = {export.annotation_mode for export in exports}
    annotation_mode = resolve_annotation_mode(annotation_source, annotation_mode)
    if export_annotation_modes != {annotation_mode}:
        raise ValueError(
            "BOP scene annotation modes must exactly match the manifest "
            f"annotation mode: {sorted(export_annotation_modes)} != "
            f"{annotation_mode!r}"
        )

    def artifact_path(value: str | Path | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        try:
            return path.relative_to(output_root).as_posix()
        except ValueError:
            return path.as_posix()

    export_entries = []
    for export in exports:
        data = asdict(export)
        data.pop("frame_map", None)
        data.pop("instance_map", None)
        data.pop("targets", None)
        export_entries.append(
            {key: value for key, value in data.items() if value is not None}
        )
    _write_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "format": "bop-scenewise",
            "layout": "<split>/<scene_id>",
            "dataset_root": ".",
            "exports": export_entries,
            "calibration_profiles_path": (
                Path(calibration_profiles_path).as_posix()
                if calibration_profiles_path is not None
                else None
            ),
            "calibration_profiles": [
                profile_to_dict(profile) for profile in calibration_profiles or []
            ],
            "object_models": [asdict(model) for model in object_models or []],
            "objectless": dataset_mode == "objectless",
            "dataset_mode": dataset_mode,
            "annotation_source": annotation_source,
            "annotation_mode": annotation_mode,
            "annotation_state": {
                "none": "absent",
                "pose": "poses",
                "pose_and_masks": "complete",
            }[annotation_mode],
            "annotation_provenance": dict(annotation_provenance or {}),
            "capabilities": dict(
                (validation or {}).get("capabilities", {})
                if isinstance((validation or {}).get("capabilities"), Mapping)
                else {}
            ),
            "pose_template": dict(pose_template_provenance or {}),
            "stable_id_mapping": dict(stable_id_mapping or {}),
            "targets_path": artifact_path(targets_path),
            "coco_annotations_path": artifact_path(coco_annotations_path),
            "frame_map_path": artifact_path(frame_map_path),
            "instance_map_path": artifact_path(instance_map_path),
            "pose_template_path": artifact_path(pose_template_path),
            "dataset_info_path": artifact_path(dataset_info_path),
            "validation": dict(validation or {}),
        },
    )
    return manifest_path
