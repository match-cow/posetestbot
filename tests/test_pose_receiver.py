from __future__ import annotations

import json
import multiprocessing
import os
import signal
import socket
from pathlib import Path
from typing import Any

import pytest

from posetestbot.robot import pose_receiver as receiver_module
from posetestbot.config import RobotProfile
from posetestbot.io.artifacts import DATASET_MANIFEST, RAW_ROBOT_EE_POSES
from posetestbot.pipeline.run_config import create_run_config, write_run_config
from posetestbot.robot.pose_receiver import (
    CLAIM_SCHEMA_VERSION,
    JOURNAL_FSYNC_INTERVAL_S,
    PARTIAL_SCHEMA_VERSION,
    POSE_PACKET_SCHEMA_VERSION,
    POSE_JOURNAL_SCHEMA_VERSION,
    RAW_POSE_CLAIM_FILE,
    PoseReceiverCanceled,
    PoseReceiverOverwriteError,
    PoseReceiverPacketError,
    PoseReceiverPermissionError,
    PoseReceiverTimeout,
    recover_pose_journals,
    run_pose_receiver,
)
from posetestbot.robot.reference_frames import POSE_TEMPLATE_BASE_SUNRISE_PATH


class FakeDatagramSocket:
    def __init__(self, events: list[Any], *, on_bind=None) -> None:
        self.events = list(events)
        self.on_bind = on_bind
        self.bound_to: tuple[str, int] | None = None
        self.timeouts: list[float] = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def bind(self, address: tuple[str, int]) -> None:
        if self.on_bind is not None:
            self.on_bind(address)
        self.bound_to = address

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def recvfrom(self, _size: int):
        if not self.events:
            raise AssertionError("Fake socket has no remaining receive event")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


class FakeSocketFactory:
    def __init__(self, sock: FakeDatagramSocket) -> None:
        self.sock = sock
        self.calls: list[tuple[int, int]] = []

    def __call__(self, family: int, socket_type: int) -> FakeDatagramSocket:
        self.calls.append((family, socket_type))
        return self.sock


def profile() -> RobotProfile:
    return RobotProfile(
        mode="real",
        robot_ip="192.0.2.10",
        command_port=30300,
        receiver_ip="127.0.0.1",
        receiver_port=18080,
        cartesian_velocity_m_s=0.02,
    )


def current_run(run_root: Path) -> str:
    config = create_run_config(
        run_root=run_root,
        capture_intent="dataset",
        bop_annotation_mode="none",
    )
    write_run_config(run_root, config)
    return config.run_id


def packet(run_id: str, *, sequence: int, motion: str = "capture_sweep") -> bytes:
    value: dict[str, Any] = {
        "schema_version": POSE_PACKET_SCHEMA_VERSION,
        "packet_kind": "end" if motion == "end" else "pose",
        "sequence": sequence,
        "sender_monotonic_ns": 1_000_000 + sequence,
        "sender_wall_timestamp_ms": 2_000_000 + sequence,
        "run_id": run_id,
        "motion": motion,
        "from_frame": "robot_flange",
        "to_frame": "template_base",
        "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
    }
    if motion != "end":
        value.update({"X": 1.0, "Y": 2.0, "Z": 3.0, "A": 0.1, "B": 0.2, "C": 0.3})
    return json.dumps(value).encode()


def journal_pose(run_id: str, sequence: int) -> dict[str, Any]:
    return {
        "framename": 2_000_000 + sequence,
        "host_received_timestamp_ns": 1_000_000 + sequence,
        "host_wall_timestamp_ns": 2_000_000_000_000 + sequence,
        "frame_delta": 0 if sequence == 0 else 1,
        "motion": "capture_sweep",
        "pose": {"X": 1.0, "Y": 2.0, "Z": 3.0, "A": 0.1, "B": 0.2, "C": 0.3},
        "source_packet": {
            "schema_version": POSE_PACKET_SCHEMA_VERSION,
            "packet_kind": "pose",
            "sequence": sequence,
            "sender_monotonic_ns": 1_000_000 + sequence,
            "sender_wall_timestamp_ms": 2_000_000 + sequence,
            "run_id": run_id,
            "from_frame": "robot_flange",
            "to_frame": "template_base",
            "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
            "sequence_delta": 0 if sequence == 0 else 1,
            "estimated_packets_lost": 0,
        },
    }


def _abrupt_journal_writer(
    run_root_value: str,
    run_id: str,
    pose_count: int,
    terminal: bool,
) -> None:
    from posetestbot.robot import pose_receiver as receiver_module

    run_root = Path(run_root_value)
    claim = receiver_module._claim_raw_pose_artifact(
        run_root / RAW_ROBOT_EE_POSES,
        expected_run_id=run_id,
    )
    journal = receiver_module._create_pose_journal(
        run_root,
        claim,
        run_id=run_id,
        started_at="2026-08-22T12:00:00+00:00",
        fsync_every_poses=256,
        fsync_interval_s=3600.0,
    )
    for sequence in range(pose_count):
        journal.append_pose(sequence, journal_pose(run_id, sequence))
    if terminal:
        journal.append_end(
            {
                "schema_version": POSE_PACKET_SCHEMA_VERSION,
                "packet_kind": "end",
                "sequence": pose_count,
                "sender_monotonic_ns": 1_000_000 + pose_count,
                "sender_wall_timestamp_ms": 2_000_000 + pose_count,
                "run_id": run_id,
                "from_frame": "robot_flange",
                "to_frame": "template_base",
                "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
                "sequence_delta": 1,
                "estimated_packets_lost": 0,
            }
        )
    os._exit(23)


def _kill_receiver_after_durable_claim(run_root_value: str, run_id: str) -> None:
    from posetestbot.robot import pose_receiver as receiver_module

    def kill_at_boundary(label: str) -> None:
        if label == "claim_durable_before_journal":
            os.kill(os.getpid(), signal.SIGKILL)

    receiver_module._receiver_startup_boundary = kill_at_boundary
    receiver_module.run_pose_receiver(
        Path(run_root_value),
        profile=profile(),
        run_id=run_id,
        allow_real_robot=True,
        allow_cameras=True,
        socket_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receiver must die before socket creation")
        ),
        send_start_command=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receiver must die before robot START")
        ),
        install_signal_handlers=False,
    )


def _kill_receiver_after_canonical_link(run_root_value: str, run_id: str) -> None:
    from posetestbot.robot import pose_receiver as receiver_module

    run_root = Path(run_root_value)
    claim = receiver_module._claim_raw_pose_artifact(
        run_root / RAW_ROBOT_EE_POSES,
        expected_run_id=run_id,
    )
    journal = receiver_module._create_pose_journal(
        run_root,
        claim,
        run_id=run_id,
        started_at="2026-08-22T12:00:00+00:00",
    )
    journal.append_pose(0, journal_pose(run_id, 0))
    journal.append_end(
        {
            "schema_version": POSE_PACKET_SCHEMA_VERSION,
            "packet_kind": "end",
            "sequence": 1,
            "sender_monotonic_ns": 1_000_001,
            "sender_wall_timestamp_ms": 2_000_001,
            "run_id": run_id,
            "from_frame": "robot_flange",
            "to_frame": "template_base",
            "sunrise_reference_frame_path": POSE_TEMPLATE_BASE_SUNRISE_PATH,
            "sequence_delta": 1,
            "estimated_packets_lost": 0,
        }
    )
    snapshot = receiver_module._read_pose_journal(journal.path)

    def kill_at_boundary(label: str) -> None:
        if label == "canonical_durable_before_claim_cleanup":
            os.kill(os.getpid(), signal.SIGKILL)

    receiver_module._receiver_startup_boundary = kill_at_boundary
    receiver_module._promote_raw_pose_claim(claim, snapshot.poses)


def stage(run_root: Path) -> dict[str, Any]:
    manifest = json.loads((run_root / DATASET_MANIFEST).read_text())
    return next(
        item for item in manifest["stages"] if item["name"] == "robot_pose_capture"
    )


@pytest.mark.parametrize(
    ("allow_real_robot", "allow_cameras"),
    [(False, False), (1, True)],
)
def test_receiver_requires_literal_fresh_acknowledgements_before_socket_io(
    tmp_path: Path,
    allow_real_robot: Any,
    allow_cameras: Any,
) -> None:
    run_root = tmp_path / "blocked"
    socket_factory = FakeSocketFactory(FakeDatagramSocket([]))
    starts: list[object] = []

    with pytest.raises(PoseReceiverPermissionError, match="fresh acknowledgements"):
        run_pose_receiver(
            run_root,
            profile=profile(),
            run_id="11111111-1111-4111-8111-111111111111",
            allow_real_robot=allow_real_robot,
            allow_cameras=allow_cameras,
            socket_factory=socket_factory,
            send_start_command=lambda *args, **kwargs: starts.append((args, kwargs)),
            install_signal_handlers=False,
        )

    assert socket_factory.calls == []
    assert starts == []
    assert not run_root.exists()


def test_receiver_lock_rejects_path_replacement_after_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "lock-replacement"
    run_root.mkdir()
    lock_path = run_root / receiver_module.RAW_POSE_RECEIVER_LOCK_FILE
    real_flock = receiver_module.fcntl.flock

    def replace_lock_path(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        lock_path.unlink()
        lock_path.write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(receiver_module.fcntl, "flock", replace_lock_path)

    with pytest.raises(PoseReceiverOverwriteError, match="changed while locking"):
        receiver_module._acquire_pose_receiver_lock(run_root)

    assert lock_path.read_text(encoding="utf-8") == "replacement"


def test_receiver_accepts_only_current_packets_and_writes_canonical_raw(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "success"
    run_id = current_run(run_root)
    sock = FakeDatagramSocket(
        [
            (packet(run_id, sequence=10), ("192.0.2.10", 40001)),
            (packet(run_id, sequence=12), ("192.0.2.10", 40001)),
            (packet(run_id, sequence=15, motion="end"), ("192.0.2.10", 40001)),
        ]
    )
    starts: list[dict[str, Any]] = []

    def fake_start(robot: RobotProfile, *, run_id: str, maximum_velocity_m_s: float):
        starts.append(
            {
                "robot": robot,
                "run_id": run_id,
                "maximum_velocity_m_s": maximum_velocity_m_s,
            }
        )
        return {"schema_version": "robot_command.v1", "run_id": run_id}

    result = run_pose_receiver(
        run_root,
        profile=profile(),
        run_id=run_id,
        allow_real_robot=True,
        allow_cameras=True,
        receive_start_timeout_s=1.25,
        receive_idle_timeout_s=2.5,
        socket_factory=FakeSocketFactory(sock),
        send_start_command=fake_start,
        install_signal_handlers=False,
    )

    assert sock.bound_to == ("127.0.0.1", 18080)
    assert sock.timeouts[0] == 1.25
    assert len(sock.timeouts) == 3
    assert all(
        0 < timeout <= JOURNAL_FSYNC_INTERVAL_S for timeout in sock.timeouts[1:]
    )
    assert starts[0]["run_id"] == run_id
    assert result.raw_pose_path == run_root / RAW_ROBOT_EE_POSES
    assert result.pose_count == 2
    saved = json.loads(result.raw_pose_path.read_text())
    assert saved["0"]["source_packet"]["run_id"] == run_id
    assert saved["0"]["source_packet"]["sequence_delta"] == 0
    assert saved["1"]["source_packet"]["estimated_packets_lost"] == 1
    assert saved["1"]["stream_end_source_packet"]["packet_kind"] == "end"
    assert saved["1"]["stream_end_source_packet"]["sequence"] == 15
    assert saved["1"]["stream_end_source_packet"]["sequence_delta"] == 3
    assert saved["1"]["stream_end_source_packet"]["estimated_packets_lost"] == 2
    assert stage(run_root)["status"] == "succeeded"
    assert not (run_root / RAW_POSE_CLAIM_FILE).exists()
    assert not list(run_root.glob("raw_robot_ee_poses.journal.*.jsonl"))


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            json.dumps(
                {"motion": "capture", "X": 1, "Y": 2, "Z": 3, "A": 0, "B": 0, "C": 0}
            ).encode(),
            "unsupported schema_version",
        ),
        (b"not-json", "invalid JSON"),
    ],
)
def test_receiver_rejects_legacy_or_malformed_packets_and_preserves_partial_evidence(
    tmp_path: Path,
    event: bytes,
    message: str,
) -> None:
    run_root = tmp_path / message.replace(" ", "-")
    run_id = current_run(run_root)
    sock = FakeDatagramSocket([(event, ("192.0.2.10", 40001))])

    with pytest.raises(PoseReceiverPacketError, match=message):
        run_pose_receiver(
            run_root,
            profile=profile(),
            run_id=run_id,
            allow_real_robot=True,
            allow_cameras=True,
            socket_factory=FakeSocketFactory(sock),
            send_start_command=lambda *_args, **_kwargs: {
                "schema_version": "robot_command.v1"
            },
            install_signal_handlers=False,
        )

    assert not (run_root / RAW_ROBOT_EE_POSES).exists()
    partials = list(run_root.glob("raw_robot_ee_poses.partial.*.json"))
    assert len(partials) == 1
    assert (
        json.loads(partials[0].read_text())["schema_version"] == PARTIAL_SCHEMA_VERSION
    )
    assert stage(run_root)["status"] == "failed"


def test_receiver_rejects_wrong_sender_ip(tmp_path: Path) -> None:
    run_root = tmp_path / "wrong-sender"
    run_id = current_run(run_root)
    sock = FakeDatagramSocket([(packet(run_id, sequence=0), ("192.0.2.99", 40001))])

    with pytest.raises(PoseReceiverPacketError, match="unexpected sender IP"):
        run_pose_receiver(
            run_root,
            profile=profile(),
            run_id=run_id,
            allow_real_robot=True,
            allow_cameras=True,
            socket_factory=FakeSocketFactory(sock),
            send_start_command=lambda *_args, **_kwargs: {
                "schema_version": "robot_command.v1"
            },
            install_signal_handlers=False,
        )


def test_receiver_timeout_preserves_failure_evidence(tmp_path: Path) -> None:
    run_root = tmp_path / "timeout"
    run_id = current_run(run_root)
    sock = FakeDatagramSocket([socket.timeout()])

    with pytest.raises(PoseReceiverTimeout, match="first robot pose"):
        run_pose_receiver(
            run_root,
            profile=profile(),
            run_id=run_id,
            allow_real_robot=True,
            allow_cameras=True,
            socket_factory=FakeSocketFactory(sock),
            send_start_command=lambda *_args, **_kwargs: {
                "schema_version": "robot_command.v1"
            },
            install_signal_handlers=False,
        )

    assert stage(run_root)["status"] == "failed"
    assert not (run_root / RAW_ROBOT_EE_POSES).exists()


def test_large_midstream_cancellation_materializes_every_committed_pose(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "large-cancellation"
    run_id = current_run(run_root)
    pose_count = 2057
    events = [
        (packet(run_id, sequence=sequence), ("192.0.2.10", 40001))
        for sequence in range(pose_count)
    ]
    events.append(PoseReceiverCanceled("injected mid-stream cancellation"))

    with pytest.raises(PoseReceiverCanceled, match="mid-stream cancellation"):
        run_pose_receiver(
            run_root,
            profile=profile(),
            run_id=run_id,
            allow_real_robot=True,
            allow_cameras=True,
            socket_factory=FakeSocketFactory(FakeDatagramSocket(events)),
            send_start_command=lambda *_args, **_kwargs: {
                "schema_version": "robot_command.v1"
            },
            install_signal_handlers=False,
        )

    assert not (run_root / RAW_ROBOT_EE_POSES).exists()
    partials = list(run_root.glob("raw_robot_ee_poses.partial.*.json"))
    assert len(partials) == 1
    partial = json.loads(partials[0].read_text())
    assert partial["received_pose_count"] == pose_count
    assert len(partial["poses"]) == pose_count
    assert partial["poses"][str(pose_count - 1)]["source_packet"]["sequence"] == (
        pose_count - 1
    )
    journal = partial["journal"]
    assert journal["schema_version"] == POSE_JOURNAL_SCHEMA_VERSION
    assert journal["committed_pose_count"] == pose_count
    assert (run_root / journal["path"]).is_file()
    assert stage(run_root)["status"] == "canceled"


def test_recovery_refuses_an_exclusively_locked_active_journal(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "active-journal"
    run_id = current_run(run_root)
    claim = receiver_module._claim_raw_pose_artifact(
        run_root / RAW_ROBOT_EE_POSES,
        expected_run_id=run_id,
    )
    journal = receiver_module._create_pose_journal(
        run_root,
        claim,
        run_id=run_id,
        started_at="2026-08-22T12:00:00+00:00",
    )
    try:
        assert not (run_root / RAW_ROBOT_EE_POSES).exists()
        assert (run_root / RAW_POSE_CLAIM_FILE).is_file()
        with pytest.raises(
            receiver_module.PoseReceiverOverwriteError,
            match="active receiver",
        ):
            recover_pose_journals(run_root)
    finally:
        journal.close()
        journal.path.unlink(missing_ok=True)
        receiver_module._cleanup_raw_pose_claim(claim)


def test_sigkill_between_claim_and_journal_recovers_retained_failure_evidence(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "abrupt-claim-only"
    run_id = current_run(run_root)
    process = multiprocessing.get_context("fork").Process(
        target=_kill_receiver_after_durable_claim,
        args=(run_root.as_posix(), run_id),
    )
    process.start()
    process.join(timeout=10)

    assert not process.is_alive()
    assert process.exitcode == -signal.SIGKILL
    assert (run_root / RAW_POSE_CLAIM_FILE).is_file()
    assert not list(run_root.glob("raw_robot_ee_poses.journal.*.jsonl"))
    assert not (run_root / RAW_ROBOT_EE_POSES).exists()

    recovered = recover_pose_journals(run_root, expected_run_id=run_id)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "claim_recovered"
    assert recovered[0]["pose_count"] == 0
    assert not (run_root / RAW_POSE_CLAIM_FILE).exists()
    recovered_claims = list(
        run_root.glob("raw_robot_ee_poses.claim.*.recovered.json")
    )
    assert len(recovered_claims) == 1
    claim = json.loads(recovered_claims[0].read_text())
    assert claim["schema_version"] == CLAIM_SCHEMA_VERSION
    assert claim["run_id"] == run_id
    partials = list(run_root.glob("raw_robot_ee_poses.partial.*.json"))
    assert len(partials) == 1
    partial = json.loads(partials[0].read_text())
    assert partial["received_pose_count"] == 0
    assert partial["recovered_claim"]["path"] == recovered_claims[0].name
    assert recover_pose_journals(run_root, expected_run_id=run_id) == []

    with pytest.raises(PoseReceiverOverwriteError, match="fresh run"):
        run_pose_receiver(
            run_root,
            profile=profile(),
            run_id=run_id,
            allow_real_robot=True,
            allow_cameras=True,
            socket_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("recovered claim evidence must block socket creation")
            ),
            send_start_command=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("recovered claim evidence must block robot START")
            ),
            install_signal_handlers=False,
        )


def test_sigkill_after_canonical_link_finishes_claim_and_journal_cleanup(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "abrupt-canonical-promotion"
    run_id = current_run(run_root)
    process = multiprocessing.get_context("fork").Process(
        target=_kill_receiver_after_canonical_link,
        args=(run_root.as_posix(), run_id),
    )
    process.start()
    process.join(timeout=10)

    assert not process.is_alive()
    assert process.exitcode == -signal.SIGKILL
    assert (run_root / RAW_ROBOT_EE_POSES).is_file()
    assert (run_root / RAW_POSE_CLAIM_FILE).is_file()
    assert len(list(run_root.glob("raw_robot_ee_poses.journal.*.jsonl"))) == 1

    recovered = recover_pose_journals(run_root, expected_run_id=run_id)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "complete"
    assert recovered[0]["pose_count"] == 1
    canonical = json.loads((run_root / RAW_ROBOT_EE_POSES).read_text())
    assert canonical["0"]["stream_end_source_packet"]["packet_kind"] == "end"
    assert not (run_root / RAW_POSE_CLAIM_FILE).exists()
    assert not list(run_root.glob("raw_robot_ee_poses.journal.*.jsonl"))
    assert stage(run_root)["status"] == "succeeded"


def test_abrupt_process_loss_recovers_only_last_durable_pose_prefix(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "abrupt-partial"
    run_id = current_run(run_root)
    process = multiprocessing.get_context("fork").Process(
        target=_abrupt_journal_writer,
        args=(run_root.as_posix(), run_id, 2050, False),
    )
    process.start()
    process.join(timeout=10)

    assert not process.is_alive()
    assert process.exitcode == 23
    assert not (run_root / RAW_ROBOT_EE_POSES).exists()
    claim = json.loads((run_root / RAW_POSE_CLAIM_FILE).read_text())
    assert claim["schema_version"] == CLAIM_SCHEMA_VERSION

    recovered = recover_pose_journals(run_root)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "partial"
    assert recovered[0]["pose_count"] == 2048
    assert not (run_root / RAW_ROBOT_EE_POSES).exists()
    assert not (run_root / RAW_POSE_CLAIM_FILE).exists()
    partial = json.loads(Path(recovered[0]["artifact"]).read_text())
    assert partial["received_pose_count"] == 2048
    assert partial["poses"]["2047"]["source_packet"]["sequence"] == 2047
    assert Path(recovered[0]["journal"]).is_file()
    assert recover_pose_journals(run_root) == []


def test_abrupt_loss_after_terminal_commit_recovers_canonical_atomically(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "abrupt-complete"
    run_id = current_run(run_root)
    process = multiprocessing.get_context("fork").Process(
        target=_abrupt_journal_writer,
        args=(run_root.as_posix(), run_id, 3, True),
    )
    process.start()
    process.join(timeout=10)

    assert not process.is_alive()
    assert process.exitcode == 23
    assert not (run_root / RAW_ROBOT_EE_POSES).exists()
    recovered = recover_pose_journals(run_root)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "complete"
    canonical = json.loads((run_root / RAW_ROBOT_EE_POSES).read_text())
    assert len(canonical) == 3
    assert canonical["2"]["stream_end_source_packet"]["packet_kind"] == "end"
    assert canonical["2"]["stream_end_source_packet"]["sequence"] == 3
    assert not (run_root / RAW_POSE_CLAIM_FILE).exists()
    assert not list(run_root.glob("raw_robot_ee_poses.journal.*.jsonl"))
    assert stage(run_root)["status"] == "succeeded"
