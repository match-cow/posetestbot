"""Evidence-backed camera/robot time-offset estimation for calibration attempts.

The optimizer estimates an *effective latency*.  It does not claim that the
camera and robot clocks are synchronized, and it never rewrites raw timestamp
evidence.  A positive ``robot_pose_time_offset_ms`` pairs a camera frame at
time ``t`` with a robot pose at ``t + offset``.  This is intentionally the
opposite sign of the dataset synchronizer's ``sync_delta_ms`` convention.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from statistics import median
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from posetestbot.calibration.attempt_solver import (
    solve_extrinsic_consensus,
)
from posetestbot.calibration.transforms import (
    invert_transform,
    residual_summary,
    robot_ee_to_reference,
    transform_from_record,
    transform_residual,
)


SCHEMA_VERSION = "calibration_time_offset_search.v1"
IMPLEMENTATION_REVISION = "constant_latency_nearest_pose_motion_lomo_warn_keep_zero.v5"
SUPPORTED_IMPLEMENTATION_REVISIONS = frozenset({IMPLEMENTATION_REVISION})
POLICIES = ("auto_offset", "fixed_zero")
DEFAULT_POLICY = "fixed_zero"
DEFAULT_MIN_OFFSET_MS = -300.0
DEFAULT_MAX_OFFSET_MS = 300.0
DEFAULT_STEP_MS = 5.0
DEFAULT_REFERENCE_METHODS = ("shah", "li")
DEFAULT_REFERENCE_PNP_METHOD = "IPPE"
DEFAULT_MAX_NEAREST_POSE_DELTA_MS = 150.0
DEFAULT_WARNING_NEAREST_POSE_DELTA_MS = 20.0
DEFAULT_WARNING_ABS_OFFSET_MS = 150.0
DEFAULT_MAX_OBSERVATIONS_PER_MOTION = 6
DEFAULT_MAX_SEARCH_MOTIONS = 18
DEFAULT_MIN_MOTIONS_PER_FOLD = 4
DEFAULT_MIN_ABSOLUTE_IMPROVEMENT_MM = 0.25
DEFAULT_MIN_RELATIVE_IMPROVEMENT = 0.10
DEFAULT_MAX_ROTATION_DEGRADATION_DEG = 0.10
DEFAULT_MIN_OFFSET_STABILITY_MS = 20.0
DEFAULT_MAX_LOMO_SEARCH_ADJUSTED_SIGN_P_VALUE = 0.05
IMPROVEMENT_EVIDENCE_STRATEGY = "leave_one_motion_out_consistency"
LOMO_CONSISTENCY_STRATEGY = "leave_one_motion_out_candidate_consistency_bonferroni.v1"
FAILURE_POLICY_WARN_KEEP_ZERO = "warn_keep_zero"


def offset_values(
    minimum_ms: float = DEFAULT_MIN_OFFSET_MS,
    maximum_ms: float = DEFAULT_MAX_OFFSET_MS,
    step_ms: float = DEFAULT_STEP_MS,
) -> list[float]:
    """Return an inclusive, deterministic offset grid."""

    if not all(math.isfinite(item) for item in (minimum_ms, maximum_ms, step_ms)):
        raise ValueError("time-offset search bounds and step must be finite")
    if minimum_ms >= maximum_ms:
        raise ValueError("time-offset search minimum must be below its maximum")
    if step_ms <= 0.0:
        raise ValueError("time-offset search step must be positive")
    count = int(math.floor((maximum_ms - minimum_ms) / step_ms + 1e-9))
    values = [round(minimum_ms + index * step_ms, 9) for index in range(count + 1)]
    if not math.isclose(values[-1], maximum_ms, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("time-offset search step must exactly span the bounds")
    if not any(math.isclose(item, 0.0, abs_tol=1e-9) for item in values):
        raise ValueError("time-offset search grid must include 0 ms")
    return values


def _nearest_robot_pose(
    query_timestamp_ns: int,
    robot_records: Sequence[Mapping[str, Any]],
    robot_timestamps_ns: Sequence[int],
) -> Mapping[str, Any]:
    index = bisect.bisect_left(robot_timestamps_ns, query_timestamp_ns)
    candidates = {
        max(0, min(len(robot_records) - 1, index - 1)),
        max(0, min(len(robot_records) - 1, index)),
    }
    return min(
        (robot_records[item] for item in candidates),
        key=lambda record: (
            abs(int(record["timestamp_ns"]) - query_timestamp_ns),
            int(record.get("pose_index", 0)),
        ),
    )


def _match_observation(
    observation: Mapping[str, Any],
    *,
    robot_pose_time_offset_ms: float,
    robot_records: Sequence[Mapping[str, Any]],
    robot_timestamps_ns: Sequence[int],
    max_nearest_pose_delta_ms: float,
) -> dict[str, Any] | None:
    try:
        frame_timestamp_ns = int(observation["image_timestamp_ns"])
    except (KeyError, TypeError, ValueError):
        return None
    query_timestamp_ns = frame_timestamp_ns + round(
        robot_pose_time_offset_ms * 1_000_000.0
    )
    closest = _nearest_robot_pose(
        query_timestamp_ns,
        robot_records,
        robot_timestamps_ns,
    )
    nearest_delta_ns = int(closest["timestamp_ns"]) - query_timestamp_ns
    expected_motion = str(observation.get("motion") or "")
    if (
        not expected_motion
        or str(closest.get("motion") or "") != expected_motion
        or abs(nearest_delta_ns) > round(max_nearest_pose_delta_ms * 1_000_000.0)
        or not isinstance(closest.get("pose"), Mapping)
    ):
        return None
    return {
        **dict(observation),
        "motion": expected_motion,
        "robot_ee_pose": dict(closest["pose"]),
        "robot_pose_time_offset_ms": float(robot_pose_time_offset_ms),
        "sync_delta_ms": float(-robot_pose_time_offset_ms),
        "timestamp_alignment": {
            "frame_timestamp_ns": frame_timestamp_ns,
            "robot_pose_query_timestamp_ns": query_timestamp_ns,
            "robot_pose_time_offset_ms": float(robot_pose_time_offset_ms),
            "sync_delta_ms": float(-robot_pose_time_offset_ms),
            "matched_robot_pose_index": int(closest.get("pose_index", 0)),
            "robot_timestamp_ns": int(closest["timestamp_ns"]),
            "nearest_robot_delta_ns": nearest_delta_ns,
        },
    }


def _evenly_spaced(
    values: Sequence[Mapping[str, Any]],
    maximum: int,
) -> list[Mapping[str, Any]]:
    if len(values) <= maximum:
        return list(values)
    indices = sorted(
        {round(index * (len(values) - 1) / (maximum - 1)) for index in range(maximum)}
    )
    return [values[index] for index in indices]


def _fixed_search_split(
    observations: Sequence[Mapping[str, Any]],
    *,
    offsets_ms: Sequence[float],
    robot_records: Sequence[Mapping[str, Any]],
    robot_timestamps_ns: Sequence[int],
    max_nearest_pose_delta_ms: float,
    max_observations_per_motion: int,
    max_search_motions: int,
    min_motions_per_fold: int,
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, Any]]:
    eligible: list[Mapping[str, Any]] = []
    for observation in observations:
        if all(
            _match_observation(
                observation,
                robot_pose_time_offset_ms=offset_ms,
                robot_records=robot_records,
                robot_timestamps_ns=robot_timestamps_ns,
                max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
            )
            is not None
            for offset_ms in offsets_ms
        ):
            eligible.append(observation)

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for observation in eligible:
        grouped[str(observation["motion"])].append(observation)
    for values in grouped.values():
        values.sort(
            key=lambda item: (
                int(item["image_timestamp_ns"]),
                str(item.get("frame_id") or ""),
            )
        )

    usable = {
        motion: _evenly_spaced(values, max_observations_per_motion)
        for motion, values in grouped.items()
        if values
    }
    minimum_motion_count = min_motions_per_fold * 3
    if len(usable) < minimum_motion_count:
        raise ValueError(
            "auto sync requires at least "
            f"{minimum_motion_count} motion groups with frames valid across the "
            f"complete search interval; found {len(usable)}"
        )

    motion_names = sorted(usable)
    if len(motion_names) > max_search_motions:
        selected_motions = _evenly_spaced(
            [{"motion": motion} for motion in motion_names],
            max_search_motions,
        )
        motion_names = [str(item["motion"]) for item in selected_motions]
    fold_motions = {
        fold: [motion for index, motion in enumerate(motion_names) if index % 3 == fold]
        for fold in range(3)
    }
    if any(len(names) < min_motions_per_fold for names in fold_motions.values()):
        raise ValueError(
            "auto sync could not form three motion-disjoint folds with at least "
            f"{min_motions_per_fold} motions each"
        )
    split = {f"fold_{fold}": [] for fold in range(3)}
    per_motion: dict[str, Any] = {}
    for motion in motion_names:
        values = usable[motion]
        fold = next(
            fold for fold, names in fold_motions.items() if motion in set(names)
        )
        destination = f"fold_{fold}"
        split[destination].extend(values)
        per_motion[motion] = {
            "eligible_count": len(grouped[motion]),
            "selected_count": len(values),
            "fold": fold,
        }
    return split, {
        "strategy": "fixed_full_range_motion_disjoint_three_fold_cv_v1",
        "input_observation_count": len(observations),
        "full_range_eligible_observation_count": len(eligible),
        "selected_observation_count": sum(len(usable[item]) for item in motion_names),
        "motion_count": len(motion_names),
        "fold_motion_ids": {str(fold): names for fold, names in fold_motions.items()},
        "fold_motion_counts": {
            str(fold): len(names) for fold, names in fold_motions.items()
        },
        "per_motion": per_motion,
        "frame_ids": {
            name: [str(item.get("frame_id")) for item in items]
            for name, items in split.items()
        },
        "fixed_observations": {
            name: [
                {
                    "observation_id": str(item.get("observation_id") or ""),
                    "preparation_frame_id": str(item.get("frame_id") or ""),
                    "source_frame_id": str(item.get("source_frame_id") or ""),
                    "image_timestamp_ns": int(item["image_timestamp_ns"]),
                }
                for item in items
            ]
            for name, items in split.items()
        },
    }


def _companion_estimate(
    observation: Mapping[str, Any],
    primary: np.ndarray,
    *,
    mode: str,
) -> np.ndarray:
    robot_pose = observation.get("robot_ee_pose")
    target_pose = observation.get("target_to_camera")
    if not isinstance(robot_pose, Mapping) or not isinstance(target_pose, Mapping):
        raise ValueError("time-offset validation requires robot and target transforms")
    robot = robot_ee_to_reference(robot_pose)
    target_camera = transform_from_record(target_pose)
    if mode == "eye_in_hand":
        return robot @ primary @ target_camera
    if mode == "eye_to_hand":
        return invert_transform(robot) @ primary @ target_camera
    raise ValueError("mode must be eye_in_hand or eye_to_hand")


def _evaluate_split(
    training: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
    *,
    offset_ms: float,
    mode: str,
    methods: Sequence[str],
    robot_records: Sequence[Mapping[str, Any]],
    robot_timestamps_ns: Sequence[int],
    max_nearest_pose_delta_ms: float,
) -> dict[str, Any]:
    matched_training = [
        _match_observation(
            item,
            robot_pose_time_offset_ms=offset_ms,
            robot_records=robot_records,
            robot_timestamps_ns=robot_timestamps_ns,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        )
        for item in training
    ]
    matched_validation = [
        _match_observation(
            item,
            robot_pose_time_offset_ms=offset_ms,
            robot_records=robot_records,
            robot_timestamps_ns=robot_timestamps_ns,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        )
        for item in validation
    ]
    if any(item is None for item in (*matched_training, *matched_validation)):
        raise ValueError("fixed auto-sync observation unexpectedly became unmatchable")
    train = [item for item in matched_training if item is not None]
    validate = [item for item in matched_validation if item is not None]
    method_results: dict[str, Any] = {}
    combined_residuals: list[dict[str, float]] = []
    for method in methods:
        try:
            primary, companion = solve_extrinsic_consensus(
                train,
                mode=mode,
                method=method,
            )
            residuals = [
                transform_residual(
                    _companion_estimate(item, primary, mode=mode),
                    companion,
                )
                for item in validate
            ]
        except (cv2.error, TypeError, ValueError, np.linalg.LinAlgError) as exc:
            raise ValueError(
                f"auto-sync reference solver {method} failed at "
                f"{offset_ms:+.1f} ms: {exc}"
            ) from exc
        combined_residuals.extend(residuals)
        method_results[method] = {
            "training_observation_count": len(train),
            "validation_observation_count": len(validate),
            "residuals": residual_summary(residuals),
        }
    return {
        "robot_pose_time_offset_ms": float(offset_ms),
        "training_observation_count": len(train),
        "validation_observation_count": len(validate),
        "validation_motion_count": len({str(item.get("motion")) for item in validate}),
        "residuals": residual_summary(combined_residuals),
        "methods": method_results,
    }


def _best_method_curve_record(
    records: Sequence[Mapping[str, Any]],
    method: str,
) -> Mapping[str, Any]:
    return min(
        records,
        key=lambda item: (
            float(item["methods"][method]["residuals"]["mean_translation_mm"]),
            float(item["methods"][method]["residuals"]["mean_rotation_deg"]),
            abs(float(item["robot_pose_time_offset_ms"])),
            float(item["robot_pose_time_offset_ms"]),
        ),
    )


def _method_optima(
    curve: Sequence[Mapping[str, Any]], methods: Sequence[str]
) -> dict[str, float]:
    result = {}
    for method in methods:
        selected = min(
            curve,
            key=lambda item: (
                float(item["methods"][method]["residuals"]["mean_translation_mm"]),
                float(item["methods"][method]["residuals"]["mean_rotation_deg"]),
                abs(float(item["robot_pose_time_offset_ms"])),
                float(item["robot_pose_time_offset_ms"]),
            ),
        )
        result[method] = float(selected["robot_pose_time_offset_ms"])
    return result


def _improvement(
    baseline: Mapping[str, Any],
    selected: Mapping[str, Any],
) -> dict[str, float | None]:
    zero_translation = float(baseline["residuals"]["mean_translation_mm"])
    selected_translation = float(selected["residuals"]["mean_translation_mm"])
    absolute = zero_translation - selected_translation
    relative = absolute / zero_translation if zero_translation > 0.0 else None
    return {
        "absolute_translation_mm": float(absolute),
        "relative_translation": float(relative) if relative is not None else None,
        "rotation_change_deg": float(
            selected["residuals"]["mean_rotation_deg"]
            - baseline["residuals"]["mean_rotation_deg"]
        ),
    }


def _method_improvement(
    baseline: Mapping[str, Any],
    selected: Mapping[str, Any],
    method: str,
) -> dict[str, float | None]:
    return _improvement(
        {"residuals": baseline["methods"][method]["residuals"]},
        {"residuals": selected["methods"][method]["residuals"]},
    )


def _aggregate_fold_curve(
    curves: Sequence[Sequence[Mapping[str, Any]]],
    *,
    offsets_ms: Sequence[float],
    methods: Sequence[str],
    selection_method: str,
) -> list[dict[str, Any]]:
    aggregated = []
    for offset_index, offset_ms in enumerate(offsets_ms):
        method_results = {}
        for method in methods:
            values = [
                curve[offset_index]["methods"][method]["residuals"] for curve in curves
            ]
            method_results[method] = {
                "outer_fold_count": len(curves),
                "residuals": residual_summary(
                    [
                        {
                            "translation_mm": float(item["mean_translation_mm"]),
                            "rotation_deg": float(item["mean_rotation_deg"]),
                        }
                        for item in values
                    ]
                ),
            }
        aggregated.append(
            {
                "robot_pose_time_offset_ms": float(offset_ms),
                "validation_strategy": "motion_disjoint_cross_validation",
                "outer_fold_count": len(curves),
                "selection_method": selection_method,
                "residuals": method_results[selection_method]["residuals"],
                "methods": method_results,
            }
        )
    return aggregated


def _median_robot_sample_period_ms(
    robot_timestamps_ns: Sequence[int],
) -> float:
    deltas = [
        (right - left) / 1_000_000.0
        for left, right in zip(
            robot_timestamps_ns,
            robot_timestamps_ns[1:],
            strict=False,
        )
        if right > left
    ]
    return float(median(deltas)) if deltas else 0.0


def _positive_sign_p_value(positive_count: int, comparison_count: int) -> float:
    """Return the exact one-sided sign-consistency tail probability."""

    if comparison_count <= 0:
        return 1.0
    if positive_count < 0 or positive_count > comparison_count:
        raise ValueError("positive sign count must be within the comparison count")
    return float(
        sum(
            math.comb(comparison_count, count)
            for count in range(positive_count, comparison_count + 1)
        )
        / (2**comparison_count)
    )


def _leave_one_motion_out_consistency(
    split: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    candidate_offset_ms: float,
    mode: str,
    methods: Sequence[str],
    robot_records: Sequence[Mapping[str, Any]],
    robot_timestamps_ns: Sequence[int],
    max_nearest_pose_delta_ms: float,
    min_absolute_improvement_mm: float,
    min_relative_improvement: float,
    candidate_search_hypothesis_count: int,
    max_search_adjusted_sign_p_value: float,
) -> dict[str, Any]:
    """Audit one selected offset against every motion as its own held-out group."""

    selected = [item for fold in range(3) for item in split[f"fold_{fold}"]]
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in selected:
        grouped[str(item["motion"])].append(item)
    motion_names = sorted(grouped)
    if len(motion_names) < 3:
        raise ValueError(
            "leave-one-motion-out timing consistency requires at least three motions"
        )
    motion_evidence: list[dict[str, Any]] = []
    improvements_by_method: dict[str, list[dict[str, float | None]]] = {
        method: [] for method in methods
    }
    for held_out_motion in motion_names:
        validation = grouped[held_out_motion]
        training = [
            item
            for motion in motion_names
            if motion != held_out_motion
            for item in grouped[motion]
        ]
        zero = _evaluate_split(
            training,
            validation,
            offset_ms=0.0,
            mode=mode,
            methods=methods,
            robot_records=robot_records,
            robot_timestamps_ns=robot_timestamps_ns,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        )
        candidate = _evaluate_split(
            training,
            validation,
            offset_ms=candidate_offset_ms,
            mode=mode,
            methods=methods,
            robot_records=robot_records,
            robot_timestamps_ns=robot_timestamps_ns,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        )
        method_evidence: dict[str, Any] = {}
        for method in methods:
            improvement = _method_improvement(zero, candidate, method)
            improvements_by_method[method].append(improvement)
            method_evidence[method] = {
                "zero_offset_residuals": zero["methods"][method]["residuals"],
                "candidate_residuals": candidate["methods"][method]["residuals"],
                "improvement": improvement,
            }
        motion_evidence.append(
            {
                "motion": held_out_motion,
                "validation_observation_count": len(validation),
                "training_motion_count": len(motion_names) - 1,
                "methods": method_evidence,
            }
        )

    method_summaries: dict[str, Any] = {}
    for method, improvements in improvements_by_method.items():
        relative_values = [
            float(item["relative_translation"])
            for item in improvements
            if item["relative_translation"] is not None
        ]
        positive_count = sum(
            float(item["absolute_translation_mm"]) > 1e-12 for item in improvements
        )
        material_count = sum(
            float(item["absolute_translation_mm"]) >= min_absolute_improvement_mm
            and item["relative_translation"] is not None
            and float(item["relative_translation"]) >= min_relative_improvement
            for item in improvements
        )
        median_improvement = {
            "absolute_translation_mm": float(
                median(float(item["absolute_translation_mm"]) for item in improvements)
            ),
            "relative_translation": (
                float(median(relative_values))
                if len(relative_values) == len(improvements)
                else None
            ),
            "rotation_change_deg": float(
                median(float(item["rotation_change_deg"]) for item in improvements)
            ),
        }
        sign_p_value = _positive_sign_p_value(
            positive_count,
            len(improvements),
        )
        search_adjusted_sign_p_value = min(
            1.0,
            sign_p_value * candidate_search_hypothesis_count,
        )
        method_ok = bool(
            median_improvement["absolute_translation_mm"] >= min_absolute_improvement_mm
            and median_improvement["relative_translation"] is not None
            and float(median_improvement["relative_translation"])
            >= min_relative_improvement
            and search_adjusted_sign_p_value <= max_search_adjusted_sign_p_value
        )
        method_summaries[method] = {
            "status": "ok" if method_ok else "error",
            "motion_count": len(improvements),
            "positive_motion_count": positive_count,
            "material_motion_count": material_count,
            "positive_sign_p_value": sign_p_value,
            "candidate_search_adjusted_positive_sign_p_value": (
                search_adjusted_sign_p_value
            ),
            "median_improvement": median_improvement,
        }
    return {
        "strategy": LOMO_CONSISTENCY_STRATEGY,
        "status": (
            "ok"
            if all(item["status"] == "ok" for item in method_summaries.values())
            else "error"
        ),
        "candidate_robot_pose_time_offset_ms": float(candidate_offset_ms),
        "candidate_selection_uses_audited_motions": True,
        "candidate_search_adjustment": "bonferroni",
        "candidate_search_hypothesis_count": candidate_search_hypothesis_count,
        "transform_training_motion_disjoint": True,
        "motion_count": len(motion_names),
        "methods": method_summaries,
        "motions": motion_evidence,
        "thresholds": {
            "minimum_median_absolute_translation_mm": (min_absolute_improvement_mm),
            "minimum_median_relative_translation": min_relative_improvement,
            "maximum_search_adjusted_positive_sign_p_value": (
                max_search_adjusted_sign_p_value
            ),
        },
    }


def fixed_zero_sensor_result(
    *,
    sensor_key: str,
    observation_count: int,
) -> dict[str, Any]:
    return {
        "sensor_key": sensor_key,
        "status": "fixed_zero",
        "decision": "fixed_zero_selected",
        "decision_reason": "fixed_zero_policy_selected",
        "selected_robot_pose_time_offset_ms": 0.0,
        "selected_sync_delta_ms": 0.0,
        "candidate_robot_pose_time_offset_ms": 0.0,
        "evidence_strength": "not_applicable",
        "boundary_hit": False,
        "input_observation_count": int(observation_count),
        "output_observation_count": int(observation_count),
        "checks": [],
        "curve": [],
    }


def failed_sensor_result(
    *,
    sensor_key: str,
    observation_count: int,
    error: Exception,
) -> dict[str, Any]:
    """Return structured fail-closed evidence when a search cannot be evaluated."""

    return {
        "sensor_key": sensor_key,
        "status": "failed",
        "decision": "auto_sync_rejected",
        "decision_reason": "time_offset_search_could_not_be_evaluated",
        "selected_robot_pose_time_offset_ms": 0.0,
        "selected_sync_delta_ms": 0.0,
        "candidate_robot_pose_time_offset_ms": 0.0,
        "candidate_sync_delta_ms": 0.0,
        "evidence_strength": "failed",
        "boundary_hit": False,
        "input_observation_count": int(observation_count),
        "output_observation_count": 0,
        "checks": [
            {
                "name": "time_offset_search_execution",
                "status": "error",
                "actual": f"{type(error).__name__}: {error}",
                "threshold": "search completes with promotable evidence",
            }
        ],
        "curve": [],
    }


def nearest_pose_delta_summary(
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, float | int | None]:
    """Summarize retained nearest-pose deltas without changing any matches."""

    values_ms: list[float] = []
    for observation in observations:
        alignment = observation.get("timestamp_alignment")
        if not isinstance(alignment, Mapping):
            continue
        value = alignment.get("nearest_robot_delta_ns")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            values_ms.append(abs(number) / 1_000_000.0)
    if not values_ms:
        return {
            "observation_count": 0,
            "mean_abs_nearest_pose_delta_ms": None,
            "maximum_abs_nearest_pose_delta_ms": None,
        }
    return {
        "observation_count": len(values_ms),
        "mean_abs_nearest_pose_delta_ms": float(sum(values_ms) / len(values_ms)),
        "maximum_abs_nearest_pose_delta_ms": float(max(values_ms)),
    }


def apply_sensor_time_offset(
    observations: Sequence[Mapping[str, Any]],
    *,
    robot_records: Sequence[Mapping[str, Any]],
    robot_pose_time_offset_ms: float,
    max_nearest_pose_delta_ms: float = DEFAULT_MAX_NEAREST_POSE_DELTA_MS,
) -> list[dict[str, Any]]:
    """Rematch fixed target-pose observations at one accepted offset."""

    robot_timestamps_ns = [int(item["timestamp_ns"]) for item in robot_records]
    if not robot_timestamps_ns:
        raise ValueError("robot-pose evidence is empty")
    if robot_timestamps_ns != sorted(robot_timestamps_ns):
        raise ValueError("robot-pose evidence is not time ordered")
    adjusted = []
    for observation in observations:
        matched = _match_observation(
            observation,
            robot_pose_time_offset_ms=robot_pose_time_offset_ms,
            robot_records=robot_records,
            robot_timestamps_ns=robot_timestamps_ns,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        )
        if matched is not None:
            adjusted.append(matched)
    return adjusted


def estimate_sensor_time_offset(
    observations: Sequence[Mapping[str, Any]],
    *,
    sensor_key: str,
    robot_records: Sequence[Mapping[str, Any]],
    mode: str,
    offsets_ms: Sequence[float] | None = None,
    methods: Sequence[str] = DEFAULT_REFERENCE_METHODS,
    max_nearest_pose_delta_ms: float = DEFAULT_MAX_NEAREST_POSE_DELTA_MS,
    warning_nearest_pose_delta_ms: float = DEFAULT_WARNING_NEAREST_POSE_DELTA_MS,
    warning_abs_offset_ms: float | None = DEFAULT_WARNING_ABS_OFFSET_MS,
    max_observations_per_motion: int = DEFAULT_MAX_OBSERVATIONS_PER_MOTION,
    max_search_motions: int = DEFAULT_MAX_SEARCH_MOTIONS,
    min_motions_per_fold: int = DEFAULT_MIN_MOTIONS_PER_FOLD,
    min_absolute_improvement_mm: float = DEFAULT_MIN_ABSOLUTE_IMPROVEMENT_MM,
    min_relative_improvement: float = DEFAULT_MIN_RELATIVE_IMPROVEMENT,
    max_rotation_degradation_deg: float = DEFAULT_MAX_ROTATION_DEGRADATION_DEG,
    minimum_offset_stability_ms: float = DEFAULT_MIN_OFFSET_STABILITY_MS,
    max_leave_one_motion_out_search_adjusted_sign_p_value: float = (
        DEFAULT_MAX_LOMO_SEARCH_ADJUSTED_SIGN_P_VALUE
    ),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Estimate one sensor's offset and return final rematched observations."""

    values = list(offsets_ms or offset_values())
    if not robot_records:
        raise ValueError(f"{sensor_key}: robot-pose evidence is empty")
    robot_timestamps_ns = [int(item["timestamp_ns"]) for item in robot_records]
    if robot_timestamps_ns != sorted(robot_timestamps_ns):
        raise ValueError(f"{sensor_key}: robot-pose evidence is not time ordered")
    if (
        not math.isfinite(max_nearest_pose_delta_ms)
        or max_nearest_pose_delta_ms <= 0.0
        or not math.isfinite(warning_nearest_pose_delta_ms)
        or warning_nearest_pose_delta_ms <= 0.0
        or warning_nearest_pose_delta_ms > max_nearest_pose_delta_ms
    ):
        raise ValueError(
            "nearest-pose warning threshold must be positive and no greater "
            "than the maximum nearest-pose delta"
        )
    if warning_abs_offset_ms is not None and (
        not math.isfinite(warning_abs_offset_ms) or warning_abs_offset_ms <= 0.0
    ):
        raise ValueError("offset warning threshold must be finite and positive")
    if not 0.0 < max_leave_one_motion_out_search_adjusted_sign_p_value <= 1.0:
        raise ValueError(
            "leave-one-motion-out maximum search-adjusted sign p-value "
            "must be in (0, 1]"
        )

    split, split_evidence = _fixed_search_split(
        observations,
        offsets_ms=values,
        robot_records=robot_records,
        robot_timestamps_ns=robot_timestamps_ns,
        max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
        max_observations_per_motion=max_observations_per_motion,
        max_search_motions=max_search_motions,
        min_motions_per_fold=min_motions_per_fold,
    )
    selection_method = methods[0]
    validation_folds: list[dict[str, Any]] = []
    fold_curves: list[list[dict[str, Any]]] = []
    for validation_fold in range(3):
        training_folds = [fold for fold in range(3) if fold != validation_fold]
        training = [item for fold in training_folds for item in split[f"fold_{fold}"]]
        validation = split[f"fold_{validation_fold}"]
        fold_curve = [
            _evaluate_split(
                training,
                validation,
                offset_ms=value,
                mode=mode,
                methods=methods,
                robot_records=robot_records,
                robot_timestamps_ns=robot_timestamps_ns,
                max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
            )
            for value in values
        ]
        fold_curves.append(fold_curve)
        best = _best_method_curve_record(fold_curve, selection_method)
        zero = next(
            item
            for item in fold_curve
            if math.isclose(float(item["robot_pose_time_offset_ms"]), 0.0, abs_tol=1e-9)
        )
        candidate_offset_ms = float(best["robot_pose_time_offset_ms"])
        validation_folds.append(
            {
                "validation_fold": validation_fold,
                "training_folds": training_folds,
                "candidate_robot_pose_time_offset_ms": candidate_offset_ms,
                "method_optima_robot_pose_time_offset_ms": _method_optima(
                    fold_curve, methods
                ),
                "validation": {
                    "transform_training_motion_disjoint": True,
                    "offset_selection_uses_this_validation_fold": True,
                    "zero_offset": zero,
                    "candidate": best,
                    "improvement": _method_improvement(zero, best, selection_method),
                },
            }
        )

    curve = _aggregate_fold_curve(
        fold_curves,
        offsets_ms=values,
        methods=methods,
        selection_method=selection_method,
    )
    fold_candidate_offsets = [
        float(item["candidate_robot_pose_time_offset_ms"]) for item in validation_folds
    ]
    best = _best_method_curve_record(curve, selection_method)
    candidate_offset_ms = float(best["robot_pose_time_offset_ms"])
    zero = next(
        item
        for item in curve
        if math.isclose(float(item["robot_pose_time_offset_ms"]), 0.0, abs_tol=1e-9)
    )
    optima = _method_optima(curve, methods)
    fold_candidate_spread_ms = max(fold_candidate_offsets) - min(fold_candidate_offsets)
    sensitivity_differences = [
        abs(
            float(item["method_optima_robot_pose_time_offset_ms"][method])
            - float(item["candidate_robot_pose_time_offset_ms"])
        )
        for item in validation_folds
        for method in methods[1:]
    ]
    max_sensitivity_difference_ms = max(sensitivity_differences, default=0.0)
    sample_period_ms = _median_robot_sample_period_ms(robot_timestamps_ns)
    offset_stability_threshold_ms = max(
        minimum_offset_stability_ms,
        2.0 * sample_period_ms,
    )
    sensitivity_failure_threshold_ms = max(
        minimum_offset_stability_ms * 2.0,
        4.0 * sample_period_ms,
    )
    boundary_hit = any(
        math.isclose(value, min(values), abs_tol=1e-9)
        or math.isclose(value, max(values), abs_tol=1e-9)
        for value in fold_candidate_offsets
    )
    tuning_improvement = _improvement(zero, best)
    fold_selected_records = [
        next(
            record
            for record in fold_curve
            if math.isclose(
                float(record["robot_pose_time_offset_ms"]),
                candidate_offset_ms,
                abs_tol=1e-9,
            )
        )
        for fold_curve in fold_curves
    ]
    fold_zero_records = [
        next(
            record
            for record in fold_curve
            if math.isclose(
                float(record["robot_pose_time_offset_ms"]), 0.0, abs_tol=1e-9
            )
        )
        for fold_curve in fold_curves
    ]
    cross_validated_zero = {
        "fold_count": 3,
        "method": selection_method,
        "residuals": residual_summary(
            [
                {
                    "translation_mm": float(
                        item["methods"][selection_method]["residuals"][
                            "mean_translation_mm"
                        ]
                    ),
                    "rotation_deg": float(
                        item["methods"][selection_method]["residuals"][
                            "mean_rotation_deg"
                        ]
                    ),
                }
                for item in fold_zero_records
            ]
        ),
    }
    cross_validated_candidate = {
        "fold_count": 3,
        "method": selection_method,
        "robot_pose_time_offset_ms": candidate_offset_ms,
        "residuals": residual_summary(
            [
                {
                    "translation_mm": float(
                        item["methods"][selection_method]["residuals"][
                            "mean_translation_mm"
                        ]
                    ),
                    "rotation_deg": float(
                        item["methods"][selection_method]["residuals"][
                            "mean_rotation_deg"
                        ]
                    ),
                }
                for item in fold_selected_records
            ]
        ),
    }
    cross_validated_improvement = _improvement(
        cross_validated_zero, cross_validated_candidate
    )
    cross_validated_relative = cross_validated_improvement["relative_translation"]
    per_fold_improvements = [
        _method_improvement(zero_record, selected_record, selection_method)
        for zero_record, selected_record in zip(
            fold_zero_records, fold_selected_records, strict=True
        )
    ]
    aggregate_materially_better = bool(
        candidate_offset_ms != 0.0
        and float(cross_validated_improvement["absolute_translation_mm"])
        >= min_absolute_improvement_mm
        and cross_validated_relative is not None
        and float(cross_validated_relative) >= min_relative_improvement
    )
    every_fold_materially_better = bool(
        candidate_offset_ms != 0.0
        and all(
            float(item["absolute_translation_mm"]) >= min_absolute_improvement_mm
            and item["relative_translation"] is not None
            and float(item["relative_translation"]) >= min_relative_improvement
            for item in per_fold_improvements
        )
    )
    motion_consistency = (
        _leave_one_motion_out_consistency(
            split,
            candidate_offset_ms=candidate_offset_ms,
            mode=mode,
            methods=methods,
            robot_records=robot_records,
            robot_timestamps_ns=robot_timestamps_ns,
            max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
            min_absolute_improvement_mm=min_absolute_improvement_mm,
            min_relative_improvement=min_relative_improvement,
            candidate_search_hypothesis_count=max(
                1,
                sum(not math.isclose(value, 0.0, abs_tol=1e-9) for value in values),
            ),
            max_search_adjusted_sign_p_value=(
                max_leave_one_motion_out_search_adjusted_sign_p_value
            ),
        )
        if candidate_offset_ms != 0.0
        else None
    )
    improvement_evidence_ok = (
        isinstance(motion_consistency, Mapping)
        and motion_consistency.get("status") == "ok"
    )
    materially_better = aggregate_materially_better and improvement_evidence_ok
    rotation_guard_ok = all(
        float(item["rotation_change_deg"]) <= max_rotation_degradation_deg
        for item in per_fold_improvements
    )
    all_folds_choose_zero = all(
        math.isclose(item, 0.0, abs_tol=1e-9) for item in fold_candidate_offsets
    )
    zero_identifiability = []
    for fold_curve, zero_record in zip(
        fold_curves,
        fold_zero_records,
        strict=True,
    ):
        nonzero = _best_method_curve_record(
            [
                item
                for item in fold_curve
                if not math.isclose(
                    float(item["robot_pose_time_offset_ms"]),
                    0.0,
                    abs_tol=1e-9,
                )
            ],
            selection_method,
        )
        superiority = _method_improvement(nonzero, zero_record, selection_method)
        relative = superiority["relative_translation"]
        zero_identifiability.append(
            {
                "best_nonzero_robot_pose_time_offset_ms": float(
                    nonzero["robot_pose_time_offset_ms"]
                ),
                "zero_superiority": superiority,
                "identified": bool(
                    float(superiority["absolute_translation_mm"])
                    >= min_absolute_improvement_mm
                    and relative is not None
                    and float(relative) >= min_relative_improvement
                ),
            }
        )
    zero_offset_identified = all_folds_choose_zero and all(
        item["identified"] for item in zero_identifiability
    )
    checks = [
        {
            "name": "fixed_full_range_observation_set",
            "status": "ok",
            "actual": split_evidence["selected_observation_count"],
            "threshold": min_motions_per_fold * 3,
        },
        {
            "name": "cross_validation_offset_stability",
            "status": (
                "ok"
                if fold_candidate_spread_ms <= offset_stability_threshold_ms
                else "error"
            ),
            "actual": fold_candidate_spread_ms,
            "threshold": offset_stability_threshold_ms,
            "unit": "ms",
        },
        {
            "name": "reference_method_sensitivity",
            "status": (
                "ok"
                if max_sensitivity_difference_ms <= offset_stability_threshold_ms
                else "warning"
                if max_sensitivity_difference_ms <= sensitivity_failure_threshold_ms
                else "error"
            ),
            "actual": max_sensitivity_difference_ms,
            "warning_threshold": offset_stability_threshold_ms,
            "failure_threshold": sensitivity_failure_threshold_ms,
            "unit": "ms",
        },
        {
            "name": "search_optimum_not_at_boundary",
            "status": "error" if boundary_hit else "ok",
            "actual": candidate_offset_ms,
            "threshold": [min(values), max(values)],
            "unit": "ms",
        },
        *(
            [
                {
                    "name": "candidate_offset_magnitude_warning",
                    "status": (
                        "warning"
                        if abs(candidate_offset_ms) > warning_abs_offset_ms
                        else "ok"
                    ),
                    "actual": abs(candidate_offset_ms),
                    "warning_threshold": warning_abs_offset_ms,
                    "unit": "ms",
                }
            ]
            if warning_abs_offset_ms is not None
            else []
        ),
        {
            "name": "cross_validated_translation_improvement",
            "status": (
                "ok"
                if aggregate_materially_better
                else "not_needed"
                if all_folds_choose_zero
                else "error"
            ),
            "actual": cross_validated_improvement,
            "per_fold": per_fold_improvements,
            "threshold": {
                "minimum_absolute_translation_mm": min_absolute_improvement_mm,
                "minimum_relative_translation": min_relative_improvement,
            },
        },
        *[
            {
                "name": "cross_validation_fold_materiality",
                "status": (
                    "ok"
                    if every_fold_materially_better
                    else "not_needed"
                    if all_folds_choose_zero
                    else "warning"
                    if aggregate_materially_better
                    else "error"
                ),
                "actual": per_fold_improvements,
                "threshold": {
                    "minimum_absolute_translation_mm": (min_absolute_improvement_mm),
                    "minimum_relative_translation": min_relative_improvement,
                },
            },
            {
                "name": "leave_one_motion_out_timing_consistency",
                "status": (
                    str(motion_consistency["status"])
                    if isinstance(motion_consistency, Mapping)
                    else "not_needed"
                ),
                "actual": (
                    {
                        "motion_count": motion_consistency["motion_count"],
                        "methods": motion_consistency["methods"],
                    }
                    if isinstance(motion_consistency, Mapping)
                    else None
                ),
                "threshold": {
                    "minimum_median_absolute_translation_mm": (
                        min_absolute_improvement_mm
                    ),
                    "minimum_median_relative_translation": (min_relative_improvement),
                    "maximum_search_adjusted_positive_sign_p_value": (
                        max_leave_one_motion_out_search_adjusted_sign_p_value
                    ),
                },
            },
        ],
        {
            "name": "cross_validated_rotation_guard",
            "status": "ok" if rotation_guard_ok else "error",
            "actual": cross_validated_improvement["rotation_change_deg"],
            "per_fold": [item["rotation_change_deg"] for item in per_fold_improvements],
            "threshold": max_rotation_degradation_deg,
            "unit": "deg",
        },
        {
            "name": "zero_offset_identifiability",
            "status": (
                "not_needed"
                if candidate_offset_ms != 0.0
                else "ok"
                if zero_offset_identified
                else "error"
            ),
            "actual": zero_identifiability,
            "threshold": {
                "minimum_absolute_translation_mm": min_absolute_improvement_mm,
                "minimum_relative_translation": min_relative_improvement,
            },
        },
    ]
    blocking = [item for item in checks if item["status"] == "error"]
    apply_candidate = materially_better and not blocking
    selected_offset_ms = candidate_offset_ms if apply_candidate else 0.0
    strict_status = (
        "applied"
        if apply_candidate
        else "kept_zero"
        if zero_offset_identified and not blocking
        else "failed"
    )
    warning_fallback_used = strict_status == "failed"
    status = "kept_zero" if warning_fallback_used else strict_status
    if warning_fallback_used:
        checks = [
            {
                **item,
                "status": "warning",
                "original_status": "error",
                "fallback": "recorded timing retained at 0 ms",
            }
            if item["status"] == "error"
            else item
            for item in checks
        ]
    reason = (
        "motion_disjoint_cross_validation_and_leave_one_motion_out_consistency_passed"
        if status == "applied"
        else (
            "ambiguous_auto_offset_kept_recorded_zero"
            if warning_fallback_used
            else "candidate_failed_safety_or_stability_checks"
            if status == "failed"
            else "zero_offset_identified_by_all_cross_validation_folds"
        )
    )

    adjusted = apply_sensor_time_offset(
        observations,
        robot_records=robot_records,
        robot_pose_time_offset_ms=selected_offset_ms,
        max_nearest_pose_delta_ms=max_nearest_pose_delta_ms,
    )
    delta_summary = nearest_pose_delta_summary(adjusted)
    maximum_delta_ms = delta_summary["maximum_abs_nearest_pose_delta_ms"]
    if maximum_delta_ms is not None:
        checks.append(
            {
                "name": "nearest_pose_delta_warning",
                "status": (
                    "warning"
                    if float(maximum_delta_ms) > warning_nearest_pose_delta_ms
                    else "ok"
                ),
                "actual": delta_summary,
                "warning_threshold": warning_nearest_pose_delta_ms,
                "failure_threshold": max_nearest_pose_delta_ms,
                "unit": "ms",
            }
        )
    evidence_strength = (
        "degraded"
        if warning_fallback_used
        else "strong"
        if status == "applied"
        and float(cross_validated_relative or 0.0) >= 0.15
        and fold_candidate_spread_ms <= DEFAULT_STEP_MS * 2.0
        else "consistent"
        if status == "applied"
        else "failed"
        if status == "failed"
        else "identified_zero"
    )
    result = {
        "sensor_key": sensor_key,
        "status": status,
        "decision": (
            "auto_offset_applied"
            if status == "applied"
            else "recorded_timing_kept"
            if warning_fallback_used
            else "auto_zero_offset_identified"
            if status == "kept_zero"
            else "auto_sync_rejected"
        ),
        "decision_reason": reason,
        "selected_robot_pose_time_offset_ms": selected_offset_ms,
        "selected_sync_delta_ms": -selected_offset_ms,
        "candidate_robot_pose_time_offset_ms": candidate_offset_ms,
        "candidate_sync_delta_ms": -candidate_offset_ms,
        "evidence_strength": evidence_strength,
        "warning_fallback_used": warning_fallback_used,
        "boundary_hit": boundary_hit,
        "reference_pnp_method": DEFAULT_REFERENCE_PNP_METHOD,
        "reference_extrinsic_methods": list(methods),
        "selection_extrinsic_method": selection_method,
        "improvement_evidence_strategy": IMPROVEMENT_EVIDENCE_STRATEGY,
        "method_optima_robot_pose_time_offset_ms": optima,
        "fold_candidate_robot_pose_time_offsets_ms": (fold_candidate_offsets),
        "fold_candidate_spread_ms": fold_candidate_spread_ms,
        "max_sensitivity_method_difference_ms": (max_sensitivity_difference_ms),
        "median_robot_sample_period_ms": sample_period_ms,
        "offset_stability_threshold_ms": offset_stability_threshold_ms,
        "sensitivity_failure_threshold_ms": (sensitivity_failure_threshold_ms),
        "split": split_evidence,
        "aggregate_search": {
            "zero_offset": zero,
            "candidate": best,
            "improvement": tuning_improvement,
        },
        "cross_validation": {
            "transform_training_motion_disjoint": True,
            "offset_selection_uses_validation_metrics": True,
            "untouched_offset_audit": False,
            "zero_offset": cross_validated_zero,
            "candidate": cross_validated_candidate,
            "improvement": cross_validated_improvement,
            "folds": validation_folds,
        },
        "motion_consistency": motion_consistency,
        "input_observation_count": len(observations),
        "output_observation_count": len(adjusted),
        "nearest_pose_delta_summary": delta_summary,
        "checks": checks,
        "curve": curve,
    }
    return result, adjusted


def search_configuration() -> dict[str, Any]:
    values = offset_values()
    return {
        "minimum_robot_pose_time_offset_ms": min(values),
        "maximum_robot_pose_time_offset_ms": max(values),
        "step_ms": DEFAULT_STEP_MS,
        "reference_pnp_method": DEFAULT_REFERENCE_PNP_METHOD,
        "reference_extrinsic_methods": list(DEFAULT_REFERENCE_METHODS),
        "max_nearest_pose_delta_ms": DEFAULT_MAX_NEAREST_POSE_DELTA_MS,
        "warning_nearest_pose_delta_ms": DEFAULT_WARNING_NEAREST_POSE_DELTA_MS,
        "warning_absolute_robot_pose_time_offset_ms": (DEFAULT_WARNING_ABS_OFFSET_MS),
        "time_offset_failure_policy": FAILURE_POLICY_WARN_KEEP_ZERO,
        "max_observations_per_motion": DEFAULT_MAX_OBSERVATIONS_PER_MOTION,
        "maximum_search_motion_count": DEFAULT_MAX_SEARCH_MOTIONS,
        "minimum_motion_count_per_cross_validation_fold": (
            DEFAULT_MIN_MOTIONS_PER_FOLD
        ),
        "minimum_absolute_cross_validated_improvement_mm": (
            DEFAULT_MIN_ABSOLUTE_IMPROVEMENT_MM
        ),
        "minimum_relative_cross_validated_improvement": (
            DEFAULT_MIN_RELATIVE_IMPROVEMENT
        ),
        "maximum_cross_validated_rotation_degradation_deg": (
            DEFAULT_MAX_ROTATION_DEGRADATION_DEG
        ),
        "minimum_offset_stability_ms": DEFAULT_MIN_OFFSET_STABILITY_MS,
        "maximum_leave_one_motion_out_search_adjusted_sign_p_value": (
            DEFAULT_MAX_LOMO_SEARCH_ADJUSTED_SIGN_P_VALUE
        ),
    }


def sign_convention() -> dict[str, Any]:
    return {
        "operator_field": "robot_pose_time_offset_ms",
        "operator_equation": "robot_pose_query_time = frame_time + offset",
        "positive_operator_value": "pair the frame with a robot pose recorded later",
        "synchronizer_field": "sync_delta_ms",
        "synchronizer_equation": "robot_pose_query_time = frame_time - sync_delta",
        "conversion": "sync_delta_ms = -robot_pose_time_offset_ms",
        "raw_timestamps_rewritten": False,
        "physical_clock_synchronization_claimed": False,
    }
