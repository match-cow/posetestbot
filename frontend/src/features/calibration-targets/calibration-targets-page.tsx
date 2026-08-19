import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Download, FileJson, FileText, Grid3X3, LoaderCircle, ScanLine, Sparkles, Trash2, TriangleAlert } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { EmptyState } from "@/components/empty-state"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { CalibrationArrangementCard, calibrationArrangementForSensors, effectiveCalibrationTargetMountingFrame, POSE_TEMPLATE_BASE_SUNRISE_PATH, type CalibrationArrangement, type CalibrationTargetMountingFrame } from "@/components/calibration-arrangement"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ApiError, api, errorMessage, query } from "@/lib/api"
import type { Job, RunConfig } from "@/lib/contracts"
import { useOperator } from "@/providers/operator-provider"

type GeneratorStatus = {
  generation_available: boolean
  generator: { reason: string | null; required_revision: string; checkout: string }
}

type Capabilities = {
  paper_sizes_mm: Record<string, [number, number]>
  dictionaries: Record<string, number>
  defaults: Configuration
}

type Configuration = {
  schema_version: "2.0"
  page: { paper_size: string; orientation: "portrait" | "landscape" }
  board: {
    type: "aruco"
    dictionary: string
    rows: number
    columns: number
    marker_size_mm: number
    separation_mm: number
    show_ids: boolean
    id_font_size_pt: number
  }
  print_compensation: { x_percent: number; y_percent: number }
  annotations: { show_ruler: boolean; show_parameters: boolean; show_frame_legend: boolean }
  coordinate_frame: {
    enabled: boolean
    pose: {
      translation_x_m: number
      translation_y_m: number
      translation_z_m: number
      roll_deg: number
      pitch_deg: number
      yaw_deg: number
    }
  }
}

type Bundle = {
  target_id: string
  display_name?: string
  created_at?: string
  valid: boolean
  error?: string
  selected: boolean
  selected_placement?: { mode: string; mounting_frame?: CalibrationTargetMountingFrame } | null
  geometry_sha256?: string
  target?: {
    target_bounds: { width_mm: number; height_mm: number }
    print_compensation: { x_percent: number; y_percent: number }
    grid_size?: [number, number]
  }
}

type LibraryResponse = {
  bundles: Bundle[]
  replacement_blockers: string[]
}

type SelectionResponse = {
  status?: "unchanged"
  job_id?: string
}

const placementLabels = {
  unknown: "Fixed target; estimate grid → PoseTemplateBase",
  template_base_identity: "Grid aligned to PoseTemplateBase",
  posegridgen_board_to_base: "Use PoseGridGen grid → PoseTemplateBase pose",
}

type RunConfigResponse = {
  config: RunConfig
  camera_contract?: { mutable: boolean; blockers: string[] }
}

export function CalibrationTargetsPage() {
  const { selectedRun } = useOperator()
  const queryClient = useQueryClient()
  const status = useQuery({ queryKey: ["calibration-targets", "status"], queryFn: () => api<GeneratorStatus>("/calibration-targets/status"), staleTime: 30_000 })
  const capabilities = useQuery({ queryKey: ["calibration-targets", "capabilities"], queryFn: () => api<Capabilities>("/calibration-targets/capabilities"), enabled: status.data?.generation_available === true, staleTime: Infinity })
  const library = useQuery({ queryKey: ["calibration-targets", "bundles", selectedRun], queryFn: () => api<LibraryResponse>(query("/calibration-targets/bundles", { run_root: selectedRun })) })
  const runConfig = useQuery({ queryKey: ["run-config", selectedRun], queryFn: () => api<RunConfigResponse>(query("/run-config", { run_root: selectedRun })), retry: false })
  const [configurationOverride, setConfiguration] = useState<Configuration | null>(null)
  const [displayName, setDisplayName] = useState("")
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [selection, setSelection] = useState<Bundle | null>(null)
  const [deleteConfirmation, setDeleteConfirmation] = useState<Bundle | null>(null)
  const [placement, setPlacement] = useState<keyof typeof placementLabels>("unknown")
  const [pendingJob, setPendingJob] = useState<{ id: string; kind: "generate" | "select"; runRoot?: string } | null>(null)

  const configuration = configurationOverride ?? capabilities.data?.defaults ?? null
  const arrangement: CalibrationArrangement = (() => {
    if (runConfig.isPending) {
      return {
        status: "blocked",
        reason: "no_enabled_cameras",
        title: "Loading the active run's camera setup",
        message: "Target selection is disabled until the run-owned camera mounting has been loaded.",
      }
    }
    if (!runConfig.data?.config) {
      return {
        status: "blocked",
        reason: "no_enabled_cameras",
        title: "Save Workflow step 1 first",
        message: runConfig.error instanceof ApiError && runConfig.error.status === 404
          ? "This run has no saved camera setup. Configure its cameras before selecting a target."
          : `The active run's camera setup could not be loaded${runConfig.error ? `: ${errorMessage(runConfig.error)}` : "."}`,
      }
    }
    return calibrationArrangementForSensors(runConfig.data.config.capture.sensors)
  })()

  const serialized = useMemo(() => configuration ? JSON.stringify(configuration) : "", [configuration])
  useEffect(() => {
    if (!configuration) return
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      setPreviewBusy(true)
      try {
        const response = await fetch("/calibration-targets/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: serialized, signal: controller.signal })
        if (!response.ok) {
          const body = await response.json().catch(() => null) as { output?: string; errors?: Array<{ message: string }> } | null
          throw new Error(body?.output || body?.errors?.map((item) => item.message).join("; ") || `Preview failed (${response.status})`)
        }
        const next = URL.createObjectURL(await response.blob())
        setPreviewUrl((current) => { if (current) URL.revokeObjectURL(current); return next })
        setPreviewError(null)
      } catch (error) {
        if (!controller.signal.aborted) setPreviewError(errorMessage(error))
      } finally {
        if (!controller.signal.aborted) setPreviewBusy(false)
      }
    }, 350)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [configuration, serialized])

  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl) }, [previewUrl])

  const job = useQuery({
    queryKey: ["calibration-target-job", pendingJob?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingJob!.id}`),
    enabled: Boolean(pendingJob),
    refetchInterval: (state) => ["succeeded", "failed", "canceled", "cancelled"].includes(state.state.data?.job.status ?? "") ? false : 500,
  })
  useEffect(() => {
    const result = job.data?.job
    if (!result || !pendingJob || !["succeeded", "failed", "canceled", "cancelled"].includes(result.status)) return
    const affectedRun = pendingJob.runRoot
    if (result.status === "succeeded") {
      toast.success(pendingJob.kind === "generate" ? "Calibration target generated" : "Calibration target selected", {
        description: affectedRun ? `Run ${affectedRun}` : undefined,
      })
      queryClient.invalidateQueries({ queryKey: ["calibration-targets", "bundles"] })
      if (affectedRun) {
        queryClient.invalidateQueries({ queryKey: ["run-config", affectedRun] })
        queryClient.invalidateQueries({ queryKey: ["overview", affectedRun] })
      }
    } else toast.error("Calibration target job did not complete", { description: result.message ?? result.tail.at(-1) })
    queueMicrotask(() => {
      if (result.status === "succeeded") {
        setDisplayName("")
        setSelection(null)
      }
      setPendingJob(null)
    })
  }, [job.data, pendingJob, queryClient])

  const fit = useMutation({
    mutationFn: () => api<{ request: Configuration }>("/calibration-targets/fit", { method: "POST", body: serialized }),
    onSuccess: (result) => { setConfiguration(result.request); toast.success("Board fitted to the selected page") },
    onError: (error) => toast.error("Board could not be fitted", { description: errorMessage(error) }),
  })
  const generate = useMutation({
    mutationFn: () => api<{ job_id: string }>("/calibration-targets/generate", { method: "POST", body: JSON.stringify({ display_name: displayName, configuration }) }),
    onSuccess: (result) => { setPendingJob({ id: result.job_id, kind: "generate" }); toast.success("Generation queued", { description: `Job ${result.job_id}` }) },
    onError: (error) => toast.error("Generation was not queued", { description: errorMessage(error) }),
  })
  const select = useMutation({
    mutationFn: () => {
      if (arrangement.status !== "ready") throw new Error(arrangement.message)
      const selectedPlacement = arrangement.mountingFrame === "robot_flange" ? "unknown" : placement
      return api<SelectionResponse>(`/calibration-targets/bundles/${selection!.target_id}/select`, {
        method: "POST",
        body: JSON.stringify({
          run_root: selectedRun,
          placement: selectedPlacement,
          mounting_frame: arrangement.mountingFrame,
        }),
      })
    },
    onSuccess: (result) => {
      if (result.status === "unchanged") {
        setSelection(null)
        toast.success("Calibration target already selected", { description: "The same saved target remains available for another calibration attempt." })
        return
      }
      if (!result.job_id) {
        toast.error("Target selection returned an invalid response")
        return
      }
      setPendingJob({ id: result.job_id, kind: "select", runRoot: selectedRun })
      toast.success("Selection queued", { description: `Job ${result.job_id}` })
    },
    onError: (error) => {
      const body = error instanceof ApiError && typeof error.body === "object" ? error.body as { blockers?: string[] } : null
      const blockers = Array.isArray(body?.blockers) ? body.blockers : []
      toast.error("Target was not selected", {
        description: blockers.length ? `${errorMessage(error)} Blockers: ${blockers.join(", ")}` : errorMessage(error),
      })
    },
  })
  const removeBundle = useMutation({
    mutationFn: (bundle: Bundle) => api<{ status: "deleted"; target_id: string }>(`/calibration-targets/bundles/${bundle.target_id}`, { method: "DELETE", body: JSON.stringify({ run_root: selectedRun, confirm: true }) }),
    onSuccess: (_result, bundle) => {
      toast.success("Calibration target deleted", { description: bundle.display_name ?? bundle.target_id })
      setDeleteConfirmation(null)
      queryClient.invalidateQueries({ queryKey: ["calibration-targets", "bundles"] })
    },
    onError: (error) => toast.error("Calibration target was not deleted", { description: errorMessage(error) }),
  })
  const active = library.data?.bundles.find((item) => item.selected)
  const activeMountingFrame = effectiveCalibrationTargetMountingFrame(active?.selected_placement)
  const targetReplacementBlockers = library.data?.replacement_blockers ?? []
  const targetSelectionLocked = Boolean(active && targetReplacementBlockers.length)
  const selectedPlacement = selection?.selected_placement?.mode as keyof typeof placementLabels | undefined
  const selectedMountingFrame = effectiveCalibrationTargetMountingFrame(selection?.selected_placement)
  const requestedPlacement = arrangement.status === "ready" && arrangement.mountingFrame === "robot_flange" ? "unknown" : placement
  const requestedMountingFrame = arrangement.status === "ready" ? arrangement.mountingFrame : null
  const selectionIsCurrent = Boolean(
    selection?.selected
    && selectedPlacement === requestedPlacement
    && selectedMountingFrame === requestedMountingFrame,
  )
  const selectionWouldReplace = Boolean(selection && !selectionIsCurrent && active)
  const selectionBlocked = arrangement.status !== "ready" || (targetSelectionLocked && selectionWouldReplace)
  const targetSetupBlocked = arrangement.status !== "ready"
  const openSelection = (bundle: Bundle) => {
    setSelection(bundle)
    setPlacement(
      arrangement.status === "ready" && arrangement.mountingFrame === "robot_flange"
        ? "unknown"
        : (bundle.selected_placement?.mode as keyof typeof placementLabels) || "unknown",
    )
  }

  if (status.isPending) return <div className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground">Checking PoseGridGen…</div>
  if (!status.data?.generation_available) return <div className="space-y-6">
    <PageHeader eyebrow="Reusable calibration geometry" title="Calibration Targets" description="Browse and select saved targets even while PoseGridGen generation is unavailable." />
    <ProcessHandoff title="The selected target is camera-calibration step 2" description="A saved target remains usable without the generator. Select the bundle that exactly matches the printed board, then return to readiness in the guided calibration workflow." to="/workflow/calibration?step=target" action="Open calibration step 2" />
    <CalibrationArrangementCard arrangement={arrangement} editHref="/workflow/calibration?step=configure" testId="calibration-target-arrangement" />
    {targetSelectionLocked && active && <TargetReuseNotice bundle={active} blockers={targetReplacementBlockers} />}
    <Card className="border-warning/40"><CardHeader><CardTitle className="flex items-center gap-2 text-base"><ScanLine className="size-5 text-warning" />Target generation is unavailable</CardTitle><CardDescription>{status.data?.generator.reason ?? "The pinned PoseGridGen source checkout could not be initialized."}</CardDescription></CardHeader><CardContent><div className="rounded border bg-muted p-3 font-mono text-xs">git submodule update --init third_party/PoseGridGen<br />bash scripts/install.sh --with-posegridgen</div></CardContent></Card>
    <div><h2 className="text-xl font-semibold">Saved target library</h2><p className="mt-1 text-sm text-muted-foreground">Existing immutable geometry remains fully usable for calibration attempts.</p></div>
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{library.data?.bundles.map((bundle) => <Card key={bundle.target_id} className={bundle.selected ? "border-primary/40" : ""}><CardHeader><CardTitle className="text-base">{bundle.display_name ?? bundle.target_id}</CardTitle><CardDescription className="font-mono text-[10px]">{bundle.target_id}</CardDescription></CardHeader><CardContent className="space-y-3">{bundle.valid ? <><TargetLibraryPreview bundle={bundle} /><div className="text-xs text-muted-foreground">{bundle.target?.grid_size?.join(" × ")} markers · {bundle.target?.target_bounds.width_mm.toFixed(1)} × {bundle.target?.target_bounds.height_mm.toFixed(1)} mm</div><div className="flex flex-wrap gap-2"><DownloadLink bundle={bundle} artifact="source" icon={<FileJson />} /><DownloadLink bundle={bundle} artifact="target" icon={<FileJson />} /><DownloadLink bundle={bundle} artifact="pdf" icon={<FileText />} /></div><Button className="w-full" variant={bundle.selected ? "outline" : "default"} disabled={!bundle.selected && (targetSelectionLocked || targetSetupBlocked)} title={!bundle.selected && targetSelectionLocked ? "This run already has target-dependent evidence. Start a fresh run to reuse a different saved target." : !bundle.selected && targetSetupBlocked ? "Save one homogeneous camera mounting group in Workflow step 1 first." : undefined} onClick={() => openSelection(bundle)}>{bundle.selected ? "Review active target" : "Select for run"}</Button>{!bundle.selected && targetSelectionLocked && <p data-testid="calibration-target-disabled-reason" className="text-[11px] leading-relaxed text-warning-foreground">This run already has target-dependent evidence. Start a fresh run to select a different target.</p>}{!bundle.selected && targetSetupBlocked && <p data-testid="calibration-target-setup-disabled-reason" className="text-[11px] leading-relaxed text-destructive">Target selection needs one saved camera mounting group in Workflow step 1.</p>}</> : <div className="text-xs text-destructive">Invalid bundle: {bundle.error}</div>}</CardContent></Card>)}</div>
    {library.data?.bundles.length === 0 && <EmptyState icon={Grid3X3} title="No saved targets" description="Generation must be restored before a new target can be created." />}
    <TargetSelectionDialog selection={selection} placement={placement} arrangement={arrangement} placementLocked={Boolean(selection?.selected && targetSelectionLocked)} selectionBlocked={selectionBlocked} selectionIsCurrent={selectionIsCurrent} busy={select.isPending || pendingJob !== null} onPlacementChange={setPlacement} onClose={() => setSelection(null)} onSelect={() => select.mutate()} />
  </div>
  if (!configuration || !capabilities.data) return <div className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground">Loading generator capabilities…</div>

  const setBoard = (values: Partial<Configuration["board"]>) => setConfiguration({ ...configuration, board: { ...configuration.board, ...values } })
  const setPage = (values: Partial<Configuration["page"]>) => setConfiguration({ ...configuration, page: { ...configuration.page, ...values } })
  const setCompensation = (values: Partial<Configuration["print_compensation"]>) => setConfiguration({ ...configuration, print_compensation: { ...configuration.print_compensation, ...values } })
  const setAnnotations = (values: Partial<Configuration["annotations"]>) => setConfiguration({ ...configuration, annotations: { ...configuration.annotations, ...values } })
  const setPose = (values: Partial<Configuration["coordinate_frame"]["pose"]>) => setConfiguration({ ...configuration, coordinate_frame: { ...configuration.coordinate_frame, pose: { ...configuration.coordinate_frame.pose, ...values } } })
  const paperDimensions = capabilities.data.paper_sizes_mm[configuration.page.paper_size] ?? [210, 297]
  const [pageWidthMm, pageHeightMm] = configuration.page.orientation === "portrait" ? paperDimensions : [paperDimensions[1], paperDimensions[0]]
  const previewMaxWidthPx = 700 * pageWidthMm / pageHeightMm

  return <div className="space-y-6">
    <PageHeader eyebrow="Printable calibration geometry" title="Calibration Targets" description="Preview compensated ArUco geometry, generate immutable JSON/PDF bundles, then explicitly select one for the active run." />
    <ProcessHandoff title="Author here, then continue camera calibration" description="Generating a bundle only adds it to the reusable library. Selecting it binds the exact geometry and placement to the active run; readiness and recording stay in the guided workflow." to="/workflow/calibration?step=target" action="Open calibration step 2" />
    <CalibrationArrangementCard arrangement={arrangement} editHref="/workflow/calibration?step=configure" testId="calibration-target-arrangement" />
    {pendingJob && <Card className="border-primary/30"><CardContent className="flex items-center justify-between gap-4 py-4"><div><div className="text-xs uppercase tracking-wide text-muted-foreground">{pendingJob.kind === "generate" ? "Reusable-library generation" : "Run selection"} job</div><div className="mt-1 flex items-center gap-2 font-mono text-sm"><LoaderCircle className="size-4 animate-spin" />{pendingJob.id} · {job.data?.job.status ?? "queued"}</div>{pendingJob.runRoot && <p className="mt-1 break-all text-xs text-muted-foreground">Selection is being written for <span className="font-mono">{pendingJob.runRoot}</span>, even if the active folder changes.</p>}</div><Button variant="outline" asChild><Link to="/jobs">Open Jobs</Link></Button></CardContent></Card>}
    {active && <Card className="border-primary/30"><CardContent className="flex items-center justify-between py-4"><div><div className="flex items-center gap-1 text-xs uppercase tracking-wide text-muted-foreground">Active for this run <HelpTip label="active calibration target">This run owns a snapshot of the selected immutable bundle and physical mounting frame. Editing generator fields above does not change it.</HelpTip></div><div className="mt-1 font-semibold">{active.display_name}</div><div className="font-mono text-xs text-muted-foreground">{active.target_id} · {activeMountingFrame === "robot_flange" ? "robot flange · offset estimated" : activeMountingFrame ? placementLabels[(active.selected_placement?.mode as keyof typeof placementLabels) ?? "unknown"] ?? active.selected_placement?.mode : "invalid mounting evidence"}</div></div><Grid3X3 className="size-7 text-primary-strong" /></CardContent></Card>}
    {targetSelectionLocked && active && <TargetReuseNotice bundle={active} blockers={targetReplacementBlockers} />}
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.9fr)]">
      <Card><CardHeader><CardTitle>ArUco specification</CardTitle><CardDescription>All dimensions are millimetres; X and Y compensation is baked into exact marker corners.</CardDescription></CardHeader><CardContent className="space-y-5">
        <div className="grid grid-cols-3 gap-4"><Field label={<span className="inline-flex items-center gap-1">Dictionary <HelpTip label="ArUco dictionary">The dictionary fixes the marker bit patterns and IDs. Detection must use the same dictionary as the printed target.</HelpTip></span>}><Select value={configuration.board.dictionary} onValueChange={(dictionary) => setBoard({ dictionary })}><SelectTrigger aria-label="Dictionary"><SelectValue /></SelectTrigger><SelectContent>{Object.keys(capabilities.data.dictionaries).map((name) => <SelectItem value={name} key={name}>{name}</SelectItem>)}</SelectContent></Select></Field><NumberField label="Columns" value={configuration.board.columns} min={1} max={100} onChange={(columns) => setBoard({ columns })} /><NumberField label="Rows" value={configuration.board.rows} min={1} max={100} onChange={(rows) => setBoard({ rows })} /></div>
        <div className="grid grid-cols-4 gap-4"><NumberField label="Marker size" value={configuration.board.marker_size_mm} min={0.1} step={0.1} onChange={(marker_size_mm) => setBoard({ marker_size_mm })} /><NumberField label="Separation" value={configuration.board.separation_mm} min={0.1} step={0.1} onChange={(separation_mm) => setBoard({ separation_mm })} /><NumberField label="X print %" help="Printer compensation changes the authored X dimensions so the measured physical print matches the target geometry." value={configuration.print_compensation.x_percent} min={0.1} max={200} step={0.1} onChange={(x_percent) => setCompensation({ x_percent })} /><NumberField label="Y print %" help="Printer compensation changes the authored Y dimensions independently. Measure the print before using a non-100% value." value={configuration.print_compensation.y_percent} min={0.1} max={200} step={0.1} onChange={(y_percent) => setCompensation({ y_percent })} /></div>
        <div className="grid grid-cols-2 gap-4"><Field label="Paper"><Select value={configuration.page.paper_size} onValueChange={(paper_size) => setPage({ paper_size })}><SelectTrigger aria-label="Paper"><SelectValue /></SelectTrigger><SelectContent>{Object.keys(capabilities.data.paper_sizes_mm).map((name) => <SelectItem value={name} key={name}>{name}</SelectItem>)}</SelectContent></Select></Field><Field label="Orientation"><Select value={configuration.page.orientation} onValueChange={(orientation: "portrait" | "landscape") => setPage({ orientation })}><SelectTrigger aria-label="Orientation"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="portrait">Portrait</SelectItem><SelectItem value="landscape">Landscape</SelectItem></SelectContent></Select></Field></div>
        <div className="grid grid-cols-2 gap-2"><Check label="Show ruler" checked={configuration.annotations.show_ruler} onChange={(show_ruler) => setAnnotations({ show_ruler })} /><Check label="Show parameters" checked={configuration.annotations.show_parameters} onChange={(show_parameters) => setAnnotations({ show_parameters })} /><Check label="Show marker IDs" checked={configuration.board.show_ids} onChange={(show_ids) => setBoard({ show_ids })} /><Check label="Show board frame" checked={configuration.annotations.show_frame_legend} onChange={(show_frame_legend) => setAnnotations({ show_frame_legend })} /></div>
        <div><Check label="Attach optional board-to-PoseTemplateBase pose" checked={configuration.coordinate_frame.enabled} onChange={(enabled) => setConfiguration({ ...configuration, coordinate_frame: { ...configuration.coordinate_frame, enabled } })} /><p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">This optional fixed placement is for robot-mounted-camera calibration, where the grid remains stationary in PoseTemplateBase. Static-camera workcell calibration instead mounts the grid on the robot and jointly estimates its attachment; this field is not used for that arrangement.</p></div>
        {configuration.coordinate_frame.enabled && <div className="grid grid-cols-3 gap-3 rounded border p-3"><NumberField label="X m" value={configuration.coordinate_frame.pose.translation_x_m} step={0.001} onChange={(translation_x_m) => setPose({ translation_x_m })} /><NumberField label="Y m" value={configuration.coordinate_frame.pose.translation_y_m} step={0.001} onChange={(translation_y_m) => setPose({ translation_y_m })} /><NumberField label="Z m" value={configuration.coordinate_frame.pose.translation_z_m} step={0.001} onChange={(translation_z_m) => setPose({ translation_z_m })} /><NumberField label="Roll°" value={configuration.coordinate_frame.pose.roll_deg} step={0.1} onChange={(roll_deg) => setPose({ roll_deg })} /><NumberField label="Pitch°" value={configuration.coordinate_frame.pose.pitch_deg} step={0.1} onChange={(pitch_deg) => setPose({ pitch_deg })} /><NumberField label="Yaw°" value={configuration.coordinate_frame.pose.yaw_deg} step={0.1} onChange={(yaw_deg) => setPose({ yaw_deg })} /></div>}
        <div className="flex gap-3"><Button variant="outline" onClick={() => fit.mutate()} disabled={fit.isPending}>{fit.isPending ? <LoaderCircle className="animate-spin" /> : <Sparkles />}Fit to page</Button><Input aria-label="Target display name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Target name" /><Button onClick={() => generate.mutate()} disabled={!displayName.trim() || generate.isPending || pendingJob !== null}>{generate.isPending || pendingJob?.kind === "generate" ? <LoaderCircle className="animate-spin" /> : null}Generate bundle</Button></div>
      </CardContent></Card>
      <Card><CardHeader><CardTitle className="flex items-center gap-2">Preview {previewBusy && <LoaderCircle className="size-4 animate-spin" />}</CardTitle><CardDescription>Debounced PNG from the same pinned renderer used for the persistent PDF.</CardDescription></CardHeader><CardContent>{previewError ? <div className="rounded border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">{previewError}</div> : previewUrl ? <div className="space-y-3"><div className="flex items-center justify-between gap-3 text-xs text-muted-foreground"><span>{configuration.page.paper_size} · <span className="capitalize">{configuration.page.orientation}</span></span><span className="font-mono tabular-nums">{pageWidthMm} × {pageHeightMm} mm</span></div><div data-testid="calibration-preview-canvas" className="surface-grid grid min-h-96 place-items-center overflow-auto rounded-lg border bg-muted p-6 shadow-inner sm:p-8"><div data-testid="calibration-preview-page" className="w-full overflow-hidden bg-white ring-1 ring-black/20 shadow-[0_20px_50px_-20px_rgba(0,0,0,0.75),0_2px_5px_rgba(0,0,0,0.35)]" style={{ aspectRatio: `${pageWidthMm} / ${pageHeightMm}`, maxWidth: `${previewMaxWidthPx}px` }}><img src={previewUrl} alt="Calibration target preview" className="block size-full object-contain" /></div></div></div> : <div className="grid h-96 place-items-center text-sm text-muted-foreground">Rendering preview…</div>}</CardContent></Card>
    </div>
    <div><h2 className="text-xl font-semibold">Immutable target library</h2><p className="mt-1 text-sm text-muted-foreground">Generation never changes the active run. Download, inspect, then select deliberately.</p></div>
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{library.data?.bundles.map((bundle) => <Card key={bundle.target_id} className={bundle.selected ? "border-primary/40" : ""}><CardHeader><CardTitle className="text-base">{bundle.display_name ?? bundle.target_id}</CardTitle><CardDescription className="font-mono text-[10px]">{bundle.target_id}</CardDescription></CardHeader><CardContent className="space-y-3">{bundle.valid ? <><TargetLibraryPreview bundle={bundle} /><div className="text-xs text-muted-foreground">{bundle.target?.grid_size?.join(" × ")} markers · {bundle.target?.target_bounds.width_mm.toFixed(1)} × {bundle.target?.target_bounds.height_mm.toFixed(1)} mm · {bundle.target?.print_compensation.x_percent}% × {bundle.target?.print_compensation.y_percent}%</div><div className="flex flex-wrap gap-2"><DownloadLink bundle={bundle} artifact="source" icon={<FileJson />} /><DownloadLink bundle={bundle} artifact="target" icon={<FileJson />} /><DownloadLink bundle={bundle} artifact="pdf" icon={<FileText />} /></div><div className="flex gap-2"><Button className="flex-1" variant={bundle.selected ? "outline" : "default"} disabled={!bundle.selected && (targetSelectionLocked || targetSetupBlocked)} title={!bundle.selected && targetSelectionLocked ? "This run already has target-dependent evidence. Start a fresh run to reuse a different saved target." : !bundle.selected && targetSetupBlocked ? "Save one homogeneous camera mounting group in Workflow step 1 first." : undefined} onClick={() => openSelection(bundle)}>{bundle.selected ? "Review active target" : "Select for run"}</Button><Button variant="outline" size="icon" aria-label={`Delete ${bundle.display_name ?? bundle.target_id}`} title={bundle.selected ? "Cannot delete while this target is selected by the active run." : "Delete target"} disabled={bundle.selected} onClick={() => setDeleteConfirmation(bundle)}><Trash2 /></Button></div>{bundle.selected && <p className="text-[11px] leading-relaxed text-muted-foreground">This library target cannot be deleted while it is selected by the active run.</p>}{!bundle.selected && targetSelectionLocked && <p data-testid="calibration-target-disabled-reason" className="text-[11px] leading-relaxed text-warning-foreground">This run already has target-dependent evidence. Start a fresh run to select a different target.</p>}{!bundle.selected && targetSetupBlocked && <p data-testid="calibration-target-setup-disabled-reason" className="text-[11px] leading-relaxed text-destructive">Target selection needs one saved camera mounting group in Workflow step 1.</p>}</> : <div className="text-xs text-destructive">Invalid bundle: {bundle.error}</div>}</CardContent></Card>)}</div>
    {library.data?.bundles.length === 0 && <EmptyState icon={Grid3X3} title="No generated targets" description="Name and generate a target above; it will appear here without being selected." />}
    <TargetSelectionDialog selection={selection} placement={placement} arrangement={arrangement} placementLocked={Boolean(selection?.selected && targetSelectionLocked)} selectionBlocked={selectionBlocked} selectionIsCurrent={selectionIsCurrent} busy={select.isPending || pendingJob !== null} onPlacementChange={setPlacement} onClose={() => setSelection(null)} onSelect={() => select.mutate()} />
    <Dialog open={deleteConfirmation !== null} onOpenChange={(open) => { if (!open && !removeBundle.isPending) setDeleteConfirmation(null) }}><DialogContent><DialogHeader><DialogTitle>Delete {deleteConfirmation?.display_name ?? "calibration target"}?</DialogTitle><DialogDescription>This permanently removes the source JSON, target specification, and printable PDF from the target library. This action cannot be undone.</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={() => setDeleteConfirmation(null)} disabled={removeBundle.isPending}>Cancel</Button><Button variant="destructive" onClick={() => deleteConfirmation && removeBundle.mutate(deleteConfirmation)} disabled={removeBundle.isPending}>{removeBundle.isPending ? <LoaderCircle className="animate-spin" /> : <Trash2 />}Confirm delete</Button></DialogFooter></DialogContent></Dialog>
  </div>
}

function TargetReuseNotice({ bundle, blockers }: { bundle: Bundle; blockers: string[] }) {
  return <Card className="border-warning/40 bg-warning/5" data-testid="calibration-target-reuse-notice">
    <CardContent className="flex items-start gap-3 py-4">
      <TriangleAlert className="mt-0.5 size-5 shrink-0 text-warning" />
      <div className="min-w-0 text-xs leading-relaxed">
        <div className="font-semibold">Target selection fixed for this run</div>
        <p className="mt-1 text-muted-foreground">
          Existing calibration evidence depends on this run-owned snapshot and placement. The saved <strong className="text-foreground">{bundle.display_name ?? bundle.target_id}</strong> target is still reusable: after moving cameras, choose a fresh run folder in the top bar, configure and capture that run, then select this same library target.
        </p>
        <details className="mt-2">
          <summary className="cursor-pointer font-medium text-foreground">Show {blockers.length} locking artifact{blockers.length === 1 ? "" : "s"}</summary>
          <ul className="mt-2 max-h-36 list-disc space-y-1 overflow-y-auto pl-5 font-mono text-[10px] text-muted-foreground">
            {blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
          </ul>
        </details>
      </div>
    </CardContent>
  </Card>
}

function TargetSelectionDialog({
  selection,
  placement,
  arrangement,
  placementLocked,
  selectionBlocked,
  selectionIsCurrent,
  busy,
  onPlacementChange,
  onClose,
  onSelect,
}: {
  selection: Bundle | null
  placement: keyof typeof placementLabels
  arrangement: CalibrationArrangement
  placementLocked: boolean
  selectionBlocked: boolean
  selectionIsCurrent: boolean
  busy: boolean
  onPlacementChange: (value: keyof typeof placementLabels) => void
  onClose: () => void
  onSelect: () => void
}) {
  return <Dialog open={selection !== null} onOpenChange={(open) => { if (!open) onClose() }}>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{selection?.selected ? "Review" : "Select"} {selection?.display_name}</DialogTitle>
        <DialogDescription>
          {selection?.selected
            ? placementLocked
              ? "This exact reusable target and placement are already bound to the run. Existing evidence locks the run-owned snapshot, but the library target remains available to every fresh calibration run."
              : "This reusable target is already bound to the run. No new selection is needed to calculate another attempt from this run."
            : "This snapshots the complete immutable bundle into the selected run. The global library target remains available for later calibration runs."}
        </DialogDescription>
      </DialogHeader>
      {arrangement.status === "ready" && arrangement.mountingFrame === "robot_flange" ? <div data-testid="static-target-mounting" className="rounded-lg border border-primary/30 bg-primary/5 p-4 text-xs">
        <div className="font-semibold">Moving calibration instrument: robot-mounted grid</div>
        <p className="mt-1 leading-relaxed text-muted-foreground">Attach the grid rigidly to the moving robot flange. Robot poses in <code>{POSE_TEMPLATE_BASE_SUNRISE_PATH}</code> provide many observations of the fixed cameras. PoseTestBot jointly estimates grid → robot_flange as supporting evidence; no measured attachment is required, and the reusable output remains camera → PoseTemplateBase rather than a hand-tracking calibration.</p>
      </div> : arrangement.status === "ready" ? <Field label="Grid placement relative to PoseTemplateBase">
        <Select disabled={placementLocked} value={placement} onValueChange={(value: keyof typeof placementLabels) => onPlacementChange(value)}>
          <SelectTrigger aria-label="Target placement"><SelectValue /></SelectTrigger>
          <SelectContent>{Object.entries(placementLabels).map(([value, label]) => <SelectItem value={value} key={value}>{label}</SelectItem>)}</SelectContent>
        </Select>
      </Field> : <CalibrationArrangementCard arrangement={arrangement} editHref="/workflow/calibration?step=configure" testId="calibration-target-dialog-arrangement" />}
      {placementLocked && <p className="rounded border border-warning/40 bg-warning/5 p-3 text-xs text-muted-foreground">Placement is fixed only for this completed run. Start a fresh run after moving cameras or the board, then select this same saved target with the placement used by the new recording.</p>}
      {selectionBlocked && arrangement.status === "ready" && <p className="rounded border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">This change would replace target-dependent evidence. Start a fresh run and select the saved target there.</p>}
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>{selectionIsCurrent ? "Close" : "Cancel"}</Button>
        {!selectionIsCurrent && <Button onClick={onSelect} disabled={busy || selectionBlocked}>{busy ? <LoaderCircle className="animate-spin" /> : null}{selection?.selected ? "Update placement" : "Select target"}</Button>}
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) { return <div className="space-y-2"><Label>{label}</Label>{children}</div> }
function NumberField({ label, help, value, onChange, min, max, step = 1 }: { label: string; help?: string; value: number; onChange: (value: number) => void; min?: number; max?: number; step?: number }) { return <Field label={<span className="inline-flex items-center gap-1">{label}{help && <HelpTip label={label}>{help}</HelpTip>}</span>}><Input aria-label={label} type="number" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /></Field> }
function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <Label className="flex items-center gap-2 rounded border p-3 text-xs"><Checkbox checked={checked} onCheckedChange={(value) => onChange(value === true)} />{label}</Label> }
function TargetLibraryPreview({ bundle }: { bundle: Bundle }) { const name = bundle.display_name ?? bundle.target_id; return <div data-testid="calibration-target-library-preview" className="surface-grid flex h-36 items-center justify-center overflow-hidden rounded-lg border bg-muted p-3 shadow-inner"><img src={`/calibration-targets/bundles/${bundle.target_id}/preview.png`} alt={`${name} calibration target preview`} decoding="async" className="block h-auto max-h-full w-auto max-w-full shadow-sm ring-1 ring-black/15" /></div> }
function DownloadLink({ bundle, artifact, icon }: { bundle: Bundle; artifact: "source" | "target" | "pdf"; icon: React.ReactNode }) { return <Button variant="outline" size="sm" asChild><a href={`/calibration-targets/bundles/${bundle.target_id}/download/${artifact}`} download>{icon}{artifact.toUpperCase()}<Download /></a></Button> }
