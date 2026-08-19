import { useDeferredValue, useMemo, useState } from "react"
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Ban, ChevronDown, Clock3, Copy, Cpu, FileText, LockKeyhole, RefreshCw, Search, Server, Square, Terminal, X } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"

import { EmptyState } from "@/components/empty-state"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage, query } from "@/lib/api"
import type { ClusterJob, Job, JobPage } from "@/lib/contracts"
import { jobStatusTone } from "@/lib/jobs"
import { formatDate } from "@/lib/utils"
import { activeWorkflowHref } from "@/lib/workflow-session"
import { useOperator } from "@/providers/operator-provider"

const ACTIVE = new Set(["queued", "running", "canceling"])
const CLUSTER_ACTIVE = new Set(["preparing", "transferring", "submitted", "pending", "running", "collecting", "canceling"])
const PAGE_SIZE = 20
type StatusFilter = "all" | "active" | "failed" | "finished"
type ScopeFilter = "all" | "active_run" | "run" | "library" | "global"

function isCaptureJob(job: Job) {
  return job.parameters.purpose === "capture"
}

function isCancelableJob(job: Job) {
  return job.parameters.cancelable !== false
}

function timing(job: Job) {
  if (job.status === "queued") return "Waiting to start"
  if (job.ended_at) return `Finished ${formatDate(job.ended_at)}`
  if (job.started_at) return `Started ${formatDate(job.started_at)}`
  return "Not started"
}

function jobRunRoot(job: Job) {
  return job.run_root
}

function jobScopeLabel(job: Job, selectedRun: string) {
  switch (job.scope_kind) {
    case "run":
      return job.run_root === selectedRun ? "Active run" : "Other run"
    case "library":
      return "Reusable library"
    case "global":
      return "Lab-wide"
    default:
      return "Unknown scope"
  }
}

async function writeClipboard(text: string) {
  if (!navigator.clipboard?.writeText) throw new Error("Clipboard access is unavailable in this browser context")
  await navigator.clipboard.writeText(text)
}

async function copyDebugText(label: string, text: string) {
  try {
    await writeClipboard(text)
    toast.success(`${label} copied`)
  } catch (error) {
    toast.error(`${label} could not be copied`, { description: errorMessage(error) })
  }
}

function clusterTone(status: string) {
  if (["succeeded", "succeeded-with-warning"].includes(status)) return "success" as const
  if (status === "failed") return "destructive" as const
  if (CLUSTER_ACTIVE.has(status)) return "warning" as const
  return "neutral" as const
}

function estimatorLabel(job: ClusterJob) {
  const estimatorId = typeof job.payload.estimator_id === "string" ? job.payload.estimator_id : "unreported_estimator"
  if (estimatorId === "foundationpose") return "FoundationPose"
  return estimatorId.split(/[-_]/).filter(Boolean).map((part) => part[0]?.toUpperCase() + part.slice(1)).join(" ")
}

function ClusterJobsSection() {
  const queryClient = useQueryClient()
  const { selectedRun } = useOperator()
  const [status, setStatus] = useState("all")
  const [search, setSearch] = useState("")
  const [detailId, setDetailId] = useState<string | null>(null)
  const jobs = useQuery({
    queryKey: ["cluster-jobs", status],
    queryFn: () => api<{ jobs: ClusterJob[]; next_cursor: string | null }>(query("/cluster/jobs", { limit: 50, state: status === "all" ? undefined : status })),
    retry: false,
    refetchInterval: (state) => state.state.data?.jobs.some((job) => CLUSTER_ACTIVE.has(job.state)) ? 2_000 : 10_000,
  })
  const detail = useQuery({
    queryKey: ["cluster-job", detailId, "log"],
    queryFn: () => api<{ job: ClusterJob; log: string }>(query(`/cluster/jobs/${detailId}`, { include_log: true })),
    enabled: Boolean(detailId),
    refetchInterval: (state) => CLUSTER_ACTIVE.has(state.state.data?.job.state ?? "") ? 1_500 : false,
  })
  const cancel = useMutation({
    mutationFn: (job: ClusterJob) => api<{ job: ClusterJob }>(`/cluster/jobs/${job.job_id}/cancel`, {
      method: "POST",
      body: "{}",
    }),
    onSuccess: () => {
      toast.success("Exact cluster job cancellation requested")
      queryClient.invalidateQueries({ queryKey: ["cluster-jobs"] })
      if (detailId) queryClient.invalidateQueries({ queryKey: ["cluster-job", detailId] })
    },
    onError: (error) => toast.error("Cluster job could not be canceled", { description: errorMessage(error) }),
  })
  const normalizedSearch = search.trim().toLowerCase()
  const filtered = (jobs.data?.jobs ?? []).filter((job) => !normalizedSearch
    || job.job_id.toLowerCase().includes(normalizedSearch)
    || String(job.payload.run_root ?? "").toLowerCase().includes(normalizedSearch)
    || String(job.slurm_job_id ?? "").includes(normalizedSearch))
  const current = detail.data?.job ?? jobs.data?.jobs.find((job) => job.job_id === detailId)

  return <Card data-testid="cluster-jobs-section" className="border-primary/25">
    <CardHeader className="pb-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><CardTitle className="flex items-center gap-2"><Server className="size-5 text-primary-strong" />Cluster provider</CardTitle><CardDescription className="mt-1">Durable estimator and SLURM state from the loopback companion controller. The bounded view loads at most 50 recent jobs.</CardDescription></div>
        <div className="flex gap-2"><Button asChild variant="outline" size="sm"><Link to="/pose-estimation"><Cpu />Pose Estimation</Link></Button><Button variant="outline" size="sm" onClick={() => jobs.refetch()} disabled={jobs.isFetching}><RefreshCw className={jobs.isFetching ? "animate-spin" : undefined} />Refresh</Button></div>
      </div>
    </CardHeader>
    <CardContent className="space-y-3">
      {jobs.isError
        ? <div className="rounded-lg border border-muted bg-muted/25 p-3 text-xs text-muted-foreground">Cluster provider unavailable: {errorMessage(jobs.error)}. Local job history below remains unaffected.</div>
        : <>
          <div className="grid gap-2 lg:grid-cols-[minmax(220px,1fr)_220px_auto]">
            <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="Search cluster jobs" className="pl-9" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Job ID, SLURM ID, run…" /></div>
            <Select value={status} onValueChange={setStatus}><SelectTrigger aria-label="Filter cluster jobs by state"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All remote states</SelectItem><SelectItem value="running">Running</SelectItem><SelectItem value="pending">Pending</SelectItem><SelectItem value="collecting">Collecting</SelectItem><SelectItem value="succeeded">Succeeded</SelectItem><SelectItem value="succeeded-with-warning">Succeeded with warning</SelectItem><SelectItem value="failed">Failed</SelectItem><SelectItem value="canceled">Canceled</SelectItem></SelectContent></Select>
            <div className="self-center text-right text-xs text-muted-foreground">{filtered.length} loaded</div>
          </div>
          {jobs.isPending
            ? <Skeleton className="h-24" />
            : filtered.length === 0
              ? <p className="rounded-lg border border-dashed p-4 text-center text-xs text-muted-foreground">No matching cluster jobs.</p>
              : <div className="max-h-[440px] space-y-2 overflow-y-auto pr-1">
                {filtered.map((job) => <div key={job.job_id} data-testid={`cluster-job-${job.job_id}`} className="grid items-center gap-3 rounded-lg border bg-muted/15 p-3 xl:grid-cols-[minmax(0,1fr)_150px_190px_auto]">
                  <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{estimatorLabel(job)}</span><StatusBadge status={job.state} tone={clusterTone(job.state)} /></div><div className="mt-1 truncate font-mono text-[11px] text-muted-foreground" title={job.job_id}>{job.job_id}</div><div className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={String(job.payload.run_root ?? "")}>{job.payload.run_root === selectedRun ? "Active run · " : "Other run · "}{String(job.payload.run_root ?? "unknown")}</div></div>
                  <div><div className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">SLURM</div><div className="mt-1 font-mono text-xs">{job.slurm_job_id ?? "not assigned"}</div></div>
                  <div><div className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">Updated</div><div className="mt-1 text-xs">{formatDate(job.updated_at)}</div>{job.result && <div className="mt-1 text-[10px] text-muted-foreground">{job.result.estimate_count} estimates · {job.result.failure_count} failures</div>}</div>
                  <div className="flex gap-2 xl:justify-end"><Button variant="outline" size="sm" onClick={() => setDetailId(job.job_id)}><FileText />Log</Button>{CLUSTER_ACTIVE.has(job.state) && <Button variant="destructive" size="sm" onClick={() => cancel.mutate(job)} disabled={cancel.isPending || job.state === "canceling"}><Ban />{job.state === "canceling" ? "Canceling…" : "Cancel"}</Button>}</div>
                </div>)}
              </div>}
        </>}
    </CardContent>
    <Sheet open={Boolean(detailId)} onOpenChange={(open) => !open && setDetailId(null)}>
      <SheetContent><SheetHeader><SheetTitle>Cluster job log</SheetTitle><SheetDescription>{current?.job_id} · SLURM {current?.slurm_job_id ?? "not assigned"}</SheetDescription></SheetHeader><div className="flex items-center justify-between gap-3"><StatusBadge status={current?.state} tone={clusterTone(current?.state ?? "unknown")} /><span className="text-xs text-muted-foreground">Controller state survives UI and PoseTestBot restarts.</span></div><pre className="min-h-0 flex-1 overflow-auto rounded-lg bg-[#11130d] p-4 text-xs leading-relaxed text-[#dce4c4]">{detail.isError ? `Log unavailable: ${errorMessage(detail.error)}` : detail.data?.log || "Waiting for controller log output…"}</pre>{current?.error && <p className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">{current.error}</p>}{current && CLUSTER_ACTIVE.has(current.state) && <Button variant="destructive" onClick={() => cancel.mutate(current)} disabled={cancel.isPending || current.state === "canceling"}><Ban />Cancel exact job</Button>}</SheetContent>
    </Sheet>
  </Card>
}

function jobContext(job: Job) {
  const metadata: Partial<Job> = { ...job }
  delete metadata.tail
  return JSON.stringify({
    schema_version: "posetestbot_job_debug_context.v1",
    job: metadata,
  }, null, 2)
}

export function JobsPage() {
  const queryClient = useQueryClient()
  const { currentWorkflow, selectedRun } = useOperator()
  const [detail, setDetail] = useState<Job | null>(null)
  const [search, setSearch] = useState("")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all")
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>("all")
  const deferredSearch = useDeferredValue(search.trim())
  const jobs = useInfiniteQuery({
    queryKey: ["jobs", "history", deferredSearch, statusFilter, scopeFilter, selectedRun],
    queryFn: ({ pageParam }) => api<JobPage>(query("/jobs", {
      limit: PAGE_SIZE,
      cursor: pageParam,
      search: deferredSearch,
      status: statusFilter === "all" ? undefined : statusFilter,
      scope_kind: scopeFilter === "all" ? undefined : scopeFilter === "active_run" ? "run" : scopeFilter,
      run_root: scopeFilter === "active_run" ? selectedRun : undefined,
    })),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: (queryState) => (queryState.state.data?.pages.some((page) => page.jobs.some((job) => ACTIVE.has(job.status))) ? 1_000 : 5_000),
  })
  const ordered = useMemo(
    () => {
      const byId = new Map<string, Job>()
      for (const page of jobs.data?.pages ?? []) {
        for (const job of page.jobs) byId.set(job.id, job)
      }
      return [...byId.values()].sort((left, right) => Number(ACTIVE.has(right.status)) - Number(ACTIVE.has(left.status)) || right.created_at.localeCompare(left.created_at))
    },
    [jobs.data],
  )
  const firstPage = jobs.data?.pages[0]
  const currentDetail = detail ? ordered.find((job) => job.id === detail.id) ?? detail : null
  const log = useQuery({
    queryKey: ["job-log", currentDetail?.id],
    queryFn: () => api<string>(`/jobs/${currentDetail!.id}/log`),
    enabled: Boolean(currentDetail),
    refetchInterval: currentDetail && ACTIVE.has(currentDetail.status) ? 1_000 : false,
  })
  const outputText = log.data || currentDetail?.tail.join("\n") || ""
  const cancel = useMutation({
    mutationFn: (job: Job) => api(isCaptureJob(job) ? `/capture/jobs/${job.id}/stop` : `/jobs/${job.id}/cancel`, { method: "POST", body: "{}" }),
    onSuccess: () => {
      toast.success("Cancellation requested")
      queryClient.invalidateQueries({ queryKey: ["jobs"] })
      queryClient.invalidateQueries({ queryKey: ["capture-jobs"] })
    },
    onError: (error) => toast.error("Job could not be canceled", { description: errorMessage(error) }),
  })

  const setFilter = (value: StatusFilter) => {
    setStatusFilter(value)
  }
  const setSearchValue = (value: string) => {
    setSearch(value)
  }
  const clearFilters = () => {
    setSearch("")
    setStatusFilter("all")
    setScopeFilter("all")
  }
  const activeCount = [...ACTIVE].reduce((count, status) => count + (firstPage?.status_counts?.[status] ?? 0), 0)
  const failedCount = firstPage?.status_counts?.failed ?? 0
  const total = firstPage?.total ?? ordered.length
  const filtersActive = Boolean(search || statusFilter !== "all" || scopeFilter !== "all")
  const workflowHref = currentWorkflow ? activeWorkflowHref(currentWorkflow) : "/workflow/setup"

  return <div className="space-y-6">
    <PageHeader eyebrow="Local and cluster providers" title="Jobs & resource locks" description="Monitor local background work and durable external estimator jobs, inspect live logs, and cancel only the exact recorded workload. Each job shows whether it belongs to the active run." actions={<Button variant="outline" onClick={() => jobs.refetch()} disabled={jobs.isFetching}><RefreshCw className={jobs.isFetching ? "animate-spin" : undefined} />Refresh local jobs</Button>} />
    <ProcessHandoff
      title="Jobs continue when you leave their originating page"
      description="Use this page for status, resource ownership, logs, and safe cancellation. Committed storage operations remain non-cancelable so they cannot be interrupted midway. When a job finishes, return to the guided workflow to review its durable evidence and continue."
      to={workflowHref}
      action="Open workflow"
    />

    <ClusterJobsSection />

    {firstPage && Object.keys(firstPage.resources).length > 0 && <Card><CardContent className="flex flex-wrap items-center gap-3 py-4"><span className="flex items-center gap-1 text-xs font-semibold"><LockKeyhole className="size-4 text-warning-foreground" />Held resources <HelpTip label="resource locks">A lock prevents two local jobs from opening the same camera, commanding the robot, or mutating the same managed catalogue at once. It is released when the owning job exits.</HelpTip></span>{Object.entries(firstPage.resources).map(([resource, id]) => <StatusBadge key={resource} status="warning" tone="warning">{resource} · {id}</StatusBadge>)}</CardContent></Card>}

    {!jobs.isPending && !jobs.isError && ordered.length > 0 && <Card>
      <CardContent className="grid items-end gap-3 py-4 xl:grid-cols-[minmax(260px,1fr)_190px_190px_auto]">
        <div className="space-y-1.5"><Label htmlFor="job-search">Search jobs</Label><div className="relative"><Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input id="job-search" className="pl-9" value={search} onChange={(event) => setSearchValue(event.target.value)} placeholder="Name, ID, resource, run…" /></div></div>
        <div className="space-y-1.5"><Label htmlFor="job-status-filter">Status</Label><Select value={statusFilter} onValueChange={(value: StatusFilter) => setFilter(value)}><SelectTrigger id="job-status-filter" aria-label="Filter jobs by status"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All jobs</SelectItem><SelectItem value="active">Active ({activeCount})</SelectItem><SelectItem value="failed">Failed ({failedCount})</SelectItem><SelectItem value="finished">Finished</SelectItem></SelectContent></Select></div>
        <div className="space-y-1.5"><Label htmlFor="job-scope-filter">Scope</Label><Select value={scopeFilter} onValueChange={(value: ScopeFilter) => setScopeFilter(value)}><SelectTrigger id="job-scope-filter" aria-label="Filter jobs by scope"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All scopes</SelectItem><SelectItem value="active_run">Active run</SelectItem><SelectItem value="run">All run-owned</SelectItem><SelectItem value="library">Reusable library</SelectItem><SelectItem value="global">Lab-wide</SelectItem></SelectContent></Select></div>
        <div className="flex items-center justify-between gap-3 xl:justify-end"><span className="text-xs tabular-nums text-muted-foreground">Loaded {ordered.length} of {total}</span><Button variant="ghost" onClick={clearFilters} disabled={!filtersActive}><X />Clear</Button></div>
      </CardContent>
    </Card>}

    {jobs.isPending
      ? <div className="space-y-2">{Array.from({ length: 6 }).map((_, index) => <Skeleton className="h-24" key={index} />)}</div>
      : jobs.isError
        ? <Card className="border-destructive/40"><CardHeader><CardTitle>Jobs unavailable</CardTitle><CardDescription>{errorMessage(jobs.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={() => jobs.refetch()}><RefreshCw />Try again</Button></CardContent></Card>
          : ordered.length === 0 && !filtersActive
          ? <EmptyState icon={Terminal} title="No jobs yet" description="Queue a readiness check, fixed workflow action, camera snapshot, or capture plan to see it here." />
          : ordered.length === 0
            ? <EmptyState icon={Search} title="No matching jobs" description="Change or clear the search and status filter." action={<Button variant="outline" onClick={clearFilters}><X />Clear filters</Button>} />
            : <div className="space-y-2">
              {ordered.map((job) => {
                const cancelPending = job.status === "canceling" || (cancel.isPending && cancel.variables?.id === job.id)
                const cancelable = isCancelableJob(job)
                const runRoot = jobRunRoot(job)
                return <Card key={job.id} data-testid={`job-card-${job.id}`} data-job-id={job.id} role="group" aria-label={`${job.name} job ${job.id}`} className={ACTIVE.has(job.status) ? "border-primary/35" : undefined}>
                  <CardContent className="grid items-center gap-4 py-4 xl:grid-cols-[minmax(0,1fr)_180px_260px_auto]">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-start gap-2"><span className="min-w-0 break-words font-semibold">{job.name}</span><StatusBadge status={job.status} tone={jobStatusTone(job.status)} /></div>
                      <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"><span className="max-w-full truncate font-mono" title={job.id}>{job.id}</span><span className="flex items-center gap-1"><Clock3 className="size-3" />Queued {formatDate(job.created_at)}</span></div>
                      <div className="mt-1 flex min-w-0 items-center gap-2 text-[10px] text-muted-foreground"><span className="shrink-0 font-semibold uppercase tracking-wider">{jobScopeLabel(job, selectedRun)}</span>{runRoot && <span className="truncate font-mono" title={runRoot}>{runRoot}</span>}</div>
                      {job.message && <p className="mt-2 line-clamp-2 text-xs text-muted-foreground" title={job.message}>{job.message}</p>}
                    </div>
                    <div><div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Timing</div><div className="mt-1 text-xs">{timing(job)}</div></div>
                    <div><div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Resources</div><div className="mt-1 flex flex-wrap gap-1">{job.resources.length ? job.resources.map((resource) => <StatusBadge status={ACTIVE.has(job.status) ? "warning" : "available"} tone={ACTIVE.has(job.status) ? "warning" : "neutral"} key={resource}>{resource}</StatusBadge>) : <span className="text-xs text-muted-foreground">none</span>}{ACTIVE.has(job.status) && !cancelable && <StatusBadge status="locked" tone="warning">non-cancelable</StatusBadge>}</div></div>
                    <div className="flex flex-wrap gap-2 xl:justify-end"><Button variant="outline" size="sm" onClick={() => setDetail(job)}><FileText />Log</Button>{ACTIVE.has(job.status) && cancelable && <Button variant="destructive" size="sm" onClick={() => cancel.mutate(job)} disabled={cancelPending}>{isCaptureJob(job) ? <><Square />{cancelPending ? "Stopping…" : "Stop capture"}</> : <><Ban />{cancelPending ? "Canceling…" : "Cancel"}</>}</Button>}</div>
                  </CardContent>
                </Card>
              })}
              {jobs.hasNextPage && <div className="flex justify-center pt-3"><Button variant="outline" onClick={() => void jobs.fetchNextPage()} disabled={jobs.isFetchingNextPage}><ChevronDown />{jobs.isFetchingNextPage ? "Loading older jobs…" : "Load older jobs"}</Button></div>}
            </div>}

    <Sheet open={Boolean(detail)} onOpenChange={(open) => !open && setDetail(null)}>
      <SheetContent>
        <SheetHeader><SheetTitle>{currentDetail?.name}</SheetTitle><SheetDescription>{currentDetail?.id} · {ACTIVE.has(currentDetail?.status ?? "") ? "live process log" : "completed process log"} · {currentDetail ? jobScopeLabel(currentDetail, selectedRun) : "Unknown scope"}{currentDetail && jobRunRoot(currentDetail) ? ` · ${jobRunRoot(currentDetail)}` : ""}</SheetDescription></SheetHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3"><StatusBadge status={currentDetail?.status} tone={jobStatusTone(currentDetail?.status)} /><span className="text-xs text-muted-foreground">Return code {currentDetail?.returncode ?? "—"}</span></div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" disabled={!outputText || log.isPending} onClick={() => void copyDebugText("Job output", outputText)} title="Copy the complete process output"><Copy />Copy output</Button>
            <Button variant="outline" size="sm" disabled={!currentDetail} onClick={() => currentDetail && void copyDebugText("Job context", jobContext(currentDetail))} title="Copy job context and metadata"><Copy />Copy context</Button>
          </div>
        </div>
        <pre data-testid="job-log" className="min-h-0 flex-1 overflow-auto rounded-lg bg-[#11130d] p-4 text-xs leading-relaxed text-[#dce4c4]">{log.isError ? `Log unavailable: ${errorMessage(log.error)}` : outputText || "Waiting for log output…"}</pre>
        {currentDetail && ACTIVE.has(currentDetail.status) && !isCancelableJob(currentDetail) && <p className="rounded-lg border border-warning/35 bg-warning/5 p-3 text-xs leading-relaxed text-warning-foreground">This committed storage operation cannot be canceled safely after submission.</p>}
        {currentDetail && ACTIVE.has(currentDetail.status) && isCancelableJob(currentDetail) && <Button variant="destructive" onClick={() => cancel.mutate(currentDetail)} disabled={currentDetail.status === "canceling" || (cancel.isPending && cancel.variables?.id === currentDetail.id)}><Square />{currentDetail.status === "canceling" ? "Canceling…" : isCaptureJob(currentDetail) ? "Stop capture" : "Cancel job"}</Button>}
      </SheetContent>
    </Sheet>
  </div>
}
