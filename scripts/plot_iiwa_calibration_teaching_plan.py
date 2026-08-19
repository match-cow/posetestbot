#!/usr/bin/env python3
"""Render the nine-frame iiwa calibration teaching aid from its manifest."""

from __future__ import annotations

import argparse
from itertools import pairwise
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Polygon, Rectangle
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from posetestbot.calibration.teaching_plan import (
    DEFAULT_TEACHING_PLAN_PATH,
    frames_by_name,
    load_teaching_plan,
    relative_result_transform_matrix,
    seed_transform_matrix,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SVG_PATH = REPO_ROOT / "docs" / "images" / "iiwa_calibration_teaching_plan.svg"
MATPLOTLIB_SVG_ID = re.compile(r'id="([A-Za-z][0-9a-f]{10})"')

COVERAGE = "#00a6c7"
ORIENTATION = "#c23bb5"
TRANSIT = "#737b85"
TEMPLATE = "#d9dde3"
AXIS_COLORS = ("#d62728", "#2ca02c", "#1f77b4")
RESULT_LABELS = (
    "A−15°",
    "A+15°",
    "Center",
    "B−12°",
    "B+12°",
    "Center",
    "C−15°",
    "C+15°",
    "Center",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the headless SVG iiwa calibration teaching aid."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_TEACHING_PLAN_PATH,
        help="Versioned teaching-plan JSON manifest.",
    )
    parser.add_argument(
        "--svg", type=Path, default=DEFAULT_SVG_PATH, help="SVG output path."
    )
    parser.add_argument(
        "--png",
        type=Path,
        default=None,
        help="Optional PNG output path; no raster copy is written when omitted.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the existing SVG does not match the generated teaching aid.",
    )
    args = parser.parse_args(argv)
    if args.check and args.png is not None:
        parser.error("--check cannot be combined with --png")
    return args


def _template_corners(plan: Mapping[str, Any]) -> np.ndarray:
    template = plan["template"]
    half_width = float(template["width_mm"]) / 2.0
    half_height = float(template["height_mm"]) / 2.0
    return np.array(
        [
            [-half_width, -half_height, 0.0],
            [half_width, -half_height, 0.0],
            [half_width, half_height, 0.0],
            [-half_width, half_height, 0.0],
        ]
    )


def _draw_triad_3d(ax: Any, transform: np.ndarray, *, length: float = 34.0) -> None:
    origin = transform[:3, 3]
    rotation = transform[:3, :3]
    for axis_index, color in enumerate(AXIS_COLORS):
        direction = rotation[:, axis_index] * length
        ax.quiver(
            origin[0],
            origin[1],
            origin[2],
            direction[0],
            direction[1],
            direction[2],
            color=color,
            linewidth=0.75,
            arrow_length_ratio=0.18,
        )


def _set_3d_equal(ax: Any, points: np.ndarray) -> None:
    minima = points.min(axis=0)
    maxima = points.max(axis=0)
    centers = (minima + maxima) / 2.0
    radius = float(np.max(maxima - minima)) / 2.0
    ax.set_xlim(centers[0] - radius, centers[0] + radius)
    ax.set_ylim(centers[1] - radius, centers[1] + radius)
    ax.set_zlim(centers[2] - radius, centers[2] + radius)
    ax.set_box_aspect((1, 1, 1))


def _arrow_3d(ax: Any, start: np.ndarray, end: np.ndarray, color: str) -> None:
    delta = end - start
    ax.plot(
        [start[0], end[0]],
        [start[1], end[1]],
        [start[2], end[2]],
        color=color,
        linewidth=1.4,
        solid_capstyle="round",
    )
    arrow_start = start + delta * 0.72
    arrow_delta = delta * 0.28
    ax.quiver(
        arrow_start[0],
        arrow_start[1],
        arrow_start[2],
        arrow_delta[0],
        arrow_delta[1],
        arrow_delta[2],
        color=color,
        linewidth=1.4,
        arrow_length_ratio=0.45,
    )


def _draw_isometric(ax: Any, plan: Mapping[str, Any]) -> None:
    frames = frames_by_name(plan)
    corners = _template_corners(plan)
    ax.add_collection3d(
        Poly3DCollection(
            [corners],
            facecolor=TEMPLATE,
            edgecolor="#32363d",
            alpha=0.55,
            linewidth=1.0,
        )
    )
    ax.text(
        0,
        0,
        8,
        "HRI template 420 × 297 mm\ncentered at TemplateBase",
        ha="center",
        va="bottom",
        fontsize=7,
        color="#32363d",
    )
    _draw_triad_3d(ax, np.eye(4), length=55.0)
    ax.text(5, 5, 60, "TemplateBase", fontsize=7, weight="bold")

    transforms = {name: seed_transform_matrix(frame) for name, frame in frames.items()}
    for name, transform in transforms.items():
        position = transform[:3, 3]
        size = 28 if name == "CalibrationCenter" else 18
        ax.scatter(*position, color=COVERAGE, s=size, depthshade=False)
        _draw_triad_3d(ax, transform)

    coverage = plan["phases"][0]
    for motion in coverage["motions"]:
        start = transforms[motion["from"]][:3, 3]
        end = transforms[motion["to"]][:3, 3]
        if motion["motion_type"] == "LIN":
            _arrow_3d(ax, start, end, COVERAGE)
        else:
            ax.plot(
                [start[0], end[0]],
                [start[1], end[1]],
                [start[2], end[2]],
                color=TRANSIT,
                linewidth=1.0,
                linestyle="--",
            )

    center = frames["CalibrationCenter"]
    for motion, result_label in zip(
        plan["phases"][1]["motions"], RESULT_LABELS, strict=True
    ):
        if result_label == "Center":
            continue
        result_transform = relative_result_transform_matrix(
            center, motion["result_offset"]
        )
        _draw_triad_3d(ax, result_transform, length=29.0)
    center_position = transforms["CalibrationCenter"][:3, 3]
    ax.text(
        center_position[0] + 10,
        center_position[1] + 8,
        center_position[2] + 18,
        "6 program-only relative\norientation results",
        color=ORIENTATION,
        fontsize=6.5,
        weight="bold",
    )

    all_points = np.vstack([corners, *[value[:3, 3] for value in transforms.values()]])
    _set_3d_equal(ax, all_points)
    ax.view_init(elev=23, azim=-48)
    ax.set_xlabel("TemplateBase X [mm]", labelpad=7)
    ax.set_ylabel("TemplateBase Y [mm]", labelpad=7)
    ax.set_zlabel("TemplateBase Z [mm]", labelpad=7)
    ax.set_title("Metric isometric — 9 taught flange frames", loc="left", weight="bold")
    ax.grid(alpha=0.25)


def _draw_raster(ax: Any, plan: Mapping[str, Any]) -> None:
    frames = frames_by_name(plan)
    coverage = plan["phases"][0]["motions"]
    ordered_names = [coverage[0]["to"]] + [motion["to"] for motion in coverage[1:-1]]
    positions = [seed_transform_matrix(frames[name])[:3, 3] for name in ordered_names]

    for index, (name, position) in enumerate(
        zip(ordered_names, positions, strict=True), 1
    ):
        ax.scatter(position[0], position[2], color=COVERAGE, s=45, zorder=3)
        ax.text(
            position[0],
            position[2],
            str(index),
            color="white",
            ha="center",
            va="center",
            fontsize=7,
            weight="bold",
            zorder=4,
        )
        ax.text(
            position[0] + 8,
            position[2] + 8,
            name.replace("Calibration", ""),
            fontsize=6,
        )
    for start, end in pairwise(positions):
        ax.annotate(
            "",
            xy=(end[0], end[2]),
            xytext=(start[0], start[2]),
            arrowprops={"arrowstyle": "-|>", "color": COVERAGE, "lw": 1.4},
        )

    center = seed_transform_matrix(frames["CalibrationCenter"])[:3, 3]
    for start, end in ((center, positions[0]), (positions[-1], center)):
        ax.annotate(
            "",
            xy=(end[0], end[2]),
            xytext=(start[0], start[2]),
            arrowprops={
                "arrowstyle": "-|>",
                "color": TRANSIT,
                "lw": 1.0,
                "linestyle": "--",
                "connectionstyle": "arc3,rad=0.12",
            },
        )
    ax.text(
        0.5,
        0.015,
        "Center → 1 [PTP]  ·  1→9 [8 LIN]  ·  9 → Center [PTP]",
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=6.2,
        color=TRANSIT,
    )
    ax.set_xlim(-205, 205)
    ax.set_ylim(320, 570)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("TemplateBase X [mm]")
    ax.set_ylabel("TemplateBase Z [mm]")
    ax.set_title("Raster view (X–Z) — numbered snake", loc="left", weight="bold")
    ax.grid(alpha=0.3)


def _draw_orientation(ax: Any, plan: Mapping[str, Any]) -> None:
    frames = frames_by_name(plan)
    center = frames["CalibrationCenter"]
    motions = plan["phases"][1]["motions"]
    node_positions = {
        "Center": np.array([0.0, 0.0]),
        "A−15°": np.array([-1.25, 1.0]),
        "A+15°": np.array([1.25, 1.0]),
        "B−12°": np.array([-1.25, 0.0]),
        "B+12°": np.array([1.25, 0.0]),
        "C−15°": np.array([-1.25, -1.0]),
        "C+15°": np.array([1.25, -1.0]),
    }
    ax.scatter([0], [0], color=COVERAGE, s=50, zorder=4)
    ax.text(
        0,
        -0.16,
        "taught CalibrationCenter",
        ha="center",
        va="top",
        fontsize=7,
        weight="bold",
    )

    for motion, result_label in zip(motions, RESULT_LABELS, strict=True):
        if result_label == "Center":
            continue
        transform = relative_result_transform_matrix(center, motion["result_offset"])
        rotation = transform[:3, :3]
        origin = node_positions[result_label]
        for axis_index, color in enumerate(AXIS_COLORS):
            projected = (
                np.array([rotation[0, axis_index], rotation[2, axis_index]]) * 0.28
            )
            ax.annotate(
                "",
                xy=origin + projected,
                xytext=origin,
                arrowprops={"arrowstyle": "-|>", "color": color, "lw": 1.0},
            )
        ax.scatter([origin[0]], [origin[1]], color=ORIENTATION, s=30, zorder=4)
        alignment = "right" if origin[0] < 0 else "left"
        label_x = origin[0] - 0.12 if origin[0] < 0 else origin[0] + 0.12
        ax.text(
            label_x,
            origin[1] + 0.1,
            result_label,
            ha=alignment,
            fontsize=7,
            weight="bold",
        )
        ax.text(
            label_x,
            origin[1] - 0.12,
            "program-only result",
            ha=alignment,
            va="top",
            fontsize=5.8,
            color=ORIENTATION,
        )

    current_label = "Center"
    for index, (motion, result_label) in enumerate(
        zip(motions, RESULT_LABELS, strict=True), 1
    ):
        start = node_positions[current_label]
        end = node_positions[result_label]
        ax.annotate(
            "",
            xy=end,
            xytext=start,
            arrowprops={
                "arrowstyle": "-|>",
                "color": ORIENTATION,
                "lw": 1.2,
                "shrinkA": 6,
                "shrinkB": 6,
            },
            zorder=2,
        )
        midpoint = (start + end) / 2.0
        normal = np.array([-(end - start)[1], (end - start)[0]])
        norm = float(np.linalg.norm(normal))
        if norm > 0:
            normal = normal / norm * 0.08
        ax.text(
            *(midpoint + normal),
            str(index),
            ha="center",
            va="center",
            fontsize=5.5,
            color=ORIENTATION,
            bbox={
                "boxstyle": "circle,pad=0.12",
                "facecolor": "white",
                "edgecolor": ORIENTATION,
                "lw": 0.5,
            },
            zorder=5,
        )
        current_label = result_label

    ax.set_xlim(-2.3, 2.3)
    ax.set_ylim(-1.72, 1.55)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    ax.set_title("Exploded center inset — 9 LIN_REL legs", loc="left", weight="bold")
    ax.text(
        0,
        1.5,
        "Zero XYZ deltas; RGB arrows are flange X/Y/Z axes. Positions are exploded, not metric.",
        ha="center",
        va="top",
        fontsize=6.3,
    )
    ax.text(
        0,
        -1.5,
        "Center→A−→A+→Center→B−→B+→Center→C−→C+→Center\n"
        "Program-relative orientation interpolation; every result/path remains uncommissioned",
        ha="center",
        va="top",
        fontsize=6.2,
        color=TRANSIT,
    )


def _draw_relative_contract(ax: Any, plan: Mapping[str, Any]) -> None:
    ax.axis("off")
    ax.set_title(
        "Program-only relative orientation contract", loc="left", weight="bold"
    )
    rows = []
    for index, (motion, result_label) in enumerate(
        zip(plan["phases"][1]["motions"], RESULT_LABELS, strict=True), 1
    ):
        delta = motion["delta"]
        rows.append(
            [
                str(index),
                f"({delta['A']:+g}, {delta['B']:+g}, {delta['C']:+g})°",
                result_label,
                motion["capture_label"],
            ]
        )
    table = ax.table(
        cellText=rows,
        colLabels=("Leg", "relative ΔA/ΔB/ΔC", "result", "capture label"),
        colWidths=(0.07, 0.25, 0.16, 0.52),
        cellLoc="left",
        colLoc="left",
        bbox=(0.0, 0.08, 1.0, 0.84),
    )
    table.auto_set_font_size(False)
    table.set_fontsize(5.7)
    for (row, _column), cell in table.get_celld().items():
        cell.set_edgecolor("#c7ccd2")
        cell.set_linewidth(0.5)
        if row == 0:
            cell.set_facecolor("#f0e4ef")
            cell.set_text_props(weight="bold", color=ORIENTATION)
    ax.text(
        0,
        0.0,
        "Reference: taught CalibrationCenter · motion: linRel(Transformation.ofDeg(...), CalibrationCenter)",
        fontsize=6.1,
        color=TRANSIT,
        transform=ax.transAxes,
    )


def _draw_cell_schematic(ax: Any) -> None:
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title("Ceiling-mounted cell context", loc="left", weight="bold")
    ax.text(
        5,
        9.65,
        "NON-METRIC SCHEMATIC",
        ha="center",
        color="#a33b2b",
        weight="bold",
        fontsize=8,
    )
    ax.add_patch(
        Rectangle((1.0, 8.5), 8.0, 0.55, facecolor="#9aa1aa", edgecolor="#32363d")
    )
    ax.text(5, 8.77, "ceiling plate", ha="center", va="center", fontsize=7)
    joints = np.array([[5.0, 8.5], [4.5, 7.4], [5.5, 6.5], [4.7, 5.4], [5.2, 4.5]])
    ax.plot(
        joints[:, 0], joints[:, 1], color="#6f7680", linewidth=7, solid_capstyle="round"
    )
    for x_value, y_value in joints:
        ax.add_patch(
            Circle((x_value, y_value), 0.2, facecolor="#c6cbd1", edgecolor="#32363d")
        )
    ax.text(6.15, 6.7, "hanging iiwa proxy", fontsize=7, rotation=-75)
    ax.add_patch(
        Rectangle((4.15, 3.9), 2.1, 0.6, facecolor="#343a43", edgecolor="#111318")
    )
    ax.text(6.45, 4.2, "flange + camera rig", va="center", fontsize=7)
    ax.add_patch(
        Polygon(
            [[2.6, 1.25], [7.4, 1.25], [6.8, 2.15], [3.2, 2.15]],
            closed=True,
            facecolor=TEMPLATE,
            edgecolor="#32363d",
        )
    )
    ax.text(
        5, 1.65, "420 × 297 mm HRI template below", ha="center", va="center", fontsize=7
    )
    ax.annotate(
        "",
        xy=(5, 2.25),
        xytext=(5, 3.85),
        arrowprops={"arrowstyle": "-[", "color": TRANSIT, "lw": 1.2},
    )


def _draw_key(ax: Any, plan: Mapping[str, Any]) -> None:
    ax.axis("off")
    ax.set_title(
        "Workbench teaching key — exactly 9 persistent frames",
        loc="left",
        weight="bold",
    )
    for index, frame in enumerate(plan["frames"]):
        column = index // 3
        row = index % 3
        x_value = 0.01 + column * 0.33
        y_value = 0.87 - row * 0.24
        seed = frame["seed"]
        detail = (
            f"XYZ=({seed['X']}, {seed['Y']}, {seed['Z']}) mm; "
            f"ABC=({seed['A']}, {seed['B']}, {seed['C']})°"
        )
        ax.text(
            x_value,
            y_value,
            frame["name"],
            color=COVERAGE,
            fontsize=6.5,
            weight="bold",
            transform=ax.transAxes,
        )
        ax.text(
            x_value,
            y_value - 0.055,
            detail,
            color="#454b53",
            fontsize=5.8,
            transform=ax.transAxes,
        )
    ax.text(
        0.01,
        0.13,
        "No CalibrationReady, depth, or orientation-variant Workbench frames. "
        "CalibrationCenter is the taught start/end anchor; program-relative motions still require full path validation.",
        color="#a33b2b",
        fontsize=7,
        weight="bold",
        transform=ax.transAxes,
    )


def build_figure(plan: Mapping[str, Any]) -> plt.Figure:
    matplotlib.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8,
            "svg.fonttype": "none",
            "svg.hashsalt": "posetestbot-iiwa-teaching-plan-v2",
        }
    )
    figure = plt.figure(figsize=(20, 14), layout="constrained", facecolor="white")
    grid = figure.add_gridspec(
        3,
        4,
        width_ratios=(1.25, 1.25, 1.05, 1.15),
        height_ratios=(1.0, 1.0, 0.88),
    )
    isometric = figure.add_subplot(grid[:2, :2], projection="3d")
    raster = figure.add_subplot(grid[0, 2])
    relative_contract = figure.add_subplot(grid[1, 2])
    orientation = figure.add_subplot(grid[:2, 3])
    schematic = figure.add_subplot(grid[2, 0])
    key = figure.add_subplot(grid[2, 1:])

    _draw_isometric(isometric, plan)
    _draw_raster(raster, plan)
    _draw_relative_contract(relative_contract, plan)
    _draw_orientation(orientation, plan)
    _draw_cell_schematic(schematic)
    _draw_key(key, plan)

    legend_handles = [
        Line2D(
            [0], [0], color=COVERAGE, lw=3, label="Taught coverage frames / LIN raster"
        ),
        Line2D(
            [0], [0], color=ORIENTATION, lw=3, label="Program-only LIN_REL orientation"
        ),
        Line2D(
            [0],
            [0],
            color=TRANSIT,
            lw=1.2,
            ls="--",
            label="PTP — sequence connector only",
        ),
        Line2D([0], [0], color="#d62728", lw=1.5, label="flange X"),
        Line2D([0], [0], color="#2ca02c", lw=1.5, label="flange Y"),
        Line2D([0], [0], color="#1f77b4", lw=1.5, label="flange Z"),
    ]
    figure.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.965),
        ncol=6,
        frameon=False,
        fontsize=8,
    )
    figure.suptitle(
        "PoseTestBot iiwa Calibration — 9 taught frames + program-relative orientation",
        fontsize=16,
        weight="bold",
        y=0.995,
    )
    figure.text(
        0.5,
        0.945,
        "Metric views use equal millimetre scales. CalibrationCenter anchors both phases. "
        "Dashed PTP connectors show order only: joint-space path not depicted.",
        ha="center",
        va="top",
        fontsize=8.5,
        weight="bold",
    )
    figure.text(
        0.5,
        0.008,
        "Teaching aid only—not reachability, redundancy, singularity, collision, or cable-clearance validation.",
        ha="center",
        va="bottom",
        color="#a33b2b",
        fontsize=10,
        weight="bold",
    )
    return figure


def render_teaching_plot(
    manifest_path: str | Path,
    svg_path: str | Path,
    png_path: str | Path | None = None,
) -> tuple[Path, Path | None]:
    plan = load_teaching_plan(manifest_path)
    svg_output = Path(svg_path)
    png_output = Path(png_path) if png_path is not None else None
    svg_output.parent.mkdir(parents=True, exist_ok=True)
    if png_output is not None:
        png_output.parent.mkdir(parents=True, exist_ok=True)
    figure = build_figure(plan)
    metadata = {
        "Title": "PoseTestBot iiwa nine-frame calibration teaching plan",
        "Date": None,
        "Description": (
            "Engineering teaching aid for nine persistent raster frames and "
            "program-relative orientation motions under /PoseTestBot/TemplateBase."
        ),
    }
    try:
        figure.savefig(svg_output, format="svg", dpi=160, metadata=metadata)
        if png_output is not None:
            figure.savefig(
                png_output,
                format="png",
                dpi=160,
                metadata={"Software": "PoseTestBot"},
            )
    finally:
        plt.close(figure)
    return svg_output, png_output


def _comparable_svg(value: str) -> str:
    """Normalize opaque Matplotlib IDs while preserving rendered SVG content."""

    identifiers = dict.fromkeys(MATPLOTLIB_SVG_ID.findall(value))
    for index, identifier in enumerate(identifiers, start=1):
        value = value.replace(identifier, f"matplotlib_auto_id_{index:04d}")
    return value


def teaching_plot_is_current(
    manifest_path: str | Path,
    svg_path: str | Path,
) -> bool:
    """Return whether the committed SVG matches a fresh deterministic render."""

    existing_path = Path(svg_path)
    try:
        existing = existing_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    with tempfile.TemporaryDirectory(prefix="posetestbot-teaching-plot-") as directory:
        generated_path = Path(directory) / "teaching.svg"
        render_teaching_plot(manifest_path, generated_path)
        generated = generated_path.read_text(encoding="utf-8")
    return _comparable_svg(existing) == _comparable_svg(generated)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        if not teaching_plot_is_current(args.manifest, args.svg):
            print(
                f"{args.svg} is stale; run "
                "`uv run python scripts/plot_iiwa_calibration_teaching_plan.py`."
            )
            return 1
        print(f"Teaching SVG is current: {args.svg}")
        return 0
    svg_path, png_path = render_teaching_plot(args.manifest, args.svg, args.png)
    print(f"Wrote {svg_path}")
    if png_path is not None:
        print(f"Wrote {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
