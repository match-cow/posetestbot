"""Read-only, run-scoped BOP pose-result inspection.

The adapter resolves only identifiers into the current exported BOP tree and an
immutable retained result.  It never writes visualization artifacts and is not
an acquisition or estimator stage.
"""

from __future__ import annotations

import csv
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import cv2
import numpy as np

from posetestbot.bop.evaluation import (
    RESULT_HEADER,
    _dataset_inventory,
    _has_symlink_component,
    _plain_file,
    _png_size,
    _sha256_file,
    get_result,
    inspect_dataset,
    list_results,
    public_dataset_descriptor,
    public_result_descriptor,
    result_file_path,
)
from posetestbot.sensors.registry import get_sensor_adapter, sensor_folder_name


INSPECTION_SETUP_SCHEMA = "bop_inspection_setup.v1"
INSPECTION_FRAME_LIST_SCHEMA = "bop_inspection_frame_list.v1"
INSPECTION_FRAME_SCHEMA = "bop_inspection_frame.v1"
MAX_SCENES = 128
MAX_OBJECTS = 1_000
MAX_FRAMES = 200_000
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 80
MAX_HYPOTHESES = 50
DEFAULT_HYPOTHESES = 20
MAX_MEDIA_BYTES = 64 * 1024 * 1024
MAX_MEDIA_PIXELS = 40_000_000
MAX_MODEL_BYTES = 64 * 1024 * 1024
MAX_INSTANCE_ROWS = 500_000
MAX_GT_INSTANCES_PER_FRAME = 1_000
MAX_CACHED_RESULTS = 3
RESULT_ID_RE = re.compile(r"^result-[0-9a-f]{12}$")
FRAME_FILTERS = {
    "all",
    "estimated",
    "target",
    "missing_estimate",
    "registration",
    "tracking",
    "reinitialization",
}


def _load_json(path: Path, *, label: str) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid: {exc}") from exc


def _required_plain_file(path: Path, *, root: Path, label: str) -> Path:
    if not _plain_file(path, root=root):
        raise ValueError(f"{label} is missing, unsafe, or not a regular file")
    return path


def _finite_vector(value: Any, *, count: int, label: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != count
        or any(type(item) not in {int, float} for item in value)
    ):
        raise ValueError(f"{label} must contain exactly {count} numbers")
    normalized = [float(item) for item in value]
    if not all(math.isfinite(item) for item in normalized):
        raise ValueError(f"{label} must contain finite numbers")
    return normalized


def _integer_id(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _safe_relative(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    path = root / relative
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or _has_symlink_component(path, root=root)
    ):
        raise ValueError(f"{label} escapes the BOP export or uses a symbolic link")
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the BOP export") from exc
    return path


def _matrix(rotation: Iterable[float], translation: Iterable[float]) -> list[list[float]]:
    r = list(rotation)
    t = list(translation)
    return [
        [r[0], r[1], r[2], t[0]],
        [r[3], r[4], r[5], t[1]],
        [r[6], r[7], r[8], t[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def project_bop_points(
    points_mm: Iterable[Iterable[float]],
    *,
    cam_k: Iterable[float],
    rotation: Iterable[float],
    translation_mm: Iterable[float],
) -> list[list[float]]:
    """Project model points using BOP's OpenCV model-to-camera convention."""

    points = np.asarray(list(points_mm), dtype=np.float64)
    intrinsic = np.asarray(list(cam_k), dtype=np.float64).reshape(3, 3)
    rotation_matrix = np.asarray(list(rotation), dtype=np.float64).reshape(3, 3)
    translation = np.asarray(list(translation_mm), dtype=np.float64).reshape(3, 1)
    if (
        points.ndim != 2
        or points.shape[1:] != (3,)
        or not np.all(np.isfinite(points))
        or not np.all(np.isfinite(intrinsic))
        or not np.all(np.isfinite(rotation_matrix))
        or not np.all(np.isfinite(translation))
    ):
        raise ValueError("Projection inputs must be finite 3D points and matrices")
    camera_points = (rotation_matrix @ points.T) + translation
    positive = camera_points[2] > 1e-9
    if not np.all(positive):
        raise ValueError("Projection points must remain in front of the camera")
    pixels = intrinsic @ camera_points
    pixels = pixels[:2] / pixels[2]
    return pixels.T.tolist()


def _projected_bounds(
    model: Mapping[str, Any],
    *,
    cam_k: list[float],
    rotation: list[float],
    translation: list[float],
) -> list[float] | None:
    bounds = model.get("bounds_mm")
    if not isinstance(bounds, Mapping):
        return None
    minimum = bounds.get("minimum")
    size = bounds.get("size")
    if not isinstance(minimum, list) or not isinstance(size, list):
        return None
    corners = [
        [minimum[0] + size[0] * x, minimum[1] + size[1] * y, minimum[2] + size[2] * z]
        for x in (0, 1)
        for y in (0, 1)
        for z in (0, 1)
    ]
    try:
        projected = project_bop_points(
            corners,
            cam_k=cam_k,
            rotation=rotation,
            translation_mm=translation,
        )
    except ValueError:
        return None
    xs = [point[0] for point in projected]
    ys = [point[1] for point in projected]
    return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]


def _rotation_delta_deg(left: Iterable[float], right: Iterable[float]) -> float:
    first = np.asarray(list(left), dtype=np.float64).reshape(3, 3)
    second = np.asarray(list(right), dtype=np.float64).reshape(3, 3)
    cosine = float(np.clip((np.trace(first @ second.T) - 1.0) / 2.0, -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _pose_delta(estimate: Mapping[str, Any], gt: Mapping[str, Any]) -> dict[str, Any]:
    translation = float(
        np.linalg.norm(
            np.asarray(estimate["translation_mm"], dtype=np.float64)
            - np.asarray(gt["translation_mm"], dtype=np.float64)
        )
    )
    return {
        "translation_mm": translation,
        "rotation_deg": _rotation_delta_deg(
            estimate["rotation"], gt["rotation"]
        ),
        "rotation_contract": "symmetry_unaware",
    }


def _artifact_signature(path: Path) -> tuple[str, int, int, int, int, int, bool]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return (path.as_posix(), -1, -1, -1, -1, -1, path.is_symlink())
    return (
        path.as_posix(),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_ino,
        metadata.st_mode,
        path.is_symlink(),
    )


def _inspection_signature(root: Path, result_id: str) -> tuple[Any, ...]:
    bop_root = root / "bop"
    paths = [
        bop_root / "bop_export_manifest.json",
        bop_root / "dataset_info.json",
        bop_root / "test_targets_bop19.json",
        bop_root / "posetestbot_bop_frame_map.json",
        bop_root / "posetestbot_instance_map.json",
        bop_root / "models_eval" / "models_info.json",
        root / "run_config.json",
        root
        / "processed"
        / "bop_evaluation"
        / "results"
        / result_id
        / "result.json",
    ]
    manifest_path = paths[0]
    if _plain_file(manifest_path, root=bop_root):
        value = _load_json(manifest_path, label="BOP export manifest")
        exports = value.get("exports") if isinstance(value, Mapping) else None
        if isinstance(exports, list):
            for item in exports[: MAX_SCENES + 1]:
                if not isinstance(item, Mapping) or type(item.get("scene_id")) is not int:
                    continue
                scene_folder = item.get("scene_folder")
                try:
                    scene = _safe_relative(
                        bop_root,
                        scene_folder,
                        label="BOP scene folder",
                    )
                except ValueError:
                    continue
                paths.extend(
                    [
                        scene / "scene_camera.json",
                        scene / "scene_gt.json",
                        scene / "scene_gt_info.json",
                    ]
                )
        models = value.get("object_models") if isinstance(value, Mapping) else None
        if isinstance(models, list):
            for item in models[: MAX_OBJECTS + 1]:
                if isinstance(item, Mapping) and type(item.get("obj_id")) is int:
                    paths.append(
                        bop_root / "models_eval" / f"obj_{item['obj_id']:06d}.ply"
                    )
    record_path = paths[7]
    if _plain_file(record_path, root=root):
        record = _load_json(record_path, label="retained BOP result record")
        filename = record.get("filename") if isinstance(record, Mapping) else None
        if isinstance(filename, str) and Path(filename).name == filename:
            paths.append(record_path.parent / filename)
        paths.append(record_path.parent / "controller-provenance.json")
    return tuple(_artifact_signature(path) for path in paths)


def _sensor_labels(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "run_config.json"
    if not _plain_file(path, root=root):
        return {}
    value = _load_json(path, label="run configuration")
    if not isinstance(value, Mapping) or value.get("schema_version") != "run_config.v4":
        raise ValueError("Pose inspection requires the current run_config.v4 contract")
    capture = value.get("capture")
    sensors = capture.get("sensors") if isinstance(capture, Mapping) else None
    if not isinstance(sensors, list):
        raise ValueError("Run configuration has no current sensor selection")
    labels: dict[str, dict[str, Any]] = {}
    for sensor in sensors:
        if not isinstance(sensor, Mapping) or sensor.get("enabled", True) is not True:
            continue
        sensor_type = sensor.get("sensor_type")
        device_id = sensor.get("device_id")
        if not isinstance(sensor_type, str) or not isinstance(device_id, str):
            raise ValueError("Run sensor identity is invalid")
        folder = sensor_folder_name(sensor_type, device_id)
        adapter = get_sensor_adapter(sensor_type)
        operator_alias = sensor.get("operator_alias")
        if operator_alias is not None and not isinstance(operator_alias, str):
            raise ValueError("Run sensor operator alias is invalid")
        effective = (
            operator_alias.strip()
            if isinstance(operator_alias, str) and operator_alias.strip()
            else adapter.display_name
        )
        labels[folder] = {
            "operator_alias": operator_alias.strip() if isinstance(operator_alias, str) else None,
            "display_name": effective,
            "physical_identity": {
                "sensor_type": sensor_type,
                "family": adapter.display_name,
                "device_id": device_id,
                "mounting_mode": sensor.get("mounting_mode"),
                "sensor_folder": folder,
            },
        }
    return labels


def _model_bounds(value: Mapping[str, Any]) -> dict[str, list[float]] | None:
    try:
        minimum = [float(value[f"min_{axis}"]) for axis in "xyz"]
        size = [float(value[f"size_{axis}"]) for axis in "xyz"]
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in [*minimum, *size]) or any(
        item < 0 for item in size
    ):
        return None
    return {"minimum": minimum, "size": size}


def _load_objects(
    bop_root: Path, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    raw_models = manifest.get("object_models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("BOP export has no inspection models")
    if len(raw_models) > MAX_OBJECTS:
        raise ValueError(f"BOP export exceeds the {MAX_OBJECTS}-object inspection cap")
    info_path = _required_plain_file(
        bop_root / "models_eval" / "models_info.json",
        root=bop_root,
        label="evaluation model metadata",
    )
    info = _load_json(info_path, label="evaluation model metadata")
    if not isinstance(info, Mapping):
        raise ValueError("Evaluation model metadata must be an object")
    objects: list[dict[str, Any]] = []
    by_id: dict[int, dict[str, Any]] = {}
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            raise ValueError("BOP object model entry is invalid")
        obj_id = _integer_id(raw.get("obj_id"), label="object ID", minimum=1)
        if obj_id in by_id:
            raise ValueError("BOP export contains duplicate object model IDs")
        name = raw.get("object_name")
        if not isinstance(name, str) or not 1 <= len(name) <= 160:
            raise ValueError(f"BOP object {obj_id} has an invalid name")
        model_path = _required_plain_file(
            bop_root / "models_eval" / f"obj_{obj_id:06d}.ply",
            root=bop_root,
            label=f"evaluation model {obj_id}",
        )
        model_size = model_path.stat(follow_symlinks=False).st_size
        if not 1 <= model_size <= MAX_MODEL_BYTES:
            raise ValueError(f"Evaluation model {obj_id} exceeds the media limit")
        with model_path.open("rb") as handle:
            header = handle.read(256)
        if not header.startswith(b"ply\n") and not header.startswith(b"ply\r\n"):
            raise ValueError(f"Evaluation model {obj_id} is not a PLY file")
        metadata = info.get(str(obj_id))
        if not isinstance(metadata, Mapping):
            raise ValueError(f"Evaluation model {obj_id} metadata is missing")
        diameter = metadata.get("diameter")
        if type(diameter) not in {int, float} or not math.isfinite(float(diameter)):
            raise ValueError(f"Evaluation model {obj_id} diameter is invalid")
        item = {
            "obj_id": obj_id,
            "name": name,
            "diameter_mm": float(diameter),
            "bounds_mm": _model_bounds(metadata),
            "symmetries_declared": bool(
                metadata.get("symmetries_discrete")
                or metadata.get("symmetries_continuous")
            ),
            "model_sha256": _sha256_file(model_path),
            "model_size_bytes": model_size,
            "model_path": model_path,
        }
        objects.append(item)
        by_id[obj_id] = item
    return sorted(objects, key=lambda item: item["obj_id"]), by_id


def _normalize_info(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    normalized: dict[str, Any] = {}
    for key in ("bbox_obj", "bbox_visib"):
        raw = value.get(key)
        if (
            not isinstance(raw, list)
            or len(raw) != 4
            or any(type(item) not in {int, float} for item in raw)
            or not all(math.isfinite(float(item)) for item in raw)
        ):
            raise ValueError(f"{label} {key} must contain four finite numbers")
        normalized[key] = [float(item) for item in raw]
    for key in ("px_count_all", "px_count_valid", "px_count_visib"):
        normalized[key] = _integer_id(value.get(key), label=f"{label} {key}")
    visibility = value.get("visib_fract")
    if (
        type(visibility) not in {int, float}
        or not math.isfinite(float(visibility))
        or not 0 <= float(visibility) <= 1
    ):
        raise ValueError(f"{label} visib_fract must be between zero and one")
    normalized["visib_fract"] = float(visibility)
    return normalized


def _load_instance_map(
    bop_root: Path,
) -> dict[tuple[int, int, int], str]:
    path = bop_root / "posetestbot_instance_map.json"
    if not path.exists():
        return {}
    _required_plain_file(path, root=bop_root, label="BOP instance map")
    value = _load_json(path, label="BOP instance map")
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "posetestbot_bop_instance_map.v1"
        or not isinstance(value.get("instances"), list)
    ):
        raise ValueError("BOP instance map does not use the current schema")
    rows = value["instances"]
    if len(rows) > MAX_INSTANCE_ROWS:
        raise ValueError("BOP instance map exceeds the inspection row cap")
    mapped: dict[tuple[int, int, int], str] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("BOP instance map row is invalid")
        key = (
            _integer_id(raw.get("scene_id"), label="instance scene ID"),
            _integer_id(raw.get("im_id"), label="instance image ID"),
            _integer_id(raw.get("gt_id"), label="instance GT ID"),
        )
        instance_uuid = raw.get("instance_uuid")
        if not isinstance(instance_uuid, str) or len(instance_uuid) > 64 or key in mapped:
            raise ValueError("BOP instance map identity is invalid or duplicated")
        mapped[key] = instance_uuid
    return mapped


def _load_estimates(path: Path) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
    estimates: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != RESULT_HEADER:
            raise ValueError("Retained BOP result header changed after import")
        for row_index, row in enumerate(reader, start=2):
            if row_index > MAX_INSTANCE_ROWS + 1:
                raise ValueError(
                    f"Retained BOP result exceeds the {MAX_INSTANCE_ROWS}-row inspection cap"
                )
            try:
                scene_id = int(row["scene_id"])
                im_id = int(row["im_id"])
                obj_id = int(row["obj_id"])
                score = float(row["score"])
                rotation = [float(item) for item in row["R"].split()]
                translation = [float(item) for item in row["t"].split()]
                timing = float(row["time"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Retained BOP result row {row_index} is invalid"
                ) from exc
            if (
                min(scene_id, im_id) < 0
                or obj_id < 1
                or len(rotation) != 9
                or len(translation) != 3
                or not all(
                    math.isfinite(item)
                    for item in [score, timing, *rotation, *translation]
                )
            ):
                raise ValueError(f"Retained BOP result row {row_index} is invalid")
            estimates.setdefault((scene_id, im_id, obj_id), []).append(
                {
                    "score": score,
                    "rotation": rotation,
                    "translation_mm": translation,
                    "time_seconds": None if timing < 0 else timing,
                    "matrix_model_to_camera": _matrix(rotation, translation),
                }
            )
    for rows in estimates.values():
        rows.sort(key=lambda item: item["score"], reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
    return estimates


def _provenance_evidence(
    root: Path, result_id: str, result: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    expected_hash = result.get("controller_provenance_sha256")
    if not isinstance(expected_hash, str):
        return None
    folder = root / "processed" / "bop_evaluation" / "results" / result_id
    path = folder / "controller-provenance.json"
    relative = result.get("controller_provenance_path")
    if (
        not isinstance(relative, str)
        or (root / relative).resolve(strict=False) != path.resolve(strict=False)
        or not _plain_file(path, root=root)
        or _sha256_file(path) != expected_hash
    ):
        raise ValueError("Retained controller provenance failed its integrity check")
    value = _load_json(path, label="retained controller provenance")
    if not isinstance(value, Mapping):
        raise ValueError("Retained controller provenance must be a JSON object")
    tracking = value.get("tracking")
    record_tracking = result.get("tracking")
    if tracking is not None and tracking != record_tracking:
        raise ValueError("Retained execution provenance changed after collection")
    return tracking if isinstance(tracking, Mapping) else None


def _operation_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    scene_id: int,
    im_id: int,
    obj_id: int,
    instance_uuid: str | None = None,
) -> list[str]:
    if evidence is None:
        return ["unknown"]
    contract = evidence.get("execution_contract")
    if contract == "independent_register_per_target.v2":
        return ["registration"]
    if contract != "sensor_local_instance_continuous_tracking.v1":
        return ["unknown"]
    operations: set[str] = set()
    segments = evidence.get("track_segments")
    if isinstance(segments, list):
        for segment in segments:
            if (
                not isinstance(segment, Mapping)
                or segment.get("scene_id") != scene_id
                or segment.get("obj_id") != obj_id
                or (
                    instance_uuid is not None
                    and segment.get("instance_uuid") != instance_uuid
                )
                or not isinstance(segment.get("start_im_id"), int)
                or not isinstance(segment.get("end_im_id"), int)
                or not segment["start_im_id"] <= im_id <= segment["end_im_id"]
            ):
                continue
            if im_id == segment["start_im_id"]:
                operations.add(
                    "reinitialization"
                    if segment.get("reinitialization_count") == 1
                    else "registration"
                )
            else:
                operations.add("tracking")
    return sorted(operations) or ["unknown"]


def _failure_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    scene_id: int,
    im_id: int,
    obj_id: int,
) -> list[dict[str, Any]]:
    if evidence is None:
        return []
    failures = evidence.get("failure_identities")
    if not isinstance(failures, list):
        return []
    return [
        dict(item)
        for item in failures
        if isinstance(item, Mapping)
        and item.get("scene_id") == scene_id
        and item.get("im_id") == im_id
        and item.get("obj_id") == obj_id
    ]


def _public_object(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "model_path"}


def _public_scene(item: Mapping[str, Any]) -> dict[str, Any]:
    hidden = {
        "scene_path",
        "camera",
        "ground_truth",
        "ground_truth_info",
        "frame_ids",
        "frame_id_set",
        "frame_ordinals",
    }
    return {key: value for key, value in item.items() if key not in hidden}


def _build_index(root: Path, result_id: str) -> dict[str, Any]:
    dataset = inspect_dataset(root)
    if not dataset["evaluation_ready"]:
        raise ValueError(
            "Pose inspection requires a valid annotation-bearing BOP export: "
            + " ".join(dataset["blockers"])
        )
    inventory = _dataset_inventory(root)
    manifest = inventory["manifest"]
    bop_root = inventory["bop_root"]
    result = get_result(root, result_id, dataset=dataset)
    if not result["compatible"]:
        raise ValueError(
            "Retained result is not compatible with the current BOP export: "
            + " ".join(item["message"] for item in result["blockers"])
        )
    result_path = result_file_path(root, result_id, dataset=dataset)
    if _sha256_file(result_path) != result.get("sha256"):
        raise ValueError("Retained BOP result failed its content hash check")
    estimates = _load_estimates(result_path)
    objects, objects_by_id = _load_objects(bop_root, manifest)
    sensor_labels = _sensor_labels(root)
    instances = _load_instance_map(bop_root)
    evidence = _provenance_evidence(root, result_id, result)
    target_counts = inventory["target_counts"]
    targets_by_frame: dict[tuple[int, int], dict[int, int]] = {}
    for (target_scene_id, target_im_id, target_obj_id), count in target_counts.items():
        targets_by_frame.setdefault((target_scene_id, target_im_id), {})[
            target_obj_id
        ] = count
    estimates_by_frame: dict[
        tuple[int, int], dict[int, list[dict[str, Any]]]
    ] = {}
    for (estimate_scene_id, estimate_im_id, estimate_obj_id), rows in estimates.items():
        estimates_by_frame.setdefault((estimate_scene_id, estimate_im_id), {})[
            estimate_obj_id
        ] = rows
    exports = manifest.get("exports")
    if not isinstance(exports, list) or len(exports) > MAX_SCENES:
        raise ValueError("BOP scene inventory exceeds the inspection cap")
    scenes: list[dict[str, Any]] = []
    scenes_by_id: dict[int, dict[str, Any]] = {}
    total_frames = 0
    total_gt_instances = 0
    image_size = dataset.get("image_size")
    if (
        not isinstance(image_size, list)
        or len(image_size) != 2
        or any(type(item) is not int or item < 1 for item in image_size)
    ):
        raise ValueError("Pose inspection requires one validated image resolution")
    expected_size = (image_size[0], image_size[1])
    for raw_export in exports:
        if not isinstance(raw_export, Mapping):
            raise ValueError("BOP scene entry is invalid")
        scene_id = _integer_id(raw_export.get("scene_id"), label="scene ID")
        split = raw_export.get("split")
        expected_folder = f"{dataset['split']}/{scene_id:06d}"
        if split != dataset["split"] or raw_export.get("scene_folder") != expected_folder:
            raise ValueError(f"BOP scene {scene_id} does not use its standard folder")
        scene_path = bop_root / expected_folder
        if scene_id in scenes_by_id or _has_symlink_component(scene_path, root=bop_root):
            raise ValueError(f"BOP scene {scene_id} is duplicated or unsafe")
        camera = _load_json(
            _required_plain_file(
                scene_path / "scene_camera.json",
                root=bop_root,
                label=f"scene {scene_id} camera metadata",
            ),
            label=f"scene {scene_id} camera metadata",
        )
        ground_truth = _load_json(
            _required_plain_file(
                scene_path / "scene_gt.json",
                root=bop_root,
                label=f"scene {scene_id} ground truth",
            ),
            label=f"scene {scene_id} ground truth",
        )
        ground_truth_info = _load_json(
            _required_plain_file(
                scene_path / "scene_gt_info.json",
                root=bop_root,
                label=f"scene {scene_id} visibility evidence",
            ),
            label=f"scene {scene_id} visibility evidence",
        )
        if not all(
            isinstance(value, Mapping)
            for value in (camera, ground_truth, ground_truth_info)
        ):
            raise ValueError(f"BOP scene {scene_id} JSON artifacts must be objects")
        try:
            frame_ids = sorted(int(key) for key in camera)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"BOP scene {scene_id} has an invalid frame ID") from exc
        canonical_frame_keys = {str(frame_id) for frame_id in frame_ids}
        if (
            any(frame_id < 0 for frame_id in frame_ids)
            or len(set(frame_ids)) != len(frame_ids)
            or set(camera) != canonical_frame_keys
            or set(ground_truth) != canonical_frame_keys
            or set(ground_truth_info) != canonical_frame_keys
        ):
            raise ValueError(f"BOP scene {scene_id} frame IDs are invalid")
        for frame_id in frame_ids:
            gt_rows = ground_truth[str(frame_id)]
            info_rows = ground_truth_info[str(frame_id)]
            if (
                not isinstance(gt_rows, list)
                or not isinstance(info_rows, list)
                or len(gt_rows) != len(info_rows)
            ):
                raise ValueError(
                    f"BOP scene {scene_id} frame {frame_id} GT evidence is invalid"
                )
            if len(gt_rows) > MAX_GT_INSTANCES_PER_FRAME:
                raise ValueError(
                    f"BOP scene {scene_id} frame {frame_id} exceeds the "
                    f"{MAX_GT_INSTANCES_PER_FRAME}-instance response cap"
                )
            total_gt_instances += len(gt_rows)
            if total_gt_instances > MAX_INSTANCE_ROWS:
                raise ValueError(
                    f"BOP ground truth exceeds the {MAX_INSTANCE_ROWS}-row "
                    "inspection cap"
                )
        total_frames += len(frame_ids)
        if total_frames > MAX_FRAMES:
            raise ValueError(f"BOP export exceeds the {MAX_FRAMES}-frame inspection cap")
        sensor_name = raw_export.get("sensor_name")
        if not isinstance(sensor_name, str) or not sensor_name:
            raise ValueError(f"BOP scene {scene_id} has no sensor identity")
        labels = sensor_labels.get(sensor_name) or {
            "operator_alias": None,
            "display_name": sensor_name,
            "physical_identity": {"sensor_folder": sensor_name},
        }
        target_frame_count = sum(
            (scene_id, frame_id) in targets_by_frame for frame_id in frame_ids
        )
        estimate_frame_count = sum(
            (scene_id, frame_id) in estimates_by_frame for frame_id in frame_ids
        )
        artifacts = raw_export.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, Mapping) else {}
        scene = {
            "scene_id": scene_id,
            "sensor_name": sensor_name,
            **labels,
            "frame_count": len(frame_ids),
            "target_frame_count": target_frame_count,
            "estimate_frame_count": estimate_frame_count,
            "frame_ids": frame_ids,
            "frame_id_set": frozenset(frame_ids),
            "frame_ordinals": {
                frame_id: ordinal for ordinal, frame_id in enumerate(frame_ids)
            },
            "image_size": list(expected_size),
            "capabilities": {
                "rgb": True,
                "depth": True,
                "ground_truth": True,
                "full_mask": artifacts.get("mask") == f"{expected_folder}/mask",
                "visible_mask": artifacts.get("mask_visib")
                == f"{expected_folder}/mask_visib",
                "execution_operations": evidence is not None,
            },
            "scene_path": scene_path,
            "camera": camera,
            "ground_truth": ground_truth,
            "ground_truth_info": ground_truth_info,
        }
        scenes.append(scene)
        scenes_by_id[scene_id] = scene
    if any(key[0] not in scenes_by_id or key[2] not in objects_by_id for key in estimates):
        raise ValueError("Retained result references an unknown scene or object")
    return {
        "root": root,
        "bop_root": bop_root,
        "dataset": dataset,
        "result": result,
        "result_path": result_path,
        "objects": objects,
        "objects_by_id": objects_by_id,
        "scenes": sorted(scenes, key=lambda item: item["scene_id"]),
        "scenes_by_id": scenes_by_id,
        "target_counts": target_counts,
        "targets_by_frame": targets_by_frame,
        "estimates": estimates,
        "estimates_by_frame": estimates_by_frame,
        "instances": instances,
        "evidence": evidence,
        "expected_size": expected_size,
    }


@lru_cache(maxsize=MAX_CACHED_RESULTS)
def _cached_index(
    root_value: str, result_id: str, _signature: tuple[Any, ...]
) -> dict[str, Any]:
    return _build_index(Path(root_value), result_id)


def _index(run_root: str | Path, result_id: str, *, refresh: bool = False) -> dict[str, Any]:
    if not isinstance(result_id, str) or RESULT_ID_RE.fullmatch(result_id) is None:
        raise KeyError("Unknown BOP result")
    root = Path(run_root).resolve()
    if refresh:
        return _build_index(root, result_id)
    return _cached_index(root.as_posix(), result_id, _inspection_signature(root, result_id))


def inspection_setup(
    run_root: str | Path, *, result_id: str | None = None
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    dataset = inspect_dataset(root)
    results = list_results(root, dataset=dataset)
    compatible = [item for item in results if item.get("compatible") is True]
    requested = next(
        (item for item in results if item.get("result_id") == result_id),
        None,
    ) if result_id is not None else None
    if result_id is not None and requested is None:
        raise KeyError("Unknown BOP result")
    selected = (
        requested
        if requested is not None and requested.get("compatible") is True
        else compatible[0] if result_id is None and compatible else None
    )
    public_results = [public_result_descriptor(item) for item in results]
    if selected is None:
        blockers = list(public_dataset_descriptor(dataset).get("blockers", []))
        if requested is not None:
            blockers.extend(public_result_descriptor(requested).get("blockers", []))
        else:
            blockers.append(
                {
                    "code": "result_required",
                    "message": "Collect or import a compatible BOP19 result before inspecting poses.",
                }
            )
        return {
            "schema_version": INSPECTION_SETUP_SCHEMA,
            "ready": False,
            "dataset": public_dataset_descriptor(dataset),
            "results": public_results,
            "selected_result_id": result_id if requested is not None else None,
            "objects": [],
            "scenes": [],
            "blockers": blockers,
            "limits": {
                "max_page_size": MAX_PAGE_SIZE,
                "max_hypotheses": MAX_HYPOTHESES,
            },
        }
    current = _index(root, str(selected["result_id"]))
    return {
        "schema_version": INSPECTION_SETUP_SCHEMA,
        "ready": True,
        "dataset": public_dataset_descriptor(current["dataset"]),
        "results": public_results,
        "selected_result_id": selected["result_id"],
        "objects": [_public_object(item) for item in current["objects"]],
        "scenes": [_public_scene(item) for item in current["scenes"]],
        "blockers": [],
        "limits": {
            "max_page_size": MAX_PAGE_SIZE,
            "max_hypotheses": MAX_HYPOTHESES,
        },
        "visualization_contract": {
            "projection": "bop_opencv_model_to_camera.v1",
            "renderer_coordinates": "opencv_x_right_y_down_z_forward_to_webgl_x_right_y_up_z_back",
            "occlusion": "xray_no_observed_depth_occlusion",
            "default_layers": ["rgb", "estimate_surface", "gt_wireframe"],
        },
    }


def _frame_summary(
    index: Mapping[str, Any], *, scene_id: int, im_id: int, object_id: int | None
) -> dict[str, Any]:
    all_targets = index["targets_by_frame"].get((scene_id, im_id), {})
    all_estimates = index["estimates_by_frame"].get((scene_id, im_id), {})
    targets = (
        all_targets
        if object_id is None
        else {object_id: all_targets[object_id]}
        if object_id in all_targets
        else {}
    )
    estimates = (
        all_estimates
        if object_id is None
        else {object_id: all_estimates[object_id]}
        if object_id in all_estimates
        else {}
    )
    operations: set[str] = set()
    for obj_id in set(targets) | set(estimates):
        operations.update(
            _operation_evidence(
                index["evidence"],
                scene_id=scene_id,
                im_id=im_id,
                obj_id=obj_id,
            )
        )
    missing = sum(
        max(0, count - len(estimates.get(obj_id, [])))
        for obj_id, count in targets.items()
    )
    estimate_count = sum(len(rows) for rows in estimates.values())
    return {
        "scene_id": scene_id,
        "im_id": im_id,
        "target": bool(targets),
        "target_instance_count": sum(targets.values()),
        "has_estimate": estimate_count > 0,
        "estimate_count": estimate_count,
        "missing_estimate": missing > 0,
        "missing_target_instance_count": missing,
        "operations": sorted(operations) if operations else ["unknown"],
    }


def list_inspection_frames(
    run_root: str | Path,
    *,
    result_id: str,
    scene_id: int,
    frame_filter: str = "all",
    object_id: int | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    if frame_filter not in FRAME_FILTERS:
        raise ValueError("frame_filter is invalid")
    if page < 1:
        raise ValueError("page must be positive")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    index = _index(run_root, result_id)
    rows = matching_inspection_frames(index, scene_id, frame_filter, object_id)
    scene = index["scenes_by_id"][scene_id]
    total = len(rows)
    for ordinal, row in enumerate(rows):
        row["ordinal"] = ordinal
        row["previous_im_id"] = rows[ordinal - 1]["im_id"] if ordinal else None
        row["next_im_id"] = rows[ordinal + 1]["im_id"] if ordinal + 1 < total else None
    start = (page - 1) * page_size
    return {
        "schema_version": INSPECTION_FRAME_LIST_SCHEMA,
        "result_id": result_id,
        "scene": _public_scene(scene),
        "frame_filter": frame_filter,
        "object_id": object_id,
        "page": page,
        "page_size": page_size,
        "total_count": total,
        "page_count": math.ceil(total / page_size) if total else 0,
        "previous_page": page - 1 if page > 1 and start < total else None,
        "next_page": page + 1 if start + page_size < total else None,
        "frames": rows[start : start + page_size],
    }


def matching_inspection_frames(
    index: Mapping[str, Any], scene_id: int, frame_filter: str, object_id: int | None
) -> list[dict[str, Any]]:
    """One selection contract for paginated browsing and whole-scene exports."""
    if frame_filter not in FRAME_FILTERS:
        raise ValueError("frame_filter is invalid")
    scene = index["scenes_by_id"].get(scene_id)
    if scene is None:
        raise KeyError("Unknown BOP inspection scene")
    if object_id is not None and object_id not in index["objects_by_id"]:
        raise KeyError("Unknown BOP inspection object")
    rows = [
        _frame_summary(index, scene_id=scene_id, im_id=im_id, object_id=object_id)
        for im_id in scene["frame_ids"]
    ]
    if frame_filter == "estimated":
        rows = [row for row in rows if row["has_estimate"]]
    elif frame_filter == "target":
        rows = [row for row in rows if row["target"]]
    elif frame_filter == "missing_estimate":
        rows = [row for row in rows if row["missing_estimate"]]
    elif frame_filter in {"registration", "tracking", "reinitialization"}:
        rows = [row for row in rows if frame_filter in row["operations"]]
    return rows


def _frame_rows(index: Mapping[str, Any], scene_id: int, im_id: int) -> tuple[Any, Any, Any]:
    scene = index["scenes_by_id"].get(scene_id)
    if scene is None or im_id not in scene["frame_id_set"]:
        raise KeyError("Unknown BOP inspection frame")
    key = str(im_id)
    camera = scene["camera"].get(key)
    gt = scene["ground_truth"].get(key)
    info = scene["ground_truth_info"].get(key)
    if not isinstance(camera, Mapping) or not isinstance(gt, list) or not isinstance(info, list):
        raise ValueError("BOP frame evidence is incomplete")
    if len(gt) != len(info):
        raise ValueError("BOP frame GT and visibility rows do not match")
    return camera, gt, info


def inspection_frame(
    run_root: str | Path,
    *,
    result_id: str,
    scene_id: int,
    im_id: int,
    max_hypotheses: int = DEFAULT_HYPOTHESES,
) -> dict[str, Any]:
    return inspection_frame_from_index(
        _index(run_root, result_id), scene_id=scene_id, im_id=im_id,
        max_hypotheses=max_hypotheses,
    )


def inspection_frame_from_index(
    index: Mapping[str, Any], *, scene_id: int, im_id: int,
    max_hypotheses: int = DEFAULT_HYPOTHESES,
) -> dict[str, Any]:
    """Render workers retain one validated index for incremental processing."""
    if not 1 <= max_hypotheses <= MAX_HYPOTHESES:
        raise ValueError(
            f"max_hypotheses must be between 1 and {MAX_HYPOTHESES}"
        )
    scene = index["scenes_by_id"].get(scene_id)
    if scene is None:
        raise KeyError("Unknown BOP inspection scene")
    camera, raw_gt, raw_info = _frame_rows(index, scene_id, im_id)
    cam_k = _finite_vector(camera.get("cam_K"), count=9, label="cam_K")
    depth_scale = camera.get("depth_scale")
    if type(depth_scale) not in {int, float} or not math.isfinite(float(depth_scale)) or float(depth_scale) <= 0:
        raise ValueError("BOP frame depth scale is invalid")
    ground_truth: list[dict[str, Any]] = []
    gt_by_object: dict[int, list[dict[str, Any]]] = {}
    for gt_id, (raw_pose, raw_visibility) in enumerate(zip(raw_gt, raw_info, strict=True)):
        if not isinstance(raw_pose, Mapping):
            raise ValueError("BOP ground-truth pose row is invalid")
        obj_id = _integer_id(raw_pose.get("obj_id"), label="GT object ID", minimum=1)
        if obj_id not in index["objects_by_id"]:
            raise ValueError("BOP ground truth references an unknown object")
        rotation = _finite_vector(
            raw_pose.get("cam_R_m2c"), count=9, label="GT rotation"
        )
        translation = _finite_vector(
            raw_pose.get("cam_t_m2c"), count=3, label="GT translation"
        )
        visibility = _normalize_info(
            raw_visibility,
            label=f"scene {scene_id} frame {im_id} GT {gt_id} visibility",
        )
        instance_uuid = index["instances"].get((scene_id, im_id, gt_id))
        operations = _operation_evidence(
            index["evidence"],
            scene_id=scene_id,
            im_id=im_id,
            obj_id=obj_id,
            instance_uuid=instance_uuid,
        )
        item = {
            "gt_id": gt_id,
            "obj_id": obj_id,
            "object_name": index["objects_by_id"][obj_id]["name"],
            "instance_uuid": instance_uuid,
            "rotation": rotation,
            "translation_mm": translation,
            "matrix_model_to_camera": _matrix(rotation, translation),
            "visibility": visibility,
            "operations": operations,
            "projected_model_bounds": _projected_bounds(
                index["objects_by_id"][obj_id],
                cam_k=cam_k,
                rotation=rotation,
                translation=translation,
            ),
            "masks": {
                "full": scene["capabilities"]["full_mask"],
                "visible": scene["capabilities"]["visible_mask"],
            },
        }
        ground_truth.append(item)
        gt_by_object.setdefault(obj_id, []).append(item)
    estimates: list[dict[str, Any]] = []
    associations: list[dict[str, Any]] = []
    omitted_hypotheses = 0
    object_ids = sorted(
        set(index["estimates_by_frame"].get((scene_id, im_id), {}))
        | set(gt_by_object)
    )
    for obj_id in object_ids:
        rows = index["estimates"].get((scene_id, im_id, obj_id), [])
        visible_rows = rows[:max_hypotheses]
        omitted_hypotheses += max(0, len(rows) - len(visible_rows))
        operations = _operation_evidence(
            index["evidence"],
            scene_id=scene_id,
            im_id=im_id,
            obj_id=obj_id,
        )
        failures = _failure_evidence(
            index["evidence"],
            scene_id=scene_id,
            im_id=im_id,
            obj_id=obj_id,
        )
        for row in visible_rows:
            estimate = {
                **row,
                "obj_id": obj_id,
                "object_name": index["objects_by_id"][obj_id]["name"],
                "operations": operations,
                "projected_model_bounds": _projected_bounds(
                    index["objects_by_id"][obj_id],
                    cam_k=cam_k,
                    rotation=row["rotation"],
                    translation=row["translation_mm"],
                ),
            }
            estimates.append(estimate)
        gt_rows = gt_by_object.get(obj_id, [])
        if len(gt_rows) == 1 and rows:
            associations.append(
                {
                    "obj_id": obj_id,
                    "status": "unambiguous",
                    "estimate_rank": 1,
                    "gt_id": gt_rows[0]["gt_id"],
                    "delta": _pose_delta(rows[0], gt_rows[0]),
                }
            )
        elif len(gt_rows) > 1:
            associations.append(
                {
                    "obj_id": obj_id,
                    "status": "ambiguous_repeated_object",
                    "estimate_rank": None,
                    "gt_id": None,
                    "delta": None,
                    "reason": "Repeated identical objects have no standard BOP19 instance pairing.",
                }
            )
        elif gt_rows and not rows:
            associations.append(
                {
                    "obj_id": obj_id,
                    "status": "missing_estimate",
                    "estimate_rank": None,
                    "gt_id": gt_rows[0]["gt_id"] if len(gt_rows) == 1 else None,
                    "delta": None,
                }
            )
        if failures:
            associations.append(
                {
                    "obj_id": obj_id,
                    "status": "execution_failure",
                    "failures": failures,
                    "delta": None,
                }
            )
    timing = None
    if isinstance(index["evidence"], Mapping):
        timings = index["evidence"].get("image_timings_seconds")
        if isinstance(timings, Mapping):
            timing = timings.get(f"{scene_id}/{im_id}")
    frame_summary = _frame_summary(
        index, scene_id=scene_id, im_id=im_id, object_id=None
    )
    ordinal = scene["frame_ordinals"][im_id]
    frame_summary.update(
        {
            "ordinal": ordinal,
            "previous_im_id": (
                scene["frame_ids"][ordinal - 1] if ordinal > 0 else None
            ),
            "next_im_id": (
                scene["frame_ids"][ordinal + 1]
                if ordinal + 1 < len(scene["frame_ids"])
                else None
            ),
        }
    )
    return {
        "schema_version": INSPECTION_FRAME_SCHEMA,
        "result": public_result_descriptor(index["result"]),
        "scene": _public_scene(scene),
        "frame": frame_summary,
        "camera": {
            "cam_K": cam_k,
            "intrinsic_matrix": [cam_k[0:3], cam_k[3:6], cam_k[6:9]],
            "depth_scale_mm": float(depth_scale),
            "image_size": list(index["expected_size"]),
            "coordinate_convention": "BOP OpenCV camera: +X right, +Y down, +Z forward",
        },
        "ground_truth": ground_truth,
        "estimates": estimates,
        "omitted_hypothesis_count": omitted_hypotheses,
        "associations": associations,
        "execution": {
            "known": index["evidence"] is not None,
            "image_time_seconds": timing,
            "oracle_mask_contract": (
                index["evidence"].get("oracle_mask_contract")
                if isinstance(index["evidence"], Mapping)
                else None
            ),
            "registration_iterations": (
                index["evidence"].get("registration_iterations")
                if isinstance(index["evidence"], Mapping)
                else None
            ),
            "tracking_iterations": (
                index["evidence"].get("tracking_iterations")
                if isinstance(index["evidence"], Mapping)
                else None
            ),
        },
        "visualization": {
            "base_layers": ["rgb", "depth"],
            "xray": True,
            "observed_depth_occlusion": False,
        },
    }


def _checked_media_path(
    index: Mapping[str, Any], *, scene_id: int, im_id: int, kind: str
) -> Path:
    scene = index["scenes_by_id"].get(scene_id)
    if scene is None or im_id not in scene["frame_id_set"]:
        raise KeyError("Unknown BOP inspection frame")
    if kind not in {"rgb", "depth"}:
        raise KeyError("Unknown BOP inspection media kind")
    path = scene["scene_path"] / kind / f"{im_id:06d}.png"
    _required_plain_file(path, root=index["bop_root"], label=f"frame {kind}")
    if path.stat(follow_symlinks=False).st_size > MAX_MEDIA_BYTES:
        raise ValueError("BOP frame media exceeds the response size cap")
    size = _png_size(path, label=f"{kind} image", expected_kind=kind)
    if size != index["expected_size"] or size[0] * size[1] > MAX_MEDIA_PIXELS:
        raise ValueError("BOP frame media dimensions are invalid")
    return path


def inspection_image_path(
    run_root: str | Path,
    *,
    result_id: str,
    scene_id: int,
    im_id: int,
    kind: str,
) -> Path:
    index = _index(run_root, result_id)
    return _checked_media_path(index, scene_id=scene_id, im_id=im_id, kind=kind)


def inspection_depth_png(
    run_root: str | Path,
    *,
    result_id: str,
    scene_id: int,
    im_id: int,
) -> bytes:
    index = _index(run_root, result_id)
    path = _checked_media_path(index, scene_id=scene_id, im_id=im_id, kind="depth")
    depth = cv2.imread(path.as_posix(), cv2.IMREAD_UNCHANGED)
    if depth is None or depth.dtype != np.uint16 or depth.shape != (
        index["expected_size"][1],
        index["expected_size"][0],
    ):
        raise ValueError("BOP depth image changed after validation")
    colored = colorize_depth(depth)
    ok, encoded = cv2.imencode(".png", colored)
    if not ok or encoded.nbytes > MAX_MEDIA_BYTES:
        raise ValueError("Colorized BOP depth image exceeds the response cap")
    return encoded.tobytes()


def colorize_depth(depth: np.ndarray) -> np.ndarray:
    """The same per-frame depth palette for the browser and saved images (BGR)."""
    valid = depth > 0
    normalized = np.zeros(depth.shape, dtype=np.uint8)
    if np.any(valid):
        values = depth[valid].astype(np.float64)
        low, high = np.percentile(values, [2.0, 98.0])
        if high <= low:
            high = low + 1.0
        normalized[valid] = np.clip(
            (depth[valid].astype(np.float64) - low) * 255.0 / (high - low),
            0,
            255,
        ).astype(np.uint8)
    colored = cv2.applyColorMap(normalized, cv2.COLORMAP_TURBO)
    colored[~valid] = 0
    return colored


def inspection_mask_path(
    run_root: str | Path,
    *,
    result_id: str,
    scene_id: int,
    im_id: int,
    gt_id: int,
    kind: str,
) -> Path:
    index = _index(run_root, result_id)
    scene = index["scenes_by_id"].get(scene_id)
    if scene is None or im_id not in scene["frame_id_set"]:
        raise KeyError("Unknown BOP inspection frame")
    _camera, ground_truth, _info = _frame_rows(index, scene_id, im_id)
    if not 0 <= gt_id < len(ground_truth):
        raise KeyError("Unknown BOP ground-truth instance")
    folders = {"full": "mask", "visible": "mask_visib"}
    folder = folders.get(kind)
    if folder is None or scene["capabilities"][f"{kind}_mask"] is not True:
        raise KeyError("Requested BOP mask is unavailable")
    path = scene["scene_path"] / folder / f"{im_id:06d}_{gt_id:06d}.png"
    _required_plain_file(path, root=index["bop_root"], label=f"{kind} GT mask")
    if path.stat(follow_symlinks=False).st_size > MAX_MEDIA_BYTES:
        raise ValueError("BOP mask exceeds the response size cap")
    size = _png_size(path, label=f"{kind} GT mask")
    if size != index["expected_size"]:
        raise ValueError("BOP mask dimensions do not match the inspected frame")
    return path


def inspection_model_path(
    run_root: str | Path, *, result_id: str, obj_id: int
) -> Path:
    index = _index(run_root, result_id)
    model = index["objects_by_id"].get(obj_id)
    if model is None:
        raise KeyError("Unknown BOP inspection object")
    path = model["model_path"]
    _required_plain_file(path, root=index["bop_root"], label="evaluation model")
    if (
        path.stat(follow_symlinks=False).st_size != model["model_size_bytes"]
        or _sha256_file(path) != model["model_sha256"]
    ):
        raise ValueError("Evaluation model failed its retained inspection hash")
    return path
