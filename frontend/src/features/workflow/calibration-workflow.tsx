import { useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, ChevronDown, Grid3X3, LoaderCircle, Save, TriangleAlert } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { HelpTip } from "@/components/help-tip"
import { CalibrationArrangementCard, effectiveCalibrationTargetMountingFrame, POSE_TEMPLATE_BASE_SUNRISE_PATH, type CalibrationArrangement, type CalibrationMode, type CalibrationTargetMountingFrame } from "@/components/calibration-arrangement"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, errorMessage, query } from "@/lib/api"
import { useOperator } from "@/providers/operator-provider"

type SynchronizationPolicy = "auto_offset" | "fixed_zero"
const REQUIRED_AUTO_TIMING_IMPLEMENTATION_REVISION = "constant_latency_nearest_pose_motion_lomo_warn_keep_zero.v5"
type ImageCoverageThresholds = {
  image_coverage_tail_support_views?: number
  min_image_centroid_x_span_ratio?: number
  min_image_centroid_y_span_ratio?: number
  min_image_centroid_hull_area_ratio?: number
}
type Camera = { sensor_key: string; sensor_name: string; display_name: string; sensor_type: string; device_id: string; current_mounting_mode?: string | null }
type SavedTarget = { target_id: string; display_name: string; valid: boolean; selected?: boolean; selected_placement?: { mode: string; mounting_frame?: CalibrationTargetMountingFrame } | null }
type Setup = {
  cameras: Camera[]
  unavailable_cameras: Array<Camera & { errors: string[] }>
  saved_targets: SavedTarget[]
  modes: Array<{ id: CalibrationMode; label: string; primary_transform: string; target_mounting: string }>
  solver: {
    synchronization: {
      implementation_revision: string
      default_policy: SynchronizationPolicy
      policies: Array<{ id: SynchronizationPolicy; label: string; description: string }>
      search: {
        minimum_robot_pose_time_offset_ms: number
        maximum_robot_pose_time_offset_ms: number
        step_ms: number
        max_nearest_pose_delta_ms?: number
        warning_nearest_pose_delta_ms?: number
        warning_absolute_robot_pose_time_offset_ms?: number
        time_offset_failure_policy: "warn_keep_zero"
        minimum_motion_count_per_cross_validation_fold?: number
        maximum_leave_one_motion_out_search_adjusted_sign_p_value?: number
      }
    }
    thresholds: {
      min_pnp_common_inliers: number
      min_pnp_common_inlier_ratio: number
      max_pnp_all_point_mean_reprojection_error_px: number
      min_pnp_supported_markers: number
      min_pnp_grid_rows: number
      min_pnp_grid_columns: number
      min_pnp_clutter_supported_markers?: number
      min_pnp_clutter_grid_rows?: number
      min_pnp_clutter_grid_columns?: number
      min_accepted_views: number
      min_coverage_cells: number
      image_coverage_tail_support_views?: number
      min_image_centroid_x_span_ratio?: number
      min_image_centroid_y_span_ratio?: number
      min_image_centroid_hull_area_ratio?: number
      image_coverage_by_mode?: Partial<Record<CalibrationMode, ImageCoverageThresholds>>
      max_per_view_reprojection_error_px: number
      max_intrinsic_rms_reprojection_error_px: number
      min_motion_poses: number
      min_translation_span_mm: number
      min_rotation_span_deg: number
      min_rotation_axis_second_to_first_ratio: number
      max_nearest_pose_delta_ms: number
      warning_nearest_pose_delta_ms?: number
    }
  }
  latest_attempt: { attempt_id: string; status: string } | null
}
type Transform = { from: string; to: string; matrix: number[][]; rotation_quaternion_wxyz: number[]; translation_mm: number[] }
type Candidate = {
  candidate_id: string; profile_id?: string; pnp_method: string; extrinsic_method: string; algorithms: string[]
  status: "passing" | "failed" | "error"; validation_state: string; recommended?: boolean; score: number | null
  observation_count: number; inlier_count: number; outlier_count: number; outlier_ratio: number
  mean_reprojection_error_px?: number | null; primary_transform?: Transform; companion_transform?: Transform
  held_out_residuals?: { mean_translation_mm: number; median_translation_mm: number; mean_rotation_deg: number; median_rotation_deg: number }
  checks?: Array<{ name: string; status: string; actual?: unknown; threshold?: unknown }>
  error?: string
}
type CameraResult = Camera & { status: "passing" | "failed"; recommended_candidate_id: string | null; recommendation: Candidate | null; candidates: Candidate[] }
type IntrinsicCandidate = {
  profile_id: string
  source: { mode: string }
  quality: { status: string; accepted_view_count: number; coverage_cells: number[]; rms_reprojection_error_px: number | null }
}
type IntrinsicComparison = {
  sensor_key: string
  status: "manual_selected" | "factory_selected" | "existing_selected" | "unusable"
  selected_profile_id: string | null
  selection_reason: string
  factory_profile_id: string
  manual_profile_id: string | null
  manual_failure: null | { message: string; quality?: { reason?: string } }
  deltas: null | {
    focal_length_delta_px: number[]
    principal_point_delta_px: number[]
    max_abs_distortion_delta: number | null
  }
  candidates: IntrinsicCandidate[]
}
type ResidualSummary = {
  mean_translation_mm: number
  median_translation_mm: number
  max_translation_mm: number
  mean_rotation_deg: number
  median_rotation_deg: number
  max_rotation_deg: number
}
type TimeOffsetMetric = { residuals: ResidualSummary }
type TimeOffsetMotionMethod = {
  status: "ok" | "error"
  motion_count: number
  positive_motion_count: number
  material_motion_count: number
  positive_sign_p_value: number
  candidate_search_adjusted_positive_sign_p_value: number
  median_improvement: {
    absolute_translation_mm: number
    relative_translation: number | null
    rotation_change_deg: number
  }
}
type TimeOffsetMotionConsistency = {
  status: "ok" | "error"
  strategy: string
  motion_count: number
  candidate_search_adjustment: "bonferroni"
  candidate_search_hypothesis_count: number
  methods: Record<string, TimeOffsetMotionMethod>
  thresholds: {
    minimum_median_absolute_translation_mm: number
    minimum_median_relative_translation: number
    maximum_search_adjusted_positive_sign_p_value: number
  }
}
type TimeOffsetSensor = {
  sensor_key: string
  sensor_name?: string
  display_name?: string
  status: "applied" | "kept_zero" | "fixed_zero" | "failed"
  decision_reason: string
  selected_robot_pose_time_offset_ms: number
  selected_sync_delta_ms: number
  candidate_robot_pose_time_offset_ms: number
  evidence_strength: string
  warning_fallback_used?: boolean
  boundary_hit: boolean
  selection_extrinsic_method?: string
  improvement_evidence_strategy?: string
  split?: { motion_count: number; selected_observation_count: number; fold_motion_counts: Record<string, number> }
  cross_validation?: {
    zero_offset: TimeOffsetMetric
    candidate: TimeOffsetMetric
    improvement: { absolute_translation_mm: number; relative_translation: number | null; rotation_change_deg: number }
  }
  motion_consistency?: TimeOffsetMotionConsistency | null
  checks: Array<{ name: string; status: string; actual?: unknown; threshold?: unknown; warning_threshold?: unknown; failure_threshold?: unknown }>
  curve: Array<{ robot_pose_time_offset_ms: number; residuals: ResidualSummary }>
}
type TimeOffsetSearch = {
  implementation_revision: string
  policy: SynchronizationPolicy
  status: "complete" | "failed"
  sign_convention: { operator_equation: string; positive_operator_value: string; conversion: string }
  search: {
    minimum_robot_pose_time_offset_ms: number
    maximum_robot_pose_time_offset_ms: number
    step_ms: number
    max_nearest_pose_delta_ms?: number
    warning_nearest_pose_delta_ms?: number
    warning_absolute_robot_pose_time_offset_ms?: number
    time_offset_failure_policy: "warn_keep_zero"
  }
  warning_sensor_keys?: string[]
  warning_sensor_count?: number
  sensors: TimeOffsetSensor[]
}
type Attempt = {
  attempt_id: string
  request: { schema_version: string; mode: "eye_in_hand" | "eye_to_hand"; sensor_keys: string[]; target_id: string; synchronization_policy: SynchronizationPolicy }
  progress: { status: "queued" | "running" | "complete" | "failed"; message: string; phases: Array<{ id: string; label: string; status: "pending" | "running" | "complete" | "failed" }> }
  results: null | { status: "complete" | "partial" | "failed"; recommended_camera_count: number; failed_camera_count: number; results: CameraResult[] }
  intrinsic_comparison?: null | { policy: string; sensors: IntrinsicComparison[] }
  time_offset_search?: TimeOffsetSearch | null
  promotion: null | { status: "queued" | "running" | "promoted" | "failed"; job_id?: string; promoted_profile_ids?: string[]; error?: string }
}

export function CalibrationWorkflow({
  arrangement,
  referenceFramePath,
}: {
  arrangement: CalibrationArrangement
  referenceFramePath?: string | null
}) {
  const { selectedRun } = useOperator()
  const queryClient = useQueryClient()
  const setup = useQuery({
    queryKey: ["calibration", "setup", selectedRun],
    queryFn: () => api<Setup>(query("/calibration/setup", { run_root: selectedRun })),
    refetchInterval: (state) => state.state.data?.cameras.length === 0 ? 2_000 : false,
  })
  const [sensorSelection, setSensorSelection] = useState<{ runRoot: string; values: string[] } | null>(null)
  const [synchronizationSelection, setSynchronizationSelection] = useState<{ runRoot: string; value: SynchronizationPolicy } | null>(null)
  const [attemptSelection, setAttemptSelection] = useState<{ runRoot: string; attemptId: string } | null>(null)
  const [overrideSelection, setOverrideSelection] = useState<{ runRoot: string; attemptId: string; values: Record<string, string> } | null>(null)
  const selectedMode: CalibrationMode | null = arrangement.status === "ready" ? arrangement.mode : null
  const availableSensorKeys = setup.data?.cameras.map((camera) => camera.sensor_key) ?? []
  const sensorKeys = sensorSelection?.runRoot === selectedRun
    ? sensorSelection.values.filter((sensorKey) => availableSensorKeys.includes(sensorKey))
    : arrangement.status === "ready" ? availableSensorKeys : []
  const selectedTarget = setup.data?.saved_targets.find((target) => target.selected && target.valid) ?? null
  const effectiveTargetMountingFrame = effectiveCalibrationTargetMountingFrame(selectedTarget?.selected_placement)
  const targetMountingMatches = arrangement.status === "ready"
    && effectiveTargetMountingFrame === arrangement.mountingFrame
  const targetId = selectedTarget?.target_id ?? ""
  const synchronizationPolicy = synchronizationSelection?.runRoot === selectedRun
    ? synchronizationSelection.value
    : setup.data?.solver.synchronization.default_policy ?? "auto_offset"
  const backendTimingRevision = setup.data?.solver.synchronization.implementation_revision
  const autoTimingRuntimeReady = backendTimingRevision === REQUIRED_AUTO_TIMING_IMPLEMENTATION_REVISION
  const activeAttemptId = attemptSelection?.runRoot === selectedRun ? attemptSelection.attemptId : setup.data?.latest_attempt?.attempt_id ?? null

  const attempt = useQuery({
    queryKey: ["calibration", "attempt", selectedRun, activeAttemptId],
    queryFn: () => api<Attempt>(query(`/calibration/attempts/${activeAttemptId}`, { run_root: selectedRun })),
    enabled: Boolean(activeAttemptId),
    refetchInterval: (state) => {
      const value = state.state.data
      if (!value || ["queued", "running"].includes(value.progress.status)) return 700
      if (value.promotion && ["queued", "running"].includes(value.promotion.status)) return 700
      return false
    },
  })
  const recommendedOverrides = useMemo(() => Object.fromEntries(
    attempt.data?.results?.results.flatMap((result) => result.recommended_candidate_id ? [[result.sensor_key, result.recommended_candidate_id]] : []) ?? [],
  ), [attempt.data?.results?.results])
  const overrides = overrideSelection?.runRoot === selectedRun && overrideSelection.attemptId === activeAttemptId
    ? overrideSelection.values
    : recommendedOverrides

  const createAttempt = useMutation({
    mutationFn: () => {
      if (!selectedMode) throw new Error("Save one homogeneous camera mounting group in Workflow step 1 before analyzing the recording")
      if (!targetMountingMatches) throw new Error("Select the printed target with the physical mounting required by this camera group")
      if (synchronizationPolicy === "auto_offset" && !autoTimingRuntimeReady) {
        throw new Error("Restart the PoseTestBot backend before creating an Auto time-aligned attempt")
      }
      return api<{ attempt_id: string; job_id: string }>("/calibration/attempts", { method: "POST", body: JSON.stringify({ run_root: selectedRun, mode: selectedMode, sensor_keys: sensorKeys, target_id: targetId, synchronization_policy: synchronizationPolicy }) })
    },
    onSuccess: (value) => { setAttemptSelection({ runRoot: selectedRun, attemptId: value.attempt_id }); setOverrideSelection(null); toast.success("Calibration queued", { description: `Attempt ${value.attempt_id} · job ${value.job_id}` }); queryClient.invalidateQueries({ queryKey: ["calibration", "setup", selectedRun] }); queryClient.invalidateQueries({ queryKey: ["jobs"] }) },
    onError: (error) => toast.error("Calibration was not queued", { description: errorMessage(error) }),
  })
  const promote = useMutation({
    mutationFn: () => api<{ job_id: string }>(`/calibration/attempts/${activeAttemptId}/promote`, { method: "POST", body: JSON.stringify({ run_root: selectedRun, candidate_ids: overrides }) }),
    onSuccess: (value) => { toast.success("Calibration acceptance queued", { description: `Job ${value.job_id}` }); queryClient.invalidateQueries({ queryKey: ["calibration", "attempt", selectedRun, activeAttemptId] }); queryClient.invalidateQueries({ queryKey: ["jobs"] }) },
    onError: (error) => toast.error("Recommendations were not accepted", { description: errorMessage(error) }),
  })
  const canRun = Boolean(selectedMode) && sensorKeys.length > 0 && Boolean(targetId) && targetMountingMatches && !createAttempt.isPending
    && (synchronizationPolicy !== "auto_offset" || autoTimingRuntimeReady)
  const passingSelections = useMemo(() => attempt.data?.results?.results.filter((result) => {
    const selected = overrides[result.sensor_key]
    return result.candidates.some((candidate) => candidate.candidate_id === selected && candidate.status === "passing")
  }).length ?? 0, [attempt.data?.results, overrides])

  if (setup.isPending) return <Card><CardContent className="grid min-h-72 place-items-center text-sm text-muted-foreground"><span className="flex items-center"><LoaderCircle className="mr-2 size-4 animate-spin" />Loading calibration setup…</span></CardContent></Card>
  if (setup.isError || !setup.data) return <Card className="border-destructive/40"><CardContent className="py-8 text-sm text-destructive">{errorMessage(setup.error)}</CardContent></Card>
  const coverageThresholds = (
    selectedMode
      ? setup.data.solver.thresholds.image_coverage_by_mode?.[selectedMode]
      : undefined
  ) ?? setup.data.solver.thresholds

  return <div className="space-y-5" data-testid="calibration-workflow">
    <Card><CardHeader><CardTitle>Analyze this calibration recording</CardTitle><CardDescription>The physical setup is read from Workflow step 1 and the exact printed grid selected in step 2. PoseTestBot compares supported solutions, but nothing becomes reusable until you review and save it.</CardDescription></CardHeader><CardContent className="space-y-6">
      <div className="space-y-3"><div className="text-sm font-semibold">Recorded physical arrangement <span className="ml-1 text-xs font-normal text-destructive">Required</span></div><CalibrationArrangementCard arrangement={arrangement} editHref="/workflow/calibration?step=configure" editLabel="Edit cameras in step 1" testId="calibration-analysis-arrangement" /><p className="text-xs text-muted-foreground">The internal solver is derived from the run-owned camera mounting and cannot be overridden here. For static cameras, robot poses use <code>{referenceFramePath ?? POSE_TEMPLATE_BASE_SUNRISE_PATH}</code> and the primary published result is camera → PoseTemplateBase; the moving grid attachment is supporting evidence. Static and robot-mounted cameras require separate recordings, but their profiles can be combined later in an object-dataset run.</p></div>
      <div className="grid gap-5 md:grid-cols-2"><fieldset className="space-y-3"><legend className="text-sm font-semibold">Cameras from this recording</legend><p className="text-xs text-muted-foreground">Select cameras within the one mounting group recorded for this run.</p><div className="space-y-2 rounded-lg border p-3">{setup.data.cameras.map((camera) => {
        const compatible = arrangement.status === "ready"
        const mountingLabel = camera.current_mounting_mode === "static" ? "Static" : camera.current_mounting_mode === "eye_in_hand" ? "Robot-mounted" : "Mounting not recorded"
        return <Label key={camera.sensor_key} className={`flex items-start gap-3 rounded-md p-2 ${compatible ? "cursor-pointer hover:bg-muted/50" : "opacity-55"}`}><Checkbox disabled={!compatible} checked={compatible && sensorKeys.includes(camera.sensor_key)} onCheckedChange={(checked) => setSensorSelection({ runRoot: selectedRun, values: checked === true ? [...sensorKeys, camera.sensor_key] : sensorKeys.filter((key) => key !== camera.sensor_key) })} /><span><span className="block text-sm font-medium">{camera.display_name}</span><span className="block font-mono text-[10px] font-normal text-muted-foreground">{camera.sensor_key}</span><span className="mt-0.5 block text-[10px] font-normal text-muted-foreground">{mountingLabel}{compatible ? "" : " · correct Workflow step 1"}</span></span></Label>
      })}{!setup.data.cameras.length && <div className="p-3 text-xs text-destructive">No captured camera has complete RGB-D, timestamp, and robot-pose evidence.</div>}</div>{setup.data.unavailable_cameras.length > 0 && <details className="rounded border border-warning/40 p-3 text-xs"><summary className="cursor-pointer font-medium">{setup.data.unavailable_cameras.length} unavailable camera folder(s)</summary><div className="mt-2 space-y-1 text-muted-foreground">{setup.data.unavailable_cameras.map((camera) => <div key={camera.sensor_key}>{camera.display_name}: {camera.errors.join("; ")}</div>)}</div></details>}</fieldset>
        <div className="space-y-5"><div className="space-y-2"><Label>Printed calibration grid from step 2 <span className="text-destructive">Required</span></Label>{selectedTarget && targetMountingMatches ? <div className="rounded-lg border border-success/30 bg-success/5 p-3"><div className="text-sm font-semibold">{selectedTarget.display_name}</div><div className="mt-1 font-mono text-[10px] text-muted-foreground">{selectedTarget.target_id}</div><p className="mt-2 text-[11px] text-muted-foreground">The analysis uses the run-owned, hash-verified copy with its explicit {effectiveTargetMountingFrame?.replaceAll("_", " ")} mounting frame.</p></div> : <div className="rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs"><div className="font-semibold text-destructive">{selectedTarget ? "Grid mounting does not match the camera group" : "No valid grid is bound to this run"}</div><p className="mt-1 text-muted-foreground">Return to step 2 and select the exact printed board with the physical mounting shown above. Grid choice and mounting cannot be overridden during analysis.</p></div>}<div className="flex items-center justify-between text-xs text-muted-foreground"><span>The saved marker geometry and mounting must match the physical setup exactly.</span><Link to="/calibration-targets" className="text-primary-strong underline-offset-4 hover:underline">Review selected grid</Link></div></div><div className="space-y-2"><div className="text-sm font-semibold">Automatic extrinsic solution comparison</div><p className="text-xs leading-relaxed text-muted-foreground">PoseTestBot compares supported grid-pose and mounting-aware extrinsic methods, validates them on held-out motion, and recommends only passing results.</p><details className="rounded-lg border bg-muted/20"><summary className="cursor-pointer px-3 py-2 text-xs font-semibold">How acceptance is decided</summary><div className="border-t px-3 py-3 text-[11px] leading-relaxed text-muted-foreground" data-testid="calibration-acceptance-thresholds">Requires ≥{setup.data.solver.thresholds.min_accepted_views} accepted views and ≥{setup.data.solver.thresholds.min_motion_poses} robot poses over ≥{setup.data.solver.thresholds.min_translation_span_mm} mm / ≥{setup.data.solver.thresholds.min_rotation_span_deg}°. Camera/grid field coverage must span ≥{((coverageThresholds.min_image_centroid_x_span_ratio ?? 0.45) * 100).toFixed(0)}% of image width and ≥{((coverageThresholds.min_image_centroid_y_span_ratio ?? 0.35) * 100).toFixed(0)}% of image height with ≥{((coverageThresholds.min_image_centroid_hull_area_ratio ?? 0.10) * 100).toFixed(0)}% supported centroid-hull area; each extreme needs ≥{coverageThresholds.image_coverage_tail_support_views ?? 5} views. The 3 × 3 centroid-cell count remains diagnostic. Each grid view needs ≥{setup.data.solver.thresholds.min_pnp_common_inliers} supported corners across at least {setup.data.solver.thresholds.min_pnp_supported_markers} markers and a retained-grid error ≤{setup.data.solver.thresholds.max_pnp_all_point_mean_reprojection_error_px} px. If other boards reuse marker IDs in the image, one target instance is retained only with a stronger consensus spanning at least {setup.data.solver.thresholds.min_pnp_clutter_supported_markers ?? 8} markers, {setup.data.solver.thresholds.min_pnp_clutter_grid_rows ?? 3} rows, and {setup.data.solver.thresholds.min_pnp_clutter_grid_columns ?? 3} columns; the discarded clutter remains visible as a warning. Camera and robot timestamps must be within {setup.data.solver.thresholds.max_nearest_pose_delta_ms} ms. A new OpenCV lens estimate separately needs ≥{setup.data.solver.thresholds.min_coverage_cells} of 9 image regions, held-out proof, ≤{setup.data.solver.thresholds.max_per_view_reprojection_error_px} px per view, and ≤{setup.data.solver.thresholds.max_intrinsic_rms_reprojection_error_px} px RMS.</div></details></div></div>
      </div>
      <fieldset className="space-y-3" data-testid="calibration-synchronization-policy">
        <legend className="text-sm font-semibold">Auto time alignment</legend>
        <div className="grid gap-3 xl:grid-cols-2">
          <Label className={`cursor-pointer rounded-lg border p-4 ${synchronizationPolicy === "auto_offset" ? "border-primary bg-primary/5 ring-1 ring-primary/30" : "border-border"}`}>
            <span className="flex items-start gap-3"><input type="radio" name="calibration-synchronization-policy" value="auto_offset" checked={synchronizationPolicy === "auto_offset"} onChange={() => setSynchronizationSelection({ runRoot: selectedRun, value: "auto_offset" })} className="mt-1" /><span><span className="block font-semibold">Estimate robot-pose time offset (recommended)</span><span className="mt-1 block text-xs font-normal leading-relaxed text-muted-foreground">Search separately for each camera. A well-supported offset is applied; an ambiguous result keeps the recorded 0 ms pairing and is shown as a warning.</span></span></span>
          </Label>
          <Label className={`cursor-pointer rounded-lg border p-4 ${synchronizationPolicy === "fixed_zero" ? "border-primary bg-primary/5 ring-1 ring-primary/30" : "border-border"}`}>
            <span className="flex items-start gap-3"><input type="radio" name="calibration-synchronization-policy" value="fixed_zero" checked={synchronizationPolicy === "fixed_zero"} onChange={() => setSynchronizationSelection({ runRoot: selectedRun, value: "fixed_zero" })} className="mt-1" /><span><span className="block font-semibold">Use captured timestamps (0 ms)</span><span className="mt-1 block text-xs font-normal leading-relaxed text-muted-foreground">Skip estimation and use the recorded camera/robot pairing explicitly.</span></span></span>
          </Label>
        </div>
        <div className="flex items-start justify-between gap-3 rounded-md border border-warning/35 bg-warning/5 p-3 text-xs leading-relaxed text-muted-foreground">
          <span><span className="font-semibold text-foreground">What it changes:</span> Auto time alignment estimates effective latency for this capture path. It does not synchronize hardware clocks or rewrite raw frame or robot timestamps.</span>
          <HelpTip label="robot-pose time-offset sign">A positive robot-pose time offset pairs a frame at time t with a robot pose recorded later at t + offset. The lower-level dataset sync delta has the opposite sign.</HelpTip>
        </div>
        {synchronizationPolicy === "auto_offset" && !autoTimingRuntimeReady && <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-xs leading-relaxed" data-testid="calibration-backend-restart-required"><div className="font-semibold text-destructive">Backend restart required</div><p className="mt-1 text-muted-foreground">This page requires timing revision <span className="font-mono">{REQUIRED_AUTO_TIMING_IMPLEMENTATION_REVISION}</span>, but the running backend reports <span className="font-mono">{backendTimingRevision ?? "no revision"}</span>. Restart the PoseTestBot service and reload this page before analyzing with Auto time alignment. The current rule keeps recorded timing with a warning when offset evidence is merely weak or ambiguous, while missing or unusable input still stops the attempt.</p></div>}
        <details className="rounded-lg border bg-muted/20"><summary className="cursor-pointer px-3 py-2 text-xs font-semibold">Time-alignment search limits and warning policy</summary><div className="space-y-2 border-t px-3 py-3 text-[11px] text-muted-foreground"><p>Fixed search: {formatSigned(setup.data.solver.synchronization.search.minimum_robot_pose_time_offset_ms, " ms")} to {formatSigned(setup.data.solver.synchronization.search.maximum_robot_pose_time_offset_ms, " ms")} in {setup.data.solver.synchronization.search.step_ms.toFixed(1)} ms steps. These limits are recorded with the attempt and are not tuned interactively.</p><p>Nearest-pose gaps above {(setup.data.solver.synchronization.search.warning_nearest_pose_delta_ms ?? setup.data.solver.thresholds.warning_nearest_pose_delta_ms ?? 20).toFixed(0)} ms are warnings; matches remain usable through {(setup.data.solver.synchronization.search.max_nearest_pose_delta_ms ?? setup.data.solver.thresholds.max_nearest_pose_delta_ms).toFixed(0)} ms. A supported offset larger than ±{(setup.data.solver.synchronization.search.warning_absolute_robot_pose_time_offset_ms ?? 150).toFixed(0)} ms is also highlighted, but it is not rejected solely for its magnitude.</p><p>At least {(setup.data.solver.synchronization.search.minimum_motion_count_per_cross_validation_fold ?? 4) * 3} eligible motion groups are required. After candidate selection, every motion is held out once while the mounting-aware extrinsic transform is fitted from the other motions. Strong, consistent evidence applies the candidate offset. Weak, ambiguous, inconsistent, or boundary-limited evidence keeps the recorded 0 ms pairing with a prominent warning so the calibration can continue. Missing, corrupt, or otherwise unevaluable input still stops the attempt.</p></div></details>
      </fieldset>
      <Button className="w-full" size="lg" disabled={!canRun} onClick={() => createAttempt.mutate()}>{createAttempt.isPending ? <LoaderCircle className="animate-spin" /> : <Grid3X3 />}{createAttempt.isPending ? "Queueing calibration…" : synchronizationPolicy === "fixed_zero" ? "Analyze recording · captured timestamps (0 ms)" : "Analyze recording · estimate time offset"}</Button>{!canRun && <p className="text-center text-xs text-muted-foreground">{synchronizationPolicy === "auto_offset" && !autoTimingRuntimeReady ? "Restart the PoseTestBot backend and reload this page to use the current Auto time-alignment rule." : arrangement.status !== "ready" ? "Return to Workflow step 1 and save one homogeneous camera mounting group." : !targetMountingMatches ? "Return to step 2 and select the printed grid with the required physical mounting." : "Select at least one camera from this recording to continue."}</p>}
    </CardContent></Card>

    {activeAttemptId && <Card className="border-primary/25" data-testid="calibration-attempt-job-status"><CardHeader><CardTitle className="flex items-center justify-between text-base"><span>Calculation progress</span><span className="font-mono text-[10px] font-normal text-muted-foreground">{activeAttemptId}</span></CardTitle><CardDescription>{attempt.data?.progress.message ?? "Loading attempt…"}{attempt.data?.request.synchronization_policy ? ` · Recorded timing policy: ${attempt.data.request.synchronization_policy === "fixed_zero" ? "captured timestamps (0 ms)" : "estimate time offset"}` : ""}</CardDescription></CardHeader><CardContent><div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"><p className="text-xs leading-relaxed text-muted-foreground" data-testid="calibration-duration-guidance">A three-camera comparison usually takes 10–20 minutes. This background work continues after navigation; returning to this run restores its progress and results.</p><Button asChild size="sm" variant="outline" className="shrink-0"><Link to="/jobs">Open Jobs</Link></Button></div><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">{attempt.data?.progress.phases.map((phase, index) => <div key={phase.id} data-phase-id={phase.id} className={`rounded-lg border p-3 ${phase.status === "running" ? "border-primary bg-primary/5" : phase.status === "complete" ? "border-success/40 bg-success/5" : phase.status === "failed" ? "border-destructive/40 bg-destructive/5" : ""}`}><div className="flex items-center gap-2 text-xs font-semibold"><span className="grid size-5 place-items-center rounded-full bg-muted font-mono text-[10px]">{index + 1}</span>{phase.id === "compare_robot_camera_solutions" ? "Compare extrinsic solutions" : phase.label}</div><div className="mt-2 flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">{phase.status === "running" && <LoaderCircle className="size-3 animate-spin" />}{phase.status === "complete" && <CheckCircle2 className="size-3 text-success" />}{phase.status === "failed" && <TriangleAlert className="size-3 text-destructive" />}{phase.status}</div></div>)}</div></CardContent></Card>}

    {attempt.data?.time_offset_search && <TimeAlignmentSummary search={attempt.data.time_offset_search} />}
    {attempt.data?.results && <div className="space-y-4" data-testid="calibration-results"><div><h2 className="text-xl font-semibold">Review calibration results</h2><p className="mt-1 text-sm text-muted-foreground">{attempt.data.results.recommended_camera_count} of {attempt.data.results.results.length} selected cameras passed. A multi-camera calibration is saved only when every selected camera belongs to one consistent passing solution bundle.</p></div>{attempt.data.results.results.map((result) => <CameraResultCard key={result.sensor_key} mode={attempt.data.request.mode} result={result} intrinsicComparison={attempt.data?.intrinsic_comparison?.sensors.find((item) => item.sensor_key === result.sensor_key) ?? null} selectedCandidateId={overrides[result.sensor_key] ?? ""} onSelect={(candidateId) => setOverrideSelection({ runRoot: selectedRun, attemptId: activeAttemptId ?? "", values: { ...overrides, [result.sensor_key]: candidateId } })} />)}<Card className="border-primary/30"><CardContent className="flex flex-col gap-4 py-5 sm:flex-row sm:items-center sm:justify-between"><div><div className="font-semibold">Save selected camera calibrations</div><div className="mt-1 text-xs text-muted-foreground">{passingSelections} of {attempt.data.results.results.length} camera profiles selected. Saving is an atomic, auditable promotion; calculation results remain immutable. The saved profiles retain the time-alignment offsets and evidence shown above.</div>{attempt.data.promotion && ["queued", "running"].includes(attempt.data.promotion.status) && <div className="mt-3 flex flex-col gap-3 rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs sm:flex-row sm:items-center sm:justify-between" data-testid="calibration-promotion-job-status"><p><span className="font-semibold">Calibration saving is {attempt.data.promotion.status}.</span> This background work continues after navigation{attempt.data.promotion.job_id ? <> in job <span className="font-mono">{attempt.data.promotion.job_id}</span></> : null}; Jobs shows its live status and output.</p><Button asChild size="sm" variant="outline" className="shrink-0"><Link to="/jobs">Open Jobs</Link></Button></div>}{attempt.data.promotion?.status === "failed" && <div className="mt-2 text-xs text-destructive">{attempt.data.promotion.error}</div>}{attempt.data.promotion?.status === "promoted" && <div className="mt-2 flex items-center gap-1 text-xs text-success"><CheckCircle2 className="size-4" />Saved {attempt.data.promotion.promoted_profile_ids?.length ?? 0} camera profile(s).</div>}</div><Button size="lg" disabled={passingSelections !== attempt.data.results.results.length || promote.isPending || ["queued", "running", "promoted"].includes(attempt.data.promotion?.status ?? "")} onClick={() => promote.mutate()}>{promote.isPending || ["queued", "running"].includes(attempt.data.promotion?.status ?? "") ? <LoaderCircle className="animate-spin" /> : <Save />}{attempt.data.promotion?.status === "promoted" ? "Calibrations saved" : "Save selected calibrations"}</Button></CardContent></Card></div>}
  </div>
}

function TimeAlignmentSummary({ search }: { search: TimeOffsetSearch }) {
  const fixedZero = search.policy === "fixed_zero"
  const hasTimingWarnings = search.sensors.some((sensor) => sensor.checks.some((check) => check.status === "warning"))
  const hasRecordedTimingFallback = search.sensors.some((sensor) => sensor.warning_fallback_used === true)
  return <Card className={search.status === "failed" ? "border-destructive/40" : hasTimingWarnings ? "border-warning/40" : ""} data-testid="calibration-time-alignment">
    <CardHeader><CardTitle className="flex items-center gap-2 text-base">{fixedZero ? "Captured timestamp pairing" : "Auto time-alignment evidence"} <HelpTip label="robot-pose time-offset evidence">A positive offset uses a later robot pose. The lower-level dataset sync delta has the opposite sign. Neither value rewrites raw timestamps.</HelpTip></CardTitle><CardDescription>{fixedZero ? "Offset estimation was explicitly skipped. This attempt uses the recorded camera/robot pairing at 0 ms." : "The offset estimates effective capture/pose latency; it is not evidence that the hardware clocks are synchronized."}</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      {search.status === "failed" && <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-xs" data-testid="calibration-time-alignment-failed"><div className="font-semibold text-destructive">Auto time alignment stopped this calibration</div><p className="mt-1 text-muted-foreground">Required robot-pose timing input was missing, corrupt, or unusable. No extrinsic result was generated or saved; inspect the checks below.</p></div>}
      {search.status === "complete" && hasTimingWarnings && <div className="rounded-md border border-warning/40 bg-warning/5 p-3 text-xs" data-testid="calibration-time-alignment-warning"><div className="font-semibold text-warning-foreground">Calibration has advisory timing evidence</div><p className="mt-1 text-muted-foreground">{hasRecordedTimingFallback ? "The offset search was inconclusive for at least one camera, so PoseTestBot kept its recorded 0 ms pairing instead of applying a weak candidate. The calibration continued; review this warning with the final residual and reprojection evidence before saving." : "A supported offset or recorded pairing has advisory nearest-pose or magnitude evidence. Review it together with the final residual and reprojection evidence before saving."}</p></div>}
      <div className="overflow-x-auto rounded-lg border">
        <table className="min-w-[1040px] w-full text-left text-[11px]">
          <caption className="sr-only">Motion-disjoint per-camera time-offset search decisions</caption>
          <thead className="bg-muted/60 text-muted-foreground"><tr><th scope="col" className="px-3 py-2">Camera</th><th scope="col" className="px-3 py-2">Decision</th><th scope="col" className="px-3 py-2">Robot-pose time offset</th><th scope="col" className="px-3 py-2">Search translation</th><th scope="col" className="px-3 py-2">Search rotation</th><th scope="col" className="px-3 py-2">Motions / views</th><th scope="col" className="px-3 py-2">Translation improvement</th><th scope="col" className="px-3 py-2">Evidence</th></tr></thead>
          <tbody>{search.sensors.map((sensor) => {
            const baseline = sensor.cross_validation?.zero_offset.residuals
            const selected = sensor.cross_validation?.candidate.residuals
            const relative = sensor.cross_validation?.improvement.relative_translation
            const motionMethods = sensor.motion_consistency?.methods ?? {}
            const motionMethod = motionMethods[sensor.selection_extrinsic_method ?? ""] ?? Object.values(motionMethods)[0]
            const warningChecks = sensor.checks.filter((check) => check.status === "warning")
            const errorChecks = sensor.checks.filter((check) => check.status === "error")
            const warning = sensor.status === "failed" || sensor.boundary_hit || warningChecks.length > 0 || errorChecks.length > 0
            const decision = sensor.status === "failed" ? "Time alignment rejected" : sensor.warning_fallback_used ? "Recorded 0 ms kept with warning" : sensor.status === "applied" && warningChecks.length > 0 ? `Applied with ${warningChecks.length} warning${warningChecks.length === 1 ? "" : "s"}` : sensor.status === "applied" ? "Time offset applied" : sensor.status === "fixed_zero" ? "Fixed zero selected" : "Zero offset identified"
            const candidateLabel = sensor.status === "failed" ? `rejected ${formatSigned(sensor.candidate_robot_pose_time_offset_ms, " ms")} candidate` : sensor.warning_fallback_used ? `${formatSigned(sensor.candidate_robot_pose_time_offset_ms, " ms")} candidate not applied` : sensor.status === "applied" ? "selected offset" : "identified 0 ms offset"
            return <tr key={sensor.sensor_key} className="border-t" data-time-offset-sensor={sensor.sensor_key}>
              <td className="px-3 py-3"><div className="font-semibold">{sensor.display_name ?? sensor.sensor_name ?? sensor.sensor_key}</div><div className="mt-0.5 font-mono text-[9px] text-muted-foreground">{sensor.sensor_key}</div></td>
              <td className="px-3 py-3"><span className={`rounded-full px-2 py-1 font-semibold ${warning ? "bg-warning/15 text-warning-foreground" : sensor.status === "applied" ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"}`}>{decision}</span></td>
              <td className="px-3 py-3 font-mono tabular-nums"><span className="text-[9px] text-muted-foreground">Applied </span>{formatSigned(sensor.selected_robot_pose_time_offset_ms, " ms")}<div className="mt-1 text-[9px] text-muted-foreground">{sensor.status === "failed" ? <>Rejected candidate {formatSigned(sensor.candidate_robot_pose_time_offset_ms, " ms")}</> : <>dataset sync delta {formatSigned(sensor.selected_sync_delta_ms, " ms")}</>}</div></td>
              <td className="px-3 py-3 font-mono tabular-nums">{baseline && selected ? `${baseline.mean_translation_mm.toFixed(3)} → ${selected.mean_translation_mm.toFixed(3)} mm` : "—"}{baseline && selected && <div className="mt-1 text-[9px] text-muted-foreground">0 ms → {candidateLabel}</div>}</td>
              <td className="px-3 py-3 font-mono tabular-nums">{baseline && selected ? `${baseline.mean_rotation_deg.toFixed(3)} → ${selected.mean_rotation_deg.toFixed(3)}°` : "—"}{baseline && selected && <div className="mt-1 text-[9px] text-muted-foreground">0 ms → {candidateLabel}</div>}</td>
              <td className="px-3 py-3 tabular-nums">{sensor.split ? `${sensor.split.motion_count} / ${sensor.split.selected_observation_count}` : "—"}</td>
              <td className="px-3 py-3 tabular-nums">{relative === null || relative === undefined ? "—" : `${(relative * 100).toFixed(1)}%`}</td>
              <td className="px-3 py-3"><span className="capitalize">{sensor.evidence_strength.replaceAll("_", " ")}</span>{sensor.boundary_hit ? " · boundary" : ""}{motionMethod && <div className="mt-1 text-[9px] tabular-nums" data-testid={`timing-motion-summary-${sensor.sensor_key}`}>{motionMethod.positive_motion_count}/{motionMethod.motion_count} held-out motions improved · corrected p {formatProbability(motionMethod.candidate_search_adjusted_positive_sign_p_value)}</div>}{warningChecks.length > 0 && <div className="mt-1 text-[9px] text-warning-foreground">{warningChecks.map((check) => check.name.replaceAll("_", " ")).join("; ")}</div>}{errorChecks.length > 0 && <div className="mt-1 text-[9px] text-destructive">{errorChecks.map((check) => check.name.replaceAll("_", " ")).join("; ")}</div>}</td>
            </tr>
          })}</tbody>
        </table>
      </div>
      <div className="space-y-2">{search.sensors.map((sensor) => <details className="rounded-lg border" key={sensor.sensor_key}><summary className="cursor-pointer px-3 py-2 text-xs font-semibold">Technical offset evidence · {sensor.display_name ?? sensor.sensor_key}</summary><div className="space-y-3 border-t p-3 text-[11px]">
        <div className="grid gap-2 sm:grid-cols-3"><Datum label="Candidate offset" value={formatSigned(sensor.candidate_robot_pose_time_offset_ms, " ms")} /><Datum label="Applied offset" value={formatSigned(sensor.selected_robot_pose_time_offset_ms, " ms")} /><Datum label="Decision reason" value={sensor.decision_reason.replaceAll("_", " ")} /></div>
        {sensor.motion_consistency && <section className="space-y-2 rounded border p-3" data-testid={`timing-motion-consistency-${sensor.sensor_key}`}><div><div className="font-semibold">Leave-one-motion-out timing consistency</div><p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">Each row refits the mounting-aware extrinsic transform without the motion being scored. Median improvement must clear both materiality thresholds. Because the candidate was selected from this fixed motion set, the positive-motion sign test is Bonferroni-corrected for all {sensor.motion_consistency.candidate_search_hypothesis_count} nonzero offset candidates.</p></div><div className="overflow-x-auto"><table className="w-full min-w-[700px] text-left"><thead className="text-muted-foreground"><tr><th className="py-1 pr-3">Reference solver</th><th className="py-1 pr-3">Motions improved</th><th className="py-1 pr-3">Material motions</th><th className="py-1 pr-3">Median improvement</th><th className="py-1 pr-3">Raw sign p</th><th className="py-1 pr-3">Search-corrected p</th><th className="py-1">State</th></tr></thead><tbody>{Object.entries(sensor.motion_consistency.methods).map(([method, evidence]) => <tr className="border-t" key={method}><td className="py-1.5 pr-3 font-mono uppercase">{method}</td><td className="py-1.5 pr-3 tabular-nums">{evidence.positive_motion_count}/{evidence.motion_count}</td><td className="py-1.5 pr-3 tabular-nums">{evidence.material_motion_count}/{evidence.motion_count}</td><td className="py-1.5 pr-3 tabular-nums">{evidence.median_improvement.absolute_translation_mm.toFixed(3)} mm · {evidence.median_improvement.relative_translation === null ? "—" : `${(evidence.median_improvement.relative_translation * 100).toFixed(1)}%`}</td><td className="py-1.5 pr-3 font-mono">{formatProbability(evidence.positive_sign_p_value)}</td><td className="py-1.5 pr-3 font-mono">{formatProbability(evidence.candidate_search_adjusted_positive_sign_p_value)}</td><td className="py-1.5 capitalize">{evidence.status}</td></tr>)}</tbody></table></div></section>}
        {sensor.checks.length > 0 && <div className="overflow-x-auto"><table className="w-full min-w-[680px] text-left"><thead className="text-muted-foreground"><tr><th className="py-1 pr-3">Check</th><th className="py-1 pr-3">State</th><th className="py-1 pr-3">Actual</th><th className="py-1">Threshold</th></tr></thead><tbody>{sensor.checks.map((check) => <tr className="border-t" key={check.name}><td className="py-1.5 pr-3">{check.name.replaceAll("_", " ")}</td><td className="py-1.5 pr-3 capitalize">{check.status}</td><td className="py-1.5 pr-3 font-mono">{compactJson(check.actual)}</td><td className="py-1.5 font-mono">{compactJson(check.threshold ?? check.warning_threshold ?? check.failure_threshold)}</td></tr>)}</tbody></table></div>}
        {sensor.curve.length > 0 && <div className="max-h-52 overflow-auto rounded border"><table className="w-full text-left font-mono text-[10px]"><caption className="sr-only">Motion-disjoint search curve for {sensor.sensor_key}</caption><thead className="sticky top-0 bg-muted"><tr><th className="px-2 py-1">Offset</th><th className="px-2 py-1">Mean translation</th><th className="px-2 py-1">Mean rotation</th></tr></thead><tbody>{sensor.curve.map((sample) => <tr className="border-t" key={sample.robot_pose_time_offset_ms}><td className="px-2 py-1">{formatSigned(sample.robot_pose_time_offset_ms, " ms")}</td><td className="px-2 py-1">{sample.residuals.mean_translation_mm.toFixed(3)} mm</td><td className="px-2 py-1">{sample.residuals.mean_rotation_deg.toFixed(3)}°</td></tr>)}</tbody></table></div>}
      </div></details>)}</div>
    </CardContent>
  </Card>
}

function CameraResultCard({ mode, result, intrinsicComparison, selectedCandidateId, onSelect }: { mode: Attempt["request"]["mode"]; result: CameraResult; intrinsicComparison: IntrinsicComparison | null; selectedCandidateId: string; onSelect: (value: string) => void }) {
  const selected = result.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId) ?? result.recommendation
  const passing = result.candidates.filter((candidate) => candidate.status === "passing")
  const staticWorkcellCalibration = mode === "eye_to_hand"
  const primaryTransformLabel = selected?.primary_transform
    ? staticWorkcellCalibration && selected.primary_transform.from === "camera" && selected.primary_transform.to === "template_base"
      ? "camera → PoseTemplateBase"
      : `${selected.primary_transform.from} → ${selected.primary_transform.to}`
    : "—"
  const manualIntrinsic = intrinsicComparison?.candidates.find((candidate) => candidate.profile_id === intrinsicComparison.manual_profile_id)
  const intrinsicLabel = intrinsicComparison?.status === "manual_selected"
    ? "Using OpenCV estimate"
    : intrinsicComparison?.status === "existing_selected"
      ? "Using matching saved values"
      : intrinsicComparison?.status === "unusable"
        ? "No usable lens model"
        : "Using factory SDK values"
  const intrinsicFailureIsBlocking = intrinsicComparison?.status === "unusable"
  const clutterCheck = selected?.checks?.find((check) => check.name === "duplicate_marker_clutter_filtered")
  const clutterEvidence = clutterCheck?.actual as { affected_view_count?: number; ignored_correspondence_count?: number } | undefined
  return <Card className={result.status === "passing" ? "border-success/35" : "border-destructive/35"} data-camera-key={result.sensor_key}>
    <CardHeader><div className="flex items-start justify-between gap-4"><div><CardTitle className="text-base">{result.display_name}</CardTitle><CardDescription className="mt-1 font-mono text-[10px]">{result.sensor_key}</CardDescription></div><span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide ${result.status === "passing" ? "bg-success/10 text-success" : "bg-destructive/10 text-destructive"}`}>{result.status === "passing" ? "Passed" : "Needs attention"}</span></div></CardHeader>
    <CardContent className="space-y-4">
      {intrinsicComparison && <section className={`rounded-md border p-3 ${intrinsicComparison.status === "unusable" ? "border-destructive/40 bg-destructive/5" : "bg-muted/20"}`} data-testid={`intrinsic-comparison-${result.sensor_key}`} aria-label="Camera lens model comparison">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="flex items-center gap-1 text-xs font-semibold">Camera lens model (intrinsics) <HelpTip label="camera intrinsics">Intrinsics describe focal length, image center, and lens distortion inside one camera. They are separate from the mounting-aware extrinsic transform calculated below.</HelpTip></div><p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-muted-foreground">Factory values come from the camera SDK. The OpenCV estimate is fitted from this recording's grid views. PoseTestBot keeps compatible factory values by default; OpenCV is activated only when factory projection cannot be used and the new estimate passes every quality gate—not merely when its RMS is lower.</p></div><span className={`rounded-full px-2 py-1 text-[10px] font-semibold ${intrinsicComparison.status === "unusable" ? "bg-destructive/10 text-destructive" : "bg-primary/10 text-primary-strong"}`}>{intrinsicLabel}</span></div>
        <div className="mt-3 grid gap-3 text-xs sm:grid-cols-3"><Metric label="Selected lens profile" value={intrinsicComparison.selected_profile_id ?? "None"} /><Metric label="OpenCV training views / image coverage" value={manualIntrinsic ? `${manualIntrinsic.quality.accepted_view_count} views · ${manualIntrinsic.quality.coverage_cells.length} of 9 regions` : "Not available"} /><Metric label="OpenCV RMS reprojection error" value={format(manualIntrinsic?.quality.rms_reprojection_error_px, " px")} /></div>
        <div className="mt-2 text-[11px] text-muted-foreground">{humanizeReason(intrinsicComparison.selection_reason)}</div>
        {intrinsicComparison.manual_failure && <div className={`mt-2 rounded border p-2 text-[11px] ${intrinsicFailureIsBlocking ? "border-destructive/35 bg-destructive/5 text-destructive" : "border-warning/35 bg-warning/5 text-warning-foreground"}`}><span className="font-semibold">OpenCV comparison was not eligible{intrinsicFailureIsBlocking ? "." : "; this does not block the selected factory lens model."}</span> {intrinsicComparison.manual_failure.quality?.reason ?? intrinsicComparison.manual_failure.message}</div>}
        {intrinsicComparison.deltas && <details className="mt-3 text-[11px]"><summary className="cursor-pointer font-semibold">Lens-model comparison details</summary><div className="mt-2 font-mono text-[10px] text-muted-foreground">Δ fx, fy: {intrinsicComparison.deltas.focal_length_delta_px.map((value) => value.toFixed(3)).join(", ")} px · Δ cx, cy: {intrinsicComparison.deltas.principal_point_delta_px.map((value) => value.toFixed(3)).join(", ")} px · max |Δ distortion|: {intrinsicComparison.deltas.max_abs_distortion_delta === null ? "not comparable" : intrinsicComparison.deltas.max_abs_distortion_delta.toExponential(3)}</div></details>}
      </section>}
      {selected?.primary_transform ? <>
        <div className="rounded-lg border border-success/35 bg-success/5 p-3" data-testid={`calibration-primary-transform-${result.sensor_key}`}><div className="text-xs font-semibold">{staticWorkcellCalibration ? "Reusable object-capture transform" : "Reusable robot-mounted-camera transform"}</div><div className="mt-1 font-mono text-sm text-primary-strong">{primaryTransformLabel}</div><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{staticWorkcellCalibration ? "This is the primary profile used to place later object observations from this static camera into PoseTemplateBase." : "This is the primary profile used with recorded robot flange poses to place observations from this moving camera."}</p></div>
        <div className="grid gap-3 text-xs sm:grid-cols-3"><Metric label="Accepted observations" value={`${selected.inlier_count} of ${selected.observation_count}`} /><Metric label="Mean grid reprojection error" value={format(selected.mean_reprojection_error_px, " px")} /><Metric label="Held-out motion residual" value={`${format(selected.held_out_residuals?.median_translation_mm, " mm")} · ${format(selected.held_out_residuals?.median_rotation_deg, "°")}`} /></div>
        {clutterCheck && <div className="rounded-md border border-warning/35 bg-warning/5 p-3 text-[11px] text-warning-foreground" data-testid={`calibration-grid-clutter-warning-${result.sensor_key}`}><div className="font-semibold">Other calibration grids were filtered from the target pose.</div><p className="mt-1 leading-relaxed text-muted-foreground">Duplicate marker IDs showed that more than one printed grid was visible. The solver retained one strong planar target instance in {clutterEvidence?.affected_view_count ?? "the affected"} views and ignored {clutterEvidence?.ignored_correspondence_count ?? "conflicting"} off-instance corner correspondences. The result remains usable with this warning; review the reprojection and held-out residuals before saving.</p></div>}
        {selected.companion_transform && <div className="rounded-lg border bg-muted/20 p-3" data-testid={`calibration-companion-transform-${result.sensor_key}`}><div className="text-xs font-semibold">{staticWorkcellCalibration ? "Moving-grid attachment estimate · supporting evidence" : "Fixed-grid placement estimate · supporting evidence"}</div><div className="mt-1 font-mono text-[11px]">{selected.companion_transform.from} → {selected.companion_transform.to}</div><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{staticWorkcellCalibration ? "The solver needs this constant grid attachment to use the robot's many target poses, but it is not the reusable camera output and does not turn the workflow into hand tracking." : "This retained target placement supports the solve; the reusable camera output remains camera → robot_flange."}</p></div>}
        <details className="rounded-lg border"><summary className="flex cursor-pointer list-none items-center justify-between p-3 text-xs font-semibold">Transform technical details <ChevronDown className="size-4 transition group-open:rotate-180" /></summary><div className="space-y-3 border-t p-3"><div className="grid gap-3 lg:grid-cols-[1.3fr_1fr]"><div className="rounded-md bg-muted/60 p-3"><div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Primary 4 × 4 matrix</div><pre className="overflow-x-auto text-[10px] leading-5">{selected.primary_transform.matrix.map((row) => row.map((value) => value.toFixed(6).padStart(12)).join(" ")).join("\n")}</pre></div><div className="space-y-3 rounded-md border p-3 text-xs"><Datum label="Primary frames" value={primaryTransformLabel} /><Datum label="Quaternion WXYZ" value={selected.primary_transform.rotation_quaternion_wxyz.map((value) => value.toFixed(7)).join(", ")} /><Datum label="Translation mm" value={selected.primary_transform.translation_mm.map((value) => value.toFixed(4)).join(", ")} /><Datum label="Algorithms" value={selected.algorithms.join(" + ")} /><Datum label="Validation" value={selected.validation_state} /></div></div>{selected.companion_transform && <div className="grid gap-3 border-t pt-3 lg:grid-cols-[1.3fr_1fr]"><div className="rounded-md bg-muted/60 p-3"><div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Supporting 4 × 4 matrix</div><pre className="overflow-x-auto text-[10px] leading-5">{selected.companion_transform.matrix.map((row) => row.map((value) => value.toFixed(6).padStart(12)).join(" ")).join("\n")}</pre></div><div className="space-y-3 rounded-md border p-3 text-xs"><Datum label="Supporting frames" value={`${selected.companion_transform.from} → ${selected.companion_transform.to}`} /><Datum label="Quaternion WXYZ" value={selected.companion_transform.rotation_quaternion_wxyz.map((value) => value.toFixed(7)).join(", ")} /><Datum label="Translation mm" value={selected.companion_transform.translation_mm.map((value) => value.toFixed(4)).join(", ")} /></div></div>}</div></details>
      </> : <div className="rounded border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">No consistent solution passed for this camera. Open the attempted solutions below for the exact failure.</div>}
      {passing.length > 0 && <details className="rounded-lg border"><summary className="cursor-pointer px-3 py-2 text-xs font-semibold">Alternative passing solution</summary><div className="grid gap-3 border-t p-3 sm:grid-cols-[220px_minmax(0,1fr)] sm:items-center"><Label htmlFor={`override-${result.sensor_key}`}>Alternative solution</Label><Select value={selectedCandidateId} onValueChange={onSelect}><SelectTrigger id={`override-${result.sensor_key}`}><SelectValue /></SelectTrigger><SelectContent>{passing.map((candidate) => <SelectItem value={candidate.candidate_id} key={candidate.candidate_id}>{candidate.recommended ? "Recommended · " : ""}{candidate.pnp_method} + {candidate.extrinsic_method} · score {candidate.score?.toFixed(4)}</SelectItem>)}</SelectContent></Select></div></details>}
      <details className="group rounded-lg border"><summary className="flex cursor-pointer list-none items-center justify-between p-3 text-xs font-semibold">All attempted solutions and failures <ChevronDown className="size-4 transition group-open:rotate-180" /></summary><div className="overflow-x-auto border-t"><table className="w-full text-left text-[11px]"><caption className="sr-only">Every attempted grid-pose and extrinsic solution for {result.display_name}</caption><thead className="bg-muted/60 text-muted-foreground"><tr><th scope="col" className="px-3 py-2">Grid pose</th><th scope="col" className="px-3 py-2">Extrinsic solver</th><th scope="col" className="px-3 py-2">State</th><th scope="col" className="px-3 py-2">Score</th><th scope="col" className="px-3 py-2">Inliers</th><th scope="col" className="px-3 py-2">Reprojection</th><th scope="col" className="px-3 py-2">Held-out T / R</th><th scope="col" className="px-3 py-2">Failure</th></tr></thead><tbody>{result.candidates.map((candidate) => <tr key={candidate.candidate_id} className="border-t"><td className="px-3 py-2 font-mono">{candidate.pnp_method}</td><td className="px-3 py-2 font-mono">{candidate.extrinsic_method}</td><td className="px-3 py-2 capitalize">{candidate.status}{candidate.recommended ? " · recommended" : ""}</td><td className="px-3 py-2 tabular-nums">{candidate.score?.toFixed(4) ?? "—"}</td><td className="px-3 py-2 tabular-nums">{candidate.inlier_count}/{candidate.observation_count}</td><td className="px-3 py-2 tabular-nums">{format(candidate.mean_reprojection_error_px, " px")}</td><td className="px-3 py-2 tabular-nums">{format(candidate.held_out_residuals?.mean_translation_mm, " mm")} / {format(candidate.held_out_residuals?.mean_rotation_deg, "°")}</td><td className="max-w-72 px-3 py-2 text-destructive">{candidate.error ?? ""}</td></tr>)}</tbody></table></div></details>
    </CardContent>
  </Card>
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="rounded-md border p-3"><div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div><div className="mt-1 font-mono text-[11px]">{value}</div></div> }
function Datum({ label, value }: { label: string; value: string }) { return <div><span className="text-muted-foreground">{label}</span><div className="mt-1 font-mono text-[10px] capitalize">{value}</div></div> }
function format(value: number | null | undefined, suffix: string) { return value === null || value === undefined ? "—" : `${value.toFixed(3)}${suffix}` }
function formatSigned(value: number, suffix: string) { return `${value >= 0 ? "+" : ""}${value.toFixed(1)}${suffix}` }
function formatProbability(value: number) { return value < 0.001 ? value.toExponential(2) : value.toFixed(3) }
function compactJson(value: unknown) {
  if (value === undefined || value === null) return "—"
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3)
  if (typeof value === "string") return value
  return JSON.stringify(value)
}
function humanizeReason(value: string) {
  const reasons: Record<string, string> = {
    manual_opencv_passed_all_intrinsic_quality_gates: "The factory projection could not be used, and this OpenCV estimate passed training, held-out, and plausibility checks.",
    manual_opencv_not_available_or_failed_quality_gates: "The factory SDK values are compatible; the OpenCV comparison was unavailable or did not pass every quality check.",
    compatible_factory_projection_retained: "The factory SDK projection is compatible and remains the expected default.",
  }
  return reasons[value] ?? value.replaceAll("_", " ")
}
