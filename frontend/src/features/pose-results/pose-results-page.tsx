import { useEffect, useRef, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router-dom"
import {
  AlertTriangle,
  Box,
  ChevronLeft,
  ChevronRight,
  CirclePause,
  CirclePlay,
  Download,
  Eye,
  FileJson,
  Gauge,
  Layers3,
  RefreshCw,
  ScanSearch,
} from "lucide-react"

import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError, api, errorMessage, query } from "@/lib/api"
import type {
  BopInspectionAssociation,
  BopInspectionFrame,
  BopInspectionFrameFilter,
  BopInspectionFrameList,
  BopInspectionSetup,
  BopResultSubmission,
} from "@/lib/contracts"
import { cn, formatDate, titleCase } from "@/lib/utils"
import { useOperator } from "@/providers/operator-provider"

import { OverlayErrorBoundary, PoseOverlay, type PoseOverlayLayers } from "./pose-overlay"
import { PoseExportControls, type MaskLayers } from "./pose-export-controls"

const PAGE_SIZE = 80
const FILTERS: Array<{ value: BopInspectionFrameFilter; label: string }> = [
  { value: "all", label: "All exported frames" },
  { value: "estimated", label: "Has estimate" },
  { value: "target", label: "Evaluation target" },
  { value: "missing_estimate", label: "Missing estimate" },
  { value: "registration", label: "Registration" },
  { value: "tracking", label: "Tracking" },
  { value: "reinitialization", label: "Reinitialization" },
]

const DEFAULT_GEOMETRY: PoseOverlayLayers = {
  estimateSurface: true,
  estimateWireframe: false,
  estimateAxes: false,
  estimateBox: false,
  gtSurface: false,
  gtWireframe: true,
  gtAxes: false,
  gtBox: false,
  estimateOpacity: 0.38,
  gtOpacity: 0.32,
}

const DEFAULT_MASKS: MaskLayers = {
  full: false,
  visible: false,
  fullOpacity: 0.28,
  visibleOpacity: 0.42,
}

function hasWebGL() {
  try {
    const canvas = document.createElement("canvas")
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"))
  } catch {
    return false
  }
}

function fixed(value: number | null | undefined, digits = 3, suffix = "") {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(digits)}${suffix}`
}

function shortHash(value?: string | null) {
  return value ? `${value.slice(0, 14)}…${value.slice(-6)}` : "—"
}

function updateSearch(
  current: URLSearchParams,
  updates: Record<string, string | number | null>,
) {
  const next = new URLSearchParams(current)
  for (const [key, value] of Object.entries(updates)) {
    if (value === null || value === "") next.delete(key)
    else next.set(key, String(value))
  }
  return next
}

function ResultActions({ result, runRoot }: { result: BopResultSubmission; runRoot: string }) {
  return <div className="flex flex-wrap gap-2">
    <Button asChild size="sm" variant="outline"><a href={query(`/bop/evaluation/results/${result.result_id}/package`, { run_root: runRoot })}><Box />Package</a></Button>
    <Button asChild size="sm" variant="outline"><a href={query(`/bop/evaluation/results/${result.result_id}/download`, { run_root: runRoot })}><Download />CSV</a></Button>
    <Button asChild size="sm" variant="outline"><a href={query("/bop/annotations/ground-truth/download", { run_root: runRoot })} title="Download pose ground truth for all exported sensor scenes"><Download />All-sensor GT JSON</a></Button>
    {result.provenance_available && <Button asChild size="sm" variant="outline"><a href={query(`/bop/evaluation/results/${result.result_id}/provenance`, { run_root: runRoot })}><FileJson />Provenance</a></Button>}
    <Button asChild size="sm" variant="outline"><Link to={query("/bop-evaluation", { result_id: result.result_id, run_root: runRoot })}><Gauge />Evaluate</Link></Button>
  </div>
}

function MatrixEvidence({ title, value }: { title: string; value?: number[][] }) {
  return <div className="rounded-lg border bg-muted/15 p-3">
    <div className="text-[9px] font-bold uppercase tracking-[0.14em] text-muted-foreground">{title}</div>
    {value ? <div className="mt-2 grid grid-cols-4 gap-x-3 gap-y-1 font-mono text-[10px] tabular-nums">
      {value.flat().map((item, index) => <span key={index} className="text-right">{item.toFixed(5)}</span>)}
    </div> : <div className="mt-2 text-xs text-muted-foreground">Unavailable</div>}
  </div>
}

function AssociationEvidence({ association }: { association: BopInspectionAssociation }) {
  const ambiguous = association.status === "ambiguous_repeated_object"
  const failed = association.status === "execution_failure" || association.status === "missing_estimate"
  return <div className={cn("rounded-lg border p-3 text-xs", ambiguous ? "border-warning/40 bg-warning/5" : failed ? "border-destructive/35 bg-destructive/5" : "bg-muted/15")}>
    <div className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold">Object {association.obj_id}</span><StatusBadge status={association.status} tone={ambiguous ? "warning" : failed ? "destructive" : "success"} /></div>
    {association.delta ? <div className="mt-2 grid grid-cols-2 gap-2"><div><span className="text-muted-foreground">Translation delta</span><div className="mt-1 font-mono">{fixed(association.delta.translation_mm, 3, " mm")}</div></div><div><span className="text-muted-foreground">Rotation delta</span><div className="mt-1 font-mono">{fixed(association.delta.rotation_deg, 3, "°")}</div></div><p className="col-span-2 text-[10px] text-muted-foreground">Rotation is symmetry-unaware.</p></div> : <p className="mt-2 leading-relaxed text-muted-foreground">{association.reason ?? (association.status === "missing_estimate" ? "No compatible estimate exists for this target." : "No numeric association is available.")}</p>}
    {association.failures && <pre className="mt-2 max-h-32 overflow-auto rounded bg-muted p-2 text-[9px]">{JSON.stringify(association.failures, null, 2)}</pre>}
  </div>
}

function EvidencePane({ frame }: { frame: BopInspectionFrame }) {
  const estimate = frame.estimates[0]
  const groundTruth = frame.ground_truth[0]
  return <div className="space-y-4" data-testid="pose-result-evidence">
    <div className="grid grid-cols-2 gap-2">
      <div className="rounded-lg border bg-muted/15 p-3"><div className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">Estimate score</div><div className="mt-1 font-mono text-sm font-semibold">{fixed(estimate?.score, 5)}</div></div>
      <div className="rounded-lg border bg-muted/15 p-3"><div className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground">Image time</div><div className="mt-1 font-mono text-sm font-semibold">{fixed(frame.execution.image_time_seconds ?? estimate?.time_seconds, 4, " s")}</div></div>
    </div>
    <MatrixEvidence title="Top estimate · model to camera" value={estimate?.matrix_model_to_camera} />
    <MatrixEvidence title="Ground truth · model to camera" value={groundTruth?.matrix_model_to_camera} />
    <div className="rounded-lg border p-3 text-xs">
      <div className="font-semibold">Camera & visibility</div>
      <div className="mt-2 space-y-1 font-mono text-[10px] text-muted-foreground">
        <div>K · {frame.camera.cam_K.map((item) => item.toFixed(3)).join("  ")}</div>
        <div>Depth scale · {fixed(frame.camera.depth_scale_mm, 6, " mm/unit")}</div>
        <div>GT bbox · {groundTruth?.visibility.bbox_obj.join(", ") ?? "—"}</div>
        <div>Visible bbox · {groundTruth?.visibility.bbox_visib.join(", ") ?? "—"}</div>
        <div>Visible fraction · {groundTruth ? `${(groundTruth.visibility.visib_fract * 100).toFixed(2)}%` : "—"}</div>
      </div>
    </div>
    <div className="space-y-2">
      <div className="text-xs font-semibold">Comparison diagnostics</div>
      {frame.associations.length ? frame.associations.map((association, index) => <AssociationEvidence key={`${association.obj_id}-${association.status}-${index}`} association={association} />) : <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">No estimate-to-GT association is available.</div>}
    </div>
    <div className="rounded-lg border p-3 text-xs">
      <div className="flex items-center justify-between gap-2"><span className="font-semibold">Execution provenance</span><StatusBadge status={frame.execution.known ? "known" : "unknown"} tone={frame.execution.known ? "informational" : "neutral"} /></div>
      <p className="mt-2 leading-relaxed text-muted-foreground">{frame.execution.known ? `${frame.frame.operations.map(titleCase).join(" / ")} · registration ${frame.execution.registration_iterations ?? "—"} iterations · tracking ${frame.execution.tracking_iterations ?? "—"} iterations.` : "Generic BOP19 results do not prove registration, tracking, reinitialization, or mask use. Those labels remain unknown."}</p>
      <p className="mt-2 text-[10px] text-muted-foreground">Mask contract · {frame.execution.oracle_mask_contract ?? "unknown"}</p>
    </div>
    {frame.estimates.length > 1 && <details className="rounded-lg border"><summary className="cursor-pointer px-3 py-2 text-xs font-semibold">Ranked hypotheses ({frame.estimates.length}{frame.omitted_hypothesis_count ? ` + ${frame.omitted_hypothesis_count} omitted` : ""})</summary><div className="max-h-72 overflow-auto border-t"><table className="w-full text-left text-[10px]"><thead className="bg-muted/50"><tr><th className="px-3 py-2">Object</th><th className="px-3 py-2">Rank</th><th className="px-3 py-2">Score</th><th className="px-3 py-2">Operation</th></tr></thead><tbody>{frame.estimates.map((item) => <tr key={`${item.obj_id}-${item.rank}`} className="border-t"><td className="px-3 py-2">{item.object_name}</td><td className="px-3 py-2 font-mono">{item.rank}</td><td className="px-3 py-2 font-mono">{item.score.toFixed(6)}</td><td className="px-3 py-2">{item.operations.map(titleCase).join(" / ")}</td></tr>)}</tbody></table></div></details>}
  </div>
}

function LayerToggle({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void }) {
  return <Label className={cn("flex items-center gap-2 rounded border px-2 py-1.5 text-[11px]", disabled ? "cursor-not-allowed opacity-55" : "cursor-pointer")}><Checkbox checked={checked} disabled={disabled} onCheckedChange={(value) => onChange(value === true)} />{label}</Label>
}

function Opacity({ label, value, disabled, onChange }: { label: string; value: number; disabled?: boolean; onChange: (value: number) => void }) {
  return <div className={cn("space-y-1", disabled && "opacity-55")}><div className="flex justify-between text-[9px] font-bold uppercase tracking-wider text-muted-foreground"><span>{label}</span><span>{Math.round(value * 100)}%</span></div><input aria-label={label} type="range" min={0.05} max={1} step={0.05} value={value} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} className="w-full accent-primary" /></div>
}

function LayerControls({
  webgl,
  geometry,
  setGeometry,
  masks,
  setMasks,
  maskCapabilities,
}: {
  webgl: boolean
  geometry: PoseOverlayLayers
  setGeometry: React.Dispatch<React.SetStateAction<PoseOverlayLayers>>
  masks: MaskLayers
  setMasks: React.Dispatch<React.SetStateAction<MaskLayers>>
  maskCapabilities: { full: boolean; visible: boolean }
}) {
  const geometryToggle = (key: keyof PoseOverlayLayers, value: boolean) => setGeometry((current) => ({ ...current, [key]: value }))
  const maskToggle = (key: "full" | "visible", value: boolean) => setMasks((current) => ({ ...current, [key]: value }))
  return <Card data-testid="pose-layer-controls">
    <CardHeader className="pb-3"><CardTitle className="flex items-center gap-2 text-base"><Layers3 className="size-4" />Overlay layers</CardTitle><CardDescription>Browser-local diagnostic layers. Export below to save the selected view settings.</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      {!webgl && <div role="alert" data-testid="pose-webgl-fallback" className="rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs"><div className="font-semibold text-warning-foreground">WebGL geometry unavailable</div><p className="mt-1 text-muted-foreground">RGB, depth, masks, navigation, and numeric evidence remain usable. Geometry controls are disabled.</p></div>}
      <div className="space-y-2"><div className="text-xs font-semibold text-cyan-500">Estimated pose · cyan</div><div className="grid grid-cols-2 gap-2"><LayerToggle label="Estimated surface" checked={geometry.estimateSurface} disabled={!webgl} onChange={(value) => geometryToggle("estimateSurface", value)} /><LayerToggle label="Estimated wireframe" checked={geometry.estimateWireframe} disabled={!webgl} onChange={(value) => geometryToggle("estimateWireframe", value)} /><LayerToggle label="Estimated axes" checked={geometry.estimateAxes} disabled={!webgl} onChange={(value) => geometryToggle("estimateAxes", value)} /><LayerToggle label="Estimated bounding box" checked={geometry.estimateBox} disabled={!webgl} onChange={(value) => geometryToggle("estimateBox", value)} /></div><Opacity label="Estimated opacity" value={geometry.estimateOpacity} disabled={!webgl} onChange={(value) => setGeometry((current) => ({ ...current, estimateOpacity: value }))} /></div>
      <div className="space-y-2 border-t pt-4"><div className="text-xs font-semibold text-fuchsia-500">Ground truth · magenta</div><div className="grid grid-cols-2 gap-2"><LayerToggle label="Ground-truth surface" checked={geometry.gtSurface} disabled={!webgl} onChange={(value) => geometryToggle("gtSurface", value)} /><LayerToggle label="Ground-truth wireframe" checked={geometry.gtWireframe} disabled={!webgl} onChange={(value) => geometryToggle("gtWireframe", value)} /><LayerToggle label="Ground-truth axes" checked={geometry.gtAxes} disabled={!webgl} onChange={(value) => geometryToggle("gtAxes", value)} /><LayerToggle label="Ground-truth bounding box" checked={geometry.gtBox} disabled={!webgl} onChange={(value) => geometryToggle("gtBox", value)} /></div><Opacity label="Ground-truth opacity" value={geometry.gtOpacity} disabled={!webgl} onChange={(value) => setGeometry((current) => ({ ...current, gtOpacity: value }))} /></div>
      <div className="space-y-2 border-t pt-4"><div className="text-xs font-semibold">Oracle mask evidence</div><div className="grid grid-cols-2 gap-2"><LayerToggle label="Full GT mask" checked={masks.full} disabled={!maskCapabilities.full} onChange={(value) => maskToggle("full", value)} /><LayerToggle label="Visible mask" checked={masks.visible} disabled={!maskCapabilities.visible} onChange={(value) => maskToggle("visible", value)} /></div><Opacity label="Full-mask opacity" value={masks.fullOpacity} disabled={!maskCapabilities.full} onChange={(value) => setMasks((current) => ({ ...current, fullOpacity: value }))} /><Opacity label="Visible-mask opacity" value={masks.visibleOpacity} disabled={!maskCapabilities.visible} onChange={(value) => setMasks((current) => ({ ...current, visibleOpacity: value }))} />{!maskCapabilities.full && !maskCapabilities.visible && <p className="text-[10px] leading-relaxed text-muted-foreground">This scene has no valid GT mask evidence.</p>}</div>
    </CardContent>
  </Card>
}

function Viewer({
  frame,
  baseLayer,
  geometry,
  masks,
  webgl,
  onOverlayError,
}: {
  frame: BopInspectionFrame
  baseLayer: "rgb" | "depth"
  geometry: PoseOverlayLayers
  masks: MaskLayers
  webgl: boolean
  onOverlayError: (message: string) => void
}) {
  const [width, height] = frame.camera.image_size
  return <div className="relative w-full overflow-hidden rounded-lg border bg-black" style={{ aspectRatio: `${width} / ${height}` }} data-testid="pose-result-viewer">
    <img src={baseLayer === "rgb" ? frame.media.rgb_url : frame.media.depth_url} alt={`${baseLayer === "rgb" ? "RGB" : "Colorized depth"} frame ${frame.frame.im_id}`} className="absolute inset-0 size-full object-contain" data-testid="pose-base-layer" />
    {masks.full && frame.ground_truth.map((item) => item.mask_urls.full && <div key={`full-${item.gt_id}`} data-testid="pose-full-mask" className="pointer-events-none absolute inset-0 bg-amber-300" style={{ opacity: masks.fullOpacity, maskImage: `url(${item.mask_urls.full})`, WebkitMaskImage: `url(${item.mask_urls.full})`, maskMode: "luminance", maskSize: "100% 100%", WebkitMaskSize: "100% 100%" }} />)}
    {masks.visible && frame.ground_truth.map((item) => item.mask_urls.visible && <div key={`visible-${item.gt_id}`} data-testid="pose-visible-mask" className="pointer-events-none absolute inset-0 bg-lime-300" style={{ opacity: masks.visibleOpacity, maskImage: `url(${item.mask_urls.visible})`, WebkitMaskImage: `url(${item.mask_urls.visible})`, maskMode: "luminance", maskSize: "100% 100%", WebkitMaskSize: "100% 100%" }} />)}
    {webgl && <OverlayErrorBoundary key={`${frame.result.result_id}:${frame.frame.scene_id}:${frame.frame.im_id}`} onError={onOverlayError}><PoseOverlay frame={frame} layers={geometry} /></OverlayErrorBoundary>}
    <div className="pointer-events-none absolute bottom-2 left-2 rounded bg-black/70 px-2 py-1 font-mono text-[9px] text-white">scene {frame.frame.scene_id} · frame {frame.frame.im_id} · {width} × {height}</div>
  </div>
}

export function PoseResultsPage() {
  const { selectedRun, selectRun } = useOperator()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedRunRoot = searchParams.get("run_root") ?? ""
  const runRoot = requestedRunRoot || selectedRun
  const requestedResultId = searchParams.get("result_id") ?? ""
  const requestedSceneValue = searchParams.get("scene_id")
  const requestedFrameValue = searchParams.get("frame_id")
  const requestedSceneId = Number(requestedSceneValue)
  const requestedFrameId = Number(requestedFrameValue)
  const [filter, setFilter] = useState<BopInspectionFrameFilter>("all")
  const [objectId, setObjectId] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const pendingOrdinal = useRef<number | null>(null)
  const autoDownloadExportRef = useRef<string | null>(null)
  const [playing, setPlaying] = useState(false)
  const [baseLayer, setBaseLayer] = useState<"rgb" | "depth">("rgb")
  const [geometry, setGeometry] = useState<PoseOverlayLayers>(DEFAULT_GEOMETRY)
  const [masks, setMasks] = useState<MaskLayers>(DEFAULT_MASKS)
  const [webglAvailable] = useState(() => hasWebGL())
  const [overlayFailure, setOverlayFailure] = useState<{ scope: string; message: string } | null>(null)

  const setup = useQuery({
    queryKey: ["bop-inspection", "setup", runRoot, requestedResultId],
    queryFn: () => api<BopInspectionSetup>(query("/bop/inspection/setup", { run_root: runRoot, result_id: requestedResultId || undefined })),
    retry: false,
  })
  const locateLegacyResult = Boolean(
    requestedResultId && !requestedRunRoot && setup.isError
    && setup.error instanceof ApiError && setup.error.status === 404
    && setup.error.message.includes("Unknown BOP result"),
  )
  const resultLocation = useQuery({
    queryKey: ["bop-inspection", "result-location", requestedResultId],
    queryFn: () => api<{ result_id: string; run_root: string }>(query("/bop/inspection/result-location", { result_id: requestedResultId })),
    enabled: locateLegacyResult,
    retry: false,
  })
  useEffect(() => {
    if (!locateLegacyResult || !resultLocation.data || requestedRunRoot) return
    setSearchParams(updateSearch(searchParams, { run_root: resultLocation.data.run_root }), { replace: true })
  }, [locateLegacyResult, resultLocation.data, requestedRunRoot, searchParams, setSearchParams])
  const resultId = setup.data?.selected_result_id ?? ""
  const validRequestedScene = requestedSceneValue !== null && Number.isInteger(requestedSceneId) && requestedSceneId >= 0
  const scene = setup.data?.scenes.find((item) => validRequestedScene && item.scene_id === requestedSceneId) ?? setup.data?.scenes[0] ?? null
  const sceneId = scene?.scene_id ?? null
  const frames = useQuery({
    queryKey: ["bop-inspection", "frames", runRoot, resultId, sceneId, filter, objectId, page],
    queryFn: () => api<BopInspectionFrameList>(query("/bop/inspection/frames", { run_root: runRoot, result_id: resultId, scene_id: sceneId, filter, object_id: objectId, page, page_size: PAGE_SIZE })),
    enabled: Boolean(resultId && sceneId != null),
  })
  const validRequestedFrame = requestedFrameValue !== null && Number.isInteger(requestedFrameId) && requestedFrameId >= 0
  const frameId = validRequestedFrame ? requestedFrameId : frames.data?.frames[0]?.im_id ?? null
  const detail = useQuery({
    queryKey: ["bop-inspection", "frame", runRoot, resultId, sceneId, frameId],
    queryFn: () => api<BopInspectionFrame>(query("/bop/inspection/frame", { run_root: runRoot, result_id: resultId, scene_id: sceneId, im_id: frameId })),
    enabled: Boolean(resultId && sceneId != null && frameId != null),
    placeholderData: (previous) => previous?.result.result_id === resultId && previous.scene.scene_id === sceneId ? previous : undefined,
  })
  const selectedResult = setup.data?.results.find((item) => item.result_id === resultId) ?? null
  const currentRow = frames.data?.frames.find((item) => item.im_id === frameId)
    ?? (detail.data?.frame.im_id === frameId ? detail.data.frame : null)
  const overlayScope = `${resultId}:${sceneId ?? ""}:${frameId ?? ""}`
  const overlayError = overlayFailure?.scope === overlayScope ? overlayFailure.message : null
  const geometryReady = webglAvailable && !overlayError

  useEffect(() => {
    if (!resultId || sceneId == null) return
    const updates: Record<string, string | number | null> = {}
    if (!requestedRunRoot) updates.run_root = runRoot
    if (requestedResultId !== resultId) updates.result_id = resultId
    if (requestedSceneId !== sceneId) updates.scene_id = sceneId
    if (!validRequestedFrame && frames.data?.frames[0]) updates.frame_id = frames.data.frames[0].im_id
    if (Object.keys(updates).length) setSearchParams(updateSearch(searchParams, updates), { replace: true })
  }, [frames.data?.frames, requestedFrameId, requestedResultId, requestedRunRoot, requestedSceneId, resultId, runRoot, sceneId, searchParams, setSearchParams, validRequestedFrame])

  useEffect(() => {
    if (pendingOrdinal.current == null || !frames.data) return
    const row = frames.data.frames.find((item) => item.ordinal === pendingOrdinal.current)
    if (!row) return
    setSearchParams(updateSearch(searchParams, { frame_id: row.im_id }), { replace: true })
    pendingOrdinal.current = null
  }, [frames.data, searchParams, setSearchParams])

  useEffect(() => {
    if (!playing) return
    if (!currentRow?.next_im_id && currentRow?.next_im_id !== 0) {
      const stop = window.setTimeout(() => setPlaying(false), 0)
      return () => window.clearTimeout(stop)
    }
    const timer = window.setTimeout(() => {
      const nextOrdinal = (currentRow.ordinal ?? 0) + 1
      setPage(Math.floor(nextOrdinal / PAGE_SIZE) + 1)
      setSearchParams(updateSearch(searchParams, { frame_id: currentRow.next_im_id ?? null }), { replace: true })
    }, 600)
    return () => window.clearTimeout(timer)
  }, [currentRow, playing, searchParams, setSearchParams])

  const navigateFrame = (imId: number | null | undefined, ordinal?: number) => {
    if (imId == null) return
    if (ordinal != null) setPage(Math.floor(ordinal / PAGE_SIZE) + 1)
    setSearchParams(updateSearch(searchParams, { frame_id: imId }), { replace: true })
    setPlaying(false)
  }
  const changeResult = (value: string) => {
    setPage(1)
    setPlaying(false)
    setSearchParams(updateSearch(searchParams, { result_id: value, scene_id: null, frame_id: null }), { replace: true })
  }
  const changeScene = (value: string) => {
    setPage(1)
    setPlaying(false)
    setSearchParams(updateSearch(searchParams, { scene_id: Number(value), frame_id: null }), { replace: true })
  }
  const changeFilter = (value: BopInspectionFrameFilter) => {
    setFilter(value)
    setPage(1)
    setPlaying(false)
    setSearchParams(updateSearch(searchParams, { frame_id: null }), { replace: true })
  }
  const changeObject = (value: string) => {
    setObjectId(value === "all" ? null : Number(value))
    setPage(1)
    setPlaying(false)
    setSearchParams(updateSearch(searchParams, { frame_id: null }), { replace: true })
  }
  const changePage = (nextPage: number) => {
    pendingOrdinal.current = (nextPage - 1) * PAGE_SIZE
    setPage(nextPage)
    setPlaying(false)
  }
  const scrub = (ordinal: number) => {
    pendingOrdinal.current = ordinal
    setPage(Math.floor(ordinal / PAGE_SIZE) + 1)
    setPlaying(false)
  }
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["bop-inspection"] })
  }
  const maskCapabilities = {
    full: Boolean(detail.data?.ground_truth.some((item) => item.masks.full)),
    visible: Boolean(detail.data?.ground_truth.some((item) => item.masks.visible)),
  }

  return <div className="space-y-5" data-testid="pose-results-page">
    <PageHeader eyebrow="Inspect · retained estimator result" title="Pose Results" description="Compare one immutable standard BOP19 result with its run's ground truth, frame by frame." actions={<div className="flex gap-2">{runRoot !== selectedRun && <Button variant="outline" onClick={() => selectRun(runRoot)}>Make linked run active</Button>}<Button asChild variant="outline"><Link to="/pose-estimation" onClick={() => selectRun(runRoot)}>Pose Estimation</Link></Button><Button variant="outline" onClick={refresh} disabled={setup.isFetching || frames.isFetching || detail.isFetching}><RefreshCw className={setup.isFetching || frames.isFetching || detail.isFetching ? "animate-spin" : undefined} />Refresh</Button></div>} />
    <ProcessHandoff title="Result inspection & visualization exports" description={`Inspecting run ${runRoot}${runRoot !== selectedRun ? " from this direct link; the active operator run is different. Make this run active before returning to Workflow" : ""}. Overlay preferences are browser-local until you export images or video. Saved visualizations feed dataset review in Workflow.`} to="/workflow/dataset?step=export" action="Review dataset workflow" />

    {searchParams.has("export_id") && (setup.isError || setup.data?.ready === false) && <PoseExportControls runRoot={runRoot} selection={null} availability={setup.data?.exports} autoDownloadRef={autoDownloadExportRef} />}

    {setup.isPending || locateLegacyResult && !resultLocation.isError ? <div className="space-y-4"><Skeleton className="h-32" /><div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_420px]"><Skeleton className="aspect-video" /><Skeleton className="h-[640px]" /></div></div>
      : setup.isError || !setup.data ? <Card className="border-destructive/40"><CardHeader><CardTitle>Pose inspection unavailable</CardTitle><CardDescription>{errorMessage(resultLocation.isError ? resultLocation.error : setup.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={refresh}><RefreshCw />Try again</Button></CardContent></Card>
        : !setup.data.ready || !resultId ? <Card className="border-dashed"><CardContent className="grid min-h-64 place-items-center p-8 text-center"><div><ScanSearch className="mx-auto size-8 text-muted-foreground" /><div className="mt-3 font-semibold">No compatible retained pose result</div><p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-muted-foreground">Collect a succeeded cluster result on Pose Estimation, or import a standard BOP19 CSV from BOP Evaluation. The exported dataset must include ground-truth poses.</p><div className="mt-4 flex justify-center gap-2"><Button asChild><Link to="/pose-estimation">Open Pose Estimation</Link></Button><Button asChild variant="outline"><Link to="/bop-evaluation">Open BOP Evaluation</Link></Button></div>{setup.data.blockers.length > 0 && <ul className="mx-auto mt-4 max-w-xl list-disc text-left text-xs text-destructive">{setup.data.blockers.map((item) => <li key={item.code}>{item.message}</li>)}</ul>}</div></CardContent></Card>
          : <>
            <Card data-testid="pose-result-selection">
              <CardContent className="grid gap-4 p-4 xl:grid-cols-[minmax(280px,1fr)_minmax(280px,0.8fr)_minmax(220px,0.65fr)_auto] xl:items-end">
                <div className="space-y-1.5"><Label htmlFor="pose-result-selector">Retained result</Label><Select value={resultId} onValueChange={changeResult}><SelectTrigger id="pose-result-selector"><SelectValue /></SelectTrigger><SelectContent>{setup.data.results.map((item) => <SelectItem key={item.result_id} value={item.result_id} disabled={!item.compatible}>{item.display_name} · {item.estimate_count.toLocaleString()} estimates</SelectItem>)}</SelectContent></Select>{selectedResult && <p className="font-mono text-[9px] text-muted-foreground">{shortHash(selectedResult.sha256)} · collected {formatDate(selectedResult.created_at)}</p>}</div>
                <div className="space-y-1.5"><Label htmlFor="pose-scene-selector">Sensor scene</Label><Select value={sceneId == null ? "" : String(sceneId)} onValueChange={changeScene}><SelectTrigger id="pose-scene-selector"><SelectValue /></SelectTrigger><SelectContent>{setup.data.scenes.map((item) => <SelectItem key={item.scene_id} value={String(item.scene_id)}>{item.display_name} · scene {item.scene_id}</SelectItem>)}</SelectContent></Select>{scene && <p className="truncate font-mono text-[9px] text-muted-foreground">{scene.physical_identity.family ?? scene.sensor_name} · {scene.physical_identity.device_id ?? scene.physical_identity.sensor_folder}</p>}</div>
                <div className="space-y-1.5"><Label htmlFor="pose-frame-filter">Frame filter</Label><Select value={filter} onValueChange={(value) => changeFilter(value as BopInspectionFrameFilter)}><SelectTrigger id="pose-frame-filter"><SelectValue /></SelectTrigger><SelectContent>{FILTERS.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectContent></Select></div>
                {selectedResult && <ResultActions result={selectedResult} runRoot={runRoot} />}
              </CardContent>
            </Card>

            <Card data-testid="pose-frame-navigation">
              <CardContent className="space-y-3 p-4">
                <div className="flex min-w-0 items-center gap-2"><Button size="icon" variant="outline" aria-label="Previous pose frame" disabled={currentRow?.previous_im_id == null} onClick={() => navigateFrame(currentRow?.previous_im_id, (currentRow?.ordinal ?? 0) - 1)}><ChevronLeft /></Button><Button size="icon" variant="outline" aria-label={playing ? "Pause pose playback" : "Play pose frames"} disabled={!currentRow?.next_im_id && currentRow?.next_im_id !== 0} onClick={() => setPlaying((value) => !value)}>{playing ? <CirclePause /> : <CirclePlay />}</Button><div className="min-w-0 flex-1"><input aria-label="Pose frame scrubber" type="range" min={0} max={Math.max(0, (frames.data?.total_count ?? 1) - 1)} value={currentRow?.ordinal ?? 0} disabled={!frames.data?.total_count} onChange={(event) => scrub(Number(event.target.value))} className="w-full accent-primary" /></div><div className="w-36 text-right font-mono text-[10px]">{currentRow ? `${(currentRow.ordinal ?? 0) + 1} / ${frames.data?.total_count ?? 0} · ID ${currentRow.im_id}` : frames.isFetching ? "Loading…" : "No frames"}</div><Button size="icon" variant="outline" aria-label="Next pose frame" disabled={currentRow?.next_im_id == null} onClick={() => navigateFrame(currentRow?.next_im_id, (currentRow?.ordinal ?? 0) + 1)}><ChevronRight /></Button></div>
                <div className="flex gap-1.5 overflow-x-auto pb-1" data-testid="pose-frame-strip">{frames.data?.frames.map((item) => <button type="button" key={item.im_id} aria-label={`Inspect frame ${item.im_id}`} aria-pressed={item.im_id === frameId} onClick={() => navigateFrame(item.im_id, item.ordinal)} className={cn("relative min-w-12 rounded border px-2 py-1.5 font-mono text-[10px] hover:bg-muted", item.im_id === frameId && "border-primary bg-primary/10", item.missing_estimate && "border-destructive/50")}><span>{item.im_id}</span>{item.operations.some((operation) => operation !== "unknown") && <span className="mt-1 block text-[7px] uppercase text-muted-foreground">{item.operations.map((operation) => operation.slice(0, 3)).join("/")}</span>}</button>)}</div>
                <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-muted-foreground"><span>{frames.data ? `${frames.data.total_count.toLocaleString()} matching frames · page ${frames.data.page} of ${Math.max(1, frames.data.page_count)}` : "Loading frame inventory…"}</span><div className="flex gap-2"><Button size="sm" variant="ghost" disabled={!frames.data?.previous_page} onClick={() => changePage(frames.data?.previous_page ?? 1)}>Previous page</Button><Button size="sm" variant="ghost" disabled={!frames.data?.next_page} onClick={() => changePage(frames.data?.next_page ?? page)}>Next page</Button></div></div>
              </CardContent>
            </Card>

            <div className="grid min-w-0 items-start gap-5 xl:grid-cols-[minmax(0,1fr)_420px]">
              <div className="min-w-0 space-y-4">
                  <Card className="overflow-hidden"><CardHeader className="border-b py-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle className="flex items-center gap-2 text-base"><Eye className="size-4" />Pose overlay</CardTitle><CardDescription className="mt-1">Exact-aspect BOP projection · cyan estimate · magenta ground truth</CardDescription></div><div className="flex gap-2"><Button size="sm" variant={baseLayer === "rgb" ? "default" : "outline"} onClick={() => setBaseLayer("rgb")}>RGB</Button><Button size="sm" variant={baseLayer === "depth" ? "default" : "outline"} onClick={() => setBaseLayer("depth")}>Depth</Button></div></div></CardHeader><CardContent className="p-3">{detail.isPending && !detail.data ? <Skeleton className="aspect-video w-full" /> : detail.isError || !detail.data ? <div className="grid aspect-video place-items-center rounded-lg border border-destructive/35 bg-destructive/5 p-8 text-center"><div><AlertTriangle className="mx-auto size-7 text-destructive" /><div className="mt-2 font-semibold">Frame evidence unavailable</div><p className="mt-1 text-xs text-muted-foreground">{errorMessage(detail.error)}</p></div></div> : <Viewer frame={detail.data} baseLayer={baseLayer} geometry={geometry} masks={masks} webgl={geometryReady} onOverlayError={(message) => setOverlayFailure({ scope: overlayScope, message })} />}{overlayError && <div role="alert" className="mt-3 rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs"><div className="font-semibold text-warning-foreground">Geometry overlay disabled</div><p className="mt-1 text-muted-foreground">{overlayError} Image, mask, navigation, and numeric evidence remain available.</p></div>}<div className="mt-3 flex items-start gap-2 rounded-lg border border-primary/25 bg-primary/5 p-3 text-xs"><ScanSearch className="mt-0.5 size-4 shrink-0 text-primary-strong" /><p><strong>X-ray diagnostic.</strong> Geometry is projected from the saved poses and camera intrinsics. It is not occluded against observed depth and must not be read as a photorealistic visibility claim. <HelpTip label="x-ray pose overlay">The surface, wireframe, axes, and boxes remain visible through scene objects so pose disagreement is easy to spot. Use GT masks and visibility evidence for observed-pixel context.</HelpTip></p></div></CardContent></Card>
                {detail.data && <Card><CardHeader><CardTitle className="text-base">Frame evidence</CardTitle><CardDescription>{detail.data.estimates.length} ranked estimate{detail.data.estimates.length === 1 ? "" : "s"} · {detail.data.ground_truth.length} GT instance{detail.data.ground_truth.length === 1 ? "" : "s"}</CardDescription></CardHeader><CardContent><EvidencePane frame={detail.data} /></CardContent></Card>}
              </div>
              <div className="space-y-4 xl:sticky xl:top-20">
                <LayerControls webgl={geometryReady} geometry={geometry} setGeometry={setGeometry} masks={masks} setMasks={setMasks} maskCapabilities={maskCapabilities} />
                <PoseExportControls runRoot={runRoot} selection={sceneId == null ? null : { result_id: resultId, scene_id: sceneId, filter, object_id: objectId, background: baseLayer, geometry, masks, max_hypotheses: 20 }} sceneName={scene?.display_name} frameCount={frames.data?.total_count} availability={setup.data.exports} autoDownloadRef={autoDownloadExportRef} />
                <Card><CardHeader className="pb-3"><CardTitle className="text-base">Inspection identity</CardTitle></CardHeader><CardContent className="space-y-2 text-[10px]"><div><span className="text-muted-foreground">Run</span><div className="mt-1 break-all font-mono">{runRoot}</div></div><div><span className="text-muted-foreground">Result</span><div className="mt-1 break-all font-mono">{resultId}</div></div><div><span className="text-muted-foreground">Scene / frame</span><div className="mt-1 font-mono">{sceneId ?? "—"} / {frameId ?? "—"}</div></div><div><span className="text-muted-foreground">Projection</span><div className="mt-1 font-mono">{setup.data.visualization_contract?.projection ?? "—"}</div></div><div><span className="text-muted-foreground">Object filter</span><Select value={objectId == null ? "all" : String(objectId)} onValueChange={changeObject}><SelectTrigger className="mt-1"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All objects</SelectItem>{setup.data.objects.map((item) => <SelectItem key={item.obj_id} value={String(item.obj_id)}>{item.name} · ID {item.obj_id}</SelectItem>)}</SelectContent></Select></div></CardContent></Card>
              </div>
            </div>
          </>}
  </div>
}
