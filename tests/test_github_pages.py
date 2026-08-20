from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

import pytest
import yaml

from posetestbot.web.app import app


ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = ROOT / "docs"
MKDOCS_CONFIG = ROOT / "mkdocs.yml"
TECHNICAL_STYLESHEET = DOCS_ROOT / "stylesheets" / "technical.css"
PAGES_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
ROUTE_REFERENCE = DOCS_ROOT / "reference" / "http-api-routes.md"
PUBLIC_URL = "https://match-cow.github.io/posetestbot/"


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.html_attributes: dict[str, str | None] = {}
        self.links: list[str] = []
        self.sources: list[str] = []
        self.h1_count = 0

    def handle_starttag(
        self,
        tag: str,
        attributes: list[tuple[str, str | None]],
    ) -> None:
        attrs = dict(attributes)
        if tag == "html":
            self.html_attributes = attrs
        if tag == "a" and attrs.get("href"):
            self.links.append(str(attrs["href"]))
        if tag in {"img", "script"} and attrs.get("src"):
            self.sources.append(str(attrs["src"]))
        if tag == "link" and attrs.get("href"):
            self.sources.append(str(attrs["href"]))
        if tag == "h1":
            self.h1_count += 1


@pytest.fixture(scope="module")
def built_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("mkdocs-site")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return destination


def _flatten_nav(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [path for item in value for path in _flatten_nav(item)]
    if isinstance(value, dict):
        return [path for item in value.values() for path in _flatten_nav(item)]
    return []


def _local_target(site_root: Path, page: Path, reference: str) -> Path | None:
    parsed = urlparse(reference)
    if parsed.scheme or parsed.netloc or reference.startswith(("#", "mailto:")):
        return None
    path = unquote(parsed.path)
    if not path:
        return None
    public_prefix = urlparse(PUBLIC_URL).path
    if path.startswith(public_prefix):
        target = site_root / path.removeprefix(public_prefix)
    elif path.startswith("/"):
        return None
    else:
        target = page.parent / path
    if path.endswith("/"):
        target /= "index.html"
    return target.resolve()


def test_mkdocs_navigation_covers_every_markdown_source() -> None:
    config = yaml.safe_load(MKDOCS_CONFIG.read_text(encoding="utf-8"))
    nav_paths = _flatten_nav(config["nav"])
    source_paths = sorted(
        path.relative_to(DOCS_ROOT).as_posix() for path in DOCS_ROOT.rglob("*.md")
    )

    assert sorted(nav_paths) == source_paths
    assert config["site_url"] == PUBLIC_URL
    assert config["docs_dir"] == "docs"
    assert config["site_dir"] == "site"
    assert config["strict"] is True
    assert config["extra_css"] == ["stylesheets/technical.css"]
    assert {"toc": {"permalink": "#"}} in config["markdown_extensions"]
    assert "search" in [
        item if isinstance(item, str) else next(iter(item))
        for item in config["plugins"]
    ]
    features = config["theme"]["features"]
    assert "navigation.instant" not in features
    assert "navigation.sections" in features
    assert "search.suggest" in features


def test_generated_route_reference_matches_every_flask_rule() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/generate_http_api_reference.py",
            "--check",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    reference = ROUTE_REFERENCE.read_text(encoding="utf-8")
    rules = [rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"]
    operation_count = sum(
        len(set(rule.methods or ()) - {"HEAD", "OPTIONS"}) for rule in rules
    )
    assert f"all **{len(rules)}** non-static Flask rules" in reference
    assert f"**{operation_count}** method-specific operations" in reference
    assert "every application-declared\nmethod has its own row" in reference
    assert "implicit `HEAD` and `OPTIONS` methods are\nexcluded" in reference
    assert "every allowed method" not in reference
    for rule in rules:
        for method in set(rule.methods or ()) - {"HEAD", "OPTIONS"}:
            row_start = f"| `{method}` | `{rule.rule}` |"
            matching_rows = [
                line
                for line in reference.splitlines()
                if line.startswith(row_start) and line.endswith(f"`{rule.endpoint}` |")
            ]
            assert len(matching_rows) == 1, (rule.endpoint, method, matching_rows)

    assert "`GET, POST`" not in reference
    mixed_method_purposes = {
        ("GET", "/capture/jobs"): "List capture jobs",
        ("POST", "/capture/jobs"): "Queue the supervised capture recipe",
        ("GET", "/run-config"): "Load run configuration",
        ("POST", "/run-config"): "Create or update run configuration",
        ("GET", "/sync/quality"): "Build synchronization quality report",
        ("POST", "/sync/quality"): ("Build and persist synchronization quality report"),
        ("GET", "/hardware/status"): "Load hardware status report",
        ("POST", "/hardware/status"): ("Collect and persist hardware status report"),
    }
    for (method, path), purpose in mixed_method_purposes.items():
        assert f"| `{method}` | `{path}` | JSON | {purpose} |" in reference


def test_strict_build_has_search_persistent_pages_and_resolved_links(
    built_site: Path,
) -> None:
    expected_pages = {
        "index.html",
        "getting-started/overview/index.html",
        "concepts/architecture/index.html",
        "reference/http-api/index.html",
        "reference/http-api-routes/index.html",
        "reference/api/capture-pipeline/index.html",
        "reference/run-config/index.html",
        "OPERATOR_WORKFLOWS/index.html",
        "COMMISSIONING/index.html",
    }
    for relative in expected_pages:
        assert (built_site / relative).is_file(), relative
    assert (built_site / "search" / "search_index.json").is_file()
    assert (built_site / "sitemap.xml").is_file()

    missing: list[tuple[str, str]] = []
    for page in built_site.rglob("*.html"):
        parser = DocumentParser()
        parser.feed(page.read_text(encoding="utf-8"))
        assert parser.html_attributes.get("lang") == "en"
        if page.name == "index.html" and "404" not in page.parts:
            assert parser.h1_count == 1
        for reference in [*parser.links, *parser.sources]:
            target = _local_target(built_site, page, reference)
            if target is not None and not target.exists():
                missing.append((page.relative_to(built_site).as_posix(), reference))
    assert missing == []

    home = (built_site / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{PUBLIC_URL}">' in home
    assert 'href="stylesheets/technical.css"' in home
    assert "PoseTestBot technical documentation" in home
    assert "Repository boundary" in home
    assert "From supervised capture to a dataset you can explain" not in home

    sitemap = ElementTree.parse(built_site / "sitemap.xml")
    locations = [
        item.text
        for item in sitemap.findall(
            "sitemap:url/sitemap:loc",
            {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"},
        )
    ]
    assert PUBLIC_URL in locations
    assert f"{PUBLIC_URL}reference/http-api/" in locations


def test_pages_workflow_builds_source_before_uploading_generated_site() -> None:
    workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")

    assert "actions/checkout@v7" in workflow
    assert "astral-sh/setup-uv@" in workflow
    teaching_plot_check = (
        "uv run --frozen --no-default-groups --group docs python "
        "scripts/plot_iiwa_calibration_teaching_plan.py --check"
    )
    route_check = (
        "uv run --frozen --no-default-groups --group docs python "
        "scripts/generate_http_api_reference.py --check"
    )
    strict_build = (
        "uv run --frozen --no-default-groups --group docs mkdocs build --strict"
    )
    assert teaching_plot_check in workflow
    assert "MPLCONFIGDIR: /tmp/posetestbot-mpl" in workflow
    assert route_check in workflow
    assert strict_build in workflow
    assert "actions/configure-pages@v5" in workflow
    assert "actions/upload-pages-artifact@v4" in workflow
    assert "actions/deploy-pages@v4" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    assert "path: site" in workflow
    assert workflow.index(teaching_plot_check) < workflow.index(route_check)
    assert workflow.index(route_check) < workflow.index(strict_build)
    assert workflow.index(strict_build) < workflow.index(
        "actions/upload-pages-artifact"
    )
    for trigger in (
        '"docs/**"',
        '"iiwa/calibration_teaching_plan.v2.json"',
        '"mkdocs.yml"',
        '"posetestbot/calibration/teaching_plan.py"',
        '"posetestbot/web/app.py"',
        '"posetestbot/web/routes/**"',
        '"scripts/generate_http_api_reference.py"',
        '"scripts/plot_iiwa_calibration_teaching_plan.py"',
        '"pyproject.toml"',
        '"uv.lock"',
    ):
        assert trigger in workflow


def test_documentation_dependency_and_generated_output_contract() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    maintenance = (DOCS_ROOT / "GITHUB_PAGES.md").read_text(encoding="utf-8")
    teaching_checklist = (
        DOCS_ROOT / "IIWA_CALIBRATION_TEACHING_CHECKLIST.md"
    ).read_text(encoding="utf-8")
    typography = TECHNICAL_STYLESHEET.read_text(encoding="utf-8")

    assert re.search(r"(?ms)^docs = \[.*mkdocs-material>=9\.7\.7.*^\]", project)
    assert re.search(r"(?m)^site/$", ignore)
    assert "Technical documentation" in readme
    assert PUBLIC_URL in readme
    assert "--md-text-font: system-ui" in typography
    assert ".md-typeset h1" in typography
    assert "font-weight: 500" in typography
    assert ".md-typeset .headerlink" in typography
    assert (
        "generated `site/` directory is a disposable ignored build artifact"
        in " ".join(maintenance.split())
    )
    assert "(images/iiwa_calibration_teaching_plan.svg)" in teaching_checklist
    assert (DOCS_ROOT / "images" / "iiwa_calibration_teaching_plan.svg").is_file()
    assert not (DOCS_ROOT / "images" / "iiwa_calibration_teaching_plan.png").exists()
    assert not (ROOT / "site" / "app.js").exists()
    assert not (ROOT / "site" / "styles.css").exists()
