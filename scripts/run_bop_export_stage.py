#!/usr/bin/env python3
"""Export synchronized sensor folders into a minimal BOP scene layout."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import cv2

from posetestbot.bop.mask_driver import (
    GENERATION_REPORT,
    run_official_bop_mask_generation,
)
from posetestbot.bop.writer import (
    ANNOTATION_MODES,
    ANNOTATION_SOURCES,
    copy_bop_instance_models,
    export_sensor_scene_to_bop,
    finalize_official_scene_annotations,
    resolve_annotation_mode,
    targets_filename,
    validate_bop_dataset,
    write_bop_coco_annotations,
    write_bop_dataset_info,
    write_bop_export_manifest,
    write_bop_frame_map,
    write_bop_instance_map,
    write_bop_pose_template,
    write_bop_targets,
)
from posetestbot.calibration.profiles import (
    CalibrationProfile,
    load_profile_collection,
    select_valid_profile_for_sensor,
)
from posetestbot.calibration.rectification import (
    RECTIFIED_DIR,
)
from posetestbot.calibration.static_reuse import (
    verify_static_profile_destination_reference,
)
from posetestbot.io.atomic import replace_directory
from posetestbot.io.artifacts import (
    BOP_DIR,
    BOP_COCO_ANNOTATIONS,
    BOP_EXPORT_MANIFEST,
    BOP_FRAME_MAP_JSON,
    BOP_INSTANCE_MAP,
    BOP_POSE_TEMPLATE,
    BOP_TARGETS_BOP19,
    CALIBRATION_PROFILES,
    CALIBRATION_PROFILE_SELECTION,
    DEPTH_DIR,
    MATCH_ROBOT_EE_POSES,
    MODELS_DIR,
    MODELS_EVAL_DIR,
    OBJECT_INSTANCES,
    PROCESSED_DIR,
    RGB_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    utc_now_iso,
    write_run_manifest,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.pipeline.sensor_selection import (
    enabled_sensor_mounting_modes_by_folder,
    filter_enabled_sensor_folders,
)
from posetestbot.pose_templates.selection import (
    load_pose_template_selection,
    prepare_object_instances,
)
from posetestbot.sensors.contracts import MountingMode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy synchronized RGB/depth frames into BOP scene folders and "
            "record the export in dataset_manifest.json."
        )
    )
    parser.add_argument("run_root", help="Run root containing processed/synchronized.")
    parser.add_argument(
        "--input-folder",
        default=None,
        help="Synchronized sensor folder root. Defaults to <run_root>/processed/synchronized.",
    )
    parser.add_argument(
        "--output-folder",
        default=None,
        help="BOP export root. Defaults to <run_root>/bop.",
    )
    parser.add_argument("--split", default="test", help="BOP split folder name.")
    parser.add_argument(
        "--objectless",
        action="store_true",
        help="Export only RGB-D and camera metadata, without object artifacts.",
    )
    parser.add_argument(
        "--no-model-export",
        action="store_true",
        help="Skip copying object models and generating target files.",
    )
    parser.add_argument(
        "--write-coco-annotations",
        action="store_true",
        help=(
            "Also write posetestbot_coco_annotations.json, a COCO-style "
            "annotation file derived from exported BOP scene GT, GT info, RGB "
            "files, and masks."
        ),
    )
    parser.add_argument(
        "--annotation-source",
        choices=sorted(ANNOTATION_SOURCES),
        default="none",
        help=(
            "Source for BOP scene GT. The acquisition-first default 'none' "
            "omits GT-derived files rather than writing placeholders; "
            "pose-template object targets remain available for inference. Use "
            "'blenderproc' only after optional GT pose generation has completed."
        ),
    )
    parser.add_argument(
        "--annotation-mode",
        choices=sorted(ANNOTATION_MODES),
        required=True,
        help=(
            "Explicit GT capability to publish: 'none', BlenderProc-derived "
            "'pose', or 'pose_and_masks'."
        ),
    )
    parser.add_argument(
        "--scene-start",
        type=int,
        default=1,
        help="Scene ID assigned to the first exported sensor folder.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing exported scene folders.",
    )
    parser.add_argument(
        "--calibration-profiles",
        default=None,
        help=(
            "Optional calibration.v2 profile collection. Matching profiles are "
            "recorded in scene_camera.json and bop_export_manifest.json."
        ),
    )
    return parser.parse_args()


def default_input_folder(run_root: Path, explicit_input_folder: str | None) -> Path:
    if explicit_input_folder:
        return Path(explicit_input_folder)
    rectified = run_root / PROCESSED_DIR / RECTIFIED_DIR
    if rectified.is_dir():
        return rectified
    return run_root / PROCESSED_DIR / SYNCHRONIZED_DIR


def default_output_folder(run_root: Path, explicit_output_folder: str | None) -> Path:
    if explicit_output_folder:
        return Path(explicit_output_folder)
    return run_root / BOP_DIR


def _run_input_path(run_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else run_root / path


def _selected_calibration_configured(run_root: Path, run_config: dict | None) -> bool:
    return (run_config or {}).get("calibration_profile_selection") is not None or (
        run_root / CALIBRATION_PROFILE_SELECTION
    ).exists()


def calibration_profile_for_sensor(
    profiles: list[CalibrationProfile],
    sensor_name: str,
    *,
    profile_ids_by_sensor_name: Mapping[str, str] | None = None,
    mounting_modes_by_sensor_name: Mapping[str, MountingMode] | None = None,
) -> CalibrationProfile:
    """Resolve a BOP camera profile, honoring managed selection when present."""

    profile_id = None
    if profile_ids_by_sensor_name is not None:
        try:
            profile_id = profile_ids_by_sensor_name[sensor_name]
        except KeyError as exc:
            raise KeyError(
                f"Calibration selection has no profile for {sensor_name!r}"
            ) from exc
    mounting_mode = None
    if mounting_modes_by_sensor_name is not None:
        try:
            mounting_mode = mounting_modes_by_sensor_name[sensor_name]
        except KeyError as exc:
            raise KeyError(
                f"Run configuration has no mounting mode for {sensor_name!r}"
            ) from exc
    return select_valid_profile_for_sensor(
        profiles,
        sensor_name,
        mounting_mode=mounting_mode,
        profile_id=profile_id,
    )


def _portable_run_path(path: Path, run_root: Path) -> str | None:
    """Return a portable run-relative path, never an absolute host path."""

    try:
        return path.resolve().relative_to(run_root.resolve()).as_posix()
    except ValueError:
        return None


def discover_exportable_sensor_folders(
    input_folder: Path,
    *,
    run_root: Path | None = None,
) -> list[Path]:
    if not input_folder.is_dir():
        raise FileNotFoundError(f"Synchronized input folder not found: {input_folder}")
    sensors = [
        child
        for child in sorted(input_folder.iterdir())
        if child.is_dir()
        and (child / RGB_DIR).is_dir()
        and (child / DEPTH_DIR).is_dir()
    ]
    if run_root is not None:
        sensors = filter_enabled_sensor_folders(run_root, sensors)
    if not sensors:
        raise FileNotFoundError(
            f"No synchronized RGB-D sensor folders in {input_folder}"
        )
    return sensors


def _uniform_export_image_size(
    output_root: Path,
    exports: list[Any],
) -> tuple[int, int]:
    sizes: set[tuple[int, int]] = set()
    for export in exports:
        rgb_folder = output_root / export.scene_folder / RGB_DIR
        first_rgb = next(iter(sorted(rgb_folder.glob("*.png"))), None)
        image = (
            cv2.imread(first_rgb.as_posix(), cv2.IMREAD_UNCHANGED)
            if first_rgb is not None
            else None
        )
        if image is None:
            raise ValueError(
                f"Cannot determine BOP scene image size: {export.scene_folder}"
            )
        height, width = image.shape[:2]
        sizes.add((int(width), int(height)))
    if len(sizes) != 1:
        raise ValueError(
            "Official BOP mask generation requires one resolution across all scenes"
        )
    return next(iter(sizes))


def complete_official_mask_annotations(
    output_root: Path,
    exports: list[Any],
    object_models: list[Any],
    *,
    split: str,
    mask_runner: Callable[..., Mapping[str, object]] = (
        run_official_bop_mask_generation
    ),
) -> tuple[list[Any], dict[str, object]]:
    """Complete staged pose GT through the injectable official-toolkit boundary."""

    report = dict(
        mask_runner(
            output_root,
            split=split,
            scene_ids=[export.scene_id for export in exports],
            object_ids=[model.obj_id for model in object_models],
            image_size=_uniform_export_image_size(output_root, exports),
            app_root=Path(__file__).resolve().parents[1],
        )
    )
    return finalize_official_scene_annotations(output_root, exports), report


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    input_folder = default_input_folder(run_root, args.input_folder)
    output_folder = default_output_folder(run_root, args.output_folder)
    calibration_profiles_path = (
        _run_input_path(run_root, args.calibration_profiles)
        if args.calibration_profiles
        else None
    )

    manifest = load_or_create_run_manifest(run_root)
    upsert_stage(manifest, name="bop_export", status="running")
    write_run_manifest(manifest, run_root)

    staging_folder = output_folder.with_name(
        f".{output_folder.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        annotation_mode = resolve_annotation_mode(
            args.annotation_source,
            args.annotation_mode,
        )
        if annotation_mode == "none" and args.write_coco_annotations:
            raise ValueError("COCO annotations require --annotation-source blenderproc")
        if args.write_coco_annotations and annotation_mode != "pose_and_masks":
            raise ValueError(
                "COCO annotations require --annotation-mode pose_and_masks"
            )
        run_config = load_run_config_for_run_root(run_root)
        mounting_modes_by_sensor_name = enabled_sensor_mounting_modes_by_folder(
            run_config
        )
        calibration_profile_ids_by_sensor_name = None
        if _selected_calibration_configured(run_root, run_config):
            if calibration_profiles_path is None:
                raise ValueError(
                    "A run with selected calibration provenance must pass its "
                    "calibration_profiles snapshot to BOP export"
                )
            from posetestbot.calibration.profile_library import (
                selected_calibration_profile_ids_by_sensor_folder,
                verify_calibration_profile_selection,
            )

            calibration_selection = verify_calibration_profile_selection(
                run_root,
                expected_calibration_profiles=calibration_profiles_path,
            )
            calibration_profile_ids_by_sensor_name = (
                selected_calibration_profile_ids_by_sensor_folder(
                    run_root,
                    selection=calibration_selection,
                )
            )
            from posetestbot.sync.calibration_policy import (
                resolve_calibration_profile_sync_policy,
            )
            from posetestbot.sync.quality import (
                verify_profile_bound_sync_evidence,
            )

            calibration_sync_policy = resolve_calibration_profile_sync_policy(run_root)
            if calibration_sync_policy is None:
                raise ValueError(
                    "Selected calibration is not bound to a synchronization policy"
                )
            verify_profile_bound_sync_evidence(
                run_root,
                calibration_sync_policy,
            )
        if output_folder.exists() and not args.overwrite:
            raise FileExistsError(
                f"BOP dataset already exists: {output_folder}; pass --overwrite"
            )
        sensor_folders = discover_exportable_sensor_folders(
            input_folder,
            run_root=run_root if args.input_folder is None else None,
        )
        calibration_profiles = (
            load_profile_collection(calibration_profiles_path)
            if calibration_profiles_path is not None
            else []
        )
        if calibration_profiles_path is not None and not calibration_profiles:
            raise ValueError("Calibration profile collection must not be empty")
        calibration_profiles_by_sensor_name = (
            {
                sensor_folder.name: calibration_profile_for_sensor(
                    calibration_profiles,
                    sensor_folder.name,
                    profile_ids_by_sensor_name=(calibration_profile_ids_by_sensor_name),
                    mounting_modes_by_sensor_name=mounting_modes_by_sensor_name,
                )
                for sensor_folder in sensor_folders
            }
            if calibration_profiles_path is not None
            else {}
        )
        verify_static_profile_destination_reference(
            run_root,
            run_config,
            calibration_profiles_by_sensor_name.values(),
            matched_robot_pose_paths_by_sensor_name={
                sensor_folder.name: sensor_folder / MATCH_ROBOT_EE_POSES
                for sensor_folder in sensor_folders
            },
        )

        staging_folder.parent.mkdir(parents=True, exist_ok=True)
        staging_folder.mkdir(parents=False, exist_ok=False)
        dataset_mode = (
            str(run_config.get("dataset_mode"))
            if run_config is not None
            else "objectless"
        )
        if args.objectless:
            dataset_mode = "objectless"
        template_mode = dataset_mode == "pose_template" and not args.objectless
        objectless_mode = dataset_mode == "objectless"
        selection = load_pose_template_selection(run_root) if template_mode else None
        object_instances = prepare_object_instances(run_root) if template_mode else None
        object_name_to_id = None
        object_models = []
        if objectless_mode:
            object_name_to_id = {}
        if not args.no_model_export:
            object_name_to_id = (
                {
                    str(item["instance_uuid"]): int(item["obj_id"])
                    for item in object_instances["instances"]
                }
                if object_instances is not None
                else {}
            )
            geometry_cache = None
            previous_models_info = output_folder / MODELS_DIR / "models_info.json"
            if previous_models_info.is_file():
                try:
                    loaded_cache = json.loads(previous_models_info.read_text())
                except json.JSONDecodeError:
                    loaded_cache = None
                if isinstance(loaded_cache, dict):
                    geometry_cache = loaded_cache
            if object_instances is not None:
                object_models = copy_bop_instance_models(
                    staging_folder,
                    run_root,
                    object_instances,
                    geometry_cache=geometry_cache,
                )
        exports = []
        for offset, sensor_folder in enumerate(sensor_folders):
            calibration_profile = calibration_profiles_by_sensor_name.get(
                sensor_folder.name
            )
            portable_sensor_folder = (
                _portable_run_path(sensor_folder, run_root) or sensor_folder.name
            )
            exports.append(
                export_sensor_scene_to_bop(
                    sensor_folder,
                    staging_folder,
                    split=args.split,
                    scene_id=args.scene_start + offset,
                    overwrite=False,
                    calibration_profile=calibration_profile,
                    object_name_to_id=object_name_to_id,
                    template_instances=(
                        object_instances["instances"]
                        if object_instances is not None
                        else None
                    ),
                    input_sensor_folder=portable_sensor_folder,
                    authoritative_source_sensor_folder=portable_sensor_folder,
                    annotation_source=args.annotation_source,
                    annotation_mode=annotation_mode,
                )
            )

        pose_generation_provenance = {
            "source": "blenderproc_analytic_gt",
            "scenes": [
                {
                    "scene_id": export.scene_id,
                    "sensor_name": export.sensor_name,
                    **export.annotation_provenance,
                }
                for export in exports
                if export.annotation_source == "blenderproc"
            ],
        }
        annotation_provenance: dict[str, object] = {}
        if annotation_mode == "pose":
            annotation_provenance = {
                "schema_version": "posetestbot_bop_gt_generation.v1",
                "annotation_mode": "pose",
                "pose_generation": pose_generation_provenance,
                "mask_generation": {"state": "absent"},
            }
        elif annotation_mode == "pose_and_masks":
            if not object_models:
                raise ValueError(
                    "Official BOP mask generation requires exported object models"
                )
            exports, mask_generation_provenance = complete_official_mask_annotations(
                staging_folder,
                exports,
                object_models,
                split=args.split,
            )
            annotation_provenance = {
                "schema_version": "posetestbot_bop_gt_generation.v1",
                "annotation_mode": "pose_and_masks",
                "pose_generation": pose_generation_provenance,
                "mask_generation": mask_generation_provenance,
            }

        targets_path = None
        coco_annotations_path = None
        if (
            args.split == "test"
            and not args.no_model_export
            and any(export.targets for export in exports)
        ):
            targets_path = write_bop_targets(staging_folder, exports, split=args.split)
        if args.write_coco_annotations:
            coco_annotations_path = write_bop_coco_annotations(
                staging_folder,
                exports,
                split=args.split,
                object_models=object_models,
            )

        frame_map_path = write_bop_frame_map(staging_folder, exports)
        instance_map_path = (
            write_bop_instance_map(staging_folder, exports)
            if template_mode and args.annotation_source == "blenderproc"
            else None
        )
        pose_template_path = (
            write_bop_pose_template(staging_folder, selection)
            if selection is not None
            else None
        )
        dataset_info_path = write_bop_dataset_info(
            staging_folder,
            exports,
            dataset_name=run_root.name,
            generated_at=utc_now_iso(),
        )
        validation = validate_bop_dataset(
            staging_folder,
            exports,
            object_models=object_models,
            targets_path=targets_path,
        )
        exported_profile_ids = {
            export.calibration_profile_id
            for export in exports
            if export.calibration_profile_id is not None
        }
        exported_calibration_profiles = [
            profile
            for profile in calibration_profiles
            if profile.profile_id in exported_profile_ids
        ]
        write_bop_export_manifest(
            staging_folder,
            exports,
            calibration_profiles_path=(
                _portable_run_path(calibration_profiles_path, run_root)
                if calibration_profiles_path is not None
                else None
            ),
            calibration_profiles=exported_calibration_profiles,
            object_models=object_models,
            targets_path=targets_path,
            coco_annotations_path=coco_annotations_path,
            frame_map_path=frame_map_path,
            dataset_info_path=dataset_info_path,
            validation=validation,
            stable_id_mapping=(
                {
                    str(item["catalog_uuid"]): int(item["obj_id"])
                    for item in object_instances["instances"]
                }
                if object_instances is not None
                else {}
            ),
            dataset_mode=dataset_mode,
            pose_template_provenance=(
                {
                    "template_uuid": selection["template_uuid"],
                    "bundle_sha256": selection["bundle_sha256"],
                    "configuration_sha256": selection["configuration_sha256"],
                    "instance_count": len(selection["instances"]),
                }
                if selection is not None
                else None
            ),
            instance_map_path=instance_map_path,
            pose_template_path=pose_template_path,
            annotation_source=args.annotation_source,
            annotation_mode=annotation_mode,
            annotation_provenance=annotation_provenance,
        )
        replace_directory(staging_folder, output_folder)

        artifacts: dict[str, Path] = {
            BOP_DIR: output_folder,
            BOP_EXPORT_MANIFEST: output_folder / BOP_EXPORT_MANIFEST,
            BOP_FRAME_MAP_JSON: output_folder / BOP_FRAME_MAP_JSON,
            "dataset_info.json": output_folder / "dataset_info.json",
        }
        if object_models:
            artifacts[MODELS_DIR] = output_folder / MODELS_DIR
            artifacts[MODELS_EVAL_DIR] = output_folder / MODELS_EVAL_DIR
        if object_instances is not None:
            artifacts[OBJECT_INSTANCES] = run_root / OBJECT_INSTANCES
            artifacts[BOP_POSE_TEMPLATE] = output_folder / BOP_POSE_TEMPLATE
        if instance_map_path is not None:
            artifacts[BOP_INSTANCE_MAP] = output_folder / BOP_INSTANCE_MAP
        if annotation_mode == "pose_and_masks":
            artifacts[GENERATION_REPORT] = output_folder / GENERATION_REPORT
        if calibration_profiles_path is not None:
            artifacts[CALIBRATION_PROFILES] = calibration_profiles_path
        for export in exports:
            artifacts[f"{export.sensor_name}:bop_scene"] = (
                output_folder / export.scene_folder
            )
        if targets_path is not None:
            artifacts[targets_filename(args.split)] = output_folder / targets_filename(
                args.split
            )
            artifacts[BOP_TARGETS_BOP19] = output_folder / BOP_TARGETS_BOP19
        if coco_annotations_path is not None:
            artifacts[BOP_COCO_ANNOTATIONS] = output_folder / BOP_COCO_ANNOTATIONS
        message = f"Exported {len(exports)} synchronized sensor folder(s) to BOP."
        if validation["capabilities"]["bop19_evaluation"]:
            message += (
                " GT poses, official full/visible masks, visibility info, and "
                "BOP19 evaluation targets are complete."
            )
        elif validation["capabilities"]["gt_poses"]:
            message += (
                " GT poses are complete; masks and BOP19 visibility evidence "
                "were intentionally omitted."
            )
        elif validation["capabilities"]["pose_estimation_input"]:
            message += (
                " RGB-D scenes, models, and populated targets are "
                "pose-estimation inputs; rendered GT and masks are not present."
            )
        upsert_stage(
            manifest,
            name="bop_export",
            status="succeeded",
            artifacts=artifacts,
            run_root=run_root,
            message=message,
        )
        write_run_manifest(manifest, run_root)
    except Exception as exc:
        if staging_folder.exists():
            shutil.rmtree(staging_folder)
        upsert_stage(manifest, name="bop_export", status="failed", message=str(exc))
        write_run_manifest(manifest, run_root)
        raise

    print(message)


if __name__ == "__main__":
    main()
