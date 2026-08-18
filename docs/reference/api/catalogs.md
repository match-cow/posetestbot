# Workpieces and pose templates API

The Workpiece Catalogue is a mutable reusable library. Pose-template bundles
are immutable published library objects. Dataset runs snapshot selected
templates and placements so later library changes cannot modify old runs.

## Workpiece Catalogue

| Method and path | Contract |
| --- | --- |
| `GET /workpieces/status` | Inspect catalogue and conversion backend status |
| `GET /workpieces/catalog` | List public workpiece records and template usage |
| `GET /workpieces/catalog/<catalog_uuid>` | Return one record, revisions, assets, and usage |
| `POST /workpieces/catalog/upload` | Multipart CAD/mesh upload; queues inspection/conversion |
| `PATCH /workpieces/catalog/<catalog_uuid>` | Update editable metadata with validation |
| `POST /workpieces/catalog/<catalog_uuid>/<action>` | Apply allow-listed lifecycle action such as archive/restore |
| `POST /workpieces/catalog/<catalog_uuid>/unit-corrections` | Create a new canonical geometry revision after archive, confirmation, provenance, and expected hash/revision checks |
| `GET /workpieces/catalog/<catalog_uuid>/assets/<kind>` | Download an allow-listed managed asset |
| `GET /workpieces/catalog/export` | Download metadata-only `object_catalog.v1` JSON |
| `POST /workpieces/catalog/import` | Import metadata; records missing managed binary assets as skipped |
| `DELETE /workpieces/catalog/<catalog_uuid>` | Confirmed retirement/tombstone and queued asset cleanup after template-reference checks |

Catalogue mutations are serialized across threads/processes and atomically
publish numbered manifest revisions. UUID and BOP `obj_id` values are never
reused. JSON export does not embed CAD, canonical PLY, or textures.

See [Workpiece Catalogue](../../WORKPIECE_CATALOGUE.md) for upload fields,
revision rules, and deletion recovery.

## Orientation analysis

| Method and path | Contract |
| --- | --- |
| `GET /pose-templates/workpieces/<catalog_uuid>/orientations` | Load orientation analysis bound to canonical geometry hash/revision |
| `POST /pose-templates/workpieces/<catalog_uuid>/orientations` | Queue reproducible stable-orientation analysis |
| `GET /pose-templates/workpieces/<catalog_uuid>/orientation-thumbnail` | Return the bounded derived thumbnail cache |

Analysis and thumbnails are mutable reproducible caches, not immutable
catalogue assets.

## Pose-template authoring and library

| Method and path | Contract |
| --- | --- |
| `GET /pose-templates/status` | Inspect source/runtime and library status |
| `POST /pose-templates/preview` | Queue exact slicing/preview validation |
| `GET /pose-templates/preview/<request_id>` | Poll preview result |
| `POST /pose-templates/validate` | Compatibility alias for preview |
| `GET /pose-templates/validate/<request_id>` | Compatibility result alias |
| `POST /pose-templates/generate` | Publish an immutable validated bundle |
| `GET /pose-templates/library` | List active/archived global bundles |
| `GET /pose-templates/library/<template_uuid>` | Return one bundle and lifecycle state |
| `POST /pose-templates/library/<template_uuid>/<action>` | Apply an allow-listed archive/restore action |
| `DELETE /pose-templates/library/<template_uuid>` | Tombstone a confirmed bundle and queue asset cleanup |
| `GET /pose-templates/library/<template_uuid>/preview` | Return exact preview JSON |
| `GET /pose-templates/library/<template_uuid>/thumbnail` | Return bounded thumbnail JSON/image response |
| `GET /pose-templates/library/<template_uuid>/download/<kind>` | Download an allow-listed bundle artifact |
| `GET …/assets/<instance_uuid>/<kind>` | Return an allow-listed per-instance asset; both supported URL aliases are in the route index |

Deleted template UUIDs are never reused. Existing run snapshots remain
independent of library retirement.

## Run-owned selection

| Method and path | Contract |
| --- | --- |
| `GET /pose-templates/runs/selection?run_root=…` | Read selected immutable bundle, instances, placements, and transaction state |
| `POST /pose-templates/runs/selection` | Atomically replace run selection and `object_instances.json` |
| `POST /pose-templates/runs/placement` | Compatibility alias for selection/placement update |

Replacement uses a durable hidden transaction journal so recovery cannot
expose a half-updated run. See
[Pose templates and object GT](../../POSETEMPLATECREATOR_OBJECT_GT.md).
