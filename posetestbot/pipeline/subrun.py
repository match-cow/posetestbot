"""Object-dataset subruns: capture a variant without repeating setup.

A subrun is a new sibling run folder that replays an existing dataset run's
camera setup, calibration snapshot, and pose-template placement. Only the
physical variant (workpiece material, environment, part copy) differs, so the
operator continues directly at readiness. Each subrun owns its own snapshots
and capture; lineage is recorded in ``run_config.json`` ``subrun``. Nothing in
the parent run is modified.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from posetestbot.calibration.profile_library import (
    load_calibration_profile_selection,
    select_calibration_profile_composite_snapshot,
    selected_calibration_run_config_defaults,
)
from posetestbot.calibration.retained_library import default_calibration_library_root
from posetestbot.pipeline.run_config import (
    MAX_SUBRUN_VARIANT_LENGTH,
    create_run_config,
    fixed_transform_from_mapping,
    load_run_config_for_run_root,
    sensor_configs_from_values,
    write_run_config_with_manifest,
)
from posetestbot.pose_templates.selection import (
    load_pose_template_selection,
    select_pose_template,
)


SUBRUN_SUFFIX = "__sub"
_FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _remove_created_folder(target: Path) -> None:
    """Remove a folder this module just created, including read-only snapshots."""

    def writable(function, path, _error):
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        if os.path.isdir(path) and not os.path.islink(path):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        function(path)

    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, onexc=writable)


def validate_variant(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > MAX_SUBRUN_VARIANT_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(
            "Describe the variant (material, environment, part copy) in 1-"
            f"{MAX_SUBRUN_VARIANT_LENGTH} characters"
        )
    return value.strip()


def _default_folder_name(parent: Path) -> str:
    base = parent.name
    index = 2
    while os.path.lexists(parent.parent / f"{base}{SUBRUN_SUFFIX}{index:02d}"):
        index += 1
    return f"{base}{SUBRUN_SUFFIX}{index:02d}"


def inspect_subrun_parent(parent_root: str | Path) -> dict[str, Any]:
    """Describe what a subrun of ``parent_root`` reuses; fail if it cannot."""

    parent = Path(parent_root).resolve()
    config = load_run_config_for_run_root(parent)
    capture = config["capture"]
    if capture["intent"] != "dataset" or config.get("dataset_mode") != "pose_template":
        raise ValueError("Subruns can only be created from an object-dataset run")
    lineage = config.get("subrun")
    if lineage is not None:
        raise ValueError(
            "This run is itself a subrun. Create the new subrun from the original "
            f"run: {lineage['parent_run_root']}"
        )
    calibration = load_calibration_profile_selection(parent)
    sources = calibration.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(
            "The parent run has no per-camera calibration sources to reuse"
        )
    template = load_pose_template_selection(parent)
    if template.get("placement_confirmed") is not True:
        raise ValueError(
            "Confirm the parent run's pose-template placement before creating a subrun"
        )
    return {
        "schema_version": "subrun_parent.v1",
        "parent_run_root": parent.as_posix(),
        "parent_run_id": config["run_id"],
        "parent_run_name": config["run_name"],
        "default_folder_name": _default_folder_name(parent),
        "capture": {
            "resolution": capture["resolution"],
            "fps": capture["fps"],
            "velocity_m_s": capture["velocity_m_s"],
            "sensors": [
                {
                    "sensor_type": sensor["sensor_type"],
                    "device_id": sensor["device_id"],
                    "display_name": sensor.get("operator_alias")
                    or sensor["display_name"],
                    "mounting_mode": sensor["mounting_mode"],
                }
                for sensor in capture["sensors"]
                if sensor.get("enabled", True)
            ],
        },
        "calibration_bundle_sha256": calibration["source"]["bundle_sha256"],
        "calibration_sources": [
            {
                "run_root": source["run_root"],
                "run_name": source.get("run_name"),
                "bundle_sha256": source["bundle_sha256"],
                "sensor_keys": source["selected_sensor_keys"],
            }
            for source in sources
        ],
        "template_uuid": template["template_uuid"],
        "template_name": template.get("display_name"),
        "template_bundle_sha256": template["bundle_sha256"],
        "placement_matrix": template["template_base_from_pose_template"]["matrix"],
    }


def subrun_target(parent_root: str | Path, folder_name: Any) -> Path:
    """Resolve the new sibling folder; it must not exist yet."""

    parent = Path(parent_root).resolve()
    if not isinstance(folder_name, str) or not _FOLDER_NAME.fullmatch(folder_name):
        raise ValueError(
            "Subrun folder name must be 1-128 letters, digits, '.', '_' or '-'"
        )
    target = parent.parent / folder_name
    if os.path.lexists(target):
        raise ValueError(f"Run folder already exists: {target}")
    return target


def _calibration_source_root(source: dict[str, Any]) -> str:
    """Prefer the original source run; fall back to its retained library copy."""

    run_root = Path(source["run_root"])
    if run_root.exists():
        return run_root.as_posix()
    retained = default_calibration_library_root() / source["bundle_sha256"]
    if retained.exists():
        return retained.as_posix()
    raise ValueError(
        f"Calibration source {run_root} no longer exists and has no retained "
        "library copy; select a calibration in a normal new run instead"
    )


def create_subrun(
    parent_root: str | Path,
    folder_name: str,
    variant: str,
    *,
    template_library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Create a ready-to-capture sibling run that reuses the parent's setup."""

    plan = inspect_subrun_parent(parent_root)
    parent = Path(plan["parent_run_root"])
    target = subrun_target(parent, folder_name)
    variant = validate_variant(variant)
    parent_config = load_run_config_for_run_root(parent)
    capture = parent_config["capture"]
    target.mkdir()
    try:
        calibration = load_calibration_profile_selection(parent)
        selected = select_calibration_profile_composite_snapshot(
            target,
            source_selections=[
                {
                    "source_run_root": _calibration_source_root(source),
                    "expected_bundle_sha256": source["bundle_sha256"],
                    "sensor_keys": list(source["selected_sensor_keys"]),
                }
                for source in calibration["sources"]
            ],
            sensors=capture["sensors"],
            resolution=capture["resolution"],
            operator=calibration.get("operator") or "subrun",
        )
        if (
            selected["selection"]["source"]["bundle_sha256"]
            != plan["calibration_bundle_sha256"]
        ):
            raise ValueError(
                "The replayed calibration differs from the parent run's snapshot"
            )
        sensors = sensor_configs_from_values(capture["sensors"])
        defaults = selected_calibration_run_config_defaults(
            target,
            sensors=sensors,
            resolution=capture["resolution"],
            requested_calibration_profiles=None,
            infer_when_omitted=True,
            expected_bundle_sha256=plan["calibration_bundle_sha256"],
        )
        if defaults is None:
            raise ValueError("The replayed calibration selection is not readable")
        config = create_run_config(
            run_root=target,
            capture_intent="dataset",
            bop_annotation_mode="none",
            resolution=capture["resolution"],
            fps=capture["fps"],
            velocity_m_s=capture["velocity_m_s"],
            sensors=sensors,
            dataset_mode="pose_template",
            calibration_profiles=defaults["calibration_profiles"],
            intrinsic_calibration_profiles=defaults["intrinsic_calibration_profiles"],
            calibration_profile_selection=defaults["calibration_profile_selection"],
            calibration_target=parent_config.get("calibration_target"),
            fixed_transforms=tuple(
                fixed_transform_from_mapping(item)
                for item in parent_config["frames"].get("fixed_transforms", [])
            ),
            synchronization=capture["synchronization"],
            subrun={
                "parent_run_id": plan["parent_run_id"],
                "parent_run_root": parent.as_posix(),
                "variant": variant,
                "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            },
        )
        write_run_config_with_manifest(target, config)
        parent_template = load_pose_template_selection(parent)
        try:
            template = select_pose_template(
                target,
                plan["template_uuid"],
                placement={"matrix": plan["placement_matrix"]},
                confirmed=True,
                operator=parent_template.get("operator"),
                library_root=template_library_root,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(
                "The parent's pose template is no longer active in the library; "
                f"create a normal new run instead ({exc})"
            ) from exc
        if template["bundle_sha256"] != plan["template_bundle_sha256"]:
            raise ValueError(
                "The pose-template library version differs from the parent's snapshot"
            )
    except BaseException:
        # The new folder holds only derived snapshots written above; never
        # leave a half-configured run behind.
        _remove_created_folder(target)
        raise
    return {
        "schema_version": "subrun_created.v1",
        "run_root": target.as_posix(),
        "run_id": config.run_id,
        "parent_run_root": parent.as_posix(),
        "variant": variant,
        "calibration_bundle_sha256": plan["calibration_bundle_sha256"],
        "template_bundle_sha256": template["bundle_sha256"],
    }
