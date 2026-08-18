from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = ROOT / "site"
INDEX_PATH = SITE_ROOT / "index.html"
PAGES_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
PUBLIC_URL = "https://match-cow.github.io/posetestbot/"


class SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.sources: list[str] = []
        self.images: list[dict[str, str | None]] = []
        self.html_attributes: dict[str, str | None] = {}
        self.h1_count = 0
        self.nav_labels: list[str | None] = []

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        attrs = dict(attributes)
        if element_id := attrs.get("id"):
            self.ids.add(element_id)
        if tag == "html":
            self.html_attributes = attrs
        elif tag == "a" and (href := attrs.get("href")):
            self.links.append(href)
        elif tag in {"img", "script"} and (source := attrs.get("src")):
            self.sources.append(source)
        elif tag == "link" and (source := attrs.get("href")):
            if attrs.get("rel") in {"icon", "stylesheet"}:
                self.sources.append(source)
        if tag == "img":
            self.images.append(attrs)
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "nav":
            self.nav_labels.append(attrs.get("aria-label"))


def _parse_site() -> tuple[str, SiteParser]:
    html = INDEX_PATH.read_text(encoding="utf-8")
    parser = SiteParser()
    parser.feed(html)
    return html, parser


def _local_path(reference: str) -> Path | None:
    parsed = urlparse(reference)
    if parsed.scheme or parsed.netloc or reference.startswith("#"):
        return None
    return SITE_ROOT / unquote(parsed.path)


def test_site_entrypoint_has_accessible_document_structure() -> None:
    html, parser = _parse_site()

    assert parser.html_attributes.get("lang") == "en"
    assert parser.h1_count == 1
    assert "main-content" in parser.ids
    assert '<a class="skip-link" href="#main-content">' in html
    assert '<main id="main-content" tabindex="-1">' in html
    assert {"Primary navigation", "Footer navigation"} <= set(parser.nav_labels)
    assert all("alt" in image for image in parser.images)
    assert 'id="copy-status" role="status" aria-live="polite"' in html
    assert 'id="theme-toggle"' in html
    assert 'aria-pressed="false"' in html
    assert 'target="_blank"' not in html


def test_all_local_assets_and_fragment_links_resolve() -> None:
    _, parser = _parse_site()

    missing_assets = [
        reference
        for reference in parser.sources
        if (path := _local_path(reference)) is not None and not path.is_file()
    ]
    missing_fragments = [
        reference
        for reference in parser.links
        if reference.startswith("#") and reference[1:] not in parser.ids
    ]

    assert not missing_assets
    assert not missing_fragments
    assert all(not reference.startswith("/") for reference in parser.sources)


def test_plain_language_site_preserves_project_and_safety_boundaries() -> None:
    html, _ = _parse_site()
    normalized_html = " ".join(html.split())

    required_phrases = (
        "Journey 1",
        "Calibrate cameras",
        "Journey 2",
        "Record an object dataset",
        "Raw data stays untouched",
        "both fresh execution safety gates",
        "A green check is not permission to move",
        "KUKA LBR iiwa",
        "PoseTestBot builds the dataset. Estimators consume it elsewhere",
        "processed/synchronized/",
        "test_targets_bop19.json",
        "POSETESTBOT_WEB_HOST=127.0.0.1 uv run posetestbot-web",
    )
    for phrase in required_phrases:
        assert phrase in normalized_html


def test_public_document_links_target_authoritative_main_branch_guides() -> None:
    _, parser = _parse_site()
    github_docs = {
        link
        for link in parser.links
        if link.startswith("https://github.com/match-cow/PoseTestBot/blob/")
    }

    assert any(link.endswith("/docs/OPERATOR_WORKFLOWS.md") for link in github_docs)
    assert any(link.endswith("/INSTALL.md") for link in github_docs)
    assert any(link.endswith("/docs/REWRITE_REMAINING_WORK.md") for link in github_docs)
    assert all("/blob/main/" in link for link in github_docs)

    for link in github_docs:
        repository_path = (
            link.split("/blob/main/", maxsplit=1)[1]
            .split("#", maxsplit=1)[0]
        )
        assert (ROOT / repository_path).is_file(), link


def test_pages_workflow_deploys_only_the_static_site() -> None:
    workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")

    assert "actions/checkout@v6" in workflow
    assert "actions/configure-pages@v5" in workflow
    assert "actions/upload-pages-artifact@v4" in workflow
    assert "actions/deploy-pages@v4" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    assert "name: github-pages" in workflow
    assert "path: site" in workflow
    assert "path: '.'" not in workflow
    assert 'path: "."' not in workflow


def test_public_url_is_consistent_across_repository_entrypoints() -> None:
    index = INDEX_PATH.read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    robots = (SITE_ROOT / "robots.txt").read_text(encoding="utf-8")
    sitemap = ElementTree.parse(SITE_ROOT / "sitemap.xml")
    namespace = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [
        element.text
        for element in sitemap.findall("sitemap:url/sitemap:loc", namespace)
    ]

    assert f'<link rel="canonical" href="{PUBLIC_URL}">' in index
    assert PUBLIC_URL in readme
    assert f"Sitemap: {PUBLIC_URL}sitemap.xml" in robots
    assert locations == [PUBLIC_URL]
    assert (SITE_ROOT / ".nojekyll").is_file()


def test_research_links_use_resolvable_persistent_identifiers() -> None:
    html = INDEX_PATH.read_text(encoding="utf-8")

    assert "https://doi.org/10.1016/j.procir.2025.02.251" in html
    assert "https://doi.org/10.1109/HRI61500.2025.10974140" in html
    assert "https://doi.org/10.5281/zenodo.14132641" in html
    assert "https://doi.org/10.5281/zenodo.14261013" in html
