# Workpiece Catalogue

The Workpiece Catalogue is PoseTestBot's persistent, JSON-backed source of
test-object identity. It owns CAD upload, retained assets, operator labels,
classification, lifecycle, and visual identification. Pose Templates consumes
active workpieces from this catalogue; the catalogue does not run pose
estimators, cameras, or the robot.

The page is available as **Workpiece Catalogue** in the operator console,
directly below **Calibration Targets** and above **Pose Templates**.

## Operator workflow

1. Open **Workpiece Catalogue** and upload one PLY, STL, or OBJ file, with an
   optional PNG texture. A local CPU/disk job validates and converts the mesh.
2. Give the workpiece a name and optionally an alias, description, tags,
   groups, and custom key/value attributes. These labels can be edited later
   without changing geometry or BOP identity.
3. Identify the stored object with the interactive canonical-PLY view and the
   compact isometric previews used on catalogue/template cards. Search the list
   and filter it by tag, group, or active/archive state.
4. If the dimensions show that the unitless CAD file was interpreted at the
   wrong scale, archive the workpiece and choose **Correct model units**. Check
   the displayed dimensions, identify the operator, explicitly confirm, and
   queue either metre-to-millimetre (×1000) or millimetre-to-metre (÷1000).
   Inspect the result before restoring the workpiece.
5. Open **Pose Templates**. Filter and add active workpieces to an immutable
   printable template. Archived workpieces cannot be added to a new template.

PoseTemplateCreator is required for a new CAD inspection/conversion job. When
that optional pinned checkout is unavailable, retained workpieces, metadata,
previews, lifecycle controls, and immutable pose-template snapshots remain
readable.

## Persistent storage

The default root is `working_data/object_catalog/`, or the equivalent root
below `POSETESTBOT_WORKING_DATA_ROOT`:

```text
working_data/object_catalog/
├── .catalog.lock
├── object_catalog.json
├── revisions/
│   ├── 00000002.json
│   └── ...
└── objects/
    └── <catalog_uuid>/
        ├── source/<uploaded-name>.ply|.stl|.obj
        ├── derived/
        │   ├── 000001/
        │   │   ├── canonical.ply
        │   │   ├── pose_template_orientation_analysis.json
        │   │   └── pose_template_orientation_thumbnail.json
        │   └── 000002-<hash>-<nonce>/...
        └── texture/texture.png              # optional
```

`object_catalog.json` uses `object_catalog.v1`. The new metadata fields are
additive: older v1 entries that do not contain them load with empty/default
values. Each asset record contains a catalogue-relative path, byte size, media
type, and SHA-256. The source and canonical PLY are required; the texture is
optional.

An abridged record has this shape:

```json
{
  "schema_version": "object_catalog.v1",
  "version": 12,
  "next_obj_id": 8,
  "objects": [
    {
      "catalog_uuid": "7b69a02e-7d20-4615-a29f-85e566ef2a24",
      "obj_id": 7,
      "name": "Valve body",
      "alias": "VB-40",
      "description": "Machined validation workpiece",
      "tags": ["metal", "reflective"],
      "groups": ["valves"],
      "attributes": {"finish": "brushed", "revision": "C"},
      "state": "active",
      "source_filename": "valve-body.stl",
      "source_format": "stl",
      "source_sha256": "<sha256>",
      "canonical_ply_sha256": "<sha256>",
      "geometry_revision": 1,
      "source_to_mm_scale": 1.0,
      "geometry_revisions": [
        {
          "revision": 1,
          "source_to_mm_scale": 1.0,
          "canonical_ply_sha256": "<sha256>",
          "operation": {"kind": "import", "factor": 1.0}
        }
      ],
      "assets": {
        "source": {
          "path": "objects/7b69a02e-7d20-4615-a29f-85e566ef2a24/source/valve-body.stl",
          "size_bytes": 1234,
          "sha256": "<sha256>",
          "media_type": "application/octet-stream"
        },
        "canonical_ply": {
          "path": "objects/7b69a02e-7d20-4615-a29f-85e566ef2a24/derived/000001/canonical.ply",
          "size_bytes": 2345,
          "sha256": "<sha256>",
          "media_type": "application/octet-stream"
        }
      }
    }
  ],
  "tombstones": []
}
```

The mutable operator fields are `name`, `alias`, `description`, `tags`,
`groups`, and `attributes`. Tags and groups accept up to 64 unique values;
attributes accept up to 64 custom scalar key/value pairs and are normalized to
strings. Catalogue UUID, BOP `obj_id`, source identity, geometry hashes,
extraction evidence, asset paths, and creation identity are not editable.

## Upload and previews

Uploads accept one CAD file (`.ply`, `.stl`, or `.obj`) of at most 50 MiB and
at most one PNG texture. The total request payload is limited to 100 MiB. The
web request stages the files and queues `workpiece_catalog_import` through the
local job runner with CPU, disk-I/O, and catalogue resources; conversion is not
performed in the request handler. Both declared and streaming requests without
`Content-Length` are capped. The worker removes its managed request directory
after either success or failure; submission also prunes abandoned request
directories older than 24 hours while preserving inputs for active jobs. Unit
correction uses the same cleanup policy.

The selected-object view loads the exact current canonical PLY in one orbitable
WebGL view. Its URL is revisioned by the canonical SHA-256, so a geometry
correction cannot reuse stale browser or Three.js loader state. It starts in
the authored catalogue orientation rather than silently choosing a stable
face; the view only centres and uniformly scales it for display.
Stable-placement comparison remains part of Pose Templates. Vertex colours and
normals are retained when present, missing normals are computed client-side,
and open CAD is rendered double-sided. Loader entries are evicted when the
selected object changes. This makes ports, holes, recesses, handles, and
separated components available for identification without a BlenderProc or
server-side rendering service.

Compact cards still read the separate, at-most-256-KiB orientation thumbnail
and never download every full mesh merely to browse a list. Before publishing
that cache, PoseTestBot welds a preview copy and keeps the indexed source
surface when it fits the 4,096-vertex/8,192-face envelope. Larger surfaces use
deterministic quadric decimation and a bounded spatial candidate. Component and
Euler signatures select the candidate that retains more source topology;
PoseTemplateCreator's broad convex proxy is used only if every bounded
recognition strategy fails. The cache records strategy, source/result counts,
topology signatures, and any fallback reason. Cards expose that evidence with
a keyboard-accessible `LOD`, `Approx`, or `Proxy` explanation instead of
silently claiming exactness. They show the authored orientation for
recognition, load only as they approach the viewport, keep small previews on an
inspectable isometric SVG path, and rasterize dense projections into one
Canvas2D element rather than creating thousands of DOM polygons.

Stable-orientation extraction is CPU/disk work. It is queued through the local
job runner when a template first needs it and cached beside the exact canonical
revision. The cache is bound to the catalogue UUID, canonical PLY SHA-256,
adapter version, and upstream revision. It is deliberately not a durable
catalogue asset: regenerating this derived cache cannot invalidate the
catalogue manifest. A separately bounded `pose_template_orientation_thumbnail.json`
uses the same binding for catalogue cards; the full
`pose_template_orientation_analysis.json` retains ranked orientations and exact
slice contours for the editor.

## Geometry revisions and unit correction

CAD formats commonly omit authoritative units. Unit correction changes the
canonical millimetre interpretation while preserving the original uploaded
bytes, catalogue UUID, and BOP `obj_id`:

- `meter_to_millimeter` multiplies the current source-to-mm scale by 1000.
- `millimeter_to_meter` multiplies it by 0.001.

The worker always regenerates from the retained source using the cumulative
`source_to_mm_scale`; it never repeatedly scales a rounded derivative. Applying
the inverse operation can therefore reproduce the original canonical hash.
Every canonical version remains under `derived/`, while the active record
points to the latest revision and records the operator, factor, prior revision,
and prior hash. A compare-and-swap check rejects a queued correction if another
mutation changed the expected revision or hash first.

Correction requires the workpiece to be archived, explicit confirmation, and
operator provenance. Existing immutable template and run bundles retain their
copied geometry and transforms; correction never rewrites them.

## Lifecycle and identity

- **Archive** hides a workpiece from new pose-template selection and is
  reversible with **Restore**. Existing immutable templates and selected runs
  are unchanged.
- **Delete** is permanent and available directly for active or archived
  workpieces. The request must contain explicit confirmation, and no active or
  archived pose-template bundle may reference its catalogue UUID. Deletion also
  strictly validates every published bundle's declared tree and hashes and
  fails closed on an unreadable, partial, modified, symlinked, or undeclared
  entry, because that library cannot safely be proven reference-free.
  A refused action keeps its confirmation open with an error notification;
  the retry and Cancel controls remain reachable while that notification is
  displayed.
- A successful delete removes the managed UUID asset directory but retains a
  tombstone with the UUID, BOP `obj_id`, source identity, and deletion time.
  Tombstoned UUIDs and BOP IDs are never reused; `next_obj_id` remains
  monotonic. The tombstone also records asset-cleanup status, managed path,
  last attempt, and any bounded error text. If filesystem cleanup fails after
  identity retirement, the response reports `deleted_cleanup_pending`; repeat
  the same confirmed delete to retry that exact managed directory safely.

Every mutation is serialized by an in-process re-entrant lock and an advisory
cross-process file lock. A numbered JSON revision is written atomically before
the current `object_catalog.json` manifest is replaced. This lets Flask edits
and queued conversion workers share one catalogue without lost updates; bundle
publication uses the same lock as deletion. Permanent deletion commits the
tombstone manifest before removing the now-unreferenced asset directory, so an
interrupted cleanup can leave only an unreferenced directory, never a live
catalogue record pointing at removed assets. Persisted pending evidence then
makes cleanup explicit and retryable.

## JSON export and import

`GET /workpieces/catalog/export` downloads the catalogue manifest without its
machine-local absolute root. It includes records, metadata, lifecycle state,
tombstones, relative asset references, sizes, and hashes.

This export is **metadata-only**. CAD, canonical PLY, and PNG bytes are not
embedded in the JSON. `POST /workpieces/catalog/import` therefore does not
create missing workpieces or fabricate/overwrite geometry. It merges only the
six editable metadata fields into entries whose catalogue UUID and managed
assets already exist locally. Optional imported `obj_id`, source hash, and
canonical hash values must agree with local immutable identity. The response
separates `updated`, `unchanged`, and `skipped_missing_assets` UUIDs.
Metadata export remains available for recovery when an asset is missing or
corrupt; import continues with intact entries and reports affected entries in
`skipped_missing_assets` instead of aborting the whole merge.

Consequently, JSON export/import is suitable for labels and classification,
not for a full binary backup or one-file host migration. Preserve the complete
managed `object_catalog/` tree through the site's normal filesystem backup,
while no catalogue mutation or import job is active, when CAD/texture
portability is required. A supported self-contained binary bundle format is not
currently provided.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/workpieces/status` | PoseTemplateCreator availability, formats, limits, root, and counts |
| `GET` | `/workpieces/catalog` | List the catalogue with pose-template usage summaries |
| `GET` | `/workpieces/catalog/<catalog_uuid>` | Read one workpiece and its usage summary |
| `POST` | `/workpieces/catalog/upload` | Queue multipart CAD/optional texture import and metadata |
| `PATCH` | `/workpieces/catalog/<catalog_uuid>` | Edit name/alias/description/tags/groups/attributes |
| `POST` | `/workpieces/catalog/<catalog_uuid>/unit-corrections` | Queue a confirmed, revision-checked metre/mm correction for an archived workpiece |
| `POST` | `/workpieces/catalog/<catalog_uuid>/archive` | Reversibly archive one workpiece |
| `POST` | `/workpieces/catalog/<catalog_uuid>/restore` | Restore one archived workpiece |
| `DELETE` | `/workpieces/catalog/<catalog_uuid>` | Permanently delete with JSON `{"confirm": true}` after all guards pass |
| `GET` | `/workpieces/catalog/<catalog_uuid>/assets/<kind>` | Hash-verify and serve `source`, `canonical_ply`, or optional `texture` |
| `GET` | `/workpieces/catalog/export` | Download metadata-only `object_catalog.json` |
| `POST` | `/workpieces/catalog/import` | Merge a multipart `catalog` JSON file, at most 16 MiB |

Append `?download=true` to an asset URL to request a download; source CAD is
always served as an attachment. Asset resolution checks containment, byte size,
and SHA-256 before serving the file.

Catalogue management is available only through `/workpieces`; template
preview, generation, library, and run selection remain under
`/pose-templates`.

## Pose-template and run snapshots

The Pose Templates page reads the same catalogue and offers only active
workpieces. Its filter covers names, aliases, descriptions, tags, and groups.
Preview and generation re-check catalogue state on the server.

Generating an immutable pose-template bundle snapshots each chosen workpiece's
stable identity, canonical geometry hash, optional texture hash, exact posed
slice evidence, copied canonical PLY/texture assets, and a hash-verified bounded
card thumbnail. Library and run-selection grids fetch only that thumbnail;
selecting one version may then fetch its full immutable preview for interactive
3D inspection. A visible **Simplified** label means thumbnail contours or
points were reduced, never that the PDF or saved geometry changed. New bounded
manifests omit duplicate raw contour arrays from instance metadata; the exact
hash-verified preview retains them. List/card reads therefore avoid hashing
unrelated PDFs and meshes, while each explicit preview, PDF, or asset read
verifies only its requested declared artifact. Selecting the bundle for a run
still performs strict whole-tree validation, copies the complete bundle below
the run's `processed/` directory, and records its tree hash. Later catalogue
relabelling, archive, or deletion cannot silently change an existing template
or selected run.
