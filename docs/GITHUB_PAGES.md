# Documentation site maintenance

The documentation (user guide and technical reference) is published at
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

The site is written first for students who use the operator console, with
technical depth available on demand. `navigation.tabs` splits it into five
top-level tabs:

1. **Home**: what PoseTestBot is and links to the main tasks. The console's
   **Documentation** button opens this page.
2. **Get started**: quickstart, annotated console tour, and lab safety.
3. **User guide**: one task page per console outcome (calibrate, objects and
   templates, record, pose estimation, pose and depth evaluation, runs and
   data, troubleshooting, glossary). Each page uses console labels, shows
   screenshots, and ends with a collapsed **Technical details** block that
   links into the reference.
4. **Technical reference**: system overview, detailed workflow contracts,
   architecture, HTTP API, run-config/artifact/CLI reference, and component
   guides.
5. **Lab administration**: installation, physical commissioning, IIWA
   integration, and this maintenance page.

Keep user-guide pages short and procedural. Put contracts, schemas, and
edge cases in the reference and link to them, rather than expanding the guide.

Add every Markdown page to `nav` in `mkdocs.yml`. Strict builds fail when a
navigation target or internal documentation link is missing.

## Console screenshots

User-guide screenshots in `docs/images/console/` are generated only by
`scripts/capture_docs_screenshots.py`. Its `SCREENSHOTS` manifest lists every
published image, the console route, the crop, and any numbered callouts. Do
not add, crop, or edit images by hand.

```bash
uv run python scripts/capture_docs_screenshots.py --list
uv run python scripts/capture_docs_screenshots.py \
  --run /mnt/working_data_ssd/<completed-dataset-run> --replace <name>=example
uv run python scripts/capture_docs_screenshots.py --only dataset-5-process jobs
```

The default source is the running lab console at `http://127.0.0.1:5000`.
The capture is read-only: the browser aborts every non-`GET` request and the
room-monitor stream, so it cannot queue jobs, start previews, or command
hardware. Choose a completed dataset run so the workflow steps show real
evidence. The script replaces the run name, its parent directory, and the
repository path in visible text. Pass `--replace OLD=NEW` for personal names
in run, template, or workpiece names. Review every image before committing.

Screens without real data use the Playwright console fixtures
(`source="fixture"`), currently **Pose Results**. Their page must say that the
screenshot shows synthetic test data.

`tests/test_github_pages.py` enforces three rules: the image directory matches
the manifest exactly, every image is referenced by a page, and every console
label in `USER_GUIDE_CONSOLE_LABELS` still exists in `frontend/src` and in
the user guide. Renaming a button that the guide quotes fails that test
until the guide is updated.

### Keeping the user guide current

When a change alters what an operator sees or does:

1. Update the matching page in `docs/guide/` (and `docs/quickstart.md` if the
   overall path changes) in the same change. Use the console's exact labels.
2. Update `USER_GUIDE_CONSOLE_LABELS` when the guide starts or stops quoting a
   label.
3. Rebuild the frontend, then recapture the affected screenshots with
   `--only` once the live console serves the new build. Add a `SCREENSHOTS`
   entry for a new guide image, or remove the entry and the file for an
   obsolete one.
4. Keep contracts, schemas, and edge cases in the technical reference. The
   guide's collapsed **Technical details** block links to them.

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
MPLCONFIGDIR=/tmp/posetestbot-mpl UV_CACHE_DIR=/tmp/uv-cache \
  uv run --frozen --no-default-groups --group docs \
  python scripts/plot_iiwa_calibration_teaching_plan.py --check
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --no-default-groups --group docs \
  python scripts/generate_http_api_reference.py --check
UV_CACHE_DIR=/tmp/uv-cache uv run --frozen --no-default-groups --group docs \
  mkdocs build --strict
```

The teaching-plot check renders to a temporary file and compares it with the
committed SVG without rewriting the documentation tree. It ignores only opaque
Matplotlib-internal SVG identifiers. The route check loads the Flask
application, so CI includes project runtime and documentation dependencies
while excluding the default development group.

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

`.github/workflows/pages.yml` runs for documentation, MkDocs configuration,
teaching-plan and teaching-plot inputs, route-generator, Flask web-route, and
locked dependency changes on `main`. It installs `uv`, checks both generated
assets, runs a strict build, uploads only generated `site/`, and deploys through
the protected `github-pages` environment. The repository Pages source must
remain **GitHub Actions**.

The deployment does not publish repository source, run data, credentials, or
the Flask operator API. It publishes only MkDocs output.

## Update checklist

1. Change authoritative Markdown and, when required, `mkdocs.yml` navigation.
   For operator-visible changes, update the user guide and recapture affected
   screenshots (see [Keeping the user guide current](#keeping-the-user-guide-current)).
2. Regenerate and check the teaching SVG after a teaching-plan or plot change.
3. Regenerate the HTTP route index after any Flask route-map change.
4. Run the strict MkDocs build.
5. Run focused source and Playwright navigation tests.
6. Check `git diff --check` and confirm `site/` is not staged.
