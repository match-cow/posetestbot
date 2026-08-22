import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, ArrowRight, FileJson, Image as ImageIcon, LoaderCircle, Play, RefreshCw } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, errorMessage, query } from "@/lib/api"
import type { BopAnnotationIssue, BopAnnotationMode, BopAnnotationSetup, Job } from "@/lib/contracts"
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

function isAnnotationIssue(value: unknown): value is BopAnnotationIssue {
  if (!value || typeof value !== "object") return false
  const issue = value as Record<string, unknown>
  return typeof issue.code === "string" && typeof issue.message === "string"
}

function annotationIssues(value: unknown): BopAnnotationIssue[] | null {
  return Array.isArray(value) && value.every(isAnnotationIssue) ? value : null
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
  const configuredMode = setup.data?.configured_mode
  const configuredModeValid = configuredMode === "none" || configuredMode === "pose" || configuredMode === "pose_and_masks"
  const selectedMode: BopAnnotationMode | null = configuredMode === "pose" || configuredMode === "pose_and_masks"
    ? configuredMode
    : null
  const annotationRequested = selectedMode !== null
  const generate = useMutation({
    mutationFn: () => {
      if (!selectedMode) throw new Error("Workflow step 1 does not request optional BOP ground truth")
      return api<{ job_id: string; job: Job }>("/bop/annotations", {
        method: "POST",
        body: JSON.stringify({ run_root: runRoot, mode: selectedMode }),
      })
    },
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
  const fullEvidenceReady = output?.mode === "pose_and_masks" && output.verified === true && output.evaluation_ready === true
  const outputMatchesConfiguredMode = selectedMode !== null && output?.mode === selectedMode
  const configuredFullEvidenceReady = selectedMode === "pose_and_masks" && outputMatchesConfiguredMode && fullEvidenceReady
  const selectedReadiness = selectedMode ? setup.data?.readiness_by_mode?.[selectedMode] : undefined
  const parsedReadinessBlockers = annotationIssues(selectedReadiness?.blockers)
  const parsedReadinessWarnings = annotationIssues(selectedReadiness?.warnings)
  const readinessContractValid = Boolean(
    selectedReadiness
    && typeof selectedReadiness.ready === "boolean"
    && parsedReadinessBlockers !== null
    && parsedReadinessWarnings !== null,
  )
  const readinessBlockers = parsedReadinessBlockers ?? []
  const readinessWarnings = parsedReadinessWarnings ?? []
  const queueBlockers = selectedMode ? Array.from(new Set([
    ...(!bopExportComplete ? ["Complete the base BOP image/model export before generating annotations."] : []),
    ...(!readinessContractValid ? [`Readiness for the configured ${modeLabel(selectedMode).toLowerCase()} outcome was not returned or was malformed.`] : []),
    ...(readinessContractValid && selectedReadiness?.ready !== true && readinessBlockers.length === 0 ? [`Readiness for the configured ${modeLabel(selectedMode).toLowerCase()} outcome was not confirmed.`] : []),
    ...readinessBlockers.map((issue) => issue.message),
    ...(active ? ["Wait for the active ground-truth job to finish or cancel it from Jobs."] : []),
  ])) : []

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["bop-annotations", "setup", runRoot] })
    void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    void queryClient.invalidateQueries({ queryKey: ["overview", runRoot] })
  }

  return <Card data-testid="bop-ground-truth-generation" className="border-primary/25">
    <CardHeader>
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <CardTitle className="text-base">Configured BOP annotation outcome</CardTitle>
          <CardDescription className="mt-1 max-w-4xl leading-relaxed">Workflow step 1 owns this run-level choice. This step reports the configured outcome and queues only the matching optional derived evidence.</CardDescription>
        </div>
        {setup.data && configuredModeValid && <div className="grid shrink-0 grid-cols-3 gap-3 rounded-lg border bg-muted/20 px-4 py-3 text-center text-[10px]">
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
          : !setup.data || !configuredModeValid
            ? <div role="alert" data-testid="bop-annotation-contract-error" className="rounded-lg border border-destructive/35 bg-destructive/5 p-4"><div className="text-sm font-semibold text-destructive">Annotation setup contract is invalid</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">The server did not return one recognized configured mode (<code>none</code>, <code>pose</code>, or <code>pose_and_masks</code>). No annotation outcome or generation action is assumed. Retry the setup check before continuing.</p><Button className="mt-3" type="button" variant="outline" size="sm" onClick={refresh}><RefreshCw aria-hidden="true" />Retry</Button></div>
          : setup.data && configuredMode === "none"
            ? <div className="rounded-lg border border-success/30 bg-success/5 p-5" data-testid="bop-base-only-outcome">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <div className="flex flex-wrap items-center gap-2 font-semibold">Base BOP dataset only <StatusBadge status={bopExportComplete ? "complete" : "waiting"} tone={bopExportComplete ? "success" : "warning"}>{bopExportComplete ? "configured outcome complete" : "waiting for base export"}</StatusBadge></div>
                  <p className="mt-2 max-w-3xl text-xs leading-relaxed text-muted-foreground">This run does not request optional pose or mask generation. {bopExportComplete ? "The verified image/model export is the complete configured acquisition outcome." : "Complete dataset processing to produce the configured image/model export."}</p>
                  <p className="mt-2 text-xs text-muted-foreground">Workflow step 1 records this run-owned outcome. {bopExportComplete ? "Because this run has been acquired, its setup is now read-only; start a fresh run to request a different outcome." : "If acquisition has not started, review step 1 to change it; after any capture attempt, use a fresh run."} Any generation request revalidates the exact configured mode.</p>
                </div>
                <Button asChild variant="outline" size="sm"><Link to="/workflow/dataset?step=configure">Review Workflow step 1</Link></Button>
              </div>
            </div>
            : setup.data && selectedMode && <>
            <div className={cn("grid gap-3", selectedMode === "pose_and_masks" && "xl:grid-cols-2")}>
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
              {selectedMode === "pose_and_masks" && <div data-testid="bop-rendering-toolkit" className={cn("rounded-lg border p-4", setup.data.toolkit.available ? "border-success/30 bg-success/5" : "border-warning/40 bg-warning/5")}>
                <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
                  Pinned rendering toolkit
                  <StatusBadge status={setup.data.toolkit.available ? "ready" : "blocked"} tone={setup.data.toolkit.available ? "success" : "destructive"}>{setup.data.toolkit.available ? "available" : "required for masks"}</StatusBadge>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                  {setup.data.toolkit.available
                    ? `Revision ${setup.data.toolkit.revision ?? "unreported"}${setup.data.toolkit.renderer ? ` · ${setup.data.toolkit.renderer} renderer` : ""}.`
                    : setup.data.toolkit.reason ?? "Install the pinned rendering toolkit before generating masks and visibility evidence."}
                </p>
                {!setup.data.toolkit.available && setup.data.toolkit.install_command && <code className="mt-2 block select-all rounded bg-background px-2 py-1.5 text-[10px]">{setup.data.toolkit.install_command}</code>}
              </div>}
            </div>
            <div className="flex justify-end"><Button type="button" variant="outline" size="sm" onClick={refresh}><RefreshCw aria-hidden="true" />Refresh readiness</Button></div>

            <section className={cn("rounded-xl border p-4", selectedMode === "pose_and_masks" ? "border-primary/35 bg-primary/5" : "bg-muted/20")} data-testid="bop-configured-annotation-mode" aria-labelledby="bop-configured-annotation-mode-heading">
              <div className="flex items-start gap-3">
                <div className={cn("grid size-9 shrink-0 place-items-center rounded-lg", selectedMode === "pose_and_masks" ? "bg-primary/10" : "bg-muted")}>
                  {selectedMode === "pose" ? <FileJson aria-hidden="true" className="size-4 text-primary-strong" /> : <ImageIcon aria-hidden="true" className="size-4 text-primary-strong" />}
                </div>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><h3 id="bop-configured-annotation-mode-heading" className="font-semibold">{selectedMode === "pose" ? "Plain pose ground truth" : "Pose + object masks and visibility"}</h3><StatusBadge status={selectedMode === "pose" ? "not_evaluation_ready" : configuredFullEvidenceReady ? "evaluation_ready" : "verification_required"} tone={configuredFullEvidenceReady ? "success" : "warning"}>{selectedMode === "pose" ? "not evaluation-ready" : configuredFullEvidenceReady ? "evaluation-ready" : "verification required"}</StatusBadge></div>
                  {selectedMode === "pose"
                    ? <><p className="mt-2 text-xs leading-relaxed text-muted-foreground">The configured job writes standard per-instance rotations and translations to each scene’s <code>scene_gt.json</code>. It does not render segmentation or visibility evidence.</p><p className="mt-2 text-[11px] text-warning-foreground">Without <code>scene_gt_info.json</code> and verified visible-mask evidence, Inspect BOP metric evaluation remains unavailable.</p></>
                    : <><p className="mt-2 text-xs leading-relaxed text-muted-foreground">BlenderProc produces pose GT, then the pinned official BOP Toolkit renders full and visible masks against captured depth and writes the visibility evidence.</p><p className="mt-2 rounded-md bg-background/65 p-2 text-[11px]"><strong>Required verified product:</strong> <code>scene_gt.json</code>, <code>scene_gt_info.json</code>, full-frame <code>mask/</code> and <code>mask_visib/</code> PNGs, and ROI/visibility metadata. Inspect evaluation stays unavailable until this evidence is verified.</p></>}
                  <p className="mt-3 text-[11px] text-muted-foreground">Configured in <Link className="font-medium text-primary-strong underline-offset-4 hover:underline" to="/workflow/dataset?step=configure">Workflow step 1</Link>. Review it there; after any capture attempt, a different run-owned outcome requires a fresh run.</p>
                </div>
              </div>
            </section>

            {annotationRequested && readinessBlockers.length > 0 && <div role="alert" data-testid="bop-annotation-blockers" className="rounded-lg border border-warning/40 bg-warning/5 p-4">
              <div className="flex items-center gap-2 text-xs font-semibold text-warning-foreground"><AlertTriangle aria-hidden="true" className="size-4" />Ground truth cannot be queued yet</div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground">{readinessBlockers.map((issue) => <li key={`${issue.code}:${issue.message}`}>{issue.message}</li>)}</ul>
            </div>}
            {annotationRequested && readinessWarnings.length > 0 && <div className="rounded-lg border bg-muted/20 p-4">
              <div className="text-xs font-semibold">Readiness notes</div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground">{readinessWarnings.map((issue) => <li key={`${issue.code}:${issue.message}`}>{issue.message}</li>)}</ul>
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

            {output && <div data-testid="bop-annotation-evidence" className={cn("rounded-lg border p-4", configuredFullEvidenceReady ? "border-success/35 bg-success/5" : "bg-muted/20")}>
              <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                <div>
                  <div className="flex flex-wrap items-center gap-2 font-semibold">
                    {modeLabel(output.mode)} evidence
                    <StatusBadge status={configuredFullEvidenceReady ? "verified" : outputMatchesConfiguredMode ? output.state : "stale"} tone={configuredFullEvidenceReady ? "success" : output.verified === true ? "warning" : "destructive"}>{configuredFullEvidenceReady ? "verified for evaluation" : outputMatchesConfiguredMode ? output.state : "does not match configured mode"}</StatusBadge>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {!outputMatchesConfiguredMode
                      ? `This retained ${modeLabel(output.mode).toLowerCase()} output does not match the run's configured ${modeLabel(selectedMode).toLowerCase()} outcome. It is not offered to Inspect; generate and verify the configured outcome. After acquisition begins, choosing a different outcome requires a fresh run.`
                      : configuredFullEvidenceReady
                      ? "Pose, visibility, full-frame instance masks, visible masks, and ROI metadata are complete. Inspect can now validate compatible BOP19 pose results."
                      : output.mode === "pose"
                        ? "Pose annotations are present, but this version intentionally has no rendered visibility or mask evidence and cannot be used for BOP metric evaluation."
                        : "The latest output has not yet supplied complete evaluation evidence. Refresh after the job finishes or review its log."}
                  </p>
                  {output.verified !== true && output.integrity_error && <p role="alert" className="mt-2 text-xs text-destructive">Current annotation evidence failed its structural recheck: {output.integrity_error}</p>}
                </div>
                {configuredFullEvidenceReady && <Button asChild><Link to="/bop-evaluation">Inspect BOP metrics<ArrowRight aria-hidden="true" /></Link></Button>}
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
