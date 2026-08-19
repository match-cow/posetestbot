import { useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Camera, Eye, EyeOff, Info, RefreshCw, Save, Webcam } from "lucide-react"
import { toast } from "sonner"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage } from "@/lib/api"
import type { PreviewJob, SensorDevice, SensorStatus } from "@/lib/contracts"
import { loadSelectedSensorKeys, saveSelectedSensorKeys } from "@/lib/sensor-selection"
import { useOperator } from "@/providers/operator-provider"

const PREVIEW_ON = new Set(["queued", "running"])
const PREVIEW_BUSY = new Set(["queued", "running", "canceling"])
const sensorKey = (device: SensorDevice) => `${device.sensor_type}:${device.device_id}`
const isCaptureReady = (device: SensorDevice) => device.connected !== false && device.capture_ready !== false

function captureReadinessMessage(device: SensorDevice): string | null {
  if (device.connected === false) return "The camera is disconnected."
  if (device.capture_ready !== false) return null
  const reason = device.capture_readiness_reason?.trim()
  if (reason === "not_enumerated_by_sdk") return "Visible on USB, but unavailable to the camera SDK."
  if (reason === "usb_connection_below_superspeed") return "The USB connection is below SuperSpeed (USB 3). Check the cable and port, then refresh discovery."
  if (reason === "sdk_unavailable") return "The camera SDK is unavailable."
  return reason ? reason.replaceAll("_", " ") : "Sensor status reports that this camera cannot be opened for capture."
}

type MountingMode = "eye_in_hand" | "static"
const UNCONFIGURED_MOUNTING = "unconfigured"
interface AliasRecord { alias: string; mounting_mode?: MountingMode; inverted?: boolean }
interface AliasState { aliases: Record<string, AliasRecord>; path?: string; error?: string | null }
type AliasDraft = Partial<AliasRecord>
type AliasSaveReason = "alias" | "mounting" | "orientation"
interface SaveAliasRequest {
  records: Record<string, AliasRecord>
  key: string
  patch: AliasDraft
  reason: AliasSaveReason
}
interface SnapshotState {
  job: { status: string }
  manifest: { sensors?: Array<{ sensor_key: string; status: string; rgb_thumbnail?: string | null; error?: string | null }> } | null
}

function Preview({ preview }: { preview?: PreviewJob }) {
  if (!preview) return <div className="grid aspect-video place-items-center rounded-lg bg-muted text-xs text-muted-foreground">Preview is off</div>
  const status = preview.preview_status
  const hasLiveFrame = PREVIEW_ON.has(preview.job.status) && status?.status === "running" && Boolean(status.latest_image)
  const source = status?.selected_node?.path ?? status?.selected_node?.device_id ?? ""
  return (
    <div data-testid="sensor-preview-slot" className="relative isolate aspect-video w-full min-w-0 overflow-hidden rounded-lg bg-muted" style={{ contain: "layout paint" }}>
      {hasLiveFrame ? <img data-testid="sensor-preview-image" src={`/sensors/previews/${preview.job.id}/latest.jpg?t=${status?.frame_count}`} className="absolute inset-0 size-full object-contain" alt="Live sensor preview" /> : <div className="sensor-preview-empty absolute inset-0 grid place-items-center px-4 text-center text-xs text-muted-foreground">{status?.error ? <span data-testid="sensor-preview-error" className="max-w-full break-words text-destructive">{status.error}</span> : preview.job.status === "canceling" ? "Stopping preview…" : "Waiting for first frame…"}</div>}
      <div data-testid="sensor-preview-meta" className="absolute inset-x-2 bottom-2 z-10 flex min-w-0 items-center justify-between gap-2 rounded bg-black/65 px-2 py-1 text-[10px] text-white"><span className="shrink-0">{status?.status ?? preview.job.status}</span><span className="min-w-0 truncate">{String(source)}</span></div>
    </div>
  )
}

export function DevicesPage() {
  const queryClient = useQueryClient()
  const { currentWorkflow } = useOperator()
  const [aliasDraft, setAliasDraft] = useState<Record<string, AliasDraft>>({})
  const [selected, setSelected] = useState<Set<string>>(loadSelectedSensorKeys)
  const [detail, setDetail] = useState<SensorDevice | null>(null)
  const [snapshotJobs, setSnapshotJobs] = useState<Record<string, string>>({})

  const status = useQuery({ queryKey: ["sensors", "status"], queryFn: () => api<SensorStatus>("/sensors/status"), refetchInterval: 10_000 })
  const aliases = useQuery({ queryKey: ["sensors", "aliases"], queryFn: () => api<AliasState>("/sensors/aliases") })
  const previews = useQuery({ queryKey: ["sensors", "previews"], queryFn: () => api<{ jobs: PreviewJob[] }>("/sensors/previews?include_terminal=true"), refetchInterval: 1_000 })
  const devices = useMemo(() => status.data?.families.flatMap((family) => family.devices) ?? [], [status.data])
  const captureReadyCount = useMemo(() => devices.filter(isCaptureReady).length, [devices])
  const previewByKey = useMemo(() => {
    const byKey = new Map<string, PreviewJob>()
    for (const item of previews.data?.jobs ?? []) {
      const key = String(item.job.parameters.sensor_key)
      const current = byKey.get(key)
      if (!current || (PREVIEW_BUSY.has(item.job.status) && !PREVIEW_BUSY.has(current.job.status))) {
        byKey.set(key, item)
      }
    }
    return byKey
  }, [previews.data])
  const snapshotStates = useQuery<Record<string, SnapshotState>>({
    queryKey: ["sensors", "snapshots", snapshotJobs],
    enabled: Object.keys(snapshotJobs).length > 0,
    queryFn: async () => Object.fromEntries(await Promise.all(Object.entries(snapshotJobs).map(async ([key, jobId]) => [key, await api<SnapshotState>(`/sensors/snapshots/${jobId}`)]))) as Record<string, SnapshotState>,
    refetchInterval: (queryState) => Object.values(queryState.state.data ?? {}).some((item) => PREVIEW_BUSY.has(item.job.status)) ? 1_000 : false,
  })

  const startPreview = useMutation({
    mutationFn: (device: SensorDevice) => api("/sensors/previews", { method: "POST", body: JSON.stringify({ sensors: [device] }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sensors", "previews"] }),
    onError: (error) => toast.error("Preview could not start", { description: errorMessage(error) }),
  })
  const stopPreview = useMutation({
    mutationFn: (jobId: string) => api(`/sensors/previews/${jobId}/stop`, { method: "POST", body: "{}" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sensors", "previews"] }),
    onError: (error) => toast.error("Preview could not stop", { description: errorMessage(error) }),
  })
  const stopAllPreviews = useMutation({
    mutationFn: () => api<{ jobs: PreviewJob[] }>("/sensors/previews/stop", { method: "POST", body: "{}" }),
    onSuccess: (data) => { toast.success(data.jobs.length ? `Stopping ${data.jobs.length} preview${data.jobs.length === 1 ? "" : "s"}` : "All previews are already off"); queryClient.invalidateQueries({ queryKey: ["sensors", "previews"] }) },
    onError: (error) => toast.error("Previews could not be stopped", { description: errorMessage(error) }),
  })
  const snapshot = useMutation({
    mutationFn: (device: SensorDevice) => api<{ job_id: string }>("/sensors/snapshots", { method: "POST", body: JSON.stringify({ sensors: [device], max_frames: 1 }) }),
    onSuccess: (data, device) => { setSnapshotJobs((current) => ({ ...current, [sensorKey(device)]: data.job_id })); toast.success("Snapshot queued", { description: `Job ${data.job_id}` }); queryClient.invalidateQueries({ queryKey: ["jobs"] }) },
    onError: (error) => toast.error("Snapshot could not be queued", { description: errorMessage(error) }),
  })
  const saveAliases = useMutation({
    mutationFn: ({ records }: SaveAliasRequest) => api<AliasState>("/sensors/aliases", { method: "PUT", body: JSON.stringify({ aliases: records }) }),
    onSuccess: (data, request) => {
      setAliasDraft((current) => {
        const draft = current[request.key]
        if (!draft) return current
        const nextDraft = { ...draft }
        for (const [field, submittedValue] of Object.entries(request.patch) as Array<[keyof AliasRecord, string | boolean | undefined]>) {
          if (nextDraft[field] === submittedValue) delete nextDraft[field]
        }
        const next = { ...current }
        if (Object.keys(nextDraft).length === 0) delete next[request.key]
        else next[request.key] = nextDraft
        return next
      })
      queryClient.setQueryData(["sensors", "aliases"], data)
      void queryClient.invalidateQueries({ queryKey: ["sensors", "status"] })
      const successMessage: Record<AliasSaveReason, string> = {
        alias: "Alias default saved",
        mounting: "Mounting default saved",
        orientation: "Orientation default saved",
      }
      toast.success(successMessage[request.reason])
    },
    onError: (error, request) => {
      if (request.reason !== "alias") {
        setAliasDraft((current) => {
          const draft = current[request.key]
          if (!draft) return current
          const nextDraft = { ...draft }
          for (const [field, submittedValue] of Object.entries(request.patch) as Array<[keyof AliasRecord, string | boolean | undefined]>) {
            if (nextDraft[field] === submittedValue) delete nextDraft[field]
          }
          const next = { ...current }
          if (Object.keys(nextDraft).length === 0) delete next[request.key]
          else next[request.key] = nextDraft
          return next
        })
      }
      toast.error("Device default could not be saved", { description: errorMessage(error) })
    },
  })

  const defaultAliasRecord = (device: SensorDevice): AliasRecord => {
    const mountingMode = device.mounting_mode === "eye_in_hand" || device.mounting_mode === "static"
      ? device.mounting_mode
      : undefined
    return {
      alias: device.effective_display_name ?? device.display_name ?? "",
      ...(mountingMode ? { mounting_mode: mountingMode } : {}),
      inverted: Boolean(device.inverted),
    }
  }
  const savedAliasRecord = (device: SensorDevice): AliasRecord => aliases.data?.aliases[sensorKey(device)] ?? defaultAliasRecord(device)
  const displayedAliasRecord = (device: SensorDevice): AliasRecord => ({
    ...savedAliasRecord(device),
    ...aliasDraft[sensorKey(device)],
  })
  const updateAliasDraft = (device: SensorDevice, alias: string) => {
    const key = sensorKey(device)
    setAliasDraft((current) => {
      const nextDraft = { ...current[key] }
      if (alias === savedAliasRecord(device).alias) delete nextDraft.alias
      else nextDraft.alias = alias
      const next = { ...current }
      if (Object.keys(nextDraft).length === 0) delete next[key]
      else next[key] = nextDraft
      return next
    })
  }
  const aliasRecords = () => {
    const records: Record<string, AliasRecord> = {
      ...(aliases.data?.aliases ?? {}),
    }
    for (const device of devices) {
      const key = sensorKey(device)
      records[key] = aliases.data?.aliases[key] ?? defaultAliasRecord(device)
    }
    return records
  }
  const persistDefault = async (device: SensorDevice, patch: AliasDraft, reason: AliasSaveReason) => {
    const key = sensorKey(device)
    setAliasDraft((current) => ({ ...current, [key]: { ...current[key], ...patch } }))
    const records = aliasRecords()
    records[key] = { ...savedAliasRecord(device), ...patch }
    return saveAliases.mutateAsync({ records, key, patch, reason })
  }
  const updateMountingMode = (device: SensorDevice, mountingMode: MountingMode) => {
    void persistDefault(device, { mounting_mode: mountingMode }, "mounting").catch(() => undefined)
  }
  const updateOrientation = async (device: SensorDevice, inverted: boolean, preview?: PreviewJob) => {
    try {
      await persistDefault(device, { inverted }, "orientation")
    } catch {
      return
    }
    if (!preview || !PREVIEW_ON.has(preview.job.status)) return
    try {
      await api(`/sensors/previews/${preview.job.id}/stop`, { method: "POST", body: "{}" })
      await api("/sensors/previews", { method: "POST", body: JSON.stringify({ sensors: [{ ...device, inverted }] }) })
      await queryClient.invalidateQueries({ queryKey: ["sensors", "previews"] })
    } catch (error) { toast.error("Preview could not restart", { description: errorMessage(error) }) }
  }
  const toggleSelected = (key: string, checked: boolean, captureReady: boolean) => {
    if (checked && !captureReady) return
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(key)
      else next.delete(key)
      saveSelectedSensorKeys(next)
      return next
    })
  }
  const previewTransitionPending = startPreview.isPending || stopPreview.isPending
  const anyPreviewBusy = [...previewByKey.values()].some((item) => PREVIEW_BUSY.has(item.job.status))
  const workflowHref = currentWorkflow ? `/workflow/${currentWorkflow.journey}?step=configure` : "/workflow/setup"
  const workflowAction = currentWorkflow ? "Open workflow step 1" : "Choose a workflow"
  const refreshDiscovery = async () => {
    const result = await status.refetch()
    if (result.error) toast.error("Sensor discovery failed", { description: errorMessage(result.error) })
  }

  return (
    <div className="space-y-6">
      <PageHeader eyebrow="Lab hardware" title="Devices" description="Discover and preview cameras, then maintain reusable lab defaults for future runs. Existing runs are edited in Workflow step 1." actions={<Button variant="outline" onClick={() => void refreshDiscovery()} disabled={status.isFetching}><RefreshCw className={status.isFetching ? "animate-spin" : ""} />Refresh discovery</Button>} />
      <ProcessHandoff
        title="Defaults here; active-run settings in Workflow"
        description="Alias, mounting, and orientation here are reusable lab defaults that seed new runs. The next-run checkboxes are browser-local only. Workflow step 1 owns the durable alias, mounting, orientation, and membership for the selected run; later default changes never rewrite it."
        to={workflowHref}
        action={workflowAction}
      />

      {(aliases.isError || aliases.data?.error) && <div className="flex items-start justify-between gap-4 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs" role="alert"><div><div className="font-semibold text-destructive">Reusable device defaults could not be loaded</div><p className="mt-1 text-muted-foreground">{aliases.data?.error ?? errorMessage(aliases.error)} Editing is disabled so retained defaults are not overwritten.</p></div><Button variant="outline" size="sm" onClick={() => void aliases.refetch()} disabled={aliases.isFetching}><RefreshCw className={aliases.isFetching ? "animate-spin" : ""} />Retry defaults</Button></div>}

      <div className="flex items-center justify-between"><div><h2 className="font-display text-xl font-semibold">RGB-D sensor lab defaults</h2><p className="text-sm text-muted-foreground">{captureReadyCount} capture-ready · {status.data?.total_connected ?? 0} connected · {selected.size} in the next-run browser draft</p></div><Button variant="outline" size="sm" onClick={() => stopAllPreviews.mutate()} disabled={!anyPreviewBusy || stopAllPreviews.isPending}><EyeOff />{stopAllPreviews.isPending ? "Stopping previews…" : "Stop all previews"}</Button></div>
      {status.isPending ? <div className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-3">{Array.from({ length: 3 }).map((_, index) => <Skeleton className="h-[430px]" key={index} />)}</div> : devices.length === 0 ? <div className="rounded-xl border border-dashed p-10 text-center text-sm text-muted-foreground">No RGB-D sensors were detected. Check SDKs, USB connections, and permissions, then refresh.</div> : <div data-testid="sensor-grid" className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-3">
        {devices.map((device) => {
          const key = sensorKey(device)
          const alias = displayedAliasRecord(device)
          const aliasDirty = aliasDraft[key]?.alias !== undefined && aliasDraft[key]?.alias !== savedAliasRecord(device).alias
          const defaultControlsDisabled = saveAliases.isPending || aliases.isPending || aliases.isError || Boolean(aliases.data?.error)
          const preview = previewByKey.get(key)
          const previewOn = Boolean(preview && PREVIEW_ON.has(preview.job.status))
          const previewBusy = Boolean(preview && PREVIEW_BUSY.has(preview.job.status))
          const previewStopping = preview?.job.status === "canceling"
          const previewSupported = device.live_rgb_preview_supported ?? device.sensor_type !== "zed_2i"
          const captureReady = isCaptureReady(device)
          const readinessMessage = captureReadinessMessage(device)
          const selectedForRun = selected.has(key)
          const snapshotState = snapshotStates.data?.[key]
          const snapshotRecord = snapshotState?.manifest?.sensors?.find((item) => item.sensor_key === key)
          const snapshotJobId = snapshotJobs[key]
          const controlReasonId = `sensor-controls-disabled-${key.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`
          const persistentControlReasons = [
            device.sensor_type !== "realsense_d435" && "Image-orientation override is available only for RealSense D435 cameras.",
            !previewSupported && "Live RGB preview is unavailable for this sensor family; use Snapshot when the camera is capture-ready.",
            previewBusy && "Stop the active live preview before taking a snapshot.",
          ].filter(Boolean) as string[]
          return <Card data-testid="sensor-card" data-sensor-key={key} data-capture-ready={captureReady ? "true" : "false"} key={key} className="min-w-0 overflow-hidden">
            <CardHeader className="pb-3"><div className="flex items-start justify-between gap-3"><div className="flex min-w-0 items-center gap-3"><div className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted"><Webcam className="size-4 text-primary-strong" /></div><div className="min-w-0"><CardTitle className="truncate text-base">{alias?.alias || device.effective_display_name || device.display_name || device.device_id}</CardTitle><CardDescription className="truncate">{device.sensor_type.replaceAll("_", " ")} · {device.device_id}</CardDescription></div></div><StatusBadge status={device.connected === false ? "disconnected" : captureReady ? "connected" : "warning"} tone={device.connected === false ? "destructive" : captureReady ? "informational" : "warning"}>{device.connected === false ? "Disconnected" : captureReady ? "Capture-ready" : "Not capture-ready"}</StatusBadge></div></CardHeader>
            <CardContent className="space-y-4">
              {readinessMessage && <div data-testid="sensor-capture-readiness" className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs leading-relaxed"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" /><span><span className="font-semibold">Capture unavailable.</span> {readinessMessage}</span></div>}
              <Preview preview={previewBusy || preview?.job.status === "failed" ? preview : undefined} />
              {snapshotJobId && <div data-testid="sensor-snapshot" className="overflow-hidden rounded-lg border border-border bg-muted/20">{snapshotRecord?.rgb_thumbnail ? <img src={`/sensors/snapshots/${snapshotJobId}/image?path=${encodeURIComponent(snapshotRecord.rgb_thumbnail)}`} className="aspect-video w-full object-cover" alt="Latest sensor snapshot" /> : <div className="grid h-16 place-items-center px-3 text-center text-xs text-muted-foreground">{snapshotRecord?.error ?? (PREVIEW_BUSY.has(snapshotState?.job.status ?? "queued") ? "Capturing snapshot…" : "Snapshot did not produce an image")}</div>}</div>}
              <div className="space-y-2"><Label htmlFor={`alias-${key}`}>Default operator alias</Label><div className="flex gap-2"><Input id={`alias-${key}`} value={alias.alias} onChange={(event) => updateAliasDraft(device, event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && aliasDirty && !defaultControlsDisabled) { event.preventDefault(); void persistDefault(device, { alias: alias.alias }, "alias").catch(() => undefined) } }} disabled={defaultControlsDisabled} /><Button variant="outline" onClick={() => void persistDefault(device, { alias: alias.alias }, "alias").catch(() => undefined)} disabled={defaultControlsDisabled || !aliasDirty} aria-label={`Save alias for ${key}`}><Save />Save alias</Button></div><p className={`text-[10px] leading-relaxed ${aliasDirty ? "font-medium text-warning" : "text-muted-foreground"}`}>{aliasDirty ? "Unsaved alias draft. Save it here before leaving." : "Saved lab default; it seeds new runs only. Existing runs keep their saved alias."}</p></div>
              <div className="grid grid-cols-2 gap-3"><div className="space-y-2"><Label className="flex items-center gap-1" htmlFor={`mounting-${key}`}>Mounting default <HelpTip label="camera mounting mode">Robot-mounted cameras move rigidly with the flange. Static cameras remain fixed in the workcell; their calibration uses a moving robot-mounted grid to publish camera → PoseTemplateBase, not to track the hand.</HelpTip></Label><Select value={alias.mounting_mode ?? UNCONFIGURED_MOUNTING} onValueChange={(value) => { if (value !== UNCONFIGURED_MOUNTING) updateMountingMode(device, value as MountingMode) }} disabled={defaultControlsDisabled}><SelectTrigger id={`mounting-${key}`} aria-label={`Mounting default for ${key}`} data-testid="sensor-mounting-mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={UNCONFIGURED_MOUNTING} disabled>Mounting not configured</SelectItem><SelectItem value="eye_in_hand">Robot-mounted</SelectItem><SelectItem value="static">Static</SelectItem></SelectContent></Select><p data-testid={!alias.mounting_mode ? "sensor-mounting-required" : undefined} className={`text-[10px] leading-relaxed ${alias.mounting_mode ? "text-muted-foreground" : "font-medium text-destructive"}`}>{alias.mounting_mode ? "Saved immediately as a new-run default. Existing runs change only in Workflow step 1." : "Required before this camera can seed a valid new run. Choose its physical mount; PoseTestBot will not assume Robot-mounted."}</p></div><div className="space-y-2"><Label className="flex items-center gap-1" htmlFor={`orientation-${key}`}>Orientation default <HelpTip label="camera image orientation">Inverted rotates supported camera output by 180° so saved RGB and aligned depth share the physical mounting orientation.</HelpTip></Label><Select value={alias.inverted ? "inverted" : "normal"} onValueChange={(value) => void updateOrientation(device, value === "inverted", preview)} disabled={defaultControlsDisabled || device.sensor_type !== "realsense_d435" || previewStopping || previewTransitionPending}><SelectTrigger id={`orientation-${key}`} data-testid="sensor-orientation" aria-label={`Orientation default for ${key}`} aria-describedby={persistentControlReasons.length ? controlReasonId : undefined}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="normal">Normal</SelectItem><SelectItem value="inverted">Inverted</SelectItem></SelectContent></Select><p className="text-[10px] leading-relaxed text-muted-foreground">Saved immediately as a new-run default. Active previews restart with it.</p></div></div>
              <div className="flex items-center justify-between rounded-lg bg-muted/55 px-3 py-2"><div><div className="text-[9px] font-semibold uppercase tracking-wide text-muted-foreground">Next-run browser draft</div><div className="mt-1 flex items-center gap-1"><Label className={`flex items-center gap-2 ${!captureReady && !selectedForRun ? "cursor-not-allowed text-muted-foreground" : ""}`} title={!captureReady && !selectedForRun ? readinessMessage ?? "Camera is not capture-ready" : undefined}><Checkbox data-testid="sensor-run-selection" checked={selectedForRun} disabled={!captureReady && !selectedForRun} onCheckedChange={(value) => toggleSelected(key, value === true, captureReady)} />Include in next run</Label><HelpTip label="run camera draft">This browser-local selection prefills a new run setup. Save Workflow step 1 to change the selected run’s durable camera membership.</HelpTip></div></div><Button variant="ghost" size="sm" onClick={() => setDetail(device)}><Info />Details</Button></div>
              <div className="grid grid-cols-2 gap-2"><Button data-testid="sensor-preview-toggle" aria-label={`Toggle preview for ${alias?.alias || device.effective_display_name || device.display_name || device.device_id}`} aria-pressed={previewOn} aria-describedby={persistentControlReasons.length ? controlReasonId : undefined} variant={previewOn ? "secondary" : "outline"} disabled={!previewSupported || previewStopping || previewTransitionPending || (!captureReady && !previewOn)} title={!captureReady ? readinessMessage ?? "Camera is not capture-ready" : previewSupported ? undefined : "Live RGB preview is unavailable for this sensor family"} onClick={() => previewOn && preview ? stopPreview.mutate(preview.job.id) : startPreview.mutate(device)}>{!captureReady && !previewOn ? <><EyeOff />Not ready</> : !previewSupported ? <><EyeOff />Unavailable</> : previewStopping ? <><EyeOff />Stopping…</> : previewOn ? <><Eye />Preview on</> : <><EyeOff />Preview off</>}</Button><Button variant="outline" aria-describedby={persistentControlReasons.length ? controlReasonId : undefined} title={!captureReady ? readinessMessage ?? "Camera is not capture-ready" : previewBusy ? "Turn this preview off before taking a snapshot" : undefined} onClick={() => snapshot.mutate(device)} disabled={!captureReady || previewBusy || snapshot.isPending || PREVIEW_BUSY.has(snapshotState?.job.status ?? "")}><Camera />{PREVIEW_BUSY.has(snapshotState?.job.status ?? "") ? "Capturing…" : "Snapshot"}</Button></div>
              {persistentControlReasons.length > 0 && <div id={controlReasonId} data-testid="sensor-disabled-action-reason" className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[10px] leading-relaxed text-muted-foreground">{persistentControlReasons.map((reason) => <p key={reason}>{reason}</p>)}</div>}
            </CardContent>
          </Card>
        })}
      </div>}

      <Sheet open={Boolean(detail)} onOpenChange={(open) => !open && setDetail(null)}><SheetContent><SheetHeader><SheetTitle className="font-display text-xl font-semibold">Raw sensor metadata</SheetTitle><SheetDescription>Discovery detail for troubleshooting. Routine controls stay on the device card.</SheetDescription></SheetHeader><pre className="mt-4 flex-1 overflow-auto rounded-lg bg-muted p-4 text-xs leading-relaxed">{JSON.stringify(detail, null, 2)}</pre></SheetContent></Sheet>

    </div>
  )
}
