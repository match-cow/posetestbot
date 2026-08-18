import { Children, isValidElement, useEffect, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom"
import { ArrowLeft, ArrowRight, Boxes, Camera, Database, Grid3X3, RefreshCw, Sparkles } from "lucide-react"
import { HelpTip } from "@/components/help-tip"
import { CalibrationArrangementCard, calibrationArrangementForSensors, effectiveCalibrationTargetMountingFrame } from "@/components/calibration-arrangement"
import { PageHeader } from "@/components/page-header"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { api, query } from "@/lib/api"
import type { BopAnnotationSetup, Overview, PreflightSummary, RunConfig } from "@/lib/contracts"
import { workflowJourneyMetadata } from "@/lib/workflow-session"
import { useOperator } from "@/providers/operator-provider"
import { CalibrationWorkflow } from "@/features/workflow/calibration-workflow"
import { BopGroundTruthGeneration } from "@/features/workflow/bop-ground-truth-generation"
import { CaptureGate } from "@/features/workflow/capture-gate"
import { GroundTruthWorkflow } from "@/features/workflow/ground-truth-workflow"
import { DatasetProcessing } from "@/features/workflow/dataset-processing"
import { ReadinessCheck, readinessSatisfied } from "@/features/workflow/readiness-check"
import { RunSetup } from "@/features/workflow/run-setup"
import { WorkflowStepCard, WorkflowStepper, type WorkflowRequirement, type WorkflowStepDefinition } from "@/features/workflow/workflow-steps"

type JourneyId = "calibration" | "dataset"
type WorkflowPageId = "setup" | JourneyId
type RunConfigResponse = { config: RunConfig; preflight: PreflightSummary }

const calibrationOutline = workflowJourneyMetadata.calibration.steps.map((step) => step.title)
const datasetOutline = workflowJourneyMetadata.dataset.steps.map((step) => step.title)

function stepStatuses(completed: boolean[]): Array<WorkflowStepDefinition["status"]> {
  const firstIncomplete = completed.findIndex((value) => !value)
  return completed.map((value, index) => value ? "complete" : index === firstIncomplete ? "current" : "not_started")
}

function WorkflowChoice({ to, icon: Icon, title, description, steps, output }: { to: string; icon: typeof Camera; title: string; description: string; steps: string[]; output: string }) {
  return <Card className="group flex h-full flex-col transition-colors hover:border-primary/55">
    <CardHeader>
      <div className="mb-2 grid size-11 place-items-center rounded-xl bg-primary/10 text-primary-strong"><Icon aria-hidden="true" className="size-5" /></div>
      <CardTitle>{title}</CardTitle>
      <CardDescription className="leading-relaxed">{description}</CardDescription>
    </CardHeader>
    <CardContent className="flex flex-1 flex-col">
      <ol className="space-y-2 border-l border-border pl-4">{steps.map((step, index) => <li key={step} className="relative text-xs"><span aria-hidden="true" className="absolute -left-[21px] top-0 grid size-3 place-items-center rounded-full bg-muted ring-4 ring-card" /><span className="font-mono text-[10px] font-bold text-muted-foreground">{String(index + 1).padStart(2, "0")}</span><span className="ml-2 text-foreground">{step}</span></li>)}</ol>
      <div className="mt-5 rounded-lg bg-muted/60 p-3 text-xs"><span className="font-semibold">Result:</span> <span className="text-muted-foreground">{output}</span></div>
      <Button asChild className="mt-5 w-full"><Link to={to}>Start this workflow <ArrowRight aria-hidden="true" /></Link></Button>
    </CardContent>
  </Card>
}

function JourneyNavigation({ current }: { current: JourneyId }) {
  return <nav aria-label="Workflow type" className="flex flex-wrap gap-2 rounded-xl border bg-card p-2">
    <Button asChild variant={current === "calibration" ? "default" : "ghost"}><Link to="/workflow/calibration"><Camera aria-hidden="true" />Camera calibration</Link></Button>
    <Button asChild variant={current === "dataset" ? "default" : "ghost"}><Link to="/workflow/dataset"><Database aria-hidden="true" />Object dataset</Link></Button>
  </nav>
}

function OptionalAction({ icon: Icon, title, description, to, action }: { icon: typeof Sparkles; title: string; description: string; to: string; action: string }) {
  return <Card className="border-dashed bg-muted/15"><CardContent className="flex flex-col gap-4 py-4 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-start gap-3"><span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"><Icon aria-hidden="true" className="size-4" /></span><div><div className="flex items-center gap-2 text-sm font-semibold">{title}<span className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">Optional</span></div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">{description}</p></div></div><Button asChild variant="outline" size="sm"><Link to={to}>{action}<ArrowRight aria-hidden="true" /></Link></Button></CardContent></Card>
}

function JourneyShell({ journey, steps, selectedStep, onSelectStep, children }: { journey: JourneyId; steps: WorkflowStepDefinition[]; selectedStep: string | null; onSelectStep: (stepId: string, scroll?: boolean) => void; children: React.ReactNode }) {
  const { rememberWorkflowStep } = useOperator()
  const meta = journey === "calibration" ? {
    eyebrow: "Guided workflow · reusable camera geometry",
    title: "Calibrate cameras",
    description: "Record a known printed ArUco grid, compare camera extrinsic solutions, then explicitly publish reusable calibration profiles in the frame required by each physical mounting.",
  } : {
    eyebrow: "Guided workflow · acquisition dataset",
    title: "Record an object dataset",
    description: "Use a previously published calibration and a confirmed physical pose template to record, synchronize, and export a BOP dataset.",
  }
  const fallbackStep = steps.find((step) => step.status === "current" || step.status === "running")?.id
    ?? steps.find((step) => step.status !== "complete")?.id
    ?? steps.at(-1)?.id
  const effectiveStep = selectedStep && steps.some((step) => step.id === selectedStep) ? selectedStep : fallbackStep
  const effectiveStepDefinition = steps.find((step) => step.id === effectiveStep)
  const effectiveStepId = effectiveStepDefinition?.id
  const effectiveStepStatus = effectiveStepDefinition?.status
  const stepContent = Children.map(children, (child) => {
    if (!isValidElement<{ id?: string }>(child)) return child
    const stepId = child.props.id
    return <div key={stepId} hidden={stepId !== effectiveStep}>{child}</div>
  })
  useEffect(() => {
    if (!effectiveStep || selectedStep === effectiveStep) return
    onSelectStep(effectiveStep, false)
  }, [effectiveStep, onSelectStep, selectedStep])
  useEffect(() => {
    if (!effectiveStepId || !effectiveStepStatus) return
    rememberWorkflowStep(journey, effectiveStepId, effectiveStepStatus)
  }, [effectiveStepId, effectiveStepStatus, journey, rememberWorkflowStep])
  return <div className="space-y-6">
    <div className="flex items-center gap-2 text-xs"><Button asChild variant="ghost" size="sm"><Link to="/workflow/setup"><ArrowLeft aria-hidden="true" />Choose workflow</Link></Button><span className="text-muted-foreground">/</span><span className="font-semibold">{meta.title}</span></div>
    <PageHeader eyebrow={meta.eyebrow} title={meta.title} description={meta.description} />
    <JourneyNavigation current={journey} />
    <div className="grid items-start gap-6 xl:grid-cols-[270px_minmax(0,1fr)]">
      <WorkflowStepper steps={steps} selectedStep={effectiveStep} onSelect={onSelectStep} />
      <div className="min-w-0" data-selected-step={effectiveStep ?? ""}>{stepContent}</div>
    </div>
  </div>
}

function artifact(overview: Overview | undefined, path: string) {
  return overview?.sidebar.flatMap((section) => section.artifacts).find((item) => item.path === path)
}

function artifactComplete(overview: Overview | undefined, path: string) {
  const item = artifact(overview, path)
  return Boolean(item?.exists && ["complete", "succeeded", "ok", "warning", "valid", "ready"].includes(item.status ?? ""))
}

function signedMilliseconds(value: number) {
  const normalized = Math.abs(value) < 0.0005 ? 0 : value
  return `${normalized >= 0 ? "+" : ""}${normalized.toFixed(3)} ms`
}

function CalibrationSyncPolicy({ configured, calibrationSync }: { configured: boolean; calibrationSync: Overview["calibration_sync"] | undefined }) {
  const status = configured ? calibrationSync?.status ?? "error" : "not_configured"
  const ready = status === "ready"
  const failure = configured && !ready
    ? status === "error"
      ? calibrationSync?.error ?? "The selected calibration timing policy could not be verified."
      : "The selected calibration snapshot does not contain a usable automatic timing policy."
    : null

  return <Card data-testid="calibration-sync-policy" className={ready ? "border-success/30 bg-success/5" : failure ? "border-destructive/35 bg-destructive/5" : "border-dashed"}>
    <CardHeader className="pb-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            Automatic calibration timing
            <StatusBadge status={ready ? "ready" : failure ? "error" : "not_configured"} tone={ready ? "success" : failure ? "destructive" : "neutral"}>{ready ? "ready" : failure ? "blocked" : "not configured"}</StatusBadge>
          </CardTitle>
          <CardDescription className="mt-1 max-w-4xl leading-relaxed">
            {ready
              ? "The per-camera offsets, timestamp sources, and match limits below are part of the selected hash-bound calibration. Dataset synchronization reuses them automatically."
              : failure
                ? "This dataset cannot use the selected calibration until its saved timing contract is valid."
                : "Select a published calibration to load its saved per-camera synchronization policy."}
          </CardDescription>
        </div>
        {ready && calibrationSync?.bundle_sha256 && <div className="shrink-0 text-right text-[10px] text-muted-foreground"><div className="font-semibold uppercase tracking-wide">Calibration bundle</div><div className="mt-1 font-mono">{calibrationSync.bundle_sha256.slice(0, 16)}…</div></div>}
      </div>
    </CardHeader>
    <CardContent>
      {failure && <div role="alert" className="rounded-md border border-destructive/30 bg-background/70 p-3 text-xs leading-relaxed text-destructive">{failure}</div>}
      {ready && calibrationSync && <div className="space-y-3">
        <div className="overflow-x-auto rounded-lg border bg-background">
          <table className="w-full min-w-[760px] text-left text-[11px]">
            <caption className="sr-only">Hash-bound automatic synchronization policy for each selected camera</caption>
            <thead className="bg-muted/60 text-muted-foreground">
              <tr>
                <th scope="col" className="px-3 py-2">Camera and profile</th>
                <th scope="col" className="px-3 py-2"><span className="inline-flex items-center gap-1">Robot-pose time offset <HelpTip label="robot-pose time offset">Positive means pair the frame with a robot pose recorded later. The raw timestamps are never changed.</HelpTip></span></th>
                <th scope="col" className="px-3 py-2"><span className="inline-flex items-center gap-1">Timestamp pair <HelpTip label="calibration timestamp pair">The exact frame and robot clock fields required by this camera profile. A required camera clock domain must match, and fallback is rejected unless the profile explicitly allows it.</HelpTip></span></th>
                <th scope="col" className="px-3 py-2"><span className="inline-flex items-center gap-1">Maximum pose gap <HelpTip label="maximum robot-pose gap">A frame is excluded when its nearest robot pose is farther away than this limit.</HelpTip></span></th>
              </tr>
            </thead>
            <tbody>
              {calibrationSync.sensors.map((sensor) => <tr key={sensor.sensor_key} className="border-t">
                <td className="px-3 py-2.5"><div className="font-semibold">{sensor.sensor_name}</div><div className="mt-0.5 font-mono text-[9px] text-muted-foreground">{sensor.sensor_key} · {sensor.profile_id}</div><div className="mt-0.5 font-mono text-[9px] text-muted-foreground">{sensor.sensor_folder}</div></td>
                <td className="px-3 py-2.5 font-mono tabular-nums">{signedMilliseconds(sensor.robot_pose_time_offset_ms)}</td>
                <td className="px-3 py-2.5"><div className="font-mono text-[10px]">{sensor.frame_timestamp_source}<span className="px-1 text-muted-foreground">→</span>{sensor.robot_timestamp_source}</div><div className="mt-0.5 text-[9px] text-muted-foreground">domain {sensor.required_frame_timestamp_domain ?? "shared host clock"} · fallback {sensor.timestamp_fallback_allowed ? "allowed" : "forbidden"}</div></td>
                <td className="px-3 py-2.5 font-mono tabular-nums">{sensor.max_nearest_pose_delta_ms.toFixed(3)} ms</td>
              </tr>)}
            </tbody>
          </table>
        </div>
        <p className="text-[11px] leading-relaxed text-muted-foreground">Profile identity and timing provenance are checked again before export.</p>
      </div>}
    </CardContent>
  </Card>
}

export function WorkflowPage() {
  const { phase } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { selectedRun } = useOperator()
  const queryClient = useQueryClient()
  const page = (phase ?? "setup") as WorkflowPageId
  const selectedStep = searchParams.get("step")
  const [autoResumedStep, setAutoResumedStep] = useState<string | null>(null)
  const [datasetProcessingJobStatus, setDatasetProcessingJobStatus] = useState<string | null>(null)
  const overview = useQuery({
    queryKey: ["overview", selectedRun],
    queryFn: () => api<Overview>(query("/ui/overview", { run_root: selectedRun })),
    refetchInterval: ["calibration", "dataset"].includes(page) ? 2_000 : false,
  })
  const config = useQuery({
    queryKey: ["run-config", selectedRun],
    queryFn: () => api<RunConfigResponse>(query("/run-config", { run_root: selectedRun })),
    retry: false,
    refetchInterval: (state) => state.state.data?.preflight.queue_blocker ? 2_000 : false,
  })
  const annotationSetup = useQuery({
    queryKey: ["bop-annotations", "setup", selectedRun],
    queryFn: () => api<BopAnnotationSetup>(query("/bop/annotations/setup", { run_root: selectedRun })),
    enabled: page === "dataset",
    retry: false,
    refetchInterval: page === "dataset" ? 2_000 : false,
  })

  useEffect(() => {
    if (!selectedStep || !["calibration", "dataset"].includes(page)) return
    if (autoResumedStep === selectedStep) return
    const frame = window.requestAnimationFrame(() => document.getElementById(`workflow-step-${selectedStep}`)?.scrollIntoView({ behavior: "smooth", block: "start" }))
    return () => window.cancelAnimationFrame(frame)
  }, [autoResumedStep, overview.isPending, page, selectedStep])

  const selectStep = (stepId: string, scroll = true) => {
    setAutoResumedStep(scroll ? null : stepId)
    setSearchParams({ step: stepId }, { replace: true })
    if (scroll) window.requestAnimationFrame(() => document.getElementById(`workflow-step-${stepId}`)?.scrollIntoView({ behavior: "smooth", block: "start" }))
  }
  const refresh = () => queryClient.invalidateQueries({ predicate: (item) => ["overview", "run-config", "calibration", "pose-template-run", "bop-annotations"].includes(String(item.queryKey[0])) })

  if (!["setup", "calibration", "dataset"].includes(page)) return <Navigate to="/workflow/setup" replace />

  if (page === "setup") return <div className="space-y-6">
    <PageHeader eyebrow="Acquisition workflows" title="What do you want to do?" description="Choose one of the two supported outcomes. Each guided workflow keeps prerequisites, physical authorization, evidence, and background-job handoffs in their required order." actions={<Button variant="outline" onClick={refresh}><RefreshCw aria-hidden="true" />Refresh evidence</Button>} />
    <div className="grid gap-5 lg:grid-cols-2">
      <WorkflowChoice to="/workflow/calibration" icon={Camera} title="Calibrate cameras" description="Use a printed calibration grid to calculate and publish camera intrinsics and mounting-aware extrinsic transforms." steps={calibrationOutline} output="A reviewed, reusable calibration profile for every selected camera." />
      <WorkflowChoice to="/workflow/dataset" icon={Boxes} title="Record an object dataset" description="Select a prior calibration and a physical pose template, then record and export an acquisition dataset." steps={datasetOutline} output="Synchronized RGB-D evidence plus a BOP dataset with calibrated images, depth, models, and provenance." />
    </div>
  </div>

  if (overview.isPending) return <div className="space-y-6"><Skeleton className="h-24" /><div className="grid gap-5 xl:grid-cols-[270px_minmax(0,1fr)]"><Skeleton className="h-96" /><div className="space-y-4"><Skeleton className="h-28" /><Skeleton className="h-80" /></div></div></div>

  const runConfig = config.data?.config ?? overview.data?.config ?? null
  const preflight = config.data?.preflight
  const configSaved = Boolean(runConfig)
  const enabledCameras = runConfig?.capture.sensors.filter((sensor) => sensor.enabled !== false) ?? []
  const calibrationArrangement = calibrationArrangementForSensors(enabledCameras)
  const targetGeometrySelected = Boolean(runConfig?.calibration_target)
  const effectiveTargetMountingFrame = effectiveCalibrationTargetMountingFrame(runConfig?.calibration_target?.placement)
  const targetMountingMatches = calibrationArrangement.status === "ready"
    && effectiveTargetMountingFrame === calibrationArrangement.mountingFrame
  const targetSelected = targetGeometrySelected && targetMountingMatches
  const templateSelected = Boolean(runConfig?.pose_template?.placement_confirmed)
  const localCalibration = artifactComplete(overview.data, "calibration_profiles.json")
  const captureComplete = artifactComplete(overview.data, "capture_execution_report.json")
  const syncQualityComplete = artifactComplete(overview.data, "sync_quality_report.json")
  // The run-level quality report validates every enabled camera's per-folder
  // sync report; there is intentionally no mutable root sync_report.json.
  const syncComplete = syncQualityComplete
  const rectificationComplete = artifactComplete(overview.data, "camera_rectification_report.json")
  const calibrationPublished = localCalibration
  const datasetCalibrationSnapshotConfigured = Boolean(
    runConfig?.calibration_profiles
    && runConfig?.intrinsic_calibration_profiles
    && runConfig?.calibration_profile_selection?.bundle_sha256,
  )
  const calibrationSync = overview.data?.calibration_sync
  const datasetCalibrationSelected = Boolean(
    datasetCalibrationSnapshotConfigured
    && calibrationSync?.status === "ready",
  )
  const calibrationRequirementDescription = !datasetCalibrationSnapshotConfigured
    ? "Choose a previously published calibration that matches every enabled camera."
    : calibrationSync?.status === "ready"
      ? `${calibrationSync.sensors.length} per-camera timing ${calibrationSync.sensors.length === 1 ? "policy is" : "policies are"} hash-bound, verified, and ready for automatic reuse.`
      : calibrationSync?.status === "error"
        ? `The selected calibration timing contract is invalid: ${calibrationSync.error ?? "verification failed without an error message."}`
        : "The calibration snapshot is selected, but its saved automatic timing policy is not configured."
  const bopComplete = artifactComplete(overview.data, "bop/bop_export_manifest.json")
  const requestedAnnotationMode = runConfig?.bop.annotation_mode
  const annotationComplete = requestedAnnotationMode === "none"
    ? bopComplete
    : Boolean(
      annotationSetup.data?.current_output?.verified
      && annotationSetup.data.current_output.mode === requestedAnnotationMode,
    )

  const calibrationRequirements: WorkflowRequirement[] = [
    { id: "config", label: "Run configuration", description: configSaved ? "The run configuration is saved." : "Save the run and camera configuration first.", status: configSaved ? "met" : "missing", onFix: () => selectStep("configure"), fixLabel: "Open step 1" },
    { id: "cameras", label: "At least one enabled camera", description: enabledCameras.length ? `${enabledCameras.length} camera${enabledCameras.length === 1 ? " is" : "s are"} enabled for this calibration.` : "No camera is enabled for capture and calibration.", status: enabledCameras.length ? "met" : "missing", onFix: () => selectStep("configure"), fixLabel: "Choose cameras" },
    { id: "arrangement", label: "One calibration mounting group", description: calibrationArrangement.status === "ready" ? `${calibrationArrangement.title}. ${calibrationArrangement.targetSummary}` : calibrationArrangement.message, status: calibrationArrangement.status === "ready" ? "met" : "missing", onFix: () => selectStep("configure"), fixLabel: "Review camera mounting" },
    { id: "target", label: "Printed grid and physical mounting selected", description: targetSelected ? `The run records the immutable grid and its ${calibrationArrangement.status === "ready" ? calibrationArrangement.mountingFrame.replaceAll("_", " ") : "physical"} mounting frame.` : targetGeometrySelected ? "The selected grid's saved mounting frame does not match the enabled cameras. Re-select it after correcting Workflow step 1." : "Select the exact printed grid and record how it is mounted for this camera group.", status: targetSelected ? "met" : "missing", onFix: () => navigate("/calibration-targets"), fixLabel: targetGeometrySelected ? "Correct target mounting" : "Choose calibration grid" },
  ]
  const calibrationReady = readinessSatisfied(preflight, calibrationRequirements)
  const calibrationStatuses = stepStatuses([configSaved, targetSelected, calibrationReady, captureComplete, calibrationPublished])
  const calibrationSteps: WorkflowStepDefinition[] = calibrationOutline.map((title, index) => ({
    id: ["configure", "target", "readiness", "capture", "calculate"][index], number: index + 1, title, summary: ["Choose camera identities and acquisition settings.", "Bind the physical printed board to this run.", "Resolve all blockers in one place.", "Open cameras and authorize supervised robot motion.", "Estimate time alignment, compare candidates, and explicitly publish profiles."][index], status: calibrationStatuses[index], required: true,
  }))

  const datasetRequirements: WorkflowRequirement[] = [
    { id: "config", label: "Run configuration", description: configSaved ? "The dataset run configuration is saved." : "Save the dataset run and camera configuration first.", status: configSaved ? "met" : "missing", onFix: () => selectStep("configure"), fixLabel: "Open step 1" },
    { id: "cameras", label: "At least one enabled camera", description: enabledCameras.length ? `${enabledCameras.length} camera${enabledCameras.length === 1 ? " is" : "s are"} enabled for this dataset.` : "No camera is enabled for capture.", status: enabledCameras.length ? "met" : "missing", onFix: () => selectStep("configure"), fixLabel: "Choose cameras" },
    { id: "calibration", label: "Calibration geometry and automatic timing verified", description: calibrationRequirementDescription, status: datasetCalibrationSelected ? "met" : "missing", onFix: () => selectStep("configure"), fixLabel: "Review calibration" },
    { id: "template", label: "Pose-template placement confirmed", description: templateSelected ? "The immutable pose template and measured placement are confirmed." : "Select an immutable pose template and confirm its measured physical placement.", status: templateSelected ? "met" : "missing", onFix: () => selectStep("template"), fixLabel: "Choose pose template" },
  ]
  const datasetReady = readinessSatisfied(preflight, datasetRequirements)
  const datasetConfigured = configSaved && datasetCalibrationSelected
  const datasetStatuses = [
    ...stepStatuses([datasetConfigured, templateSelected, datasetReady, captureComplete, syncComplete && syncQualityComplete && rectificationComplete && bopComplete]),
    annotationComplete ? "complete" : bopComplete ? "ready" : "not_started",
  ] satisfies Array<WorkflowStepDefinition["status"]>
  if (["queued", "running", "canceling"].includes(datasetProcessingJobStatus ?? "")) datasetStatuses[4] = "running"
  if (["failed", "canceled", "cancelled"].includes(datasetProcessingJobStatus ?? "") && !bopComplete) datasetStatuses[4] = "blocked"
  const datasetSteps: WorkflowStepDefinition[] = datasetOutline.map((title, index) => ({
    id: ["configure", "template", "readiness", "capture", "sync", "export"][index], number: index + 1, title, summary: ["Reuse calibration that matches the selected cameras.", "Bind known object poses to the physical scene.", "Resolve all blockers in one place.", "Open cameras and authorize supervised robot motion.", "Synchronize, verify, rectify, and write the base BOP dataset.", "Optionally add pose or pose-and-mask annotations."][index], status: datasetStatuses[index], required: index < 5,
  }))

  if (page === "calibration") return <JourneyShell journey="calibration" steps={calibrationSteps} selectedStep={selectedStep} onSelectStep={selectStep}>
    <WorkflowStepCard id="configure" number={1} title="Configure the run and cameras" description="Choose the camera identities, resolution, frame rate, and supervised robot velocity for this calibration recording." status={calibrationStatuses[0]} help="This saves configuration only. It does not open a camera or command the robot.">
      <RunSetup intent="calibration" />
    </WorkflowStepCard>

    <WorkflowStepCard id="target" number={2} title="Choose the printed grid and its mounting" description="Select the immutable grid bundle that exactly matches the board, then record whether it is fixed or moves with the robot for this camera group." status={calibrationStatuses[1]} help="The target UUID, geometry hash, and physical mounting frame prevent detections from being interpreted with the wrong kinematic observation model.">
      <CalibrationArrangementCard arrangement={calibrationArrangement} editHref="/workflow/calibration?step=configure" testId="workflow-target-arrangement" />
      <Card><CardContent className="flex flex-col gap-4 py-5 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-start gap-3"><span className="grid size-10 shrink-0 place-items-center rounded-lg bg-muted"><Grid3X3 aria-hidden="true" className="size-5 text-primary-strong" /></span><div>{runConfig?.calibration_target ? <><div className={targetSelected ? "font-semibold" : "font-semibold text-destructive"}>{targetSelected ? "Calibration grid and mounting selected" : "Grid mounting does not match cameras"}</div><div className="mt-1 font-mono text-[11px] text-muted-foreground">{runConfig.calibration_target.target_id}</div><div className="mt-1 text-xs text-muted-foreground">Physical frame: {effectiveTargetMountingFrame?.replaceAll("_", " ") ?? "not recorded"} · Placement: {runConfig.calibration_target.placement.mode.replaceAll("_", " ")}</div></> : <><div className="font-semibold text-destructive">No grid selected</div><p className="mt-1 text-xs text-muted-foreground">Choose the physical board and mounting before readiness and capture.</p></>}</div></div><Button asChild variant={targetSelected ? "outline" : "default"}><Link to="/calibration-targets">{targetSelected ? "Review selected grid" : targetGeometrySelected ? "Correct grid mounting" : "Choose grid"}<ArrowRight aria-hidden="true" /></Link></Button></CardContent></Card>
      <OptionalAction icon={Sparkles} title="Reuse or create a printable grid" description="Saved targets are global reusable library entries. A fresh calibration run can select the same board again after cameras move; generate a new target only when the physical grid changes." to="/calibration-targets" action="Open target library" />
    </WorkflowStepCard>

    <WorkflowStepCard id="readiness" number={3} title="Check readiness" description="Run one consolidated operator check after cameras and the exact printed grid are selected." status={calibrationStatuses[2]} help="The saved report proves which configuration was checked. Physical capture repeats the time-sensitive safety checks at startup.">
      <CalibrationArrangementCard arrangement={calibrationArrangement} editHref="/workflow/calibration?step=configure" testId="workflow-readiness-arrangement" />
      <ReadinessCheck runRoot={selectedRun} intent="calibration" preflight={preflight} loading={config.isPending} requirements={calibrationRequirements} />
    </WorkflowStepCard>

    <WorkflowStepCard id="capture" number={4} title="Record calibration images" description="Mount the selected grid as described for the physical arrangement, clear the workcell, then authorize the supervised capture." status={calibrationStatuses[3]} help="For static-camera workcell calibration, the cameras stay fixed while the robot moves its attached grid through many views; the output places each camera in PoseTemplateBase. For robot-mounted cameras, the cameras move around a grid fixed in PoseTemplateBase.">
      <CalibrationArrangementCard arrangement={calibrationArrangement} editHref="/workflow/calibration?step=configure" testId="workflow-capture-arrangement" />
      <CaptureGate intent="calibration" readiness={{ ready: calibrationReady, onReview: () => selectStep("readiness") }} />
    </WorkflowStepCard>

    <WorkflowStepCard id="calculate" number={5} title="Calculate, review, and publish" description="Estimate camera/robot time alignment, process the captured grid observations, review every camera, and explicitly publish only passing profiles." status={calibrationStatuses[4]} help="Publishing is deliberate: calculated candidates remain inactive until you accept the reviewed recommendations.">
      <CalibrationArrangementCard arrangement={calibrationArrangement} editHref="/workflow/calibration?step=configure" testId="workflow-calculate-arrangement" />
      <Card className="border-primary/25 bg-primary/5"><CardContent className="py-4 text-xs leading-relaxed"><div className="flex items-center gap-2 font-semibold">Factory and OpenCV intrinsics <HelpTip label="Factory and OpenCV intrinsics">Factory is the per-camera projection supplied by the camera SDK. OpenCV is a new model fitted from this run's grid observations. Existing means an exact compatible profile was already available.</HelpTip></div><p className="mt-1 text-muted-foreground"><strong className="text-foreground">Factory</strong> stays selected when its projection is compatible. The fitted <strong className="text-foreground">OpenCV</strong> model is comparison and fallback evidence; it is activated only when factory projection is unusable and all coverage, held-out, plausibility, and error checks pass. A lower RMS alone does not make it the preferred model.</p></CardContent></Card>
      <CalibrationWorkflow arrangement={calibrationArrangement} referenceFramePath={runConfig?.frames?.robot_pose.sunrise_reference_frame_path} />
    </WorkflowStepCard>
  </JourneyShell>

  if (page === "dataset") {
    return <JourneyShell journey="dataset" steps={datasetSteps} selectedStep={selectedStep} onSelectStep={selectStep}>
      <WorkflowStepCard id="configure" number={1} title="Configure cameras and select calibration" description="Choose the cameras for this recording and select a published calibration made for those exact camera identities and acquisition settings." status={datasetStatuses[0]} help="A calibration profile maps camera pixels into the shared robot/template coordinate system. It is required for an object dataset.">
        <Card className={datasetCalibrationSnapshotConfigured ? "border-success/30" : "border-destructive/30"}><CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between"><div><div className="flex items-center gap-2 font-semibold">Selected calibration snapshot <StatusBadge status={datasetCalibrationSnapshotConfigured ? "configured" : "missing"} tone={datasetCalibrationSnapshotConfigured ? "informational" : "destructive"} /></div><p className="mt-1 text-xs text-muted-foreground">{datasetCalibrationSnapshotConfigured ? `Bundle ${runConfig?.calibration_profile_selection?.bundle_sha256.slice(0, 16)}… is copied into this run. Geometry and timing are revalidated before capture and export.` : "Required: select and validate a previously published calibration below."}</p></div><HelpTip label="selected calibration snapshot">PoseTestBot copies both profile files into this run and records their hashes, so later source-run changes cannot alter the dataset. Readiness rechecks every enabled camera and its saved time-alignment policy.</HelpTip></CardContent></Card>
        <CalibrationSyncPolicy configured={datasetCalibrationSnapshotConfigured} calibrationSync={calibrationSync} />
        <RunSetup intent="dataset" />
      </WorkflowStepCard>

      <WorkflowStepCard id="template" number={2} title="Choose the pose template and placement" description="Select the immutable printed pose template that is physically present, enter its measured transform into template base, and confirm it." status={datasetStatuses[1]} help="The pose template fixes object identities and relative poses. The measured placement locates the printed template in the robot's dataset reference frame.">
        <GroundTruthWorkflow />
        <div className="grid gap-3 md:grid-cols-2"><OptionalAction icon={Boxes} title="Add or edit workpieces" description="Manage source CAD, canonical geometry, names, tags, and lifecycle before making a new template." to="/workpieces" action="Open catalogue" /><OptionalAction icon={Grid3X3} title="Create a pose template" description="Lay out stable object orientations and publish a new immutable printable version." to="/pose-templates" action="Open templates" /></div>
      </WorkflowStepCard>

      <WorkflowStepCard id="readiness" number={3} title="Check readiness" description="Run one consolidated operator check after calibration and object placement are confirmed." status={datasetStatuses[2]} help="This is the only visible preflight step. The capture supervisor still repeats live checks immediately before hardware starts.">
        <ReadinessCheck runRoot={selectedRun} intent="dataset" preflight={preflight} loading={config.isPending} requirements={datasetRequirements} />
      </WorkflowStepCard>

      <WorkflowStepCard id="capture" number={4} title="Record the object dataset" description="Place the objects exactly as confirmed, clear the workcell, then authorize supervised camera and robot capture." status={datasetStatuses[3]} help="Raw RGB, depth, timestamp, and robot-pose evidence is preserved. Use a new run folder rather than overwriting a prior capture.">
        <CaptureGate intent="dataset" readiness={{ ready: datasetReady, onReview: () => selectStep("readiness") }} />
      </WorkflowStepCard>

      <WorkflowStepCard id="sync" number={5} title="Process frames and create the base BOP export" description="Synchronize frames to robot poses, verify match quality, validate calibration, rectify RGB-D data, and write the base image/model BOP dataset. Raw captures remain unchanged." status={datasetStatuses[4]} help="The processing job rejects missing matches, excessive pose gaps, incompatible timestamps, calibration-provenance mismatches, and invalid exports.">
        <Card data-testid="dataset-sync-timing-contract" className={datasetCalibrationSelected ? "border-primary/30 bg-primary/5" : "border-destructive/30 bg-destructive/5"}><CardContent className="flex items-start gap-3 py-4"><Database aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary-strong" /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2 text-sm font-semibold">Calibration timing <StatusBadge status={datasetCalibrationSelected ? "ready" : "blocked"} tone={datasetCalibrationSelected ? "success" : "destructive"} /><HelpTip label="automatic calibration timing">Processing uses the per-camera offset, timestamp fields, clock-domain rule, and pose-gap limit shown in Step 1. Manual values and generic defaults cannot override them, and export rechecks the evidence.</HelpTip></div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">{datasetCalibrationSelected ? "The selected per-camera timing policy will be applied and verified automatically." : "Return to Step 1 and select a calibration with valid timing for every enabled camera."}</p></div></CardContent></Card>
        <DatasetProcessing runRoot={selectedRun} ready={datasetReady} captureComplete={captureComplete} syncComplete={syncComplete} syncQualityComplete={syncQualityComplete} calibrationComplete={rectificationComplete} exportComplete={bopComplete} onReviewReadiness={() => selectStep("readiness")} onJobStatusChange={setDatasetProcessingJobStatus} />
      </WorkflowStepCard>

      <WorkflowStepCard id="export" number={6} title="Add optional BOP ground-truth evidence" description="After the base image/model export is verified, optionally add pose-only or rendered pose-and-mask annotations for this run." status={datasetStatuses[5]} required={false} help="The base BOP export is already a portable pose-estimation input. Pose + masks is optional and enables the Inspect page to evaluate an already compatible BOP19 result CSV.">
        <Card className={bopComplete ? "border-success/35 bg-success/5" : "border-dashed"}><CardContent className="flex flex-col gap-4 py-5 sm:flex-row sm:items-center sm:justify-between"><div><div className="font-semibold">{bopComplete ? "BOP image/model export is ready" : "BOP export has not completed"}</div><p className="mt-1 text-xs text-muted-foreground">{bopComplete ? "The base export has populated calibrated scenes, models, and object targets. You can finish here, add pose-only ground truth, or add rendered poses and masks for Inspect metrics." : "Use the processing job in step 5. It validates calibration, rectifies frames, copies models, and writes the base BOP scenes before optional ground-truth generation."}</p></div>{bopComplete ? <Button asChild variant="outline"><Link to="/cell">Review dataset in Cell View<ArrowRight aria-hidden="true" /></Link></Button> : <Button type="button" variant="outline" onClick={() => selectStep("sync")}>Open processing step</Button>}</CardContent></Card>
        <BopGroundTruthGeneration runRoot={selectedRun} bopExportComplete={bopComplete} />
      </WorkflowStepCard>
    </JourneyShell>
  }

  return <Navigate to="/workflow/setup" replace />
}
