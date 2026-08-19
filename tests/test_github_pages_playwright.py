from __future__ import annotations

import re
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright


pytestmark = pytest.mark.playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


class DocumentationServer:
    def __init__(self, site_root: Path) -> None:
        handler = partial(QuietStaticHandler, directory=str(site_root))
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
def docs_server(tmp_path_factory: pytest.TempPathFactory):
    site_root = tmp_path_factory.mktemp("mkdocs-browser-site")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(site_root),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    server = DocumentationServer(site_root)
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
            color_scheme="light",
        )
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def test_desktop_sidebar_uses_persistent_pages_and_browser_history(
    page, docs_server
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(docs_server.url, wait_until="networkidle")

    expect(page).to_have_title(re.compile("PoseTestBot Technical Documentation"))
    expect(
        page.get_by_role("heading", name="PoseTestBot technical documentation")
    ).to_be_visible()
    sidebar = page.locator(".md-sidebar--primary")
    expect(sidebar).to_be_visible()
    expect(sidebar.get_by_role("link", name="System overview", exact=True)).to_be_visible()
    expect(sidebar.get_by_role("link", name="API conventions", exact=True)).to_be_visible()

    sidebar.get_by_role("link", name="API conventions", exact=True).click()
    expect(page).to_have_url(re.compile(r"/reference/http-api/$"))
    expect(page.get_by_role("heading", name="HTTP API conventions")).to_be_visible()

    page.get_by_role("link", name="Complete route index", exact=True).first.click()
    expect(page).to_have_url(re.compile(r"/reference/http-api-routes/$"))
    expect(page.get_by_role("heading", name="Complete HTTP route index")).to_be_visible()
    expect(page.get_by_text("116", exact=False).first).to_be_visible()

    page.go_back(wait_until="networkidle")
    expect(page.get_by_role("heading", name="HTTP API conventions")).to_be_visible()
    page.go_back(wait_until="networkidle")
    expect(
        page.get_by_role("heading", name="PoseTestBot technical documentation")
    ).to_be_visible()

    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []


@pytest.mark.parametrize(
    ("level", "name"),
    [
        ("h1", "PoseTestBot technical documentation"),
        ("h2", "Repository boundary"),
    ],
)
def test_heading_typography_and_permalink_share_a_stable_line_box(
    page, docs_server, level: str, name: str
) -> None:
    page.goto(docs_server.url, wait_until="networkidle")

    heading = page.locator(f".md-typeset {level}", has_text=name).first
    expect(heading).to_be_visible()
    permalink = heading.locator(".headerlink")
    expect(permalink).to_have_text("#")

    metrics = heading.evaluate(
        """element => {
            const text = document.createRange();
            text.selectNodeContents(element.firstChild);
            const textRect = text.getBoundingClientRect();
            const linkRect = element.querySelector('.headerlink').getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
                fontFamily: style.fontFamily,
                fontWeight: style.fontWeight,
                textCenter: textRect.top + textRect.height / 2,
                linkCenter: linkRect.top + linkRect.height / 2,
            };
        }"""
    )
    assert "system-ui" in metrics["fontFamily"]
    assert metrics["fontWeight"] == "500"
    assert abs(metrics["textCenter"] - metrics["linkCenter"]) <= 2


def test_search_indexes_technical_contracts_and_opens_result(page, docs_server) -> None:
    page.goto(docs_server.url, wait_until="networkidle")
    search = page.get_by_role("textbox", name="Search")
    expect(search).to_be_visible()
    search.fill("State meanings")

    results = page.locator("[data-md-component='search-result'] a")
    expect(results.first).to_be_visible(timeout=10_000)
    assert results.count() > 0
    results.filter(has_text="Safety and authorization").first.click()
    expect(page).to_have_url(re.compile(r"/concepts/safety/"))
    expect(page.get_by_role("heading", name="Safety and authorization")).to_be_visible()


def test_keyboard_skip_link_and_narrow_navigation_drawer_work(page, docs_server) -> None:
    page.goto(docs_server.url, wait_until="networkidle")
    page.keyboard.press("Home")
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()

    page.set_viewport_size({"width": 900, "height": 900})
    page.goto(docs_server.url, wait_until="networkidle")
    navigation_toggle = page.locator("label.md-header__button[for='__drawer']")
    expect(navigation_toggle).to_be_visible()
    navigation_toggle.click()

    drawer = page.locator(".md-sidebar--primary")
    expect(drawer).to_be_visible()
    drawer.locator("label.md-nav__link", has_text="File and command reference").click()
    drawer.get_by_role("link", name="Run configuration", exact=True).click()
    expect(page).to_have_url(re.compile(r"/reference/run-config/$"))
    expect(
        page.get_by_role("heading", name=re.compile(r"Run configuration"))
    ).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
