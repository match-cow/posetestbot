import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Download, Film, Images, Info } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, errorMessage, query } from "@/lib/api"
import type { DepthComparisonReport, DepthFrame, DepthKind, DepthMetrics, DepthReport, DepthRow, DepthSettings, DepthVideoChoices, DepthVideoSeries } from "./contracts"
import { DepthVideos } from "./depth-videos"

const pct = (n: number | null) => n == null ? "Unavailable" : `${(100 * n).toFixed(1)}%`
const mm = (n: number | null) => n == null ? "Unavailable" : n.toFixed(3)
const base = (kind: DepthKind, id: string, run: string, suffix = "") => query(`/depth/${kind}/${id}${suffix}`, { run_root: run })
const viewNames = { error: "Signed error", measured: "Measured depth", reference: "Reference depth", missing: "Missing returns" }

function MetricsCells({ row }: { row: DepthMetrics }) {
  return <><td>{row.reference_pixels.toLocaleString()}</td><td>{row.valid_pixels.toLocaleString()}</td><td>{row.missing_pixels.toLocaleString()}</td><td>{pct(row.completeness)}</td><td>{mm(row.bias_mm)}</td><td>{mm(row.mae_mm)}</td><td>{mm(row.rmse_mm)}</td><td>{mm(row.median_absolute_error_mm)}</td><td>{mm(row.p95_absolute_error_mm)}</td><td>{row.thresholds.map(t => <div className="whitespace-nowrap" key={t.threshold_mm}>{t.threshold_mm} mm: {pct(t.outlier_rate)} / {pct(t.usable_coverage)}</div>)}</td></>
}
function MetricsHead() {
  return <><th>Reference px</th><th>Valid px</th><th>Missing px</th><th>Completeness</th><th>Bias mm</th><th>MAE mm</th><th>RMSE mm</th><th>Median abs mm</th><th>P95 abs mm</th><th>Threshold: outliers / usable coverage</th></>
}
function MetricsTable({ rows, settings }: { rows: DepthRow[]; settings: DepthSettings }) {
  const [filter, setFilter] = useState("")
  const [count, setCount] = useState(40)
  const filtered = rows.filter(r => JSON.stringify(r).toLowerCase().includes(filter.toLowerCase()))
  return <div className="space-y-3">
    <div className="max-w-sm space-y-1.5"><Label htmlFor="depth-metric-filter">Filter metric rows</Label><Input id="depth-metric-filter" placeholder="Sensor, instance, geometry or bin…" value={filter} onChange={e => { setFilter(e.target.value); setCount(40) }} /></div>
    <div className="overflow-x-auto rounded-lg border"><table className="depth-table w-full text-left text-xs"><thead><tr><th>Run input / finish</th><th>Sensor / instance</th><th>Geometry</th><th>Distance / incidence</th><th>Contributing / observed frames</th><MetricsHead /></tr></thead><tbody>{filtered.slice(0, count).map((row, index) => <tr key={index}>
      <td>{row.input_id ?? "Active run"}<br />{row.finish_label}</td><td>{row.sensor_name || "All selected sensors"}<div className="font-mono text-[10px]">{row.instance_uuid}</div></td><td title={row.canonical_sha256} className="font-mono">{row.canonical_sha256.slice(0, 12)}</td><td>{row.scope === "bin" ? `${row.distance_bin * settings.distance_bin_mm}–${(row.distance_bin + 1) * settings.distance_bin_mm} mm / ${row.angle_bin * settings.angle_bin_deg}–${Math.min(90, (row.angle_bin + 1) * settings.angle_bin_deg)}°` : row.scope}</td><td>{row.contributing_frame_count} / {row.frame_count}</td><MetricsCells row={row} />
    </tr>)}</tbody></table></div>
    <p className="text-xs text-muted-foreground">{filtered.length} rows · Exact pixel-weighted metrics. Outlier rates use valid returns; usable coverage uses all reference pixels. Accuracy is unavailable without valid returns.</p>
    {count < filtered.length && <Button variant="outline" size="sm" onClick={() => setCount(count + 40)}>Show 40 more metric rows</Button>}
  </div>
}

function ComparisonCoverage({ data, region }: { data: DepthComparisonReport; region: string }) {
  return <div data-testid="depth-common-coverage" className="space-y-3">
    <div><h3 className="text-sm font-semibold">Common-bin coverage</h3><p className="mt-1 text-xs text-muted-foreground">Compare finishes within common distance/incidence bins. Runs and sensors remain separate; there is no pooled finish ranking.</p></div>
    <div className="overflow-x-auto rounded-lg border"><table className="depth-table w-full text-left text-xs"><thead><tr><th>Finish / sensor</th><th>Geometry</th><th>Common bins</th><th>Common reference px</th><th>Unmatched reference px</th><th>Coverage</th></tr></thead><tbody>{data.coverage.filter(c => c.region === region).map((c, i) => <tr key={i}>
      <td><strong>{c.finish_label}</strong><div>{c.sensor_name}</div><span className="text-muted-foreground">Input {c.input_id}</span></td><td className="font-mono" title={c.canonical_sha256}>{c.canonical_sha256.slice(0, 12)}</td><td>{c.common_bin_count}</td><td>{c.common_reference_pixels.toLocaleString()}</td><td>{c.unmatched_reference_pixels.toLocaleString()}</td><td>{c.reason ?? "Common support available"}{c.unmatched_bins.length > 0 && <details><summary>Unmatched bins ({c.unmatched_bins.length})</summary><pre>{JSON.stringify(c.unmatched_bins, null, 2)}</pre></details>}</td>
    </tr>)}</tbody></table></div>
  </div>
}

function FrameInspection({ run, kind, id, data, region, series }: { run: string; kind: DepthKind; id: string; data: DepthReport; region: string; series: DepthVideoSeries[] }) {
  const [offset, setOffset] = useState(0)
  const [frameKey, setFrameKey] = useState("")
  const [seriesIndex, setSeriesIndex] = useState(0)
  const [view, setView] = useState<keyof typeof viewNames>("error")
  const camera = series[seriesIndex]
  const frames = useQuery({
    queryKey: ["depth-frames", run, kind, id, offset, camera?.scene_id, camera?.input_id],
    queryFn: () => api<{ frames: DepthFrame[]; total: number }>(query(`/depth/${kind}/${id}/frames`, { run_root: run, offset, limit: 12, scene_id: camera?.scene_id, input_id: camera?.input_id })),
  })
  const frame = frames.data?.frames.find(f => f.key === frameKey) ?? frames.data?.frames[0]
  return <section data-testid="depth-frame-inspection" className="space-y-4">
    <div className="grid grid-cols-2 gap-3">
      <div className="min-w-0 space-y-1.5"><Label htmlFor="depth-frame-camera">Camera / finish</Label><select id="depth-frame-camera" className="depth-select w-full" value={seriesIndex} onChange={e => { setSeriesIndex(Number(e.target.value)); setOffset(0); setFrameKey("") }}>{series.map((s, i) => <option key={`${s.input_id}:${s.scene_id}`} value={i}>{s.sensor_name} · {s.finish_label}</option>)}</select></div>
      <div className="min-w-0 space-y-1.5"><Label htmlFor="depth-frame">Retained frame</Label><select id="depth-frame" className="depth-select w-full" value={frame?.key ?? ""} onChange={e => setFrameKey(e.target.value)}>{frames.data?.frames.map(f => <option key={f.key} value={f.key}>Frame {f.im_id} · {f.key}</option>)}</select></div>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2"><Button variant="outline" size="sm" disabled={offset === 0} onClick={() => { setOffset(Math.max(0, offset - 12)); setFrameKey("") }}>Previous frames</Button><Button variant="outline" size="sm" disabled={!frames.data || offset + 12 >= frames.data.total} onClick={() => { setOffset(offset + 12); setFrameKey("") }}>Next frames</Button><span className="text-xs text-muted-foreground">{frames.data?.total.toLocaleString() ?? 0} frames</span></div>
      <Label className="flex items-center gap-2">Depth image view<select className="depth-select" value={view} onChange={e => setView(e.target.value as typeof view)}>{Object.entries(viewNames).map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select></Label>
    </div>
    {frames.error && <p role="alert" className="text-xs">{errorMessage(frames.error)}</p>}
    {frames.isPending && <p role="status" className="text-xs text-muted-foreground">Loading retained frame…</p>}
    {frame && <>
      <div className="grid grid-cols-2 gap-3">{([['rgb', 'RGB'], [view, viewNames[view]]] as const).map(([media, label]) => <figure className="min-w-0 overflow-hidden rounded-lg border bg-[#1f2937]" key={media}><img className="w-full object-contain [image-rendering:pixelated]" alt={label} src={base(kind, id, run, `/frames/${frame.key}/media/${media}`)} /><figcaption className="bg-card px-3 py-2 text-xs font-semibold">{label}</figcaption></figure>)}</div>
      <div className="space-y-2 text-xs text-muted-foreground"><p>Depth 0–{data.scales.depth_max_mm} mm · Error −{data.scales.error_max_mm} to +{data.scales.error_max_mm} mm (blue → red). Positive error is farther than reference.</p><div className="flex flex-wrap gap-3"><span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-[#e137be]" />Magenta: missing depth</span><span className="flex items-center gap-1.5"><i className="size-2.5 rounded-sm bg-[#1f2937]" />Dark grey: outside object</span></div></div>
      {frame.missing_depth_map && <p className="rounded border border-warning/40 p-3 text-xs">Entire measured depth map is missing: zero completeness; accuracy is unavailable.</p>}
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs"><span className="text-muted-foreground">{frame.sensor_name} · {frame.width} × {frame.height} · {frame.key}</span><a className="underline" href={base(kind, id, run, `/frames/${frame.key}/media/numeric`)}>Download frame numeric maps (NPZ)</a></div>
      <details className="rounded-lg border p-3"><summary className="cursor-pointer text-xs font-semibold">Per-instance frame metrics</summary><div className="mt-3 overflow-x-auto"><table className="depth-table w-full text-left text-xs"><thead><tr><th>Instance</th><MetricsHead /></tr></thead><tbody>{frame.instances.map(i => <tr key={i.instance_uuid}><td className="font-mono">{i.instance_uuid}<div>{i.regions[region]?.status === "unavailable" ? "No rendered visible pixels" : ""}</div></td><MetricsCells row={i.regions[region]} /></tr>)}</tbody></table></div></details>
    </>}
  </section>
}

export function DepthResults({ run, kind, id }: { run: string; kind: DepthKind; id: string }) {
  const [tab, setTab] = useState("overview")
  const [region, setRegion] = useState("interior")
  const [scope, setScope] = useState("sensor_geometry")
  const [videoOffset, setVideoOffset] = useState(0)
  const report = useQuery({ queryKey: ["depth-report", run, kind, id], queryFn: () => api<DepthReport>(base(kind, id, run, "/report")) })
  const videos = useQuery({
    queryKey: ["depth-video-choices", run, kind, id, videoOffset],
    queryFn: () => api<DepthVideoChoices>(query(`/depth/${kind}/${id}/videos`, { run_root: run, offset: videoOffset, limit: 12 })),
    refetchInterval: q => q.state.data?.records.some(r => !r.video_available && !["failed", "canceled", "invalid", "abandoned"].includes(r.state)) ? 2000 : false,
  })
  const data = report.data
  if (report.error) return <p role="alert" className="p-4 text-sm">{errorMessage(report.error)}</p>
  if (!data) return <p role="status" className="p-4 text-sm text-muted-foreground">Loading frozen depth evidence…</p>
  const rows = (data.kind === "evaluation" ? data.aggregates.filter(r => r.scope === scope) : data.bins).filter(r => r.region === region)
  const overall = data.kind === "evaluation" ? data.aggregates.find(r => r.scope === "run" && r.region === region) : undefined
  return <Card className="min-w-0" data-testid="depth-results">
    <CardHeader className="gap-3 border-b">
      <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><p className="mb-1 text-[10px] font-bold uppercase tracking-wider text-primary-strong">Retained {data.kind}</p><h2 className="font-display text-lg font-semibold">{data.kind === "evaluation" ? data.finish_label : `${data.series.length} separate run/sensor series`}</h2><p className="mt-1 text-xs text-muted-foreground">{data.frame_count.toLocaleString()} frames · <span className="font-mono text-[10px]" title={id}>{id}</span></p></div>
        <Button asChild variant="outline" size="sm"><a href={base(kind, id, run, "/download")}><Download className="size-4" />Download research bundle</a></Button>
      </div>
      <Label className="flex items-center gap-2 text-xs">Region<select aria-label="Depth metric region" className="depth-select" value={region} onChange={e => setRegion(e.target.value)}><option value="interior">2-pixel-eroded interior</option><option value="full">Full reference region</option><option value="boundary">Boundary only</option></select></Label>
    </CardHeader>
    <CardContent className="pt-4">
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-1 h-auto max-w-full flex-wrap justify-start" aria-label="Depth result views"><TabsTrigger value="overview">Overview</TabsTrigger><TabsTrigger value="frames"><Images className="mr-1.5 size-3.5" />Frames</TabsTrigger><TabsTrigger value="metrics">Metrics</TabsTrigger><TabsTrigger value="videos"><Film className="mr-1.5 size-3.5" />Videos</TabsTrigger><TabsTrigger value="evidence">Evidence</TabsTrigger></TabsList>
        <TabsContent value="overview" className="space-y-5">
          {overall && <div className="grid grid-cols-2 gap-3 2xl:grid-cols-4" data-testid="depth-summary">{[
            ["Depth completeness", pct(overall.completeness), "Valid returns / object pixels"],
            ["Mean absolute error", `${mm(overall.mae_mm)}${overall.mae_mm == null ? "" : " mm"}`, "On valid measured returns"],
            ["RMSE", `${mm(overall.rmse_mm)}${overall.rmse_mm == null ? "" : " mm"}`, "On valid measured returns"],
            ["Missing returns", overall.missing_pixels.toLocaleString(), "Reference pixels without depth"],
          ].map(([title, value, note]) => <div key={title} className="min-w-0 rounded-lg border bg-muted/20 p-3"><p className="text-[11px] text-muted-foreground">{title}</p><p className="my-1.5 font-display text-xl font-semibold tabular-nums">{value}</p><p className="text-[10px] text-muted-foreground">{note}</p></div>)}</div>}
          {data.kind === "comparison" ? <ComparisonCoverage data={data} region={region} /> : <div className="space-y-3"><h3 className="text-sm font-semibold">Quality by camera and geometry</h3><div className="overflow-x-auto rounded-lg border"><table className="depth-table w-full text-left text-xs"><thead><tr><th>Camera / geometry</th><th>Completeness</th><th>MAE mm</th><th>RMSE mm</th><th>P95 abs mm</th></tr></thead><tbody>{data.aggregates.filter(r => r.scope === "sensor_geometry" && r.region === region).map((r, i) => <tr key={i}><td className="break-all"><strong>{r.sensor_name}</strong><div className="mt-1 font-mono text-[10px] text-muted-foreground" title={r.canonical_sha256}>{r.canonical_sha256.slice(0, 12)}</div></td><td>{pct(r.completeness)}</td><td>{mm(r.mae_mm)}</td><td>{mm(r.rmse_mm)}</td><td>{mm(r.p95_absolute_error_mm)}</td></tr>)}</tbody></table></div></div>}
          <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" onClick={() => setTab("frames")}><Images className="size-4" />Inspect frames</Button><Button size="sm" onClick={() => setTab("videos")}><Film className="size-4" />Create quality video</Button></div>
          <p className="flex items-start gap-2 text-xs text-muted-foreground"><Info className="mt-0.5 size-3.5 shrink-0" />Metrics use the selected reference region and frozen calibrated poses. Pose uncertainty contributes to depth error.</p>
        </TabsContent>
        <TabsContent value="frames"><FrameInspection run={run} kind={kind} id={id} data={data} region={region} series={videos.data?.series ?? []} /></TabsContent>
        <TabsContent value="metrics" className="space-y-4">
          {data.kind === "evaluation" && <Label className="flex items-center gap-2">Breakdown<select aria-label="Depth metric breakdown" className="depth-select" value={scope} onChange={e => setScope(e.target.value)}><option value="sensor_geometry">Sensor and geometry</option><option value="instance">Individual instance</option><option value="sensor">Sensor</option><option value="run">Run</option><option value="bin">Distance and incidence bins</option></select></Label>}
          <p className="text-xs text-muted-foreground">Thresholds {data.settings.thresholds_mm.join(" / ")} mm · Bins {data.settings.distance_bin_mm} mm / {data.settings.angle_bin_deg}°, anchored at zero.</p>
          <MetricsTable rows={rows} settings={data.settings} />
          <a className="inline-block text-xs underline" href={base(kind, id, run, "/report")}>Metrics JSON</a>
        </TabsContent>
        <TabsContent value="videos"><DepthVideos run={run} kind={kind} id={id} data={videos.data} error={videos.error} offset={videoOffset} setOffset={setVideoOffset} onRetry={() => { void videos.refetch() }} retrying={videos.isFetching} /></TabsContent>
        <TabsContent value="evidence" className="space-y-4 text-xs">
          <div className="rounded-lg border bg-muted/20 p-4"><h3 className="font-semibold">Frozen evaluation evidence</h3><p className="mt-2 text-muted-foreground">Labels, geometry, settings, calibration and source hashes belong to this submitted result. Browser draft edits do not change it.</p><p className="mt-2">Metric revision: <span className="font-mono">{data.metric_revision}</span></p></div>
          <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">Calibration and placement quality evidence</summary><p className="mt-2 text-muted-foreground">Quality warnings are retained evidence. The evaluation does not refine the reference.</p><pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify(data.quality_evidence, null, 2)}</pre></details>
          <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">Settings and implementation hashes</summary><pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify({ settings: data.settings, geometry_hashes: data.geometry_hashes, scales: data.scales, implementation_sha256: data.implementation_sha256 }, null, 2)}</pre></details>
          <ul className="list-disc space-y-2 pl-4 text-muted-foreground">{data.assumptions.map(a => <li key={a}>{a}</li>)}</ul>
          <p className="text-muted-foreground">The research bundle includes offline HTML, CSV/JSON metrics, full-precision maps, PNG/SVG figures, settings and provenance.</p>
        </TabsContent>
      </Tabs>
    </CardContent>
  </Card>
}
