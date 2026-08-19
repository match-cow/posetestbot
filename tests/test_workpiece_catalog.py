from __future__ import annotations

import copy
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import trimesh

from posetestbot.pose_templates import adapter
from posetestbot.pose_templates import catalog as catalog_module
from posetestbot.pose_templates.catalog import (
    CATALOG_MANIFEST,
    CatalogGeometryRevisionConflict,
    CatalogObjectInUseError,
    catalog_export_manifest,
    correct_catalog_object_units,
    delete_catalog_object,
    import_catalog_metadata,
    import_catalog_object,
    load_catalog,
    resolve_catalog_asset,
    set_catalog_object_state,
    update_catalog_object_metadata,
)
from posetestbot.pose_templates.library import generate_template_bundle
from posetestbot.pose_templates.orientations import analyze_catalog_orientations


class FakeMeshBackend:
    constants = SimpleNamespace(MAX_UPLOAD_BYTES=50 * 1024 * 1024)

    @staticmethod
    def safe_filename(filename: str | None) -> str:
        value = Path(str(filename or "")).name
        if not value:
            raise ValueError("A filename is required")
        return value

    @staticmethod
    def file_format(filename: str) -> str:
        extension = Path(filename).suffix.lower().lstrip(".")
        if extension not in {"ply", "stl", "obj"}:
            raise ValueError("Unsupported CAD format")
        return extension

    def canonical_ply(self, filename: str, data: bytes) -> tuple[bytes, dict]:
        self.file_format(filename)
        return (
            b"ply\nformat ascii 1.0\ncomment derived by pytest\nend_header\n",
            {
                "vertices": 8,
                "faces": 12,
                "bounds_mm": [[-5.0, -5.0, -5.0], [5.0, 5.0, 5.0]],
                "watertight": True,
            },
        )


class ScalingMeshBackend(FakeMeshBackend):
    def load_mesh(self, filename: str, data: bytes):
        extension = self.file_format(filename)
        loaded = trimesh.load(
            file_obj=io.BytesIO(data),
            file_type=extension,
            process=False,
        )
        if isinstance(loaded, trimesh.Scene):
            loaded = loaded.to_geometry()
        assert isinstance(loaded, trimesh.Trimesh)
        return loaded

    def canonical_ply(self, filename: str, data: bytes) -> tuple[bytes, dict]:
        mesh = self.load_mesh(filename, data)
        exported = mesh.export(file_type="ply", encoding="binary_little_endian")
        payload = (
            exported.encode("utf-8") if isinstance(exported, str) else bytes(exported)
        )
        return (
            payload,
            {
                "vertices": int(len(mesh.vertices)),
                "faces": int(len(mesh.faces)),
                "bounds_mm": mesh.bounds.tolist(),
                "watertight": bool(mesh.is_watertight),
            },
        )


class FailingOrientationBackend(ScalingMeshBackend):
    def orientation_artifacts(self, _filename: str, _data: bytes) -> dict:
        raise RuntimeError("injected optional orientation analysis failure")


@pytest.fixture(autouse=True)
def fake_mesh_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: FakeMeshBackend(),
    )


def cad_file(path: Path, payload: bytes = b"solid fixture\nendsolid fixture\n") -> Path:
    path.write_bytes(payload)
    return path


def add_workpiece(catalog_root: Path, source: Path, **metadata) -> dict:
    return import_catalog_object(
        name=metadata.pop("name", "Fixture"),
        cad_path=source,
        catalog_root=catalog_root,
        **metadata,
    )


def referenced_template_configuration(catalog_uuid: str, orientation_id: str) -> dict:
    return {
        "display_name": "Workpiece deletion guard",
        "instances": [
            {
                "instance_uuid": "11111111-1111-4111-8111-111111111111",
                "catalog_uuid": catalog_uuid,
                "orientation_id": orientation_id,
                "pose": {
                    "x_mm": 40,
                    "y_mm": 40,
                    "rotation_deg": 0,
                },
            }
        ],
    }


def test_import_persists_normalized_metadata_and_separates_canonical_named_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "object_catalog"
    source_bytes = b"ply\nformat ascii 1.0\ncomment original upload\nend_header\n"
    source = cad_file(tmp_path / "canonical.ply", source_bytes)

    record = add_workpiece(
        root,
        source,
        name="  Clamp body  ",
        alias="  Small clamp  ",
        description="  Machined fixture  ",
        tags=["Metal", " metal ", "Reflective"],
        groups=["Calibration A", " calibration a ", "Clamps"],
        attributes={"Mass g": 125, "Finish": " matte "},
    )

    assert record["name"] == "Clamp body"
    assert record["alias"] == "Small clamp"
    assert record["description"] == "Machined fixture"
    assert record["tags"] == ["Metal", "Reflective"]
    assert record["groups"] == ["Calibration A", "Clamps"]
    assert record["attributes"] == {"Finish": "matte", "Mass g": "125"}
    source_record = record["assets"]["source"]
    canonical_record = record["assets"]["canonical_ply"]
    assert source_record["path"].endswith("/source/canonical.ply")
    assert canonical_record["path"].endswith("/derived/000001/canonical.ply")
    assert source_record["path"] != canonical_record["path"]

    _, _, resolved_source = resolve_catalog_asset(
        record["catalog_uuid"], "source", catalog_root=root
    )
    _, _, resolved_canonical = resolve_catalog_asset(
        record["catalog_uuid"], "canonical_ply", catalog_root=root
    )
    assert resolved_source.read_bytes() == source_bytes
    assert resolved_canonical.read_bytes() != source_bytes


def test_load_catalog_migrates_orientation_cache_out_of_durable_assets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "object_catalog"
    record = add_workpiece(root, cad_file(tmp_path / "cache-pointer.stl"))
    manifest_path = root / CATALOG_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    stale_cache = {
        "path": f"objects/{record['catalog_uuid']}/derived/000001/"
        "pose_template_orientation_analysis.json",
        "media_type": "application/json",
        "size_bytes": 999,
        "sha256": "0" * 64,
    }
    manifest["objects"][0]["assets"]["orientation_analysis"] = stale_cache
    manifest["objects"][0]["geometry_revisions"][0]["orientation_analysis"] = (
        stale_cache
    )
    manifest_path.write_text(json.dumps(manifest))

    loaded = load_catalog(root)

    assert "orientation_analysis" not in loaded["objects"][0]["assets"]
    assert "orientation_analysis" not in loaded["objects"][0]["geometry_revisions"][0]
    update_catalog_object_metadata(
        record["catalog_uuid"], {"alias": "Migrated"}, catalog_root=root
    )
    persisted = json.loads(manifest_path.read_text())["objects"][0]
    assert "orientation_analysis" not in persisted["assets"]
    assert "orientation_analysis" not in persisted["geometry_revisions"][0]


def test_metadata_and_archive_mutations_preserve_geometry_and_are_revisioned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "object_catalog"
    record = add_workpiece(root, cad_file(tmp_path / "fixture.stl"))
    immutable = {
        key: copy.deepcopy(record[key])
        for key in (
            "catalog_uuid",
            "obj_id",
            "source_filename",
            "source_sha256",
            "canonical_ply_sha256",
            "assets",
            "extraction",
            "created_at",
        )
    }
    starting_version = load_catalog(root)["version"]

    updated = update_catalog_object_metadata(
        record["catalog_uuid"],
        {
            "alias": "Inspection clamp",
            "tags": ["Metal", "metal", "QA"],
            "groups": ["Bench 2"],
            "attributes": {"Owner": "Vision lab", "Revision": 3},
        },
        catalog_root=root,
    )

    assert updated["alias"] == "Inspection clamp"
    assert updated["tags"] == ["Metal", "QA"]
    assert updated["groups"] == ["Bench 2"]
    assert updated["attributes"] == {"Owner": "Vision lab", "Revision": "3"}
    assert {key: updated[key] for key in immutable} == immutable
    assert load_catalog(root)["version"] == starting_version + 1

    # Repeating an identical edit is a no-op rather than another revision.
    update_catalog_object_metadata(
        record["catalog_uuid"],
        {
            "alias": "Inspection clamp",
            "tags": ["Metal", "QA"],
            "groups": ["Bench 2"],
            "attributes": {"Owner": "Vision lab", "Revision": 3},
        },
        catalog_root=root,
    )
    assert load_catalog(root)["version"] == starting_version + 1

    archived = set_catalog_object_state(
        record["catalog_uuid"], state="archived", catalog_root=root
    )
    restored = set_catalog_object_state(
        record["catalog_uuid"], state="active", catalog_root=root
    )
    assert archived["state"] == "archived"
    assert restored["state"] == "active"
    assert restored["obj_id"] == record["obj_id"]
    assert len(list((root / "revisions").glob("*.json"))) >= 4

    with pytest.raises(ValueError, match="tags must be a JSON array"):
        update_catalog_object_metadata(
            record["catalog_uuid"], {"tags": "not-a-list"}, catalog_root=root
        )


def test_direct_active_delete_tombstones_identity_and_never_reuses_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "object_catalog"
    library = tmp_path / "pose_templates"
    first = add_workpiece(root, cad_file(tmp_path / "first.stl"))

    deleted = delete_catalog_object(
        first["catalog_uuid"],
        catalog_root=root,
        template_library_root=library,
    )
    catalog = load_catalog(root)
    assert deleted["status"] == "deleted"
    assert catalog["objects"] == []
    assert catalog["tombstones"][0]["catalog_uuid"] == first["catalog_uuid"]
    assert catalog["tombstones"][0]["obj_id"] == first["obj_id"]
    assert not (root / "objects" / first["catalog_uuid"]).exists()

    # Explicit imports may not bypass the retired UUID or BOP identity.
    with pytest.raises(ValueError):
        add_workpiece(
            root,
            cad_file(tmp_path / "reuse-id.stl"),
            obj_id=first["obj_id"],
        )
    assert load_catalog(root)["objects"] == []
    with pytest.raises(ValueError):
        add_workpiece(
            root,
            cad_file(tmp_path / "reuse-uuid.stl"),
            catalog_uuid=first["catalog_uuid"],
        )
    assert load_catalog(root)["objects"] == []

    second = add_workpiece(root, cad_file(tmp_path / "second.stl"))
    assert second["obj_id"] == first["obj_id"] + 1


def test_direct_delete_blocks_valid_pose_template_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = tmp_path / "working_data"
    root = working / "object_catalog"
    library = working / "pose_templates"
    monkeypatch.setenv("POSETESTBOT_WORKING_DATA_ROOT", working.as_posix())
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        adapter.load_posetemplatecreator_backend,
    )
    source = tmp_path / "referenced.stl"
    source.write_bytes(
        trimesh.creation.box(extents=(20, 10, 10)).export(file_type="stl")
    )
    record = add_workpiece(root, source, name="Referenced fixture")
    orientation_id = analyze_catalog_orientations(
        record["catalog_uuid"], catalog_root=root
    )["orientations"][0]["orientation_id"]
    bundle = generate_template_bundle(
        referenced_template_configuration(record["catalog_uuid"], orientation_id),
        catalog_root=root,
        library_root=library,
    )
    set_catalog_object_state(
        record["catalog_uuid"], state="archived", catalog_root=root
    )
    object_folder = root / "objects" / record["catalog_uuid"]

    with pytest.raises(CatalogObjectInUseError) as conflict:
        delete_catalog_object(
            record["catalog_uuid"],
            catalog_root=root,
            template_library_root=library,
        )

    assert conflict.value.blockers
    assert bundle["template_uuid"] in json.dumps(conflict.value.blockers)
    assert object_folder.is_dir()
    assert load_catalog(root)["objects"][0]["catalog_uuid"] == record["catalog_uuid"]


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        b'{"schema_version":"pose_template_bundle.v1"}',
        b"{not-json",
    ],
    ids=["invalid", "unreadable"],
)
def test_direct_delete_fails_closed_when_a_template_bundle_cannot_be_validated(
    tmp_path: Path,
    manifest_bytes: bytes,
) -> None:
    root = tmp_path / "object_catalog"
    library = tmp_path / "pose_templates"
    record = add_workpiece(root, cad_file(tmp_path / "guarded.stl"))
    set_catalog_object_state(
        record["catalog_uuid"], state="archived", catalog_root=root
    )
    bundle_uuid = "22222222-2222-4222-8222-222222222222"
    bundle_folder = library / bundle_uuid
    bundle_folder.mkdir(parents=True)
    (bundle_folder / "pose_template_bundle.json").write_bytes(manifest_bytes)

    with pytest.raises(CatalogObjectInUseError) as conflict:
        delete_catalog_object(
            record["catalog_uuid"],
            catalog_root=root,
            template_library_root=library,
        )

    assert conflict.value.blockers
    assert bundle_uuid in json.dumps(conflict.value.blockers)
    assert (root / "objects" / record["catalog_uuid"]).is_dir()
    assert load_catalog(root)["objects"][0]["catalog_uuid"] == record["catalog_uuid"]


def test_direct_delete_fails_closed_for_unexpected_library_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "object_catalog"
    library = tmp_path / "pose_templates"
    record = add_workpiece(root, cad_file(tmp_path / "guarded-file.stl"))
    set_catalog_object_state(
        record["catalog_uuid"], state="archived", catalog_root=root
    )
    library.mkdir()
    unexpected = library / "22222222-2222-4222-8222-222222222222"
    unexpected.write_text("corrupt bundle entry")

    with pytest.raises(CatalogObjectInUseError) as conflict:
        delete_catalog_object(
            record["catalog_uuid"],
            catalog_root=root,
            template_library_root=library,
        )

    assert conflict.value.blockers[0]["reason"] == "unreadable_template_bundle"
    assert unexpected.is_file()
    assert (root / "objects" / record["catalog_uuid"]).is_dir()


def test_delete_commit_failure_observes_and_preserves_the_asset_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "object_catalog"
    library = tmp_path / "pose_templates"
    record = add_workpiece(root, cad_file(tmp_path / "commit-failure.stl"))
    set_catalog_object_state(
        record["catalog_uuid"], state="archived", catalog_root=root
    )
    object_folder = root / "objects" / record["catalog_uuid"]
    source_asset = root / record["assets"]["source"]["path"]

    def fail_commit(_catalog: dict, _root: Path) -> None:
        # The durable manifest must be attempted before any asset is moved or removed.
        assert object_folder.is_dir()
        assert source_asset.is_file()
        raise OSError("injected catalog manifest failure")

    monkeypatch.setattr(catalog_module, "_commit_catalog", fail_commit)

    with pytest.raises(OSError, match="injected catalog manifest failure"):
        delete_catalog_object(
            record["catalog_uuid"],
            catalog_root=root,
            template_library_root=library,
        )

    assert object_folder.is_dir()
    assert source_asset.is_file()
    assert load_catalog(root)["objects"][0]["catalog_uuid"] == record["catalog_uuid"]
    assert not list(root.glob(".delete-*.tmp"))


def test_metadata_manifest_export_import_round_trip_is_identity_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "object_catalog"
    record = add_workpiece(
        root,
        cad_file(tmp_path / "portable.stl"),
        alias="Portable",
        tags=["original"],
        groups=["set-a"],
        attributes={"owner": "lab"},
    )
    exported = catalog_export_manifest(root)
    assert exported["schema_version"] == "object_catalog.v1"
    assert "catalog_root" not in exported

    update_catalog_object_metadata(
        record["catalog_uuid"],
        {"alias": "Changed", "tags": ["changed"]},
        catalog_root=root,
    )
    portable = copy.deepcopy(exported)
    missing_uuid = str(uuid.uuid4())
    portable["objects"].append({"catalog_uuid": missing_uuid})

    result = import_catalog_metadata(portable, catalog_root=root)
    restored = load_catalog(root)["objects"][0]

    assert result["updated"] == [record["catalog_uuid"]]
    assert result["skipped_missing_assets"] == [missing_uuid]
    assert restored["alias"] == "Portable"
    assert restored["tags"] == ["original"]
    assert restored["obj_id"] == record["obj_id"]
    assert restored["source_sha256"] == record["source_sha256"]

    mismatched = copy.deepcopy(exported)
    mismatched["objects"][0]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="immutable identity"):
        import_catalog_metadata(mismatched, catalog_root=root)


def test_resolve_catalog_asset_rejects_tampered_content(tmp_path: Path) -> None:
    root = tmp_path / "object_catalog"
    record = add_workpiece(root, cad_file(tmp_path / "tampered.stl"))
    source = root / record["assets"]["source"]["path"]
    source.write_bytes(b"x" * source.stat().st_size)

    with pytest.raises(ValueError, match="hash mismatch"):
        resolve_catalog_asset(record["catalog_uuid"], "source", catalog_root=root)


def test_unit_correction_revisions_geometry_from_retained_source_and_is_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "object_catalog"
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: ScalingMeshBackend(),
    )
    source = tmp_path / "meter-authored.ply"
    source.write_bytes(
        bytes(trimesh.creation.box(extents=(0.02, 0.01, 0.005)).export(file_type="ply"))
    )
    original = add_workpiece(root, source, name="Meter-authored fixture")
    original_source = copy.deepcopy(original["assets"]["source"])
    original_source_bytes = (root / original_source["path"]).read_bytes()
    original_canonical = copy.deepcopy(original["assets"]["canonical_ply"])
    set_catalog_object_state(
        original["catalog_uuid"], state="archived", catalog_root=root
    )

    corrected = correct_catalog_object_units(
        original["catalog_uuid"],
        conversion="meter_to_millimeter",
        confirm=True,
        operator="pytest operator",
        expected_geometry_revision=1,
        expected_canonical_sha256=original["canonical_ply_sha256"],
        catalog_root=root,
    )

    assert corrected["catalog_uuid"] == original["catalog_uuid"]
    assert corrected["obj_id"] == original["obj_id"]
    assert corrected["geometry_revision"] == 2
    assert corrected["source_to_mm_scale"] == 1000.0
    assert corrected["state"] == "archived"
    assert corrected["assets"]["source"] == original_source
    assert (root / original_source["path"]).read_bytes() == original_source_bytes
    assert (root / original_canonical["path"]).is_file()
    assert corrected["assets"]["canonical_ply"]["path"] != original_canonical["path"]
    assert corrected["extraction"]["bounds_mm"][1] == pytest.approx([10.0, 5.0, 2.5])
    operation = corrected["geometry_revisions"][-1]["operation"]
    assert operation["conversion"] == "meter_to_millimeter"
    assert (
        operation["previous_canonical_ply_sha256"] == original["canonical_ply_sha256"]
    )

    restored_scale = correct_catalog_object_units(
        original["catalog_uuid"],
        conversion="millimeter_to_meter",
        confirm=True,
        operator="pytest operator",
        expected_geometry_revision=2,
        expected_canonical_sha256=corrected["canonical_ply_sha256"],
        catalog_root=root,
    )
    assert restored_scale["geometry_revision"] == 3
    assert restored_scale["source_to_mm_scale"] == pytest.approx(1.0)
    assert restored_scale["canonical_ply_sha256"] == original["canonical_ply_sha256"]
    assert len(restored_scale["geometry_revisions"]) == 3
    assert all(
        (root / revision["canonical_ply"]["path"]).is_file()
        for revision in restored_scale["geometry_revisions"]
    )


def test_unit_correction_commits_when_optional_orientation_cache_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "object_catalog"
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: FailingOrientationBackend(),
    )
    source = tmp_path / "cache-failure.ply"
    source.write_bytes(
        bytes(trimesh.creation.box(extents=(0.02, 0.01, 0.005)).export(file_type="ply"))
    )
    original = add_workpiece(root, source)
    set_catalog_object_state(
        original["catalog_uuid"], state="archived", catalog_root=root
    )

    corrected = correct_catalog_object_units(
        original["catalog_uuid"],
        conversion="meter_to_millimeter",
        confirm=True,
        operator="pytest operator",
        expected_geometry_revision=1,
        expected_canonical_sha256=original["canonical_ply_sha256"],
        catalog_root=root,
    )

    canonical = root / corrected["assets"]["canonical_ply"]["path"]
    assert corrected["geometry_revision"] == 2
    assert corrected["source_to_mm_scale"] == 1000.0
    assert canonical.is_file()
    assert not canonical.with_name("pose_template_orientation_analysis.json").exists()
    assert corrected["orientation_analysis_cache"] == {
        "status": "unavailable",
        "reason": (
            "Stable-orientation cache generation failed: "
            "injected optional orientation analysis failure"
        ),
    }
    persisted = load_catalog(root)
    assert persisted["objects"][0]["geometry_revision"] == 2
    assert "orientation_analysis_cache" not in persisted["objects"][0]


def test_metadata_import_accepts_retained_canonical_hash_after_unit_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "object_catalog"
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: ScalingMeshBackend(),
    )
    source = tmp_path / "portable-history.ply"
    source.write_bytes(
        bytes(trimesh.creation.box(extents=(0.02, 0.01, 0.005)).export(file_type="ply"))
    )
    original = add_workpiece(root, source, alias="Before correction")
    exported_before_correction = catalog_export_manifest(root)
    set_catalog_object_state(
        original["catalog_uuid"], state="archived", catalog_root=root
    )
    corrected = correct_catalog_object_units(
        original["catalog_uuid"],
        conversion="meter_to_millimeter",
        confirm=True,
        operator="pytest operator",
        expected_geometry_revision=1,
        expected_canonical_sha256=original["canonical_ply_sha256"],
        catalog_root=root,
    )
    update_catalog_object_metadata(
        original["catalog_uuid"], {"alias": "Changed locally"}, catalog_root=root
    )

    result = import_catalog_metadata(exported_before_correction, catalog_root=root)

    assert result["updated"] == [original["catalog_uuid"]]
    restored = load_catalog(root)["objects"][0]
    assert restored["alias"] == "Before correction"
    assert restored["geometry_revision"] == corrected["geometry_revision"]
    assert restored["canonical_ply_sha256"] == corrected["canonical_ply_sha256"]

    unknown = copy.deepcopy(exported_before_correction)
    unknown["objects"][0]["canonical_ply_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="canonical_ply_sha256"):
        import_catalog_metadata(unknown, catalog_root=root)


def test_metadata_import_skips_record_with_missing_retained_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "object_catalog"
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: ScalingMeshBackend(),
    )
    source = tmp_path / "missing-history.ply"
    source.write_bytes(
        bytes(trimesh.creation.box(extents=(0.02, 0.01, 0.005)).export(file_type="ply"))
    )
    original = add_workpiece(root, source, alias="Portable alias")
    exported = catalog_export_manifest(root)
    set_catalog_object_state(
        original["catalog_uuid"], state="archived", catalog_root=root
    )
    corrected = correct_catalog_object_units(
        original["catalog_uuid"],
        conversion="meter_to_millimeter",
        confirm=True,
        operator="pytest operator",
        expected_geometry_revision=1,
        expected_canonical_sha256=original["canonical_ply_sha256"],
        catalog_root=root,
    )
    update_catalog_object_metadata(
        original["catalog_uuid"], {"alias": "Keep local"}, catalog_root=root
    )
    old_revision = corrected["geometry_revisions"][0]
    (root / old_revision["canonical_ply"]["path"]).unlink()

    result = import_catalog_metadata(exported, catalog_root=root)

    assert result["updated"] == []
    assert result["skipped_missing_assets"] == [original["catalog_uuid"]]
    loaded_without_verification = load_catalog(root, verify_assets=False)
    assert loaded_without_verification["objects"][0]["alias"] == "Keep local"


def test_unit_correction_rejects_stale_intent_and_retry_survives_commit_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "object_catalog"
    monkeypatch.setattr(
        catalog_module,
        "load_posetemplatecreator_backend",
        lambda: ScalingMeshBackend(),
    )
    source = tmp_path / "retry.ply"
    source.write_bytes(
        bytes(trimesh.creation.box(extents=(1, 2, 3)).export(file_type="ply"))
    )
    record = add_workpiece(root, source)

    with pytest.raises(ValueError, match="must be archived"):
        correct_catalog_object_units(
            record["catalog_uuid"],
            conversion="meter_to_millimeter",
            confirm=True,
            operator="pytest",
            expected_geometry_revision=1,
            expected_canonical_sha256=record["canonical_ply_sha256"],
            catalog_root=root,
        )
    set_catalog_object_state(
        record["catalog_uuid"], state="archived", catalog_root=root
    )
    real_commit = catalog_module._commit_catalog

    def fail_commit(_value: dict, _root: Path) -> None:
        raise OSError("injected correction commit failure")

    monkeypatch.setattr(catalog_module, "_commit_catalog", fail_commit)
    with pytest.raises(OSError, match="injected correction commit failure"):
        correct_catalog_object_units(
            record["catalog_uuid"],
            conversion="meter_to_millimeter",
            confirm=True,
            operator="pytest",
            expected_geometry_revision=1,
            expected_canonical_sha256=record["canonical_ply_sha256"],
            catalog_root=root,
        )
    assert load_catalog(root)["objects"][0]["geometry_revision"] == 1

    monkeypatch.setattr(catalog_module, "_commit_catalog", real_commit)
    corrected = correct_catalog_object_units(
        record["catalog_uuid"],
        conversion="meter_to_millimeter",
        confirm=True,
        operator="pytest",
        expected_geometry_revision=1,
        expected_canonical_sha256=record["canonical_ply_sha256"],
        catalog_root=root,
    )
    assert corrected["geometry_revision"] == 2
    assert (
        len(
            list(
                (root / "objects" / record["catalog_uuid"] / "derived").glob("000002-*")
            )
        )
        == 2
    )
    with pytest.raises(CatalogGeometryRevisionConflict):
        correct_catalog_object_units(
            record["catalog_uuid"],
            conversion="meter_to_millimeter",
            confirm=True,
            operator="pytest",
            expected_geometry_revision=1,
            expected_canonical_sha256=record["canonical_ply_sha256"],
            catalog_root=root,
        )
