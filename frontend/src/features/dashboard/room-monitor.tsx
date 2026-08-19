import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Radio, RefreshCw, SunMedium } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"

import { StatusBadge, type StatusTone } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { api, errorMessage } from "@/lib/api"

interface MonitorStatus {
  schema_version: "monitor_webrtc.v2"
  transport: "webrtc"
  status: string
  signaling_ready: boolean
  peer_count: number
  frame_count: number
  capture_frame_count: number
  media_frame_count: number
  heartbeat_at: string
  camera_open: boolean
  connected_peer_count: number
  stun_port: number | null
  selected_node: { path?: string } | null
  error: string | null
  error_reason: string | null
  brightness?: BrightnessStatus
}

interface BrightnessStatus {
  schema_version: "monitor_brightness.v1"
  supported: boolean
  state: "unavailable" | "idle" | "queued" | "running" | "succeeded" | "failed"
  target_luma: number
  tolerance: number
  measured_luma: number | null
  control: { minimum: number; maximum: number; step: number; default: number; value: number } | null
  attempts: number
  max_attempts: number
  started_at: string | null
  completed_at: string | null
  message: string
}

interface MonitorPayload {
  job: { id: string; status: string; message?: string | null } | null
  webrtc_status: MonitorStatus | null
}

interface SessionDescriptionPayload {
  type: RTCSdpType
  sdp: string
}

const TERMINAL_JOB_STATUSES = new Set(["failed", "canceled", "cancelled", "succeeded"])
const AUTOMATIC_RETRY_DELAYS_MS = [1_000, 3_000, 10_000]
const FIRST_VIDEO_FRAME_TIMEOUT_MS = 5_000

function monitorStatusTone(status: string): StatusTone {
  if (status === "connected") return "informational"
  if (status === "failed") return "destructive"
  if (status === "canceled" || status === "cancelled" || status === "succeeded") return "neutral"
  return "warning"
}

function waitForIceGatheringComplete(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === "complete") return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      peer.removeEventListener("icegatheringstatechange", changed)
      reject(new Error("Timed out while gathering WebRTC host candidates"))
    }, 10_000)
    const changed = () => {
      if (peer.iceGatheringState !== "complete") return
      window.clearTimeout(timeout)
      peer.removeEventListener("icegatheringstatechange", changed)
      resolve()
    }
    peer.addEventListener("icegatheringstatechange", changed)
  })
}

function stopPeer(peer: RTCPeerConnection | null, video: HTMLVideoElement | null) {
  peer?.close()
  const stream = video?.srcObject
  if (stream instanceof MediaStream) stream.getTracks().forEach((track) => track.stop())
  if (video) video.srcObject = null
}

function preferBrowserVp8(transceiver: RTCRtpTransceiver) {
  const codecs = RTCRtpSender.getCapabilities("video")?.codecs
  if (!codecs) return
  transceiver.setCodecPreferences([
    ...codecs.filter((codec) => codec.mimeType.toLowerCase() === "video/vp8"),
    ...codecs.filter((codec) => codec.mimeType.toLowerCase() !== "video/vp8"),
  ])
}

export function RoomMonitor() {
  const queryClient = useQueryClient()
  const videoRef = useRef<HTMLVideoElement>(null)
  const peerRef = useRef<RTCPeerConnection | null>(null)
  const negotiationAttempts = useRef(0)
  const previousJobId = useRef<string | null>(null)
  const negotiationSequence = useRef(0)
  const [connection, setConnection] = useState({ jobId: null as string | null, status: "waiting", error: null as string | null })
  const [negotiationVersion, setNegotiationVersion] = useState(0)

  const monitor = useQuery({
    queryKey: ["monitor"],
    queryFn: () => api<MonitorPayload>("/monitoring/webcam"),
    refetchInterval: (query) => {
      const currentJob = query.state.data?.job
      const connected = connection.jobId === (currentJob?.id ?? null)
        && connection.status === "connected"
        && currentJob?.status === "running"
      const brightnessState = query.state.data?.webrtc_status?.brightness?.state
      const calibrating = brightnessState === "queued" || brightnessState === "running"
      return connected && !calibrating ? 5_000 : 1_000
    },
  })
  const startMonitor = useMutation({
    mutationFn: () => api<MonitorPayload>("/monitoring/webcam", { method: "POST", body: "{}" }),
    onSuccess: (data) => queryClient.setQueryData(["monitor"], data),
    onError: (error) => toast.error("Room monitor could not start", { description: errorMessage(error) }),
  })
  const calibrateBrightness = useMutation({
    mutationFn: (currentJobId: string) => api<{ brightness: BrightnessStatus }>(
      `/monitoring/webcam/${currentJobId}/brightness/autocalibrate`,
      { method: "POST", body: "{}" },
    ),
    onSuccess: ({ brightness }) => queryClient.setQueryData<MonitorPayload>(["monitor"], (current) => {
      if (!current?.webrtc_status) return current
      return { ...current, webrtc_status: { ...current.webrtc_status, brightness } }
    }),
    onError: (error) => toast.error("Brightness could not be calibrated", { description: errorMessage(error) }),
  })

  const jobId = monitor.data?.job?.id ?? null
  const jobStatus = monitor.data?.job?.status ?? null
  const signalingReady = monitor.data?.webrtc_status?.signaling_ready === true
  const connectionStatus = connection.jobId === jobId ? connection.status : "waiting"

  useEffect(() => {
    if (previousJobId.current === jobId) return
    previousJobId.current = jobId
    negotiationAttempts.current = 0
    setConnection({ jobId, status: "waiting", error: null })
  }, [jobId])

  useEffect(() => {
    if (!jobId || jobStatus !== "running" || !signalingReady) return
    const sequence = ++negotiationSequence.current
    const attempt = ++negotiationAttempts.current
    let disposed = false
    let retryTimer: number | null = null
    let firstFrameTimer: number | null = null
    let videoFrameCallback: number | null = null
    let failureHandled = false
    let firstFrameRendered = false
    const video = videoRef.current
    stopPeer(peerRef.current, video)

    const stunPort = monitor.data?.webrtc_status?.stun_port
    const peer = new RTCPeerConnection({
      iceServers: stunPort ? [{ urls: `stun:${window.location.hostname}:${stunPort}` }] : [],
    })
    peerRef.current = peer
    const transceiver = peer.addTransceiver("video", { direction: "recvonly" })
    preferBrowserVp8(transceiver)

    const retryFailedConnection = (reason = "WebRTC connection failed") => {
      if (disposed || failureHandled || sequence !== negotiationSequence.current) return
      failureHandled = true
      if (firstFrameTimer !== null) window.clearTimeout(firstFrameTimer)
      firstFrameTimer = null
      setConnection({ jobId, status: "failed", error: reason })
      stopPeer(peer, video)
      if (attempt <= AUTOMATIC_RETRY_DELAYS_MS.length) {
        retryTimer = window.setTimeout(
          () => setNegotiationVersion((version) => version + 1),
          AUTOMATIC_RETRY_DELAYS_MS[attempt - 1],
        )
      }
    }

    const markFirstFrameRendered = () => {
      if (disposed || failureHandled || firstFrameRendered || sequence !== negotiationSequence.current) return
      firstFrameRendered = true
      if (firstFrameTimer !== null) window.clearTimeout(firstFrameTimer)
      firstFrameTimer = null
      setConnection({ jobId, status: "connected", error: null })
    }

    const markFirstFrameFromMediaEvent = () => {
      if (
        video
        && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
        && video.videoWidth > 0
        && video.videoHeight > 0
      ) markFirstFrameRendered()
    }

    const diagnoseMissingFirstFrame = async () => {
      let packetsReceived = 0
      let framesReceived = 0
      let framesDecoded = 0
      try {
        const stats = await peer.getStats()
        stats.forEach((rawReport) => {
          const report = rawReport as RTCInboundRtpStreamStats & {
            kind?: string
            mediaType?: string
            framesReceived?: number
            framesDecoded?: number
          }
          if (
            report.type !== "inbound-rtp"
            || (report.kind ?? report.mediaType) !== "video"
          ) return
          packetsReceived += report.packetsReceived ?? 0
          framesReceived += report.framesReceived ?? 0
          framesDecoded += report.framesDecoded ?? 0
        })
      } catch {
        // A closing peer may make stats unavailable; the timeout is still useful.
      }
      retryFailedConnection(
        "WebRTC connected, but the browser did not render a camera frame within "
        + `${FIRST_VIDEO_FRAME_TIMEOUT_MS / 1_000} seconds `
        + `(packets ${packetsReceived}, received frames ${framesReceived}, decoded frames ${framesDecoded}).`,
      )
    }

    const waitForFirstFrame = () => {
      if (firstFrameRendered || firstFrameTimer !== null) return
      setConnection({ jobId, status: "receiving", error: null })
      firstFrameTimer = window.setTimeout(
        () => void diagnoseMissingFirstFrame(),
        FIRST_VIDEO_FRAME_TIMEOUT_MS,
      )
    }

    const handleVideoError = () => {
      retryFailedConnection(video?.error?.message || "The browser could not decode the room-monitor video.")
    }

    video?.addEventListener("loadeddata", markFirstFrameFromMediaEvent)
    video?.addEventListener("playing", markFirstFrameFromMediaEvent)
    video?.addEventListener("timeupdate", markFirstFrameFromMediaEvent)
    video?.addEventListener("error", handleVideoError)
    if (video && "requestVideoFrameCallback" in video) {
      videoFrameCallback = video.requestVideoFrameCallback(() => {
        videoFrameCallback = null
        markFirstFrameRendered()
      })
    }

    peer.ontrack = (event) => {
      const stream = event.streams[0] ?? new MediaStream([event.track])
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        void videoRef.current.play().catch((error) => retryFailedConnection(errorMessage(error)))
      }
    }
    peer.onconnectionstatechange = () => {
      if (peer.connectionState === "connected") {
        if (firstFrameRendered) setConnection({ jobId, status: "connected", error: null })
        else waitForFirstFrame()
      }
      if (["failed", "disconnected"].includes(peer.connectionState)) retryFailedConnection(`WebRTC peer became ${peer.connectionState}`)
    }

    void (async () => {
      try {
        setConnection({ jobId, status: "connecting", error: null })
        const offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        await waitForIceGatheringComplete(peer)
        if (!peer.localDescription) throw new Error("WebRTC offer did not produce a local description")
        const answer = await api<SessionDescriptionPayload>(`/monitoring/webcam/${jobId}/webrtc/offer`, {
          method: "POST",
          body: JSON.stringify({ type: peer.localDescription.type, sdp: peer.localDescription.sdp }),
        })
        if (disposed) return
        await peer.setRemoteDescription(answer)
      } catch (error) {
        retryFailedConnection(errorMessage(error))
      }
    })()

    return () => {
      disposed = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      if (firstFrameTimer !== null) window.clearTimeout(firstFrameTimer)
      if (video && videoFrameCallback !== null && "cancelVideoFrameCallback" in video) {
        video.cancelVideoFrameCallback(videoFrameCallback)
      }
      video?.removeEventListener("loadeddata", markFirstFrameFromMediaEvent)
      video?.removeEventListener("playing", markFirstFrameFromMediaEvent)
      video?.removeEventListener("timeupdate", markFirstFrameFromMediaEvent)
      video?.removeEventListener("error", handleVideoError)
      if (peerRef.current === peer) peerRef.current = null
      stopPeer(peer, video)
    }
  }, [jobId, jobStatus, signalingReady, negotiationVersion, monitor.data?.webrtc_status?.stun_port])

  useEffect(() => {
    const video = videoRef.current
    return () => stopPeer(peerRef.current, video)
  }, [])

  const retry = () => {
    negotiationAttempts.current = 0
    setConnection({ jobId, status: "waiting", error: null })
    if (jobId && !TERMINAL_JOB_STATUSES.has(jobStatus ?? "") && signalingReady) {
      setNegotiationVersion((version) => version + 1)
      return
    }
    startMonitor.mutate()
  }
  const displayStatus = ["connected", "connecting", "receiving", "failed"].includes(connectionStatus)
    ? connectionStatus
    : monitor.data?.webrtc_status?.status ?? jobStatus ?? "waiting"
  const message = startMonitor.isPending
    ? "Starting camera…"
    : monitor.data?.webrtc_status?.error_reason
      ?? monitor.data?.webrtc_status?.error
      ?? connection.error
      ?? (connectionStatus === "receiving" ? "WebRTC connected; waiting for the first camera frame…" : null)
      ?? (connectionStatus === "failed" ? "WebRTC connection failed" : "Waiting for room camera…")
  const brightness = monitor.data?.webrtc_status?.brightness
  const brightnessActive = brightness?.state === "queued" || brightness?.state === "running"
  const brightnessReady = connectionStatus === "connected" && brightness?.supported === true
  const brightnessMessage = brightness?.message ?? "Brightness control is available after the camera opens."
  const monitorNeedsStart = !jobStatus || TERMINAL_JOB_STATUSES.has(jobStatus)

  return (
    <Card data-testid="dashboard-room-monitor" className="col-span-12 overflow-hidden xl:col-span-7">
      <CardHeader><CardTitle className="flex items-center gap-2"><Radio className="size-4 text-primary-strong" />Test cell monitor</CardTitle><CardDescription>Live supervised overview of the workcell during setup and acquisition. The room camera opens only after an operator starts it.</CardDescription></CardHeader>
      <CardContent>
        <div className="surface-grid relative aspect-video overflow-hidden rounded-lg bg-muted">
          <video
            ref={videoRef}
            data-testid="room-monitor-video"
            data-connection-state={connectionStatus}
            className="size-full rotate-180 object-cover"
            muted
            autoPlay
            playsInline
            aria-label="Live room monitor"
          />
          {connectionStatus !== "connected" && <div data-testid="room-monitor-message" className="absolute inset-0 grid place-items-center bg-muted/80 text-xs text-muted-foreground">{message}</div>}
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <StatusBadge status={displayStatus} tone={monitorStatusTone(displayStatus)} />
            <div data-testid="room-monitor-brightness-status" className="mt-1 max-w-xl truncate text-[11px] text-muted-foreground" title={brightnessMessage}>{brightnessMessage}</div>
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <Button
              data-testid="room-monitor-auto-brightness"
              size="sm"
              variant="outline"
              onClick={() => jobId && calibrateBrightness.mutate(jobId)}
              disabled={!jobId || !brightnessReady || brightnessActive || calibrateBrightness.isPending}
            >
              <SunMedium className={brightnessActive ? "animate-pulse" : ""} />
              {brightnessActive || calibrateBrightness.isPending ? "Calibrating…" : "Auto brightness"}
            </Button>
            <Button size="sm" variant={monitorNeedsStart ? "outline" : "ghost"} onClick={retry} disabled={monitor.isPending || startMonitor.isPending}><RefreshCw />{monitor.isPending ? "Checking…" : startMonitor.isPending ? "Starting…" : connectionStatus === "connected" ? "Reconnect" : monitorNeedsStart ? "Start monitor" : "Retry"}</Button>
            {jobId && <Button asChild size="sm" variant="ghost"><Link to="/jobs">Open Jobs</Link></Button>}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
