from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from aiortc import VideoStreamTrack
from aiortc.codecs import vpx as aiortc_vpx

from posetestbot.monitoring import smoke
from posetestbot.monitoring import webrtc
from posetestbot.sensors import v4l2_preview
from posetestbot.sensors.v4l2_preview import (
    V4L2IntegerControl,
    V4L2NodeCandidate,
    V4L2PreviewSelection,
)


def test_monitor_command_uses_fixed_webrtc_capture_defaults(tmp_path: Path) -> None:
    command = webrtc.build_monitor_webrtc_command(monitor_root=tmp_path / "monitor")

    assert command[:4] == ["uv", "run", "python", "scripts/run_monitor_webrtc.py"]
    assert command[command.index("--vendor-id") + 1] == "0c45"
    assert command[command.index("--product-id") + 1] == "2283"
    assert command[command.index("--width") + 1] == "640"
    assert command[command.index("--height") + 1] == "480"
    assert command[command.index("--fps") + 1] == "30"


def test_open_v4l2_capture_requests_mjpeg_and_one_frame_buffer(monkeypatch) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.settings: list[tuple[int, float | int]] = []

        def set(self, prop: int, value: float | int):
            self.settings.append((prop, value))
            return True

        def isOpened(self):
            return True

        def release(self):
            raise AssertionError("An opened capture must not be released")

    capture = FakeCapture()
    monkeypatch.setattr(v4l2_preview.cv2, "VideoCapture", lambda *_args: capture)

    opened = v4l2_preview.open_v4l2_capture(
        "/dev/video18",
        width=640,
        height=480,
        fps=30,
        pixel_format="MJPG",
    )

    assert opened is capture
    settings = dict(capture.settings)
    assert settings[
        v4l2_preview.cv2.CAP_PROP_FOURCC
    ] == v4l2_preview.cv2.VideoWriter_fourcc(*"MJPG")
    assert settings[v4l2_preview.cv2.CAP_PROP_FRAME_WIDTH] == 640.0
    assert settings[v4l2_preview.cv2.CAP_PROP_FRAME_HEIGHT] == 480.0
    assert settings[v4l2_preview.cv2.CAP_PROP_FPS] == 30.0
    assert settings[v4l2_preview.cv2.CAP_PROP_BUFFERSIZE] == 1


def test_bgr_frame_conversion_assigns_90khz_timestamps() -> None:
    image = np.zeros((3, 4, 3), dtype=np.uint8)
    first = webrtc.bgr_frame_to_av(image, frame_index=0, fps=30)
    second = webrtc.bgr_frame_to_av(image, frame_index=1, fps=30)

    assert first.width == 4
    assert first.height == 3
    assert first.format.name == "bgr24"
    assert first.pts == 0
    assert first.time_base == Fraction(1, 90_000)
    assert second.pts == 3_000
    assert second.time_base == Fraction(1, 90_000)


def test_monitor_track_preserves_pixels_for_browser_orientation(monkeypatch) -> None:
    image = np.array(
        [
            [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
            [[10, 11, 12], [13, 14, 15], [16, 17, 18]],
        ],
        dtype=np.uint8,
    )

    class FakeCapture:
        def __init__(self) -> None:
            self.released = False

        def read(self):
            return True, image.copy()

        def release(self) -> None:
            self.released = True

    capture = FakeCapture()

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(webrtc.asyncio, "to_thread", run_inline)

    async def receive_frame() -> np.ndarray:
        track = webrtc.OpenCVVideoTrack(
            capture,
            fps=30,
            selected_path="/dev/video18",
        )
        try:
            return (await track.recv()).to_ndarray(format="bgr24")
        finally:
            track.stop()

    assert np.array_equal(asyncio.run(receive_frame()), image)
    assert capture.released is True


def test_brightness_autocalibrator_converges_with_bounded_camera_updates() -> None:
    current = {"value": 0}
    statuses: list[dict] = []
    calibrator = webrtc.BrightnessAutoCalibrator(
        V4L2IntegerControl(
            name="brightness",
            minimum=0,
            maximum=255,
            step=1,
            default=128,
            value=0,
        ),
        set_value=lambda value: current.update(value=value) is None,
        on_status=statuses.append,
        settle_frames=1,
    )

    assert calibrator.request()["state"] == "queued"
    for _ in range(50):
        image = np.full((30, 40, 3), current["value"], dtype=np.uint8)
        calibrator.process_frame(image)
        if calibrator.snapshot()["state"] == "succeeded":
            break

    result = calibrator.snapshot()
    assert result["state"] == "succeeded"
    assert abs(result["measured_luma"] - webrtc.BRIGHTNESS_TARGET_LUMA) <= 6
    assert result["attempts"] <= webrtc.BRIGHTNESS_MAX_ADJUSTMENTS
    assert result["control"]["value"] == current["value"]
    assert statuses[-1]["state"] == "succeeded"


def test_vp8_packetization_leaves_tailscale_mtu_headroom(monkeypatch) -> None:
    monkeypatch.setattr(aiortc_vpx, "PACKET_MAX", 1300)

    configured = webrtc.configure_vp8_packet_size()
    payloads = aiortc_vpx.Vp8Encoder._packetize(bytes(5000), picture_id=1)

    assert configured == webrtc.VP8_PACKET_MAX_BYTES == 1100
    assert len(payloads) > 1
    assert max(map(len, payloads)) <= 1100


def test_worker_releases_camera_and_stops_signaling(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            self.released = True

    class FakeServer:
        instances: list["FakeServer"] = []

        def __init__(
            self,
            *,
            track_factory,
            on_peers_changed,
            on_camera_open_changed,
            on_error,
        ) -> None:
            self.track_factory = track_factory
            self.on_peers_changed = on_peers_changed
            self.stopped = False
            self.track = None
            self.instances.append(self)

        async def start(self) -> int:
            self.track = self.track_factory()
            return 34567

        async def stop(self) -> None:
            assert self.track is not None
            self.track.stop()
            self.stopped = True

    class FakeStunTransport:
        def close(self) -> None:
            pass

    async def start_stun(_port):
        return FakeStunTransport(), object(), 3478

    capture = FakeCapture()
    selection = V4L2PreviewSelection(
        path="/dev/video18",
        formats=("MJPG",),
        candidate=V4L2NodeCandidate("/dev/video18"),
        score=100,
    )
    monkeypatch.setattr(webrtc, "select_usb_rgb_node", lambda *_args: selection)
    monkeypatch.setattr(webrtc, "open_v4l2_capture", lambda *_args, **_kwargs: capture)
    monkeypatch.setattr(webrtc, "MonitorWebRTCServer", FakeServer)
    monkeypatch.setattr(webrtc, "start_stun_binding_responder", start_stun)

    async def run() -> int:
        stop_event = asyncio.Event()
        stop_event.set()
        return await webrtc.run_monitor_webrtc(tmp_path, stop_event=stop_event)

    assert asyncio.run(run()) == 0
    assert capture.released is True
    assert FakeServer.instances[0].stopped is True
    status = webrtc.load_monitor_status(tmp_path)
    assert status is not None
    assert status["status"] == "stopped"
    assert status["signaling_ready"] is False
    assert status["signaling_port"] is None


def test_monitor_status_rejects_pre_v2_schema(tmp_path: Path) -> None:
    (tmp_path / webrtc.MONITOR_STATUS_NAME).write_text(
        json.dumps({"schema_version": "monitor_webrtc.v1"})
    )

    with pytest.raises(ValueError, match="monitor_webrtc.v2"):
        webrtc.load_monitor_status(tmp_path)


def test_monitor_smoke_requires_operator_and_both_execution_gates() -> None:
    for missing in (
        "operator_authorized",
        "allow_cameras",
        "allow_real_robot",
    ):
        gates = {
            "operator_authorized": True,
            "allow_cameras": True,
            "allow_real_robot": True,
        }
        gates[missing] = False
        try:
            smoke.validate_execution_gates(**gates)
        except ValueError as exc:
            assert f"--{missing.replace('_', '-')}" in str(exc)
        else:
            raise AssertionError(f"Missing {missing} must prevent physical execution")

    smoke.validate_execution_gates(
        operator_authorized=True,
        allow_cameras=True,
        allow_real_robot=True,
    )


def test_monitor_smoke_plan_is_monitor_only(tmp_path: Path) -> None:
    plan = smoke.build_smoke_plan(tmp_path / "smoke")

    assert plan["mode"] == "monitor_only"
    assert plan["expected_node"] == "/dev/video0"
    assert plan["expected_capture"] == {
        "pixel_format": "MJPG",
        "width": 640,
        "height": 480,
        "fps": 30.0,
    }
    assert plan["worker_command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_monitor_webrtc.py",
    ]
    assert plan["robot_commands"] == []
    assert plan["acquisition_pipeline_commands"] == []


def test_parse_active_v4l2_monitor_negotiation() -> None:
    negotiated = smoke.parse_v4l2_negotiation(
        """
Format Video Capture:
    Width/Height      : 640/480
    Pixel Format      : 'MJPG' (Motion-JPEG)
Streaming Parameters Video Capture:
    Frames per second: 30.000 (30/1)
"""
    )

    assert negotiated == {
        "width": 640,
        "height": 480,
        "pixel_format": "MJPG",
        "fps": 30.0,
    }


def test_stun_binding_protocol_returns_xor_mapped_address() -> None:
    class Transport:
        def __init__(self) -> None:
            self.sent: list[tuple[bytes, tuple[str, int]]] = []

        def sendto(self, data, addr) -> None:
            self.sent.append((data, addr))

    request = webrtc.stun.Message(
        message_method=webrtc.stun.Method.BINDING,
        message_class=webrtc.stun.Class.REQUEST,
    )
    transport = Transport()
    protocol = webrtc.StunBindingProtocol()
    protocol.connection_made(transport)  # type: ignore[arg-type]
    protocol.datagram_received(bytes(request), ("10.145.8.50", 49152))

    assert len(transport.sent) == 1
    response = webrtc.stun.parse_message(transport.sent[0][0])
    assert response.message_class == webrtc.stun.Class.RESPONSE
    assert response.transaction_id == request.transaction_id
    assert response.attributes["XOR-MAPPED-ADDRESS"] == ("10.145.8.50", 49152)


def test_monitor_v2_health_rejects_stale_heartbeat_and_media() -> None:
    now = datetime.now(UTC)
    healthy = {
        "schema_version": webrtc.MONITOR_STATUS_SCHEMA,
        "status": "ready",
        "signaling_ready": True,
        "heartbeat_at": now.isoformat(),
        "peer_count": 0,
        "connected_peer_count": 0,
    }

    assert webrtc.monitor_status_health(healthy, now=now) == (True, None)

    stale_heartbeat = dict(healthy)
    stale_heartbeat["heartbeat_at"] = (now - timedelta(seconds=6)).isoformat()
    ok, reason = webrtc.monitor_status_health(stale_heartbeat, now=now)
    assert ok is False
    assert "heartbeat" in str(reason).lower()

    stale_media = dict(healthy)
    stale_media.update(
        connected_peer_count=1,
        peer_count=1,
        peer_connected_at=(now - timedelta(seconds=10)).isoformat(),
        last_media_frame_at=(now - timedelta(seconds=6)).isoformat(),
    )
    ok, reason = webrtc.monitor_status_health(stale_media, now=now)
    assert ok is False
    assert "media" in str(reason).lower()


def test_monitor_server_opens_and_releases_factory_track_lazily() -> None:
    class EmptyTrack(VideoStreamTrack):
        async def recv(self):
            raise NotImplementedError

        async def wait_stopped(self) -> None:
            return None

    created: list[EmptyTrack] = []
    camera_states: list[bool] = []
    server = webrtc.MonitorWebRTCServer(
        track_factory=lambda: created.append(EmptyTrack()) or created[-1],
        on_camera_open_changed=camera_states.append,
    )

    async def exercise() -> None:
        assert server.track is None
        await server._ensure_track()
        assert server.track is created[0]
        await server._release_track()

    asyncio.run(exercise())

    assert camera_states == [True, False]
    assert created[0].readyState == "ended"
