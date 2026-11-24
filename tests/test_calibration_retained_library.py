"""A promoted calibration must outlive the run folder that produced it."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import posetestbot.web.routes.run_folders as run_folders_routes_module
from posetestbot.calibration.profile_library import (
    verify_calibration_profile_selection,
)
from posetestbot.calibration.retained_library import (
    RETAINED_RECORD,
    calibration_library_overview,
    default_calibration_library_root,
    list_retained_calibrations,
)
from posetestbot.io.artifacts import (
    CALIBRATION_PROFILE_SELECTION,
    CALIBRATION_PROFILES,
    INTRINSIC_CALIBRATION_PROFILES,
)
from posetestbot.run_folders import (
    delete_run_folder,
    run_identity,
    write_run_folder_inventory,
)
from posetestbot.web.app import create_app
from tests.test_calibration_profile_library_api import (
    SENSOR_ID,
    _write_destination,
    _write_source,
)
from tests.test_run_folders import _FakeRunner

SENSOR_KEY = f"realsense_d435:{SENSOR_ID}"


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One run root holding a promoted calibration run and an isolated library."""

    runs = tmp_path / "runs"
    source = runs / "calibration_run"
    _write_source(source)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs.as_posix())
    monkeypatch.setenv("POSETESTBOT_WORKING_DATA_ROOT", (tmp_path / "working").as_posix())
    return runs, source


def _delete(run: Path, *, retain_calibration: bool | None) -> dict:
    return delete_run_folder(
        run,
        expected_identity=run_identity(run),
        allowed_roots=[run.parent],
        retain_calibration=retain_calibration,
    )


def _select(client, destination: Path, candidate: dict):
    return client.post(
        "/ui/calibrations/select",
        json={
            "run_root": destination.as_posix(),
            "source_selections": [
                {
                    "source_run_root": candidate["source_run_root"],
                    "expected_bundle_sha256": candidate["bundle_sha256"],
                    "sensor_keys": [SENSOR_KEY],
                }
            ],
        },
    )


def _listed(client, destination: Path) -> list[dict]:
    # The default run roots stay configured, so match this test's own sources.
    response = client.get("/ui/calibrations", query_string={"run_root": destination})
    assert response.status_code == 200, response.get_json()
    return [
        item
        for item in response.get_json()["calibrations"]
        if item["source_run_name"] == "Reusable camera calibration"
    ]


def test_deleted_calibration_run_stays_selectable_from_the_library(lab) -> None:
    runs, source = lab
    promoted = (source / CALIBRATION_PROFILES).read_bytes()
    intrinsic = (source / INTRINSIC_CALIBRATION_PROFILES).read_bytes()

    result = _delete(source, retain_calibration=True)

    assert not source.exists()
    entry = Path(result["retained_calibration"]["path"])
    assert entry.parent == default_calibration_library_root()
    assert entry.name == result["retained_calibration"]["bundle_sha256"]
    assert (entry / CALIBRATION_PROFILES).read_bytes() == promoted
    assert (entry / INTRINSIC_CALIBRATION_PROFILES).read_bytes() == intrinsic

    destination = runs / "object_run"
    _write_destination(destination)
    client = create_app().test_client()
    (candidate,) = _listed(client, destination)
    assert candidate["origin"] == "library"
    assert candidate["source_run_root"] == entry.as_posix()
    assert candidate["compatible"] is True
    assert candidate["calibrated_at"] == "2026-07-20T10:00:00+00:00"

    selected = _select(client, destination, candidate)

    assert selected.status_code == 201, selected.get_json()
    snapshot = destination / selected.get_json()["calibration_profiles"]
    assert snapshot.read_bytes() == promoted
    provenance = json.loads((destination / CALIBRATION_PROFILE_SELECTION).read_text())
    (recorded,) = provenance["sources"]
    assert recorded["run_root"] == entry.as_posix()
    assert recorded["run_name"] == "Reusable camera calibration"
    assert recorded["retained"]["origin"] == {
        "kind": "calibration_run",
        "run_root": source.as_posix(),
        "run_name": "Reusable camera calibration",
        "run_id": recorded["retained"]["origin"]["run_id"],
    }
    verify_calibration_profile_selection(destination, verify_run_config=False)
    # Selecting the same retained source again is recognised as the same intent.
    assert _select(client, destination, candidate).get_json()["idempotent"] is True


def test_delete_needs_an_explicit_calibration_decision(lab) -> None:
    _runs, source = lab

    with pytest.raises(ValueError, match="Choose whether to keep"):
        _delete(source, retain_calibration=None)
    assert (source / CALIBRATION_PROFILES).is_file()

    result = _delete(source, retain_calibration=False)

    assert not source.exists()
    assert result["retained_calibration"] is None
    assert list_retained_calibrations() == []


def test_unretainable_calibration_stops_the_delete(lab) -> None:
    _runs, source = lab
    (source / INTRINSIC_CALIBRATION_PROFILES).write_text("{}")

    with pytest.raises(ValueError, match="nothing was deleted"):
        _delete(source, retain_calibration=True)

    assert (source / CALIBRATION_PROFILES).is_file()
    assert list_retained_calibrations() == []


def test_delete_api_requires_the_decision_only_for_calibration_runs(
    lab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, source = lab
    plain = runs / "object_run"
    _write_destination(plain)
    roots = (runs.resolve(),)
    monkeypatch.setattr(run_folders_routes_module, "web_run_roots", lambda: roots)
    runner = _FakeRunner(tmp_path / "jobs")
    write_run_folder_inventory(
        runner.job_root / "run_folder_inventory.json", allowed_roots=roots
    )
    client = create_app(job_runner=runner).test_client()

    def delete(run: Path, **extra):
        return client.delete(
            "/ui/run-folders",
            json={
                "run_root": run.as_posix(),
                "confirm": True,
                "expected_identity": run_identity(run),
                **extra,
            },
        )

    assert delete(source).status_code == 400
    assert delete(source, retain_calibration="true").status_code == 400
    assert delete(plain, retain_calibration=True).status_code == 400
    assert runner.submissions == []

    assert delete(source, retain_calibration=True).status_code == 202
    assert "--retain-calibration" in runner.submissions[-1]["command"]
    assert runner.submissions[-1]["parameters"]["retain_calibration"] is True
    runner.jobs.clear()
    assert delete(source, retain_calibration=False).status_code == 202
    assert "--discard-calibration" in runner.submissions[-1]["command"]
    runner.jobs.clear()
    assert delete(plain).status_code == 202
    assert not {"--retain-calibration", "--discard-calibration"} & set(
        runner.submissions[-1]["command"]
    )


def test_snapshot_only_calibration_is_listed_recovered_and_deleted(lab) -> None:
    runs, source = lab
    user = runs / "object_run"
    _write_destination(user)
    client = create_app().test_client()
    (candidate,) = _listed(client, user)
    assert _select(client, user, candidate).status_code == 201
    digest = candidate["bundle_sha256"]
    _delete(source, retain_calibration=False)

    def overview() -> dict:
        response = client.get("/ui/calibrations/library")
        assert response.status_code == 200
        return next(
            item
            for item in response.get_json()["calibrations"]
            if item["bundle_sha256"] == digest
        )

    stranded = overview()
    assert stranded["availability"] == "snapshot"
    assert stranded["selectable"] is False
    assert stranded["name"] == "Reusable camera calibration"
    assert [item["run_root"] for item in stranded["used_by"]] == [user.as_posix()]
    assert stranded["used_by"][0]["sensor_keys"] == [SENSOR_KEY]
    assert stranded["profiles"][0]["quality"]["num_observations"] == 20
    newcomer = runs / "second_object_run"
    _write_destination(newcomer)
    assert _listed(client, newcomer) == []

    stale = client.post(
        "/ui/calibrations/library/retain",
        json={"run_root": user.as_posix(), "bundle_sha256": "0" * 64},
    )
    assert stale.status_code == 409
    retained = client.post(
        "/ui/calibrations/library/retain",
        json={"run_root": user.as_posix(), "bundle_sha256": digest},
    )
    assert retained.status_code == 201, retained.get_json()
    assert retained.get_json()["already_retained"] is False
    again = client.post(
        "/ui/calibrations/library/retain",
        json={"run_root": user.as_posix(), "bundle_sha256": digest},
    )
    assert again.get_json()["already_retained"] is True

    recovered = overview()
    assert recovered["availability"] == "library"
    assert recovered["selectable"] is True
    assert recovered["retained"]["origin"]["kind"] == "run_snapshot"
    (candidate,) = _listed(client, newcomer)
    assert _select(client, newcomer, candidate).status_code == 201

    unconfirmed = client.delete(f"/ui/calibrations/library/{digest}", json={})
    assert unconfirmed.status_code == 400
    deleted = client.delete(f"/ui/calibrations/library/{digest}", json={"confirm": True})
    assert deleted.status_code == 200
    assert list_retained_calibrations() == []
    assert client.delete(
        f"/ui/calibrations/library/{digest}", json={"confirm": True}
    ).status_code == 404
    # Runs that already selected it are independent of the library entry.
    verify_calibration_profile_selection(user, verify_run_config=False)
    verify_calibration_profile_selection(newcomer, verify_run_config=False)
    assert overview()["availability"] == "snapshot"


def test_tampered_entry_is_reported_unusable_and_cannot_be_selected(lab) -> None:
    runs, source = lab
    entry = Path(_delete(source, retain_calibration=True)["retained_calibration"]["path"])
    destination = runs / "object_run"
    _write_destination(destination)
    client = create_app().test_client()
    (candidate,) = _listed(client, destination)
    os.chmod(entry, 0o755)
    os.chmod(entry / RETAINED_RECORD, 0o644)
    record = json.loads((entry / RETAINED_RECORD).read_text())
    record["name"] = "Renamed after publication"
    record["calibration_profiles"]["sha256"] = "0" * 64
    (entry / RETAINED_RECORD).write_text(json.dumps(record))

    assert _listed(client, destination) == []
    damaged = next(
        item
        for item in client.get(
            "/ui/calibrations", query_string={"run_root": destination}
        ).get_json()["calibrations"]
        if item["source_run_root"] == entry.as_posix()
    )
    assert damaged["valid"] is False
    assert damaged["issues"][0]["code"] == "invalid_calibration_bundle"
    assert _select(client, destination, candidate).status_code == 400
    assert not (destination / CALIBRATION_PROFILE_SELECTION).exists()
    (unusable,) = calibration_library_overview()["unusable"]
    assert unusable["path"] == entry.as_posix()
