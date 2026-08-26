"""Thin browser-safe proxy for the external cluster controller."""

from __future__ import annotations

import hashlib
import json
import math
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
from posetestbot.sensors.registry import sensor_folder_name
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
PUBLIC_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PUBLIC_SENSOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
JOB_SETTINGS_DESCRIPTOR_SCHEMA = (
    "posetestbot_cluster_job_settings_descriptor.v1"
)
JOB_SETTINGS_SCHEMA = "posetestbot_cluster_job_settings.v1"
EXECUTION_MODES = {"continuous_tracking", "independent_registration"}


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


def _public_job_settings_descriptor(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema_version",
            "value_schema_version",
            "additional_properties",
            "fields",
        }
        or value.get("schema_version") != JOB_SETTINGS_DESCRIPTOR_SCHEMA
        or value.get("value_schema_version") != JOB_SETTINGS_SCHEMA
        or value.get("additional_properties") is not False
        or not isinstance(value.get("fields"), list)
        or not value["fields"]
    ):
        raise RuntimeError("The controller returned an invalid job-settings form")
    fields: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in value["fields"]:
        if not isinstance(raw, Mapping):
            raise RuntimeError("The controller returned an invalid settings field")
        key = raw.get("key")
        control = raw.get("control")
        if (
            not isinstance(key, str)
            or PUBLIC_SETTING_KEY_RE.fullmatch(key) is None
            or key in keys
            or control not in {"enum", "sensor_scene_multiselect"}
        ):
            raise RuntimeError("The controller returned an invalid settings field")
        keys.add(key)
        label = _public_controller_text(raw.get("label"))
        description = _public_controller_text(raw.get("description"))
        if not label or len(label) > 120 or not description or len(description) > 500:
            raise RuntimeError("The controller returned unsafe settings copy")
        if control == "enum":
            if set(raw) != {
                "key",
                "control",
                "label",
                "description",
                "required",
                "default",
                "options",
            } or raw.get("required") is not True:
                raise RuntimeError("The controller returned an invalid enum field")
            options = []
            for option in raw.get("options", []):
                if not isinstance(option, Mapping) or set(option) != {"value", "label"}:
                    raise RuntimeError("The controller returned an invalid enum option")
                option_value = option.get("value")
                option_label = _public_controller_text(option.get("label"))
                if (
                    not isinstance(option_value, str)
                    or PUBLIC_SETTING_KEY_RE.fullmatch(option_value) is None
                    or not option_label
                    or len(option_label) > 120
                ):
                    raise RuntimeError("The controller returned an invalid enum option")
                options.append({"value": option_value, "label": option_label})
            if (
                not options
                or len({item["value"] for item in options}) != len(options)
                or raw.get("default") not in {item["value"] for item in options}
            ):
                raise RuntimeError("The controller returned an invalid enum default")
            fields.append(
                {
                    "key": key,
                    "control": control,
                    "label": label,
                    "description": description,
                    "required": True,
                    "default": raw["default"],
                    "options": options,
                }
            )
        else:
            if set(raw) != {
                "key",
                "control",
                "label",
                "description",
                "required",
                "minimum_selected",
                "default",
            } or raw.get("required") is not True:
                raise RuntimeError(
                    "The controller returned an invalid sensor-selection field"
                )
            minimum = raw.get("minimum_selected")
            if (
                type(minimum) is not int
                or minimum < 1
                or raw.get("default") != "all_eligible"
            ):
                raise RuntimeError(
                    "The controller returned an invalid sensor-selection default"
                )
            fields.append(
                {
                    "key": key,
                    "control": control,
                    "label": label,
                    "description": description,
                    "required": True,
                    "minimum_selected": minimum,
                    "default": "all_eligible",
                }
            )
    return {
        "schema_version": JOB_SETTINGS_DESCRIPTOR_SCHEMA,
        "value_schema_version": JOB_SETTINGS_SCHEMA,
        "additional_properties": False,
        "fields": fields,
    }


def _public_estimator_settings(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {"schema_version", "execution_mode", "selected_scene_ids"}
        or value.get("schema_version") != JOB_SETTINGS_SCHEMA
        or value.get("execution_mode") not in EXECUTION_MODES
        or not isinstance(value.get("selected_scene_ids"), list)
        or not value["selected_scene_ids"]
        or any(type(item) is not int or item < 1 for item in value["selected_scene_ids"])
        or len(set(value["selected_scene_ids"])) != len(value["selected_scene_ids"])
    ):
        raise RuntimeError("The controller returned invalid estimator settings")
    return {
        "schema_version": JOB_SETTINGS_SCHEMA,
        "execution_mode": value["execution_mode"],
        "selected_scene_ids": sorted(value["selected_scene_ids"]),
    }


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
    job_settings = _public_job_settings_descriptor(value.get("job_settings"))
    return {
        **public,
        "job_settings": job_settings,
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
    if (
        isinstance(controller_payload, Mapping)
        and controller_payload.get("estimator_settings") is not None
    ):
        payload["estimator_settings"] = _public_estimator_settings(
            controller_payload["estimator_settings"]
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
            "selected_target_inventory_sha256",
            "selected_target_count",
            "processed_target_count",
            "selected_scope_excluded_target_count",
            "profile_excluded_target_count",
            "registration_count",
            "tracking_count",
            "reinitialization_count",
        },
    )
    if isinstance(value.get("result"), Mapping) and value["result"].get(
        "estimator_settings"
    ) is not None:
        result["estimator_settings"] = _public_estimator_settings(
            value["result"]["estimator_settings"]
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


def _bop_json_artifact(
    run_root: Path,
    value: Any,
    *,
    label: str,
) -> Any:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"{label} must remain below the BOP export")
    bop_root = run_root / "bop"
    path = bop_root / relative
    try:
        path.resolve(strict=False).relative_to(bop_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must remain below the BOP export") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is missing")
    loaded = json.loads(path.read_text())
    return loaded


def _sensor_sequences(
    run_root: Path,
    manifest: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build browser-safe exported-sensor descriptors without source paths."""

    exports_value = manifest.get("exports")
    exports = (
        [item for item in exports_value if isinstance(item, Mapping)]
        if isinstance(exports_value, list)
        else []
    )
    issues: list[str] = []
    try:
        frame_map = _bop_json_artifact(
            run_root,
            manifest.get("frame_map_path"),
            label="BOP frame map",
        )
        if (
            not isinstance(frame_map, Mapping)
            or frame_map.get("schema_version") != "posetestbot_bop_frame_map.v3"
            or not isinstance(frame_map.get("scenes"), Mapping)
        ):
            raise ValueError("BOP frame map must use posetestbot_bop_frame_map.v3")
        frame_scenes = frame_map["scenes"]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        frame_scenes = {}
        issues.append(str(exc))
    try:
        instance_map = _bop_json_artifact(
            run_root,
            manifest.get("instance_map_path"),
            label="BOP instance map",
        )
        if (
            not isinstance(instance_map, Mapping)
            or instance_map.get("schema_version")
            != "posetestbot_bop_instance_map.v1"
            or not isinstance(instance_map.get("instances"), list)
        ):
            raise ValueError(
                "BOP instance map must use posetestbot_bop_instance_map.v1"
            )
        instance_rows = instance_map["instances"]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        instance_rows = []
        issues.append(str(exc))
    try:
        targets = _bop_json_artifact(
            run_root,
            manifest.get("targets_path") or "test_targets_bop19.json",
            label="BOP19 target inventory",
        )
        if not isinstance(targets, list) or any(
            not isinstance(item, Mapping) for item in targets
        ):
            raise ValueError("BOP19 target inventory is invalid")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        targets = []
        issues.append(str(exc))

    configuration_by_folder: dict[str, Mapping[str, Any]] = {}
    try:
        run_config = json.loads((run_root / "run_config.json").read_text())
    except (OSError, json.JSONDecodeError):
        run_config = {}
    capture = run_config.get("capture") if isinstance(run_config, Mapping) else None
    configured_sensors = (
        capture.get("sensors") if isinstance(capture, Mapping) else None
    )
    if isinstance(configured_sensors, list):
        for sensor in configured_sensors:
            if not isinstance(sensor, Mapping):
                continue
            sensor_type = sensor.get("sensor_type")
            device_id = sensor.get("device_id")
            if not isinstance(sensor_type, str) or not isinstance(device_id, str):
                continue
            try:
                configuration_by_folder[sensor_folder_name(sensor_type, device_id)] = sensor
            except ValueError:
                continue

    identity_by_key: dict[tuple[int, int, int], Mapping[str, Any]] = {}
    for row in instance_rows:
        if not isinstance(row, Mapping):
            continue
        values = (row.get("scene_id"), row.get("im_id"), row.get("gt_id"))
        if any(type(item) is not int for item in values):
            continue
        key = (int(values[0]), int(values[1]), int(values[2]))
        if key in identity_by_key:
            issues.append("BOP instance map contains duplicate GT identity rows")
            continue
        identity_by_key[key] = row

    targets_by_scene: dict[int, list[Mapping[str, Any]]] = {}
    for target in targets:
        scene_id = target.get("scene_id")
        if type(scene_id) is int:
            targets_by_scene.setdefault(scene_id, []).append(target)

    sequences: list[dict[str, Any]] = []
    for export in sorted(exports, key=lambda item: int(item.get("scene_id", -1))):
        raw_scene_id = export.get("scene_id")
        if type(raw_scene_id) is not int or raw_scene_id < 1:
            issues.append("BOP export contains an invalid sensor scene ID")
            continue
        scene_id = int(raw_scene_id)
        scene_issues: list[str] = []
        frame_scene = frame_scenes.get(str(scene_id))
        if not isinstance(frame_scene, Mapping):
            frame_scene = {}
            scene_issues.append("Frame identity is missing for this sensor scene.")
            issues.append(f"BOP frame identity is missing for scene {scene_id}")
        sensor_name = frame_scene.get("sensor_name") or export.get("sensor_name")
        if (
            not isinstance(sensor_name, str)
            or PUBLIC_SENSOR_ID_RE.fullmatch(sensor_name) is None
        ):
            sensor_name = f"scene-{scene_id}"
            scene_issues.append("The exported sensor identifier is invalid.")
            issues.append(f"BOP sensor identifier is invalid for scene {scene_id}")
        frames = frame_scene.get("frames")
        frame_count = len(frames) if isinstance(frames, Mapping) else 0
        if frame_count < 1:
            scene_issues.append("This sensor scene has no frame identity inventory.")
            issues.append(f"BOP frame inventory is empty for scene {scene_id}")
        scene_targets = targets_by_scene.get(scene_id, [])
        target_count = sum(
            int(item.get("inst_count", 0))
            for item in scene_targets
            if type(item.get("inst_count")) is int
        )
        if target_count < 1:
            scene_issues.append("This sensor scene has no BOP19 target instances.")

        split = str(export.get("split") or "test")
        scene_folder = run_root / "bop" / split / f"{scene_id:06d}"
        try:
            scene_gt = json.loads((scene_folder / "scene_gt.json").read_text())
            scene_info = json.loads(
                (scene_folder / "scene_gt_info.json").read_text()
            )
            if not isinstance(scene_gt, Mapping) or not isinstance(
                scene_info, Mapping
            ):
                raise ValueError
            for target in scene_targets:
                im_id = target.get("im_id")
                obj_id = target.get("obj_id")
                inst_count = target.get("inst_count")
                if any(type(item) is not int for item in (im_id, obj_id, inst_count)):
                    raise ValueError
                gt_rows = scene_gt.get(str(im_id))
                info_rows = scene_info.get(str(im_id))
                if (
                    not isinstance(gt_rows, list)
                    or not isinstance(info_rows, list)
                    or len(gt_rows) != len(info_rows)
                ):
                    raise ValueError
                visible_gt_ids = []
                for gt_id, (gt_row, info_row) in enumerate(zip(gt_rows, info_rows)):
                    if (
                        isinstance(gt_row, Mapping)
                        and isinstance(info_row, Mapping)
                        and gt_row.get("obj_id") == obj_id
                    ):
                        visibility = float(info_row.get("visib_fract", 0.0))
                        if math.isfinite(visibility) and visibility >= 0.1:
                            visible_gt_ids.append(gt_id)
                if len(visible_gt_ids) != inst_count:
                    raise ValueError
                for gt_id in visible_gt_ids:
                    identity = identity_by_key.get((scene_id, im_id, gt_id))
                    try:
                        identity_obj_id = identity.get("obj_id")  # type: ignore[union-attr]
                        uuid.UUID(str(identity.get("instance_uuid")))  # type: ignore[union-attr]
                    except (AttributeError, ValueError):
                        raise ValueError from None
                    if identity_obj_id != obj_id:
                        raise ValueError
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            scene_issues.append(
                "Exact target-to-instance identity is incomplete for this sensor scene."
            )
            if target_count:
                issues.append(
                    f"BOP target-to-instance identity is incomplete for scene {scene_id}"
                )

        configured = configuration_by_folder.get(sensor_name)
        if configured is not None:
            sensor_type = str(configured.get("sensor_type") or "sensor")
            device_id = str(configured.get("device_id") or sensor_name)
            safe_sensor_id = f"{sensor_type}:{device_id}"
            if PUBLIC_SENSOR_ID_RE.fullmatch(safe_sensor_id) is None:
                safe_sensor_id = sensor_name
            operator_alias = configured.get("operator_alias")
            operator_alias = (
                str(operator_alias)[:120]
                if isinstance(operator_alias, str) and operator_alias.strip()
                else None
            )
            display_name = str(
                configured.get("display_name") or operator_alias or sensor_name
            )[:120]
            mounting_mode = str(configured.get("mounting_mode") or "unknown")
        else:
            safe_sensor_id = sensor_name
            operator_alias = None
            display_name = sensor_name
            mounting_mode = "unknown"
            scene_issues.append(
                "Run-owned sensor alias and mounting metadata are unavailable."
            )
        unique_scene_issues = list(dict.fromkeys(scene_issues))
        sequences.append(
            {
                "scene_id": scene_id,
                "sensor_id": safe_sensor_id,
                "operator_alias": operator_alias,
                "display_name": display_name,
                "mounting_mode": mounting_mode,
                "frame_count": frame_count,
                "target_count": target_count,
                "tracking_eligible": not unique_scene_issues,
                "tracking_blocker": (
                    " ".join(unique_scene_issues) if unique_scene_issues else None
                ),
            }
        )
    return sequences, list(dict.fromkeys(issues))


def _normalize_estimator_settings_submission(
    value: Any,
    *,
    descriptor: Any,
    sensor_sequences: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if value is None:
        return None
    public_descriptor = _public_job_settings_descriptor(descriptor)
    if public_descriptor is None or not isinstance(value, Mapping):
        raise ValueError("The selected estimator does not accept job settings")
    fields = public_descriptor["fields"]
    expected_keys = {"schema_version", *(str(item["key"]) for item in fields)}
    if set(value) != expected_keys or value.get("schema_version") != public_descriptor.get(
        "value_schema_version"
    ):
        raise ValueError("estimator_settings contains unsupported fields")
    normalized: dict[str, Any] = {
        "schema_version": public_descriptor["value_schema_version"]
    }
    eligible_scene_ids = {
        int(item["scene_id"])
        for item in sensor_sequences
        if item.get("tracking_eligible") is True
    }
    for field in fields:
        key = str(field["key"])
        field_value = value.get(key)
        if field["control"] == "enum":
            options = {str(item["value"]) for item in field["options"]}
            if not isinstance(field_value, str) or field_value not in options:
                raise ValueError(f"estimator_settings {key} is invalid")
            normalized[key] = field_value
        elif field["control"] == "sensor_scene_multiselect":
            if (
                not isinstance(field_value, list)
                or len(field_value) < int(field["minimum_selected"])
                or any(type(item) is not int or item < 1 for item in field_value)
                or len(set(field_value)) != len(field_value)
            ):
                raise ValueError("Select at least one unique eligible sensor sequence")
            unknown = sorted(set(field_value) - eligible_scene_ids)
            if unknown:
                raise ValueError(
                    "estimator_settings selects an ineligible sensor scene: "
                    + ", ".join(str(item) for item in unknown)
                )
            normalized[key] = sorted(field_value)
        else:  # pragma: no cover - descriptor sanitizer is closed above
            raise ValueError("Unsupported estimator settings control")
    return _public_estimator_settings(normalized)


def _build_pose_setup(
    run_root: Path, *, estimator_id: str | None = None
) -> dict[str, Any]:
    # Readiness stays request-bounded: the companion hashes every staged file
    # in its background worker before submission, while this identity binds the
    # existing BOP metadata and semantic content without synchronously reading
    # every depth image in a Flask request.
    dataset = inspect_dataset(run_root)
    manifest = _load_bop_manifest(run_root)
    sensor_sequences, sensor_identity_issues = _sensor_sequences(run_root, manifest)
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
    settings_descriptor = (
        selected.get("job_settings") if isinstance(selected, Mapping) else None
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
    has_sensor_settings = bool(
        isinstance(settings_descriptor, Mapping)
        and any(
            isinstance(field, Mapping)
            and field.get("control") == "sensor_scene_multiselect"
            for field in settings_descriptor.get("fields", [])
        )
    )
    if has_sensor_settings:
        if sensor_identity_issues:
            blockers.extend(
                {
                    "code": "tracking_identity_invalid",
                    "message": message,
                }
                for message in sensor_identity_issues
            )
        if not any(item["tracking_eligible"] for item in sensor_sequences):
            blockers.append(
                {
                    "code": "no_tracking_sensor",
                    "message": (
                        "No exported sensor sequence has complete frame, target, "
                        "instance, alias, and mounting evidence for tracking."
                    ),
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
        "schema_version": "cluster_estimation_setup.v3",
        "run_root": run_root.as_posix(),
        "dataset": public_dataset_descriptor(dataset),
        "annotation_mode": manifest.get("annotation_mode"),
        "estimator_id": selected_id,
        "estimator": selected,
        "estimators": estimators,
        "sensor_sequences": sensor_sequences,
        "oracle_mask_contract": (
            "bop_mask_visib_gt_instance.v1" if oracle_masks_required else None
        ),
        "score_contract": (
            "constant_1.0_no_detection_confidence" if oracle_masks_required else None
        ),
        "execution_contract": (
            "driver_advertised_immutable_job_settings.v1"
            if settings_descriptor is not None
            else (
                "independent_register_per_target_no_tracking.v1"
                if oracle_masks_required
                else None
            )
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
                        "BOP GT-visible instance masks initialize and recover tracks; "
                        "independent mode uses one for every target. This is pose "
                        "estimation, not detection or segmentation."
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
        if not set(value) <= {
            "run_root",
            "estimator_id",
            "profile_id",
            "operator",
            "estimator_settings",
            "dataset_sha256",
        }:
            raise ValueError("Pose-estimation submission contains unsupported fields")
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
        estimator_settings = _normalize_estimator_settings_submission(
            value.get("estimator_settings"),
            descriptor=(
                setup["estimator"].get("job_settings")
                if isinstance(setup.get("estimator"), Mapping)
                else None
            ),
            sensor_sequences=setup["sensor_sequences"],
        )
        dataset = inspect_dataset(run_root)
        submission = {
            "estimator_id": estimator_id,
            "run_root": run_root.as_posix(),
            "dataset_alias": dataset["dataset_alias"],
            "dataset_sha256": dataset["dataset_sha256"],
            "profile_id": profile_id,
            "operator": operator.strip(),
        }
        if estimator_settings is not None:
            submission["estimator_settings"] = estimator_settings
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
        provenance_driver_id = (
            provenance_estimator.get("driver_id")
            if isinstance(provenance_estimator, Mapping)
            else None
        )
        if provenance_driver_id == "foundationpose.v2":
            settings_values = (
                payload.get("estimator_settings"),
                result.get("estimator_settings"),
                provenance.get("estimator_settings"),
            )
            if any(not isinstance(item, Mapping) for item in settings_values):
                raise RuntimeError(
                    "FoundationPose v2 result lacks immutable estimator settings"
                )
            if not (
                dict(settings_values[0])
                == dict(settings_values[1])
                == dict(settings_values[2])
            ):
                raise RuntimeError(
                    "FoundationPose v2 estimator settings changed across job evidence"
                )
            execution_mode = settings_values[0].get("execution_mode")
            method_name = (
                "FoundationPose (continuous tracking, oracle initialization)"
                if execution_mode == "continuous_tracking"
                else "FoundationPose (independent registration, oracle GT masks)"
            )
            for field in (
                "selected_target_inventory_sha256",
                "selected_target_count",
                "processed_target_count",
                "selected_scope_excluded_target_count",
                "profile_excluded_target_count",
                "registration_count",
                "tracking_count",
                "reinitialization_count",
                "estimate_count",
                "failure_count",
            ):
                if result.get(field) != provenance.get(field):
                    raise RuntimeError(
                        "FoundationPose v2 result scope or tracking evidence changed"
                    )
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
