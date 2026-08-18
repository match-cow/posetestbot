from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import trimesh

from posetestbot.pose_templates import catalog as catalog_module
from posetestbot.pose_templates import library as library_module
from posetestbot.pose_templates.catalog import import_catalog_object
from posetestbot.pose_templates.library import (
    clone_template_configuration,
    generate_template_bundle,
    list_template_bundle_summaries,
    load_template_bundle_detail,
    load_template_thumbnail,
    set_template_archive_state,
    validate_template_bundle,
)
from posetestbot.pose_templates.orientations import analyze_catalog_orientations
from posetestbot.web.app import create_app
from posetestbot.web.routes import pose_templates as pose_template_routes


def _bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    cad = tmp_path / "box.stl"
    cad.write_bytes(trimesh.creation.box(extents=(20, 10, 8)).export(file_type="stl"))
    catalog = tmp_path / "object_catalog"
    record = import_catalog_object(name="Box", cad_path=cad, catalog_root=catalog)
    analysis = analyze_catalog_orientations(
        record["catalog_uuid"], catalog_root=catalog
    )
    library = tmp_path / "pose_templates"
    bundle = generate_template_bundle(
        {
            "display_name": "Bounded card",
            "description": "Card metadata",
            "instances": [
                {
                    "instance_uuid": "11111111-1111-4111-8111-111111111111",
                    "catalog_uuid": record["catalog_uuid"],
                    "orientation_id": analysis["orientations"][0]["orientation_id"],
                    "pose": {"x_mm": 40, "y_mm": 40, "rotation_deg": 0},
                }
            ],
        },
        catalog_root=catalog,
        library_root=library,
    )
    return library, bundle


def test_new_bundle_manifest_omits_duplicate_exact_contours(tmp_path: Path) -> None:
    _library, bundle = _bundle(tmp_path)
    manifest = json.loads(
        (Path(bundle["bundle_path"]) / "pose_template_bundle.json").read_text()
    )

    assert "nominal_contours" not in manifest["instances"][0]
    assert "compensated_contours" not in manifest["instances"][0]
    assert manifest["instances"][0]["nominal_geometry_sha256"]
    assert manifest["instances"][0]["compensated_geometry_sha256"]


def test_new_library_cards_do_not_hash_full_bundle_assets(
    tmp_path: Path, monkeypatch: Any
) -> None:
    library, bundle = _bundle(tmp_path)

    hashed: list[str] = []

    def bounded_hash(path: Path) -> str:
        hashed.append(Path(path).name)
        if Path(path).name != "pose_template_thumbnail.json":
            raise AssertionError("bounded card reads must not hash full bundle files")
        return original_hash(path)

    original_hash = library_module._sha256
    monkeypatch.setattr(library_module, "_sha256", bounded_hash)

    summaries = list_template_bundle_summaries(library)
    thumbnail = load_template_thumbnail(bundle["template_uuid"], library_root=library)

    assert summaries[0]["display_name"] == "Bounded card"
    assert summaries[0]["instance_count"] == 1
    assert summaries[0]["instances"][0]["catalog_uuid"]
    assert thumbnail["display_name"] == "Bounded card"
    assert hashed == ["pose_template_thumbnail.json"]


def test_synchronous_bundle_routes_hash_only_the_requested_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    library, bundle = _bundle(tmp_path)
    template_uuid = bundle["template_uuid"]
    monkeypatch.setenv("POSETESTBOT_WORKING_DATA_ROOT", tmp_path.as_posix())
    monkeypatch.setattr(
        pose_template_routes,
        "REQUEST_ROOT",
        tmp_path / "jobs" / "pose_template_requests",
    )

    class FakeJob:
        id = "clone-job"

        def to_dict(self) -> dict[str, Any]:
            return {"id": self.id, "status": "queued"}

    class FakeRunner:
        def list(self, *, include_services: bool = True) -> list[Any]:
            return []

        def submit(self, **_kwargs: Any) -> FakeJob:
            return FakeJob()

    monkeypatch.setattr(pose_template_routes, "job_runner", FakeRunner())
    original_hash = library_module._sha256
    hashed: list[str] = []

    def recording_hash(path: Path) -> str:
        hashed.append(Path(path).name)
        return original_hash(path)

    monkeypatch.setattr(library_module, "_sha256", recording_hash)
    catalog_hashes: list[str] = []

    def unexpected_catalog_hash(path: Path) -> str:
        catalog_hashes.append(Path(path).name)
        raise AssertionError("clone must not hash catalogue assets")

    monkeypatch.setattr(catalog_module, "_sha256", unexpected_catalog_hash)
    client = create_app().test_client()

    assert client.get(f"/pose-templates/library/{template_uuid}").status_code == 200
    assert hashed == []

    assert (
        client.get(f"/pose-templates/library/{template_uuid}/preview").status_code
        == 200
    )
    assert hashed == ["pose_template_preview.json"]
    hashed.clear()

    assert (
        client.get(f"/pose-templates/library/{template_uuid}/download/pdf").status_code
        == 200
    )
    assert hashed == ["pose_template.pdf"]
    hashed.clear()

    assert (
        client.get(
            f"/pose-templates/library/{template_uuid}/download/manifest"
        ).status_code
        == 200
    )
    assert hashed == []

    assert (
        client.get(
            f"/pose-templates/library/{template_uuid}/assets/"
            "11111111-1111-4111-8111-111111111111/canonical_ply"
        ).status_code
        == 200
    )
    assert hashed == ["canonical.ply"]
    hashed.clear()

    archived = client.post(f"/pose-templates/library/{template_uuid}/archive")
    assert archived.status_code == 200
    assert archived.get_json()["archive"]["state"] == "archived"
    assert hashed == []

    clone = client.post(f"/pose-templates/library/{template_uuid}/clone", json={})
    assert clone.status_code == 202
    assert hashed == []

    # The reusable library operations have the same bounded behavior when used
    # by scripts or future synchronous callers.
    set_template_archive_state(template_uuid, state="active", library_root=library)
    cloned = clone_template_configuration(
        template_uuid,
        library_root=library,
        catalog_root=tmp_path / "object_catalog",
    )
    assert cloned["display_name"] == "Bounded card (copy)"
    assert hashed == []
    assert catalog_hashes == []


def test_oversized_manifest_fails_closed_without_full_asset_fallback(
    tmp_path: Path,
) -> None:
    library, bundle = _bundle(tmp_path)
    manifest_path = Path(bundle["bundle_path"]) / "pose_template_bundle.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["legacy_exact_geometry"] = "x" * (
        library_module.CARD_MANIFEST_MAX_JSON_BYTES + 1
    )
    manifest["bundle_sha256"] = library_module._hash_json(
        {key: value for key, value in manifest.items() if key != "bundle_sha256"}
    )
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="exceeds the current size limit"):
        load_template_bundle_detail(bundle["template_uuid"], library_root=library)


def test_bundle_validation_rejects_undeclared_files_and_symlinks(
    tmp_path: Path,
) -> None:
    library, bundle = _bundle(tmp_path)
    bundle_root = Path(bundle["bundle_path"])
    unexpected = bundle_root / "unexpected.txt"
    unexpected.write_text("not declared by the immutable manifest")

    try:
        with pytest.raises(ValueError, match="undeclared file"):
            validate_template_bundle(bundle_root, library_root=library)
        assert list_template_bundle_summaries(library) == []
    finally:
        unexpected.unlink()

    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (bundle_root / "unexpected-link").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        validate_template_bundle(bundle_root, library_root=library)
