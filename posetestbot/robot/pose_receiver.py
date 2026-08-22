"""Safety-hardened UDP robot-pose acquisition.

The reusable capture plan deliberately omits execution acknowledgements.  They
must be supplied to this module for every invocation before it binds a socket
or sends the robot start message.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import math
import os
import re
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
CLAIM_SCHEMA_VERSION = "raw_robot_ee_poses_claim.v2"
POSE_PACKET_SCHEMA_VERSION = "robot_pose.v1"
POSE_JOURNAL_SCHEMA_VERSION = "raw_robot_ee_poses_journal.v1"
RAW_POSE_CLAIM_FILE = ".raw_robot_ee_poses.claim.json"
RAW_POSE_RECEIVER_LOCK_FILE = ".raw_robot_ee_poses.receiver.lock"
STREAM_END_SOURCE_PACKET = "stream_end_source_packet"
MAX_PACKET_BYTES = 65_535
JOURNAL_FSYNC_EVERY_POSES = 32
JOURNAL_FSYNC_INTERVAL_S = 0.25
POSE_JOURNAL_PATTERN = re.compile(
    r"^raw_robot_ee_poses\.journal\.([0-9a-f]{32})\.jsonl$"
)
RECOVERED_CLAIM_PATTERN = re.compile(
    r"^raw_robot_ee_poses\.claim\.([0-9a-f]{32})\.recovered\.json$"
)


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


@dataclass
class RawPoseClaim:
    """Hidden exclusive ownership token for the canonical raw-pose path."""

    path: Path
    raw_pose_path: Path
    claim_id: str
    receiver_lock_fd: int | None = None


@dataclass(frozen=True)
class PoseJournalSnapshot:
    """Last content prefix made durable by a journal commit marker."""

    path: Path
    claim_id: str
    run_id: str
    started_at: str
    poses: Mapping[int, Mapping[str, Any]]
    terminal_accepted: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _receiver_startup_boundary(_label: str) -> None:
    """Fault-injection seam for receiver claim/journal crash tests."""


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _acquire_pose_receiver_lock(run_root: Path) -> int:
    """Serialize receiver startup, recovery, and claim ownership for one run."""

    path = run_root / RAW_POSE_RECEIVER_LOCK_FILE
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(run_root, directory_flags)
    descriptor = -1
    try:
        opened_directory = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(opened_directory.st_mode):
            raise PoseReceiverOverwriteError(
                f"Robot pose receiver lock parent is not a directory: {run_root}"
            )
        lock_flags = os.O_RDWR | os.O_CREAT
        lock_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            RAW_POSE_RECEIVER_LOCK_FILE,
            lock_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        opened_lock = os.fstat(descriptor)
        if not stat.S_ISREG(opened_lock.st_mode):
            raise PoseReceiverOverwriteError(
                f"Robot pose receiver lock is not a regular file: {path}"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PoseReceiverOverwriteError(
                f"An active receiver owns this run: {path}"
            ) from exc
        try:
            current_directory = os.stat(run_root, follow_symlinks=False)
            current_lock = os.stat(
                RAW_POSE_RECEIVER_LOCK_FILE,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PoseReceiverOverwriteError(
                f"Robot pose receiver lock changed while locking: {path}"
            ) from exc
        if (
            not stat.S_ISDIR(current_directory.st_mode)
            or current_directory.st_dev != opened_directory.st_dev
            or current_directory.st_ino != opened_directory.st_ino
            or not stat.S_ISREG(current_lock.st_mode)
            or current_lock.st_dev != opened_lock.st_dev
            or current_lock.st_ino != opened_lock.st_ino
        ):
            raise PoseReceiverOverwriteError(
                f"Robot pose receiver lock changed while locking: {path}"
            )
        os.fsync(directory_descriptor)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        os.close(directory_descriptor)


def _release_pose_receiver_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _journal_path(run_root: Path, claim_id: str) -> Path:
    return run_root / f"raw_robot_ee_poses.journal.{claim_id}.jsonl"


def _recovered_journal_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.recovered.jsonl")


def _journal_line(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


class _PoseJournal:
    """Exclusive append-only pose journal with explicit durable prefixes."""

    def __init__(
        self,
        *,
        path: Path,
        claim_id: str,
        run_id: str,
        started_at: str,
        handle: Any,
        fsync_every_poses: int,
        fsync_interval_s: float,
    ) -> None:
        self.path = path
        self.claim_id = claim_id
        self.run_id = run_id
        self.started_at = started_at
        self._handle = handle
        self._fsync_every_poses = fsync_every_poses
        self._fsync_interval_s = fsync_interval_s
        self._pose_count = 0
        self._committed_pose_count = 0
        self._last_fsync_monotonic = time.monotonic()
        self._terminal_written = False
        self._terminal_committed = False
        self._closed = False

    @property
    def pose_count(self) -> int:
        return self._pose_count

    def pending_fsync_timeout_s(self) -> float | None:
        if self._pose_count == self._committed_pose_count:
            return None
        elapsed = time.monotonic() - self._last_fsync_monotonic
        return max(0.001, self._fsync_interval_s - elapsed)

    def _write(self, value: Mapping[str, Any]) -> None:
        if self._closed:
            raise ValueError("Robot pose journal is closed")
        self._handle.write(_journal_line(value))

    def append_pose(self, index: int, pose: Mapping[str, Any]) -> None:
        if self._terminal_written:
            raise ValueError("Cannot append a pose after the terminal packet")
        if index != self._pose_count:
            raise ValueError("Robot pose journal indices must be contiguous")
        self._write(
            {
                "schema_version": POSE_JOURNAL_SCHEMA_VERSION,
                "record_kind": "pose",
                "pose_index": index,
                "pose": dict(pose),
            }
        )
        self._pose_count += 1
        if (
            self._pose_count - self._committed_pose_count
            >= self._fsync_every_poses
            or time.monotonic() - self._last_fsync_monotonic
            >= self._fsync_interval_s
        ):
            self.commit()

    def append_end(self, source_packet: Mapping[str, Any]) -> None:
        if self._terminal_written:
            raise ValueError("Robot pose journal already has a terminal packet")
        if self._pose_count <= 0:
            raise ValueError("Cannot terminate an empty robot pose journal")
        self._write(
            {
                "schema_version": POSE_JOURNAL_SCHEMA_VERSION,
                "record_kind": "end",
                "pose_count": self._pose_count,
                "source_packet": dict(source_packet),
                "accepted_at": _now(),
            }
        )
        self._terminal_written = True
        self.commit(terminal_accepted=True)

    def commit(self, *, terminal_accepted: bool | None = None) -> None:
        if self._closed:
            return
        if terminal_accepted is None:
            terminal_accepted = self._terminal_written
        if terminal_accepted and not self._terminal_written:
            raise ValueError("Terminal journal commit has no accepted end packet")
        if self._pose_count == self._committed_pose_count and (
            not terminal_accepted or self._terminal_committed
        ):
            return
        self._write(
            {
                "schema_version": POSE_JOURNAL_SCHEMA_VERSION,
                "record_kind": "commit",
                "committed_pose_count": self._pose_count,
                "last_pose_index": self._pose_count - 1,
                "terminal_accepted": terminal_accepted,
                "committed_at": _now(),
            }
        )
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._committed_pose_count = self._pose_count
        if terminal_accepted:
            self._terminal_committed = True
        self._last_fsync_monotonic = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        self._handle.close()
        self._closed = True


def _create_pose_journal(
    run_root: Path,
    claim: RawPoseClaim,
    *,
    run_id: str,
    started_at: str,
    fsync_every_poses: int = JOURNAL_FSYNC_EVERY_POSES,
    fsync_interval_s: float = JOURNAL_FSYNC_INTERVAL_S,
) -> _PoseJournal:
    if (
        isinstance(fsync_every_poses, bool)
        or not isinstance(fsync_every_poses, int)
        or fsync_every_poses <= 0
    ):
        raise ValueError("fsync_every_poses must be a positive integer")
    if not math.isfinite(fsync_interval_s) or fsync_interval_s <= 0:
        raise ValueError("fsync_interval_s must be finite and positive")
    path = _journal_path(run_root, claim.claim_id)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(pending, flags, 0o600)
    except FileExistsError as exc:
        raise PoseReceiverOverwriteError(
            f"Robot pose journal staging path already exists: {pending}"
        ) from exc
    handle = None
    published = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        handle.write(
            _journal_line(
                {
                    "schema_version": POSE_JOURNAL_SCHEMA_VERSION,
                    "record_kind": "header",
                    "claim_id": claim.claim_id,
                    "run_id": run_id,
                    "started_at": started_at,
                }
            )
        )
        handle.flush()
        os.fsync(handle.fileno())
        try:
            os.link(pending, path)
        except FileExistsError as exc:
            raise PoseReceiverOverwriteError(
                f"Robot pose journal already exists: {path}"
            ) from exc
        published = True
        _fsync_directory(run_root)
        pending.unlink()
        return _PoseJournal(
            path=path,
            claim_id=claim.claim_id,
            run_id=run_id,
            started_at=started_at,
            handle=handle,
            fsync_every_poses=fsync_every_poses,
            fsync_interval_s=fsync_interval_s,
        )
    except BaseException:
        if handle is not None:
            handle.close()
        elif descriptor >= 0:
            os.close(descriptor)
        pending.unlink(missing_ok=True)
        if published:
            path.unlink(missing_ok=True)
            _fsync_directory(run_root)
        raise


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
    journal_path: Path | None = None,
    journal_claim_id: str | None = None,
    recovered_claim_path: Path | None = None,
    recovered_claim_id: str | None = None,
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
    if journal_path is not None:
        evidence["journal"] = {
            "schema_version": POSE_JOURNAL_SCHEMA_VERSION,
            "path": journal_path.name,
            "claim_id": journal_claim_id,
            "committed_pose_count": len(poses),
        }
    if recovered_claim_path is not None:
        evidence["recovered_claim"] = {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "path": recovered_claim_path.name,
            "claim_id": recovered_claim_id,
        }
    atomic_write_json(path, evidence, indent=2, sort_keys=False)
    _fsync_directory(run_root)

    set_manifest_artifact(manifest, path.name, path, run_root=run_root)
    artifacts = _stage_artifact_paths(manifest, run_root)
    artifacts[path.name] = path
    if journal_path is not None:
        artifacts[journal_path.name] = journal_path
    if recovered_claim_path is not None:
        artifacts[recovered_claim_path.name] = recovered_claim_path
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


def _existing_pose_evidence(run_root: Path) -> list[Path]:
    candidates = [
        run_root / RAW_ROBOT_EE_POSES,
        *run_root.glob("raw_robot_ee_poses.partial.*.json"),
        *run_root.glob("raw_robot_ee_poses.journal.*.recovered.jsonl"),
        *run_root.glob("raw_robot_ee_poses.claim.*.recovered.json"),
    ]
    return sorted(
        {candidate for candidate in candidates if os.path.lexists(candidate)},
        key=lambda candidate: candidate.name,
    )


def _claim_raw_pose_artifact(
    path: Path,
    *,
    expected_run_id: str,
) -> RawPoseClaim:
    """Recover stale ownership and reserve the canonical artifact exclusively."""

    receiver_lock_fd = _acquire_pose_receiver_lock(path.parent)
    claim: RawPoseClaim | None = None
    try:
        _recover_pose_journals_locked(
            path.parent,
            expected_run_id=expected_run_id,
        )
        blockers = _existing_pose_evidence(path.parent)
        if blockers:
            raise PoseReceiverOverwriteError(
                "Existing robot pose evidence requires a fresh run: "
                + ", ".join(candidate.as_posix() for candidate in blockers)
            )
        claim = RawPoseClaim(
            path=path.with_name(RAW_POSE_CLAIM_FILE),
            raw_pose_path=path,
            claim_id=uuid.uuid4().hex,
            receiver_lock_fd=receiver_lock_fd,
        )
        payload = {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "status": "reserved",
            "claim_id": claim.claim_id,
            "run_id": expected_run_id,
            "owner_pid": os.getpid(),
            "created_at": _now(),
        }
        try:
            descriptor = os.open(
                claim.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError as exc:
            raise PoseReceiverOverwriteError(
                f"Robot pose receiver already owns this run: {claim.path}"
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
                current_inode = claim.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    current_inode.st_dev == claimed_inode.st_dev
                    and current_inode.st_ino == claimed_inode.st_ino
                ):
                    claim.path.unlink(missing_ok=True)
            raise
        _fsync_directory(claim.path.parent)
        if claim.raw_pose_path.exists() or claim.raw_pose_path.is_symlink():
            claim.path.unlink(missing_ok=True)
            _fsync_directory(claim.path.parent)
            raise PoseReceiverOverwriteError(
                "Raw pose artifact appeared while reserving the receiver; refusing "
                f"to continue: {claim.raw_pose_path}"
            )
        return claim
    except BaseException:
        if claim is not None:
            claim.receiver_lock_fd = None
        _release_pose_receiver_lock(receiver_lock_fd)
        raise


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
    """Install complete pose data only while this receiver owns the claim."""

    if not _owns_raw_pose_claim(claim):
        raise PoseReceiverOverwriteError(
            "Raw pose reservation ownership changed before promotion; refusing "
            f"to replace {claim.raw_pose_path}."
        )
    if claim.raw_pose_path.exists() or claim.raw_pose_path.is_symlink():
        raise PoseReceiverOverwriteError(
            f"Refusing to replace existing raw pose artifact: {claim.raw_pose_path}"
        )
    pending = claim.raw_pose_path.with_name(
        f".{claim.raw_pose_path.name}.{claim.claim_id}.{uuid.uuid4().hex}.pending"
    )
    atomic_write_json(pending, dict(poses), indent=4, sort_keys=False)
    try:
        if not _owns_raw_pose_claim(claim):
            raise PoseReceiverOverwriteError(
                "Raw pose reservation ownership changed during promotion; "
                f"refusing to replace {claim.raw_pose_path}."
            )
        try:
            os.link(pending, claim.raw_pose_path)
        except FileExistsError as exc:
            raise PoseReceiverOverwriteError(
                "Raw pose artifact appeared during promotion; refusing to replace "
                f"{claim.raw_pose_path}."
            ) from exc
        _fsync_directory(claim.raw_pose_path.parent)
        _receiver_startup_boundary("canonical_durable_before_claim_cleanup")
        _cleanup_raw_pose_claim(claim)
    finally:
        pending.unlink(missing_ok=True)
    return claim.raw_pose_path


def _cleanup_raw_pose_claim(claim: RawPoseClaim) -> None:
    """Remove a failed receiver's reservation, but never a foreign artifact."""

    try:
        if _owns_raw_pose_claim(claim):
            claim.path.unlink(missing_ok=True)
            _fsync_directory(claim.path.parent)
    finally:
        receiver_lock_fd = claim.receiver_lock_fd
        claim.receiver_lock_fd = None
        _release_pose_receiver_lock(receiver_lock_fd)


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


def _stored_source_packet(
    value: object,
    *,
    packet_kind: str,
    run_id: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Robot pose journal source packet is invalid")
    source = dict(value)
    normalized = _packet_metadata(
        {**source, "motion": "end" if packet_kind == "end" else "pose"}
    )
    if normalized["packet_kind"] != packet_kind or normalized["run_id"] != run_id:
        raise ValueError("Robot pose journal source packet identity is invalid")
    for key in ("sequence_delta", "estimated_packets_lost"):
        field = source.get(key)
        if isinstance(field, bool) or not isinstance(field, int) or field < 0:
            raise ValueError(
                f"Robot pose journal source packet {key} is invalid"
            )
    return source


def _journal_pose_record(value: object, *, run_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Robot pose journal pose record is invalid")
    record = dict(value)
    expected = {
        "framename",
        "host_received_timestamp_ns",
        "host_wall_timestamp_ns",
        "frame_delta",
        "motion",
        "pose",
        "source_packet",
    }
    if set(record) != expected:
        raise ValueError("Robot pose journal pose record fields are invalid")
    for key in (
        "framename",
        "host_received_timestamp_ns",
        "host_wall_timestamp_ns",
    ):
        field = record.get(key)
        if isinstance(field, bool) or not isinstance(field, int) or field < 0:
            raise ValueError(f"Robot pose journal {key} is invalid")
    frame_delta = record.get("frame_delta")
    if isinstance(frame_delta, bool) or not isinstance(frame_delta, int):
        raise ValueError("Robot pose journal frame_delta is invalid")
    motion = record.get("motion")
    if not isinstance(motion, str) or not motion.strip() or motion == "end":
        raise ValueError("Robot pose journal motion is invalid")
    pose = record.get("pose")
    if not isinstance(pose, Mapping) or set(pose) != {"X", "Y", "Z", "A", "B", "C"}:
        raise ValueError("Robot pose journal pose coordinates are invalid")
    for axis, coordinate in pose.items():
        if (
            isinstance(coordinate, bool)
            or not isinstance(coordinate, (int, float))
            or not math.isfinite(float(coordinate))
        ):
            raise ValueError(f"Robot pose journal coordinate {axis} is invalid")
    record["source_packet"] = _stored_source_packet(
        record["source_packet"],
        packet_kind="pose",
        run_id=run_id,
    )
    return record


def _read_pose_journal(path: Path) -> PoseJournalSnapshot:
    """Read only the prefix covered by the last valid fsynced commit marker."""

    match = POSE_JOURNAL_PATTERN.fullmatch(path.name)
    if match is None or path.is_symlink() or not path.is_file():
        raise ValueError(f"Robot pose journal path is invalid: {path}")
    with open(path, "rb") as handle:
        header_line = handle.readline()
        if not header_line.endswith(b"\n"):
            raise ValueError("Robot pose journal header is incomplete")
        try:
            header = json.loads(header_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Robot pose journal header is invalid") from exc
        if (
            not isinstance(header, Mapping)
            or header.get("schema_version") != POSE_JOURNAL_SCHEMA_VERSION
            or header.get("record_kind") != "header"
            or set(header)
            != {"schema_version", "record_kind", "claim_id", "run_id", "started_at"}
            or header.get("claim_id") != match.group(1)
            or not isinstance(header.get("started_at"), str)
        ):
            raise ValueError("Robot pose journal header is invalid")
        try:
            canonical_run_id = str(uuid.UUID(str(header.get("run_id", ""))))
        except ValueError as exc:
            raise ValueError("Robot pose journal run identity is invalid") from exc
        if header.get("run_id") != canonical_run_id:
            raise ValueError("Robot pose journal run identity is invalid")

        parsed_poses: list[dict[str, Any]] = []
        committed_poses: dict[int, dict[str, Any]] = {}
        pending_end: dict[str, Any] | None = None
        terminal_accepted = False
        previous_source_sequence: int | None = None
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            try:
                raw = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                break
            if (
                not isinstance(raw, Mapping)
                or raw.get("schema_version") != POSE_JOURNAL_SCHEMA_VERSION
                or terminal_accepted
            ):
                break
            kind = raw.get("record_kind")
            try:
                if kind == "pose":
                    if pending_end is not None or set(raw) != {
                        "schema_version",
                        "record_kind",
                        "pose_index",
                        "pose",
                    }:
                        break
                    index = raw.get("pose_index")
                    if (
                        isinstance(index, bool)
                        or not isinstance(index, int)
                        or index != len(parsed_poses)
                    ):
                        break
                    pose_record = _journal_pose_record(
                        raw.get("pose"), run_id=canonical_run_id
                    )
                    source = pose_record["source_packet"]
                    sequence = int(source["sequence"])
                    expected_delta = (
                        0
                        if previous_source_sequence is None
                        else sequence - previous_source_sequence
                    )
                    if (
                        (previous_source_sequence is not None and expected_delta <= 0)
                        or source["sequence_delta"] != expected_delta
                        or source["estimated_packets_lost"]
                        != max(0, expected_delta - 1)
                    ):
                        break
                    previous_source_sequence = sequence
                    parsed_poses.append(pose_record)
                elif kind == "end":
                    if pending_end is not None or set(raw) != {
                        "schema_version",
                        "record_kind",
                        "pose_count",
                        "source_packet",
                        "accepted_at",
                    }:
                        break
                    if raw.get("pose_count") != len(parsed_poses) or not isinstance(
                        raw.get("accepted_at"), str
                    ):
                        break
                    end_packet = _stored_source_packet(
                        raw.get("source_packet"),
                        packet_kind="end",
                        run_id=canonical_run_id,
                    )
                    end_sequence = int(end_packet["sequence"])
                    expected_delta = (
                        0
                        if previous_source_sequence is None
                        else end_sequence - previous_source_sequence
                    )
                    if (
                        previous_source_sequence is None
                        or expected_delta <= 0
                        or end_packet["sequence_delta"] != expected_delta
                        or end_packet["estimated_packets_lost"]
                        != max(0, expected_delta - 1)
                    ):
                        break
                    pending_end = end_packet
                elif kind == "commit":
                    if set(raw) != {
                        "schema_version",
                        "record_kind",
                        "committed_pose_count",
                        "last_pose_index",
                        "terminal_accepted",
                        "committed_at",
                    }:
                        break
                    count = raw.get("committed_pose_count")
                    terminal = raw.get("terminal_accepted")
                    if (
                        isinstance(count, bool)
                        or not isinstance(count, int)
                        or count <= 0
                        or count != len(parsed_poses)
                        or raw.get("last_pose_index") != count - 1
                        or type(terminal) is not bool
                        or not isinstance(raw.get("committed_at"), str)
                        or terminal != (pending_end is not None)
                    ):
                        break
                    committed_poses = {
                        index: dict(record)
                        for index, record in enumerate(parsed_poses)
                    }
                    terminal_accepted = terminal
                    if terminal:
                        committed_poses[count - 1][STREAM_END_SOURCE_PACKET] = dict(
                            pending_end
                        )
                else:
                    break
            except (PoseReceiverPacketError, ValueError):
                break
    return PoseJournalSnapshot(
        path=path,
        claim_id=str(header["claim_id"]),
        run_id=canonical_run_id,
        started_at=str(header["started_at"]),
        poses=committed_poses,
        terminal_accepted=terminal_accepted,
    )


@contextmanager
def _locked_existing_journal(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PoseReceiverOverwriteError(
                f"Robot pose journal is owned by an active receiver: {path}"
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _install_recovered_raw_poses(
    raw_pose_path: Path,
    poses: Mapping[int, Mapping[str, Any]],
) -> None:
    pending = raw_pose_path.with_name(
        f".{raw_pose_path.name}.{uuid.uuid4().hex}.recovered"
    )
    atomic_write_json(pending, dict(poses), indent=4, sort_keys=False)
    try:
        try:
            os.link(pending, raw_pose_path)
        except FileExistsError as exc:
            raise PoseReceiverOverwriteError(
                f"Refusing to replace existing raw pose artifact: {raw_pose_path}"
            ) from exc
        _fsync_directory(raw_pose_path.parent)
    finally:
        pending.unlink(missing_ok=True)


def _canonical_matches_snapshot(
    raw_pose_path: Path,
    snapshot: PoseJournalSnapshot,
) -> bool:
    if not raw_pose_path.is_file() or raw_pose_path.is_symlink():
        return False
    try:
        with open(raw_pose_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return value == {str(index): record for index, record in snapshot.poses.items()}


def _record_complete_pose_manifest(
    manifest: dict[str, Any],
    run_root: Path,
    raw_pose_path: Path,
    *,
    pose_count: int,
    recovered: bool,
) -> None:
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
        message=(
            "Recovered a complete robot pose stream after receiver interruption."
            if recovered
            else f"Captured {pose_count} robot poses."
        ),
    )
    write_run_manifest(manifest, run_root)


def _recover_pose_journal(
    run_root: Path,
    journal_path: Path,
    *,
    manifest: dict[str, Any],
    partial_status: str,
    message: str,
    expected_run_id: str,
    last_packet_preview: str | None = None,
    last_sender: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    recovered_path = _recovered_journal_path(journal_path)
    with _locked_existing_journal(journal_path):
        snapshot = _read_pose_journal(journal_path)
        if snapshot.run_id != expected_run_id:
            raise ValueError(
                "Robot pose journal run identity does not match run_config.json"
            )
        raw_pose_path = run_root / RAW_ROBOT_EE_POSES
        claim = RawPoseClaim(
            path=run_root / RAW_POSE_CLAIM_FILE,
            raw_pose_path=raw_pose_path,
            claim_id=snapshot.claim_id,
        )
        owns_claim = _owns_raw_pose_claim(claim)
        if raw_pose_path.exists() or raw_pose_path.is_symlink():
            if snapshot.terminal_accepted and _canonical_matches_snapshot(
                raw_pose_path, snapshot
            ):
                _record_complete_pose_manifest(
                    manifest,
                    run_root,
                    raw_pose_path,
                    pose_count=len(snapshot.poses),
                    recovered=True,
                )
                if owns_claim:
                    _cleanup_raw_pose_claim(claim)
                journal_path.unlink()
                _fsync_directory(run_root)
                return {
                    "status": "complete",
                    "pose_count": len(snapshot.poses),
                    "artifact": raw_pose_path,
                }
            raise PoseReceiverOverwriteError(
                "Robot pose journal recovery found a foreign canonical artifact"
            )
        if snapshot.terminal_accepted:
            if owns_claim:
                _promote_raw_pose_claim(claim, snapshot.poses)
            else:
                _install_recovered_raw_poses(raw_pose_path, snapshot.poses)
            _record_complete_pose_manifest(
                manifest,
                run_root,
                raw_pose_path,
                pose_count=len(snapshot.poses),
                recovered=True,
            )
            journal_path.unlink()
            _fsync_directory(run_root)
            return {
                "status": "complete",
                "pose_count": len(snapshot.poses),
                "artifact": raw_pose_path,
            }

        if recovered_path.exists() or recovered_path.is_symlink():
            raise PoseReceiverOverwriteError(
                f"Recovered robot pose journal already exists: {recovered_path}"
            )
        partial_path = _write_partial_evidence(
            manifest,
            run_root,
            status=partial_status,
            message=message,
            poses=snapshot.poses,
            started_at=snapshot.started_at,
            last_packet_preview=last_packet_preview,
            last_sender=last_sender,
            journal_path=recovered_path,
            journal_claim_id=snapshot.claim_id,
        )
        if owns_claim:
            _cleanup_raw_pose_claim(claim)
        os.replace(journal_path, recovered_path)
        _fsync_directory(run_root)
        return {
            "status": "partial",
            "pose_count": len(snapshot.poses),
            "artifact": partial_path,
            "journal": recovered_path,
        }


def _validated_stale_claim(
    claim_path: Path,
    *,
    expected_run_id: str,
) -> dict[str, Any]:
    if claim_path.is_symlink() or not claim_path.is_file():
        raise PoseReceiverOverwriteError(
            f"Robot pose receiver claim is not a regular file: {claim_path}"
        )
    try:
        with open(claim_path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PoseReceiverOverwriteError(
            f"Robot pose receiver claim is unreadable and was preserved: {claim_path}"
        ) from exc
    expected_fields = {
        "schema_version",
        "status",
        "claim_id",
        "run_id",
        "owner_pid",
        "created_at",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise PoseReceiverOverwriteError(
            f"Robot pose receiver claim is invalid and was preserved: {claim_path}"
        )
    claim_id = value.get("claim_id")
    owner_pid = value.get("owner_pid")
    if (
        value.get("schema_version") != CLAIM_SCHEMA_VERSION
        or value.get("status") != "reserved"
        or not isinstance(claim_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", claim_id) is None
        or value.get("run_id") != expected_run_id
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or not isinstance(value.get("created_at"), str)
        or not value["created_at"]
    ):
        raise PoseReceiverOverwriteError(
            f"Robot pose receiver claim is invalid and was preserved: {claim_path}"
        )
    return value


def _recover_stale_pose_claim(
    run_root: Path,
    *,
    manifest: dict[str, Any],
    expected_run_id: str,
) -> dict[str, Any] | None:
    claim_path = run_root / RAW_POSE_CLAIM_FILE
    if not os.path.lexists(claim_path):
        return None
    value = _validated_stale_claim(
        claim_path,
        expected_run_id=expected_run_id,
    )
    claim_id = str(value["claim_id"])
    recovered_path = run_root / f"raw_robot_ee_poses.claim.{claim_id}.recovered.json"
    if os.path.lexists(recovered_path):
        raise PoseReceiverOverwriteError(
            f"Recovered robot pose claim already exists: {recovered_path}"
        )
    os.replace(claim_path, recovered_path)
    _fsync_directory(run_root)

    raw_pose_path = run_root / RAW_ROBOT_EE_POSES
    if raw_pose_path.exists() or raw_pose_path.is_symlink():
        return {
            "status": "claim_recovered",
            "pose_count": 0,
            "artifact": recovered_path,
        }

    message = (
        "Recovered an inactive robot-pose receiver reservation created before "
        "its durable packet journal was published. No pose packet was claimed."
    )
    partial_path = _write_partial_evidence(
        manifest,
        run_root,
        status="failed",
        message=message,
        poses={},
        started_at=str(value["created_at"]),
        last_packet_preview=None,
        last_sender=None,
        recovered_claim_path=recovered_path,
        recovered_claim_id=claim_id,
    )
    return {
        "status": "claim_recovered",
        "pose_count": 0,
        "artifact": partial_path,
        "claim": recovered_path,
    }


def _recover_pose_journals_locked(
    run_root: str | Path,
    *,
    expected_run_id: str | None = None,
) -> list[dict[str, Any]]:
    root = Path(run_root)
    journals = [
        path
        for path in sorted(root.glob("raw_robot_ee_poses.journal.*.jsonl"))
        if POSE_JOURNAL_PATTERN.fullmatch(path.name)
    ]
    claim_path = root / RAW_POSE_CLAIM_FILE
    if not journals and not os.path.lexists(claim_path):
        return []
    if expected_run_id is None:
        from posetestbot.pipeline.run_config import load_run_config_for_run_root

        expected_run_id = str(load_run_config_for_run_root(root)["run_id"])
    manifest = load_or_create_run_manifest(root)
    recoveries = [
        _recover_pose_journal(
            root,
            path,
            manifest=manifest,
            partial_status="failed",
            message="Recovered journal-committed robot poses after receiver loss.",
            expected_run_id=expected_run_id,
        )
        for path in journals
    ]
    stale_claim = _recover_stale_pose_claim(
        root,
        manifest=manifest,
        expected_run_id=expected_run_id,
    )
    if stale_claim is not None:
        recoveries.append(stale_claim)
    return recoveries


def recover_pose_journals(
    run_root: str | Path,
    *,
    expected_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recover inactive current-schema journals and orphaned claims exclusively."""

    root = Path(run_root)
    if not root.is_dir():
        return []
    if not os.path.lexists(root / RAW_POSE_CLAIM_FILE) and not any(
        POSE_JOURNAL_PATTERN.fullmatch(path.name)
        for path in root.glob("raw_robot_ee_poses.journal.*.jsonl")
    ):
        return []
    receiver_lock_fd = _acquire_pose_receiver_lock(root)
    try:
        return _recover_pose_journals_locked(
            root,
            expected_run_id=expected_run_id,
        )
    finally:
        _release_pose_receiver_lock(receiver_lock_fd)


def _commit_and_recover_pose_journal(
    journal: _PoseJournal,
    *,
    run_root: Path,
    manifest: dict[str, Any] | None,
    partial_status: str,
    message: str,
    last_packet_preview: str | None,
    last_sender: tuple[Any, ...] | None,
) -> dict[str, Any]:
    commit_error: Exception | None = None
    try:
        journal.commit()
    except Exception as exc:
        commit_error = exc
    finally:
        journal.close()
    recovery = _recover_pose_journal(
        run_root,
        journal.path,
        manifest=manifest or load_or_create_run_manifest(run_root),
        partial_status=partial_status,
        message=(
            message
            if commit_error is None
            else f"{message} Final journal commit also failed: {commit_error}"
        ),
        expected_run_id=journal.run_id,
        last_packet_preview=last_packet_preview,
        last_sender=last_sender,
    )
    if commit_error is not None:
        recovery["commit_error"] = f"{type(commit_error).__name__}: {commit_error}"
    return recovery


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
    claim = _claim_raw_pose_artifact(
        raw_pose_path,
        expected_run_id=run_id,
    )

    started_at = _now()
    try:
        _receiver_startup_boundary("claim_durable_before_journal")
        journal = _create_pose_journal(
            run_root,
            claim,
            run_id=run_id,
            started_at=started_at,
        )
    except BaseException:
        _cleanup_raw_pose_claim(claim)
        raise
    manifest: dict[str, Any] | None = None
    poses: dict[int, dict[str, Any]] = {}
    previous_frame_ts = 0
    last_packet_preview: str | None = None
    last_sender: tuple[Any, ...] | None = None
    start_message: Mapping[str, Any] = {}
    sender_stream_identity: dict[str, Any] | None = None
    previous_sender_sequence: int | None = None
    last_accepted_pose_monotonic: float | None = None

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
                            journal.commit()
                            if last_accepted_pose_monotonic is not None:
                                idle_elapsed = (
                                    time.monotonic() - last_accepted_pose_monotonic
                                )
                                idle_remaining = receive_idle_timeout_s - idle_elapsed
                                if idle_remaining > 0:
                                    sock.settimeout(idle_remaining)
                                    continue
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
                        journal.append_end(source_packet)
                        poses[max(poses)][STREAM_END_SOURCE_PACKET] = dict(
                            source_packet
                        )
                        break

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
                    pose_index = len(poses)
                    journal.append_pose(pose_index, pose_record)
                    poses[pose_index] = pose_record
                    last_accepted_pose_monotonic = time.monotonic()
                    fsync_timeout = journal.pending_fsync_timeout_s()
                    sock.settimeout(
                        min(receive_idle_timeout_s, fsync_timeout)
                        if fsync_timeout is not None
                        else receive_idle_timeout_s
                    )

                    if verbose:
                        print(
                            f"framename: {framename}, addr: {sender}, "
                            f"motion: {motion}, pose: {pose}"
                        )
                    print(f"Received poses: {len(poses)}", end="\r", flush=True)

        journal.close()
        durable = _read_pose_journal(journal.path)
        if not durable.terminal_accepted or len(durable.poses) != len(poses):
            raise PoseReceiverError(
                "Robot pose journal did not durably commit the complete stream"
            )
        poses = {index: dict(record) for index, record in durable.poses.items()}
        _promote_raw_pose_claim(claim, poses)
        _record_complete_pose_manifest(
            manifest,
            run_root,
            raw_pose_path,
            pose_count=len(poses),
            recovered=False,
        )
        journal.path.unlink()
        _fsync_directory(run_root)
    except (PoseReceiverCanceled, KeyboardInterrupt, InterruptedError) as exc:
        canceled = (
            exc
            if isinstance(exc, PoseReceiverCanceled)
            else PoseReceiverCanceled("Robot pose capture was interrupted.")
        )
        try:
            _commit_and_recover_pose_journal(
                journal,
                run_root=run_root,
                manifest=manifest,
                partial_status="canceled",
                message=str(canceled),
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
            _commit_and_recover_pose_journal(
                journal,
                run_root=run_root,
                manifest=manifest,
                partial_status="failed",
                message=str(exc),
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
