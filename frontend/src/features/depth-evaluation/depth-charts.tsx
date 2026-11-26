import { useState, type KeyboardEvent } from "react"

/**
 * Small dependency-free SVG charts for depth quality.
 *
 * Colour follows the entity (a stable series slot), never its rank; at most
 * eight categorical slots are drawn. Every chart has a legend for two or more
 * series, a hover/keyboard tooltip, and sits next to a table with the same
 * values, so identity and values never depend on colour alone.
 */
export const MAX_SERIES = 8
export const seriesColor = (slot: number) => `var(--series-${(slot % MAX_SERIES) + 1})`

export interface ChartPoint { x: number; y: number; key: string; label?: string }
export interface ChartSeries { id: string; label: string; slot: number; points: ChartPoint[] }

const WIDTH = 760
const HEIGHT = 240
const PAD = { top: 12, right: 16, bottom: 34, left: 56 }

function niceTicks(min: number, max: number, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0]
  if (min === max) { const pad = Math.abs(min) * 0.1 || 1; min -= pad; max += pad }
  const raw = (max - min) / count
  const power = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map(m => m * power).find(s => s >= raw) ?? raw
  const ticks: number[] = []
  for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)))
  if (ticks[ticks.length - 1] < max) ticks.push(Number((ticks[ticks.length - 1] + step).toPrecision(12)))
  return ticks
}

function Legend({ series }: { series: ChartSeries[] }) {
  if (series.length < 2) return null
  return <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-foreground" aria-label="Chart legend">
    {series.map(s => <li key={s.id} className="flex min-w-0 items-center gap-1.5"><span aria-hidden className="h-0.5 w-4 shrink-0 rounded" style={{ background: seriesColor(s.slot) }} /><span className="truncate">{s.label}</span></li>)}
  </ul>
}

/** Multi-series line chart with a crosshair tooltip (mouse, focus and arrow keys). */
export function LineChart({ title, series, xLabel, yLabel, formatX, formatY, onSelect, testId }: {
  title: string
  series: ChartSeries[]
  xLabel: string
  yLabel: string
  formatX: (value: number) => string
  formatY: (value: number) => string
  onSelect?: (point: ChartPoint, series: ChartSeries) => void
  testId?: string
}) {
  const drawn = series.slice(0, MAX_SERIES).filter(s => s.points.length)
  const xs = [...new Set(drawn.flatMap(s => s.points.map(p => p.x)))].sort((a, b) => a - b)
  const [cursor, setCursor] = useState<number | null>(null)
  const all = drawn.flatMap(s => s.points)
  if (!all.length) return <div className="grid h-40 place-items-center rounded-lg border border-dashed text-xs text-muted-foreground" data-testid={testId}>No values for this selection.</div>
  const xTicks = niceTicks(Math.min(...all.map(p => p.x)), Math.max(...all.map(p => p.x)))
  const yTicks = niceTicks(Math.min(0, ...all.map(p => p.y)), Math.max(...all.map(p => p.y)))
  const [x0, x1, y0, y1] = [xTicks[0], xTicks[xTicks.length - 1], yTicks[0], yTicks[yTicks.length - 1]]
  const sx = (v: number) => PAD.left + ((v - x0) / (x1 - x0 || 1)) * (WIDTH - PAD.left - PAD.right)
  const sy = (v: number) => HEIGHT - PAD.bottom - ((v - y0) / (y1 - y0 || 1)) * (HEIGHT - PAD.top - PAD.bottom)
  const active = cursor == null ? null : xs[cursor]
  const nearest = (s: ChartSeries, x: number) => s.points.reduce((best, p) => Math.abs(p.x - x) < Math.abs(best.x - x) ? p : best, s.points[0])
  const hits = active == null ? [] : drawn.map(s => ({ s, p: nearest(s, active) }))
  const move = (clientX: number, rect: DOMRect) => {
    const x = x0 + ((clientX - rect.left) / rect.width * WIDTH - PAD.left) / (WIDTH - PAD.left - PAD.right) * (x1 - x0)
    let index = 0
    xs.forEach((v, i) => { if (Math.abs(v - x) < Math.abs(xs[index] - x)) index = i })
    setCursor(index)
  }
  const key = (event: KeyboardEvent) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault()
      setCursor(c => Math.max(0, Math.min(xs.length - 1, (c ?? -1) + (event.key === "ArrowRight" ? 1 : -1))))
    } else if (event.key === "Enter" && hits[0] && onSelect) onSelect(hits[0].p, hits[0].s)
    else if (event.key === "Escape") setCursor(null)
  }
  return <figure className="min-w-0 space-y-2" data-testid={testId}>
    <figcaption className="text-xs font-semibold">{title}</figcaption>
    <div className="relative">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full touch-none select-none rounded-lg border bg-card outline-none focus-visible:ring-2 focus-visible:ring-ring"
        role="img"
        aria-label={`${title}. ${drawn.length} series. Focus and use the arrow keys to read values${onSelect ? "; Enter opens the frame" : ""}.`}
        tabIndex={0}
        onKeyDown={key}
        onBlur={() => setCursor(null)}
        onMouseMove={e => move(e.clientX, e.currentTarget.getBoundingClientRect())}
        onMouseLeave={() => setCursor(null)}
        onClick={() => { if (hits[0] && onSelect) onSelect(hits[0].p, hits[0].s) }}
        style={{ cursor: onSelect ? "pointer" : "crosshair" }}
      >
        {yTicks.map(t => <g key={`y${t}`}><line x1={PAD.left} x2={WIDTH - PAD.right} y1={sy(t)} y2={sy(t)} stroke="var(--color-border)" strokeWidth={1} /><text x={PAD.left - 6} y={sy(t)} dy="0.32em" textAnchor="end" fontSize={10} fill="var(--color-muted-foreground)">{formatY(t)}</text></g>)}
        {xTicks.map(t => <text key={`x${t}`} x={sx(t)} y={HEIGHT - PAD.bottom + 14} textAnchor="middle" fontSize={10} fill="var(--color-muted-foreground)">{formatX(t)}</text>)}
        <text x={(PAD.left + WIDTH - PAD.right) / 2} y={HEIGHT - 4} textAnchor="middle" fontSize={10} fill="var(--color-muted-foreground)">{xLabel}</text>
        <text transform={`translate(12 ${(PAD.top + HEIGHT - PAD.bottom) / 2}) rotate(-90)`} textAnchor="middle" fontSize={10} fill="var(--color-muted-foreground)">{yLabel}</text>
        {drawn.map(s => <polyline key={s.id} data-series={s.id} fill="none" stroke={seriesColor(s.slot)} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" points={[...s.points].sort((a, b) => a.x - b.x).map(p => `${sx(p.x)},${sy(p.y)}`).join(" ")} />)}
        {active != null && <line x1={sx(active)} x2={sx(active)} y1={PAD.top} y2={HEIGHT - PAD.bottom} stroke="var(--color-muted-foreground)" strokeWidth={1} strokeDasharray="3 3" />}
        {hits.map(({ s, p }) => <circle key={s.id} cx={sx(p.x)} cy={sy(p.y)} r={4.5} fill={seriesColor(s.slot)} stroke="var(--color-card)" strokeWidth={2} />)}
      </svg>
      {hits.length > 0 && active != null && <div role="status" className="pointer-events-none absolute top-2 z-10 max-w-[18rem] rounded-md border bg-popover px-3 py-2 text-[11px] shadow-md" style={sx(active) > WIDTH / 2 ? { left: 12 } : { right: 12 }}>
        <p className="mb-1 font-semibold">{xLabel}: {formatX(active)}</p>
        {hits.map(({ s, p }) => <p key={s.id} className="flex items-center gap-1.5"><span aria-hidden className="size-2 shrink-0 rounded-full" style={{ background: seriesColor(s.slot) }} /><span className="min-w-0 truncate">{s.label}</span><strong className="ml-auto pl-2 tabular-nums">{formatY(p.y)}</strong></p>)}
        {hits[0].p.label && <p className="mt-1 text-muted-foreground">{hits[0].p.label}</p>}
      </div>}
    </div>
    <Legend series={drawn} />
    {series.length > MAX_SERIES && <p className="text-[11px] text-muted-foreground">Showing the first {MAX_SERIES} of {series.length} series. Narrow the camera or geometry filter to see the rest.</p>}
  </figure>
}

export interface BarRow { id: string; label: string; slot: number; value: number | null }

/** Horizontal bars, one per entity, value labelled at the bar end. */
export function BarChart({ title, rows, format, testId }: { title: string; rows: BarRow[]; format: (value: number) => string; testId?: string }) {
  const [hover, setHover] = useState<string | null>(null)
  const values = rows.map(r => r.value).filter((v): v is number => v != null)
  const max = Math.max(0, ...values)
  const ticks = niceTicks(0, max || 1, 4)
  const top = ticks[ticks.length - 1] || 1
  return <figure className="min-w-0 space-y-2" data-testid={testId}>
    <figcaption className="text-xs font-semibold">{title}</figcaption>
    <ul className="space-y-[2px]">
      {rows.map(r => <li key={r.id} className="grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)] items-center gap-2 text-[11px]" onMouseEnter={() => setHover(r.id)} onMouseLeave={() => setHover(null)} title={`${r.label}: ${r.value == null ? "Unavailable" : format(r.value)}`}>
        <span className="truncate text-muted-foreground">{r.label}</span>
        <span className="flex min-w-0 items-center gap-2">
          {r.value == null
            ? <span className="text-muted-foreground">Unavailable</span>
            : <><span className="h-3.5 rounded-r-[4px]" style={{ width: `${Math.max(0.5, (r.value / top) * 100)}%`, background: seriesColor(r.slot), opacity: hover && hover !== r.id ? 0.45 : 1 }} /><span className="shrink-0 tabular-nums text-foreground">{format(r.value)}</span></>}
        </span>
      </li>)}
    </ul>
  </figure>
}
