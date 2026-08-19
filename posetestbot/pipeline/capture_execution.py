"""Capture execution planning from a validated capture plan."""

from __future__ import annotations

import json
import math
import os
import signal
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import (
    CAPTURE_EXECUTION_LOGS_DIR,
    CAPTURE_EXECUTION_PLAN,
    CAPTURE_EXECUTION_REPORT,
    CAPTURE_EXECUTION_STATUS,
    CAPTURE_PLAN,
    FRAME_METADATA_JSONL,
    RAW_ROBOT_EE_POSES,
)
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.pipeline.capture_plan import (
    build_capture_plan,
    capture_plan_build_options,
    load_capture_plan,
)
from posetestbot.pipeline.capture_plan_preflight import build_capture_plan_preflight
from posetestbot.pipeline.capture_completion import build_capture_completion
from posetestbot.pipeline.run_config import (
    load_run_config_for_run_root,
    run_config_lock,
)
from posetestbot.robot.pose_receiver import (
    CLAIM_SCHEMA_VERSION,
    DEFAULT_RECEIVE_IDLE_TIMEOUT_S,
    DEFAULT_RECEIVE_START_TIMEOUT_S,
)
from posetestbot.sensors.status import collect_sensor_status


SCHEMA_VERSION = "capture_execution_plan.v1"
STATUS_SCHEMA_VERSION = "capture_execution_status.v1"
REPORT_SCHEMA_VERSION = "capture_execution_report.v1"
DEFAULT_CAPTURE_EXECUTION_TIMEOUT_S = 720.0
DEFAULT_CAMERA_READINESS_TIMEOUT_S = 15.0
DEFAULT_CAMERA_STARTUP_ATTEMPTS = 3
DEFAULT_CAMERA_STARTUP_RETRY_DELAY_S = 1.0
MIN_CAMERA_READINESS_RECORDS = 3
RECEIVER_MONITOR_INTERVAL_S = 0.1
EXECUTION_ONLY_RECEIVER_FLAGS = frozenset(
    {
        "--allow-cameras",
        "--allow-real-robot",
        "--receive-start-timeout-s",
        "--receive-idle-timeout-s",
    }
)


class CaptureExecutionCanceled(RuntimeError):
    """Raised by supervisor signal handlers to trigger complete cleanup."""


class CaptureExecutionPermissionError(RuntimeError):
    """Raised before any mutation when execution acknowledgements are absent."""


CAPTURE_CANCELLATION_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _capture_cancellation_error(signum: int) -> CaptureExecutionCanceled:
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    return CaptureExecutionCanceled(f"Capture execution canceled by {signal_name}.")


def _pthread_sigmask(how: int, mask: set[signal.Signals]) -> set[signal.Signals] | None:
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if pthread_sigmask is None:
        return None
    try:
        return set(pthread_sigmask(how, mask))
    except (OSError, ValueError):
        return None


@contextmanager
def _defer_capture_cancellation():
    """Defer cancellation until a newly spawned child is registered.

    POSIX signals are blocked only while handlers are swapped. They are unblocked
    before ``Popen`` so the child inherits the normal signal mask, while the
    parent temporarily records SIGINT/SIGTERM instead of raising asynchronously.
    On exit the original handlers are restored before the mask is restored.
    """

    deferred_signals: list[int] = []
    previous_handlers: dict[signal.Signals, Any] = {}
    signal_set = set(CAPTURE_CANCELLATION_SIGNALS)
    previous_mask = _pthread_sigmask(signal.SIG_BLOCK, signal_set)

    def defer(signum: int, _frame: Any) -> None:
        deferred_signals.append(signum)

    try:
        for deferred_signal in CAPTURE_CANCELLATION_SIGNALS:
            try:
                previous_handlers[deferred_signal] = signal.getsignal(deferred_signal)
                signal.signal(deferred_signal, defer)
            except (OSError, ValueError):
                previous_handlers.pop(deferred_signal, None)
    finally:
        if previous_mask is not None:
            _pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    body_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
    finally:
        restore_mask = _pthread_sigmask(signal.SIG_BLOCK, signal_set)
        try:
            for deferred_signal, previous_handler in previous_handlers.items():
                signal.signal(deferred_signal, previous_handler)
        finally:
            if restore_mask is not None:
                _pthread_sigmask(signal.SIG_SETMASK, restore_mask)

    if deferred_signals:
        cancellation = _capture_cancellation_error(deferred_signals[0])
        if body_error is not None:
            raise cancellation from body_error
        raise cancellation
    if body_error is not None:
        raise body_error


@dataclass(frozen=True)
class CaptureExecutionGate:
    """One readiness or operator-intent gate for capture execution."""

    name: str
    status: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["details"] = dict(self.details)
        return data


@dataclass(frozen=True)
class CaptureProcessRecord:
    """Execution metadata for one selected capture command."""

    role: str
    name: str
    command: list[str]
    command_text: str
    startup_order: int
    log_file: str
    pid: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    elapsed_s: float | None = None
    returncode: int | None = None
    status: str = "planned"
    termination_reason: str | None = None
    startup_attempt: int | None = None
    startup_attempt_limit: int | None = None
    readiness_record_count: int | None = None
    output_mutated: bool | None = None
    output_tail: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_tail"] = list(self.output_tail)
        return data


@dataclass(frozen=True)
class CaptureExecutionBoundary:
    """Read-only inputs validated before execution creates any artifacts."""

    expected_receiver_command: tuple[str, ...]
    expected_command_fingerprints: tuple[str, ...]
    sensor_output_paths: tuple[Path, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _overall_status(gates: list[CaptureExecutionGate]) -> str:
    statuses = {gate.status for gate in gates}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


def _command_with_metadata(command: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    data = dict(command)
    data["plan_index"] = index
    command_array = data.get("command")
    if isinstance(command_array, list) and all(
        isinstance(item, str) for item in command_array
    ):
        data["command_text"] = shlex.join(command_array)
    return data


def _resources(commands: list[Mapping[str, Any]]) -> list[str]:
    resources: set[str] = set()
    for command in commands:
        for resource in command.get("resources", []):
            if isinstance(resource, str):
                resources.add(resource)
    return sorted(resources)


def _safe_log_stem(command: Mapping[str, Any], *, index: int) -> str:
    name = str(command.get("name") or command.get("role") or f"command_{index}")
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return f"{index:02d}_{safe or 'command'}"


def _tail(path: Path, limit: int = 40) -> tuple[str, ...]:
    if not path.is_file():
        return ()
    return tuple(path.read_text(errors="replace").splitlines()[-limit:])


def _process_elapsed_s(info: Mapping[str, Any]) -> float | None:
    started = info.get("started_monotonic")
    if not isinstance(started, (int, float)):
        return None
    ended = info.get("ended_monotonic")
    if not isinstance(ended, (int, float)):
        ended = time.monotonic()
    return max(0.0, ended - started)


def _mark_process_ended(info: dict[str, Any]) -> None:
    if info.get("ended_at") is None:
        info["ended_at"] = _now()
    if info.get("ended_monotonic") is None:
        info["ended_monotonic"] = time.monotonic()


def _premature_camera_exit(
    background_processes: list[dict[str, Any]],
) -> RuntimeError | None:
    for info in background_processes:
        process = info["process"]
        returncode = process.poll()
        if returncode is None:
            continue
        _mark_process_ended(info)
        info["returncode"] = returncode
        info["status"] = "failed"
        info["termination_reason"] = "camera_exited_while_receiver_active"
        return RuntimeError(
            "Camera capture command exited before the robot pose receiver "
            f"completed: {info['command'].get('name')} (status {returncode})."
        )
    return None


def _camera_startup_exit(
    background_processes: list[dict[str, Any]],
) -> RuntimeError | None:
    for info in background_processes:
        process = info["process"]
        returncode = process.poll()
        if returncode is None:
            continue
        _mark_process_ended(info)
        info["returncode"] = returncode
        info["status"] = "failed"
        info["termination_reason"] = "exited_before_receiver_start"
        return RuntimeError(
            "Camera capture command exited before first-frame readiness: "
            f"{info['command'].get('name')} (status {returncode})."
        )
    return None


def _missing_sensor_readiness(
    sensor_output_paths: tuple[Path, ...],
) -> list[Path]:
    missing = []
    for output_path in sensor_output_paths:
        metadata_path = output_path / FRAME_METADATA_JSONL
        ready = (
            _valid_frame_metadata_record_count(metadata_path)
            >= MIN_CAMERA_READINESS_RECORDS
        )
        if not ready:
            missing.append(metadata_path)
    return missing


def _valid_frame_metadata_record_count(path: Path) -> int:
    """Count complete JSONL records that satisfy the shared frame contract."""

    return len(_valid_frame_metadata_records(path))


def _valid_frame_metadata_records(
    path: Path,
    *,
    limit: int | None = MIN_CAMERA_READINESS_RECORDS,
) -> list[Mapping[str, Any]]:
    """Read the first committed records satisfying the shared frame contract."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = list(handle)
    except (FileNotFoundError, OSError, UnicodeError):
        return []

    records: list[Mapping[str, Any]] = []
    for line in lines:
        # A writer may be appending while the supervisor reads.  Only a
        # newline-terminated record has completed the JSONL append contract.
        if not line.endswith("\n"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            continue
        frame_index = value.get("frame_index")
        host_received_timestamp_ns = value.get("host_received_timestamp_ns")
        if (
            value.get("schema_version") != "frame_metadata.v1"
            or isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or frame_index < 0
            or isinstance(host_received_timestamp_ns, bool)
            or not isinstance(host_received_timestamp_ns, int)
            or host_received_timestamp_ns <= 0
            or not isinstance(value.get("sensor_id"), str)
            or not value["sensor_id"]
            or not isinstance(value.get("frame_id"), str)
            or not value["frame_id"]
            or not isinstance(value.get("rgb_path"), str)
            or not value["rgb_path"]
            or not isinstance(value.get("depth_path"), str)
            or not value["depth_path"]
        ):
            continue
        records.append(value)
        if limit is not None and len(records) >= limit:
            break
    return records


def _sensor_output_has_mutation(output_path: Path) -> bool:
    """Return whether a startup attempt left any raw sensor evidence.

    The execution boundary requires every output path to be absent. Therefore
    even an empty directory is attempt-owned mutation and blocks an automatic
    retry. This strict check prevents a later child from mixing with or replacing
    partial evidence whose writer may have failed before committing metadata.
    """

    return os.path.lexists(output_path)


def _sensor_output_path(command: Mapping[str, Any]) -> Path:
    raw_output = command.get("output_folder")
    if not isinstance(raw_output, str) or not raw_output:
        raise ValueError("Every sensor_capture command requires output_folder")
    output_path = Path(raw_output)
    return output_path if output_path.is_absolute() else Path.cwd() / output_path


def _raw_pose_count(run_root: Path) -> int:
    path = run_root / RAW_ROBOT_EE_POSES
    if not path.is_file():
        return 0
    with open(path, "r") as f:
        value = json.load(f)
    if isinstance(value, dict) and value.get("schema_version") == CLAIM_SCHEMA_VERSION:
        return 0
    return len(value) if isinstance(value, dict) else 0


def _receiver_command_from_plan(plan: Mapping[str, Any]) -> tuple[str, ...]:
    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise ValueError("Capture plan commands must be a list")
    receivers = [
        command
        for command in commands
        if isinstance(command, Mapping) and command.get("role") == "robot_pose_receiver"
    ]
    if len(receivers) != 1:
        raise ValueError(
            "Capture plan must contain exactly one robot_pose_receiver command; "
            f"found {len(receivers)}."
        )
    return tuple(_command_array(receivers[0]))


def _capture_command_fingerprints(plan: Mapping[str, Any]) -> tuple[str, ...]:
    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise ValueError("Capture plan commands must be a list")
    fingerprints = []
    for command in commands:
        if not isinstance(command, Mapping):
            raise ValueError("Every capture plan command must be an object")
        canonical = {
            key: value for key, value in command.items() if key != "plan_index"
        }
        fingerprints.append(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        )
    return tuple(sorted(fingerprints))


def _sensor_output_paths_from_plan(
    plan: Mapping[str, Any],
    *,
    run_root: Path,
) -> tuple[Path, ...]:
    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise ValueError("Capture plan commands must be a list")
    root_resolved = run_root.resolve()
    output_paths: list[Path] = []
    for command in commands:
        if not isinstance(command, Mapping) or command.get("role") != "sensor_capture":
            continue
        raw_output = command.get("output_folder")
        if not isinstance(raw_output, str) or not raw_output:
            raise ValueError("Every sensor_capture command requires output_folder")
        output_path = Path(raw_output)
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path
        try:
            output_path.resolve().relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError(
                f"Sensor output folder escapes the run root: {raw_output}"
            ) from exc
        output_paths.append(output_path)
    if len(output_paths) != len(set(path.resolve() for path in output_paths)):
        raise ValueError("Planned sensor output folders must be unique")
    return tuple(output_paths)


def _assert_capture_outputs_absent(
    run_root: Path,
    sensor_output_paths: tuple[Path, ...],
) -> None:
    blockers = []
    raw_pose_path = run_root / RAW_ROBOT_EE_POSES
    if os.path.lexists(raw_pose_path):
        blockers.append(raw_pose_path.as_posix())
    blockers.extend(
        path.as_posix() for path in sensor_output_paths if os.path.lexists(path)
    )
    if blockers:
        raise FileExistsError(
            "Capture execution requires unused raw output paths; already present: "
            + ", ".join(blockers)
        )


def _validate_capture_execution_boundary(
    run_root: Path,
) -> CaptureExecutionBoundary:
    """Perform only read-only checks before supervisor artifact creation."""

    config = load_run_config_for_run_root(run_root)
    capture = config.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("Run configuration capture must be an object")
    plan_path = run_root / CAPTURE_PLAN
    persisted_plan: dict[str, Any] | None = None
    build_options: dict[str, int | None] = {}
    if plan_path.is_file():
        persisted_plan = load_capture_plan(run_root)
        build_options = capture_plan_build_options(persisted_plan)
    elif os.path.lexists(plan_path):
        raise ValueError(f"Capture plan path is not a regular file: {plan_path}")

    expected_plan = build_capture_plan(config, **build_options).to_dict()
    expected_receiver = _receiver_command_from_plan(expected_plan)
    expected_fingerprints = _capture_command_fingerprints(expected_plan)
    expected_prefix = (
        "uv",
        "run",
        "python",
        "scripts/pose_receiver_udp_json.py",
        str(config["run_root"]),
    )
    if expected_receiver[:5] != expected_prefix:
        raise ValueError(
            "Generated receiver command does not use the hardened receiver contract."
        )

    if persisted_plan is not None:
        persisted_receiver = _receiver_command_from_plan(persisted_plan)
        persisted_fingerprints = _capture_command_fingerprints(persisted_plan)
        if (
            persisted_receiver != expected_receiver
            or persisted_fingerprints != expected_fingerprints
        ):
            raise ValueError(
                "Persisted capture commands do not exactly match the canonical "
                "commands generated from the fresh run configuration."
            )
    sensor_output_paths = _sensor_output_paths_from_plan(
        expected_plan,
        run_root=run_root,
    )
    _assert_capture_outputs_absent(run_root, sensor_output_paths)
    return CaptureExecutionBoundary(
        expected_receiver_command=expected_receiver,
        expected_command_fingerprints=expected_fingerprints,
        sensor_output_paths=sensor_output_paths,
    )


def _terminate_process_group(
    process: subprocess.Popen,
    *,
    timeout_s: float,
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        process.wait(timeout=timeout_s)


def _preflight_gate(preflight: Mapping[str, Any]) -> CaptureExecutionGate:
    preflight_status = str(preflight.get("overall_status", "error"))
    return CaptureExecutionGate(
        name="capture_plan_preflight",
        status=preflight_status if preflight_status in {"ok", "warning"} else "error",
        message=f"Capture-plan preflight status is {preflight_status}.",
        details={"preflight_status": preflight_status},
    )


def _robot_gate(
    *,
    allow_real_robot: bool,
) -> CaptureExecutionGate:
    return CaptureExecutionGate(
        name="real_robot_permission",
        status="ok" if allow_real_robot is True else "error",
        message=(
            "Real robot execution was explicitly allowed."
            if allow_real_robot is True
            else "Capture execution requires allow_real_robot=true."
        ),
        details={"allow_real_robot": allow_real_robot},
    )


def _camera_gate(
    *,
    allow_cameras: bool,
) -> CaptureExecutionGate:
    return CaptureExecutionGate(
        name="camera_permission",
        status="ok" if allow_cameras is True else "error",
        message=(
            "Camera execution was explicitly allowed."
            if allow_cameras is True
            else "Capture execution requires allow_cameras=true."
        ),
        details={"allow_cameras": allow_cameras},
    )


def _select_full_capture(
    commands: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], CaptureExecutionGate]:
    selected = [
        _command_with_metadata(command, index=index)
        for index, command in enumerate(commands)
    ]
    return (
        selected,
        [],
        CaptureExecutionGate(
            name="command_selection",
            status="ok",
            message="Selected all capture-plan commands for full capture.",
            details={
                "selected_count": len(selected),
                "skipped_count": 0,
            },
        ),
    )


def build_capture_execution_plan(
    run_root: str | Path,
    *,
    allow_cameras: bool = False,
    allow_real_robot: bool = False,
    include_sensor_status: bool | None = None,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    write_plan_if_missing: bool = True,
    camera_startup_attempts: int = DEFAULT_CAMERA_STARTUP_ATTEMPTS,
    camera_startup_retry_delay_s: float = DEFAULT_CAMERA_STARTUP_RETRY_DELAY_S,
) -> dict[str, Any]:
    """Build a non-executing command selection plan for capture startup."""

    if (
        isinstance(camera_startup_attempts, bool)
        or not isinstance(camera_startup_attempts, int)
        or camera_startup_attempts <= 0
    ):
        raise ValueError("camera_startup_attempts must be a positive integer")
    if (
        not math.isfinite(camera_startup_retry_delay_s)
        or camera_startup_retry_delay_s < 0
    ):
        raise ValueError(
            "camera_startup_retry_delay_s must be a finite value greater than or equal to 0"
        )

    run_root_path = Path(run_root)
    if include_sensor_status is None:
        include_sensor_status = True

    preflight = build_capture_plan_preflight(
        run_root_path,
        include_sensor_status=include_sensor_status,
        allow_real_robot=allow_real_robot,
        collect_sensors=collect_sensors,
        write_plan_if_missing=write_plan_if_missing,
    )
    capture_plan = preflight["capture_plan"]
    commands = [
        command
        for command in capture_plan.get("commands", [])
        if isinstance(command, Mapping)
    ]

    selected, skipped, selection_gate = _select_full_capture(commands)
    gates = [
        _robot_gate(allow_real_robot=allow_real_robot),
        _camera_gate(allow_cameras=allow_cameras),
        _preflight_gate(preflight),
        selection_gate,
    ]
    status = _overall_status(gates)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "run_root": run_root_path.as_posix(),
        "mode": "full",
        "status": status,
        "message": (
            "Capture execution plan is ready."
            if status == "ok"
            else (
                "Capture execution plan has warnings."
                if status == "warning"
                else "Capture execution plan is blocked by safety gates."
            )
        ),
        "allow_cameras": allow_cameras,
        "allow_real_robot": allow_real_robot,
        "include_sensor_status": include_sensor_status,
        "ready_to_execute": status == "ok",
        "preflight_status": preflight.get("overall_status"),
        "selected_roles": [
            str(command.get("role"))
            for command in selected
            if isinstance(command.get("role"), str)
        ],
        "selected_resources": _resources(selected),
        "selected_commands": selected,
        "skipped_commands": skipped,
        "gates": [gate.to_dict() for gate in gates],
        "execution_strategy": {
            "supervisor": "planned_process_group",
            "working_directory": ".",
            "start_order": (
                "ascending startup_order then plan_index; start one sensor child "
                "and require its readiness before starting the next"
            ),
            "camera_startup_attempts": camera_startup_attempts,
            "camera_startup_retry_delay_s": camera_startup_retry_delay_s,
            "camera_retry_policy": (
                "Retry only when the current attempt leaves no sensor output "
                "evidence; preserve and fail closed on any partial raw output."
            ),
            "camera_readiness": (
                "Each planned sensor output must publish at least "
                f"{MIN_CAMERA_READINESS_RECORDS} valid committed "
                f"{FRAME_METADATA_JSONL} records before the next sensor starts. "
                "The robot pose receiver starts only after every sensor is ready."
            ),
            "stop_policy": (
                "After robot_pose_receiver exits, terminate remaining selected "
                "camera processes by process group."
            ),
        },
        "capture_plan": capture_plan,
        "preflight_report": preflight,
    }


def capture_execution_plan_path(run_root: str | Path) -> Path:
    return Path(run_root) / CAPTURE_EXECUTION_PLAN


def load_capture_execution_plan(run_root: str | Path) -> dict[str, Any]:
    path = capture_execution_plan_path(run_root)
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Capture execution plan must be a JSON object: {path}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported capture execution plan schema: "
            f"{value.get('schema_version')!r}"
        )
    return value


def write_capture_execution_plan(
    run_root: str | Path,
    plan: Mapping[str, Any],
) -> Path:
    path = capture_execution_plan_path(run_root)
    return atomic_write_json(path, dict(plan))


def write_capture_execution_plan_with_manifest(
    run_root: str | Path,
    *,
    allow_cameras: bool = False,
    allow_real_robot: bool = False,
    include_sensor_status: bool | None = None,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    write_plan_if_missing: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Write ``capture_execution_plan.json`` and record the stage."""

    run_root_path = Path(run_root)
    manifest = load_or_create_run_manifest(run_root_path)
    upsert_stage(manifest, name="capture_execution_plan", status="running")
    write_run_manifest(manifest, run_root_path)
    try:
        plan = build_capture_execution_plan(
            run_root_path,
            allow_cameras=allow_cameras,
            allow_real_robot=allow_real_robot,
            include_sensor_status=include_sensor_status,
            collect_sensors=collect_sensors,
            write_plan_if_missing=write_plan_if_missing,
        )
        path = write_capture_execution_plan(run_root_path, plan)
        config = plan["preflight_report"].get("config", {})
        manifest["robot_profile"] = dict(config.get("robot_profile") or {})
        manifest["capture_config"] = dict(config.get("capture") or {})
        upsert_stage(
            manifest,
            name="capture_execution_plan",
            status="succeeded" if plan["status"] != "error" else "failed",
            artifacts={
                CAPTURE_EXECUTION_PLAN: path,
                CAPTURE_PLAN: run_root_path / CAPTURE_PLAN,
            },
            run_root=run_root_path,
            message=f"Capture execution plan status: {plan['status']}.",
        )
        write_run_manifest(manifest, run_root_path)
    except Exception as exc:
        upsert_stage(
            manifest,
            name="capture_execution_plan",
            status="failed",
            message=str(exc),
        )
        write_run_manifest(manifest, run_root_path)
        raise
    return path, plan


def capture_execution_report_path(run_root: str | Path) -> Path:
    return Path(run_root) / CAPTURE_EXECUTION_REPORT


def capture_execution_status_path(run_root: str | Path) -> Path:
    return Path(run_root) / CAPTURE_EXECUTION_STATUS


def load_capture_execution_status(run_root: str | Path) -> dict[str, Any]:
    path = capture_execution_status_path(run_root)
    with open(path, "r") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Capture execution status must be a JSON object: {path}")
    if value.get("schema_version") != STATUS_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported capture execution status schema: "
            f"{value.get('schema_version')!r}"
        )
    return value


def write_capture_execution_status(
    run_root: str | Path,
    status: Mapping[str, Any],
) -> Path:
    path = capture_execution_status_path(run_root)
    return atomic_write_json(path, dict(status))


def write_capture_execution_report(
    run_root: str | Path,
    report: Mapping[str, Any],
) -> Path:
    path = capture_execution_report_path(run_root)
    return atomic_write_json(path, dict(report))


def _command_array(command: Mapping[str, Any]) -> list[str]:
    value = command.get("command")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Selected command has invalid command array: {command!r}")
    return list(value)


def _process_record(
    command: Mapping[str, Any],
    *,
    log_path: Path,
    pid: int | None,
    started_at: str | None,
    ended_at: str | None,
    elapsed_s: float | None,
    returncode: int | None,
    status: str,
    termination_reason: str | None = None,
    startup_attempt: int | None = None,
    startup_attempt_limit: int | None = None,
    readiness_record_count: int | None = None,
    output_mutated: bool | None = None,
) -> CaptureProcessRecord:
    command_array = _command_array(command)
    return CaptureProcessRecord(
        role=str(command.get("role") or ""),
        name=str(command.get("name") or ""),
        command=command_array,
        command_text=str(command.get("command_text") or shlex.join(command_array)),
        startup_order=int(command.get("startup_order") or 0),
        log_file=log_path.as_posix(),
        pid=pid,
        started_at=started_at,
        ended_at=ended_at,
        elapsed_s=elapsed_s,
        returncode=returncode,
        status=status,
        termination_reason=termination_reason,
        startup_attempt=startup_attempt,
        startup_attempt_limit=startup_attempt_limit,
        readiness_record_count=readiness_record_count,
        output_mutated=output_mutated,
        output_tail=_tail(log_path),
    )


def _status_process_record(info: Mapping[str, Any]) -> dict[str, Any]:
    command = info.get("command")
    if not isinstance(command, Mapping):
        command = {}
    command_array = command.get("command")
    if not isinstance(command_array, list) or not all(
        isinstance(item, str) for item in command_array
    ):
        command_array = []

    process = info.get("process")
    pid = info.get("pid")
    returncode = info.get("returncode")
    active = False
    if process is not None:
        pid = getattr(process, "pid", pid)
        try:
            polled = process.poll()
        except Exception:
            polled = getattr(process, "returncode", None)
        if returncode is None:
            returncode = polled
        active = polled is None and str(info.get("status")) == "running"
    else:
        active = str(info.get("status")) == "running"

    log_path = info.get("log_path")
    output_tail: tuple[str, ...] = ()
    if isinstance(log_path, Path):
        output_tail = _tail(log_path, limit=8)

    return {
        "role": str(command.get("role") or ""),
        "name": str(command.get("name") or ""),
        "command": command_array,
        "command_text": str(command.get("command_text") or shlex.join(command_array)),
        "startup_order": int(command.get("startup_order") or 0),
        "log_file": log_path.as_posix() if isinstance(log_path, Path) else None,
        "pid": pid if isinstance(pid, int) else None,
        "started_at": info.get("started_at"),
        "ended_at": info.get("ended_at"),
        "elapsed_s": _process_elapsed_s(info),
        "status": str(info.get("status") or "unknown"),
        "returncode": returncode,
        "termination_reason": info.get("termination_reason"),
        "startup_attempt": info.get("startup_attempt"),
        "startup_attempt_limit": info.get("startup_attempt_limit"),
        "readiness_record_count": info.get("readiness_record_count"),
        "output_mutated": info.get("output_mutated"),
        "active": active,
        "output_tail": list(output_tail),
    }


def _build_capture_execution_status(
    run_root: Path,
    *,
    status: str,
    message: str,
    allow_cameras: bool,
    allow_real_robot: bool,
    receive_start_timeout_s: float,
    receive_idle_timeout_s: float,
    started_monotonic: float,
    plan: Mapping[str, Any] | None,
    process_infos: list[dict[str, Any]],
    report_path: Path | None = None,
) -> dict[str, Any]:
    process_records = [_status_process_record(info) for info in process_infos]
    active_count = sum(1 for process in process_records if process["active"])
    data = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "generated_at": _now(),
        "run_root": run_root.as_posix(),
        "status": status,
        "message": message,
        "mode": "full",
        "allow_cameras": allow_cameras,
        "allow_real_robot": allow_real_robot,
        "receive_start_timeout_s": receive_start_timeout_s,
        "receive_idle_timeout_s": receive_idle_timeout_s,
        "elapsed_s": time.monotonic() - started_monotonic,
        "active_process_count": active_count,
        "process_count": len(process_records),
        "processes": process_records,
        "raw_pose_artifact": RAW_ROBOT_EE_POSES,
        "raw_pose_count": _raw_pose_count(run_root),
        "capture_execution_plan_artifact": CAPTURE_EXECUTION_PLAN,
        "capture_execution_report_artifact": (
            CAPTURE_EXECUTION_REPORT if report_path is not None else None
        ),
        "log_dir": (run_root / CAPTURE_EXECUTION_LOGS_DIR).as_posix(),
    }
    if isinstance(plan, Mapping):
        data["plan_status"] = plan.get("status")
        data["selected_roles"] = list(plan.get("selected_roles", []))
        data["ready_to_execute"] = bool(plan.get("ready_to_execute", False))
    return data


def _selected_commands_for_execution(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    selected = plan.get("selected_commands", [])
    if not isinstance(selected, list):
        raise ValueError("Capture execution plan selected_commands must be a list")
    commands = [dict(command) for command in selected if isinstance(command, Mapping)]
    return sorted(
        commands,
        key=lambda item: (
            int(item.get("startup_order") or 0),
            int(item.get("plan_index") or 0),
        ),
    )


def _validated_execution_commands(
    plan: Mapping[str, Any],
    *,
    boundary: CaptureExecutionBoundary,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate the accepted plan completely before supervisor mutation."""

    if plan.get("status") != "ok":
        raise RuntimeError(str(plan.get("message") or "Capture execution is blocked."))
    commands = _selected_commands_for_execution(plan)
    if not commands:
        raise RuntimeError("Capture execution plan selected no commands.")
    selected_fingerprints = _capture_command_fingerprints({"commands": commands})
    if selected_fingerprints != boundary.expected_command_fingerprints:
        raise RuntimeError(
            "Selected capture commands do not exactly match the canonical "
            "commands generated from the fresh run configuration."
        )
    receiver_commands = [
        command for command in commands if command.get("role") == "robot_pose_receiver"
    ]
    if len(receiver_commands) != 1:
        raise RuntimeError(
            "Capture execution requires exactly one robot_pose_receiver "
            f"command; found {len(receiver_commands)}."
        )
    receiver_command = receiver_commands[0]
    planned_receiver_array = _command_array(receiver_command)
    if tuple(planned_receiver_array) != boundary.expected_receiver_command:
        raise RuntimeError(
            "Selected robot pose receiver command does not exactly match the "
            "fresh run configuration and hardened receiver contract."
        )
    persisted_execution_flags = sorted(
        set(planned_receiver_array) & EXECUTION_ONLY_RECEIVER_FLAGS
    )
    if persisted_execution_flags:
        raise RuntimeError(
            "Capture plans must not persist receiver execution flags: "
            + ", ".join(persisted_execution_flags)
            + "."
        )
    receiver_order = int(receiver_command.get("startup_order") or 0)
    late_commands = [
        command
        for command in commands
        if command is not receiver_command
        and int(command.get("startup_order") or 0) > receiver_order
    ]
    if late_commands:
        names = ", ".join(str(command.get("name")) for command in late_commands)
        raise RuntimeError(
            "Capture execution requires the pose receiver to be the final "
            f"startup command; later commands violate the plan contract: {names}."
        )
    return commands, receiver_command


def run_capture_execution(
    run_root: str | Path,
    *,
    allow_cameras: bool = False,
    allow_real_robot: bool = False,
    include_sensor_status: bool | None = None,
    timeout_s: float = DEFAULT_CAPTURE_EXECUTION_TIMEOUT_S,
    startup_wait_s: float = DEFAULT_CAMERA_READINESS_TIMEOUT_S,
    camera_startup_attempts: int = DEFAULT_CAMERA_STARTUP_ATTEMPTS,
    camera_startup_retry_delay_s: float = DEFAULT_CAMERA_STARTUP_RETRY_DELAY_S,
    terminate_timeout_s: float = 2.0,
    receive_start_timeout_s: float = DEFAULT_RECEIVE_START_TIMEOUT_S,
    receive_idle_timeout_s: float = DEFAULT_RECEIVE_IDLE_TIMEOUT_S,
    collect_sensors: Callable[[], dict] = collect_sensor_status,
    write_plan_if_missing: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Execute full real capture with process-group supervision."""

    missing_permissions = []
    if allow_cameras is not True:
        missing_permissions.append("allow_cameras=True")
    if allow_real_robot is not True:
        missing_permissions.append("allow_real_robot=True")
    if missing_permissions:
        raise CaptureExecutionPermissionError(
            "Capture execution requires fresh strict acknowledgements before "
            "any filesystem or hardware preparation: "
            + ", ".join(missing_permissions)
            + "."
        )
    for name, value in (
        ("timeout_s", timeout_s),
        ("terminate_timeout_s", terminate_timeout_s),
        ("receive_start_timeout_s", receive_start_timeout_s),
        ("receive_idle_timeout_s", receive_idle_timeout_s),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite value greater than 0")
    if not math.isfinite(startup_wait_s) or startup_wait_s < 0:
        raise ValueError(
            "startup_wait_s must be a finite value greater than or equal to 0"
        )
    if (
        isinstance(camera_startup_attempts, bool)
        or not isinstance(camera_startup_attempts, int)
        or camera_startup_attempts <= 0
    ):
        raise ValueError("camera_startup_attempts must be a positive integer")
    if (
        not math.isfinite(camera_startup_retry_delay_s)
        or camera_startup_retry_delay_s < 0
    ):
        raise ValueError(
            "camera_startup_retry_delay_s must be a finite value greater than or equal to 0"
        )

    run_root_path = Path(run_root)
    with run_config_lock(run_root_path):
        boundary = _validate_capture_execution_boundary(run_root_path)
        plan = build_capture_execution_plan(
            run_root_path,
            allow_cameras=allow_cameras,
            allow_real_robot=allow_real_robot,
            include_sensor_status=include_sensor_status,
            collect_sensors=collect_sensors,
            write_plan_if_missing=write_plan_if_missing,
            camera_startup_attempts=camera_startup_attempts,
            camera_startup_retry_delay_s=camera_startup_retry_delay_s,
        )
        commands, receiver_command = _validated_execution_commands(
            plan,
            boundary=boundary,
        )
        # Sensor discovery can take time. Recheck under the shared run-config
        # transaction before publishing the first durable execution evidence.
        _assert_capture_outputs_absent(
            run_root_path,
            boundary.sensor_output_paths,
        )

        logs_dir = run_root_path / CAPTURE_EXECUTION_LOGS_DIR
        logs_dir.mkdir(parents=True, exist_ok=True)
        plan_path = write_capture_execution_plan(run_root_path, plan)
        manifest = load_or_create_run_manifest(run_root_path)
        upsert_stage(manifest, name="capture_execution", status="running")
        write_run_manifest(manifest, run_root_path)

    started_monotonic = time.monotonic()
    process_infos: list[dict[str, Any]] = []
    background_processes: list[dict[str, Any]] = []
    status = "succeeded"
    message = "Capture execution completed successfully."
    report_path: Path | None = None

    def record_status(status_value: str, message_value: str) -> Path:
        return write_capture_execution_status(
            run_root_path,
            _build_capture_execution_status(
                run_root_path,
                status=status_value,
                message=message_value,
                allow_cameras=allow_cameras,
                allow_real_robot=allow_real_robot,
                receive_start_timeout_s=receive_start_timeout_s,
                receive_idle_timeout_s=receive_idle_timeout_s,
                started_monotonic=started_monotonic,
                plan=plan,
                process_infos=process_infos,
                report_path=report_path,
            ),
        )

    status_path = record_status("starting", "Capture execution supervisor starting.")

    def cleanup_processes(reason: str) -> None:
        for info in process_infos:
            process = info.get("process")
            if process is None:
                if info.get("status") in {"starting", "running"}:
                    _mark_process_ended(info)
                    info["status"] = (
                        "canceled" if reason == "cancellation_cleanup" else "failed"
                    )
                    info["termination_reason"] = f"not_spawned_during_{reason}"
            elif process.poll() is None:
                preserve_failure = (
                    info.get("status") == "failed"
                    and isinstance(info.get("termination_reason"), str)
                    and bool(info["termination_reason"])
                )
                _terminate_process_group(process, timeout_s=terminate_timeout_s)
                _mark_process_ended(info)
                if not preserve_failure:
                    info["status"] = "terminated"
                    info["termination_reason"] = reason
            elif info.get("status") in {"starting", "running"}:
                _mark_process_ended(info)
                info["status"] = "succeeded" if process.returncode == 0 else "failed"
                info["termination_reason"] = f"exited_during_{reason}"
            log_file = info.get("log_file")
            if log_file is not None and not log_file.closed:
                log_file.close()

    previous_signal_handlers: dict[int, Any] = {}

    def cancel_from_signal(signum: int, _frame: Any) -> None:
        raise _capture_cancellation_error(signum)

    for supervisor_signal in CAPTURE_CANCELLATION_SIGNALS:
        try:
            previous_signal_handlers[supervisor_signal] = signal.getsignal(
                supervisor_signal
            )
            signal.signal(supervisor_signal, cancel_from_signal)
        except (ValueError, OSError):
            previous_signal_handlers.pop(supervisor_signal, None)

    try:
        record_status("planning", "Capture execution plan accepted.")

        # Close the final gap between preflight and the first child process.
        _assert_capture_outputs_absent(
            run_root_path,
            boundary.sensor_output_paths,
        )

        sensor_commands = [
            (index, command)
            for index, command in enumerate(commands)
            if command is not receiver_command
        ]
        for sensor_position, (index, command) in enumerate(
            sensor_commands,
            start=1,
        ):
            output_path = _sensor_output_path(command)
            metadata_path = output_path / FRAME_METADATA_JSONL
            command_name = str(command.get("name") or f"sensor_{sensor_position}")
            sensor_ready = False

            for startup_attempt in range(1, camera_startup_attempts + 1):
                prior_error = _camera_startup_exit(background_processes)
                if prior_error is not None:
                    raise prior_error

                command_array = _command_array(command)
                log_stem = _safe_log_stem(command, index=index)
                log_path = logs_dir / (f"{log_stem}_attempt_{startup_attempt:02d}.log")
                log_file = open(log_path, "w", buffering=1)
                log_file.write(f"$ {shlex.join(command_array)}\n")
                info: dict[str, Any] = {
                    "command": command,
                    "log_path": log_path,
                    "log_file": log_file,
                    "process": None,
                    "pid": None,
                    "started_at": _now(),
                    "started_monotonic": time.monotonic(),
                    "ended_at": None,
                    "ended_monotonic": None,
                    "status": "starting",
                    "termination_reason": None,
                    "startup_attempt": startup_attempt,
                    "startup_attempt_limit": camera_startup_attempts,
                    "readiness_record_count": 0,
                    "output_mutated": False,
                }
                process_infos.append(info)
                try:
                    with _defer_capture_cancellation():
                        process = subprocess.Popen(
                            command_array,
                            cwd=_repo_root(),
                            env=os.environ.copy(),
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                            text=True,
                            start_new_session=(os.name != "nt"),
                        )
                        info["process"] = process
                        info["pid"] = getattr(process, "pid", None)
                except CaptureExecutionCanceled:
                    raise
                except Exception as exc:
                    if info.get("process") is not None:
                        raise
                    log_file.write(
                        "Supervisor could not spawn capture child: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    info["status"] = "failed"
                    info["termination_reason"] = "startup_spawn_failed"
                    info["output_mutated"] = _sensor_output_has_mutation(output_path)
                    _mark_process_ended(info)
                    log_file.close()
                    if (
                        not info["output_mutated"]
                        and startup_attempt < camera_startup_attempts
                    ):
                        record_status(
                            "starting",
                            f"Camera {command_name} startup attempt "
                            f"{startup_attempt}/{camera_startup_attempts} could not "
                            "spawn and left no output evidence; retrying.",
                        )
                        time.sleep(camera_startup_retry_delay_s)
                        continue
                    if info["output_mutated"]:
                        raise RuntimeError(
                            f"Camera {command_name} startup failed and produced "
                            f"sensor output at {output_path}; preserving partial "
                            "raw evidence and refusing automatic retry."
                        ) from exc
                    raise RuntimeError(
                        f"Camera {command_name} exhausted "
                        f"{camera_startup_attempts} startup attempt(s) while "
                        f"spawning the capture child: {type(exc).__name__}: {exc}"
                    ) from exc

                info["status"] = "running"
                record_status(
                    "starting",
                    f"Started camera {sensor_position}/{len(sensor_commands)} "
                    f"({command_name}), startup attempt "
                    f"{startup_attempt}/{camera_startup_attempts}; waiting for "
                    "its sustained frame metadata before starting the next camera.",
                )

                readiness_deadline = time.monotonic() + startup_wait_s
                retry_current = False
                while True:
                    prior_error = _camera_startup_exit(background_processes)
                    if prior_error is not None:
                        raise prior_error

                    returncode = process.poll()
                    record_count = _valid_frame_metadata_record_count(metadata_path)
                    info["readiness_record_count"] = record_count
                    if returncode is not None:
                        _mark_process_ended(info)
                        info["returncode"] = returncode
                        info["status"] = "failed"
                        info["output_mutated"] = _sensor_output_has_mutation(
                            output_path
                        )
                        log_file.close()
                        if (
                            not info["output_mutated"]
                            and startup_attempt < camera_startup_attempts
                        ):
                            info["termination_reason"] = "startup_exit_retry"
                            record_status(
                                "starting",
                                f"Camera {command_name} startup attempt "
                                f"{startup_attempt}/{camera_startup_attempts} "
                                f"exited with status {returncode} and left no "
                                "output evidence; retrying.",
                            )
                            time.sleep(camera_startup_retry_delay_s)
                            retry_current = True
                            break
                        if info["output_mutated"]:
                            info["termination_reason"] = (
                                "startup_partial_output_no_retry"
                            )
                            raise RuntimeError(
                                "Camera capture command exited before first-frame "
                                f"readiness: {command_name} (status {returncode}) "
                                f"after publishing {record_count} valid record(s); "
                                f"preserving partial raw evidence at {output_path} "
                                "and refusing automatic retry."
                            )
                        info["termination_reason"] = "exited_before_receiver_start"
                        raise RuntimeError(
                            "Camera capture command exited before first-frame "
                            f"readiness: {command_name} (status {returncode}); "
                            f"exhausted {camera_startup_attempts} startup attempt(s)."
                        )

                    if record_count >= MIN_CAMERA_READINESS_RECORDS:
                        info["output_mutated"] = True
                        info["termination_reason"] = "camera_ready"
                        background_processes.append(info)
                        sensor_ready = True
                        record_status(
                            "starting",
                            f"Camera {sensor_position}/{len(sensor_commands)} "
                            f"({command_name}) is ready after startup attempt "
                            f"{startup_attempt}/{camera_startup_attempts}; "
                            f"observed {record_count} valid committed records.",
                        )
                        break

                    remaining_s = readiness_deadline - time.monotonic()
                    if remaining_s <= 0:
                        _terminate_process_group(
                            process,
                            timeout_s=terminate_timeout_s,
                        )
                        _mark_process_ended(info)
                        info["returncode"] = process.returncode
                        info["status"] = "stopped"
                        record_count = _valid_frame_metadata_record_count(metadata_path)
                        info["readiness_record_count"] = record_count
                        info["output_mutated"] = _sensor_output_has_mutation(
                            output_path
                        )
                        log_file.close()
                        if (
                            not info["output_mutated"]
                            and startup_attempt < camera_startup_attempts
                        ):
                            info["termination_reason"] = (
                                "startup_readiness_timeout_retry"
                            )
                            record_status(
                                "starting",
                                f"Camera {command_name} startup attempt "
                                f"{startup_attempt}/{camera_startup_attempts} "
                                "timed out and left no output evidence; retrying.",
                            )
                            time.sleep(camera_startup_retry_delay_s)
                            retry_current = True
                            break
                        if info["output_mutated"]:
                            info["termination_reason"] = (
                                "startup_partial_output_no_retry"
                            )
                            raise RuntimeError(
                                "Camera readiness deadline expired before robot "
                                f"START; {command_name} published {record_count} "
                                f"valid committed {FRAME_METADATA_JSONL} record(s), "
                                "instead of at least "
                                f"{MIN_CAMERA_READINESS_RECORDS} valid committed "
                                "records. "
                                f"Preserving partial raw evidence at {output_path} "
                                "and refusing automatic retry."
                            )
                        info["termination_reason"] = "startup_attempts_exhausted"
                        raise RuntimeError(
                            "Camera readiness deadline expired before robot START; "
                            f"{command_name} exhausted {camera_startup_attempts} "
                            "startup attempt(s) without publishing at least "
                            f"{MIN_CAMERA_READINESS_RECORDS} valid committed "
                            f"{FRAME_METADATA_JSONL} records."
                        )

                    time.sleep(min(RECEIVER_MONITOR_INTERVAL_S, remaining_s))

                if sensor_ready:
                    break
                if retry_current:
                    continue

            if not sensor_ready:
                raise RuntimeError(
                    f"Camera {command_name} did not satisfy startup readiness."
                )

        startup_error = _camera_startup_exit(background_processes)
        if startup_error is not None:
            raise startup_error
        missing_readiness = _missing_sensor_readiness(boundary.sensor_output_paths)
        if missing_readiness:
            raise RuntimeError(
                "Camera readiness changed before robot START; missing sustained "
                f"{FRAME_METADATA_JSONL} evidence: "
                + ", ".join(path.as_posix() for path in missing_readiness)
                + "."
            )
        record_status(
            "running",
            "Every camera published sustained frame metadata; receiver may start.",
        )

        receiver_array = _command_array(receiver_command)
        receiver_array.extend(
            [
                "--allow-cameras",
                "--allow-real-robot",
                "--receive-start-timeout-s",
                str(receive_start_timeout_s),
                "--receive-idle-timeout-s",
                str(receive_idle_timeout_s),
            ]
        )
        runtime_receiver_command = dict(receiver_command)
        runtime_receiver_command["command"] = receiver_array
        runtime_receiver_command["command_text"] = shlex.join(receiver_array)
        receiver_index = commands.index(receiver_command)
        receiver_log = (
            logs_dir / f"{_safe_log_stem(receiver_command, index=receiver_index)}.log"
        )
        receiver_info = {
            "command": runtime_receiver_command,
            "log_path": receiver_log,
            "process": None,
            "pid": None,
            "started_at": _now(),
            "started_monotonic": time.monotonic(),
            "ended_at": None,
            "ended_monotonic": None,
            "returncode": None,
            "status": "starting",
            "termination_reason": None,
        }
        log_file = open(receiver_log, "w", buffering=1)
        receiver_info["log_file"] = log_file
        process_infos.append(receiver_info)
        log_file.write(f"$ {shlex.join(receiver_array)}\n")
        try:
            with _defer_capture_cancellation():
                receiver_process = subprocess.Popen(
                    receiver_array,
                    cwd=_repo_root(),
                    env=os.environ.copy(),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=(os.name != "nt"),
                )
                receiver_info["process"] = receiver_process
                receiver_info["pid"] = getattr(receiver_process, "pid", None)
        except CaptureExecutionCanceled:
            raise
        except Exception:
            receiver_info["status"] = "failed"
            receiver_info["termination_reason"] = "receiver_spawn_failed"
            _mark_process_ended(receiver_info)
            log_file.close()
            raise
        receiver_info["status"] = "running"
        record_status("running", "Robot pose receiver is running.")
        receiver_deadline = time.monotonic() + timeout_s
        while True:
            camera_error = _premature_camera_exit(background_processes)
            if camera_error is not None:
                raise camera_error
            remaining_s = receiver_deadline - time.monotonic()
            if remaining_s <= 0:
                _terminate_process_group(
                    receiver_process,
                    timeout_s=terminate_timeout_s,
                )
                _mark_process_ended(receiver_info)
                receiver_info["returncode"] = receiver_process.returncode
                receiver_info["status"] = "failed"
                receiver_info["termination_reason"] = "receiver_timeout"
                log_file.close()
                raise RuntimeError(
                    f"Robot pose receiver exceeded timeout of {timeout_s} seconds."
                )
            try:
                returncode = receiver_process.wait(
                    timeout=min(RECEIVER_MONITOR_INTERVAL_S, remaining_s)
                )
                break
            except subprocess.TimeoutExpired:
                continue

        camera_error = _premature_camera_exit(background_processes)
        if camera_error is not None:
            raise camera_error
        log_file.close()
        receiver_info["returncode"] = returncode
        _mark_process_ended(receiver_info)
        receiver_info["status"] = "succeeded" if returncode == 0 else "failed"
        receiver_info["termination_reason"] = "receiver_completed"
        record_status(
            "running" if returncode == 0 else "failed",
            f"Robot pose receiver exited with status {returncode}.",
        )
        if returncode != 0:
            raise RuntimeError(f"Robot pose receiver exited with status {returncode}.")

        camera_failures: list[str] = []
        for info in background_processes:
            process = info["process"]
            try:
                process.wait(timeout=terminate_timeout_s)
                _mark_process_ended(info)
                info["status"] = "succeeded" if process.returncode == 0 else "failed"
                info["termination_reason"] = "exited_after_receiver"
                if process.returncode != 0:
                    camera_failures.append(
                        f"{info['command'].get('name')} (status {process.returncode})"
                    )
            except subprocess.TimeoutExpired:
                _terminate_process_group(process, timeout_s=terminate_timeout_s)
                _mark_process_ended(info)
                info["status"] = "stopped"
                info["termination_reason"] = "stopped_after_receiver_exit"

            if info.get("log_file") is not None:
                info["log_file"].close()
            record_status(
                "running",
                f"Background command finished: {info['command'].get('name')}.",
            )
        if camera_failures:
            raise RuntimeError(
                "Camera capture command failure after receiver completion: "
                + ", ".join(camera_failures)
                + "."
            )

    except CaptureExecutionCanceled as exc:
        status = "canceled"
        message = str(exc)
        cleanup_processes("cancellation_cleanup")
        record_status("canceled", message)
    except Exception as exc:
        status = "failed"
        message = str(exc)
        cleanup_processes("failure_cleanup")
        record_status("failed", message)
    finally:
        for supervisor_signal, previous_handler in previous_signal_handlers.items():
            signal.signal(supervisor_signal, previous_handler)

    process_records = []
    for info in process_infos:
        process = info.get("process")
        returncode = info.get("returncode")
        if process is not None:
            returncode = process.returncode
        process_records.append(
            _process_record(
                info["command"],
                log_path=info["log_path"],
                pid=info.get("pid") if isinstance(info.get("pid"), int) else None,
                started_at=info.get("started_at"),
                ended_at=info.get("ended_at"),
                elapsed_s=_process_elapsed_s(info),
                returncode=returncode,
                status=str(info.get("status") or "unknown"),
                termination_reason=info.get("termination_reason"),
                startup_attempt=(
                    int(info["startup_attempt"])
                    if isinstance(info.get("startup_attempt"), int)
                    else None
                ),
                startup_attempt_limit=(
                    int(info["startup_attempt_limit"])
                    if isinstance(info.get("startup_attempt_limit"), int)
                    else None
                ),
                readiness_record_count=(
                    int(info["readiness_record_count"])
                    if isinstance(info.get("readiness_record_count"), int)
                    else None
                ),
                output_mutated=(
                    bool(info["output_mutated"])
                    if isinstance(info.get("output_mutated"), bool)
                    else None
                ),
            ).to_dict()
        )

    elapsed_s = time.monotonic() - started_monotonic
    if status == "succeeded":
        completion = build_capture_completion(
            run_root_path,
            load_run_config_for_run_root(run_root_path),
            process_records,
        )
        if completion["status"] != "ok":
            status = "failed"
            failed_checks = [
                str(check["name"])
                for check in completion["checks"]
                if check["status"] == "error"
            ]
            message = (
                "Capture children exited, but completion validation failed: "
                + ", ".join(failed_checks)
                + ". Raw evidence was preserved."
            )
    else:
        completion = {
            "schema_version": "capture_completion.v1",
            "status": "not_run",
            "enabled_sensor_count": 0,
            "checks": [],
            "error_count": 0,
        }
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "run_root": run_root_path.as_posix(),
        "status": status,
        "message": message,
        "mode": "full",
        "allow_cameras": allow_cameras,
        "allow_real_robot": allow_real_robot,
        "timeout_s": timeout_s,
        "startup_wait_s": startup_wait_s,
        "camera_startup_attempts": camera_startup_attempts,
        "camera_startup_retry_delay_s": camera_startup_retry_delay_s,
        "camera_readiness_contract": {
            "artifact": FRAME_METADATA_JSONL,
            "minimum_valid_committed_records": MIN_CAMERA_READINESS_RECORDS,
            "deadline_s": startup_wait_s,
            "deadline_scope": "per_camera_startup_attempt",
            "startup_order": "one_camera_at_a_time_in_deterministic_plan_order",
            "retry_policy": ("bounded_retry_only_without_sensor_output_evidence"),
            "attempt_log_policy": "one_distinct_log_per_camera_startup_attempt",
            "validated_sensor_outputs": [
                path.as_posix() for path in boundary.sensor_output_paths
            ],
        },
        "terminate_timeout_s": terminate_timeout_s,
        "receive_start_timeout_s": receive_start_timeout_s,
        "receive_idle_timeout_s": receive_idle_timeout_s,
        "elapsed_s": elapsed_s,
        "raw_pose_artifact": RAW_ROBOT_EE_POSES,
        "raw_pose_count": _raw_pose_count(run_root_path),
        "log_dir": (run_root_path / CAPTURE_EXECUTION_LOGS_DIR).as_posix(),
        "supervisor_stop_policy": (
            "Background camera capture commands are allowed to run while "
            "the robot pose receiver is active. After the receiver exits, the "
            "supervisor waits for them briefly and then stops remaining process "
            "groups."
        ),
        "robot_stop_policy": (
            "Failure and cancellation cleanup terminate local child process "
            "groups only; the supervisor never sends an iiwa STOP command."
        ),
        "capture_execution_plan_artifact": CAPTURE_EXECUTION_PLAN,
        "capture_execution_plan": plan,
        "processes": process_records,
        "completion": completion,
    }
    report_path = write_capture_execution_report(run_root_path, report)
    status_path = record_status(status, message)

    # The receiver is a child process that records robot_pose_capture and any
    # partial evidence independently.  Reload its latest manifest before the
    # supervisor adds capture_execution so those child updates are not lost to
    # the supervisor's startup-era in-memory copy.
    manifest = load_or_create_run_manifest(run_root_path)
    config = {}
    if isinstance(plan, Mapping):
        preflight = plan.get("preflight_report")
        if isinstance(preflight, Mapping) and isinstance(
            preflight.get("config"), Mapping
        ):
            config = dict(preflight["config"])
    manifest["robot_profile"] = dict(config.get("robot_profile") or {})
    manifest["capture_config"] = dict(config.get("capture") or {})
    artifacts: dict[str, str | Path] = {
        CAPTURE_EXECUTION_REPORT: report_path,
        CAPTURE_EXECUTION_PLAN: plan_path,
        CAPTURE_EXECUTION_STATUS: status_path,
        CAPTURE_EXECUTION_LOGS_DIR: logs_dir,
    }
    raw_pose_path = run_root_path / RAW_ROBOT_EE_POSES
    if raw_pose_path.is_file():
        artifacts[RAW_ROBOT_EE_POSES] = raw_pose_path
    upsert_stage(
        manifest,
        name="capture_execution",
        status=(
            "succeeded"
            if status == "succeeded"
            else "canceled"
            if status == "canceled"
            else "failed"
        ),
        artifacts=artifacts,
        run_root=run_root_path,
        message=message,
    )
    write_run_manifest(manifest, run_root_path)

    if status != "succeeded":
        raise RuntimeError(message)
    return report_path, report
