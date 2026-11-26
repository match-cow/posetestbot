import type { JsonValue } from "@/lib/contracts"

export interface DepthSettings {
  thresholds_mm: number[]
  erosion_pixels: 2
  distance_bin_mm: number
  angle_bin_deg: number
}
export interface DepthSubmission {
  schema_version: "depth_evaluation_submission.v1"
  run_root: string
  finish_label: string
  geometry_hashes: string[]
  sensors: string[]
  settings: DepthSettings
}
export interface DepthInput { run_root: string; evaluation_id: string }
interface DepthCameraProjection {
  scene_id: number; sensor_name: string; width: number; height: number
  calibration_profile_id: string; projection: "native" | "rectified"
  distortion_model: string; distortion: number[]
}
export interface DepthComparisonSubmission {
  schema_version: "depth_comparison_submission.v1"
  run_root: string
  evaluations: DepthInput[]
}
export interface DepthSetup {
  schema_version: "depth_evaluation_setup.v1"
  run_root: string
  dataset: {
    ready: boolean; blockers: string[]; frame_count: number
    objects: { obj_id: number; name: string; canonical_sha256: string; catalog_uuid: string }[]
    sensors: (DepthCameraProjection & { unavailable_reason: string | null })[]
    quality_evidence: JsonValue
  }
  renderer: { available: boolean; version: string | null; reason: string | null; required_version: string; device: string }
  defaults: DepthSettings
  assumptions: string[]
}
export interface DepthMetrics {
  status: "available" | "unavailable"
  reference_pixels: number; valid_pixels: number; missing_pixels: number
  completeness: number | null; bias_mm: number | null; mae_mm: number | null; rmse_mm: number | null
  median_absolute_error_mm: number | null; p95_absolute_error_mm: number | null
  thresholds: { threshold_mm: number; outlier_pixels: number; outlier_rate: number | null; usable_coverage: number | null }[]
}
export interface DepthRow extends DepthMetrics {
  scope: string; sensor_name: string; canonical_sha256: string; instance_uuid: string
  region: string; distance_bin: number; angle_bin: number; frame_count: number; contributing_frame_count: number
  input_id?: string; finish_label?: string
}
export interface DepthScales { depth_max_mm: number; error_max_mm: number }
export interface DepthFrame {
  key: string; scene_id: number; im_id: number; sensor_name: string; width: number; height: number
  scales: DepthScales; missing_depth_map: boolean; input_id?: string; finish_label?: string
  instances: { instance_uuid: string; obj_id: number; canonical_sha256: string; regions: Record<string, DepthMetrics> }[]
  reference_camera: { cam_K: number[]; depth_scale: number }
  reference_instances: { instance_uuid: string; obj_id: number; canonical_sha256: string; render_id: number; cam_R_m2c: number[]; cam_t_m2c: number[] }[]
}
export interface DepthRecord {
  id: string; created_at: string; state: string; report_available: boolean; job_id?: string
  submission?: DepthSubmission | DepthComparisonSubmission; inputs?: DepthInput[]
  progress: { state: string; completed_frames?: number; rendered_frames?: number; total_frames?: number; error?: string }
}
export interface DepthHistory { schema_version: "depth_history.v1"; records: DepthRecord[]; total: number; offset: number; limit: number }
interface ReportBase {
  id: string; metric_revision: string; settings: DepthSettings; geometry_hashes: string[]; frame_count: number; scales: DepthScales
  implementation_sha256: Record<string, string>
  quality_evidence: JsonValue; assumptions: string[]; created_at: string
}
export interface DepthEvaluationReport extends ReportBase {
  schema_version: "depth_evaluation_report.v1"; kind: "evaluation"
  run_root: string; finish_label: string; sensors: string[]; aggregates: DepthRow[]
  source_sha256: Record<string, string | null>; source_identity_sha256: string
  camera_projections?: DepthCameraProjection[] | null
}
export interface DepthComparisonReport extends ReportBase {
  schema_version: "depth_comparison_report.v1"; kind: "comparison"
  inputs: (DepthInput & { camera_projections?: DepthCameraProjection[] })[]; bins: DepthRow[]
  /** Each input's own aggregates and bins; absent in comparisons published before they existed. */
  summary?: DepthRow[]; series_bins?: DepthRow[]
  series: { input_id: string; evaluation_id: string; run_root: string; finish_label: string; sensor_name: string }[]
  coverage: {
    input_id: string; evaluation_id: string; run_root: string; finish_label: string; sensor_name: string
    canonical_sha256: string; region: string; common_bin_count: number; common_reference_pixels: number
    unmatched_reference_pixels: number; status: string; reason: string | null; unmatched_bins: DepthRow[]
  }[]
}
export type DepthReport = DepthEvaluationReport | DepthComparisonReport

export type DepthKind = "evaluations" | "comparisons"
export interface DepthVideoSubmission {
  schema_version: "depth_video_submission.v1"
  run_root: string; source_kind: DepthKind; source_id: string
  scene_id: number; input_id: string | null
  view: "overview" | "measured" | "error" | "missing"
  fps: number; stride: number; width: 960 | 1280 | 1920
}
export interface DepthVideoSeries {
  scene_id: number; input_id: string | null; sensor_name: string; finish_label: string
  width: number; height: number; frame_count: number
}
export interface DepthVideoRecord {
  id: string; created_at: string; state: string; job_id?: string
  submission: DepthVideoSubmission | null; sensor_name?: string; finish_label?: string
  video_available: boolean; frame_count?: number; duration_seconds?: number
  output_size: [number, number] | null
  progress: { state: string; completed_frames?: number; total_frames?: number; error?: string }
}
export interface DepthVideoChoices {
  schema_version: "depth_video_choices.v1"
  encoder: { available: boolean; reason: string | null }
  series: DepthVideoSeries[]; records: DepthVideoRecord[]
  total: number; offset: number; limit: number
}

export interface DepthFrameSeriesRow extends DepthMetrics {
  level: "frame" | "frame_geometry"
  frame_key: string; ordinal: number; scene_id: number; im_id: number
  sensor_name: string; canonical_sha256: string; region: string
  input_id?: string; finish_label?: string
  robot_timestamp_ns: number | null; t_motion_s: number | null
  path_mm: number | null; path_fraction: number | null
}
export interface DepthFrameSeries {
  schema_version: "depth_frame_series.v1"; id: string
  available: boolean; reason?: string; motion_available?: boolean
  inputs_without_series?: string[]
  rows: DepthFrameSeriesRow[]
}
