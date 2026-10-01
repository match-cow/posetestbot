"""Estimate capture/pose latency from reversible, nearly optical-axis spins.

Timing is fitted from angular motion, independently of hand-eye translation
residual materiality. Near-optical-axis rotations avoid relying on the weak
out-of-plane component of planar PnP. A shared angular mapping across forward
and reverse traversals absorbs small projection biases without absorbing lag.
Raw timestamps and poses are never modified. Interpolation is used only for
estimation; accepted offsets still use the authoritative nearest-pose writer.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.spatial.transform import Rotation


STRATEGY = "optical_axis_spin_block_bootstrap.v1"
MIN_OPTICAL_ALIGNMENT = 0.85
MAX_AXIS_SINGULAR_RATIO = 0.05
MIN_ROTATION_DEG = 5.0
MAX_TRANSLATION_SPAN_MM = 0.5
MIN_VIEWS_PER_MOTION = 20
MAX_VIEWS_PER_MOTION = 300
MIN_MOTION_COUNT = 3
MAX_MOTION_COUNT = 6
MIN_SHARED_AXIS_ALIGNMENT = 0.98
MAX_SHARED_AXIS_DEVIATION_DEG = 0.5
MAX_INTERPOLATION_GAP_MS = 40.0
BOOTSTRAP_BLOCK_SECONDS = 0.5
BOOTSTRAP_REPLICATES = 200
BOOTSTRAP_SEED = 37


def configuration() -> dict[str, Any]:
    return {
        "strategy": STRATEGY,
        "minimum_optical_axis_alignment": MIN_OPTICAL_ALIGNMENT,
        "maximum_axis_singular_ratio": MAX_AXIS_SINGULAR_RATIO,
        "minimum_rotation_deg": MIN_ROTATION_DEG,
        "maximum_translation_span_mm": MAX_TRANSLATION_SPAN_MM,
        "minimum_views_per_motion": MIN_VIEWS_PER_MOTION,
        "maximum_views_per_motion": MAX_VIEWS_PER_MOTION,
        "minimum_motion_count": MIN_MOTION_COUNT,
        "maximum_motion_count": MAX_MOTION_COUNT,
        "minimum_shared_axis_alignment": MIN_SHARED_AXIS_ALIGNMENT,
        "maximum_shared_axis_deviation_deg": MAX_SHARED_AXIS_DEVIATION_DEG,
        "maximum_interpolation_gap_ms": MAX_INTERPOLATION_GAP_MS,
        "bootstrap_block_seconds": BOOTSTRAP_BLOCK_SECONDS,
        "bootstrap_replicates": BOOTSTRAP_REPLICATES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "angular_mapping_degrees": [1, 2, 3],
        "timestamp_uncertainty_floor": "half_median_robot_sample_period",
    }


def _principal_axis(vectors: np.ndarray) -> tuple[np.ndarray, float]:
    _, singular, axes = np.linalg.svd(
        vectors - vectors.mean(axis=0), full_matrices=False
    )
    ratio = float(singular[1] / singular[0]) if singular[0] > 1e-12 else float("inf")
    axis = axes[0]
    # Fix the otherwise arbitrary SVD sign for reproducible retained evidence.
    if axis[np.argmax(abs(axis))] < 0:
        axis = -axis
    return axis, ratio


def _valid_queries(times: np.ndarray, queries: np.ndarray) -> np.ndarray:
    high = np.searchsorted(times, queries)
    valid = (high > 0) & (high < len(times))
    lower = np.clip(high - 1, 0, len(times) - 1)
    upper = np.clip(high, 0, len(times) - 1)
    return valid & ((times[upper] - times[lower]) <= MAX_INTERPOLATION_GAP_MS / 1000)


def estimate_rotational_timing(
    observations: Sequence[Mapping[str, Any]],
    *,
    robot_records: Sequence[Mapping[str, Any]],
    offsets_ms: Sequence[float],
    maximum_uncertainty_ms: float,
) -> dict[str, Any]:
    """Return a measured latency plus reproducible uncertainty, or its absence."""

    unavailable = {
        "strategy": STRATEGY,
        "status": "unavailable",
        "configuration": configuration(),
    }
    timestamps = np.asarray(
        [int(record["timestamp_ns"]) for record in robot_records], dtype=np.int64
    )
    if len(timestamps) < 3 or np.any(np.diff(timestamps) <= 0):
        return {**unavailable, "reason": "strictly_ordered_robot_samples_required"}
    epoch = int(timestamps[0])
    times = (timestamps - epoch) / 1e9
    poses = [record["pose"] for record in robot_records]
    rotations = Rotation.from_euler(
        "ZYX", [[float(pose[key]) for key in ("A", "B", "C")] for pose in poses]
    )
    translations = np.asarray(
        [[float(pose[key]) for key in ("X", "Y", "Z")] for pose in poses]
    )
    robot_by_motion: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(robot_records):
        robot_by_motion[str(record["motion"])].append(index)
    observations_by_motion: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        observations_by_motion[str(observation["motion"])].append(observation)
    grid = np.asarray(offsets_ms, dtype=float) / 1000
    candidates = []
    diagnostics = []
    for motion, indices in sorted(robot_by_motion.items()):
        selected = np.asarray(indices)
        if len(selected) < 3:
            continue
        translation_span = float(
            np.max(
                np.linalg.norm(
                    translations[selected] - translations[selected[0]], axis=1
                )
            )
        )
        vectors = (rotations[selected] * rotations[selected[0]].inv()).as_rotvec()
        robot_axis, robot_ratio = _principal_axis(vectors)
        angular_span = float(np.degrees(np.ptp(vectors @ robot_axis)))
        if (
            translation_span > MAX_TRANSLATION_SPAN_MM
            or angular_span < MIN_ROTATION_DEG
            or robot_ratio > MAX_AXIS_SINGULAR_RATIO
        ):
            continue
        source = sorted(
            observations_by_motion.get(motion, ()),
            key=lambda item: (
                int(item["image_timestamp_ns"]),
                str(item.get("source_frame_id") or item["frame_id"]),
            ),
        )
        motion_times = times[selected]
        eligible = []
        for item in source:
            frame_time = (int(item["image_timestamp_ns"]) - epoch) / 1e9
            endpoints = frame_time + np.array([min(grid), max(grid)])
            if not np.all(_valid_queries(motion_times, endpoints)):
                continue
            low, high = np.searchsorted(motion_times, endpoints)
            # Validate the continuous query interval, including positions between
            # coarse grid points used by the continuous refinement.
            if (
                np.max(np.diff(motion_times[low - 1 : high + 1]))
                > MAX_INTERPOLATION_GAP_MS / 1000
            ):
                continue
            eligible.append(item)
        if len(eligible) > MAX_VIEWS_PER_MOTION:
            eligible = [
                eligible[index]
                for index in np.linspace(0, len(eligible) - 1, MAX_VIEWS_PER_MOTION)
                .round()
                .astype(int)
            ]
        if len(eligible) < MIN_VIEWS_PER_MOTION:
            diagnostics.append(
                {
                    "motion": motion,
                    "reason": "insufficient_full_range_views",
                    "view_count": len(eligible),
                }
            )
            continue
        camera_rotations = Rotation.from_matrix(
            np.asarray([item["target_to_camera"]["matrix"] for item in eligible])[
                :, :3, :3
            ]
        )
        camera_vectors = (camera_rotations * camera_rotations[0].inv()).as_rotvec()
        camera_axis, camera_ratio = _principal_axis(camera_vectors)
        camera_span_deg = float(np.degrees(np.ptp(camera_vectors @ camera_axis)))
        optical_alignment = abs(float(camera_axis[2]))
        if camera_span_deg < MIN_ROTATION_DEG:
            diagnostics.append(
                {
                    "motion": motion,
                    "reason": "insufficient_observed_rotation",
                    "observed_rotation_span_deg": camera_span_deg,
                }
            )
            continue
        if (
            optical_alignment < MIN_OPTICAL_ALIGNMENT
            or camera_ratio > MAX_AXIS_SINGULAR_RATIO
        ):
            diagnostics.append(
                {
                    "motion": motion,
                    "reason": "rotation_not_near_optical_axis",
                    "optical_axis_alignment": optical_alignment,
                    "axis_singular_ratio": camera_ratio,
                }
            )
            continue
        candidates.append(
            {
                "motion": motion,
                "indices": selected,
                "observations": eligible,
                "robot_axis": robot_axis,
                "camera_axis": camera_axis,
                "optical_axis_alignment": optical_alignment,
                "axis_singular_ratio": camera_ratio,
                "observed_rotation_span_deg": camera_span_deg,
            }
        )
    # A common axis and flange location give one shared spatial mapping. Motion
    # selection uses geometry, before evaluating any offset objective.
    clusters = []
    for candidate in candidates:
        for cluster in clusters:
            anchor = cluster[0]
            relative = (
                rotations[candidate["indices"]] * rotations[anchor["indices"][0]].inv()
            ).as_rotvec()
            perpendicular = relative - np.outer(
                relative @ anchor["robot_axis"], anchor["robot_axis"]
            )
            if (
                abs(np.dot(anchor["robot_axis"], candidate["robot_axis"]))
                >= MIN_SHARED_AXIS_ALIGNMENT
                and abs(np.dot(anchor["camera_axis"], candidate["camera_axis"]))
                >= MIN_SHARED_AXIS_ALIGNMENT
                and np.max(np.linalg.norm(perpendicular, axis=1))
                <= np.radians(MAX_SHARED_AXIS_DEVIATION_DEG)
                and np.linalg.norm(
                    translations[anchor["indices"][0]]
                    - translations[candidate["indices"][0]]
                )
                <= MAX_TRANSLATION_SPAN_MM
            ):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
    usable = [cluster for cluster in clusters if len(cluster) >= MIN_MOTION_COUNT]
    if not usable:
        return {
            **unavailable,
            "reason": "three_reversible_optical_axis_motions_required",
            "motion_diagnostics": diagnostics,
        }
    cluster = min(
        usable,
        key=lambda group: (
            -min(item["optical_axis_alignment"] for item in group),
            tuple(item["motion"] for item in group),
        ),
    )[:MAX_MOTION_COUNT]
    selected = np.concatenate([item["indices"] for item in cluster])
    selected.sort()
    reference_robot = rotations[selected[0]]
    robot_vectors = (rotations[selected] * reference_robot.inv()).as_rotvec()
    robot_axis, _ = _principal_axis(robot_vectors)
    robot_angles = robot_vectors @ robot_axis
    motion_times = times[selected]
    source = sorted(
        [item for group in cluster for item in group["observations"]],
        key=lambda item: int(item["image_timestamp_ns"]),
    )
    frame_times = np.asarray(
        [(int(item["image_timestamp_ns"]) - epoch) / 1e9 for item in source]
    )
    camera_rotations = Rotation.from_matrix(
        np.asarray([item["target_to_camera"]["matrix"] for item in source])[:, :3, :3]
    )
    camera_vectors = (camera_rotations * camera_rotations[0].inv()).as_rotvec()
    camera_axis, _ = _principal_axis(camera_vectors)
    camera_angles = camera_vectors @ camera_axis
    motion_ids = np.asarray([str(item["motion"]) for item in source])
    names = sorted(set(motion_ids))
    directions = []
    for group in cluster:
        indices = group["indices"]
        change = (
            rotations[indices[-1]] * rotations[indices[0]].inv()
        ).as_rotvec() @ robot_axis
        directions.append(1 if change > 0 else -1)
    if len(set(directions)) != 2:
        return {
            **unavailable,
            "reason": "opposite_rotation_directions_required",
            "motion_diagnostics": diagnostics,
        }
    weights = np.zeros(len(source))
    for name in names:
        weights[motion_ids == name] = 1.0 / np.sum(motion_ids == name)

    def loss(
        offset: float, selection: np.ndarray | None = None, degree: int = 3
    ) -> float:
        angle = np.interp(frame_times + offset, motion_times, robot_angles)
        normalized = (angle - np.mean(angle)) / np.std(angle)
        design = np.polynomial.polynomial.polyvander(normalized, degree)
        if selection is None:
            selection = np.arange(len(source))
        root_weights = np.sqrt(weights[selection])
        coefficients = np.linalg.lstsq(
            design[selection] * root_weights[:, None],
            camera_angles[selection] * root_weights,
            rcond=None,
        )[0]
        errors = camera_angles[selection] - design[selection] @ coefficients
        return float(
            np.sum(weights[selection] * errors**2) / np.sum(weights[selection])
        )

    def estimate(selection: np.ndarray | None = None, degree: int = 3) -> float:
        values = [loss(offset, selection, degree) for offset in grid]
        best_index = int(np.argmin(values))
        lower = grid[max(0, best_index - 1)]
        upper = grid[min(len(grid) - 1, best_index + 1)]
        return float(
            minimize_scalar(
                lambda offset: loss(offset, selection, degree),
                bounds=(lower, upper),
                method="bounded",
                options={"xatol": 1e-8},
            ).x
        )

    curve = [
        {
            "robot_pose_time_offset_ms": float(offset * 1000),
            "angular_mse_rad2": loss(float(offset)),
        }
        for offset in grid
    ]
    measured = estimate()
    block_groups = []
    for name in names:
        indices = np.where(motion_ids == name)[0]
        block_ids = np.floor(
            (frame_times[indices] - frame_times[indices[0]]) / BOOTSTRAP_BLOCK_SECONDS
        ).astype(int)
        block_groups.append(
            [indices[block_ids == block] for block in np.unique(block_ids)]
        )
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    bootstrap = []
    for _ in range(BOOTSTRAP_REPLICATES):
        sampled = np.concatenate(
            [
                np.concatenate(
                    [
                        blocks[index]
                        for index in rng.choice(len(blocks), len(blocks), replace=True)
                    ]
                )
                for blocks in block_groups
            ]
        )
        bootstrap.append(estimate(sampled) * 1000)
    held_out = [
        {
            "held_out_motion": name,
            "robot_pose_time_offset_ms": estimate(np.where(motion_ids != name)[0])
            * 1000,
        }
        for name in names
    ]
    models = [
        {
            "angular_mapping_degree": degree,
            "robot_pose_time_offset_ms": estimate(degree=degree) * 1000,
        }
        for degree in (1, 2, 3)
    ]
    statistical_interval = np.quantile(bootstrap, [0.025, 0.975])
    sample_period_ms = float(np.median(np.diff(times)) * 1000)
    alternatives = [item["robot_pose_time_offset_ms"] for item in (*held_out, *models)]
    interval = [
        float(min(statistical_interval[0], *alternatives) - sample_period_ms / 2),
        float(max(statistical_interval[1], *alternatives) + sample_period_ms / 2),
    ]
    nearest_grid = min(
        offsets_ms,
        key=lambda offset: (abs(offset - measured * 1000), abs(offset), offset),
    )
    identified = (
        nearest_grid != 0.0
        and interval[1] - interval[0] <= maximum_uncertainty_ms
        and (interval[0] > 0 or interval[1] < 0)
        and interval[0] > min(offsets_ms)
        and interval[1] < max(offsets_ms)
    )
    return {
        "strategy": STRATEGY,
        "configuration": configuration(),
        "status": "identified" if identified else "inconclusive",
        "reason": "nonzero_latency_identified_from_reversible_optical_axis_motion"
        if identified
        else "timing_uncertainty_or_search_resolution_inconclusive",
        "estimated_robot_pose_time_offset_ms": float(measured * 1000),
        "candidate_robot_pose_time_offset_ms": float(nearest_grid),
        "confidence_interval_ms": interval,
        "bootstrap_percentile_interval_ms": statistical_interval.tolist(),
        "maximum_uncertainty_ms": float(maximum_uncertainty_ms),
        "median_robot_sample_period_ms": sample_period_ms,
        "held_out_motions": held_out,
        "mapping_sensitivity": models,
        "zero_offset_angular_mse_rad2": loss(0.0),
        "estimated_offset_angular_mse_rad2": loss(measured),
        "view_count": len(source),
        "motion_count": len(names),
        "motions": [
            {
                "motion": group["motion"],
                "direction": direction,
                "view_count": len(group["observations"]),
                "optical_axis_alignment": group["optical_axis_alignment"],
                "axis_singular_ratio": group["axis_singular_ratio"],
                "observed_rotation_span_deg": group["observed_rotation_span_deg"],
            }
            for group, direction in zip(cluster, directions, strict=True)
        ],
        "source_frames": [
            {
                "source_frame_id": str(item.get("source_frame_id") or item["frame_id"]),
                "image_timestamp_ns": int(item["image_timestamp_ns"]),
            }
            for item in source
        ],
        "curve": curve,
    }
