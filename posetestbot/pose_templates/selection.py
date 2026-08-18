"""Run-owned immutable template selection and per-instance GT resolution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import fcntl
import numpy as np

from posetestbot.io.artifacts import (
    BLENDERPROC_RENDER_PLAN,
    BOP_DIR,
    MASKS_DIR,
    OBJECT_INSTANCES,
    POSE_TEMPLATE_SELECTION,
    PROCESSED_DIR,
)
from posetestbot.io.atomic import atomic_write_json
from posetestbot.pose_templates.catalog import utc_now_iso
from posetestbot.pose_templates.library import (
    default_template_library_root,
    template_library_lock,
    validate_template_bundle,
)
from posetestbot.pose_templates.transforms import (
    matrix_from_record,
    transform_record,
    validate_rigid_matrix,
)


SELECTION_SCHEMA_VERSION = "pose_template_selection.v1"
OBJECT_INSTANCES_SCHEMA_VERSION = "object_instances.v1"
SELECTION_DIRECTORY = "pose_template_selection"
SELECTION_LOCK = ".pose_template_selection.lock"
SELECTION_TRANSACTION = ".pose_template_selection.transaction.json"
SELECTION_TRANSACTION_SCHEMA_VERSION = "pose_template_selection_transaction.v1"
_STAGING_IDENTIFIER = r"[0-9a-f]{32}"
_SNAPSHOT_STAGING_PATTERN = re.compile(
    rf"^\.{re.escape(SELECTION_DIRECTORY)}\.{_STAGING_IDENTIFIER}\.tmp$"
)
_SELECTION_STAGING_PATTERN = re.compile(
    rf"^\.{re.escape(POSE_TEMPLATE_SELECTION)}\.{_STAGING_IDENTIFIER}\.tmp$"
)


_RUN_LOCKS_GUARD = threading.Lock()
_RUN_LOCKS: dict[str, threading.RLock] = {}


class PoseTemplateSelectionConflict(RuntimeError):
    def __init__(self, message: str, *, blockers: list[str]):
        super().__init__(message)
        self.blockers = blockers


def _run_lock(root: Path) -> threading.RLock:
    key = root.resolve().as_posix()
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RUN_LOCKS[key] = lock
        return lock


@contextmanager
def _selection_lock(root: Path):
    """Serialize readers and writers for one run, including across processes."""

    # Import lazily to preserve the existing pipeline/pose-template boundary.
    from posetestbot.pipeline.run_config import run_config_lock

    if not root.is_dir():
        raise FileNotFoundError(f"Run root does not exist: {root}")
    with _run_lock(root):
        lock_path = root / SELECTION_LOCK
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "a+b", closefd=False) as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    with run_config_lock(root):
                        _recover_selection_transaction(root)
                        _cleanup_orphaned_selection_staging(root)
                        yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _assert_no_symlink_ancestors(root: Path, path: Path, *, label: str) -> None:
    """Reject lexical run paths whose existing parent chain contains a symlink."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Selection transaction {label} path escapes the run") from exc
    cursor = root
    for part in relative.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(
                f"Selection transaction {label} path must not contain symlink ancestors"
            )
        if cursor.exists() and not cursor.is_dir():
            raise ValueError(
                f"Selection transaction {label} path has a non-directory ancestor"
            )


def _cleanup_orphaned_selection_staging(root: Path) -> None:
    """Remove exact selection staging names left before journal publication."""

    locations = (
        (root / PROCESSED_DIR, _SNAPSHOT_STAGING_PATTERN),
        (root, _SELECTION_STAGING_PATTERN),
    )
    for parent, pattern in locations:
        probe = parent / ".selection-staging-probe"
        _assert_no_symlink_ancestors(root, probe, label="staging cleanup")
        if not parent.exists():
            continue
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(
                "Selection transaction staging parent must be a regular directory"
            )
        for candidate in parent.iterdir():
            if pattern.fullmatch(candidate.name):
                _remove_path(candidate)


def _transaction_relative(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Selection transaction path escapes the run") from exc
    _assert_no_symlink_ancestors(root, path, label="managed")
    return relative


def _transaction_entry_path(
    root: Path,
    value: object,
    *,
    label: str,
    target: Path | None = None,
    suffix: str | None = None,
) -> Path:
    relative = Path(str(value))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Selection transaction {label} path is invalid")
    path = root / relative
    _assert_no_symlink_ancestors(root, path, label=label)
    if target is not None:
        if path.parent != target.parent:
            raise ValueError(f"Selection transaction {label} path is invalid")
        prefix = f".{target.name}."
        if not path.name.startswith(prefix) or (
            suffix is not None and not path.name.endswith(suffix)
        ):
            raise ValueError(f"Selection transaction {label} path is invalid")
    return path


def _recover_selection_transaction(root: Path) -> None:
    """Recover a durable multi-artifact selection promotion after process loss."""

    journal_path = root / SELECTION_TRANSACTION
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    if journal_path.is_symlink() or not journal_path.is_file():
        raise ValueError("Selection transaction journal must be a regular file")
    with open(journal_path, "r", encoding="utf-8") as handle:
        journal = json.load(handle)
    if (
        not isinstance(journal, Mapping)
        or journal.get("schema_version") != SELECTION_TRANSACTION_SCHEMA_VERSION
        or journal.get("phase") not in {"prepared", "committed"}
        or not isinstance(journal.get("entries"), list)
    ):
        raise ValueError("Selection transaction journal is invalid")
    allowed_targets = {
        (root / PROCESSED_DIR / SELECTION_DIRECTORY).as_posix(),
        (root / POSE_TEMPLATE_SELECTION).as_posix(),
        (root / "run_config.json").as_posix(),
    }
    entries: list[tuple[Path, Path, Path, bool]] = []
    seen_targets: set[str] = set()
    for raw in journal["entries"]:
        if not isinstance(raw, Mapping) or type(raw.get("had_target")) is not bool:
            raise ValueError("Selection transaction entry is invalid")
        target = _transaction_entry_path(root, raw.get("target"), label="target")
        if target.as_posix() not in allowed_targets:
            raise ValueError("Selection transaction target is not managed")
        if target.as_posix() in seen_targets:
            raise ValueError("Selection transaction target appears more than once")
        seen_targets.add(target.as_posix())
        staged = _transaction_entry_path(
            root,
            raw.get("staged"),
            label="staged",
            target=target,
            suffix=".tmp",
        )
        backup = _transaction_entry_path(
            root,
            raw.get("backup"),
            label="backup",
            target=target,
            suffix=".bak",
        )
        entries.append((staged, target, backup, raw["had_target"]))
    required_targets = {
        (root / PROCESSED_DIR / SELECTION_DIRECTORY).as_posix(),
        (root / POSE_TEMPLATE_SELECTION).as_posix(),
    }
    if not required_targets.issubset(seen_targets) or len(entries) not in {2, 3}:
        raise ValueError("Selection transaction entry set is incomplete")

    if journal["phase"] == "prepared":
        for staged, target, backup, had_target in reversed(entries):
            if backup.exists() or backup.is_symlink():
                if target.exists() or target.is_symlink():
                    _remove_path(target)
                os.replace(backup, target)
            elif had_target and not (target.exists() or target.is_symlink()):
                raise ValueError(
                    "Selection transaction cannot recover a missing prior artifact"
                )
            elif not had_target and (target.exists() or target.is_symlink()):
                _remove_path(target)
            if staged.exists() or staged.is_symlink():
                _remove_path(staged)
    else:
        for staged, target, backup, _had_target in entries:
            if not (target.exists() or target.is_symlink()):
                raise ValueError(
                    "Committed selection transaction is missing a live artifact"
                )
            if staged.exists() or staged.is_symlink():
                _remove_path(staged)
            if backup.exists() or backup.is_symlink():
                _remove_path(backup)
    journal_path.unlink()


def _promote_paths(
    promotions: list[tuple[Path, Path]],
    *,
    root: Path,
    validate_live: Callable[[], Any],
) -> Any:
    """Promote staged files/directories and retain backups until validation."""

    backups = [
        target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
        for _source, target in promotions
    ]
    moved_existing: list[int] = []
    promoted: list[int] = []
    journal_path = root / SELECTION_TRANSACTION
    journal = {
        "schema_version": SELECTION_TRANSACTION_SCHEMA_VERSION,
        "phase": "prepared",
        "entries": [
            {
                "staged": _transaction_relative(root, source),
                "target": _transaction_relative(root, target),
                "backup": _transaction_relative(root, backup),
                "had_target": bool(target.exists() or target.is_symlink()),
            }
            for (source, target), backup in zip(promotions, backups, strict=True)
        ],
    }
    atomic_write_json(journal_path, journal)
    try:
        for index, ((_source, target), backup) in enumerate(
            zip(promotions, backups, strict=True)
        ):
            if target.exists() or target.is_symlink():
                os.replace(target, backup)
                moved_existing.append(index)
        for index, (source, target) in enumerate(promotions):
            os.replace(source, target)
            promoted.append(index)
        result = validate_live()
    except Exception:
        for index in reversed(promoted):
            source, target = promotions[index]
            if (target.exists() or target.is_symlink()) and not source.exists():
                os.replace(target, source)
        for index in reversed(moved_existing):
            _source, target = promotions[index]
            backup = backups[index]
            if backup.exists() or backup.is_symlink():
                os.replace(backup, target)
        journal_path.unlink(missing_ok=True)
        raise
    else:
        journal["phase"] = "committed"
        atomic_write_json(journal_path, journal)
        # Backup cleanup is deliberately after live validation. If cleanup itself
        # encounters an OS error, the committed artifacts remain authoritative and
        # the hidden backup is retained for manual recovery.
        for backup in backups:
            if backup.exists() or backup.is_symlink():
                try:
                    _remove_path(backup)
                except OSError:
                    pass
        if not any(backup.exists() or backup.is_symlink() for backup in backups):
            journal_path.unlink(missing_ok=True)
        return result


def _replacement_blockers_unlocked(root: Path) -> list[str]:
    candidates = [
        root / OBJECT_INSTANCES,
        root / BLENDERPROC_RENDER_PLAN,
        root / "blenderproc_output",
        root / MASKS_DIR,
        root / BOP_DIR,
        root / PROCESSED_DIR / "blenderproc",
    ]
    for tree_name in ("synchronized", "rectified"):
        tree = root / PROCESSED_DIR / tree_name
        if not tree.is_dir():
            continue
        for sensor in tree.iterdir():
            if not sensor.is_dir():
                continue
            candidates.extend((sensor / "blenderproc", sensor / MASKS_DIR))
    return sorted(
        {path.relative_to(root).as_posix() for path in candidates if path.exists()}
    )


def replacement_blockers(run_root: str | Path) -> list[str]:
    root = Path(run_root).resolve()
    with _selection_lock(root):
        return _replacement_blockers_unlocked(root)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_run_config(value: Mapping[str, Any]) -> None:
    # Import lazily to preserve the existing pipeline/pose-template module boundary.
    from posetestbot.pipeline.run_config import validate_run_config

    validate_run_config(value)


def select_pose_template(
    run_root: str | Path,
    template_uuid: str,
    *,
    placement: Mapping[str, Any],
    confirmed: bool,
    operator: str,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Run root does not exist: {root}")
    if type(confirmed) is not bool:
        raise ValueError("confirmed must be a boolean")
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("Selection operator provenance must be a non-empty string")
    operator_name = operator.strip()
    placement_matrix = matrix_from_record(placement, label="template placement")
    library = Path(library_root or default_template_library_root())
    with _selection_lock(root):
        source = validate_template_bundle(
            library / str(uuid.UUID(template_uuid)), library_root=library
        )
        if source["archive"]["state"] != "active":
            raise ValueError("Archived pose templates cannot be selected for a new run")
        current_path = root / POSE_TEMPLATE_SELECTION
        if current_path.exists() or current_path.is_symlink():
            current = _load_pose_template_selection_unlocked(root)
            same = (
                current["template_uuid"] == source["template_uuid"]
                and np.allclose(
                    np.asarray(current["template_base_from_pose_template"]["matrix"]),
                    placement_matrix,
                    atol=1e-10,
                    rtol=0,
                )
                and current["placement_confirmed"] == confirmed
                and current["operator"] == operator_name
            )
            if same:
                return current
            blockers = _replacement_blockers_unlocked(root)
            if blockers:
                raise PoseTemplateSelectionConflict(
                    "Pose-template selection cannot be replaced after dependent artifacts "
                    "exist.",
                    blockers=blockers,
                )

        selection_root = root / PROCESSED_DIR / SELECTION_DIRECTORY
        selection_root.parent.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid.uuid4().hex
        staging_snapshot = selection_root.parent / (
            f".{SELECTION_DIRECTORY}.{transaction_id}.tmp"
        )
        staging_selection = root / f".{POSE_TEMPLATE_SELECTION}.{transaction_id}.tmp"
        config_path = root / "run_config.json"
        staging_config: Path | None = None
        staged_config: dict[str, Any] | None = None
        try:
            with template_library_lock(library):
                source = validate_template_bundle(
                    library / str(uuid.UUID(template_uuid)), library_root=library
                )
                if source["archive"]["state"] != "active":
                    raise ValueError(
                        "Archived pose templates cannot be selected for a new run"
                    )
                shutil.copytree(source["bundle_path"], staging_snapshot)
            snapshot_bundle = validate_template_bundle(
                staging_snapshot,
                library_root=staging_snapshot.parent,
                allow_staging=True,
            )
            resolved = []
            for item in snapshot_bundle["instances"]:
                nominal = validate_rigid_matrix(
                    item["pose_template_from_object"]["matrix"],
                    label="pose_template_from_object",
                )
                final = placement_matrix @ nominal
                resolved.append(
                    {
                        "instance_uuid": item["instance_uuid"],
                        "catalog_uuid": item["catalog"]["catalog_uuid"],
                        "obj_id": int(item["catalog"]["obj_id"]),
                        "name": item["catalog"]["name"],
                        "pose_template_from_object": item["pose_template_from_object"],
                        "template_base_from_object": transform_record(
                            final,
                            parent="template_base",
                            child=f"object:{item['instance_uuid']}",
                        ),
                        "assets": item["assets"],
                    }
                )
            selection = {
                "schema_version": SELECTION_SCHEMA_VERSION,
                "template_uuid": snapshot_bundle["template_uuid"],
                "bundle_sha256": snapshot_bundle["bundle_sha256"],
                "configuration_sha256": snapshot_bundle["hashes"]["configuration"],
                "bundle_snapshot": (
                    Path(PROCESSED_DIR) / SELECTION_DIRECTORY
                ).as_posix(),
                "bundle_snapshot_sha256": _tree_hash(staging_snapshot),
                "template_base_from_pose_template": transform_record(
                    placement_matrix,
                    parent="template_base",
                    child="pose_template",
                ),
                "placement_confirmed": confirmed,
                "instances": resolved,
                "selected_at": utc_now_iso(),
                "operator": operator_name,
                "source": snapshot_bundle["source"],
                "print_compensation": snapshot_bundle["print_compensation"],
                "catalog_snapshot": snapshot_bundle["catalog_snapshot"],
            }
            atomic_write_json(staging_selection, selection)
            with open(staging_selection, "r", encoding="utf-8") as handle:
                staged_selection = json.load(handle)
            _validate_selection_value(
                root,
                staged_selection,
                snapshot_override=staging_snapshot,
            )

            promotions = [
                (staging_snapshot, selection_root),
                (staging_selection, current_path),
            ]
            if config_path.is_file():
                from posetestbot.pipeline.run_config import (
                    SCHEMA_VERSION as RUN_CONFIG_SCHEMA_VERSION,
                    capture_synchronization_from_mapping,
                    load_run_config,
                )

                config = load_run_config(config_path)
                capture_config = config.get("capture")
                if not isinstance(capture_config, dict):
                    raise ValueError("Run config capture must be an object")
                capture_config["synchronization"] = (
                    capture_synchronization_from_mapping(
                        capture_config.get("synchronization")
                    ).to_dict()
                )
                config["schema_version"] = RUN_CONFIG_SCHEMA_VERSION
                config["dataset_mode"] = "pose_template"
                config["pose_template"] = {
                    "template_uuid": snapshot_bundle["template_uuid"],
                    "selection_artifact": POSE_TEMPLATE_SELECTION,
                    "bundle_sha256": snapshot_bundle["bundle_sha256"],
                    "placement_confirmed": confirmed,
                }
                _validate_run_config(config)
                staging_config = root / f".run_config.json.{transaction_id}.tmp"
                atomic_write_json(staging_config, config)
                with open(staging_config, "r", encoding="utf-8") as handle:
                    staged_config = json.load(handle)
                _validate_run_config(staged_config)
                promotions.append((staging_config, config_path))

            def validate_live() -> dict[str, Any]:
                selected = _load_pose_template_selection_unlocked(root)
                if staged_config is not None:
                    with open(config_path, "r", encoding="utf-8") as handle:
                        live_config = json.load(handle)
                    _validate_run_config(live_config)
                    if live_config != staged_config:
                        raise ValueError(
                            "Promoted run config does not match the staged selection"
                        )
                return selected

            return _promote_paths(promotions, root=root, validate_live=validate_live)
        finally:
            for path in (staging_snapshot, staging_selection, staging_config):
                if path is not None and (path.exists() or path.is_symlink()):
                    _remove_path(path)


def _json_snapshot_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON snapshots without Python's bool/int equality coercion."""

    try:
        return json.dumps(
            actual,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) == json.dumps(
            expected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _validate_transform_snapshot(
    value: Any,
    expected_matrix: np.ndarray,
    *,
    parent: str,
    child: str,
    label: str,
) -> np.ndarray:
    """Validate both a transform matrix and its duplicated frame/numeric fields."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a transform object")
    if "matrix" not in value:
        raise ValueError(f"{label}.matrix is required")
    actual = matrix_from_record(value, label=label)
    if not np.allclose(actual, expected_matrix, atol=1e-8, rtol=0):
        raise ValueError(f"{label} matrix mismatch")
    if (
        value.get("semantics") != "entity_to_parent"
        or value.get("parent_frame") != parent
        or value.get("child_frame") != child
    ):
        raise ValueError(f"{label} frame semantics mismatch")
    try:
        duplicated = matrix_from_record(
            {
                "translation_mm": value.get("translation_mm"),
                "rotation_quaternion_wxyz": value.get("rotation_quaternion_wxyz"),
            },
            label=f"{label} duplicated values",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} duplicated values are invalid") from exc
    if not np.allclose(duplicated, expected_matrix, atol=1e-8, rtol=0):
        raise ValueError(f"{label} duplicated values mismatch")
    return actual


def _validate_selection_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Selection selected_at provenance must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Selection selected_at provenance must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Selection selected_at provenance must include a timezone")


def _validate_selection_value(
    root: Path,
    value: Any,
    *,
    snapshot_override: Path | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != SELECTION_SCHEMA_VERSION
    ):
        raise ValueError(f"Selection schema must be {SELECTION_SCHEMA_VERSION}")
    if type(value.get("placement_confirmed")) is not bool:
        raise ValueError("Selection placement_confirmed must be a boolean")
    operator = value.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("Selection operator provenance must be a non-empty string")
    _validate_selection_timestamp(value.get("selected_at"))
    snapshot_relative = Path(str(value.get("bundle_snapshot", "")))
    if snapshot_relative.is_absolute() or ".." in snapshot_relative.parts:
        raise ValueError("Selection bundle snapshot must be run-relative")
    expected_snapshot = Path(PROCESSED_DIR) / SELECTION_DIRECTORY
    if snapshot_relative != expected_snapshot:
        raise ValueError(
            f"Selection bundle snapshot must be {expected_snapshot.as_posix()}"
        )
    snapshot = snapshot_override or root / snapshot_relative
    try:
        snapshot.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Selection bundle snapshot escapes the run root") from exc
    bundle = validate_template_bundle(
        snapshot, library_root=snapshot.parent, allow_staging=True
    )
    if bundle["template_uuid"] != value.get("template_uuid"):
        raise ValueError("Selection snapshot template UUID mismatch")
    if bundle["bundle_sha256"] != value.get("bundle_sha256"):
        raise ValueError("Selection snapshot bundle hash mismatch")
    if bundle["hashes"]["configuration"] != value.get("configuration_sha256"):
        raise ValueError("Selection snapshot configuration hash mismatch")
    for field in ("source", "print_compensation", "catalog_snapshot"):
        if not _json_snapshot_equal(value.get(field), bundle.get(field)):
            raise ValueError(f"Selection snapshot {field} mismatch")
    # archive_state is intentionally mutable library metadata and does not affect
    # a selected run's immutable content provenance.
    if _tree_hash(snapshot) != value.get("bundle_snapshot_sha256"):
        raise ValueError("Selection bundle snapshot hash mismatch")
    placement_value = value.get("template_base_from_pose_template")
    if not isinstance(placement_value, Mapping):
        raise ValueError("Selection template placement must be a transform object")
    placement = matrix_from_record(
        placement_value, label="Selection template_base_from_pose_template"
    )
    _validate_transform_snapshot(
        placement_value,
        placement,
        parent="template_base",
        child="pose_template",
        label="Selection template_base_from_pose_template",
    )
    instances = value.get("instances")
    if not isinstance(instances, list) or len(instances) != len(bundle["instances"]):
        raise ValueError("Selection resolved instance count mismatch")
    verified_assets = bundle.get("files", {}).get("assets", {})
    if not isinstance(verified_assets, Mapping):
        raise ValueError("Selection snapshot asset table is invalid")
    for selected, source in zip(instances, bundle["instances"], strict=True):
        if not isinstance(selected, Mapping) or not isinstance(source, Mapping):
            raise ValueError("Selection resolved instance must be an object")
        instance_uuid = source.get("instance_uuid")
        try:
            valid_instance_uuid = (
                isinstance(instance_uuid, str)
                and str(uuid.UUID(instance_uuid)) == instance_uuid
            )
        except ValueError:
            valid_instance_uuid = False
        if not valid_instance_uuid or selected.get("instance_uuid") != instance_uuid:
            raise ValueError("Selection resolved instance UUID mismatch")
        source_catalog = source.get("catalog")
        if not isinstance(source_catalog, Mapping):
            raise ValueError("Selection snapshot instance catalog is invalid")
        catalog_uuid = source_catalog.get("catalog_uuid")
        try:
            valid_catalog_uuid = (
                isinstance(catalog_uuid, str)
                and str(uuid.UUID(catalog_uuid)) == catalog_uuid
            )
        except ValueError:
            valid_catalog_uuid = False
        if not valid_catalog_uuid or selected.get("catalog_uuid") != catalog_uuid:
            raise ValueError("Selection resolved instance catalog UUID mismatch")
        obj_id = source_catalog.get("obj_id")
        if type(obj_id) is not int or obj_id <= 0:
            raise ValueError("Selection snapshot instance obj_id is invalid")
        if type(selected.get("obj_id")) is not int or selected.get("obj_id") != obj_id:
            raise ValueError("Selection resolved instance obj_id mismatch")
        name = source_catalog.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Selection snapshot instance name is invalid")
        if selected.get("name") != name:
            raise ValueError("Selection resolved instance name mismatch")
        expected_assets = verified_assets.get(instance_uuid)
        if not _json_snapshot_equal(source.get("assets"), expected_assets):
            raise ValueError("Selection snapshot instance assets are not verified")
        if not _json_snapshot_equal(selected.get("assets"), expected_assets):
            raise ValueError("Selection resolved instance assets mismatch")
        source_nominal_value = source.get("pose_template_from_object")
        if not isinstance(source_nominal_value, Mapping):
            raise ValueError("Selection snapshot nominal transform is invalid")
        nominal = matrix_from_record(
            source_nominal_value, label="Snapshot pose_template_from_object"
        )
        _validate_transform_snapshot(
            source_nominal_value,
            nominal,
            parent="pose_template",
            child=f"object:{instance_uuid}",
            label="Selection snapshot pose_template_from_object",
        )
        _validate_transform_snapshot(
            selected.get("pose_template_from_object"),
            nominal,
            parent="pose_template",
            child=f"object:{instance_uuid}",
            label="Selection resolved pose_template_from_object",
        )
        expected = placement @ nominal
        _validate_transform_snapshot(
            selected.get("template_base_from_object"),
            expected,
            parent="template_base",
            child=f"object:{instance_uuid}",
            label="Selection resolved template_base_from_object",
        )
    return dict(value)


def _load_pose_template_selection_unlocked(root: Path) -> dict[str, Any]:
    path = root / POSE_TEMPLATE_SELECTION
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Pose-template selection does not exist: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    return _validate_selection_value(root, value)


def load_pose_template_selection(run_root: str | Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with _selection_lock(root):
        return _load_pose_template_selection_unlocked(root)


def prepare_object_instances(run_root: str | Path) -> dict[str, Any]:
    root = Path(run_root).resolve()
    with _selection_lock(root):
        selection = _load_pose_template_selection_unlocked(root)
        if not selection.get("placement_confirmed"):
            raise ValueError("Pose-template placement must be explicitly confirmed")
        snapshot = root / selection["bundle_snapshot"]
        objects = []
        for item in selection["instances"]:
            files = item["assets"]
            canonical = snapshot / files["canonical_ply"]["path"]
            texture = (
                snapshot / files["texture"]["path"] if "texture" in files else None
            )
            objects.append(
                {
                    "instance_uuid": item["instance_uuid"],
                    "catalog_uuid": item["catalog_uuid"],
                    "obj_id": item["obj_id"],
                    "name": item["name"],
                    "canonical_ply": canonical.relative_to(root).as_posix(),
                    "canonical_ply_sha256": files["canonical_ply"]["sha256"],
                    "texture": texture.relative_to(root).as_posix()
                    if texture
                    else None,
                    "texture_sha256": files.get("texture", {}).get("sha256"),
                    "pose_template_from_object": item["pose_template_from_object"],
                    "template_base_from_object": item["template_base_from_object"],
                }
            )
        artifact = {
            "schema_version": OBJECT_INSTANCES_SCHEMA_VERSION,
            "created_at": utc_now_iso(),
            "template_uuid": selection["template_uuid"],
            "bundle_sha256": selection["bundle_sha256"],
            "selection_sha256": hashlib.sha256(
                (root / POSE_TEMPLATE_SELECTION).read_bytes()
            ).hexdigest(),
            "instances": objects,
            "provenance": {
                "source": selection["source"],
                "operator": selection["operator"],
                "selected_at": selection["selected_at"],
            },
        }
        atomic_write_json(root / OBJECT_INSTANCES, artifact)
        return artifact
