# Documentation site maintenance

The technical documentation is published at
<https://match-cow.github.io/posetestbot/>. Authoritative source is Markdown
below `docs/`, organized by `mkdocs.yml`. The generated `site/` directory is a
disposable ignored build artifact.

## Framework contract

The site uses Material for MkDocs from the locked `docs` dependency group. It
provides persistent multi-page URLs, hierarchical desktop navigation, a
keyboard-accessible drawer at narrower widths, table-of-contents navigation,
and client-side full-text search.

`navigation.instant` is intentionally not enabled. Navigation uses ordinary
document requests, which keeps URLs, browser history, direct links, and static
hosting behavior simple and testable.

The local `stylesheets/technical.css` defines the cross-browser typography
contract. It uses the browser's platform-tuned UI and monospace faces, gives
primary headings a stable medium-weight request instead of Material's thin
cross-platform fallback, and centers the compact heading permalink
independently of the text baseline. Keep this stylesheet self-contained: the
published reference must not depend on a third-party font request.

## Information architecture

The navigation is technical and task-oriented:

1. system overview, installation, and canonical operator workflows;
2. architecture, filesystem boundaries, artifact lineage, and safety;
3. HTTP conventions, generated complete route inventory, and domain API
   contracts;
4. run-config, artifact, and CLI reference;
5. specialist calibration, workpiece, template, IIWA, and physical
   commissioning guides; and
6. the clean-break design record and documentation-maintenance contract.

Add every Markdown page to `nav` in `mkdocs.yml`. Strict builds fail when a
navigation target or internal documentation link is missing.

## HTTP route inventory

`docs/reference/http-api-routes.md` is generated from Flask's registered URL
map. Do not edit it directly.

```bash
uv run python scripts/generate_http_api_reference.py --write
uv run python scripts/generate_http_api_reference.py --check
```

The focused repository test runs `--check`, so a route change without a
matching regenerated index fails validation. Behavioral request/response and
safety semantics belong in the appropriate `docs/reference/api/` domain page.

## Preview and validate locally

Build exactly as CI does:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --only-group docs \
  mkdocs build --strict
```

In a normal development environment already synchronized with all groups:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run mkdocs serve \
  --dev-addr 127.0.0.1:8000
```

Then open <http://127.0.0.1:8000/>. Validate source/build contracts and actual
navigation/search behavior with:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_github_pages.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -m playwright \
  tests/test_github_pages_playwright.py
```

Chromium installation remains explicitly opt-in.

## Publish

`.github/workflows/pages.yml` runs for documentation, MkDocs configuration, and
locked dependency changes on `main`. It installs `uv`, creates a docs-only
environment, runs a strict build, uploads only generated `site/`, and deploys
through the protected `github-pages` environment. The repository Pages source
must remain **GitHub Actions**.

The deployment does not publish repository source, run data, credentials, or
the Flask operator API. It publishes only MkDocs output.

## Update checklist

1. Change authoritative Markdown and, when required, `mkdocs.yml` navigation.
2. Regenerate the HTTP route index after any Flask route-map change.
3. Run the strict MkDocs build.
4. Run focused source and Playwright navigation tests.
5. Check `git diff --check` and confirm `site/` is not staged.
