"""Thin browser-safe proxy for the external cluster controller."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from posetestbot.bop.evaluation import (
    import_external_bop_result,
    inspect_dataset,
    public_dataset_descriptor,
)
from posetestbot.cluster.client import ClusterClientError, new_idempotency_key
from posetestbot.jobs.runner import ResourceBusyError, TERMINAL_STATUSES
from posetestbot.run_folders import (
    resolve_destination_root,
    resolve_direct_run_folder,
    validate_expected_identity,
)
from posetestbot.web.runtime import (
    get_cluster_client,
    get_cluster_service_manager,
    get_job_runner,
    get_web_runtime,
)
from posetestbot.web.security import resolve_web_run_root, web_run_roots


cluster_bp = Blueprint("cluster", __name__)
CONTROLLER_ID_RE = re.compile(
    r"^(?:archive|job|pose|restore)-[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SUCCESS_STATES = {"succeeded", "succeeded-with-warning"}
PUBLIC_PROFILE_FIELDS = {
    "profile_id",
    "enabled",
    "partition",
    "gres",
    "cpus",
    "memory",
    "walltime",
    "max_targets",
}
PUBLIC_ESTIMATOR_FIELDS = {
    "estimator_id",
    "driver_id",
    "display_name",
    "installed",
    "configured",
    "enabled",
    "ready",
    "input_contracts",
    "output_contract",
}
PUBLIC_SERVICE_FIELDS = {
    "managed",
    "service_unit",
    "unit_installed",
    "state",
    "active",
    "can_start",
    "can_stop",
    "load_state",
    "active_state",
    "sub_state",
    "unit_file_state",
}
CONTROLLER_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s'\"<>|,;)\]}]+)")
CONTROLLER_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
CONTROLLER_SECRET_LINE_RE = re.compile(
    r"(?im)^.*\b(?:authorization|api[_ -]?token|password|private key|secret)\b\s*[:=].*$"
)
PUBLIC_ESTIMATOR_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
PUBLIC_DRIVER_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,95}$")
PUBLIC_RUNTIME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,95}$")
PUBLIC_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PUBLIC_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
PUBLIC_CONTRACT_RE = re.compile(r"^[a-z][a-z0-9._-]{2,127}$")
PUBLIC_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
PUBLIC_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _json_object() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _require_id(value: Any, *, prefix: str | None = None) -> str:
    if not isinstance(value, str) or CONTROLLER_ID_RE.fullmatch(value) is None:
        raise ValueError("Controller identifier is invalid")
    if prefix is not None and not value.startswith(f"{prefix}-"):
        raise ValueError("Controller identifier has the wrong kind")
    return value


def _error(exc: Exception):
    if isinstance(exc, ClusterClientError):
        return jsonify({"output": _public_controller_text(exc)}), exc.status
    if isinstance(exc, ResourceBusyError | FileExistsError | RuntimeError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, FileNotFoundError | KeyError):
        return jsonify({"output": str(exc)}), 404
    if isinstance(exc, PermissionError):
        return jsonify({"output": str(exc)}), 403
    return jsonify({"output": str(exc)}), 400


def _settings():
    return get_web_runtime().settings


def _require_cluster_enabled() -> None:
    if not _settings().cluster_enabled:
        raise PermissionError("Cluster integration is disabled")


def _public_controller_text(value: Any) -> str | None:
    if value is None:
        return None
    text = CONTROLLER_SECRET_LINE_RE.sub("[redacted controller detail]", str(value))
    text = CONTROLLER_BEARER_RE.sub("Bearer [redacted]", text)
    return CONTROLLER_PATH_RE.sub("[controller path]", text)


def _selected_mapping(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {field: value[field] for field in fields if field in value}


def _safe_public_string(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _public_runtime_artifact(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    filename = _safe_public_string(value.get("filename"), PUBLIC_FILENAME_RE)
    digest = _safe_public_string(value.get("sha256"), PUBLIC_SHA256_RE)
    return (
        {"filename": filename, "sha256": digest}
        if filename is not None and digest is not None
        else {}
    )


def _public_estimator_runtime(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    container = _public_runtime_artifact(value.get("container"))
    assets = {
        artifact_id: public_artifact
        for artifact_id, artifact in (
            value.get("assets", {}).items()
            if isinstance(value.get("assets"), Mapping)
            else []
        )
        if _safe_public_string(artifact_id, PUBLIC_COMPONENT_RE) is not None
        and (public_artifact := _public_runtime_artifact(artifact))
    }
    source_revisions = {
        source_id: revision
        for source_id, revision in (
            value.get("source_revisions", {}).items()
            if isinstance(value.get("source_revisions"), Mapping)
            else []
        )
        if _safe_public_string(source_id, PUBLIC_COMPONENT_RE) is not None
        and _safe_public_string(revision, PUBLIC_REVISION_RE) is not None
    }
    licenses = []
    for item in value.get("licenses", []):
        if not isinstance(item, Mapping):
            continue
        name = _public_controller_text(item.get("name"))
        digest = _safe_public_string(item.get("sha256"), PUBLIC_SHA256_RE)
        if name and digest and len(name) <= 160:
            licenses.append({"name": name, "sha256": digest})
    public: dict[str, Any] = {
        "container": container,
        "assets": assets,
        "source_revisions": source_revisions,
        "licenses": licenses,
        "input_contracts": [
            item
            for item in value.get("input_contracts", [])
            if _safe_public_string(item, PUBLIC_CONTRACT_RE) is not None
        ],
        "qualified_resource_profiles": [
            item
            for item in value.get("qualified_resource_profiles", [])
            if _safe_public_string(item, PUBLIC_COMPONENT_RE) is not None
        ],
        "qualification_blockers": [
            _public_controller_text(item) or "Estimator runtime is not ready."
            for item in value.get("qualification_blockers", [])
        ],
    }
    for field, pattern in (
        ("estimator_id", PUBLIC_ESTIMATOR_ID_RE),
        ("driver_id", PUBLIC_DRIVER_ID_RE),
        ("runtime_id", PUBLIC_RUNTIME_ID_RE),
        ("output_contract", PUBLIC_CONTRACT_RE),
        ("qualification_manifest_sha256", PUBLIC_SHA256_RE),
    ):
        selected = _safe_public_string(value.get(field), pattern)
        if selected is not None:
            public[field] = selected
    for field in ("qualified", "ready"):
        if isinstance(value.get(field), bool):
            public[field] = value[field]
    return public


def _public_estimator(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError("The controller returned an invalid estimator")
    estimator_id = _safe_public_string(
        value.get("estimator_id"), PUBLIC_ESTIMATOR_ID_RE
    )
    if estimator_id is None:
        raise RuntimeError("The controller returned an invalid estimator identifier")
    public = _selected_mapping(value, PUBLIC_ESTIMATOR_FIELDS)
    public["estimator_id"] = estimator_id
    public["display_name"] = (
        _public_controller_text(value.get("display_name")) or estimator_id
    )[:120]
    driver_id = _safe_public_string(value.get("driver_id"), PUBLIC_DRIVER_ID_RE)
    if driver_id is None:
        public.pop("driver_id", None)
    else:
        public["driver_id"] = driver_id
    public["input_contracts"] = [
        item
        for item in value.get("input_contracts", [])
        if _safe_public_string(item, PUBLIC_CONTRACT_RE) is not None
    ]
    output_contract = _safe_public_string(
        value.get("output_contract"), PUBLIC_CONTRACT_RE
    )
    if output_contract is None:
        public.pop("output_contract", None)
    else:
        public["output_contract"] = output_contract
    return {
        **public,
        "blockers": [
            _public_controller_text(item) or "Estimator submission is blocked."
            for item in value.get("blockers", [])
        ],
        "readiness_blockers": [
            _public_controller_text(item) or "Estimator is not ready."
            for item in value.get("readiness_blockers", [])
        ],
        "runtime": _public_estimator_runtime(value.get("runtime")),
        "profiles": [
            _selected_mapping(item, PUBLIC_PROFILE_FIELDS)
            for item in value.get("profiles", [])
            if isinstance(item, Mapping)
        ],
    }


def _public_domain(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"ready": False, "blockers": []}
    return {
        **_selected_mapping(value, {"ready", "read", "mutation"}),
        "blockers": [
            _public_controller_text(item) or "Cluster capability is not ready."
            for item in value.get("blockers", [])
        ],
    }


def _public_job(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError("The controller returned an invalid job")
    job_id = _require_id(value.get("job_id"))
    controller_payload = value.get("payload")
    payload = _selected_mapping(
        controller_payload,
        {
            "run_root",
            "estimator_id",
            "driver_id",
            "runtime_id",
            "dataset_alias",
            "dataset_sha256",
            "profile_id",
            "operator",
        },
    )
    if (
        isinstance(controller_payload, Mapping)
        and controller_payload.get("archive_id") is not None
    ):
        payload["archive_id"] = _require_id(
            controller_payload.get("archive_id"), prefix="archive"
        )
    result = _selected_mapping(
        value.get("result"),
        {
            "filename",
            "sha256",
            "dataset_sha256",
            "estimator_id",
            "runtime_id",
            "provenance_sha256",
            "estimate_count",
            "failure_count",
        },
    )
    return {
        "schema_version": "posetestbot_cluster_job.v1",
        "job_id": job_id,
        "kind": value.get("kind"),
        "state": value.get("state"),
        "status": value.get("status", value.get("state")),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "slurm_job_id": value.get("slurm_job_id"),
        "payload": payload,
        "result": result or None,
        "error": _public_controller_text(value.get("error")),
        "log_available": value.get("log_available") is True,
        "cancel_requested": value.get("cancel_requested") is True,
        "terminal": value.get("terminal") is True,
    }


def _public_job_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError("The controller returned an invalid response")
    return {"job": _public_job(value.get("job"))}


def _public_archive(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError("The controller returned an invalid archive")
    return {
        "schema_version": "posetestbot_cluster_archive.v1",
        "archive_id": _require_id(value.get("archive_id"), prefix="archive"),
        "job_id": value.get("job_id"),
        "state": value.get("state"),
        "status": value.get("status", value.get("state")),
        "source_run_root": value.get("source_run_root"),
        "source_identity": _selected_mapping(
            value.get("source_identity"), {"device", "inode"}
        ),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "archive_sha256": value.get("archive_sha256"),
        "operator": value.get("operator"),
        "verified": value.get("verified") is True,
    }


def _controller_status() -> dict[str, Any]:
    settings = _settings()
    integration = {
        "enabled": settings.cluster_enabled,
        "controller_configured": get_web_runtime().cluster_client is not None,
    }
    if not settings.cluster_enabled:
        return {
            "schema_version": "posetestbot_cluster_status_proxy.v1",
            "ready": False,
            "available": False,
            "integration": integration,
            "blockers": [
                {
                    "code": "cluster_disabled",
                    "message": "Cluster integration is disabled on this workstation.",
                }
            ],
        }
    if get_web_runtime().cluster_client is None:
        return {
            "schema_version": "posetestbot_cluster_status_proxy.v1",
            "ready": False,
            "available": False,
            "integration": integration,
            "blockers": [
                {
                    "code": "controller_not_configured",
                    "message": "The loopback cluster controller token is not configured.",
                }
            ],
        }
    try:
        status = get_cluster_client().status()
    except ClusterClientError as exc:
        return {
            "schema_version": "posetestbot_cluster_status_proxy.v1",
            "ready": False,
            "available": False,
            "integration": integration,
            "blockers": [{"code": "controller_unavailable", "message": str(exc)}],
        }
    if (
        status.get("schema_version") != "posetestbot_cluster_status.v1"
        or not isinstance(status.get("domains"), Mapping)
        or not isinstance(status.get("estimators"), list)
        or not isinstance(status.get("domains", {}).get("storage"), Mapping)
        or not isinstance(status.get("domains", {}).get("scheduler"), Mapping)
        or any(not isinstance(item, Mapping) for item in status.get("estimators", []))
    ):
        return {
            "schema_version": "posetestbot_cluster_status_proxy.v1",
            "ready": False,
            "available": False,
            "integration": integration,
            "blockers": [
                {
                    "code": "controller_contract_invalid",
                    "message": (
                        "The cluster controller did not return the current status "
                        "contract with domains and advertised estimators."
                    ),
                }
            ],
        }
    blockers = []
    for index, item in enumerate(status.get("blockers") or []):
        if isinstance(item, Mapping):
            blockers.append(
                {
                    "code": str(item.get("code") or f"controller_blocker_{index + 1}"),
                    "message": _public_controller_text(
                        item.get("message") or "Cluster controller is not ready."
                    ),
                }
            )
        else:
            blockers.append(
                {
                    "code": f"controller_blocker_{index + 1}",
                    "message": _public_controller_text(item),
                }
            )
    profiles = [
        _selected_mapping(item, PUBLIC_PROFILE_FIELDS)
        for item in status.get("profiles", [])
        if isinstance(item, Mapping)
    ]
    runtime = _public_estimator_runtime(status.get("runtime"))
    features = _selected_mapping(
        status.get("features"),
        {
            "pose_estimation",
            "estimation_submission",
            "archive_read",
            "archive_mutation",
            "archive_move",
        },
    )
    raw_feature_blockers = status.get("feature_blockers")
    feature_blockers = {
        key: [
            _public_controller_text(message) or "Cluster feature is not ready."
            for message in messages
        ]
        for key, messages in (
            raw_feature_blockers.items()
            if isinstance(raw_feature_blockers, Mapping)
            else []
        )
        if key in {"estimation", "estimation_submission", "archive", "archive_move"}
        and isinstance(messages, list)
    }
    raw_domains = status.get("domains")
    domains = {
        key: _public_domain(value)
        for key, value in (
            raw_domains.items() if isinstance(raw_domains, Mapping) else []
        )
        if key in {"storage", "scheduler"}
    }
    estimators = [
        _public_estimator(item)
        for item in status.get("estimators", [])
        if isinstance(item, Mapping)
    ]
    configuration_blockers = [
        _public_controller_text(item) or "Controller configuration is invalid."
        for item in status.get("configuration_blockers", [])
    ]
    if not estimators:
        configuration_blockers.append(
            "The controller did not advertise any estimators."
        )
    return {
        "schema_version": "posetestbot_cluster_status_proxy.v1",
        "ready": status.get("ready") is True,
        "available": True,
        "mode": status.get("mode"),
        "features": features,
        "feature_blockers": feature_blockers,
        "domains": domains,
        "estimators": estimators,
        "configuration_blockers": configuration_blockers,
        "runtime": runtime,
        "profiles": profiles,
        "integration": integration,
        "blockers": blockers,
    }


def _controller_service_status() -> dict[str, Any]:
    runtime = get_web_runtime()
    settings = runtime.settings
    integration = {
        "enabled": settings.cluster_enabled,
        "controller_configured": runtime.cluster_client is not None,
        "environment_file_configured": settings.cluster_env_file is not None,
    }
    manager = runtime.cluster_service_manager
    if manager is None:
        return {
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
            "integration": integration,
            "blockers": [
                {
                    "code": "service_management_not_configured",
                    "message": (
                        "Controller lifecycle management is not configured for "
                        "this web process."
                    ),
                }
            ],
        }
    raw = manager.status()
    selected = _selected_mapping(raw, PUBLIC_SERVICE_FIELDS)
    blockers = []
    for index, item in enumerate(raw.get("blockers") or []):
        if isinstance(item, Mapping):
            blockers.append(
                {
                    "code": str(item.get("code") or f"service_blocker_{index + 1}"),
                    "message": str(
                        item.get("message") or "Controller service is unavailable."
                    ),
                }
            )
    return {
        "schema_version": "posetestbot_cluster_controller_service.v1",
        **selected,
        "integration": integration,
        "blockers": blockers,
    }


def _load_bop_manifest(run_root: Path) -> Mapping[str, Any]:
    path = run_root / "bop" / "bop_export_manifest.json"
    if path.is_symlink() or not path.is_file():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, Mapping):
        raise ValueError("BOP export manifest must be a JSON object")
    return value


def _build_pose_setup(
    run_root: Path, *, estimator_id: str | None = None
) -> dict[str, Any]:
    # Readiness stays request-bounded: the companion hashes every staged file
    # in its background worker before submission, while this identity binds the
    # existing BOP metadata and semantic content without synchronously reading
    # every depth image in a Flask request.
    dataset = inspect_dataset(run_root)
    manifest = _load_bop_manifest(run_root)
    status = _controller_status()
    estimators = (
        status.get("estimators") if isinstance(status.get("estimators"), list) else []
    )
    available_ids = {
        item.get("estimator_id")
        for item in estimators
        if isinstance(item, Mapping) and isinstance(item.get("estimator_id"), str)
    }
    selected_id = estimator_id
    if selected_id is None:
        selected_id = next(iter(sorted(available_ids)), None)
    selected = next(
        (
            item
            for item in estimators
            if isinstance(item, Mapping) and item.get("estimator_id") == selected_id
        ),
        None,
    )
    blockers = [
        {"code": f"dataset_{index + 1}", "message": str(message)}
        for index, message in enumerate(dataset.get("blockers", []))
    ]
    if selected is None:
        blockers.append(
            {
                "code": "estimator_unavailable",
                "message": (
                    "The selected estimator is not installed on the cluster controller."
                ),
            }
        )
        blockers.extend(
            {
                "code": "controller_configuration",
                "message": str(message),
            }
            for message in status.get("configuration_blockers", [])
        )
    input_contracts = (
        selected.get("input_contracts", []) if isinstance(selected, Mapping) else []
    )
    oracle_masks_required = "posetestbot.bop.v5.pose_and_masks" in input_contracts
    if oracle_masks_required and manifest.get("annotation_mode") != "pose_and_masks":
        blockers.append(
            {
                "code": "pose_and_masks_required",
                "message": (
                    "The selected estimator requires a complete BOP v5 "
                    "pose_and_masks export with visible GT instance masks."
                ),
            }
        )
    if oracle_masks_required and dataset.get("split") != "test":
        blockers.append(
            {
                "code": "test_split_required",
                "message": "The selected estimator requires the exported test split.",
            }
        )
    capabilities = manifest.get("capabilities")
    if oracle_masks_required and (
        not isinstance(capabilities, Mapping)
        or capabilities.get("gt_masks_visible") is not True
    ):
        blockers.append(
            {
                "code": "visible_masks_missing",
                "message": "The BOP export does not declare complete visible GT masks.",
            }
        )
    if not status.get("available"):
        blockers.extend(status.get("blockers") or [])
    elif status.get("ready") is not True:
        blockers.extend(status.get("blockers") or [])
    if isinstance(selected, Mapping) and selected.get("ready") is not True:
        messages = selected.get("readiness_blockers")
        blockers.extend(
            {"code": "controller_estimation_blocked", "message": str(message)}
            for message in (messages if isinstance(messages, list) else [])
        )
    if not _settings().cluster_enabled:
        blockers.append(
            {
                "code": "cluster_disabled",
                "message": "Pose-estimation submission is disabled on this workstation.",
            }
        )
    profiles = (
        selected.get("profiles", [])
        if isinstance(selected, Mapping) and isinstance(selected.get("profiles"), list)
        else []
    )
    enabled_profiles = [
        profile
        for profile in profiles
        if isinstance(profile, Mapping) and profile.get("enabled") is True
    ]
    if not enabled_profiles:
        blockers.append(
            {
                "code": "no_qualified_profile",
                "message": (
                    "No server-owned GPU resource profile is qualified and enabled "
                    "for the selected estimator."
                ),
            }
        )
    unique_blockers = list(
        {
            (str(item.get("code")), str(item.get("message"))): {
                "code": str(item.get("code")),
                "message": str(item.get("message")),
            }
            for item in blockers
            if isinstance(item, Mapping)
        }.values()
    )
    return {
        "schema_version": "cluster_estimation_setup.v2",
        "run_root": run_root.as_posix(),
        "dataset": public_dataset_descriptor(dataset),
        "annotation_mode": manifest.get("annotation_mode"),
        "estimator_id": selected_id,
        "estimator": selected,
        "estimators": estimators,
        "oracle_mask_contract": (
            "bop_mask_visib_gt_instance.v1" if oracle_masks_required else None
        ),
        "score_contract": (
            "constant_1.0_no_detection_confidence" if oracle_masks_required else None
        ),
        "execution_contract": (
            "independent_register_per_target_no_tracking.v1"
            if oracle_masks_required
            else None
        ),
        "controller": status,
        "runtime": (
            selected.get("runtime")
            if status.get("available") and isinstance(selected, Mapping)
            else None
        ),
        "profiles": profiles,
        "enabled_profiles": enabled_profiles,
        "ready": not unique_blockers,
        "blockers": unique_blockers,
        "warnings": (
            [
                {
                    "code": "oracle_gt_masks",
                    "message": (
                        "Every estimate is conditioned on a BOP GT-visible instance "
                        "mask; this is pose estimation, not detection or segmentation."
                    ),
                }
            ]
            if oracle_masks_required
            else []
        ),
    }


def _all_local_jobs():
    return get_job_runner().list(include_services=True)


def _assert_no_active_run_jobs(run_root: Path) -> None:
    active: list[str] = []
    for job in _all_local_jobs():
        if (
            job.status in TERMINAL_STATUSES
            or job.scope_kind != "run"
            or not job.run_root
        ):
            continue
        try:
            same = Path(job.run_root).resolve() == run_root.resolve()
        except OSError:
            same = job.run_root == run_root.as_posix()
        if same:
            active.append(job.id)
    if active:
        raise ResourceBusyError(
            "Run folder has active background work: " + ", ".join(sorted(active))
        )


@cluster_bp.get("/cluster/status")
def cluster_status():
    return jsonify(_controller_status())


@cluster_bp.get("/cluster/controller-service")
def cluster_controller_service_status():
    try:
        return jsonify(_controller_service_status())
    except Exception as exc:
        return _error(exc)


@cluster_bp.post("/cluster/controller-service/<action>")
def control_cluster_controller_service(action: str):
    try:
        if action not in {"start", "stop"}:
            raise ValueError("Controller service action must be start or stop")
        value = _json_object()
        if set(value) != {"confirm"}:
            raise ValueError("Controller service action contains unsupported fields")
        if value.get("confirm") is not True:
            raise ValueError("Controller service action requires explicit confirmation")
        service = _controller_service_status()
        if not service.get("managed"):
            raise RuntimeError(
                "Cluster controller service management is not configured"
            )
        if not service.get("unit_installed"):
            raise RuntimeError(
                "The configured cluster controller service is not installed"
            )
        allowed = (
            service.get("can_start") if action == "start" else service.get("can_stop")
        )
        if not allowed:
            desired_state = "running" if action == "start" else "stopped"
            if service.get("state") == desired_state:
                return jsonify({"accepted": False, "service": service})
            raise RuntimeError(
                f"Cluster controller service cannot {action} while its state is "
                f"{service.get('state') or 'unknown'}"
            )
        manager = get_cluster_service_manager()
        job = get_job_runner().submit(
            name=f"cluster_controller_{action}",
            command=manager.command(action),
            resources=["cluster_controller_service"],
            parameters={
                "cluster_controller_service": True,
                "action": action,
                "service_unit": service.get("service_unit"),
            },
            scope_kind="global",
        )
        return (
            jsonify(
                {
                    "accepted": True,
                    "action": action,
                    "job_id": job.id,
                    "job": job.to_dict(),
                    "service": service,
                }
            ),
            202,
        )
    except Exception as exc:
        return _error(exc)


@cluster_bp.get("/cluster/pose-estimation/setup")
def cluster_pose_setup():
    try:
        run_root = resolve_web_run_root(request.args.get("run_root"))
        estimator_id = request.args.get("estimator_id")
        if (
            estimator_id is not None
            and re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", estimator_id) is None
        ):
            raise ValueError("estimator_id is invalid")
        return jsonify(_build_pose_setup(run_root, estimator_id=estimator_id))
    except Exception as exc:
        return _error(exc)


@cluster_bp.post("/cluster/pose-estimation/jobs")
def submit_cluster_pose_job():
    try:
        _require_cluster_enabled()
        value = _json_object()
        run_root = resolve_web_run_root(value.get("run_root"))
        estimator_id = value.get("estimator_id")
        if (
            not isinstance(estimator_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", estimator_id) is None
        ):
            raise ValueError("estimator_id is required")
        setup = _build_pose_setup(run_root, estimator_id=estimator_id)
        if not setup["ready"]:
            raise RuntimeError(
                "Pose estimation is blocked: "
                + " ".join(item["message"] for item in setup["blockers"])
            )
        profile_id = value.get("profile_id")
        enabled_ids = {
            item.get("profile_id")
            for item in setup["enabled_profiles"]
            if isinstance(item, Mapping)
        }
        if profile_id not in enabled_ids:
            raise ValueError("Selected resource profile is not enabled")
        operator = value.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator is required")
        dataset = inspect_dataset(run_root)
        submission = {
            "estimator_id": estimator_id,
            "run_root": run_root.as_posix(),
            "dataset_alias": dataset["dataset_alias"],
            "dataset_sha256": dataset["dataset_sha256"],
            "profile_id": profile_id,
            "operator": operator.strip(),
        }
        response = get_cluster_client().create_estimation_job(
            submission,
            idempotency_key=new_idempotency_key("estimation-submit"),
        )
        return jsonify(_public_job_response(response)), 202
    except Exception as exc:
        return _error(exc)


@cluster_bp.get("/cluster/jobs")
def list_cluster_jobs():
    try:
        _require_cluster_enabled()
        limit = request.args.get("limit", default=50, type=int)
        if limit is None or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        response = get_cluster_client().estimation_jobs(
            limit=limit,
            state=request.args.get("state"),
        )
        jobs = response.get("jobs") if isinstance(response, Mapping) else None
        if not isinstance(jobs, list):
            raise RuntimeError("The controller returned an invalid job list")
        return jsonify(
            {
                "jobs": [_public_job(job) for job in jobs],
                "next_cursor": response.get("next_cursor"),
            }
        )
    except Exception as exc:
        return _error(exc)


@cluster_bp.get("/cluster/jobs/<job_id>")
def get_cluster_job(job_id: str):
    try:
        _require_cluster_enabled()
        _require_id(job_id)
        response = _public_job_response(get_cluster_client().job(job_id))
        if request.args.get("include_log") in {"1", "true", "yes"}:
            response["log"] = _public_controller_text(
                get_cluster_client().job_log(job_id)
            )
        return jsonify(response)
    except Exception as exc:
        return _error(exc)


@cluster_bp.post("/cluster/jobs/<job_id>/cancel")
def cancel_cluster_job(job_id: str):
    try:
        _require_cluster_enabled()
        _require_id(job_id)
        return (
            jsonify(
                _public_job_response(
                    get_cluster_client().cancel_job(
                        job_id, idempotency_key=new_idempotency_key("job-cancel")
                    )
                )
            ),
            202,
        )
    except Exception as exc:
        return _error(exc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@cluster_bp.post("/cluster/jobs/<job_id>/import-result")
def import_cluster_result(job_id: str):
    import_root: Path | None = None
    try:
        _require_cluster_enabled()
        _require_id(job_id, prefix="pose")
        value = _json_object()
        run_root = resolve_web_run_root(value.get("run_root"))
        response = get_cluster_client().job(job_id)
        job = response.get("job")
        if not isinstance(job, Mapping) or job.get("state") not in SUCCESS_STATES:
            raise RuntimeError(
                "The cluster pose job has no successful result to import"
            )
        payload = job.get("payload")
        result = job.get("result")
        if not isinstance(payload, Mapping) or not isinstance(result, Mapping):
            raise RuntimeError("The cluster job is missing immutable result evidence")
        if payload.get("run_root") != run_root.as_posix():
            raise ValueError("The cluster job belongs to a different run")
        expected_dataset = result.get("dataset_sha256")
        if not isinstance(expected_dataset, str):
            raise RuntimeError("The cluster result has no staged dataset digest")
        import_root = (
            run_root
            / "processed"
            / "bop_evaluation"
            / ".cluster-imports"
            / uuid.uuid4().hex
        )
        import_root.mkdir(parents=True, exist_ok=False)
        filename = result.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise RuntimeError("The controller returned an invalid result filename")
        result_path = get_cluster_client().download_artifact(
            job_id, "result.csv", import_root / filename
        )
        provenance_path = get_cluster_client().download_artifact(
            job_id,
            "provenance.json",
            import_root / "provenance.json",
            max_bytes=8 * 1024 * 1024,
        )
        if _sha256(result_path) != result.get("sha256") or _sha256(
            provenance_path
        ) != result.get("provenance_sha256"):
            raise RuntimeError(
                "Downloaded controller artifacts failed integrity checks"
            )
        provenance = json.loads(provenance_path.read_text())
        if not isinstance(provenance, Mapping):
            raise ValueError("Controller provenance must be a JSON object")
        payload_estimator_id = payload.get("estimator_id")
        result_estimator_id = result.get("estimator_id")
        if (
            payload_estimator_id is not None
            and result_estimator_id is not None
            and payload_estimator_id != result_estimator_id
        ):
            raise RuntimeError("The cluster result estimator identity changed")
        estimator_id = result_estimator_id or payload_estimator_id
        if (
            not isinstance(estimator_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", estimator_id) is None
        ):
            raise RuntimeError("The cluster result has an invalid estimator identity")
        if estimator_id == "foundationpose":
            method_name = "FoundationPose (oracle GT masks)"
        else:
            method_name = f"{estimator_id.replace('_', ' ').title()} (cluster)"
        provenance_estimator = provenance.get("estimator")
        if (
            isinstance(provenance_estimator, Mapping)
            and provenance_estimator.get("estimator_id") != estimator_id
        ):
            raise RuntimeError("Controller provenance names another estimator")
        registered, created = import_external_bop_result(
            run_root,
            result_path,
            external_job_id=job_id,
            expected_dataset_sha256=expected_dataset,
            source_provenance_sha256=result["provenance_sha256"],
            controller_provenance=provenance,
            method_name=method_name,
        )
        result_id = registered["result_id"]
        return (
            jsonify(
                {
                    "result": registered,
                    "created": created,
                    "evaluation_url": f"/bop-evaluation?result_id={result_id}",
                    "download_url": (
                        f"/bop/evaluation/results/{result_id}/download"
                        f"?run_root={run_root.as_posix()}"
                    ),
                }
            ),
            201 if created else 200,
        )
    except Exception as exc:
        return _error(exc)
    finally:
        if import_root is not None:
            shutil.rmtree(import_root, ignore_errors=True)


@cluster_bp.get("/cluster/archives")
def list_cluster_archives():
    try:
        _require_cluster_enabled()
        response = get_cluster_client().archives()
        archives = response.get("archives") if isinstance(response, Mapping) else None
        if not isinstance(archives, list):
            raise RuntimeError("The controller returned an invalid archive list")
        status = _controller_status()
        domains = status.get("domains")
        storage = (
            domains.get("storage")
            if isinstance(domains, Mapping)
            and isinstance(domains.get("storage"), Mapping)
            else {
                "ready": status.get("ready") is True,
                "read": True,
                "mutation": bool(
                    isinstance(status.get("features"), Mapping)
                    and status["features"].get("archive_mutation") is True
                ),
                "blockers": [],
            }
        )
        return jsonify(
            {
                "archives": [_public_archive(archive) for archive in archives],
                "integration": {"enabled": _settings().cluster_enabled},
                "storage": storage,
            }
        )
    except Exception as exc:
        return _error(exc)


@cluster_bp.post("/cluster/archives")
def create_cluster_archive():
    try:
        _require_cluster_enabled()
        value = _json_object()
        run_root = resolve_direct_run_folder(
            resolve_web_run_root(value.get("run_root")),
            allowed_roots=web_run_roots(),
        )
        expected = value.get("expected_identity")
        validate_expected_identity(run_root, expected)
        _assert_no_active_run_jobs(run_root)
        operator = value.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator is required")
        response = get_cluster_client().create_archive(
            {
                "run_root": run_root.as_posix(),
                "operator": operator.strip(),
            },
            idempotency_key=new_idempotency_key("archive-copy"),
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("The controller returned an invalid response")
        return jsonify({"archive": _public_archive(response.get("archive"))}), 202
    except Exception as exc:
        return _error(exc)


@cluster_bp.post("/cluster/archives/<archive_id>/restore")
def restore_cluster_archive(archive_id: str):
    try:
        _require_cluster_enabled()
        _require_id(archive_id, prefix="archive")
        value = _json_object()
        destination_root = resolve_destination_root(
            value.get("destination_root"), allowed_roots=web_run_roots()
        )
        destination_name = value.get("destination_name")
        if destination_name is not None and (
            not isinstance(destination_name, str)
            or Path(destination_name).name != destination_name
            or destination_name in {".", ".."}
        ):
            raise ValueError("destination_name must be one folder name")
        operator = value.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator is required")
        response = get_cluster_client().restore_archive(
            archive_id,
            {
                "destination_root": destination_root.as_posix(),
                "destination_name": destination_name,
                "operator": operator.strip(),
            },
            idempotency_key=new_idempotency_key("archive-restore"),
        )
        return jsonify(_public_job_response(response)), 202
    except Exception as exc:
        return _error(exc)


@cluster_bp.delete("/cluster/archives/<archive_id>")
def delete_cluster_archive(archive_id: str):
    try:
        _require_cluster_enabled()
        _require_id(archive_id, prefix="archive")
        value = _json_object()
        if set(value) != {"confirm", "operator"}:
            raise ValueError("Archive deletion contains unsupported fields")
        if value.get("confirm") is not True:
            raise ValueError("Archive deletion requires explicit confirmation")
        operator = value.get("operator")
        if not isinstance(operator, str) or not 2 <= len(operator.strip()) <= 120:
            raise ValueError("operator must contain between 2 and 120 characters")
        response = get_cluster_client().delete_archive(
            archive_id,
            {"confirm": True, "operator": operator.strip()},
            idempotency_key=new_idempotency_key("archive-delete"),
        )
        return jsonify(_public_job_response(response)), 202
    except Exception as exc:
        return _error(exc)
