"""Desktop depth workflow against real local APIs and retained worker outputs."""

from pathlib import Path
import os
import re
import threading
import zipfile

import pytest
from werkzeug.serving import make_server

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

from posetestbot.bop import depth_evaluation as depth
from posetestbot.bop import depth_videos as videos
from posetestbot.jobs.runner import LocalJobRunner
from posetestbot.web.app import create_app
from posetestbot.web.routes import depth_evaluation as routes, ui
from tests.test_bop_evaluation_api import FakeRunner
from tests.test_depth_evaluation import (
    add_side_sensor,
    analytic_reference,
    evaluate,
    make_depth_run,
)


pytestmark = pytest.mark.playwright


@pytest.mark.parametrize("viewport", [(1920, 1080), (1440, 900)])
def test_depth_labels_submission_retained_maps_comparison_and_jobs(
    tmp_path, monkeypatch, viewport
):
    a, b = (
        make_depth_run(tmp_path, name="matte-run"),
        make_depth_run(tmp_path, name="polished-run"),
    )
    add_side_sensor(a, distortion=(0.1,))
    evaluate(b, label="polished")
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", str(tmp_path))
    monkeypatch.setattr(ui, "default_web_run_root", lambda: a)
    monkeypatch.setattr(
        ui,
        "discover_web_runs",
        lambda: [
            {
                "path": str(p),
                "name": p.name,
                "run_name": p.name,
                "run_id": None,
                "intent": "dataset",
                "annotation_mode": "pose",
                "config_valid": True,
                "config_error": None,
                "modified_at": "2026-10-21T00:00:00Z",
            }
            for p in (a, b)
        ],
    )
    monkeypatch.setattr(
        depth,
        "renderer_status",
        lambda: {
            "available": True,
            "version": "2.8.0",
            "required_version": "2.8.0",
            "device": "CPU",
            "reason": None,
        },
    )
    runner = FakeRunner()
    monkeypatch.setattr(routes, "job_runner", runner)
    server = make_server(
        "127.0.0.1",
        0,
        create_app(job_runner=LocalJobRunner(tmp_path / "jobs")),
        threaded=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": viewport[0], "height": viewport[1]}
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/#/depth-evaluation")
            expect(
                page.get_by_role("heading", name="Depth Evaluation", exact=True)
            ).to_be_visible()
            submit = page.get_by_role(
                "button", name="Queue depth evaluation", exact=True
            )
            expect(submit).to_be_disabled()
            expect(page.get_by_test_id("depth-submit-reasons")).to_contain_text(
                "finish label is required"
            )
            expect(page.get_by_role("checkbox", name="side_sensor")).to_be_disabled()
            expect(page.get_by_role("checkbox", name="side_sensor")).not_to_be_checked()
            expect(
                page.get_by_text("Use a rectified BOP export", exact=False)
            ).to_be_visible()
            expect(page.get_by_role("checkbox", name="realsense_test")).to_be_checked()
            page.get_by_label("Finish label (required)").fill("matte")
            expect(submit).to_be_enabled()
            page.reload()
            expect(page.get_by_label("Finish label (required)")).to_have_value("matte")
            expect(
                page.get_by_text(
                    "CPU/disk processing continues after navigation.", exact=False
                )
            ).to_be_visible()
            submit.click()
            expect(page.get_by_role("link", name="View depth job")).to_be_visible()
            command = runner.submissions[0]["command"]
            request_path = Path(command[command.index("--request") + 1])
            assert depth.run_evaluation(
                request_path, render_reference=analytic_reference
            )["sensors"] == ["realsense_test"]
            expect(
                page.get_by_role("button", name="Open retained evaluation")
            ).to_be_enabled(timeout=10000)
            page.get_by_role("button", name="Open retained evaluation").click()
            expect(
                page.get_by_role("heading", name="matte", exact=True)
            ).to_be_visible()
            expect(page.get_by_test_id("depth-summary")).to_contain_text(
                "Mean absolute error"
            )
            expect(page.get_by_role("tab", name="New evaluation")).to_be_visible()
            page.screenshot(
                path=str(tmp_path / f"overview-{viewport[0]}.png"), full_page=True
            )
            # A newer UI can be served before an existing backend is restarted.
            # Missing video routes must explain recovery without raw HTML or
            # an empty form that misleadingly reports zero captured frames.
            video_route = "**/depth/evaluations/*/videos?*"
            page.route(
                video_route,
                lambda route: route.fulfill(
                    status=404,
                    content_type="text/html",
                    body="<!doctype html><html><title>404 Not Found</title><h1>Not Found</h1></html>",
                ),
            )
            page.reload()
            page.get_by_role("tab", name="Videos", exact=True).click()
            video_panel = page.get_by_test_id("depth-videos")
            expect(video_panel.get_by_role("alert")).to_contain_text(
                "restart the backend"
            )
            expect(video_panel).not_to_contain_text("<!doctype")
            expect(video_panel).not_to_contain_text("0 frames")
            expect(
                page.get_by_role("button", name="Generate MP4", exact=True)
            ).to_have_count(0)
            page.unroute(video_route)
            page.get_by_role("button", name="Retry video options", exact=True).click()
            expect(
                page.get_by_label("Camera / finish", exact=True).locator("option")
            ).to_have_count(1)
            expect(
                page.get_by_role("button", name="Retry video options", exact=True)
            ).to_have_count(0)
            page.get_by_role("tab", name="New evaluation").click()
            page.get_by_label("Finish label (required)").fill("draft-only-change")
            expect(
                page.get_by_text("Calibration and placement quality", exact=True)
            ).to_be_visible()
            assert (
                not page.locator("details")
                .filter(
                    has=page.get_by_text(
                        "Calibration and placement quality", exact=True
                    )
                )
                .get_attribute("open")
            )
            page.get_by_role("tab", name="Results & videos").click()
            expect(
                page.get_by_role("heading", name="matte", exact=True)
            ).to_be_visible()
            page.get_by_role("tab", name="Frames", exact=True).click()
            page.get_by_test_id("depth-frame-inspection").scroll_into_view_if_needed()
            expect(page.get_by_label("Camera / finish", exact=True)).to_be_visible()
            expect(page.get_by_role("img", name="RGB", exact=True)).to_be_visible()
            for value, name in (
                ("measured", "Measured depth"),
                ("reference", "Reference depth"),
                ("error", "Signed error"),
                ("missing", "Missing returns"),
            ):
                page.get_by_label("Depth image view").select_option(value)
                img = page.get_by_role("img", name=name, exact=True)
                expect(img).to_be_visible()
                expect(img).to_have_js_property("complete", True)
                assert img.evaluate("img => img.naturalWidth") == 32
            expect(
                page.get_by_text("Magenta: missing depth", exact=True)
            ).to_be_visible()
            page.get_by_label("Depth metric region").select_option("boundary")
            expect(page.get_by_test_id("depth-results")).to_contain_text(
                "Boundary only"
            )
            with page.expect_download() as csv_download:
                page.get_by_role("link", name="Download depth quality CSV").click()
            assert csv_download.value.suggested_filename.endswith("-depth-quality.csv")
            csv_path = tmp_path / "depth-quality.csv"
            csv_download.value.save_as(csv_path)
            csv_lines = csv_path.read_text().splitlines()
            assert csv_lines[0].startswith("level,input_id,finish_label")
            assert any(line.startswith("frame_instance,") for line in csv_lines)
            page.get_by_role("tab", name="Along motion", exact=True).click()
            motion = page.get_by_test_id("depth-motion-charts")
            # No matched robot poses in this fixture: plotted by frame instead.
            expect(motion.get_by_test_id("depth-motion-fallback")).to_be_visible()
            error_chart = motion.get_by_test_id("depth-motion-error-chart")
            expect(error_chart.locator("polyline")).to_have_count(1)
            expect(error_chart).to_contain_text("RMSE per frame")
            expect(
                motion.get_by_test_id("depth-motion-completeness-chart").locator("polyline")
            ).to_have_count(1)
            with page.expect_download() as download:
                page.get_by_role("link", name="Download research bundle").click()
            assert download.value.suggested_filename.endswith("-research.zip")
            bundle = tmp_path / "download.zip"
            download.value.save_as(bundle)
            standalone = tmp_path / "standalone"
            with zipfile.ZipFile(bundle) as archive:
                archive.extractall(standalone)
            offline = browser.new_context(offline=True)
            offline_page = offline.new_page()
            offline_page.goto((standalone / "report.html").as_uri())
            expect(
                offline_page.get_by_role("heading", name="Metrics", exact=True)
            ).to_be_visible()
            expect(offline_page.locator("#frame option")).to_have_count(1)
            expect(
                offline_page.get_by_role("img", name="error", exact=True)
            ).to_have_js_property("complete", True)
            assert (
                offline_page.get_by_role("img", name="error", exact=True).evaluate(
                    "img => img.naturalWidth"
                )
                == 32
            )
            offline.close()
            if videos.mp4_status()["available"]:
                page.get_by_role("tab", name="Videos", exact=True).click()
                expect(page.get_by_role("button", name="Generate MP4")).to_be_enabled()
                page.get_by_label("Frames / second").fill("1")
                page.get_by_label("Every nth frame").fill("0")
                expect(page.get_by_role("button", name="Generate MP4")).to_be_disabled()
                page.get_by_label("Every nth frame").fill("1")
                page.get_by_role("button", name="Generate MP4").click()
                expect(page.get_by_role("link", name="View video job")).to_be_visible()
                command = runner.submissions[-1]["command"]
                videos.run_video(command[command.index("--request") + 1])
                player = page.get_by_label("Depth quality video", exact=True)
                expect(player).to_be_visible(timeout=10000)
                page.wait_for_function(
                    "() => { const v = document.querySelector('video'); return v && v.readyState >= 2 && v.videoWidth > 0 }"
                )
                assert player.evaluate("v => v.error") is None
                with page.expect_download() as video_download:
                    page.get_by_role("link", name="Download MP4", exact=True).click()
                assert video_download.value.suggested_filename.endswith(".mp4")
                page.reload()
                page.get_by_role("tab", name="Videos", exact=True).click()
                expect(
                    page.get_by_label("Depth quality video", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="Download MP4", exact=True)
                ).to_be_visible()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(
                    path=str(tmp_path / f"video-{viewport[0]}.png"), full_page=True
                )
            page.get_by_role("tab", name="Compare finishes", exact=True).click()
            page.get_by_role("button", name="Add matte", exact=False).click(
                timeout=10000
            )
            page.get_by_label("Input run").select_option(str(b))
            page.get_by_role("button", name="Add polished", exact=False).click()
            page.get_by_role(
                "button", name="Queue depth comparison", exact=True
            ).click()
            expect(page.get_by_role("link", name="View comparison job")).to_be_visible()
            command = runner.submissions[-1]["command"]
            depth.run_comparison(command[command.index("--request") + 1])
            expect(
                page.get_by_role("button", name="Open retained comparison")
            ).to_be_enabled(timeout=10000)
            page.get_by_role("button", name="Open retained comparison").click()
            coverage = page.get_by_test_id("depth-common-coverage")
            expect(coverage).to_contain_text("matte")
            expect(coverage).to_contain_text("polished")
            expect(coverage).to_contain_text("Unmatched reference px")
            expect(page.get_by_test_id("depth-results")).to_contain_text(
                "2 separate run/sensor series"
            )
            summary = page.get_by_test_id("depth-comparison-summary-table")
            expect(summary.locator("tbody tr")).to_have_count(2)
            expect(summary).to_contain_text("matte")
            expect(summary).to_contain_text("polished")
            expect(summary.locator("thead")).to_contain_text("RMSE mm")
            expect(summary.locator("thead")).to_contain_text("Completeness")
            expect(
                page.get_by_test_id("depth-comparison-rmse-chart").locator("li")
            ).to_have_count(2)
            page.get_by_role("tab", name="Along motion", exact=True).click()
            expect(
                page.get_by_test_id("depth-motion-error-chart").locator("polyline")
            ).to_have_count(2)
            page.get_by_role("tab", name="Overview", exact=True).click()
            page.get_by_role("tab", name="Frames", exact=True).click()
            page.get_by_test_id("depth-frame-inspection").scroll_into_view_if_needed()
            expect(
                page.get_by_role("img", name="Signed error", exact=True)
            ).to_be_visible()
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            page.screenshot(
                path=str(tmp_path / f"depth-{viewport[0]}.png"), full_page=True
            )
            page.get_by_label("Study type").select_option("evaluations")
            page.get_by_role(
                "link", name="Job progress & logs", exact=True
            ).first.click()
            expect(page).to_have_url(re.compile(r"/#/jobs\?search="))
            expect(
                page.get_by_role("textbox", name="Search jobs", exact=True)
            ).to_have_value("bopeval1")
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.skipif(
    not os.environ.get("POSETESTBOT_DEPTH_REVIEW_RUN"),
    reason="Opt-in review of an existing completed study",
)
def test_completed_depth_study_desktop_review(tmp_path, monkeypatch):
    """Read-only acceptance against retained production data; no evaluation or capture."""
    root = Path(os.environ["POSETESTBOT_DEPTH_REVIEW_RUN"]).resolve()
    record = next(
        r
        for r in depth.history(root, "evaluations")["records"]
        if r["report_available"]
    )
    report_path = depth.artifact(root, "evaluations", record["id"], "report.json")
    report_hash = depth._sha256_file(report_path)
    completed_videos = [
        v
        for v in videos.history(root, "evaluations", record["id"])["records"]
        if v["video_available"]
    ]
    monkeypatch.setattr(ui, "default_web_run_root", lambda: root)
    monkeypatch.setattr(
        ui,
        "discover_web_runs",
        lambda: [
            {
                "path": str(root),
                "name": root.name,
                "run_name": root.name,
                "run_id": None,
                "intent": "dataset",
                "annotation_mode": "pose",
                "config_valid": True,
                "config_error": None,
                "modified_at": record["created_at"],
            }
        ],
    )
    server = make_server(
        "127.0.0.1",
        0,
        create_app(job_runner=LocalJobRunner(tmp_path / "jobs")),
        threaded=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    screenshots = Path("/tmp/posetestbot-depth-review")
    screenshots.mkdir(exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for width, height in ((1920, 1080), (1440, 900)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(f"http://127.0.0.1:{server.server_port}/#/depth-evaluation")
                expect(page.get_by_test_id("depth-summary")).to_be_visible(
                    timeout=30000
                )
                expect(page.get_by_test_id("depth-run-summary")).to_contain_text(
                    "Depth inputs verified", timeout=30000
                )
                expect(
                    page.get_by_role(
                        "heading", name=record["submission"]["finish_label"], exact=True
                    )
                ).to_be_visible()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(
                    path=str(screenshots / f"overview-{width}.png"), full_page=True
                )
                page.get_by_role("tab", name="Frames", exact=True).click()
                cameras = page.get_by_label("Camera / finish", exact=True)
                expect(cameras.locator("option")).to_have_count(
                    len(record["submission"]["sensors"])
                )
                cameras.select_option(
                    index=min(1, len(record["submission"]["sensors"]) - 1)
                )
                image = page.get_by_role("img", name="Signed error", exact=True)
                expect(image).to_be_visible()
                expect(image).to_have_js_property("complete", True)
                assert image.evaluate("img => img.naturalWidth") > 0
                page.screenshot(
                    path=str(screenshots / f"frames-{width}.png"), full_page=True
                )
                page.get_by_role("tab", name="Videos", exact=True).click()
                if completed_videos:
                    page.get_by_text("Generate another video", exact=True).click()
                expect(page.get_by_role("button", name="Generate MP4")).to_be_enabled()
                expect(
                    page.get_by_label("Camera / finish", exact=True).locator("option")
                ).to_have_count(len(record["submission"]["sensors"]))
                if completed_videos:
                    page.get_by_text("Generate another video", exact=True).click()
                    player = page.get_by_label("Depth quality video", exact=True)
                    expect(player).to_be_visible(timeout=10000)
                    page.wait_for_function(
                        "() => { const v = document.querySelector('video'); return v && v.readyState >= 2 && v.videoWidth > 0 }"
                    )
                    assert player.evaluate("v => v.error") is None
                    player.evaluate("async v => { await v.play() }")
                    page.wait_for_function(
                        "() => document.querySelector('video').currentTime > 0.1"
                    )
                    player.evaluate("v => v.pause()")
                    with page.expect_download() as video_download:
                        page.get_by_role(
                            "link", name="Download MP4", exact=True
                        ).click()
                    assert video_download.value.suggested_filename.endswith(".mp4")
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(
                    path=str(screenshots / f"videos-{width}.png"), full_page=True
                )
                page.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert depth._sha256_file(report_path) == report_hash
