from __future__ import annotations

import json

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from posetestbot.monitoring.webrtc import (
    MONITOR_STATUS_NAME,
    MONITOR_STATUS_SCHEMA,
)

from posetestbot.web.app import create_app

from posetestbot.web.routes import monitoring


class FakeJob:
    def __init__(
        self,
        job_id: str,
        *,
        status: str = "running",
        monitor_root: Path | None = None,
        owned: bool = True,
    ) -> None:
        self.id = job_id
        self.name = "monitor-webrtc:ugreen"
        self.status = status
        self.started_at = datetime.now(UTC).isoformat()
        self.message = "worker failed" if status == "failed" else None
        self.parameters = {
            "monitor_webrtc": owned,
            "monitor_root": monitor_root.as_posix() if monitor_root else None,
        }
        self.resources = ["monitoring_camera:0c45:2283"]
        self.tail: list[str] = []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "parameters": self.parameters,
            "resources": self.resources,
            "tail": self.tail,
        }


class FakeRunner:
    def __init__(self, jobs: list[FakeJob] | None = None) -> None:
        self.jobs = {job.id: job for job in jobs or []}
        self.submitted: list[dict] = []

    def active_jobs(self):
        return [job for job in self.jobs.values() if job.status in {"queued", "running", "canceling"}]

    def list_page(self, **kwargs):
        return SimpleNamespace(jobs=list(self.jobs.values()))

    def get(self, job_id: str):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def submit(self, **kwargs):
        self.submitted.append(kwargs)
        job = FakeJob(
            "new-monitor",
            status="queued",
            monitor_root=Path(kwargs["parameters"]["monitor_root"]),
        )
        job.parameters.update(kwargs["parameters"])
        self.jobs[job.id] = job
        return job

    def cancel(self, job_id: str):
        job = self.get(job_id)
        job.status = "canceled"
        return job


def write_ready_status(root: Path, *, ready: bool = True, port: int = 39876) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MONITOR_STATUS_NAME).write_text(
        json.dumps(
            {
                "schema_version": MONITOR_STATUS_SCHEMA,
                "transport": "webrtc",
                "status": "ready",
                "signaling_ready": ready,
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "signaling_port": port if ready else None,
                "peer_count": 0,
                "frame_count": 0,
                "selected_node": {"path": "/dev/video18"},
                "error": None,
            }
        )
    )


@pytest.fixture
def client(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(monitoring, "job_runner", runner)
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client(), runner


def test_start_queues_dedicated_webrtc_worker(
    client, monkeypatch, tmp_path: Path
) -> None:
    flask_client, runner = client
    monkeypatch.setattr(monitoring, "monitor_stream_root", lambda: tmp_path / "monitor")

    response = flask_client.post("/monitoring/webcam")

    assert response.status_code == 202
    submission = runner.submitted[0]
    assert submission["resources"] == ["monitoring_camera:0c45:2283"]
    assert submission["scope_kind"] == "global"
    assert submission.get("run_root") is None
    assert submission["parameters"]["monitor_webcam"] is True
    assert submission["parameters"]["monitor_webrtc"] is True
    assert submission["parameters"]["transport"] == "webrtc"
    command = submission["command"]
    assert command[:4] == ["uv", "run", "python", "scripts/run_monitor_webrtc.py"]
    assert command[command.index("--width") + 1] == "640"
    assert command[command.index("--height") + 1] == "480"
    assert command[command.index("--fps") + 1] == "30"
    assert command[command.index("--vendor-id") + 1] == "0c45"
    assert command[command.index("--product-id") + 1] == "2283"


def test_get_status_does_not_expose_private_signaling_port(
    client,
    tmp_path: Path,
) -> None:
    flask_client, runner = client
    root = tmp_path / "monitor"
    write_ready_status(root, port=39876)
    runner.jobs["monitor-1"] = FakeJob("monitor-1", monitor_root=root)

    response = flask_client.get("/monitoring/webcam")

    assert response.status_code == 200
    status = response.get_json()["webrtc_status"]
    assert status["schema_version"] == MONITOR_STATUS_SCHEMA
    assert status["transport"] == "webrtc"
    assert status["signaling_ready"] is True
    assert "signaling_port" not in status


def test_offer_proxies_valid_sdp_to_ready_worker(
    client,
    monkeypatch,
    tmp_path: Path,
) -> None:
    flask_client, runner = client
    root = tmp_path / "monitor"
    write_ready_status(root, port=39876)
    runner.jobs["monitor-1"] = FakeJob("monitor-1", monitor_root=root)
    seen: list[tuple[int, dict]] = []

    def proxy(port: int, payload: dict):
        seen.append((port, payload))
        return {"type": "answer", "sdp": "v=0\r\nanswer"}

    monkeypatch.setattr(monitoring, "_proxy_webrtc_offer", proxy)

    response = flask_client.post(
        "/monitoring/webcam/monitor-1/webrtc/offer",
        json={"type": "offer", "sdp": "v=0\r\noffer"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"type": "answer", "sdp": "v=0\r\nanswer"}
    assert seen == [(39876, {"type": "offer", "sdp": "v=0\r\noffer"})]


def test_brightness_autocalibration_is_forwarded_to_camera_worker(
    client,
    monkeypatch,
    tmp_path: Path,
) -> None:
    flask_client, runner = client
    root = tmp_path / "monitor"
    write_ready_status(root, port=39876)
    runner.jobs["monitor-1"] = FakeJob("monitor-1", monitor_root=root)
    seen: list[int] = []
    brightness = {
        "schema_version": "monitor_brightness.v1",
        "supported": True,
        "state": "queued",
    }

    def proxy(port: int):
        seen.append(port)
        return {"brightness": brightness}

    monkeypatch.setattr(monitoring, "_proxy_brightness_autocalibration", proxy)

    response = flask_client.post(
        "/monitoring/webcam/monitor-1/brightness/autocalibrate",
        json={},
    )

    assert response.status_code == 202
    assert response.get_json() == {"brightness": brightness}
    assert seen == [39876]


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"type": "answer", "sdp": "v=0"}, {"type": "offer", "sdp": ""}],
)
def test_offer_rejects_malformed_payloads(client, tmp_path: Path, payload) -> None:
    flask_client, runner = client
    root = tmp_path / "monitor"
    write_ready_status(root)
    runner.jobs["monitor-1"] = FakeJob("monitor-1", monitor_root=root)

    response = flask_client.post(
        "/monitoring/webcam/monitor-1/webrtc/offer",
        data=json.dumps(payload) if payload is not None else "not json",
        content_type="application/json",
    )

    assert response.status_code == 400


def test_offer_returns_404_for_unknown_or_unowned_jobs(client, tmp_path: Path) -> None:
    flask_client, runner = client
    unknown = flask_client.post(
        "/monitoring/webcam/missing/webrtc/offer",
        json={"type": "offer", "sdp": "v=0"},
    )
    runner.jobs["other"] = FakeJob("other", monitor_root=tmp_path, owned=False)
    unowned = flask_client.post(
        "/monitoring/webcam/other/webrtc/offer",
        json={"type": "offer", "sdp": "v=0"},
    )

    assert unknown.status_code == 404
    assert unowned.status_code == 404


def test_offer_returns_503_when_signaling_is_not_ready(client, tmp_path: Path) -> None:
    flask_client, runner = client
    root = tmp_path / "monitor"
    write_ready_status(root, ready=False)
    runner.jobs["monitor-1"] = FakeJob("monitor-1", monitor_root=root)

    response = flask_client.post(
        "/monitoring/webcam/monitor-1/webrtc/offer",
        json={"type": "offer", "sdp": "v=0"},
    )

    assert response.status_code == 503
