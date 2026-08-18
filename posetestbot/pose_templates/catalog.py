"""Global transactional JSON-backed workpiece catalog."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import fcntl

from PIL import Image

from posetestbot.io.atomic import atomic_write_bytes, atomic_write_json
from posetestbot.pose_templates.adapter import load_posetemplatecreator_backend


SCHEMA_VERSION = "object_catalog.v1"
CATALOG_MANIFEST = "object_catalog.json"
CATALOG_DIRECTORY = "object_catalog"
STAGING_DIRECTORY = "object_catalog_staging"
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 2_000
MAX_ALIAS_LENGTH = 120
MAX_CLASSIFICATION_ITEMS = 64
MAX_CLASSIFICATION_LENGTH = 80
MAX_ATTRIBUTES = 64
MAX_ATTRIBUTE_KEY_LENGTH = 80
MAX_ATTRIBUTE_VALUE_LENGTH = 500
UNIT_CORRECTION_FACTORS = {
    "meter_to_millimeter": 1_000.0,
    "millimeter_to_meter": 0.001,
}
_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


class CatalogObjectInUseError(ValueError):
    """Raised when a hard delete could invalidate pose-template bundles."""

    def __init__(self, blockers: list[dict[str, Any]]) -> None:
        super().__init__(
            "Workpiece is referenced by or cannot be checked against pose-template bundles"
        )
        self.blockers = blockers


class CatalogGeometryRevisionConflict(RuntimeError):
    """Raised when a queued geometry mutation no longer targets current bytes."""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def default_working_data_root() -> Path:
    configured = os.environ.get("POSETESTBOT_WORKING_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    app_root = os.environ.get("POSETESTBOT_APP_ROOT")
    root = (
        Path(app_root).expanduser().resolve()
        if app_root
        else Path(__file__).resolve().parents[2]
    )
    return root / "working_data"


def default_catalog_root() -> Path:
    return default_working_data_root() / CATALOG_DIRECTORY


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_catalog() -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "next_obj_id": 1,
        "objects": [],
        "tombstones": [],
    }


@contextmanager
def _mutation_lock(root: Path):
    """Serialize catalog commits across Flask and queued worker processes."""

    with _LOCK:
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / ".catalog.lock"
        held = getattr(_LOCK_STATE, "catalog_locks", None)
        if held is None:
            held = {}
            _LOCK_STATE.catalog_locks = held
        depth = int(held.get(lock_path, 0))
        if depth:
            held[lock_path] = depth + 1
            try:
                yield
            finally:
                held[lock_path] -= 1
            return
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "a+b", closefd=False) as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                held[lock_path] = 1
                try:
                    yield
                finally:
                    held.pop(lock_path, None)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _required_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters")
    return normalized


def _optional_text(value: Any, *, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return normalized


def _classification_list(value: Any, *, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list | tuple):
        raise ValueError(f"{label} must be a JSON array")
    if len(value) > MAX_CLASSIFICATION_ITEMS:
        raise ValueError(
            f"{label} may contain at most {MAX_CLASSIFICATION_ITEMS} values"
        )
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _required_text(
            raw, label=f"{label} value", maximum=MAX_CLASSIFICATION_LENGTH
        )
        identity = item.casefold()
        if identity not in seen:
            result.append(item)
            seen.add(identity)
    return result


def _attributes(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("attributes must be a JSON object")
    if len(value) > MAX_ATTRIBUTES:
        raise ValueError(f"attributes may contain at most {MAX_ATTRIBUTES} values")
    result: dict[str, str] = {}
    seen: set[str] = set()
    for raw_key, raw_value in value.items():
        key = _required_text(
            raw_key, label="attribute name", maximum=MAX_ATTRIBUTE_KEY_LENGTH
        )
        identity = key.casefold()
        if identity in seen:
            raise ValueError(f"attribute names must be unique: {key}")
        if raw_value is None:
            normalized_value = ""
        elif isinstance(raw_value, str | int | float | bool):
            normalized_value = str(raw_value).strip()
        else:
            raise ValueError(
                "attribute values must be strings, numbers, booleans, or null"
            )
        if len(normalized_value) > MAX_ATTRIBUTE_VALUE_LENGTH:
            raise ValueError(
                f"attribute value for {key!r} must not exceed "
                f"{MAX_ATTRIBUTE_VALUE_LENGTH} characters"
            )
        result[key] = normalized_value
        seen.add(identity)
    return dict(sorted(result.items(), key=lambda item: item[0].casefold()))


def normalize_catalog_metadata(
    value: Mapping[str, Any], *, require_name: bool = True
) -> dict[str, Any]:
    """Validate the mutable, portable labels attached to one workpiece."""

    if not isinstance(value, Mapping):
        raise ValueError("Workpiece metadata must be a JSON object")
    result: dict[str, Any] = {}
    if require_name or "name" in value:
        result["name"] = _required_text(
            value.get("name"), label="Object name", maximum=MAX_NAME_LENGTH
        )
    if require_name or "alias" in value:
        result["alias"] = _optional_text(
            value.get("alias"), label="Object alias", maximum=MAX_ALIAS_LENGTH
        )
    if require_name or "description" in value:
        result["description"] = _optional_text(
            value.get("description"),
            label="Object description",
            maximum=MAX_DESCRIPTION_LENGTH,
        )
    if require_name or "tags" in value:
        result["tags"] = _classification_list(value.get("tags"), label="tags")
    if require_name or "groups" in value:
        result["groups"] = _classification_list(value.get("groups"), label="groups")
    if require_name or "attributes" in value:
        result["attributes"] = _attributes(value.get("attributes"))
    return result


def _validate_uuid(value: Any, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _contained(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Catalog asset escapes managed root: {path}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"Catalog assets must not use symlinks: {cursor}")
    resolved_root = root.resolve()
    resolved = path.resolve(strict=must_exist)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Catalog asset escapes managed root: {path}") from exc
    return resolved


def _asset_record(path: Path, root: Path, *, media_type: str) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    return {
        "path": relative,
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _validate_asset_record(record: Mapping[str, Any], root: Path) -> Path:
    relative = Path(str(record.get("path", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Catalog asset path must be catalog-relative")
    path = root / relative
    _contained(path, root)
    if not path.is_file():
        raise FileNotFoundError(f"Catalog asset is missing: {path}")
    if path.stat().st_size != int(record.get("size_bytes", -1)):
        raise ValueError(f"Catalog asset size mismatch: {relative}")
    if _sha256(path) != record.get("sha256"):
        raise ValueError(f"Catalog asset hash mismatch: {relative}")
    return path


def normalize_unit_correction_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the intentionally narrow unit-correction command contract."""

    if not isinstance(value, Mapping):
        raise ValueError("Unit correction request must be a JSON object")
    allowed = {
        "conversion",
        "confirm",
        "operator",
        "expected_geometry_revision",
        "expected_canonical_sha256",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("Unknown unit correction fields: " + ", ".join(unknown))
    conversion = value.get("conversion")
    if conversion not in UNIT_CORRECTION_FACTORS:
        raise ValueError(
            "conversion must be meter_to_millimeter or millimeter_to_meter"
        )
    if value.get("confirm") is not True:
        raise ValueError("confirm must be true to correct workpiece units")
    operator = _required_text(
        value.get("operator"), label="Unit correction operator", maximum=120
    )
    raw_revision = value.get("expected_geometry_revision")
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
        raise ValueError("expected_geometry_revision must be a positive integer")
    if raw_revision <= 0:
        raise ValueError("expected_geometry_revision must be a positive integer")
    expected_hash = value.get("expected_canonical_sha256")
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise ValueError("expected_canonical_sha256 must be a lowercase SHA-256")
    return {
        "conversion": str(conversion),
        "factor": UNIT_CORRECTION_FACTORS[str(conversion)],
        "confirm": True,
        "operator": operator,
        "expected_geometry_revision": raw_revision,
        "expected_canonical_sha256": expected_hash,
    }


def _normalize_geometry_state(
    record: Mapping[str, Any], root: Path, *, verify_assets: bool
) -> dict[str, Any]:
    """Validate the explicit retained geometry-revision history."""

    raw_revisions = record.get("geometry_revisions")
    if not isinstance(raw_revisions, list) or not raw_revisions:
        raise ValueError("geometry_revisions must be a non-empty array")
    if len(raw_revisions) > 10_000:
        raise ValueError("geometry_revisions exceeds 10,000 entries")
    current_revision = int(record.get("geometry_revision", 0))
    current_scale = float(record.get("source_to_mm_scale", 0.0))
    revisions = []
    seen: set[int] = set()
    for raw in raw_revisions:
        if not isinstance(raw, Mapping):
            raise ValueError("Geometry revision entries must be objects")
        revision = int(raw.get("revision", 0))
        if revision <= 0 or revision in seen:
            raise ValueError("Geometry revision numbers must be unique and positive")
        seen.add(revision)
        canonical = raw.get("canonical_ply")
        if not isinstance(canonical, Mapping):
            raise ValueError("Geometry revision canonical_ply must be an asset record")
        digest = raw.get("canonical_ply_sha256")
        if digest != canonical.get("sha256"):
            raise ValueError("Geometry revision canonical hash mismatch")
        extraction = raw.get("extraction")
        if not isinstance(extraction, Mapping):
            raise ValueError("Geometry revision extraction must be an object")
        revision_scale = float(raw.get("source_to_mm_scale", 0.0))
        if not math.isfinite(revision_scale) or revision_scale <= 0:
            raise ValueError("Geometry revision source_to_mm_scale must be positive")
        operation = raw.get("operation")
        if not isinstance(operation, Mapping):
            raise ValueError("Geometry revision operation must be an object")
        normalized = {
            **raw,
            "revision": revision,
            "canonical_ply": dict(canonical),
            "extraction": dict(extraction),
            "source_to_mm_scale": revision_scale,
            "operation": dict(operation),
        }
        normalized.pop("orientation_analysis", None)
        if verify_assets:
            _validate_asset_record(canonical, root)
        revisions.append(normalized)
    revisions.sort(key=lambda item: int(item["revision"]))
    if current_revision not in seen:
        raise ValueError("geometry_revision must select a retained revision")
    if not math.isfinite(current_scale) or current_scale <= 0:
        raise ValueError("source_to_mm_scale must be positive")

    active = next(
        item for item in revisions if int(item["revision"]) == current_revision
    )
    assets = record.get("assets")
    if not isinstance(assets, Mapping):
        raise ValueError("Catalog object assets must be an object")
    if record.get("canonical_ply_sha256") != active["canonical_ply_sha256"]:
        raise ValueError("Active geometry revision canonical hash mismatch")
    if dict(assets.get("canonical_ply") or {}) != active["canonical_ply"]:
        raise ValueError("Active geometry revision asset pointer mismatch")
    if dict(record.get("extraction") or {}) != active["extraction"]:
        raise ValueError("Active geometry revision extraction mismatch")
    if not math.isclose(
        current_scale,
        float(active["source_to_mm_scale"]),
        rel_tol=0.0,
        abs_tol=0.0,
    ):
        raise ValueError("Active geometry revision scale mismatch")
    # Orientation analysis is a reproducible, implementation-version-bound
    # cache beside the canonical PLY, not an immutable catalogue asset. Early
    # development manifests briefly recorded it in ``assets`` and geometry
    # revisions. Strip those pointers while loading so regenerating a cache can
    # never invalidate the durable catalogue manifest.
    durable_assets = {
        key: value for key, value in assets.items() if key != "orientation_analysis"
    }
    return {
        **record,
        "assets": durable_assets,
        "geometry_revision": current_revision,
        "source_to_mm_scale": current_scale,
        "geometry_revisions": revisions,
    }


def load_catalog(
    catalog_root: str | Path | None = None, *, verify_assets: bool = True
) -> dict[str, Any]:
    root = Path(catalog_root or default_catalog_root())
    manifest_path = root / CATALOG_MANIFEST
    if not manifest_path.exists():
        return {**_empty_catalog(), "catalog_root": root.resolve().as_posix()}
    _contained(manifest_path, root)
    with open(manifest_path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Object catalog schema must be {SCHEMA_VERSION}")
    records = value.get("objects")
    if not isinstance(records, list):
        raise ValueError("Object catalog objects must be a list")
    uuids: set[str] = set()
    ids: set[int] = set()
    normalized_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("Object catalog records must be objects")
        opaque_id = _validate_uuid(record.get("catalog_uuid"), label="catalog_uuid")
        obj_id = int(record.get("obj_id", 0))
        if obj_id <= 0 or opaque_id in uuids or obj_id in ids:
            raise ValueError("Catalog UUIDs and positive obj_id values must be unique")
        uuids.add(opaque_id)
        ids.add(obj_id)
        if record.get("state") not in {"active", "archived"}:
            raise ValueError("Catalog object state must be active or archived")
        metadata = normalize_catalog_metadata(record)
        assets = record.get("assets")
        if (
            not isinstance(assets, Mapping)
            or "source" not in assets
            or "canonical_ply" not in assets
        ):
            raise ValueError(
                "Catalog object must retain source and canonical PLY assets"
            )
        if verify_assets:
            for kind, asset in assets.items():
                if kind == "orientation_analysis":
                    continue
                if not isinstance(asset, Mapping):
                    raise ValueError("Catalog asset record must be an object")
                _validate_asset_record(asset, root)
        normalized_records.append(
            _normalize_geometry_state(
                {**record, "archived_at": record.get("archived_at"), **metadata},
                root,
                verify_assets=verify_assets,
            )
        )
    tombstones = value.get("tombstones", [])
    if not isinstance(tombstones, list):
        raise ValueError("Object catalog tombstones must be a list")
    tombstone_uuids: set[str] = set()
    tombstone_ids: set[int] = set()
    normalized_tombstones: list[dict[str, Any]] = []
    for tombstone in tombstones:
        if not isinstance(tombstone, Mapping):
            raise ValueError("Object catalog tombstones must be objects")
        opaque_id = _validate_uuid(tombstone.get("catalog_uuid"), label="catalog_uuid")
        obj_id = int(tombstone.get("obj_id", 0))
        if obj_id <= 0 or opaque_id in uuids or opaque_id in tombstone_uuids:
            raise ValueError("Catalog tombstone UUIDs must be unique")
        if obj_id in ids or obj_id in tombstone_ids:
            raise ValueError("Catalog tombstone obj_id values must be unique")
        tombstone_uuids.add(opaque_id)
        tombstone_ids.add(obj_id)
        normalized_tombstones.append(dict(tombstone))
    next_obj_id = int(value.get("next_obj_id", 1))
    if next_obj_id <= max(ids | tombstone_ids, default=0):
        raise ValueError(
            "Object catalog next_obj_id must be greater than every assigned ID"
        )
    result = dict(value)
    result["objects"] = normalized_records
    result["tombstones"] = normalized_tombstones
    result["catalog_root"] = root.resolve().as_posix()
    return result


def _commit_catalog(value: dict[str, Any], root: Path) -> None:
    value = {key: item for key, item in value.items() if key != "catalog_root"}
    value["version"] = int(value.get("version", 0)) + 1
    value["updated_at"] = utc_now_iso()
    root.mkdir(parents=True, exist_ok=True)
    revisions = root / "revisions"
    revisions.mkdir(exist_ok=True)
    # A revision without a current-manifest pointer is harmless and auditable;
    # the reverse ordering can leave the live manifest pointing at assets whose
    # importing transaction is about to roll back.
    atomic_write_json(revisions / f"{value['version']:08d}.json", value)
    atomic_write_json(root / CATALOG_MANIFEST, value)


def _validate_texture(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Texture upload must be a regular staged file")
    if path.suffix.lower() != ".png":
        raise ValueError("Texture must be a single PNG file")
    data = path.read_bytes()
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("Texture exceeds the 50 MiB file limit")
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError("Texture content is not PNG")
            image.verify()
    except (OSError, SyntaxError) as exc:
        raise ValueError("Texture content is not a valid PNG") from exc
    return data


def import_catalog_object(
    *,
    name: str,
    cad_path: str | Path,
    description: str | None = None,
    alias: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    groups: list[str] | tuple[str, ...] | None = None,
    attributes: Mapping[str, Any] | None = None,
    texture_path: str | Path | None = None,
    catalog_root: str | Path | None = None,
    catalog_uuid: str | None = None,
    obj_id: int | None = None,
    import_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect staged assets and atomically add one immutable asset snapshot."""
    metadata = normalize_catalog_metadata(
        {
            "name": name,
            "alias": alias,
            "description": description,
            "tags": list(tags or []),
            "groups": list(groups or []),
            "attributes": dict(attributes or {}),
        }
    )
    source_path = Path(cad_path)
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("CAD upload must be a regular staged file")
    source_data = source_path.read_bytes()
    backend = load_posetemplatecreator_backend()
    safe_name = backend.safe_filename(source_path.name)
    source_format = backend.file_format(safe_name)
    canonical, extraction = backend.canonical_ply(safe_name, source_data)
    texture_data = (
        _validate_texture(Path(texture_path)) if texture_path is not None else None
    )
    if (
        len(source_data) + (len(texture_data) if texture_data else 0)
        > 100 * 1024 * 1024
    ):
        raise ValueError("Upload batch exceeds the 100 MiB limit")

    root = Path(catalog_root or default_catalog_root())
    staging_root = root.parent / STAGING_DIRECTORY
    staging_root.mkdir(parents=True, exist_ok=True)
    opaque_id = _validate_uuid(catalog_uuid or uuid.uuid4(), label="catalog_uuid")
    stage = staging_root / f"{opaque_id}.{uuid.uuid4().hex}.tmp"
    destination = root / "objects" / opaque_id
    stage.mkdir(parents=False, exist_ok=False)
    moved = False
    try:
        source_asset = Path("source") / safe_name
        canonical_asset = Path("derived") / "000001" / "canonical.ply"
        texture_asset = Path("texture") / "texture.png"
        atomic_write_bytes(stage / source_asset, source_data)
        atomic_write_bytes(stage / canonical_asset, canonical)
        if texture_data is not None:
            atomic_write_bytes(stage / texture_asset, texture_data)
        with _mutation_lock(root):
            catalog = load_catalog(root)
            if any(
                item["catalog_uuid"] == opaque_id
                for item in [*catalog["objects"], *catalog.get("tombstones", [])]
            ):
                raise ValueError(f"Catalog UUID already exists: {opaque_id}")
            assigned_id = (
                int(obj_id) if obj_id is not None else int(catalog["next_obj_id"])
            )
            if assigned_id <= 0 or any(
                int(item["obj_id"]) == assigned_id
                for item in [*catalog["objects"], *catalog.get("tombstones", [])]
            ):
                raise ValueError(f"BOP obj_id is not available: {assigned_id}")
            if destination.exists():
                raise ValueError(f"Catalog asset directory already exists: {opaque_id}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
            moved = True
            now = utc_now_iso()
            assets = {
                "source": _asset_record(
                    destination / source_asset,
                    root,
                    media_type="application/octet-stream",
                ),
                "canonical_ply": _asset_record(
                    destination / canonical_asset,
                    root,
                    media_type="application/octet-stream",
                ),
            }
            if texture_data is not None:
                assets["texture"] = _asset_record(
                    destination / texture_asset, root, media_type="image/png"
                )
            record = {
                "catalog_uuid": opaque_id,
                "obj_id": assigned_id,
                **metadata,
                "source_filename": safe_name,
                "source_format": source_format,
                "source_sha256": _sha256_bytes(source_data),
                "canonical_ply_sha256": _sha256_bytes(canonical),
                "texture_sha256": _sha256_bytes(texture_data) if texture_data else None,
                "assets": assets,
                "extraction": extraction,
                "geometry_revision": 1,
                "source_to_mm_scale": 1.0,
                "geometry_revisions": [
                    {
                        "revision": 1,
                        "created_at": now,
                        "canonical_ply_sha256": _sha256_bytes(canonical),
                        "canonical_ply": dict(assets["canonical_ply"]),
                        "extraction": dict(extraction),
                        "source_to_mm_scale": 1.0,
                        "operation": {
                            "kind": "import",
                            "factor": 1.0,
                            "source_to_mm_scale": 1.0,
                        },
                    }
                ],
                "created_at": now,
                "updated_at": now,
                "archived_at": None,
                "state": "active",
                "import_provenance": dict(import_provenance or {}),
            }
            catalog["objects"].append(record)
            catalog["objects"].sort(key=lambda item: int(item["obj_id"]))
            catalog["next_obj_id"] = max(int(catalog["next_obj_id"]), assigned_id + 1)
            _commit_catalog(catalog, root)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if moved:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return get_catalog_object(opaque_id, catalog_root=root)


def get_catalog_object(
    catalog_uuid: str,
    *,
    catalog_root: str | Path | None = None,
    verify_assets: bool = True,
) -> dict[str, Any]:
    opaque_id = _validate_uuid(catalog_uuid, label="catalog_uuid")
    catalog = load_catalog(catalog_root, verify_assets=verify_assets)
    for item in catalog["objects"]:
        if item["catalog_uuid"] == opaque_id:
            return {**item, "catalog_root": catalog["catalog_root"]}
    raise KeyError(f"Unknown catalog object: {opaque_id}")


def resolve_catalog_asset(
    catalog_uuid: str,
    kind: str,
    *,
    catalog_root: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Resolve and hash-verify one requested asset without scanning the catalog."""

    item = get_catalog_object(
        catalog_uuid, catalog_root=catalog_root, verify_assets=False
    )
    if kind not in item["assets"]:
        raise KeyError("Unknown catalog asset")
    record = item["assets"][kind]
    root = Path(item["catalog_root"])
    path = _validate_asset_record(record, root)
    return item, dict(record), path


def _assert_unit_correction_target(
    item: Mapping[str, Any], request_value: Mapping[str, Any]
) -> None:
    if item.get("state") != "archived":
        raise ValueError("A workpiece must be archived before correcting its units")
    if int(item.get("geometry_revision", 0)) != int(
        request_value["expected_geometry_revision"]
    ):
        raise CatalogGeometryRevisionConflict(
            "Workpiece geometry revision changed before unit correction"
        )
    if item.get("canonical_ply_sha256") != request_value["expected_canonical_sha256"]:
        raise CatalogGeometryRevisionConflict(
            "Workpiece canonical geometry changed before unit correction"
        )


def preflight_catalog_unit_correction(
    catalog_uuid: str,
    value: Mapping[str, Any],
    *,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate operator intent and the current immutable geometry pointer."""

    normalized = normalize_unit_correction_request(value)
    item = get_catalog_object(
        catalog_uuid, catalog_root=catalog_root, verify_assets=False
    )
    _assert_unit_correction_target(item, normalized)
    bounds = item.get("extraction", {}).get("bounds_mm")
    resulting_bounds = None
    if (
        isinstance(bounds, list)
        and len(bounds) == 2
        and all(isinstance(row, list) and len(row) == 3 for row in bounds)
    ):
        resulting_bounds = [
            [float(coordinate) * normalized["factor"] for coordinate in row]
            for row in bounds
        ]
    return {
        **normalized,
        "catalog_uuid": item["catalog_uuid"],
        "obj_id": int(item["obj_id"]),
        "current_bounds_mm": bounds,
        "resulting_bounds_mm": resulting_bounds,
    }


def _scaled_canonical_ply(
    backend: Any, source_filename: str, source_data: bytes, source_to_mm_scale: float
) -> tuple[bytes, dict[str, Any]]:
    """Regenerate canonical geometry from retained source at an explicit scale."""

    mesh = backend.load_mesh(source_filename, source_data).copy()
    mesh.apply_scale(source_to_mm_scale)
    exported = mesh.export(file_type="ply", encoding="binary_little_endian")
    intermediate = (
        exported.encode("utf-8") if isinstance(exported, str) else bytes(exported)
    )
    return backend.canonical_ply("canonical.ply", intermediate)


def correct_catalog_object_units(
    catalog_uuid: str,
    *,
    conversion: str,
    confirm: bool,
    operator: str,
    expected_geometry_revision: int,
    expected_canonical_sha256: str,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    """Publish a scaled canonical revision without changing source or identity."""

    root = Path(catalog_root or default_catalog_root())
    request_value = normalize_unit_correction_request(
        {
            "conversion": conversion,
            "confirm": confirm,
            "operator": operator,
            "expected_geometry_revision": expected_geometry_revision,
            "expected_canonical_sha256": expected_canonical_sha256,
        }
    )
    item = get_catalog_object(catalog_uuid, catalog_root=root, verify_assets=False)
    _assert_unit_correction_target(item, request_value)
    current_record = item["assets"]["canonical_ply"]
    _validate_asset_record(current_record, root)
    source_path = _validate_asset_record(item["assets"]["source"], root)
    source_data = source_path.read_bytes()
    resulting_scale = float(item["source_to_mm_scale"]) * float(request_value["factor"])
    if not math.isfinite(resulting_scale) or resulting_scale <= 0:
        raise ValueError("Resulting source-to-millimetre scale is invalid")
    backend = load_posetemplatecreator_backend()
    canonical, extraction = _scaled_canonical_ply(
        backend, item["source_filename"], source_data, resulting_scale
    )
    canonical_sha256 = _sha256_bytes(canonical)
    revisions = item["geometry_revisions"]
    next_revision = max(int(entry["revision"]) for entry in revisions) + 1
    revision_name = (
        f"{next_revision:06d}-{canonical_sha256[:12]}-{uuid.uuid4().hex[:8]}"
    )
    derived_root = root / "objects" / item["catalog_uuid"] / "derived"
    _contained(derived_root, root, must_exist=False)
    derived_root.mkdir(parents=True, exist_ok=True)
    stage = derived_root / f".{revision_name}.{uuid.uuid4().hex}.tmp"
    destination = derived_root / revision_name
    stage.mkdir(exist_ok=False)
    moved = False
    orientation_analysis_cache: dict[str, Any] = {
        "status": "not_generated",
        "reason": "The PoseTemplateCreator backend does not provide orientation analysis.",
    }
    try:
        staged_canonical = stage / "canonical.ply"
        atomic_write_bytes(staged_canonical, canonical)
        if callable(getattr(backend, "orientation_artifacts", None)):
            try:
                from posetestbot.pose_templates.orientations import (
                    ORIENTATION_CACHE_FILENAME,
                    ORIENTATION_THUMBNAIL_FILENAME,
                    build_orientation_analysis,
                    write_orientation_thumbnail,
                )

                analysis = build_orientation_analysis(
                    item["catalog_uuid"],
                    staged_canonical,
                    canonical_sha256,
                    catalog_root=root,
                    backend=backend,
                )
                atomic_write_json(stage / ORIENTATION_CACHE_FILENAME, analysis)
                write_orientation_thumbnail(
                    stage / ORIENTATION_THUMBNAIL_FILENAME, analysis
                )
            except Exception as exc:
                # Stable-orientation analysis is a reproducible cache, not part
                # of the canonical geometry transaction. A corrected mesh must
                # remain publishable when that optional analysis cannot be
                # produced; operators can queue analysis again afterwards.
                reason = str(exc).strip() or exc.__class__.__name__
                orientation_analysis_cache = {
                    "status": "unavailable",
                    "reason": (f"Stable-orientation cache generation failed: {reason}")[
                        :2_000
                    ],
                }
            else:
                orientation_analysis_cache = {
                    "status": "ready",
                    "reason": None,
                }

        with _mutation_lock(root):
            catalog = load_catalog(root, verify_assets=False)
            current = next(
                (
                    record
                    for record in catalog["objects"]
                    if record["catalog_uuid"] == item["catalog_uuid"]
                ),
                None,
            )
            if current is None:
                raise CatalogGeometryRevisionConflict(
                    "Workpiece was removed before unit correction"
                )
            _assert_unit_correction_target(current, request_value)
            _validate_asset_record(current["assets"]["canonical_ply"], root)
            _validate_asset_record(current["assets"]["source"], root)
            if destination.exists():
                raise CatalogGeometryRevisionConflict(
                    f"Geometry revision destination already exists: {next_revision}"
                )
            os.replace(stage, destination)
            moved = True
            canonical_asset = _asset_record(
                destination / "canonical.ply",
                root,
                media_type="application/vnd.ply",
            )
            corrected_at = utc_now_iso()
            revision = {
                "revision": next_revision,
                "created_at": corrected_at,
                "canonical_ply_sha256": canonical_sha256,
                "canonical_ply": canonical_asset,
                "extraction": dict(extraction),
                "source_to_mm_scale": resulting_scale,
                "operation": {
                    "kind": "unit_correction",
                    "conversion": request_value["conversion"],
                    "factor": float(request_value["factor"]),
                    "source_to_mm_scale": resulting_scale,
                    "operator": request_value["operator"],
                    "previous_revision": int(current["geometry_revision"]),
                    "previous_canonical_ply_sha256": current["canonical_ply_sha256"],
                },
            }
            current["geometry_revisions"].append(revision)
            current["geometry_revision"] = next_revision
            current["source_to_mm_scale"] = resulting_scale
            current["canonical_ply_sha256"] = canonical_sha256
            current["extraction"] = dict(extraction)
            current["assets"] = dict(current["assets"])
            current["assets"]["canonical_ply"] = canonical_asset
            current["assets"].pop("orientation_analysis", None)
            current["updated_at"] = corrected_at
            _commit_catalog(catalog, root)
    except Exception:
        if not moved:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    result = get_catalog_object(item["catalog_uuid"], catalog_root=root)
    result["orientation_analysis_cache"] = orientation_analysis_cache
    return result


def update_catalog_object_metadata(
    catalog_uuid: str,
    metadata: Mapping[str, Any],
    *,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    """Update labels without mutating immutable geometry or stable BOP identity."""

    updates = normalize_catalog_metadata(metadata, require_name=False)
    if not updates:
        raise ValueError("At least one editable metadata field is required")
    root = Path(catalog_root or default_catalog_root())
    opaque_id = _validate_uuid(catalog_uuid, label="catalog_uuid")
    with _mutation_lock(root):
        catalog = load_catalog(root, verify_assets=False)
        for item in catalog["objects"]:
            if item["catalog_uuid"] != opaque_id:
                continue
            if all(item.get(key) == value for key, value in updates.items()):
                return {**item, "catalog_root": catalog["catalog_root"]}
            item.update(updates)
            item["updated_at"] = utc_now_iso()
            _commit_catalog(catalog, root)
            return get_catalog_object(opaque_id, catalog_root=root, verify_assets=False)
    raise KeyError(f"Unknown catalog object: {opaque_id}")


def _template_delete_blockers(
    catalog_uuid: str, *, library_root: str | Path | None = None
) -> list[dict[str, Any]]:
    """Return references and integrity uncertainties that make deletion unsafe."""

    from posetestbot.pose_templates.library import (  # Avoid the library/catalog cycle.
        default_template_library_root,
        validate_template_bundle,
    )

    library = Path(library_root or default_template_library_root())
    try:
        library_mode = os.lstat(library).st_mode
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [
            {
                "template_uuid": None,
                "display_name": library.name,
                "state": "invalid",
                "reason": "unreadable_template_library",
                "detail": str(exc),
            }
        ]
    if stat.S_ISLNK(library_mode) or not stat.S_ISDIR(library_mode):
        return [
            {
                "template_uuid": None,
                "display_name": library.name,
                "state": "invalid",
                "reason": "unreadable_template_library",
                "detail": "Pose-template library is not a regular directory",
            }
        ]
    blockers: list[dict[str, Any]] = []
    try:
        children = sorted(library.iterdir())
    except OSError as exc:
        return [
            {
                "template_uuid": None,
                "display_name": library.name,
                "state": "invalid",
                "reason": "unreadable_template_library",
                "detail": str(exc),
            }
        ]
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            bundle = validate_template_bundle(child, library_root=library)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(
                {
                    "template_uuid": child.name,
                    "display_name": child.name,
                    "state": "invalid",
                    "reason": "unreadable_template_bundle",
                    "detail": str(exc),
                }
            )
            continue
        if any(
            isinstance(instance, Mapping)
            and instance.get("catalog_uuid") == catalog_uuid
            for instance in bundle.get("instances", [])
        ):
            blockers.append(
                {
                    "template_uuid": bundle["template_uuid"],
                    "display_name": bundle["display_name"],
                    "state": bundle["archive"]["state"],
                    "reason": "catalog_reference",
                }
            )
    return blockers


def delete_catalog_object(
    catalog_uuid: str,
    *,
    catalog_root: str | Path | None = None,
    template_library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Delete one active or archived unreferenced entry, retaining a tombstone."""

    root = Path(catalog_root or default_catalog_root())
    opaque_id = _validate_uuid(catalog_uuid, label="catalog_uuid")
    with _mutation_lock(root):
        catalog = load_catalog(root, verify_assets=False)
        existing_tombstone = next(
            (
                record
                for record in catalog.get("tombstones", [])
                if record.get("catalog_uuid") == opaque_id
            ),
            None,
        )
        item = next(
            (
                record
                for record in catalog["objects"]
                if record["catalog_uuid"] == opaque_id
            ),
            None,
        )
        if item is None:
            if existing_tombstone is not None:
                destination = _contained(
                    root / "objects" / opaque_id, root, must_exist=False
                )
                cleanup = existing_tombstone.setdefault(
                    "asset_cleanup",
                    {
                        "status": "pending",
                        "path": destination.relative_to(root).as_posix(),
                    },
                )
                cleanup["last_attempt_at"] = utc_now_iso()
                try:
                    if destination.is_symlink():
                        raise OSError(
                            "Refusing to remove a symlink at the managed object path"
                        )
                    shutil.rmtree(destination)
                except FileNotFoundError:
                    cleanup.update(status="complete", last_error=None)
                except OSError as exc:
                    cleanup.update(
                        status="pending",
                        last_error=(f"{type(exc).__name__}: {exc}")[:2_000],
                    )
                else:
                    cleanup.update(status="complete", last_error=None)
                _commit_catalog(catalog, root)
                return {
                    "schema_version": "workpiece_catalog_delete.v1",
                    "status": (
                        "deleted"
                        if cleanup["status"] == "complete"
                        else "deleted_cleanup_pending"
                    ),
                    "already_deleted": True,
                    **existing_tombstone,
                }
            raise KeyError(f"Unknown catalog object: {opaque_id}")
        blockers = _template_delete_blockers(
            opaque_id, library_root=template_library_root
        )
        if blockers:
            raise CatalogObjectInUseError(blockers)
        destination = root / "objects" / opaque_id
        resolved_destination = _contained(destination, root, must_exist=False)
        for asset in item["assets"].values():
            if not isinstance(asset, Mapping):
                raise ValueError("Catalog asset record must be an object")
            relative = Path(str(asset.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Catalog asset path must be catalog-relative")
            asset_path = _contained(root / relative, root, must_exist=False)
            try:
                asset_path.relative_to(resolved_destination)
            except ValueError as exc:
                raise ValueError(
                    "Workpiece assets must stay in its managed object folder"
                ) from exc
        deleted_at = utc_now_iso()
        tombstone = {
            "catalog_uuid": opaque_id,
            "obj_id": int(item["obj_id"]),
            "name": item["name"],
            "alias": item.get("alias"),
            "source_filename": item["source_filename"],
            "source_sha256": item["source_sha256"],
            "canonical_ply_sha256": item["canonical_ply_sha256"],
            "geometry_revision": int(item["geometry_revision"]),
            "source_to_mm_scale": float(item["source_to_mm_scale"]),
            "geometry_revision_hashes": [
                revision["canonical_ply_sha256"]
                for revision in item["geometry_revisions"]
            ],
            "deleted_at": deleted_at,
            "asset_cleanup": {
                "status": "pending",
                "path": destination.relative_to(root).as_posix(),
                "last_attempt_at": None,
                "last_error": None,
            },
        }
        catalog["objects"] = [
            record
            for record in catalog["objects"]
            if record["catalog_uuid"] != opaque_id
        ]
        catalog.setdefault("tombstones", []).append(tombstone)
        catalog["tombstones"].sort(key=lambda record: int(record["obj_id"]))
        # Commit the manifest first. A process death after this point can leave
        # only an unreferenced asset directory, never a live record whose sole
        # asset snapshot has already disappeared.
        _commit_catalog(catalog, root)
        cleanup = tombstone["asset_cleanup"]
        cleanup["last_attempt_at"] = utc_now_iso()
        try:
            shutil.rmtree(destination)
        except FileNotFoundError:
            cleanup.update(status="complete", last_error=None)
        except OSError as exc:
            cleanup.update(
                status="pending",
                last_error=(f"{type(exc).__name__}: {exc}")[:2_000],
            )
        else:
            cleanup.update(status="complete", last_error=None)
        # Persist cleanup evidence separately. If the process stops between the
        # tombstone commit and this update, a repeated confirmed delete retries
        # the deterministic managed path and repairs the pending evidence.
        _commit_catalog(catalog, root)
        return {
            "schema_version": "workpiece_catalog_delete.v1",
            "status": (
                "deleted"
                if cleanup["status"] == "complete"
                else "deleted_cleanup_pending"
            ),
            **tombstone,
        }


def catalog_export_manifest(
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the portable JSON manifest without its machine-local root path."""

    catalog = load_catalog(catalog_root, verify_assets=False)
    return {key: value for key, value in catalog.items() if key != "catalog_root"}


def import_catalog_metadata(
    value: Mapping[str, Any],
    *,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    """Merge portable labels for matching immutable workpiece snapshots.

    JSON intentionally contains catalog-relative asset references, not file
    payloads. Records absent from this installation are reported as skipped;
    CAD files must first be uploaded (or the managed asset tree copied).
    """

    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Imported object catalog schema must be {SCHEMA_VERSION}")
    records = value.get("objects")
    if not isinstance(records, list):
        raise ValueError("Imported object catalog objects must be a list")
    if len(records) > 10_000:
        raise ValueError("Imported object catalog exceeds 10,000 records")
    root = Path(catalog_root or default_catalog_root())
    with _mutation_lock(root):
        catalog = load_catalog(root, verify_assets=False)
        current = {item["catalog_uuid"]: item for item in catalog["objects"]}
        updated: list[str] = []
        unchanged: list[str] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for raw in records:
            if not isinstance(raw, Mapping):
                raise ValueError("Imported catalog records must be objects")
            opaque_id = _validate_uuid(raw.get("catalog_uuid"), label="catalog_uuid")
            if opaque_id in seen:
                raise ValueError(
                    f"Imported catalog UUID appears more than once: {opaque_id}"
                )
            seen.add(opaque_id)
            existing = current.get(opaque_id)
            if existing is None:
                skipped.append(opaque_id)
                continue
            for field in ("obj_id", "source_sha256"):
                if field in raw and raw[field] != existing[field]:
                    raise ValueError(
                        f"Imported immutable identity does not match {opaque_id}: {field}"
                    )
            if "canonical_ply_sha256" in raw:
                imported_hash = raw["canonical_ply_sha256"]
                retained_hashes = {
                    revision["canonical_ply_sha256"]
                    for revision in existing["geometry_revisions"]
                }
                if (
                    not isinstance(imported_hash, str)
                    or imported_hash not in retained_hashes
                ):
                    raise ValueError(
                        "Imported immutable identity does not match "
                        f"{opaque_id}: canonical_ply_sha256"
                    )
            try:
                for asset in existing["assets"].values():
                    if not isinstance(asset, Mapping):
                        raise ValueError("Catalog asset record must be an object")
                    _validate_asset_record(asset, root)
                # A portable manifest may describe an earlier canonical
                # snapshot after this installation has published a scale
                # correction. Those retained canonical revisions are managed
                # assets too, so validate every one before importing labels.
                for revision in existing["geometry_revisions"]:
                    canonical = revision.get("canonical_ply")
                    if not isinstance(canonical, Mapping):
                        raise ValueError(
                            "Geometry revision canonical_ply must be an asset record"
                        )
                    _validate_asset_record(canonical, root)
            except (OSError, ValueError, TypeError):
                skipped.append(opaque_id)
                continue
            metadata = normalize_catalog_metadata(raw)
            if all(existing.get(key) == item for key, item in metadata.items()):
                unchanged.append(opaque_id)
                continue
            existing.update(metadata)
            existing["updated_at"] = utc_now_iso()
            updated.append(opaque_id)
        if updated:
            _commit_catalog(catalog, root)
        return {
            "schema_version": "workpiece_catalog_import.v1",
            "updated": updated,
            "unchanged": unchanged,
            "skipped_missing_assets": skipped,
        }


def set_catalog_object_state(
    catalog_uuid: str, *, state: str, catalog_root: str | Path | None = None
) -> dict[str, Any]:
    if state not in {"active", "archived"}:
        raise ValueError("Catalog state must be active or archived")
    root = Path(catalog_root or default_catalog_root())
    opaque_id = _validate_uuid(catalog_uuid, label="catalog_uuid")
    with _mutation_lock(root):
        catalog = load_catalog(root, verify_assets=False)
        for item in catalog["objects"]:
            if item["catalog_uuid"] == opaque_id:
                if item["state"] != state:
                    item["state"] = state
                    changed_at = utc_now_iso()
                    item["updated_at"] = changed_at
                    item["archived_at"] = changed_at if state == "archived" else None
                    _commit_catalog(catalog, root)
                return get_catalog_object(
                    opaque_id, catalog_root=root, verify_assets=False
                )
    raise KeyError(f"Unknown catalog object: {opaque_id}")
