from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from posetestbot.config import RobotProfile
from posetestbot.io.artifacts import DATASET_MANIFEST, RAW_ROBOT_EE_POSES
from posetestbot.pipeline.run_config import create_run_config, write_run_config
from posetestbot.robot.pose_receiver import (
    PARTIAL_SCHEMA_VERSION,
    POSE_PACKET_SCHEMA_VERSION,
    PoseReceiverPacketError,
    PoseReceiverPermissionError,
    PoseReceiverTimeout,
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


def test_receiver_accepts_only_current_packets_and_writes_canonical_raw(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "success"
    run_id = current_run(run_root)
    sock = FakeDatagramSocket(
        [
            (packet(run_id, sequence=10), ("192.0.2.10", 40001)),
            (packet(run_id, sequence=12), ("192.0.2.10", 40001)),
            (packet(run_id, sequence=13, motion="end"), ("192.0.2.10", 40001)),
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
    assert sock.timeouts == [1.25, 2.5]
    assert starts[0]["run_id"] == run_id
    assert result.raw_pose_path == run_root / RAW_ROBOT_EE_POSES
    assert result.pose_count == 2
    saved = json.loads(result.raw_pose_path.read_text())
    assert saved["0"]["source_packet"]["run_id"] == run_id
    assert saved["0"]["source_packet"]["sequence_delta"] == 0
    assert saved["1"]["source_packet"]["estimated_packets_lost"] == 1
    assert stage(run_root)["status"] == "succeeded"


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
