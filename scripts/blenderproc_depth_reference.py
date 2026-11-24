import blenderproc as bproc  # Must be the first import for BlenderProc's launcher.

import argparse
import hashlib
import json
from pathlib import Path

import bpy
import numpy as np


def render(plan_path):
    if bproc.__version__ != "2.8.0":
        raise RuntimeError("Depth references require pinned BlenderProc 2.8.0")
    plan = json.loads(Path(plan_path).read_text())
    if plan["schema_version"] != "depth_render_plan.v1":
        raise ValueError("Unsupported depth render plan")
    output = Path(plan["output"])
    output.mkdir(parents=True, exist_ok=True)
    bproc.init()
    bproc.renderer.set_render_devices(use_only_cpu=True)
    bproc.renderer.set_denoiser(None)
    bproc.renderer.set_max_amount_of_samples(1)
    bproc.renderer.set_noise_threshold(0)
    bpy.context.scene.cycles.filter_width = 0.0
    bpy.context.scene.render.threads_mode = "FIXED"
    bpy.context.scene.render.threads = 4
    material = bproc.material.create("opaque_geometry")
    material.set_principled_shader_value("Alpha", 1.0)
    material.set_principled_shader_value("Transmission Weight", 0.0)
    loaded = {}
    # Each instance has its own transform; shared meshes are loaded just once.
    geometry = {}
    for instance in plan["instances"]:
        model_path = instance["path"]
        if model_path not in geometry:
            meshes = bproc.loader.load_obj(model_path)
            if len(meshes) != 1:
                raise ValueError("Depth canonical geometry must contain one mesh")
            geometry[model_path] = meshes[0]
            obj = meshes[0]
        else:
            obj = geometry[model_path].duplicate()
        obj.set_scale([0.001] * 3)
        obj.set_shading_mode("FLAT")
        obj.replace_materials(material)
        obj.set_cp("category_id", instance["render_id"])
        loaded[instance["instance_uuid"]] = obj
    # OpenCV camera to Blender world. Geometry is placed directly in CV space.
    bproc.camera.add_camera_pose(np.diag([1.0, -1.0, -1.0, 1.0]))
    bproc.renderer.enable_depth_output(activate_antialiasing=False)
    bproc.renderer.enable_normals_output()
    bproc.renderer.enable_segmentation_output(
        map_by="category_id", default_values={"category_id": 0}
    )
    outputs = []
    for frame in plan["frames"]:
        for obj in loaded.values():
            obj.blender_obj.hide_render = True
        for gt in frame["gt"]:
            obj = loaded[gt["instance_uuid"]]
            obj.blender_obj.hide_render = False
            transform = np.eye(4)
            transform[:3, :3] = np.reshape(gt["cam_R_m2c"], (3, 3))
            transform[:3, 3] = np.array(gt["cam_t_m2c"]) / 1000.0
            obj.set_local2world_mat(transform)
            obj.set_scale([0.001] * 3)
        bproc.camera.set_intrinsics_from_K_matrix(
            np.reshape(frame["camera"]["cam_K"], (3, 3)),
            frame["width"],
            frame["height"],
            clip_start=0.0001,
            clip_end=10000,
        )
        data = bproc.renderer.render(
            output_dir=str(output / ".render"),
            load_keys={"depth", "normals", "segmap"},
            verbose=False,
        )
        ids = data["category_id_segmaps"][0].astype(np.int32)
        depth = data["depth"][0].astype(np.float64) * 1000.0
        # BlenderProc encodes OpenGL camera normals in [0,1]: X right, Y up,
        # Z toward the camera. Convert Y/Z to OpenCV's down/forward axes.
        # FLAT shading supplies geometric face normals, without material maps.
        normals = 2.0 * data["normals"][0].astype(np.float32) - 1.0
        normals[..., 1:] *= -1
        depth[ids == 0] = 0
        normals[ids == 0] = 0
        path = output / f"{frame['scene_id']:06d}_{frame['im_id']:06d}.npz"
        np.savez_compressed(path, depth_mm=depth, normals_cv=normals, instance_ids=ids)
        outputs.append(
            {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
        print(f"Depth reference {path.name}", flush=True)
        del data, depth, normals, ids
    (output / "renderer.json").write_text(
        json.dumps(
            {
                "schema_version": "depth_renderer.v1",
                "blenderproc_version": bproc.__version__,
                "blender_version": bpy.app.version_string,
                "device": "CPU",
                "samples": 1,
                "denoiser": None,
                "filter_width": 0.0,
                "depth": "optical_axis_mm_unsmoothed",
                "normals": "flat_opencv_camera",
                "normal_axes": "x_right_y_down_z_forward",
                "materials": "opaque",
                "outputs": outputs,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    render(parser.parse_args().plan)
