import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, ArrowRight, Check, Circle, LoaderCircle, Play, RefreshCw, ShieldCheck } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, errorMessage } from "@/lib/api"
import type { Job } from "@/lib/contracts"
import { jobStatusTone } from "@/lib/jobs"
import { cn, formatDate } from "@/lib/utils"

interface DatasetProcessingProps {
  runRoot: string
  ready: boolean
  captureComplete: boolean
  syncComplete: boolean
  syncQualityComplete: boolean
  calibrationComplete: boolean
  exportComplete: boolean
  onReviewReadiness: () => void
  onJobStatusChange?: (status: string | null) => void
}

const outcomes = [
  { id: "sync", label: "Match camera frames to robot poses" },
  { id: "quality", label: "Verify timestamp and match quality" },
  { id: "calibration", label: "Validate calibration and rectify RGB-D frames" },
  { id: "export", label: "Copy models and write the base BOP dataset" },
]

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"])
const FAILED_JOB_STATUSES = new Set(["failed", "canceled", "cancelled"])
const TERMINAL_JOB_STATUSES = new Set(["succeeded", ...FAILED_JOB_STATUSES])

function isDatasetProcessingJob(job: Job, runRoot: string) {
  return job.scope_kind === "run"
    && job.run_root === runRoot
    && job.parameters.purpose === "dataset_processing"
}

type OutcomeState = "complete" | "queued" | "running" | "verifying" | "failed" | "waiting"

const outcomePresentation: Record<OutcomeState, { label: string; className: string }> = {
  complete: { label: "Complete", className: "bg-success text-success-foreground" },
  queued: { label: "Queued", className: "bg-warning/15 text-warning-foreground" },
  running: { label: "Running", className: "bg-warning/15 text-warning-foreground" },
  verifying: { label: "Verifying", className: "bg-primary/10 text-primary-strong" },
  failed: { label: "Needs attention", className: "bg-destructive/10 text-destructive" },
  waiting: { label: "Waiting", className: "bg-muted text-muted-foreground" },
}

export function DatasetProcessing({ runRoot, ready, captureComplete, syncComplete, syncQualityComplete, calibrationComplete, exportComplete, onReviewReadiness, onJobStatusChange }: DatasetProcessingProps) {
  const queryClient = useQueryClient()
  const [submittedJob, setSubmittedJob] = useState<{ id: string; runRoot: string } | null>(null)
  const submittedJobId = submittedJob?.runRoot === runRoot ? submittedJob.id : null
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<{ jobs: Job[]; resources: Record<string, string> }>("/jobs"),
    refetchInterval: (queryState) => queryState.state.data?.jobs.some((job) => isDatasetProcessingJob(job, runRoot) && ACTIVE_JOB_STATUSES.has(job.status)) ? 1_000 : 5_000,
  })
  const latestPersistedJob = useMemo(
    () => [...(jobs.data?.jobs ?? [])]
      .filter((job) => isDatasetProcessingJob(job, runRoot))
      .sort((left, right) => right.created_at.localeCompare(left.created_at))[0] ?? null,
    [jobs.data, runRoot],
  )
  const currentJob = submittedJobId
    ? jobs.data?.jobs.find((job) => job.id === submittedJobId) ?? null
    : latestPersistedJob
  const currentJobId = submittedJobId ?? currentJob?.id ?? null
  const currentJobStatus = currentJob?.status ?? (submittedJobId ? "queued" : null)
  const active = ACTIVE_JOB_STATUSES.has(currentJobStatus ?? "")
  const failed = FAILED_JOB_STATUSES.has(currentJobStatus ?? "")
  const succeeded = currentJobStatus === "succeeded"
  const process = useMutation({
    mutationFn: () => api<{ job_id: string }>("/dataset-processing/jobs", {
      method: "POST",
      body: JSON.stringify({ run_root: runRoot }),
    }),
    onSuccess: (data) => {
      setSubmittedJob({ id: data.job_id, runRoot })
      toast.success("Dataset processing queued", { description: `Job ${data.job_id} continues after navigation; status and logs are available in Jobs.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
      void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
      void queryClient.invalidateQueries({ queryKey: ["run-config", runRoot] })
    },
    onError: (error) => toast.error("Dataset processing was not queued", { description: errorMessage(error) }),
  })
  const complete = [syncComplete, syncQualityComplete, calibrationComplete, exportComplete]
  const firstIncompleteIndex = complete.findIndex((value) => !value)
  const activeOutcomeIndex = firstIncompleteIndex >= 0 ? firstIncompleteIndex : outcomes.length - 1
  const blocked = !ready || !captureComplete
  const showStatus = Boolean(currentJobId || exportComplete || jobs.isError)

  useEffect(() => {
    onJobStatusChange?.(currentJobStatus)
  }, [currentJobStatus, onJobStatusChange])

  useEffect(() => {
    if (!currentJobId || !TERMINAL_JOB_STATUSES.has(currentJobStatus ?? "")) return
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["run-config", runRoot] })
  }, [currentJobId, currentJobStatus, queryClient, runRoot])

  const refreshEvidence = () => {
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["run-config", runRoot] })
  }

  const outcomeState = (index: number): OutcomeState => {
    if (complete[index]) return "complete"
    if (active && index === activeOutcomeIndex) return currentJobStatus === "queued" ? "queued" : "running"
    if (succeeded && index === activeOutcomeIndex) return "verifying"
    if (failed && index === activeOutcomeIndex) return "failed"
    return "waiting"
  }

  const verified = exportComplete && !active && !failed
  const statusTitle = active
    ? currentJobStatus === "queued" ? "Dataset processing is queued" : currentJobStatus === "canceling" ? "Dataset processing is canceling" : "Dataset processing is running"
    : failed
      ? currentJobStatus === "canceled" || currentJobStatus === "cancelled" ? "Dataset processing was canceled" : "Dataset processing failed"
      : succeeded && !exportComplete
        ? "Processing finished; export evidence is still being verified"
        : verified
          ? "Dataset processing finished and BOP export is verified"
          : "Job status is temporarily unavailable"
  const statusDescription = active
    ? currentJobStatus === "queued"
      ? `Job ${currentJobId} is waiting for its CPU and disk resources. It continues after navigation; Jobs shows resource locks, live output, and cancellation.`
      : `Job ${currentJobId} is executing the fixed four-command recipe. The next unverified outcome is “${outcomes[activeOutcomeIndex].label}”. It continues after navigation; Jobs has the live process log and cancellation.`
    : failed
      ? `Job ${currentJobId} ended with status ${currentJobStatus}${currentJob?.returncode == null ? "" : ` and return code ${currentJob.returncode}`}. Raw capture evidence was preserved. Review the job output, resolve the reported cause, then retry here.`
      : succeeded && !exportComplete
        ? `Job ${currentJobId} returned successfully${currentJob?.ended_at ? ` at ${formatDate(currentJob.ended_at)}` : ""}, but this page has not yet accepted bop/bop_export_manifest.json as durable completion evidence. Refresh the evidence; if it remains open, use Jobs to copy the process output and context for debugging.`
        : verified
          ? `${currentJobId ? `Job ${currentJobId} completed successfully. ` : ""}The synchronized RGB-D scenes, models, camera metadata, and populated targets have durable export evidence and are ready for pose-estimator input.`
          : "The workflow cannot currently read persistent job history. Refresh evidence or open Jobs to inspect the local runner."
  const StatusIcon = active ? LoaderCircle : failed ? AlertTriangle : verified ? Check : RefreshCw
  const statusBadgeStatus = active ? currentJobStatus : failed ? currentJobStatus : verified ? "succeeded" : "warning"

  return <Card data-testid="dataset-processing" className="border-primary/25">
    <CardHeader>
      <CardTitle className="text-base">Process the recorded dataset</CardTitle>
      <CardDescription>One queued job runs the fixed four-command recipe below. It synchronizes, validates, rectifies, and writes the base image/model BOP dataset. Ground-truth generation is chosen separately in optional step 6. Raw camera frames and robot poses are never renamed or replaced.</CardDescription>
    </CardHeader>
    <CardContent className="space-y-5">
      <ol className="grid gap-2 sm:grid-cols-2" aria-label="Automatic dataset processing">
        {outcomes.map((outcome, index) => {
          const state = outcomeState(index)
          const presentation = outcomePresentation[state]
          return <li key={outcome.id} className={cn("flex items-start gap-3 rounded-lg border p-3", state === "running" || state === "queued" ? "border-warning/35 bg-warning/5" : state === "failed" ? "border-destructive/35 bg-destructive/5" : "bg-muted/20")}>
            <span className={cn("grid size-6 shrink-0 place-items-center rounded-full", presentation.className)}>
              {state === "complete"
                ? <Check aria-hidden="true" className="size-3.5" />
                : state === "running" || state === "queued"
                  ? <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />
                  : state === "failed"
                    ? <AlertTriangle aria-hidden="true" className="size-3.5" />
                    : state === "verifying"
                      ? <RefreshCw aria-hidden="true" className="size-3.5 animate-spin" />
                      : <Circle aria-hidden="true" className="size-3" />}
            </span>
            <span className="min-w-0"><span className="flex items-center justify-between gap-2"><span className="font-mono text-[10px] font-bold text-muted-foreground">{index + 1}</span><span className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">{presentation.label}</span></span><span className="mt-0.5 block text-xs font-semibold">{outcome.label}</span></span>
          </li>
        })}
      </ol>
      {showStatus && <div data-testid="dataset-processing-job-status" role="status" className={cn("rounded-lg border p-4", active ? "border-warning/40 bg-warning/5" : failed ? "border-destructive/40 bg-destructive/5" : verified ? "border-success/35 bg-success/5" : "border-primary/35 bg-primary/5")}>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <StatusIcon aria-hidden="true" className={cn("mt-0.5 size-5 shrink-0", active && "animate-spin", failed ? "text-destructive" : verified ? "text-success" : "text-primary-strong")} />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{statusTitle}</span><StatusBadge status={statusBadgeStatus} tone={verified ? "success" : jobStatusTone(currentJobStatus)}>{verified ? "verified" : currentJobStatus ?? "unavailable"}</StatusBadge></div>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{statusDescription}</p>
              {currentJob?.message && failed && <p className="mt-2 font-mono text-[10px] text-destructive">{currentJob.message}</p>}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            {!verified && !active && <Button type="button" variant="outline" size="sm" onClick={refreshEvidence}><RefreshCw aria-hidden="true" />Refresh evidence</Button>}
            {currentJobId && <Button asChild variant="outline" size="sm"><Link to="/jobs">{active ? "Open live log in Jobs" : "Open job details"}<ArrowRight aria-hidden="true" /></Link></Button>}
          </div>
        </div>
      </div>}
      {blocked ? <div className="flex flex-col gap-3 rounded-lg border border-warning/35 bg-warning/5 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-2 text-xs text-muted-foreground"><ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning-foreground" /><span>{!ready ? "Complete the readiness step before processing this run." : "Record the object dataset before processing it."}</span></div>
        {!ready && <Button type="button" variant="outline" size="sm" onClick={onReviewReadiness}>Review readiness</Button>}
      </div> : <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-muted-foreground">Calibration validation is automatic here; there is no second operator preflight. The queued job continues after navigation and is recovered from persistent job history when you return. After the base export is verified, step 6 offers optional pose-only or pose-and-mask ground truth.</p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" onClick={() => process.mutate()} disabled={process.isPending || active}>
            {process.isPending || active ? <LoaderCircle aria-hidden="true" className="animate-spin" /> : <Play aria-hidden="true" />}
            {process.isPending ? "Queueing…" : active ? "Processing…" : failed ? "Retry processing" : exportComplete ? "Rebuild dataset" : "Process and export dataset"}
          </Button>
        </div>
      </div>}
    </CardContent>
  </Card>
}
