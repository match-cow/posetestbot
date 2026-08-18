# PoseTemplateCreator Object Ground Truth

PoseTestBot uses the pinned PoseTemplateCreator backend to turn managed CAD
models into printable, immutable object-pose templates. This workflow creates
ground-truth inputs and a validated base BOP dataset; it does not run pose
estimators. When optional pose-plus-mask evidence is added, official metric
inspection is the separate run-scoped **Inspect → BOP Evaluation** path and
consumes that completed annotation-bearing export. Test-object upload and
lifecycle are owned by the separate **Workpiece Catalogue** page; see
[WORKPIECE_CATALOGUE.md](WORKPIECE_CATALOGUE.md) for its persistence and API
contract.

## Install and verify

Initialize both printable-source checkouts and validate the local environment:

```bash
bash scripts/install.sh --with-posegridgen --with-posetemplatecreator
bash scripts/install.sh --check-only --with-posetemplatecreator
```

PoseTemplateCreator must be clean and exactly at
`97ddb9b7b756912deb8c2d2d6dde186b461e5d9d`. If it is missing, dirty, or at a
different revision, existing catalogs, bundles, and run selections remain
browsable, and existing workpiece metadata remains editable, but new CAD
inspection/conversion, exact slicing, and generation are disabled. Generating
optional BOP ground-truth evidence requires BlenderProc 2.8.0. The
evaluation-compatible pose-plus-mask product additionally requires the pinned
official BOP Toolkit and its isolated runtime:

```bash
bash scripts/install.sh --with-blenderproc --with-bop-toolkit
```

## Coordinate contract

The nominal blue `pose_template` origin is 15 mm from the lower-left page
corner: +X points right, +Y points up, and +Z points out of the page. Printer
compensation moves and scales that dot with all other meaningful PDF content
about page centre; the footprint cards show its compensated physical position.
CAD and translations use millimetres. For each catalogue mesh,
PoseTemplateCreator ranks physically stable, grounded orientations and returns
an exact `source_to_placed` rigid transform. The editor then exposes only the
meaningful template-plane placement: X, Y, and rotation about +Z.

For a selected stable orientation, the authoritative transform is:

```text
pose_template_from_object =
    translate_xy * rotate_z * source_to_placed
```

Only current stable-orientation configurations and bundles are accepted. The
operator is never asked to reproduce arbitrary roll, pitch, or Z values by eye.

The operator-confirmed placement maps the blue dot into `template_base`:

```text
template_base_from_object =
    template_base_from_pose_template * pose_template_from_object
```

X/Y print scaling follows the pinned upstream renderer and corrects printable
template content about the physical page centre. It does not scale CAD, rigid
transforms, the PDF page boundary, or GT.

## Operator workflow

1. Open **Workpiece Catalogue**. Upload one PLY/STL/OBJ and optionally one PNG.
   Inspection and conversion run as local CPU/disk jobs; the page reports
   queued/running status and refreshes the catalogue at completion. Add or edit
   the name, alias, description, tags, groups, and custom attributes. Use the
   single orbitable bounded 3D preview and compact isometric cards to identify
   the object without loading its full canonical PLY. Archive is reversible;
   permanent deletion is available directly from either lifecycle state after
   explicit confirmation, and only when no pose-template bundle references the
   workpiece. If a unitless CAD file was interpreted at the
   wrong scale, archive it, inspect the before/after dimensions, and create an
   audited metre-to-millimetre (×1000) or millimetre-to-metre (÷1000) geometry
   revision. Restore it only after checking the corrected preview.
2. Open **Pose Templates** and filter the active workpieces by name, alias, tag,
   or group. Duplicate physical instances are allowed. Choose a ranked stable
   orientation using the same-scale, topology-aware recognition surface and
   exact base footprint, then add it to the page. This chooser uses the bounded
   high-detail mesh (up to 4,096 vertices and 8,192 faces), not the tiny
   printable-layout proxy. Drag or use arrow keys to position it, use the
   rotation handle, or enter exact X/Y/rotation values. PoseTestBot retains the
   upstream orientation ID, probability, grounded transform, slice height, and
   contours instead of accepting uploaded geometry in this workflow.
3. Select ISO paper/orientation and X/Y print compensation in percent. The
   upstream page contract fixes the nominal printable margin and blue origin at
   15 mm. Generate an
   immutable version only after the debounced server preview for the current
   editor state passes exact fit and geometry validation. Download the PDF and
   manifest; clone to make another immutable version. If a referenced
   workpiece now has a different geometry revision, cloning fails clearly and
   the operator must create a new template and review its stable orientation.
   Any active or archived global template version can also be permanently
   deleted after explicit confirmation. Its library entry disappears
   immediately; asset cleanup continues as a background job visible under
   **Jobs**. Run-owned copied snapshots remain intact.
4. Create or update the run in pose-template mode:

   ```bash
   uv run python scripts/create_run_config.py working_data/my_run \
     --intent dataset --annotation-mode none \
     --dataset-mode pose_template
   ```

5. Open **Workflow → Object dataset → Choose the pose template and
   placement**, select an active immutable version from its bounded
   footprint-preview card, and inspect the immutable objects in the single full
   interactive 3D scene. A **Simplified** badge reports card-only contour/point
   reduction; it never changes the printable or GT geometry. Enter the measured
   full template-to-`template_base` placement, identify the operator, and
   explicitly confirm it. Changing the version or any placement value clears
   that confirmation; identity defaults are not implicitly trusted.
6. Return to the guided dataset workflow. Step 5, **Process frames and create
   the base BOP export**, performs synchronization and quality verification,
   revalidates calibration, rectifies RGB-D frames, and writes the required
   image/model BOP dataset.
7. Step 6, **Add optional BOP ground-truth evidence**, can then generate
   **Plain pose ground truth** or **Pose + object masks and ROI**. BlenderProc
   2.8.0 validates the calibrated scene and derives pose GT; the complete
   product then uses the pinned BOP Toolkit with captured depth for
   full/visible masks, ROI, and visibility evidence. The base export remains
   valid when this optional step is skipped. No camera or robot operation is
   initiated by catalogue, template, selection, preparation, annotation, or
   export actions.

## Artifacts and immutability

- Global Workpiece Catalogue:
  `working_data/object_catalog/object_catalog.json`, numbered revisions, and
  UUID-addressed retained source/canonical/texture assets. Canonical geometry
  revisions live below each object's `derived/` directory; a current
  `pose_template_orientation_analysis.json` cache and its separately bounded
  `pose_template_orientation_thumbnail.json` card cache sit beside the
  canonical PLY. Both are reproducible, hash/revision-bound derivatives rather
  than immutable catalogue assets. The selected Workpiece Catalogue detail
  reads the exact canonical PLY, while cards use the thumbnail's welded,
  topology-scored bounded surface (quadric-decimated or spatially clustered
  only when necessary) in the authored orientation. Approximation metadata
  identifies the chosen strategy and warns when topology could not fit the card
  budget. Stable-orientation transforms remain explicit choices in the
  template editor. Editable metadata lives beside stable catalogue UUID and
  BOP `obj_id` identity.
- Global library: `working_data/pose_templates/<template_uuid>/`, containing
  `pose_template_bundle.json`, exact preview data, a hash-verified bounded
  `pose_template_thumbnail.json`, asset snapshots, and PDF. The thumbnail keeps
  every instance's largest compensated contour, then admits secondary contours
  round-robin, with hard limits of 400 contours, 4096 total points, and 48
  points per contour. Its approximation record reports every source/included
  count. A current bundle must contain this declared, hash-verified thumbnail;
  missing or oversized thumbnail evidence fails closed. `GET /pose-templates/library` returns
  metadata-only summaries; exact contours and preview meshes are available only
  from the explicitly requested detail/full-preview endpoints. New manifests
  omit the duplicate raw `nominal_contours` and `compensated_contours` arrays
  from instance records; authoritative exact contours remain hash-verified in
  `pose_template_preview.json`, so this reduces synchronous metadata cost
  without changing the PDF, placement, or GT.
  Permanent deletion atomically removes the global UUID directory from library
  visibility before a disk job cleans its files. The job continues after
  navigation and remains visible under **Jobs**. A compact retained tombstone
  below `working_data/pose_templates/.deleted/` prevents UUID reuse and records
  retryable cleanup status; run-owned snapshots do not depend on the deleted
  source directory.
- Run selection: `pose_template_selection.json` plus the copied bundle at
  `processed/pose_template_selection/`. A hidden
  `.pose_template_selection.transaction.json` exists only while a replacement
  transaction or its cleanup is recoverable.
- Prepared identity: `object_instances.json` and per-sensor BlenderProc
  `objects.json`/`posetestbot_render_instances.json`.
- Optional run-owned annotation status and renderer provenance:
  `processed/bop_annotations/generation_report.json`.
- BOP provenance: `bop/posetestbot_pose_template.json` and
  `bop/posetestbot_instance_map.json` beside standards-compatible BOP files.
  The pose-plus-mask product also contains standard `scene_gt.json`,
  `scene_gt_info.json`, full-frame `mask/`, and `mask_visib/` files.
- Optional Inspect-only result registrations and metric reports:
  `processed/bop_evaluation/`. These are derived consumers of the finished BOP
  dataset and never mutate the template selection or raw capture.

Catalogue and library archives are reversible. Catalogue JSON export/import is
metadata-only: JSON never embeds CAD or texture bytes, and import skips records
whose matching local UUID assets are absent. A generated immutable bundle
snapshots its selected workpieces and assets, and a selected run owns a complete
copy of that bundle, so later catalogue metadata or archive actions do not
change either snapshot. Selection replacement is blocked after dependent
object-instance, render, mask, or BOP artifacts exist; the UI/API reports the
exact blocking paths.

Ordinary library cards/details read the bounded, self-hashed manifest and do
not hash every immutable PDF, preview, mesh, and texture. A thumbnail, full
preview, PDF, or individual instance asset request verifies the manifest plus
only the requested declared artifact. Oversized or malformed manifests are
rejected. Authoritative operations remain strict: run selection,
catalogue-reference checks before
permanent deletion, and explicit whole-bundle validation reject missing,
modified, undeclared, or symlinked tree entries.

Template slicing, preview construction, PDF rendering, and asset copying run
outside the catalogue mutation lock. Publication takes that lock only long
enough to re-check every selected canonical geometry/texture identity and
atomically expose the staged bundle. A unit correction, archive, or deletion
that wins the race therefore causes stale publication to fail and discard its
stage, without blocking ordinary catalogue work for the full render.

## Validation and recovery

Validate the current base export and optional annotation setup directly:

```bash
uv run python scripts/process_dataset.py working_data/my_run
curl --fail-with-body --get http://127.0.0.1:5000/bop/annotations/setup \
  --data-urlencode run_root=working_data/my_run
```

The fixed processing recipe cross-checks selection, prepared geometry,
calibration, model hashes, and provenance before writing the current base
export. Optional annotation readiness then verifies its mode-specific inputs
and toolkit identity. Preserve raw capture data; retry derived processing after
correcting a blocker.

Selection creation and replacement hold the template-library lock while they
strictly validate and snapshot the chosen active bundle. The run-local reader
then cross-checks the selection record against that verified copy, including
UUIDs, catalogue/BOP identities, assets, transforms, print compensation,
configuration hash, frame semantics, timestamp, confirmation type, and
operator provenance. It fails closed on path traversal, symlinks, partial
trees, or inconsistent fields.

Promotion of the copied bundle, `pose_template_selection.json`, and any updated
`run_config.json` is one staged transaction. A durable prepared journal is
written before live paths move. On the next selection-locked access, an
interrupted prepared transaction restores the exact prior artifacts; a
committed transaction finishes the remaining stage/backup cleanup. The journal
accepts only the three managed run-local targets and rejects unsafe or
incomplete recovery metadata.
