"""Offline Inspect evidence: robot consistency versus retained ground truth.

This deliberately supports the unambiguous single-instance/single-estimate
case. It reuses completed official evaluations; it never runs an estimator or
changes a dataset, and publishes only in an input run's bop_evaluation tree.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import trimesh
from PIL import Image
from scipy.spatial.transform import Rotation
from scipy.stats import pearsonr, spearmanr

from posetestbot.bop import evaluation as ev, robot_consistency as rc
from posetestbot.io.atomic import atomic_write_json
from posetestbot.sensors.registry import get_sensor_adapter

REVISION = "posetestbot_consistency_gt_comparison.v1"
COLORS = ["#137b78", "#d47523", "#7a59ba", "#bb4d69", "#3479b4"]


def vertex_errors(
    prediction: np.ndarray, truth: np.ndarray, vertices: np.ndarray, model_info: dict
) -> dict[str, float]:
    """Same corresponding vertices and IPD symmetry convention as consistency."""
    difference = rc.canonical_pose(prediction, model_info) - rc.canonical_pose(
        truth, model_info
    )
    maximum = total = 0.0
    for start in range(0, len(vertices), rc.VERTEX_CHUNK):
        chunk = vertices[start : start + rc.VERTEX_CHUNK]
        distances = np.linalg.norm(
            chunk @ difference[:3, :3].T + difference[:3, 3], axis=1
        )
        maximum = max(maximum, float(distances.max()))
        total += float(distances.sum())
    if not len(vertices):
        raise ValueError("Comparison model has no vertices")
    return {"mvd_mm": maximum, "add_mm": total / len(vertices)}


def correlation(x: list[float], y: list[float]) -> dict[str, float | None]:
    a, b = np.asarray(x), np.asarray(y)
    if len(a) < 3 or np.ptp(a) < 1e-10 or np.ptp(b) < 1e-10:
        return {"pearson": None, "spearman": None}
    return {
        "pearson": float(pearsonr(a, b).statistic),
        "spearman": float(spearmanr(a, b).statistic),
    }


def summarize(rows: list[dict], kind: str, threshold: float = 10.0) -> dict:
    gt_key, rc_key = f"gt_{kind}_mm", f"rc_{kind}_mm"
    predicted = [r for r in rows if r[gt_key] is not None]
    matched = [r for r in predicted if r[rc_key] is not None]
    truth = np.asarray([r[gt_key] for r in predicted])
    gt = np.asarray([r[gt_key] for r in matched])
    consistency = np.asarray([r[rc_key] for r in matched])
    ordinary = [r for r in matched if r[gt_key] < 20]
    windows = []
    # Blocks of 30 eligible frames from one camera/instance only. Compare the
    # same matched subset within each block, retaining the block boundaries.
    if len({(r["condition"], r["scene_id"], r["instance_uuid"]) for r in matched}) == 1:
        for start in range(0, len(rows), 30):
            eligible_block = rows[start : start + 30]
            block = [r for r in eligible_block if r[rc_key] is not None]
            if len(eligible_block) == 30 and len(block) >= 2:
                windows.append(
                    [
                        float(np.mean([r[gt_key] for r in block])),
                        float(np.mean([r[rc_key] for r in block])),
                    ]
                )
    counts = {
        "both_below": int(np.sum((gt < threshold) & (consistency < threshold))),
        "low_rc_high_gt": int(np.sum((consistency < threshold) & (gt >= threshold))),
        "high_rc_low_gt": int(np.sum((consistency >= threshold) & (gt < threshold))),
        "both_above": int(np.sum((gt >= threshold) & (consistency >= threshold))),
    }
    return {
        "eligible": len(rows),
        "predicted": len(predicted),
        "matched": len(matched),
        "missing": len(rows) - len(predicted),
        "excluded": len(predicted) - len(matched),
        "gt_mean_all_mm": float(truth.mean()) if len(truth) else None,
        "gt_p95_all_mm": float(np.percentile(truth, 95)) if len(truth) else None,
        "gt_max_all_mm": float(truth.max()) if len(truth) else None,
        "gt_mean_matched_mm": float(gt.mean()) if len(gt) else None,
        "rc_mean_mm": float(consistency.mean()) if len(consistency) else None,
        "gt_below_threshold_all": int(np.sum(truth < threshold)),
        "signed_difference_mm": float((consistency - gt).mean()) if len(gt) else None,
        "mae_mm": float(np.abs(consistency - gt).mean()) if len(gt) else None,
        "correlation": correlation(gt.tolist(), consistency.tolist()),
        "ordinary_correlation": correlation(
            [r[gt_key] for r in ordinary], [r[rc_key] for r in ordinary]
        ),
        "ordinary_count": len(ordinary),
        "window_correlation": correlation(
            [w[0] for w in windows], [w[1] for w in windows]
        ),
        "window_count": len(windows),
        "threshold_mm": threshold,
        "threshold_counts": counts,
    }


def _read(root: Path, relative: str, hashes: dict[str, str]) -> dict | list:
    path = rc._path(root, relative)
    if not ev._plain_file(path, root=root):
        raise ValueError("Comparison input is missing or unsafe: " + relative)
    content = path.read_bytes()
    hashes[relative] = hashlib.sha256(content).hexdigest()
    return json.loads(content)


def _load_source(
    label: str, root: Path, evaluation_id: str, index: int
) -> tuple[dict, dict]:
    hashes: dict[str, str] = {}
    folder = ev.evaluation_report_path(root, evaluation_id).parent
    relative = folder.relative_to(root).as_posix()
    report = _read(root, relative + "/report.json", hashes)
    request = _read(root, relative + "/request.json", hashes)
    if (
        report.get("schema_version") != "bop_evaluation_report.v1"
        or report.get("status") != "succeeded"
        or report.get("evaluation_id") != evaluation_id
        or request.get("run_root") != str(root)
        or request.get("result_id") != report.get("result_id")
    ):
        raise ValueError("Comparison requires a completed immutable-result evaluation")
    provenance = report["provenance"]
    dataset = ev.inspect_dataset(root, include_depth_content=True)
    if not dataset["evaluation_ready"] or any(
        dataset.get(key) != provenance.get(key)
        for key in (
            "dataset_sha256",
            "dataset_content_sha256",
            "export_manifest_sha256",
        )
    ):
        raise ValueError(
            "Comparison dataset no longer matches its completed evaluation"
        )
    result = ev.get_result(root, report["result_id"], dataset=dataset)
    path = ev.result_file_path(root, report["result_id"], dataset=dataset)
    if result["sha256"] != provenance["result_sha256"]:
        raise ValueError("Comparison result no longer matches its completed evaluation")
    robot = _read(root, relative + "/" + rc.REPORT_FILENAME, hashes)
    inputs = _read(root, relative + "/" + rc.INPUTS_FILENAME, hashes)
    if (
        hashes[relative + "/" + rc.REPORT_FILENAME]
        != provenance["robot_consistency_report_sha256"]
        or hashes[relative + "/" + rc.INPUTS_FILENAME]
        != provenance["robot_consistency_inputs_sha256"]
        or robot.get("result_sha256") != result["sha256"]
        or robot.get("dataset_sha256") != dataset["dataset_sha256"]
        or robot.get("implementation_revision") != rc.REVISION
        or robot.get("status") != "available"
    ):
        raise ValueError(
            "Comparison robot-consistency evidence failed integrity validation"
        )
    rc.verify_sources(root, inputs)
    hashes.update(inputs["source_sha256"])
    _read(root, "bop/bop_export_manifest.json", hashes)
    models_info = _read(root, "bop/models_eval/models_info.json", hashes)
    targets = _read(root, provenance["selected_targets_path"], hashes)
    if (
        hashes[provenance["selected_targets_path"]]
        != provenance["selected_targets_sha256"]
    ):
        raise ValueError("Comparison target inventory failed integrity validation")
    target_keys = {(r["scene_id"], r["im_id"], r["obj_id"]) for r in targets}
    estimates = {}
    hashes[path.relative_to(root).as_posix()] = result["sha256"]
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (int(row["scene_id"]), int(row["im_id"]), int(row["obj_id"]))
            if key not in target_keys:
                continue
            if key in estimates:
                raise ValueError(
                    "GT comparison requires at most one estimate per target"
                )
            pose = np.eye(4)
            pose[:3, :3] = np.asarray([float(v) for v in row["R"].split()]).reshape(
                3, 3
            )
            pose[:3, 3] = [float(v) for v in row["t"].split()]
            estimates[key] = rc._rigid(pose)
    config = (
        _read(root, "run_config.json", hashes)
        if (root / "run_config.json").exists()
        else {}
    )
    aliases = {
        f"{get_sensor_adapter(s['sensor_type']).folder_prefix}_{s['device_id']}": s.get(
            "operator_alias"
        )
        or s.get("display_name")
        for s in config.get("capture", {}).get("sensors", [])
    }
    condition = {
        "index": index,
        "label": label,
        "run_name": root.name,
        "color": COLORS[index % len(COLORS)],
        "evaluation_id": evaluation_id,
        "result_id": result["result_id"],
        "method_name": result.get("method_name", result.get("method")),
        "oracle_gt_masks": "oracle gt masks" in result.get("method_name", "").lower(),
        "independent_registration": (result.get("sensor_scope") or {}).get(
            "execution_mode"
        )
        == "independent_registration",
        "result_filename": result["filename"],
        "dataset_sha256": dataset["dataset_sha256"],
        "result_sha256": result["sha256"],
        "evaluation_report_sha256": hashes[relative + "/report.json"],
        "robot_consistency_report_sha256": provenance[
            "robot_consistency_report_sha256"
        ],
        "toolkit_revision": provenance["toolkit_revision"],
        "ipd_revision": rc.IPD_REVISION,
        "official_scores": report["official_scores"],
        "tracks": [],
        "rows": [],
    }
    internals = {
        "root": root,
        "hashes": hashes,
        "inputs": inputs,
        "dataset": dataset,
        "poses": {},
        "vertices": {},
        "model_info": models_info,
        "cameras": {},
        "geometry": {},
    }
    for scene in inputs["scenes"]:
        scene_id = scene["scene_id"]
        if scene["status"] != "available":
            raise ValueError("GT comparison requires complete robot-driven viewpoints")
        scene_path = f"bop/{dataset['split']}/{scene_id:06d}"
        gt = _read(root, scene_path + "/scene_gt.json", hashes)
        camera = _read(root, scene_path + "/scene_camera.json", hashes)
        gt_info = (
            _read(root, scene_path + "/scene_gt_info.json", hashes)
            if rc._path(root, scene_path + "/scene_gt_info.json").exists()
            else {}
        )
        scene_tracks = [t for t in robot["tracks"] if t["scene_id"] == scene_id]
        if len(scene_tracks) != 1 or scene_tracks[0]["status"] != "available":
            raise ValueError(
                "GT comparison currently requires one fixed instance per scene"
            )
        saved_track = scene_tracks[0]
        obj_id = saved_track["obj_id"]
        model_path = rc._path(root, f"bop/models_eval/obj_{obj_id:06d}.ply")
        model_relative = model_path.relative_to(root).as_posix()
        hashes[model_relative] = ev._sha256_file(model_path)
        vertices = np.asarray(trimesh.load(model_path, process=False).vertices)
        internals["vertices"][scene_id] = vertices
        errors = {e["im_id"]: e for e in saved_track["frame_errors"]}
        rows, references, truths, matched_poses, matched_references = [], [], [], [], []
        for frame in scene["frames"]:
            im_id = frame["im_id"]
            identities = [
                i
                for i in frame["instances"]
                if (scene_id, im_id, i["obj_id"]) in target_keys
            ]
            if (
                len(identities) != 1
                or identities[0]["instance_uuid"] != saved_track["instance_uuid"]
            ):
                raise ValueError(
                    "GT comparison requires one unambiguous fixed instance per target"
                )
            identity = identities[0]
            truth = rc._annotation_pose(gt[str(im_id)][identity["gt_id"]])
            reference = rc._rigid(frame["camera_to_reference_mm"])
            prediction = estimates.get((scene_id, im_id, obj_id))
            row = {
                "condition": index,
                "scene_id": scene_id,
                "sensor_name": scene["sensor_name"],
                "sensor_alias": aliases.get(scene["sensor_name"])
                or scene["sensor_name"],
                "instance_uuid": identity["instance_uuid"],
                "obj_id": obj_id,
                "gt_id": identity["gt_id"],
                "im_id": im_id,
                "gt_mvd_mm": None,
                "gt_add_mm": None,
                "rc_mvd_mm": None,
                "rc_add_mm": None,
                "translation_error_mm": None,
                "rotation_error_deg": None,
                "gt_visible_fraction": gt_info[str(im_id)][identity["gt_id"]].get(
                    "visib_fract"
                )
                if gt_info
                else None,
                "status": "missing"
                if prediction is None
                else "matched"
                if im_id in errors
                else "matching_rejected",
            }
            if prediction is not None:
                values = vertex_errors(
                    prediction, truth, vertices, models_info[str(obj_id)]
                )
                row.update({"gt_" + key: value for key, value in values.items()})
                row["translation_error_mm"] = float(
                    np.linalg.norm(prediction[:3, 3] - truth[:3, 3])
                )
                row["rotation_error_deg"] = float(
                    np.rad2deg(
                        Rotation.from_matrix(
                            prediction[:3, :3] @ truth[:3, :3].T
                        ).magnitude()
                    )
                )
                distance = np.linalg.norm(
                    rc.canonical_pose(prediction, models_info[str(obj_id)])[:3, 3]
                    - rc.canonical_pose(truth, models_info[str(obj_id)])[:3, 3]
                )
                if (distance < rc.MATCH_THRESHOLD_MM) != (im_id in errors):
                    raise ValueError(
                        "Comparison matching disagrees with the retained robot-consistency report"
                    )
                if im_id in errors:
                    row.update(
                        rc_mvd_mm=errors[im_id]["mvd_mm"],
                        rc_add_mm=errors[im_id]["add_mm"],
                    )
                    matched_poses.append(prediction)
                    matched_references.append(reference)
            rows.append(row)
            references.append(reference)
            truths.append(truth)
            internals["poses"][(scene_id, im_id)] = (reference, truth, prediction)
            internals["cameras"][(scene_id, im_id)] = camera[str(im_id)]
        recomputed = rc.measure_track(
            np.asarray(matched_references),
            np.asarray(matched_poses),
            vertices,
            models_info[str(obj_id)],
        )
        for key in ("mvd_mm", "add_mm"):
            if not np.isclose(recomputed[key], saved_track[key], rtol=0, atol=1e-8):
                raise ValueError(
                    "Comparison does not reproduce the retained robot-consistency score"
                )
        for recorded, actual in zip(
            saved_track["frame_errors"], recomputed["frame_errors"], strict=True
        ):
            if any(
                not np.isclose(recorded[k], actual[k], rtol=0, atol=1e-8)
                for k in ("mvd_mm", "add_mm")
            ):
                raise ValueError("Comparison does not reproduce per-frame consistency")
        gt_control = rc.measure_track(
            np.asarray(references),
            np.asarray(truths),
            vertices,
            models_info[str(obj_id)],
        )
        if gt_control["mvd_mm"] > 1e-4:
            raise ValueError(
                "Retained GT fails the fixed-workpiece consistency control"
            )
        mean_bias = vertex_errors(
            np.asarray(saved_track["reference_mean_model_to_template_base_mm"]),
            references[0] @ truths[0],
            vertices,
            models_info[str(obj_id)],
        )
        track = {
            k: saved_track[k]
            for k in (
                "scene_id",
                "sensor_name",
                "instance_uuid",
                "obj_id",
                "eligible_frames",
                "matched_frames",
                "coverage",
            )
        }
        track.update(
            sensor_alias=rows[0]["sensor_alias"],
            model_sha256=hashes[model_relative],
            vertex_count=len(vertices),
            diameter_mm=models_info[str(obj_id)]["diameter"],
            declared_symmetries=bool(
                models_info[str(obj_id)].get("symmetries_discrete")
                or models_info[str(obj_id)].get("symmetries_continuous")
            ),
            mean_reference_bias=mean_bias,
            gt_control={k: gt_control[k] for k in ("mvd_mm", "add_mm")},
            statistics={kind: summarize(rows, kind) for kind in ("mvd", "add")},
        )
        if not track["declared_symmetries"]:
            official_path = (
                Path(provenance["official_scores_path"]).parent
                / "error=mssd_ntop=-1"
                / f"errors_{scene_id:06d}.json"
            ).as_posix()
            official = _read(root, official_path, hashes)
            by_key = {
                (r["im_id"], r["obj_id"]): r for r in official if r["est_id"] == 0
            }
            differences = []
            for row in rows:
                if row["gt_mvd_mm"] is None:
                    continue
                entry = by_key.get((row["im_id"], obj_id))
                if entry is None or str(row["gt_id"]) not in entry["errors"]:
                    raise ValueError(
                        "Official MSSD evidence is missing a compared GT pose"
                    )
                differences.append(
                    abs(float(entry["errors"][str(row["gt_id"])][0]) - row["gt_mvd_mm"])
                )
            if (
                not differences
                or not np.isfinite(differences).all()
                or max(differences) > 1e-4
            ):
                raise ValueError(
                    "GT vertex errors disagree with official asymmetric-model MSSD"
                )
            track["official_mssd_check"] = {
                "status": "verified",
                "frames": len(differences),
                "max_difference_mm": max(differences),
            }
        else:
            track["official_mssd_check"] = {
                "status": "not_applicable",
                "reason": "Official MSSD optimizes over symmetries; this comparison uses the IPD identity gauge.",
            }
        condition["tracks"].append(track)
        condition["rows"].extend(rows)
    if (
        len(condition["rows"]) != robot["eligible_frames"]
        or sum(r["status"] == "matched" for r in condition["rows"])
        != robot["matched_frames"]
    ):
        raise ValueError(
            "Comparison frame coverage disagrees with the retained evaluation"
        )
    condition["statistics"] = {
        kind: summarize(condition["rows"], kind) for kind in ("mvd", "add")
    }
    for kind in ("mvd", "add"):
        condition["statistics"][kind]["rc_macro_mean_mm"] = float(
            np.mean([t["statistics"][kind]["rc_mean_mm"] for t in condition["tracks"]])
        )
    return condition, internals


def interpretation(conditions: list[dict]) -> dict:
    """Factual overview, with explicit MVD thresholds and matching scope."""
    gt_order = sorted(
        conditions, key=lambda c: c["statistics"]["mvd"]["gt_mean_all_mm"]
    )
    rc_order = sorted(
        conditions, key=lambda c: c["statistics"]["mvd"]["rc_macro_mean_mm"]
    )
    ranking_agrees = [c["index"] for c in gt_order] == [c["index"] for c in rc_order]
    rows = [r for c in conditions for r in c["rows"]]
    matched = [r for r in rows if r["rc_mvd_mm"] is not None]
    large = [r for r in matched if r["gt_mvd_mm"] >= 50]
    detected = [r for r in large if r["rc_mvd_mm"] >= 20]
    low_rc_high_gt = sum(r["rc_mvd_mm"] < 10 and r["gt_mvd_mm"] >= 10 for r in matched)
    excluded = sum(r["status"] == "matching_rejected" for r in rows)
    high_rc_low_gt = max(
        ((c, t) for c in conditions for t in c["tracks"]),
        key=lambda ct: ct[1]["statistics"]["mvd"]["threshold_counts"]["high_rc_low_gt"],
    )
    condition, track = high_rc_low_gt
    count = track["statistics"]["mvd"]["threshold_counts"]["high_rc_low_gt"]
    findings = [
        (
            "The overall condition ranking agrees: "
            if ranking_agrees
            else "The overall condition rankings disagree. GT order: "
        )
        + " → ".join(c["label"] for c in gt_order)
        + ". GT uses every estimate; RC uses matched estimates and equal camera weights.",
        f"At the illustrative 10 mm MVD threshold, {low_rc_high_gt} matched frames have RC below 10 mm but GT error at or above 10 mm. Another {excluded} estimates are excluded by the 100 mm association gate; their GT errors remain in this report.",
    ]
    if large:
        findings.insert(
            1,
            f"Large failures are visible in consistency: {len(detected)} of {len(large)} matched estimates with GT MVD at or above 50 mm also have RC MVD at or above 20 mm. This does not include association rejections.",
        )
    if count:
        findings.append(
            f"{condition['label']} / {track['sensor_alias']}: {count} matched frames with GT MVD below 10 mm have RC MVD at or above 10 mm. The mean-reference vertex bias is {track['mean_reference_bias']['mvd_mm']:.2f} mm. Wrong predictions can shift the mean and give accurate views a large consistency error."
        )
    return {
        "ranking_agrees": ranking_agrees,
        "gt_order": [c["index"] for c in gt_order],
        "rc_order": [c["index"] for c in rc_order],
        "large_gt_matched": len(large),
        "large_detected": len(detected),
        "low_rc_high_gt": low_rc_high_gt,
        "matching_rejected": excluded,
        "findings": findings,
    }


def _paired_geometry(conditions: list[dict], internals: list[dict]) -> list[dict]:
    pairs = []
    baseline = conditions[0]
    for condition, data in zip(conditions[1:], internals[1:], strict=True):
        if {
            (t["scene_id"], t["sensor_name"], t["instance_uuid"], t["model_sha256"])
            for t in condition["tracks"]
        } != {
            (t["scene_id"], t["sensor_name"], t["instance_uuid"], t["model_sha256"])
            for t in baseline["tracks"]
        }:
            raise ValueError(
                "Comparison conditions must share camera identities, instances and evaluation geometry"
            )
        for track in condition["tracks"]:
            scene = track["scene_id"]
            keys = sorted(set(internals[0]["poses"]) & set(data["poses"]))
            keys = [k for k in keys if k[0] == scene]
            if not keys:
                raise ValueError(
                    "Comparison conditions have no common trajectory frame IDs"
                )
            vertex, translation, rotation = [], [], []
            for key in keys:
                base_ref, base_gt, _ = internals[0]["poses"][key]
                ref, gt, _ = data["poses"][key]
                vertex.append(
                    vertex_errors(
                        gt,
                        base_gt,
                        data["vertices"][scene],
                        data["model_info"][str(track["obj_id"])],
                    )["mvd_mm"]
                )
                translation.append(float(np.linalg.norm(ref[:3, 3] - base_ref[:3, 3])))
                rotation.append(
                    float(
                        np.rad2deg(
                            Rotation.from_matrix(
                                ref[:3, :3] @ base_ref[:3, :3].T
                            ).magnitude()
                        )
                    )
                )
            pairs.append(
                {
                    "condition": condition["index"],
                    "scene_id": scene,
                    "sensor_alias": track["sensor_alias"],
                    "paired_frames": len(keys),
                    "gt_vertex_difference_mean_mm": float(np.mean(vertex)),
                    "gt_vertex_difference_max_mm": float(np.max(vertex)),
                    "camera_translation_difference_max_mm": max(translation),
                    "camera_rotation_difference_max_deg": max(rotation),
                }
            )
    return pairs


def _examples(
    conditions: list[dict], internals: list[dict], reference_frame: int | None
) -> list[dict]:
    from posetestbot.bop.inspection_render import (
        GEOMETRY_DEFAULTS,
        load_geometry,
        render_geometry,
    )

    shared_frames = {}
    for track in conditions[0]["tracks"]:
        scene_id = track["scene_id"]
        common = set.intersection(
            *(
                {
                    row["im_id"]
                    for row in condition["rows"]
                    if row["scene_id"] == scene_id
                }
                for condition in conditions
            )
        )
        if reference_frame is not None:
            if reference_frame not in common:
                raise ValueError(
                    f"Reference frame {reference_frame} must exist in every condition "
                    f"for scene {scene_id}"
                )
            shared_frames[scene_id] = reference_frame
        elif common:
            ordered = sorted(common)
            shared_frames[scene_id] = ordered[len(ordered) // 4]
        else:
            raise ValueError(
                f"Scene {scene_id} has no shared example frame across every condition"
            )

    examples = []
    for condition, data in zip(conditions, internals, strict=True):
        for track in condition["tracks"]:
            rows = [r for r in condition["rows"] if r["scene_id"] == track["scene_id"]]
            predicted = [r for r in rows if r["gt_mvd_mm"] is not None]
            matched = [r for r in rows if r["status"] == "matched"]
            chosen: dict[int, list[str]] = {}

            def choose(row, title):
                chosen.setdefault(row["im_id"], []).append(title)

            reference = next(
                r for r in rows if r["im_id"] == shared_frames[track["scene_id"]]
            )
            choose(reference, "Shared trajectory view")
            if predicted:
                choose(max(predicted, key=lambda r: r["gt_mvd_mm"]), "Largest GT error")
            low = [r for r in matched if r["rc_mvd_mm"] < 10 and r["gt_mvd_mm"] >= 10]
            if low:
                choose(
                    max(low, key=lambda r: r["gt_mvd_mm"]),
                    "Low consistency error, higher GT error",
                )
            rejected = [r for r in rows if r["status"] == "matching_rejected"]
            if rejected:
                choose(
                    max(rejected, key=lambda r: r["gt_mvd_mm"]),
                    "Excluded by the 100 mm association gate",
                )
            model = load_geometry(
                rc._path(data["root"], f"bop/models_eval/obj_{track['obj_id']:06d}.ply")
            )
            for im_id, titles in chosen.items():
                row = next(r for r in rows if r["im_id"] == im_id)
                relative = f"bop/{data['dataset']['split']}/{track['scene_id']:06d}/rgb/{im_id:06d}.png"
                path = rc._path(data["root"], relative)
                if not ev._plain_file(path, root=data["root"]):
                    raise ValueError("Comparison example RGB is missing or unsafe")
                data["hashes"][relative] = ev._sha256_file(path)
                with Image.open(path) as image:
                    rgb = np.array(image.convert("RGB"))
                _, truth, prediction = data["poses"][(track["scene_id"], im_id)]
                frame = {
                    "camera": data["cameras"][(track["scene_id"], im_id)],
                    "ground_truth": [
                        {
                            "obj_id": track["obj_id"],
                            "gt_id": 0,
                            "rotation": truth[:3, :3].reshape(-1).tolist(),
                            "translation_mm": truth[:3, 3].tolist(),
                        }
                    ],
                    "estimates": [
                        {
                            "obj_id": track["obj_id"],
                            "rank": 0,
                            "rotation": prediction[:3, :3].reshape(-1).tolist(),
                            "translation_mm": prediction[:3, 3].tolist(),
                        }
                    ]
                    if prediction is not None
                    else [],
                }
                overlay = render_geometry(
                    rgb.copy(),
                    frame,
                    {track["obj_id"]: model},
                    {**GEOMETRY_DEFAULTS, "gtOpacity": 0.65},
                )

                def encode(pixels):
                    image = Image.fromarray(pixels)
                    image.thumbnail((640, 360))
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=90)
                    return "data:image/jpeg;base64," + base64.b64encode(
                        buffer.getvalue()
                    ).decode("ascii")

                examples.append(
                    {
                        "row": row,
                        "titles": titles,
                        "rgb": encode(rgb),
                        "overlay": encode(overlay),
                    }
                )
    return examples


def scientific_plots(report: dict, folder: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
    )
    scenes = sorted({t["scene_id"] for t in report["conditions"][0]["tracks"]})
    fig, axes = plt.subplots(
        2, len(scenes), figsize=(5 * len(scenes), 8), squeeze=False
    )
    for col, scene in enumerate(scenes):
        for condition in report["conditions"]:
            rows = [r for r in condition["rows"] if r["scene_id"] == scene]
            paired = [r for r in rows if r["rc_mvd_mm"] is not None]
            axes[0, col].scatter(
                [r["gt_mvd_mm"] for r in paired],
                [r["rc_mvd_mm"] for r in paired],
                s=8,
                alpha=0.4,
                color=condition["color"],
                label=condition["label"],
            )
            axes[1, col].plot(
                [r["im_id"] for r in rows],
                [r["gt_mvd_mm"] for r in rows],
                color=condition["color"],
                linewidth=0.8,
                label=condition["label"],
            )
        high = (
            max(
                20,
                max(
                    r["gt_mvd_mm"]
                    for c in report["conditions"]
                    for r in c["rows"]
                    if r["scene_id"] == scene and r["rc_mvd_mm"] is not None
                ),
                max(
                    r["rc_mvd_mm"]
                    for c in report["conditions"]
                    for r in c["rows"]
                    if r["scene_id"] == scene and r["rc_mvd_mm"] is not None
                ),
            )
            * 1.05
        )
        axes[0, col].plot(
            [0, high], [0, high], color="#9ca3af", linewidth=1, linestyle="--"
        )
        axes[0, col].set(
            xlabel="GT MVD (mm)",
            ylabel="Robot consistency MVD (mm)",
            xlim=(0, high),
            ylim=(0, high),
            title=f"{rows[0]['sensor_alias']} · matched estimates",
        )
        axes[1, col].set(
            xlabel="BOP image ID (repeated trajectory)",
            ylabel="GT MVD (mm)",
            title="All estimates, including association rejections",
        )
        axes[1, col].set_yscale("symlog", linthresh=10)
        for ax in axes[:, col]:
            ax.grid(alpha=0.15)
    axes[0, 0].legend(loc="upper left", fontsize=8)
    fig.suptitle(report["title"] + " — consistency versus saved GT", fontsize=14)
    fig.tight_layout()
    fig.savefig(folder / "consistency_vs_gt.png", dpi=180)
    fig.savefig(folder / "consistency_vs_gt.svg")
    plt.close(fig)


def create_comparison(
    sources: list[tuple[str, Path, str]],
    *,
    output_run: Path,
    title: str = "Robot consistency vs ground truth",
    reference_frame: int | None = None,
) -> Path:
    if len(sources) < 2:
        raise ValueError("Choose a baseline and at least one comparison condition")
    output_run = output_run.resolve()
    normalized = [
        (label, root.resolve(), identifier) for label, root, identifier in sources
    ]
    if output_run not in {root for _, root, _ in normalized}:
        raise ValueError(
            "Comparison output must belong to one of its evaluated input runs"
        )
    if len({label for label, _, _ in normalized}) != len(normalized):
        raise ValueError("Comparison condition labels must be unique")
    conditions, internals = [], []
    for index, (label, root, identifier) in enumerate(normalized):
        condition, internal = _load_source(label, root, identifier, index)
        conditions.append(condition)
        internals.append(internal)
    report = {
        "schema_version": "bop_consistency_gt_comparison.v1",
        "implementation_revision": REVISION,
        "title": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "conditions": conditions,
        "paired_geometry": _paired_geometry(conditions, internals),
        "metric_definition": "corresponding evaluation-model vertex maximum/mean displacement; IPD identity-referenced symmetry convention",
        "matching_threshold_mm": rc.MATCH_THRESHOLD_MM,
        "diagnostic_threshold_mm": 10,
        "source_url": rc.IPD_SOURCE,
    }
    report["examples"] = _examples(conditions, internals, reference_frame)
    report["overview"] = interpretation(conditions)
    # Validate every exact input again before publishing any report artifacts.
    for data in internals:
        for relative, expected in data["hashes"].items():
            path = rc._path(data["root"], relative)
            if (
                not ev._plain_file(path, root=data["root"])
                or ev._sha256_file(path) != expected
            ):
                raise ValueError(
                    "Comparison input changed during analysis: " + relative
                )
        dataset = ev.inspect_dataset(data["root"], include_depth_content=True)
        if any(
            dataset[key] != data["dataset"][key]
            for key in ("dataset_sha256", "dataset_content_sha256")
        ):
            raise ValueError("Comparison dataset changed during analysis")
    comparison_id = "comparison-" + uuid4().hex[:12]
    report["comparison_id"] = comparison_id
    folder = rc._path(
        output_run, "processed/bop_evaluation/comparisons/" + comparison_id
    )
    folder.mkdir(parents=True, exist_ok=False)
    request = {
        "schema_version": "bop_consistency_gt_comparison_request.v1",
        "comparison_id": comparison_id,
        "implementation_revision": REVISION,
        "title": title,
        "reference_frame": reference_frame,
        "sources": [
            {
                "label": label,
                "run_root": str(root),
                "evaluation_id": identifier,
                "source_sha256": internal["hashes"],
            }
            for (label, root, identifier), internal in zip(
                normalized, internals, strict=True
            )
        ],
    }
    atomic_write_json(folder / "request.json", request)
    (folder / "request.json").chmod(0o444)
    atomic_write_json(folder / "comparison.json", report)
    rows = [r for condition in conditions for r in condition["rows"]]
    with (folder / "frames.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["condition_label", *rows[0]])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {"condition_label": conditions[row["condition"]]["label"], **row}
            )
    from posetestbot.bop.consistency_comparison_html import render_html

    (folder / "index.html").write_text(render_html(report), encoding="utf-8")
    scientific_plots(report, folder)
    artifacts = {p.name: ev._sha256_file(p) for p in folder.iterdir() if p.is_file()}
    atomic_write_json(
        folder / "manifest.json",
        {
            "schema_version": "bop_consistency_gt_comparison_manifest.v1",
            "comparison_id": comparison_id,
            "status": "succeeded",
            "artifacts_sha256": artifacts,
        },
    )
    return folder / "index.html"
