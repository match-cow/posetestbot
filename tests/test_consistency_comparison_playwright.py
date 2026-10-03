from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

from posetestbot.bop.consistency_comparison import create_comparison
from tests.test_consistency_comparison import comparison_sources

pytestmark = pytest.mark.playwright


@pytest.fixture
def comparison_page(tmp_path, monkeypatch):
    existing = os.environ.get("POSETESTBOT_COMPARISON_HTML")
    if existing:
        path = Path(existing)
    else:
        sources = comparison_sources(tmp_path, monkeypatch)
        path = create_comparison(
            sources, output_run=sources[0][1], title="Browser comparison fixture"
        )
    data = json.loads(path.with_name("comparison.json").read_text())
    return path, data


@pytest.mark.parametrize("viewport", [(1920, 1080), (1440, 900)])
def test_offline_desktop_filters_thresholds_examples_and_downloads(
    comparison_page, viewport, tmp_path
):
    path, data = comparison_page
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}
        )
        page = context.new_page()
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                external.append(request.url)
                if request.url.startswith(("http:", "https:"))
                else None
            ),
        )
        page.goto(path.as_uri(), wait_until="load")
        expect(
            page.get_by_role("heading", name=data["title"], exact=True)
        ).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#track-table tbody tr").count() == sum(
            len(c["tracks"]) for c in data["conditions"]
        )
        assert page.locator("#bop-table tbody tr").count() == len(data["conditions"])
        first = data["conditions"][0]
        page.locator("#condition").select_option("0")
        page.locator("#camera").select_option("all")
        page.locator("#metric").select_option("add")
        expected = f"{first['statistics']['add']['gt_mean_all_mm']:.2f} mm"
        expect(page.locator("#selection-stats .value").first).to_have_text(expected)
        page.locator("#metric").select_option("mvd")
        threshold = first["statistics"]["mvd"]["threshold_counts"]
        expect(page.locator("#confusion .cell b").nth(1)).to_have_text(
            f"{threshold['low_rc_high_gt']:,}"
        )
        page.locator("#threshold").fill("2")
        rows = first["rows"]
        low = sum(
            r["rc_mvd_mm"] is not None and r["rc_mvd_mm"] < 2 and r["gt_mvd_mm"] >= 2
            for r in rows
        )
        expect(page.locator("#confusion .cell b").nth(1)).to_have_text(f"{low:,}")
        page.locator("#range").select_option("10")
        expect(page.locator("#scatter-note")).to_contain_text(
            "outside the selected range"
        )
        page.locator("#range").select_option("full")
        page.locator("#scatter").focus()
        page.locator("#scatter").press("Enter")
        expect(page.locator("#selection")).to_contain_text("BOP image")
        page.locator("#condition").select_option("all")
        page.locator("#example").select_option("shared")
        page.locator("#camera").select_option(str(first["tracks"][0]["scene_id"]))
        expect(page.locator("#gallery img")).to_have_count(len(data["conditions"]))
        raw_source = page.locator("#gallery img").first.get_attribute("src")
        page.locator("#overlay").select_option("overlay")
        assert page.locator("#gallery img").first.get_attribute("src") != raw_source
        assert (
            page.locator("#gallery img")
            .first.get_attribute("alt")
            .endswith("with GT and estimate overlay")
        )
        page.locator("#provenance summary").click()
        expect(page.locator("#source-table")).to_be_visible()
        with page.expect_download() as download:
            page.locator("#csv-download").click()
        destination = tmp_path / "downloaded.csv"
        download.value.save_as(destination)
        assert len(destination.read_text().splitlines()) == 1 + sum(
            len(c["rows"]) for c in data["conditions"]
        )
        assert not errors and not external
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.environ.get("POSETESTBOT_COMPARISON_HTML"):
            page.locator("#provenance summary").click()
            page.locator("#threshold").fill("10")
            page.locator("#range").select_option("full")
            page.locator("#overlay").select_option("rgb")
            page.evaluate("scrollTo(0,0)")
            page.screenshot(
                path=f"/tmp/ma-leonie-comparison-{viewport[0]}.png", full_page=False
            )
        context.close()
        browser.close()


def test_long_comparison_renders_all_frames_without_argument_limit_errors(
    comparison_page,
):
    path, _ = comparison_page
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(path.as_uri(), wait_until="load")
        # Long multi-camera recordings can exceed the JavaScript engine's
        # function-argument limit even though their rows fit comfortably in RAM.
        page.evaluate("""() => {
            const seed = all[0];
            all.length = 0;
            for (let i = 0; i < 200000; i++) {
                all.push({...seed, im_id: i,
                    gt_mvd_mm: 1 + i % 100, rc_mvd_mm: 1 + i % 75});
            }
            $('condition').value = 'all';
            $('range').value = 'full';
            update();
        }""")
        expect(page.locator("#scatter-note")).to_contain_text(
            "200,000 / 200,000 matched points in view"
        )
        page.locator("#scatter").focus()
        page.locator("#scatter").press("Enter")
        expect(page.locator("#selection")).to_contain_text("BOP image")
        assert not errors
        browser.close()
