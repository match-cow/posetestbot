import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Archive, ArrowRight, LoaderCircle, Power, Server, Square } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"

import { HelpTip } from "@/components/help-tip"
import { StatusBadge, type StatusTone } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { api, errorMessage } from "@/lib/api"
import type { ClusterControllerServiceStatus, ClusterStatus, Job } from "@/lib/contracts"

interface ServiceActionResponse {
  accepted: boolean
  action?: "start" | "stop"
  job_id?: string
  job?: Job
  service: ClusterControllerServiceStatus
}

function readyEstimators(controller: ClusterStatus | undefined) {
  return controller?.estimators?.filter((estimator) => estimator.ready && estimator.profiles.some((profile) => profile.enabled)) ?? []
}

function storageReady(controller: ClusterStatus | undefined) {
  return controller?.domains?.storage?.ready === true
}

function serviceTone(service: ClusterControllerServiceStatus | undefined, controller: ClusterStatus | undefined, failed: boolean): StatusTone {
  if (failed || service?.state === "failed" || service?.state === "unavailable") return "destructive"
  if (service?.state === "running" && controller?.available && storageReady(controller)) return "success"
  if (["starting", "stopping"].includes(service?.state ?? "") || service?.state === "running") return "warning"
  if (service?.state === "stopped") return "neutral"
  return "neutral"
}

function serviceValue(service: ClusterControllerServiceStatus | undefined, controller: ClusterStatus | undefined, pending: boolean, failed: boolean) {
  if (pending) return "Checking"
  if (failed) return "Unavailable"
  if (!service?.managed) return "Not configured"
  if (service.state === "running" && controller?.available && storageReady(controller)) return "Ready"
  if (service.state === "running" && controller?.available) return "Connected"
  if (service.state === "running") return "Starting API"
  return service.state.replaceAll("_", " ")
}

function serviceDetail(service: ClusterControllerServiceStatus | undefined, controller: ClusterStatus | undefined, failed: boolean) {
  if (failed) return "The local service state could not be loaded. Refresh before using controller controls."
  if (!service?.managed) return "Set POSETESTBOT_CLUSTER_ENV_FILE and POSETESTBOT_CLUSTER_SERVICE_UNIT in the web-service environment, then restart PoseTestBot."
  if (!service.integration.enabled) return "Service control is configured, but cluster integration is disabled in this PoseTestBot process."
  if (service.state === "running" && storageReady(controller) && readyEstimators(controller).length > 0) {
    const labels = readyEstimators(controller).map((estimator) => estimator.display_name).join(", ")
    return `Cluster storage/archive and estimator execution are ready. Qualified estimators: ${labels}.`
  }
  if (service.state === "running" && storageReady(controller)) {
    const blocker = controller?.estimators?.flatMap((estimator) => estimator.readiness_blockers)[0]
      ?? controller?.feature_blockers?.estimation_submission?.[0]
    return blocker
      ? `Cluster storage/archive is ready independently. Estimator execution is unavailable: ${blocker}`
      : "Cluster storage/archive is ready independently. No estimator runtime is ready."
  }
  if (service.state === "running" && !controller?.available) return "The service is active, but its authenticated loopback API is not answering yet."
  if (service.state === "running" && controller?.available) return controller.domains?.storage?.blockers?.[0] ?? controller.blockers[0]?.message ?? "The loopback API is connected, but cluster storage is not ready."
  if (service.state === "stopped") return "The fixed user service is installed and stopped. Start it before archive or estimator operations."
  return service.blockers[0]?.message ?? `The fixed user service is ${service.state}.`
}

export function ClusterControllerControl() {
  const queryClient = useQueryClient()
  const [stopDialogOpen, setStopDialogOpen] = useState(false)
  const [stopConfirmed, setStopConfirmed] = useState(false)
  const service = useQuery({
    queryKey: ["cluster-controller-service"],
    queryFn: () => api<ClusterControllerServiceStatus>("/cluster/controller-service"),
    retry: false,
    refetchInterval: (query) => ["starting", "stopping"].includes(query.state.data?.state ?? "") ? 2_000 : 5_000,
  })
  const controller = useQuery({
    queryKey: ["cluster-status"],
    queryFn: () => api<ClusterStatus>("/cluster/status"),
    retry: false,
    refetchInterval: (query) => service.data?.state === "running" && !query.state.data?.available ? 10_000 : 60_000,
  })
  const action = useMutation({
    mutationFn: (nextAction: "start" | "stop") => api<ServiceActionResponse>(`/cluster/controller-service/${nextAction}`, {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    }),
    onSuccess: (value, nextAction) => {
      setStopDialogOpen(false)
      setStopConfirmed(false)
      if (value.accepted) {
        toast.success(`Controller ${nextAction} queued`, { description: value.job_id ? `Job ${value.job_id}; progress is visible in Jobs.` : "Service status will refresh automatically." })
      } else {
        toast.info(`Controller is already ${nextAction === "start" ? "running" : "stopped"}`)
      }
      void queryClient.invalidateQueries({ queryKey: ["cluster-controller-service"] })
      void queryClient.invalidateQueries({ queryKey: ["cluster-status"] })
      void queryClient.invalidateQueries({ queryKey: ["cluster-pose-setup"] })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error, nextAction) => toast.error(`Controller could not ${nextAction}`, { description: errorMessage(error) }),
  })
  const pending = service.isPending || controller.isPending
  const failed = service.isError
  const value = serviceValue(service.data, controller.data, pending, failed)
  const tone = serviceTone(service.data, controller.data, failed)
  const estimatorsReady = readyEstimators(controller.data)
  const openStopDialog = () => {
    setStopConfirmed(false)
    setStopDialogOpen(true)
  }

  return <Card data-testid="cluster-controller-control" className="h-full">
    <CardContent className="pt-5">
      <div className="flex items-start justify-between">
        <div className="grid size-9 place-items-center rounded-lg bg-muted"><Server className="size-4 text-primary-strong" /></div>
        <StatusBadge status={service.data?.state ?? (failed ? "unavailable" : "checking")} tone={tone}>{value}</StatusBadge>
      </div>
      <div className="mt-4 flex items-center gap-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Cluster controller
        <HelpTip label="cluster controller status">Running describes the local user-systemd service. Connected means its loopback API answered. Ready means the independent cluster storage/archive capability is available; estimator readiness is reported separately.</HelpTip>
      </div>
      <div className="mt-1 truncate font-display text-lg font-semibold" title={service.data?.service_unit ?? "External companion"}>{service.data?.service_unit ?? "External companion"}</div>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{serviceDetail(service.data, controller.data, failed)}</p>
      {service.data?.state === "running" && controller.data?.available ? <div className="mt-3 grid grid-cols-2 gap-2 text-[11px]" data-testid="cluster-capability-status">
        <div className="rounded-md border bg-muted/30 px-2 py-1.5"><span className="text-muted-foreground">Storage</span><div className="font-semibold">{storageReady(controller.data) ? "Ready" : "Blocked"}</div></div>
        <div className="rounded-md border bg-muted/30 px-2 py-1.5"><span className="text-muted-foreground">Estimators</span><div className="font-semibold">{estimatorsReady.length > 0 ? `${estimatorsReady.length} ready` : "None ready"}</div></div>
      </div> : null}
      <div className="mt-3 grid grid-cols-2 gap-2">
        <Button size="sm" onClick={() => action.mutate("start")} disabled={!service.data?.can_start || action.isPending}>{action.isPending && action.variables === "start" ? <LoaderCircle className="animate-spin" /> : <Power />}Start</Button>
        <Button size="sm" variant="destructive" onClick={openStopDialog} disabled={!service.data?.can_stop || action.isPending}><Square />Stop</Button>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <Button asChild size="sm" variant="outline"><Link to="/run-folders"><Archive />Cluster storage <ArrowRight /></Link></Button>
        <Button asChild size="sm" variant="outline"><Link to="/pose-estimation">Pose Estimation <ArrowRight /></Link></Button>
      </div>
    </CardContent>
    <Dialog open={stopDialogOpen} onOpenChange={(open) => { if (!action.isPending) { setStopDialogOpen(open); if (!open) setStopConfirmed(false) } }}>
      <DialogContent data-testid="cluster-controller-stop-dialog">
        <DialogHeader><DialogTitle>Stop the cluster controller?</DialogTitle><DialogDescription>The local controller will stop accepting, staging, collecting, or reconciling archive and estimator work until it is started again.</DialogDescription></DialogHeader>
        <div className="flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 p-4 text-sm"><AlertTriangle className="mt-0.5 size-5 shrink-0 text-warning" /><p className="text-xs leading-relaxed text-muted-foreground">Remote SLURM identity is durable, but stopping during an active transfer or collection can delay completion and requires restart reconciliation. Check Jobs and stop during idle time when possible.</p></div>
        <Label className="flex items-start gap-3 rounded-lg border p-3"><Checkbox data-testid="cluster-controller-stop-confirmation" checked={stopConfirmed} onCheckedChange={(checked) => setStopConfirmed(checked === true)} /><span>I understand that active controller work may be interrupted until restart.</span></Label>
        <DialogFooter><Button variant="outline" onClick={() => setStopDialogOpen(false)} disabled={action.isPending}>Cancel</Button><Button variant="destructive" onClick={() => action.mutate("stop")} disabled={!stopConfirmed || action.isPending}>{action.isPending ? <LoaderCircle className="animate-spin" /> : <Square />}{action.isPending ? "Queueing…" : "Confirm stop"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </Card>
}
