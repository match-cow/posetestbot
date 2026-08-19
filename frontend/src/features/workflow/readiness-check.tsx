import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, ArrowRight, LoaderCircle, RefreshCw, ShieldCheck } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { HelpTip } from "@/components/help-tip"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, errorMessage } from "@/lib/api"
import type { Job, PreflightSummary } from "@/lib/contracts"
import { jobStatusTone } from "@/lib/jobs"
import { readinessBlockerCopy } from "@/features/workflow/readiness-copy"
import { RequirementList, type WorkflowRequirement } from "@/features/workflow/workflow-steps"

export interface ReadinessCheckProps {
  runRoot: string
  intent: "calibration" | "dataset"
  preflight?: PreflightSummary | null
  loading?: boolean
  requirements: WorkflowRequirement[]
}

export function readinessSatisfied(preflight: PreflightSummary | null | undefined, requirements: WorkflowRequirement[]) {
  return preflight?.queue_blocker === null && requirements.every((item) => item.required === false || item.status === "met")
}

function blockerDescription(blocker: string | null | undefined, loading: boolean) {
  if (loading) return "Reading the latest durable readiness evidence…"
  return readinessBlockerCopy(blocker).description
}

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"])
const FAILED_JOB_STATUSES = new Set(["failed", "canceled", "cancelled"])
const TERMINAL_JOB_STATUSES = new Set(["succeeded", ...FAILED_JOB_STATUSES])

function isReadinessJob(job: Job, runRoot: string) {
  return job.scope_kind === "run"
    && job.run_root === runRoot
    && job.parameters.purpose === "preflight"
}

export function ReadinessCheck({ runRoot, intent, preflight, loading = false, requirements }: ReadinessCheckProps) {
  const queryClient = useQueryClient()
  const [submittedJob, setSubmittedJob] = useState<{ id: string; runRoot: string } | null>(null)
  const submittedJobId = submittedJob?.runRoot === runRoot ? submittedJob.id : null
  const ready = readinessSatisfied(preflight, requirements)
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<{ jobs: Job[]; resources: Record<string, string> }>("/jobs"),
    refetchInterval: (state) => {
      const history = state.state.data?.jobs ?? []
      const submittedJobMissing = Boolean(submittedJobId && !history.some((job) => job.id === submittedJobId))
      const activeReadinessJob = history.some((job) => isReadinessJob(job, runRoot) && ACTIVE_JOB_STATUSES.has(job.status))
      return submittedJobMissing || activeReadinessJob ? 1_000 : 5_000
    },
  })
  const matchingJobs = useMemo(
    () => [...(jobs.data?.jobs ?? [])]
      .filter((job) => isReadinessJob(job, runRoot))
      .sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [jobs.data, runRoot],
  )
  const persistedSubmittedJob = submittedJobId
    ? matchingJobs.find((job) => job.id === submittedJobId) ?? null
    : null
  const submittedJobMissing = Boolean(submittedJobId && jobs.data && !persistedSubmittedJob)
  const currentJob = submittedJobMissing
    ? null
    : matchingJobs.find((job) => ACTIVE_JOB_STATUSES.has(job.status))
      ?? persistedSubmittedJob
      ?? matchingJobs[0]
      ?? null
  const currentJobId = submittedJobMissing ? submittedJobId : currentJob?.id ?? null
  const currentJobStatus = submittedJobMissing ? "queued" : currentJob?.status ?? null
  const active = ACTIVE_JOB_STATUSES.has(currentJobStatus ?? "")
  const failed = FAILED_JOB_STATUSES.has(currentJobStatus ?? "")
  const succeeded = currentJobStatus === "succeeded"
  const jobHistoryPending = jobs.isPending
  const jobHistoryUnavailable = jobs.isError

  const check = useMutation({
    mutationFn: () => api<{ job_id: string }>("/preflight/jobs", {
      method: "POST",
      body: JSON.stringify({ run_root: runRoot }),
    }),
    onSuccess: (data) => {
      setSubmittedJob({ id: data.job_id, runRoot })
      toast.success("Readiness check queued", { description: `Job ${data.job_id} continues after navigation; status and output are available in Jobs.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
      void queryClient.invalidateQueries({ queryKey: ["run-config", runRoot] })
      void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
    },
    onError: (error) => toast.error("Readiness check was not queued", { description: errorMessage(error) }),
  })

  useEffect(() => {
    if (!currentJobId || !TERMINAL_JOB_STATUSES.has(currentJobStatus ?? "") || jobHistoryUnavailable) return
    void queryClient.invalidateQueries({ queryKey: ["run-config", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
  }, [currentJobId, currentJobStatus, jobHistoryUnavailable, queryClient, runRoot])

  const coreRequirement: WorkflowRequirement = {
    id: "run-preflight",
    label: loading ? "Checking saved run readiness" : readinessBlockerCopy(preflight?.queue_blocker).heading,
    description: blockerDescription(preflight?.queue_blocker, loading),
    status: loading ? "checking" : preflight?.queue_blocker === null ? "met" : "missing",
    required: true,
  }

  const refreshEvidence = () => {
    void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    void queryClient.invalidateQueries({ queryKey: ["run-config", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
  }

  const jobStatusTitle = active
    ? currentJobStatus === "queued"
      ? "Readiness check is queued"
      : currentJobStatus === "canceling"
        ? "Readiness check is canceling"
        : "Readiness check is running"
    : failed
      ? currentJobStatus === "canceled" || currentJobStatus === "cancelled"
        ? "Readiness check was canceled"
        : "Readiness check failed"
      : succeeded
        ? "Readiness job finished; saved evidence is authoritative"
        : null
  const jobStatusDescription = active
    ? `Job ${currentJobId} continues after navigation. Jobs shows its live status and output. Another readiness check remains disabled while this job is ${currentJobStatus}.`
    : failed
      ? `Job ${currentJobId} ended with status ${currentJobStatus}. Review its output in Jobs, resolve the reported cause, then retry the readiness check here.`
      : succeeded
        ? `Job ${currentJobId} returned successfully, but job success alone does not mark this run ready. Only the refreshed durable requirements below determine readiness.`
        : null

  return <Card data-testid={`${intent}-readiness-check`} className={ready ? "border-success/35" : "border-warning/35"}>
    <CardHeader>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2"><CardTitle className="text-base">One readiness check</CardTitle><HelpTip label="readiness check">This step validates saved configuration and provenance, then briefly opens every enabled selected camera through its configured RGB-D adapter. Each camera must deliver one frame; SDK visibility alone is not enough.</HelpTip></div>
          <CardDescription className="mt-1">Selected cameras open briefly without recording. The robot never moves, and cameras are released when the check ends.</CardDescription>
        </div>
        <Button onClick={() => check.mutate()} disabled={check.isPending || loading || jobHistoryPending || jobHistoryUnavailable || active} variant={ready ? "outline" : "default"}>
          <RefreshCw aria-hidden="true" className={check.isPending || active ? "animate-spin" : ""} />
          {check.isPending ? "Queueing…" : jobHistoryPending ? "Checking history…" : jobHistoryUnavailable ? "Status unavailable" : active ? "Check in progress…" : ready ? "Check again" : "Check readiness"}
        </Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-4">
      <RequirementList requirements={[coreRequirement, ...requirements]} />
      {jobHistoryPending && <div role="status" className="flex items-start gap-3 rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs">
        <LoaderCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0 animate-spin text-primary-strong" />
        <div><div className="font-semibold text-foreground">Checking prior readiness jobs</div><p className="mt-1 text-muted-foreground">Submission stays disabled until persistent Jobs history confirms that this run has no active readiness check.</p></div>
      </div>}
      {jobHistoryUnavailable && <div role="alert" className="flex flex-col gap-3 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div><div className="font-semibold text-destructive">Readiness job status unavailable</div><p className="mt-1 text-muted-foreground">The console cannot rule out an active preflight for this run. Another submission remains disabled until persistent Jobs history can be read.</p></div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => jobs.refetch()}><RefreshCw aria-hidden="true" />Retry status</Button>
          <Button asChild variant="outline" size="sm"><Link to="/jobs">Open Jobs</Link></Button>
        </div>
      </div>}
      {!jobHistoryPending && !jobHistoryUnavailable && currentJobId && jobStatusTitle && jobStatusDescription && <div data-testid={`${intent}-readiness-job-status`} role="status" className={`rounded-lg border p-3 text-xs ${active ? "border-warning/40 bg-warning/5" : failed ? "border-destructive/35 bg-destructive/5" : "border-primary/30 bg-primary/5"}`}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            {active
              ? <LoaderCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0 animate-spin text-warning-foreground" />
              : failed
                ? <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-destructive" />
                : <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary-strong" />}
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-foreground">{jobStatusTitle}</span><StatusBadge status={currentJobStatus} tone={jobStatusTone(currentJobStatus)} /></div>
              <p className="mt-1 leading-relaxed text-muted-foreground">{jobStatusDescription}</p>
              {failed && currentJob?.message && <p className="mt-2 font-mono text-[10px] text-destructive">{currentJob.message}</p>}
            </div>
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            {!active && <Button type="button" variant="outline" size="sm" onClick={refreshEvidence}><RefreshCw aria-hidden="true" />Refresh evidence</Button>}
            <Button asChild variant="outline" size="sm"><Link to="/jobs">{active ? "Open live status in Jobs" : "Open job details"}<ArrowRight aria-hidden="true" /></Link></Button>
          </div>
        </div>
      </div>}
      <div className={`flex items-start gap-2 rounded-lg border p-3 text-xs ${ready ? "border-success/30 bg-success/5 text-success" : "border-warning/30 bg-warning/5 text-muted-foreground"}`}>
        <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
        <span>{ready ? "All required items are ready." : "Resolve every required item, then run this check again. Optional items never block capture."}</span>
      </div>
    </CardContent>
  </Card>
}
