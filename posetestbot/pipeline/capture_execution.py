"""Capture execution planning from a validated capture plan."""

from __future__ import annotations

import fcntl
import json
import math
import os
import signal
import shlex
import subprocess
import sys
import time
import uuid
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
    RUN_CONFIG,
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
    run_config_sha256,
)
from posetestbot.robot.pose_receiver import (
    DEFAULT_RECEIVE_IDLE_TIMEOUT_S,
    DEFAULT_RECEIVE_START_TIMEOUT_S,
    RAW_POSE_CLAIM_FILE,
    recover_pose_journals,
)
from posetestbot.sensors.registry import is_auto_device_id
from posetestbot.sensors.status import collect_sensor_status


SCHEMA_VERSION = "capture_execution_plan.v2"
STATUS_SCHEMA_VERSION = "capture_execution_status.v2"
REPORT_SCHEMA_VERSION = "capture_execution_report.v2"
DEFAULT_CAPTURE_EXECUTION_TIMEOUT_S = 720.0
DEFAULT_CAMERA_READINESS_TIMEOUT_S = 15.0
DEFAULT_CAMERA_STARTUP_ATTEMPTS = 3
DEFAULT_CAMERA_STARTUP_RETRY_DELAY_S = 1.0
MIN_CAMERA_READINESS_RECORDS = 3
MAX_CAMERA_READINESS_RECORD_AGE_S = 2.0
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


@contextmanager
def _exclusive_capture_execution(run_root: Path):
    """Reject a second supervisor for the same run without creating artifacts."""

    root = run_root.resolve(strict=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(root, flags)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"A capture execution supervisor is already active for {root}."
            ) from exc
        yield
    finally:
        os.close(descriptor)


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
    run_config_sha256: str
    config_snapshot: dict[str, Any]


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
) -> list[str]:
    """Record camera exits observed while robot motion is still active.

    Once the receiver has sent START, stopping it cannot stop the iiwa motion and
    would discard the rest of the robot-pose stream.  Camera exits are therefore
    retained as deferred failures while the receiver and every healthy camera
    continue until the protocol's motion=end packet.
    """

    failures: list[str] = []
    for info in background_processes:
        if info.get("termination_reason") == "camera_exited_while_receiver_active":
            continue
        process = info["process"]
        returncode = process.poll()
        if returncode is None:
            continue
        _mark_process_ended(info)
        info["returncode"] = returncode
        info["status"] = "failed"
        info["termination_reason"] = "camera_exited_while_receiver_active"
        failures.append(f"{info['command'].get('name')} (status {returncode})")
    return failures


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


def _valid_frame_metadata_record_count(path: Path) -> int:
    """Count complete JSONL records that satisfy the shared frame contract."""

    return len(_valid_frame_metadata_records(path, limit=None))


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


def _camera_readiness_advancement_failures(
    background_processes: list[dict[str, Any]],
    *,
    maximum_record_age_s: float,
) -> list[str]:
    """Return cameras without post-readiness, recent committed metadata."""

    now_ns = time.monotonic_ns()
    maximum_record_age_ns = int(maximum_record_age_s * 1_000_000_000)
    failures: list[str] = []
    for info in background_processes:
        metadata_path = info.get("readiness_metadata_path")
        baseline_count = info.get("readiness_baseline_record_count")
        baseline_timestamp_ns = info.get("readiness_baseline_timestamp_ns")
        if (
            not isinstance(metadata_path, Path)
            or not isinstance(baseline_count, int)
            or not isinstance(baseline_timestamp_ns, int)
        ):
            failures.append(f"{info['command'].get('name')}: missing baseline")
            continue

        records = _valid_frame_metadata_records(metadata_path, limit=None)
        info["readiness_record_count"] = len(records)
        if len(records) <= baseline_count:
            failures.append(
                f"{info['command'].get('name')}: metadata did not advance beyond "
                f"{baseline_count} committed record(s)"
            )
            continue

        latest_timestamp_ns = records[-1].get("host_received_timestamp_ns")
        if (
            isinstance(latest_timestamp_ns, bool)
            or not isinstance(latest_timestamp_ns, int)
            or latest_timestamp_ns <= baseline_timestamp_ns
        ):
            failures.append(
                f"{info['command'].get('name')}: latest metadata timestamp did "
                "not advance"
            )
            continue

        record_age_ns = now_ns - latest_timestamp_ns
        if record_age_ns < 0 or record_age_ns > maximum_record_age_ns:
            failures.append(
                f"{info['command'].get('name')}: latest committed metadata is "
                f"not recent (age {record_age_ns / 1_000_000_000:.3f}s; "
                f"maximum {maximum_record_age_s:.3f}s)"
            )
            continue

        info["readiness_latest_timestamp_ns"] = latest_timestamp_ns
        info["readiness_latest_record_age_s"] = record_age_ns / 1_000_000_000
    return failures


def _sensor_output_has_mutation(output_path: Path) -> bool:
    """Return whether a startup attempt left any raw sensor evidence.

    The execution boundary requires every output path to be absent. Therefore
    even an empty directory is attempt-owned mutation and blocks an automatic
    retry. This strict check prevents a later child from mixing with or replacing
    partial evidence whose writer may have failed before committing metadata.
    """

    return os.path.lexists(output_path)


def _sensor_output_path(
    command: Mapping[str, Any],
    *,
    run_root: Path,
    require_absent: bool = False,
) -> Path:
    """Resolve one direct run-owned sensor output without following it at use time."""

    raw_output = command.get("output_folder")
    if not isinstance(raw_output, str) or not raw_output:
        raise ValueError("Every sensor_capture command requires output_folder")
    candidate = Path(raw_output)
    if not candidate.is_absolute():
        candidate = _repo_root() / candidate
    root_resolved = run_root.resolve(strict=True)
    resolved = candidate.resolve(strict=False)
    if candidate != resolved or resolved.parent != root_resolved:
        raise ValueError(
            "Sensor output folder escapes the run root, is not a direct child, "
            f"or traverses a symlink ancestor: {raw_output}"
        )
    if require_absent and os.path.lexists(candidate):
        raise FileExistsError(
            "Capture execution requires unused raw output paths; already present: "
            f"{candidate}"
        )
    return resolved


def _raw_pose_count(run_root: Path) -> int:
    path = run_root / RAW_ROBOT_EE_POSES
    if not path.is_file():
        return 0
    with open(path, "r") as f:
        value = json.load(f)
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
    return tuple(fingerprints)


def _sensor_output_paths_from_plan(
    plan: Mapping[str, Any],
    *,
    run_root: Path,
) -> tuple[Path, ...]:
    commands = plan.get("commands")
    if not isinstance(commands, list):
        raise ValueError("Capture plan commands must be a list")
    output_paths: list[Path] = []
    for command in commands:
        if not isinstance(command, Mapping) or command.get("role") != "sensor_capture":
            continue
        output_paths.append(
            _sensor_output_path(command, run_root=run_root, require_absent=True)
        )
    if len(output_paths) != len(set(output_paths)):
        raise ValueError("Planned sensor output folders must be unique")
    return tuple(output_paths)


def _revalidate_sensor_output_path_before_spawn(
    command: Mapping[str, Any],
    *,
    run_root: Path,
    expected_path: Path,
) -> Path:
    """Close path/symlink changes between planning and one camera spawn."""

    output_path = _sensor_output_path(
        command,
        run_root=run_root,
        require_absent=True,
    )
    if output_path != expected_path:
        raise ValueError(
            "Sensor output folder changed after capture planning: "
            f"{output_path} != {expected_path}"
        )
    return output_path


def _revalidate_sensor_output_paths(
    commands: list[Mapping[str, Any]],
    *,
    run_root: Path,
    expected_paths: tuple[Path, ...],
) -> tuple[Path, ...]:
    sensor_commands = [
        command for command in commands if command.get("role") == "sensor_capture"
    ]
    if len(sensor_commands) != len(expected_paths):
        raise ValueError("Capture plan sensor output count changed after planning")
    return tuple(
        _revalidate_sensor_output_path_before_spawn(
            command,
            run_root=run_root,
            expected_path=expected_path,
        )
        for command, expected_path in zip(
            sensor_commands,
            expected_paths,
            strict=True,
        )
    )


def _assert_capture_outputs_absent(
    run_root: Path,
    sensor_output_paths: tuple[Path, ...],
) -> None:
    blockers = []
    raw_pose_path = run_root / RAW_ROBOT_EE_POSES
    if os.path.lexists(raw_pose_path):
        blockers.append(raw_pose_path.as_posix())
    claim_path = run_root / RAW_POSE_CLAIM_FILE
    if os.path.lexists(claim_path):
        blockers.append(claim_path.as_posix())
    blockers.extend(
        path.as_posix()
        for path in sorted(run_root.glob("raw_robot_ee_poses.journal.*.jsonl"))
        if os.path.lexists(path)
    )
    blockers.extend(
        path.as_posix()
        for path in sorted(run_root.glob("raw_robot_ee_poses.partial.*.json"))
        if os.path.lexists(path)
    )
    blockers.extend(
        path.as_posix()
        for path in sorted(run_root.glob("raw_robot_ee_poses.claim.*.recovered.json"))
        if os.path.lexists(path)
    )
    blockers.extend(
        path.as_posix() for path in sensor_output_paths if os.path.lexists(path)
    )
    if blockers:
        raise FileExistsError(
            "Capture execution requires unused raw output paths; already present: "
            + ", ".join(blockers)
        )


def _load_bound_run_config(run_root: Path) -> tuple[dict[str, Any], str]:
    config_path = run_root / RUN_CONFIG
    if not config_path.is_file() or config_path.is_symlink():
        raise ValueError(
            f"Capture execution requires a regular run configuration: {config_path}"
        )
    config = load_run_config_for_run_root(run_root)
    return config, run_config_sha256(config)


def _assert_run_config_digest(
    run_root: Path,
    expected_sha256: str,
    *,
    phase: str,
) -> dict[str, Any]:
    try:
        config, actual_sha256 = _load_bound_run_config(run_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "run_config.json became missing, unreadable, or invalid after capture "
            f"authorization ({phase}). Raw evidence is preserved and capture "
            "cannot be accepted against an unverified configuration."
        ) from exc
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "run_config.json changed after capture authorization "
            f"({phase}); expected {expected_sha256}, found {actual_sha256}. "
            "Raw evidence is preserved and the robot receiver will not be "
            "started from a stale configuration."
        )
    return config


def _validate_capture_execution_boundary(
    run_root: Path,
) -> CaptureExecutionBoundary:
    """Perform only read-only checks before supervisor artifact creation."""

    config, config_sha256 = _load_bound_run_config(run_root)
    capture = config.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("Run configuration capture must be an object")
    sensors = capture.get("sensors")
    if not isinstance(sensors, list):
        raise ValueError("Run configuration capture sensors must be a list")
    automatic_sensors = [
        f"{index}:{sensor.get('sensor_type')}"
        for index, sensor in enumerate(sensors)
        if isinstance(sensor, Mapping)
        and sensor.get("enabled", True) is True
        and isinstance(sensor.get("device_id"), str)
        and is_auto_device_id(str(sensor["device_id"]))
    ]
    if automatic_sensors:
        raise ValueError(
            "Physical capture execution requires a concrete device_id for every "
            "enabled sensor; replace auto after discovery before authorization: "
            + ", ".join(automatic_sensors)
            + "."
        )
    plan_path = run_root / CAPTURE_PLAN
    persisted_plan: dict[str, Any] | None = None
    build_options: dict[str, int | None] = {}
    if plan_path.is_file() and not plan_path.is_symlink():
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
        run_config_sha256=config_sha256,
        config_snapshot=json.loads(json.dumps(config, allow_nan=False)),
    )


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    start_time: int | None


def _linux_process_stat(pid: int) -> tuple[int, str, int] | None:
    """Return ``(parent_pid, state, start_time)`` from procfs."""

    if not sys.platform.startswith("linux"):
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        return int(fields[1]), fields[0], int(fields[19])
    except (IndexError, OSError, ValueError):
        return None


def _process_start_time(pid: int) -> int | None:
    stat = _linux_process_stat(pid)
    return stat[2] if stat is not None else None


def _process_identity_is_live(identity: _ProcessIdentity) -> bool:
    if sys.platform.startswith("linux"):
        stat = _linux_process_stat(identity.pid)
        return bool(
            stat is not None
            and stat[1] != "Z"
            and (identity.start_time is None or stat[2] == identity.start_time)
        )
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _snapshot_process_tree(
    root_pid: int,
    *,
    expected_start_time: int | None,
) -> dict[int, _ProcessIdentity]:
    """Snapshot a verified Linux descendant tree rooted at ``root_pid``.

    Every retained PID is paired with its kernel start time before any signal is
    sent.  Later checks use that identity so PID reuse cannot redirect cleanup.
    """

    if not sys.platform.startswith("linux"):
        return {root_pid: _ProcessIdentity(root_pid, expected_start_time)}
    if expected_start_time is None:
        return {}

    process_table: dict[int, tuple[int, str, int]] = {}
    try:
        proc_entries = tuple(Path("/proc").iterdir())
    except OSError:
        proc_entries = ()
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _linux_process_stat(pid)
        if stat is not None:
            process_table[pid] = stat

    root_stat = process_table.get(root_pid)
    if root_stat is None or root_stat[1] == "Z":
        return {}
    if expected_start_time is not None and root_stat[2] != expected_start_time:
        return {}

    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, state, _start_time) in process_table.items():
            if state == "Z" or pid in descendants or parent_pid not in descendants:
                continue
            descendants.add(pid)
            changed = True
    return {
        pid: _ProcessIdentity(pid=pid, start_time=process_table[pid][2])
        for pid in descendants
    }


def _signal_process_identities(
    identities: Mapping[int, _ProcessIdentity],
    signum: int,
) -> None:
    for identity in sorted(
        identities.values(), key=lambda item: item.pid, reverse=True
    ):
        if identity.pid == os.getpid() or not _process_identity_is_live(identity):
            continue
        try:
            os.kill(identity.pid, signum)
        except (PermissionError, ProcessLookupError):
            continue


def _terminate_process_trees(
    processes: list[tuple[subprocess.Popen[Any], int | None]],
    *,
    timeout_s: float,
) -> set[int]:
    """Stop multiple child trees within one bounded shared deadline.

    Capture children deliberately inherit the outer job process group, allowing
    LocalJobRunner to contain them after cancellation or an owner crash.  Normal
    per-camera shutdown therefore signals verified PIDs instead of process
    groups, which would also signal the capture supervisor.

    The returned set contains root PIDs that still appear live after SIGKILL.
    """

    active = [item for item in processes if item[0].poll() is None]
    if not active:
        return set()
    timeout_s = max(0.0, timeout_s)
    final_deadline = time.monotonic() + timeout_s
    graceful_deadline = time.monotonic() + timeout_s * 0.75

    tree_identities: dict[int, dict[int, _ProcessIdentity]] = {
        process.pid: {} for process, _start_time in active
    }

    def all_identities() -> dict[int, _ProcessIdentity]:
        return {
            pid: identity
            for tree in tree_identities.values()
            for pid, identity in tree.items()
        }

    if os.name == "nt":
        for process, _start_time in active:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                continue
    else:
        for process, start_time in active:
            tree_identities[process.pid].update(
                _snapshot_process_tree(
                    process.pid,
                    expected_start_time=start_time,
                )
            )
        _signal_process_identities(all_identities(), signal.SIGTERM)

    while time.monotonic() < graceful_deadline:
        for process, start_time in active:
            process.poll()
            if process.returncode is None and os.name != "nt":
                tree_identities[process.pid].update(
                    _snapshot_process_tree(
                        process.pid,
                        expected_start_time=start_time,
                    )
                )
        if all(process.poll() is not None for process, _start_time in active) and (
            os.name == "nt"
            or not any(
                _process_identity_is_live(item) for item in all_identities().values()
            )
        ):
            return set()
        time.sleep(min(0.02, max(0.0, graceful_deadline - time.monotonic())))

    if os.name == "nt":
        for process, _start_time in active:
            if process.poll() is None:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    continue
    else:
        _signal_process_identities(all_identities(), signal.SIGKILL)

    while time.monotonic() < final_deadline:
        for process, _start_time in active:
            process.poll()
        if all(process.poll() is not None for process, _start_time in active) and (
            os.name == "nt"
            or not any(
                _process_identity_is_live(item) for item in all_identities().values()
            )
        ):
            return set()
        time.sleep(min(0.02, max(0.0, final_deadline - time.monotonic())))

    for process, _start_time in active:
        try:
            process.wait(timeout=0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        process.pid
        for process, start_time in active
        if process.poll() is None
        or _process_identity_is_live(_ProcessIdentity(process.pid, start_time))
        or any(
            _process_identity_is_live(identity)
            for identity in tree_identities[process.pid].values()
        )
    }


def _terminate_process_tree(
    process: subprocess.Popen[Any],
    *,
    timeout_s: float,
    expected_start_time: int | None = None,
) -> bool:
    """Stop one verified child tree; return whether its root still appears live."""

    return process.pid in _terminate_process_trees(
        [(process, expected_start_time)],
        timeout_s=timeout_s,
    )


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
    config_snapshot = preflight.get("config")
    if not isinstance(config_snapshot, Mapping):
        raise ValueError("Capture preflight did not retain a valid run configuration")
    config_sha256 = run_config_sha256(config_snapshot)
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
        "run_config_artifact": RUN_CONFIG,
        "run_config_sha256": config_sha256,
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
            "supervisor": "outer_job_group_with_verified_child_trees",
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
                "After robot_pose_receiver exits, cooperatively stop remaining "
                "selected camera descendant trees within one shared deadline."
            ),
        },
        "capture_plan": capture_plan,
        "preflight_report": preflight,
    }


def capture_execution_plan_path(run_root: str | Path) -> Path:
    return Path(run_root) / CAPTURE_EXECUTION_PLAN


def load_capture_execution_plan(run_root: str | Path) -> dict[str, Any]:
    path = capture_execution_plan_path(run_root)
    if path.is_symlink():
        raise ValueError(f"Capture execution plan must be a regular file: {path}")
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise ValueError(f"Capture execution plan must be a regular file: {path}")
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
    if path.is_symlink():
        raise ValueError(f"Capture execution status must be a regular file: {path}")
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise ValueError(f"Capture execution status must be a regular file: {path}")
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
    execution_id: str,
    execution_dir: Path,
    run_config_digest: str,
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
        "execution_id": execution_id,
        "execution_archive": execution_dir.relative_to(run_root).as_posix(),
        "run_config_artifact": RUN_CONFIG,
        "run_config_sha256": run_config_digest,
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
        "log_dir": execution_dir.as_posix(),
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
    if plan.get("run_config_sha256") != boundary.run_config_sha256:
        raise RuntimeError(
            "Capture execution plan is not bound to the exact run configuration "
            "accepted at the execution boundary."
        )
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
    """Execute one exclusively supervised full real capture."""

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
    with _exclusive_capture_execution(Path(run_root)):
        return _run_capture_execution_locked(
            run_root,
            allow_cameras=allow_cameras,
            allow_real_robot=allow_real_robot,
            include_sensor_status=include_sensor_status,
            timeout_s=timeout_s,
            startup_wait_s=startup_wait_s,
            camera_startup_attempts=camera_startup_attempts,
            camera_startup_retry_delay_s=camera_startup_retry_delay_s,
            terminate_timeout_s=terminate_timeout_s,
            receive_start_timeout_s=receive_start_timeout_s,
            receive_idle_timeout_s=receive_idle_timeout_s,
            collect_sensors=collect_sensors,
            write_plan_if_missing=write_plan_if_missing,
        )


def _run_capture_execution_locked(
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
    """Execute full real capture with job-group and verified-tree supervision."""

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
    execution_id = uuid.uuid4().hex
    with run_config_lock(run_root_path):
        recovery_config, _recovery_digest = _load_bound_run_config(run_root_path)
        recover_pose_journals(
            run_root_path,
            expected_run_id=str(recovery_config["run_id"]),
        )
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
        _assert_run_config_digest(
            run_root_path,
            boundary.run_config_sha256,
            phase="before execution evidence publication",
        )
        # Sensor discovery can take time. Recheck under the shared run-config
        # transaction before publishing the first durable execution evidence.
        _revalidate_sensor_output_paths(
            commands,
            run_root=run_root_path,
            expected_paths=boundary.sensor_output_paths,
        )
        _assert_capture_outputs_absent(
            run_root_path,
            boundary.sensor_output_paths,
        )

        logs_root = run_root_path / CAPTURE_EXECUTION_LOGS_DIR
        logs_dir = logs_root / execution_id
        plan = {
            **plan,
            "execution_id": execution_id,
            "execution_archive": logs_dir.relative_to(run_root_path).as_posix(),
            "log_dir": logs_dir.as_posix(),
        }
        if os.path.lexists(logs_root):
            if not logs_root.is_dir() or logs_root.is_symlink():
                raise ValueError(
                    "Capture execution archive root must be a regular directory: "
                    f"{logs_root}"
                )
        else:
            logs_root.mkdir(parents=True, exist_ok=False)
        logs_dir.mkdir(parents=False, exist_ok=False)
        atomic_write_json(logs_dir / CAPTURE_EXECUTION_PLAN, plan)
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
        status_record = _build_capture_execution_status(
            run_root_path,
            execution_id=execution_id,
            execution_dir=logs_dir,
            run_config_digest=boundary.run_config_sha256,
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
        )
        atomic_write_json(logs_dir / CAPTURE_EXECUTION_STATUS, status_record)
        return write_capture_execution_status(run_root_path, status_record)

    status_path = record_status("starting", "Capture execution supervisor starting.")

    def cleanup_processes(reason: str) -> None:
        live_infos = [
            info
            for info in process_infos
            if info.get("process") is not None and info["process"].poll() is None
        ]
        live_info_ids = {id(info) for info in live_infos}
        survivors: set[int] = set()
        termination_error: str | None = None
        try:
            survivors = _terminate_process_trees(
                [
                    (info["process"], info.get("process_start_time"))
                    for info in live_infos
                ],
                timeout_s=terminate_timeout_s,
            )
        except Exception as exc:
            termination_error = f"{type(exc).__name__}: {exc}"

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
                info["status"] = "failed"
                info["termination_reason"] = f"{reason}_termination_incomplete"
                if termination_error is not None:
                    info["termination_error"] = termination_error
            elif id(info) in live_info_ids:
                preserve_failure = (
                    info.get("status") == "failed"
                    and isinstance(info.get("termination_reason"), str)
                    and bool(info["termination_reason"])
                )
                _mark_process_ended(info)
                if process.pid in survivors:
                    info["status"] = "failed"
                    info["termination_reason"] = f"{reason}_termination_incomplete"
                elif not preserve_failure:
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
            expected_output_path = boundary.sensor_output_paths[sensor_position - 1]
            output_path = expected_output_path
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
                _assert_run_config_digest(
                    run_root_path,
                    boundary.run_config_sha256,
                    phase=(
                        "immediately before camera child "
                        f"{sensor_position} startup attempt {startup_attempt}"
                    ),
                )
                output_path = _revalidate_sensor_output_path_before_spawn(
                    command,
                    run_root=run_root_path,
                    expected_path=expected_output_path,
                )
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
                    "process_start_time": None,
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
                            start_new_session=False,
                        )
                        info["process"] = process
                        info["pid"] = getattr(process, "pid", None)
                        info["process_start_time"] = _process_start_time(process.pid)
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
                        readiness_records = _valid_frame_metadata_records(
                            metadata_path,
                            limit=None,
                        )
                        baseline_record = readiness_records[-1]
                        info["output_mutated"] = True
                        info["termination_reason"] = "camera_ready"
                        info["readiness_metadata_path"] = metadata_path
                        info["readiness_baseline_record_count"] = len(readiness_records)
                        info["readiness_baseline_timestamp_ns"] = int(
                            baseline_record["host_received_timestamp_ns"]
                        )
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
                        termination_incomplete = _terminate_process_tree(
                            process,
                            timeout_s=terminate_timeout_s,
                            expected_start_time=info.get("process_start_time"),
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
                        if termination_incomplete:
                            info["status"] = "failed"
                            info["termination_reason"] = (
                                "startup_termination_incomplete"
                            )
                            raise RuntimeError(
                                f"Camera {command_name} readiness timed out and "
                                "its verified process tree could not be fully "
                                "terminated; refusing automatic retry."
                            )
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
                            info["output_mutated"] = _sensor_output_has_mutation(
                                output_path
                            )
                            if info["output_mutated"]:
                                info["status"] = "failed"
                                info["termination_reason"] = (
                                    "startup_late_output_no_retry"
                                )
                                raise RuntimeError(
                                    f"Camera {command_name} published sensor output "
                                    "after startup termination; preserving that raw "
                                    "evidence and refusing automatic retry."
                                )
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
        final_readiness_deadline = time.monotonic() + startup_wait_s
        while True:
            startup_error = _camera_startup_exit(background_processes)
            if startup_error is not None:
                raise startup_error
            readiness_failures = _camera_readiness_advancement_failures(
                background_processes,
                maximum_record_age_s=MAX_CAMERA_READINESS_RECORD_AGE_S,
            )
            if not readiness_failures:
                break
            remaining_s = final_readiness_deadline - time.monotonic()
            if remaining_s <= 0:
                raise RuntimeError(
                    "Camera readiness did not advance with recent committed "
                    f"{FRAME_METADATA_JSONL} evidence before robot START: "
                    + "; ".join(readiness_failures)
                    + "."
                )
            time.sleep(min(RECEIVER_MONITOR_INTERVAL_S, remaining_s))
        record_status(
            "running",
            "Every camera advanced with recent committed frame metadata; "
            "receiver may start.",
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
            "process_start_time": None,
        }
        _assert_run_config_digest(
            run_root_path,
            boundary.run_config_sha256,
            phase="immediately before robot receiver START",
        )
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
                    start_new_session=False,
                )
                receiver_info["process"] = receiver_process
                receiver_info["pid"] = getattr(receiver_process, "pid", None)
                receiver_info["process_start_time"] = _process_start_time(
                    receiver_process.pid
                )
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
        camera_failures: list[str] = []
        while True:
            camera_failures.extend(_premature_camera_exit(background_processes))
            remaining_s = receiver_deadline - time.monotonic()
            if remaining_s <= 0:
                receiver_info["status"] = "failed"
                receiver_info["termination_reason"] = "receiver_timeout"
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

        camera_failures.extend(_premature_camera_exit(background_processes))
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
        _assert_run_config_digest(
            run_root_path,
            boundary.run_config_sha256,
            phase="after robot receiver completion",
        )

        for info in background_processes:
            process = info["process"]
            if process.poll() is None:
                try:
                    process.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    pass

        live_camera_infos = [
            info for info in background_processes if info["process"].poll() is None
        ]
        live_camera_info_ids = {id(info) for info in live_camera_infos}
        camera_survivors = _terminate_process_trees(
            [
                (info["process"], info.get("process_start_time"))
                for info in live_camera_infos
            ],
            timeout_s=terminate_timeout_s,
        )
        for info in background_processes:
            process = info["process"]
            if info.get("termination_reason") == "camera_exited_while_receiver_active":
                pass
            elif id(info) in live_camera_info_ids:
                _mark_process_ended(info)
                if process.pid in camera_survivors or process.poll() is None:
                    info["status"] = "failed"
                    info["termination_reason"] = (
                        "camera_termination_incomplete_after_receiver_exit"
                    )
                    camera_failures.append(
                        f"{info['command'].get('name')} (termination incomplete)"
                    )
                else:
                    info["status"] = "stopped"
                    info["termination_reason"] = "stopped_after_receiver_exit"
            else:
                _mark_process_ended(info)
                info["status"] = "succeeded" if process.returncode == 0 else "failed"
                info["termination_reason"] = "exited_after_receiver"
                if process.returncode != 0:
                    camera_failures.append(
                        f"{info['command'].get('name')} (status {process.returncode})"
                    )

            if info.get("log_file") is not None:
                info["log_file"].close()
            record_status(
                "running",
                f"Background command finished: {info['command'].get('name')}.",
            )
        if camera_failures:
            raise RuntimeError(
                "Camera capture command failure after receiver completion; "
                "failures observed during robot motion were deferred so the pose "
                "receiver and remaining cameras could continue through motion=end: "
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
        try:
            _assert_run_config_digest(
                run_root_path,
                boundary.run_config_sha256,
                phase="before capture completion validation",
            )
        except RuntimeError as exc:
            status = "failed"
            message = str(exc)
            completion = {
                "schema_version": "capture_completion.v1",
                "status": "not_run",
                "enabled_sensor_count": 0,
                "checks": [],
                "error_count": 0,
            }
        else:
            completion = build_capture_completion(
                run_root_path,
                boundary.config_snapshot,
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
    if status == "succeeded":
        try:
            _assert_run_config_digest(
                run_root_path,
                boundary.run_config_sha256,
                phase="after capture completion validation",
            )
        except RuntimeError as exc:
            status = "failed"
            message = str(exc)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": _now(),
        "run_root": run_root_path.as_posix(),
        "execution_id": execution_id,
        "execution_archive": logs_dir.relative_to(run_root_path).as_posix(),
        "run_config_artifact": RUN_CONFIG,
        "run_config_sha256": boundary.run_config_sha256,
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
            "required_post_readiness_record_advance": 1,
            "maximum_latest_record_age_s": MAX_CAMERA_READINESS_RECORD_AGE_S,
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
        "log_dir": logs_dir.as_posix(),
        "run_config_binding_contract": {
            "digest": "sha256_of_canonical_validated_run_config",
            "checkpoints": [
                "before_execution_evidence_publication",
                "immediately_before_each_camera_child",
                "immediately_before_robot_receiver_start",
                "after_robot_receiver_completion",
                "before_capture_completion_validation",
                "after_capture_completion_validation",
            ],
            "completion_uses_accepted_snapshot": True,
        },
        "supervisor_stop_policy": (
            "Background camera capture commands are allowed to run while "
            "the robot pose receiver is active. After the receiver exits, the "
            "supervisor cooperatively stops their verified descendant process "
            "trees within one shared deadline."
        ),
        "robot_stop_policy": (
            "Failure and cancellation cleanup terminate local child process trees "
            "only; the supervisor never sends an iiwa STOP command."
        ),
        "capture_execution_plan_artifact": CAPTURE_EXECUTION_PLAN,
        "capture_execution_plan": plan,
        "processes": process_records,
        "completion": completion,
    }
    atomic_write_json(logs_dir / CAPTURE_EXECUTION_REPORT, report)
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
        CAPTURE_EXECUTION_LOGS_DIR: logs_root,
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
