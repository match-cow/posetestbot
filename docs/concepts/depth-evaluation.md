# Depth quality and surface finishes

**Inspect → Depth Evaluation** compares captured optical-axis depth with opaque
geometric surfaces rendered from existing calibrated GT. It is an Inspect-only
derived view, outside acquisition orchestration, and never runs an estimator,
commands hardware, registers surfaces, or corrects GT from measured depth.

## Operator workflow

1. Complete the dataset workflow, export BOP, and generate GT in `pose` or
   `pose_and_masks` mode. Depth readiness requires no result CSV, BOP19 target
   list, visible masks, or visibility annotations.
2. Open **New evaluation**, select the frozen geometry and cameras, and enter a finish label
   for the whole run. Labels and settings are browser drafts until submission,
   then become immutable evaluation metadata. Metric settings and reference
   evidence are expandable beside the form. The reference renderer accepts
   rectified cameras and native cameras with zero lens distortion. Use a
   rectified BOP export for each unavailable camera. Other compatible cameras
   in the run remain selectable.
3. Queue evaluation. CPU/disk work continues after navigation; **Jobs** provides
   process status, logs, and cancellation. **Results & videos** opens the latest
   completed study, with a searchable study list and **Overview**, **Frames**,
   **Metrics**, **Videos**, and **Evidence** views. The overview shows exact
   completeness/error summaries and camera/geometry breakdowns. Frame review
   shows RGB beside a selectable depth/error/missing map, filtered by camera.
   Download the research bundle from the result header.
4. Open **Compare finishes** and add completed evaluations. The active run owns it and must
   be one input. Inputs need a common canonical geometry SHA-256 and identical
   metric settings. Names and BOP object IDs never establish compatibility.
   Conflicting finish labels for the same run are rejected.
5. Inspect common distance/incidence bins, pixel counts, contributing frame
   counts, and unmatched coverage. Runs and sensors remain separate series;
   no frame pairing or pooled finish ranking is produced. Empty common support
   is explicitly unavailable. Return to the dataset workflow for new captures
   or BOP Evaluation for pose accuracy.

## Quality videos

In a completed study, open **Videos**, select one camera/finish series, and
choose **Generate MP4**. The overview video shows RGB, measured depth, reference
depth, and signed error together. Single-view measured-depth, signed-error,
and missing-return videos are also available. Every video retains the study's
fixed scales, missing-depth/outside-object colors, camera identity, finish,
frame identity, and interior completeness counts. Comparisons keep each input
and camera separate and use the comparison's shared scales.

Choose 1–60 frames/second, every nth frame (1–120), and an output width of 960,
1280, or 1920 pixels. The form estimates output frame count and duration before
submission. Frames follow ascending BOP image ID within the selected series.
Playback uses a constant frame rate; it does not reproduce capture timestamps.
The full series is included by default. Changing FPS changes playback speed;
increasing the frame step shortens the review by sampling the retained series.

Videos are derived from completed retained images and metrics. They do not
rerun BlenderProc or require the original capture to remain available. Encoding
uses FFmpeg/libx264, the same optional runtime as Pose Results MP4 export.
Missing encoder support disables generation with a visible explanation; frame
inspection and research downloads remain available.
If video options fail to load, the panel shows an explanation and a **Retry
video options** action. A missing video API after a console update asks for a
backend restart through the **Restart** menu; it does not display an empty
camera selection or report zero frames.

Video generation is a cancelable CPU/disk job and continues after navigation.
The selected completed export has a browser player, **Download MP4**, and a
settings/provenance link. Exports survive reload and appear in the study's
paginated video history. Failed/canceled exports expose progress and logs,
never playable partials.

Video artifacts live below `processed/bop_evaluation/depth/videos/<id>/`.
Immutable requests bind the source publication hash, selected frame/media
hashes, scales, encoding settings, and implementation hashes. Workers validate
sources before encoding and again before atomically publishing `complete/`.
Published MP4 reads verify their content hash and support HTTP range requests
for seeking. Existing study publications and research ZIPs remain immutable.

## Reference and metrics

Pinned **BlenderProc 2.8.0** renders at each frame's exported resolution and
intrinsics on CPU, using opaque geometry, flat normals, one sample, no denoiser,
and unsmoothed optical-axis depth. All known GT objects render together so the
nearest surface determines instance visibility. Measured depth and existing
visible masks never determine eligibility. The
[renderer documentation](https://dlr-rm.github.io/BlenderProc/docs/tutorials/renderer.html)
distinguishes optical-axis depth from radial distance. Reference maps retain
optical-axis millimetres, OpenCV camera normals, and stable render-instance IDs.

Measured PNG values are multiplied by exported `depth_scale`. Finite positive
returns are valid. Signed error is **measured − reference**, in millimetres;
positive errors are farther than reference. No fitting or registration occurs.

| Metric | Definition |
| --- | --- |
| Completeness | Valid measured returns / reference object pixels |
| Missing returns | Reference pixels without finite positive measured depth |
| Bias, MAE, RMSE | Mean signed, mean absolute, root mean square error on valid returns |
| Median / P95 absolute error | Exact linear-interpolated quantiles on valid returns |
| Outlier rate | Valid returns with absolute error strictly above threshold / valid returns |
| Usable-depth coverage | Valid returns within threshold (inclusive) / all reference pixels |

Default thresholds are 1, 2, 5, and 10 mm. Full region, 2-pixel-eroded interior
(5 × 5 square; outside-image pixels treated as outside), and remaining boundary
are retained per frame, instance, sensor/geometry, sensor, and run. Entirely
missing depth gives zero completeness and null accuracy; no rendered visible
pixels gives unavailable metrics. Aggregate quantiles spill to disk and
partition a memory map. Geometry is reused; frames are processed one at a time.

Distance bins use Euclidean camera-to-reference-surface range calculated from
optical-axis depth and calibrated pixel rays. Incidence is the acute angle
between the flat normal and viewing ray. Defaults are 100 mm and 10°, anchored
at zero; intervals are left-inclusive and right-exclusive, with 90° included
in the last bin. Comparisons restrict each geometry/region to support common
to all selected evaluation/sensor series. Counts and metrics stay separate.

Frame views show RGB, measured/reference depth, signed error, and missing
returns. Magenta means missing depth; dark grey means outside selected objects.
Error scales use ± the largest threshold. Comparisons regenerate media with a
common depth maximum. Numeric NPZ maps preserve full precision.

## Evidence and runtime

The adapter reuses containment, immutable pose-provenance, calibration, and
frame-binding validators without BOP19 readiness. Canonical geometry hashes
come from run-owned snapshots and immutable instance maps. Submission binds
filesystem identities, including missing depth maps. Workers verify the
frozen input inventory at startup and before publication. They check each
frame's RGB and depth hashes during measurement. Requests and renderer scripts
are also bound. Source changes fail closed. Completed outputs retain their
maps and RGB independently of later source changes.

Reports freeze the metric revision and source-code hashes. Comparison inputs
must share the metric revision as well as settings. Each retained frame records
its reference intrinsics, poses, and render-ID-to-instance mapping, so numeric
maps remain interpretable in exported research bundles. Renderer provenance
includes the actual Blender version and CPU/sample/normal-axis settings.
New studies retain each selected camera's projection, calibration profile,
resolution, and distortion coefficients. A retained study created before this
evidence was recorded can enter a new comparison when its original BOP export
manifest still matches its frozen hash and proves a compatible projection.

Artifacts live below
`processed/bop_evaluation/depth/{evaluations,comparisons}/<id>/`. Requests,
progress, and job identity remain outside `complete/`. Workers write `.work/`
then atomically rename it to `complete/` after validation. The publication
manifest binds the request and completed artifacts; reads verify hashes.
Failed/canceled jobs retain diagnostics and expose no partial downloads.
Comparisons bind input publication hashes and revalidate before completion.
Completed studies and videos are readable at the current approved run path
after a move. Requests preserve the original worker location for provenance.

The research ZIP includes offline standalone HTML with frame browsing,
PNG/SVG figures, CSV/JSON metrics, frame tables, numeric maps, settings,
provenance, and a bundle hash manifest. The external publication manifest also
hashes the ZIP. Source BOP, raw data, and acquisition config remain unchanged.

Provision pinned BlenderProc and its Blender installation before studies:

```bash
bash scripts/install.sh --with-blenderproc
```

BOP Toolkit is unnecessary for depth-only studies. Available calibration and
placement quality evidence is displayed beside results; conservative quality
margins do not block internally valid evidence. Assume controlled scenes
without unmodelled foreground occluders and existing calibrated GT. Temporal
repeatability, independent reference poses, and new pose metrics are deferred.
