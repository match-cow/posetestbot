# Public GitHub Pages Site

The plain-language project guide is published at
<https://match-cow.github.io/posetestbot/>. Its tracked source lives in
`site/`; it is separate from both the Flask operator console and the detailed
Markdown manuals in `docs/`.

## Why it is separate

The public page introduces the project before asking a reader to navigate its
technical contracts. It explains the two guided outcomes, a safe software-only
first launch, physical-execution boundaries, retained evidence, repository
scope, current lab hardware, research outputs, and the next detailed guide for
each task.

The site uses semantic HTML, system fonts, and local assets only. Its core
content remains available without JavaScript. JavaScript adds the remembered
light/dark theme and the quick-start copy button. Keyboard focus, reduced-motion
preferences, high-contrast preferences, narrow-view reachability, and print
output have explicit styles.

## Preview and validate locally

From the repository root, start a loopback-only static server:

```bash
uv run python -m http.server 8000 --bind 127.0.0.1 --directory site
```

Then open <http://127.0.0.1:8000/>. Validate the source and browser behavior
with:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_github_pages.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -m playwright \
  tests/test_github_pages_playwright.py
```

The Playwright module checks meaningful desktop and narrow-view contracts,
including keyboard skip navigation, image loading, horizontal overflow, theme
persistence, command copying, and expandable glossary content. Chromium must
already be installed; browser installation remains explicitly opt-in.

## Publish

`.github/workflows/pages.yml` uploads only `site/` and deploys it through the
protected `github-pages` environment. A push to `main` that changes the site or
workflow starts deployment; maintainers can also run it manually. The
repository's Pages source must be **GitHub Actions**.

Keep these publication details consistent when the repository name or site URL
changes:

- the canonical and Open Graph URLs in `site/index.html`;
- `site/robots.txt` and `site/sitemap.xml`;
- the visual-guide links in `README.md`; and
- the repository homepage field on GitHub.

The logo mark at `site/assets/posetestbot-mark.png` is a deliberate published
copy of `posetestbot/web/static/cow_favicon.png`, so the Pages artifact stays
self-contained. Update both when the established project mark changes.

Detailed documentation links in the page point to the authoritative files on
the `main` branch. Do not use links from the deployed site into sibling paths
outside `site/`: the Pages artifact intentionally does not publish the source
tree.
