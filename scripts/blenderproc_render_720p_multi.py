import blenderproc as bproc  # isort:skip

# Load a BlenderProc 2.8 scene and publish deterministic analytic BOP pose GT.
import argparse
import hashlib
import json
import os
import shutil

import numpy as np


SUPPORTED_BLENDERPROC_VERSION = "2.8.0"
ANNOTATION_MODES = ("pose", "pose_and_masks")
ANALYTIC_IMPLEMENTATION_REVISION = "posetestbot_analytic_bop_gt.v1"


def validated_blenderproc_version():
    """Reject every renderer version except the qualified BlenderProc release."""

    version = getattr(bproc, "__version__", None)
    if version != SUPPORTED_BLENDERPROC_VERSION:
        raise RuntimeError(
            "Pose-template instance GT is validated only with BlenderProc "
            f"{SUPPORTED_BLENDERPROC_VERSION}; found {version}"
        )
    return version


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Load prepared objects and cameras with BlenderProc 2.8, then write "
            "analytic model-to-OpenCV-camera BOP pose annotations."
        )
    )
    parser.add_argument("poses_file", help="Path to camera poses file (.npy)")
    parser.add_argument("camera_matrix", help="Path to camera matrix file (.npy)")
    parser.add_argument("output_dir", help="Path to prepared BlenderProc workspace")
    parser.add_argument(
        "--annotation-mode",
        choices=ANNOTATION_MODES,
        required=True,
        help=(
            "Both modes publish pose evidence only. pose_and_masks records the "
            "request for the later official BOP Toolkit depth-mask step."
        ),
    )
    return parser.parse_args()


def _load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            value,
            handle,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        handle.write("\n")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rigid_transform(value, *, label):
    matrix = np.asarray(value, dtype=float)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{label} must be a finite 4x4 matrix")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise ValueError(f"{label} must have a rigid homogeneous final row")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6):
        raise ValueError(f"{label} rotation must be orthonormal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-6):
        raise ValueError(f"{label} rotation determinant must be +1")
    return matrix


def load_object(
    object_dir,
    *,
    obj_id,
    instance_uuid,
    mesh,
    transform,
):
    """Load one prepared PLY and apply its template-base pose."""

    object_path = os.path.join(object_dir, mesh)
    object2template_path = os.path.join(object_dir, transform)
    if not os.path.isfile(object_path):
        raise FileNotFoundError(f"Prepared object mesh is missing: {object_path}")
    if not os.path.isfile(object2template_path):
        raise FileNotFoundError(
            f"Prepared object transform is missing: {object2template_path}"
        )

    loaded = bproc.loader.load_obj(object_path)
    if len(loaded) != 1:
        raise RuntimeError(
            f"BlenderProc must load exactly one mesh from {object_path}; "
            f"loaded {len(loaded)}"
        )
    obj = loaded[0]
    for material in obj.get_materials():
        material.map_vertex_color()

    object2template = _rigid_transform(
        np.load(object2template_path),
        label=f"object transform {instance_uuid}",
    )
    obj.set_local2world_mat(object2template)
    # Canonical catalogue PLY coordinates are millimetres; Blender uses metres.
    obj.set_scale([0.001, 0.001, 0.001])
    obj.set_cp("category_id", int(obj_id))
    obj.set_cp("posetestbot_instance_uuid", instance_uuid)
    return obj


def load_objects(objects_dir, objects_json):
    if objects_json.get(
        "schema_version"
    ) != "blenderproc_object_instances.v1" or not isinstance(
        objects_json.get("instances"), list
    ):
        raise ValueError("objects.json must use blenderproc_object_instances.v1")
    return [
        load_object(
            objects_dir,
            obj_id=int(item["obj_id"]),
            instance_uuid=item["instance_uuid"],
            mesh=item["mesh"],
            transform=item["transform"],
        )
        for item in objects_json["instances"]
    ]


def setup_camera(camera_matrix_path, poses_file_path, *, width, height):
    """Load the exact prepared OpenCV cameras into BlenderProc."""

    camera_matrix = np.asarray(np.load(camera_matrix_path), dtype=float)
    if (
        camera_matrix.shape != (3, 3)
        or not np.all(np.isfinite(camera_matrix))
        or camera_matrix[0, 0] <= 0
        or camera_matrix[1, 1] <= 0
    ):
        raise ValueError("camera_matrix.npy must be a finite 3x3 intrinsic matrix")
    camera_poses = np.asarray(np.load(poses_file_path), dtype=float)
    if (
        camera_poses.ndim != 3
        or camera_poses.shape[0] < 1
        or camera_poses.shape[1:] != (4, 4)
    ):
        raise ValueError("camera_poses.npy must be a non-empty Nx4x4 array")

    bproc.camera.set_intrinsics_from_K_matrix(camera_matrix, width, height)
    for index, camera_pose in enumerate(camera_poses):
        camera_pose = _rigid_transform(
            camera_pose,
            label=f"camera pose {index}",
        )
        cam2world = bproc.math.change_source_coordinate_frame_of_transformation_matrix(
            camera_pose,
            ["X", "-Y", "-Z"],
        )
        bproc.camera.add_camera_pose(cam2world)
    return camera_matrix, camera_poses


def _stable_float(value):
    value = float(value)
    return 0.0 if abs(value) < 1e-12 else value


def build_analytic_scene_gt(camera_poses, objects_dir, instance_records):
    """Return BOP model-to-OpenCV-camera poses from calibrated rigid transforms."""

    object_poses = [
        _rigid_transform(
            np.load(os.path.join(objects_dir, item["transform"])),
            label=f"object transform {item['instance_uuid']}",
        )
        for item in instance_records
    ]
    scene_gt = {}
    for image_id, template_from_camera in enumerate(camera_poses):
        camera_from_template = np.linalg.inv(
            _rigid_transform(
                template_from_camera,
                label=f"camera pose {image_id}",
            )
        )
        annotations = []
        for instance, template_from_object in zip(
            instance_records, object_poses, strict=True
        ):
            camera_from_object = camera_from_template @ template_from_object
            camera_from_object = _rigid_transform(
                camera_from_object,
                label=(f"model-to-camera pose {image_id}/{instance['instance_uuid']}"),
            )
            annotations.append(
                {
                    "cam_R_m2c": [
                        _stable_float(value)
                        for value in camera_from_object[:3, :3].reshape(-1)
                    ],
                    "cam_t_m2c": [
                        _stable_float(value * 1000.0)
                        for value in camera_from_object[:3, 3]
                    ],
                    "obj_id": int(instance["obj_id"]),
                }
            )
        scene_gt[str(image_id)] = annotations
    return scene_gt


def build_identity_sidecar(
    *,
    blenderproc_version,
    annotation_mode,
    instance_records,
    frame_bindings,
):
    frames = {}
    for frame in frame_bindings:
        image_id = str(frame["output_image_id"])
        frames[image_id] = [
            {
                "gt_id": gt_id,
                "obj_id": int(instance["obj_id"]),
                "instance_uuid": instance["instance_uuid"],
                "catalog_uuid": instance["catalog_uuid"],
            }
            for gt_id, instance in enumerate(instance_records)
        ]
    return {
        "schema_version": "posetestbot_render_instances.v1",
        "blenderproc_version": blenderproc_version,
        "supported_blenderproc_version": SUPPORTED_BLENDERPROC_VERSION,
        "annotation_mode": annotation_mode,
        "identity_contract": "bop_gt_index_matches_loaded_instance_order.v1",
        "instances": instance_records,
        "frame_bindings": frame_bindings,
        "frames": frames,
    }


def build_provenance(
    *,
    output_dir,
    blenderproc_version,
    annotation_mode,
    frame_contract,
    instance_records,
):
    input_files = {
        "camera_matrix.npy": _sha256(os.path.join(output_dir, "camera_matrix.npy")),
        "camera_poses.npy": _sha256(os.path.join(output_dir, "camera_poses.npy")),
        "frame_contract.json": _sha256(os.path.join(output_dir, "frame_contract.json")),
        "objects.json": _sha256(os.path.join(output_dir, "objects.json")),
    }
    object_files = []
    objects_dir = os.path.join(output_dir, "objects")
    for instance in instance_records:
        record = {
            "instance_uuid": instance["instance_uuid"],
            "mesh": instance["mesh"],
            "mesh_sha256": _sha256(os.path.join(objects_dir, instance["mesh"])),
            "transform": instance["transform"],
            "transform_sha256": _sha256(
                os.path.join(objects_dir, instance["transform"])
            ),
        }
        texture = instance.get("texture")
        if texture:
            record["texture"] = texture
            record["texture_sha256"] = _sha256(os.path.join(objects_dir, texture))
        object_files.append(record)
    return {
        "schema_version": "posetestbot_gt_provenance.v1",
        "blenderproc_version": blenderproc_version,
        "supported_blenderproc_version": SUPPORTED_BLENDERPROC_VERSION,
        "annotation_mode": annotation_mode,
        "pose_contract": "analytic_model_to_opencv_camera_rigid_transform.v1",
        "coordinate_frames": {
            "model": "canonical_object_model",
            "camera": "opencv_camera",
            "camera_pose_input": "template_base_from_opencv_camera",
            "object_pose_input": "template_base_from_object",
        },
        "translation_unit": "mm",
        "rotation_storage": "row_major_3x3",
        "projection": frame_contract["projection"],
        "resolution": frame_contract["resolution"],
        "frame_bindings": frame_contract["frames"],
        "source_artifact_sha256": frame_contract["source_artifact_sha256"],
        "analytic_implementation": {
            "revision": ANALYTIC_IMPLEMENTATION_REVISION,
            "script_sha256": _sha256(os.path.abspath(__file__)),
        },
        "scene_loading": {
            "objects": "blenderproc.loader.load_obj",
            "camera_intrinsics": "blenderproc.camera.set_intrinsics_from_K_matrix",
            "camera_poses": "blenderproc.camera.add_camera_pose",
            "image_rendering": False,
            "mask_generation": (
                "official_bop_toolkit_depth_step"
                if annotation_mode == "pose_and_masks"
                else "not_requested"
            ),
        },
        "input_sha256": input_files,
        "object_files": object_files,
    }


def publish_analytic_gt(
    *,
    output_dir,
    camera_poses,
    objects_json,
    frame_contract,
    annotation_mode,
    blenderproc_version,
):
    instance_records = objects_json["instances"]
    frame_bindings = frame_contract["frames"]
    if frame_contract.get("annotation_mode") != annotation_mode:
        raise ValueError(
            "Prepared frame contract annotation mode does not match render request"
        )
    if len(frame_bindings) != len(camera_poses):
        raise ValueError(
            "Prepared frame bindings do not match loaded camera pose count"
        )
    expected_output_ids = list(range(len(frame_bindings)))
    if [item.get("output_image_id") for item in frame_bindings] != expected_output_ids:
        raise ValueError("Prepared frame output IDs must be contiguous and ordered")

    objects_dir = os.path.join(output_dir, "objects")
    scene_gt = build_analytic_scene_gt(
        camera_poses,
        objects_dir,
        instance_records,
    )
    identity = build_identity_sidecar(
        blenderproc_version=blenderproc_version,
        annotation_mode=annotation_mode,
        instance_records=instance_records,
        frame_bindings=frame_bindings,
    )
    provenance = build_provenance(
        output_dir=output_dir,
        blenderproc_version=blenderproc_version,
        annotation_mode=annotation_mode,
        frame_contract=frame_contract,
        instance_records=instance_records,
    )

    scene_dir = os.path.join(output_dir, "train_pbr", "000000")
    if os.path.exists(scene_dir):
        shutil.rmtree(scene_dir)
    os.makedirs(scene_dir)
    _write_json(os.path.join(scene_dir, "scene_gt.json"), scene_gt)
    _write_json(
        os.path.join(scene_dir, "posetestbot_render_instances.json"),
        identity,
    )
    _write_json(
        os.path.join(scene_dir, "posetestbot_gt_provenance.json"),
        provenance,
    )


def main():
    args = parse_arguments()
    blenderproc_version = validated_blenderproc_version()
    bproc.init()

    objects_json_path = os.path.join(args.output_dir, "objects.json")
    frame_contract_path = os.path.join(args.output_dir, "frame_contract.json")
    if not os.path.isfile(objects_json_path):
        raise FileNotFoundError(f"Prepared objects are missing: {objects_json_path}")
    if not os.path.isfile(frame_contract_path):
        raise FileNotFoundError(
            f"Prepared frame contract is missing: {frame_contract_path}"
        )
    objects_json = _load_json(objects_json_path)
    frame_contract = _load_json(frame_contract_path)
    resolution = frame_contract.get("resolution")
    if (
        not isinstance(resolution, dict)
        or not isinstance(resolution.get("width"), int)
        or not isinstance(resolution.get("height"), int)
        or resolution["width"] <= 0
        or resolution["height"] <= 0
    ):
        raise ValueError("Prepared frame resolution is invalid")

    objects_dir = os.path.join(args.output_dir, "objects")
    objects_list = load_objects(objects_dir, objects_json)
    if len(objects_list) != len(objects_json["instances"]):
        raise RuntimeError("Loaded BlenderProc object count changed unexpectedly")
    _camera_matrix, camera_poses = setup_camera(
        args.camera_matrix,
        args.poses_file,
        width=resolution["width"],
        height=resolution["height"],
    )
    publish_analytic_gt(
        output_dir=args.output_dir,
        camera_poses=camera_poses,
        objects_json=objects_json,
        frame_contract=frame_contract,
        annotation_mode=args.annotation_mode,
        blenderproc_version=blenderproc_version,
    )


if __name__ == "__main__":
    main()
