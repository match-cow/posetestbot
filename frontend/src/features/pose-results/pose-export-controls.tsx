import { useEffect, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { Link, useSearchParams } from "react-router-dom"
import { Download } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { StatusBadge } from "@/components/status-badge"
import { api, errorMessage, query } from "@/lib/api"
import { jobStatusTone } from "@/lib/jobs"
import type { BopInspectionFrameFilter, BopInspectionSetup } from "@/lib/contracts"
import type { PoseOverlayLayers } from "./pose-overlay"

export interface MaskLayers {
  full: boolean
  visible: boolean
  fullOpacity: number
  visibleOpacity: number
}

interface ExportSelection {
  result_id: string
  scene_id: number
  filter: BopInspectionFrameFilter
  object_id: number | null
  background: "rgb" | "depth"
  geometry: PoseOverlayLayers
  masks: MaskLayers
  max_hypotheses: number
}

interface ExportStatus {
  export_id: string
  settings: ExportSelection & { format: "zip" | "mp4"; fps: number }
  scene_name: string
  frame_count: number
  job_id: string
  job_state: string
  progress: { state: string; completed_frames: number; total_frames: number }
  error: string | null
  download_available: boolean
  download_url: string | null
}

const ACTIVE = new Set(["queued", "running", "canceling"])

export function PoseExportControls({ runRoot, selection, sceneName, frameCount, availability, autoDownloadRef }: {
  runRoot: string
  selection: ExportSelection | null
  sceneName?: string
  frameCount?: number
  availability?: BopInspectionSetup["exports"]
  autoDownloadRef: { current: string | null }
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const exportId = searchParams.get("export_id")
  const [fps, setFps] = useState("30")
  const validFps = /^\d+$/.test(fps) && Number(fps) >= 1 && Number(fps) <= 120
  const status = useQuery({
    queryKey: ["bop-inspection-export", runRoot, exportId],
    queryFn: () => api<ExportStatus>(query(`/bop/inspection/exports/${exportId}`, { run_root: runRoot })),
    enabled: Boolean(exportId),
    retry: false,
    refetchInterval: (current) => current.state.error || current.state.data && !ACTIVE.has(current.state.data.job_state) ? false : 1000,
  })
  const submit = useMutation({
    onMutate: () => ({ originUrl: window.location.href }),
    mutationFn: (snapshot: ExportSelection & { format: "zip" | "mp4"; fps: number; run_root: string }) => api<{ export_id: string; job_id: string }>("/bop/inspection/exports", { method: "POST", body: JSON.stringify(snapshot) }),
    onSuccess: (response, snapshot, context) => {
      // The job survives navigation, including a pending lazy route transition.
      // Attach its download only while the submitted view is still current.
      if (window.location.href !== context?.originUrl) return
      autoDownloadRef.current = response.export_id
      setSearchParams((current) => {
        const next = new URLSearchParams(current)
        next.set("run_root", snapshot.run_root)
        next.set("export_id", response.export_id)
        return next
      }, { replace: true })
    },
  })
  useEffect(() => {
    const current = status.data
    if (!current?.download_available || !current.download_url || autoDownloadRef.current !== current.export_id) return
    autoDownloadRef.current = null
    const link = document.createElement("a")
    link.href = current.download_url
    document.body.appendChild(link)
    link.click()
    link.remove()
  }, [status.data, autoDownloadRef])
  const busy = submit.isPending || Boolean(exportId && (status.isPending || ACTIVE.has(status.data?.job_state ?? "")))
  const disabled = !selection || !frameCount || busy
  const start = (format: "zip" | "mp4") => {
    if (!selection) return
    submit.mutate(structuredClone({ ...selection, format, fps: validFps ? Number(fps) : 30, run_root: runRoot }))
  }
  const saved = status.data
  return <Card data-testid="pose-export-controls">
    <CardHeader className="pb-3"><CardTitle className="text-base">Export scene images & video</CardTitle><CardDescription>Original-resolution images with the selected layers, without controls or frame labels.</CardDescription></CardHeader>
    <CardContent className="space-y-3 text-xs">
      <p>{selection ? `${sceneName} · scene ${selection.scene_id} · ${frameCount?.toLocaleString() ?? "…"} matching frames across all pages` : "Select a compatible result and scene to create an export."}</p>
      <div className="flex items-end gap-4">
        <div className="w-24 space-y-1"><Label htmlFor="pose-export-fps">Video FPS</Label><Input id="pose-export-fps" type="number" min={1} max={120} step={1} value={fps} onChange={(event) => setFps(event.target.value)} aria-invalid={!validFps} /></div>
        <div className="pb-2">Video duration <strong data-testid="pose-export-duration">{validFps && frameCount != null ? `${(frameCount / Number(fps)).toFixed(2)} s` : "—"}</strong></div>
      </div>
      {!validFps && <p className="text-destructive">FPS must be an integer from 1 to 120.</p>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => start("zip")}><Download />Download images (.zip)</Button>
        <Button size="sm" disabled={disabled || !validFps || !availability?.mp4.available} onClick={() => start("mp4")}><Download />Create & download MP4</Button>
      </div>
      {!availability?.mp4.available && <p className="text-muted-foreground">{availability?.mp4.reason ?? "MP4 availability has not been verified."}</p>}
      {selection && frameCount === 0 && <p>No frames match this selection. Change the frame or object filter.</p>}
      <p className="text-muted-foreground">Settings are saved when clicked. Work continues after navigation. <Link className="font-semibold text-primary underline" to="/jobs">Open Jobs</Link> to monitor or cancel.</p>
      {submit.isError && <p role="alert" className="text-destructive">{errorMessage(submit.error)}</p>}
      {exportId && <div className="space-y-2 rounded border p-3" data-testid="pose-export-progress" aria-live="polite">
        {status.isError ? <p role="alert" className="text-destructive">{errorMessage(status.error)}</p> : saved ? <>
          <div className="flex items-center justify-between gap-2"><span className="font-semibold">Saved {saved.settings.format.toUpperCase()} export</span><StatusBadge status={saved.job_state} tone={jobStatusTone(saved.job_state)} /></div>
          <p>{saved.scene_name} · scene {saved.settings.scene_id} · {saved.frame_count.toLocaleString()} frames · {saved.settings.background.toUpperCase()}{saved.settings.format === "mp4" && ` · ${saved.settings.fps} FPS`}</p>
          <p>{saved.progress.state} · {saved.progress.completed_frames} / {saved.progress.total_frames} frames</p>
          <progress className="w-full" aria-label="Export rendering progress" value={saved.progress.completed_frames} max={Math.max(1, saved.progress.total_frames)} />
          {saved.error && <p role="alert" className="text-destructive">{saved.error}</p>}
          <details><summary className="cursor-pointer text-muted-foreground">Saved export settings</summary><pre className="mt-2 max-h-48 overflow-auto text-[10px]">{JSON.stringify(saved.settings, null, 2)}</pre></details>
          {saved.download_available && saved.download_url && <Button asChild size="sm" variant="outline"><a href={saved.download_url}><Download />Download completed {saved.settings.format.toUpperCase()}</a></Button>}
        </> : <p>Loading saved export…</p>}
      </div>}
    </CardContent>
  </Card>
}
