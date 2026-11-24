"""Depth readiness for pose-bearing exports, independent of BOP19 targets/masks."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from uuid import UUID

import numpy as np

from posetestbot.bop.annotations import _verified_pose_provenance
from posetestbot.bop.evaluation import _has_symlink_component, _png_size, _sha256_file
from posetestbot.bop.inspection import (
    _finite_vector,
    _integer_id,
    _required_plain_file,
    _safe_relative,
)
from posetestbot.calibration.profiles import CalibrationStatus, profile_from_dict
from posetestbot.pose_templates.selection import OBJECT_INSTANCES_SCHEMA_VERSION


BOP_MANIFEST = "bop/bop_export_manifest.json"


def read_json(root: Path, relative: str):
    path = _safe_relative(root, relative, label="Depth evidence")
    _required_plain_file(path, root=root, label="Depth evidence")
    return json.loads(path.read_text())


def identity(root: Path, relative: str):
    path = _safe_relative(root, relative, label="Depth source")
    if not path.exists():
        return {"path": relative, "stat": None}
    _required_plain_file(path, root=root, label="Depth source")
    st = path.stat()
    return {
        "path": relative,
        "stat": [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns],
    }


def verify_identities(root: Path, sources):
    for source in sources:
        if identity(root, source["path"]) != source:
            raise ValueError(f"Depth source changed: {source['path']}")


def hash_sources(root: Path, sources):
    verify_identities(root, sources)
    hashes = {
        s["path"]: _sha256_file(root / s["path"]) if s["stat"] is not None else None
        for s in sources
    }
    verify_identities(root, sources)
    return hashes


def verify_hashes(root: Path, hashes):
    for relative, expected in hashes.items():
        path = _safe_relative(root, relative, label="Depth source")
        if expected is None:
            if path.exists():
                raise ValueError(
                    f"Previously missing depth source appeared: {relative}"
                )
        elif not path.is_file() or _sha256_file(path) != expected:
            raise ValueError(f"Depth source hash changed: {relative}")


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Depth instance/catalogue UUID is invalid")
    return value


def _profiles(manifest):
    profiles = {}
    for raw in manifest.get("calibration_profiles", []):
        profile = profile_from_dict(raw)
        if profile.profile_id in profiles or profile.status != CalibrationStatus.VALID:
            raise ValueError(
                "Depth export calibration snapshots are invalid or duplicated"
            )
        profiles[profile.profile_id] = profile
    return profiles


def _scene_projection(export, profiles):
    projection = export.get("projection")
    if projection not in ("native", "rectified"):
        raise ValueError("Depth scene projection is invalid")
    profile = profiles.get(export.get("calibration_profile_id"))
    if profile is None:
        raise ValueError("Depth scene has no exported valid calibration snapshot")
    intrinsics = (
        profile.rectified_intrinsics
        if projection == "rectified"
        else profile.intrinsics
    )
    if intrinsics is None:
        raise ValueError("Depth scene lacks its rectified calibration snapshot")
    return intrinsics, {
        "scene_id": export["scene_id"],
        "sensor_name": export["sensor_name"],
        "calibration_profile_id": profile.profile_id,
        "width": intrinsics.width,
        "height": intrinsics.height,
        "projection": projection,
        "distortion_model": intrinsics.distortion_model,
        "distortion": list(intrinsics.distortion),
    }


def projection_blocker(scene):
    if any(scene["distortion"]):
        return (
            f"{scene['projection'].capitalize()} export has nonzero lens distortion. "
            "Use a rectified BOP export with zero distortion for depth evaluation."
        )
    return None


def require_pinhole_projections(scenes):
    for scene in scenes:
        if reason := projection_blocker(scene):
            raise ValueError(f"{scene['sensor_name']} — {reason}")


def retained_projections(root, expected_sha256, scene_ids):
    """Recover projection evidence for an older study from its hash-bound export."""

    path = _safe_relative(root, BOP_MANIFEST, label="Depth export manifest")
    _required_plain_file(path, root=root, label="Depth export manifest")
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != expected_sha256:
        raise ValueError(
            "BOP export manifest hash changed since the retained depth study"
        )
    manifest = json.loads(contents)
    profiles = _profiles(manifest)
    scenes = [
        _scene_projection(export, profiles)[1]
        for export in manifest["exports"]
        if export["scene_id"] in scene_ids
    ]
    return scenes


def inventory(run_root: str | Path):
    root = Path(run_root).resolve()
    if _has_symlink_component(root / "bop", root=root):
        raise ValueError("Depth BOP export uses a symbolic link")
    paths = {
        BOP_MANIFEST,
        "bop/posetestbot_bop_frame_map.json",
        "bop/posetestbot_instance_map.json",
        "object_instances.json",
        "bop/models/models_info.json",
    }
    manifest = read_json(root, BOP_MANIFEST)
    mode = manifest.get("annotation_mode")
    if (
        manifest.get("schema_version") != "bop_export_manifest.v5"
        or mode not in ("pose", "pose_and_masks")
        or manifest.get("annotation_state")
        != {"pose": "poses", "pose_and_masks": "complete"}.get(mode)
        or manifest.get("annotation_source") != "blenderproc"
        or manifest.get("format") != "bop-scenewise"
        or manifest.get("layout") != "<split>/<scene_id>"
        or manifest.get("dataset_root") != "."
        or manifest.get("validation", {}).get("status") != "ok"
    ):
        raise ValueError(
            "Depth evaluation requires a completed, validated, pose-bearing BOP v5 export"
        )
    profiles = _profiles(manifest)
    quality = [
        {
            "profile_id": profile.profile_id,
            "quality": asdict(profile.quality),
            "metadata": dict(profile.metadata),
        }
        for profile in profiles.values()
    ]
    instances = read_json(root, "object_instances.json")
    if instances.get("schema_version") != OBJECT_INSTANCES_SCHEMA_VERSION:
        raise ValueError("Depth evaluation requires current immutable object instances")
    by_instance = {}
    objects = {}
    model_info = read_json(root, "bop/models/models_info.json")
    models = {row["obj_id"]: row for row in manifest.get("object_models", [])}
    if len(models) != len(manifest.get("object_models", [])):
        raise ValueError("Depth export model IDs are duplicated")
    for raw in instances["instances"]:
        instance = _uuid(raw["instance_uuid"])
        obj = _integer_id(raw["obj_id"], label="Depth object ID", minimum=1)
        _uuid(raw["catalog_uuid"])
        canonical = raw["canonical_ply_sha256"]
        if (
            not isinstance(canonical, str)
            or len(canonical) != 64
            or any(c not in "0123456789abcdef" for c in canonical)
        ):
            raise ValueError("Depth canonical geometry hash is invalid")
        path = _safe_relative(
            root, raw["canonical_ply"], label="Canonical geometry snapshot"
        )
        _required_plain_file(path, root=root, label="Canonical geometry snapshot")
        if instance in by_instance or (
            obj in objects
            and (
                objects[obj]["canonical_sha256"] != canonical
                or objects[obj]["catalog_uuid"] != raw["catalog_uuid"]
            )
        ):
            raise ValueError(
                "Depth object instance identity contradicts its geometry snapshot"
            )
        if obj not in models or models[obj].get("source_path") != raw["canonical_ply"]:
            raise ValueError(
                "Depth exported model does not bind its canonical snapshot"
            )
        info = model_info.get(str(obj), {})
        if (
            info.get("posetestbot_geometry", {}).get("source_sha256") != canonical
            or info.get("source_path") != raw["canonical_ply"]
            or info.get("catalog_uuid") != raw["catalog_uuid"]
        ):
            raise ValueError(
                "Depth export model geometry hash contradicts its canonical snapshot"
            )
        by_instance[instance] = raw
        objects[obj] = {
            "obj_id": obj,
            "name": raw["name"],
            "canonical_sha256": canonical,
            "path": raw["canonical_ply"],
            "catalog_uuid": raw["catalog_uuid"],
        }
        paths.add(raw["canonical_ply"])
    if not objects or set(models) != set(objects):
        raise ValueError(
            "Depth export models do not exactly cover known object geometry"
        )
    maps = read_json(root, "bop/posetestbot_bop_frame_map.json")
    imap = read_json(root, "bop/posetestbot_instance_map.json")
    if (
        maps.get("schema_version") != "posetestbot_bop_frame_map.v3"
        or imap.get("schema_version") != "posetestbot_bop_instance_map.v1"
    ):
        raise ValueError("Depth evaluation requires current frame and instance maps")
    identities = {}
    for item in imap["instances"]:
        key = tuple(
            _integer_id(item[k], label=k) for k in ("scene_id", "im_id", "gt_id")
        )
        instance = _uuid(item["instance_uuid"])
        if (
            key in identities
            or instance not in by_instance
            or any(
                item[k] != by_instance[instance][k] for k in ("obj_id", "catalog_uuid")
            )
        ):
            raise ValueError("Depth instance map contradicts frozen geometry")
        identities[key] = item
    scenes, frames, seen, seen_instances = [], [], set(), set()
    for export in manifest.get("exports", []):
        scene_id = _integer_id(export["scene_id"], label="scene_id")
        sensor = export["sensor_name"]
        if scene_id in seen or not isinstance(sensor, str) or not sensor:
            raise ValueError("Depth scene/sensor identity is invalid or duplicated")
        seen.add(scene_id)
        relative = export["scene_folder"]
        if relative != f"{export['split']}/{scene_id:06d}":
            raise ValueError("Depth scene does not use the standard BOP folder")
        scene = _safe_relative(root / "bop", relative, label="Depth scene")
        _verified_pose_provenance(
            root, export, scene=scene, scene_gt_path=scene / "scene_gt.json", mode=mode
        )
        provenance_rel = f"bop/{export['annotation_provenance']['artifact']}"
        provenance = read_json(root, provenance_rel)
        if provenance.get("projection") != export.get("projection"):
            raise ValueError(
                "Depth pose provenance contradicts its exported projection"
            )
        camera_rel, gt_rel = (
            f"bop/{relative}/scene_camera.json",
            f"bop/{relative}/scene_gt.json",
        )
        camera, gt = read_json(root, camera_rel), read_json(root, gt_rel)
        paths.update((provenance_rel, camera_rel, gt_rel))
        mapped = maps["scenes"].get(str(scene_id))
        if not isinstance(mapped, dict) or any(
            mapped.get(k) != export.get(k)
            for k in ("sensor_name", "scene_folder", "split", "projection")
        ):
            raise ValueError("Depth frame map contradicts its exported scene")
        bindings = provenance["frame_bindings"]
        binding_by_id = {str(row["output_image_id"]): row for row in bindings}
        if (
            set(camera) != set(gt)
            or set(gt) != set(mapped["frames"])
            or set(gt) != set(binding_by_id)
            or len(bindings) != len(binding_by_id)
            or len(gt) != export["rgb_count"]
            or not gt
        ):
            raise ValueError("Depth camera, GT, and provenance frame bindings differ")
        intrinsics, projection = _scene_projection(export, profiles)
        width, height = intrinsics.width, intrinsics.height
        if width * height > 32_000_000 or provenance.get("resolution") != {
            "width": width,
            "height": height,
        }:
            raise ValueError(
                "Depth resolution contradicts calibration/pose evidence or exceeds the pixel limit"
            )
        scenes.append(projection)
        for key in sorted(gt, key=int):
            im_id = int(key)
            if key != str(im_id) or im_id < 0:
                raise ValueError(
                    "Depth frame IDs must be canonical nonnegative integers"
                )
            cam = camera[key]
            intrinsic = np.array(
                _finite_vector(cam["cam_K"], count=9, label="cam_K")
            ).reshape(3, 3)
            scale = cam["depth_scale"]
            if (
                type(scale) not in (int, float)
                or not np.isfinite(scale)
                or scale <= 0
                or not np.allclose(
                    intrinsic.ravel(), intrinsics.cam_k, rtol=0, atol=1e-7
                )
                or not np.isclose(
                    scale, intrinsics.depth_scale_to_mm, rtol=0, atol=1e-9
                )
                or not np.allclose(intrinsic[[0, 1], [1, 0]], 0, atol=1e-9)
            ):
                raise ValueError(
                    "Depth camera metadata contradicts exported calibration"
                )
            binding, mapping = binding_by_id[key], mapped["frames"][key]
            if (
                mapping.get("bop_rgb") != f"rgb/{im_id:06d}.png"
                or mapping.get("bop_depth") != f"depth/{im_id:06d}.png"
                or Path(mapping.get("source_rgb", "")).name
                != binding.get("source_filename")
                or Path(mapping.get("source_depth", "")).name
                != binding.get("source_filename")
            ):
                raise ValueError("Depth source image binding is contradictory")
            rgb, depth = (
                f"bop/{relative}/rgb/{im_id:06d}.png",
                f"bop/{relative}/depth/{im_id:06d}.png",
            )
            for media, kind in ((rgb, "rgb"), (depth, "depth")):
                path = _safe_relative(root, media, label="Depth frame image")
                if kind == "depth" and not path.exists():
                    continue
                _required_plain_file(path, root=root, label="Depth frame image")
                if _png_size(path, label="Depth frame image", expected_kind=kind) != (
                    width,
                    height,
                ):
                    raise ValueError(
                        "Depth frame dimensions contradict exported calibration"
                    )
            rows = gt[key]
            if not isinstance(rows, list):
                raise ValueError("Depth GT rows must be an array")
            frame_instances = set()
            for gt_id, row in enumerate(rows):
                rotation = np.array(
                    _finite_vector(row["cam_R_m2c"], count=9, label="GT rotation")
                ).reshape(3, 3)
                _finite_vector(row["cam_t_m2c"], count=3, label="GT translation")
                if not np.allclose(
                    rotation.T @ rotation, np.eye(3), atol=1e-5
                ) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5):
                    raise ValueError("Depth GT rotation must be proper and orthonormal")
                identity_key = scene_id, im_id, gt_id
                mapped_instance = identities.get(identity_key)
                if (
                    mapped_instance is None
                    or mapped_instance["obj_id"] != row["obj_id"]
                    or mapped_instance["instance_uuid"] in frame_instances
                ):
                    raise ValueError(
                        "Depth GT does not exactly match immutable instance identities"
                    )
                frame_instances.add(mapped_instance["instance_uuid"])
                seen_instances.add(identity_key)
                row = {
                    **row,
                    "instance_uuid": mapped_instance["instance_uuid"],
                    "canonical_sha256": objects[row["obj_id"]]["canonical_sha256"],
                }
                rows[gt_id] = row
            if frame_instances != set(by_instance):
                raise ValueError(
                    "Depth GT frame does not cover every known scene instance"
                )
            frames.append(
                {
                    "scene_id": scene_id,
                    "im_id": im_id,
                    "sensor_name": sensor,
                    "width": width,
                    "height": height,
                    "camera": cam,
                    "gt": rows,
                    "rgb": rgb,
                    "depth": depth,
                }
            )
    if (
        not scenes
        or seen_instances != set(identities)
        or set(maps["scenes"]) != {str(s) for s in seen}
    ):
        raise ValueError("Depth scene/instance maps do not exactly cover the export")
    # Optional quality evidence is frozen when present. It never sets eligibility.
    optional_evidence = {}
    for relative in (
        "bop/posetestbot_pose_template.json",
        "pose_template_selection.json",
        "calibration_profile_selection.json",
        "sync_quality_report.json",
        "camera_rectification_report.json",
    ):
        path = _safe_relative(root, relative, label="Depth quality evidence")
        if path.exists():
            optional_evidence[relative] = read_json(root, relative)
            paths.add(relative)
    return {
        "objects": list(objects.values()),
        "scenes": scenes,
        "frames": frames,
        "paths": sorted(paths),
        "quality_evidence": {
            "calibration": quality,
            "placement": manifest.get("pose_template", {}),
            "retained_evidence": optional_evidence,
            "optional_artifacts": sorted(
                p
                for p in paths
                if not p.startswith("bop/") and p != "object_instances.json"
            ),
        },
    }


def setup(run_root):
    try:
        data = inventory(run_root)
        blocked = {
            scene["sensor_name"]: reason
            for scene in data["scenes"]
            if (reason := projection_blocker(scene)) is not None
        }
        sensors = [
            {**scene, "unavailable_reason": blocked.get(scene["sensor_name"])}
            for scene in data["scenes"]
        ]
        ready = any(s["unavailable_reason"] is None for s in sensors)
        return {
            "ready": ready,
            "blockers": []
            if ready
            else ["Use a rectified BOP export to evaluate depth for these cameras."],
            "objects": [
                {k: v for k, v in o.items() if k != "path"} for o in data["objects"]
            ],
            "sensors": sensors,
            "frame_count": len(data["frames"]),
            "quality_evidence": data["quality_evidence"],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {
            "ready": False,
            "blockers": [str(exc)],
            "objects": [],
            "sensors": [],
            "frame_count": 0,
            "quality_evidence": {},
        }
