"""Shared implementations for PoseTestBot's focused operator APIs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from flask import Response, jsonify, request

from posetestbot.calibration.profile_library import (
    CalibrationSelectionConflict,
    selected_calibration_run_config_defaults,
)
from posetestbot.config import (
    DEFAULT_CAPTURE_VELOCITY_M_S,
    MANUAL_TEST_COMMAND_VELOCITY_M_S,
    robot_profile,
)
from posetestbot.io.artifacts import (
    CAPTURE_EXECUTION_LOGS_DIR,
    CAPTURE_EXECUTION_REPORT,
    CAPTURE_EXECUTION_STATUS,
    DEPTH_DIR,
    FRAME_METADATA_JSONL,
    RAW_ROBOT_EE_POSES,
    RGB_DIR,
)
from posetestbot.jobs.runner import ResourceBusyError, TERMINAL_STATUSES
from posetestbot.pipeline.capture_execution import load_capture_execution_status
from posetestbot.pipeline.hardware_status import (
    load_hardware_status_report,
    write_hardware_status_report_with_manifest,
)
from posetestbot.pipeline.orchestration import (
    capture_job_recipe,
    dataset_processing_job_recipe,
    preflight_job_recipe,
)
from posetestbot.pipeline.preflight import run_preflight_queue_summary
from posetestbot.pipeline.run_config import (
    BOP_ANNOTATION_MODES,
    CAPTURE_INTENTS,
    capture_synchronization_from_mapping,
    create_run_config,
    fixed_transform_from_mapping,
    load_run_config_for_run_root,
    run_config_lock,
    sensor_configs_from_status,
    sensor_configs_from_values,
    write_run_config_with_manifest,
)
from posetestbot.robot.status import collect_robot_status
from posetestbot.runtime.status import collect_runtime_status
from posetestbot.sensors.registry import list_sensor_adapters
from posetestbot.sensors.status import collect_sensor_status
from posetestbot.sync.quality import (
    build_sync_quality_report,
    write_sync_quality_report_with_manifest,
)
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.runtime import job_runner
from posetestbot.web.security import resolve_web_run_root


ACTIVE_JOB_STATUSES = {"queued", "running", "canceling"}


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def _raw_capture_evidence(run_root: str | Path) -> list[str]:
    """Return material raw evidence that freezes a run's camera contract."""

    root = Path(run_root)
    evidence: list[str] = []
    for artifact in (CAPTURE_EXECUTION_STATUS, CAPTURE_EXECUTION_REPORT):
        if (root / artifact).is_file():
            evidence.append(artifact)
    logs = root / CAPTURE_EXECUTION_LOGS_DIR
    if logs.is_dir() and any(logs.iterdir()):
        evidence.append(CAPTURE_EXECUTION_LOGS_DIR)
    if (root / RAW_ROBOT_EE_POSES).is_file():
        evidence.append(RAW_ROBOT_EE_POSES)
    if not root.is_dir():
        return sorted(set(evidence))
    for candidate in root.iterdir():
        if not candidate.is_dir() or candidate.name == "processed":
            continue
        for relative in (FRAME_METADATA_JSONL, RGB_DIR, DEPTH_DIR):
            path = candidate / relative
            populated = (
                path.is_file()
                if relative == FRAME_METADATA_JSONL
                else (path.is_dir() and any(path.glob("*.png")))
            )
            if populated:
                evidence.append(f"{candidate.name}/{relative}")
                break
    return sorted(set(evidence))


def _capture_sensor_contract(sensors: Any) -> list[tuple[Any, ...]]:
    contract = []
    for sensor in sensors or []:
        value = sensor.to_dict() if hasattr(sensor, "to_dict") else sensor
        if not isinstance(value, Mapping):
            continue
        contract.append(
            (
                str(value.get("sensor_type") or ""),
                str(value.get("device_id") or ""),
                str(value.get("mounting_mode") or ""),
                value.get("enabled", True) is True,
                value.get("inverted", False) is True,
            )
        )
    return sorted(contract)


def _camera_contract_state(run_root: str | Path) -> dict[str, Any]:
    blockers = _raw_capture_evidence(run_root)
    return {"mutable": not blockers, "blockers": blockers}


def _run_config_from_payload(data: dict[str, Any]):
    run_root = data.get("run_root")
    if not run_root:
        raise ValueError("run_root is required")
    allowed = {
        "run_root",
        "run_name",
        "intent",
        "annotation_mode",
        "resolution",
        "fps",
        "velocity_m_s",
        "sensors",
        "from_detected_sensors",
        "mounting_mode",
        "synchronization",
        "dataset_mode",
        "calibration_profiles",
        "intrinsic_calibration_profiles",
        "expected_calibration_bundle_sha256",
    }
    unsupported = sorted(set(data) - allowed)
    if unsupported:
        raise ValueError(
            "Request contains unsupported fields: " + ", ".join(unsupported)
        )
    try:
        existing = load_run_config_for_run_root(run_root)
    except FileNotFoundError:
        existing = None
    existing_capture = existing["capture"] if existing is not None else {}

    intent = data.get("intent")
    if intent not in CAPTURE_INTENTS:
        raise ValueError("intent is required and must be calibration or dataset")
    annotation_mode = data.get("annotation_mode")
    if annotation_mode not in BOP_ANNOTATION_MODES:
        raise ValueError(
            "annotation_mode is required and must be one of: "
            + ", ".join(sorted(BOP_ANNOTATION_MODES))
        )

    mounting_mode = data.get("mounting_mode")
    if data.get("from_detected_sensors") is True and "sensors" not in data:
        sensors = sensor_configs_from_status(
            collect_sensor_status(),
            default_mounting_mode=mounting_mode,
        )
        if not sensors:
            raise ValueError("No connected sensors were detected")
    elif "sensors" in data:
        sensors = sensor_configs_from_values(
            data["sensors"], default_mounting_mode=mounting_mode
        )
    elif existing is not None:
        sensors = sensor_configs_from_values(existing_capture["sensors"])
    else:
        sensors = sensor_configs_from_values(None, default_mounting_mode=mounting_mode)

    resolution = data.get("resolution", existing_capture.get("resolution", "720p"))
    fps = int(data.get("fps", existing_capture.get("fps", 6)))
    synchronization = capture_synchronization_from_mapping(
        data.get("synchronization", existing_capture.get("synchronization"))
    )
    if existing is not None:
        changed = (
            _capture_sensor_contract(existing_capture["sensors"])
            != _capture_sensor_contract(sensors)
            or resolution != existing_capture["resolution"]
            or fps != existing_capture["fps"]
            or synchronization.to_dict() != existing_capture["synchronization"]
            or intent != existing_capture["intent"]
        )
        evidence = _raw_capture_evidence(run_root) if changed else []
        if evidence:
            raise ValueError(
                "Cannot change capture intent or camera contract after raw evidence "
                "exists; create a new run: " + ", ".join(evidence)
            )

    requested_profiles = data.get(
        "calibration_profiles",
        existing.get("calibration_profiles") if existing is not None else None,
    )
    expected_bundle = data.get("expected_calibration_bundle_sha256")
    selection_defaults = selected_calibration_run_config_defaults(
        run_root,
        sensors=sensors,
        resolution=resolution,
        requested_calibration_profiles=(
            str(requested_profiles) if requested_profiles else None
        ),
        infer_when_omitted="calibration_profiles" not in data,
        expected_bundle_sha256=expected_bundle,
    )
    if selection_defaults is not None:
        selected_by_key = {
            (item["sensor_type"], item["device_id"]): item["profile_id"]
            for item in selection_defaults["sensor_profile_mapping"]
        }
        sensors = tuple(
            replace(
                sensor,
                calibration_profile_id=selected_by_key.get(
                    (sensor.sensor_type, sensor.device_id),
                    sensor.calibration_profile_id,
                ),
            )
            for sensor in sensors
        )
        calibration_profiles = selection_defaults["calibration_profiles"]
        intrinsic_profiles = selection_defaults["intrinsic_calibration_profiles"]
        calibration_selection = selection_defaults["calibration_profile_selection"]
    else:
        calibration_profiles = requested_profiles
        intrinsic_profiles = data.get(
            "intrinsic_calibration_profiles",
            existing.get("intrinsic_calibration_profiles")
            if existing is not None
            else None,
        )
        calibration_selection = (
            existing.get("calibration_profile_selection")
            if existing is not None
            else None
        )

    dataset_mode = data.get(
        "dataset_mode", existing.get("dataset_mode") if existing else "objectless"
    )
    if intent == "calibration":
        dataset_mode = "objectless"
    pose_template = existing.get("pose_template") if existing else None
    if dataset_mode != "pose_template":
        pose_template = None
    frames = existing.get("frames", {}) if existing else {}
    fixed_transforms = tuple(
        fixed_transform_from_mapping(item)
        for item in frames.get("fixed_transforms", [])
    )
    velocity = float(
        data.get(
            "velocity_m_s",
            existing_capture.get("velocity_m_s", DEFAULT_CAPTURE_VELOCITY_M_S),
        )
    )
    return create_run_config(
        run_root=run_root,
        run_id=existing.get("run_id") if existing else None,
        capture_intent=intent,
        bop_annotation_mode=annotation_mode,
        run_name=data.get(
            "run_name", existing.get("run_name") if existing is not None else None
        ),
        resolution=resolution,
        fps=fps,
        velocity_m_s=velocity,
        sensors=sensors,
        dataset_mode=dataset_mode,
        pose_template=pose_template,
        calibration_profiles=calibration_profiles,
        intrinsic_calibration_profiles=intrinsic_profiles,
        calibration_profile_selection=calibration_selection,
        calibration_target=(
            existing.get("calibration_target") if existing is not None else None
        ),
        fixed_transforms=fixed_transforms,
        synchronization=synchronization,
    )


def run_config():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"output": "JSON object required"}), 400
        try:
            root = resolve_web_run_root(data.get("run_root"))
            normalized = {**data, "run_root": root.as_posix()}
            with run_config_lock(root):
                config = _run_config_from_payload(normalized)
                path = write_run_config_with_manifest(root, config)
                config_data = config.to_dict()
                preflight = run_preflight_queue_summary(root, config_data)
        except CalibrationSelectionConflict as exc:
            return jsonify({"output": str(exc), "issues": exc.issues}), 409
        except (OSError, ValueError) as exc:
            return jsonify({"output": str(exc)}), 400
        return (
            jsonify(
                {
                    "output": f"Wrote {path}",
                    "path": path.as_posix(),
                    "run_root": root.as_posix(),
                    "config": config_data,
                    "preflight": preflight,
                    "camera_contract": _camera_contract_state(root),
                }
            ),
            201,
        )

    run_root = request.args.get("run_root")
    if not run_root:
        return jsonify({"output": "Missing run_root"}), 400
    try:
        root = resolve_web_run_root(run_root)
        config = load_run_config_for_run_root(root)
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify(
        {
            "run_root": root.as_posix(),
            "config": config,
            "preflight": run_preflight_queue_summary(root, config),
            "camera_contract": _camera_contract_state(root),
        }
    )


def _submit_recipe(recipe, run_root: str | Path):
    try:
        job = job_runner.submit(
            name=recipe.name,
            command=list(recipe.command),
            cwd=APP_ROOT,
            resources=list(recipe.resources),
            parameters=dict(recipe.parameters),
            scope_kind="run",
            run_root=run_root,
        )
    except ResourceBusyError as exc:
        return jsonify({"output": str(exc)}), 409
    return (
        jsonify(
            {
                "output": f"Queued {recipe.name} as job {job.id}",
                "job_id": job.id,
                "status": job.status,
                "job": job.to_dict(),
            }
        ),
        202,
    )


def _purpose_payload(
    *,
    allowed: set[str],
    required: set[str],
) -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    unsupported = sorted(set(data) - allowed)
    if unsupported:
        raise ValueError("Unsupported fields: " + ", ".join(unsupported))
    missing = sorted(field for field in required if data.get(field) is None)
    if missing:
        raise ValueError("Required fields: " + ", ".join(missing))
    return data


def submit_preflight_job():
    try:
        data = _purpose_payload(allowed={"run_root"}, required={"run_root"})
        root = resolve_web_run_root(data["run_root"])
        load_run_config_for_run_root(root)
        recipe = preflight_job_recipe(root)
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400
    return _submit_recipe(recipe, root)


def submit_capture_job():
    try:
        data = _purpose_payload(
            allowed={
                "run_root",
                "intent",
                "allow_cameras",
                "allow_real_robot",
            },
            required={
                "run_root",
                "intent",
                "allow_cameras",
                "allow_real_robot",
            },
        )
        root = resolve_web_run_root(data["run_root"])
        recipe = capture_job_recipe(
            root,
            intent=data.get("intent"),
            allow_cameras=data.get("allow_cameras") is True,
            allow_real_robot=data.get("allow_real_robot") is True,
        )
        config = load_run_config_for_run_root(root)
        if config["capture"]["intent"] != data.get("intent"):
            raise ValueError("Requested intent does not match run_config.json")
        preflight = run_preflight_queue_summary(root, config)
        if preflight["ready_for_queue"] is not True:
            raise ValueError(
                "A fresh successful preflight is required before capture: "
                + str(preflight.get("queue_blocker"))
            )
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400
    return _submit_recipe(recipe, root)


def submit_dataset_processing_job():
    try:
        data = _purpose_payload(allowed={"run_root"}, required={"run_root"})
        root = resolve_web_run_root(data["run_root"])
        recipe = dataset_processing_job_recipe(root)
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"output": str(exc)}), 400
    return _submit_recipe(recipe, root)


def robot_commands():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"output": "JSON object required"}), 400
    command = data.get("command")
    profile = robot_profile()
    if command == "start":
        allowed = {
            "command",
            "run_root",
            "allow_real_robot",
            "allow_cameras",
        }
        extra = sorted(set(data) - allowed)
        if extra:
            return jsonify({"output": "Unsupported fields: " + ", ".join(extra)}), 400
        if (
            data.get("allow_real_robot") is not True
            or data.get("allow_cameras") is not True
        ):
            return jsonify(
                {
                    "output": (
                        "Start requires allow_real_robot=true and allow_cameras=true"
                    )
                }
            ), 400
        if not data.get("run_root"):
            return jsonify({"output": "Start requires run_root"}), 400
        try:
            root = resolve_web_run_root(data["run_root"])
            config = load_run_config_for_run_root(root)
        except (OSError, ValueError) as exc:
            return jsonify({"output": str(exc)}), 400
        command_array = [
            "uv",
            "run",
            "python",
            "start_iiwa.py",
            "--run-id",
            config["run_id"],
            "--manual-test-speed",
            "--allow-real-robot",
            "--allow-cameras",
        ]
        parameters = {
            "purpose": "robot_command",
            "command": "start",
            "run_root": root.as_posix(),
            "run_id": config["run_id"],
            "allow_real_robot": True,
            "allow_cameras": True,
            "commanded_velocity_m_s": MANUAL_TEST_COMMAND_VELOCITY_M_S,
        }
        scope_kind = "run"
        run_root = root
    elif command == "stop":
        allowed = {"command", "confirm_idle_program_exit"}
        extra = sorted(set(data) - allowed)
        if extra:
            return jsonify({"output": "Unsupported fields: " + ", ".join(extra)}), 400
        if data.get("confirm_idle_program_exit") is not True:
            return jsonify(
                {"output": "Stop requires confirm_idle_program_exit=true"}
            ), 400
        command_array = ["uv", "run", "python", "stop_iiwa.py"]
        parameters = {
            "purpose": "robot_command",
            "command": "stop",
            "confirm_idle_program_exit": True,
        }
        scope_kind = "global"
        run_root = None
    else:
        return jsonify({"output": "command must be start or stop"}), 400

    try:
        job = job_runner.submit(
            name=f"IIWA {command}",
            command=command_array,
            cwd=APP_ROOT,
            resources=["robot_command"],
            parameters=parameters,
            scope_kind=scope_kind,
            run_root=run_root,
        )
    except ResourceBusyError as exc:
        return jsonify({"output": str(exc)}), 409
    return jsonify(
        {
            "output": f"Queued IIWA {command} as job {job.id}",
            "job_id": job.id,
            "status": job.status,
            "target": {
                "robot_ip": profile.robot_ip,
                "command_port": profile.command_port,
            },
            "job": job.to_dict(),
        }
    ), 202


def list_jobs():
    include_services = request.args.get("include_services", "").lower() in {
        "1",
        "true",
    }
    try:
        limit = int(request.args.get("limit", 50))
        statuses = {
            item.strip().lower()
            for value in request.args.getlist("status")
            for item in value.split(",")
            if item.strip()
        }
        expanded: set[str] = set()
        for status in statuses:
            if status == "active":
                expanded.update(ACTIVE_JOB_STATUSES)
            elif status in {"finished", "terminal"}:
                expanded.update(TERMINAL_STATUSES)
            else:
                expanded.add(status)
        scopes = {
            item.strip().lower()
            for key in ("scope_kind", "scope")
            for value in request.args.getlist(key)
            for item in value.split(",")
            if item.strip()
        }
        page = job_runner.list_page(
            limit=limit,
            cursor=request.args.get("cursor") or None,
            search=request.args.get("search") or None,
            statuses=expanded or None,
            scope_kinds=scopes or None,
            run_root=request.args.get("run_root") or None,
            include_services=include_services,
        )
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify(
        {
            "jobs": [job.to_dict() for job in page.jobs],
            "resources": job_runner.resource_holders(include_services=include_services),
            "total": page.total,
            "status_counts": page.status_counts,
            "next_cursor": page.next_cursor,
            "limit": limit,
        }
    )


def get_job(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    return jsonify({"job": job.to_dict()})


def get_job_log(job_id: str):
    try:
        text = job_runner.log_text(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    return Response(text, mimetype="text/plain")


def cancel_job(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    if (
        job.status not in TERMINAL_STATUSES
        and (job.parameters or {}).get("cancelable") is False
    ):
        return jsonify({"output": "This operation cannot be canceled safely."}), 409
    return jsonify({"job": job_runner.cancel(job_id).to_dict()})


def _capture_job_summary(job) -> dict[str, Any]:
    active = job.status in ACTIVE_JOB_STATUSES
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "kind": "capture",
        "intent": (job.parameters or {}).get("intent"),
        "run_root": job.run_root,
        "resources": list(job.resources or []),
        "message": job.message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "returncode": job.returncode,
        "active": active,
        "tail": list(job.tail or []),
        "log_endpoint": f"/jobs/{job.id}/log",
        "stop_endpoint": f"/capture/jobs/{job.id}/stop" if active else None,
    }


def _capture_jobs_for_run(run_root: str | Path | None) -> list[dict[str, Any]]:
    result = []
    for job in job_runner.list():
        if (job.parameters or {}).get("purpose") != "capture":
            continue
        if run_root is not None and (
            not job.run_root or not _same_path(job.run_root, run_root)
        ):
            continue
        result.append(_capture_job_summary(job))
    return result


def list_capture_jobs():
    raw_run_root = request.args.get("run_root") or None
    try:
        run_root = resolve_web_run_root(raw_run_root) if raw_run_root else None
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    jobs = _capture_jobs_for_run(run_root)
    status_artifact = None
    if run_root:
        try:
            status_artifact = load_capture_execution_status(run_root)
        except FileNotFoundError:
            pass
        except ValueError as exc:
            status_artifact = {"error": str(exc)}
    return jsonify(
        {
            "run_root": run_root.as_posix() if run_root else None,
            "jobs": jobs,
            "active_count": sum(1 for job in jobs if job["active"]),
            "resources": job_runner.resource_holders(),
            "status_artifact": status_artifact,
        }
    )


def capture_execution_status():
    run_root = request.args.get("run_root")
    if not run_root:
        return jsonify({"output": "Missing run_root"}), 400
    try:
        root = resolve_web_run_root(run_root)
        status = load_capture_execution_status(root)
    except FileNotFoundError:
        return jsonify({"output": f"Missing {CAPTURE_EXECUTION_STATUS}"}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify({"run_root": root.as_posix(), "status": status})


def stop_capture_job(job_id: str):
    try:
        job = job_runner.get(job_id)
    except KeyError:
        return jsonify({"output": "Unknown job"}), 404
    if (job.parameters or {}).get("purpose") != "capture":
        return jsonify({"output": "Job is not a capture job"}), 400
    job = job_runner.cancel(job_id)
    return jsonify(
        {
            "output": (
                "Cancel requested. Camera children will be cleaned up; this does "
                "not send the iiwa idle-program exit command and cannot interrupt motion."
            ),
            "job": job.to_dict(),
            "capture_job": _capture_job_summary(job),
        }
    )


def sensor_adapters():
    return jsonify({"adapters": list_sensor_adapters()})


def runtime_status():
    return jsonify(collect_runtime_status())


def robot_status():
    return jsonify(collect_robot_status())


def hardware_status():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data.get("run_root"):
            return jsonify({"output": "run_root is required"}), 400
        try:
            path, report = write_hardware_status_report_with_manifest(
                data["run_root"],
                include_sensor_status=data.get("include_sensors", True) is True,
                include_runtime_status=data.get("include_runtimes", True) is True,
            )
        except ValueError as exc:
            return jsonify({"output": str(exc)}), 400
        return jsonify(
            {
                "output": f"Wrote {path}",
                "path": path.as_posix(),
                "run_root": str(Path(data["run_root"])),
                "report": report,
            }
        ), 201 if report["overall_status"] != "error" else 409
    run_root = request.args.get("run_root")
    if not run_root:
        return jsonify({"output": "Missing run_root"}), 400
    try:
        report = load_hardware_status_report(run_root)
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify({"run_root": str(Path(run_root)), "report": report})


def sync_quality_endpoint():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not data.get("run_root"):
            return jsonify({"output": "run_root is required"}), 400
        try:
            path, report = write_sync_quality_report_with_manifest(data["run_root"])
        except FileNotFoundError as exc:
            return jsonify({"output": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"output": str(exc)}), 400
        return jsonify(
            {
                "output": f"Wrote {path}",
                "path": path.as_posix(),
                "run_root": str(Path(data["run_root"])),
                "report": report,
            }
        ), 201 if report["overall_status"] != "error" else 409
    run_root = request.args.get("run_root")
    if not run_root:
        return jsonify({"output": "Missing run_root"}), 400
    try:
        report = build_sync_quality_report(run_root)
    except FileNotFoundError as exc:
        return jsonify({"output": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"output": str(exc)}), 400
    return jsonify({"run_root": str(Path(run_root)), "report": report})
