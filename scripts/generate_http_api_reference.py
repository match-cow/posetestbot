#!/usr/bin/env python3
"""Generate the checked-in HTTP route inventory from the Flask application."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from flask import Flask
from werkzeug.routing import Rule

from posetestbot.web.app import app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "docs" / "reference" / "http-api-routes.md"
IGNORED_ENDPOINTS = {"static"}


@dataclass(frozen=True)
class RouteRecord:
    group: str
    method: str
    path: str
    interface: str
    purpose: str
    endpoint: str


def _group(path: str) -> str:
    prefixes = (
        ("/bop/", "BOP annotation and evaluation"),
        ("/calibration-targets", "Calibration targets"),
        ("/calibration/", "Calibration"),
        ("/capture", "Capture"),
        ("/cluster/", "External cluster controller proxy"),
        ("/dataset-processing/", "Focused orchestration"),
        ("/jobs", "Local jobs and commands"),
        ("/monitoring/", "Monitoring"),
        ("/pose-templates/", "Pose templates"),
        ("/preflight/", "Focused orchestration"),
        ("/robot/commands", "Focused orchestration"),
        ("/robot/", "System status"),
        ("/run-config", "Focused orchestration"),
        ("/runtime/", "System status"),
        ("/sensors/", "Sensors"),
        ("/sync/", "Synchronization"),
        ("/system/", "System lifecycle"),
        ("/ui/", "Operator-console data"),
        ("/workpieces/", "Workpiece Catalogue"),
        ("/hardware/", "System status"),
    )
    for prefix, name in prefixes:
        if path.startswith(prefix):
            return name
    return "Operator console and static assets"


INTERFACE_OVERRIDES = {
    "calibration_targets.calibration_target_preview": "PNG",
    "jobs_commands.get_job_log": "Text",
    "pose_templates.library_thumbnail": "JSON",
    "pose_templates.workpiece_orientation_thumbnail": "JSON",
    "ui.ui_cell_camera_frame": "PNG",
    "workpieces.workpiece_catalog_export": "File",
}


def _interface(path: str, endpoint: str) -> str:
    if endpoint in INTERFACE_OVERRIDES:
        return INTERFACE_OVERRIDES[endpoint]
    if path == "/":
        return "HTML"
    if path.endswith(".png"):
        return "PNG"
    if path.endswith(".jpg"):
        return "JPEG"
    if path.endswith(".svg"):
        return "SVG"
    if "/download" in path or endpoint.endswith(
        (
            "_asset",
            "_download",
            "_image",
            "_log",
            "_pdf",
            "_report",
        )
    ):
        return "File"
    return "JSON"


PURPOSE_OVERRIDES = {
    "brand_asset": "Serve an operator-console brand asset",
    "hri_cell_template": "Serve the fixed HRI cell SVG",
    "pages.index": "Serve the bundled operator console",
}

METHOD_PURPOSE_OVERRIDES = {
    ("capture.list_capture_jobs", "GET"): "List capture jobs",
    ("capture.list_capture_jobs", "POST"): "Queue the supervised capture recipe",
    ("orchestration.run_config", "GET"): "Load run configuration",
    ("orchestration.run_config", "POST"): "Create or update run configuration",
    ("sync_quality.sync_quality_endpoint", "GET"): (
        "Build synchronization quality report"
    ),
    ("sync_quality.sync_quality_endpoint", "POST"): (
        "Build and persist synchronization quality report"
    ),
    ("system_status.hardware_status", "GET"): "Load hardware status report",
    ("system_status.hardware_status", "POST"): (
        "Collect and persist hardware status report"
    ),
}


def _purpose(endpoint: str, method: str) -> str:
    if (endpoint, method) in METHOD_PURPOSE_OVERRIDES:
        return METHOD_PURPOSE_OVERRIDES[(endpoint, method)]
    if endpoint in PURPOSE_OVERRIDES:
        return PURPOSE_OVERRIDES[endpoint]
    function_name = endpoint.rsplit(".", maxsplit=1)[-1]
    for suffix in ("_endpoint", "_response"):
        if function_name.endswith(suffix):
            function_name = function_name[: -len(suffix)]
    return function_name.replace("_", " ").capitalize()


def non_static_rules(application: Flask) -> list[Rule]:
    return [
        rule
        for rule in application.url_map.iter_rules()
        if rule.endpoint not in IGNORED_ENDPOINTS
    ]


def route_records(application: Flask) -> list[RouteRecord]:
    records: list[RouteRecord] = []
    for rule in non_static_rules(application):
        for method in sorted(set(rule.methods or ()) - {"HEAD", "OPTIONS"}):
            records.append(
                RouteRecord(
                    group=_group(rule.rule),
                    method=method,
                    path=rule.rule,
                    interface=_interface(rule.rule, rule.endpoint),
                    purpose=_purpose(rule.endpoint, method),
                    endpoint=rule.endpoint,
                )
            )
    return sorted(records, key=lambda item: (item.group, item.path, item.method))


def render_route_index(application: Flask) -> str:
    records = route_records(application)
    rule_count = len(non_static_rules(application))
    grouped: dict[str, list[RouteRecord]] = defaultdict(list)
    for record in records:
        grouped[record.group].append(record)

    lines = [
        "<!-- Generated by scripts/generate_http_api_reference.py; do not edit. -->",
        "",
        "# Complete HTTP route index",
        "",
        f"This index contains all **{rule_count}** non-static Flask rules registered by",
        "`posetestbot.web.app.create_app`, rendered as",
        f"**{len(records)}** method-specific operations. It is generated from the",
        "running route map, so aliases remain explicit and every application-declared",
        "method has its own row. Flask's implicit `HEAD` and `OPTIONS` methods are",
        "excluded.",
        "",
        "The index describes reachability and transport type. Request bodies, state",
        "transitions, safety gates, and response semantics are documented in the",
        "[domain API guides](http-api.md). The implicit Flask `/static/<path:filename>`",
        "handler is intentionally excluded.",
        "",
        "Regenerate after changing a route:",
        "",
        "```bash",
        "uv run python scripts/generate_http_api_reference.py --write",
        "uv run python scripts/generate_http_api_reference.py --check",
        "```",
        "",
    ]
    for group in sorted(grouped):
        lines.extend(
            [
                f"## {group}",
                "",
                "| Method | Path | Returns | Purpose | Flask endpoint |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for record in grouped[group]:
            lines.append(
                "| "
                + " | ".join(
                    (
                        f"`{record.method}`",
                        f"`{record.path}`",
                        record.interface,
                        record.purpose,
                        f"`{record.endpoint}`",
                    )
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--write", action="store_true", help="replace the generated file"
    )
    action.add_argument(
        "--check", action="store_true", help="fail if the file is stale"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    output = arguments.output.resolve()
    rendered = render_route_index(app)
    if arguments.check:
        try:
            current = output.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current != rendered:
            print(
                f"{output} is stale; run "
                "`uv run python scripts/generate_http_api_reference.py --write`."
            )
            return 1
        print(
            "HTTP route reference is current "
            f"({len(non_static_rules(app))} rules, "
            f"{len(route_records(app))} operations)."
        )
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(
        f"Wrote {output} ({len(non_static_rules(app))} rules, "
        f"{len(route_records(app))} operations)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
