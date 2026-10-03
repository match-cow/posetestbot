"""IPD robot consistency for immutable, run-scoped Inspect evaluations.

IPD holds objects fixed in the gripper frame. PoseTestBot holds them fixed in
template_base and moves an eye-in-hand camera; the same transform/mean/vertex
distance calculation applies. Ground truth supplies coarse instance matching,
never the reference pose against which consistency is measured.
"""

from __future__ import annotations

import csv
import hashlib
import json
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import numpy as np
import pytransform3d.rotations as pr
import pytransform3d.transformations as pt
import trimesh
from scipy.optimize import linear_sum_assignment
from scipy.spatial.transform import Rotation

from posetestbot.bop.evaluation import (
    _has_symlink_component,
    _plain_file,
    _sha256_file,
)
from posetestbot.calibration.profiles import CalibrationStatus, profile_from_dict
from posetestbot.calibration.transforms import robot_ee_to_reference
from posetestbot.sensors.contracts import MountingMode

REVISION = "posetestbot_ipd_robot_consistency.v1"
IPD_REVISION = "75dfe72e2f194f2a0e8a82bd8268fbec09205567"
IPD_SOURCE = f"https://github.com/intrinsic-ai/ipd/blob/{IPD_REVISION}/src/intrinsic_ipd/evaluator.py"
INPUTS_FILENAME = "robot_consistency_inputs.json"
REPORT_FILENAME = "robot_consistency.json"
MATCH_THRESHOLD_MM = 100.0
PUBLIC_TRACK_LIMIT = 200
VERTEX_CHUNK = 65536


def _path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Robot consistency requires a run-relative evidence path")
    child = Path(relative)
    if child.is_absolute() or ".." in child.parts:
        raise ValueError("Robot consistency evidence must remain inside the run")
    path = root / child
    if _has_symlink_component(path, root=root):
        raise ValueError("Robot consistency evidence must not use symbolic links")
    return path


def _read(root: Path, relative: str, sources: dict[str, str]) -> Any:
    path = _path(root, relative)
    if not _plain_file(path, root=root):
        raise ValueError(
            "Robot consistency evidence is missing or is not a regular file"
        )
    content = path.read_bytes()
    sources[relative] = hashlib.sha256(content).hexdigest()
    return json.loads(content)


def _rigid(value: Any) -> np.ndarray:
    matrix = np.asarray(value, dtype=float)
    if (
        matrix.shape != (4, 4)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix[3], [0, 0, 0, 1], rtol=0, atol=1e-9)
        or not np.allclose(
            matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), rtol=0, atol=1e-5
        )
        or not np.isclose(np.linalg.det(matrix[:3, :3]), 1, rtol=0, atol=1e-5)
    ):
        raise ValueError(
            "Robot consistency requires finite rigid transforms in millimetres"
        )
    return matrix


def verify_sources(root: Path, inputs: Mapping[str, Any]) -> None:
    for relative, expected_hash in inputs["source_sha256"].items():
        path = _path(root, relative)
        if not _plain_file(path, root=root) or _sha256_file(path) != expected_hash:
            raise ValueError(
                "Robot consistency input changed after the request was queued"
            )


def freeze_inputs(
    run_root: Path,
    manifest: Mapping[str, Any],
    targets: list[dict[str, int]],
) -> dict[str, Any]:
    """Bind original export evidence without modifying the dataset or raw data."""
    root = run_root.resolve()
    sources: dict[str, str] = {}
    snapshot: dict[str, Any] = {
        "schema_version": "bop_robot_consistency_inputs.v1",
        "implementation_revision": REVISION,
        "ipd_revision": IPD_REVISION,
        "reference_frame": "template_base",
        "translation_unit": "mm",
        "matching_threshold_mm": MATCH_THRESHOLD_MM,
        "source_sha256": sources,
        "scenes": [],
    }
    selected_scenes = sorted({row["scene_id"] for row in targets})
    common_missing = [
        name
        for name in ("posetestbot_bop_frame_map.json", "posetestbot_instance_map.json")
        if not _path(root, f"bop/{name}").exists()
    ]
    if (
        not isinstance(manifest.get("calibration_profiles"), list)
        or not manifest["calibration_profiles"]
    ):
        common_missing.append("exported calibration snapshots")
    if common_missing:
        snapshot["scenes"] = [
            {
                "scene_id": scene_id,
                "status": "unavailable",
                "reason": "Missing " + ", ".join(common_missing) + ".",
            }
            for scene_id in selected_scenes
        ]
        return snapshot

    frame_map = _read(root, "bop/posetestbot_bop_frame_map.json", sources)
    instance_map = _read(root, "bop/posetestbot_instance_map.json", sources)
    if (
        not isinstance(frame_map, dict)
        or frame_map.get("schema_version") != "posetestbot_bop_frame_map.v3"
        or not isinstance(frame_map.get("scenes"), dict)
    ):
        raise ValueError("Robot consistency requires the current BOP frame map")
    if (
        not isinstance(instance_map, dict)
        or instance_map.get("schema_version") != "posetestbot_bop_instance_map.v1"
        or not isinstance(instance_map.get("instances"), list)
    ):
        raise ValueError("Robot consistency requires the current BOP instance map")
    profiles = {}
    for raw in manifest["calibration_profiles"]:
        profile = profile_from_dict(raw)
        if profile.profile_id in profiles:
            raise ValueError(
                "Robot consistency calibration snapshot IDs are duplicated"
            )
        profiles[profile.profile_id] = profile
    exports = {row["scene_id"]: row for row in manifest["exports"]}
    instance_rows: dict[tuple[int, int], list[dict]] = defaultdict(list)
    identities: set[tuple[int, int, int]] = set()
    instance_objects: dict[str, int] = {}
    fixed_object_poses: dict[str, np.ndarray] = {}
    for row in instance_map["instances"]:
        if not isinstance(row, dict) or any(
            type(row.get(key)) is not int or row[key] < 0
            for key in ("scene_id", "im_id", "gt_id", "obj_id")
        ):
            raise ValueError("Robot consistency instance identity is invalid")
        identity = (row["scene_id"], row["im_id"], row["gt_id"])
        try:
            instance_uuid = str(UUID(row["instance_uuid"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Robot consistency instance UUID is invalid") from exc
        if instance_uuid != row["instance_uuid"]:
            raise ValueError(
                "Robot consistency instance UUID must use its canonical form"
            )
        if (
            identity in identities
            or row["obj_id"] < 1
            or instance_objects.get(instance_uuid, row["obj_id"]) != row["obj_id"]
        ):
            raise ValueError("Robot consistency instance identity is contradictory")
        identities.add(identity)
        instance_objects[instance_uuid] = row["obj_id"]
        instance_rows[identity[:2]].append(
            {
                "gt_id": row["gt_id"],
                "obj_id": row["obj_id"],
                "instance_uuid": instance_uuid,
            }
        )

    for scene_id in selected_scenes:
        export = exports[scene_id]
        scene: dict[str, Any] = {
            "scene_id": scene_id,
            "sensor_name": export["sensor_name"],
            "status": "available",
            "frames": [],
        }
        snapshot["scenes"].append(scene)
        profile = profiles.get(export.get("calibration_profile_id"))
        if profile is None:
            scene.update(
                status="unavailable",
                reason="The scene has no exported calibration snapshot.",
            )
            continue
        if profile.status != CalibrationStatus.VALID:
            raise ValueError("Robot consistency calibration snapshot is not valid")
        if profile.mounting_mode != MountingMode.EYE_IN_HAND:
            scene.update(
                status="unavailable",
                reason="A static camera supplies no robot-driven change of viewpoint for fixed workpieces.",
            )
            continue
        mapped_scene = frame_map["scenes"].get(str(scene_id))
        if not isinstance(mapped_scene, dict) or any(
            mapped_scene.get(key) != export.get(key)
            for key in (
                "sensor_name",
                "split",
                "scene_folder",
                "input_sensor_folder",
                "projection",
            )
        ):
            raise ValueError(
                "Robot consistency frame map does not match the exported scene"
            )
        folder = export.get("input_sensor_folder")
        if not isinstance(folder, str):
            raise ValueError("Robot consistency scene has no run-owned source folder")
        matched_relative = (Path(folder) / "match_robot_ee_poses.json").as_posix()
        if not _path(root, matched_relative).exists():
            scene.update(
                status="unavailable",
                reason="Matched robot poses retained by the export are missing.",
            )
            continue
        provenance_relative = (
            f"bop/{export['scene_folder']}/posetestbot_gt_provenance.json"
        )
        if not _path(root, provenance_relative).exists():
            scene.update(
                status="unavailable",
                reason="The scene has no retained GT frame-binding provenance.",
            )
            continue
        provenance = _read(root, provenance_relative, sources)
        matched = _read(root, matched_relative, sources)
        if (
            not isinstance(provenance, dict)
            or not isinstance(matched, dict)
            or provenance.get("schema_version") != "posetestbot_gt_provenance.v1"
            or provenance.get("translation_unit") != "mm"
            or provenance.get("coordinate_frames", {}).get("camera_pose_input")
            != "template_base_from_opencv_camera"
            or provenance.get("source_artifact_sha256", {}).get(
                "match_robot_ee_poses.json"
            )
            != sources[matched_relative]
            or export.get("annotation_provenance", {}).get("sha256")
            != sources[provenance_relative]
        ):
            raise ValueError(
                "Robot consistency robot poses/provenance do not match the original export"
            )
        bindings = provenance.get("frame_bindings")
        if (
            not isinstance(bindings, list)
            or not isinstance(matched, dict)
            or not isinstance(mapped_scene.get("frames"), dict)
        ):
            raise ValueError("Robot consistency frame bindings are invalid")
        by_image = {}
        for binding in bindings:
            if (
                not isinstance(binding, dict)
                or type(binding.get("output_image_id")) is not int
                or binding["output_image_id"] < 0
                or type(binding.get("source_frame_id")) is not int
                or binding["source_frame_id"] < 0
                or not isinstance(binding.get("source_filename"), str)
                or binding["output_image_id"] in by_image
            ):
                raise ValueError(
                    "Robot consistency frame binding is invalid or duplicated"
                )
            by_image[binding["output_image_id"]] = binding
        if set(by_image) != set(range(len(bindings))):
            raise ValueError(
                "Robot consistency GT frame bindings must use contiguous BOP image IDs"
            )
        camera_to_flange = pt.transform_from(
            pr.matrix_from_quaternion(
                np.asarray(profile.extrinsics.rotation_quaternion_wxyz)
            ),
            np.asarray(profile.extrinsics.translation_mm),
        )
        gt = _read(root, f"bop/{export['scene_folder']}/scene_gt.json", sources)
        prepared_relative = (
            Path(folder) / "blenderproc" / "camera_poses.npy"
        ).as_posix()
        prepared_path = _path(root, prepared_relative)
        if not _plain_file(prepared_path, root=root):
            scene.update(
                status="unavailable",
                reason="The export's prepared camera-pose evidence is missing.",
            )
            continue
        sources[prepared_relative] = _sha256_file(prepared_path)
        if sources[prepared_relative] != provenance.get("input_sha256", {}).get(
            "camera_poses.npy"
        ):
            raise ValueError(
                "Robot consistency prepared camera poses do not match the original export"
            )
        prepared_poses = np.load(prepared_path, allow_pickle=False)
        if (
            prepared_poses.shape != (len(bindings), 4, 4)
            or not np.isfinite(prepared_poses).all()
        ):
            raise ValueError("Robot consistency prepared camera poses are invalid")
        for im_id in sorted(
            {row["im_id"] for row in targets if row["scene_id"] == scene_id}
        ):
            frame = mapped_scene["frames"].get(str(im_id))
            binding = by_image.get(im_id)
            if not isinstance(frame, dict) or not isinstance(binding, dict):
                raise ValueError(
                    "Robot consistency is missing an exported frame binding"
                )
            source = frame.get("source_rgb")
            if (
                not isinstance(source, str)
                or Path(source).parts != ("rgb", binding.get("source_filename"))
                or Path(source).stem
                != str(binding.get("source_frame_id")).zfill(len(Path(source).stem))
            ):
                raise ValueError(
                    "Robot consistency source frame does not match GT provenance"
                )
            pose = matched.get(binding["source_filename"], {}).get("robot_ee_pose")
            if not isinstance(pose, dict) or any(
                type(pose.get(key)) not in (int, float) or not np.isfinite(pose[key])
                for key in ("X", "Y", "Z", "A", "B", "C")
            ):
                raise ValueError(
                    "Robot consistency matched robot pose is missing or invalid"
                )
            reference = _rigid(robot_ee_to_reference(pose) @ camera_to_flange)
            expected_reference = prepared_poses[im_id].copy()
            expected_reference[:3, 3] *= 1000.0
            if not np.allclose(reference, expected_reference, rtol=0, atol=1e-6):
                raise ValueError(
                    "Robot consistency calibration/robot transforms contradict the exported camera poses"
                )
            rows = sorted(
                instance_rows[(scene_id, im_id)], key=lambda row: row["gt_id"]
            )
            annotations = gt[str(im_id)]
            if (
                [row["gt_id"] for row in rows] != list(range(len(annotations)))
                or any(
                    row["obj_id"] != annotation["obj_id"]
                    for row, annotation in zip(rows, annotations, strict=True)
                )
                or len({row["instance_uuid"] for row in rows}) != len(rows)
            ):
                raise ValueError(
                    "Robot consistency instance map does not match the scene annotations"
                )
            for row, annotation in zip(rows, annotations, strict=True):
                fixed = reference @ _annotation_pose(annotation)
                previous = fixed_object_poses.setdefault(row["instance_uuid"], fixed)
                if not np.allclose(fixed, previous, rtol=0, atol=1e-4):
                    raise ValueError(
                        "Robot consistency requires each workpiece to stay fixed in template_base"
                    )
            scene["frames"].append(
                {
                    "im_id": im_id,
                    "camera_to_reference_mm": reference.tolist(),
                    "instances": rows,
                }
            )
    verify_sources(root, snapshot)
    return snapshot


def _symmetries(info: Mapping[str, Any]) -> tuple[list[np.ndarray], list[dict]]:
    discrete = [np.eye(4)]
    for values in info.get("symmetries_discrete", []):
        symmetry = _rigid(np.asarray(values).reshape(4, 4))
        if not any(
            np.allclose(symmetry, previous, rtol=0, atol=1e-9) for previous in discrete
        ):
            discrete.append(symmetry)
    continuous = []
    for raw in info.get("symmetries_continuous", []):
        axis = np.asarray(raw["axis"], dtype=float)
        offset = np.asarray(raw["offset"], dtype=float)
        if (
            axis.shape != (3,)
            or offset.shape != (3,)
            or not np.isfinite(axis).all()
            or not np.isfinite(offset).all()
            or not np.isclose(np.linalg.norm(axis), 1, atol=1e-6)
        ):
            raise ValueError("Robot consistency continuous symmetry is invalid")
        continuous.append({"axis": axis, "offset": offset})
    return discrete, continuous


def canonical_pose(pose: np.ndarray, info: Mapping[str, Any]) -> np.ndarray:
    """IPD's identity-referenced symmetry gauge, using BOP symmetry metadata.

    Unlike a rigid-pose average, the elementwise mean below is intentionally
    allowed to be non-rigid, as in IPD. Only continuous symmetry reduction uses
    scipy's rotation conversion, also as in the reference implementation.
    """
    discrete, continuous = _symmetries(info)
    candidates = [pose @ symmetry for symmetry in discrete]
    if not continuous:
        return min(
            candidates,
            key=lambda candidate: np.square(candidate[:3, :3] - np.eye(3)).sum(),
        )
    if len(continuous) > 1:
        projectors = [
            np.eye(3) - np.outer(row["axis"], row["axis"]) for row in continuous
        ]
        lhs = np.concatenate(projectors)
        rhs = np.concatenate(
            [
                projector @ row["offset"]
                for projector, row in zip(projectors, continuous, strict=True)
            ]
        )
        center, _, rank, _ = np.linalg.lstsq(lhs, rhs, rcond=None)
        if rank != 3 or not np.allclose(lhs @ center, rhs, atol=1e-6):
            raise ValueError(
                "Robot consistency continuous symmetry axes must share a center"
            )
        result = pose.copy()
        result[:3, 3] += (pose[:3, :3] - np.eye(3)) @ center
        result[:3, :3] = np.eye(3)
        return result
    axis, offset = continuous[0]["axis"], continuous[0]["offset"]
    coordinate_axis = int(np.argmax(np.abs(axis)))
    if np.allclose(np.abs(axis), np.eye(3)[coordinate_axis], atol=1e-8):
        basis = np.eye(3)
        axis = np.eye(3)[coordinate_axis]
    else:
        coordinate_axis = 2
        helper = np.eye(3)[int(np.argmin(np.abs(axis)))]
        first = np.cross(helper, axis)
        first /= np.linalg.norm(first)
        basis = np.column_stack([first, np.cross(axis, first), axis])
    flip_allowed = any(
        np.allclose(symmetry[:3, :3] @ axis, -axis, rtol=0, atol=1e-8)
        for symmetry in discrete
    )
    if flip_allowed:
        # IPD's mixed-symmetry case removes spin by aligning the symmetry axis.
        # Discrete reductions can flip that axis; the identity reference fixes
        # its sign, with the next reference axis breaking perpendicular ties.
        direction = pose[:3, :3] @ axis
        if direction @ axis < -1e-12 or (
            abs(direction @ axis) <= 1e-12
            and direction @ basis[:, (coordinate_axis + 1) % 3] > 0
        ):
            direction = -direction
        # IPD also applies this reduction to its non-rigid arithmetic mean.
        # Do not normalize the averaged axis or project this matrix to SO(3):
        # either would change the reference metric for mixed symmetries.
        cosine = float(direction @ axis)
        cross = np.cross(axis, direction)
        skew = np.asarray(
            [
                [0, -cross[2], cross[1]],
                [cross[2], 0, -cross[0]],
                [-cross[1], cross[0], 0],
            ]
        )
        canonical_rotation = (
            np.eye(3)
            if cosine >= 1
            else np.eye(3) + skew + (skew @ skew) / (1 + cosine)
        )
    else:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Gimbal lock detected")
            euler = Rotation.from_matrix(basis.T @ pose[:3, :3] @ basis).as_euler(
                ("xyx", "yxy", "zyz")[coordinate_axis]
            )
        euler[0] = 0
        canonical_rotation = (
            basis
            @ Rotation.from_euler(
                ("xyx", "yxy", "zyz")[coordinate_axis], euler
            ).as_matrix()
            @ basis.T
        )
    result = pose.copy()
    result[:3, 3] += (pose[:3, :3] - canonical_rotation) @ offset
    result[:3, :3] = canonical_rotation
    return result


def measure_track(
    camera_to_reference: np.ndarray,
    model_to_camera: np.ndarray,
    vertices_mm: np.ndarray,
    model_info: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute both IPD vertex metrics; do not project its arithmetic mean."""
    reference_poses = np.asarray(
        [
            canonical_pose(reference @ prediction, model_info)
            for reference, prediction in zip(
                camera_to_reference, model_to_camera, strict=True
            )
        ]
    )
    reference_mean = reference_poses.mean(axis=0)
    frame_errors = []
    for reference, prediction in zip(camera_to_reference, model_to_camera, strict=True):
        synthesized = canonical_pose(
            np.linalg.inv(reference) @ reference_mean, model_info
        )
        difference = canonical_pose(prediction, model_info) - synthesized
        maximum, total, count = 0.0, 0.0, 0
        for start in range(0, len(vertices_mm), VERTEX_CHUNK):
            chunk = vertices_mm[start : start + VERTEX_CHUNK]
            distances = np.linalg.norm(
                chunk @ difference[:3, :3].T + difference[:3, 3], axis=1
            )
            maximum = max(maximum, float(distances.max()))
            total += float(distances.sum())
            count += len(chunk)
        if count == 0:
            raise ValueError("Robot consistency evaluation model has no vertices")
        frame_errors.append({"mvd_mm": maximum, "add_mm": total / count})
    return {
        "mvd_mm": float(np.mean([row["mvd_mm"] for row in frame_errors])),
        "add_mm": float(np.mean([row["add_mm"] for row in frame_errors])),
        "reference_mean_model_to_template_base_mm": reference_mean.tolist(),
        "frame_errors": frame_errors,
    }


def evaluate(
    run_root: Path,
    result_path: Path,
    manifest: Mapping[str, Any],
    targets: list[dict[str, int]],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate matched predictions per sensor/instance, retaining coverage."""
    root = run_root.resolve()
    verify_sources(root, inputs)
    models_info = json.loads(
        _path(root, "bop/models_eval/models_info.json").read_text()
    )
    estimates: dict[tuple[int, int, int], list[np.ndarray]] = defaultdict(list)
    target_keys = {(row["scene_id"], row["im_id"], row["obj_id"]) for row in targets}
    with result_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (int(row["scene_id"]), int(row["im_id"]), int(row["obj_id"]))
            if key in target_keys:
                pose = np.eye(4)
                pose[:3, :3] = np.asarray(
                    [float(value) for value in row["R"].split()]
                ).reshape(3, 3)
                pose[:3, 3] = [float(value) for value in row["t"].split()]
                estimates[key].append(_rigid(pose))
    tracks: dict[tuple[int, str], dict] = {}
    excluded = []
    candidate_count = matched_count = prediction_count = 0
    for scene in inputs["scenes"]:
        scene_id = scene["scene_id"]
        if scene["status"] != "available":
            excluded.append(
                {key: scene[key] for key in ("scene_id", "status", "reason")}
            )
            continue
        gt_path = _path(
            root, f"bop/{manifest['exports'][0]['split']}/{scene_id:06d}/scene_gt.json"
        )
        ground_truth = json.loads(gt_path.read_text())
        for frame in scene["frames"]:
            im_id = frame["im_id"]
            by_object: dict[int, list[dict]] = defaultdict(list)
            for identity in frame["instances"]:
                obj_id = identity["obj_id"]
                if (scene_id, im_id, obj_id) not in target_keys:
                    continue
                by_object[obj_id].append(identity)
                key = (scene_id, identity["instance_uuid"])
                track = tracks.setdefault(
                    key,
                    {
                        "scene_id": scene_id,
                        "sensor_name": scene["sensor_name"],
                        "instance_uuid": identity["instance_uuid"],
                        "obj_id": obj_id,
                        "eligible_frames": 0,
                        "observations": [],
                    },
                )
                track["eligible_frames"] += 1
                candidate_count += 1
            for obj_id, identities in by_object.items():
                predictions = estimates[(scene_id, im_id, obj_id)]
                prediction_count += len(predictions)
                if not predictions:
                    continue
                info = models_info[str(obj_id)]
                truth_positions = np.asarray(
                    [
                        canonical_pose(
                            _annotation_pose(
                                ground_truth[str(im_id)][identity["gt_id"]]
                            ),
                            info,
                        )[:3, 3]
                        for identity in identities
                    ]
                )
                prediction_positions = np.asarray(
                    [
                        canonical_pose(prediction, info)[:3, 3]
                        for prediction in predictions
                    ]
                )
                distances = np.linalg.norm(
                    truth_positions[:, None] - prediction_positions[None], axis=2
                )
                penalty = MATCH_THRESHOLD_MM * (len(identities) + 1)
                costs = np.concatenate(
                    [
                        np.where(
                            distances < MATCH_THRESHOLD_MM, distances, penalty * 2
                        ),
                        np.full((len(identities), len(identities)), penalty),
                    ],
                    axis=1,
                )
                gt_indices, pred_indices = linear_sum_assignment(costs)
                for gt_index, pred_index in zip(gt_indices, pred_indices, strict=True):
                    if (
                        pred_index >= len(predictions)
                        or distances[gt_index, pred_index] >= MATCH_THRESHOLD_MM
                    ):
                        continue
                    identity = identities[gt_index]
                    tracks[(scene_id, identity["instance_uuid"])][
                        "observations"
                    ].append(
                        {
                            "im_id": im_id,
                            "reference": _rigid(frame["camera_to_reference_mm"]),
                            "prediction": predictions[pred_index],
                        }
                    )
                    matched_count += 1
    records = []
    vertices_by_object = {}
    for track in tracks.values():
        observations = track.pop("observations")
        track["matched_frames"] = len(observations)
        track["coverage"] = len(observations) / track["eligible_frames"]
        if len(observations) < 2:
            track.update(
                status="unavailable",
                reason="At least two matched views of this instance are required.",
            )
        elif all(
            np.allclose(
                observation["reference"],
                observations[0]["reference"],
                rtol=0,
                atol=1e-9,
            )
            for observation in observations[1:]
        ):
            track.update(
                status="unavailable",
                reason="Matched views have no robot-driven change of viewpoint.",
            )
        else:
            obj_id = track["obj_id"]
            if obj_id not in vertices_by_object:
                model_path = _path(root, f"bop/models_eval/obj_{obj_id:06d}.ply")
                if not _plain_file(model_path, root=root):
                    raise ValueError("Robot consistency evaluation model is missing")
                mesh = trimesh.load(model_path, process=False)
                vertices = np.asarray(mesh.vertices, dtype=float)
                if (
                    vertices.ndim != 2
                    or vertices.shape[1] != 3
                    or not np.isfinite(vertices).all()
                ):
                    raise ValueError(
                        "Robot consistency evaluation model vertices are invalid"
                    )
                vertices_by_object[obj_id] = vertices
            track.update(
                status="available",
                **measure_track(
                    np.asarray([row["reference"] for row in observations]),
                    np.asarray([row["prediction"] for row in observations]),
                    vertices_by_object[obj_id],
                    models_info[str(obj_id)],
                ),
            )
            for observation, error in zip(
                observations, track["frame_errors"], strict=True
            ):
                error["im_id"] = observation["im_id"]
        records.append(track)
    available = [row for row in records if row["status"] == "available"]
    metrics = [
        {
            "id": f"robot_consistency_{kind}",
            "label": f"Robot consistency {kind.upper()}",
            "unit": "mm",
            "value": float(np.mean([row[f"{kind}_mm"] for row in available])),
            "display": f"{np.mean([row[f'{kind}_mm'] for row in available]):.4f}",
            "source": "ipd",
            "direction": "lower",
        }
        for kind in ("mvd", "add")
        if available
    ]
    verify_sources(root, inputs)
    return {
        "schema_version": "bop_robot_consistency_report.v1",
        "implementation_revision": REVISION,
        "ipd_revision": IPD_REVISION,
        "source_url": IPD_SOURCE,
        "status": (
            "partial" if excluded or len(available) < len(records) else "available"
        )
        if available
        else "unavailable",
        "reason": None
        if available
        else "No instance has two matched robot-driven viewpoints with complete retained evidence.",
        "reference_frame": "template_base",
        "translation_unit": "mm",
        "matching_threshold_mm": MATCH_THRESHOLD_MM,
        "aggregation": "mean_over_matched_views_then_equal_weight_mean_over_sensor_instance_tracks",
        "eligible_frames": candidate_count,
        "matched_frames": matched_count,
        "prediction_count": prediction_count,
        "unmatched_predictions": prediction_count - matched_count,
        "coverage": matched_count / candidate_count if candidate_count else None,
        "track_count": len(records),
        "evaluated_track_count": len(available),
        "excluded_scenes": excluded,
        "warnings": [
            "Consistency measures repeatability across viewpoints; a fixed pose bias can still produce zero error. Scores depend on robot/hand-eye calibration and viewpoint diversity.",
            "GT translations are used only for 100 mm instance association; missing/rejected predictions are excluded from the score and retained in coverage.",
        ],
        "metrics": metrics,
        "tracks": records,
    }


def _annotation_pose(annotation: Mapping[str, Any]) -> np.ndarray:
    pose = np.eye(4)
    pose[:3, :3] = np.asarray(annotation["cam_R_m2c"]).reshape(3, 3)
    pose[:3, 3] = annotation["cam_t_m2c"]
    return _rigid(pose)


def public_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    summary = {key: value for key, value in report.items() if key != "tracks"}
    summary["tracks"] = [
        {
            key: value
            for key, value in track.items()
            if key not in {"frame_errors", "reference_mean_model_to_template_base_mm"}
        }
        for track in report["tracks"][:PUBLIC_TRACK_LIMIT]
    ]
    summary["tracks_truncated"] = len(report["tracks"]) > PUBLIC_TRACK_LIMIT
    return summary
