import { useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, LoaderCircle, RefreshCcw, RotateCcw, Server } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { api, errorMessage } from "@/lib/api"

interface LifecycleBlocker {
  code: string
  message: string
}

interface LifecycleStatus {
  schema_version: "web_lifecycle.v1"
  instance_id: string
  backend_restart: {
    configured: boolean
    available: boolean
    service_unit: string | null
    state: string
    blockers: LifecycleBlocker[]
  }
  active_local_jobs: number
}

interface RestartAccepted {
  accepted: true
  instance_id: string
  retry_after_ms: number
}

type RestartOperation = "backend" | "both" | null

const RESTART_TIMEOUT_MS = 60_000

function delay(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

async function waitForNewBackend(previousInstanceId: string, retryAfterMs: number) {
  const deadline = Date.now() + RESTART_TIMEOUT_MS
  await delay(retryAfterMs)
  while (Date.now() < deadline) {
    try {
      const status = await api<LifecycleStatus>(
        `/system/lifecycle?restart_probe=${Date.now()}`,
        { cache: "no-store" },
      )
      if (status.instance_id !== previousInstanceId) return
    } catch {
      // A short connection failure is expected while systemd replaces Flask.
    }
    await delay(500)
  }
  throw new Error("The backend did not return within 60 seconds. Check the user-systemd service and journal.")
}

export function RestartControl() {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [operation, setOperation] = useState<RestartOperation>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  const lifecycle = useQuery({
    queryKey: ["web-lifecycle"],
    queryFn: () => api<LifecycleStatus>("/system/lifecycle", { cache: "no-store" }),
    enabled: open,
    staleTime: 0,
  })
  const busy = operation !== null
  const restartAvailable = lifecycle.data?.backend_restart.available === true
  const blocker = lifecycle.data?.backend_restart.blockers[0]?.message

  const changeOpen = (nextOpen: boolean) => {
    if (busy) return
    setOpen(nextOpen)
    if (!nextOpen) {
      setAcknowledged(false)
      setOperationError(null)
    }
  }

  const reloadFrontend = () => {
    window.location.reload()
  }

  const restartBackend = async (reloadAfterward: boolean) => {
    const requestedOperation: RestartOperation = reloadAfterward ? "both" : "backend"
    setOperation(requestedOperation)
    setOperationError(null)
    try {
      const accepted = await api<RestartAccepted>("/system/restart-backend", {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      })
      await waitForNewBackend(accepted.instance_id, accepted.retry_after_ms)
      if (reloadAfterward) {
        window.location.reload()
        return
      }
      await queryClient.invalidateQueries()
      setOperation(null)
      setOpen(false)
      setAcknowledged(false)
      toast.success("Backend restarted", {
        description: "The current browser frontend stayed open and reconnected to the new backend instance.",
      })
    } catch (error) {
      setOperation(null)
      setOperationError(errorMessage(error))
    }
  }

  return <>
    <Button
      variant="outline"
      className="h-[34px] px-2.5"
      onClick={() => setOpen(true)}
      aria-label="Open frontend and backend restart controls"
      data-testid="open-restart-controls"
    >
      <RotateCcw aria-hidden="true" />
      <span className="hidden 2xl:inline">Restart</span>
    </Button>
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogContent data-testid="restart-controls-dialog" onEscapeKeyDown={(event) => busy && event.preventDefault()} onPointerDownOutside={(event) => busy && event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Restart operator console</DialogTitle>
          <DialogDescription>Choose the smallest restart that matches what you are debugging. None of these actions command the robot or open cameras.</DialogDescription>
        </DialogHeader>

        {busy ? <div className="rounded-lg border border-primary/30 bg-primary/5 p-5 text-center" role="status" data-testid="backend-restart-waiting">
          <LoaderCircle className="mx-auto size-6 animate-spin text-primary" aria-hidden="true" />
          <div className="mt-3 text-sm font-semibold">Restarting backend…</div>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">Waiting for the managed service to return with a new process. Keep this tab open.</p>
        </div> : <div className="space-y-3">
          <div className="rounded-lg border p-3">
            <div className="flex items-start gap-3">
              <RefreshCcw className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
              <div className="min-w-0 flex-1"><div className="text-sm font-semibold">Frontend only</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">Reload this browser tab and fetch the current built UI. Backend jobs and services keep running.</p></div>
              <Button variant="outline" size="sm" onClick={reloadFrontend} data-testid="restart-frontend">Reload frontend</Button>
            </div>
          </div>

          <div className="rounded-lg border p-3">
            <div className="flex items-start gap-3">
              <Server className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
              <div className="min-w-0 flex-1"><div className="text-sm font-semibold">Managed backend</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">Restart the fixed PoseTestBot web service. Backend-only keeps this browser bundle open; Both reloads it after reconnection.</p></div>
            </div>
            {lifecycle.isPending ? <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground"><LoaderCircle className="size-3.5 animate-spin" />Checking managed-service status…</div> : lifecycle.isError ? <div className="mt-3 rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">Backend restart status is unavailable: {errorMessage(lifecycle.error)}</div> : !restartAvailable ? <div className="mt-3 rounded-md border border-warning/35 bg-warning/10 p-2 text-xs leading-relaxed text-muted-foreground" data-testid="backend-restart-disabled-reason">{blocker ?? "Managed backend restart is unavailable."}</div> : <>
              <div className="mt-3 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-2.5 text-xs" role="alert">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning-foreground" aria-hidden="true" />
                <p className="leading-relaxed"><span className="font-semibold">Local work will be interrupted.</span> Active captures, previews, and process-owned jobs stop during backend shutdown. Remote cluster jobs continue independently.{lifecycle.data && lifecycle.data.active_local_jobs > 0 ? ` ${lifecycle.data.active_local_jobs} local job${lifecycle.data.active_local_jobs === 1 ? " is" : "s are"} currently active.` : " No active local jobs are currently reported."}</p>
              </div>
              <Label className="mt-3 flex items-start gap-2 rounded-md border p-2.5 text-xs">
                <Checkbox checked={acknowledged} onCheckedChange={(value) => setAcknowledged(value === true)} data-testid="backend-restart-acknowledgement" />
                <span>I understand that restarting the backend interrupts process-owned local work.</span>
              </Label>
              <div className="mt-3 flex justify-end gap-2">
                <Button variant="outline" size="sm" disabled={!acknowledged} onClick={() => void restartBackend(false)} data-testid="restart-backend">Restart backend</Button>
                <Button size="sm" disabled={!acknowledged} onClick={() => void restartBackend(true)} data-testid="restart-both">Restart both</Button>
              </div>
            </>}
          </div>
        </div>}

        {operationError && <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs leading-relaxed text-destructive" role="alert" data-testid="backend-restart-error">{operationError}</div>}
        <DialogFooter><Button variant="outline" onClick={() => changeOpen(false)} disabled={busy}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </>
}
