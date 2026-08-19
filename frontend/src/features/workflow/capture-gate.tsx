import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, ArrowRight, Camera, LoaderCircle, RefreshCw, ShieldAlert } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { api, errorMessage, query } from "@/lib/api"
import type { CaptureState, PreflightSummary, RunConfig } from "@/lib/contracts"
import { useOperator } from "@/providers/operator-provider"
import { readinessBlockerCopy } from "@/features/workflow/readiness-copy"

const ACTIVE_CAPTURE_STATUSES = new Set(["queued", "running", "canceling"])

export interface CaptureGateReadiness {
  ready: boolean
  message?: string
  onReview?: () => void
}

export interface CaptureGateProps {
  intent: "calibration" | "dataset"
  readiness?: CaptureGateReadiness
}

const intentCopy = {
  calibration: {
    title: "Record calibration images",
    description: "Open the selected cameras and run the supervised calibration motion only after readiness passes.",
    open: "Review and start capture",
    dialogTitle: "Authorize calibration capture",
    supervision: "Calibration capture supervision",
    queued: "Calibration capture queued",
  },
  dataset: {
    title: "Record object dataset",
    description: "Open the selected cameras and run the supervised dataset motion using the confirmed calibration and object placement.",
    open: "Review and start capture",
    dialogTitle: "Authorize dataset capture",
    supervision: "Dataset capture supervision",
    queued: "Dataset capture queued",
  },
} as const

export function CaptureGate({ intent, readiness }: CaptureGateProps) {
  const { selectedRun } = useOperator()
  const queryClient = useQueryClient()
  const copy = intentCopy[intent]
  const [open, setOpen] = useState(false)
  const [captureAuthorized, setCaptureAuthorized] = useState(false)
  const [submittedCapture, setSubmittedCapture] = useState<{ runRoot: string; jobId: string } | null>(null)
  const config = useQuery({ queryKey: ["run-config", selectedRun], queryFn: () => api<{ config: RunConfig; preflight: PreflightSummary }>(query("/run-config", { run_root: selectedRun })), retry: false, refetchInterval: (state) => state.state.data?.preflight.queue_blocker ? 2_000 : false })
  const captureState = useQuery({
    queryKey: ["capture-jobs", selectedRun],
    queryFn: () => api<CaptureState>(query("/capture/jobs", { run_root: selectedRun })),
    refetchInterval: (state) => state.state.data?.active_count ? 1_000 : 5_000,
  })
  const recoveredActiveCapture = captureState.data?.jobs.find((job) => job.active && ACTIVE_CAPTURE_STATUSES.has(job.status)) ?? null
  const submittedJob = submittedCapture?.runRoot === selectedRun
    ? captureState.data?.jobs.find((job) => job.id === submittedCapture.jobId) ?? null
    : null
  const submittedCapturePending = Boolean(
    submittedCapture?.runRoot === selectedRun
    && (!submittedJob || ACTIVE_CAPTURE_STATUSES.has(submittedJob.status)),
  )
  const activeCapture = recoveredActiveCapture ?? (submittedCapturePending
    ? {
        id: submittedCapture!.jobId,
        name: intent === "dataset" ? "Dataset capture" : "Calibration capture",
        status: submittedJob?.status ?? "queued",
      }
    : null)
  const internalBlocker = config.data?.preflight.queue_blocker ?? (config.isError ? "missing_run_config" : null)
  const blocker = readiness ? (readiness.ready ? null : readiness.message ?? "readiness_incomplete") : internalBlocker
  const blockerCopy = readinessBlockerCopy(blocker)

  useEffect(() => {
    if (!submittedCapture || submittedCapture.runRoot !== selectedRun || !submittedJob || ACTIVE_CAPTURE_STATUSES.has(submittedJob.status)) return
    queueMicrotask(() => setSubmittedCapture((current) => current?.jobId === submittedCapture.jobId ? null : current))
  }, [selectedRun, submittedCapture, submittedJob])

  const preflight = useMutation({
    mutationFn: () => api<{ job_id: string }>("/preflight/jobs", { method: "POST", body: JSON.stringify({ run_root: selectedRun }) }),
    onSuccess: (data) => { toast.success("Preflight queued", { description: `Job ${data.job_id} continues after navigation; monitor it in Jobs.` }); queryClient.invalidateQueries({ queryKey: ["jobs"] }); queryClient.invalidateQueries({ queryKey: ["run-config", selectedRun] }) },
    onError: (error) => toast.error("Preflight was not queued", { description: errorMessage(error) }),
  })
  const capture = useMutation({
    mutationFn: async () => {
      const latestCaptureState = await captureState.refetch()
      if (latestCaptureState.error) throw new Error(`Selected-run capture status is unavailable: ${errorMessage(latestCaptureState.error)}`)
      const latestActiveCapture = latestCaptureState.data?.jobs.find((job) => job.active && ACTIVE_CAPTURE_STATUSES.has(job.status))
      if (latestActiveCapture) throw new Error(`Capture job ${latestActiveCapture.id} is already ${latestActiveCapture.status}`)
      await api("/sensors/previews/stop", { method: "POST", body: "{}" })
      return api<{ job_id: string }>("/capture/jobs", {
        method: "POST",
        body: JSON.stringify({
          run_root: selectedRun,
          intent,
          allow_cameras: true,
          allow_real_robot: true,
        }),
      })
    },
    onSuccess: (data) => { setSubmittedCapture({ runRoot: selectedRun, jobId: data.job_id }); toast.success(copy.queued, { description: `Job ${data.job_id} continues after navigation; status, logs, and stop controls are available in Jobs.` }); setOpen(false); setCaptureAuthorized(false); queryClient.invalidateQueries({ queryKey: ["jobs"] }); queryClient.invalidateQueries({ queryKey: ["capture-jobs", selectedRun] }); queryClient.invalidateQueries({ queryKey: ["overview", selectedRun] }); queryClient.invalidateQueries({ queryKey: ["calibration", "setup", selectedRun] }) },
    onError: (error) => toast.error("Physical capture was not queued", { description: errorMessage(error) }),
  })
  const resetOpen = (value: boolean) => { setOpen(value); setCaptureAuthorized(false) }

  const requestedCaptureSpeedMps = Number(config.data?.config.capture.velocity_m_s ?? 0.01)
  const usesExtendedDatasetSpeed = intent === "dataset" && requestedCaptureSpeedMps > 0.03

  return <Card className="border-warning/50 bg-warning/5"><CardHeader><CardTitle className="flex items-center gap-2"><ShieldAlert aria-hidden="true" className="size-5 text-warning-foreground" />{copy.title}</CardTitle><CardDescription>{copy.description}</CardDescription></CardHeader><CardContent>{config.isPending || captureState.isPending ? <div className="rounded-lg border border-border bg-muted/30 p-4 text-sm text-muted-foreground">Checking fresh readiness evidence and selected-run capture activity…</div> : activeCapture ? <div data-testid="capture-active-job" role="status" className="flex flex-col gap-4 rounded-lg border border-warning/40 bg-warning/10 p-4 sm:flex-row sm:items-start sm:justify-between"><div className="flex min-w-0 items-start gap-3"><LoaderCircle aria-hidden="true" className="mt-0.5 size-5 shrink-0 animate-spin text-warning-foreground" /><div className="min-w-0"><div className="flex flex-wrap items-center gap-2 font-semibold">{activeCapture.name} is {activeCapture.status}<StatusBadge status={activeCapture.status} tone="warning" /></div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">Job <span className="font-mono">{activeCapture.id}</span> continues after navigation. Jobs shows its live log, resource ownership, and stop controls. Another capture cannot be submitted while this job is active.</p></div></div><Button asChild variant="outline" size="sm" className="shrink-0 bg-card"><Link to="/jobs">Open capture in Jobs<ArrowRight aria-hidden="true" /></Link></Button></div> : captureState.isError ? <div role="alert" className="flex flex-col gap-4 rounded-lg border border-destructive/30 bg-destructive/5 p-4 sm:flex-row sm:items-start sm:justify-between"><div className="flex gap-3"><AlertTriangle aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-destructive" /><div><div className="font-semibold">Capture status unavailable</div><p className="mt-1 text-xs text-muted-foreground">The console cannot confirm that this run has no active capture. Recording remains disabled until selected-run capture status can be checked.</p></div></div><div className="flex shrink-0 flex-wrap gap-2"><Button variant="outline" size="sm" onClick={() => captureState.refetch()}><RefreshCw aria-hidden="true" />Retry status</Button><Button asChild variant="outline" size="sm"><Link to="/jobs">Open Jobs</Link></Button></div></div> : blocker ? <div className="flex flex-col gap-4 rounded-lg border border-destructive/30 bg-destructive/5 p-4 sm:flex-row sm:items-start sm:justify-between"><div className="flex gap-3"><AlertTriangle aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-destructive" /><div><div className="font-semibold">Capture blocked: {blockerCopy.heading}</div><p className="mt-1 text-xs text-muted-foreground">{blockerCopy.description} This console never submits override flags.</p></div></div>{readiness ? readiness.onReview && <Button variant="outline" onClick={readiness.onReview}>Review readiness</Button> : <Button onClick={() => preflight.mutate()} disabled={preflight.isPending}>{preflight.isPending ? "Queueing…" : "Run preflight"}</Button>}</div> : <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between"><div><div className="font-semibold">Readiness evidence is current</div><p className="mt-1 text-xs text-muted-foreground">Opening the dialog resets authorization. Capture rechecks selected-camera openability and empty outputs before writing capture artifacts.</p></div><Button variant="destructive" onClick={() => resetOpen(true)}><Camera aria-hidden="true" />{copy.open}</Button></div>}</CardContent>
      <Dialog open={open} onOpenChange={resetOpen}><DialogContent><DialogHeader><DialogTitle>{copy.dialogTitle}</DialogTitle><DialogDescription>This combined authorization is fresh for this request and is not stored in run_config.json or local storage.</DialogDescription></DialogHeader><div className="space-y-3"><div className="rounded-lg bg-muted p-4 text-sm"><div><span className="text-muted-foreground">Run</span><div className="mt-1 break-all font-mono font-semibold">{selectedRun}</div></div><div className="mt-3"><span className="text-muted-foreground">Fixed robot target</span><div className="mt-1 font-mono font-semibold">172.31.1.147:30300</div></div><div className="mt-3"><span className="text-muted-foreground">Requested capture speed</span><div className="mt-1 font-mono font-semibold">{requestedCaptureSpeedMps.toFixed(2)} m/s</div></div></div>{usesExtendedDatasetSpeed && <div data-testid="extended-dataset-speed-warning" className="flex items-start gap-3 rounded-lg border border-warning/50 bg-warning/10 p-3 text-xs"><AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning-foreground" /><div><div className="font-semibold">Dataset speed above the calibration limit</div><p className="mt-1 leading-relaxed text-muted-foreground">The strict structured command carries this request. Verify that the commissioned Sunrise application is active; it independently caps A1 at 3°/s. Speed alone cannot guarantee sharp frames—exposure time and lighting still matter.</p></div></div>}<div data-testid="capture-timeout-envelope" className="rounded-lg border border-primary/25 bg-primary/5 p-3 text-xs"><div className="font-semibold">{copy.supervision}</div><div className="mt-1 text-muted-foreground">720 s total · up to 15 s per camera startup attempt to publish 3 valid metadata records · 120 s to first robot packet · 60 s between robot packets</div></div>{activeCapture && <div role="alert" className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs"><div className="font-semibold">Another capture is already {activeCapture.status}</div><p className="mt-1 text-muted-foreground">Close this dialog and open Jobs to monitor or stop job <span className="font-mono">{activeCapture.id}</span>.</p></div>}<Label className="flex items-start gap-3 rounded-lg border p-4"><Checkbox data-testid="capture-authorization-ack" checked={captureAuthorized} onCheckedChange={(value) => setCaptureAuthorized(value === true)} /><span>I confirm the robot workcell is clear and the target is correct; I authorize supervised robot motion and opening the selected cameras, including stopping active previews.</span></Label></div><DialogFooter><Button variant="outline" onClick={() => resetOpen(false)}>Cancel</Button><Button data-testid="capture-submit" variant="destructive" disabled={!captureAuthorized || capture.isPending || Boolean(activeCapture) || captureState.isError} onClick={() => capture.mutate()}>{capture.isPending ? "Checking and starting…" : activeCapture ? "Capture already active" : "Start recording"}</Button></DialogFooter></DialogContent></Dialog>
    </Card>
}
