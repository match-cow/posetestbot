from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from posetestbot.bop import consistency_comparison as compare, evaluation as ev
from tests.test_robot_consistency import (
    _hash,
    _inputs,
    _json,
    _result,
    make_robot_consistency_run,
)


def comparison_sources(tmp_path: Path, monkeypatch) -> list[tuple[str, Path, str]]:
    """Small completed evaluation fixtures; official scores are explicitly stubbed."""
    monkeypatch.setattr(ev, "toolkit_status", lambda _: {"available": True})
    original_run = ev.subprocess.run

    def official_fixture(command, **_kwargs):
        destination = (
            Path(command[command.index("--eval-path") + 1])
            / Path(command[command.index("--result-filename") + 1]).stem
            / "scores_bop19.json"
        )
        _json(
            destination,
            {
                "bop19_average_recall": 0.8,
                "bop19_average_recall_vsd": 0.7,
                "bop19_average_recall_mssd": 0.8,
                "bop19_average_recall_mspd": 0.9,
                "bop19_average_time_per_image": -1,
            },
        )
        run = Path(command[command.index("--datasets-path") + 1])
        gt = json.loads((run / "bop/test/000001/scene_gt.json").read_text())
        result_path = (
            Path(command[command.index("--results-path") + 1])
            / command[command.index("--result-filename") + 1]
        )
        errors = []
        for row in csv.DictReader(result_path.open()):
            im_id = int(row["im_id"])
            # Fixture predictions have the exact GT rotation, so official MSSD
            # is independently the norm of their known translation offset.
            distance = float(
                np.linalg.norm(
                    np.array([float(v) for v in row["t"].split()])
                    - gt[str(im_id)][0]["cam_t_m2c"]
                )
            )
            errors.append(
                {
                    "scene_id": 1,
                    "im_id": im_id,
                    "obj_id": 1,
                    "est_id": 0,
                    "errors": {"0": [distance]},
                }
            )
        _json(destination.parent / "error=mssd_ntop=-1/errors_000001.json", errors)

    monkeypatch.setattr(ev.subprocess, "run", official_fixture)
    sources = []
    for label, offsets in [("Biased", (25, 25)), ("Variable", (0, 4))]:
        run = make_robot_consistency_run(tmp_path / label.lower())
        _, _, inputs = _inputs(run)
        result = ev.import_bop_result(run, _result(run, inputs, offsets=offsets))
        request = ev.create_evaluation_request(run, result_id=result["result_id"])
        ev.run_evaluation_request(
            ev.evaluation_request_path(run, request["evaluation_id"]), app_root=tmp_path
        )
        sources.append((label, run, request["evaluation_id"]))
    monkeypatch.setattr(ev.subprocess, "run", original_run)
    return sources


def test_retained_constant_bias_vs_variable_error_and_bound_publication(
    tmp_path, monkeypatch
):
    sources = comparison_sources(tmp_path, monkeypatch)
    raw = sources[0][1] / "bop/test/000001/rgb/000000.png"
    before = _hash(raw)
    path = compare.create_comparison(
        sources, output_run=sources[0][1], title="Fixture comparison"
    )
    assert path.relative_to(sources[0][1]).parts[:3] == (
        "processed",
        "bop_evaluation",
        "comparisons",
    )
    report = json.loads(path.with_name("comparison.json").read_text())
    biased, variable = report["conditions"]
    for kind in ("mvd", "add"):
        assert biased["statistics"][kind]["gt_mean_all_mm"] == pytest.approx(25)
        assert biased["statistics"][kind]["rc_mean_mm"] == pytest.approx(0, abs=1e-10)
        assert biased["statistics"][kind]["threshold_counts"]["low_rc_high_gt"] == 2
        assert variable["statistics"][kind]["gt_mean_all_mm"] == pytest.approx(2)
        assert variable["statistics"][kind]["rc_mean_mm"] == pytest.approx(2)
    assert biased["tracks"][0]["gt_control"]["mvd_mm"] < 1e-10
    assert biased["tracks"][0]["official_mssd_check"]["frames"] == 2
    assert biased["tracks"][0]["official_mssd_check"]["max_difference_mm"] < 1e-10
    assert report["overview"]["ranking_agrees"] is False
    assert report["overview"]["low_rc_high_gt"] == 2
    assert biased["tracks"][0]["mean_reference_bias"]["mvd_mm"] == pytest.approx(25)
    assert report["paired_geometry"][0]["gt_vertex_difference_max_mm"] == pytest.approx(
        0
    )
    assert _hash(raw) == before
    request = json.loads(path.with_name("request.json").read_text())
    assert (
        request["sources"][0]["source_sha256"][
            raw.relative_to(sources[0][1]).as_posix()
        ]
        == before
    )
    assert path.with_name("request.json").stat().st_mode & 0o222 == 0
    manifest = json.loads(path.with_name("manifest.json").read_text())
    for name, sha in manifest["artifacts_sha256"].items():
        assert _hash(path.with_name(name)) == sha
    rows = list(csv.DictReader(path.with_name("frames.csv").open()))
    assert len(rows) == 4 and {r["condition_label"] for r in rows} == {
        "Biased",
        "Variable",
    }
    assert report["examples"] and all(
        e["rgb"].startswith("data:image/jpeg;base64,") for e in report["examples"]
    )


def test_rejected_and_missing_estimates_never_become_zero_consistency():
    rows = [
        {
            "condition": 0,
            "scene_id": 1,
            "instance_uuid": "a",
            "gt_mvd_mm": gt,
            "rc_mvd_mm": rc,
        }
        for gt, rc in [(2, 2), (25, 0), (250, None), (None, None)]
    ]
    summary = compare.summarize(rows, "mvd")
    assert (
        summary["eligible"] == 4
        and summary["predicted"] == 3
        and summary["matched"] == 2
    )
    assert summary["excluded"] == 1 and summary["missing"] == 1
    assert summary["gt_mean_all_mm"] == pytest.approx(277 / 3)
    assert summary["gt_mean_matched_mm"] == pytest.approx(13.5)
    assert summary["threshold_counts"]["low_rc_high_gt"] == 1
    assert compare.correlation([1, 1, 1], [1, 2, 3]) == {
        "pearson": None,
        "spearman": None,
    }


@pytest.mark.parametrize("reference_frame", [None, 0])
def test_shared_examples_keep_the_same_frame_when_an_estimate_is_missing(
    tmp_path, monkeypatch, reference_frame
):
    sources = comparison_sources(tmp_path, monkeypatch)
    loaded = [
        compare._load_source(*source, index) for index, source in enumerate(sources)
    ]
    conditions, internals = map(list, zip(*loaded, strict=True))
    missing = conditions[1]["rows"][0]
    missing.update(
        status="missing",
        gt_mvd_mm=None,
        gt_add_mm=None,
        rc_mvd_mm=None,
        rc_add_mm=None,
        translation_error_mm=None,
        rotation_error_deg=None,
    )
    reference, truth, _ = internals[1]["poses"][(1, 0)]
    internals[1]["poses"][(1, 0)] = (reference, truth, None)

    examples = compare._examples(conditions, internals, reference_frame)
    shared = [row for row in examples if "Shared trajectory view" in row["titles"]]
    assert [row["row"]["im_id"] for row in shared] == [0, 0]
    assert shared[1]["row"]["status"] == "missing"
    assert shared[1]["row"]["rc_mvd_mm"] is None
    assert all(row["overlay"].startswith("data:image/jpeg;base64,") for row in shared)


def test_explicit_shared_example_must_exist_in_every_condition(tmp_path, monkeypatch):
    sources = comparison_sources(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Reference frame.*every condition"):
        compare.create_comparison(sources, output_run=sources[0][1], reference_frame=99)
    assert not (sources[0][1] / "processed/bop_evaluation/comparisons").exists()


@pytest.mark.parametrize(
    "changed", ["robot_report", "robot_source", "csv", "depth", "official_mssd"]
)
def test_changed_evaluation_evidence_is_rejected_before_publication(
    tmp_path, monkeypatch, changed
):
    sources = comparison_sources(tmp_path, monkeypatch)
    _, root, identifier = sources[0]
    folder = ev.evaluation_report_path(root, identifier).parent
    if changed == "robot_report":
        path = folder / "robot_consistency.json"
    elif changed == "robot_source":
        path = (
            root / "processed/synchronized/realsense_fixture/match_robot_ee_poses.json"
        )
    elif changed == "csv":
        request = json.loads((folder / "request.json").read_text())
        path = ev.result_file_path(root, request["result_id"])
    elif changed == "depth":
        path = root / "bop/test/000001/depth/000000.png"
    else:
        report = json.loads((folder / "report.json").read_text())
        path = (
            root / report["provenance"]["official_scores_path"]
        ).parent / "error=mssd_ntop=-1/errors_000001.json"
    path.chmod(0o644)
    if changed == "official_mssd":
        values = json.loads(path.read_text())
        values[0]["errors"]["0"][0] += 1
        _json(path, values)
    else:
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        compare.create_comparison(sources, output_run=root)
    assert not (root / "processed/bop_evaluation/comparisons").exists()


def test_output_containment_and_safe_embedded_html(tmp_path):
    with pytest.raises(ValueError, match="output must belong"):
        compare.create_comparison(
            [
                ("A", tmp_path / "a", "evaluation-000000000000"),
                ("B", tmp_path / "b", "evaluation-111111111111"),
            ],
            output_run=tmp_path / "raw",
        )
    from posetestbot.bop.consistency_comparison_html import render_html

    document = render_html(
        {"title": "<unsafe>", "label": "</script><script>alert(1)</script>"}
    )
    assert "<title>&lt;unsafe&gt;" in document
    assert "</script><script>alert(1)</script>" not in document
    assert "\\u003c/script\\u003e" in document


def test_gt_vertex_error_uses_rotation_and_every_vertex():
    pose, truth = np.eye(4), np.eye(4)
    pose[:3, :3] = np.diag([-1, -1, 1])
    vertices = np.array([[0, 0, 0], [10, 0, 0], [0, 20, 0]])
    values = compare.vertex_errors(pose, truth, vertices, {})
    assert values == {"mvd_mm": 40, "add_mm": 20}
