import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Download, Film, Loader2 } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError, api, errorMessage, query } from "@/lib/api"
import type { DepthKind, DepthVideoChoices, DepthVideoSubmission } from "./contracts"

const views = { overview: "RGB, depth, reference + error", measured: "Measured depth", error: "Signed error", missing: "Missing returns" }
const duration = (seconds: number) => seconds < 1 ? `${seconds.toFixed(2)}s` : `${Math.floor(Math.round(seconds) / 60)}:${(Math.round(seconds) % 60).toString().padStart(2, "0")}`
const videoError = (error: unknown) => error instanceof ApiError && error.status === 404
  ? "Video options are unavailable in the running console. Use the Restart menu to restart the backend, then retry."
  : errorMessage(error)

export function DepthVideos({ run, kind, id, data, error, offset, setOffset, onRetry, retrying }: {
  run: string; kind: DepthKind; id: string
  data?: DepthVideoChoices; error: unknown; offset: number; setOffset: (offset: number) => void
  onRetry: () => void; retrying: boolean
}) {
  const cache = useQueryClient()
  const [seriesIndex, setSeriesIndex] = useState(0)
  const [view, setView] = useState<DepthVideoSubmission["view"]>("overview")
  const [fps, setFps] = useState(15)
  const [stride, setStride] = useState(1)
  const [width, setWidth] = useState<DepthVideoSubmission["width"]>(1280)
  const [videoId, setVideoId] = useState("")
  const series = data?.series[seriesIndex] ?? data?.series[0]
  const video = data?.records.find(r => r.id === videoId) ?? data?.records[0]
  const count = series && Number.isInteger(stride) && stride > 0 ? Math.ceil(series.frame_count / stride) : 0
  const valid = Number.isInteger(fps) && fps >= 1 && fps <= 60 && Number.isInteger(stride) && stride >= 1 && stride <= 120
  const create = useMutation({
    mutationFn: () => {
      if (!series) throw new Error("Choose a camera series")
      const value: DepthVideoSubmission = {
        schema_version: "depth_video_submission.v1", run_root: run, source_kind: kind, source_id: id,
        scene_id: series.scene_id, input_id: series.input_id, view, fps, stride, width,
      }
      return api<{ id: string; job_id: string }>("/depth/videos", { method: "POST", body: JSON.stringify(value) })
    },
    onSuccess: result => {
      setVideoId(result.id)
      setOffset(0)
      void cache.invalidateQueries({ queryKey: ["depth-video-choices", run, kind, id] })
      toast.success("Quality video queued", { description: "Work continues after navigation. Open Jobs for progress and cancellation." })
    },
    onError: e => toast.error(videoError(e)),
  })
  const url = (suffix: string) => query(`/depth/videos/${video?.id}/${suffix}`, { run_root: run })
  const heading = <div>
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Film className="size-4 text-primary-strong" />Quality videos</h3>
      <p className="mt-1 text-xs text-muted-foreground">Review one camera through the finished run. Export an MP4 to share or keep with your study.</p>
    </div>
  if (!data) return <section className="space-y-4" data-testid="depth-videos">
    {heading}
    {error ? <div className="space-y-3 rounded-lg border border-warning/30 p-4">
      <p role="alert" className="text-xs">{videoError(error)}</p>
      <Button variant="outline" size="sm" disabled={retrying} onClick={onRetry}>{retrying && <Loader2 className="size-4 animate-spin" />}Retry video options</Button>
    </div> : <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-4 animate-spin" />Loading camera and video options…</p>}
  </section>
  return <section className="space-y-5" data-testid="depth-videos">
    {heading}
    {Boolean(error) && <p role="alert" className="text-xs">{videoError(error)}</p>}
    <details open={!video} className="rounded-lg border bg-muted/20">
      <summary className="cursor-pointer px-4 py-3 text-xs font-semibold">{video ? "Generate another video" : "Video settings"}</summary>
      <div className="space-y-4 border-t p-4">
      <div className="space-y-1.5"><Label htmlFor="depth-video-series">Camera / finish</Label>
        <select id="depth-video-series" className="depth-select w-full" value={seriesIndex} onChange={e => setSeriesIndex(Number(e.target.value))}>
          {data?.series.map((s, i) => <option key={`${s.input_id}:${s.scene_id}`} value={i}>{s.sensor_name} · {s.finish_label} · {s.frame_count.toLocaleString()} frames</option>)}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-3 2xl:grid-cols-[minmax(0,2fr)_1fr_1fr_1fr]">
        <div className="space-y-1.5"><Label htmlFor="depth-video-view">Video view</Label><select id="depth-video-view" className="depth-select w-full" value={view} onChange={e => setView(e.target.value as typeof view)}>{Object.entries(views).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></div>
        <div className="space-y-1.5"><Label htmlFor="depth-video-fps">Frames / second</Label><Input id="depth-video-fps" type="number" min={1} max={60} step={1} value={Number.isNaN(fps) ? "" : fps} onChange={e => setFps(e.target.valueAsNumber)} /></div>
        <div className="space-y-1.5"><Label htmlFor="depth-video-stride">Every nth frame</Label><Input id="depth-video-stride" type="number" min={1} max={120} step={1} value={Number.isNaN(stride) ? "" : stride} onChange={e => setStride(e.target.valueAsNumber)} /></div>
        <div className="space-y-1.5"><Label htmlFor="depth-video-width">Width</Label><select id="depth-video-width" className="depth-select w-full" value={width} onChange={e => setWidth(Number(e.target.value) as typeof width)}>{[960, 1280, 1920].map(w => <option key={w} value={w}>{w} px</option>)}</select></div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-xs text-muted-foreground">{valid ? `${count.toLocaleString()} frames · ${duration(count / fps)} at ${fps} fps` : "Use whole numbers: 1–60 fps and a frame step of 1–120."}<br />Fixed scales and interior completeness; constant-rate playback.</div>
        <Button disabled={!series || !valid || !data?.encoder.available || create.isPending} onClick={() => create.mutate()}>{create.isPending ? <Loader2 className="size-4 animate-spin" /> : <Film className="size-4" />}Generate MP4</Button>
      </div>
      {data && !data.encoder.available && <p role="status" className="text-xs text-warning-foreground">{data.encoder.reason}</p>}
      <p className="text-xs text-muted-foreground">Video generation continues after navigation. <Link className="underline" to="/jobs">Open Jobs</Link> for logs and cancellation.</p>
      {create.error && <p role="alert" className="text-xs">{videoError(create.error)}</p>}
      </div>
    </details>
    {create.data && <p role="status" className="text-xs">Video submitted. <Link className="underline" to={query("/jobs", { search: create.data.job_id })}>View video job</Link></p>}
    {data && data.records.length === 0 && <div className="rounded-lg border border-dashed p-8 text-center"><Film className="mx-auto mb-3 size-7 text-muted-foreground" /><p className="text-sm font-semibold">Your quality video will appear here</p><p className="mt-1 text-xs text-muted-foreground">Choose a camera and generate an MP4. Existing evaluation results are reused.</p></div>}
    {video && <div className="space-y-3">
      <div className="space-y-1.5"><Label htmlFor="depth-retained-video">Retained depth video</Label><select id="depth-retained-video" className="depth-select w-full" value={video.id} onChange={e => setVideoId(e.target.value)}>{data?.records.map(v => <option key={v.id} value={v.id}>{v.submission ? `${v.sensor_name} · ${views[v.submission.view]} · ${v.state} · ${new Date(v.created_at).toLocaleString()}` : `${v.id} · ${v.state}`}</option>)}</select></div>
      <div className="flex flex-wrap items-center gap-3 text-xs"><strong>{video.finish_label ?? video.id}</strong>{video.submission && <span>{video.frame_count?.toLocaleString()} frames · {duration(video.duration_seconds ?? 0)} · {video.submission.fps} fps</span>}{video.job_id && <Link className="ml-auto underline" to={query("/jobs", { search: video.job_id })}>Video job</Link>}</div>
      {video.video_available ? <>
        <video key={video.id} className="max-h-[520px] w-full rounded-lg border bg-[#0f172a]" controls preload="metadata" aria-label="Depth quality video" poster={url("poster")} src={url("media")} />
        <div className="flex items-center gap-4"><Button asChild size="sm"><a href={url("download")}><Download className="size-4" />Download MP4</a></Button><a className="text-xs underline" href={url("manifest")}>Video settings & provenance</a></div>
      </> : <div role="status" className="rounded-lg border p-5 text-sm">
        <p className="font-semibold capitalize">{video.state}{["queued", "running", "encoding", "validating", "publishing"].includes(video.state) && <Loader2 className="ml-2 inline size-4 animate-spin" />}</p>
        {video.progress.total_frames != null && <><p className="mt-1 text-xs text-muted-foreground">{(video.progress.completed_frames ?? 0).toLocaleString()} / {video.progress.total_frames.toLocaleString()} frames processed</p>
        <progress className="mt-3 h-2 w-full accent-primary" max={video.progress.total_frames || 1} value={video.progress.completed_frames} aria-label="Depth video progress" /></>}
        {video.progress.error && <p role="alert" className="mt-2 text-xs">{video.progress.error}</p>}
      </div>}
    </div>}
    {data && data.total > 12 && <div className="flex items-center gap-2"><Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 12))}>Previous videos</Button><Button size="sm" variant="outline" disabled={offset + 12 >= data.total} onClick={() => setOffset(offset + 12)}>Next videos</Button><span className="text-xs text-muted-foreground">{data.total} videos</span></div>}
  </section>
}
