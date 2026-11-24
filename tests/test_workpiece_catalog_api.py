from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from posetestbot.pose_templates import catalog as catalog_module
from posetestbot.pose_templates.catalog import (
    import_catalog_object,
    load_catalog,
)
from posetestbot.web.app import create_app
from posetestbot.web.routes import workpieces as routes


@dataclass
class FakeJob:
    status = "queued"

    id: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": "workpiece_catalog_import",
            "command": [],
            "cwd": None,
            "status": "queued",
            "created_at": "2026-07-22T00:00:00+00:00",
            "started_at": None,
            "ended_at": None,
            "returncode": None,
            "message": None,
            "tail": [],
            "resources": [],
            "parameters": {},
            "log_path": "log.txt",
            "visibility": "operator",
        }


class FakeRunner:
    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return FakeJob(f"workpiece-job-{len(self.submissions)}")

    def list(self, *, include_services: bool = True):
        return []


class FakeMeshBackend:
    constants = SimpleNamespace(MAX_UPLOAD_BYTES=50 * 1024 * 1024)

    @staticmethod
    def safe_filename(filename: str | None) -> str:
        value = Path(str(filename or "")).name
        if not value:
            raise ValueError("A filename is required")
        return value

    @staticmethod
    def file_format(filename: str) -> str:
        extension = Path(filename).suffix.lower().lstrip(".")
        if extension not in {"ply", "stl", "obj"}:
            raise ValueError("Unsupported CAD format")
        return extension

    def canonical_ply(self, filename: str, data: bytes) -> tuple[bytes, dict]:
        self.file_format(filename)
        return (
            b"ply\nformat ascii 1.0\ncomment canonical pytest mesh\nend_header\n",
            {
                "vertices": 8,
                "faces": 12,
                "bounds_mm": [[-5.0, -5.0, -5.0], [5.0, 5.0, 5.0]],
                "watertight": True,
            },
        )


@pytest.fixture
def workpiece_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    working = tmp_path / "working"
    monkeypatch.setenv("POSETESTBOT_WORKING_DATA_ROOT", working.as_posix())
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: FakeMeshBackend(),
    )
    monkeypatch.setattr(
        routes,
        "load_posetemplatecreator_backend",
        lambda: FakeMeshBackend(),
    )
    monkeypatch.setattr(
        routes,
        "posetemplatecreator_status",
        lambda: {
            "schema_version": "posetemplatecreator_source_status.v1",
            "status": "available",
            "available": True,
            "reason": None,
            "capabilities": {
                "formats": ["ply", "stl", "obj"],
                "limits": {
                    "cad_bytes": 50 * 1024 * 1024,
                    "batch_bytes": 100 * 1024 * 1024,
                },
            },
        },
    )
    monkeypatch.setattr(
        routes,
        "REQUEST_ROOT",
        working / "jobs" / "workpiece_catalog_requests",
    )
    runner = FakeRunner()
    monkeypatch.setattr(routes, "job_runner", runner)

    source = tmp_path / "fixture.stl"
    source.write_bytes(b"solid fixture\nendsolid fixture\n")
    record = import_catalog_object(
        name="Clamp",
        alias="Small clamp",
        description="Textured fixture",
        tags=["metal", "reflective"],
        groups=["clamps"],
        attributes={"owner": "vision"},
        cad_path=source,
    )
    return create_app().test_client(), runner, working, record


def test_workpiece_status_list_detail_and_asset_headers(workpiece_client) -> None:
    client, _runner, working, record = workpiece_client

    status = client.get("/workpieces/status")
    listing = client.get("/workpieces/catalog")
    detail = client.get(f"/workpieces/catalog/{record['catalog_uuid']}")

    assert status.status_code == 200
    assert status.get_json()["schema_version"] == "workpiece_catalog_status.v1"
    assert status.get_json()["counts"] == {"active": 1, "archived": 0, "total": 1}
    assert status.get_json()["unit_corrections"] == {
        "supported": True,
        "requires_archived": True,
        "conversions": [
            {"id": "meter_to_millimeter", "factor": 1000.0},
            {"id": "millimeter_to_meter", "factor": 0.001},
        ],
    }
    assert status.get_json()["catalog_root"] == (working / "object_catalog").as_posix()
    assert listing.status_code == 200
    assert listing.get_json()["schema_version"] == "object_catalog.v1"
    listed = listing.get_json()["objects"][0]
    assert listed["catalog_uuid"] == record["catalog_uuid"]
    assert listed["usage"] == {"template_count": 0, "templates": []}
    assert "catalog_root" not in listing.get_json()
    assert detail.get_json()["alias"] == "Small clamp"
    assert detail.get_json()["tags"] == ["metal", "reflective"]
    assert "catalog_root" not in detail.get_json()

    canonical = client.get(
        f"/workpieces/catalog/{record['catalog_uuid']}/assets/canonical_ply"
    )
    source = client.get(f"/workpieces/catalog/{record['catalog_uuid']}/assets/source")
    assert canonical.status_code == 200
    assert canonical.mimetype == "application/vnd.ply"
    assert canonical.headers["Content-Disposition"].startswith("inline;")
    assert source.status_code == 200
    assert source.mimetype == "application/octet-stream"
    assert source.headers["Content-Disposition"].startswith("attachment;")
    assert "fixture.stl" in source.headers["Content-Disposition"]
    assert (
        client.get(
            f"/workpieces/catalog/{record['catalog_uuid']}/assets/unknown"
        ).status_code
        == 404
    )


def test_workpiece_status_disables_unit_correction_when_backend_is_unavailable(
    workpiece_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _runner, _working, _record = workpiece_client
    monkeypatch.setattr(
        routes,
        "posetemplatecreator_status",
        lambda: {
            "schema_version": "posetemplatecreator_source_status.v1",
            "status": "missing",
            "available": False,
            "reason": "Pinned PoseTemplateCreator checkout is missing",
        },
    )

    response = client.get("/workpieces/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is False
    assert payload["unit_corrections"]["supported"] is False
    assert payload["unit_corrections"]["conversions"] == [
        {"id": "meter_to_millimeter", "factor": 1000.0},
        {"id": "millimeter_to_meter", "factor": 0.001},
    ]


def test_workpiece_upload_queues_validated_metadata_and_catalog_resource(
    workpiece_client,
) -> None:
    client, runner, working, _record = workpiece_client

    response = client.post(
        "/workpieces/catalog/upload",
        data={
            "cad": (io.BytesIO(b"solid queued\nendsolid queued\n"), "queued.STL"),
            "name": "Queued clamp",
            "alias": "Queue A",
            "description": "Uploaded from the browser",
            "tags": json.dumps(["Metal", "metal", "new"]),
            "groups": json.dumps(["Bench 1"]),
            "attributes": json.dumps({"owner": "operator", "revision": 2}),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 202
    assert response.get_json()["job_id"] == "workpiece-job-1"
    submission = runner.submissions[0]
    assert submission["name"] == "workpiece_catalog_import"
    assert submission["resources"] == ["cpu", "disk_io", "workpiece_catalog"]
    assert submission["scope_kind"] == "library"
    assert submission.get("run_root") is None
    assert submission["command"][:4] == [
        "uv",
        "run",
        "python",
        "scripts/run_object_catalog_import.py",
    ]
    request_path = Path(submission["parameters"]["request_path"])
    queued = json.loads(request_path.read_text())
    assert queued["name"] == "Queued clamp"
    assert queued["alias"] == "Queue A"
    assert queued["tags"] == ["Metal", "new"]
    assert queued["groups"] == ["Bench 1"]
    assert queued["attributes"] == {"owner": "operator", "revision": "2"}
    assert queued["catalog_root"] == (working / "object_catalog").as_posix()
    assert queued["cleanup_request_folder"] is True
    assert Path(queued["cad_path"]).read_bytes().startswith(b"solid queued")

    malformed = client.post(
        "/workpieces/catalog/upload",
        data={
            "cad": (io.BytesIO(b"solid bad\nendsolid bad\n"), "bad.stl"),
            "tags": "not-json",
        },
        content_type="multipart/form-data",
    )
    assert malformed.status_code == 400
    assert "tags must contain valid JSON" in malformed.get_json()["output"]
    assert len(runner.submissions) == 1


def test_workpiece_patch_archive_restore_and_queued_confirmed_delete(
    workpiece_client,
) -> None:
    client, runner, working, record = workpiece_client
    endpoint = f"/workpieces/catalog/{record['catalog_uuid']}"

    updated = client.patch(
        endpoint,
        json={
            "name": "Clamp body",
            "alias": "Inspection clamp",
            "tags": ["QA", "qa", "metal"],
            "groups": ["inspection"],
            "attributes": {"station": 2},
        },
    )
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "Clamp body"
    assert updated.get_json()["tags"] == ["QA", "metal"]
    assert updated.get_json()["attributes"] == {"station": "2"}
    assert updated.get_json()["obj_id"] == record["obj_id"]

    immutable = client.patch(endpoint, json={"obj_id": 99})
    assert immutable.status_code == 400
    assert "immutable workpiece fields" in immutable.get_json()["output"]

    archived = client.post(f"{endpoint}/archive")
    assert archived.status_code == 200
    assert archived.get_json()["state"] == "archived"
    restored = client.post(f"{endpoint}/restore")
    assert restored.status_code == 200
    assert restored.get_json()["state"] == "active"
    unconfirmed = client.delete(endpoint, json={})
    assert unconfirmed.status_code == 400
    assert "confirm must be true" in unconfirmed.get_json()["output"]
    assert client.get(endpoint).get_json()["state"] == "active"
    deleted = client.delete(endpoint, json={"confirm": True})
    assert deleted.status_code == 202, deleted.get_json()
    assert deleted.get_json()["status"] == "queued"
    submission = runner.submissions[-1]
    assert submission["scope_kind"] == "library"
    assert {"cpu", "disk_io"} <= set(submission["resources"])
    catalog_module.delete_catalog_object(record["catalog_uuid"])
    catalog = load_catalog(working / "object_catalog")
    assert catalog["objects"] == []
    assert catalog["tombstones"][0]["catalog_uuid"] == record["catalog_uuid"]


def test_workpiece_json_export_and_metadata_import_round_trip(workpiece_client) -> None:
    client, _runner, _working, record = workpiece_client
    endpoint = f"/workpieces/catalog/{record['catalog_uuid']}"

    exported = client.get("/workpieces/catalog/export")
    assert exported.status_code == 200
    assert exported.mimetype == "application/json"
    assert exported.headers["Content-Disposition"].startswith("attachment;")
    assert "object_catalog.json" in exported.headers["Content-Disposition"]
    portable = exported.get_json()
    assert portable["schema_version"] == "object_catalog.v1"
    assert "catalog_root" not in portable

    assert client.patch(endpoint, json={"alias": "Changed locally"}).status_code == 200
    imported = client.post(
        "/workpieces/catalog/import",
        data={
            "catalog": (
                io.BytesIO(json.dumps(portable).encode("utf-8")),
                "object_catalog.json",
            )
        },
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200
    assert imported.get_json()["updated"] == [record["catalog_uuid"]]
    assert client.get(endpoint).get_json()["alias"] == "Small clamp"

    wrong_extension = client.post(
        "/workpieces/catalog/import",
        data={"catalog": (io.BytesIO(b"{}"), "catalog.txt")},
        content_type="multipart/form-data",
    )
    assert wrong_extension.status_code == 400
    assert "must be a JSON file" in wrong_extension.get_json()["output"]


def test_workpiece_delete_worker_reports_pose_template_references(
    workpiece_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _runner, working, record = workpiece_client
    endpoint = f"/workpieces/catalog/{record['catalog_uuid']}"
    blockers = [
        {
            "template_uuid": "22222222-2222-4222-8222-222222222222",
            "display_name": "Clamp pair",
            "state": "active",
            "reason": "catalog_reference",
        }
    ]
    monkeypatch.setattr(
        catalog_module,
        "_template_delete_blockers",
        lambda _catalog_uuid, *, library_root=None: blockers,
    )

    response = client.delete(endpoint, json={"confirm": True})

    assert response.status_code == 202
    from scripts import run_workpiece_delete
    monkeypatch.setattr("sys.argv", ["run_workpiece_delete.py", record["catalog_uuid"], "--confirm-delete"])
    with pytest.raises(SystemExit, match="pose-template bundles"):
        run_workpiece_delete.main()
    catalog = load_catalog(working / "object_catalog")
    assert catalog["objects"][0]["catalog_uuid"] == record["catalog_uuid"]
    assert catalog["objects"][0]["state"] == "active"


def test_workpiece_unit_correction_requires_intent_and_queues_catalog_job(
    workpiece_client,
) -> None:
    client, runner, working, record = workpiece_client
    endpoint = f"/workpieces/catalog/{record['catalog_uuid']}/unit-corrections"
    request_value = {
        "conversion": "meter_to_millimeter",
        "confirm": True,
        "operator": "pytest operator",
        "expected_geometry_revision": record["geometry_revision"],
        "expected_canonical_sha256": record["canonical_ply_sha256"],
    }

    active = client.post(endpoint, json=request_value)
    assert active.status_code == 400
    assert "must be archived" in active.get_json()["output"]
    assert (
        client.post(f"/workpieces/catalog/{record['catalog_uuid']}/archive").status_code
        == 200
    )
    unconfirmed = client.post(endpoint, json={**request_value, "confirm": False})
    assert unconfirmed.status_code == 400
    stale = client.post(
        endpoint,
        json={**request_value, "expected_canonical_sha256": "0" * 64},
    )
    assert stale.status_code == 409

    response = client.post(endpoint, json=request_value)

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["job_id"] == "workpiece-job-1"
    assert payload["correction"]["factor"] == 1000.0
    assert payload["correction"]["current_bounds_mm"] == [
        [-5.0, -5.0, -5.0],
        [5.0, 5.0, 5.0],
    ]
    assert payload["correction"]["resulting_bounds_mm"] == [
        [-5000.0, -5000.0, -5000.0],
        [5000.0, 5000.0, 5000.0],
    ]
    queued = runner.submissions[-1]
    assert queued["name"] == "workpiece_unit_correction"
    assert queued["resources"] == ["cpu", "disk_io", "workpiece_catalog"]
    assert queued["scope_kind"] == "library"
    assert queued.get("run_root") is None
    assert queued["command"][3] == "scripts/run_workpiece_unit_correction.py"
    request_path = Path(queued["parameters"]["request_path"])
    request_json = json.loads(request_path.read_text())
    assert request_json["catalog_uuid"] == record["catalog_uuid"]
    assert request_json["catalog_root"] == (working / "object_catalog").as_posix()
    assert request_json["cleanup_request_folder"] is True
