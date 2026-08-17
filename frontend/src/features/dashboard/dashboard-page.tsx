import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router-dom"
import { AlertTriangle, ArrowRight, Bot, Camera, CheckCircle2, CircleDot, Clock3, Cpu, HardDrive, ListChecks, Power, RefreshCw, Route, ShieldCheck, Square } from "lucide-react"
import { toast } from "sonner"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { StatusBadge, type StatusTone } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage, query } from "@/lib/api"
import type { BopAnnotationSetup, CaptureState, Job, Overview, RunStorage, SensorStatus } from "@/lib/contracts"
import { jobStatusTone } from "@/lib/jobs"
import { formatDate, titleCase } from "@/lib/utils"
import { workflowJourneyMetadata, type WorkflowJourneyId, type WorkflowProgressStatus } from "@/lib/workflow-session"
import { useOperator } from "@/providers/operator-provider"
import { RoomMonitor } from "@/features/dashboard/room-monitor"
import { ClusterControllerControl } from "@/features/dashboard/cluster-controller-control"

function SummaryCard({ icon: Icon, label, value, status, tone, detail }: { icon: typeof Bot; label: string; value: string; status?: string; tone: StatusTone; detail: string }) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="flex items-start justify-between"><div className="grid size-9 place-items-center rounded-lg bg-muted"><Icon className="size-4 text-primary-strong" /></div><StatusBadge status={status ?? value} tone={tone} /></div>
        <div className="mt-5 text-xs font-semibold uppercase tracking-wider text-muted-foreground">{label}</div>
        <div className="mt-1 font-display text-lg font-semibold">{value}</div>
        <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{detail}</p>
      </CardContent>
    </Card>
  )
}

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"])
const COMPLETE_EVIDENCE_STATUSES = new Set(["complete", "succeeded", "ok", "warning", "valid", "ready"])

interface DashboardWorkflowStep {
  id: string
  number: number
  title: string
  status: WorkflowProgressStatus
  evidenceBlocked: boolean
  required: boolean
}

interface DashboardWorkflowEvidence {
  journey: WorkflowJourneyId
  label: string
  description: string
  steps: DashboardWorkflowStep[]
}

function artifactComplete(overview: Overview, path: string, fallbackSectionId: string) {
  const artifact = overview.sidebar.flatMap((section) => section.artifacts).find((item) => item.path === path)
  if (artifact) return Boolean(artifact.exists && COMPLETE_EVIDENCE_STATUSES.has(artifact.status ?? ""))
  return overview.sidebar.find((section) => section.id === fallbackSectionId)?.status === "complete"
}

function workflowStatuses(completed: boolean[]): WorkflowProgressStatus[] {
  const firstIncomplete = completed.findIndex((value) => !value)
  return completed.map((value, index) => value ? "complete" : index === firstIncomplete ? "current" : "not_started")
}

function dashboardWorkflowEvidence(overview: Overview | undefined, annotationComplete = false): DashboardWorkflowEvidence | null {
  const config = overview?.config
  if (!overview || !config) return null

  const journey: WorkflowJourneyId = config.dataset_mode === "pose_template" ? "dataset" : "calibration"
  const metadata = workflowJourneyMetadata[journey]
  const readinessComplete = artifactComplete(overview, "run_preflight_report.json", "preflight")
  const captureComplete = artifactComplete(overview, "capture_execution_report.json", "capture")
  const calibrationComplete = artifactComplete(overview, "calibration_profiles.json", "calibration")
  const syncComplete = artifactComplete(overview, "sync_quality_report.json", "sync")
  const rectificationComplete = artifactComplete(overview, "camera_rectification_report.json", "bop")
  const bopComplete = artifactComplete(overview, "bop/bop_export_manifest.json", "bop")
  const datasetCalibrationConfigured = Boolean(
    config.calibration_profiles
    && config.intrinsic_calibration_profiles
    && config.calibration_profile_selection?.bundle_sha256
    && overview.calibration_sync.status === "ready",
  )

  const requiredCompleted = journey === "dataset"
    ? [
        datasetCalibrationConfigured,
        Boolean(config.pose_template?.placement_confirmed),
        readinessComplete,
        captureComplete,
        syncComplete && rectificationComplete && bopComplete,
      ]
    : [
        true,
        Boolean(config.calibration_target),
        readinessComplete,
        captureComplete,
        calibrationComplete,
      ]
  const requiredStatuses = workflowStatuses(requiredCompleted)
  const statuses: WorkflowProgressStatus[] = journey === "dataset"
    ? [...requiredStatuses, annotationComplete ? "complete" : bopComplete ? "ready" : "not_started"]
    : requiredStatuses
  const evidenceSections = journey === "dataset"
    ? [["run_setup"], ["run_setup"], ["preflight"], ["capture"], ["sync", "calibration", "bop"], []]
    : [["run_setup"], ["run_setup"], ["preflight"], ["capture"], ["calibration"]]

  return {
    journey,
    label: metadata.title,
    description: journey === "dataset"
      ? "5 required steps plus 1 optional ground-truth step. This run records an object dataset; a saved camera calibration is an input to step 1."
      : `${metadata.steps.length} required steps. This run records a printed grid and publishes reusable camera calibration.`,
    steps: metadata.steps.map((step, index) => {
      const evidenceBlocked = evidenceSections[index].some(
        (sectionId) => overview.sidebar.find((section) => section.id === sectionId)?.status === "blocked",
      )
      const evidenceCanBlockNow = ["current", "ready", "running"].includes(statuses[index])
      return {
        ...step,
        status: evidenceBlocked && evidenceCanBlockNow ? "blocked" : statuses[index],
        evidenceBlocked: Boolean(evidenceBlocked && evidenceCanBlockNow),
        required: step.required !== false,
      }
    }),
  }
}

function workflowStatusLabel(step: DashboardWorkflowStep) {
  if (step.status === "complete") return "Complete"
  if (step.evidenceBlocked) return "Blocked"
  if (step.status === "current") return "Current step"
  if (step.status === "ready") return step.required ? "Ready" : "Optional · ready"
  if (!step.required) return "Optional"
  return "Waiting"
}

function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable"
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  const digits = amount >= 100 || unit === 0 ? 0 : amount >= 10 ? 1 : 2
  return `${amount.toFixed(digits)} ${units[unit]}`
}

function StorageSummaryCard({ storage }: { storage?: RunStorage }) {
  const freePercent = storage?.free_fraction === null || storage?.free_fraction === undefined
    ? null
    : Math.max(0, Math.min(100, Math.round(storage.free_fraction * 100)))
  const usedPercent = freePercent === null ? 0 : 100 - freePercent
  const statusLabel = storage?.status === "error" ? "critical" : storage?.status ?? "unavailable"
  const detail = storage?.error
    ?? (storage?.filesystem_path
      ? `${freePercent}% free of ${formatBytes(storage.total_bytes)} on ${storage.filesystem_path}`
      : "Filesystem capacity could not be read.")

  return <Card data-testid="dashboard-storage">
    <CardContent className="pt-5">
      <div className="flex items-start justify-between">
        <div className="grid size-9 place-items-center rounded-lg bg-muted"><HardDrive className="size-4 text-primary-strong" /></div>
        <StatusBadge status={storage?.status} tone={storage?.status === "ready" ? "success" : storage?.status === "warning" ? "warning" : storage?.status === "error" ? "destructive" : "neutral"}>{statusLabel}</StatusBadge>
      </div>
      <div className="mt-5 flex items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Run storage
        <HelpTip label="storage thresholds">Warning below the smaller of 500 GiB or 15% capacity. Critical below the smaller of 100 GiB or 5%. Capacity is polled from the filesystem containing the selected run.</HelpTip>
      </div>
      <div className="mt-1 font-display text-lg font-semibold">{formatBytes(storage?.free_bytes)} free</div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label="Run filesystem space used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={usedPercent}>
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${usedPercent}%` }} />
      </div>
      <p className="mt-2 line-clamp-2 text-xs text-muted-foreground" title={detail}>{detail}</p>
    </CardContent>
  </Card>
}

function jobTimestamp(job: Job) {
  return job.ended_at ?? job.started_at ?? job.created_at
}

function jobRunRoot(job: Job) {
  return job.run_root
}

function jobScopeLabel(job: Job, selectedRun: string) {
  if (job.scope_kind === "run") return job.run_root === selectedRun ? "Active run" : "Other run"
  if (job.scope_kind === "library") return "Reusable library"
  if (job.scope_kind === "global") return "Lab-wide"
  return "Legacy unknown scope"
}

function DashboardJobRow({ job, selectedRun }: { job: Job; selectedRun: string }) {
  const timingLabel = job.status === "queued"
    ? `Queued ${formatDate(job.created_at)}`
    : job.status === "failed"
      ? `Failed ${formatDate(job.ended_at ?? job.created_at)}`
      : `Started ${formatDate(job.started_at ?? job.created_at)}`
  const runRoot = jobRunRoot(job)
  return <Link to="/jobs" data-testid={`dashboard-job-${job.id}`} data-job-id={job.id} aria-label={`Open ${job.name} in Jobs`} className="block rounded-lg border border-border p-3 transition-colors hover:border-primary/55 hover:bg-primary/5">
    <div className="flex min-w-0 items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold" title={job.name}>{job.name}</div>
        <div className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground"><Clock3 className="size-3 shrink-0" />{timingLabel}</div>
      </div>
      <StatusBadge status={job.status} tone={jobStatusTone(job.status)} />
    </div>
    <div className="mt-2 flex min-w-0 items-center gap-2 text-[10px] text-muted-foreground">
      <span className="shrink-0 font-semibold uppercase tracking-wider">{jobScopeLabel(job, selectedRun)}</span>
      {runRoot && <span className="truncate font-mono" title={runRoot}>{runRoot}</span>}
    </div>
    {job.message && <p className="mt-2 line-clamp-2 text-xs text-muted-foreground" title={job.message}>{job.message}</p>}
    {job.resources.length > 0 && <div className="mt-2 truncate text-[10px] font-semibold uppercase tracking-wider text-muted-foreground" title={job.resources.join(", ")}>{job.resources.join(" · ")}</div>}
  </Link>
}

function DashboardJobActivity({ jobs, pending, failed, selectedRun }: { jobs?: Job[]; pending: boolean; failed: boolean; selectedRun: string }) {
  const activeJobs = [...(jobs ?? [])]
    .filter((job) => ACTIVE_JOB_STATUSES.has(job.status))
    .sort((left, right) => jobTimestamp(right).localeCompare(jobTimestamp(left)))
  const recentFailures = [...(jobs ?? [])]
    .filter((job) => job.status === "failed")
    .sort((left, right) => jobTimestamp(right).localeCompare(jobTimestamp(left)))
    .slice(0, 3)

  return <Card data-testid="dashboard-job-activity" className="col-span-12 h-full overflow-hidden xl:col-span-5">
    <CardHeader className="border-b border-border bg-muted/20">
      <div className="flex items-start justify-between gap-3">
        <div><CardTitle className="flex items-center gap-2"><ListChecks className="size-4 text-primary-strong" />Job activity</CardTitle><CardDescription className="mt-1">Lab-wide: all queued or running work, plus the latest failures. Each entry names its run scope.</CardDescription></div>
        <Button asChild size="sm" variant="outline"><Link to="/jobs">Open Jobs <ArrowRight /></Link></Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-5 pt-4">
      {pending ? <div className="space-y-2"><Skeleton className="h-16" /><Skeleton className="h-16" /><Skeleton className="h-16" /></div> : failed ? <div role="alert" className="rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs text-destructive">Job status is unavailable. Open Jobs or refresh before starting acquisition work.</div> : <>
        <section aria-labelledby="active-jobs-heading">
          <div className="mb-2 flex items-center justify-between gap-3"><h4 id="active-jobs-heading" className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Active jobs</h4><StatusBadge status={activeJobs.length ? "running" : "available"} tone={activeJobs.length ? "warning" : "neutral"}>{activeJobs.length}</StatusBadge></div>
          {activeJobs.length > 0
            ? <div className="max-h-48 space-y-2 overflow-y-auto pr-1">{activeJobs.map((job) => <DashboardJobRow key={job.id} job={job} selectedRun={selectedRun} />)}</div>
            : <p className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">No queued or running jobs.</p>}
        </section>
        <section aria-labelledby="recent-failures-heading">
          <div className="mb-2 flex items-center justify-between gap-3"><h4 id="recent-failures-heading" className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Recent failures</h4><StatusBadge status={recentFailures.length ? "failed" : "available"} tone={recentFailures.length ? "destructive" : "neutral"}>{recentFailures.length}</StatusBadge></div>
          {recentFailures.length > 0
            ? <div className="space-y-2">{recentFailures.map((job) => <DashboardJobRow key={job.id} job={job} selectedRun={selectedRun} />)}</div>
            : <p className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">No failed jobs in retained history.</p>}
        </section>
      </>}
    </CardContent>
  </Card>
}

type IiwaCommand = "start_iiwa" | "stop_iiwa"

function IiwaQuickControls({ profileStatus }: { profileStatus: "checking" | "configured" | "error" }) {
  const { robotTarget } = useOperator()
  const queryClient = useQueryClient()
  const [command, setCommand] = useState<IiwaCommand | null>(null)
  const [commandConfirmed, setCommandConfirmed] = useState(false)
  const robotCommand = useMutation({
    mutationFn: (nextCommand: IiwaCommand) => api<{ job_id: string }>("/run-command", {
      method: "POST",
      body: JSON.stringify({ command: nextCommand, robot_ip: robotTarget.ip, robot_port: robotTarget.port, ...(nextCommand === "start_iiwa" ? { allow_real_robot: true, allow_cameras: true } : {}) }),
    }),
    onSuccess: (data, nextCommand) => {
      toast.success(nextCommand === "start_iiwa" ? "IIWA start queued" : "IIWA stop queued", { description: `Job ${data.job_id}` })
      setCommand(null)
      setCommandConfirmed(false)
      queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Robot command was not queued", { description: errorMessage(error) }),
  })
  const openCommand = (nextCommand: IiwaCommand) => {
    setCommandConfirmed(false)
    setCommand(nextCommand)
  }
  const setDialogOpen = (open: boolean) => {
    if (open) return
    setCommand(null)
    setCommandConfirmed(false)
  }

  return (
    <Card data-testid="iiwa-quick-controls">
      <CardContent className="pt-5">
        <div className="flex items-start justify-between"><div className="grid size-9 place-items-center rounded-lg bg-muted"><Bot className="size-4 text-primary-strong" /></div><StatusBadge status={profileStatus} tone={profileStatus === "configured" ? "informational" : profileStatus === "error" ? "destructive" : "neutral"}>{profileStatus}</StatusBadge></div>
        <div className="mt-5 flex items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Manual robot control <HelpTip label="robot control status">Configured means the fixed lab profile loaded. The command target is the browser-local manual target shown in Devices; this status does not contact the robot or prove that Sunrise is running.</HelpTip></div>
        <div className="mt-1 font-display text-lg font-semibold">Lab IIWA</div>
        <p className="mt-1 text-xs text-muted-foreground">Quick commands use this browser's manual target and require confirmation.</p>
        <div className="mt-4 grid grid-cols-2 gap-2"><Button size="sm" onClick={() => openCommand("start_iiwa")} disabled={robotCommand.isPending}><Power />Start IIWA</Button><Button size="sm" variant="destructive" onClick={() => openCommand("stop_iiwa")} disabled={robotCommand.isPending}><Square />Stop IIWA</Button></div>
      </CardContent>
      <Dialog open={command !== null} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>Confirm IIWA {command === "stop_iiwa" ? "stop" : "start"}</DialogTitle><DialogDescription>{command === "stop_iiwa" ? "Stopping" : "Starting"} sends a command to the configured lab robot target.</DialogDescription></DialogHeader>
          {command === "stop_iiwa" && <div data-testid="iiwa-stop-warning" className="flex items-start gap-3 rounded-lg border border-destructive/45 bg-destructive/10 p-4 text-sm"><AlertTriangle className="mt-0.5 size-5 shrink-0 text-destructive" /><div><div className="font-semibold text-destructive">IIWA STOP is not a safety stop</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">It cannot interrupt active motion. In the current calibration application it exits the waiting program, so Sunrise must be restarted manually before another START.</p></div></div>}
          <div className="rounded-lg border border-warning/40 bg-warning/10 p-4"><div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Target</div><div className="mt-1 font-mono text-lg font-semibold">{robotTarget.ip}:{robotTarget.port}</div>{command === "start_iiwa" && <div className="mt-3 text-xs"><span className="font-semibold">Manual test request:</span> 0.1 m/s (100 mm/s)</div>}</div>
          <Label className="flex items-start gap-3 rounded-lg border p-3"><Checkbox data-testid="iiwa-command-confirmation" checked={commandConfirmed} onCheckedChange={(value) => setCommandConfirmed(value === true)} /><span className="space-y-1"><span className="block">I confirm this is the intended lab IIWA target.</span>{command === "start_iiwa" && <><span className="block">I authorize motion of the real lab IIWA for this start.</span><span className="block">I confirm the capture cameras and pose receiver are ready.</span></>}</span></Label>
          <DialogFooter><Button variant="outline" onClick={() => setDialogOpen(false)}>Cancel</Button><Button variant={command === "stop_iiwa" ? "destructive" : "default"} disabled={!commandConfirmed || robotCommand.isPending || command === null} onClick={() => command && robotCommand.mutate(command)}>{command === "stop_iiwa" ? <Square /> : <Power />}{robotCommand.isPending ? "Queueing…" : command === "stop_iiwa" ? "Queue stop" : "Queue start"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

export function DashboardPage() {
  const { selectedRun } = useOperator()
  const queryClient = useQueryClient()
  const overview = useQuery({ queryKey: ["overview", selectedRun], queryFn: () => api<Overview>(query("/ui/overview", { run_root: selectedRun })) })
  const storage = useQuery({ queryKey: ["storage", selectedRun], queryFn: () => api<RunStorage>(query("/ui/storage", { run_root: selectedRun })), refetchInterval: 5_000 })
  const sensors = useQuery({ queryKey: ["sensors", "status"], queryFn: () => api<SensorStatus>("/sensors/status"), staleTime: 10_000 })
  const robot = useQuery({ queryKey: ["robot", "status"], queryFn: () => api<Record<string, unknown>>("/robot/status"), staleTime: 10_000 })
  const runtime = useQuery({ queryKey: ["runtime", "status"], queryFn: () => api<Record<string, unknown>>("/runtime/status"), staleTime: 10_000 })
  const capture = useQuery({ queryKey: ["capture-jobs", selectedRun], queryFn: () => api<CaptureState>(query("/capture/jobs", { run_root: selectedRun })), refetchInterval: (state) => state.state.data?.active_count ? 1_000 : 5_000 })
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api<{ jobs: Job[]; resources: Record<string, string> }>("/jobs"), refetchInterval: 1_000 })
  const annotationSetup = useQuery({
    queryKey: ["bop-annotations", "setup", selectedRun],
    queryFn: () => api<BopAnnotationSetup>(query("/bop/annotations/setup", { run_root: selectedRun })),
    enabled: overview.data?.config?.dataset_mode === "pose_template",
    retry: false,
  })
  const stopCapture = useMutation({
    mutationFn: (jobId: string) => api(`/capture/jobs/${jobId}/stop`, { method: "POST", body: "{}" }),
    onSuccess: () => { toast.success("Capture stop requested"); queryClient.invalidateQueries({ queryKey: ["capture-jobs", selectedRun] }); queryClient.invalidateQueries({ queryKey: ["jobs"] }) },
    onError: (error) => toast.error("Capture could not be stopped", { description: errorMessage(error) }),
  })

  const activeCapture = capture.data?.jobs.find((job) => job.active)
  const sections = overview.data?.sidebar ?? []
  const preflight = sections.find((item) => item.id === "preflight")
  const runtimeItems = Array.isArray(runtime.data?.runtimes) ? runtime.data.runtimes as Array<{ available?: boolean }> : []
  const availableRuntimes = runtimeItems.filter((item) => item.available).length
  const workflowEvidence = dashboardWorkflowEvidence(overview.data, Boolean(annotationSetup.data?.current_output?.verified))

  const refresh = () => queryClient.invalidateQueries({ predicate: (item) => ["overview", "storage", "sensors", "robot", "runtime", "capture-jobs", "jobs", "bop-annotations", "cluster-controller-service", "cluster-status"].includes(String(item.queryKey[0])) })
  const statusErrors = [
    overview.isError && "run evidence",
    storage.isError && "run storage",
    sensors.isError && "sensor discovery",
    robot.isError && "robot profile",
    runtime.isError && "runtime status",
    capture.isError && "capture status",
    jobs.isError && "job status",
  ].filter(Boolean) as string[]
  const robotProfileStatus = robot.isPending ? "checking" : robot.isError ? "error" : "configured"

  return (
    <div className="space-y-6">
      <PageHeader eyebrow="Current run" title="Acquisition readiness" description="Live workcell visibility, background work, storage capacity, and artifact-backed readiness for the selected run." actions={<Button variant="outline" onClick={refresh}><RefreshCw />Refresh</Button>} />

      {statusErrors.length > 0 && <div role="alert" className="flex flex-col gap-3 rounded-xl border border-destructive/35 bg-destructive/5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-start gap-3"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" /><div><div className="text-sm font-semibold">Some dashboard status is unavailable</div><p className="mt-1 text-xs text-muted-foreground">Could not load {statusErrors.join(", ")}. Missing responses are not treated as ready; refresh before relying on this overview.</p></div></div><Button variant="outline" size="sm" onClick={refresh}><RefreshCw />Refresh status</Button></div>}
      {activeCapture && <div className="flex flex-col items-start justify-between gap-4 rounded-xl border border-primary/35 bg-primary/10 px-5 py-4 sm:flex-row sm:items-center"><div className="flex items-center gap-3"><span className="relative flex size-3"><span className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-60" /><span className="relative inline-flex size-3 rounded-full bg-primary" /></span><div><div className="font-semibold">Capture is {activeCapture.status}</div><div className="text-xs text-muted-foreground">{activeCapture.name} · continues after navigation; use Jobs for live logs and stop controls</div></div></div><div className="flex flex-wrap gap-2"><Button variant="destructive" size="sm" onClick={() => stopCapture.mutate(activeCapture.id)} disabled={stopCapture.isPending || activeCapture.status === "canceling"}><Square />{stopCapture.isPending || activeCapture.status === "canceling" ? "Stopping…" : "Stop capture"}</Button><Button asChild size="sm"><Link to="/jobs">Open controls <ArrowRight /></Link></Button></div></div>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
        {overview.isPending || storage.isPending || sensors.isPending ? Array.from({ length: 6 }).map((_, index) => <Skeleton className="h-40" key={index} />) : <>
          <StorageSummaryCard storage={storage.data} />
          <SummaryCard icon={ShieldCheck} label="Readiness check" value={titleCase(preflight?.status ?? "pending")} status={preflight?.status} tone={preflight?.status === "complete" ? "success" : preflight?.status === "blocked" ? "destructive" : "neutral"} detail={preflight?.status === "complete" ? "Artifact-backed readiness evidence is present." : "Check or refresh readiness before recording."} />
          <SummaryCard icon={Camera} label="Sensors" value={`${sensors.data?.total_connected ?? 0} connected`} status={sensors.data?.all_expected_connected ? "connected" : "warning"} tone={sensors.data?.all_expected_connected ? "informational" : "warning"} detail="RealSense, OAK-D Pro, and ZED discovery." />
          <IiwaQuickControls profileStatus={robotProfileStatus} />
          <SummaryCard icon={Cpu} label="Optional runtimes" value={`${availableRuntimes}/${runtimeItems.length} available`} status={availableRuntimes === runtimeItems.length ? "ready" : "warning"} tone={availableRuntimes === runtimeItems.length ? "success" : "warning"} detail="BlenderProc and Stereolabs SDK visibility." />
          <ClusterControllerControl />
        </>}
      </div>

      <div className="operator-grid">
        <RoomMonitor />
        <DashboardJobActivity jobs={jobs.data?.jobs} pending={jobs.isPending} failed={jobs.isError} selectedRun={selectedRun} />
      </div>

      {overview.isPending ? <Card data-testid="dashboard-workflow-overview"><CardHeader><Skeleton className="h-6 w-52" /><Skeleton className="h-4 w-full max-w-2xl" /></CardHeader><CardContent><Skeleton className="h-28 w-full" /></CardContent></Card> : workflowEvidence ? <Card data-testid="dashboard-workflow-overview" data-workflow-journey={workflowEvidence.journey}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Route className="size-5 text-primary-strong" />{workflowEvidence.label} workflow</CardTitle>
          <CardDescription>{workflowEvidence.description} The journey is selected from the saved run configuration; progress comes from durable run artifacts.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className={`grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 ${workflowEvidence.journey === "dataset" ? "xl:grid-cols-6" : "xl:grid-cols-5"}`}>
            {workflowEvidence.steps.map((step) => <Link to={`/workflow/${workflowEvidence.journey}?step=${step.id}`} aria-label={`Open ${workflowEvidence.label.toLowerCase()} step ${step.number}: ${step.title}`} data-workflow-step={step.id} key={step.id} className="group relative rounded-lg border border-border p-3 transition hover:border-primary/60 hover:bg-primary/5">
              <div className="mb-5 flex items-center justify-between">
                <span className="text-[10px] font-bold text-muted-foreground">{String(step.number).padStart(2, "0")}</span>
                {step.status === "complete"
                  ? <CheckCircle2 className="size-4 text-success" aria-label="Complete" />
                  : step.evidenceBlocked
                    ? <AlertTriangle className="size-4 text-destructive" aria-label="Blocked" />
                    : <CircleDot className={step.status === "current" || step.status === "ready" ? "size-4 text-primary-strong" : "size-4 text-muted-foreground"} aria-label={step.status === "current" ? "Current step" : step.status === "ready" ? "Ready" : "Waiting"} />}
              </div>
              <div className="text-sm font-semibold">{step.title}</div>
              <div className="mt-1 text-xs text-muted-foreground">{workflowStatusLabel(step)}</div>
            </Link>)}
          </div>
        </CardContent>
      </Card> : <Card data-testid="dashboard-workflow-overview" data-workflow-journey="unconfigured">
        <CardHeader><CardTitle className="flex items-center gap-2"><Route className="size-5 text-primary-strong" />Choose a workflow</CardTitle><CardDescription>No workflow intent is saved for this run yet. Choose camera calibration or object dataset acquisition; saving step 1 makes this overview follow that outcome.</CardDescription></CardHeader>
        <CardContent><Button asChild><Link to="/workflow/setup">Choose workflow <ArrowRight /></Link></Button></CardContent>
      </Card>}
    </div>
  )
}
