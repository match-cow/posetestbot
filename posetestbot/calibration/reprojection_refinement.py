"""Recorded-data shared-grid refinement, with fixed intrinsics and explicit promotion.

This does not rewrite an attempt's solver comparison. A separate retained report
binds the passing seed bundle, measured corners, robot poses, and a reproducible
motion-disjoint optical audit. No camera or robot is opened by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from posetestbot.aruco.grid import _matched_points
from posetestbot.calibration.attempt_solver import solve_extrinsic
from posetestbot.calibration.profiles import (
    CalibrationProfile,
    RigidTransform,
    TransformFrame,
    load_profile_collection,
)
from posetestbot.calibration.targets import opencv_grid_board, validate_target_identity
from posetestbot.calibration.transforms import (
    average_transform,
    invert_transform,
    robot_ee_to_reference,
    transform_from_record,
    transform_record,
    transform_residual,
)
from posetestbot.io.atomic import atomic_write_json

SCHEMA_VERSION = "calibration_reprojection_refinement.v1"
IMPLEMENTATION_REVISION = "shared_grid_fixed_intrinsics.v2"
DIRECTORY = "reprojection_refinement"
REPORT = "report.json"
POLICY = {
    "reserved_view_identity": "preparation_source_frame",
    "frames_per_motion": 6,
    "motion_folds": 3,
    "minimum_common_motions": 9,
    "maximum_rotation_adjustment_rad": 0.25,
    "maximum_translation_adjustment_mm": 30.0,
    "maximum_function_evaluations": 150,
    "robust_loss": "soft_l1",
    "weighted_loss_scale": 0.15,
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _binding(root: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    relative = resolved.relative_to(root.resolve())
    with resolved.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "path": relative.as_posix(),
        "sha256": digest,
        "size_bytes": resolved.stat().st_size,
    }


def refinement_report_path(attempt_root: Path, refinement_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", refinement_id):
        raise ValueError("Invalid reprojection refinement identity")
    parent = attempt_root.resolve() / DIRECTORY
    path = attempt_root / DIRECTORY / refinement_id / REPORT
    if not path.resolve().is_relative_to(parent):
        raise ValueError("Reprojection refinement escapes its attempt")
    return path


def _inputs(
    root: Path, attempt: Mapping[str, Any], selections: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Import lazily: attempts invokes this module only for explicitly requested
    # refinements, after its ordinary seed/timing/promotion checks have passed.
    from posetestbot.calibration.attempts import (
        _require_current_attempt_request,
        _verify_robot_pose_artifact_bindings,
        calibration_attempt_root,
    )

    request = attempt["request"]
    _require_current_attempt_request(request)
    if (
        attempt["progress"]["status"] != "complete"
        or request["mode"] != "eye_in_hand"
        or request["target_mounting"]["state"] != "estimated"
        or len(selections) < 2
        or set(selections) != set(request["sensor_keys"])
    ):
        raise ValueError(
            "Shared-grid refinement requires a complete wrist-camera bundle"
        )
    folder = calibration_attempt_root(root, str(request["attempt_id"]))
    raw = _verify_robot_pose_artifact_bindings(root, request)
    _, board = opencv_grid_board(request["target"])
    profiles = {
        str(p.metadata["candidate_id"]): p
        for p in load_profile_collection(folder / "candidate_profiles.json")
    }
    observations = _read(folder / "observations.json")["observations"]
    pnp = {
        s["sensor_key"]: {f["source_frame_id"]: f for f in s["frames"]}
        for s in _read(folder / "pnp_candidates.json")["sensors"]
    }
    intrinsic = {
        s["sensor_key"]: set(s["comparison_split"]["heldout_views"])
        for s in _read(folder / "intrinsic_comparison.json")["sensors"]
    }
    paths = [
        folder / name
        for name in (
            "request.json",
            "ranking.json",
            "candidate_profiles.json",
            "pnp_candidates.json",
            "observations.json",
            "time_offset_search.json",
            "intrinsic_comparison.json",
        )
    ]
    paths.extend(root / name for name in raw)
    cameras = []
    for sensor in request["sensors"]:
        key = str(sensor["sensor_key"])
        profile = profiles[selections[key]]
        sync = folder / "processed/synchronized" / sensor["sensor_name"]
        detection_path = (
            folder
            / "processed/preparation_synchronized"
            / sensor["sensor_name"]
            / "aruco_detections.json"
        )
        paths.extend([sync / "match_robot_ee_poses.json", detection_path])
        detections = _read(detection_path)
        validate_target_identity(
            detections["target"], request["target"], label="Refinement"
        )
        accepted = {
            o["source_frame_id"]: o
            for o in observations
            if f"{o['sensor_type']}:{o['device_id']}" == key
            and o["pnp_method"] == profile.metadata["pnp_method"]
        }
        rows = []
        for frame, match in sorted(_read(sync / "match_robot_ee_poses.json").items()):
            source_id = match["source_frame_id"]
            observation = accepted.get(source_id)
            if observation is None:
                continue
            packet = raw[sensor["robot_pose_path"]][
                str(match["matched_robot_pose_index"])
            ]
            if (
                packet["pose"] != match["robot_ee_pose"]
                or observation["robot_ee_pose"] != match["robot_ee_pose"]
            ):
                raise ValueError(
                    "Refinement robot pose differs from immutable raw evidence"
                )
            support = pnp[key][source_id]
            objects, pixels = _matched_points(
                detections["frames"][support["frame_id"]], board
            )
            indices = np.asarray(support["common_inlier_indices"], dtype=int)
            if (
                len(indices) < 6
                or len(set(indices)) != len(indices)
                or np.any(indices < 0)
                or np.any(indices >= len(objects))
            ):
                raise ValueError("Refinement common-corner support is invalid")
            paths.append(root / sensor["folder"] / match["source_rgb"])
            rows.append(
                {
                    "frame_id": frame,
                    "source_frame_id": source_id,
                    "motion": match["motion"],
                    "robot_ee_pose": match["robot_ee_pose"],
                    "G": robot_ee_to_reference(match["robot_ee_pose"]),
                    "vision_pose": transform_from_record(
                        observation["target_to_camera"]
                    ),
                    "objects": objects[indices].astype(float),
                    "pixels": pixels[indices].astype(float),
                    "preparation_frame_id": support["frame_id"],
                    "reserved": support["frame_id"] in intrinsic[key],
                }
            )
        projection = profile.intrinsics
        cameras.append(
            {
                "sensor_key": key,
                "profile": profile,
                "rows": rows,
                "K": np.asarray(projection.cam_k).reshape(3, 3),
                "D": np.asarray(projection.distortion),
                "X": transform_from_record(
                    {
                        "rotation_quaternion_wxyz": profile.extrinsics.rotation_quaternion_wxyz,
                        "translation_mm": profile.extrinsics.translation_mm,
                    }
                ),
                "Y": transform_from_record(profile.metadata["companion_transform"]),
            }
        )
    return cameras, [_binding(root, path) for path in sorted(set(paths))]


def _sample(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row["reserved"] and not str(row["motion"]).endswith("_settled"):
            groups[str(row["motion"])].append(row)
    result = []
    for motion in sorted(groups):
        group = groups[motion]
        indices = np.unique(
            np.linspace(
                0, len(group) - 1, min(POLICY["frames_per_motion"], len(group))
            ).astype(int)
        )
        result.extend(group[i] for i in indices)
    return result


def _delta(values: np.ndarray) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, :3] = cv2.Rodrigues(values[:3])[0]
    matrix[:3, 3] = values[3:]
    return matrix


def _fit(
    cameras: Sequence[Mapping[str, Any]], groups: Sequence[Sequence[Mapping[str, Any]]]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    xs, ys, packed = [], [], []
    for camera, rows in zip(cameras, groups, strict=True):
        if len({r["motion"] for r in rows}) < 6:
            raise ValueError(
                "Refinement training requires at least six distinct motions per camera"
            )
        observations = [
            {
                "robot_ee_pose": r["robot_ee_pose"],
                "target_to_camera": transform_record(
                    r["vision_pose"], from_frame="aruco_grid", to_frame="camera"
                ),
            }
            for r in rows
        ]
        x, y = solve_extrinsic(observations, mode="eye_in_hand", method="tsai")
        xs.append(x)
        ys.append(y)
        counts = [len(r["objects"]) for r in rows]
        packed.append(
            {
                "objects": np.concatenate([r["objects"] for r in rows]),
                "pixels": np.concatenate([r["pixels"] for r in rows]),
                "gi": np.repeat(
                    [invert_transform(r["G"]) for r in rows], counts, axis=0
                ),
                "weights": np.concatenate([np.full(n, 1 / np.sqrt(n)) for n in counts]),
            }
        )
    shared = average_transform(ys)
    seed = {"xs": xs, "y": shared}
    size = 6 * (len(cameras) + 1)

    def decode(parameters: np.ndarray) -> dict[str, Any]:
        return {
            "y": shared @ _delta(parameters[:6]),
            "xs": [
                x @ _delta(parameters[6 + i * 6 : 12 + i * 6]) for i, x in enumerate(xs)
            ],
        }

    def residual(parameters: np.ndarray) -> np.ndarray:
        model = decode(parameters)
        values = []
        for camera, data, x in zip(cameras, packed, model["xs"], strict=True):
            base = data["objects"] @ model["y"][:3, :3].T + model["y"][:3, 3]
            flange = (
                np.einsum("nij,nj->ni", data["gi"][:, :3, :3], base)
                + data["gi"][:, :3, 3]
            )
            xi = invert_transform(x)
            points = flange @ xi[:3, :3].T + xi[:3, 3]
            if np.any(points[:, 2] <= 0):
                raise ValueError("Refinement projected target behind camera")
            uv = cv2.projectPoints(
                points, np.zeros(3), np.zeros(3), camera["K"], camera["D"]
            )[0].reshape(-1, 2)
            values.append(
                ((uv - data["pixels"]) * data["weights"][:, None]).reshape(-1)
            )
        return np.concatenate(values)

    sparsity = lil_matrix((sum(2 * len(d["pixels"]) for d in packed), size), dtype=int)
    offset = 0
    for i, data in enumerate(packed):
        stop = offset + 2 * len(data["pixels"])
        sparsity[offset:stop, :6] = 1
        sparsity[offset:stop, 6 + 6 * i : 12 + 6 * i] = 1
        offset = stop
    upper = np.tile(
        [POLICY["maximum_rotation_adjustment_rad"]] * 3
        + [POLICY["maximum_translation_adjustment_mm"]] * 3,
        len(cameras) + 1,
    )
    result = least_squares(
        residual,
        np.zeros(size),
        bounds=(-upper, upper),
        x_scale=np.tile([0.01] * 3 + [5.0] * 3, len(cameras) + 1),
        loss=POLICY["robust_loss"],
        f_scale=POLICY["weighted_loss_scale"],
        max_nfev=POLICY["maximum_function_evaluations"],
        jac_sparsity=sparsity.tocsr(),
        ftol=1e-8,
        xtol=1e-8,
        gtol=1e-8,
    )
    if (
        not result.success
        or np.any(result.active_mask)
        or not np.all(np.isfinite(result.x))
    ):
        raise ValueError(
            "Joint refinement did not converge within its adjustment bounds"
        )
    return (
        decode(result.x),
        seed,
        {
            "success": True,
            "nfev": int(result.nfev),
            "cost": float(result.cost),
            "optimality": float(result.optimality),
            "active_bounds": result.active_mask.tolist(),
        },
    )


def _errors(
    camera: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    x: np.ndarray,
    y: np.ndarray,
) -> list[float]:
    values = []
    for row in rows:
        pose = invert_transform(row["G"] @ x) @ y
        points = row["objects"] @ pose[:3, :3].T + pose[:3, 3]
        if np.any(points[:, 2] <= 0):
            raise ValueError("Refinement validation projected target behind camera")
        uv = cv2.projectPoints(
            points, np.zeros(3), np.zeros(3), camera["K"], camera["D"]
        )[0].reshape(-1, 2)
        values.append(
            float(np.sqrt(np.mean(np.sum((uv - row["pixels"]) ** 2, axis=1))))
        )
    return values


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values or not np.all(np.isfinite(values)):
        raise ValueError("Refinement has missing or invalid validation residuals")
    return {
        "count": len(values),
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "p95": float(np.percentile(values, 95)),
        "max": float(np.max(values)),
    }


def _compute(cameras: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups = [_sample(c["rows"]) for c in cameras]
    common = sorted(
        set.intersection(*[{r["motion"] for r in group} for group in groups])
    )
    if len(common) < POLICY["minimum_common_motions"]:
        raise ValueError("Refinement requires at least nine shared motion groups")
    # Restrict the CV training pool to the common set. Every tested motion is
    # absent from every camera's training data in that fold, including seeds.
    groups = [[r for r in group if r["motion"] in common] for group in groups]
    folds = []
    audited = [{"before": [], "after": []} for _ in cameras]
    for fold in range(POLICY["motion_folds"]):
        heldout = set(common[fold :: POLICY["motion_folds"]])
        training = [[r for r in rows if r["motion"] not in heldout] for rows in groups]
        validation = [[r for r in rows if r["motion"] in heldout] for rows in groups]
        fitted, seed, optimization = _fit(cameras, training)
        sensor_results = []
        for i, (camera, rows) in enumerate(zip(cameras, validation, strict=True)):
            before = _errors(camera, rows, seed["xs"][i], seed["y"])
            after = _errors(camera, rows, fitted["xs"][i], fitted["y"])
            audited[i]["before"].extend(before)
            audited[i]["after"].extend(after)
            sensor_results.append(
                {
                    "sensor_key": camera["sensor_key"],
                    "before": _stats(before),
                    "after": _stats(after),
                }
            )
        folds.append(
            {
                "fold": fold,
                "training_motions": sorted(set(common) - heldout),
                "validation_motions": sorted(heldout),
                "optimization": optimization,
                "sensors": sensor_results,
            }
        )
    fitted, _, optimization = _fit(cameras, groups)
    original_shared = average_transform([c["Y"] for c in cameras])
    sensor_results = []
    checks = []
    for i, camera in enumerate(cameras):
        rows = camera["rows"]
        after = _stats(_errors(camera, rows, fitted["xs"][i], fitted["y"]))
        before_shared = _stats(_errors(camera, rows, camera["X"], original_shared))
        before_own = _stats(_errors(camera, rows, camera["X"], camera["Y"]))
        audit_before, audit_after = (
            _stats(audited[i]["before"]),
            _stats(audited[i]["after"]),
        )
        reserved = [r for r in rows if r["reserved"]]
        sensor_results.append(
            {
                "sensor_key": camera["sensor_key"],
                "camera_to_robot_flange": transform_record(
                    fitted["xs"][i], from_frame="camera", to_frame="robot_flange"
                ),
                "delta_vs_seed": transform_residual(camera["X"], fitted["xs"][i]),
                "all_frames": {
                    "seed_shared": before_shared,
                    "seed_own": before_own,
                    "refined": after,
                },
                "motion_disjoint_audit": {"before": audit_before, "after": audit_after},
                "reserved_intrinsic_views": {
                    "frame_ids": [r["frame_id"] for r in reserved],
                    "before": _stats(
                        _errors(camera, reserved, camera["X"], original_shared)
                    ),
                    "after": _stats(
                        _errors(camera, reserved, fitted["xs"][i], fitted["y"])
                    ),
                }
                if reserved
                else None,
                "training_source_frames": [r["source_frame_id"] for r in groups[i]],
                "training_motions": common,
            }
        )
        for name, actual, baseline in (
            (
                "motion_disjoint_mean_improvement",
                audit_after["mean"],
                audit_before["mean"],
            ),
            (
                "all_frame_shared_median_improvement",
                after["median"],
                before_shared["median"],
            ),
            ("all_frame_own_median_improvement", after["median"], before_own["median"]),
        ):
            checks.append(
                {
                    "sensor_key": camera["sensor_key"],
                    "name": name,
                    "status": "ok" if actual < baseline else "error",
                    "actual_px": actual,
                    "baseline_px": baseline,
                }
            )
    return {
        "status": "passing" if all(c["status"] == "ok" for c in checks) else "failed",
        "grid_to_template_base": transform_record(
            fitted["y"], from_frame="aruco_grid", to_frame="template_base"
        ),
        "optimization": optimization,
        "motion_folds": folds,
        "sensors": sensor_results,
        "checks": checks,
    }


def create_reprojection_refinement(
    run_root: str | Path, attempt_id: str
) -> dict[str, Any]:
    from posetestbot.calibration.attempts import (
        calibration_attempt_root,
        load_calibration_attempt,
        _promotion_selections,
    )

    root = Path(run_root).resolve()
    attempt = load_calibration_attempt(root, attempt_id)
    selections = _promotion_selections(attempt, None)
    cameras, bindings = _inputs(root, attempt, selections)
    result = _compute(cameras)
    refinement_id = uuid.uuid4().hex
    report = {
        "schema_version": SCHEMA_VERSION,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "attempt_id": attempt_id,
        "refinement_id": refinement_id,
        "target_id": attempt["request"]["target_id"],
        "geometry_sha256": attempt["request"]["target"]["geometry_sha256"],
        "policy": POLICY,
        "selections": selections,
        "input_bindings": bindings,
        "intrinsics": "fixed_to_selected_attempt_profiles",
        "limits": "Recorded reprojection audit; not independent absolute-pose metrology. Timing, corner-support masks, and intrinsic selection precede the motion split. Settled frames and reserved intrinsic views are excluded from transform fitting; every fold refits its seed without the tested motions.",
        **result,
    }
    path = refinement_report_path(
        calibration_attempt_root(root, attempt_id), refinement_id
    )
    path.parent.mkdir(parents=True, exist_ok=False)
    atomic_write_json(path, report)
    return report


def validate_reprojection_refinement(
    run_root: str | Path,
    attempt: Mapping[str, Any],
    refinement_id: str,
    selections: Mapping[str, str],
    *,
    expected_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from posetestbot.calibration.attempts import calibration_attempt_root

    root = Path(run_root).resolve()
    attempt_id = str(attempt["attempt_id"])
    path = refinement_report_path(
        calibration_attempt_root(root, attempt_id), refinement_id
    )
    if expected_binding is not None and _binding(root, path) != dict(expected_binding):
        raise ValueError(
            "Reprojection refinement changed after promotion was requested"
        )
    report = _read(path)
    if (
        report.get("schema_version") != SCHEMA_VERSION
        or report.get("implementation_revision") != IMPLEMENTATION_REVISION
        or report.get("policy") != POLICY
        or report.get("attempt_id") != attempt_id
        or report.get("refinement_id") != refinement_id
        or report.get("selections") != dict(selections)
        or report.get("target_id") != attempt["request"]["target_id"]
        or report.get("geometry_sha256")
        != attempt["request"]["target"]["geometry_sha256"]
    ):
        raise ValueError("Reprojection refinement provenance is inconsistent")
    cameras, bindings = _inputs(root, attempt, selections)
    if bindings != report.get("input_bindings"):
        raise ValueError("Reprojection refinement inputs changed")
    reproduced = _compute(cameras)
    # Runtime duration/timestamps are deliberately absent: deterministic inputs,
    # transforms, residuals, optimizer state, folds and checks must all reproduce.
    if (
        any(report.get(key) != value for key, value in reproduced.items())
        or reproduced["status"] != "passing"
    ):
        raise ValueError("Reprojection refinement failed to reproduce passing evidence")
    return _binding(root, path)


def apply_reprojection_refinement(
    root: Path,
    attempt: Mapping[str, Any],
    profiles: Sequence[CalibrationProfile],
    evidence: Mapping[str, Any],
    selections: Mapping[str, str],
) -> list[CalibrationProfile]:
    from posetestbot.calibration.attempts import calibration_attempt_root

    refinement_id = str(evidence["refinement_id"])
    binding = validate_reprojection_refinement(
        root, attempt, refinement_id, selections, expected_binding=evidence["report"]
    )
    report = _read(
        refinement_report_path(
            calibration_attempt_root(root, str(attempt["attempt_id"])), refinement_id
        )
    )
    by_sensor = {s["sensor_key"]: s for s in report["sensors"]}
    result = []
    for profile in profiles:
        sensor = by_sensor[str(profile.metadata["sensor_key"])]
        transform = sensor["camera_to_robot_flange"]
        result.append(
            replace(
                profile,
                profile_id=f"{profile.profile_id}_joint_{refinement_id[:8]}",
                method=f"{profile.method}+{IMPLEMENTATION_REVISION}",
                extrinsics=RigidTransform(
                    from_frame=TransformFrame.CAMERA,
                    to_frame=TransformFrame.ROBOT_FLANGE,
                    rotation_quaternion_wxyz=tuple(
                        transform["rotation_quaternion_wxyz"]
                    ),
                    translation_mm=tuple(transform["translation_mm"]),
                ),
                quality=replace(
                    profile.quality,
                    mean_reprojection_error_px=sensor["motion_disjoint_audit"]["after"][
                        "mean"
                    ],
                    notes="Joint fixed-intrinsic pixel refinement. Translation/rotation quality fields describe the passing seed's leave-one-pose-out audit; refined optical validation is in metadata.reprojection_refinement.",
                ),
                metadata={
                    **profile.metadata,
                    "companion_transform": report["grid_to_template_base"],
                    "seed_companion_transform": profile.metadata["companion_transform"],
                    "reprojection_refinement": {
                        "refinement_id": refinement_id,
                        "report": binding,
                        "implementation_revision": IMPLEMENTATION_REVISION,
                        "motion_disjoint_audit": sensor["motion_disjoint_audit"],
                        "seed_extrinsics": transform_record(
                            transform_from_record(
                                {
                                    "rotation_quaternion_wxyz": profile.extrinsics.rotation_quaternion_wxyz,
                                    "translation_mm": profile.extrinsics.translation_mm,
                                }
                            ),
                            from_frame="camera",
                            to_frame="robot_flange",
                        ),
                    },
                },
            )
        )
    return result
