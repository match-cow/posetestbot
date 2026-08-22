"""Fixed orchestration recipes for the two supported operator workflows.

The public web API and CLIs deliberately share these command builders.  There
is no registry or caller-supplied stage list: capture and dataset processing
always execute the recipes defined here.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from posetestbot.calibration.profile_library import (
    verify_calibration_profile_selection,
)
from posetestbot.io.artifacts import CAPTURE_EXECUTION_REPORT
from posetestbot.io.artifacts import (
    CAPTURE_EXECUTION_LOGS_DIR,
    CAPTURE_EXECUTION_PLAN,
    CAPTURE_EXECUTION_STATUS,
)
from posetestbot.pipeline.capture_completion import (
    SCHEMA_VERSION as CAPTURE_COMPLETION_SCHEMA_VERSION,
    build_capture_completion,
)
from posetestbot.pipeline.capture_execution import (
    REPORT_SCHEMA_VERSION as CAPTURE_EXECUTION_REPORT_SCHEMA_VERSION,
    run_capture_execution,
    write_capture_execution_plan_with_manifest,
)
from posetestbot.pipeline.capture_plan import write_capture_plan_with_manifest
from posetestbot.pipeline.capture_plan_preflight import (
    write_capture_plan_preflight_with_manifest,
)
from posetestbot.pipeline.preflight import (
    current_intent_preflight_checks,
    run_preflight_queue_summary,
)
from posetestbot.pipeline.run_config import (
    CAPTURE_INTENTS,
    load_run_config_for_run_root,
    run_config_sha256,
)
from posetestbot.sensors.readiness import (
    probe_selected_sensor_readiness,
    selected_sensor_readiness_matches_config,
)
from posetestbot.sensors.status import collect_sensor_status


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
        raise ValueError("intent must be one of: " + ", ".join(sorted(CAPTURE_INTENTS)))
    actual = config["capture"]["intent"]
    if actual != expected:
        raise ValueError(
            f"Run capture intent is {actual!r}, not requested intent {expected!r}"
        )


def _verify_successful_capture_for_processing(
    run_root: Path,
    config: Mapping[str, Any],
    *,
    revalidate_raw_evidence: bool = True,
) -> dict[str, Any]:
    """Verify capture provenance, optionally rescanning all raw RGB-D evidence."""

    report_path = run_root / CAPTURE_EXECUTION_REPORT
    if not report_path.is_file() or report_path.is_symlink():
        raise ValueError(
            "Dataset processing requires a successful supervised capture report: "
            f"{report_path}"
        )
    try:
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Capture execution report is unreadable: {report_path}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise ValueError("Capture execution report must be a JSON object")
    if report.get("schema_version") != CAPTURE_EXECUTION_REPORT_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported capture execution report schema: "
            f"{report.get('schema_version')!r}"
        )
    if report.get("status") != "succeeded":
        raise ValueError(
            "Dataset processing requires capture_execution_report.json with "
            "status 'succeeded'"
        )

    expected_config_sha256 = run_config_sha256(config)
    if report.get("run_config_sha256") != expected_config_sha256:
        raise ValueError(
            "Dataset processing requires capture evidence bound to the exact "
            "current run_config.json"
        )
    execution_id = report.get("execution_id")
    if (
        not isinstance(execution_id, str)
        or len(execution_id) != 32
        or execution_id != execution_id.lower()
        or any(character not in "0123456789abcdef" for character in execution_id)
    ):
        raise ValueError("Capture execution report has an invalid execution identity")
    expected_archive = f"{CAPTURE_EXECUTION_LOGS_DIR}/{execution_id}"
    if report.get("execution_archive") != expected_archive:
        raise ValueError("Capture execution report has an invalid archive binding")

    embedded_plan = report.get("capture_execution_plan")
    if (
        not isinstance(embedded_plan, Mapping)
        or embedded_plan.get("schema_version") != "capture_execution_plan.v2"
        or embedded_plan.get("execution_id") != execution_id
        or embedded_plan.get("execution_archive") != expected_archive
        or embedded_plan.get("run_config_sha256") != expected_config_sha256
    ):
        raise ValueError("Capture execution report has invalid bound plan evidence")
    preflight = embedded_plan.get("preflight_report")
    snapshot = preflight.get("config") if isinstance(preflight, Mapping) else None
    if not isinstance(snapshot, Mapping) or run_config_sha256(snapshot) != (
        expected_config_sha256
    ):
        raise ValueError(
            "Capture execution plan's accepted run-configuration snapshot is invalid"
        )

    logs_root = run_root / CAPTURE_EXECUTION_LOGS_DIR
    archive_root = run_root / expected_archive
    if (
        not logs_root.is_dir()
        or logs_root.is_symlink()
        or not archive_root.is_dir()
        or archive_root.is_symlink()
    ):
        raise ValueError("Capture execution archive path is invalid")

    def load_archived_json(filename: str) -> dict[str, Any]:
        path = archive_root / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Capture execution archive artifact is missing: {path}")
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Capture execution archive artifact is unreadable: {path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"Capture execution archive artifact is invalid: {path}")
        return value

    archived_report = load_archived_json(CAPTURE_EXECUTION_REPORT)
    archived_plan = load_archived_json(CAPTURE_EXECUTION_PLAN)
    archived_status = load_archived_json(CAPTURE_EXECUTION_STATUS)
    if archived_report != report or archived_plan != embedded_plan:
        raise ValueError(
            "Capture execution archive does not match the current successful generation"
        )
    if (
        archived_status.get("schema_version") != "capture_execution_status.v2"
        or archived_status.get("execution_id") != execution_id
        or archived_status.get("execution_archive") != expected_archive
        or archived_status.get("run_config_sha256") != expected_config_sha256
        or archived_status.get("status") != "succeeded"
    ):
        raise ValueError("Capture execution archive has invalid final status evidence")
    current_plan_path = run_root / CAPTURE_EXECUTION_PLAN
    current_status_path = run_root / CAPTURE_EXECUTION_STATUS
    for path, expected in (
        (current_plan_path, archived_plan),
        (current_status_path, archived_status),
    ):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Current capture execution artifact is missing: {path}")
        try:
            with open(path, encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Current capture execution artifact is unreadable: {path}"
            ) from exc
        if current != expected:
            raise ValueError(
                "Current capture execution artifacts do not match the successful archive"
            )

    stored_completion = report.get("completion")
    if (
        not isinstance(stored_completion, Mapping)
        or stored_completion.get("schema_version") != CAPTURE_COMPLETION_SCHEMA_VERSION
        or stored_completion.get("status") != "ok"
    ):
        raise ValueError(
            "Dataset processing requires a successful capture completion check"
        )
    process_records = report.get("processes")
    if not isinstance(process_records, list) or not all(
        isinstance(record, Mapping) for record in process_records
    ):
        raise ValueError("Capture execution report has invalid process evidence")

    if revalidate_raw_evidence:
        # The original report proves that the supervisor completed. Rebuilding
        # the completion contract inside the queued worker also detects raw
        # evidence deletion/corruption without decoding every PNG in the Flask
        # submission request.
        current_completion = build_capture_completion(
            run_root,
            config,
            process_records,
        )
        if current_completion.get("status") != "ok":
            failed = [
                str(check.get("name"))
                for check in current_completion.get("checks", [])
                if isinstance(check, Mapping) and check.get("status") == "error"
            ]
            detail = ", ".join(failed) if failed else "unknown completion check"
            raise ValueError(
                "Current capture evidence no longer passes completion validation: "
                f"{detail}"
            )
    final_config = load_run_config_for_run_root(run_root)
    if run_config_sha256(final_config) != expected_config_sha256:
        raise ValueError(
            "run_config.json changed while successful capture evidence was being "
            "verified; dataset processing refuses the stale configuration snapshot"
        )
    return report


def _validate_dataset_processing_inputs(
    run_root: Path,
    *,
    revalidate_raw_evidence: bool,
) -> Mapping[str, Any]:
    config = load_run_config_for_run_root(run_root)
    _validated_intent(config, "dataset")
    _verify_successful_capture_for_processing(
        run_root,
        config,
        revalidate_raw_evidence=revalidate_raw_evidence,
    )
    verify_calibration_profile_selection(
        run_root,
        expected_calibration_profiles=config.get("calibration_profiles"),
        expected_intrinsic_calibration_profiles=config.get(
            "intrinsic_calibration_profiles"
        ),
    )
    return config


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
    collect_sensors=collect_sensor_status,
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
            f"A fresh successful run preflight is required before capture ({blocker})"
        )

    current_intent_checks = current_intent_preflight_checks(root, config)
    current_intent_errors = [
        check
        for check in current_intent_checks
        if isinstance(check, Mapping) and check.get("status") == "error"
    ]
    if current_intent_errors:
        messages = [
            str(check.get("message") or check.get("name") or "invalid evidence")
            for check in current_intent_errors
        ]
        raise ValueError(
            "Current intent-specific preflight evidence is invalid: "
            + "; ".join(messages)
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

    # General SDK/device discovery is diagnostic once the selected adapters
    # have actually opened and delivered a frame. Capture planning rebuilds its
    # immutable commands several times, so reuse one adjacent status snapshot
    # instead of repeatedly touching the same SDKs immediately before launch.
    sensor_status = collect_sensors()

    def cached_sensor_status() -> dict:
        return sensor_status

    plan_capture(root)
    _, capture_preflight = write_capture_plan_preflight_with_manifest(
        root,
        include_sensor_status=True,
        allow_real_robot=True,
        collect_sensors=cached_sensor_status,
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
        collect_sensors=cached_sensor_status,
        write_plan_if_missing=False,
    )
    if execution_plan["ready_to_execute"] is not True:
        raise ValueError("Capture execution plan is not ready")
    return run_capture_execution(
        root,
        allow_cameras=True,
        allow_real_robot=True,
        include_sensor_status=True,
        collect_sensors=cached_sensor_status,
        write_plan_if_missing=False,
    )


def dataset_processing_commands(run_root: str | Path) -> tuple[tuple[str, ...], ...]:
    """Return the immutable four-command dataset processing recipe."""

    root = Path(run_root)
    config = _validate_dataset_processing_inputs(
        root,
        revalidate_raw_evidence=True,
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
        raise ValueError("intent must be one of: " + ", ".join(sorted(CAPTURE_INTENTS)))
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
    # Keep request-time work bounded. The queued process repeats these checks
    # and performs the exhaustive raw RGB-D completion scan before stage one.
    _validate_dataset_processing_inputs(root, revalidate_raw_evidence=False)
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
