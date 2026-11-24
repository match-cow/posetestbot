from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
import shutil
import subprocess
import time
import zipfile

import cv2
import numpy as np
import pytest

from posetestbot.bop import depth_evaluation as depth
from posetestbot.bop import depth_sources
from posetestbot.bop.depth_metrics import (
    Aggregate,
    MetricSettings,
    measure,
    reference_bins,
    regions,
)
from posetestbot.bop.depth_sources import inventory, setup
from posetestbot.calibration.profiles import profile_to_dict
from posetestbot.io.atomic import atomic_write_json
from posetestbot.jobs.runner import LocalJobRunner
from posetestbot.sensors.contracts import CameraIntrinsics
from tests.test_bop_annotations import _publish_annotation_manifest
from tests.test_bop_evaluation import make_tiny_evaluation_run
from tests.test_calibration_profiles import static_profile


INSTANCE = "11111111-1111-4111-8111-111111111111"
CATALOG = "22222222-2222-4222-8222-222222222222"


def make_depth_run(
    tmp_path,
    *,
    name="run",
    scale=1.0,
    mode="pose",
    projection="native",
    distortion=(),
    frame_count=1,
):
    root = make_tiny_evaluation_run(tmp_path, name=name)
    scene = root / "bop/test/000001"
    if mode == "pose_and_masks":
        for kind in ("mask", "mask_visib"):
            (scene / kind).mkdir()
            cv2.imwrite(
                str(scene / kind / "000000_000000.png"), np.full((8, 8), 255, np.uint8)
            )
    _publish_annotation_manifest(root, scene, mode=mode)
    # Pose-only is intentionally independent of BOP19 targets and visibility.
    if mode == "pose":
        (scene / "scene_gt_info.json").unlink()
    (root / "bop/test_targets_bop19.json").unlink()
    width, height = 32, 24
    k = (40.0, 0.0, 15.5, 0.0, 40.0, 11.5, 0.0, 0.0, 1.0)
    native = CameraIntrinsics(
        cam_k=k,
        width=width,
        height=height,
        distortion=distortion,
        depth_scale_to_mm=scale,
    )
    profile = replace(
        static_profile(),
        profile_id="realsense_test_profile",
        intrinsics=native,
        rectified_intrinsics=replace(native, distortion=())
        if projection == "rectified"
        else None,
    )
    gt = json.loads((scene / "scene_gt.json").read_text())["0"]
    atomic_write_json(scene / "scene_gt.json", {str(i): gt for i in range(frame_count)})
    atomic_write_json(
        scene / "scene_camera.json",
        {str(i): {"cam_K": list(k), "depth_scale": scale} for i in range(frame_count)},
    )
    for i in range(frame_count):
        cv2.imwrite(
            str(scene / f"depth/{i:06d}.png"),
            np.full((height, width), round(500 / scale), np.uint16),
        )
        cv2.imwrite(
            str(scene / f"rgb/{i:06d}.png"), np.zeros((height, width, 3), np.uint8)
        )
    canonical = root / "processed/snapshots/model.ply"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "ply\nformat ascii 1.0\nelement vertex 4\nproperty float x\nproperty float y\nproperty float z\nelement face 2\nproperty list uchar int vertex_indices\nend_header\n-100 -80 0\n100 -80 0\n100 80 0\n-100 80 0\n3 0 2 1\n3 0 3 2\n"
    )
    canonical_hash = depth._sha256_file(canonical)
    info_path = root / "bop/models/models_info.json"
    info = json.loads(info_path.read_text())
    info["1"].update(
        {
            "source_path": "processed/snapshots/model.ply",
            "catalog_uuid": CATALOG,
            "posetestbot_geometry": {"source_sha256": canonical_hash},
        }
    )
    atomic_write_json(info_path, info)
    atomic_write_json(
        root / "object_instances.json",
        {
            "schema_version": "object_instances.v1",
            "instances": [
                {
                    "instance_uuid": INSTANCE,
                    "catalog_uuid": CATALOG,
                    "obj_id": 1,
                    "name": "plate",
                    "canonical_ply": "processed/snapshots/model.ply",
                    "canonical_ply_sha256": canonical_hash,
                }
            ],
        },
    )
    atomic_write_json(
        root / "bop/posetestbot_instance_map.json",
        {
            "schema_version": "posetestbot_bop_instance_map.v1",
            "instances": [
                {
                    "scene_id": 1,
                    "im_id": i,
                    "gt_id": 0,
                    "obj_id": 1,
                    "instance_uuid": INSTANCE,
                    "catalog_uuid": CATALOG,
                }
                for i in range(frame_count)
            ],
        },
    )
    atomic_write_json(
        root / "bop/posetestbot_bop_frame_map.json",
        {
            "schema_version": "posetestbot_bop_frame_map.v3",
            "scenes": {
                "1": {
                    "sensor_name": "realsense_test",
                    "scene_folder": "test/000001",
                    "split": "test",
                    "projection": projection,
                    "frames": {
                        str(i): {
                            "source_rgb": f"rgb/{i:06d}.png",
                            "source_depth": f"depth/{i:06d}.png",
                            "bop_rgb": f"rgb/{i:06d}.png",
                            "bop_depth": f"depth/{i:06d}.png",
                        }
                        for i in range(frame_count)
                    },
                }
            },
        },
    )
    provenance_path = scene / "posetestbot_gt_provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["resolution"] = {"width": width, "height": height}
    provenance["projection"] = projection
    provenance["frame_bindings"] = [
        {"output_image_id": i, "source_frame_id": i, "source_filename": f"{i:06d}.png"}
        for i in range(frame_count)
    ]
    atomic_write_json(provenance_path, provenance)
    manifest_path = root / "bop/bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["validation"]["status"] = "ok"
    export = manifest["exports"][0]
    export.update(projection=projection, rgb_count=frame_count, depth_count=frame_count)
    export["annotation_provenance"].update(
        sha256=depth._sha256_file(provenance_path),
        scene_gt_sha256=depth._sha256_file(scene / "scene_gt.json"),
        frame_binding_count=frame_count,
    )
    manifest["object_models"][0]["source_path"] = "processed/snapshots/model.ply"
    manifest["calibration_profiles"] = [profile_to_dict(profile)]
    atomic_write_json(manifest_path, manifest)
    return root


def submission(root, *, label="matte", settings=None, sensors=None):
    data = inventory(root)
    return {
        "schema_version": "depth_evaluation_submission.v1",
        "run_root": str(root),
        "finish_label": label,
        "geometry_hashes": [o["canonical_sha256"] for o in data["objects"]],
        "sensors": sensors or sorted({s["sensor_name"] for s in data["scenes"]}),
        "settings": settings or MetricSettings().model_dump(),
    }


def analytic_reference(root, data, frames, work):
    """Test-only reference provider; the production path always uses BlenderProc."""
    output = work / "reference"
    output.mkdir()
    for frame in frames:
        shape = frame["height"], frame["width"]
        ids = np.zeros(shape, np.int32)
        ids[3:-3, 3:-3] = 1
        normals = np.zeros((*shape, 3), np.float32)
        normals[ids > 0, 2] = -1
        reference = np.where(ids > 0, 500.0, 0.0)
        np.savez_compressed(
            output / f"{frame['scene_id']:06d}_{frame['im_id']:06d}.npz",
            depth_mm=reference,
            normals_cv=normals,
            instance_ids=ids,
        )
    atomic_write_json(
        output / "renderer.json",
        {"schema_version": "depth_renderer.v1", "test_only": True},
    )
    return {INSTANCE: 1}


def evaluate(root, **kwargs):
    request = depth.create_evaluation(root, submission(root, **kwargs))
    report = depth.run_evaluation(
        depth.folder(root, "evaluations", request["id"]) / "request.json",
        render_reference=analytic_reference,
    )
    return request, report


def test_exact_metrics_missing_offsets_outliers_and_empty_regions(tmp_path):
    settings = MetricSettings()
    reference = np.full((2, 3), 500.0)
    measured = np.array([[500, 502, 0], [490, np.nan, 520]], dtype=float)
    metrics, errors = measure(measured, reference, np.ones((2, 3), bool), settings)
    assert errors.tolist() == [0, 2, -10, 20]
    assert metrics["completeness"] == 4 / 6
    assert metrics["missing_pixels"] == 2
    assert metrics["bias_mm"] == 3
    assert metrics["mae_mm"] == 8
    assert metrics["rmse_mm"] == np.sqrt(126)
    assert metrics["median_absolute_error_mm"] == 6
    assert metrics["p95_absolute_error_mm"] == pytest.approx(18.5)
    assert metrics["thresholds"][0]["outlier_rate"] == 3 / 4
    assert metrics["thresholds"][-1]["usable_coverage"] == 3 / 6
    aggregate = Aggregate(tmp_path / "errors.f64", settings)
    aggregate.add(6, errors)
    aggregate.add(0, np.array([]))
    result = aggregate.result()
    assert {
        k: v
        for k, v in result.items()
        if k not in ("frame_count", "contributing_frame_count")
    } == metrics
    assert result["frame_count"] == 2 and result["contributing_frame_count"] == 1
    missing, _ = measure(
        np.zeros_like(reference), reference, np.ones((2, 3), bool), settings
    )
    assert missing["completeness"] == 0 and missing["mae_mm"] is None
    empty, _ = measure(measured, reference, np.zeros((2, 3), bool), settings)
    assert empty["status"] == "unavailable" and empty["completeness"] is None
    perfect, _ = measure(reference, reference, np.ones((2, 3), bool), settings)
    assert perfect["completeness"] == 1 and perfect["mae_mm"] == 0


def test_reference_regions_erosion_bins_and_measured_independence():
    ids = np.ones((10, 12), np.int32)
    result = regions(ids, 1)
    assert result["interior"].sum() == 6 * 8
    assert result["boundary"].sum() == 120 - 48
    k = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    normals = np.zeros((1, 2, 3))
    normals[..., 2] = -1
    d, a = reference_bins(np.full((1, 2), 500.0), normals, k, MetricSettings())
    assert d.tolist() == [[5, 7]]  # off-axis range differs from optical-axis Z
    assert a.tolist() == [[0, 4]]
    assert regions(ids, 99)["full"].sum() == 0


def test_pose_only_evaluation_scale_artifacts_history_and_immutable_download(tmp_path):
    root = make_depth_run(tmp_path, scale=0.25)
    assert setup(root)["ready"] is True
    request, report = evaluate(root)
    full = next(
        r for r in report["aggregates"] if r["scope"] == "run" and r["region"] == "full"
    )
    assert full["mae_mm"] == 0 and full["completeness"] == 1
    assert report["source_sha256"]["bop/test/000001/depth/000000.png"]
    assert report["geometry_hashes"] == submission(root)["geometry_hashes"]
    record_id = request["id"]
    assert depth.history(root, "evaluations")["records"][0]["state"] == "complete"
    page = depth.frames_page(root, "evaluations", record_id, limit=1)
    assert (
        page["total"] == 1
        and page["frames"][0]["instances"][0]["regions"]["interior"]["mae_mm"] == 0
    )
    bundle = depth.artifact(root, "evaluations", record_id, "research.zip")
    with zipfile.ZipFile(bundle) as archive:
        assert {
            "report.html",
            "metrics.csv",
            "metrics.png",
            "metrics.svg",
            "provenance.json",
            "settings.json",
            "frames.csv",
            "frames/000001_000000.npz",
        } <= set(archive.namelist())
        assert "fetch(" not in archive.read("report.html").decode()
    with np.load(
        depth.artifact(root, "evaluations", record_id, "frames/000001_000000.npz")
    ) as maps:
        assert maps["measured_mm"].max() == 500
    bundle.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash changed"):
        depth.artifact(root, "evaluations", record_id, "research.zip")


@pytest.mark.parametrize("mutation", ["missing_map", "tamper", "cancel", "symlink"])
def test_missing_maps_tampering_containment_and_atomic_cancellation(tmp_path, mutation):
    root = make_depth_run(tmp_path)
    image = root / "bop/test/000001/depth/000000.png"
    if mutation == "missing_map":
        image.unlink()
        request, report = evaluate(root)
        full = next(
            r
            for r in report["aggregates"]
            if r["scope"] == "run" and r["region"] == "full"
        )
        assert full["completeness"] == 0 and full["mae_mm"] is None
        return
    request = depth.create_evaluation(root, submission(root))
    path = depth.folder(root, "evaluations", request["id"])
    with pytest.raises(FileNotFoundError):
        depth.artifact(root, "evaluations", request["id"], "research.zip")
    if mutation == "symlink":
        image.unlink()
        image.symlink_to(tmp_path / "outside.png")

    def render(*args):
        result = analytic_reference(*args)
        if mutation == "cancel":
            raise depth.DepthCanceled("operator canceled")
        if mutation == "tamper":
            cv2.imwrite(str(image), np.ones((24, 32), np.uint16))
        return result

    with pytest.raises((ValueError, depth.DepthCanceled)):
        depth.run_evaluation(path / "request.json", render_reference=render)
    assert not (path / "complete").exists()
    assert depth.record_status(root, "evaluations", request["id"])["state"] == (
        "canceled" if mutation == "cancel" else "failed"
    )


def test_published_study_omits_renderer_intermediates_and_survives_a_damaged_peer(
    tmp_path, monkeypatch
):
    root = make_depth_run(tmp_path)
    request, _report = evaluate(root)
    complete = depth.folder(root, "evaluations", request["id"]) / "complete"

    # Reference maps live in frames/<key>.npz; the renderer's working copies
    # and its plan (absolute host paths) are not part of the publication.
    assert not (complete / "reference").exists()
    assert not (complete / "render_plan.json").exists()
    with zipfile.ZipFile(complete / "research.zip") as archive:
        assert not [
            name
            for name in archive.namelist()
            if name.startswith("reference/") or name == "render_plan.json"
        ]
    assert sorted(np.load(next((complete / "frames").glob("*.npz"))).files) == [
        "depth_mm",
        "instance_ids",
        "measured_mm",
        "normals_cv",
        "selected",
    ]

    # A record whose worker died before its first progress write is listed as
    # invalid; it must not hide the completed study beside it.
    damaged = depth.create_evaluation(root, submission(root, label="damaged"))
    (depth.folder(root, "evaluations", damaged["id"]) / "progress.json").unlink()
    records = {
        item["id"]: item for item in depth.history(root, "evaluations")["records"]
    }
    assert records[request["id"]]["state"] == "complete"
    assert records[damaged["id"]]["state"] == "invalid"
    assert records[damaged["id"]]["report_available"] is False

    # A probe that failed once is retried instead of being remembered.
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args[0], 5)
        return subprocess.CompletedProcess(args[0], 0, stdout="2.8.0\n")

    depth._version.cache_clear()
    monkeypatch.setattr(depth.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(depth.subprocess, "run", flaky)
    assert depth.renderer_status()["available"] is False
    assert depth.renderer_status()["available"] is True
    depth._version.cache_clear()


def test_comparison_common_bins_geometry_settings_owner_and_separate_sensors(tmp_path):
    a, b = make_depth_run(tmp_path, name="a"), make_depth_run(tmp_path, name="b")
    ar, _ = evaluate(a, label="matte")
    br, _ = evaluate(b, label="polished")
    value = {
        "schema_version": "depth_comparison_submission.v1",
        "run_root": str(a),
        "evaluations": [
            {"run_root": str(a), "evaluation_id": ar["id"]},
            {"run_root": str(b), "evaluation_id": br["id"]},
        ],
    }
    request = depth.create_comparison(a, value)
    report = depth.run_comparison(
        depth.folder(a, "comparisons", request["id"]) / "request.json"
    )
    assert len(report["series"]) == 2
    assert {s["finish_label"] for s in report["series"]} == {"matte", "polished"}
    assert report["bins"] and all(
        r["unmatched_reference_pixels"] == 0 for r in report["coverage"]
    )
    assert report["frame_count"] == 2
    c = make_depth_run(tmp_path, name="c")
    with pytest.raises(ValueError, match="one of"):
        depth.create_comparison(c, {**value, "run_root": str(c)})
    other, _ = evaluate(
        b,
        label="polished",
        settings={**MetricSettings().model_dump(), "distance_bin_mm": 200.0},
    )
    with pytest.raises(ValueError, match="settings"):
        depth.create_comparison(
            a,
            {
                **value,
                "evaluations": [
                    value["evaluations"][0],
                    {"run_root": str(b), "evaluation_id": other["id"]},
                ],
            },
        )
    canonical = c / "processed/snapshots/model.ply"
    canonical.write_text(canonical.read_text().replace("100 -80", "101 -80"))
    instances = json.loads((c / "object_instances.json").read_text())
    instances["instances"][0]["canonical_ply_sha256"] = depth._sha256_file(canonical)
    atomic_write_json(c / "object_instances.json", instances)
    info_path = c / "bop/models/models_info.json"
    info = json.loads(info_path.read_text())
    info["1"]["posetestbot_geometry"]["source_sha256"] = depth._sha256_file(canonical)
    atomic_write_json(info_path, info)
    cr, _ = evaluate(c)
    with pytest.raises(ValueError, match="canonical geometry"):
        depth.create_comparison(
            a,
            {
                **value,
                "evaluations": [
                    value["evaluations"][0],
                    {"run_root": str(c), "evaluation_id": cr["id"]},
                ],
            },
        )


def test_masks_export_missing_visibility_and_invisible_reference_are_supported(
    tmp_path,
):
    root = make_depth_run(tmp_path, mode="pose_and_masks")
    scene = root / "bop/test/000001"
    (scene / "scene_gt_info.json").unlink()
    shutil.rmtree(scene / "mask")
    shutil.rmtree(scene / "mask_visib")
    assert setup(root)["ready"]
    request = depth.create_evaluation(root, submission(root))

    def invisible(*args):
        ids = analytic_reference(*args)
        path = args[3] / "reference/000001_000000.npz"
        with np.load(path) as arrays:
            values = {k: np.zeros_like(v) for k, v in arrays.items()}
        np.savez_compressed(path, **values)
        return ids

    report = depth.run_evaluation(
        depth.folder(root, "evaluations", request["id"]) / "request.json",
        render_reference=invisible,
    )
    assert all(
        r["status"] == "unavailable"
        and r["reference_pixels"] == 0
        and r["mae_mm"] is None
        for r in report["aggregates"]
    )
    assert report["frame_count"] == 1


def test_final_publication_revalidates_sources_after_hashing_outputs(
    tmp_path, monkeypatch
):
    root = make_depth_run(tmp_path)
    request = depth.create_evaluation(root, submission(root))
    original = depth._manifest_files

    def tamper_during_publication(work):
        result = original(work)
        if (work / "research.zip").exists():
            cv2.imwrite(
                str(root / "bop/test/000001/depth/000000.png"),
                np.ones((24, 32), np.uint16),
            )
        return result

    monkeypatch.setattr(depth, "_manifest_files", tamper_during_publication)
    with pytest.raises(ValueError, match="source changed"):
        depth.run_evaluation(
            depth.folder(root, "evaluations", request["id"]) / "request.json",
            render_reference=analytic_reference,
        )
    with pytest.raises(FileNotFoundError):
        depth.artifact(root, "evaluations", request["id"], "research.zip")


def add_side_sensor(root, *, distortion=None):
    shutil.copytree(root / "bop/test/000001", root / "bop/test/000002")
    manifest = json.loads((root / "bop/bop_export_manifest.json").read_text())
    second = json.loads(json.dumps(manifest["exports"][0]))
    second.update(scene_id=2, sensor_name="side_sensor", scene_folder="test/000002")
    second["annotation_provenance"]["artifact"] = (
        "test/000002/posetestbot_gt_provenance.json"
    )
    if distortion is not None:
        profile = json.loads(json.dumps(manifest["calibration_profiles"][0]))
        profile["profile_id"] = "side_sensor_profile"
        profile["intrinsics"]["native"]["distortion"] = list(distortion)
        second["calibration_profile_id"] = profile["profile_id"]
        second["annotation_provenance"]["calibration_profile_id"] = profile[
            "profile_id"
        ]
        manifest["calibration_profiles"].append(profile)
    manifest["exports"].append(second)
    atomic_write_json(root / "bop/bop_export_manifest.json", manifest)
    mapping = json.loads((root / "bop/posetestbot_bop_frame_map.json").read_text())
    mapping["scenes"]["2"] = {
        **mapping["scenes"]["1"],
        "sensor_name": "side_sensor",
        "scene_folder": "test/000002",
    }
    atomic_write_json(root / "bop/posetestbot_bop_frame_map.json", mapping)
    instances = json.loads((root / "bop/posetestbot_instance_map.json").read_text())
    instances["instances"] += [{**row, "scene_id": 2} for row in instances["instances"]]
    atomic_write_json(root / "bop/posetestbot_instance_map.json", instances)


def test_sensor_breakdowns_and_same_run_sensor_comparison(tmp_path):
    root = make_depth_run(tmp_path)
    add_side_sensor(root)
    _, all_sensors = evaluate(root)
    sensor_rows = [
        r
        for r in all_sensors["aggregates"]
        if r["scope"] == "sensor" and r["region"] == "full"
    ]
    run_row = next(
        r
        for r in all_sensors["aggregates"]
        if r["scope"] == "run" and r["region"] == "full"
    )
    assert {r["sensor_name"] for r in sensor_rows} == {"realsense_test", "side_sensor"}
    assert run_row["reference_pixels"] == sum(
        r["reference_pixels"] for r in sensor_rows
    )
    assert run_row["frame_count"] == 2 and all(
        r["frame_count"] == 1 for r in sensor_rows
    )
    a, _ = evaluate(root, sensors=["realsense_test"])
    b, _ = evaluate(root, sensors=["side_sensor"])
    request = depth.create_comparison(
        root,
        {
            "schema_version": "depth_comparison_submission.v1",
            "run_root": str(root),
            "evaluations": [
                {"run_root": str(root), "evaluation_id": r["id"]} for r in (a, b)
            ],
        },
    )
    comparison = depth.run_comparison(
        depth.folder(root, "comparisons", request["id"]) / "request.json"
    )
    assert {s["sensor_name"] for s in comparison["series"]} == {
        "realsense_test",
        "side_sensor",
    }


def test_rectified_projection_uses_rectified_intrinsics_with_native_distortion(
    tmp_path,
):
    root = make_depth_run(tmp_path, projection="rectified", distortion=(0.1,))
    ready = setup(root)
    assert ready["ready"] and ready["sensors"][0]["unavailable_reason"] is None
    request, report = evaluate(root)
    assert report["camera_projections"] == request["camera_projections"]
    projection = report["camera_projections"][0]
    assert (
        projection["projection"] == "rectified"
        and projection["distortion"] == [0.0] * 5
    )


def test_distorted_native_sensors_are_excluded_at_setup_submission_and_execution(
    tmp_path,
):
    distorted = make_depth_run(tmp_path, name="distorted", distortion=(0.1,))
    blocked = setup(distorted)
    assert not blocked["ready"]
    assert "Use a rectified BOP export" in blocked["sensors"][0]["unavailable_reason"]

    root = make_depth_run(tmp_path, name="mixed")
    add_side_sensor(root, distortion=(0.1,))
    ready = setup(root)
    cameras = {c["sensor_name"]: c for c in ready["sensors"]}
    assert ready["ready"] and not ready["blockers"]
    assert cameras["realsense_test"]["unavailable_reason"] is None
    assert "Use a rectified BOP export" in cameras["side_sensor"]["unavailable_reason"]
    with pytest.raises(ValueError, match="side_sensor.*rectified BOP export"):
        depth.create_evaluation(root, submission(root))

    selected, report = evaluate(root, sensors=["realsense_test"])
    assert (
        report["frame_count"] == 1
        and report["camera_projections"][0]["projection"] == "native"
    )
    assert report["camera_projections"][0]["distortion"] == [0.0] * 5

    request = depth.create_evaluation(root, selected["submission"])
    request["submission"]["sensors"] = ["side_sensor"]
    path = depth.folder(root, "evaluations", request["id"])
    atomic_write_json(path / "request.json", request)

    def must_not_render(*_args):
        pytest.fail("An incompatible projection reached the renderer")

    with pytest.raises(ValueError, match="side_sensor.*rectified BOP export"):
        depth.run_evaluation(path / "request.json", render_reference=must_not_render)
    assert not (path / "complete").exists()


def test_legacy_projection_qualification_binds_the_frozen_export_manifest(tmp_path):
    old = make_depth_run(tmp_path, name="legacy")
    current = make_depth_run(tmp_path, name="current")
    request, report = evaluate(old)
    current_request, _ = evaluate(current, label="polished")
    # Recreate the publication format from before per-camera projection evidence.
    folder = depth.folder(old, "evaluations", request["id"])
    complete = folder / "complete"
    del request["camera_projections"], report["camera_projections"]
    atomic_write_json(folder / "request.json", request)
    manifest = json.loads((complete / "manifest.json").read_text())
    manifest["request_sha256"] = depth._sha256_file(folder / "request.json")

    def publish_legacy_report():
        atomic_write_json(complete / "report.json", report)
        manifest["files"]["report.json"] = depth._sha256_file(complete / "report.json")
        atomic_write_json(complete / "manifest.json", manifest)

    publish_legacy_report()
    (current / depth_sources.BOP_MANIFEST).unlink()
    value = {
        "schema_version": "depth_comparison_submission.v1",
        "run_root": str(old),
        "evaluations": [
            {"run_root": r["run_root"], "evaluation_id": r["id"]}
            for r in (request, current_request)
        ],
    }
    comparison = depth.create_comparison(old, value)
    assert comparison["inputs"][0]["camera_projections"][0]["distortion"] == [0.0] * 5

    source_path = old / depth_sources.BOP_MANIFEST
    changed = json.loads(source_path.read_text())
    changed["calibration_profiles"][0]["intrinsics"]["native"]["distortion"][0] = 0.1
    atomic_write_json(source_path, changed)
    with pytest.raises(ValueError, match="BOP export manifest hash changed"):
        depth.run_comparison(
            depth.folder(old, "comparisons", comparison["id"]) / "request.json"
        )
    with pytest.raises(ValueError, match="BOP export manifest hash changed"):
        depth.create_comparison(old, value)

    # A genuinely retained distorted study remains readable but cannot qualify.
    report["source_sha256"][depth_sources.BOP_MANIFEST] = depth._sha256_file(
        source_path
    )
    publish_legacy_report()
    with pytest.raises(ValueError, match="Use a rectified BOP export"):
        depth.create_comparison(old, value)
    assert depth.artifact(old, "evaluations", request["id"], "research.zip").is_file()
    assert depth.record_status(old, "evaluations", request["id"])["report_available"]


def test_frame_source_checks_are_linear_and_comparison_reuses_manifests(
    tmp_path, monkeypatch
):
    root = make_depth_run(tmp_path, frame_count=12)
    other = make_depth_run(tmp_path, name="single")
    single, single_report = evaluate(other, label="polished")
    other = other.rename(other.with_name("moved comparison input #?%"))
    request = depth.create_evaluation(root, submission(root))
    identity_counts = Counter()
    original_identity = depth_sources.identity

    def count_identity(base, relative):
        identity_counts[relative] += 1
        return original_identity(base, relative)

    monkeypatch.setattr(depth_sources, "identity", count_identity)
    report = depth.run_evaluation(
        depth.folder(root, "evaluations", request["id"]) / "request.json",
        render_reference=analytic_reference,
    )
    assert set(identity_counts) == {source["path"] for source in request["sources"]}
    assert max(identity_counts.values()) <= 4
    full = next(
        r for r in report["aggregates"] if r["scope"] == "run" and r["region"] == "full"
    )
    single_full = next(
        r
        for r in single_report["aggregates"]
        if r["scope"] == "run" and r["region"] == "full"
    )
    assert full["reference_pixels"] == 12 * single_full["reference_pixels"]
    for metric in (
        "mae_mm",
        "rmse_mm",
        "bias_mm",
        "completeness",
        "p95_absolute_error_mm",
    ):
        assert full[metric] == single_full[metric]
    comparison = depth.create_comparison(
        root,
        {
            "schema_version": "depth_comparison_submission.v1",
            "run_root": str(root),
            "evaluations": [
                {"run_root": str(root), "evaluation_id": request["id"]},
                {"run_root": str(other), "evaluation_id": single["id"]},
            ],
        },
    )
    completed_counts = Counter()
    get_publication = depth.completed

    def count_publication(base, kind, record_id, **kwargs):
        completed_counts[record_id] += 1
        return get_publication(base, kind, record_id, **kwargs)

    monkeypatch.setattr(depth, "completed", count_publication)
    result = depth.run_comparison(
        depth.folder(root, "comparisons", comparison["id"]) / "request.json"
    )
    assert result["frame_count"] == 13
    assert completed_counts == {request["id"]: 2, single["id"]: 2}
    assert result["bins"] and all(row["mae_mm"] == 0 for row in result["bins"])
    moved = root.rename(root.with_name("moved comparison #?%"))
    page = depth.frames_page(moved, "comparisons", comparison["id"], input_id="1")
    assert page["total"] == 1 and page["frames"][0]["finish_label"] == "polished"
    assert depth.artifact(
        moved, "comparisons", comparison["id"], "research.zip"
    ).is_file()


def test_absent_common_bins_comparison_tampering_and_cancellation(
    tmp_path, monkeypatch
):
    a, b = make_depth_run(tmp_path, name="a"), make_depth_run(tmp_path, name="b")
    ar, _ = evaluate(a)
    br = depth.create_evaluation(b, submission(b, label="polished"))

    def distant_reference(*args):
        ids = analytic_reference(*args)
        file = args[3] / "reference/000001_000000.npz"
        with np.load(file) as arrays:
            values = dict(arrays)
        values["depth_mm"] *= 2
        np.savez_compressed(file, **values)
        return ids

    depth.run_evaluation(
        depth.folder(b, "evaluations", br["id"]) / "request.json",
        render_reference=distant_reference,
    )
    value = {
        "schema_version": "depth_comparison_submission.v1",
        "run_root": str(a),
        "evaluations": [
            {"run_root": str(a), "evaluation_id": ar["id"]},
            {"run_root": str(b), "evaluation_id": br["id"]},
        ],
    }
    request = depth.create_comparison(a, value)
    report = depth.run_comparison(
        depth.folder(a, "comparisons", request["id"]) / "request.json"
    )
    assert report["bins"] == []
    assert all(
        r["status"] == "unavailable" and r["unmatched_reference_pixels"] > 0
        for r in report["coverage"]
    )
    canceled = depth.create_comparison(a, value)
    with monkeypatch.context() as patch:

        def cancel(*_args):
            raise depth.DepthCanceled("Canceled during report generation")

        patch.setattr(depth, "_tables_and_bundle", cancel)
        with pytest.raises(depth.DepthCanceled):
            depth.run_comparison(
                depth.folder(a, "comparisons", canceled["id"]) / "request.json"
            )
    assert depth.record_status(a, "comparisons", canceled["id"])["state"] == "canceled"
    with pytest.raises(FileNotFoundError):
        depth.artifact(a, "comparisons", canceled["id"], "research.zip")
    tampered = depth.create_comparison(a, value)
    depth.artifact(b, "evaluations", br["id"], "frames/000001_000000.npz").write_bytes(
        b"changed"
    )
    with pytest.raises(ValueError, match="hash changed"):
        depth.run_comparison(
            depth.folder(a, "comparisons", tampered["id"]) / "request.json"
        )


@pytest.mark.parametrize(
    "mutation", ["pose_hash", "calibration", "binding", "schema", "canonical_escape"]
)
def test_depth_readiness_fails_closed_on_contradictory_evidence(tmp_path, mutation):
    root = make_depth_run(tmp_path)
    if mutation == "pose_hash":
        (root / "bop/test/000001/scene_gt.json").write_text("{}")
    elif mutation == "calibration":
        camera = json.loads((root / "bop/test/000001/scene_camera.json").read_text())
        camera["0"]["cam_K"][0] = 50
        atomic_write_json(root / "bop/test/000001/scene_camera.json", camera)
    elif mutation == "binding":
        mapping = json.loads((root / "bop/posetestbot_bop_frame_map.json").read_text())
        mapping["scenes"]["1"]["frames"]["0"]["source_depth"] = "depth/000001.png"
        atomic_write_json(root / "bop/posetestbot_bop_frame_map.json", mapping)
    else:
        instances = json.loads((root / "object_instances.json").read_text())
        if mutation == "schema":
            instances["schema_version"] = "object_instances.v0"
        else:
            instances["instances"][0]["canonical_ply"] = "../../outside.ply"
        atomic_write_json(root / "object_instances.json", instances)
    assert not setup(root)["ready"]


def test_real_depth_job_cancellation_retains_diagnostics_and_no_downloads(tmp_path):
    if not depth.renderer_status()["available"]:
        pytest.skip("Pinned BlenderProc 2.8.0 is not installed")
    root = make_depth_run(tmp_path)
    request = depth.create_evaluation(root, submission(root))
    path = depth.folder(root, "evaluations", request["id"])
    runner = LocalJobRunner(tmp_path / "jobs")
    job = runner.submit(
        name="depth_evaluation",
        command=[
            "uv",
            "run",
            "python",
            "scripts/run_depth_evaluation.py",
            "--kind",
            "evaluations",
            "--request",
            str(path / "request.json"),
        ],
        cwd=depth.APP_ROOT,
        resources=["cpu", "disk_io"],
        scope_kind="run",
        run_root=root,
    )
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            state = json.loads((path / "progress.json").read_text())["state"]
            status = runner.get(job.id).status
            if state == "rendering" or status in {"failed", "succeeded", "canceled"}:
                break
            time.sleep(0.02)
        assert state == "rendering", runner.get(job.id).to_dict()
        runner.cancel(job.id)
        while time.monotonic() < deadline and runner.get(job.id).status not in {
            "failed",
            "succeeded",
            "canceled",
        }:
            time.sleep(0.02)
        assert runner.get(job.id).status == "canceled"
        assert json.loads((path / "progress.json").read_text())["state"] == "canceled"
        assert (path / ".work").is_dir()
        with pytest.raises(FileNotFoundError):
            depth.artifact(root, "evaluations", request["id"], "research.zip")
    finally:
        if runner.get(job.id).status in {"queued", "running", "canceling"}:
            runner.cancel(job.id)


def test_real_blenderproc_front_tilt_off_axis_occlusion_and_resolutions(tmp_path):
    if not depth.renderer_status()["available"]:
        pytest.skip("Pinned BlenderProc 2.8.0 is not installed")
    root = make_depth_run(tmp_path)
    data = inventory(root)
    base = data["frames"][0]
    near = {
        **base["gt"][0],
        "instance_uuid": "33333333-3333-4333-8333-333333333333",
        "cam_t_m2c": [0, 0, 450],
    }
    tilted = {
        **base["gt"][0],
        "cam_R_m2c": [
            np.cos(np.pi / 6),
            0,
            np.sin(np.pi / 6),
            0,
            1,
            0,
            -np.sin(np.pi / 6),
            0,
            np.cos(np.pi / 6),
        ],
    }
    frames = [
        base,
        {**base, "im_id": 1, "gt": [base["gt"][0], near]},
        {**base, "im_id": 2, "gt": [tilted]},
        {
            **base,
            "im_id": 3,
            "width": 64,
            "height": 48,
            "camera": {**base["camera"], "cam_K": [80, 0, 31.5, 0, 80, 23.5, 0, 0, 1]},
        },
        {
            **base,
            "im_id": 4,
            "gt": [
                {
                    **base["gt"][0],
                    "cam_R_m2c": [
                        1,
                        0,
                        0,
                        0,
                        np.cos(np.pi / 6),
                        -np.sin(np.pi / 6),
                        0,
                        np.sin(np.pi / 6),
                        np.cos(np.pi / 6),
                    ],
                }
            ],
        },
    ]
    try:
        ids = depth._render(root, data, frames, tmp_path)
    except subprocess.CalledProcessError:
        raise
    front, normals, mask = depth._validate_reference(
        tmp_path / "reference/000001_000000.npz", base, ids
    )
    assert np.allclose(front[mask > 0], 500, atol=0.02)  # Includes off-axis pixels.
    assert np.allclose(normals[mask > 0], [0, 0, -1], atol=0.002)
    nearer, _, masks = depth._validate_reference(
        tmp_path / "reference/000001_000001.npz", frames[1], ids
    )
    assert np.allclose(nearer[masks > 0], 450, atol=0.02)
    assert ids[INSTANCE] not in set(np.unique(masks))  # Occluded by known geometry.
    tilt, ntilt, mid = depth._validate_reference(
        tmp_path / "reference/000001_000002.npz", frames[2], ids
    )
    ys, xs = np.nonzero(mid > 0)
    expected = 500 / (1 + np.tan(np.pi / 6) * (xs - 15.5) / 40)
    assert np.allclose(tilt[ys, xs], expected, atol=0.05)
    assert np.allclose(ntilt[mid > 0], [-0.5, 0, -np.cos(np.pi / 6)], atol=0.002)
    high, _, hid = depth._validate_reference(
        tmp_path / "reference/000001_000003.npz", frames[3], ids
    )
    assert high.shape == (48, 64) and (hid > 0).sum() > (mask > 0).sum() * 3
    _, y_normals, y_ids = depth._validate_reference(
        tmp_path / "reference/000001_000004.npz", frames[4], ids
    )
    assert np.allclose(y_normals[y_ids > 0], [0, 0.5, -np.cos(np.pi / 6)], atol=0.002)
    # Real rendering must produce exactly the same reference eligibility even
    # when every measured return is changed or disappears.
    cv2.imwrite(str(root / base["depth"]), np.zeros((24, 32), np.uint16))
    request = depth.create_evaluation(root, submission(root))
    report = depth.run_evaluation(
        depth.folder(root, "evaluations", request["id"]) / "request.json"
    )
    assert report["renderer"]["blenderproc_version"] == "2.8.0"
    assert report["renderer"]["device"] == "CPU"
    full = next(
        r for r in report["aggregates"] if r["scope"] == "run" and r["region"] == "full"
    )
    assert full["completeness"] == 0 and full["mae_mm"] is None
    with np.load(
        depth.artifact(root, "evaluations", request["id"], "frames/000001_000000.npz")
    ) as values:
        again, again_normals, again_mask = (
            values["depth_mm"],
            values["normals_cv"],
            values["instance_ids"],
        )
    assert (
        np.array_equal(again, front)
        and np.array_equal(again_normals, normals)
        and np.array_equal(again_mask, mask)
    )
