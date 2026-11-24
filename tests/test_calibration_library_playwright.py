"""Calibration lifetime in the console: nothing consequential is preselected."""

# ruff: noqa: F811 - the imported pytest fixtures are requested by parameter name

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from tests.test_web_console_playwright import (  # noqa: F401 - pytest fixtures
    RUN_ROOT,
    console_server,
    fulfill_json,
    install_common_mocks,
    overview_payload,
    page,
    run_config,
    run_folder_inventory,
)

pytestmark = pytest.mark.playwright

DIGEST = "ab" * 32
PROFILE = {
    "profile_id": "wrist_eye_in_hand",
    "calibrated_at": "2020-01-01T00:00:00+00:00",
    "sensor_type": "realsense_d435",
    "sensor_id": "wrist-1",
    "sensor_name": "Wrist RGB-D",
    "mounting_mode": "eye_in_hand",
    "status": "valid",
    "resolution": [1280, 720],
    "method": "auto_compare:IPPE+tsai",
    "quality": {
        "num_observations": 1072,
        "num_inliers": 1069,
        "mean_reprojection_error_px": 1.0457,
        "residual_translation_mm": 2.97,
        "residual_rotation_deg": 0.48,
    },
}


def _calibration(**changes) -> dict:
    return {
        "bundle_sha256": DIGEST,
        "name": "calibration_20260921",
        "valid": True,
        "issues": [],
        "calibrated_at": "2020-01-01T00:00:00+00:00",
        "valid_profile_count": 1,
        "profiles": [PROFILE],
        "availability": "snapshot",
        "selectable": False,
        "source_run": None,
        "retained": None,
        "snapshot_runs": [{"run_root": RUN_ROOT, "run_name": "Leonie dataset"}],
        "used_by": [
            {
                "run_root": RUN_ROOT,
                "run_name": "Leonie dataset",
                "selected_at": "2026-09-21T09:51:19+00:00",
                "sensor_keys": ["realsense_d435:wrist-1"],
            }
        ],
        **changes,
    }


def _overview(*calibrations: dict) -> dict:
    return {
        "schema_version": "calibration_library_overview.v1",
        "age_warning_days": 90,
        "library_root": "/tmp/posetestbot-console/calibration_library",
        "calibrations": list(calibrations),
        "unusable": [],
    }


@pytest.mark.parametrize("viewport", [(1920, 1080), (1440, 900)])
def test_calibrations_page_recovers_and_deletes_a_calibration(
    console_server, page, viewport
) -> None:
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    state = {"calibration": _calibration()}
    requests: list[tuple[str, dict]] = []
    install_common_mocks(page)
    page.route(
        "**/ui/calibrations/library",
        lambda route: fulfill_json(route, _overview(state["calibration"])),
    )

    def retain(route) -> None:
        requests.append(("retain", route.request.post_data_json))
        state["calibration"] = _calibration(
            availability="library",
            selectable=True,
            retained={
                "path": f"/tmp/posetestbot-console/calibration_library/{DIGEST}",
                "retained_at": "2026-11-23T12:00:00+00:00",
                "reason": "operator_request",
                "origin": {
                    "kind": "run_snapshot",
                    "run_root": RUN_ROOT,
                    "run_name": "Leonie dataset",
                },
            },
        )
        fulfill_json(route, {"name": "calibration_20260921"}, status=201)

    def remove(route) -> None:
        requests.append(("delete", route.request.post_data_json))
        state["calibration"] = _calibration()
        fulfill_json(route, {"bundle_sha256": DIGEST, "status": "deleted"})

    page.route("**/ui/calibrations/library/retain", retain)
    page.route(f"**/ui/calibrations/library/{DIGEST}", remove)
    page.goto(f"{console_server.url}/#/calibrations", wait_until="networkidle")

    row = page.get_by_test_id("calibration-row")
    expect(row).to_have_attribute("data-availability", "snapshot")
    expect(row).to_contain_text("calibration_20260921")
    expect(row).to_contain_text("Only inside dataset runs")
    expect(row).to_contain_text("1.05 px mean reprojection error")
    expect(row).to_contain_text("Leonie dataset")
    expect(row.get_by_test_id("calibration-age")).to_contain_text("days old")
    expect(row).to_contain_text("90+ days since capture. Confirm the mount before reuse.")
    summary = page.get_by_test_id("calibration-summary")
    expect(summary).to_contain_text("Selectable for new runs0")
    # Wide evidence scrolls inside its own table, never the whole document.
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )

    row.get_by_role("button", name="Keep calibration_20260921 in the library").click()

    expect(row).to_have_attribute("data-availability", "library")
    expect(row).to_contain_text("Kept from Leonie dataset")
    expect(summary).to_contain_text("Selectable for new runs1")
    assert requests == [("retain", {"run_root": RUN_ROOT, "bundle_sha256": DIGEST})]

    row.get_by_role(
        "button", name="Delete calibration_20260921 from the library"
    ).click()
    dialog = page.get_by_test_id("calibration-delete-dialog")
    expect(dialog).to_contain_text("1 existing dataset run on this machine")
    expect(dialog).to_contain_text("keeps its own snapshot")
    dialog.get_by_role("button", name="Delete calibration").click()

    expect(dialog).to_be_hidden()
    expect(row).to_have_attribute("data-availability", "snapshot")
    assert requests[-1] == ("delete", {"confirm": True})


def test_deleting_a_calibration_run_requires_choosing_what_happens_to_it(
    console_server, page
) -> None:
    install_common_mocks(page)
    inventory = run_folder_inventory()
    old_run = next(run for run in inventory["runs"] if run["name"] == "old-run")
    old_run["contents"]["evidence"]["calibration"] = True
    deletions: list[dict] = []
    job = {
        "id": "delete-1",
        "name": "run_folder_delete",
        "status": "succeeded",
        "command": [],
        "cwd": "/tmp",
        "created_at": "2026-11-23T12:00:00+00:00",
        "started_at": "2026-11-23T12:00:00+00:00",
        "ended_at": "2026-11-23T12:00:01+00:00",
        "returncode": 0,
        "message": "Command completed successfully.",
        "resources": ["disk_io", "run_folder_storage"],
        "parameters": {"run_folder_operation": "delete", "cancelable": False},
        "scope_kind": "run",
        "run_root": old_run["path"],
        "tail": [
            json.dumps(
                {
                    "status": "deleted",
                    "source_run_root": old_run["path"],
                    "retained_calibration": {"bundle_sha256": DIGEST},
                }
            )
        ],
    }

    def run_folders(route) -> None:
        if route.request.method == "DELETE":
            deletions.append(route.request.post_data_json)
            fulfill_json(
                route,
                {
                    "job_id": "delete-1",
                    "status": "queued",
                    "job": {**job, "status": "queued", "tail": []},
                    "source_run_root": old_run["path"],
                },
                status=202,
            )
        else:
            fulfill_json(route, inventory)

    page.unroute("**/ui/run-folders")
    page.route("**/ui/run-folders", run_folders)
    page.route("**/jobs/delete-1", lambda route: fulfill_json(route, {"job": job}))
    page.goto(f"{console_server.url}/#/run-folders", wait_until="networkidle")

    page.get_by_role("button", name="Delete old-run").click()
    dialog = page.get_by_test_id("run-folder-delete-dialog")
    choice = dialog.get_by_test_id("run-folder-delete-calibration-choice")
    confirm = dialog.get_by_role("button", name="Confirm delete")
    expect(choice).to_contain_text("Choice required")
    expect(choice).to_contain_text("A retention decision is required")
    expect(choice.get_by_role("radio")).to_have_count(2)
    for index in range(2):
        expect(choice.get_by_role("radio").nth(index)).not_to_be_checked()
    expect(confirm).to_be_disabled()

    choice.get_by_role("radio").first.check()
    expect(confirm).to_be_enabled()
    confirm.click()

    expect(page.get_by_text("Run folder deleted; calibration kept")).to_be_visible()
    assert deletions == [
        {
            "run_root": old_run["path"],
            "confirm": True,
            "expected_identity": old_run["identity"],
            "retain_calibration": True,
        }
    ]


def test_dataset_setup_never_preselects_the_annotation_outcome(
    console_server, page
) -> None:
    # A configured calibration run routes a dataset link back to the chooser.
    install_common_mocks(page, config_payload=run_config(intent="calibration"))
    page.route(
        "**/ui/calibrations?**",
        lambda route: fulfill_json(route, {"selected": None, "calibrations": []}),
    )
    page.goto(
        f"{console_server.url}/#/workflow/dataset?step=configure",
        wait_until="networkidle",
    )
    expect(page).to_have_url(f"{console_server.url}/#/workflow/setup")
    expect(page.get_by_role("link", name="Start with a new run folder")).to_have_attribute(
        "href", "#/run-folders"
    )
    expect(page.get_by_role("link", name="Active run new-run · Continue")).to_be_visible()

    page.route("**/ui/overview**", lambda route: fulfill_json(route, {**overview_payload(), "config": None}))
    page.route("**/run-config**", lambda route: fulfill_json(route, {"output": "Run config missing"}, status=404))
    page.goto(f"{console_server.url}/?unconfigured#/workflow/dataset?step=configure", wait_until="networkidle")

    setup = page.get_by_test_id("dataset-run-setup")
    outcome = setup.get_by_role("combobox", name="BOP annotation mode")
    expect(outcome).to_contain_text("Choose the dataset outcome")
    expect(outcome).to_have_attribute("aria-invalid", "true")
    expect(setup.get_by_test_id("annotation-mode-required")).to_contain_text(
        "Nothing is preselected"
    )
    expect(setup.get_by_role("button", name="Save setup")).to_be_disabled()

    outcome.click()
    expect(page.get_by_role("option")).to_have_count(3)
    page.get_by_role("option").filter(has_text="Pose ground truth + masks").click()

    expect(outcome).to_contain_text("Pose ground truth + masks")
    expect(outcome).to_have_attribute("aria-invalid", "false")
    expect(setup.get_by_test_id("annotation-mode-required")).to_have_count(0)


def test_dataset_setup_shows_the_outcome_a_dataset_run_already_saved(
    console_server, page
) -> None:
    install_common_mocks(
        page, config_payload=run_config(intent="dataset", annotation_mode="pose")
    )
    page.route(
        "**/ui/calibrations?**",
        lambda route: fulfill_json(route, {"selected": None, "calibrations": []}),
    )
    page.goto(
        f"{console_server.url}/#/workflow/dataset?step=configure",
        wait_until="networkidle",
    )

    setup = page.get_by_test_id("dataset-run-setup")
    expect(setup.get_by_role("combobox", name="BOP annotation mode")).to_contain_text(
        "Pose ground truth"
    )
    expect(setup.get_by_test_id("annotation-mode-required")).to_have_count(0)
    expect(setup.get_by_test_id("run-intent-mismatch")).to_have_count(0)
