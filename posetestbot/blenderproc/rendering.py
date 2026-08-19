"""Transactional BlenderProc render planning and output promotion."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from posetestbot.blenderproc.preparation import (
    FRAME_CONTRACT,
    validate_annotation_mode,
    validate_subdir,
)
from posetestbot.io.atomic import atomic_write_json, replace_directories
from posetestbot.io.artifacts import (
    BLENDERPROC_RENDER_PLAN,
    DEPTH_DIR,
    MASKS_DIR,
    MATCH_ROBOT_EE_POSES,
    RGB_DIR,
)

ANALYTIC_IMPLEMENTATION_REVISION = "posetestbot_analytic_bop_gt.v1"
WORKSPACE_RENDER_SCRIPT = "posetestbot_analytic_gt.py"


@dataclass(frozen=True)
class RenderJob:
    sensor_name: str
    sensor_folder: str
    blenderproc_folder: str
    camera_poses: str
    camera_matrix: str
    frame_contract: str
    annotation_mode: str
    frame_bindings: tuple[dict[str, int | str], ...]
    source_artifact_sha256: dict[str, str]
    analytic_implementation_revision: str
    render_script_sha256: str
    resolution: tuple[int, int]
    expected_frame_count: int
    command: list[str]


def _frame_contract(path: Path) -> dict[str, Any]:
    value = _read_json_mapping(path)
    if value.get("schema_version") != "blenderproc_frame_contract.v1":
        raise ValueError(f"Unsupported BlenderProc frame contract: {path}")
    annotation_mode = value.get("annotation_mode")
    if not isinstance(annotation_mode, str):
        raise ValueError(f"Frame contract annotation_mode is missing: {path}")
    validate_annotation_mode(annotation_mode)
    resolution = value.get("resolution")
    if (
        not isinstance(resolution, Mapping)
        or not isinstance(resolution.get("width"), int)
        or not isinstance(resolution.get("height"), int)
        or int(resolution["width"]) <= 0
        or int(resolution["height"]) <= 0
    ):
        raise ValueError(f"Frame contract resolution is invalid: {path}")
    frames = value.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError(f"Frame contract must bind at least one frame: {path}")
    expected_output_ids = list(range(len(frames)))
    actual_output_ids = []
    filenames = []
    source_ids = []
    for frame in frames:
        if not isinstance(frame, Mapping):
            raise ValueError(f"Frame contract entries must be objects: {path}")
        output_id = frame.get("output_image_id")
        source_id = frame.get("source_frame_id")
        filename = frame.get("source_filename")
        if (
            not isinstance(output_id, int)
            or not isinstance(source_id, int)
            or source_id < 0
            or not isinstance(filename, str)
            or Path(filename).name != filename
            or Path(filename).suffix != ".png"
        ):
            raise ValueError(f"Frame contract entry is invalid: {frame!r}")
        try:
            filename_id = int(Path(filename).stem)
        except ValueError as exc:
            raise ValueError(
                f"Frame contract filename must have a numeric stem: {filename!r}"
            ) from exc
        if filename_id != source_id:
            raise ValueError(
                f"Frame contract source ID does not match {filename!r}: {source_id}"
            )
        actual_output_ids.append(output_id)
        source_ids.append(source_id)
        filenames.append(filename)
    if actual_output_ids != expected_output_ids:
        raise ValueError("Frame contract output IDs must be contiguous and ordered")
    if source_ids != sorted(source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("Frame contract source IDs must be unique and sorted")
    if filenames != sorted(filenames) or len(set(filenames)) != len(filenames):
        raise ValueError("Frame contract filenames must be unique and sorted")
    source_hashes = value.get("source_artifact_sha256")
    if (
        not isinstance(source_hashes, Mapping)
        or set(source_hashes) != {MATCH_ROBOT_EE_POSES}
        or not isinstance(source_hashes.get(MATCH_ROBOT_EE_POSES), str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(source_hashes[MATCH_ROBOT_EE_POSES]),
        )
        is None
    ):
        raise ValueError(
            "Frame contract must bind the exact matched robot-pose artifact SHA-256"
        )
    return dict(value)


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Hash-bound artifact is missing: {path}")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_source_artifact_hash(
    sensor_folder: Path,
    source_artifact_sha256: Mapping[str, str],
) -> None:
    expected = source_artifact_sha256[MATCH_ROBOT_EE_POSES]
    current = _sha256_file(sensor_folder / MATCH_ROBOT_EE_POSES)
    if current != expected:
        raise ValueError(
            "Matched robot-pose artifact changed after BlenderProc preparation: "
            f"{sensor_folder / MATCH_ROBOT_EE_POSES}"
        )


def validate_prepared_folder(
    sensor_folder: Path,
    subdir: str,
    *,
    annotation_mode: str | None = None,
) -> tuple[Path, int, dict[str, Any]]:
    validate_subdir(subdir)
    blenderproc_folder = sensor_folder / subdir
    required_files = [
        blenderproc_folder / "objects.json",
        blenderproc_folder / "camera_matrix.npy",
        blenderproc_folder / "camera_poses.npy",
        blenderproc_folder / FRAME_CONTRACT,
    ]
    missing = [path for path in required_files if not path.is_file()]
    if not (blenderproc_folder / "objects").is_dir():
        missing.append(blenderproc_folder / "objects")
    if missing:
        raise FileNotFoundError(
            f"Prepared BlenderProc folder for {sensor_folder.name} is missing: "
            + ", ".join(path.as_posix() for path in missing)
        )
    try:
        camera_matrix = np.load(blenderproc_folder / "camera_matrix.npy")
        camera_poses = np.load(blenderproc_folder / "camera_poses.npy")
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Invalid prepared arrays in {blenderproc_folder}: {exc}"
        ) from exc
    if camera_matrix.shape != (3, 3) or not np.all(np.isfinite(camera_matrix)):
        raise ValueError(
            f"camera_matrix.npy must be a finite 3x3 array: {blenderproc_folder}"
        )
    if (
        camera_poses.ndim != 3
        or camera_poses.shape[0] < 1
        or camera_poses.shape[1:] != (4, 4)
        or not np.all(np.isfinite(camera_poses))
    ):
        raise ValueError(
            f"camera_poses.npy must be a non-empty finite Nx4x4 array: {blenderproc_folder}"
        )
    contract = _frame_contract(blenderproc_folder / FRAME_CONTRACT)
    _validate_source_artifact_hash(
        sensor_folder,
        contract["source_artifact_sha256"],
    )
    if len(contract["frames"]) != int(camera_poses.shape[0]):
        raise ValueError(
            "Frame contract count does not match camera_poses.npy: "
            f"{blenderproc_folder}"
        )
    prepared_mode = str(contract["annotation_mode"])
    if annotation_mode is not None and prepared_mode != annotation_mode:
        raise ValueError(
            "Prepared BlenderProc annotation mode does not match the render request: "
            f"prepared={prepared_mode!r}, requested={annotation_mode!r}"
        )
    objects = _read_json_mapping(blenderproc_folder / "objects.json")
    if objects.get(
        "schema_version"
    ) != "blenderproc_object_instances.v1" or not isinstance(
        objects.get("instances"), list
    ):
        raise ValueError(
            f"Prepared BlenderProc objects.json is unsupported: {blenderproc_folder}"
        )
    return blenderproc_folder, int(camera_poses.shape[0]), contract


def discover_render_jobs(
    *,
    input_folder: str | Path,
    render_script: str | Path,
    subdir: str,
    blenderproc_executable: str,
    annotation_mode: str,
    sensor_names: Sequence[str] | None = None,
) -> list[RenderJob]:
    input_path = Path(input_folder)
    script_path = Path(render_script)
    validate_subdir(subdir)
    validate_annotation_mode(annotation_mode)
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input folder not found: {input_path}")
    if not script_path.is_file():
        raise FileNotFoundError(f"BlenderProc render script not found: {script_path}")
    if not blenderproc_executable.strip():
        raise ValueError("BlenderProc executable cannot be empty")
    selected_names = set(sensor_names) if sensor_names is not None else None
    jobs = []
    for sensor_folder in sorted(input_path.iterdir()):
        if not sensor_folder.is_dir() or sensor_folder.name.startswith("."):
            continue
        if selected_names is not None and sensor_folder.name not in selected_names:
            continue
        prepared, frame_count, contract = validate_prepared_folder(
            sensor_folder,
            subdir,
            annotation_mode=annotation_mode,
        )
        camera_poses = prepared / "camera_poses.npy"
        camera_matrix = prepared / "camera_matrix.npy"
        frame_contract = prepared / FRAME_CONTRACT
        prepared_mode = str(contract["annotation_mode"])
        resolution = contract["resolution"]
        render_script_sha256 = _sha256_file(script_path)
        jobs.append(
            RenderJob(
                sensor_name=sensor_folder.name,
                sensor_folder=sensor_folder.as_posix(),
                blenderproc_folder=prepared.as_posix(),
                camera_poses=camera_poses.as_posix(),
                camera_matrix=camera_matrix.as_posix(),
                frame_contract=frame_contract.as_posix(),
                annotation_mode=prepared_mode,
                frame_bindings=tuple(dict(frame) for frame in contract["frames"]),
                source_artifact_sha256=dict(contract["source_artifact_sha256"]),
                analytic_implementation_revision=(ANALYTIC_IMPLEMENTATION_REVISION),
                render_script_sha256=render_script_sha256,
                resolution=(int(resolution["width"]), int(resolution["height"])),
                expected_frame_count=frame_count,
                command=[
                    blenderproc_executable,
                    "run",
                    script_path.as_posix(),
                    camera_poses.as_posix(),
                    camera_matrix.as_posix(),
                    prepared.as_posix(),
                    "--annotation-mode",
                    prepared_mode,
                ],
            )
        )
    if not jobs:
        raise FileNotFoundError(
            f"No prepared BlenderProc sensor folders in {input_path}"
        )
    return jobs


def write_render_plan(
    run_root: str | Path,
    jobs: list[RenderJob],
    *,
    dry_run: bool,
    skipped: bool = False,
    skip_reason: str | None = None,
) -> Path:
    return atomic_write_json(
        Path(run_root) / BLENDERPROC_RENDER_PLAN,
        {
            "schema_version": "blenderproc_render_plan.v1",
            "dry_run": dry_run,
            "skipped": skipped,
            "skip_reason": skip_reason,
            "jobs": [asdict(job) for job in jobs],
        },
    )


def _read_json_mapping(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing BlenderProc render artifact: {path}")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON render artifact {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"Render artifact must be a JSON object: {path}")
    return value


def validate_render_output(
    output: str | Path,
    *,
    expected_frame_count: int,
    annotation_mode: str,
    frame_bindings: Sequence[Mapping[str, int | str]],
    source_artifact_sha256: Mapping[str, str],
    analytic_implementation_revision: str,
    render_script_sha256: str,
    resolution: tuple[int, int],
) -> None:
    """Validate analytic GT evidence and reject renderer-owned image artifacts."""

    validate_annotation_mode(annotation_mode)
    scene = Path(output)
    for forbidden_name in (
        RGB_DIR,
        DEPTH_DIR,
        "mask",
        "mask_visib",
        "scene_gt_info.json",
    ):
        if (scene / forbidden_name).exists():
            raise ValueError(
                "BlenderProc analytic GT output must not contain renderer-owned "
                f"RGB, depth, masks, or GT-info: {scene / forbidden_name}"
            )
    scene_gt = _read_json_mapping(scene / "scene_gt.json")
    expected_json_ids = {str(index) for index in range(expected_frame_count)}
    if set(scene_gt) != expected_json_ids:
        raise ValueError("scene_gt.json keys do not match camera pose count")

    prepared_instances = scene.parent.parent / "objects.json"
    prepared = _read_json_mapping(prepared_instances)
    instances = prepared.get("instances")
    if not isinstance(instances, list):
        raise ValueError("Prepared BlenderProc instance list is invalid")
    for image_id in range(expected_frame_count):
        annotations = scene_gt[str(image_id)]
        if not isinstance(annotations, list) or len(annotations) != len(instances):
            raise ValueError(
                f"scene_gt.json frame {image_id} does not match prepared instances"
            )
        for annotation_index, (annotation, instance) in enumerate(
            zip(annotations, instances, strict=True)
        ):
            if not isinstance(annotation, Mapping) or int(
                annotation.get("obj_id", -1)
            ) != int(instance["obj_id"]):
                raise ValueError(
                    "scene_gt annotation identity does not match loaded instance "
                    f"order at frame {image_id}, index {annotation_index}"
                )
            for key, length in (("cam_R_m2c", 9), ("cam_t_m2c", 3)):
                values = annotation.get(key)
                if (
                    not isinstance(values, list)
                    or len(values) != length
                    or not np.all(np.isfinite(np.asarray(values, dtype=float)))
                ):
                    raise ValueError(
                        f"scene_gt annotation {key} must contain {length} finite values"
                    )
            rotation = np.asarray(annotation["cam_R_m2c"], dtype=float).reshape(3, 3)
            if not np.allclose(
                rotation.T @ rotation, np.eye(3), atol=1e-6
            ) or not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-6):
                raise ValueError(
                    "scene_gt annotation rotation must be a proper orthonormal "
                    f"matrix at frame {image_id}, index {annotation_index}"
                )

    instance_sidecar = scene / "posetestbot_render_instances.json"
    rendered = _read_json_mapping(instance_sidecar)
    if rendered.get("schema_version") != "posetestbot_render_instances.v1":
        raise ValueError("Rendered instance identity sidecar has the wrong schema")
    if rendered.get("instances") != instances:
        raise ValueError("Rendered instance identity does not match prepared objects")
    if rendered.get("blenderproc_version") != "2.8.0":
        raise ValueError(
            "Rendered pose-template GT was not produced by BlenderProc 2.8.0"
        )
    if (
        rendered.get("identity_contract")
        != "bop_gt_index_matches_loaded_instance_order.v1"
    ):
        raise ValueError(
            "Rendered instance identity contract is missing or unsupported"
        )
    frames = rendered.get("frames")
    if not isinstance(frames, Mapping) or set(frames) != expected_json_ids:
        raise ValueError("Rendered instance identity does not cover every output frame")
    if rendered.get("annotation_mode") != annotation_mode:
        raise ValueError("Rendered instance identity annotation mode does not match")
    if rendered.get("frame_bindings") != [dict(item) for item in frame_bindings]:
        raise ValueError("Rendered instance identity frame bindings do not match")
    for image_id in range(expected_frame_count):
        identities = frames[str(image_id)]
        if not isinstance(identities, list) or len(identities) != len(instances):
            raise ValueError(
                f"Rendered instance identity count is invalid at frame {image_id}"
            )
        for gt_id, (identity, instance) in enumerate(
            zip(identities, instances, strict=True)
        ):
            if (
                not isinstance(identity, Mapping)
                or identity.get("gt_id") != gt_id
                or identity.get("obj_id") != instance.get("obj_id")
                or identity.get("instance_uuid") != instance.get("instance_uuid")
                or identity.get("catalog_uuid") != instance.get("catalog_uuid")
            ):
                raise ValueError(
                    "Rendered instance identity does not match loaded instance "
                    f"order at frame {image_id}, index {gt_id}"
                )

    provenance = _read_json_mapping(scene / "posetestbot_gt_provenance.json")
    if provenance.get("schema_version") != "posetestbot_gt_provenance.v1":
        raise ValueError("Rendered GT provenance sidecar has the wrong schema")
    if provenance.get("blenderproc_version") != "2.8.0":
        raise ValueError("Rendered GT provenance does not bind BlenderProc 2.8.0")
    if provenance.get("annotation_mode") != annotation_mode:
        raise ValueError("Rendered GT provenance annotation mode does not match")
    if provenance.get("frame_bindings") != [dict(item) for item in frame_bindings]:
        raise ValueError("Rendered GT provenance frame bindings do not match")
    if provenance.get("source_artifact_sha256") != dict(source_artifact_sha256):
        raise ValueError(
            "Rendered GT provenance matched-pose artifact hash does not match"
        )
    if provenance.get("analytic_implementation") != {
        "revision": analytic_implementation_revision,
        "script_sha256": render_script_sha256,
    }:
        raise ValueError(
            "Rendered GT provenance analytic implementation does not match"
        )
    if provenance.get("resolution") != {
        "width": resolution[0],
        "height": resolution[1],
    }:
        raise ValueError("Rendered GT provenance resolution does not match")
    if (
        provenance.get("pose_contract")
        != "analytic_model_to_opencv_camera_rigid_transform.v1"
    ):
        raise ValueError("Rendered GT provenance pose contract is unsupported")


def _workspace_command(job: RenderJob, workspace: Path) -> list[str]:
    return [
        *job.command[:2],
        (workspace / WORKSPACE_RENDER_SCRIPT).as_posix(),
        (workspace / "camera_poses.npy").as_posix(),
        (workspace / "camera_matrix.npy").as_posix(),
        workspace.as_posix(),
        "--annotation-mode",
        job.annotation_mode,
    ]


def run_render_jobs(
    jobs: Sequence[RenderJob],
    *,
    command_runner: Callable[..., object] = subprocess.run,
) -> dict[str, Path]:
    """Render all sensors in workspaces and atomically promote every result."""

    promotions: list[tuple[Path, Path]] = []
    workspaces: list[Path] = []
    artifacts: dict[str, Path] = {}
    try:
        for job in jobs:
            sensor_folder = Path(job.sensor_folder)
            prepared = Path(job.blenderproc_folder)
            workspace = sensor_folder / f".blenderproc-render.{uuid.uuid4().hex}.work"
            workspaces.append(workspace)
            shutil.copytree(prepared, workspace)
            workspace_script = workspace / WORKSPACE_RENDER_SCRIPT
            shutil.copy2(Path(job.command[2]), workspace_script)
            if _sha256_file(workspace_script) != job.render_script_sha256:
                raise ValueError(
                    "BlenderProc analytic implementation changed before launch"
                )
            _validate_source_artifact_hash(
                sensor_folder,
                job.source_artifact_sha256,
            )
            command_runner(_workspace_command(job, workspace), check=True)
            if _sha256_file(workspace_script) != job.render_script_sha256:
                raise ValueError(
                    "BlenderProc analytic implementation changed during execution"
                )
            _validate_source_artifact_hash(
                sensor_folder,
                job.source_artifact_sha256,
            )
            scene = workspace / "train_pbr" / "000000"
            validate_render_output(
                scene,
                expected_frame_count=job.expected_frame_count,
                annotation_mode=job.annotation_mode,
                frame_bindings=job.frame_bindings,
                source_artifact_sha256=job.source_artifact_sha256,
                analytic_implementation_revision=(job.analytic_implementation_revision),
                render_script_sha256=job.render_script_sha256,
                resolution=job.resolution,
            )

            output_staging = prepared / f".output.{uuid.uuid4().hex}.staging"
            shutil.move(scene.as_posix(), output_staging.as_posix())
            mask_staging = sensor_folder / f".{MASKS_DIR}.{uuid.uuid4().hex}.staging"
            mask_staging.mkdir()
            mask_destination = sensor_folder / MASKS_DIR
            output_destination = prepared / "output"
            promotions.extend(
                [
                    (mask_staging, mask_destination),
                    (output_staging, output_destination),
                ]
            )
            artifacts[f"{job.sensor_name}:blenderproc_output"] = output_destination
        replace_directories(promotions)
        for job in jobs:
            mask_destination = Path(job.sensor_folder) / MASKS_DIR
            try:
                mask_destination.rmdir()
            except OSError:
                # The transactional replacement has already removed every stale
                # mask. If another worker populated the new directory, never
                # delete its files or report a false render rollback.
                pass
    except Exception:
        for staging, _destination in promotions:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        for workspace in workspaces:
            shutil.rmtree(workspace, ignore_errors=True)
    return artifacts
