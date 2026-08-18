export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

export interface Bootstrap {
  schema_version: "web_bootstrap.v1"
  brand: {
    name: string
    logo_url: string
    logo_urls: { light: string; dark: string }
    favicon_url: string
  }
  robot: { ip: string; port: number }
  default_run_root: string
  allowed_run_roots: string[]
}

export interface RunIndexItem {
  path: string
  name: string
  run_name: string | null
  sequence: string | null
  plan_only: boolean | null
  config_valid: boolean
  config_error: string | null
  modified_at: string
}

export interface OverviewSection {
  id: string
  label: string
  status: string
  artifacts: Array<{ path: string; exists: boolean; status: string | null }>
}

export interface CalibrationSyncSensor {
  sensor_key: string
  sensor_name: string
  sensor_folder: string
  profile_id: string
  robot_pose_time_offset_ms: number
  sync_delta_ms: number
  frame_timestamp_source: string
  robot_timestamp_source: string
  required_frame_timestamp_domain: string | null
  timestamp_fallback_allowed: boolean
  max_nearest_pose_delta_ms: number
}

export interface CalibrationSyncOverview {
  status: "not_configured" | "ready" | "error"
  bundle_sha256?: string
  sensors: CalibrationSyncSensor[]
  error?: string
}

export interface Overview {
  run_root: string
  config: RunConfig | null
  config_error: string | null
  calibration_sync: CalibrationSyncOverview
  sidebar: OverviewSection[]
  steps: Array<{
    index: number
    id: string
    stage_id: string
    label: string
    description: string
    status: string
    resources: string[]
    artifacts: OverviewSection["artifacts"]
  }>
  recommendations: Array<Record<string, JsonValue>>
  recommendation_error: string | null
}

export interface RunStorage {
  schema_version: "run_storage.v1"
  run_root: string
  filesystem_path: string | null
  status: "ready" | "warning" | "error" | "unavailable"
  total_bytes: number | null
  used_bytes: number | null
  free_bytes: number | null
  free_fraction: number | null
  thresholds: {
    critical_free_bytes: number
    warning_free_bytes: number
    critical_free_bytes_cap: number
    warning_free_bytes_cap: number
    critical_free_fraction: number
    warning_free_fraction: number
  }
  error: string | null
}

export interface RunFolderIdentity {
  device: number
  inode: number
}

export interface RunFolderSensor {
  sensor_type: string
  device_id: string
  name: string
  mounting_mode: string
  enabled: boolean
}

export interface RunFolderEvidence {
  raw_capture: boolean
  synchronized: boolean
  calibration: boolean
  bop_export: boolean
  bop_evaluation: boolean
}

export interface RunFolder {
  path: string
  name: string
  root: string
  modified_at: string
  size_bytes: number
  allocated_bytes: number
  file_count: number
  directory_count: number
  symlink_count: number
  scan_complete: boolean
  scan_error_count: number
  scan_errors: string[]
  identity: RunFolderIdentity
  config: {
    valid: boolean
    error: string | null
    run_name: string | null
    sequence: string | null
    plan_only: boolean | null
  }
  contents: {
    dataset_mode: string | null
    resolution: string | null
    fps: number | null
    synchronization_mode: string | null
    sensor_count: number
    enabled_sensor_count: number
    sensors: RunFolderSensor[]
    object_count: number
    object_names: string[]
    template_uuid: string | null
    evidence: RunFolderEvidence
  }
  breakdown: Record<string, {
    size_bytes: number
    allocated_bytes: number
    file_count: number
  }>
  relocation: {
    original_path: string
    aliases: string[]
    history_count: number
  } | null
}

export interface RunFolderInventory {
  schema_version: "run_folder_inventory.v1"
  generated_at: string | null
  inventory_state: "missing" | "refreshing" | "ready" | "stale"
  stale: boolean
  roots: Array<{
    path: string
    exists: boolean
    identity: RunFolderIdentity | null
    storage: RunStorage
  }>
  runs: RunFolder[]
  refresh_job: Job | null
  operation_job: Job | null
  maintenance?: {
    schema_version: "run_folder_maintenance.v1"
    recovered_count: number
    transactions: Array<{
      transaction_id: string
      operation: "move" | "delete"
      action: "rolled_back_move" | "completed_move" | "resumed_delete"
    }>
    unresolved_count: number
    journal_fingerprint: string
    unresolved: Array<{
      transaction_id: string | null
      operation: "move" | "delete" | null
      error: string
      remnant_bytes: number | null
    }>
  }
}

export interface SensorDevice {
  sensor_type: string
  device_id: string
  display_name?: string
  effective_display_name?: string
  alias?: string
  connected?: boolean
  capture_ready?: boolean
  capture_readiness_reason?: string | null
  live_rgb_preview_supported?: boolean
  inverted?: boolean
  mounting_mode?: string
  metadata?: Record<string, JsonValue>
}

export interface SensorStatus {
  schema_version: string
  families: Array<{
    sensor_type: string
    display_name: string
    devices: SensorDevice[]
    [key: string]: unknown
  }>
  total_connected: number
  total_capture_ready?: number
  all_expected_connected?: boolean
  [key: string]: unknown
}

export interface Job {
  id: string
  name: string
  command: string[]
  cwd: string | null
  status: string
  created_at: string
  started_at: string | null
  ended_at: string | null
  returncode: number | null
  message: string | null
  tail: string[]
  resources: string[]
  parameters: Record<string, JsonValue>
  log_path: string
  visibility: "operator" | "service"
  scope_kind: "run" | "library" | "global" | "unknown"
  run_root: string | null
  process_pid?: number | null
  process_group_id?: number | null
  process_start_time?: number | null
  supervisor_pid?: number | null
  supervisor_process_group_id?: number | null
  supervisor_start_time?: number | null
}

export interface JobPage {
  jobs: Job[]
  resources: Record<string, string>
  total: number
  status_counts: Record<string, number>
  next_cursor: string | null
  limit: number
}

export interface ClusterBlocker {
  code: string
  message: string
}

export interface ClusterProfile {
  profile_id: string
  enabled: boolean
  partition: string
  gres: string
  cpus: number
  memory: string
  walltime: string
  max_targets: number | null
}

export interface ClusterRuntimeIdentity {
  estimator_id?: string
  driver_id?: string
  runtime_id?: string
  container?: { filename?: string; sha256?: string }
  assets?: Record<string, { filename?: string; sha256?: string }>
  source_revisions?: Record<string, string>
  input_contracts?: string[]
  output_contract?: string
  qualified_resource_profiles?: string[]
  qualification_manifest_sha256?: string
  qualification_blockers?: string[]
  ready?: boolean
  foundationpose_revision?: string
  bop_toolkit_revision?: string
  sif_sha256?: string | null
  weights_sha256?: string | null
  foundationpose_license?: string
  foundationpose_license_sha256?: string
  qualified?: boolean
  [key: string]: JsonValue | undefined
}

export interface ClusterCapabilityDomain {
  ready: boolean
  read?: boolean
  mutation?: boolean
  blockers: string[]
}

export interface ClusterEstimator {
  estimator_id: string
  driver_id?: string | null
  display_name: string
  installed: boolean
  configured: boolean
  enabled: boolean
  ready: boolean
  blockers: string[]
  readiness_blockers: string[]
  input_contracts: string[]
  output_contract?: string | null
  runtime: ClusterRuntimeIdentity
  profiles: ClusterProfile[]
}

export interface ClusterStatus {
  schema_version: string
  ready: boolean
  available: boolean
  mode?: string
  connection?: Record<string, JsonValue>
  features?: Record<string, boolean>
  feature_blockers?: Record<string, string[]>
  domains?: Record<string, ClusterCapabilityDomain>
  estimators?: ClusterEstimator[]
  configuration_blockers?: string[]
  runtime?: ClusterRuntimeIdentity
  profiles?: ClusterProfile[]
  blockers: ClusterBlocker[]
  integration: {
    enabled: boolean
    controller_configured: boolean
  }
}

export interface ClusterControllerServiceStatus {
  schema_version: "posetestbot_cluster_controller_service.v1"
  managed: boolean
  service_unit: string | null
  unit_installed: boolean
  state: "unmanaged" | "unavailable" | "stopped" | "starting" | "running" | "stopping" | "failed" | "unknown"
  active: boolean
  can_start: boolean
  can_stop: boolean
  load_state: string | null
  active_state: string | null
  sub_state: string | null
  unit_file_state: string | null
  integration: {
    enabled: boolean
    controller_configured: boolean
    environment_file_configured: boolean
  }
  blockers: ClusterBlocker[]
}

export interface ClusterPoseSetup {
  schema_version: "cluster_pose_estimation_setup.v1" | "cluster_estimation_setup.v2"
  run_root: string
  ready: boolean
  estimator_id?: string | null
  estimator?: ClusterEstimator | null
  estimators?: ClusterEstimator[]
  dataset: {
    dataset_alias: string
    dataset_sha256: string
    name: string
    split: string
    scene_count: number
    frame_count: number
    model_count: number
    target_count: number
    annotation_count: number
    annotation_source: string
    status: string
    blockers: ClusterBlocker[]
    warnings: ClusterBlocker[]
  }
  annotation_mode: string | null
  oracle_mask_contract: string | null
  score_contract: string | null
  execution_contract: string | null
  controller: ClusterStatus
  runtime: ClusterRuntimeIdentity | null
  profiles: ClusterProfile[]
  enabled_profiles: ClusterProfile[]
  blockers: ClusterBlocker[]
  warnings: ClusterBlocker[]
}

export interface ClusterJob {
  schema_version: "posetestbot_cluster_job.v1"
  job_id: string
  kind: string
  state: string
  status: string
  created_at: string
  updated_at: string
  slurm_job_id: string | null
  payload: {
    run_root?: string
    estimator_id?: string
    driver_id?: string
    runtime_id?: string
    dataset_alias?: string
    dataset_sha256?: string
    profile_id?: string
    operator?: string
    [key: string]: JsonValue | undefined
  }
  result: {
    filename: string
    sha256: string
    dataset_sha256: string
    estimate_count: number
    failure_count: number
    [key: string]: JsonValue
  } | null
  error: string | null
  log_available: boolean
  cancel_requested: boolean
  terminal: boolean
}

export interface ClusterArchive {
  schema_version: "posetestbot_cluster_archive.v1"
  archive_id: string
  job_id: string
  state: string
  status: string
  source_run_root: string
  source_identity: RunFolderIdentity
  created_at: string
  updated_at: string
  archive_sha256: string | null
  operator: string
  verified: boolean
}

export interface PreviewJob {
  job: Job
  preview_root: string | null
  preview_status: {
    status: string
    frame_count: number
    latest_image: string | null
    selected_node?: Record<string, JsonValue> | null
    error?: string | null
    sensor_key?: string
    [key: string]: JsonValue | undefined
  } | null
}

export interface CaptureJob {
  id: string
  name: string
  status: string
  kind: string | null
  stage: string | null
  sequence: string | null
  run_root: string | null
  resources: string[]
  message: string | null
  created_at: string
  started_at: string | null
  ended_at: string | null
  active: boolean
  tail: string[]
  log_endpoint: string
  stop_endpoint: string | null
}

export interface CaptureState {
  run_root: string
  jobs: CaptureJob[]
  active_count: number
  resources: Record<string, string>
  status_artifact: Record<string, JsonValue> | null
}

export interface PipelineParameter {
  name: string
  flag: string
  kind: "str" | "path" | "int" | "float" | "bool"
  path_scope: "run" | "input" | "output" | "repository" | null
  required: boolean
  default: JsonValue
  choices: string[]
  multiple: boolean
  help: string
}

export interface PipelineStage {
  id: string
  label: string
  description: string
  resources: string[]
  parameters: PipelineParameter[]
}

export interface PipelineSequence {
  id: string
  label: string
  description: string
  steps: Array<{ id: string; stage_id: string; [key: string]: JsonValue }>
}

export interface CaptureSynchronization {
  schema_version: "capture_synchronization.v1"
  mode: "timestamp_aligned"
}

export interface RunConfig {
  schema_version: string
  run_name: string
  run_root: string
  robot_profile: Record<string, JsonValue>
  capture: {
    resolution: string
    fps: number
    velocity_m_s: number
    synchronization?: CaptureSynchronization
    sensors: Array<{
      sensor_type: string
      device_id: string
      display_name: string
      operator_alias: string | null
      mounting_mode: string
      enabled: boolean
      inverted: boolean
      [key: string]: JsonValue
    }>
  }
  frames?: {
    robot_pose: {
      from: "robot_flange"
      to: "template_base"
      convention: "kuka_abc_radians"
      sunrise_reference_frame_path?: string
    }
    dataset_reference_frame: "template_base"
    fixed_transforms: JsonValue[]
  }
  dataset_mode: "objectless" | "pose_template"
  pose_template?: {
    template_uuid: string
    selection_artifact: "pose_template_selection.json"
    bundle_sha256: string
    placement_confirmed: boolean
  } | null
  calibration_profiles: string | null
  intrinsic_calibration_profiles?: string | null
  calibration_profile_selection?: {
    selection_artifact: "calibration_profile_selection.json"
    bundle_sha256: string
    selected_at: string
  } | null
  calibration_target: {
    target_id: string
    bundle_path: string
    source_sha256: string
    spec_sha256: string
    pdf_sha256: string
    configuration_sha256: string
    geometry_sha256: string
    placement: {
      mode: "unknown" | "template_base_identity" | "posegridgen_board_to_base"
      mounting_frame?: "robot_flange" | "template_base"
      [key: string]: JsonValue | undefined
    }
  } | null
  pipeline: {
    sequence_id: string
    plan_only: boolean
    options: Record<string, JsonValue>
  }
}

export interface PoseTemplateSourceStatus {
  status: "available" | "missing" | "dirty" | "revision_mismatch" | "unavailable"
  available: boolean
  checkout: string
  required_revision: string
  revision: string | null
  reason: string | null
  capabilities?: {
    formats: string[]
    limits: { cad_bytes: number; batch_bytes: number; faces: number; contour_vertices: number; instances: number }
  }
}

export interface CatalogObject {
  catalog_uuid: string
  obj_id: number
  name: string
  alias: string | null
  description: string | null
  tags: string[]
  groups: string[]
  attributes: Record<string, string>
  source_filename: string
  source_format: string
  source_sha256?: string
  canonical_ply_sha256?: string
  geometry_revision?: number
  source_to_mm_scale?: number
  geometry_revisions?: Array<Record<string, JsonValue>>
  state: "active" | "archived"
  created_at: string
  updated_at: string
  extraction: { vertices: number; faces: number; bounds_mm: number[][]; watertight: boolean }
  assets: Record<string, { path: string; sha256: string; size_bytes?: number; media_type?: string }>
  usage?: {
    template_count: number
    templates: Array<{ template_uuid: string; display_name: string; state: "active" | "archived" }>
  }
}

export type Matrix4x4 = [
  [number, number, number, number],
  [number, number, number, number],
  [number, number, number, number],
  [number, number, number, number],
]

export interface PoseTemplatePreviewMesh {
  vertices: Array<[number, number, number]>
  faces: Array<[number, number, number]>
}

export interface RecognitionMeshApproximation {
  strategy: "welded_source" | "quadric_decimation" | "spatial_clustering" | "convex_proxy"
  implementation_revision: string
  source_vertices: number
  source_faces: number
  welded_vertices: number
  welded_faces: number
  result_vertices: number
  result_faces: number
  source_components: number | null
  source_euler_number: number | null
  result_components: number | null
  result_euler_number: number | null
  topology_preserved: boolean
  spatial_resolution: number | null
  fallback_reason: string | null
}

export type PoseTemplateContour =
  | Array<{ x_mm: number; y_mm: number }>
  | { points: Array<{ x_mm: number; y_mm: number }> }

export interface PoseTemplateOrientation {
  orientation_id: string
  rank?: number
  label: string
  probability: number
  source_to_placed: Matrix4x4
  slice_z_mm: number
  contours: PoseTemplateContour[]
}

export interface PoseTemplateOrientationAnalysis {
  schema_version: "pose_template_orientation_analysis.v1"
  catalog_uuid: string
  source_filename?: string
  source_sha256?: string
  orientations: PoseTemplateOrientation[]
  preview_mesh: PoseTemplatePreviewMesh
  recognition_mesh?: PoseTemplatePreviewMesh
  recognition_mesh_approximation?: RecognitionMeshApproximation
}

export interface PoseTemplateOrientationThumbnail {
  schema_version: "pose_template_orientation_thumbnail.v1"
  catalog_uuid: string
  catalog?: { catalog_uuid?: string; name?: string; obj_id?: number }
  source?: { canonical_ply_sha256?: string; geometry_revision?: number }
  preview_mesh: PoseTemplatePreviewMesh
  recognition_mesh_approximation?: RecognitionMeshApproximation
  orientation: Pick<PoseTemplateOrientation, "orientation_id" | "label" | "probability" | "slice_z_mm" | "source_to_placed"> & { rank: number }
}

export interface PoseTemplateInstanceDraft {
  instance_uuid: string
  catalog_uuid: string
  orientation_id: string
  pose: { x_mm: number; y_mm: number; rotation_deg: number }
}

export interface PoseTemplateBundle {
  template_uuid: string
  display_name: string
  description: string | null
  created_at: string
  bundle_sha256: string
  archive: { state: "active" | "archived" }
  page?: { size?: string; orientation?: string; width_mm?: number; height_mm?: number; origin_from_lower_left_mm?: [number, number] }
  print_compensation?: { x_scale: number; y_scale: number }
  configuration?: {
    page?: { size?: string; orientation?: string; width_mm?: number; height_mm?: number; origin_from_lower_left_mm?: [number, number] }
    print_compensation?: { x_scale: number; y_scale: number }
  }
  instance_count?: number
  thumbnail?: { schema_version: "pose_template_thumbnail.v1"; stored: boolean }
  instances: Array<{
    instance_uuid: string
    catalog_uuid?: string
    orientation_id?: string
    catalog: { catalog_uuid?: string; name: string; obj_id: number; canonical_ply_sha256?: string }
    pose_template_from_object?: { matrix: Matrix4x4 }
  }>
}

export interface PoseTemplatePreview {
  schema_version: "pose_template_preview.v1"
  valid: boolean
  configuration_sha256: string
  page?: { width_mm: number; height_mm: number }
  configuration?: {
    page?: { origin_from_lower_left_mm?: [number, number] }
    print_compensation?: { x_scale: number; y_scale: number }
  }
  instances: Array<{
    instance_uuid: string
    catalog_uuid?: string
    orientation_id?: string
    catalog: { catalog_uuid?: string; name: string; obj_id: number; canonical_ply_sha256?: string }
    orientation?: { label?: string } | null
    pose_template_from_object?: { matrix: Matrix4x4 }
    preview_mesh_sha256?: string | null
    compensated_contours: Array<Array<{ x_mm: number; y_mm: number }>>
  }>
  preview_meshes?: Record<string, PoseTemplatePreviewMesh>
  errors: Array<{ instance_uuid?: string; code: string; message: string }>
}

export interface PoseTemplateThumbnail {
  schema_version: "pose_template_thumbnail.v1"
  template_uuid: string
  valid: boolean
  page: { width_mm: number; height_mm: number }
  configuration: {
    page: {
      size?: string | null
      orientation?: string | null
      origin_from_lower_left_mm: [number, number]
      print_compensation_origin: "page_center" | string
    }
    print_compensation: { x_scale: number; y_scale: number }
  }
  instances: Array<{
    instance_uuid: string
    catalog: { catalog_uuid?: string; name?: string; obj_id?: number }
    compensated_contours: Array<Array<{ x_mm: number; y_mm: number }>>
    primary_contour_source_index: number
    approximation: {
      truncated: boolean
      source_contours: number
      included_contours: number
      source_points: number
      included_points: number
    }
  }>
  approximation: {
    approximate: boolean
    truncated: boolean
    strategy: string
    source_contours: number
    included_contours: number
    source_points: number
    included_points: number
    limits: { instances: number; contours: number; points: number; points_per_contour: number }
  }
}

export interface CellTransform {
  semantics: "entity_to_parent"
  parent_frame: string | null
  translation_mm: [number, number, number]
  rotation_quaternion_wxyz: [number, number, number, number]
}

export interface CellCalibrationEvidence {
  profile_id: string
  schema_version: string
  status: "valid"
  mounting_mode: "eye_in_hand" | "static"
  rig_position: string
  extrinsics: {
    from: string
    to: string
    matrix: number[][]
    rotation_quaternion_wxyz: [number, number, number, number]
    translation_mm: [number, number, number]
  }
  companion_transform: {
    from: string
    to: string
    matrix: number[][]
    rotation_quaternion_wxyz: [number, number, number, number]
    translation_mm: [number, number, number]
  } | null
  quality: {
    num_observations: number
    num_inliers: number
    mean_reprojection_error_px: number | null
    max_reprojection_error_px: number | null
    residual_translation_mm: number | null
    residual_rotation_deg: number | null
    outlier_count: number | null
    outlier_ratio: number | null
    held_out_residuals: Record<string, JsonValue> | null
    notes: string | null
  }
  evidence: {
    profile_source: string
    method: string | null
    calibration_dataset_id: string | null
    target_type: string
    target_id: string | null
    calibrated_at: string | null
    operator: string | null
    sync_delta_ms: number | null
    promotion_attempt_id: string | null
    promotion_candidate_id: string | null
    promotion_multi_camera_bundle_id: string | null
    promotion_solver_provenance: {
      solver_policy?: string
      pnp_method?: string
      extrinsic_method?: string
      [key: string]: JsonValue | undefined
    } | null
    promoted_at: string | null
    promoted_by: string | null
    intrinsic_profile_id: string | null
  }
}

export interface CellEntity {
  id: string
  type: string
  label: string
  status: "planned" | "recorded" | "reference" | "not_configured" | "unresolved"
  transform: CellTransform | null
  unresolved_reason: string | null
  geometry: Record<string, JsonValue>
  provenance: Record<string, JsonValue>
  calibration?: CellCalibrationEvidence
}

export interface CellTimelineMetadata {
  id: string
  label: string
  kind: "synchronized" | "raw"
  frame_count: number
  default: boolean
  exact: true
  interpolation: "none"
  page_limit: number
  source: string
  camera: {
    sensor_folder: string
    sensor_type: string
    device_id: string
    display_name: string
    mounting_mode: string
    inverted: boolean
    image_presentation: {
      configured_inverted: boolean
      stored_rotation_degrees: number | null
      display_rotation_degrees: number
      correction: "not_required" | "capture" | "viewer"
    }
  } | null
  camera_frames: {
    available: boolean
    rgb: {
      available: boolean
      kind: "rgb"
      media_type: "image/png"
      source: string | null
    }
    depth: {
      available: boolean
      kind: "depth"
      media_type: "image/png"
      source: string | null
      depth_scale_to_mm: number | null
      visualization: "turbo_near_warm_fixed_range"
      preview_min_depth_mm: number
      preview_max_depth_mm: number
      invalid_depth_value: 0
    }
  }
}

export interface CellPose {
  index: number
  frame_index: number
  frame_id: string
  timestamp_ns: number | null
  motion: string | null
  transform: CellTransform
}

export interface CellTrajectoryMetadata {
  entity_id: string
  label: string
  reference_frame: string
  reference_frame_label: string
  source_timeline_id: string | null
  derivation: string
}

export interface CellScene {
  schema_version: "cell_scene.v1"
  coordinate_system: Record<string, JsonValue>
  run_root: string
  entities: CellEntity[]
  warnings: Array<{ code: string; message: string }>
  timelines: CellTimelineMetadata[]
  default_timeline_id: string | null
  trajectory?: CellTrajectoryMetadata
  trajectory_preview: CellPose[]
  object_selection: {
    objectless: boolean
    dataset_mode: "objectless" | "pose_template"
    instance_count: number
    pose_template: Record<string, JsonValue> | null
    bop_export: Record<string, JsonValue>
  }
}

export interface CellTimelinePage {
  schema_version: "cell_timeline.v1"
  timeline: CellTimelineMetadata
  offset: number
  limit: number
  total: number
  next_offset: number | null
  previous_offset: number | null
  poses: CellPose[]
}

export type BopAnnotationMode = "pose" | "pose_and_masks"

export interface BopAnnotationIssue {
  code: string
  message: string
}

export interface BopAnnotationRuntime {
  available: boolean
  required_version: string | null
  detected_version: string | null
  install_command: string | null
  reason: string | null
}

export interface BopAnnotationToolkit {
  available: boolean
  status?: string
  revision?: string | null
  required_revision?: string | null
  environment_ready?: boolean
  renderer?: string | null
  install_command?: string | null
  reason?: string | null
}

export interface BopAnnotationReadiness {
  ready: boolean
  blockers: BopAnnotationIssue[]
  warnings: BopAnnotationIssue[]
}

export interface BopAnnotationOutput {
  mode: BopAnnotationMode
  state: string
  annotation_count: number
  mask_count: number
  visible_mask_count: number
  evaluation_ready: boolean
  verified: boolean
  integrity_error: string | null
  manifest_sha256: string | null
  blenderproc_version: string | null
  toolkit_revision: string | null
  [key: string]: JsonValue
}

export interface BopAnnotationSetup {
  schema_version: string
  run_root: string
  runtime: BopAnnotationRuntime
  toolkit: BopAnnotationToolkit
  readiness: BopAnnotationReadiness
  readiness_by_mode?: Record<BopAnnotationMode, BopAnnotationReadiness>
  current_output: BopAnnotationOutput | null
  counts: {
    sensors: number
    frames: number
    instances: number
  }
  provenance?: Record<string, JsonValue>
}

export interface BopEvaluationIssue {
  code: string
  message: string
}

export interface BopEvaluationToolkit {
  status: string
  available: boolean
  revision: string | null
  required_revision: string
  environment_ready: boolean
  renderer: string | null
  install_command: string | null
  reason: string | null
}

export interface BopEvaluationDataset {
  status: string
  evaluation_ready: boolean
  simulation_ready: boolean
  dataset_id: string | null
  name: string | null
  split: string | null
  export_manifest_sha256: string | null
  manifest_schema_version: string | null
  scene_count: number
  frame_count: number
  target_count: number
  model_count: number
  annotation_count: number
  annotation_source: string | null
  image_size: [number, number] | null
  result_registration_ready: boolean
  result_filename_template: string | null
  blockers: BopEvaluationIssue[]
  warnings: BopEvaluationIssue[]
}

export interface BopSimulationParameters {
  method_name?: string
  translation_sigma_mm: number
  rotation_sigma_deg: number
  seed: number
  score?: number
}

export interface BopResultSubmission {
  result_id: string
  method: string
  display_name: string
  filename: string
  source_kind: string
  created_at: string
  sha256: string
  estimate_count: number
  target_estimate_count: number
  target_coverage: number
  compatible: boolean
  blockers: BopEvaluationIssue[]
  simulation?: BopSimulationParameters | null
}

export interface BopEvaluationMetric {
  id: string
  label: string
  value: number
  display: string
  unit?: string | null
}

export interface BopEvaluationSummary {
  evaluation_id: string
  created_at: string
  completed_at: string | null
  result_id: string | null
  result: BopResultSubmission | null
  source_kind: string
  simulation?: BopSimulationParameters | null
  protocol: string
  status: string
  metrics: BopEvaluationMetric[]
  provenance: Record<string, JsonValue>
  report_available: boolean
}

export interface BopEvaluationSetup {
  toolkit: BopEvaluationToolkit
  dataset: BopEvaluationDataset
  results: BopResultSubmission[]
  evaluations: BopEvaluationSummary[]
}

export interface PreflightSummary {
  queue_blocker?: string | null
  status?: string
  path?: string
  [key: string]: JsonValue | undefined
}

export interface ApiErrorBody {
  output?: string
  [key: string]: unknown
}
