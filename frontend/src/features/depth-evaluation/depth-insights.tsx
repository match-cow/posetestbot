import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { ArrowDownUp } from "lucide-react"
import { Label } from "@/components/ui/label"
import { api, errorMessage, query } from "@/lib/api"
import type { DepthComparisonReport, DepthFrameSeries, DepthFrameSeriesRow, DepthKind, DepthRow, DepthSettings } from "./contracts"
import { BarChart, LineChart, type ChartSeries } from "./depth-charts"

const pct = (n: number | null) => n == null ? "Unavailable" : `${(100 * n).toFixed(1)}%`
const mm = (n: number | null) => n == null ? "Unavailable" : n.toFixed(3)
const short = (hash: string) => hash ? hash.slice(0, 8) : "all geometry"

export const ERROR_METRICS = {
  rmse_mm: "RMSE",
  mae_mm: "Mean absolute error",
  bias_mm: "Bias (signed)",
  median_absolute_error_mm: "Median absolute error",
  p95_absolute_error_mm: "P95 absolute error",
} as const
type ErrorMetric = keyof typeof ERROR_METRICS

/** Stable colour slot per comparison input; colour follows the entity, not rank. */
const inputSlot = (row: { input_id?: string | null }) => Number(row.input_id ?? 0)

type SortKey = "label" | "completeness" | "valid_pixels" | ErrorMetric
const SUMMARY_COLUMNS: { key: SortKey; label: string }[] = [
  { key: "completeness", label: "Completeness" },
  { key: "valid_pixels", label: "Valid px" },
  { key: "bias_mm", label: "Bias mm" },
  { key: "mae_mm", label: "MAE mm" },
  { key: "rmse_mm", label: "RMSE mm" },
  { key: "median_absolute_error_mm", label: "Median abs mm" },
  { key: "p95_absolute_error_mm", label: "P95 abs mm" },
]

function rowLabel(row: DepthRow) {
  return `${row.finish_label ?? `Input ${row.input_id}`} · ${row.sensor_name || "all cameras"}`
}

/** Pixel-weighted merge of incidence bins into one row per distance bin (exact for mean/RMSE/completeness). */
function byDistance(rows: DepthRow[]) {
  const groups = new Map<number, { reference: number; valid: number; abs: number; sq: number }>()
  for (const row of rows) {
    const g = groups.get(row.distance_bin) ?? { reference: 0, valid: 0, abs: 0, sq: 0 }
    g.reference += row.reference_pixels
    g.valid += row.valid_pixels
    if (row.valid_pixels && row.mae_mm != null && row.rmse_mm != null) {
      g.abs += row.mae_mm * row.valid_pixels
      g.sq += row.rmse_mm ** 2 * row.valid_pixels
    }
    groups.set(row.distance_bin, g)
  }
  return [...groups.entries()].sort(([a], [b]) => a - b).map(([bin, g]) => ({
    bin,
    completeness: g.reference ? g.valid / g.reference : null,
    mae: g.valid ? g.abs / g.valid : null,
    rmse: g.valid ? Math.sqrt(g.sq / g.valid) : null,
  }))
}

export function ComparisonSummary({ data, region }: { data: DepthComparisonReport; region: string }) {
  const [scope, setScope] = useState("sensor_geometry")
  const [sort, setSort] = useState<{ key: SortKey; descending: boolean }>({ key: "label", descending: false })
  if (!data.summary?.length) return <p className="rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs" data-testid="depth-comparison-legacy">This comparison was published before per-input summaries existed. Queue the comparison again to see every input's errors and completeness; the common-bin coverage below is unchanged.</p>
  const rows = data.summary.filter(r => r.scope === scope && r.region === region)
  const sorted = [...rows].sort((a, b) => {
    const value = (r: DepthRow) => sort.key === "label" ? rowLabel(r) : r[sort.key]
    const [x, y] = [value(a), value(b)]
    if (x == null) return 1
    if (y == null) return -1
    const order = typeof x === "string" ? x.localeCompare(String(y)) : Number(x) - Number(y)
    return sort.descending ? -order : order
  })
  const header = (key: SortKey, label: string) => <th key={key} aria-sort={sort.key === key ? (sort.descending ? "descending" : "ascending") : "none"}><button type="button" className="inline-flex items-center gap-1 font-semibold" onClick={() => setSort({ key, descending: sort.key === key ? !sort.descending : false })}>{label}<ArrowDownUp className="size-3 opacity-60" /></button></th>
  const sensorRows = data.summary.filter(r => r.scope === "sensor" && r.region === region)
  const bars = (metric: "completeness" | ErrorMetric) => sensorRows.map(r => ({ id: `${r.input_id}:${r.sensor_name}`, label: rowLabel(r), slot: inputSlot(r), value: r[metric] }))
  const distanceSeries = (metric: "completeness" | "mae"): ChartSeries[] => {
    const keys = [...new Set((data.series_bins ?? []).filter(r => r.region === region).map(r => `${r.input_id}|${r.sensor_name}`))]
    return keys.map(key => {
      const [input, sensor] = key.split("|")
      const rows = (data.series_bins ?? []).filter(r => r.region === region && r.input_id === input && r.sensor_name === sensor)
      return {
        id: key,
        label: rowLabel(rows[0]),
        slot: Number(input),
        points: byDistance(rows).flatMap(p => (metric === "mae" ? p.mae : p.completeness) == null ? [] : [{ x: p.bin * data.settings.distance_bin_mm, y: (metric === "mae" ? p.mae : p.completeness) as number, key: String(p.bin) }]),
      }
    })
  }
  return <section className="space-y-5" data-testid="depth-comparison-summary">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><h3 className="text-sm font-semibold">Errors and completeness per input</h3><p className="mt-1 text-xs text-muted-foreground">Each input's own exact metrics. Completeness = valid returns / reference pixels; errors use valid returns only.</p></div>
      <Label className="flex items-center gap-2 text-xs">Breakdown<select aria-label="Comparison summary breakdown" className="depth-select" value={scope} onChange={e => setScope(e.target.value)}><option value="sensor_geometry">Camera and geometry</option><option value="sensor">Camera</option><option value="instance">Individual instance</option><option value="run">Whole input</option></select></Label>
    </div>
    <div className="overflow-x-auto rounded-lg border"><table className="depth-table w-full text-left text-xs" data-testid="depth-comparison-summary-table"><thead><tr>{header("label", "Finish / camera")}<th>Geometry / instance</th><th>Frames</th><th>Reference px</th><th>Missing px</th>{SUMMARY_COLUMNS.map(c => header(c.key, c.label))}</tr></thead><tbody>{sorted.map((r, i) => <tr key={i}>
      <td><span aria-hidden className="mr-1.5 inline-block size-2 rounded-full" style={{ background: `var(--series-${(inputSlot(r) % 8) + 1})` }} /><strong>{r.finish_label}</strong><div>{r.sensor_name || "All cameras"}</div><span className="text-muted-foreground">Input {r.input_id}</span></td>
      <td className="font-mono" title={r.canonical_sha256}>{short(r.canonical_sha256)}{r.instance_uuid && <div className="text-[10px] text-muted-foreground">{r.instance_uuid.slice(0, 8)}</div>}</td>
      <td>{r.contributing_frame_count} / {r.frame_count}</td><td>{r.reference_pixels.toLocaleString()}</td><td>{r.missing_pixels.toLocaleString()}</td>
      <td>{pct(r.completeness)}</td><td>{r.valid_pixels.toLocaleString()}</td><td>{mm(r.bias_mm)}</td><td>{mm(r.mae_mm)}</td><td>{mm(r.rmse_mm)}</td><td>{mm(r.median_absolute_error_mm)}</td><td>{mm(r.p95_absolute_error_mm)}</td>
    </tr>)}</tbody></table></div>
    <div className="grid gap-5 xl:grid-cols-2">
      <BarChart title="Completeness by input and camera" rows={bars("completeness")} format={v => `${(100 * v).toFixed(1)}%`} testId="depth-comparison-completeness-chart" />
      <BarChart title="RMSE by input and camera (mm)" rows={bars("rmse_mm")} format={v => `${v.toFixed(2)} mm`} testId="depth-comparison-rmse-chart" />
    </div>
    {(data.series_bins?.length ?? 0) > 0 && <div className="grid gap-5 xl:grid-cols-2">
      <LineChart title="Mean absolute error by reference distance" series={distanceSeries("mae")} xLabel="Reference distance bin start (mm)" yLabel="MAE (mm)" formatX={v => `${v}`} formatY={v => v.toFixed(1)} testId="depth-comparison-distance-mae" />
      <LineChart title="Completeness by reference distance" series={distanceSeries("completeness")} xLabel="Reference distance bin start (mm)" yLabel="Completeness" formatX={v => `${v}`} formatY={v => `${Math.round(v * 100)}%`} testId="depth-comparison-distance-completeness" />
    </div>}
  </section>
}

type XAxis = "frame" | "time" | "path_mm" | "path_fraction"
const X_AXES: Record<XAxis, { label: string; format: (v: number) => string; value: (r: DepthFrameSeriesRow) => number | null }> = {
  path_fraction: { label: "Robot path progress (%)", format: v => `${Math.round(v * 100)}`, value: r => r.path_fraction },
  path_mm: { label: "Robot path travelled (mm)", format: v => `${Math.round(v)}`, value: r => r.path_mm },
  time: { label: "Time since motion start (s)", format: v => v.toFixed(1), value: r => r.t_motion_s },
  frame: { label: "Frame (image ID)", format: v => `${Math.round(v)}`, value: r => r.im_id },
}

/** Per-frame errors and completeness along the recording / robot motion. */
export function MotionCharts({ run, kind, id, region, settings, onOpenFrame }: {
  run: string; kind: DepthKind; id: string; region: string; settings: DepthSettings
  onOpenFrame: (row: DepthFrameSeriesRow, all: DepthFrameSeriesRow[]) => void
}) {
  const series = useQuery({ queryKey: ["depth-frame-series", run, kind, id], queryFn: () => api<DepthFrameSeries>(query(`/depth/${kind}/${id}/frame-series`, { run_root: run })) })
  const [metric, setMetric] = useState<ErrorMetric>("rmse_mm")
  const [axis, setAxis] = useState<XAxis | null>(null)
  const [level, setLevel] = useState<"frame" | "frame_geometry">("frame")
  const [sensor, setSensor] = useState("")
  if (series.error) return <p role="alert" className="text-xs">{errorMessage(series.error)}</p>
  if (!series.data) return <p role="status" className="text-xs text-muted-foreground">Loading per-frame series…</p>
  if (!series.data.available) return <p className="rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs" data-testid="depth-motion-unavailable">{series.data.reason}</p>
  const motion = Boolean(series.data.motion_available)
  const xAxis: XAxis = axis ?? (motion ? "path_fraction" : "frame")
  const sensors = [...new Set(series.data.rows.map(r => r.sensor_name))].sort()
  const rows = series.data.rows.filter(r => r.level === level && r.region === region && (!sensor || r.sensor_name === sensor))
  const keyOf = (r: DepthFrameSeriesRow) => [r.input_id ?? "", r.sensor_name, level === "frame_geometry" ? r.canonical_sha256 : ""].join("|")
  const keys = [...new Set(rows.map(keyOf))].sort()
  const build = (value: (r: DepthFrameSeriesRow) => number | null): ChartSeries[] => keys.map((key, index) => {
    const members = rows.filter(r => keyOf(r) === key)
    const first = members[0]
    return {
      id: key,
      label: [first.finish_label ?? (first.input_id != null ? `Input ${first.input_id}` : null), first.sensor_name, level === "frame_geometry" ? short(first.canonical_sha256) : null].filter(Boolean).join(" · "),
      // Comparisons keep each input's colour; evaluations use one slot per camera/geometry.
      slot: kind === "comparisons" ? Number(first.input_id ?? 0) : index,
      points: members.flatMap(r => {
        const x = X_AXES[xAxis].value(r)
        const y = value(r)
        return x == null || y == null ? [] : [{ x, y, key: r.frame_key, label: `Frame ${r.im_id} · ${r.frame_key} — click to open` }]
      }),
    }
  })
  const select = (key: string) => {
    const row = rows.find(r => r.frame_key === key)
    if (row && series.data) onOpenFrame(row, series.data.rows)
  }
  return <section className="space-y-4" data-testid="depth-motion-charts">
    <div className="flex flex-wrap items-end gap-3">
      <Label className="flex items-center gap-2 text-xs">Error metric<select aria-label="Per-frame error metric" className="depth-select" value={metric} onChange={e => setMetric(e.target.value as ErrorMetric)}>{Object.entries(ERROR_METRICS).map(([k, label]) => <option key={k} value={k}>{label}</option>)}</select></Label>
      <Label className="flex items-center gap-2 text-xs">X axis<select aria-label="Per-frame x axis" className="depth-select" value={xAxis} onChange={e => setAxis(e.target.value as XAxis)}>{(Object.keys(X_AXES) as XAxis[]).map(k => <option key={k} value={k} disabled={!motion && k !== "frame"}>{X_AXES[k].label}</option>)}</select></Label>
      <Label className="flex items-center gap-2 text-xs">Lines<select aria-label="Per-frame grouping" className="depth-select" value={level} onChange={e => setLevel(e.target.value as typeof level)}><option value="frame">All selected objects per frame</option><option value="frame_geometry">One line per geometry</option></select></Label>
      <Label className="flex items-center gap-2 text-xs">Camera<select aria-label="Per-frame camera" className="depth-select" value={sensor} onChange={e => setSensor(e.target.value)}><option value="">All cameras</option>{sensors.map(s => <option key={s} value={s}>{s}</option>)}</select></Label>
    </div>
    {!motion && <p className="text-[11px] text-muted-foreground" data-testid="depth-motion-fallback">Matched robot poses are not bound to this export, so values are plotted by frame (image ID) instead of robot path progress.</p>}
    {(series.data.inputs_without_series?.length ?? 0) > 0 && <p className="text-[11px] text-warning-foreground">Inputs {series.data.inputs_without_series?.join(", ")} were evaluated before per-frame series existed and are not plotted. Re-run those evaluations to include them.</p>}
    <LineChart title={`${ERROR_METRICS[metric]} per frame (mm)`} series={build(r => r[metric])} xLabel={X_AXES[xAxis].label} yLabel={`${ERROR_METRICS[metric]} (mm)`} formatX={X_AXES[xAxis].format} formatY={v => v.toFixed(1)} onSelect={p => select(p.key)} testId="depth-motion-error-chart" />
    <LineChart title="Completeness per frame (valid / reference pixels)" series={build(r => r.completeness)} xLabel={X_AXES[xAxis].label} yLabel="Completeness" formatX={X_AXES[xAxis].format} formatY={v => `${Math.round(v * 100)}%`} onSelect={p => select(p.key)} testId="depth-motion-completeness-chart" />
    <p className="text-[11px] text-muted-foreground">Exact per-frame metrics in the {region} region; thresholds {settings.thresholds_mm.join(" / ")} mm. Hover or focus a chart and use the arrow keys to read values; click or press Enter to open that frame. The depth quality CSV contains every value shown here.</p>
  </section>
}
