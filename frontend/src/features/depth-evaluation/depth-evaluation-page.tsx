import { QueryProblem } from "@/components/query-problem"
import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, CheckCircle2, ChevronRight, Film, FlaskConical, Layers, Loader2, Plus, X } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { PageHeader } from "@/components/page-header"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, errorMessage, query } from "@/lib/api"
import { cn } from "@/lib/utils"
import { useOperator } from "@/providers/operator-provider"
import type { DepthComparisonSubmission, DepthHistory, DepthInput, DepthKind, DepthRecord, DepthSettings, DepthSetup, DepthSubmission } from "./contracts"
import { DepthResults } from "./depth-results"

const defaults: DepthSettings = { thresholds_mm: [1, 2, 5, 10], erosion_pixels: 2, distance_bin_mm: 100, angle_bin_deg: 10 }
const finish = (r: DepthRecord) => r.submission && "finish_label" in r.submission ? r.submission.finish_label : "Finish comparison"
const toggle = (values: string[], value: string) => values.includes(value) ? values.filter(v => v !== value) : [...values, value]
type Draft = { label: string; thresholds: string; distance: number; angle: number; geometry: string[] | null; sensors: string[] | null }

function RecordState({ record }: { record: DepthRecord }) {
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] font-semibold capitalize", record.report_available ? "border-success/25 bg-success/10 text-success" : ["failed", "canceled", "invalid", "abandoned"].includes(record.state) ? "border-warning/30 bg-warning/10 text-warning-foreground" : "bg-muted text-muted-foreground")}>
    {record.report_available ? <CheckCircle2 className="size-3" /> : !["failed", "canceled", "invalid", "abandoned"].includes(record.state) ? <Loader2 className="size-3 animate-spin" /> : null}{record.state}
  </span>
}

const STAGE_LABELS: Record<string, string> = {
  queued: "Queued",
  validating: "Validating frozen inputs",
  rendering: "Rendering reference depth",
  measuring: "Measuring depth quality",
  reporting: "Writing report",
}

/** Live stage and per-stage frame counter: rendering and measuring each count to the total. */
function RecordProgress({ record }: { record: DepthRecord }) {
  const progress = record.progress
  const total = progress.total_frames
  if (record.report_available || ["failed", "canceled", "invalid", "abandoned"].includes(record.state)) return null
  const done = progress.state === "rendering" ? progress.rendered_frames ?? 0 : progress.completed_frames ?? 0
  const counted = total != null && ["rendering", "measuring", "reporting"].includes(progress.state)
  return <div className="space-y-1 px-3 pb-2" data-testid="depth-record-progress">
    <p className="text-[10px] font-medium text-foreground">{STAGE_LABELS[progress.state] ?? progress.state}</p>
    {counted && <><p className="text-[10px] text-muted-foreground">{done.toLocaleString()} / {total.toLocaleString()} frames {progress.state === "rendering" ? "rendered" : "measured"}</p>
    <progress className="h-1.5 w-full accent-primary" max={total || 1} value={done} aria-label={`${STAGE_LABELS[progress.state] ?? progress.state} progress`} /></>}
  </div>
}

function DepthRunPage({ run }: { run: string }) {
  const { runs } = useOperator()
  const cache = useQueryClient()
  const [draft] = useState(() => { try { return JSON.parse(localStorage.getItem(`depth-draft.v1:${run}`) ?? "null") as Draft | null } catch { return null } })
  const [label, setLabel] = useState(draft?.label ?? "")
  const [thresholds, setThresholds] = useState(draft?.thresholds ?? "1, 2, 5, 10")
  const [distance, setDistance] = useState(draft?.distance ?? 100)
  const [angle, setAngle] = useState(draft?.angle ?? 10)
  const [geometry, setGeometry] = useState<string[] | null>(draft?.geometry ?? null)
  const [sensors, setSensors] = useState<string[] | null>(draft?.sensors ?? null)
  const [tab, setTab] = useState<string | null>(null)
  const [historyKind, setHistoryKind] = useState<DepthKind>("evaluations")
  const [historyOffset, setHistoryOffset] = useState(0)
  const [comparisonOffset, setComparisonOffset] = useState(0)
  const [search, setSearch] = useState("")
  const [selected, setSelected] = useState<{ kind: DepthKind; id: string } | null>(null)
  const [compareRun, setCompareRun] = useState(run)
  const [compareOffset, setCompareOffset] = useState(0)
  const [inputs, setInputs] = useState<(DepthInput & { label: string })[]>([])
  const setup = useQuery({ queryKey: ["depth-setup", run], queryFn: () => api<DepthSetup>(query("/depth/evaluation/setup", { run_root: run })) })
  const history = useQuery({ queryKey: ["depth-history", run, historyKind, historyOffset, search], queryFn: () => api<DepthHistory>(query(`/depth/${historyKind}`, { run_root: run, offset: historyOffset, limit: 12, search })), refetchInterval: pollWhileActive })
  const comparisons = useQuery({ queryKey: ["depth-comparisons", run, comparisonOffset], queryFn: () => api<DepthHistory>(query("/depth/comparisons", { run_root: run, offset: comparisonOffset, limit: 12 })), refetchInterval: pollWhileActive })
  const choices = useQuery({ queryKey: ["depth-choices", compareRun, compareOffset], queryFn: () => api<DepthHistory>(query("/depth/evaluations", { run_root: compareRun, offset: compareOffset, limit: 12 })), enabled: tab === "compare", refetchInterval: 5000 })
  const availableGeometry = new Set(setup.data?.dataset.objects.map(o => o.canonical_sha256) ?? [])
  const currentGeometry = geometry ?? [...availableGeometry]
  const missingGeometry = currentGeometry.filter(hash => setup.data && !availableGeometry.has(hash))
  const availableSensors = new Set(setup.data?.dataset.sensors.filter(s => !s.unavailable_reason).map(s => s.sensor_name))
  const currentSensors = sensors?.filter(s => availableSensors.has(s)) ?? [...availableSensors]
  const activeTab = tab ?? (!search && history.data && history.data.total === 0 ? "new" : "results")
  const latest = history.data?.records.find(r => r.report_available)
  const result = selected ?? (latest ? { kind: historyKind, id: latest.id } : null)
  const preparing = history.data?.records.some(record => record.state === "queued" || record.state === "running")
  const resultsHeading = preparing ? "Study in progress" : history.data?.total ? "No completed study in this view" : "Explore the quality of your depth data"
  useEffect(() => { try { localStorage.setItem(`depth-draft.v1:${run}`, JSON.stringify({ label, thresholds, distance, angle, geometry, sensors })) } catch { /* Browser drafts are optional. */ } }, [run, label, thresholds, distance, angle, geometry, sensors])
  const settings = { ...defaults, thresholds_mm: thresholds.split(",").map(v => Number(v.trim())), distance_bin_mm: distance, angle_bin_deg: angle }
  const reasons = [
    ...(setup.data?.dataset.blockers ?? []), ...(missingGeometry.length ? ["Remove unavailable geometry from the draft."] : []), ...(!label.trim() ? ["A finish label is required for this run."] : []),
    ...(setup.data && !setup.data.renderer.available ? [setup.data.renderer.reason ?? "BlenderProc 2.8.0 is unavailable."] : []),
    ...(!currentGeometry.length || !currentSensors.length ? ["Select at least one geometry and sensor."] : []),
    ...(!settings.thresholds_mm.length || settings.thresholds_mm.length > 16 || settings.thresholds_mm.some((t, i, all) => !Number.isFinite(t) || t <= 0 || t > 10000 || i > 0 && t <= all[i - 1]) ? ["Thresholds must be increasing unique positive millimetres, separated by commas (at most 16)."] : []),
    ...(!Number.isFinite(distance) || distance < 1 || distance > 10000 || !Number.isFinite(angle) || angle < 1 || angle > 90 ? ["Distance bins must be 1–10000 mm; angle bins 1–90°."] : []),
  ]
  const queued = (kind: DepthKind, value: { id: string; job_id: string }) => {
    toast.success("Depth job queued", { description: "Processing continues after navigation. Open Jobs for logs and cancellation." })
    void cache.invalidateQueries({ queryKey: ["depth-history"] }); void cache.invalidateQueries({ queryKey: ["depth-comparisons"] })
    setHistoryKind(kind); setHistoryOffset(0); setSearch(""); setSelected(null); setTab("results")
    return value
  }
  const evaluate = useMutation({ mutationFn: () => {
    const value: DepthSubmission = { schema_version: "depth_evaluation_submission.v1", run_root: run, finish_label: label.trim(), geometry_hashes: currentGeometry, sensors: currentSensors, settings }
    return api<{ id: string; job_id: string }>("/depth/evaluations", { method: "POST", body: JSON.stringify(value) })
  }, onSuccess: value => queued("evaluations", value), onError: e => toast.error(errorMessage(e)) })
  const compare = useMutation({ mutationFn: () => {
    const value: DepthComparisonSubmission = { schema_version: "depth_comparison_submission.v1", run_root: run, evaluations: inputs.map(({ run_root, evaluation_id }) => ({ run_root, evaluation_id })) }
    return api<{ id: string; job_id: string }>("/depth/comparisons", { method: "POST", body: JSON.stringify(value) })
  }, onSuccess: value => queued("comparisons", value), onError: e => toast.error(errorMessage(e)) })
  const open = (kind: DepthKind, id: string) => { setSelected({ kind, id }); setHistoryKind(kind); setHistoryOffset(0); setSearch(""); setTab("results") }

  return <div className="space-y-5" data-testid="depth-evaluation-page">
    <style>{`.depth-table th,.depth-table td{padding:10px 12px;vertical-align:top;border-bottom:1px solid var(--color-border)}.depth-table th{white-space:nowrap;background:var(--color-muted);font-size:10px;font-weight:600;color:var(--color-muted-foreground)}.depth-table tr:last-child td{border-bottom:0}.depth-table pre{max-height:180px;overflow:auto}.depth-select{height:36px;min-width:0;max-width:100%;border:1px solid var(--color-border);border-radius:6px;background:var(--color-card);padding:0 10px;font-size:12px}.depth-select:focus-visible{outline:2px solid var(--color-ring);outline-offset:2px}`}</style>
    <PageHeader eyebrow="Inspect / Depth quality" title="Depth Evaluation" description="Review depth accuracy and missing returns, compare surface finishes, and export quality videos." actions={<Button asChild variant="outline" size="sm"><Link to="/workflow/dataset?step=sync"><ArrowLeft className="size-4" />Dataset workflow</Link></Button>} />
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-lg border bg-card px-4 py-3 text-xs" data-testid="depth-run-summary">
      <span className="flex items-center gap-2"><Layers className="size-4 text-muted-foreground" /><strong>{setup.data?.dataset.frame_count.toLocaleString() ?? "…"}</strong><span className="text-muted-foreground">exported frames</span></span><span><strong>{setup.data?.dataset.sensors.length ?? "…"}</strong><span className="ml-1.5 text-muted-foreground">cameras</span></span><span><strong>{setup.data?.dataset.objects.length ?? "…"}</strong><span className="ml-1.5 text-muted-foreground">geometries</span></span>
      <span className={cn("ml-auto flex items-center gap-1.5 text-[11px]", setup.data?.dataset.ready ? "text-success" : "text-muted-foreground")}>{setup.data?.dataset.ready && <CheckCircle2 className="size-3.5" />}{setup.data ? setup.data.dataset.ready ? "Depth inputs verified" : "Depth inputs unavailable" : setup.isError ? "Depth inputs unavailable" : "Checking depth inputs…"}</span>
    </div>
    {setup.isError && <QueryProblem title="Depth inputs unavailable" error={setup.error} retry={() => void setup.refetch()} />}{missingGeometry.map(hash => <div key={hash} className="flex items-center justify-between gap-3 rounded border p-3 text-xs">Unavailable geometry {hash.slice(0, 12)}<Button size="sm" variant="outline" onClick={() => setGeometry(currentGeometry.filter(item => item !== hash))}>Remove from draft</Button></div>)}
    {(evaluate.data || compare.data) && <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-primary/25 bg-primary/5 px-4 py-3 text-xs" role="status"><span>Submitted work continues after navigation.</span>{evaluate.data && <Link className="underline" to={query("/jobs", { search: evaluate.data.job_id })}>View depth job</Link>}{compare.data && <Link className="underline" to={query("/jobs", { search: compare.data.job_id })}>View comparison job</Link>}</div>}
    <Tabs value={activeTab} onValueChange={setTab}>
      <div className="flex flex-wrap items-center justify-between gap-3"><TabsList aria-label="Depth evaluation workspace"><TabsTrigger value="results">Results & videos</TabsTrigger><TabsTrigger value="new"><Plus className="mr-1.5 size-3.5" />New evaluation</TabsTrigger><TabsTrigger value="compare">Compare finishes</TabsTrigger></TabsList><div className="flex gap-4 text-[11px] text-muted-foreground"><Link className="underline underline-offset-2" to="/bop-evaluation">BOP Evaluation</Link><Link className="underline underline-offset-2" to="/jobs">Jobs & cancellation</Link></div></div>
      <TabsContent value="results"><div className="grid items-start gap-4 xl:grid-cols-[260px_minmax(0,1fr)]">
        <Card className="min-w-0"><CardHeader><CardTitle>Saved studies</CardTitle><CardDescription>Select a completed study to review or export.</CardDescription></CardHeader><CardContent className="space-y-3">
          <Label className="sr-only" htmlFor="depth-history-kind">Study type</Label><select id="depth-history-kind" className="depth-select w-full" value={historyKind} onChange={e => { setHistoryKind(e.target.value as DepthKind); setHistoryOffset(0); setSearch(""); setSelected(null) }}><option value="evaluations">Evaluations</option><option value="comparisons">Comparisons</option></select>
          <Label className="sr-only" htmlFor="depth-history-search">Filter retained evaluations</Label><Input id="depth-history-search" placeholder="Filter finish or status…" value={search} onChange={e => { setSearch(e.target.value); setHistoryOffset(0) }} />
          {history.isError && <QueryProblem title="Study history unavailable" error={history.error} retry={() => void history.refetch()} />}{history.isPending && <p role="status" className="text-xs text-muted-foreground">Loading studies…</p>}{history.data?.records.length === 0 && <p className="py-3 text-xs text-muted-foreground">{search ? "No matching studies." : "No studies yet. Create an evaluation to get started."}</p>}
          <div className="space-y-2">{history.data?.records.map(r => <div key={r.id} className={cn("overflow-hidden rounded-lg border", result?.id === r.id && "border-primary/50 bg-primary/5")}>
            <button className="w-full p-3 text-left transition-colors hover:bg-muted/40 disabled:cursor-default disabled:hover:bg-transparent" disabled={!r.report_available} aria-label={`Open retained ${historyKind === "evaluations" ? "evaluation" : "comparison"} ${finish(r)} ${r.id}`} aria-pressed={result?.id === r.id} onClick={() => setSelected({ kind: historyKind, id: r.id })}><div className="flex items-center justify-between gap-2"><strong className="truncate text-xs">{finish(r)}</strong>{r.report_available && <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />}</div><div className="my-2"><RecordState record={r} /></div><p className="text-[10px] text-muted-foreground">{new Date(r.created_at).toLocaleString()}</p><p className="mt-1 truncate font-mono text-[9px] text-muted-foreground" title={r.id}>{r.id}</p></button>
            {r.progress.error && <p role="alert" className="break-words px-3 pb-2 text-[11px] text-warning-foreground">{r.progress.error}</p>}<RecordProgress record={r} />{r.job_id && <Link className="block border-t px-3 py-2 text-[10px] text-muted-foreground underline" to={query("/jobs", { search: r.job_id })}>Job progress & logs</Link>}
          </div>)}</div>
          <div className="flex items-center justify-between gap-2"><Button size="sm" variant="outline" disabled={!historyOffset} aria-label={`Previous ${historyKind}`} onClick={() => setHistoryOffset(Math.max(0, historyOffset - 12))}>Previous</Button><span className="text-[10px] text-muted-foreground">{history.data?.total ?? 0} studies</span><Button size="sm" variant="outline" disabled={!history.data || historyOffset + 12 >= history.data.total} aria-label={`Next ${historyKind}`} onClick={() => setHistoryOffset(historyOffset + 12)}>Next</Button></div>
        </CardContent></Card>
        {result ? <DepthResults key={`${result.kind}:${result.id}`} run={run} kind={result.kind} id={result.id} /> : <Card className="grid min-h-[360px] place-items-center p-8 text-center"><div><FlaskConical className="mx-auto mb-4 size-8 text-muted-foreground" /><h2 className="font-display text-lg font-semibold">{resultsHeading}</h2><p className="mx-auto mt-2 max-w-md text-xs leading-relaxed text-muted-foreground">{preparing ? "Track progress in Jobs. A completed study opens here with metrics, frame inspection and quality videos." : history.data?.total ? "Open a completed study or review the status and error details beside each record." : "Evaluate the finished run to inspect accuracy and missing returns, then create a video for each camera."}</p><Button className="mt-5" variant="outline" size="sm" onClick={() => setTab("new")}>New evaluation</Button></div></Card>}
      </div></TabsContent>
      <TabsContent value="new"><div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(300px,1fr)]">
        <Card><CardHeader><CardTitle>Evaluate this run</CardTitle><CardDescription>Name the surface finish and choose what to evaluate.</CardDescription></CardHeader><CardContent className="space-y-5">
          <div className="space-y-1.5"><Label htmlFor="depth-finish">Finish label (required)</Label><Input id="depth-finish" maxLength={120} value={label} onChange={e => setLabel(e.target.value)} placeholder="e.g. polished, matte, spray painted metal" /><p className="text-[11px] text-muted-foreground">One finish for the whole run. The submitted label is retained with the result.</p></div>
          <fieldset className="space-y-2"><legend className="mb-2 text-xs font-semibold">Frozen geometry <span className="ml-1 font-normal text-muted-foreground">{currentGeometry.length} selected</span></legend>{setup.data?.dataset.objects.map(o => <Label className="flex cursor-pointer items-center gap-3 rounded-lg border p-3 text-xs" key={o.obj_id}><input className="size-4 accent-primary" type="checkbox" checked={currentGeometry.includes(o.canonical_sha256)} onChange={() => setGeometry(toggle(currentGeometry, o.canonical_sha256))} /><span className="min-w-0"><strong className="block">{o.name}</strong><span className="mt-1 block text-[10px] text-muted-foreground">Object {o.obj_id} · <span className="font-mono" title={o.canonical_sha256}>{o.canonical_sha256.slice(0, 12)}</span></span></span></Label>)}</fieldset>
          <fieldset className="space-y-2">
            <legend className="mb-2 text-xs font-semibold">Cameras <span className="ml-1 font-normal text-muted-foreground">{currentSensors.length} selected</span></legend>
            {setup.data?.dataset.sensors.map(s => (
              <Label className={cn("flex items-start gap-3 rounded-lg border px-3 py-2.5 text-xs", s.unavailable_reason ? "cursor-default" : "cursor-pointer")} key={s.scene_id}>
                <input className="size-4 shrink-0 accent-primary" type="checkbox" aria-label={s.sensor_name} aria-describedby={s.unavailable_reason ? `depth-projection-${s.scene_id}` : undefined} disabled={!!s.unavailable_reason} checked={currentSensors.includes(s.sensor_name)} onChange={() => setSensors(toggle(currentSensors, s.sensor_name))} />
                <span className="min-w-0 flex-1">
                  <span className="break-all">{s.sensor_name}</span>
                  {s.unavailable_reason && <span className="mt-1 block text-[11px] leading-relaxed text-warning-foreground" id={`depth-projection-${s.scene_id}`}>{s.unavailable_reason}</span>}
                </span>
                <span className="shrink-0 text-[10px] text-muted-foreground">{s.width} × {s.height}</span>
              </Label>
            ))}
          </fieldset>
          <details className="rounded-lg border"><summary className="cursor-pointer px-3 py-3 text-xs font-semibold">Metric settings <span className="ml-2 font-normal text-muted-foreground">{thresholds} mm · {distance} mm / {angle}° bins</span></summary><div className="space-y-3 border-t p-3"><div className="grid grid-cols-3 gap-3"><div className="space-y-1.5"><Label htmlFor="depth-thresholds">Thresholds (mm)</Label><Input id="depth-thresholds" value={thresholds} onChange={e => setThresholds(e.target.value)} /></div><div className="space-y-1.5"><Label htmlFor="depth-distance-bin">Distance bin (mm)</Label><Input id="depth-distance-bin" type="number" min={1} max={10000} value={Number.isNaN(distance) ? "" : distance} onChange={e => setDistance(e.target.valueAsNumber)} /></div><div className="space-y-1.5"><Label htmlFor="depth-angle-bin">Incidence bin (°)</Label><Input id="depth-angle-bin" type="number" min={1} max={90} value={Number.isNaN(angle) ? "" : angle} onChange={e => setAngle(e.target.valueAsNumber)} /></div></div><p className="text-[11px] text-muted-foreground">Full, 2-pixel-eroded interior and boundary regions are retained. Bins start at zero.</p></div></details>
          <div className="space-y-3 border-t pt-4">{reasons.length > 0 && <div className="space-y-1 text-xs text-muted-foreground" data-testid="depth-submit-reasons">{reasons.map((r, i) => <p key={i}>{r}</p>)}</div>}<Button onClick={() => evaluate.mutate()} disabled={!setup.data?.dataset.ready || reasons.length > 0 || evaluate.isPending}>{evaluate.isPending && <Loader2 className="size-4 animate-spin" />}Queue depth evaluation</Button>{evaluate.error && <p role="alert" className="text-xs">{errorMessage(evaluate.error)}</p>}<p className="text-[11px] leading-relaxed text-muted-foreground">CPU/disk processing continues after navigation. <Link className="underline" to="/jobs">Open Jobs</Link> for progress, logs and cancellation.</p></div>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>Evaluation reference</CardTitle><CardDescription>Compare the captured depth against the run’s calibrated geometry.</CardDescription></CardHeader><CardContent className="space-y-4 text-xs">
          <div className="space-y-2.5 rounded-lg border bg-muted/20 p-3"><p className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Dataset</span><strong>{setup.data?.dataset.ready ? "Inputs verified" : "Checking inputs"}</strong></p><p className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Reference renderer</span><strong>BlenderProc {setup.data?.renderer.version ?? "unavailable"}</strong></p><p className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Depth error</span><strong>Measured − reference (mm)</strong></p></div>
          <div className="flex gap-3 rounded-lg bg-primary/5 p-3"><Film className="mt-0.5 size-5 shrink-0 text-primary-strong" /><p className="leading-relaxed">After evaluation, open <strong>Results & videos → Videos</strong> to preview and download camera quality videos.</p></div><p className="leading-relaxed text-muted-foreground">Labels and settings are browser-local drafts until submission. Each study freezes its geometry, label, settings and reference evidence.</p>
          <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">Reference method & assumptions</summary><div className="mt-3 space-y-3 leading-relaxed text-muted-foreground"><p>All known objects render together with opaque geometry and flat normals. Measured depth and visible masks do not determine eligibility. No depth-based alignment is applied.</p>{setup.data?.assumptions.map(a => <p key={a}>{a}</p>)}</div></details><details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">Calibration and placement quality</summary><pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify(setup.data?.dataset.quality_evidence ?? {}, null, 2)}</pre></details>
        </CardContent></Card>
      </div></TabsContent>
      <TabsContent value="compare"><div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(300px,1fr)]">
        <Card><CardHeader><CardTitle>Compare surface finishes</CardTitle><CardDescription>Add completed studies with shared geometry and identical metric settings. Include the active run.</CardDescription></CardHeader><CardContent className="space-y-4">
          <div className="space-y-1.5"><Label htmlFor="depth-comparison-run">Input run</Label><select id="depth-comparison-run" className="depth-select w-full" value={compareRun} onChange={e => { setCompareRun(e.target.value); setCompareOffset(0) }}>{[...new Set([run, ...runs.map(r => r.path)])].map(r => <option key={r} value={r}>{runs.find(item => item.path === r)?.name ?? r}</option>)}</select></div>
          {choices.isError && <QueryProblem title="Comparison inputs unavailable" error={choices.error} retry={() => void choices.refetch()} />}
          <div className="space-y-2">{choices.data?.records.filter(r => r.report_available).map(r => <div className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2" key={r.id}><div className="min-w-0"><p className="text-xs font-semibold">{finish(r)}</p><p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{r.id}</p></div><Button size="sm" variant="outline" disabled={inputs.length >= 24 || inputs.some(i => i.evaluation_id === r.id && i.run_root === compareRun)} aria-label={`Add ${finish(r)} · ${r.id}`} onClick={() => setInputs([...inputs, { run_root: compareRun, evaluation_id: r.id, label: finish(r) }])}><Plus className="size-3.5" />Add</Button></div>)}</div>
          {choices.data && !choices.data.records.some(r => r.report_available) && <p className="text-xs text-muted-foreground">No completed evaluations on this page. Evaluate this run first or choose another run.</p>}{choices.data && choices.data.total > 12 && <div className="flex gap-2"><Button size="sm" variant="outline" disabled={compareOffset === 0} onClick={() => setCompareOffset(Math.max(0, compareOffset - 12))}>Previous inputs</Button><Button size="sm" variant="outline" disabled={compareOffset + 12 >= choices.data.total} onClick={() => setCompareOffset(compareOffset + 12)}>Next inputs</Button></div>}
          <div className="space-y-2 border-t pt-4"><p className="text-xs font-semibold">Selected studies ({inputs.length}/24)</p>{inputs.map((i, index) => <div className="flex items-center gap-3 rounded-lg border bg-muted/20 p-3 text-xs" key={`${i.run_root}:${i.evaluation_id}`}><div className="min-w-0 flex-1"><strong>{i.label}</strong><p className="mt-1 break-all text-[10px] text-muted-foreground">{i.run_root.split("/").pop()} · {i.evaluation_id}</p></div><Button size="icon" variant="ghost" className="size-7 shrink-0" aria-label={`Remove comparison input ${index}`} onClick={() => setInputs(inputs.filter(item => item !== i))}><X className="size-3.5" /></Button></div>)}<p className="text-[11px] text-muted-foreground">{inputs.length < 2 ? "Choose at least two completed evaluations." : !inputs.some(i => i.run_root === run) ? "Add an evaluation from the active run." : "Geometry, labels and metric settings will be verified on submission."}</p></div>
          <Button disabled={inputs.length < 2 || !inputs.some(i => i.run_root === run) || compare.isPending} onClick={() => compare.mutate()}>Queue depth comparison</Button><p className="text-[11px] text-muted-foreground">Comparison continues after navigation. <Link className="underline" to="/jobs">Open Jobs</Link> for logs and cancellation.</p>{compare.error && <p role="alert" className="text-xs">{errorMessage(compare.error)}</p>}
        </CardContent></Card>
        <Card><CardHeader><CardTitle>Saved comparisons</CardTitle><CardDescription>Common distance/incidence bins, with runs and sensors kept separate.</CardDescription></CardHeader><CardContent className="space-y-3">
          {comparisons.isError && <QueryProblem title="Saved comparisons unavailable" error={comparisons.error} retry={() => void comparisons.refetch()} />}{comparisons.data?.records.length === 0 && <p className="py-4 text-xs text-muted-foreground">Your comparisons will appear here.</p>}
          {comparisons.data?.records.map(r => <div className="space-y-2 rounded-lg border p-3" key={r.id}><div className="flex items-center justify-between gap-2"><span className="font-mono text-[10px]">{r.id}</span><RecordState record={r} /></div><p className="text-[10px] text-muted-foreground">{new Date(r.created_at).toLocaleString()}</p><Button size="sm" variant="outline" disabled={!r.report_available} onClick={() => open("comparisons", r.id)}>Open retained comparison</Button>{r.job_id && <Link className="ml-3 text-[10px] underline" to={query("/jobs", { search: r.job_id })}>Job</Link>}{r.progress.error && <p role="alert" className="text-xs">{r.progress.error}</p>}</div>)}
          {comparisons.data && comparisons.data.total > 12 && <div className="flex items-center gap-2"><Button size="sm" variant="outline" disabled={!comparisonOffset} onClick={() => setComparisonOffset(Math.max(0, comparisonOffset - 12))}>Previous comparisons</Button><Button size="sm" variant="outline" disabled={comparisonOffset + 12 >= comparisons.data.total} onClick={() => setComparisonOffset(comparisonOffset + 12)}>Next comparisons</Button></div>}
        </CardContent></Card>
      </div></TabsContent>
    </Tabs>
  </div>
}

/** Listing re-hashes every study on the server, so poll only while one is still running. */
function pollWhileActive(query: { state: { data?: DepthHistory } }) {
  return query.state.data?.records.some((record) => !record.report_available && !["failed", "canceled", "invalid", "abandoned"].includes(record.state)) ? 2000 : false
}

export function DepthEvaluationPage() {
  const { selectedRun } = useOperator()
  return <DepthRunPage key={selectedRun} run={selectedRun} />
}
