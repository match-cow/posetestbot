"""Console contracts for template print scale and object-dataset subruns."""

# ruff: noqa: F811 - the imported pytest fixtures are requested by parameter name
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from tests.test_web_console_playwright import (  # noqa: F401 - pytest fixtures
    console_server,
    fulfill_json,
    install_common_mocks,
    page,
    run_config,
)


pytestmark = pytest.mark.playwright

PARENT = "/tmp/posetestbot-console/old-run"
CHILD = "/tmp/posetestbot-console/old-run__sub02"


def test_template_cards_show_the_print_scale(console_server, page) -> None:
    install_common_mocks(page)
    template = {
        "schema_version": "pose_template_bundle.v1",
        "template_uuid": "33333333-3333-4333-8333-333333333333",
        "display_name": "Three clamps",
        "description": None,
        "created_at": "2026-11-01T10:00:00Z",
        "bundle_sha256": "b" * 64,
        "archive": {"state": "active"},
        "print_compensation": {"x_scale": 1.0, "y_scale": 0.996},
        "instances": [],
    }
    page.route(
        "**/pose-templates/library",
        lambda route: fulfill_json(route, {"templates": [template]}),
    )
    page.goto(f"{console_server.url}/#/pose-templates", wait_until="networkidle")

    card = page.get_by_test_id(f"pose-template-library-card-{template['template_uuid']}")
    expect(card.get_by_test_id("template-print-scale")).to_have_text(
        "Print scale X 100.0 % · Y 99.6 %"
    )


def test_subrun_dialog_requires_a_variant_and_opens_the_new_run_at_readiness(
    console_server, page
) -> None:
    install_common_mocks(page, config_payload=run_config(intent="dataset"))
    submitted: list[dict] = []
    page.route(
        "**/runs/subrun?**",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "subrun_parent.v1",
                "parent_run_root": PARENT,
                "parent_run_id": "22222222-2222-4222-8222-222222222222",
                "parent_run_name": "Object A capture",
                "default_folder_name": "old-run__sub02",
                "capture": {
                    "resolution": "720p",
                    "fps": 15,
                    "velocity_m_s": 0.2,
                    "sensors": [
                        {
                            "sensor_type": "realsense_d435",
                            "device_id": "123",
                            "display_name": "Front D435",
                            "mounting_mode": "static",
                        }
                    ],
                },
                "calibration_bundle_sha256": "c" * 64,
                "calibration_sources": [
                    {
                        "run_root": "/tmp/posetestbot-console/new-run",
                        "run_name": "Calibration baseline",
                        "bundle_sha256": "c" * 64,
                        "sensor_keys": ["realsense_d435:123"],
                    }
                ],
                "template_uuid": "33333333-3333-4333-8333-333333333333",
                "template_name": None,
                "template_bundle_sha256": "d" * 64,
                "placement_matrix": [[1, 0, 0, 120], [0, 1, 0, -40], [0, 0, 1, 5], [0, 0, 0, 1]],
            },
        ),
    )

    def submit(route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        submitted.append(json.loads(route.request.post_data or "{}"))
        fulfill_json(
            route,
            {"job_id": "subrun-job", "job": {"id": "subrun-job"}, "run_root": CHILD},
            status=202,
        )

    page.route("**/runs/subrun", submit)
    page.route(
        "**/jobs/subrun-job",
        lambda route: fulfill_json(
            route, {"job": {"id": "subrun-job", "status": "succeeded", "message": None}}
        ),
    )
    page.goto(f"{console_server.url}/#/run-folders", wait_until="networkidle")

    page.get_by_role("button", name="New subrun from this run").click()
    dialog = page.get_by_test_id("subrun-dialog")
    expect(dialog.get_by_test_id("subrun-reused")).to_contain_text("15 fps · 0.20 m/s")
    expect(dialog.get_by_test_id("subrun-reused")).to_contain_text("120.0, -40.0, 5.0 mm")
    create = dialog.get_by_role("button", name="Create subrun")
    # The variant is consequential provenance: blank, required, and gating.
    expect(dialog.get_by_label("Variant description")).to_have_value("")
    expect(create).to_be_disabled()
    expect(dialog.get_by_label("New run folder")).to_have_value("old-run__sub02")
    dialog.get_by_label("Variant description").fill("anodised aluminium")
    create.click()

    expect(page).to_have_url(f"{console_server.url}/#/workflow/dataset?step=readiness")
    assert submitted == [
        {
            "parent_run_root": PARENT,
            "folder_name": "old-run__sub02",
            "variant": "anodised aluminium",
        }
    ]
    expect(page.get_by_test_id("active-run-path")).to_have_text(CHILD)
