"""Capture the operator-console screenshots used by the user guide.

The screenshots in ``docs/images/console/`` are produced only by this script,
so they can be refreshed after a visible console change. ``SCREENSHOTS`` is
the single list of published images; ``tests/test_github_pages.py`` checks
that it matches the directory and that every image is used by a page.

Two sources are supported:

* ``live`` (default): a running console, normally the lab service at
  ``http://127.0.0.1:5000``. The capture is read-only. Every non-GET request
  and the room-monitor stream are aborted in the browser, so no job, preview,
  inventory refresh, or hardware command can be triggered. Pick a completed
  dataset run with ``--run`` so the workflow pages show real evidence.
* ``fixture``: pages without real data (such as Pose Results before any
  estimator result exists) are rendered from the Playwright test fixtures in
  ``tests/test_web_console_playwright.py``. Their captions must say so.

Before every capture, visible text is anonymized: the run-folder name becomes
``example-dataset``, its parent directory ``/data/runs``, the repository path
``~/PoseTestBot``, plus every ``--replace OLD=NEW`` pair (use this for personal
names in run, template, or workpiece names). Inspect the images before
committing them.

Examples::

    uv run python scripts/capture_docs_screenshots.py \\
        --run /mnt/working_data_ssd/my-dataset --replace alice=Example
    uv run python scripts/capture_docs_screenshots.py --list
    uv run python scripts/capture_docs_screenshots.py --only dataset-5-process jobs
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "images" / "console"
VIEWPORT = {"width": 1600, "height": 1000}
SIDEBAR_WIDTH = 244
TOP_BAR_HEIGHT = 56
MAIN = (SIDEBAR_WIDTH, TOP_BAR_HEIGHT, VIEWPORT["width"], VIEWPORT["height"])
FULL = (0, 0, VIEWPORT["width"], VIEWPORT["height"])
WEBP_QUALITY = 82


@dataclass(frozen=True)
class Screenshot:
    name: str
    route: str
    crop: tuple[int, int, int, int] = MAIN
    source: str = "live"
    settle_seconds: float = 3.0
    # Numbered callouts drawn on the image: number -> (x, y) in viewport pixels.
    markers: tuple[tuple[int, int, int], ...] = ()


SCREENSHOTS: tuple[Screenshot, ...] = (
    Screenshot(
        "console-tour",
        "/dashboard",
        crop=FULL,
        markers=(
            (1, 236, 270),
            (2, 262, 28),
            (3, 290, 212),
            (4, 290, 500),
            (5, 1048, 846),
            (6, 236, 922),
            (7, 1290, 62),
            (8, 1045, 28),
        ),
    ),
    Screenshot("workflow-choose", "/workflow/setup", crop=(SIDEBAR_WIDTH, TOP_BAR_HEIGHT, 1600, 720)),
    Screenshot("dataset-1-configure", "/workflow/dataset?step=configure"),
    Screenshot("dataset-2-template", "/workflow/dataset?step=template"),
    Screenshot("dataset-3-readiness", "/workflow/dataset?step=readiness"),
    Screenshot("dataset-4-capture", "/workflow/dataset?step=capture"),
    Screenshot("dataset-5-process", "/workflow/dataset?step=sync"),
    Screenshot("dataset-6-ground-truth", "/workflow/dataset?step=export"),
    Screenshot("calibration-targets", "/calibration-targets"),
    Screenshot("calibrations", "/calibrations"),
    Screenshot("workpieces", "/workpieces", settle_seconds=6.0),
    Screenshot("pose-templates", "/pose-templates", settle_seconds=6.0),
    Screenshot("cell-view", "/cell", settle_seconds=6.0),
    Screenshot("devices", "/devices"),
    Screenshot("jobs", "/jobs"),
    Screenshot("run-folders", "/run-folders"),
    Screenshot("pose-estimation", "/pose-estimation"),
    Screenshot("bop-evaluation", "/bop-evaluation"),
    Screenshot("depth-evaluation", "/depth-evaluation"),
    Screenshot("pose-results", "/pose-results", source="fixture"),
)

ANONYMIZE_SCRIPT = """(pairs) => {
  const swap = (value) => pairs.reduce(
    (text, [from, to]) => text.split(from).join(to), value);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) node.nodeValue = swap(node.nodeValue);
  for (const element of document.querySelectorAll('input, textarea')) {
    if (element.value) element.value = swap(element.value);
  }
}"""


def replacement_pairs(run_root: str | None, extra: list[str]) -> list[tuple[str, str]]:
    # Specific paths first, so a generic --replace cannot pre-empt them.
    pairs: list[tuple[str, str]] = []
    if run_root:
        run = Path(run_root)
        pairs.append((run.name, "example-dataset"))
        pairs.append((str(run.parent), "/data/runs"))
    pairs.append((str(ROOT), "~/PoseTestBot"))
    for item in extra:
        old, separator, new = item.partition("=")
        if not separator or not old:
            raise SystemExit(f"--replace expects OLD=NEW, got {item!r}")
        pairs.extend([(old, new), (old.capitalize(), new.capitalize()), (old.upper(), new.upper())])
    return pairs


def _settle(page, shot: Screenshot) -> None:
    try:
        page.get_by_text("Loading view").wait_for(state="detached", timeout=20_000)
    except Exception:  # The placeholder may never appear for a cached chunk.
        pass
    page.wait_for_load_state("networkidle")
    time.sleep(shot.settle_seconds)
    page.add_style_tag(content="[data-sonner-toaster]{display:none!important}")
    if "?step=" in shot.route:
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.5)


def _publish(raw_png: Path, shot: Screenshot, output: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(raw_png).convert("RGB")
    if shot.markers:
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
        except OSError:
            font = ImageFont.load_default()
        radius = 17
        for number, x, y in shot.markers:
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=(214, 40, 90),
                outline="white",
                width=3,
            )
            draw.text((x, y), str(number), fill="white", font=font, anchor="mm")
    target = output / f"{shot.name}.webp"
    image.crop(shot.crop).save(target, "WEBP", quality=WEBP_QUALITY, method=6)
    return target


def capture_live(shots, base_url: str, run_root: str | None, pairs, output: Path, raw: Path) -> list[str]:
    from playwright.sync_api import sync_playwright

    blocked: list[str] = []

    def guard(route) -> None:
        request = route.request
        if request.method != "GET" or "/monitoring/" in request.url or "webrtc" in request.url:
            blocked.append(f"{request.method} {request.url}")
            route.abort()
        else:
            route.continue_()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT, color_scheme="light")
        init = [
            "localStorage.setItem('posetestbot.currentContracts.v4', 'reset');",
            "localStorage.setItem('theme', 'light');",
        ]
        if run_root:
            init.append(f"localStorage.setItem('posetestbot.selectedRun', {run_root!r});")
        context.add_init_script("\n".join(init))
        context.route("**/*", guard)
        page = context.new_page()
        for shot in shots:
            page.goto(f"{base_url.rstrip('/')}/#{shot.route}", wait_until="networkidle", timeout=60_000)
            _settle(page, shot)
            page.evaluate(ANONYMIZE_SCRIPT, [list(pair) for pair in pairs])
            png = raw / f"{shot.name}.png"
            page.screenshot(path=str(png))
            print("captured", _publish(png, shot, output).relative_to(ROOT) if output.is_relative_to(ROOT) else shot.name)
        browser.close()
    return sorted(set(blocked))


def capture_fixtures(shots, output: Path, raw: Path) -> None:
    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(ROOT / "tests"))
    import test_web_console_playwright as fixtures  # noqa: PLC0415

    server = fixtures.LiveServer()
    server.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for shot in shots:
                page = browser.new_page(viewport=VIEWPORT)
                fixtures.install_common_mocks(page)
                if shot.name != "pose-results":
                    raise SystemExit(f"No fixture setup is defined for {shot.name}")
                results = fixtures.install_pose_inspection_mocks(page)
                page.goto(
                    f"{server.url}/#{shot.route}?result_id={results[0]['result_id']}&scene_id=1&frame_id=0",
                    wait_until="networkidle",
                )
                viewer = page.get_by_test_id("pose-result-viewer")
                viewer.wait_for()
                time.sleep(shot.settle_seconds)
                viewer.scroll_into_view_if_needed()
                time.sleep(1)
                png = raw / f"{shot.name}.png"
                page.screenshot(path=str(png))
                print("captured", _publish(png, shot, output).name, "(fixture data)")
                page.close()
            browser.close()
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="running console (live source)")
    parser.add_argument("--run", help="completed dataset run root to make active in the browser")
    parser.add_argument("--replace", action="append", default=[], metavar="OLD=NEW", help="extra visible-text replacement")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", nargs="+", metavar="NAME", help="capture only these screenshots")
    parser.add_argument("--list", action="store_true", help="print the screenshot manifest and exit")
    args = parser.parse_args(argv)

    if args.list:
        for shot in SCREENSHOTS:
            print(f"{shot.name:24} {shot.source:8} {shot.route}")
        return 0

    names = {shot.name for shot in SCREENSHOTS}
    unknown = sorted(set(args.only or ()) - names)
    if unknown:
        parser.error(f"unknown screenshot(s): {', '.join(unknown)}")
    selected = [shot for shot in SCREENSHOTS if not args.only or shot.name in args.only]
    args.output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="posetestbot-docs-shots-") as tmp:
        raw = Path(tmp)
        live = [shot for shot in selected if shot.source == "live"]
        if live:
            if not args.run:
                print("warning: no --run given; workflow pages show the browser's default run", file=sys.stderr)
            blocked = capture_live(live, args.base_url, args.run, replacement_pairs(args.run, args.replace), args.output, raw)
            for request in blocked:
                print("blocked (read-only capture):", request)
        fixture = [shot for shot in selected if shot.source == "fixture"]
        if fixture:
            capture_fixtures(fixture, args.output, raw)
    print("Review every image for personal data before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
