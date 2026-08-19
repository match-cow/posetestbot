import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, ArrowRight, FileJson, Image as ImageIcon, LoaderCircle, Play, RefreshCw } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, errorMessage, query } from "@/lib/api"
import type { BopAnnotationMode, BopAnnotationSetup, Job } from "@/lib/contracts"
import { jobStatusTone } from "@/lib/jobs"
import { cn, formatDate } from "@/lib/utils"

interface BopGroundTruthGenerationProps {
  runRoot: string
  bopExportComplete: boolean
}

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"])
const FAILED_JOB_STATUSES = new Set(["failed", "canceled", "cancelled"])
const TERMINAL_JOB_STATUSES = new Set(["succeeded", ...FAILED_JOB_STATUSES])

function isAnnotationJob(job: Job, runRoot: string) {
  return job.scope_kind === "run"
    && job.run_root === runRoot
    && job.parameters.bop_annotations === true
    && (job.parameters.annotation_mode === "pose" || job.parameters.annotation_mode === "pose_and_masks")
}

function modeLabel(mode: BopAnnotationMode) {
  return mode === "pose" ? "Pose ground truth" : "Pose + masks"
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 16)}…` : "not recorded"
}

export function BopGroundTruthGeneration({ runRoot, bopExportComplete }: BopGroundTruthGenerationProps) {
  const queryClient = useQueryClient()
  const [submittedJob, setSubmittedJob] = useState<{ runRoot: string; id: string } | null>(null)
  const submittedJobId = submittedJob?.runRoot === runRoot ? submittedJob.id : null

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<{ jobs: Job[]; resources: Record<string, string> }>("/jobs"),
    refetchInterval: (state) => state.state.data?.jobs.some((job) => isAnnotationJob(job, runRoot) && ACTIVE_JOB_STATUSES.has(job.status)) ? 1_000 : 5_000,
  })
  const latestPersistedJob = useMemo(
    () => [...(jobs.data?.jobs ?? [])]
      .filter((job) => isAnnotationJob(job, runRoot))
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

  const setup = useQuery({
    queryKey: ["bop-annotations", "setup", runRoot],
    queryFn: () => api<BopAnnotationSetup>(query("/bop/annotations/setup", { run_root: runRoot })),
    refetchInterval: active ? 1_000 : false,
  })
  const configuredMode = setup.data?.configured_mode ?? "none"
  const selectedMode: BopAnnotationMode = configuredMode === "pose" ? "pose" : "pose_and_masks"
  const annotationRequested = configuredMode !== "none"
  const generate = useMutation({
    mutationFn: () => api<{ job_id: string; job: Job }>("/bop/annotations", {
      method: "POST",
      body: JSON.stringify({ run_root: runRoot, mode: selectedMode }),
    }),
    onSuccess: (data) => {
      setSubmittedJob({ runRoot, id: data.job_id })
      toast.success("Ground-truth generation queued", {
        description: `Job ${data.job_id} continues after navigation; status and output are available in Jobs.`,
      })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
      void queryClient.invalidateQueries({ queryKey: ["bop-annotations", "setup", runRoot] })
    },
    onError: (error) => toast.error("Ground-truth generation was not queued", {
      description: errorMessage(error),
    }),
  })

  useEffect(() => {
    if (!currentJobId || !TERMINAL_JOB_STATUSES.has(currentJobStatus ?? "")) return
    void queryClient.invalidateQueries({ queryKey: ["bop-annotations", "setup", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
  }, [currentJobId, currentJobStatus, queryClient, runRoot])

  const output = setup.data?.current_output ?? null
  const fullEvidenceReady = output?.mode === "pose_and_masks" && output.verified && output.evaluation_ready
  const selectedReadiness = setup.data?.readiness_by_mode[selectedMode]
  const queueBlockers = Array.from(new Set([
    ...(!bopExportComplete ? ["Complete the base BOP image/model export before generating annotations."] : []),
    ...(!annotationRequested ? ["Run setup records base BOP export only; change the annotation outcome in Workflow step 1 to request ground truth."] : []),
    ...(annotationRequested && selectedReadiness && !selectedReadiness.ready && selectedReadiness.blockers.length === 0 ? [`${modeLabel(selectedMode)} is not ready for this run.`] : []),
    ...(annotationRequested ? selectedReadiness?.blockers.map((issue) => issue.message) ?? [] : []),
    ...(active ? ["Wait for the active ground-truth job to finish or cancel it from Jobs."] : []),
  ]))

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["bop-annotations", "setup", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
  }

  return <Card data-testid="bop-ground-truth-generation" className="border-primary/25">
    <CardHeader>
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <CardTitle className="text-base">Choose the BOP ground-truth evidence</CardTitle>
          <CardDescription className="mt-1 max-w-4xl leading-relaxed">
            Both choices derive each model-to-camera pose through the immutable object model, pose template, measured placement, robot pose, and selected camera calibration. Choose how much annotation evidence this exported dataset needs.
          </CardDescription>
        </div>
        {setup.data && <div className="grid shrink-0 grid-cols-3 gap-3 rounded-lg border bg-muted/20 px-4 py-3 text-center text-[10px]">
          <div><div className="font-mono text-sm font-semibold">{setup.data.counts.sensors.toLocaleString()}</div><div className="text-muted-foreground">sensors</div></div>
          <div><div className="font-mono text-sm font-semibold">{setup.data.counts.frames.toLocaleString()}</div><div className="text-muted-foreground">frames</div></div>
          <div><div className="font-mono text-sm font-semibold">{setup.data.counts.instances.toLocaleString()}</div><div className="text-muted-foreground">instances</div></div>
        </div>}
      </div>
    </CardHeader>
    <CardContent className="space-y-5">
      {setup.isPending
        ? <div className="flex items-center gap-2 rounded-lg border p-4 text-xs text-muted-foreground"><LoaderCircle aria-hidden="true" className="size-4 animate-spin" />Checking BlenderProc and dataset readiness…</div>
        : setup.isError
          ? <div role="alert" className="flex flex-col gap-3 rounded-lg border border-destructive/35 bg-destructive/5 p-4 sm:flex-row sm:items-center sm:justify-between"><div><div className="text-sm font-semibold text-destructive">Ground-truth setup is unavailable</div><p className="mt-1 text-xs text-muted-foreground">{errorMessage(setup.error)}</p></div><Button type="button" variant="outline" size="sm" onClick={refresh}><RefreshCw aria-hidden="true" />Retry</Button></div>
          : setup.data && <>
            <div className="grid gap-3 xl:grid-cols-2">
              <div className={cn("rounded-lg border p-4", setup.data.runtime.available ? "border-success/30 bg-success/5" : "border-warning/40 bg-warning/5")}>
                <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
                  BlenderProc runtime
                  <StatusBadge status={setup.data.runtime.available ? "ready" : "blocked"} tone={setup.data.runtime.available ? "success" : "destructive"}>{setup.data.runtime.available ? "available" : "required"}</StatusBadge>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  {setup.data.runtime.available
                    ? setup.data.runtime.detected_version
                      ? `Detected ${setup.data.runtime.detected_version}${setup.data.runtime.required_version ? `; required ${setup.data.runtime.required_version}` : ""}.`
                      : `Executable found${setup.data.runtime.required_version ? `; the queued process verifies required version ${setup.data.runtime.required_version}` : "; the queued process verifies its version"} before writing derived evidence.`
                    : setup.data.runtime.reason ?? "Install the pinned BlenderProc runtime before generating scene annotations."}
                </p>
                {!setup.data.runtime.available && setup.data.runtime.install_command && <code className="mt-2 block select-all rounded bg-background px-2 py-1.5 text-[10px]">{setup.data.runtime.install_command}</code>}
              </div>
              <div className={cn("rounded-lg border p-4", setup.data.toolkit.available ? "border-success/30 bg-success/5" : selectedMode === "pose_and_masks" ? "border-warning/40 bg-warning/5" : "bg-muted/20")}>
                <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
                  Pinned rendering toolkit
                  <StatusBadge status={setup.data.toolkit.available ? "ready" : selectedMode === "pose_and_masks" ? "blocked" : "not_required"} tone={setup.data.toolkit.available ? "success" : selectedMode === "pose_and_masks" ? "destructive" : "neutral"}>
                    {setup.data.toolkit.available ? "available" : selectedMode === "pose_and_masks" ? "required for masks" : "not required for pose-only"}
                  </StatusBadge>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  {setup.data.toolkit.available
                    ? `Revision ${setup.data.toolkit.revision ?? "unreported"}${setup.data.toolkit.renderer ? ` · ${setup.data.toolkit.renderer} renderer` : ""}.`
                    : selectedMode === "pose"
                      ? "Plain pose GT is transform-derived and remains available without the rendering toolkit."
                      : setup.data.toolkit.reason ?? "Install the pinned rendering toolkit before generating masks and visibility evidence."}
                </p>
                {!setup.data.toolkit.available && selectedMode === "pose_and_masks" && setup.data.toolkit.install_command && <code className="mt-2 block select-all rounded bg-background px-2 py-1.5 text-[10px]">{setup.data.toolkit.install_command}</code>}
              </div>
            </div>
            <div className="flex justify-end"><Button type="button" variant="outline" size="sm" onClick={refresh}><RefreshCw aria-hidden="true" />Refresh readiness</Button></div>

            <fieldset>
              <legend className="text-sm font-semibold">Configured optional annotation outcome</legend>
              <p className="mt-1 text-xs text-muted-foreground">This choice is owned by <Link className="font-medium text-primary-strong underline-offset-4 hover:underline" to="/workflow/dataset?step=configure">Workflow step 1</Link>. Return there to change it before queueing derived evidence.</p>
              {configuredMode === "none" && <div className="mt-3 rounded-lg border bg-muted/20 p-4 text-xs"><div className="font-semibold">Base BOP dataset only</div><p className="mt-1 text-muted-foreground">No ground-truth job is requested for this run. The verified image/model export remains the complete configured outcome.</p></div>}
              {annotationRequested && <div className="mt-3 grid gap-4 xl:grid-cols-2" role="radiogroup" aria-label="BOP ground-truth annotation version">
                <button
                  type="button"
                  role="radio"
                  aria-checked={selectedMode === "pose"}
                  disabled
                  className={cn("rounded-xl border p-4 text-left transition-colors", selectedMode === "pose" ? "border-primary bg-primary/5 ring-1 ring-primary/30" : "hover:border-foreground/25")}
                >
                  <span className="flex items-start gap-3">
                    <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted"><FileJson aria-hidden="true" className="size-4 text-primary-strong" /></span>
                    <span className="min-w-0">
                      <span className="flex flex-wrap items-center gap-2"><span className="font-semibold">Plain pose ground truth</span><StatusBadge status="warning" tone="warning">not evaluation-ready</StatusBadge></span>
                      <span className="mt-2 block text-xs leading-relaxed text-muted-foreground">Writes standard per-instance rotations and translations to each scene’s <code>scene_gt.json</code>. No segmentation render is performed.</span>
                      <span className="mt-3 block rounded-md bg-muted/50 p-2 text-[11px]"><strong>Contains:</strong> target identity and exact model-to-camera pose.</span>
                      <span className="mt-2 block text-[11px] text-warning-foreground">Does not create <code>scene_gt_info.json</code>, masks, visible masks, or BOP evaluation visibility evidence.</span>
                    </span>
                  </span>
                </button>

                <button
                  type="button"
                  role="radio"
                  aria-checked={selectedMode === "pose_and_masks"}
                  disabled
                  className={cn("rounded-xl border p-4 text-left transition-colors", selectedMode === "pose_and_masks" ? "border-primary bg-primary/5 ring-1 ring-primary/30" : "hover:border-foreground/25")}
                >
                  <span className="flex items-start gap-3">
                    <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary/10"><ImageIcon aria-hidden="true" className="size-4 text-primary-strong" /></span>
                    <span className="min-w-0">
                      <span className="flex flex-wrap items-center gap-2"><span className="font-semibold">Pose + object masks and ROI</span><StatusBadge status="ready" tone="success">recommended</StatusBadge></span>
                      <span className="mt-2 block text-xs leading-relaxed text-muted-foreground">BlenderProc loads and validates the calibrated scene and emits pose GT; the pinned official BOP Toolkit then renders full and visible masks against captured depth and writes the BOP visibility evidence.</span>
                      <span className="mt-3 block rounded-md bg-primary/5 p-2 text-[11px]"><strong>Contains:</strong> <code>scene_gt.json</code>, <code>scene_gt_info.json</code>, standard full-frame per-instance <code>mask/</code> and <code>mask_visib/</code> PNGs, plus <code>bbox_obj</code> and <code>bbox_visib</code> ROI metadata.</span>
                      <span className="mt-2 block text-[11px] text-success">This is the evaluation-compatible choice for simulated or real BOP19 pose results.</span>
                    </span>
                  </span>
                </button>
              </div>}
            </fieldset>

            {annotationRequested && (selectedReadiness?.blockers.length ?? 0) > 0 && <div role="alert" data-testid="bop-annotation-blockers" className="rounded-lg border border-warning/40 bg-warning/5 p-4">
              <div className="flex items-center gap-2 text-xs font-semibold text-warning-foreground"><AlertTriangle aria-hidden="true" className="size-4" />Ground truth cannot be queued yet</div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground">{selectedReadiness?.blockers.map((issue) => <li key={`${issue.code}:${issue.message}`}>{issue.message}</li>)}</ul>
            </div>}
            {annotationRequested && (selectedReadiness?.warnings.length ?? 0) > 0 && <div className="rounded-lg border bg-muted/20 p-4">
              <div className="text-xs font-semibold">Readiness notes</div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground">{selectedReadiness?.warnings.map((issue) => <li key={`${issue.code}:${issue.message}`}>{issue.message}</li>)}</ul>
            </div>}

            {currentJobId && <div data-testid="bop-annotation-job-status" role="status" className={cn("rounded-lg border p-4", active ? "border-warning/40 bg-warning/5" : failed ? "border-destructive/40 bg-destructive/5" : "border-primary/35 bg-primary/5")}>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <div className="flex flex-wrap items-center gap-2 font-semibold">
                    {active ? `Ground-truth generation is ${currentJobStatus}` : failed ? "Ground-truth generation needs attention" : "Ground-truth job finished"}
                    <StatusBadge status={currentJobStatus} tone={jobStatusTone(currentJobStatus)}>{currentJobStatus}</StatusBadge>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {active
                      ? `Job ${currentJobId} continues after navigation. Jobs shows resource locks, the live generation log, cancellation, and retained failure evidence.`
                      : failed
                        ? `Job ${currentJobId} ended with status ${currentJobStatus}. The base BOP dataset and raw evidence were preserved; review the job output before retrying.`
                        : `Job ${currentJobId} completed${currentJob?.ended_at ? ` at ${formatDate(currentJob.ended_at)}` : ""}. The verified annotation evidence below is read from the run, not inferred from job success.`}
                  </p>
                  {currentJob?.message && failed && <p className="mt-2 font-mono text-[10px] text-destructive">{currentJob.message}</p>}
                </div>
                <Button asChild variant="outline" size="sm"><Link to="/jobs">{active ? "Open live log in Jobs" : "Open job details"}<ArrowRight aria-hidden="true" /></Link></Button>
              </div>
            </div>}

            {output && <div data-testid="bop-annotation-evidence" className={cn("rounded-lg border p-4", fullEvidenceReady ? "border-success/35 bg-success/5" : "bg-muted/20")}>
              <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                <div>
                  <div className="flex flex-wrap items-center gap-2 font-semibold">
                    {modeLabel(output.mode)} evidence
                    <StatusBadge status={fullEvidenceReady ? "verified" : output.state} tone={fullEvidenceReady ? "success" : output.verified ? "warning" : "destructive"}>{fullEvidenceReady ? "verified for evaluation" : output.state}</StatusBadge>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {fullEvidenceReady
                      ? "Pose, visibility, full-frame instance masks, visible masks, and ROI metadata are complete. Inspect can now validate compatible BOP19 pose results."
                      : output.mode === "pose"
                        ? "Pose annotations are present, but this version intentionally has no rendered visibility or mask evidence and cannot be used for BOP metric evaluation."
                        : "The latest output has not yet supplied complete evaluation evidence. Refresh after the job finishes or review its log."}
                  </p>
                  {!output.verified && output.integrity_error && <p role="alert" className="mt-2 text-xs text-destructive">Current annotation evidence failed its structural recheck: {output.integrity_error}</p>}
                </div>
                {fullEvidenceReady && <Button asChild><Link to="/bop-evaluation">Inspect BOP metrics<ArrowRight aria-hidden="true" /></Link></Button>}
              </div>
              <dl className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
                <div className="rounded-md border bg-background/70 p-3"><dt className="text-[10px] uppercase tracking-wide text-muted-foreground">GT annotations</dt><dd className="mt-1 font-mono text-sm font-semibold">{output.annotation_count.toLocaleString()}</dd></div>
                <div className="rounded-md border bg-background/70 p-3"><dt className="text-[10px] uppercase tracking-wide text-muted-foreground">Object masks</dt><dd className="mt-1 font-mono text-sm font-semibold">{output.mask_count.toLocaleString()}</dd></div>
                <div className="rounded-md border bg-background/70 p-3"><dt className="text-[10px] uppercase tracking-wide text-muted-foreground">Visible masks</dt><dd className="mt-1 font-mono text-sm font-semibold">{output.visible_mask_count.toLocaleString()}</dd></div>
                <div className="rounded-md border bg-background/70 p-3"><dt className="text-[10px] uppercase tracking-wide text-muted-foreground">Manifest</dt><dd className="mt-1 font-mono text-[10px]">{shortHash(output.manifest_sha256)}</dd></div>
                <div className="rounded-md border bg-background/70 p-3"><dt className="text-[10px] uppercase tracking-wide text-muted-foreground">BlenderProc</dt><dd className="mt-1 font-mono text-[10px]">{output.blenderproc_version ?? "not recorded"}</dd></div>
                <div className="rounded-md border bg-background/70 p-3"><dt className="text-[10px] uppercase tracking-wide text-muted-foreground">Renderer revision</dt><dd className="mt-1 font-mono text-[10px]">{shortHash(output.toolkit_revision)}</dd></div>
              </dl>
            </div>}

            {queueBlockers.length > 0 && <div data-testid="bop-annotation-disabled-reasons" className="rounded-lg border border-warning/35 bg-warning/5 p-3">
              <div className="text-xs font-semibold text-warning-foreground">Generation is disabled</div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted-foreground">{queueBlockers.map((reason) => <li key={reason}>{reason}</li>)}</ul>
            </div>}
            <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-center sm:justify-between">
              <p className="max-w-3xl text-xs leading-relaxed text-muted-foreground">This creates derived BOP annotations only. It never changes raw RGB-D frames, robot poses, the selected template, or calibration snapshots. The background job continues after navigation and remains recoverable in <Link className="font-medium text-primary-strong underline-offset-4 hover:underline" to="/jobs">Jobs</Link>.</p>
              <Button type="button" onClick={() => generate.mutate()} disabled={!annotationRequested || queueBlockers.length > 0 || generate.isPending || active}>
                {generate.isPending || active ? <LoaderCircle aria-hidden="true" className="animate-spin" /> : <Play aria-hidden="true" />}
                {generate.isPending ? "Queueing…" : active ? "Generating…" : selectedMode === "pose" ? "Generate pose GT" : "Generate pose + masks"}
              </Button>
            </div>
          </>}
    </CardContent>
  </Card>
}
