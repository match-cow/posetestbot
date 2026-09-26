from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from posetestbot.bop.evaluation import import_bop_result, inspect_dataset
from posetestbot.bop.inspection import (
    inspection_depth_png,
    inspection_frame,
    inspection_image_path,
    inspection_mask_path,
    inspection_model_path,
    inspection_setup,
    list_inspection_frames,
    project_bop_points,
)
from posetestbot.web.app import create_app
from posetestbot.web.runtime import WebSettings
from tests.test_bop_evaluation import (
    GT_T,
    IDENTITY_R,
    make_tiny_evaluation_run,
    write_result_csv,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def make_inspection_run(tmp_path: Path, *, two_frames: bool = False) -> Path:
    run = make_tiny_evaluation_run(tmp_path, name="pose-inspection")
    bop = run / "bop"
    scene = bop / "test" / "000001"
    for folder in ("mask", "mask_visib"):
        (scene / folder).mkdir()
        assert cv2.imwrite(
            (scene / folder / "000000_000000.png").as_posix(),
            np.pad(np.full((4, 4), 255, dtype=np.uint8), 2),
        )
    manifest_path = bop / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    export = manifest["exports"][0]
    export["sensor_name"] = "realsense_fixture"
    export["artifacts"].update(
        {
            "mask": "test/000001/mask",
            "mask_visib": "test/000001/mask_visib",
        }
    )
    manifest["capabilities"].update(
        {"gt_masks_full": True, "gt_masks_visible": True}
    )
    manifest["validation"]["capabilities"].update(
        {"gt_masks_full": True, "gt_masks_visible": True}
    )
    instances = [
        {
            "scene_id": 1,
            "im_id": 0,
            "gt_id": 0,
            "obj_id": 1,
            "instance_uuid": "11111111-1111-4111-8111-111111111111",
            "catalog_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
    ]
    if two_frames:
        for kind in ("rgb", "depth"):
            image = cv2.imread(
                (scene / kind / "000000.png").as_posix(), cv2.IMREAD_UNCHANGED
            )
            assert image is not None
            assert cv2.imwrite((scene / kind / "000001.png").as_posix(), image)
        for folder in ("mask", "mask_visib"):
            mask = cv2.imread(
                (scene / folder / "000000_000000.png").as_posix(),
                cv2.IMREAD_UNCHANGED,
            )
            assert mask is not None
            assert cv2.imwrite(
                (scene / folder / "000001_000000.png").as_posix(), mask
            )
        for filename in ("scene_camera.json", "scene_gt.json", "scene_gt_info.json"):
            path = scene / filename
            value = json.loads(path.read_text())
            value["1"] = json.loads(json.dumps(value["0"]))
            _write_json(path, value)
        targets_path = bop / "test_targets_bop19.json"
        targets = json.loads(targets_path.read_text())
        targets.append({"scene_id": 1, "im_id": 1, "obj_id": 1, "inst_count": 1})
        _write_json(targets_path, targets)
        export["rgb_count"] = 2
        export["depth_count"] = 2
        manifest["validation"].update(
            {"frame_count": 2, "annotation_count": 2, "target_count": 2}
        )
        instances.append(
            {
                **instances[0],
                "im_id": 1,
            }
        )
    _write_json(manifest_path, manifest)
    _write_json(
        bop / "posetestbot_instance_map.json",
        {
            "schema_version": "posetestbot_bop_instance_map.v1",
            "instances": instances,
        },
    )
    _write_json(
        run / "run_config.json",
        {
            "schema_version": "run_config.v4",
            "capture": {
                "sensors": [
                    {
                        "enabled": True,
                        "sensor_type": "realsense_d435",
                        "device_id": "fixture",
                        "operator_alias": "Center",
                        "display_name": "Center RGB-D",
                        "mounting_mode": "eye_in_hand",
                    }
                ]
            },
        },
    )
    return run


def _import_result(run: Path, tmp_path: Path, *, rows: list[dict] | None = None) -> dict:
    dataset = inspect_dataset(run)
    source = write_result_csv(
        tmp_path / f"method_{dataset['dataset_alias']}-test.csv", rows=rows
    )
    return import_bop_result(run, source, method_name="Inspection method")


def test_inspection_setup_frames_evidence_and_id_addressed_media(tmp_path: Path) -> None:
    run = make_inspection_run(tmp_path, two_frames=True)
    result = _import_result(
        run,
        tmp_path,
        rows=[
            {
                "scene_id": 1,
                "im_id": 0,
                "obj_id": 1,
                "score": 0.75,
                "R": IDENTITY_R,
                "t": GT_T,
                "time": 0.25,
            }
        ],
    )

    setup = inspection_setup(run, result_id=result["result_id"])
    all_frames = list_inspection_frames(
        run, result_id=result["result_id"], scene_id=1, page_size=1
    )
    second_page = list_inspection_frames(
        run, result_id=result["result_id"], scene_id=1, page=2, page_size=1
    )
    missing = list_inspection_frames(
        run,
        result_id=result["result_id"],
        scene_id=1,
        frame_filter="missing_estimate",
    )
    frame = inspection_frame(
        run, result_id=result["result_id"], scene_id=1, im_id=0
    )

    assert setup["schema_version"] == "bop_inspection_setup.v1"
    assert setup["ready"] is True
    assert setup["scenes"][0]["display_name"] == "Center"
    assert "frame_ids" not in setup["scenes"][0]
    assert setup["scenes"][0]["physical_identity"]["device_id"] == "fixture"
    assert setup["scenes"][0]["capabilities"] == {
        "rgb": True,
        "depth": True,
        "ground_truth": True,
        "full_mask": True,
        "visible_mask": True,
        "execution_operations": False,
    }
    assert [item["im_id"] for item in all_frames["frames"]] == [0]
    assert all_frames["frames"][0]["ordinal"] == 0
    assert all_frames["frames"][0]["previous_im_id"] is None
    assert all_frames["frames"][0]["next_im_id"] == 1
    assert all_frames["next_page"] == 2
    assert [item["im_id"] for item in second_page["frames"]] == [1]
    assert [item["im_id"] for item in missing["frames"]] == [1]
    assert frame["camera"]["image_size"] == [8, 8]
    assert frame["estimates"][0]["score"] == 0.75
    assert frame["estimates"][0]["time_seconds"] == 0.25
    assert frame["ground_truth"][0]["visibility"]["bbox_visib"] == [1.0, 1.0, 5.0, 5.0]
    assert frame["associations"] == [
        {
            "obj_id": 1,
            "status": "unambiguous",
            "estimate_rank": 1,
            "gt_id": 0,
            "delta": {
                "translation_mm": 0.0,
                "rotation_deg": 0.0,
                "rotation_contract": "symmetry_unaware",
            },
        }
    ]
    assert frame["execution"]["known"] is False
    assert frame["ground_truth"][0]["operations"] == ["unknown"]
    assert inspection_image_path(
        run,
        result_id=result["result_id"],
        scene_id=1,
        im_id=0,
        kind="rgb",
    ).name == "000000.png"
    assert inspection_mask_path(
        run,
        result_id=result["result_id"],
        scene_id=1,
        im_id=0,
        gt_id=0,
        kind="visible",
    ).name == "000000_000000.png"
    assert inspection_model_path(
        run, result_id=result["result_id"], obj_id=1
    ).name == "obj_000001.ply"
    assert inspection_depth_png(
        run, result_id=result["result_id"], scene_id=1, im_id=0
    ).startswith(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ValueError, match="page_size"):
        list_inspection_frames(
            run,
            result_id=result["result_id"],
            scene_id=1,
            page_size=201,
        )
    with pytest.raises(KeyError, match="Unknown BOP result"):
        inspection_setup(run, result_id="result-ffffffffffff")


def test_repeated_identical_objects_remain_explicitly_ambiguous(tmp_path: Path) -> None:
    run = make_inspection_run(tmp_path)
    scene = run / "bop" / "test" / "000001"
    gt_path = scene / "scene_gt.json"
    info_path = scene / "scene_gt_info.json"
    gt = json.loads(gt_path.read_text())
    info = json.loads(info_path.read_text())
    gt["0"].append(json.loads(json.dumps(gt["0"][0])))
    info["0"].append(json.loads(json.dumps(info["0"][0])))
    _write_json(gt_path, gt)
    _write_json(info_path, info)
    targets_path = run / "bop" / "test_targets_bop19.json"
    targets = json.loads(targets_path.read_text())
    targets[0]["inst_count"] = 2
    _write_json(targets_path, targets)
    manifest_path = run / "bop" / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["validation"].update({"annotation_count": 2, "target_count": 2})
    _write_json(manifest_path, manifest)
    result = _import_result(run, tmp_path)

    frame = inspection_frame(
        run, result_id=result["result_id"], scene_id=1, im_id=0
    )

    [association] = frame["associations"]
    assert association["status"] == "ambiguous_repeated_object"
    assert association["delta"] is None
    assert "instance pairing" in association["reason"]
    assert len(frame["ground_truth"]) == 2
    missing = list_inspection_frames(
        run,
        result_id=result["result_id"],
        scene_id=1,
        frame_filter="missing_estimate",
    )
    assert missing["frames"][0]["missing_target_instance_count"] == 1


def test_inspection_caps_ground_truth_rows_in_one_frame(tmp_path: Path) -> None:
    run = make_inspection_run(tmp_path)
    scene = run / "bop" / "test" / "000001"
    gt_path = scene / "scene_gt.json"
    info_path = scene / "scene_gt_info.json"
    gt = json.loads(gt_path.read_text())
    info = json.loads(info_path.read_text())
    gt["0"] = [json.loads(json.dumps(gt["0"][0])) for _ in range(1_001)]
    info["0"] = [json.loads(json.dumps(info["0"][0])) for _ in range(1_001)]
    _write_json(gt_path, gt)
    _write_json(info_path, info)
    targets_path = run / "bop" / "test_targets_bop19.json"
    targets = json.loads(targets_path.read_text())
    targets[0]["inst_count"] = 1_001
    _write_json(targets_path, targets)
    manifest_path = run / "bop" / "bop_export_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["validation"].update(
        {"annotation_count": 1_001, "target_count": 1_001}
    )
    _write_json(manifest_path, manifest)
    result = _import_result(run, tmp_path)

    with pytest.raises(ValueError, match="1000-instance response cap"):
        inspection_setup(run, result_id=result["result_id"])


def test_projection_matches_known_bop_visibility_bounds() -> None:
    corners = [
        [x, y, z]
        for x in (-10.0, 10.0)
        for y in (-10.0, 10.0)
        for z in (-10.0, 10.0)
    ]
    projected = project_bop_points(
        corners,
        cam_k=[100.0, 0.0, 4.0, 0.0, 100.0, 4.0, 0.0, 0.0, 1.0],
        rotation=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        translation_mm=[0.0, 0.0, 500.0],
    )
    xs = [item[0] for item in projected]
    ys = [item[1] for item in projected]
    projected_bounds = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
    scene_gt_info_bbox = [1.0, 1.0, 5.0, 5.0]

    assert projected_bounds == pytest.approx(
        scene_gt_info_bbox, abs=1.05
    )


def test_inspection_media_rejects_symlink_escape_after_setup(
    tmp_path: Path,
) -> None:
    run = make_inspection_run(tmp_path)
    result = _import_result(run, tmp_path)
    assert inspection_setup(run, result_id=result["result_id"])["ready"] is True
    rgb = run / "bop" / "test" / "000001" / "rgb" / "000000.png"
    outside = tmp_path / "outside.png"
    outside.write_bytes(rgb.read_bytes())
    rgb.unlink()
    rgb.symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe|symbolic"):
        inspection_image_path(
            run,
            result_id=result["result_id"],
            scene_id=1,
            im_id=0,
            kind="rgb",
        )


def test_inspection_rehashes_result_after_same_size_and_mtime_tampering(
    tmp_path: Path,
) -> None:
    run = make_inspection_run(tmp_path)
    result = _import_result(
        run,
        tmp_path,
        rows=[
            {
                "scene_id": 1,
                "im_id": 0,
                "obj_id": 1,
                "score": 0.75,
                "R": IDENTITY_R,
                "t": GT_T,
                "time": 0.25,
            }
        ],
    )
    assert inspection_setup(run, result_id=result["result_id"])["ready"] is True
    stored = (
        run
        / "processed"
        / "bop_evaluation"
        / "results"
        / result["result_id"]
        / result["filename"]
    )
    metadata = stored.stat()
    content = stored.read_bytes()
    assert b",0.75," in content
    stored.chmod(0o600)
    stored.write_bytes(content.replace(b",0.75,", b",0.74,", 1))
    os.utime(stored, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    with pytest.raises(ValueError, match="content hash"):
        inspection_frame(
            run,
            result_id=result["result_id"],
            scene_id=1,
            im_id=0,
        )


def test_inspection_http_contract_uses_only_ids_for_media(
    tmp_path: Path, monkeypatch
) -> None:
    runs_root = tmp_path / "runs"
    run = make_inspection_run(runs_root)
    result = _import_result(run, tmp_path)
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    settings = WebSettings(
        host="127.0.0.1",
        port=5000,
        debug=False,
        job_root=tmp_path / "jobs",
    )
    client = create_app(settings=settings).test_client()

    setup = client.get(
        "/bop/inspection/setup",
        query_string={"run_root": run.as_posix(), "result_id": result["result_id"]},
    )
    frame = client.get(
        "/bop/inspection/frame",
        query_string={
            "run_root": run.as_posix(),
            "result_id": result["result_id"],
            "scene_id": 1,
            "im_id": 0,
        },
    )

    assert setup.status_code == 200
    assert frame.status_code == 200
    payload = frame.get_json()
    assert "/bop/inspection/media/" in payload["media"]["rgb_url"]
    assert "path=" not in payload["media"]["rgb_url"]
    assert client.get(payload["media"]["rgb_url"]).status_code == 200
    assert client.get(payload["media"]["depth_url"]).status_code == 200
    assert client.get(payload["ground_truth"][0]["mask_urls"]["full"]).status_code == 200
    assert client.get(payload["model_urls"]["1"]).status_code == 200


def test_inspection_result_location_recovers_legacy_direct_link_without_crossing_runs(
    tmp_path: Path, monkeypatch
) -> None:
    runs_root = tmp_path / "runs"
    run = make_inspection_run(runs_root)
    result = _import_result(run, tmp_path)
    other_run = runs_root / "other-run"
    other_run.mkdir()
    _write_json(other_run / "run_config.json", {"schema_version": "run_config.v4"})
    monkeypatch.setenv("POSETESTBOT_WEB_RUN_ROOTS", runs_root.as_posix())
    client = create_app().test_client()
    result_id = result["result_id"]

    assert client.get(
        "/bop/inspection/setup",
        query_string={"run_root": other_run.as_posix(), "result_id": result_id},
    ).status_code == 404
    location = client.get(
        "/bop/inspection/result-location", query_string={"result_id": result_id}
    )
    assert location.status_code == 200
    assert location.get_json() == {"result_id": result_id, "run_root": run.as_posix()}
    assert client.get(
        "/bop/inspection/result-location", query_string={"result_id": "result-000000000000"}
    ).status_code == 404
    assert client.get(
        "/bop/inspection/result-location", query_string={"result_id": "../result.json"}
    ).status_code == 400

    duplicate = other_run / "processed" / "bop_evaluation" / "results" / result_id
    duplicate.mkdir(parents=True)
    shutil.copy2(
        run / "processed" / "bop_evaluation" / "results" / result_id / "result.json",
        duplicate / "result.json",
    )
    assert client.get(
        "/bop/inspection/result-location", query_string={"result_id": result_id}
    ).status_code == 400
    (duplicate / "result.json").unlink()
    (duplicate / "result.json").symlink_to(
        run / "processed" / "bop_evaluation" / "results" / result_id / "result.json"
    )
    assert client.get(
        "/bop/inspection/result-location", query_string={"result_id": result_id}
    ).status_code == 200
