import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router-dom"
import {
  AlertTriangle,
  ArrowRight,
  Box,
  ChartNoAxesCombined,
  CheckCircle2,
  Clock3,
  Database,
  Download,
  FileJson,
  FileUp,
  FlaskConical,
  History,
  LoaderCircle,
  Play,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
} from "lucide-react"
import { toast } from "sonner"

import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage, query } from "@/lib/api"
import type {
  BopEvaluationIssue,
  BopEvaluationSetup,
  BopEvaluationSummary,
  BopResultSubmission,
  Job,
} from "@/lib/contracts"
import { jobStatusTone } from "@/lib/jobs"
import { cn, formatDate, titleCase } from "@/lib/utils"
import { useOperator } from "@/providers/operator-provider"

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"])
const FAILED_JOB_STATUSES = new Set(["failed", "canceled", "cancelled"])
const TERMINAL_JOB_STATUSES = new Set(["succeeded", ...FAILED_JOB_STATUSES])
const HISTORY_LIMIT = 20

type SourceKind = "registered_result" | "gt_simulation"

interface EvaluationResponse {
  job: Job
  job_id: string
  evaluation_id: string
}

interface ImportResponse {
  result?: {
    result_id: string
    display_name: string
  }
  result_id?: string
  output?: string
}

interface SubmittedEvaluation {
  runRoot: string
  evaluationId: string
  job: Job
}

function shortHash(value: string | null | undefined) {
  return value ? `${value.slice(0, 16)}…` : "—"
}

function targetCoverage(value: number) {
  const percentage = value <= 1 ? value * 100 : value
  return `${percentage.toFixed(1)}%`
}

function evaluationJob(job: Job, runRoot: string) {
  return job.scope_kind === "run"
    && job.run_root === runRoot
    && typeof job.parameters.evaluation_id === "string"
}

function Detail({ label, value, mono = false }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return <div className="min-w-0">
    <div className="text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">{label}</div>
    <div className={cn("mt-1 break-words text-xs", mono && "font-mono text-[10px]")}>{value}</div>
  </div>
}

function Issues({ title, issues }: { title: string; issues: BopEvaluationIssue[] }) {
  if (!issues.length) return null
  return <div role="alert" className="rounded-lg border border-destructive/35 bg-destructive/5 p-3">
    <div className="flex items-center gap-2 text-xs font-semibold text-destructive"><AlertTriangle aria-hidden="true" className="size-4" />{title}</div>
    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground">
      {issues.map((issue) => <li key={`${issue.code}:${issue.message}`}>{issue.message}<span className="ml-1 font-mono text-[9px]">({issue.code})</span></li>)}
    </ul>
  </div>
}

function ResultDetails({ result, runRoot }: { result: BopResultSubmission; runRoot: string }) {
  return <div data-testid="bop-result-details" className={cn("rounded-lg border p-4", result.compatible ? "border-success/30 bg-success/5" : "border-destructive/35 bg-destructive/5")}>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{result.display_name}</span><StatusBadge status={result.compatible ? "valid" : "invalid"} tone={result.compatible ? "success" : "destructive"}>{result.compatible ? "compatible" : "incompatible"}</StatusBadge></div>
        <div className="mt-1 text-xs text-muted-foreground">{result.method} · imported {formatDate(result.created_at)}</div>
      </div>
      <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-[10px] text-muted-foreground">{shortHash(result.sha256)}</span>
        <Button asChild size="sm"><Link to={query("/pose-results", { result_id: result.result_id, run_root: runRoot })}><ScanSearch />Inspect poses</Link></Button>
        <Button asChild variant="outline" size="sm"><a href={query(`/bop/evaluation/results/${result.result_id}/package`, { run_root: runRoot })}><Box />Package</a></Button>
        <Button asChild variant="outline" size="sm"><a href={query(`/bop/evaluation/results/${result.result_id}/download`, { run_root: runRoot })}><Download />CSV</a></Button>
        {result.provenance_available && <Button asChild variant="outline" size="sm"><a href={query(`/bop/evaluation/results/${result.result_id}/provenance`, { run_root: runRoot })}><FileJson />Provenance</a></Button>}
      </div>
    </div>
    <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Detail label="BOP CSV" value={result.filename} mono />
      <Detail label="Estimates" value={result.estimate_count.toLocaleString()} />
      <Detail label="Target estimates" value={result.target_estimate_count.toLocaleString()} />
      <Detail label="Target coverage" value={targetCoverage(result.target_coverage)} />
    </div>
    {result.sensor_scope && <div className="mt-3 rounded border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
      {result.sensor_scope.execution_mode === "continuous_tracking" ? "Continuous tracking" : "Independent registration"} · BOP sensor scene{result.sensor_scope.selected_scene_ids.length === 1 ? "" : "s"} {result.sensor_scope.selected_scene_ids.join(", ")} · {result.sensor_scope.selected_target_count.toLocaleString()} selected targets
    </div>}
    <Issues title="This result cannot be evaluated" issues={result.blockers} />
  </div>
}

function EvaluationJobStatus({ job, evaluationId, reportAvailable }: { job: Job; evaluationId: string | null; reportAvailable: boolean }) {
  const active = ACTIVE_JOB_STATUSES.has(job.status)
  const failed = FAILED_JOB_STATUSES.has(job.status)
  const succeeded = job.status === "succeeded"
  const title = active
    ? job.status === "queued"
      ? "BOP evaluation is queued"
      : job.status === "canceling"
        ? "BOP evaluation is canceling"
        : "BOP evaluation is running"
    : failed
      ? job.status === "canceled" || job.status === "cancelled"
        ? "BOP evaluation was canceled"
        : "BOP evaluation failed"
      : succeeded && !reportAvailable
        ? "Evaluation finished; the report is being verified"
        : "BOP evaluation report is available"
  const description = active
    ? `Job ${job.id} continues after navigation. Jobs shows resource locks, the live BOP Toolkit output, and cancellation.`
    : failed
      ? `Job ${job.id} ended with status ${job.status}${job.returncode == null ? "" : ` and return code ${job.returncode}`}. The dataset and imported result were not modified.`
      : succeeded && !reportAvailable
        ? `Job ${job.id} returned successfully, but the durable evaluation report is not visible yet. This page refreshes it automatically.`
        : `Job ${job.id} completed and evaluation ${evaluationId ?? "—"} has durable metric evidence.`
  const Icon = active ? LoaderCircle : failed ? AlertTriangle : reportAvailable ? CheckCircle2 : RefreshCw

  return <div data-testid="bop-evaluation-job-status" role="status" className={cn("rounded-lg border p-4", active ? "border-warning/40 bg-warning/5" : failed ? "border-destructive/40 bg-destructive/5" : reportAvailable ? "border-success/35 bg-success/5" : "border-primary/35 bg-primary/5")}>
    <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        <Icon aria-hidden="true" className={cn("mt-0.5 size-5 shrink-0", active && "animate-spin", failed ? "text-destructive" : reportAvailable ? "text-success" : "text-primary-strong")} />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{title}</span><StatusBadge status={job.status} tone={jobStatusTone(job.status)} /></div>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{description}</p>
          {job.message && failed && <p className="mt-2 break-words font-mono text-[10px] text-destructive">{job.message}</p>}
        </div>
      </div>
      <Button asChild variant="outline" size="sm" className="shrink-0 bg-card"><Link to="/jobs">{active ? "Open live log in Jobs" : "Open job details"}<ArrowRight aria-hidden="true" /></Link></Button>
    </div>
  </div>
}

function MetricsReport({ evaluation }: { evaluation: BopEvaluationSummary }) {
  const result = evaluation.result
  const simulation = evaluation.simulation ?? result?.simulation
  return <Card data-testid="bop-evaluation-report" className={evaluation.report_available ? "border-success/30" : undefined}>
    <CardHeader className="border-b border-border bg-muted/20">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">Official BOP metrics <StatusBadge status={evaluation.status} tone={jobStatusTone(evaluation.status)} /></CardTitle>
          <CardDescription className="mt-1">{evaluation.protocol} · evaluation {evaluation.evaluation_id}</CardDescription>
        </div>
        <div className="text-left text-[10px] text-muted-foreground sm:text-right"><div className="font-semibold uppercase tracking-wide">Completed</div><div className="mt-1">{formatDate(evaluation.completed_at)}</div></div>
      </div>
    </CardHeader>
    <CardContent className="space-y-5 pt-5">
      {evaluation.sensor_scoped && evaluation.sensor_scope && <div data-testid="bop-evaluation-sensor-scope" className="flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs">
        <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning-foreground" />
        <div><div className="font-semibold text-warning-foreground">Sensor-scoped evaluation</div><p className="mt-1 leading-relaxed text-muted-foreground">Official metrics use only BOP sensor scene{evaluation.sensor_scope.selected_scene_ids.length === 1 ? "" : "s"} {evaluation.sensor_scope.selected_scene_ids.join(", ")} and their {evaluation.sensor_scope.selected_target_count.toLocaleString()} targets. This report is not directly comparable to a full-dataset result.</p></div>
      </div>}
      {evaluation.source_kind === "gt_simulation" && <div className="flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs"><FlaskConical aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning-foreground" /><div><div className="font-semibold text-warning-foreground">Test-only simulated estimates</div><p className="mt-1 leading-relaxed text-muted-foreground">These values measure a GT-derived compatibility fixture, not pose-estimator performance.</p></div></div>}
      {evaluation.source_kind === "gt_simulation" && simulation && <div data-testid="bop-evaluation-simulation-evidence" className="grid gap-3 rounded-lg border border-warning/30 bg-warning/5 p-4 sm:grid-cols-2 xl:grid-cols-5">
        <Detail label="Simulation method" value={simulation.method_name ?? "GT slight offset"} />
        <Detail label="Translation sigma" value={`${simulation.translation_sigma_mm.toFixed(3)} mm`} />
        <Detail label="Rotation sigma" value={`${simulation.rotation_sigma_deg.toFixed(3)}°`} />
        <Detail label="Seed" value={simulation.seed} mono />
        <Detail label="Estimate score" value={simulation.score ?? "—"} />
      </div>}
      {result && !result.compatible && <Issues title="This historical result no longer matches the current dataset" issues={result.blockers.length ? result.blockers : [{ code: "result_incompatible", message: "The registered result is not compatible with the current selected-run dataset." }]} />}
      {evaluation.metrics.length > 0
        ? <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {evaluation.metrics.map((metric) => <div key={metric.id} className="rounded-lg border bg-background p-4">
            <div className="text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">{metric.label}</div>
            <div className="metric-number mt-2 tabular-nums">{metric.display || metric.value.toPrecision(5)}</div>
            <div className="mt-2 font-mono text-[9px] text-muted-foreground">{metric.id}{metric.unit ? ` · ${metric.unit}` : ""}</div>
          </div>)}
        </div>
        : <div className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">The evaluation has no published metrics yet.</div>}
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full min-w-[560px] text-left text-xs">
            <caption className="sr-only">Official BOP evaluation metric values</caption>
            <thead className="bg-muted/60 text-muted-foreground"><tr><th scope="col" className="px-3 py-2">Metric</th><th scope="col" className="px-3 py-2">Official ID</th><th scope="col" className="px-3 py-2 text-right">Value</th></tr></thead>
            <tbody>{evaluation.metrics.map((metric) => <tr key={metric.id} className="border-t"><td className="px-3 py-2.5 font-semibold">{metric.label}</td><td className="px-3 py-2.5 font-mono text-[10px] text-muted-foreground">{metric.id}</td><td className="px-3 py-2.5 text-right font-mono tabular-nums">{metric.display || metric.value}{metric.unit ? ` ${metric.unit}` : ""}</td></tr>)}</tbody>
          </table>
        </div>
        <div className="space-y-3 rounded-lg border p-4">
          <div className="text-sm font-semibold">Evaluation identity</div>
          <div className="grid grid-cols-2 gap-3">
            <Detail label="Source" value={evaluation.source_kind === "gt_simulation" ? "GT simulation" : "Registered result"} />
            <Detail label="Result" value={result?.display_name ?? evaluation.result_id ?? "—"} />
            <Detail label="Method" value={result?.method ?? "—"} />
            <Detail label="Result hash" value={shortHash(result?.sha256)} mono />
          </div>
          <details className="rounded border">
            <summary className="cursor-pointer px-3 py-2 text-xs font-semibold">Raw provenance</summary>
            <pre data-testid="bop-evaluation-provenance" className="max-h-72 overflow-auto border-t bg-muted p-3 text-[10px] leading-relaxed">{JSON.stringify({ simulation: simulation ?? null, evaluation: evaluation.provenance }, null, 2)}</pre>
          </details>
        </div>
      </div>
    </CardContent>
  </Card>
}

export function BopEvaluationPage() {
  const { selectedRun: activeRun, selectRun } = useOperator()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedRun = searchParams.get("run_root") || activeRun
  const queryClient = useQueryClient()
  const terminalRefresh = useRef<string | null>(null)
  const [sourceKind, setSourceKind] = useState<SourceKind>("registered_result")
  const [resultSelection, setResultSelection] = useState<{ runRoot: string; resultId: string } | null>(null)
  const [evaluationSelection, setEvaluationSelection] = useState<{ runRoot: string; evaluationId: string } | null>(null)
  const [submittedEvaluation, setSubmittedEvaluation] = useState<SubmittedEvaluation | null>(null)
  const [uploadSelection, setUploadSelection] = useState<{ runRoot: string; file: File } | null>(null)
  const [uploadName, setUploadName] = useState<{ runRoot: string; value: string } | null>(null)
  const [uploadEpoch, setUploadEpoch] = useState(0)
  const [translationSigmaMm, setTranslationSigmaMm] = useState(1)
  const [rotationSigmaDeg, setRotationSigmaDeg] = useState(0.25)
  const [seed, setSeed] = useState(42)
  const [score, setScore] = useState(1)
  const uploadFile = uploadSelection?.runRoot === selectedRun ? uploadSelection.file : null
  const uploadDisplayName = uploadName?.runRoot === selectedRun ? uploadName.value : ""

  const setup = useQuery({
    queryKey: ["bop-evaluation", "setup", selectedRun],
    queryFn: () => api<BopEvaluationSetup>(query("/bop/evaluation/setup", { run_root: selectedRun })),
    retry: false,
  })
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<{ jobs: Job[]; resources: Record<string, string> }>("/jobs"),
    refetchInterval: (state) => state.state.data?.jobs.some((job) => evaluationJob(job, selectedRun) && ACTIVE_JOB_STATUSES.has(job.status)) ? 1_000 : 5_000,
  })

  const realResults = useMemo(
    () => (setup.data?.results ?? []).filter((result) => result.source_kind !== "gt_simulation"),
    [setup.data?.results],
  )
  const requestedResultId = searchParams.get("result_id") ?? ""
  const savedResultId = resultSelection?.runRoot === selectedRun ? resultSelection.resultId : ""
  const selectedResultId = requestedResultId
    ? requestedResultId
    : realResults.some((result) => result.result_id === savedResultId)
      ? savedResultId
    : realResults.find((result) => result.compatible)?.result_id ?? realResults[0]?.result_id ?? ""
  const selectedResult = realResults.find((result) => result.result_id === selectedResultId) ?? null

  const orderedEvaluations = useMemo(
    () => [...(setup.data?.evaluations ?? [])].sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [setup.data?.evaluations],
  )
  const scopedEvaluations = useMemo(
    () => orderedEvaluations.filter((evaluation) => sourceKind === "registered_result"
      ? evaluation.source_kind !== "gt_simulation" && evaluation.result_id === selectedResultId
      : evaluation.source_kind === "gt_simulation"),
    [orderedEvaluations, selectedResultId, sourceKind],
  )
  const savedEvaluationId = evaluationSelection?.runRoot === selectedRun ? evaluationSelection.evaluationId : ""
  const submittedEvaluationId = submittedEvaluation?.runRoot === selectedRun ? submittedEvaluation.evaluationId : ""
  const selectedEvaluationId = scopedEvaluations.some((evaluation) => evaluation.evaluation_id === savedEvaluationId)
    ? savedEvaluationId
    : scopedEvaluations.some((evaluation) => evaluation.evaluation_id === submittedEvaluationId)
      ? submittedEvaluationId
      : scopedEvaluations.find((evaluation) => evaluation.report_available)?.evaluation_id
        ?? scopedEvaluations[0]?.evaluation_id
        ?? ""
  const selectedEvaluation = scopedEvaluations.find((evaluation) => evaluation.evaluation_id === selectedEvaluationId) ?? null

  const persistedEvaluationJobs = useMemo(
    () => [...(jobs.data?.jobs ?? [])]
      .filter((job) => evaluationJob(job, selectedRun))
      .sort((left, right) => right.created_at.localeCompare(left.created_at)),
    [jobs.data?.jobs, selectedRun],
  )
  const scopedEvaluationJobs = useMemo(
    () => persistedEvaluationJobs.filter((job) => sourceKind === "registered_result"
      ? job.parameters.source_kind === "registered_result" && job.parameters.result_id === selectedResultId
      : job.parameters.source_kind === "gt_simulation"),
    [persistedEvaluationJobs, selectedResultId, sourceKind],
  )
  const submittedForRun = submittedEvaluation?.runRoot === selectedRun ? submittedEvaluation : null
  const submittedMatchesScope = Boolean(submittedForRun && (sourceKind === "registered_result"
    ? submittedForRun.job.parameters.source_kind === "registered_result" && submittedForRun.job.parameters.result_id === selectedResultId
    : submittedForRun.job.parameters.source_kind === "gt_simulation"))
  const submittedJob = submittedForRun && submittedMatchesScope
    ? scopedEvaluationJobs.find((job) => job.id === submittedForRun.job.id) ?? submittedForRun.job
    : null
  const currentJob = scopedEvaluationJobs.find((job) => ACTIVE_JOB_STATUSES.has(job.status))
    ?? submittedJob
    ?? scopedEvaluationJobs[0]
    ?? null
  const currentEvaluationId = typeof currentJob?.parameters.evaluation_id === "string"
    ? currentJob.parameters.evaluation_id
    : currentJob?.id === submittedJob?.id
      ? submittedForRun?.evaluationId ?? null
      : null
  const currentReportAvailable = Boolean(
    currentEvaluationId
    && scopedEvaluations.find((evaluation) => evaluation.evaluation_id === currentEvaluationId)?.report_available,
  )
  const activeJob = persistedEvaluationJobs.some((item) => ACTIVE_JOB_STATUSES.has(item.status))

  useEffect(() => {
    if (!currentJob || !TERMINAL_JOB_STATUSES.has(currentJob.status)) return
    const refreshKey = `${currentJob.id}:${currentJob.status}`
    if (terminalRefresh.current === refreshKey) return
    terminalRefresh.current = refreshKey
    void queryClient.invalidateQueries({ queryKey: ["bop-evaluation", "setup", selectedRun] })
  }, [currentJob, queryClient, selectedRun])

  const importResult = useMutation({
    mutationFn: async () => {
      if (!uploadFile) throw new Error("Choose a BOP result CSV")
      const body = new FormData()
      body.append("run_root", selectedRun)
      body.append("file", uploadFile)
      body.append("display_name", uploadDisplayName.trim() || uploadFile.name.replace(/\.csv$/i, ""))
      return api<ImportResponse>("/bop/evaluation/results", { method: "POST", body })
    },
    onSuccess: (data) => {
      const resultId = data.result?.result_id ?? data.result_id
      if (resultId) {
        setSourceKind("registered_result")
        setResultSelection({ runRoot: selectedRun, resultId })
        setSearchParams({ result_id: resultId, run_root: selectedRun })
      }
      setUploadSelection(null)
      setUploadName(null)
      setUploadEpoch((value) => value + 1)
      toast.success("BOP result imported", { description: data.result?.display_name ?? data.output ?? "The compatibility evidence is ready to inspect." })
      void queryClient.invalidateQueries({ queryKey: ["bop-evaluation", "setup", selectedRun] })
    },
    onError: (error) => toast.error("BOP result was not imported", { description: errorMessage(error) }),
  })

  const queueEvaluation = useMutation({
    mutationFn: () => api<EvaluationResponse>("/bop/evaluations", {
      method: "POST",
      body: JSON.stringify({
        run_root: selectedRun,
        source: sourceKind === "registered_result"
          ? { kind: "registered_result", result_id: selectedResultId }
          : {
              kind: "gt_simulation",
              translation_sigma_mm: translationSigmaMm,
              rotation_sigma_deg: rotationSigmaDeg,
              seed,
              score,
            },
      }),
    }),
    onSuccess: (data) => {
      setSubmittedEvaluation({ runRoot: selectedRun, evaluationId: data.evaluation_id, job: data.job })
      setEvaluationSelection({ runRoot: selectedRun, evaluationId: data.evaluation_id })
      toast.success("BOP evaluation queued", { description: `Job ${data.job_id} continues after navigation; status and output are available in Jobs.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
      void queryClient.invalidateQueries({ queryKey: ["bop-evaluation", "setup", selectedRun] })
    },
    onError: (error) => toast.error("BOP evaluation was not queued", { description: errorMessage(error) }),
  })

  const dataset = setup.data?.dataset
  const toolkit = setup.data?.toolkit
  const queueBlockers = useMemo(() => {
    const blockers: string[] = []
    if (setup.isPending) return ["Dataset and toolkit compatibility are still loading."]
    if (setup.isError || !dataset || !toolkit) return ["Dataset evaluation status is unavailable. Refresh before queueing."]
    if (!toolkit.available || !toolkit.environment_ready) blockers.push(toolkit.reason ?? "The required BOP Toolkit environment is not ready.")
    if (!dataset.evaluation_ready) {
      if (dataset.blockers.length) blockers.push(...dataset.blockers.map((item) => item.message))
      else blockers.push("The selected dataset is not ready for BOP metric evaluation.")
    }
    if (sourceKind === "registered_result") {
      if (!selectedResult) blockers.push(requestedResultId ? "The linked result is unavailable in this run. Select a retained result or open its original run." : "Import or select a BOP result CSV.")
      else if (!selectedResult.compatible) blockers.push(...(selectedResult.blockers.length ? selectedResult.blockers.map((item) => item.message) : ["The selected result is not compatible with this dataset."]))
    } else {
      if (!dataset.simulation_ready) blockers.push("Ground-truth simulation is unavailable for this dataset.")
      if (!Number.isFinite(translationSigmaMm) || translationSigmaMm < 0 || translationSigmaMm > 100) blockers.push("Translation sigma must be between 0 and 100 mm.")
      if (!Number.isFinite(rotationSigmaDeg) || rotationSigmaDeg < 0 || rotationSigmaDeg > 30) blockers.push("Rotation sigma must be between 0° and 30°.")
      if (!Number.isInteger(seed) || seed < -(2 ** 31) || seed >= 2 ** 31) blockers.push("Simulation seed must be a signed 32-bit integer.")
      if (!Number.isFinite(score)) blockers.push("Simulation score must be a finite number.")
    }
    if (activeJob) blockers.push("Wait for the active BOP evaluation job to finish or cancel it from Jobs.")
    return [...new Set(blockers)]
  }, [activeJob, dataset, requestedResultId, rotationSigmaDeg, score, seed, selectedResult, setup.isError, setup.isPending, sourceKind, toolkit, translationSigmaMm])

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["bop-evaluation", "setup", selectedRun] })
    void queryClient.invalidateQueries({ queryKey: ["jobs"] })
  }

  return <div className="space-y-6">
    <PageHeader
      eyebrow="Dataset inspection"
      title="BOP evaluation"
      description="Validate an exported dataset with official BOP metrics, using a compatible estimator result or a clearly marked GT-derived test fixture."
      actions={<Button variant="outline" onClick={refresh} disabled={setup.isFetching || jobs.isFetching}><RefreshCw className={setup.isFetching || jobs.isFetching ? "animate-spin" : undefined} />Refresh</Button>}
    />
    {selectedRun !== activeRun && <div role="status" className="flex items-center justify-between gap-3 rounded-lg border p-3 text-xs"><span>Evaluating linked run <span className="font-mono">{selectedRun}</span>. The active operator run is different.</span><Button variant="outline" size="sm" onClick={() => selectRun(selectedRun)}>Make linked run active</Button></div>}
    <ProcessHandoff
      title="Evaluation consumes the selected run's BOP export"
      description="This page reads the run-owned export and writes separate result and evaluation evidence. It never changes raw capture, synchronization, calibration, or exported dataset files."
      to="/workflow/dataset?step=export"
      action="Review BOP export"
    />

    {setup.isPending
      ? <div className="space-y-4"><Skeleton className="h-52" /><div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_390px]"><Skeleton className="h-[520px]" /><Skeleton className="h-[520px]" /></div></div>
      : setup.isError || !dataset || !toolkit
        ? <Card className="border-destructive/40"><CardHeader><CardTitle>BOP evaluation setup unavailable</CardTitle><CardDescription>{errorMessage(setup.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={refresh}><RefreshCw />Try again</Button></CardContent></Card>
        : <>
          <Card data-testid="bop-evaluation-dataset" className={dataset.evaluation_ready ? "border-success/30" : "border-warning/40"}>
            <CardHeader className="border-b border-border bg-muted/20">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div><CardTitle className="flex flex-wrap items-center gap-2 text-base"><Database aria-hidden="true" className="size-4 text-primary-strong" />Selected-run dataset <StatusBadge status={dataset.evaluation_ready ? "ready" : dataset.status} tone={dataset.evaluation_ready ? "success" : "destructive"}>{dataset.evaluation_ready ? "evaluation ready" : dataset.status}</StatusBadge></CardTitle><CardDescription className="mt-1">A direct link selects its recorded run; otherwise this page uses the active operator run. This run owns one BOP export.</CardDescription></div>
                <div className="max-w-full text-left sm:text-right"><div className="text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Selected run</div><div className="mt-1 max-w-3xl truncate font-mono text-[10px]" title={selectedRun}>{selectedRun}</div></div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-5">
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
                <Detail label="Dataset" value={dataset.name ?? dataset.dataset_id ?? "—"} />
                <Detail label="Split" value={dataset.split ?? "—"} />
                <Detail label="Scenes" value={dataset.scene_count.toLocaleString()} />
                <Detail label="Frames" value={dataset.frame_count.toLocaleString()} />
                <Detail label="Targets" value={dataset.target_count.toLocaleString()} />
                <Detail label="Models" value={dataset.model_count.toLocaleString()} />
                <Detail label="GT annotations" value={`${dataset.annotation_count.toLocaleString()} · ${dataset.annotation_source ? titleCase(dataset.annotation_source) : "none"}`} />
                <Detail label="Image size" value={dataset.image_size ? `${dataset.image_size[0]} × ${dataset.image_size[1]}` : "—"} />
              </div>
              <div className="grid gap-3 border-t pt-4 lg:grid-cols-3">
                <Detail label="Export manifest" value={`${dataset.manifest_schema_version ?? "—"} · ${shortHash(dataset.export_manifest_sha256)}`} mono />
                <Detail label="Dataset ID" value={dataset.dataset_id ?? "—"} mono />
                <Detail label="Expected result filename" value={dataset.result_filename_template ?? "Not advertised"} mono />
              </div>
              <Issues title="Dataset evaluation is blocked" issues={dataset.blockers} />
              {dataset.warnings.length > 0 && <div className="rounded-lg border border-warning/40 bg-warning/5 p-3"><div className="text-xs font-semibold text-warning-foreground">Dataset warnings</div><ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-muted-foreground">{dataset.warnings.map((warning) => <li key={`${warning.code}:${warning.message}`}>{warning.message}<span className="ml-1 font-mono text-[9px]">({warning.code})</span></li>)}</ul></div>}
            </CardContent>
          </Card>

          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_390px]">
            <Card data-testid="bop-evaluation-source">
              <CardHeader><CardTitle className="text-base">1 · Retained result selection and readiness</CardTitle><CardDescription>Collected cluster results are the primary path. Every choice is an immutable, locally revalidated standard BOP19 CSV.</CardDescription></CardHeader>
              <CardContent className="space-y-5">
                {sourceKind === "gt_simulation" && <div role="status" className="flex items-center justify-between gap-3 rounded-lg border border-warning/45 bg-warning/10 p-3 text-xs"><span><strong>Test-only simulation is selected.</strong> Its history and report are isolated from retained estimator results.</span>{selectedResult && <Button size="sm" variant="outline" onClick={() => setSourceKind("registered_result")}>Use retained result</Button>}</div>}
                {realResults.length > 0 ? <div className="space-y-3">
                  <div className="space-y-1.5"><Label htmlFor="bop-result-selection">Retained pose result</Label><Select value={selectedResultId} onValueChange={(resultId) => { setSourceKind("registered_result"); setResultSelection({ runRoot: selectedRun, resultId }); setSearchParams({ result_id: resultId, run_root: selectedRun }) }}><SelectTrigger id="bop-result-selection" aria-label="Retained pose result"><SelectValue /></SelectTrigger><SelectContent>{realResults.map((result) => <SelectItem key={result.result_id} value={result.result_id}>{result.display_name} · {result.method} · {result.compatible ? "compatible" : "incompatible"}</SelectItem>)}</SelectContent></Select></div>
                  {selectedResult && <ResultDetails result={selectedResult} runRoot={selectedRun} />}
                </div> : <div className="rounded-lg border border-dashed p-6 text-center"><ScanSearch className="mx-auto size-6 text-muted-foreground" /><div className="mt-2 text-sm font-semibold">No retained estimator result</div><p className="mt-1 text-xs text-muted-foreground">Submit and collect a compatible external estimator result first, or expand manual import below.</p><Button asChild className="mt-4" size="sm"><Link to="/pose-estimation">Open Pose Estimation<ArrowRight /></Link></Button></div>}

                <details className="rounded-lg border" data-testid="bop-manual-import">
                  <summary className="cursor-pointer px-4 py-3 text-sm font-semibold">Manual BOP19 CSV import · secondary</summary>
                  <form className="space-y-4 border-t bg-muted/15 p-4" onSubmit={(event) => { event.preventDefault(); importResult.mutate() }}>
                    <div><div className="flex items-center gap-2 text-sm font-semibold"><FileUp aria-hidden="true" className="size-4" />Import standard BOP result CSV</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">The server validates the official columns, dataset/split identity, target coverage, and pose values before registering a selectable immutable result. Manual imports have no fabricated controller provenance.</p></div>
                    <div data-testid="bop-result-csv-contract" className="space-y-2 rounded-lg border bg-background p-3 text-[11px] leading-relaxed text-muted-foreground">
                      <div className="font-semibold text-foreground">Interoperable BOP CSV contract</div>
                      <code className="block max-w-full overflow-x-auto rounded bg-muted px-2 py-1.5 text-[10px] text-foreground">scene_id,im_id,obj_id,score,R,t,time</code>
                      <p><strong className="text-foreground">R</strong> contains nine row-major model-to-camera rotation values. <strong className="text-foreground">t</strong> contains three model-to-camera millimetre values. <strong className="text-foreground">time</strong> is consistent per image, or <code>-1</code> when unavailable.</p>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="space-y-1.5"><Label htmlFor="bop-result-file">Result CSV</Label><Input key={`${selectedRun}:${uploadEpoch}`} id="bop-result-file" type="file" accept=".csv,text/csv" onChange={(event) => { const file = event.target.files?.[0] ?? null; setUploadSelection(file ? { runRoot: selectedRun, file } : null); if (file && !uploadDisplayName.trim()) setUploadName({ runRoot: selectedRun, value: file.name.replace(/\.csv$/i, "") }) }} /></div>
                      <div className="space-y-1.5"><Label htmlFor="bop-result-display-name">Display name</Label><Input id="bop-result-display-name" value={uploadDisplayName} onChange={(event) => setUploadName({ runRoot: selectedRun, value: event.target.value })} placeholder="Method and result run" /></div>
                    </div>
                    {!dataset.result_registration_ready && <div role="alert" className="rounded border border-warning/40 bg-warning/5 p-3 text-xs text-warning-foreground">Result import requires an exported manifest and populated BOP target inventory for this run.</div>}
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between"><p className="text-[11px] text-muted-foreground">Import never rewrites the source file or the BOP dataset.</p><Button type="submit" variant="outline" disabled={!dataset.result_registration_ready || !uploadFile || importResult.isPending}>{importResult.isPending ? <LoaderCircle className="animate-spin" /> : <FileUp />}{importResult.isPending ? "Importing…" : "Import result"}</Button></div>
                  </form>
                </details>

                <details className="rounded-lg border border-warning/35" data-testid="bop-test-simulation">
                  <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-warning-foreground">Advanced · deterministic GT perturbation · Test only</summary>
                  <div className="space-y-4 border-t p-4">
                    <div role="alert" className="flex items-start gap-3 rounded-lg border border-warning/45 bg-warning/10 p-4"><FlaskConical aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-warning-foreground" /><div><div className="font-semibold text-warning-foreground">Estimates are derived from ground truth</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">This fixture checks formatting and the official evaluation path. Its metrics must never be presented as pose-estimator performance.</p></div></div>
                    <div className="grid gap-4 rounded-lg border p-4 sm:grid-cols-2">
                      <div className="space-y-1.5"><Label htmlFor="translation-sigma">Translation sigma (mm)</Label><Input id="translation-sigma" type="number" min={0} max={100} step={0.1} value={Number.isNaN(translationSigmaMm) ? "" : translationSigmaMm} onChange={(event) => setTranslationSigmaMm(event.currentTarget.value === "" ? Number.NaN : event.currentTarget.valueAsNumber)} /></div>
                      <div className="space-y-1.5"><Label htmlFor="rotation-sigma">Rotation sigma (degrees)</Label><Input id="rotation-sigma" type="number" min={0} max={30} step={0.05} value={Number.isNaN(rotationSigmaDeg) ? "" : rotationSigmaDeg} onChange={(event) => setRotationSigmaDeg(event.currentTarget.value === "" ? Number.NaN : event.currentTarget.valueAsNumber)} /></div>
                      <div className="space-y-1.5"><Label htmlFor="simulation-seed">Deterministic seed</Label><Input id="simulation-seed" type="number" min={-(2 ** 31)} max={2 ** 31 - 1} step={1} value={Number.isNaN(seed) ? "" : seed} onChange={(event) => setSeed(event.currentTarget.value === "" ? Number.NaN : event.currentTarget.valueAsNumber)} /></div>
                      <div className="space-y-1.5"><Label htmlFor="simulation-score">Estimate score</Label><Input id="simulation-score" type="number" step={0.01} value={Number.isNaN(score) ? "" : score} onChange={(event) => setScore(event.currentTarget.value === "" ? Number.NaN : event.currentTarget.valueAsNumber)} /></div>
                    </div>
                    {!dataset.simulation_ready && <div role="alert" className="rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs text-destructive">Simulation requires complete ground-truth annotations and evaluation targets for the selected dataset.</div>}
                    <Button variant={sourceKind === "gt_simulation" ? "secondary" : "outline"} onClick={() => setSourceKind("gt_simulation")} disabled={!dataset.simulation_ready}>{sourceKind === "gt_simulation" ? "Test-only simulation selected" : "Use test-only simulation source"}</Button>
                  </div>
                </details>
              </CardContent>
            </Card>

            <div className="space-y-5">
              <Card data-testid="bop-toolkit-status" className={toolkit.available && toolkit.environment_ready ? "border-success/30" : "border-destructive/35"}>
                <CardHeader><CardTitle className="flex flex-wrap items-center gap-2 text-base"><ShieldCheck aria-hidden="true" className="size-4" />BOP Toolkit environment <StatusBadge status={toolkit.available && toolkit.environment_ready ? "ready" : toolkit.status} tone={toolkit.available && toolkit.environment_ready ? "success" : "destructive"} /></CardTitle><CardDescription>Evaluation is bound to the pinned toolkit and renderer environment reported by the server.</CardDescription></CardHeader>
                <CardContent className="space-y-4">
                  <div className="grid grid-cols-2 gap-3">
                    <Detail label="Detected revision" value={toolkit.revision ?? "—"} mono />
                    <Detail label="Required revision" value={toolkit.required_revision} mono />
                    <Detail label="Renderer" value={toolkit.renderer ?? "—"} />
                    <Detail label="Environment" value={toolkit.environment_ready ? "ready" : "not ready"} />
                  </div>
                  {toolkit.reason && <div role="alert" className="rounded border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">{toolkit.reason}</div>}
                  {toolkit.install_command && <div><div className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">Setup command</div><code className="mt-1 block overflow-x-auto rounded bg-muted p-2 text-[10px]">{toolkit.install_command}</code></div>}
                </CardContent>
              </Card>

              <Card className="border-primary/30" data-testid="bop-evaluation-submit">
                <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Play aria-hidden="true" className="size-4" />2 · Run official evaluation</CardTitle><CardDescription>Explicitly queue the pinned official BOP Toolkit for the source selected at left. The CPU/disk job continues after navigation.</CardDescription></CardHeader>
                <CardContent className="space-y-4">
                  {queueBlockers.length > 0 ? <div role="alert" data-testid="bop-evaluation-disabled-reasons" className="rounded-lg border border-warning/40 bg-warning/5 p-3"><div className="text-xs font-semibold text-warning-foreground">Evaluation cannot be queued yet</div><ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-relaxed text-muted-foreground">{queueBlockers.map((reason) => <li key={reason}>{reason}</li>)}</ul></div> : <div className="flex items-start gap-2 rounded-lg border border-success/30 bg-success/5 p-3 text-xs"><CheckCircle2 aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-success" /><span>The dataset, toolkit, and selected result source are compatible.</span></div>}
                  <Button className="w-full" onClick={() => queueEvaluation.mutate()} disabled={queueBlockers.length > 0 || queueEvaluation.isPending}>{queueEvaluation.isPending || activeJob ? <LoaderCircle className="animate-spin" /> : <ChartNoAxesCombined />}{queueEvaluation.isPending ? "Queueing…" : activeJob ? "Evaluation running…" : "Queue BOP evaluation"}</Button>
                  <p className="text-[11px] leading-relaxed text-muted-foreground">Work continues after navigation. <Link to="/jobs" className="font-semibold text-foreground underline underline-offset-4">Open Jobs</Link> for resource ownership, logs, and cancellation.</p>
                </CardContent>
              </Card>
            </div>
          </div>

          {currentJob && <EvaluationJobStatus job={currentJob} evaluationId={currentEvaluationId} reportAvailable={currentReportAvailable} />}

          {scopedEvaluations.length > 0 && <Card data-testid="bop-evaluation-history">
            <CardHeader><CardTitle className="flex items-center gap-2 text-base"><History aria-hidden="true" className="size-4" />3 · Result-scoped evaluation history</CardTitle><CardDescription>{sourceKind === "registered_result" ? `Only evaluations bound to ${selectedResult?.display_name ?? selectedResultId} are shown.` : "Only deterministic test-fixture evaluations are shown."}</CardDescription></CardHeader>
            <CardContent>
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[820px] text-left text-xs">
                  <caption className="sr-only">BOP evaluation history for the selected run</caption>
                  <thead className="bg-muted/60 text-muted-foreground"><tr><th scope="col" className="px-3 py-2">Evaluation</th><th scope="col" className="px-3 py-2">Result source</th><th scope="col" className="px-3 py-2">Protocol</th><th scope="col" className="px-3 py-2">Created</th><th scope="col" className="px-3 py-2">Status</th><th scope="col" className="px-3 py-2 text-right">Report</th></tr></thead>
                  <tbody>{scopedEvaluations.slice(0, HISTORY_LIMIT).map((evaluation) => <tr key={evaluation.evaluation_id} className={cn("border-t", evaluation.evaluation_id === selectedEvaluationId && "bg-primary/5")}>
                    <td className="px-3 py-2.5 font-mono text-[10px]">{evaluation.evaluation_id}</td>
                    <td className="px-3 py-2.5"><div className="font-semibold">{evaluation.source_kind === "gt_simulation" ? "GT simulation · Test only" : evaluation.result?.display_name ?? evaluation.result_id ?? "Registered result"}</div><div className="mt-0.5 text-[10px] text-muted-foreground">{evaluation.source_kind === "gt_simulation" && evaluation.simulation ? `${evaluation.simulation.translation_sigma_mm.toFixed(3)} mm · ${evaluation.simulation.rotation_sigma_deg.toFixed(3)}° · seed ${evaluation.simulation.seed}` : evaluation.result?.method ?? titleCase(evaluation.source_kind)}</div></td>
                    <td className="px-3 py-2.5">{evaluation.protocol}</td>
                    <td className="px-3 py-2.5"><span className="inline-flex items-center gap-1"><Clock3 aria-hidden="true" className="size-3" />{formatDate(evaluation.created_at)}</span></td>
                    <td className="px-3 py-2.5"><StatusBadge status={evaluation.status} tone={jobStatusTone(evaluation.status)} /></td>
                    <td className="px-3 py-2.5 text-right"><Button size="sm" variant={evaluation.evaluation_id === selectedEvaluationId ? "secondary" : "outline"} aria-label={`${evaluation.report_available ? "View metrics" : "View status"} for evaluation ${evaluation.evaluation_id}`} aria-pressed={evaluation.evaluation_id === selectedEvaluationId} onClick={() => setEvaluationSelection({ runRoot: selectedRun, evaluationId: evaluation.evaluation_id })}>{evaluation.report_available ? "View metrics" : "View status"}</Button></td>
                  </tr>)}</tbody>
                </table>
              </div>
              {scopedEvaluations.length > HISTORY_LIMIT && <p className="mt-3 text-xs text-muted-foreground">Showing the {HISTORY_LIMIT} newest of {scopedEvaluations.length} evaluations for this source.</p>}
            </CardContent>
          </Card>}

          {selectedEvaluation
            ? <MetricsReport evaluation={selectedEvaluation} />
            : <Card className="border-dashed"><CardContent className="grid min-h-40 place-items-center p-8 text-center"><div><ChartNoAxesCombined className="mx-auto size-7 text-muted-foreground" /><div className="mt-3 text-sm font-semibold">No evaluation report for this result</div><p className="mt-1 text-xs text-muted-foreground">Queue the selected source explicitly. Reports from another retained result are intentionally not shown here.</p>{!selectedResult && sourceKind === "registered_result" && <Button asChild className="mt-4" size="sm"><Link to="/pose-estimation">Collect an estimator result<ArrowRight /></Link></Button>}</div></CardContent></Card>}
        </>}
  </div>
}
