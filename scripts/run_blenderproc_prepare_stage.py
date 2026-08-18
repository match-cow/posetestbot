#!/usr/bin/env python3
"""Prepare manifest-tracked inputs for optional BlenderProc annotation."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path

from posetestbot.blenderproc.preparation import (
    load_camera_transformations,
    prepare_sensor_folders,
    write_camera_transformations,
)
from posetestbot.calibration.profiles import (
    blenderproc_camera_transform_map_from_profiles,
    load_profile_collection,
)
from posetestbot.calibration.static_reuse import (
    verify_static_profile_destination_reference,
)
from posetestbot.io.artifacts import (
    CALIBRATION_DIR,
    CALIBRATION_PROFILES,
    CALIBRATION_PROFILE_SELECTION,
    DERIVED_CAMERA_EE_TRANSFORM,
    MATCH_ROBOT_EE_POSES,
    OBJECT_INSTANCES,
    PROCESSED_DIR,
    SYNCHRONIZED_DIR,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.run_config import load_run_config_for_run_root
from posetestbot.pipeline.sensor_selection import (
    enabled_sensor_folder_names,
    enabled_sensor_mounting_modes_by_folder,
)
from posetestbot.pose_templates.selection import prepare_object_instances
from posetestbot.sensors.contracts import MountingMode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare BlenderProc inputs for derived synchronized sensor folders "
            "and record the stage in dataset_manifest.json."
        )
    )
    parser.add_argument(
        "run_root",
        help="Run root containing processed/synchronized sensor folders.",
    )
    parser.add_argument(
        "--input-folder",
        default=None,
        help="Folder containing synchronized sensor folders. Defaults to <run_root>/processed/synchronized.",
    )
    parser.add_argument(
        "--objectless",
        action="store_true",
        help="Prepare camera inputs with explicit empty object metadata.",
    )
    parser.add_argument(
        "--camera-transformations",
        default="scripts/default_data/camera_ee_transform.json",
        help="Camera transform JSON used by BlenderProc preparation.",
    )
    parser.add_argument(
        "--calibration-profiles",
        default=None,
        help=(
            "calibration.v2 profile collection. When supplied, matching "
            "eye-in-hand or static profiles are resolved for synchronized sensor "
            "folders and converted to the camera transform map consumed by the "
            "current prep script."
        ),
    )
    parser.add_argument(
        "--subdir",
        default="blenderproc",
        help="Subdirectory created inside each synchronized sensor folder.",
    )
    parser.add_argument(
        "--annotation-mode",
        choices=("pose", "pose_and_masks"),
        required=True,
        help=(
            "Prepare deterministic pose GT only, or pose GT for a later "
            "depth-aware mask generation step."
        ),
    )
    return parser.parse_args()


def synchronized_input_folder(
    run_root: Path, explicit_input_folder: str | None
) -> Path:
    if explicit_input_folder:
        return Path(explicit_input_folder)
    rectified = run_root / PROCESSED_DIR / "rectified"
    if rectified.is_dir():
        return rectified
    return run_root / PROCESSED_DIR / SYNCHRONIZED_DIR


def synchronized_sensor_names(
    input_folder: Path,
    *,
    sensor_names: tuple[str, ...] | None = None,
) -> list[str]:
    if not input_folder.is_dir():
        raise FileNotFoundError(f"Synchronized input folder not found: {input_folder}")
    selected_names = set(sensor_names) if sensor_names is not None else None
    return [
        child.name
        for child in sorted(input_folder.iterdir())
        if child.is_dir() and (selected_names is None or child.name in selected_names)
    ]


def derived_camera_transform_path(run_root: Path) -> Path:
    return run_root / PROCESSED_DIR / CALIBRATION_DIR / DERIVED_CAMERA_EE_TRANSFORM


def _selected_calibration_configured(run_root: Path, run_config: dict | None) -> bool:
    return (run_config or {}).get("calibration_profile_selection") is not None or (
        run_root / CALIBRATION_PROFILE_SELECTION
    ).exists()


def _run_input_path(run_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else run_root / path


def camera_transformations_from_calibration_profiles(
    *,
    input_folder: Path,
    calibration_profiles_path: Path,
    sensor_names: tuple[str, ...] | None = None,
    profile_ids_by_sensor_name: dict[str, str] | None = None,
    mounting_modes_by_sensor_name: Mapping[str, MountingMode] | None = None,
    destination_run_root: Path | None = None,
    run_config: Mapping[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    profiles = load_profile_collection(calibration_profiles_path)
    resolved_sensor_names = synchronized_sensor_names(
        input_folder,
        sensor_names=sensor_names,
    )
    transform_map = blenderproc_camera_transform_map_from_profiles(
        profiles,
        resolved_sensor_names,
        profile_ids_by_sensor_name=profile_ids_by_sensor_name,
        mounting_modes_by_sensor_name=mounting_modes_by_sensor_name,
    )
    if destination_run_root is not None:
        profiles_by_id = {profile.profile_id: profile for profile in profiles}
        selected_profiles = [
            profiles_by_id[str(transform_map[name]["profile_id"])]
            for name in resolved_sensor_names
        ]
        verify_static_profile_destination_reference(
            destination_run_root,
            run_config,
            selected_profiles,
            matched_robot_pose_paths_by_sensor_name={
                name: input_folder / name / MATCH_ROBOT_EE_POSES
                for name in resolved_sensor_names
            },
        )
    return transform_map


def run_prepare(
    *,
    input_folder: Path,
    camera_transformations: dict[str, object],
    subdir: str,
    object_instances: dict | None = None,
    run_root: Path | None = None,
    sensor_names: tuple[str, ...] | None = None,
    annotation_mode: str,
) -> dict[str, Path]:
    prepared = prepare_sensor_folders(
        input_folder=input_folder,
        camera_transformations=camera_transformations,
        subdir=subdir,
        object_instances=object_instances,
        run_root=run_root,
        sensor_names=sensor_names,
        annotation_mode=annotation_mode,
    )
    return {f"{item.sensor_name}:{subdir}": item.output_folder for item in prepared}


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    input_folder = synchronized_input_folder(run_root, args.input_folder)
    sensor_names = (
        enabled_sensor_folder_names(run_root) if args.input_folder is None else None
    )
    camera_transformations_path = Path(args.camera_transformations)
    calibration_profiles = (
        _run_input_path(run_root, args.calibration_profiles)
        if args.calibration_profiles
        else None
    )

    manifest = load_or_create_run_manifest(run_root)
    upsert_stage(manifest, name="blenderproc_prepare", status="running")
    write_run_manifest(manifest, run_root)

    try:
        stage_artifacts: dict[str, Path] = {}
        object_instances = None
        run_config = load_run_config_for_run_root(run_root)
        mounting_modes_by_sensor_name = enabled_sensor_mounting_modes_by_folder(
            run_config
        )
        calibration_profile_ids_by_sensor_name = None
        if _selected_calibration_configured(run_root, run_config):
            if calibration_profiles is None:
                raise ValueError(
                    "A run with selected calibration provenance must pass its "
                    "calibration_profiles snapshot to BlenderProc preparation"
                )
            from posetestbot.calibration.profile_library import (
                selected_calibration_profile_ids_by_sensor_folder,
                verify_calibration_profile_selection,
            )

            calibration_selection = verify_calibration_profile_selection(
                run_root,
                expected_calibration_profiles=calibration_profiles,
            )
            calibration_profile_ids_by_sensor_name = (
                selected_calibration_profile_ids_by_sensor_folder(
                    run_root,
                    selection=calibration_selection,
                )
            )
        if (
            not args.objectless
            and run_config is not None
            and run_config.get("dataset_mode") == "pose_template"
        ):
            object_instances = prepare_object_instances(run_root)
            stage_artifacts[OBJECT_INSTANCES] = run_root / OBJECT_INSTANCES
        if calibration_profiles is not None:
            camera_transformations = camera_transformations_from_calibration_profiles(
                input_folder=input_folder,
                calibration_profiles_path=calibration_profiles,
                sensor_names=sensor_names,
                profile_ids_by_sensor_name=calibration_profile_ids_by_sensor_name,
                mounting_modes_by_sensor_name=mounting_modes_by_sensor_name,
                destination_run_root=run_root,
                run_config=run_config,
            )
            stage_artifacts[CALIBRATION_PROFILES] = calibration_profiles
        else:
            camera_transformations = dict(
                load_camera_transformations(camera_transformations_path)
            )

        prepared_artifacts = run_prepare(
            input_folder=input_folder,
            camera_transformations=camera_transformations,
            subdir=args.subdir,
            object_instances=object_instances,
            run_root=run_root,
            sensor_names=sensor_names,
            annotation_mode=args.annotation_mode,
        )
        if calibration_profiles is not None:
            transform_path = write_camera_transformations(
                derived_camera_transform_path(run_root), camera_transformations
            )
            stage_artifacts[DERIVED_CAMERA_EE_TRANSFORM] = transform_path
    except Exception as exc:
        upsert_stage(
            manifest,
            name="blenderproc_prepare",
            status="failed",
            message=str(exc),
        )
        write_run_manifest(manifest, run_root)
        raise

    artifacts = {**stage_artifacts, **prepared_artifacts}
    upsert_stage(
        manifest,
        name="blenderproc_prepare",
        status="succeeded",
        artifacts=artifacts,
        run_root=run_root,
        message=f"Prepared BlenderProc inputs for {len(artifacts)} sensor folder(s).",
    )
    write_run_manifest(manifest, run_root)
    print(f"Prepared BlenderProc inputs for {len(artifacts)} sensor folder(s).")


if __name__ == "__main__":
    main()
