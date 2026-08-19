"""Fixed orchestration recipes for the two supported operator workflows.

The public web API and CLIs deliberately share these command builders.  There
is no registry or caller-supplied stage list: capture and dataset processing
always execute the recipes defined here.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from posetestbot.calibration.profile_library import (
    verify_calibration_profile_selection,
)
from posetestbot.pipeline.capture_execution import (
    run_capture_execution,
    write_capture_execution_plan_with_manifest,
)
from posetestbot.pipeline.capture_plan import write_capture_plan_with_manifest
from posetestbot.pipeline.capture_plan_preflight import (
    write_capture_plan_preflight_with_manifest,
)
from posetestbot.pipeline.preflight import run_preflight_queue_summary
from posetestbot.pipeline.run_config import (
    CAPTURE_INTENTS,
    load_run_config_for_run_root,
)
from posetestbot.sensors.readiness import (
    probe_selected_sensor_readiness,
    selected_sensor_readiness_matches_config,
)


@dataclass(frozen=True)
class JobRecipe:
    """One fixed background-job submission contract."""

    name: str
    command: tuple[str, ...]
    resources: tuple[str, ...]
    parameters: Mapping[str, Any]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _validated_intent(config: Mapping[str, Any], expected: str) -> None:
    if expected not in CAPTURE_INTENTS:
        raise ValueError(
            "intent must be one of: " + ", ".join(sorted(CAPTURE_INTENTS))
        )
    actual = config["capture"]["intent"]
    if actual != expected:
        raise ValueError(
            f"Run capture intent is {actual!r}, not requested intent {expected!r}"
        )


def plan_capture(
    run_root: str | Path,
    *,
    max_frames: int | None = None,
    warmup_frames: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Persist the canonical non-executing capture plan for one v4 run."""

    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    path, plan = write_capture_plan_with_manifest(
        root,
        config,
        max_frames=max_frames,
        warmup_frames=warmup_frames,
    )
    return path, plan.to_dict()


def execute_capture(
    run_root: str | Path,
    *,
    intent: str,
    allow_cameras: bool,
    allow_real_robot: bool,
    probe_selected_sensors=probe_selected_sensor_readiness,
) -> tuple[Path, dict[str, Any]]:
    """Run the fixed supervised physical-capture recipe."""

    if allow_cameras is not True or allow_real_robot is not True:
        raise ValueError(
            "Capture requires allow_cameras=true and allow_real_robot=true"
        )
    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    _validated_intent(config, intent)
    saved_preflight = run_preflight_queue_summary(root, config)
    if saved_preflight["ready_for_queue"] is not True:
        blocker = saved_preflight.get("queue_blocker") or "not_ready"
        raise ValueError(
            "A fresh successful run preflight is required before capture "
            f"({blocker})"
        )

    # This must precede capture-plan and supervisor artifacts. SDK discovery can
    # still enumerate a camera held by a crashed recorder, so prove that every
    # selected adapter can actually open and deliver a frame without recording.
    selected_sensor_readiness = probe_selected_sensors(config)
    if not selected_sensor_readiness_matches_config(
        selected_sensor_readiness,
        config,
    ):
        blocked = [
            str(probe.get("message"))
            for probe in selected_sensor_readiness.get("probes", [])
            if isinstance(probe, Mapping) and probe.get("capture_ready") is not True
        ]
        raise ValueError(
            "Selected cameras are not ready for capture: "
            + ("; ".join(blocked) if blocked else "readiness probe failed")
        )

    plan_capture(root)
    _, capture_preflight = write_capture_plan_preflight_with_manifest(
        root,
        include_sensor_status=True,
        allow_real_robot=True,
        write_plan_if_missing=False,
        selected_sensor_readiness=selected_sensor_readiness,
    )
    if capture_preflight["overall_status"] == "error":
        raise ValueError("Capture-plan preflight failed; inspect its checks")
    _, execution_plan = write_capture_execution_plan_with_manifest(
        root,
        allow_cameras=True,
        allow_real_robot=True,
        include_sensor_status=True,
        write_plan_if_missing=False,
    )
    if execution_plan["ready_to_execute"] is not True:
        raise ValueError("Capture execution plan is not ready")
    return run_capture_execution(
        root,
        allow_cameras=True,
        allow_real_robot=True,
        include_sensor_status=True,
        write_plan_if_missing=False,
    )


def dataset_processing_commands(run_root: str | Path) -> tuple[tuple[str, ...], ...]:
    """Return the immutable four-command dataset processing recipe."""

    root = Path(run_root)
    config = load_run_config_for_run_root(root)
    _validated_intent(config, "dataset")
    verify_calibration_profile_selection(
        root,
        expected_calibration_profiles=config.get("calibration_profiles"),
        expected_intrinsic_calibration_profiles=config.get(
            "intrinsic_calibration_profiles"
        ),
    )

    export_command = [
        "uv",
        "run",
        "python",
        "scripts/run_bop_export_stage.py",
        root.as_posix(),
        "--overwrite",
        "--calibration-profiles",
        str(config["calibration_profiles"]),
        "--annotation-source",
        "none",
        "--annotation-mode",
        "none",
    ]
    if config["dataset_mode"] == "objectless":
        export_command.append("--objectless")

    return (
        (
            "uv",
            "run",
            "python",
            "scripts/sync_run_non_destructive.py",
            root.as_posix(),
        ),
        (
            "uv",
            "run",
            "python",
            "scripts/run_sync_quality.py",
            root.as_posix(),
        ),
        (
            "uv",
            "run",
            "python",
            "scripts/run_camera_rectification.py",
            root.as_posix(),
            "--intrinsic-profiles",
            str(config["intrinsic_calibration_profiles"]),
            "--overwrite",
        ),
        tuple(export_command),
    )


def process_dataset(run_root: str | Path) -> tuple[tuple[str, ...], ...]:
    """Execute sync, quality, rectification, and calibrated BOP export."""

    commands = dataset_processing_commands(run_root)
    for command in commands:
        subprocess.run(
            command,
            cwd=_repo_root(),
            check=True,
        )
    return commands


def preflight_job_recipe(run_root: str | Path) -> JobRecipe:
    root = Path(run_root)
    return JobRecipe(
        name="Run preflight",
        command=(
            "uv",
            "run",
            "python",
            "scripts/run_preflight.py",
            root.as_posix(),
            "--check",
            "--write",
        ),
        resources=("camera", "disk_io"),
        parameters={"purpose": "preflight", "run_root": root.as_posix()},
    )


def capture_job_recipe(
    run_root: str | Path,
    *,
    intent: str,
    allow_cameras: bool,
    allow_real_robot: bool,
) -> JobRecipe:
    if intent not in CAPTURE_INTENTS:
        raise ValueError(
            "intent must be one of: " + ", ".join(sorted(CAPTURE_INTENTS))
        )
    if allow_cameras is not True or allow_real_robot is not True:
        raise ValueError(
            "Capture requires allow_cameras=true and allow_real_robot=true"
        )
    root = Path(run_root)
    return JobRecipe(
        name=f"{intent.capitalize()} capture",
        command=(
            "uv",
            "run",
            "python",
            "scripts/run_capture.py",
            root.as_posix(),
            "--intent",
            intent,
            "--allow-cameras",
            "--allow-real-robot",
        ),
        resources=("camera", "disk_io", "robot_command"),
        parameters={
            "purpose": "capture",
            "run_root": root.as_posix(),
            "intent": intent,
            "allow_cameras": True,
            "allow_real_robot": True,
        },
    )


def dataset_processing_job_recipe(run_root: str | Path) -> JobRecipe:
    root = Path(run_root)
    # Validate the fixed recipe before queueing so bad run state fails fast.
    dataset_processing_commands(root)
    return JobRecipe(
        name="Dataset processing",
        command=(
            "uv",
            "run",
            "python",
            "scripts/process_dataset.py",
            root.as_posix(),
        ),
        resources=("cpu", "disk_io"),
        parameters={"purpose": "dataset_processing", "run_root": root.as_posix()},
    )
