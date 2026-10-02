# Robot consistency (IPD)

**Inspect → BOP Evaluation** calculates robot consistency alongside the pinned
official BOP19 scores in the same CPU/disk background job. It consumes an
immutable standard BOP19 result and the selected run's annotated export. It
does not run an estimator, contact hardware, or change the exported dataset.

The definition follows
[IPD's evaluator at revision 75dfe72](https://github.com/intrinsic-ai/ipd/blob/75dfe72e2f194f2a0e8a82bd8268fbec09205567/src/intrinsic_ipd/evaluator.py)
and its [instance matcher](https://github.com/intrinsic-ai/ipd/blob/75dfe72e2f194f2a0e8a82bd8268fbec09205567/src/intrinsic_ipd/matcher.py).
IPD moves gripper-mounted objects past fixed cameras. PoseTestBot moves
eye-in-hand cameras past workpieces fixed in the template base. Both measure
the variation of estimated object poses in the frame where objects stay fixed.

## Calculation

For every sensor scene and stable workpiece UUID:

1. Associate unordered predictions with GT instances using translation-distance
   Hungarian matching, with IPD's default strict **less than 100 mm** threshold.
   Association is one-to-one; result order and confidence do not define identity.
   GT translations supply only coarse association, not the reference pose used
   to measure consistency. GT poses also validate that the recorded transforms
   describe workpieces fixed in the declared reference frame.
2. Form `template_base_from_camera = template_base_from_flange ×
   flange_from_camera`, using the matched KUKA pose and the calibration snapshot
   embedded in the export. XYZ translations are millimetres; ABC angles are
   radians. Sparse source frame IDs are resolved through the current frame map
   and original GT frame bindings, rather than equated with BOP image IDs.
3. Transform each matched estimated object pose into template base, and reduce
   declared object symmetries using IPD's identity reference convention.
4. Average those transforms **elementwise**, as IPD does. The mean rotation is
   deliberately not projected back to a rigid rotation.
5. Transform that mean back into each camera frame, reduce symmetry, and compare
   each corresponding evaluation-model vertex with the predicted vertex.

**MVD** takes the maximum vertex displacement in each matched view. **ADD**
takes the mean vertex displacement. Both average over matched views to produce
a per-sensor/per-instance score. The aggregate is the equal-weight mean of
available sensor/instance scores. All `models_eval` PLY vertices are used,
without sampling, and distances are reported in **millimetres; lower is better**.
The metric IDs are `robot_consistency_mvd` and `robot_consistency_add`.
These are additional IPD-derived values, not official BOP recall scores.

Symmetry metadata comes from `models_eval/models_info.json`. Discrete rigid
symmetries, continuous rotation axes (including offsets), and continuous axes
sharing a center are supported. Origin-centered coordinate-axis gauges follow
IPD; arbitrary BOP axes and nonzero offsets extend that convention to the BOP
metadata contract. Undeclared symmetries are treated as asymmetric, as for the
existing BOP evaluation.

## Evidence and unavailable scores

Submission freezes `robot_consistency_inputs.json` inside the evaluation's
directory. It binds the export's calibration snapshot, current v3 frame map,
v1 instance map, original GT provenance, matched robot poses, and prepared
camera-pose NPY by SHA-256. Prepared camera translations are converted from
metres to millimetres and checked against the independently composed robot
and hand-eye transforms. Evidence paths remain below the selected run and
cannot use symlinks. The worker verifies the hashes before and after metrics.

Static cameras, missing source evidence, fewer than two matched predictions
for an instance, or identical matched camera viewpoints produce an explicit
**unavailable** reason. No zero or NaN score is fabricated. BOP metrics remain
available when robot consistency has no qualifying evidence. Corrupt,
contradictory, changed, unsafe, or unsupported evidence fails closed.

Missing estimates and threshold-rejected estimates do not enter the mean or
the vertex score. The report separately retains eligible instance views,
matched views, matching coverage, prediction count, and unmatched predictions.
A partial report identifies unavailable tracks and excluded scenes. These
counts include annotated instances for the selected BOP object/image target
pairs, not all raw captured frames. Every annotated instance can participate
in IPD's coarse association, including occluded instances; this count can
differ from BOP's visibility-qualified `inst_count`.
Tracks never combine different sensor scenes. A verified sensor-scoped result
uses the same selected-scene inventory for both BOP and robot consistency.

`robot_consistency.json` retains every instance score, synthesized reference
mean, per-view errors, implementation/source revisions, and dataset, result,
and frozen-input hashes. The ordinary `report.json` adds the two available
metric values plus a compact `robot_consistency` summary. Setup/history APIs
expose that summary, with at most 200 tracks; the UI initially shows ten.
All outputs stay below
`processed/bop_evaluation/evaluations/<evaluation_id>/`.
Existing completed reports remain intact; queue a new evaluation to add robot
consistency evidence for an older result.

## Interpretation

Robot consistency measures repeatability across robot-driven viewpoints.
A constant pose bias in the fixed frame can produce **zero consistency error**
while absolute accuracy is poor. Missed detections can also lower the score
by removing difficult views; compare matching coverage and evaluated tracks.
Robot/hand-eye calibration and viewpoint diversity affect the result. Small
nonzero motion is retained rather than rejected by a conservative diversity
margin. Use the official BOP scores alongside consistency when judging accuracy.

The deterministic GT perturbation source remains test-only. Its robot
consistency values verify the evaluation path; they do not measure an estimator.

## Offline comparison with saved ground truth

After completing the normal Inspect evaluations, use
`scripts/compare_bop_consistency_gt.py` to compare their consistency evidence
with the same estimates' saved GT vertex errors. The first evaluation is the
baseline. Repeat `--evaluation LABEL RUN EVALUATION_ID` for each condition and
choose one input run as `--output-run`:

```bash
uv run python scripts/compare_bop_consistency_gt.py \
  --evaluation "Without occlusion" /path/to/baseline evaluation-0123456789ab \
  --evaluation "Occlusion" /path/to/occlusion evaluation-abcdef012345 \
  --output-run /path/to/baseline --title "Occlusion comparison"
```

This is an offline Inspect evidence worker, not a new processing stage or
estimator. For supervised background execution, submit that command through
`LocalJobRunner` with `cpu` and `disk_io` resources and the output run scope.
Return to **Inspect → BOP Evaluation** for the source evaluations or **Jobs**
for their execution status. The CLI prints the completed HTML path.

The comparison currently requires one unambiguous fixed instance per sensor
scene and at most one estimate per selected target. Conditions must share
sensor identities, instance UUIDs and exact evaluation geometry. It rejects
changed datasets, depth content, results, robot inputs or report hashes;
reproduces the retained RC scores and per-frame errors; and checks that feeding
GT poses through RC yields numerical zero. For models without declared
symmetries, it also verifies GT MVD against the official toolkit's per-frame
MSSD output. All source hashes are frozen in the
comparison request. No source artifact is replaced.

Outputs live under the selected input run's
`processed/bop_evaluation/comparisons/<comparison_id>/`: `request.json`,
`comparison.json`, `frames.csv`, `index.html`, scientific PNG/SVG figures and
`manifest.json`. The desktop HTML embeds its data and selected RGB/pose-overlay
examples and works offline without external scripts. Downloadable scientific
figures remain beside it. `--reference-frame` chooses the shared BOP image ID
and must exist in every condition for each compared scene. Otherwise each scene
uses an ID one quarter through the intersection of its conditions' selected
frames. A missing estimate keeps that frame visible with GT alone; it never
substitutes a different image. These are separate captures of a repeated
trajectory, so a common image ID does not prove identical physical viewpoints;
review the retained paired-geometry differences when comparing them.

GT MVD/ADD uses the same corresponding evaluation-model vertices and IPD
symmetry convention as RC, but compares the estimate directly with retained
GT. It is derived diagnostic evidence, distinct from official BOP recall.
GT errors retain every prediction, including 100 mm association rejections;
rejected and missing estimates have **no RC value**, never a zero. The page
provides per-camera and per-condition statistics, scatter plots, trajectory
plots, Pearson/Spearman correlations, ordinary-frame correlations (GT error
below 20 mm), matched-subset means within consecutive 30-eligible-frame blocks,
and adjustable threshold agreement
counts. The default 10 mm threshold illustrates disagreements; it is not a
BOP19 acceptance rule. Correlations are descriptive because adjacent frames
are dependent and the mean reference uses the evaluated sequence itself.

The source GT and RC share the robot/hand-eye calibration chain. Agreement
therefore assesses the **saved GT**, not independent physical metrology.
Estimator mask/initialization conditions and repeated-trajectory differences
must be considered when interpreting a comparison. Dark foreground occluders
with invalid captured depth can remain inside depth-derived visible masks;
the stored `visib_fract` is not an independent physical-occlusion measurement.
