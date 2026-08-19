"""Run-folder inventory and queued storage-management endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from posetestbot.jobs.runner import ResourceBusyError, TERMINAL_STATUSES
from posetestbot.run_folders import (
    INVENTORY_FILENAME,
    INVENTORY_SCHEMA_VERSION,
    discover_run_folders,
    load_run_folder_inventory,
    preflight_move_run_folder,
    resolve_destination_root,
    resolve_direct_run_folder,
    run_folder_transaction_fingerprint,
    run_root_identity_snapshot,
    validate_expected_identity,
)
from posetestbot.web.paths import APP_ROOT
from posetestbot.web.routes.ui import run_storage_status
from posetestbot.web.runtime import get_job_runner
from posetestbot.web.security import (
    DEFAULT_RUN_ROOT,
    web_run_roots,
)


run_folders_bp = Blueprint("run_folders", __name__)
INVENTORY_STALE_SECONDS = 300


def _json_object() -> dict[str, Any]:
    """Read the original JSON bytes, before the global path normalizer mutates them."""

    try:
        value = json.loads(request.get_data(cache=True))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("A JSON object is required") from exc
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _raw_run_path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_root must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        if path.parts and path.parts[0] == DEFAULT_RUN_ROOT.name:
            path = APP_ROOT / path
        else:
            path = DEFAULT_RUN_ROOT / path
    if path.is_symlink():
        raise ValueError("Run folder must not be a symbolic link")
    return path


def _source_from_payload(value: Mapping[str, Any]) -> Path:
    resolved = _raw_run_path(value.get("run_root"))
    return resolve_direct_run_folder(resolved, allowed_roots=web_run_roots())


def _expected_identity(value: Mapping[str, Any]) -> Mapping[str, Any]:
    expected = value.get("expected_identity")
    if not isinstance(expected, Mapping):
        raise ValueError("expected_identity must be an object")
    return expected


def _expected_destination_root_identity(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
    expected = value.get("expected_destination_root_identity")
    if not isinstance(expected, Mapping):
        raise ValueError("expected_destination_root_identity must be an object")
    return expected


def _inventory_cache_path() -> Path:
    return Path(get_job_runner().job_root) / INVENTORY_FILENAME


def _all_jobs():
    return get_job_runner().list(include_services=True)


def _active_inventory_job():
    candidates = [
        job
        for job in _all_jobs()
        if job.status not in TERMINAL_STATUSES
        and bool((job.parameters or {}).get("run_folder_inventory"))
    ]
    return max(candidates, key=lambda item: item.created_at) if candidates else None


def _active_operation_job():
    candidates = [
        job
        for job in _all_jobs()
        if job.status not in TERMINAL_STATUSES
        and (job.parameters or {}).get("run_folder_operation") in {"move", "delete"}
    ]
    return max(candidates, key=lambda item: item.created_at) if candidates else None


def _assert_no_active_run_jobs(run_root: Path) -> None:
    active = []
    for job in _all_jobs():
        if (
            job.status in TERMINAL_STATUSES
            or job.scope_kind != "run"
            or not job.run_root
        ):
            continue
        try:
            same_run = Path(job.run_root).resolve() == run_root.resolve()
        except OSError:
            same_run = job.run_root == run_root.as_posix()
        if same_run:
            active.append(job.id)
    if active:
        raise ResourceBusyError(
            "Run folder has active background work: " + ", ".join(sorted(active))
        )


def _inventory_is_stale(value: Mapping[str, Any] | None) -> bool:
    if value is None:
        return True
    generated_at = value.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=UTC)
        if (datetime.now(UTC) - generated).total_seconds() > INVENTORY_STALE_SECONDS:
            return True
    except (TypeError, ValueError):
        return True
    configured = [root.as_posix() for root in web_run_roots()]
    if value.get("run_roots") != configured:
        return True
    if value.get("root_identities") != run_root_identity_snapshot(web_run_roots()):
        return True
    maintenance = value.get("maintenance")
    if not isinstance(maintenance, Mapping):
        return True
    fingerprint = maintenance.get("journal_fingerprint")
    if not isinstance(fingerprint, str):
        return True
    try:
        if fingerprint != run_folder_transaction_fingerprint(web_run_roots()):
            return True
    except OSError:
        return True
    runs = value.get("runs")
    if not isinstance(runs, list):
        return True
    cached = set()
    for item in runs:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            return True
        identity = item.get("identity")
        if not isinstance(identity, Mapping):
            return True
        device = identity.get("device")
        inode = identity.get("inode")
        if (
            isinstance(device, bool)
            or not isinstance(device, int)
            or isinstance(inode, bool)
            or not isinstance(inode, int)
            or device < 0
            or inode <= 0
        ):
            return True
        record = (item["path"], device, inode)
        if record in cached:
            return True
        cached.add(record)
    current = set()
    for _storage_root, run in discover_run_folders(web_run_roots()):
        try:
            metadata = run.lstat()
        except OSError:
            return True
        current.add((run.as_posix(), int(metadata.st_dev), int(metadata.st_ino)))
    return cached != current


def _require_current_inventory_selection(
    source: Path,
    *,
    expected_identity: Mapping[str, Any],
    destination_root: Path | None = None,
    expected_destination_root_identity: Mapping[str, Any] | None = None,
) -> None:
    cached = load_run_folder_inventory(_inventory_cache_path())
    if _active_inventory_job() is not None or _inventory_is_stale(cached):
        raise RuntimeError(
            "Run-folder inventory is not current; refresh inventory before "
            "changing run storage"
        )
    if cached is None:
        raise RuntimeError(
            "Run-folder inventory is missing; refresh inventory before "
            "changing run storage"
        )
    selected_identity = {
        "device": expected_identity.get("device"),
        "inode": expected_identity.get("inode"),
    }
    matching_runs = [
        item
        for item in cached["runs"]
        if isinstance(item, Mapping) and item.get("path") == source.as_posix()
    ]
    if len(matching_runs) != 1 or matching_runs[0].get("identity") != selected_identity:
        raise RuntimeError(
            "Run selection no longer matches the current inventory; refresh "
            "inventory before changing run storage"
        )
    if destination_root is None:
        return
    selected_destination_identity = {
        "device": (
            expected_destination_root_identity.get("device")
            if expected_destination_root_identity is not None
            else None
        ),
        "inode": (
            expected_destination_root_identity.get("inode")
            if expected_destination_root_identity is not None
            else None
        ),
    }
    root_identities = cached.get("root_identities")
    if (
        not isinstance(root_identities, Mapping)
        or root_identities.get(destination_root.as_posix())
        != selected_destination_identity
    ):
        raise RuntimeError(
            "Destination root no longer matches the current inventory; refresh "
            "inventory before moving the run"
        )


def _job_response(job, **values: Any):
    return (
        jsonify(
            {
                "job_id": job.id,
                "status": job.status,
                "job": job.to_dict(),
                **values,
            }
        ),
        202,
    )


def _error(exc: Exception):
    if isinstance(exc, ResourceBusyError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, FileExistsError | RuntimeError):
        return jsonify({"output": str(exc)}), 409
    if isinstance(exc, FileNotFoundError):
        return jsonify({"output": str(exc)}), 404
    return jsonify({"output": str(exc)}), 400


@run_folders_bp.get("/ui/run-folders")
def run_folder_inventory():
    cached = load_run_folder_inventory(_inventory_cache_path())
    refresh = _active_inventory_job()
    operation = _active_operation_job()
    stale = _inventory_is_stale(cached)
    if refresh is not None:
        state = "refreshing"
    elif cached is None:
        state = "missing"
    elif stale:
        state = "stale"
    else:
        state = "ready"
    roots = []
    cached_root_identities = (
        cached.get("root_identities")
        if cached and isinstance(cached.get("root_identities"), Mapping)
        else {}
    )
    current_root_identities = run_root_identity_snapshot(web_run_roots())
    for root in web_run_roots():
        exists = root.is_dir()
        cached_identity = cached_root_identities.get(root.as_posix())
        current_identity = current_root_identities.get(root.as_posix())
        roots.append(
            {
                "path": root.as_posix(),
                "exists": exists,
                "identity": (
                    cached_identity if cached_identity == current_identity else None
                ),
                "storage": run_storage_status(root),
            }
        )
    return jsonify(
        {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "generated_at": cached.get("generated_at") if cached else None,
            "inventory_state": state,
            "stale": stale,
            "roots": roots,
            "runs": cached.get("runs", []) if cached else [],
            "maintenance": (
                cached.get("maintenance")
                if cached
                else {
                    "schema_version": "run_folder_maintenance.v1",
                    "journal_fingerprint": (
                        run_folder_transaction_fingerprint(web_run_roots())
                    ),
                    "recovered_count": 0,
                    "transactions": [],
                    "unresolved_count": 0,
                    "unresolved": [],
                }
            ),
            "refresh_job": refresh.to_dict() if refresh is not None else None,
            "operation_job": operation.to_dict() if operation is not None else None,
        }
    )


@run_folders_bp.post("/ui/run-folders/refresh")
def refresh_run_folder_inventory():
    try:
        active = _active_inventory_job()
        if active is not None:
            return _job_response(active)
        job_runner = get_job_runner()
        job = job_runner.submit(
            name="run_folder_inventory",
            command=[
                "uv",
                "run",
                "python",
                "scripts/manage_run_folders.py",
                "inventory",
                "--cache",
                _inventory_cache_path().as_posix(),
            ],
            cwd=APP_ROOT,
            resources=["disk_io", "run_folder_storage"],
            scope_kind="global",
            parameters={
                "run_folder_inventory": True,
                "cancelable": False,
            },
        )
        return _job_response(job)
    except Exception as exc:
        return _error(exc)


@run_folders_bp.post("/ui/run-folders/move")
def move_run_folder():
    try:
        value = _json_object()
        source = _source_from_payload(value)
        expected = _expected_identity(value)
        expected_destination = _expected_destination_root_identity(value)
        validate_expected_identity(source, expected)
        destination_root = resolve_destination_root(
            value.get("destination_root"), allowed_roots=web_run_roots()
        )
        validate_expected_identity(destination_root, expected_destination)
        _require_current_inventory_selection(
            source,
            expected_identity=expected,
            destination_root=destination_root,
            expected_destination_root_identity=expected_destination,
        )
        preflight = preflight_move_run_folder(
            source,
            destination_root,
            expected_identity=expected,
            expected_destination_root_identity=expected_destination,
            allowed_roots=web_run_roots(),
        )
        _assert_no_active_run_jobs(source)
        destination = preflight["destination"]
        job_runner = get_job_runner()
        job = job_runner.submit(
            name="run_folder_move",
            command=[
                "uv",
                "run",
                "python",
                "scripts/manage_run_folders.py",
                "move",
                source.as_posix(),
                destination_root.as_posix(),
                "--expected-device",
                str(expected["device"]),
                "--expected-inode",
                str(expected["inode"]),
                "--expected-destination-device",
                str(expected_destination["device"]),
                "--expected-destination-inode",
                str(expected_destination["inode"]),
                "--cache",
                _inventory_cache_path().as_posix(),
            ],
            cwd=APP_ROOT,
            resources=["disk_io", "run_folder_storage"],
            scope_kind="run",
            run_root=source,
            parameters={
                "run_folder_operation": "move",
                "cancelable": False,
                "source_run_root": source.as_posix(),
                "destination_run_root": destination.as_posix(),
            },
        )
        return _job_response(
            job,
            source_run_root=source.as_posix(),
            destination_run_root=destination.as_posix(),
        )
    except Exception as exc:
        return _error(exc)


@run_folders_bp.delete("/ui/run-folders")
def delete_run_folder():
    try:
        value = _json_object()
        if value.get("confirm") is not True:
            raise ValueError("confirm must be literal true to delete a run folder")
        source = _source_from_payload(value)
        expected = _expected_identity(value)
        validate_expected_identity(source, expected)
        _require_current_inventory_selection(
            source,
            expected_identity=expected,
        )
        _assert_no_active_run_jobs(source)
        job_runner = get_job_runner()
        job = job_runner.submit(
            name="run_folder_delete",
            command=[
                "uv",
                "run",
                "python",
                "scripts/manage_run_folders.py",
                "delete",
                source.as_posix(),
                "--expected-device",
                str(expected["device"]),
                "--expected-inode",
                str(expected["inode"]),
                "--cache",
                _inventory_cache_path().as_posix(),
                "--confirm-delete",
            ],
            cwd=APP_ROOT,
            resources=["disk_io", "run_folder_storage"],
            scope_kind="run",
            run_root=source,
            parameters={
                "run_folder_operation": "delete",
                "cancelable": False,
                "source_run_root": source.as_posix(),
            },
        )
        return _job_response(job, source_run_root=source.as_posix())
    except Exception as exc:
        return _error(exc)
