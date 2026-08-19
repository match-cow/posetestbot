"""Canonical artifact names used by current PoseTestBot workflows."""

from __future__ import annotations

RAW_ROBOT_EE_POSES = "raw_robot_ee_poses.json"
ROBOT_POSE_CADENCE_REPORT = "robot_pose_cadence_report.json"
MATCH_ROBOT_EE_POSES = "match_robot_ee_poses.json"
ARUCO_DETECTIONS = "aruco_detections.json"
CAM_K = "cam_K.txt"
DEPTH_SCALE = "depthscale.txt"
CAMERA_JSON = "camera.json"
CAMERA_DATA_JSON = "camera_data.json"
FRAME_METADATA_JSONL = "frame_metadata.jsonl"
DATASET_MANIFEST = "dataset_manifest.json"
RUN_CONFIG = "run_config.json"
RUN_PREFLIGHT_REPORT = "run_preflight_report.json"
HARDWARE_STATUS_REPORT = "hardware_status_report.json"
CAPTURE_PLAN = "capture_plan.json"
CAPTURE_PLAN_PREFLIGHT_REPORT = "capture_plan_preflight_report.json"
CAPTURE_EXECUTION_PLAN = "capture_execution_plan.json"
CAPTURE_EXECUTION_STATUS = "capture_execution_status.json"
CAPTURE_EXECUTION_REPORT = "capture_execution_report.json"
CAPTURE_EXECUTION_LOGS_DIR = "capture_execution_logs"
REALSENSE_CAPTURE_SMOKE_REPORT = "realsense_capture_smoke_report.json"
SYNC_REPORT = "sync_report.json"
SYNC_QUALITY_REPORT = "sync_quality_report.json"
BLENDERPROC_RENDER_PLAN = "blenderproc_render_plan.json"
BOP_ANNOTATION_GENERATION_REPORT = "generation_report.json"
POSE_TEMPLATE_SELECTION = "pose_template_selection.json"
OBJECT_INSTANCES = "object_instances.json"
BOP_EXPORT_MANIFEST = "bop_export_manifest.json"
BOP_FRAME_MAP_JSON = "posetestbot_bop_frame_map.json"
BOP_DATASET_INFO = "dataset_info.json"
BOP_TARGETS_BOP19 = "test_targets_bop19.json"
BOP_COCO_ANNOTATIONS = "posetestbot_coco_annotations.json"
BOP_POSE_TEMPLATE = "posetestbot_pose_template.json"
BOP_INSTANCE_MAP = "posetestbot_instance_map.json"
CALIBRATION_PROFILES = "calibration_profiles.json"
CALIBRATION_PROFILE_SELECTION = "calibration_profile_selection.json"
CALIBRATION_TARGET = "calibration_target.json"
INTRINSIC_CALIBRATION_PROFILES = "intrinsic_calibration_profiles.json"
INTRINSIC_COMPARISON = "intrinsic_comparison.json"
TIME_OFFSET_SEARCH = "time_offset_search.json"
CAMERA_RECTIFICATION_REPORT = "camera_rectification_report.json"
DERIVED_CAMERA_EE_TRANSFORM = "camera_ee_transform_from_calibration_profiles.json"

CURRENT_SENSOR_METADATA_ARTIFACTS = (
    CAM_K,
    DEPTH_SCALE,
    CAMERA_JSON,
    CAMERA_DATA_JSON,
    FRAME_METADATA_JSONL,
)

RGB_DIR = "rgb"
DEPTH_DIR = "depth"
MASKS_DIR = "masks"
BOP_DIR = "bop"
MODELS_DIR = "models"
MODELS_EVAL_DIR = "models_eval"
CALIBRATION_DIR = "calibration"
PROCESSED_DIR = "processed"
SYNCHRONIZED_DIR = "synchronized"
