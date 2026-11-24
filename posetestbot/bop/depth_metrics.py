"""Reference-defined depth metrics, with disk-backed exact aggregate quantiles."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator


REVISION = "depth_metrics.v1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class MetricSettings(Contract):
    thresholds_mm: list[float] = Field(
        default_factory=lambda: [1.0, 2.0, 5.0, 10.0], min_length=1, max_length=16
    )
    erosion_pixels: Literal[2] = 2
    distance_bin_mm: float = Field(default=100.0, ge=1, le=10000)
    angle_bin_deg: float = Field(default=10.0, ge=1, le=90)

    @field_validator("thresholds_mm")
    @classmethod
    def thresholds(cls, values):
        if values != sorted(set(values)) or any(v <= 0 or v > 10000 for v in values):
            raise ValueError(
                "thresholds_mm must be increasing, unique, positive, and at most 10000"
            )
        return values


class EvaluationSubmission(Contract):
    schema_version: Literal["depth_evaluation_submission.v1"]
    run_root: str
    finish_label: str = Field(min_length=1, max_length=120)
    geometry_hashes: list[str] = Field(min_length=1, max_length=256)
    sensors: list[str] = Field(min_length=1, max_length=256)
    settings: MetricSettings = Field(default_factory=MetricSettings)

    @field_validator("finish_label")
    @classmethod
    def label(cls, value):
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError(
                "finish_label must be trimmed text without control characters"
            )
        return value


class EvaluationInput(Contract):
    run_root: str
    evaluation_id: str


class ComparisonSubmission(Contract):
    schema_version: Literal["depth_comparison_submission.v1"]
    run_root: str
    evaluations: list[EvaluationInput] = Field(min_length=2, max_length=24)


class Metrics(Contract):
    status: Literal["available", "unavailable"]
    reference_pixels: int
    valid_pixels: int
    missing_pixels: int
    completeness: float | None
    bias_mm: float | None
    mae_mm: float | None
    rmse_mm: float | None
    median_absolute_error_mm: float | None
    p95_absolute_error_mm: float | None
    thresholds: list[dict[str, float | int | None]]


class AggregateRow(Metrics):
    scope: Literal["run", "sensor", "sensor_geometry", "instance", "bin"]
    sensor_name: str
    canonical_sha256: str
    instance_uuid: str
    region: Literal["full", "interior", "boundary"]
    distance_bin: int
    angle_bin: int
    frame_count: int
    contributing_frame_count: int
    input_id: str | None = None
    finish_label: str | None = None
    evaluation_id: str | None = None
    run_root: str | None = None


class ReportBase(Contract):
    id: str
    metric_revision: str
    implementation_sha256: dict[str, str]
    settings: MetricSettings
    geometry_hashes: list[str]
    frame_count: int
    scales: dict[str, Any]
    quality_evidence: dict[str, Any] | list[dict[str, Any]]
    assumptions: list[str]
    created_at: str


class EvaluationReport(ReportBase):
    schema_version: Literal["depth_evaluation_report.v1"]
    kind: Literal["evaluation"]
    run_root: str
    finish_label: str
    sensors: list[str]
    aggregates: list[AggregateRow]
    renderer: dict[str, Any]
    source_sha256: dict[str, str | None]
    source_identity_sha256: str
    camera_projections: list[dict[str, Any]] | None = None


class ComparisonReport(ReportBase):
    schema_version: Literal["depth_comparison_report.v1"]
    kind: Literal["comparison"]
    inputs: list[dict[str, Any]]
    series: list[dict[str, Any]]
    bins: list[AggregateRow]
    coverage: list[dict[str, Any]]


def regions(instance_ids: np.ndarray, instance_id: int) -> dict[str, np.ndarray]:
    full = instance_ids == instance_id
    interior = cv2.erode(
        full.astype(np.uint8),
        np.ones((5, 5), np.uint8),
        borderType=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)
    return {"full": full, "interior": interior, "boundary": full & ~interior}


def reference_bins(depth_mm, normals_cv, intrinsic, settings: MetricSettings):
    """Surface range (Euclidean camera distance) and acute incidence; zero anchored."""
    h, w = depth_mm.shape
    ys, xs = np.indices((h, w), dtype=float)
    rays = np.stack(
        (
            (xs - intrinsic[0, 2]) / intrinsic[0, 0],
            (ys - intrinsic[1, 2]) / intrinsic[1, 1],
            np.ones((h, w)),
        ),
        axis=-1,
    )
    length = np.linalg.norm(rays, axis=-1)
    distance = depth_mm * length
    normal_length = np.linalg.norm(normals_cv, axis=-1)
    cosine = np.abs(np.sum(normals_cv * rays, axis=-1)) / np.maximum(
        normal_length * length, 1e-30
    )
    angle = np.rad2deg(np.arccos(np.clip(cosine, 0, 1)))
    # The closed endpoint 90 degrees belongs to the last (possibly partial) bin.
    angle = np.minimum(angle, np.nextafter(90.0, 0.0))
    return (
        np.floor(distance / settings.distance_bin_mm).astype(np.int32),
        np.floor(angle / settings.angle_bin_deg).astype(np.int32),
    )


def _result(n, errors, settings):
    valid = len(errors)
    absolute = np.abs(errors)
    return Metrics(
        status="available" if n else "unavailable",
        reference_pixels=n,
        valid_pixels=valid,
        missing_pixels=n - valid,
        completeness=valid / n if n else None,
        bias_mm=float(np.mean(errors)) if valid else None,
        mae_mm=float(np.mean(absolute)) if valid else None,
        rmse_mm=float(np.sqrt(np.mean(errors * errors))) if valid else None,
        median_absolute_error_mm=float(np.median(absolute)) if valid else None,
        p95_absolute_error_mm=float(np.percentile(absolute, 95)) if valid else None,
        thresholds=[
            {
                "threshold_mm": t,
                "outlier_pixels": int(np.count_nonzero(absolute > t)),
                "outlier_rate": float(np.mean(absolute > t)) if valid else None,
                "usable_coverage": int(np.count_nonzero(absolute <= t)) / n
                if n
                else None,
            }
            for t in settings.thresholds_mm
        ],
    ).model_dump()


def measure(measured_mm, reference_mm, region, settings: MetricSettings):
    if measured_mm.shape != reference_mm.shape or region.shape != reference_mm.shape:
        raise ValueError("Measured and reference depth dimensions differ")
    if np.any(region & (~np.isfinite(reference_mm) | (reference_mm <= 0))):
        raise ValueError("Reference region contains invalid reference depth")
    valid = region & np.isfinite(measured_mm) & (measured_mm > 0)
    errors = measured_mm[valid].astype(np.float64) - reference_mm[valid]
    return _result(int(np.count_nonzero(region)), errors, settings), errors


class Aggregate:
    """One frame at a time; absolute errors spill to disk for exact percentiles.

    In-place memmap partition avoids allocating a run-sized pixel array.
    Frame counts are incremented once per update, including empty references.
    """

    def __init__(self, path: Path, settings: MetricSettings):
        self.path, self.settings = path, settings
        self.n = self.valid = self.frames = self.contributing_frames = 0
        self.signed = self.absolute = self.squared = 0.0
        self.outliers = np.zeros(len(settings.thresholds_mm), dtype=np.int64)

    def add(self, n: int, errors: np.ndarray):
        self.n += n
        self.frames += 1
        self.contributing_frames += int(n > 0)
        self.valid += len(errors)
        absolute = np.abs(errors)
        self.signed += float(np.sum(errors))
        self.absolute += float(np.sum(absolute))
        self.squared += float(np.sum(errors * errors))
        self.outliers += [
            np.count_nonzero(absolute > t) for t in self.settings.thresholds_mm
        ]
        if len(errors):
            with self.path.open("ab") as stream:
                absolute.astype(np.float64).tofile(stream)

    def result(self):
        median = p95 = None
        if self.valid:
            values = np.memmap(
                self.path, dtype=np.float64, mode="r+", shape=(self.valid,)
            )
            ranks = [(self.valid - 1) * p for p in (0.5, 0.95)]
            values.partition(
                sorted(
                    {int(math.floor(r)) for r in ranks}
                    | {int(math.ceil(r)) for r in ranks}
                )
            )
            median, p95 = [
                float(
                    values[math.floor(r)]
                    + (values[math.ceil(r)] - values[math.floor(r)]) * (r % 1)
                )
                for r in ranks
            ]
            del values
        result = Metrics(
            status="available" if self.n else "unavailable",
            reference_pixels=self.n,
            valid_pixels=self.valid,
            missing_pixels=self.n - self.valid,
            completeness=self.valid / self.n if self.n else None,
            bias_mm=self.signed / self.valid if self.valid else None,
            mae_mm=self.absolute / self.valid if self.valid else None,
            rmse_mm=math.sqrt(self.squared / self.valid) if self.valid else None,
            median_absolute_error_mm=median,
            p95_absolute_error_mm=p95,
            thresholds=[
                {
                    "threshold_mm": t,
                    "outlier_pixels": int(o),
                    "outlier_rate": int(o) / self.valid if self.valid else None,
                    "usable_coverage": (self.valid - int(o)) / self.n
                    if self.n
                    else None,
                }
                for t, o in zip(self.settings.thresholds_mm, self.outliers, strict=True)
            ],
        ).model_dump()
        return {
            **result,
            "frame_count": self.frames,
            "contributing_frame_count": self.contributing_frames,
        }
