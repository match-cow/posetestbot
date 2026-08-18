"""Safety-hardened UDP robot-pose acquisition.

The reusable capture plan deliberately omits execution acknowledgements.  They
must be supplied to this module for every invocation before it binds a socket
or sends the robot start message.
"""

from __future__ import annotations

import json
import ipaddress
import math
import os
import signal
import socket
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from posetestbot.config import (
    MAX_CAPTURE_COMMAND_VELOCITY_M_S,
    RobotProfile,
    bounded_capture_velocity_m_s,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.io.artifacts import RAW_ROBOT_EE_POSES
from posetestbot.io.manifest import (
    load_or_create_run_manifest,
    set_manifest_artifact,
    upsert_stage,
    write_run_manifest,
)
from posetestbot.robot.udp import send_start
from posetestbot.robot.reference_frames import (
    POSE_TEMPLATE_BASE_SUNRISE_PATH,
    configured_sunrise_reference_frame_path,
)


DEFAULT_RECEIVE_START_TIMEOUT_S = 120.0
DEFAULT_RECEIVE_IDLE_TIMEOUT_S = 60.0
PARTIAL_SCHEMA_VERSION = "raw_robot_ee_poses_partial.v1"
CLAIM_SCHEMA_VERSION = "raw_robot_ee_poses_claim.v1"
POSE_PACKET_SCHEMA_VERSION = "robot_pose.v1"
MAX_PACKET_BYTES = 65_535


class PoseReceiverError(RuntimeError):
    """Base error for an incomplete robot-pose capture."""


class PoseReceiverPermissionError(PoseReceiverError):
    """Raised before I/O when fresh execution acknowledgements are absent."""


class PoseReceiverOverwriteError(PoseReceiverError):
    """Raised when the canonical raw-pose artifact already exists."""


class PoseReceiverTimeout(PoseReceiverError):
    """Raised when the first or next pose packet does not arrive in time."""


class PoseReceiverPacketError(PoseReceiverError):
    """Raised when a robot-pose datagram violates the packet contract."""


class PoseReceiverCanceled(PoseReceiverError):
    """Raised on an operator or supervisor interruption."""


@dataclass(frozen=True)
class PoseReceiverResult:
    """Successful pose-receiver result."""

    raw_pose_path: Path
    pose_count: int
    start_message: Mapping[str, Any]


@dataclass(frozen=True)
class RawPoseClaim:
    """Exclusive ownership token for the canonical raw-pose path."""

    path: Path
    claim_id: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_execution_boundary(
    *,
    allow_real_robot: bool,
    allow_cameras: bool,
    receive_start_timeout_s: float,
    receive_idle_timeout_s: float,
) -> None:
    missing = []
    if allow_real_robot is not True:
        missing.append("--allow-real-robot")
    if allow_cameras is not True:
        missing.append("--allow-cameras")
    if missing:
        raise PoseReceiverPermissionError(
            "Pose receiver execution requires fresh acknowledgements: "
            + ", ".join(missing)
            + "."
        )
    for name, value in (
        ("receive_start_timeout_s", receive_start_timeout_s),
        ("receive_idle_timeout_s", receive_idle_timeout_s),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite value greater than 0")


def _stage_artifact_paths(
    manifest: Mapping[str, Any], run_root: Path
) -> dict[str, Path]:
    for stage in manifest.get("stages", []):
        if not isinstance(stage, Mapping) or stage.get("name") != "robot_pose_capture":
            continue
        artifacts = stage.get("artifacts")
        if not isinstance(artifacts, Mapping):
            return {}
        paths: dict[str, Path] = {}
        for name, value in artifacts.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            path = Path(value)
            paths[name] = path if path.is_absolute() else run_root / path
        return paths
    return {}


def _partial_path(run_root: Path) -> Path:
    return run_root / (
        f"raw_robot_ee_poses.partial.{time.time_ns()}.{uuid.uuid4().hex}.json"
    )


def _write_partial_evidence(
    manifest: dict[str, Any],
    run_root: Path,
    *,
    status: str,
    message: str,
    poses: Mapping[int, Mapping[str, Any]],
    started_at: str,
    last_packet_preview: str | None,
    last_sender: tuple[Any, ...] | None,
) -> Path:
    path = _partial_path(run_root)
    evidence: dict[str, Any] = {
        "schema_version": PARTIAL_SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "ended_at": _now(),
        "message": message,
        "received_pose_count": len(poses),
        "poses": dict(poses),
    }
    if last_packet_preview is not None:
        evidence["last_packet_preview"] = last_packet_preview
    if last_sender is not None:
        evidence["last_sender"] = [str(value) for value in last_sender]
    atomic_write_json(path, evidence, indent=2, sort_keys=False)

    set_manifest_artifact(manifest, path.name, path, run_root=run_root)
    artifacts = _stage_artifact_paths(manifest, run_root)
    artifacts[path.name] = path
    upsert_stage(
        manifest,
        name="robot_pose_capture",
        status=status,
        artifacts=artifacts,
        run_root=run_root,
        message=message,
    )
    write_run_manifest(manifest, run_root)
    return path


def _claim_raw_pose_artifact(path: Path) -> RawPoseClaim:
    """Atomically reserve the canonical path before any network operation."""

    claim = RawPoseClaim(path=path, claim_id=uuid.uuid4().hex)
    payload = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "status": "reserved",
        "claim_id": claim.claim_id,
        "created_at": _now(),
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PoseReceiverOverwriteError(
            f"Refusing to replace existing raw pose artifact: {path}"
        ) from exc
    claimed_inode = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            current_inode = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                current_inode.st_dev == claimed_inode.st_dev
                and current_inode.st_ino == claimed_inode.st_ino
            ):
                path.unlink(missing_ok=True)
        raise
    return claim


def _owns_raw_pose_claim(claim: RawPoseClaim) -> bool:
    try:
        metadata = claim.path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        with open(claim.path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema_version") == CLAIM_SCHEMA_VERSION
        and value.get("claim_id") == claim.claim_id
        and value.get("status") == "reserved"
    )


def _promote_raw_pose_claim(
    claim: RawPoseClaim,
    poses: Mapping[int, Mapping[str, Any]],
) -> Path:
    """Replace only this receiver's verified claim with complete pose data."""

    if not _owns_raw_pose_claim(claim):
        raise PoseReceiverOverwriteError(
            "Raw pose reservation ownership changed before promotion; refusing "
            f"to replace {claim.path}."
        )
    pending = claim.path.with_name(
        f".{claim.path.name}.{claim.claim_id}.{uuid.uuid4().hex}.pending"
    )
    atomic_write_json(pending, dict(poses), indent=4, sort_keys=False)
    try:
        if not _owns_raw_pose_claim(claim):
            raise PoseReceiverOverwriteError(
                "Raw pose reservation ownership changed during promotion; "
                f"refusing to replace {claim.path}."
            )
        os.replace(pending, claim.path)
    finally:
        pending.unlink(missing_ok=True)
    return claim.path


def _cleanup_raw_pose_claim(claim: RawPoseClaim) -> None:
    """Remove a failed receiver's reservation, but never a foreign artifact."""

    if _owns_raw_pose_claim(claim):
        claim.path.unlink(missing_ok=True)


def _packet_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate mandatory robot_pose.v1 sender provenance."""

    schema_version = value.get("schema_version")
    if schema_version != POSE_PACKET_SCHEMA_VERSION:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: unsupported schema_version "
            f"{schema_version!r}."
        )

    packet_kind = value.get("packet_kind")
    if packet_kind not in {"pose", "end"}:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: packet_kind must be 'pose' or 'end'."
        )
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: sequence must be a non-negative integer."
        )
    sender_monotonic_ns = value.get("sender_monotonic_ns")
    if (
        isinstance(sender_monotonic_ns, bool)
        or not isinstance(sender_monotonic_ns, int)
        or sender_monotonic_ns < 0
    ):
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: sender_monotonic_ns must be a "
            "non-negative integer."
        )
    sender_wall_timestamp_ms = value.get("sender_wall_timestamp_ms")
    if (
        isinstance(sender_wall_timestamp_ms, bool)
        or not isinstance(sender_wall_timestamp_ms, int)
        or sender_wall_timestamp_ms < 0
    ):
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: sender_wall_timestamp_ms must be a "
            "non-negative integer."
        )

    run_id = value.get("run_id")
    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: run_id must be a canonical UUID."
        ) from exc
    if run_id != canonical_run_id:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: run_id must be a canonical UUID."
        )
    if value.get("from_frame") != "robot_flange":
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: from_frame must be robot_flange."
        )
    if value.get("to_frame") != "template_base":
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: to_frame must be template_base."
        )
    reference_path = value.get("sunrise_reference_frame_path")
    if reference_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: sunrise_reference_frame_path must be "
            f"{POSE_TEMPLATE_BASE_SUNRISE_PATH}."
        )

    motion = value.get("motion")
    expected_kind = "end" if motion == "end" else "pose"
    if packet_kind != expected_kind:
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: packet_kind is inconsistent with motion."
        )
    metadata = {
        "schema_version": schema_version,
        "packet_kind": packet_kind,
        "sequence": sequence,
        "sender_monotonic_ns": sender_monotonic_ns,
        "sender_wall_timestamp_ms": sender_wall_timestamp_ms,
        "run_id": run_id,
        "from_frame": "robot_flange",
        "to_frame": "template_base",
        "sunrise_reference_frame_path": reference_path,
    }
    timing_fields = (
        "sender_target_period_ms",
        "sender_previous_pose_delta_ns",
        "sender_pose_query_duration_ns",
    )
    present_timing_fields = [field for field in timing_fields if field in value]
    if present_timing_fields and len(present_timing_fields) != len(timing_fields):
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: sender cadence evidence must include "
            + ", ".join(timing_fields)
            + "."
        )
    if present_timing_fields:
        target_period_ms = value["sender_target_period_ms"]
        previous_pose_delta_ns = value["sender_previous_pose_delta_ns"]
        pose_query_duration_ns = value["sender_pose_query_duration_ns"]
        if (
            isinstance(target_period_ms, bool)
            or not isinstance(target_period_ms, int)
            or target_period_ms <= 0
        ):
            raise PoseReceiverPacketError(
                "Malformed robot pose packet: sender_target_period_ms must be "
                "a positive integer."
            )
        for field, field_value in (
            ("sender_previous_pose_delta_ns", previous_pose_delta_ns),
            ("sender_pose_query_duration_ns", pose_query_duration_ns),
        ):
            if (
                isinstance(field_value, bool)
                or not isinstance(field_value, int)
                or field_value < 0
            ):
                raise PoseReceiverPacketError(
                    f"Malformed robot pose packet: {field} must be a "
                    "non-negative integer."
                )
        metadata.update(
            {
                "sender_target_period_ms": target_period_ms,
                "sender_previous_pose_delta_ns": previous_pose_delta_ns,
                "sender_pose_query_duration_ns": pose_query_duration_ns,
            }
        )
    return metadata


def _decode_packet(
    data: bytes,
) -> tuple[str, dict[str, int | float] | None, dict[str, Any]]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PoseReceiverPacketError(
            f"Malformed robot pose packet: invalid JSON ({exc})."
        ) from exc
    if not isinstance(value, dict):
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: expected a JSON object."
        )

    motion = value.get("motion")
    if not isinstance(motion, str) or not motion.strip():
        raise PoseReceiverPacketError(
            "Malformed robot pose packet: motion must be a non-empty string."
        )
    metadata = _packet_metadata(value)
    if motion == "end":
        return motion, None, metadata

    pose: dict[str, int | float] = {}
    for axis in ("X", "Y", "Z", "A", "B", "C"):
        coordinate = value.get(axis)
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(float(coordinate))
        ):
            raise PoseReceiverPacketError(
                f"Malformed robot pose packet: {axis} must be a finite number."
            )
        pose[axis] = coordinate
    return motion, pose, metadata


def _stream_identity(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: metadata[key]
        for key in (
            "schema_version",
            "run_id",
            "from_frame",
            "to_frame",
            "sunrise_reference_frame_path",
        )
        if key in metadata
    }


def _validate_sender(
    sender: Any,
    *,
    expected_robot_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[Any, ...]:
    if not isinstance(sender, tuple) or len(sender) < 2:
        raise PoseReceiverPacketError(
            "Malformed robot pose sender address: expected an IP/port tuple."
        )
    try:
        sender_ip = ipaddress.ip_address(str(sender[0]))
    except ValueError as exc:
        raise PoseReceiverPacketError(
            f"Malformed robot pose sender IP: {sender[0]!r}."
        ) from exc
    if sender_ip != expected_robot_ip:
        raise PoseReceiverPacketError(
            "Rejected robot pose packet from unexpected sender IP "
            f"{sender_ip}; expected {expected_robot_ip}."
        )
    return tuple(sender)


@contextmanager
def _cancellation_signal_handlers(enabled: bool) -> Iterator[None]:
    previous_handlers: dict[int, Any] = {}

    def cancel(signum: int, _frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        raise PoseReceiverCanceled(f"Robot pose capture canceled by {name}.")

    if enabled:
        for receiver_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[receiver_signal] = signal.getsignal(receiver_signal)
                signal.signal(receiver_signal, cancel)
            except (OSError, ValueError):
                previous_handlers.pop(receiver_signal, None)
    try:
        yield
    finally:
        for receiver_signal, handler in previous_handlers.items():
            signal.signal(receiver_signal, handler)


def run_pose_receiver(
    output_path: str | Path,
    *,
    profile: RobotProfile,
    run_id: str,
    verbose: bool = False,
    allow_real_robot: bool = False,
    allow_cameras: bool = False,
    maximum_command_velocity_m_s: float = MAX_CAPTURE_COMMAND_VELOCITY_M_S,
    receive_start_timeout_s: float = DEFAULT_RECEIVE_START_TIMEOUT_S,
    receive_idle_timeout_s: float = DEFAULT_RECEIVE_IDLE_TIMEOUT_S,
    socket_factory: Callable[..., Any] = socket.socket,
    send_start_command: Callable[..., Mapping[str, Any]] = send_start,
    install_signal_handlers: bool = True,
) -> PoseReceiverResult:
    """Receive one pose stream after validating fresh execution permissions."""

    _validate_execution_boundary(
        allow_real_robot=allow_real_robot,
        allow_cameras=allow_cameras,
        receive_start_timeout_s=receive_start_timeout_s,
        receive_idle_timeout_s=receive_idle_timeout_s,
    )
    requested_velocity_m_s = profile.cartesian_velocity_m_s
    commanded_velocity_m_s = bounded_capture_velocity_m_s(
        requested_velocity_m_s,
        maximum_velocity_m_s=maximum_command_velocity_m_s,
    )
    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("run_id must be a canonical UUID") from exc
    if run_id != canonical_run_id:
        raise ValueError("run_id must be a canonical UUID")
    command_profile = profile.with_overrides(
        cartesian_velocity_m_s=commanded_velocity_m_s
    )

    run_root = Path(output_path)
    run_root.mkdir(parents=True, exist_ok=True)
    if not run_root.is_dir():
        raise ValueError(f"Output path is not a directory: {run_root}")
    raw_pose_path = run_root / RAW_ROBOT_EE_POSES
    from posetestbot.pipeline.run_config import load_run_config_for_run_root

    run_config = load_run_config_for_run_root(run_root)
    if run_config["run_id"] != run_id:
        raise ValueError("run_id does not match run_config.json")
    expected_reference_path = configured_sunrise_reference_frame_path(run_config)
    if expected_reference_path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
        raise ValueError(
            "run_config.json does not use the canonical PoseTemplateBase frame"
        )
    try:
        expected_robot_ip = ipaddress.ip_address(profile.robot_ip)
    except ValueError as exc:
        raise ValueError(
            f"Robot profile robot_ip must be an IP address: {profile.robot_ip!r}"
        ) from exc
    claim = _claim_raw_pose_artifact(raw_pose_path)

    started_at = _now()
    manifest: dict[str, Any] | None = None
    poses: dict[int, dict[str, Any]] = {}
    previous_frame_ts = 0
    last_packet_preview: str | None = None
    last_sender: tuple[Any, ...] | None = None
    start_message: Mapping[str, Any] = {}
    sender_stream_identity: dict[str, Any] | None = None
    previous_sender_sequence: int | None = None

    try:
        manifest = load_or_create_run_manifest(
            run_root,
            robot_profile=command_profile,
            capture_config={
                "cartesian_velocity_m_s": commanded_velocity_m_s,
                "requested_cartesian_velocity_m_s": requested_velocity_m_s,
                "command_velocity_cap_m_s": maximum_command_velocity_m_s,
                "protocol": "robot_command.v1",
                "mode": "real",
            },
        )
        upsert_stage(manifest, name="robot_pose_capture", status="running")
        write_run_manifest(manifest, run_root)
        with _cancellation_signal_handlers(install_signal_handlers):
            with socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind((profile.receiver_ip, profile.receiver_port))
                sock.settimeout(receive_start_timeout_s)
                print(f"Listening on {profile.receiver_ip}:{profile.receiver_port}")

                start_message = send_start_command(
                    command_profile,
                    run_id=run_id,
                    maximum_velocity_m_s=maximum_command_velocity_m_s,
                )
                print(
                    "Sent start message to "
                    f"{command_profile.robot_ip}:{command_profile.command_port} "
                    f"with capture vel {commanded_velocity_m_s}"
                )
                if commanded_velocity_m_s < requested_velocity_m_s:
                    print(
                        "Configured capture velocity "
                        f"{requested_velocity_m_s} m/s was capped at "
                        f"{commanded_velocity_m_s} m/s before START"
                    )
                print(f"Message: {start_message}")

                received_any_packet = False
                while True:
                    try:
                        data, sender = sock.recvfrom(MAX_PACKET_BYTES)
                    except socket.timeout as exc:
                        if received_any_packet:
                            message = (
                                "Timed out waiting for the next robot pose packet "
                                f"after {receive_idle_timeout_s:g} seconds."
                            )
                        else:
                            message = (
                                "Timed out waiting for the first robot pose packet "
                                f"after {receive_start_timeout_s:g} seconds."
                            )
                        raise PoseReceiverTimeout(message) from exc

                    host_received_timestamp_ns = time.monotonic_ns()
                    host_wall_timestamp_ns = time.time_ns()
                    received_any_packet = True
                    last_sender = tuple(sender) if isinstance(sender, tuple) else None
                    last_packet_preview = data[:4096].decode("utf-8", errors="replace")
                    last_sender = _validate_sender(
                        sender,
                        expected_robot_ip=expected_robot_ip,
                    )
                    motion, pose, source_packet = _decode_packet(data)
                    if source_packet.get("run_id") != run_id:
                        raise PoseReceiverPacketError(
                            "Robot pose packet run_id does not match the requested capture."
                        )
                    observed_reference_path = source_packet.get(
                        "sunrise_reference_frame_path"
                    )
                    if observed_reference_path != expected_reference_path:
                        raise PoseReceiverPacketError(
                            "Robot pose stream Sunrise reference frame does not "
                            "match run_config.json: observed "
                            f"{observed_reference_path!r}, expected "
                            f"{expected_reference_path!r}."
                        )
                    current_identity = _stream_identity(source_packet)
                    if sender_stream_identity is None:
                        sender_stream_identity = current_identity
                    elif current_identity != sender_stream_identity:
                        raise PoseReceiverPacketError(
                            "Robot pose packet stream identity changed during capture."
                        )

                    sender_sequence = int(source_packet["sequence"])
                    if (
                        previous_sender_sequence is not None
                        and sender_sequence <= previous_sender_sequence
                    ):
                        raise PoseReceiverPacketError(
                            "Robot pose packet sequence must increase strictly; "
                            f"received {sender_sequence} after "
                            f"{previous_sender_sequence}."
                        )
                    if previous_sender_sequence is None:
                        source_packet["sequence_delta"] = 0
                        source_packet["estimated_packets_lost"] = 0
                    else:
                        sequence_delta = sender_sequence - previous_sender_sequence
                        source_packet["sequence_delta"] = sequence_delta
                        source_packet["estimated_packets_lost"] = max(
                            0, sequence_delta - 1
                        )
                    previous_sender_sequence = sender_sequence
                    if motion == "end":
                        if not poses:
                            raise PoseReceiverPacketError(
                                "Robot pose stream ended before any pose packet was "
                                "captured."
                            )
                        break

                    if not poses:
                        sock.settimeout(receive_idle_timeout_s)
                    framename = int(round(host_wall_timestamp_ns / 1_000_000))
                    frame_delta = 0 if not poses else framename - int(previous_frame_ts)
                    previous_frame_ts = framename
                    pose_record: dict[str, Any] = {
                        "framename": framename,
                        "host_received_timestamp_ns": host_received_timestamp_ns,
                        "host_wall_timestamp_ns": host_wall_timestamp_ns,
                        "frame_delta": frame_delta,
                        "motion": motion,
                        "pose": pose,
                    }
                    pose_record["source_packet"] = source_packet
                    poses[len(poses)] = pose_record

                    if verbose:
                        print(
                            f"framename: {framename}, addr: {sender}, "
                            f"motion: {motion}, pose: {pose}"
                        )
                    print(f"Received poses: {len(poses)}", end="\r", flush=True)

        _promote_raw_pose_claim(claim, poses)
        set_manifest_artifact(
            manifest,
            RAW_ROBOT_EE_POSES,
            raw_pose_path,
            run_root=run_root,
        )
        artifacts = _stage_artifact_paths(manifest, run_root)
        artifacts[RAW_ROBOT_EE_POSES] = raw_pose_path
        upsert_stage(
            manifest,
            name="robot_pose_capture",
            status="succeeded",
            artifacts=artifacts,
            run_root=run_root,
            message=f"Captured {len(poses)} robot poses.",
        )
        write_run_manifest(manifest, run_root)
    except (PoseReceiverCanceled, KeyboardInterrupt, InterruptedError) as exc:
        canceled = (
            exc
            if isinstance(exc, PoseReceiverCanceled)
            else PoseReceiverCanceled("Robot pose capture was interrupted.")
        )
        try:
            if manifest is not None:
                _write_partial_evidence(
                    manifest,
                    run_root,
                    status="canceled",
                    message=str(canceled),
                    poses=poses,
                    started_at=started_at,
                    last_packet_preview=last_packet_preview,
                    last_sender=last_sender,
                )
        finally:
            _cleanup_raw_pose_claim(claim)
        if canceled is exc:
            raise
        raise canceled from exc
    except Exception as exc:
        try:
            if manifest is not None:
                _write_partial_evidence(
                    manifest,
                    run_root,
                    status="failed",
                    message=str(exc),
                    poses=poses,
                    started_at=started_at,
                    last_packet_preview=last_packet_preview,
                    last_sender=last_sender,
                )
        finally:
            _cleanup_raw_pose_claim(claim)
        raise

    if poses:
        print()
    return PoseReceiverResult(
        raw_pose_path=raw_pose_path,
        pose_count=len(poses),
        start_message=start_message,
    )
