from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from posetestbot.calibration.teaching_plan import load_teaching_plan


ROOT = Path(__file__).resolve().parents[1]
COMMITTED_SVG = ROOT / "docs" / "images" / "iiwa_calibration_teaching_plan.svg"


def test_headless_teaching_plot_contains_metric_contract_and_all_labels(
    tmp_path: Path,
) -> None:
    svg_path = tmp_path / "teaching.svg"
    png_path = tmp_path / "teaching.png"
    environment = os.environ.copy()
    environment["MPLCONFIGDIR"] = str(tmp_path / "matplotlib")
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/plot_iiwa_calibration_teaching_plan.py",
            "--svg",
            str(svg_path),
            "--png",
            str(png_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert svg_path.stat().st_size > 100_000
    assert png_path.stat().st_size > 100_000
    assert png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    second_svg = tmp_path / "teaching-second.svg"
    second = subprocess.run(
        [
            sys.executable,
            "scripts/plot_iiwa_calibration_teaching_plan.py",
            "--svg",
            str(second_svg),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second.returncode == 0, second.stderr
    assert second_svg.read_bytes() == svg_path.read_bytes()
    assert second.stdout.strip() == f"Wrote {second_svg}"

    svg = svg_path.read_text()
    plan = load_teaching_plan()
    assert "420 × 297 mm" in svg
    assert "Metric views use equal millimetre scales" in svg
    assert "CalibrationCenter anchors both phases" in svg
    assert "9 taught frames + program-relative orientation" in svg
    assert "Taught coverage frames / LIN raster" in svg
    assert "Program-only LIN_REL orientation" in svg
    assert "joint-space path not depicted" in svg
    assert "Center→A−→A+→Center→B−→B+→Center→C−→C+→Center" in svg
    assert (
        "Teaching aid only—not reachability, redundancy, singularity, collision, or cable-clearance validation."
        in svg
    )
    assert "NON-METRIC SCHEMATIC" in svg
    assert "RGB arrows are flange X/Y/Z axes" in svg
    assert "camera optical axis" not in svg
    assert "CalibrationDepth" not in svg
    for frame in plan["frames"]:
        assert frame["name"] in svg
    for motion in plan["phases"][1]["motions"]:
        assert motion["capture_label"] in svg


def test_committed_teaching_plot_is_current_and_check_does_not_rewrite(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["MPLCONFIGDIR"] = str(tmp_path / "matplotlib-check")
    current = subprocess.run(
        [
            sys.executable,
            "scripts/plot_iiwa_calibration_teaching_plan.py",
            "--check",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert current.returncode == 0, current.stdout + current.stderr
    assert current.stdout.strip() == f"Teaching SVG is current: {COMMITTED_SVG}"

    stale_svg = tmp_path / "stale.svg"
    stale_content = COMMITTED_SVG.read_text(encoding="utf-8").replace(
        "Metric views use equal millimetre scales",
        "Stale metric-view text",
        1,
    )
    stale_svg.write_text(stale_content, encoding="utf-8")
    stale = subprocess.run(
        [
            sys.executable,
            "scripts/plot_iiwa_calibration_teaching_plan.py",
            "--check",
            "--svg",
            str(stale_svg),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert stale.returncode == 1
    assert "is stale" in stale.stdout
    assert stale_svg.read_text(encoding="utf-8") == stale_content


def test_plot_geometry_uses_exact_template_and_equal_metric_axes(
    tmp_path: Path,
) -> None:
    os.environ["MPLCONFIGDIR"] = str(tmp_path / "matplotlib-geometry")
    namespace = runpy.run_path("scripts/plot_iiwa_calibration_teaching_plan.py")
    assert namespace["parse_args"](["--svg", str(tmp_path / "only.svg")]).png is None
    plan = load_teaching_plan()

    corners = namespace["_template_corners"](plan)
    assert np.ptp(corners[:, 0]) == pytest.approx(420.0)
    assert np.ptp(corners[:, 1]) == pytest.approx(297.0)
    assert np.ptp(corners[:, 2]) == pytest.approx(0.0)

    figure = namespace["build_figure"](plan)
    isometric, raster = figure.axes[:2]
    isometric_ranges = [
        np.ptp(isometric.get_xlim3d()),
        np.ptp(isometric.get_ylim3d()),
        np.ptp(isometric.get_zlim3d()),
    ]
    assert isometric_ranges[0] == pytest.approx(isometric_ranges[1])
    assert isometric_ranges[1] == pytest.approx(isometric_ranges[2])
    assert raster.get_aspect() in {1, 1.0, "equal"}
    namespace["plt"].close(figure)
