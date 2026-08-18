from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright


pytestmark = pytest.mark.playwright

SITE_ROOT = Path(__file__).resolve().parents[1] / "site"


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


class DocumentationServer:
    def __init__(self) -> None:
        handler = partial(QuietStaticHandler, directory=str(SITE_ROOT))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


@pytest.fixture(scope="module")
def docs_server():
    server = DocumentationServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.fail(
                "Playwright Chromium is not installed; run "
                "`UV_CACHE_DIR=/tmp/uv-cache uv run playwright install chromium`. "
                f"Original error: {exc}"
            )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
            color_scheme="light",
        )
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def test_documentation_site_is_keyboard_reachable_and_complete(page, docs_server) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(docs_server.url, wait_until="networkidle")

    expect(page).to_have_title("PoseTestBot — traceable RGB-D datasets")
    expect(
        page.get_by_role(
            "heading",
            name="From supervised capture to a dataset you can explain.",
        )
    ).to_be_visible()
    expect(page.get_by_role("heading", name="Calibrate cameras")).to_be_visible()
    expect(
        page.get_by_role("heading", name="Record an object dataset")
    ).to_be_visible()
    expect(
        page.get_by_role(
            "heading",
            name="A green check is not permission to move.",
        )
    ).to_be_visible()
    expect(page.locator("img")).to_have_count(2)
    assert page.locator("img").evaluate_all(
        "images => images.every(image => image.complete && image.naturalWidth > 0)"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []

    page.keyboard.press("Home")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to main content")).to_be_focused()
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main-content"


def test_theme_and_copy_controls_work_without_hiding_content(page, docs_server) -> None:
    page.goto(docs_server.url, wait_until="networkidle")

    theme = page.get_by_role("button", name="Switch to dark theme")
    expect(theme).to_be_visible()
    theme.click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(
        page.get_by_role("button", name="Switch to light theme")
    ).to_have_attribute("aria-pressed", "true")

    page.reload(wait_until="networkidle")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    copy = page.locator(".copy-button")
    copy.click()
    expect(copy).to_have_text("Copied")
    expect(page.locator("#copy-status")).to_have_text(
        "Quick-start commands copied."
    )
    assert "POSETESTBOT_WEB_HOST=127.0.0.1" in page.evaluate(
        "navigator.clipboard.readText()"
    )


def test_public_layout_remains_reachable_at_narrow_width(page, docs_server) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(docs_server.url, wait_until="networkidle")

    expect(
        page.get_by_role(
            "heading",
            name="From supervised capture to a dataset you can explain.",
        )
    ).to_be_visible()
    expect(page.get_by_role("link", name="See the two workflows")).to_be_visible()
    expect(page.get_by_role("button", name="Switch to dark theme")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.get_by_role("link", name="See the two workflows").click()
    expect(
        page.get_by_role("heading", name="Start with the result you need.")
    ).to_be_in_viewport()
    page.get_by_text("What is BOP?").click()
    expect(
        page.get_by_text("BOP is a standard dataset and benchmark format")
    ).to_be_visible()
