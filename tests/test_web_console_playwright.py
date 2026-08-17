from __future__ import annotations

import json
import re
import threading
from urllib.parse import urlparse
import pytest
from werkzeug.serving import make_server

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from posetestbot.web.app import create_app


pytestmark = pytest.mark.playwright

RUN_ROOT = "/tmp/posetestbot-console/new-run"
LOCAL_JOBS_URL_RE = re.compile(r"^https?://[^/]+/jobs(?:\?.*)?$")


class LiveServer:
    def __init__(self):
        self.server = make_server("127.0.0.1", 0, create_app(), threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)


@pytest.fixture(scope="module")
def console_server():
    server = LiveServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.fail(
                "Playwright Chromium is not installed; run "
                "`UV_CACHE_DIR=/tmp/uv-cache uv run playwright install chromium`. "
                f"Original error: {exc}"
            )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            yield page
        finally:
            browser.close()


def fulfill_json(route, value: object, *, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(value),
    )


def run_config(*, plan_only: bool = True, sensors: list[dict] | None = None) -> dict:
    return {
        "schema_version": "run_config.v3",
        "run_name": "new-run",
        "run_root": RUN_ROOT,
        "robot_profile": {
            "mode": "real",
            "robot_ip": "172.31.1.147",
            "command_port": 30300,
            "receiver_ip": "172.31.1.169",
            "receiver_port": 8080,
            "cartesian_velocity_m_s": 0.2,
        },
        "capture": {
            "resolution": "720p",
            "fps": 6,
            "velocity_m_s": 0.2,
            "sensors": sensors or [],
            "synchronization": {
                "schema_version": "capture_synchronization.v1",
                "mode": "timestamp_aligned",
            },
        },
        "frames": {
            "robot_pose": {
                "from": "robot_flange",
                "to": "template_base",
                "convention": "kuka_abc_radians",
            },
            "dataset_reference_frame": "template_base",
            "fixed_transforms": [],
        },
        "dataset_mode": "objectless",
        "pose_template": None,
        "calibration_profiles": None,
        "calibration_target": None,
        "pipeline": {
            "sequence_id": "real_full_capture_validation",
            "plan_only": plan_only,
            "options": {},
        },
    }


def run_folder_inventory() -> dict:
    def record(
        *,
        folder: str,
        run_name: str,
        modified_at: str,
        sequence: str,
        object_names: list[str],
        object_count: int,
        size_bytes: int,
    ) -> dict:
        path = f"/tmp/posetestbot-console/{folder}"
        return {
            "path": path,
            "name": folder,
            "root": "/tmp/posetestbot-console",
            "modified_at": modified_at,
            "size_bytes": size_bytes,
            "allocated_bytes": size_bytes,
            "file_count": 12,
            "directory_count": 4,
            "symlink_count": 0,
            "scan_complete": True,
            "scan_error_count": 0,
            "scan_errors": [],
            "identity": {"device": 1, "inode": 10 if folder == "new-run" else 11},
            "config": {
                "valid": True,
                "error": None,
                "run_name": run_name,
                "sequence": sequence,
                "plan_only": True,
            },
            "contents": {
                "dataset_mode": "pose_template" if object_count else "objectless",
                "resolution": "720p",
                "fps": 6,
                "synchronization_mode": "timestamp_aligned",
                "sensor_count": 1,
                "enabled_sensor_count": 1,
                "sensors": [
                    {
                        "sensor_type": "realsense",
                        "device_id": "123",
                        "name": "Front D435",
                        "mounting_mode": "static",
                        "enabled": True,
                    }
                ],
                "object_count": object_count,
                "object_names": object_names,
                "template_uuid": "template-a" if object_count else None,
                "evidence": {
                    "raw_capture": folder == "old-run",
                    "synchronized": False,
                    "calibration": folder == "new-run",
                    "bop_export": False,
                    "bop_evaluation": False,
                },
            },
            "breakdown": {
                "other": {
                    "size_bytes": size_bytes,
                    "allocated_bytes": size_bytes,
                    "file_count": 12,
                }
            },
            "relocation": None,
        }

    gib = 1024**3
    return {
        "schema_version": "run_folder_inventory.v1",
        "generated_at": "2026-08-06T10:00:00Z",
        "inventory_state": "ready",
        "stale": False,
        "roots": [
            {
                "path": "/tmp/posetestbot-console",
                "exists": True,
                "identity": {"device": 1, "inode": 2},
                "storage": {
                    "schema_version": "run_storage.v1",
                    "run_root": "/tmp/posetestbot-console",
                    "filesystem_path": "/tmp",
                    "status": "ready",
                    "total_bytes": 1000 * gib,
                    "used_bytes": 200 * gib,
                    "free_bytes": 800 * gib,
                    "free_fraction": 0.8,
                    "thresholds": {
                        "critical_free_bytes": 100 * gib,
                        "warning_free_bytes": 150 * gib,
                        "critical_free_bytes_cap": 100 * gib,
                        "warning_free_bytes_cap": 500 * gib,
                        "critical_free_fraction": 0.05,
                        "warning_free_fraction": 0.15,
                    },
                    "error": None,
                },
            }
        ],
        "runs": [
            record(
                folder="new-run",
                run_name="Calibration baseline",
                modified_at="2026-08-06T09:00:00Z",
                sequence="real_full_capture_validation",
                object_names=[],
                object_count=0,
                size_bytes=2 * gib,
            ),
            record(
                folder="old-run",
                run_name="Object A capture",
                modified_at="2026-08-05T09:00:00Z",
                sequence="sync_aruco",
                object_names=["Object A"],
                object_count=1,
                size_bytes=gib,
            ),
        ],
        "refresh_job": None,
        "operation_job": None,
        "maintenance": {
            "schema_version": "run_folder_maintenance.v1",
            "recovered_count": 0,
            "transactions": [],
            "unresolved_count": 0,
            "journal_fingerprint": "test",
            "unresolved": [],
        },
    }


def calibration_selection_artifact(
    *,
    bundle_sha256: str,
    calibration_profiles: str,
    intrinsic_calibration_profiles: str,
    source_run_root: str = "/tmp/posetestbot-console/calibration-source",
    source_run_name: str = "Reusable calibration",
) -> dict:
    return {
        "schema_version": "calibration_profile_selection.v1",
        "selected_at": "2026-07-22T12:00:00+00:00",
        "operator": "web_operator",
        "source": {
            "run_root": source_run_root,
            "run_name": source_run_name,
            "bundle_sha256": bundle_sha256,
        },
        "snapshot": {
            "calibration_profiles": {
                "relative_path": calibration_profiles,
                "sha256": "b" * 64,
            },
            "intrinsic_calibration_profiles": {
                "relative_path": intrinsic_calibration_profiles,
                "sha256": "c" * 64,
            },
        },
        "sensor_profiles": {
            "realsense_d435:wrist-1": "profile-wrist-1",
        },
    }


def valid_library_selection(**kwargs) -> dict:
    return {
        **calibration_selection_artifact(**kwargs),
        "valid": True,
        "issues": [],
    }


def overview_payload(config: dict | None = None) -> dict:
    resolved_config = config or run_config()
    selected_bundle = (resolved_config.get("calibration_profile_selection") or {}).get(
        "bundle_sha256", "a" * 64
    )
    sections = [
        ("run_setup", "Run Setup", "complete"),
        ("preflight", "Preflight", "pending"),
        ("capture", "Capture", "pending"),
        ("sync", "Sync", "pending"),
        ("calibration", "Calibration", "pending"),
        ("bop", "BOP Export", "pending"),
    ]
    return {
        "run_root": RUN_ROOT,
        "config": resolved_config,
        "config_error": None,
        "calibration_sync": {
            "status": "ready",
            "bundle_sha256": selected_bundle,
            "sensors": [
                {
                    "sensor_key": "realsense_d435:wrist-1",
                    "sensor_name": "Wrist RGB-D",
                    "sensor_folder": "realsense_wrist-1",
                    "profile_id": "profile-wrist-1",
                    "robot_pose_time_offset_ms": 70.0,
                    "sync_delta_ms": -70.0,
                    "frame_timestamp_source": "sensor",
                    "robot_timestamp_source": "host_wall",
                    "required_frame_timestamp_domain": "global_time",
                    "timestamp_fallback_allowed": False,
                    "max_nearest_pose_delta_ms": 20.0,
                }
            ],
        },
        "sidebar": [
            {"id": id_, "label": label, "status": status, "artifacts": []}
            for id_, label, status in sections
        ],
        "steps": [],
        "recommendations": [
            {
                "label": "Run preflight",
                "description": "Create fresh readiness evidence before capture.",
            }
        ],
        "recommendation_error": None,
    }


def selected_sensor_status() -> dict:
    return {
        "schema_version": "sensor_status.v1",
        "families": [
            {
                "sensor_type": "realsense_d435",
                "display_name": "Intel RealSense D435",
                "devices": [
                    {
                        "sensor_type": "realsense_d435",
                        "device_id": "wrist-1",
                        "display_name": "RealSense wrist",
                        "effective_display_name": "Wrist RGB-D",
                        "connected": True,
                        "mounting_mode": "eye_in_hand",
                        "inverted": False,
                    },
                    {
                        "sensor_type": "realsense_d435",
                        "device_id": "static-1",
                        "display_name": "RealSense static",
                        "effective_display_name": "Static RGB-D",
                        "connected": True,
                        "mounting_mode": "static",
                        "inverted": True,
                    },
                ],
            }
        ],
        "total_connected": 2,
        "all_expected_connected": True,
    }


def cluster_profile() -> dict:
    return {
        "profile_id": "smoke-a100",
        "enabled": True,
        "partition": "gpu",
        "gres": "gpu:a100:1",
        "cpus": 8,
        "memory": "32G",
        "walltime": "00:15:00",
        "max_targets": 12,
    }


def cluster_pose_setup(*, ready: bool = True) -> dict:
    blocker = {
        "code": "runtime_unqualified",
        "message": "The pinned FoundationPose runtime has not been qualified.",
    }
    profile = cluster_profile()
    runtime = {
        "estimator_id": "foundationpose",
        "driver_id": "foundationpose.v1",
        "runtime_id": "foundationpose-a1b694b8",
        "container": {"filename": "foundationpose.sif", "sha256": "a" * 64},
        "assets": {
            "weights-manifest": {
                "filename": "weights-manifest.json",
                "sha256": "b" * 64,
            }
        },
        "source_revisions": {
            "foundationpose": "a1b694b83e633c2cb6115b9063d940a687759392",
            "bop_toolkit": "cea62d651c7e395b2e1962b9749e4e89693c6ac4",
        },
        "input_contracts": ["posetestbot.bop.v5.pose_and_masks"],
        "output_contract": "bop19.csv.v1",
        "qualified_resource_profiles": ["smoke-a100"],
        "qualification_manifest_sha256": "c" * 64,
        "qualified": ready,
        "ready": ready,
    }
    estimator = {
        "estimator_id": "foundationpose",
        "driver_id": "foundationpose.v1",
        "display_name": "FoundationPose",
        "installed": True,
        "configured": ready,
        "enabled": ready,
        "ready": ready,
        "blockers": [] if ready else [blocker["message"]],
        "readiness_blockers": [] if ready else [blocker["message"]],
        "input_contracts": ["posetestbot.bop.v5.pose_and_masks"],
        "output_contract": "bop19.csv.v1",
        "runtime": runtime,
        "profiles": [profile],
    }
    return {
        "schema_version": "cluster_estimation_setup.v2",
        "run_root": RUN_ROOT,
        "ready": ready,
        "dataset": {
            "dataset_alias": "ptb123456789abc",
            "dataset_sha256": "d" * 64,
            "name": "PoseTestBot",
            "split": "test",
            "scene_count": 3,
            "frame_count": 24,
            "model_count": 2,
            "target_count": 31,
            "annotation_count": 31,
            "annotation_source": "blenderproc",
            "status": "ready",
            "blockers": [],
            "warnings": [],
        },
        "annotation_mode": "pose_and_masks",
        "oracle_mask_contract": "bop_mask_visib_gt_instance",
        "score_contract": "oracle_mask_score_1.0",
        "execution_contract": "independent_per_target_no_tracking",
        "controller": {
            "schema_version": "posetestbot_cluster_status.v1",
            "ready": ready,
            "available": True,
            "mode": "production",
            "connection": {"login": "ready", "transfer": "ready"},
            "features": {"pose_estimation": ready},
            "feature_blockers": {},
            "domains": {
                "storage": {
                    "ready": ready,
                    "read": True,
                    "mutation": ready,
                    "blockers": [],
                },
                "scheduler": {"ready": ready, "blockers": []},
            },
            "estimators": [estimator],
            "runtime": runtime,
            "profiles": [profile],
            "blockers": [] if ready else [blocker],
            "integration": {
                "enabled": True,
            },
        },
        "estimator_id": "foundationpose",
        "estimator": estimator,
        "estimators": [estimator],
        "runtime": runtime,
        "profiles": [profile],
        "enabled_profiles": [profile] if ready else [],
        "blockers": [] if ready else [blocker],
        "warnings": [],
    }


def cluster_pose_job(*, state: str, with_result: bool = False) -> dict:
    return {
        "schema_version": "posetestbot_cluster_job.v1",
        "job_id": "pose-11111111-1111-4111-8111-111111111111",
        "kind": "pose_estimation",
        "state": state,
        "status": state,
        "created_at": "2026-08-04T10:00:00Z",
        "updated_at": "2026-08-04T10:01:00Z",
        "slurm_job_id": "482991",
        "payload": {
            "run_root": RUN_ROOT,
            "estimator_id": "foundationpose",
            "driver_id": "foundationpose.v1",
            "runtime_id": "foundationpose-a1b694b8",
            "dataset_alias": "ptb123456789abc",
            "dataset_sha256": "d" * 64,
            "profile_id": "smoke-a100",
            "operator": "Test Operator",
        },
        "result": (
            {
                "filename": (
                    "foundationpose_ptb123456789abc-test_"
                    "pose-11111111-1111-4111-8111-111111111111.csv"
                ),
                "sha256": "e" * 64,
                "dataset_sha256": "d" * 64,
                "estimate_count": 29,
                "failure_count": 2,
            }
            if with_result
            else None
        ),
        "error": None,
        "log_available": True,
        "cancel_requested": False,
        "terminal": state
        in {"succeeded", "succeeded-with-warning", "failed", "canceled"},
    }


def install_common_mocks(
    page,
    *,
    preflight_state: dict | None = None,
    requests: list[dict] | None = None,
    generator_available: bool = False,
    config_payload: dict | None = None,
) -> None:
    requests = requests if requests is not None else []
    preflight_state = (
        preflight_state if preflight_state is not None else {"blocker": None}
    )
    config_payload = config_payload if config_payload is not None else run_config()

    page.route(
        "**/ui/bootstrap",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "web_bootstrap.v1",
                "brand": {
                    "name": "PoseTestBot",
                    "logo_url": "/assets/cow_light.png",
                    "logo_urls": {
                        "light": "/assets/cow_light.png",
                        "dark": "/assets/cow_dark.png",
                    },
                    "favicon_url": "/assets/cow_favicon.png",
                },
                "robot": {"ip": "172.31.1.147", "port": 30300},
                "default_run_root": "/tmp/posetestbot-console/default",
                "allowed_run_roots": ["/tmp/posetestbot-console"],
            },
        ),
    )
    page.route(
        "**/ui/runs",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "web_run_index.v1",
                "runs": [
                    {
                        "path": RUN_ROOT,
                        "name": "new-run",
                        "run_name": "Calibration baseline",
                        "sequence": "real_full_capture_validation",
                        "plan_only": True,
                        "config_valid": True,
                        "config_error": None,
                        "modified_at": "2026-07-10T12:00:00Z",
                    },
                    {
                        "path": "/tmp/posetestbot-console/old-run",
                        "name": "old-run",
                        "run_name": "Object A capture",
                        "sequence": "sync_aruco",
                        "plan_only": True,
                        "config_valid": True,
                        "config_error": None,
                        "modified_at": "2026-07-09T12:00:00Z",
                    },
                ],
            },
        ),
    )
    page.route(
        "**/ui/run-folders",
        lambda route: fulfill_json(route, run_folder_inventory()),
    )
    page.route(
        "**/cluster/archives",
        lambda route: fulfill_json(
            route,
            {"archives": [], "integration": {"enabled": False}},
        ),
    )
    page.route(
        "**/calibration-targets/status",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "calibration_target_generator_status.v1",
                "generation_available": generator_available,
                "generator": {
                    "checkout": "/repo/third_party/PoseGridGen",
                    "required_revision": "9e6975901fe096bf65f7b7b599d7b82461d2e67c",
                    "reason": None
                    if generator_available
                    else "Pinned source checkout is unavailable",
                },
            },
        ),
    )
    page.route(
        "**/ui/overview**",
        lambda route: fulfill_json(route, overview_payload(config_payload)),
    )
    page.route(
        "**/ui/storage**",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "run_storage.v1",
                "run_root": RUN_ROOT,
                "filesystem_path": "/tmp",
                "status": "ready",
                "total_bytes": 3 * 1024**4,
                "used_bytes": 7 * 1024**4 // 4,
                "free_bytes": 5 * 1024**4 // 4,
                "free_fraction": 5 / 12,
                "thresholds": {
                    "critical_free_bytes": 100 * 1024**3,
                    "warning_free_bytes": int(3 * 1024**4 * 0.15),
                    "critical_free_bytes_cap": 100 * 1024**3,
                    "warning_free_bytes_cap": 500 * 1024**3,
                    "critical_free_fraction": 0.05,
                    "warning_free_fraction": 0.15,
                },
                "error": None,
            },
        ),
    )
    page.route(
        "**/sensors/status",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "sensor_status.v1",
                "families": [],
                "total_connected": 0,
                "all_expected_connected": True,
            },
        ),
    )
    page.route(
        "**/robot/status",
        lambda route: fulfill_json(
            route,
            {"schema_version": "robot_status.v2", "selected_profile": {"mode": "real"}},
        ),
    )
    page.route(
        "**/runtime/status",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "runtime_status.v1",
                "runtimes": [{"runtime_id": "blenderproc", "available": True}],
            },
        ),
    )
    page.route(
        "**/cluster/controller-service",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "posetestbot_cluster_controller_service.v1",
                "managed": False,
                "service_unit": None,
                "unit_installed": False,
                "state": "unmanaged",
                "active": False,
                "can_start": False,
                "can_stop": False,
                "load_state": None,
                "active_state": None,
                "sub_state": None,
                "unit_file_state": None,
                "integration": {
                    "enabled": False,
                    "controller_configured": False,
                    "environment_file_configured": False,
                },
                "blockers": [
                    {
                        "code": "service_management_not_configured",
                        "message": "Controller lifecycle management is not configured.",
                    }
                ],
            },
        ),
    )
    page.route(
        "**/cluster/status",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "posetestbot_cluster_status_proxy.v1",
                "ready": False,
                "available": False,
                "integration": {
                    "enabled": False,
                    "controller_configured": False,
                },
                "blockers": [
                    {
                        "code": "cluster_disabled",
                        "message": "Cluster integration is disabled on this workstation.",
                    }
                ],
            },
        ),
    )
    page.route(
        "**/bop/annotations/setup?**",
        lambda route: fulfill_json(
            route,
            {
                "schema_version": "bop_annotation_setup.v1",
                "run_root": RUN_ROOT,
                "runtime": {
                    "available": True,
                    "required_version": "2.8.0",
                    "detected_version": "2.8.0",
                    "install_command": None,
                    "reason": None,
                },
                "toolkit": {
                    "available": True,
                    "status": "ready",
                    "revision": "renderer-revision",
                    "required_revision": "renderer-revision",
                    "environment_ready": True,
                    "renderer": "vispy",
                    "install_command": None,
                    "reason": None,
                },
                "readiness": {
                    "ready": False,
                    "blockers": [
                        {
                            "code": "bop_export_missing",
                            "message": "Complete the base BOP export first.",
                        }
                    ],
                    "warnings": [],
                },
                "readiness_by_mode": {
                    "pose": {
                        "ready": False,
                        "blockers": [
                            {
                                "code": "bop_export_missing",
                                "message": "Complete the base BOP export first.",
                            }
                        ],
                        "warnings": [],
                    },
                    "pose_and_masks": {
                        "ready": False,
                        "blockers": [
                            {
                                "code": "bop_export_missing",
                                "message": "Complete the base BOP export first.",
                            }
                        ],
                        "warnings": [],
                    },
                },
                "current_output": None,
                "counts": {"sensors": 0, "frames": 0, "instances": 0},
            },
        ),
    )
    page.route(
        "**/capture/jobs**",
        lambda route: fulfill_json(
            route,
            {"jobs": [], "active_count": 0, "resources": {}, "status_artifact": None},
        ),
    )
    page.route(
        LOCAL_JOBS_URL_RE,
        lambda route: fulfill_json(
            route,
            {
                "jobs": [],
                "resources": {},
                "total": 0,
                "status_counts": {},
                "next_cursor": None,
                "limit": 20,
            },
        ),
    )
    page.route(
        "**/monitoring/webcam",
        lambda route: fulfill_json(
            route,
            {
                "job": {"id": "monitor-1", "status": "failed"},
                "webrtc_status": {
                    "schema_version": "monitor_webrtc.v1",
                    "transport": "webrtc",
                    "status": "failed",
                    "signaling_ready": False,
                    "peer_count": 0,
                    "frame_count": 0,
                    "selected_node": None,
                    "error": "mock camera offline",
                },
            },
        ),
    )
    page.route(
        "**/pipeline/sequences",
        lambda route: fulfill_json(
            route,
            {
                "sequences": [
                    {
                        "id": "real_full_capture_validation",
                        "label": "Real Full Capture Validation",
                        "description": "Safe plan",
                        "steps": [],
                    }
                ]
            },
        ),
    )
    page.route(
        "**/pipeline/stages",
        lambda route: fulfill_json(
            route,
            {
                "stages": [
                    {
                        "id": "capture_plan",
                        "label": "Capture Plan",
                        "description": "Write a command plan without hardware.",
                        "resources": ["disk_io"],
                        "parameters": [],
                    }
                ]
            },
        ),
    )

    def config_handler(route) -> None:
        if route.request.method == "POST":
            requests.append(
                {"path": "/run-config", "body": route.request.post_data_json}
            )
            fulfill_json(
                route, {"config": config_payload, "output": "written"}, status=201
            )
        else:
            fulfill_json(
                route,
                {
                    "config": config_payload,
                    "preflight": {"queue_blocker": preflight_state["blocker"]},
                },
            )

    page.route("**/run-config**", config_handler)

    def pipeline_handler(route) -> None:
        requests.append({"path": "/pipeline/run", "body": route.request.post_data_json})
        fulfill_json(
            route, {"job_id": f"job-{len(requests)}", "status": "queued"}, status=202
        )

    page.route("**/pipeline/run", pipeline_handler)
    page.route(
        "**/sensors/previews/stop",
        lambda route: (
            requests.append({"path": "/sensors/previews/stop", "body": {}}),
            fulfill_json(route, {"jobs": []}),
        )[1],
    )


def test_navigation_run_fallback_persistence_and_both_themes(
    console_server, page
) -> None:
    install_common_mocks(page)
    page.emulate_media(color_scheme="dark")
    page.add_init_script(
        "if (!localStorage.getItem('posetestbot.selectedRun')) localStorage.setItem('posetestbot.selectedRun', '/tmp/posetestbot-console/deleted-run'); localStorage.removeItem('posetestbot.theme')"
    )

    page.goto(console_server.url, wait_until="networkidle")

    expect(page.locator("html")).to_have_class("dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.get_by_role("img", name="PoseTestBot")).to_have_attribute(
        "src", "/assets/cow_dark.png"
    )
    primary_navigation = page.get_by_role("navigation", name="Primary navigation")
    assert primary_navigation.get_by_role("link").all_inner_texts() == [
        "Dashboard",
        "Workflow",
        "Devices",
        "Calibration Targets",
        "Workpiece Catalogue",
        "Pose Templates",
        "Cell View",
        "Run folders",
        "Pose Estimation",
        "BOP Evaluation",
        "Jobs",
    ]
    expect(primary_navigation.get_by_role("link", name="Workflow")).to_have_attribute(
        "href", "#/workflow/setup"
    )
    expect(page.get_by_text("Recommended next action", exact=True)).to_have_count(0)
    workflow_overview = page.get_by_test_id("dashboard-workflow-overview")
    expect(workflow_overview).to_have_attribute("data-workflow-journey", "calibration")
    expect(
        workflow_overview.get_by_role("heading", name="Camera calibration workflow")
    ).to_be_visible()
    expect(workflow_overview.locator("[data-workflow-step]")).to_have_count(5)
    expect(
        page.get_by_role(
            "link",
            name="Open camera calibration step 1: Configure the run and cameras",
        )
    ).to_have_attribute("href", "#/workflow/calibration?step=configure")
    assert page.evaluate("localStorage.getItem('posetestbot.theme')") is None
    assert page.evaluate("localStorage.getItem('posetestbot.selectedRun')") is None
    active_run_context = page.get_by_test_id("active-run-context")
    expect(active_run_context).to_contain_text("Active acquisition run")
    expect(active_run_context.get_by_test_id("active-run-name")).to_have_text(
        "Calibration baseline"
    )
    expect(active_run_context.get_by_test_id("active-run-path")).to_have_text(RUN_ROOT)
    expect(page.get_by_role("combobox", name="Active run folder")).to_have_count(0)
    change_run = page.get_by_role("link", name="Change active run folder")
    expect(change_run).to_have_attribute("href", "#/run-folders")
    summary_box = active_run_context.locator(":scope > div").bounding_box()
    change_box = change_run.bounding_box()
    app_header_box = page.locator("header").first.bounding_box()
    assert summary_box is not None and change_box is not None and app_header_box is not None
    assert summary_box["height"] == pytest.approx(change_box["height"], abs=1)
    assert summary_box["y"] == pytest.approx(change_box["y"], abs=1)
    top_gap = summary_box["y"] - app_header_box["y"]
    bottom_gap = (
        app_header_box["y"]
        + app_header_box["height"]
        - summary_box["y"]
        - summary_box["height"]
    )
    assert top_gap == pytest.approx(bottom_gap, abs=1)
    change_run.click()
    expect(page).to_have_url(f"{console_server.url}/#/run-folders")
    expect(page.get_by_role("heading", name="Run folders", exact=True)).to_be_visible()
    cluster_storage = page.get_by_test_id("cluster-storage-section")
    expect(cluster_storage.get_by_role("heading", name="Cluster storage")).to_be_visible()
    expect(cluster_storage).to_contain_text(
        "independent of every pose-estimator runtime"
    )
    active_selection = page.get_by_test_id("active-run-selection")
    expect(active_selection).to_contain_text("Calibration baseline")
    expect(active_selection).to_contain_text(RUN_ROOT)
    cluster_storage_box = cluster_storage.bounding_box()
    active_selection_box = active_selection.bounding_box()
    assert cluster_storage_box is not None and active_selection_box is not None
    assert cluster_storage_box["y"] < active_selection_box["y"]
    assert cluster_storage_box["y"] < 1080
    storage_search = page.get_by_role("textbox", name="Search storage inventory")
    storage_search.fill("Object A")
    storage_table = page.get_by_test_id("run-folders-table")
    expect(storage_table.get_by_test_id("run-folder-row")).to_have_count(1)
    expect(storage_table.get_by_test_id("run-folder-row")).to_contain_text(
        "Object A capture"
    )
    storage_search.fill("")
    chooser = page.get_by_test_id("run-folder-chooser")
    expect(chooser.get_by_role("heading", name="Choose an existing run")).to_be_visible()
    chooser.get_by_role("textbox", name="Search run folders").fill("Object A")
    expect(chooser.get_by_test_id("run-selection-row")).to_have_count(1)
    expect(chooser.get_by_test_id("run-selection-row")).to_contain_text(
        "Object A capture"
    )
    chooser.get_by_role("button", name="Use old-run as active run").click()
    expect(active_run_context).to_contain_text("Object A capture")
    expect(active_run_context).to_contain_text(
        "/tmp/posetestbot-console/old-run"
    )
    assert (
        page.evaluate("localStorage.getItem('posetestbot.selectedRun')")
        == "/tmp/posetestbot-console/old-run"
    )
    page.get_by_role("complementary", name="Application sidebar").get_by_role(
        "link", name="Devices"
    ).click()
    expect(page).to_have_url(f"{console_server.url}/#/devices")
    page.reload(wait_until="networkidle")
    expect(active_run_context).to_contain_text("Object A capture")
    page.get_by_role("link", name="Change active run folder").click()
    expect(
        page.get_by_text("Use one sibling folder per physical acquisition.")
    ).to_be_visible()
    expect(page.get_by_role("combobox", name="New run storage root")).to_contain_text(
        "/tmp/posetestbot-console"
    )
    expect(page.locator("#new-run-folder-name")).to_have_value("")
    custom_run = "/tmp/posetestbot-console/unlisted-run"
    page.locator("#new-run-folder-name").fill("unlisted-run")
    expect(page.get_by_test_id("new-run-path-preview")).to_have_text(custom_run)
    page.get_by_role("button", name="Use new run folder", exact=True).click()
    expect(active_run_context).to_contain_text("unlisted-run")
    expect(active_run_context).to_contain_text(custom_run)
    expect(active_run_context).to_contain_text("Not configured")
    assert (
        page.evaluate("localStorage.getItem('posetestbot.selectedRun')") == custom_run
    )
    page.reload(wait_until="networkidle")
    expect(active_run_context).to_contain_text("unlisted-run")
    expect(active_run_context).to_contain_text(custom_run)
    assert (
        page.evaluate("localStorage.getItem('posetestbot.selectedRun')") == custom_run
    )
    page.get_by_role("button", name="Open operator console guide").click()
    expect(page.get_by_role("heading", name="Operator console guide")).to_be_visible()
    expect(
        page.get_by_text("Choose an outcome in Workflow", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_text("IIWA STOP is not a safety stop", exact=False)
    ).to_be_visible()
    page.keyboard.press("Escape")
    theme_toggle = page.get_by_role("button", name="Switch to light theme")
    theme_toggle_box = theme_toggle.bounding_box()
    assert theme_toggle_box is not None
    assert theme_toggle_box["width"] == pytest.approx(34)
    assert theme_toggle_box["height"] == pytest.approx(34)
    theme_toggle.click()
    expect(page.locator("html")).to_have_class("light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    expect(page.get_by_role("img", name="PoseTestBot")).to_have_attribute(
        "src", "/assets/cow_light.png"
    )
    assert page.evaluate("localStorage.getItem('posetestbot.theme')") == "light"
    expect(
        page.get_by_role("link", name="Open PoseTestBot on GitHub")
    ).to_have_attribute("href", "https://github.com/match-cow/PoseTestBot")
    sidebar_rgb = page.get_by_role(
        "complementary", name="Application sidebar"
    ).evaluate(
        """element => {
            const canvas = document.createElement("canvas")
            canvas.width = 1
            canvas.height = 1
            const context = canvas.getContext("2d")
            context.fillStyle = getComputedStyle(element).backgroundColor
            context.fillRect(0, 0, 1, 1)
            return Array.from(context.getImageData(0, 0, 1, 1).data)
        }"""
    )
    assert min(sidebar_rgb[:3]) > 220
    expect(
        page.get_by_text(
            "Physical capture always requires fresh operator acknowledgement.",
            exact=True,
        )
    ).to_have_count(0)
    expect(page.get_by_role("img", name="PoseTestBot")).to_have_css(
        "background-color", "rgba(0, 0, 0, 0)"
    )
    expect(page.get_by_role("img", name="PoseTestBot")).to_have_css("padding", "0px")


def test_active_run_header_alignment_and_change_affordance(
    console_server, page
) -> None:
    install_common_mocks(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto(f"{console_server.url}/#/dashboard", wait_until="networkidle")

    active_run_context = page.get_by_test_id("active-run-context")
    change_run = page.get_by_role("link", name="Change active run folder")
    active_context_box = active_run_context.bounding_box()
    page_content_box = page.locator("main > div").first.bounding_box()

    assert active_context_box is not None and page_content_box is not None
    assert active_context_box["x"] == pytest.approx(page_content_box["x"], abs=1)
    assert (
        active_context_box["x"] + active_context_box["width"]
        <= page_content_box["x"] + page_content_box["width"] + 1
    )
    expect(change_run).to_have_css("background-color", "rgb(177, 203, 33)")
    expect(change_run).to_contain_text("Change run")


def test_dashboard_starts_and_stops_managed_cluster_controller(
    console_server, page
) -> None:
    install_common_mocks(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    actions: list[str] = []
    blocked_estimator = cluster_pose_setup(ready=False)["estimator"]
    ready_estimator = cluster_pose_setup(ready=True)["estimator"]
    service = {
        "schema_version": "posetestbot_cluster_controller_service.v1",
        "managed": True,
        "service_unit": "posetestbot-cluster.service",
        "unit_installed": True,
        "state": "stopped",
        "active": False,
        "can_start": True,
        "can_stop": False,
        "load_state": "loaded",
        "active_state": "inactive",
        "sub_state": "dead",
        "unit_file_state": "disabled",
        "integration": {
            "enabled": True,
            "controller_configured": True,
            "environment_file_configured": True,
        },
        "blockers": [],
    }
    controller = {
        "schema_version": "posetestbot_cluster_status_proxy.v1",
        "ready": False,
        "available": False,
        "integration": {"enabled": True, "controller_configured": True},
        "features": {"pose_estimation": False},
        "feature_blockers": {
            "estimation": ["Controller is in status-only mode."]
        },
        "domains": {
            "storage": {
                "ready": False,
                "read": True,
                "mutation": False,
                "blockers": ["Controller is stopped."],
            },
            "scheduler": {
                "ready": False,
                "blockers": ["Controller is stopped."],
            },
        },
        "estimators": [blocked_estimator],
        "profiles": [],
        "blockers": [
            {
                "code": "controller_unavailable",
                "message": "Cluster controller is unavailable",
            }
        ],
    }

    page.route(
        "**/cluster/controller-service",
        lambda route: fulfill_json(route, service),
    )
    page.route("**/cluster/status", lambda route: fulfill_json(route, controller))

    def action_handler(route) -> None:
        action = route.request.url.rsplit("/", 1)[-1]
        assert route.request.post_data_json == {"confirm": True}
        actions.append(action)
        running = action == "start"
        service.update(
            {
                "state": "running" if running else "stopped",
                "active": running,
                "can_start": not running,
                "can_stop": running,
                "active_state": "active" if running else "inactive",
                "sub_state": "running" if running else "dead",
            }
        )
        controller.update(
            {
                "ready": running,
                "available": running,
                "features": {"pose_estimation": False},
                "feature_blockers": {
                    "estimation": [
                        "Runtime manifest is not configured."
                        if running
                        else "Controller is stopped."
                    ]
                },
                "domains": {
                    "storage": {
                        "ready": running,
                        "read": True,
                        "mutation": running,
                        "blockers": [] if running else ["Controller is stopped."],
                    },
                    "scheduler": {
                        "ready": running,
                        "blockers": [] if running else ["Controller is stopped."],
                    },
                },
                "estimators": [blocked_estimator],
                "profiles": [],
                "blockers": [],
            }
        )
        fulfill_json(
            route,
            {
                "accepted": True,
                "action": action,
                "job_id": f"cluster-{action}-1",
                "job": {"id": f"cluster-{action}-1", "status": "queued"},
                "service": service,
            },
            status=202,
        )

    page.route("**/cluster/controller-service/*", action_handler)
    page.goto(f"{console_server.url}/#/dashboard", wait_until="networkidle")

    card = page.get_by_test_id("cluster-controller-control")
    expect(card).to_contain_text("posetestbot-cluster.service")
    expect(card).to_contain_text("stopped")
    expect(card.get_by_role("link", name="Cluster storage")).to_have_attribute(
        "href", "#/run-folders"
    )
    card_box = card.bounding_box()
    room_monitor_box = page.get_by_test_id("dashboard-room-monitor").bounding_box()
    assert card_box is not None and room_monitor_box is not None
    assert card_box["y"] < room_monitor_box["y"]
    assert card_box["y"] + card_box["height"] <= 1080
    card.get_by_role("button", name="Start").click()
    expect(card).to_contain_text("Ready")
    expect(card).to_contain_text("Cluster storage/archive is ready independently")
    expect(card).to_contain_text("None ready")

    controller.update(
        {
            "features": {"pose_estimation": True},
            "feature_blockers": {"estimation": []},
            "estimators": [ready_estimator],
            "profiles": [cluster_profile()],
        }
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(card).to_contain_text("Ready")
    expect(card).to_contain_text("1 ready")
    expect(page.get_by_text("Controller start queued")).to_be_visible()

    card.get_by_role("button", name="Stop").click()
    dialog = page.get_by_test_id("cluster-controller-stop-dialog")
    expect(dialog).to_contain_text("Remote SLURM identity is durable")
    dialog.get_by_test_id("cluster-controller-stop-confirmation").click()
    dialog.get_by_role("button", name="Confirm stop").click()

    expect(card).to_contain_text("stopped")
    expect(page.get_by_text("Controller stop queued")).to_be_visible()
    assert actions == ["start", "stop"]
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


def test_pose_estimation_blockers_submission_and_cluster_job_handoff(
    console_server,
    page,
) -> None:
    install_common_mocks(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    setup = cluster_pose_setup(ready=False)
    jobs: list[dict] = []
    submissions: list[dict] = []

    def setup_handler(route) -> None:
        response = json.loads(json.dumps(setup))
        if "estimator_id=megapose" in route.request.url:
            selected = next(
                estimator
                for estimator in response["estimators"]
                if estimator["estimator_id"] == "megapose"
            )
            response.update(
                {
                    "estimator_id": "megapose",
                    "estimator": selected,
                    "runtime": selected["runtime"],
                    "profiles": selected["profiles"],
                    "enabled_profiles": selected["profiles"],
                }
            )
        fulfill_json(route, response)

    page.route("**/cluster/pose-estimation/setup?**", setup_handler)

    def submit_handler(route) -> None:
        submission = route.request.post_data_json
        submissions.append(submission)
        job = cluster_pose_job(state="running")
        job["payload"].update(
            {
                "estimator_id": submission["estimator_id"],
                "driver_id": f"{submission['estimator_id']}.v1",
                "runtime_id": f"{submission['estimator_id']}-fixture",
            }
        )
        jobs[:] = [job]
        fulfill_json(route, {"job": job}, status=202)

    page.route("**/cluster/pose-estimation/jobs", submit_handler)
    page.route(
        "**/cluster/jobs?**",
        lambda route: fulfill_json(
            route,
            {"jobs": jobs, "next_cursor": None},
        ),
    )

    def job_detail_handler(route) -> None:
        fulfill_json(
            route,
            {
                "job": jobs[0],
                "log": "staged portable BOP tree\nsbatch job 482991\n",
            },
        )

    page.route("**/cluster/jobs/**", job_detail_handler)
    page.goto(f"{console_server.url}/#/pose-estimation", wait_until="networkidle")

    pose_page = page.get_by_test_id("pose-estimation-page")
    expect(page.get_by_role("heading", name="Pose Estimation")).to_be_visible()
    expect(page.get_by_role("link", name="Cluster storage")).to_have_attribute(
        "href", "#/run-folders"
    )
    expect(pose_page).to_contain_text("1 blocker")
    expect(pose_page).to_contain_text(
        "The pinned FoundationPose runtime has not been qualified."
    )
    expect(pose_page).to_contain_text("Oracle-mask qualification")
    expect(pose_page).to_contain_text("without tracking across images or cameras")
    expect(
        page.get_by_role("button", name="Submit FoundationPose job")
    ).to_be_disabled()

    ready_setup = cluster_pose_setup(ready=True)
    megapose = json.loads(json.dumps(ready_setup["estimator"]))
    megapose.update(
        {
            "estimator_id": "megapose",
            "driver_id": "megapose.v1",
            "display_name": "MegaPose",
        }
    )
    megapose["runtime"].update(
        {
            "estimator_id": "megapose",
            "driver_id": "megapose.v1",
            "runtime_id": "megapose-fixture",
            "container": {"filename": "megapose.sif", "sha256": "f" * 64},
            "source_revisions": {"megapose": "abcdef0123456789"},
        }
    )
    ready_setup["estimators"].append(megapose)
    ready_setup["controller"]["estimators"].append(megapose)
    setup.clear()
    setup.update(ready_setup)
    page.get_by_role("button", name="Refresh evidence").click()
    expect(pose_page).to_contain_text("Ready to submit")
    expect(pose_page).to_contain_text("foundationpose-a1b694b8")
    expect(pose_page).to_contain_text("gpu:a100:1")
    page.get_by_label("Estimator method").click()
    page.get_by_role("option", name="MegaPose · ready").click()
    expect(page.get_by_role("button", name="Submit MegaPose job")).to_be_visible()
    page.get_by_label("Operator / submitter").fill("Test Operator")
    page.get_by_role("button", name="Submit MegaPose job").click()

    expect(page.get_by_text("MegaPose job accepted")).to_be_visible()
    current = page.get_by_test_id("pose-estimation-current-job")
    expect(current).to_contain_text("running")
    expect(current).to_contain_text("482991")
    expect(current).to_contain_text("Remote work is durable and still running.")
    assert submissions == [
        {
            "run_root": RUN_ROOT,
            "estimator_id": "megapose",
            "profile_id": "smoke-a100",
            "operator": "Test Operator",
        }
    ]

    current.get_by_role("link", name="View logs and all cluster jobs").click()
    expect(page).to_have_url(f"{console_server.url}/#/jobs")
    cluster_section = page.get_by_test_id("cluster-jobs-section")
    expect(cluster_section).to_contain_text("Durable estimator and SLURM state")
    expect(cluster_section).to_contain_text("482991")
    cluster_section.get_by_role("button", name="Log").click()
    expect(page.get_by_text("Cluster job log")).to_be_visible()
    expect(
        page.get_by_text("Controller state survives UI and PoseTestBot restarts.")
    ).to_be_visible()
    expect(page.get_by_text("sbatch job 482991")).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


def test_workpiece_catalogue_metadata_filters_actions_import_and_upload(
    console_server, page
) -> None:
    install_common_mocks(page)
    page.add_init_script("HTMLCanvasElement.prototype.getContext = () => null")
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    catalogue = workpiece_catalog()
    catalog_requests = {"count": 0}
    delete_requests = {"count": 0}
    background_job_status = {"upload": "queued", "correction": "queued"}
    requests: list[dict] = []

    def item(catalog_uuid: str) -> dict:
        return next(
            value
            for value in catalogue["objects"]
            if value["catalog_uuid"] == catalog_uuid
        )

    def status_handler(route) -> None:
        active = sum(value["state"] == "active" for value in catalogue["objects"])
        fulfill_json(
            route,
            {
                "schema_version": "workpiece_catalog_status.v1",
                "available": True,
                "status": "available",
                "reason": None,
                "catalog_root": "/repo/working_data/object_catalog",
                "formats": ["ply", "stl", "obj"],
                "limits": {"cad_bytes": 52428800, "batch_bytes": 104857600},
                "counts": {
                    "active": active,
                    "archived": len(catalogue["objects"]) - active,
                    "total": len(catalogue["objects"]),
                },
            },
        )

    def catalog_handler(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path == "/workpieces/catalog" and request.method == "GET":
            catalog_requests["count"] += 1
            fulfill_json(route, catalogue)
            return
        if path == "/workpieces/catalog/import" and request.method == "POST":
            requests.append(
                {
                    "path": path,
                    "method": request.method,
                    "body": request.post_data or "",
                }
            )
            fulfill_json(
                route,
                {
                    "schema_version": "workpiece_catalog_import.v1",
                    "updated": [catalogue["objects"][0]["catalog_uuid"]],
                    "unchanged": [catalogue["objects"][1]["catalog_uuid"]],
                    "skipped_missing_assets": [],
                },
            )
            return
        if path == "/workpieces/catalog/upload" and request.method == "POST":
            requests.append(
                {
                    "path": path,
                    "method": request.method,
                    "body": request.post_data or "",
                }
            )
            catalogue["objects"].append(
                {
                    "catalog_uuid": "99999999-9999-4999-8999-999999999999",
                    "obj_id": 9,
                    "name": "New clamp",
                    "alias": "Queued workpiece",
                    "description": None,
                    "tags": ["new", "metal"],
                    "groups": ["incoming"],
                    "attributes": {},
                    "source_filename": "new-clamp.stl",
                    "source_format": "stl",
                    "source_sha256": "f" * 64,
                    "canonical_ply_sha256": "1" * 64,
                    "texture_sha256": None,
                    "created_at": "2026-07-22T12:00:00Z",
                    "updated_at": "2026-07-22T12:00:00Z",
                    "archived_at": None,
                    "state": "active",
                    "extraction": {
                        "vertices": 8,
                        "faces": 12,
                        "bounds_mm": [[-4, -4, -4], [4, 4, 4]],
                        "watertight": True,
                    },
                    "assets": {
                        "source": {
                            "path": "objects/9/source/new-clamp.stl",
                            "sha256": "f" * 64,
                        },
                        "canonical_ply": {
                            "path": "objects/9/derived/canonical.ply",
                            "sha256": "1" * 64,
                        },
                    },
                    "usage": {"template_count": 0, "templates": []},
                }
            )
            fulfill_json(
                route,
                {"job_id": "workpiece-upload-job", "request_id": "a" * 32},
                status=202,
            )
            return
        parts = path.removeprefix("/workpieces/catalog/").split("/")
        catalog_uuid = parts[0]
        current = item(catalog_uuid)
        if (
            len(parts) == 2
            and parts[1] == "unit-corrections"
            and request.method == "POST"
        ):
            body = request.post_data_json
            requests.append({"path": path, "method": request.method, "body": body})
            current["geometry_revision"] = 2
            current["source_to_mm_scale"] = (
                0.001 if body["conversion"] == "millimeter_to_meter" else 1000.0
            )
            current["canonical_ply_sha256"] = "2" * 64
            factor = 0.001 if body["conversion"] == "millimeter_to_meter" else 1000.0
            current["extraction"]["bounds_mm"] = [
                [coordinate * factor for coordinate in corner]
                for corner in current["extraction"]["bounds_mm"]
            ]
            fulfill_json(
                route,
                {"job_id": "unit-correction-job", "request_id": "b" * 32},
                status=202,
            )
            return
        if len(parts) == 1 and request.method == "PATCH":
            body = request.post_data_json
            requests.append({"path": path, "method": request.method, "body": body})
            current.update(body)
            current["updated_at"] = "2026-07-22T12:30:00Z"
            fulfill_json(route, current)
            return
        if (
            len(parts) == 2
            and parts[1] in {"archive", "restore"}
            and request.method == "POST"
        ):
            requests.append({"path": path, "method": request.method, "body": None})
            current["state"] = "archived" if parts[1] == "archive" else "active"
            current["archived_at"] = (
                "2026-07-22T12:45:00Z" if parts[1] == "archive" else None
            )
            fulfill_json(route, current)
            return
        if len(parts) == 1 and request.method == "DELETE":
            requests.append(
                {"path": path, "method": request.method, "body": request.post_data_json}
            )
            delete_requests["count"] += 1
            if delete_requests["count"] == 1:
                fulfill_json(
                    route,
                    {
                        "output": "Workpiece is referenced by or cannot be checked against pose-template bundles",
                        "blockers": [
                            {
                                "template_uuid": "22222222-2222-4222-8222-222222222222",
                                "display_name": "Clamp pair",
                                "state": "active",
                                "reason": "catalog_reference",
                            }
                        ],
                    },
                    status=409,
                )
                return
            catalogue["objects"].remove(current)
            fulfill_json(
                route,
                {"schema_version": "workpiece_catalog_delete.v1", "status": "deleted"},
            )
            return
        fulfill_json(route, {"output": "Unexpected workpiece request"}, status=404)

    page.route("**/workpieces/status", status_handler)
    page.route("**/workpieces/catalog**", catalog_handler)
    page.route(
        "**/pose-templates/workpieces/*/orientations",
        lambda route: fulfill_json(
            route,
            pose_template_orientation_analysis(
                urlparse(route.request.url).path.split("/")[-2]
            ),
        ),
    )
    page.route(
        "**/pose-templates/workpieces/*/orientation-thumbnail",
        lambda route: fulfill_json(
            route,
            pose_template_orientation_thumbnail(
                urlparse(route.request.url).path.split("/")[-2]
            ),
        ),
    )
    page.route(
        "**/jobs/workpiece-upload-job",
        lambda route: fulfill_json(
            route,
            {
                "job": {
                    "id": "workpiece-upload-job",
                    "status": background_job_status["upload"],
                    "message": None,
                    "tail": [],
                }
            },
        ),
    )
    page.route(
        "**/jobs/unit-correction-job",
        lambda route: fulfill_json(
            route,
            {
                "job": {
                    "id": "unit-correction-job",
                    "status": background_job_status["correction"],
                    "message": None,
                    "tail": [],
                }
            },
        ),
    )

    page.goto(f"{console_server.url}/#/workpieces", wait_until="networkidle")

    expect(page.get_by_test_id("workpieces-page")).to_be_visible()
    expect(page.get_by_role("link", name="Workpiece Catalogue")).to_be_visible()
    expect(
        page.get_by_text("This is a global reusable library", exact=False)
    ).to_be_visible()
    expect(
        page.get_by_text("do not mutate the active run", exact=False)
    ).to_be_visible()
    expect(page.get_by_test_id("workpiece-preview-fallback")).to_be_visible()
    expect(page.get_by_text("3D preview is unavailable")).to_be_visible()
    expect(page.get_by_role("heading", name="3D preview")).to_be_visible()
    expect(
        page.get_by_text("Archive this workpiece to enable unit correction.")
    ).to_be_visible()
    expect(page.get_by_role("button", name="Correct model units")).to_be_disabled()
    expect(page.get_by_test_id("workpiece-previews")).to_have_count(0)
    expect(page.get_by_role("button", name="Select Clamp")).to_be_visible()
    expect(
        page.get_by_test_id(
            "workpiece-isometric-11111111-1111-4111-8111-111111111111"
        ).locator("polygon")
    ).to_have_count(6)
    expect(page.get_by_role("button", name="Select Gauge block")).to_have_count(0)
    expect(page.get_by_text("Wrong model scale?", exact=True)).to_have_count(0)
    dimensions = page.get_by_test_id("workpiece-dimensions")
    dimensions.get_by_role("button", name="About model dimensions").hover()
    expect(page.get_by_role("tooltip")).to_contain_text("Wrong model scale?")
    expect(page.get_by_role("tooltip")).to_contain_text(
        "Archive this workpiece first, then use Correct model units."
    )
    expect(page.get_by_role("tooltip")).to_contain_text(
        "Existing immutable templates keep their original geometry snapshot."
    )
    page.keyboard.press("Escape")

    page.get_by_label("Search workpieces").fill("not present")
    expect(page.get_by_text("No matches", exact=True)).to_be_visible()
    page.get_by_label("Search workpieces").fill("small clamp")
    expect(page.get_by_role("button", name="Select Clamp")).to_be_visible()
    page.get_by_label("Search workpieces").fill("")

    page.get_by_role("combobox", name="Filter by tag").click()
    expect(page.get_by_role("option", name="metal", exact=True)).to_have_count(1)
    page.get_by_role("option", name="reflective").click()
    expect(page.get_by_text("1 of 2 visible")).to_be_visible()
    page.get_by_role("combobox", name="Filter by group").click()
    page.get_by_role("option", name="clamps").click()
    expect(page.get_by_role("button", name="Select Clamp")).to_be_visible()
    page.get_by_role("button", name="Clear").click()
    page.get_by_role("combobox", name="Filter by state").click()
    page.get_by_role("option", name="Archived").click()
    expect(page.get_by_role("button", name="Select Gauge block")).to_be_visible()
    expect(page.get_by_role("button", name="Select Clamp")).to_have_count(0)
    page.get_by_role("button", name="Select Gauge block").click()
    page.get_by_role("button", name="Correct model units").click()
    correction = page.get_by_test_id("workpiece-unit-correction-dialog")
    expect(
        correction.get_by_text("File was authored in metres — enlarge ×1000")
    ).to_be_visible()
    expect(
        correction.get_by_text("Model is 1000× too large — shrink ÷1000")
    ).to_be_visible()
    expect(correction.get_by_text("Current dimensions")).to_be_visible()
    expect(correction.get_by_text("After correction")).to_be_visible()
    correction.get_by_role("radio").filter(has_text="shrink ÷1000").click()
    correction.get_by_label("Unit correction operator").fill("qa-operator")
    correction.get_by_label("Confirm unit correction").click()
    correction.get_by_role("button", name="Queue unit correction").click()
    unit_progress = page.get_by_test_id("workpiece-unit-correction-progress")
    expect(unit_progress).to_contain_text("continues after navigation")
    expect(unit_progress.get_by_role("link", name="Open Jobs")).to_have_attribute(
        "href", "#/jobs"
    )
    background_job_status["correction"] = "succeeded"
    expect(page.get_by_text("Workpiece units corrected")).to_be_visible()
    unit_request = next(
        value for value in requests if value["path"].endswith("/unit-corrections")
    )
    assert unit_request["body"] == {
        "conversion": "millimeter_to_meter",
        "confirm": True,
        "operator": "qa-operator",
        "expected_geometry_revision": 1,
        "expected_canonical_sha256": "e" * 64,
    }
    page.get_by_role("button", name="Clear").click()
    page.get_by_role("button", name="Select Clamp").click()

    page.get_by_role("button", name="Edit metadata").click()
    page.get_by_test_id("workpiece-edit-alias").fill("Fixture A")
    page.get_by_test_id("workpiece-edit-tags").fill("metal, QA, qa")
    page.get_by_test_id("workpiece-edit-groups").fill("clamps, set-a")
    page.get_by_test_id("workpiece-edit-attribute-value-0").fill("metrology")
    page.get_by_role("button", name="Add attribute").click()
    page.get_by_role("button", name="Save metadata").click()
    expect(page.get_by_test_id("workpiece-edit-attribute-error")).to_contain_text(
        "Add a name or remove attribute row 3"
    )
    assert not any(value["method"] == "PATCH" for value in requests)
    page.get_by_test_id("workpiece-edit-attribute-key-2").fill("OWNER")
    page.get_by_test_id("workpiece-edit-attribute-value-2").fill("duplicate")
    page.get_by_role("button", name="Save metadata").click()
    expect(page.get_by_test_id("workpiece-edit-attribute-error")).to_contain_text(
        "Attribute names must be unique"
    )
    assert not any(value["method"] == "PATCH" for value in requests)
    page.get_by_test_id("workpiece-edit-attribute-key-2").fill("station")
    page.get_by_test_id("workpiece-edit-attribute-value-2").fill("2")
    page.get_by_role("button", name="Save metadata").click()
    expect(page.get_by_text("Workpiece metadata saved")).to_be_visible()
    patch_request = next(value for value in requests if value["method"] == "PATCH")
    assert patch_request["body"] == {
        "name": "Clamp",
        "alias": "Fixture A",
        "description": "Textured fixture",
        "tags": ["metal", "QA"],
        "groups": ["clamps", "set-a"],
        "attributes": {"owner": "metrology", "finish": "matte", "station": "2"},
    }

    page.get_by_test_id("workpiece-catalog-import").click()
    page.get_by_test_id("workpiece-import-input").set_input_files(
        {
            "name": "object_catalog.json",
            "mimeType": "application/json",
            "buffer": json.dumps(workpiece_catalog()).encode(),
        }
    )
    page.get_by_role("button", name="Import metadata").click()
    expect(page.get_by_text("Catalogue metadata imported")).to_be_visible()
    import_request = next(
        value for value in requests if value["path"].endswith("/import")
    )
    assert "object_catalog.json" in import_request["body"]

    page.get_by_test_id("workpiece-upload-button").click()
    page.get_by_test_id("workpiece-cad-input").set_input_files(
        {
            "name": "new-clamp.stl",
            "mimeType": "application/octet-stream",
            "buffer": b"solid clamp",
        }
    )
    page.get_by_test_id("workpiece-upload-name").fill("New clamp")
    page.get_by_test_id("workpiece-upload-alias").fill("Queued workpiece")
    page.get_by_test_id("workpiece-upload-tags").fill("new, metal")
    page.get_by_test_id("workpiece-upload-groups").fill("incoming")
    page.get_by_role("button", name="Upload and inspect").click()
    expect(page.get_by_text("Workpiece inspection queued")).to_be_visible()
    upload_progress = page.get_by_test_id("workpiece-upload-progress")
    expect(upload_progress).to_contain_text("continues after navigation")
    expect(upload_progress.get_by_role("link", name="Open Jobs")).to_have_attribute(
        "href", "#/jobs"
    )
    background_job_status["upload"] = "succeeded"
    expect(page.get_by_text("Workpiece added to the catalogue")).to_be_visible()
    upload_request = next(
        value for value in requests if value["path"].endswith("/upload")
    )
    assert "new-clamp.stl" in upload_request["body"]
    assert "Queued workpiece" in upload_request["body"]
    expect(page.get_by_role("button", name="Select New clamp")).to_be_visible()
    assert catalog_requests["count"] > 1

    page.get_by_role("button", name="Select New clamp").click()
    page.get_by_role("button", name="Archive").click()
    confirmation = page.get_by_test_id("workpiece-action-confirmation")
    expect(confirmation).to_contain_text("hidden from active-object workflows")
    confirmation.get_by_role("button", name="Confirm archive").click()
    expect(page.get_by_text("Workpiece archived")).to_be_visible()
    assert any(value["path"].endswith("/archive") for value in requests)

    page.get_by_role("button", name="Restore").click()
    confirmation = page.get_by_test_id("workpiece-action-confirmation")
    expect(confirmation).to_contain_text("returns to active-object workflows")
    confirmation.get_by_role("button", name="Confirm restore").click()
    expect(page.get_by_text("Workpiece restored")).to_be_visible()
    assert any(value["path"].endswith("/restore") for value in requests)

    expect(page.get_by_role("button", name="Delete New clamp")).to_be_enabled()
    page.get_by_role("button", name="Delete New clamp").click()
    confirmation = page.get_by_test_id("workpiece-action-confirmation")
    expect(confirmation).to_contain_text("permanently removes")
    confirmation.get_by_role("button", name="Confirm delete").click()
    expect(page.get_by_text("Catalogue action failed")).to_be_visible()
    expect(page.get_by_text("pose-template bundles")).to_be_visible()
    expect(confirmation).to_be_visible()
    confirmation.get_by_role("button", name="Confirm delete").click()
    expect(page.get_by_text("Workpiece deleted")).to_be_visible()
    expect(page.get_by_role("button", name="Select New clamp")).to_have_count(0)
    assert delete_requests["count"] == 2
    assert page_errors == []


def test_run_config_preflight_blocker_and_fresh_capture_gates(
    console_server, page
) -> None:
    requests: list[dict] = []
    preflight_state = {"blocker": "missing_preflight"}
    configured = run_config(
        sensors=[
            {
                "sensor_type": "realsense_d435",
                "device_id": "wrist-1",
                "display_name": "Wrist RGB-D",
                "mounting_mode": "eye_in_hand",
                "enabled": True,
                "inverted": False,
            },
            {
                "sensor_type": "realsense_d435",
                "device_id": "static-1",
                "display_name": "Static RGB-D",
                "mounting_mode": "eye_in_hand",
                "enabled": True,
                "inverted": True,
            },
        ]
    )
    configured["calibration_target"] = {
        "target_id": "5f09f41c-dd91-44ef-a048-1f43fc990e17",
        "placement": {"mode": "unknown", "mounting_frame": "template_base"},
    }
    install_common_mocks(
        page,
        preflight_state=preflight_state,
        requests=requests,
        config_payload=configured,
    )
    page.route(
        "**/sensors/status", lambda route: fulfill_json(route, selected_sensor_status())
    )
    capture_setup = {
        "queued": False,
        "post_queue_reads": 0,
        "job_id": None,
        "status": None,
    }
    readiness_job_status: dict[str, str | None] = {"value": None}

    def readiness_job_payload(status: str) -> dict:
        return {
            "id": "readiness-1",
            "name": "pipeline:run_preflight",
            "command": ["uv", "run", "python", "scripts/run_pipeline_stage.py"],
            "cwd": "/repo",
            "status": status,
            "created_at": "2026-07-27T11:00:00Z",
            "started_at": ("2026-07-27T11:00:01Z" if status != "queued" else None),
            "ended_at": ("2026-07-27T11:00:03Z" if status == "succeeded" else None),
            "returncode": 0 if status == "succeeded" else None,
            "message": None,
            "tail": [],
            "resources": ["disk_io"],
            "parameters": {
                "run_root": RUN_ROOT,
                "pipeline_stage": "run_preflight",
            },
            "scope_kind": "run",
            "run_root": RUN_ROOT,
            "log_path": "/tmp/readiness-1.log",
        }

    def readiness_submit_handler(route) -> None:
        requests.append({"path": "/pipeline/run", "body": route.request.post_data_json})
        readiness_job_status["value"] = "queued"
        fulfill_json(route, {"job_id": "readiness-1", "status": "queued"}, status=202)

    def jobs_handler(route) -> None:
        status = readiness_job_status["value"]
        fulfill_json(
            route,
            {
                "jobs": [] if status is None else [readiness_job_payload(status)],
                "resources": {},
            },
        )

    page.route("**/pipeline/run", readiness_submit_handler)
    page.route("**/jobs", jobs_handler)

    def calibration_setup_handler(route) -> None:
        cameras = []
        if capture_setup["queued"]:
            capture_setup["post_queue_reads"] += 1
            if capture_setup["post_queue_reads"] >= 2:
                cameras = [
                    {
                        "sensor_key": "realsense_d435:wrist-1",
                        "sensor_name": "realsense_wrist-1",
                        "display_name": "Wrist RGB-D",
                        "sensor_type": "realsense_d435",
                        "device_id": "wrist-1",
                        "current_mounting_mode": "eye_in_hand",
                    }
                ]
        fulfill_json(
            route,
            {
                "schema_version": "calibration_setup.v1",
                "run_root": RUN_ROOT,
                "cameras": cameras,
                "unavailable_cameras": [],
                "saved_targets": [
                    {
                        "target_id": configured["calibration_target"]["target_id"],
                        "display_name": "Lab board",
                        "valid": True,
                        "selected": True,
                        "selected_placement": {
                            "mode": "unknown",
                            "mounting_frame": "template_base",
                        },
                    }
                ],
                "modes": [
                    {
                        "id": "eye_in_hand",
                        "label": "Robot-mounted camera (eye-in-hand)",
                        "primary_transform": "camera → robot_flange",
                        "target_mounting": "stationary relative to template_base",
                    },
                    {
                        "id": "eye_to_hand",
                        "label": "Static camera (eye-to-hand)",
                        "primary_transform": "camera → template_base",
                        "target_mounting": "rigidly attached to robot_flange",
                    },
                ],
                "solver": {
                    "default_pnp_methods": ["IPPE", "ITERATIVE", "SQPNP"],
                    "default_extrinsic_methods": ["tsai", "park"],
                    "intrinsics_policy": "compare_factory_opencv",
                    "intrinsics_policies": [],
                    "thresholds": {
                        "min_pnp_common_inliers": 12,
                        "min_pnp_common_inlier_ratio": 0.5,
                        "max_pnp_all_point_mean_reprojection_error_px": 3.0,
                        "min_pnp_supported_markers": 4,
                        "min_pnp_grid_rows": 2,
                        "min_pnp_grid_columns": 2,
                        "min_accepted_views": 15,
                        "min_coverage_cells": 6,
                        "max_per_view_reprojection_error_px": 3.0,
                        "max_intrinsic_rms_reprojection_error_px": 1.5,
                        "min_motion_poses": 4,
                        "min_translation_span_mm": 20.0,
                        "min_rotation_span_deg": 5.0,
                        "min_rotation_axis_second_to_first_ratio": 0.15,
                        "max_nearest_pose_delta_ms": 20.0,
                    },
                },
                "latest_attempt": None,
            },
        )

    def capture_jobs_handler(route) -> None:
        status = capture_setup["status"]
        job_id = capture_setup["job_id"]
        fulfill_json(
            route,
            {
                "run_root": RUN_ROOT,
                "jobs": (
                    []
                    if status is None or job_id is None
                    else [
                        {
                            "id": job_id,
                            "name": "Calibration capture",
                            "status": status,
                            "kind": "pipeline_sequence",
                            "stage": None,
                            "sequence": "real_full_capture_validation",
                            "run_root": RUN_ROOT,
                            "resources": ["cameras", "robot", "disk_io"],
                            "message": None,
                            "created_at": "2026-07-27T12:00:00Z",
                            "started_at": None,
                            "ended_at": None,
                            "active": True,
                            "tail": [],
                            "log_endpoint": f"/capture/jobs/{job_id}/log",
                            "stop_endpoint": f"/capture/jobs/{job_id}/stop",
                        }
                    ]
                ),
                "active_count": 0 if status is None else 1,
                "resources": {},
                "status_artifact": None,
            },
        )

    def pipeline_sequence_handler(route) -> None:
        body = route.request.post_data_json
        requests.append({"path": "/pipeline/run-sequence", "body": body})
        capture_setup["queued"] = True
        capture_setup["job_id"] = f"job-{len(requests)}"
        capture_setup["status"] = "queued"
        fulfill_json(
            route,
            {"job_id": capture_setup["job_id"], "status": "queued"},
            status=202,
        )

    page.route("**/calibration/setup?**", calibration_setup_handler)
    page.route("**/capture/jobs**", capture_jobs_handler)
    page.route("**/pipeline/run-sequence", pipeline_sequence_handler)
    page.add_init_script(
        "localStorage.setItem('posetestbot.selectedSensors', "
        "JSON.stringify(['realsense_d435:wrist-1', 'realsense_d435:static-1']))"
    )
    page.goto(
        f"{console_server.url}/#/workflow/calibration?step=configure",
        wait_until="networkidle",
    )

    speed = page.locator("#velocity")
    expect(speed).to_have_value("0.03")
    expect(speed).to_have_attribute("max", "0.03")
    expect(
        page.get_by_text("Full capture is an A1 joint PTP", exact=False)
    ).to_be_visible()
    page.get_by_role("button", name="Save setup").click()
    expect(page.get_by_text("Calibration recording setup saved")).to_be_visible()
    written = next(item["body"] for item in requests if item["path"] == "/run-config")
    assert written["plan_only"] is True
    assert written["velocity"] == 0.03
    assert "mounting_mode" not in written
    assert [sensor["mounting_mode"] for sensor in written["sensors"]] == [
        "eye_in_hand",
        "eye_in_hand",
    ]
    assert "allow_cameras" not in json.dumps(written)
    assert "allow_real_robot" not in json.dumps(written)

    page.goto(
        f"{console_server.url}/#/workflow/calibration?step=readiness",
        wait_until="networkidle",
    )
    readiness = page.get_by_test_id("calibration-readiness-check")
    expect(readiness).to_have_count(1)
    expect(readiness).to_be_visible()
    expect(readiness).to_contain_text("Readiness has not been checked")
    readiness.get_by_role("button", name="Check readiness", exact=True).click()
    preflight_request = next(
        item["body"] for item in requests if item["path"] == "/pipeline/run"
    )
    assert preflight_request["stage"] == "run_preflight"
    assert "allow_cameras" not in json.dumps(preflight_request)
    readiness_job = readiness.get_by_test_id("calibration-readiness-job-status")
    expect(readiness_job).to_contain_text("Readiness check is queued", timeout=5_000)
    expect(readiness_job).to_contain_text("continues after navigation")
    expect(
        readiness_job.get_by_role("link", name="Open live status in Jobs")
    ).to_have_attribute("href", "#/jobs")
    expect(readiness.get_by_role("button", name="Check in progress…")).to_be_disabled()

    readiness_job_status["value"] = "running"
    page.reload(wait_until="networkidle")
    readiness = page.get_by_test_id("calibration-readiness-check")
    expect(
        readiness.get_by_test_id("calibration-readiness-job-status")
    ).to_contain_text("Readiness check is running", timeout=5_000)
    expect(readiness.get_by_role("button", name="Check in progress…")).to_be_disabled()
    readiness_job_status["value"] = "succeeded"
    preflight_state["blocker"] = None
    page.reload(wait_until="networkidle")
    page.get_by_role("navigation", name="Workflow steps").get_by_role("button").filter(
        has_text="Record calibration images"
    ).click()
    page.get_by_role("button", name="Review and start capture", exact=True).click()
    expect(page.get_by_test_id("capture-timeout-envelope")).to_contain_text(
        "720 s total · up to 15 s per camera startup attempt to publish 3 valid metadata records · 120 s to first robot packet · 60 s between robot packets"
    )
    submit = page.locator('[data-testid="capture-submit"]')
    expect(submit).to_be_disabled()
    page.locator('[data-testid="capture-robot-ack"]').click()
    expect(submit).to_be_disabled()
    page.locator('[data-testid="capture-camera-ack"]').click()
    expect(submit).to_be_enabled()
    submit.click()
    expect(page.get_by_text("Calibration capture queued")).to_be_visible()
    capture_request = [
        item["body"] for item in requests if item["path"] == "/pipeline/run-sequence"
    ][-1]
    assert capture_request == {
        "sequence": "real_full_capture_validation",
        "run_root": RUN_ROOT,
        "plan_only": False,
        "options": {
            "capture_plan_preflight": {"allow_real_robot": True},
            "capture_execution_plan": {
                "allow_cameras": True,
                "allow_real_robot": True,
                "include_sensors": True,
            },
            "capture_execution": {
                "allow_cameras": True,
                "allow_real_robot": True,
                "include_sensors": True,
                "timeout_s": 720,
                "startup_wait_s": 15,
                "receive_start_timeout_s": 120,
                "receive_idle_timeout_s": 60,
            },
        },
    }
    assert any(item["path"] == "/sensors/previews/stop" for item in requests)
    capture_job = page.get_by_test_id("capture-active-job")
    expect(capture_job).to_contain_text("Calibration capture is queued")
    expect(capture_job).to_contain_text("continues after navigation")
    expect(
        capture_job.get_by_role("link", name="Open capture in Jobs")
    ).to_have_attribute("href", "#/jobs")
    expect(
        page.get_by_role("button", name="Review and start capture", exact=True)
    ).to_have_count(0)
    assert (
        len([item for item in requests if item["path"] == "/pipeline/run-sequence"])
        == 1
    )
    page.get_by_role("navigation", name="Workflow steps").get_by_role("button").filter(
        has_text="Calculate, review, and publish"
    ).click()
    analysis_arrangement = page.get_by_test_id("calibration-analysis-arrangement")
    expect(analysis_arrangement).to_have_attribute(
        "data-calibration-mode", "eye_in_hand", timeout=6_000
    )
    expect(page.locator('input[name="calibration-mode"]')).to_have_count(0)
    expect(
        page.get_by_test_id("calibration-workflow").get_by_text(
            "Wrist RGB-D", exact=True
        )
    ).to_be_visible()
    assert capture_setup["post_queue_reads"] >= 2


def test_dataset_processing_is_one_ordered_operator_action(
    console_server,
    page,
) -> None:
    processing_requests: list[dict] = []
    processing_job_status: dict[str, str | None] = {"value": None}
    selected_bundle_sha256 = "d" * 64
    selected_calibration_path = (
        "processed/calibration_inputs/current/calibration_profiles.json"
    )
    selected_intrinsics_path = (
        "processed/calibration_inputs/current/intrinsic_calibration_profiles.json"
    )
    configured = run_config(
        plan_only=False,
        sensors=[
            {
                "sensor_type": "realsense_d435",
                "device_id": "wrist-1",
                "display_name": "Wrist RGB-D",
                "mounting_mode": "eye_in_hand",
                "enabled": True,
                "inverted": False,
                "calibration_profile_id": "profile-wrist-1",
            }
        ],
    )
    configured["dataset_mode"] = "pose_template"
    configured["calibration_profiles"] = selected_calibration_path
    configured["intrinsic_calibration_profiles"] = selected_intrinsics_path
    configured["calibration_profile_selection"] = {
        "selection_artifact": "calibration_profile_selection.json",
        "bundle_sha256": selected_bundle_sha256,
        "selected_at": "2026-07-22T12:00:00+00:00",
    }
    configured["pose_template"] = {
        "template_uuid": "22222222-2222-4222-8222-222222222222",
        "placement_confirmed": True,
    }
    overview = overview_payload(configured)
    capture_section = next(
        section for section in overview["sidebar"] if section["id"] == "capture"
    )
    capture_section["artifacts"] = [
        {
            "path": "capture_execution_report.json",
            "exists": True,
            "status": "complete",
        }
    ]
    next(section for section in overview["sidebar"] if section["id"] == "sync")[
        "artifacts"
    ] = [
        {
            "path": "sync_quality_report.json",
            "exists": True,
            "status": "ok",
        }
    ]
    next(section for section in overview["sidebar"] if section["id"] == "bop")[
        "artifacts"
    ] = [
        {
            "path": "camera_rectification_report.json",
            "exists": True,
            "status": "complete",
        }
    ]

    install_common_mocks(page, config_payload=configured)
    page.route("**/ui/overview**", lambda route: fulfill_json(route, overview))
    page.route(
        "**/sensors/status", lambda route: fulfill_json(route, selected_sensor_status())
    )
    page.route(
        "**/ui/calibrations?**",
        lambda route: fulfill_json(
            route,
            {
                "selected": valid_library_selection(
                    bundle_sha256=selected_bundle_sha256,
                    calibration_profiles=selected_calibration_path,
                    intrinsic_calibration_profiles=selected_intrinsics_path,
                ),
                "calibrations": [],
            },
        ),
    )
    page.route(
        "**/pose-templates/library",
        lambda route: fulfill_json(route, {"templates": []}),
    )
    page.route(
        "**/pose-templates/runs/selection?**",
        lambda route: fulfill_json(
            route,
            {"selection": None, "replacement_blockers": [], "ready": False},
        ),
    )

    def processing_handler(route) -> None:
        processing_requests.append(route.request.post_data_json)
        processing_job_status["value"] = "queued"
        fulfill_json(
            route,
            {"job_id": "dataset-processing-1", "status": "queued"},
            status=202,
        )

    def jobs_handler(route) -> None:
        status = processing_job_status["value"]
        jobs = (
            []
            if status is None
            else [
                {
                    "id": "dataset-processing-1",
                    "name": "pipeline-run-config:calibrated_capture_to_bop_dataset_dry_run",
                    "command": [
                        "uv",
                        "run",
                        "python",
                        "scripts/run_pipeline_sequence.py",
                    ],
                    "cwd": "/repo",
                    "status": status,
                    "created_at": "2026-07-26T10:57:51Z",
                    "started_at": "2026-07-26T10:57:52Z",
                    "ended_at": (
                        "2026-07-26T11:00:48Z"
                        if status in {"succeeded", "failed", "canceled"}
                        else None
                    ),
                    "returncode": 0 if status == "succeeded" else None,
                    "message": (
                        "Command completed successfully."
                        if status == "succeeded"
                        else None
                    ),
                    "tail": ["processing"],
                    "resources": ["cpu", "disk_io"],
                    "parameters": {
                        "pipeline_sequence": "calibrated_capture_to_bop_dataset_dry_run",
                        "run_root": RUN_ROOT,
                    },
                    "scope_kind": "run",
                    "run_root": RUN_ROOT,
                    "log_path": "/tmp/dataset-processing-1.log",
                    "visibility": "operator",
                }
            ]
        )
        fulfill_json(route, {"jobs": jobs, "resources": {}})

    page.route("**/pipeline/run-config", processing_handler)
    page.route("**/jobs", jobs_handler)
    page.goto(
        f"{console_server.url}/#/workflow/dataset?step=sync",
        wait_until="networkidle",
    )

    stepper = page.get_by_role("navigation", name="Workflow steps")
    configure_step_button = stepper.get_by_role("button").filter(
        has_text="Configure cameras and select calibration"
    )
    sync_step_button = stepper.get_by_role("button").filter(
        has_text="Process frames and create the base BOP export"
    )
    export_step_button = stepper.get_by_role("button").filter(
        has_text="Add optional BOP ground-truth evidence"
    )
    configure_step_button.click()
    timing_policy = page.get_by_test_id("calibration-sync-policy")
    expect(timing_policy).to_contain_text("Automatic calibration timing")
    expect(timing_policy).to_contain_text("ready")
    expect(timing_policy).to_contain_text("Wrist RGB-D")
    expect(timing_policy).to_contain_text("profile-wrist-1")
    expect(timing_policy).to_contain_text("+70.000 ms")
    expect(timing_policy).to_contain_text("sensor")
    expect(timing_policy).to_contain_text("host_wall")
    expect(timing_policy).to_contain_text("domain global_time")
    expect(timing_policy).to_contain_text("fallback forbidden")
    expect(timing_policy).to_contain_text("20.000 ms")
    page.mouse.move(0, 0)
    timing_policy.get_by_role("button", name="About robot-pose time offset").hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "Positive means pair the frame with a robot pose recorded later"
    )
    page.keyboard.press("Escape")
    timing_policy.get_by_role("button", name="About calibration timestamp pair").hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "exact frame and robot clock fields"
    )
    page.keyboard.press("Escape")
    timing_policy.get_by_role("button", name="About maximum robot-pose gap").hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "frame is excluded when its nearest robot pose is farther away"
    )
    page.keyboard.press("Escape")
    sync_step_button.click()
    timing_contract = page.get_by_test_id("dataset-sync-timing-contract")
    expect(timing_contract).to_contain_text(
        "selected per-camera timing policy will be applied and verified automatically"
    )
    page.mouse.move(0, 0)
    timing_contract.get_by_role(
        "button", name="About automatic calibration timing"
    ).hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "Manual values and generic defaults cannot override them"
    )

    processing = page.get_by_test_id("dataset-processing")
    expect(processing).to_have_count(1)
    sync_step = page.locator('[data-workflow-step="sync"]')
    expect(sync_step.get_by_text("Current step", exact=True)).to_be_visible()
    expect(
        sync_step.get_by_text(
            "Copy models and write the base BOP dataset",
            exact=True,
        )
    ).to_be_visible()
    expect(processing).to_contain_text(
        "One queued job runs five backend stages grouped into the four operator outcomes below"
    )
    expect(processing).to_contain_text(
        "Ground-truth generation is chosen separately in optional step 6"
    )
    expect(processing).to_contain_text(
        "Calibration validation is automatic here; there is no second operator preflight."
    )
    expect(processing).to_contain_text("Copy models and write the base BOP dataset")
    process_action = page.get_by_role(
        "button", name="Process and export dataset", exact=True
    )
    expect(process_action).to_have_count(1)
    expect(process_action).to_be_enabled()
    for stale_action in (
        "Synchronize frames",
        "Verify synchronization",
        "Validate selected calibration",
        "Export BOP dataset",
    ):
        expect(page.get_by_role("button", name=stale_action, exact=True)).to_have_count(
            0
        )

    export_step_button.click()
    export_outcome = page.locator('[data-workflow-step="export"]')
    expect(export_outcome).to_contain_text("BOP export has not completed")
    expect(export_outcome).to_contain_text("Use the processing job in step 5")
    expect(export_outcome).to_contain_text("before optional ground-truth generation")

    sync_step_button.click()
    process_action.click()
    expect(page.get_by_text("Dataset processing queued")).to_be_visible()
    assert processing_requests == [{"run_root": RUN_ROOT}]
    processing_job_status["value"] = "running"
    job_status = processing.get_by_test_id("dataset-processing-job-status")
    expect(job_status).to_contain_text("Dataset processing is running", timeout=5_000)
    expect(job_status).to_contain_text("dataset-processing-1")
    expect(job_status).to_contain_text("continues after navigation")
    expect(
        job_status.get_by_role("link", name="Open live log in Jobs")
    ).to_have_attribute("href", "#/jobs")
    expect(page.get_by_role("button", name="Processing…")).to_be_disabled()
    expect(sync_step_button).to_contain_text("Running")

    processing_job_status["value"] = "succeeded"
    expect(job_status).to_contain_text(
        "Processing finished; export evidence is still being verified",
        timeout=5_000,
    )
    expect(job_status).to_contain_text(
        "has not yet accepted bop/bop_export_manifest.json"
    )

    next(section for section in overview["sidebar"] if section["id"] == "bop")[
        "artifacts"
    ] = [
        {
            "path": "camera_rectification_report.json",
            "exists": True,
            "status": "complete",
        },
        {
            "path": "bop/bop_export_manifest.json",
            "exists": True,
            "status": "complete",
        },
    ]
    job_status.get_by_role("button", name="Refresh evidence").click()
    expect(job_status).to_contain_text(
        "Dataset processing finished and BOP export is verified"
    )
    expect(sync_step_button).to_contain_text("Complete")
    export_step_button.click()
    export_outcome = page.locator('[data-workflow-step="export"]')
    expect(export_outcome.get_by_text("BOP image/model export is ready")).to_be_visible(
        timeout=5_000
    )
    expect(export_outcome).to_contain_text(
        "base export has populated calibrated scenes, models, and object targets"
    )


def test_robot_controls_validate_and_confirm_start_and_stop(
    console_server, page
) -> None:
    commands: list[dict] = []
    install_common_mocks(page)

    def command_handler(route) -> None:
        commands.append(route.request.post_data_json)
        fulfill_json(
            route, {"job_id": f"robot-{len(commands)}", "status": "queued"}, status=202
        )

    page.route("**/run-command", command_handler)
    page.goto(f"{console_server.url}/#/devices", wait_until="networkidle")

    page.get_by_label("Robot IP").fill("")
    page.get_by_role("button", name="Start IIWA").click()
    expect(page.get_by_text("Enter a valid robot IP and port")).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert commands == []

    page.get_by_label("Robot IP").fill("172.31.1.200")
    page.get_by_label("Command port").fill("30301")
    page.get_by_role("button", name="Start IIWA").click()
    expect(page.get_by_role("dialog")).to_contain_text("172.31.1.200:30301")
    expect(page.get_by_role("dialog")).to_contain_text(
        "Manual test request: 0.1 m/s (100 mm/s)"
    )
    expect(page.get_by_role("button", name="Queue start")).to_be_disabled()
    expect(page.get_by_role("dialog").get_by_role("checkbox")).to_have_count(1)
    page.get_by_text("I confirm this is the intended lab IIWA target.").click()
    expect(
        page.get_by_text("I authorize motion of the real lab IIWA for this start.")
    ).to_be_visible()
    expect(
        page.get_by_text("I confirm the capture cameras and pose receiver are ready.")
    ).to_be_visible()
    expect(page.get_by_role("button", name="Queue start")).to_be_enabled()
    page.get_by_role("button", name="Queue start").click()
    expect(page.get_by_text("IIWA start queued")).to_be_visible()

    page.get_by_role("button", name="Stop IIWA").click()
    stop_warning = page.get_by_test_id("iiwa-stop-warning")
    expect(stop_warning).to_contain_text("IIWA STOP is not a safety stop")
    expect(stop_warning).to_contain_text("cannot interrupt active motion")
    expect(stop_warning).to_contain_text(
        "Sunrise must be restarted manually before another START"
    )
    expect(page.get_by_role("button", name="Queue stop")).to_be_disabled()
    expect(page.get_by_role("dialog").get_by_role("checkbox")).to_have_count(1)
    assert [item["command"] for item in commands] == ["start_iiwa"]
    page.get_by_text("I confirm this is the intended lab IIWA target.").click()
    page.get_by_role("button", name="Queue stop").click()
    expect(page.get_by_text("IIWA stop queued")).to_be_visible()

    assert commands == [
        {
            "command": "start_iiwa",
            "robot_ip": "172.31.1.200",
            "robot_port": 30301,
            "allow_real_robot": True,
            "allow_cameras": True,
        },
        {"command": "stop_iiwa", "robot_ip": "172.31.1.200", "robot_port": 30301},
    ]


def test_jobs_log_cancel_and_removed_artifacts_route(console_server, page) -> None:
    install_common_mocks(page)
    page.add_init_script(
        """
        window.__copiedDebugTexts = [];
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: {
            writeText: async () => {
              throw new DOMException("Clipboard permission denied", "NotAllowedError");
            },
          },
        });
        document.execCommand = (command) => {
          if (command !== "copy") return false;
          const target = document.activeElement;
          if (!(target instanceof HTMLTextAreaElement)) return false;
          if (!target.closest('[role="dialog"]')) return false;
          window.__copiedDebugTexts.push(
            target.value.slice(target.selectionStart, target.selectionEnd)
          );
          return true;
        };
        """
    )
    canceled: list[str] = []
    job = {
        "id": "capture-1",
        "name": "pipeline:sync_run",
        "command": ["uv"],
        "cwd": "/repo",
        "status": "running",
        "created_at": "2026-07-10T12:00:00Z",
        "log_path": "/tmp/log",
        "started_at": "2026-07-10T12:00:01Z",
        "ended_at": None,
        "returncode": None,
        "message": None,
        "tail": ["working"],
        "resources": ["disk_io"],
        "parameters": {"pipeline_stage": "sync_run", "run_root": RUN_ROOT},
        "scope_kind": "run",
        "run_root": RUN_ROOT,
    }
    page.route(
        LOCAL_JOBS_URL_RE,
        lambda route: fulfill_json(
            route,
            {
                "jobs": [job],
                "resources": {"disk_io": "capture-1"},
                "total": 1,
                "status_counts": {"running": 1},
                "next_cursor": None,
                "limit": 20,
            },
        ),
    )
    page.route(
        "**/jobs/capture-1/log",
        lambda route: route.fulfill(
            status=200, content_type="text/plain", body="line one\nline two\n"
        ),
    )
    page.route(
        "**/jobs/capture-1/cancel",
        lambda route: (
            canceled.append("capture-1"),
            fulfill_json(route, {"job": {**job, "status": "canceling"}}),
        )[1],
    )
    page.goto(f"{console_server.url}/#/jobs", wait_until="networkidle")
    page.get_by_role("button", name="Log").click()
    expect(page.locator('[data-testid="job-log"]')).to_contain_text("line two")
    page.get_by_role("button", name="Copy output").click()
    expect(page.get_by_text("Job output copied")).to_be_visible()
    page.get_by_role("button", name="Copy context").click()
    expect(page.get_by_text("Job context copied")).to_be_visible()
    copied = page.evaluate("window.__copiedDebugTexts")
    assert copied[0] == "line one\nline two\n"
    context = json.loads(copied[1])
    assert context["schema_version"] == "posetestbot_job_debug_context.v1"
    assert context["job"]["id"] == "capture-1"
    assert context["job"]["parameters"] == {
        "pipeline_stage": "sync_run",
        "run_root": RUN_ROOT,
    }
    assert context["job"]["scope_kind"] == "run"
    assert context["job"]["run_root"] == RUN_ROOT
    assert "tail" not in context["job"]
    page.get_by_role("button", name="Cancel job").click()
    assert canceled == ["capture-1"]

    expect(page.get_by_role("link", name="Artifacts")).to_have_count(0)
    page.goto(f"{console_server.url}/#/artifacts", wait_until="networkidle")
    expect(page).to_have_url(f"{console_server.url}/#/dashboard")


def test_calibration_workflow_explains_intrinsics_and_saves_complete_bundle(
    console_server, page
) -> None:
    requests: list[dict] = []
    promotion_status: dict[str, str | None] = {"value": None}
    install_common_mocks(page)
    setup = {
        "schema_version": "calibration_setup.v1",
        "run_root": RUN_ROOT,
        "cameras": [
            {
                "sensor_key": "realsense_d435:wrist-1",
                "sensor_name": "realsense_wrist-1",
                "display_name": "Wrist RGB-D",
                "sensor_type": "realsense_d435",
                "device_id": "wrist-1",
                "current_mounting_mode": "eye_in_hand",
            },
            {
                "sensor_key": "oak_d_pro:static-1",
                "sensor_name": "luxonis_static-1",
                "display_name": "Auxiliary OAK-D",
                "sensor_type": "oak_d_pro",
                "device_id": "static-1",
                "current_mounting_mode": "eye_in_hand",
            },
        ],
        "unavailable_cameras": [],
        "saved_targets": [
            {
                "target_id": "5f09f41c-dd91-44ef-a048-1f43fc990e17",
                "display_name": "Lab board",
                "valid": True,
                "selected": True,
                "selected_placement": {
                    "mode": "unknown",
                    "mounting_frame": "template_base",
                },
            },
            {
                "target_id": "9ab5ff1c-60f6-46b1-823d-2a912d5d4e3f",
                "display_name": "Alternate board",
                "valid": True,
            },
        ],
        "modes": [
            {
                "id": "eye_in_hand",
                "label": "Robot-mounted camera (eye-in-hand)",
                "primary_transform": "camera → robot_flange",
                "target_mounting": "stationary relative to template_base",
            },
            {
                "id": "eye_to_hand",
                "label": "Static camera (eye-to-hand)",
                "primary_transform": "camera → template_base",
                "target_mounting": "rigidly attached to robot_flange",
            },
        ],
        "solver": {
            "default_pnp_methods": ["IPPE", "ITERATIVE", "SQPNP"],
            "default_extrinsic_methods": [
                "tsai",
                "park",
                "horaud",
                "andreff",
                "daniilidis",
                "shah",
                "li",
            ],
            "intrinsics_policy": "compare_factory_opencv",
            "intrinsics_policies": [
                {
                    "id": "compare_factory_opencv",
                    "label": "Compare captured factory intrinsics with a gated OpenCV calibration",
                },
                {
                    "id": "reuse_compatible_or_factory",
                    "label": "Reuse an exact compatible profile, otherwise captured factory intrinsics",
                },
            ],
            "synchronization": {
                "implementation_revision": "constant_latency_nearest_pose_motion_lomo_fail_closed.v4",
                "default_policy": "auto_offset",
                "policies": [
                    {
                        "id": "auto_offset",
                        "label": "Auto-estimate robot-pose offset — recommended",
                        "description": "Estimate effective per-camera latency.",
                    },
                    {
                        "id": "fixed_zero",
                        "label": "Use captured timestamps (0 ms)",
                        "description": "Use the recorded pairing.",
                    },
                ],
                "search": {
                    "minimum_robot_pose_time_offset_ms": -300.0,
                    "maximum_robot_pose_time_offset_ms": 300.0,
                    "step_ms": 5.0,
                    "max_nearest_pose_delta_ms": 150.0,
                    "warning_nearest_pose_delta_ms": 20.0,
                    "warning_absolute_robot_pose_time_offset_ms": 150.0,
                    "time_offset_failure_policy": "fail_closed",
                    "minimum_motion_count_per_cross_validation_fold": 4,
                    "maximum_leave_one_motion_out_search_adjusted_sign_p_value": 0.05,
                },
            },
            "thresholds": {
                "min_pnp_common_inliers": 12,
                "min_pnp_common_inlier_ratio": 0.5,
                "max_pnp_all_point_mean_reprojection_error_px": 3.0,
                "min_pnp_supported_markers": 4,
                "min_pnp_grid_rows": 2,
                "min_pnp_grid_columns": 2,
                "min_accepted_views": 15,
                "min_coverage_cells": 6,
                "image_coverage_tail_support_views": 5,
                "min_image_centroid_x_span_ratio": 0.45,
                "min_image_centroid_y_span_ratio": 0.35,
                "min_image_centroid_hull_area_ratio": 0.1,
                "max_per_view_reprojection_error_px": 3.0,
                "max_intrinsic_rms_reprojection_error_px": 1.5,
                "min_motion_poses": 4,
                "min_translation_span_mm": 20.0,
                "min_rotation_span_deg": 5.0,
                "min_rotation_axis_second_to_first_ratio": 0.15,
                "max_nearest_pose_delta_ms": 150.0,
                "warning_nearest_pose_delta_ms": 20.0,
            },
        },
        "latest_attempt": None,
    }
    page.route("**/calibration/setup?**", lambda route: fulfill_json(route, setup))

    def create_handler(route) -> None:
        requests.append(
            {"path": "/calibration/attempts", "body": route.request.post_data_json}
        )
        fulfill_json(
            route,
            {"attempt_id": "a" * 32, "job_id": "calculation-1", "status": "queued"},
            status=202,
        )

    page.route("**/calibration/attempts", create_handler)
    transform = {
        "from": "camera",
        "to": "robot_flange",
        "matrix": [[1, 0, 0, 10], [0, 1, 0, 20], [0, 0, 1, 30], [0, 0, 0, 1]],
        "rotation_quaternion_wxyz": [1, 0, 0, 0],
        "translation_mm": [10, 20, 30],
    }
    recommended = {
        "candidate_id": "realsense_d435:wrist-1|IPPE|park",
        "profile_id": "wrist_ippe_park",
        "pnp_method": "IPPE",
        "extrinsic_method": "park",
        "algorithms": ["IPPE", "park"],
        "status": "passing",
        "validation_state": "passed",
        "recommended": True,
        "score": 0.12,
        "observation_count": 10,
        "inlier_count": 9,
        "outlier_count": 1,
        "outlier_ratio": 0.1,
        "mean_reprojection_error_px": 0.25,
        "primary_transform": transform,
        "companion_transform": {
            **transform,
            "from": "aruco_grid",
            "to": "template_base",
        },
        "held_out_residuals": {
            "mean_translation_mm": 0.8,
            "median_translation_mm": 0.7,
            "mean_rotation_deg": 0.3,
            "median_rotation_deg": 0.2,
        },
    }
    override = {
        **recommended,
        "candidate_id": "realsense_d435:wrist-1|SQPNP|tsai",
        "profile_id": "wrist_sqpnp_tsai",
        "pnp_method": "SQPNP",
        "extrinsic_method": "tsai",
        "recommended": False,
        "score": 0.2,
    }
    static_transform = {
        **transform,
        "to": "robot_flange",
        "translation_mm": [110, 20, 530],
        "matrix": [[1, 0, 0, 110], [0, 1, 0, 20], [0, 0, 1, 530], [0, 0, 0, 1]],
    }
    static_recommended = {
        **recommended,
        "candidate_id": "oak_d_pro:static-1|IPPE|park",
        "profile_id": "static_ippe_park",
        "recommended": True,
        "score": 0.16,
        "primary_transform": static_transform,
        "companion_transform": {
            **static_transform,
            "from": "aruco_grid",
            "to": "template_base",
        },
    }
    failed = {
        "candidate_id": "oak_d_pro:static-1|ITERATIVE|li",
        "pnp_method": "ITERATIVE",
        "extrinsic_method": "li",
        "algorithms": ["ITERATIVE", "li"],
        "status": "error",
        "validation_state": "failed",
        "score": None,
        "observation_count": 3,
        "inlier_count": 0,
        "outlier_count": 3,
        "outlier_ratio": 1,
        "error": "leave-one-pose-out validation requires at least four poses",
    }

    def attempt_payload() -> dict:
        return {
            "schema_version": "calibration_attempt.v1",
            "attempt_id": "a" * 32,
            "request": {
                "mode": "eye_in_hand",
                "sensor_keys": ["realsense_d435:wrist-1", "oak_d_pro:static-1"],
                "target_id": setup["saved_targets"][0]["target_id"],
                "solver_policy": "auto_compare",
                "intrinsics_policy": "compare_factory_opencv",
                "synchronization_policy": "auto_offset",
            },
            "progress": {
                "status": "complete",
                "message": "Calibration calculations are complete and awaiting review.",
                "phases": [
                    {
                        "id": "prepare_data",
                        "label": "Prepare data",
                        "status": "complete",
                    },
                    {
                        "id": "estimate_target_poses",
                        "label": "Estimate target poses",
                        "status": "complete",
                    },
                    {
                        "id": "estimate_time_offsets",
                        "label": "Estimate time alignment",
                        "status": "complete",
                    },
                    {
                        "id": "compare_robot_camera_solutions",
                        "label": "Compare robot-camera solutions",
                        "status": "complete",
                    },
                    {
                        "id": "validate_and_rank",
                        "label": "Validate and rank",
                        "status": "complete",
                    },
                ],
            },
            "results": {
                "status": "complete",
                "recommended_camera_count": 2,
                "failed_camera_count": 0,
                "results": [
                    {
                        **setup["cameras"][0],
                        "status": "passing",
                        "recommended_candidate_id": recommended["candidate_id"],
                        "recommendation": recommended,
                        "candidates": [recommended, override],
                    },
                    {
                        **setup["cameras"][1],
                        "status": "passing",
                        "recommended_candidate_id": static_recommended["candidate_id"],
                        "recommendation": static_recommended,
                        "candidates": [static_recommended, failed],
                    },
                ],
            },
            "intrinsic_comparison": {
                "policy": "compare_factory_opencv",
                "sensors": [
                    {
                        "sensor_key": "realsense_d435:wrist-1",
                        "status": "manual_selected",
                        "selected_profile_id": "wrist_manual",
                        "selection_reason": "manual_opencv_passed_all_intrinsic_quality_gates",
                        "factory_profile_id": "wrist_factory",
                        "manual_profile_id": "wrist_manual",
                        "manual_failure": None,
                        "deltas": {
                            "focal_length_delta_px": [1.25, -0.75],
                            "principal_point_delta_px": [0.5, -0.25],
                            "max_abs_distortion_delta": 0.002,
                        },
                        "candidates": [
                            {
                                "profile_id": "wrist_manual",
                                "source": {"mode": "calibrate"},
                                "quality": {
                                    "status": "accepted",
                                    "accepted_view_count": 18,
                                    "coverage_cells": [0, 1, 2, 3, 4, 5],
                                    "rms_reprojection_error_px": 0.82,
                                },
                            }
                        ],
                    },
                    {
                        "sensor_key": "oak_d_pro:static-1",
                        "status": "factory_selected",
                        "selected_profile_id": "static_factory",
                        "selection_reason": "manual_opencv_not_available_or_failed_quality_gates",
                        "factory_profile_id": "static_factory",
                        "manual_profile_id": None,
                        "manual_failure": {
                            "message": "coverage failed",
                            "quality": {"reason": "coverage 2/9 is below 6/9"},
                        },
                        "deltas": None,
                        "candidates": [],
                    },
                ],
            },
            "time_offset_search": {
                "implementation_revision": "constant_latency_nearest_pose_motion_lomo_fail_closed.v4",
                "policy": "auto_offset",
                "status": "complete",
                "sign_convention": {
                    "operator_equation": "robot_pose_query_time = frame_time + offset",
                    "positive_operator_value": "pair the frame with a robot pose recorded later",
                    "conversion": "sync_delta_ms = -robot_pose_time_offset_ms",
                },
                "search": {
                    "minimum_robot_pose_time_offset_ms": -300.0,
                    "maximum_robot_pose_time_offset_ms": 300.0,
                    "step_ms": 5.0,
                    "max_nearest_pose_delta_ms": 150.0,
                    "warning_nearest_pose_delta_ms": 20.0,
                    "warning_absolute_robot_pose_time_offset_ms": 150.0,
                    "time_offset_failure_policy": "fail_closed",
                },
                "sensors": [
                    {
                        "sensor_key": "realsense_d435:wrist-1",
                        "sensor_name": "realsense_wrist-1",
                        "display_name": "Wrist RGB-D",
                        "status": "applied",
                        "decision_reason": "motion_disjoint_cross_validation_passed",
                        "selected_robot_pose_time_offset_ms": 65.0,
                        "selected_sync_delta_ms": -65.0,
                        "candidate_robot_pose_time_offset_ms": 65.0,
                        "evidence_strength": "strong",
                        "boundary_hit": False,
                        "selection_extrinsic_method": "shah",
                        "improvement_evidence_strategy": "leave_one_motion_out_consistency",
                        "split": {
                            "motion_count": 17,
                            "selected_observation_count": 102,
                            "fold_motion_counts": {"0": 6, "1": 6, "2": 5},
                        },
                        "cross_validation": {
                            "zero_offset": {
                                "residuals": {
                                    "mean_translation_mm": 3.91,
                                    "median_translation_mm": 3.8,
                                    "max_translation_mm": 6.0,
                                    "mean_rotation_deg": 0.42,
                                    "median_rotation_deg": 0.4,
                                    "max_rotation_deg": 0.8,
                                }
                            },
                            "candidate": {
                                "residuals": {
                                    "mean_translation_mm": 2.77,
                                    "median_translation_mm": 2.6,
                                    "max_translation_mm": 4.8,
                                    "mean_rotation_deg": 0.39,
                                    "median_rotation_deg": 0.37,
                                    "max_rotation_deg": 0.7,
                                }
                            },
                            "improvement": {
                                "absolute_translation_mm": 1.14,
                                "relative_translation": 0.29156,
                                "rotation_change_deg": -0.03,
                            },
                        },
                        "motion_consistency": {
                            "status": "ok",
                            "strategy": "leave_one_motion_out_candidate_consistency_bonferroni.v1",
                            "motion_count": 17,
                            "candidate_search_adjustment": "bonferroni",
                            "candidate_search_hypothesis_count": 120,
                            "methods": {
                                "shah": {
                                    "status": "ok",
                                    "motion_count": 17,
                                    "positive_motion_count": 17,
                                    "material_motion_count": 16,
                                    "positive_sign_p_value": 0.0000076294,
                                    "candidate_search_adjusted_positive_sign_p_value": 0.000457764,
                                    "median_improvement": {
                                        "absolute_translation_mm": 0.811,
                                        "relative_translation": 0.2864,
                                        "rotation_change_deg": -0.02,
                                    },
                                },
                                "li": {
                                    "status": "ok",
                                    "motion_count": 17,
                                    "positive_motion_count": 16,
                                    "material_motion_count": 16,
                                    "positive_sign_p_value": 0.000137329,
                                    "candidate_search_adjusted_positive_sign_p_value": 0.00823974,
                                    "median_improvement": {
                                        "absolute_translation_mm": 0.792,
                                        "relative_translation": 0.2941,
                                        "rotation_change_deg": -0.018,
                                    },
                                },
                            },
                            "thresholds": {
                                "minimum_median_absolute_translation_mm": 0.25,
                                "minimum_median_relative_translation": 0.1,
                                "maximum_search_adjusted_positive_sign_p_value": 0.05,
                            },
                        },
                        "checks": [
                            {
                                "name": "cross_validation_offset_stability",
                                "status": "ok",
                                "actual": 10.0,
                                "threshold": 22.0,
                            },
                        ],
                        "curve": [
                            {
                                "robot_pose_time_offset_ms": 0.0,
                                "residuals": {
                                    "mean_translation_mm": 3.91,
                                    "median_translation_mm": 3.8,
                                    "max_translation_mm": 6.0,
                                    "mean_rotation_deg": 0.42,
                                    "median_rotation_deg": 0.4,
                                    "max_rotation_deg": 0.8,
                                },
                            },
                            {
                                "robot_pose_time_offset_ms": 65.0,
                                "residuals": {
                                    "mean_translation_mm": 2.77,
                                    "median_translation_mm": 2.6,
                                    "max_translation_mm": 4.8,
                                    "mean_rotation_deg": 0.39,
                                    "median_rotation_deg": 0.37,
                                    "max_rotation_deg": 0.7,
                                },
                            },
                        ],
                    },
                    {
                        "sensor_key": "oak_d_pro:static-1",
                        "sensor_name": "luxonis_static-1",
                        "display_name": "Auxiliary OAK-D",
                        "status": "applied",
                        "decision_reason": "motion_disjoint_cross_validation_passed",
                        "selected_robot_pose_time_offset_ms": 85.0,
                        "selected_sync_delta_ms": -85.0,
                        "candidate_robot_pose_time_offset_ms": 85.0,
                        "evidence_strength": "consistent",
                        "boundary_hit": False,
                        "split": {
                            "motion_count": 15,
                            "selected_observation_count": 90,
                            "fold_motion_counts": {"0": 5, "1": 5, "2": 5},
                        },
                        "cross_validation": {
                            "zero_offset": {
                                "residuals": {
                                    "mean_translation_mm": 4.7,
                                    "median_translation_mm": 4.5,
                                    "max_translation_mm": 7.0,
                                    "mean_rotation_deg": 0.5,
                                    "median_rotation_deg": 0.45,
                                    "max_rotation_deg": 0.9,
                                }
                            },
                            "candidate": {
                                "residuals": {
                                    "mean_translation_mm": 3.0,
                                    "median_translation_mm": 2.8,
                                    "max_translation_mm": 5.0,
                                    "mean_rotation_deg": 0.4,
                                    "median_rotation_deg": 0.38,
                                    "max_rotation_deg": 0.8,
                                }
                            },
                            "improvement": {
                                "absolute_translation_mm": 1.7,
                                "relative_translation": 0.3617,
                                "rotation_change_deg": -0.1,
                            },
                        },
                        "checks": [
                            {
                                "name": "reference_method_sensitivity",
                                "status": "warning",
                                "actual": 28.0,
                                "warning_threshold": 22.0,
                                "failure_threshold": 44.0,
                            }
                        ],
                        "curve": [],
                    },
                ],
            },
            "promotion": (
                {
                    "status": "promoted",
                    "promoted_profile_ids": ["wrist_sqpnp_tsai", "static_ippe_park"],
                }
                if promotion_status["value"] == "promoted"
                else {
                    "status": promotion_status["value"],
                    "job_id": "promotion-1",
                }
                if promotion_status["value"] in {"queued", "running"}
                else None
            ),
        }

    page.route(
        "**/calibration/attempts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?**",
        lambda route: fulfill_json(route, attempt_payload()),
    )

    def promote_handler(route) -> None:
        requests.append(
            {"path": "/calibration/promote", "body": route.request.post_data_json}
        )
        promotion_status["value"] = "queued"
        fulfill_json(
            route,
            {
                "attempt_id": "a" * 32,
                "job_id": "promotion-1",
                "status": "queued",
                "selections": route.request.post_data_json["candidate_ids"],
            },
            status=202,
        )

    page.route(
        "**/calibration/attempts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/promote",
        promote_handler,
    )
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(
        f"{console_server.url}/#/workflow/calibration?step=calculate",
        wait_until="networkidle",
    )

    expect(page.get_by_test_id("calibration-workflow")).to_be_visible()
    analysis_arrangement = page.get_by_test_id("calibration-analysis-arrangement")
    expect(analysis_arrangement).to_have_attribute(
        "data-calibration-mode", "eye_in_hand"
    )
    expect(analysis_arrangement).to_have_attribute(
        "data-target-mounting-frame", "template_base"
    )
    expect(analysis_arrangement).to_contain_text("Robot-mounted cameras · fixed target")
    expect(
        analysis_arrangement.get_by_role("link", name="Edit cameras in step 1")
    ).to_have_attribute("href", "#/workflow/calibration?step=configure")
    expect(page.locator('input[name="calibration-mode"]')).to_have_count(0)
    expect(page.locator("[data-stage-id]")).to_have_count(0)
    intrinsics_guidance = page.locator('[data-workflow-step="calculate"]')
    expect(intrinsics_guidance).to_contain_text("Factory and OpenCV intrinsics")
    expect(intrinsics_guidance).to_contain_text(
        "A lower RMS alone does not make it the preferred model"
    )
    intrinsics_guidance.get_by_role(
        "button", name="About Factory and OpenCV intrinsics"
    ).hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "Factory is the per-camera projection supplied by the camera SDK"
    )
    page.keyboard.press("Escape")
    expect(page.get_by_text("Automatic extrinsic solution comparison")).to_be_visible()
    expect(page.locator('input[value="auto_offset"]')).to_be_checked()
    expect(page.get_by_test_id("calibration-synchronization-policy")).to_contain_text(
        "It does not synchronize hardware clocks or rewrite raw frame or robot timestamps"
    )
    page.get_by_text("Time-alignment search limits and warning policy").click()
    expect(page.get_by_test_id("calibration-synchronization-policy")).to_contain_text(
        "At least 12 eligible motion groups are required"
    )
    expect(page.get_by_test_id("calibration-synchronization-policy")).to_contain_text(
        "above 20 ms are warnings; matches remain usable through 150 ms"
    )
    page.get_by_test_id("calibration-synchronization-policy").get_by_role(
        "button", name="About robot-pose time-offset sign"
    ).hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "positive robot-pose time offset pairs a frame"
    )
    page.locator('input[value="fixed_zero"]').check()
    expect(page.locator('input[value="fixed_zero"]')).to_be_checked()
    page.locator('input[value="auto_offset"]').check()
    camera_choices = page.get_by_test_id("calibration-workflow").get_by_role("checkbox")
    expect(camera_choices).to_have_count(2)
    camera_choices.nth(1).click()
    expect(camera_choices.nth(1)).not_to_be_checked()
    camera_choices.nth(1).click()
    expect(
        page.get_by_test_id("calibration-workflow").get_by_text("Lab board", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("button", name="Analyze recording")).to_be_enabled()
    page.get_by_role("button", name="Analyze recording").click()
    expect(page.get_by_text("Calibration queued")).to_be_visible()
    assert requests[0]["body"] == {
        "run_root": RUN_ROOT,
        "mode": "eye_in_hand",
        "sensor_keys": [
            "realsense_d435:wrist-1",
            "oak_d_pro:static-1",
        ],
        "target_id": "5f09f41c-dd91-44ef-a048-1f43fc990e17",
        "synchronization_policy": "auto_offset",
    }

    expect(page.get_by_text("Prepare data")).to_be_visible()
    expect(page.locator('[data-phase-id="estimate_time_offsets"]')).to_contain_text(
        "Estimate time alignment"
    )
    attempt_job = page.get_by_test_id("calibration-attempt-job-status")
    expect(attempt_job.get_by_test_id("calibration-duration-guidance")).to_contain_text(
        "three-camera comparison usually takes 10–20 minutes"
    )
    expect(attempt_job).to_contain_text("background work continues after navigation")
    expect(attempt_job.get_by_role("link", name="Open Jobs")).to_have_attribute(
        "href", "#/jobs"
    )
    expect(page.get_by_test_id("calibration-results")).to_be_visible()
    alignment = page.get_by_test_id("calibration-time-alignment")
    expect(alignment).to_contain_text(
        "not evidence that the hardware clocks are synchronized"
    )
    expect(page.get_by_test_id("calibration-time-alignment-warning")).to_contain_text(
        "Calibration has advisory timing evidence"
    )
    expect(page.get_by_test_id("calibration-time-alignment-warning")).to_contain_text(
        "passed every required fail-closed identification and stability check"
    )
    page.mouse.move(0, 0)
    alignment.get_by_role(
        "button", name="About robot-pose time-offset evidence"
    ).hover()
    expect(page.get_by_role("tooltip")).to_contain_text(
        "positive offset uses a later robot pose"
    )
    expect(
        alignment.locator('[data-time-offset-sensor="realsense_d435:wrist-1"]')
    ).to_contain_text("+65.0 ms")
    expect(
        alignment.locator('[data-time-offset-sensor="realsense_d435:wrist-1"]')
    ).to_contain_text("3.910 → 2.770 mm")
    expect(
        alignment.locator('[data-time-offset-sensor="realsense_d435:wrist-1"]')
    ).to_contain_text("29.2%")
    expect(
        page.get_by_test_id("timing-motion-summary-realsense_d435:wrist-1")
    ).to_contain_text("17/17 held-out motions improved")
    oak_alignment = alignment.locator('[data-time-offset-sensor="oak_d_pro:static-1"]')
    expect(oak_alignment).to_contain_text("Applied with 1 warning")
    expect(oak_alignment).to_contain_text("reference method sensitivity")
    alignment.get_by_text("Advanced offset evidence · Wrist RGB-D").click()
    expect(alignment).to_contain_text("cross validation offset stability")
    motion_consistency = page.get_by_test_id(
        "timing-motion-consistency-realsense_d435:wrist-1"
    )
    expect(motion_consistency).to_contain_text("Bonferroni-corrected")
    expect(motion_consistency).to_contain_text("120 nonzero offset candidates")
    expect(motion_consistency).to_contain_text("16/17")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    expect(page.get_by_test_id("calibration-acceptance-thresholds")).to_contain_text(
        "≥15 accepted views"
    )
    expect(page.get_by_test_id("calibration-acceptance-thresholds")).to_contain_text(
        "≥45% of image width"
    )
    expect(page.get_by_test_id("calibration-acceptance-thresholds")).to_contain_text(
        "3 × 3 centroid-cell count remains diagnostic"
    )
    wrist_intrinsics = page.get_by_test_id(
        "intrinsic-comparison-realsense_d435:wrist-1"
    )
    expect(wrist_intrinsics).to_contain_text("Using OpenCV estimate")
    expect(wrist_intrinsics).to_contain_text(
        "Factory values come from the camera SDK. The OpenCV estimate is fitted from this recording's grid views."
    )
    expect(wrist_intrinsics).to_contain_text("OpenCV training views / image coverage")
    expect(wrist_intrinsics).to_contain_text("18 views · 6 of 9 regions")
    static_intrinsics = page.get_by_test_id("intrinsic-comparison-oak_d_pro:static-1")
    expect(static_intrinsics).to_contain_text("Using factory SDK values")
    expect(static_intrinsics).to_contain_text("The factory SDK values are compatible")
    expect(static_intrinsics).to_contain_text("coverage 2/9 is below 6/9")
    wrist_result = page.locator('[data-camera-key="realsense_d435:wrist-1"]')
    expect(wrist_result).to_contain_text("Reusable robot-mounted-camera transform")
    expect(wrist_result).to_contain_text("camera → robot_flange")
    expect(
        page.get_by_text("All attempted solutions and failures").first
    ).to_be_visible()
    wrist_result.get_by_text("Alternative solution (advanced)", exact=True).click()
    wrist_result.get_by_label("Alternative solution", exact=True).click()
    page.get_by_role("option", name="SQPNP + tsai · score 0.2000").click()
    page.get_by_role("button", name="Save selected calibrations").click()
    expect(page.get_by_text("Calibration acceptance queued")).to_be_visible()
    promotion_job = page.get_by_test_id("calibration-promotion-job-status")
    expect(promotion_job).to_contain_text("continues after navigation")
    expect(promotion_job.get_by_role("link", name="Open Jobs")).to_have_attribute(
        "href", "#/jobs"
    )
    assert requests[-1]["body"]["candidate_ids"] == {
        "realsense_d435:wrist-1": override["candidate_id"],
        "oak_d_pro:static-1": static_recommended["candidate_id"],
    }
    promotion_status["value"] = "promoted"
    expect(page.get_by_role("button", name="Calibrations saved")).to_be_visible()
    expect(page.get_by_text("Saved 2 camera profile(s).")).to_be_visible()


def calibration_time_alignment_setup(
    *,
    latest_attempt_id: str | None,
    latest_status: str = "complete",
    implementation_revision: str | None = (
        "constant_latency_nearest_pose_motion_lomo_fail_closed.v4"
    ),
) -> dict:
    latest_attempt = (
        {"attempt_id": latest_attempt_id, "status": latest_status}
        if latest_attempt_id
        else None
    )
    return {
        "schema_version": "calibration_setup.v1",
        "run_root": RUN_ROOT,
        "cameras": [
            {
                "sensor_key": "realsense_d435:wrist-1",
                "sensor_name": "realsense_wrist-1",
                "display_name": "Wrist RGB-D",
                "sensor_type": "realsense_d435",
                "device_id": "wrist-1",
                "current_mounting_mode": "eye_in_hand",
            }
        ],
        "unavailable_cameras": [],
        "saved_targets": [
            {
                "target_id": "5f09f41c-dd91-44ef-a048-1f43fc990e17",
                "display_name": "Lab board",
                "valid": True,
                "selected": True,
                "selected_placement": {
                    "mode": "unknown",
                    "mounting_frame": "template_base",
                },
            }
        ],
        "modes": [
            {
                "id": "eye_in_hand",
                "label": "Robot-mounted camera (eye-in-hand)",
                "primary_transform": "camera → robot_flange",
                "target_mounting": "stationary relative to template_base",
            },
            {
                "id": "eye_to_hand",
                "label": "Static camera (eye-to-hand)",
                "primary_transform": "camera → template_base",
                "target_mounting": "rigidly attached to robot_flange",
            },
        ],
        "solver": {
            "default_pnp_methods": ["IPPE", "ITERATIVE", "SQPNP"],
            "default_extrinsic_methods": ["shah", "li"],
            "intrinsics_policy": "compare_factory_opencv",
            "intrinsics_policies": [],
            "synchronization": {
                "implementation_revision": implementation_revision,
                "default_policy": "auto_offset",
                "policies": [
                    {
                        "id": "auto_offset",
                        "label": "Auto-estimate robot-pose offset — recommended",
                        "description": "Estimate effective per-camera latency.",
                    },
                    {
                        "id": "fixed_zero",
                        "label": "Use captured timestamps (0 ms)",
                        "description": "Use the recorded pairing.",
                    },
                ],
                "search": {
                    "minimum_robot_pose_time_offset_ms": -300.0,
                    "maximum_robot_pose_time_offset_ms": 300.0,
                    "step_ms": 5.0,
                    "max_nearest_pose_delta_ms": 150.0,
                    "warning_nearest_pose_delta_ms": 20.0,
                    "warning_absolute_robot_pose_time_offset_ms": 150.0,
                    "time_offset_failure_policy": "fail_closed",
                },
            },
            "thresholds": {
                "min_pnp_common_inliers": 12,
                "min_pnp_common_inlier_ratio": 0.5,
                "max_pnp_all_point_mean_reprojection_error_px": 3.0,
                "min_pnp_supported_markers": 4,
                "min_pnp_grid_rows": 2,
                "min_pnp_grid_columns": 2,
                "min_accepted_views": 15,
                "min_coverage_cells": 6,
                "max_per_view_reprojection_error_px": 3.0,
                "max_intrinsic_rms_reprojection_error_px": 1.5,
                "min_motion_poses": 4,
                "min_translation_span_mm": 20.0,
                "min_rotation_span_deg": 5.0,
                "min_rotation_axis_second_to_first_ratio": 0.15,
                "max_nearest_pose_delta_ms": 150.0,
                "warning_nearest_pose_delta_ms": 20.0,
            },
        },
        "latest_attempt": latest_attempt,
    }


def test_calibration_workflow_explains_immutable_legacy_timing_attempt(
    console_server,
    page,
) -> None:
    attempt_id = "e" * 32
    setup = calibration_time_alignment_setup(
        latest_attempt_id=attempt_id,
        latest_status="failed",
    )
    install_common_mocks(page)
    page.route("**/calibration/setup?**", lambda route: fulfill_json(route, setup))
    attempt = {
        "schema_version": "calibration_attempt.v1",
        "attempt_id": attempt_id,
        "request": {
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:wrist-1"],
            "target_id": setup["saved_targets"][0]["target_id"],
            "solver_policy": "auto_compare",
            "intrinsics_policy": "compare_factory_opencv",
            "synchronization_policy": "auto_offset",
        },
        "progress": calibration_attempt_progress(
            status="failed",
            time_alignment_status="failed",
            message="ValueError: Auto-sync evidence failed closed",
        ),
        "results": None,
        "intrinsic_comparison": None,
        "time_offset_search": {
            "implementation_revision": "constant_latency_nearest_pose_motion_cv.v1",
            "policy": "auto_offset",
            "status": "failed",
            "sign_convention": {
                "operator_equation": "robot_pose_query_time = frame_time + offset",
                "positive_operator_value": (
                    "pair the frame with a robot pose recorded later"
                ),
                "conversion": "sync_delta_ms = -robot_pose_time_offset_ms",
            },
            "search": {
                "minimum_robot_pose_time_offset_ms": -150.0,
                "maximum_robot_pose_time_offset_ms": 150.0,
                "step_ms": 5.0,
            },
            "sensors": [
                {
                    "sensor_key": "realsense_d435:wrist-1",
                    "status": "failed",
                    "decision_reason": "candidate_failed_safety_or_stability_checks",
                    "selected_robot_pose_time_offset_ms": 0.0,
                    "selected_sync_delta_ms": 0.0,
                    "candidate_robot_pose_time_offset_ms": 45.0,
                    "evidence_strength": "insufficient",
                    "boundary_hit": False,
                    "checks": [],
                    "curve": [],
                }
            ],
        },
        "promotion": None,
    }
    page.route(
        f"**/calibration/attempts/{attempt_id}?**",
        lambda route: fulfill_json(route, attempt),
    )

    page.goto(
        f"{console_server.url}/#/workflow/calibration?step=calculate",
        wait_until="networkidle",
    )

    expect(page.get_by_test_id("calibration-backend-restart-required")).to_have_count(0)
    warning = page.get_by_test_id("calibration-attempt-legacy-timing-revision")
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("Historical timing evidence · inspection only")
    expect(warning).to_contain_text("cannot be rerun or promoted")
    expect(page.get_by_test_id("calibration-time-alignment-failed")).to_contain_text(
        "decided by the recorded legacy rule"
    )


def calibration_attempt_progress(
    *,
    status: str,
    time_alignment_status: str,
    message: str,
) -> dict:
    later_status = "pending" if status == "failed" else "complete"
    return {
        "status": status,
        "message": message,
        "phases": [
            {"id": "prepare_data", "label": "Prepare data", "status": "complete"},
            {
                "id": "estimate_target_poses",
                "label": "Estimate target poses",
                "status": "complete",
            },
            {
                "id": "estimate_time_offsets",
                "label": "Estimate time alignment",
                "status": time_alignment_status,
            },
            {
                "id": "compare_robot_camera_solutions",
                "label": "Compare robot-camera solutions",
                "status": later_status,
            },
            {
                "id": "validate_and_rank",
                "label": "Validate and rank",
                "status": later_status,
            },
        ],
    }


def calibration_failed_results(camera: dict) -> dict:
    return {
        "status": "failed",
        "recommended_camera_count": 0,
        "failed_camera_count": 1,
        "results": [
            {
                **camera,
                "status": "failed",
                "recommended_candidate_id": None,
                "recommendation": None,
                "candidates": [],
            }
        ],
    }


def test_failed_auto_sync_evidence_remains_visible_without_solver_results(
    console_server,
    page,
) -> None:
    attempt_id = "f" * 32
    setup = calibration_time_alignment_setup(
        latest_attempt_id=attempt_id,
        latest_status="failed",
    )
    install_common_mocks(page)
    page.route("**/calibration/setup?**", lambda route: fulfill_json(route, setup))
    attempt = {
        "schema_version": "calibration_attempt.v1",
        "attempt_id": attempt_id,
        "request": {
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:wrist-1"],
            "target_id": setup["saved_targets"][0]["target_id"],
            "solver_policy": "auto_compare",
            "intrinsics_policy": "compare_factory_opencv",
            "synchronization_policy": "auto_offset",
        },
        "progress": calibration_attempt_progress(
            status="failed",
            time_alignment_status="failed",
            message=(
                "ValueError: Auto-sync evidence failed closed for: "
                "realsense_d435:wrist-1"
            ),
        ),
        "results": None,
        "intrinsic_comparison": None,
        "time_offset_search": {
            "implementation_revision": "constant_latency_nearest_pose_motion_lomo_cv.v2",
            "policy": "auto_offset",
            "status": "failed",
            "sign_convention": {
                "operator_equation": "robot_pose_query_time = frame_time + offset",
                "positive_operator_value": (
                    "pair the frame with a robot pose recorded later"
                ),
                "conversion": "sync_delta_ms = -robot_pose_time_offset_ms",
            },
            "search": {
                "minimum_robot_pose_time_offset_ms": -150.0,
                "maximum_robot_pose_time_offset_ms": 150.0,
                "step_ms": 5.0,
            },
            "sensors": [
                {
                    "sensor_key": "realsense_d435:wrist-1",
                    "sensor_name": "realsense_wrist-1",
                    "display_name": "Wrist RGB-D",
                    "status": "failed",
                    "decision_reason": ("candidate_failed_safety_or_stability_checks"),
                    "selected_robot_pose_time_offset_ms": 0.0,
                    "selected_sync_delta_ms": 0.0,
                    "candidate_robot_pose_time_offset_ms": 150.0,
                    "evidence_strength": "insufficient",
                    "boundary_hit": True,
                    "split": {
                        "motion_count": 15,
                        "selected_observation_count": 90,
                        "fold_motion_counts": {"0": 5, "1": 5, "2": 5},
                    },
                    "cross_validation": {
                        "zero_offset": {
                            "residuals": {
                                "mean_translation_mm": 4.0,
                                "median_translation_mm": 3.9,
                                "max_translation_mm": 6.0,
                                "mean_rotation_deg": 0.5,
                                "median_rotation_deg": 0.45,
                                "max_rotation_deg": 0.9,
                            }
                        },
                        "candidate": {
                            "residuals": {
                                "mean_translation_mm": 2.8,
                                "median_translation_mm": 2.7,
                                "max_translation_mm": 4.5,
                                "mean_rotation_deg": 0.47,
                                "median_rotation_deg": 0.43,
                                "max_rotation_deg": 0.8,
                            }
                        },
                        "improvement": {
                            "absolute_translation_mm": 1.2,
                            "relative_translation": 0.3,
                            "rotation_change_deg": -0.03,
                        },
                    },
                    "checks": [
                        {
                            "name": "reference_method_sensitivity",
                            "status": "warning",
                            "actual": 30.0,
                            "warning_threshold": 22.0,
                            "failure_threshold": 44.0,
                        },
                        {
                            "name": "search_optimum_not_at_boundary",
                            "status": "error",
                            "actual": 150.0,
                            "threshold": [-150.0, 150.0],
                        },
                    ],
                    "curve": [
                        {
                            "robot_pose_time_offset_ms": 0.0,
                            "residuals": {
                                "mean_translation_mm": 4.0,
                                "median_translation_mm": 3.9,
                                "max_translation_mm": 6.0,
                                "mean_rotation_deg": 0.5,
                                "median_rotation_deg": 0.45,
                                "max_rotation_deg": 0.9,
                            },
                        },
                        {
                            "robot_pose_time_offset_ms": 150.0,
                            "residuals": {
                                "mean_translation_mm": 2.8,
                                "median_translation_mm": 2.7,
                                "max_translation_mm": 4.5,
                                "mean_rotation_deg": 0.47,
                                "median_rotation_deg": 0.43,
                                "max_rotation_deg": 0.8,
                            },
                        },
                    ],
                }
            ],
        },
        "promotion": None,
    }
    page.route(
        f"**/calibration/attempts/{attempt_id}?**",
        lambda route: fulfill_json(route, attempt),
    )

    page.goto(
        f"{console_server.url}/#/workflow/calibration?step=calculate",
        wait_until="networkidle",
    )

    expect(page.get_by_test_id("calibration-results")).to_have_count(0)
    alignment = page.get_by_test_id("calibration-time-alignment")
    expect(alignment).to_be_visible()
    expect(page.get_by_test_id("calibration-time-alignment-failed")).to_contain_text(
        "Auto time alignment stopped this calibration"
    )
    row = alignment.locator('[data-time-offset-sensor="realsense_d435:wrist-1"]')
    expect(row).to_contain_text("Time alignment rejected")
    expect(row).to_contain_text("Applied +0.0 ms")
    expect(row).to_contain_text("Rejected candidate +150.0 ms")
    expect(row).to_contain_text("0 ms → rejected +150.0 ms candidate")
    expect(row).to_contain_text("reference method sensitivity")
    expect(row).to_contain_text("search optimum not at boundary")
    expect(page.get_by_role("button", name="Save selected calibrations")).to_have_count(
        0
    )
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


def test_fixed_zero_policy_is_submitted_and_reported(
    console_server,
    page,
) -> None:
    attempt_id = "0" * 32
    setup = calibration_time_alignment_setup(latest_attempt_id=None)
    requests: list[dict] = []
    install_common_mocks(page)
    page.route("**/calibration/setup?**", lambda route: fulfill_json(route, setup))

    def create_handler(route) -> None:
        requests.append(route.request.post_data_json)
        fulfill_json(
            route,
            {"attempt_id": attempt_id, "job_id": "fixed-zero-job", "status": "queued"},
            status=202,
        )

    page.route("**/calibration/attempts", create_handler)
    attempt = {
        "schema_version": "calibration_attempt.v1",
        "attempt_id": attempt_id,
        "request": {
            "mode": "eye_in_hand",
            "sensor_keys": ["realsense_d435:wrist-1"],
            "target_id": setup["saved_targets"][0]["target_id"],
            "solver_policy": "auto_compare",
            "intrinsics_policy": "compare_factory_opencv",
            "synchronization_policy": "fixed_zero",
        },
        "progress": calibration_attempt_progress(
            status="complete",
            time_alignment_status="complete",
            message="Calibration calculations are complete and awaiting review.",
        ),
        "results": calibration_failed_results(setup["cameras"][0]),
        "intrinsic_comparison": None,
        "time_offset_search": {
            "implementation_revision": "constant_latency_nearest_pose_motion_lomo_cv.v2",
            "policy": "fixed_zero",
            "status": "complete",
            "sign_convention": {
                "operator_equation": "robot_pose_query_time = frame_time + offset",
                "positive_operator_value": (
                    "pair the frame with a robot pose recorded later"
                ),
                "conversion": "sync_delta_ms = -robot_pose_time_offset_ms",
            },
            "search": {
                "minimum_robot_pose_time_offset_ms": -150.0,
                "maximum_robot_pose_time_offset_ms": 150.0,
                "step_ms": 5.0,
            },
            "sensors": [
                {
                    "sensor_key": "realsense_d435:wrist-1",
                    "sensor_name": "realsense_wrist-1",
                    "display_name": "Wrist RGB-D",
                    "status": "fixed_zero",
                    "decision_reason": "fixed_zero_policy_selected",
                    "selected_robot_pose_time_offset_ms": 0.0,
                    "selected_sync_delta_ms": 0.0,
                    "candidate_robot_pose_time_offset_ms": 0.0,
                    "evidence_strength": "not_applicable",
                    "boundary_hit": False,
                    "checks": [],
                    "curve": [],
                }
            ],
        },
        "promotion": None,
    }
    page.route(
        f"**/calibration/attempts/{attempt_id}?**",
        lambda route: fulfill_json(route, attempt),
    )
    page.goto(
        f"{console_server.url}/#/workflow/calibration?step=calculate",
        wait_until="networkidle",
    )

    page.locator('input[value="fixed_zero"]').check()
    page.get_by_role("button", name="Analyze recording").click()

    expect(page.get_by_text("Calibration queued")).to_be_visible()
    assert requests[-1]["synchronization_policy"] == "fixed_zero"
    row = page.get_by_test_id("calibration-time-alignment").locator(
        '[data-time-offset-sensor="realsense_d435:wrist-1"]'
    )
    expect(row).to_contain_text("Fixed zero selected")
    expect(row).to_contain_text("Applied +0.0 ms")
    expect(row).to_contain_text("not applicable")
    expect(page.get_by_test_id("calibration-time-alignment-failed")).to_have_count(0)


def pose_template_catalog() -> dict:
    return {
        "schema_version": "object_catalog.v1",
        "objects": [
            {
                "catalog_uuid": "11111111-1111-4111-8111-111111111111",
                "obj_id": 7,
                "name": "Clamp",
                "alias": "Small clamp",
                "description": "Textured fixture",
                "tags": ["metal", "reflective"],
                "groups": ["clamps", "validation set"],
                "attributes": {"owner": "vision", "finish": "matte"},
                "source_filename": "clamp.stl",
                "source_format": "stl",
                "source_sha256": "a" * 64,
                "canonical_ply_sha256": "b" * 64,
                "geometry_revision": 1,
                "source_to_mm_scale": 1.0,
                "texture_sha256": "c" * 64,
                "created_at": "2026-07-20T09:00:00Z",
                "updated_at": "2026-07-20T10:00:00Z",
                "archived_at": None,
                "state": "active",
                "extraction": {
                    "vertices": 8,
                    "faces": 12,
                    "bounds_mm": [[-5, -5, -5], [5, 5, 5]],
                    "watertight": True,
                },
                "assets": {
                    "source": {
                        "path": "objects/1/source/clamp.stl",
                        "sha256": "a" * 64,
                        "size_bytes": 100,
                        "media_type": "application/octet-stream",
                    },
                    "canonical_ply": {
                        "path": "objects/1/derived/canonical.ply",
                        "sha256": "b" * 64,
                        "size_bytes": 80,
                        "media_type": "application/octet-stream",
                    },
                    "texture": {
                        "path": "objects/1/texture/texture.png",
                        "sha256": "c" * 64,
                        "size_bytes": 40,
                        "media_type": "image/png",
                    },
                },
                "usage": {"template_count": 0, "templates": []},
            }
        ],
    }


def workpiece_catalog() -> dict:
    value = pose_template_catalog()
    value.update(
        {
            "version": 4,
            "created_at": "2026-07-20T09:00:00Z",
            "updated_at": "2026-07-21T11:00:00Z",
            "next_obj_id": 9,
            "tombstones": [],
        }
    )
    value["objects"].append(
        {
            "catalog_uuid": "88888888-8888-4888-8888-888888888888",
            "obj_id": 8,
            "name": "Gauge block",
            "alias": "Archived gauge",
            "description": "Reference ceramic block",
            "tags": ["Metal", "reference"],
            "groups": ["gauges"],
            "attributes": {"length_mm": "25"},
            "source_filename": "gauge.ply",
            "source_format": "ply",
            "source_sha256": "d" * 64,
            "canonical_ply_sha256": "e" * 64,
            "geometry_revision": 1,
            "source_to_mm_scale": 1.0,
            "texture_sha256": None,
            "created_at": "2026-07-20T09:30:00Z",
            "updated_at": "2026-07-21T11:00:00Z",
            "archived_at": "2026-07-21T11:00:00Z",
            "state": "archived",
            "extraction": {
                "vertices": 8,
                "faces": 12,
                "bounds_mm": [[-12.5, -5, -2.5], [12.5, 5, 2.5]],
                "watertight": True,
            },
            "assets": {
                "source": {
                    "path": "objects/8/source/gauge.ply",
                    "sha256": "d" * 64,
                    "size_bytes": 120,
                    "media_type": "application/octet-stream",
                },
                "canonical_ply": {
                    "path": "objects/8/derived/canonical.ply",
                    "sha256": "e" * 64,
                    "size_bytes": 90,
                    "media_type": "application/octet-stream",
                },
            },
            "usage": {"template_count": 0, "templates": []},
        }
    )
    return value


def pose_template_orientation_analysis(
    catalog_uuid: str = "11111111-1111-4111-8111-111111111111",
) -> dict:
    return {
        "schema_version": "pose_template_orientation_analysis.v1",
        "catalog_uuid": catalog_uuid,
        "preview_mesh": {
            "vertices": [
                [-10, -5, 0],
                [10, -5, 0],
                [10, 5, 0],
                [-10, 5, 0],
                [0, 0, 12],
            ],
            "faces": [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4], [0, 3, 2], [0, 2, 1]],
        },
        "recognition_mesh": {
            "vertices": [
                [-10, -5, 0],
                [10, -5, 0],
                [10, 5, 0],
                [-10, 5, 0],
                [-10, -5, 12],
                [10, -5, 12],
                [10, 5, 12],
                [-10, 5, 12],
            ],
            "faces": [
                [0, 1, 2],
                [0, 2, 3],
                [4, 6, 5],
                [4, 7, 6],
                [0, 4, 5],
                [0, 5, 1],
                [1, 5, 6],
                [1, 6, 2],
                [2, 6, 7],
                [2, 7, 3],
                [3, 7, 4],
                [3, 4, 0],
            ],
        },
        "recognition_mesh_approximation": {
            "strategy": "welded_source",
            "implementation_revision": "posetestbot_posetemplatecreator_adapter.v4",
            "source_vertices": 8,
            "source_faces": 12,
            "welded_vertices": 8,
            "welded_faces": 12,
            "result_vertices": 8,
            "result_faces": 12,
            "source_components": 1,
            "source_euler_number": 2,
            "result_components": 1,
            "result_euler_number": 2,
            "topology_preserved": True,
            "spatial_resolution": None,
            "fallback_reason": None,
        },
        "orientations": [
            {
                "orientation_id": "stable-wide",
                "label": "Wide base",
                "probability": 0.82,
                "source_to_placed": [
                    [1, 0, 0, 0],
                    [0, 1, 0, 0],
                    [0, 0, 1, 0],
                    [0, 0, 0, 1],
                ],
                "slice_z_mm": 0.1,
                "contours": [
                    {
                        "points": [
                            {"x_mm": -10, "y_mm": -5},
                            {"x_mm": 10, "y_mm": -5},
                            {"x_mm": 7, "y_mm": 5},
                            {"x_mm": -10, "y_mm": 3},
                        ]
                    }
                ],
            },
            {
                "orientation_id": "stable-side",
                "label": "Side base",
                "probability": 0.18,
                "source_to_placed": [
                    [1, 0, 0, 0],
                    [0, 0, -1, 0],
                    [0, 1, 0, 5],
                    [0, 0, 0, 1],
                ],
                "slice_z_mm": 0.1,
                "contours": [
                    {
                        "points": [
                            {"x_mm": -10, "y_mm": -6},
                            {"x_mm": 10, "y_mm": -6},
                            {"x_mm": 10, "y_mm": 6},
                            {"x_mm": -10, "y_mm": 6},
                        ]
                    }
                ],
            },
        ],
    }


def pose_template_orientation_thumbnail(
    catalog_uuid: str = "11111111-1111-4111-8111-111111111111",
) -> dict:
    analysis = pose_template_orientation_analysis(catalog_uuid)
    orientation = analysis["orientations"][0]
    return {
        "schema_version": "pose_template_orientation_thumbnail.v1",
        "catalog_uuid": catalog_uuid,
        "catalog": {"catalog_uuid": catalog_uuid},
        "source": {"canonical_ply_sha256": "b" * 64, "geometry_revision": 1},
        "preview_mesh": analysis["preview_mesh"],
        "orientation": {
            "orientation_id": orientation["orientation_id"],
            "label": orientation["label"],
            "rank": 1,
            "probability": orientation["probability"],
            "slice_z_mm": orientation["slice_z_mm"],
            "source_to_placed": orientation["source_to_placed"],
        },
    }
