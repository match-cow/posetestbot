import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import {
  AlertTriangle,
  Archive,
  Boxes,
  Camera,
  CloudDownload,
  CloudUpload,
  Clock3,
  FileWarning,
  FolderPlus,
  FolderOpen,
  HardDrive,
  LoaderCircle,
  MoveRight,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"

import { EmptyState } from "@/components/empty-state"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage } from "@/lib/api"
import type { ClusterArchive, ClusterCapabilityDomain, ClusterJob, Job, RunFolder, RunFolderInventory, RunFolderInventory as Inventory } from "@/lib/contracts"
import { cn, formatDate, titleCase } from "@/lib/utils"
import { activeWorkflowHref } from "@/lib/workflow-session"
import { useOperator } from "@/providers/operator-provider"

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "canceling"])
const TERMINAL_JOB_STATUSES = new Set(["succeeded", "failed", "canceled", "cancelled"])

type OperationKind = "refresh" | "move" | "delete"

interface JobSubmission {
  job_id: string
  status: string
  job: Job
}

interface RunFolderMutationResponse extends JobSubmission {
  source_run_root: string
  destination_run_root?: string
}

interface TrackedOperation {
  kind: OperationKind
  runName?: string
  sourceRunRoot?: string
  destinationRunRoot?: string
  job: Job
}

interface RemoteArchiveOperation {
  kind: "restore" | "delete"
  jobId: string
}

function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable"
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  const digits = amount >= 100 || unit === 0 ? 0 : amount >= 10 ? 1 : 2
  return `${amount.toFixed(digits)} ${units[unit]}`
}

function plural(value: number, singular: string) {
  const label = value === 1
    ? singular
    : singular.endsWith("y")
      ? `${singular.slice(0, -1)}ies`
      : `${singular}s`
  return `${value.toLocaleString()} ${label}`
}

function normalizeRunPath(value: string) {
  const absolute = value.startsWith("/")
  const segments: string[] = []
  for (const segment of value.split("/")) {
    if (!segment || segment === ".") continue
    if (segment === "..") segments.pop()
    else segments.push(segment)
  }
  const normalized = `${absolute ? "/" : ""}${segments.join("/")}`
  return normalized || (absolute ? "/" : ".")
}

function isSelectedRunFolder(run: RunFolder, selectedRun: string) {
  const selected = normalizeRunPath(selectedRun)
  return normalizeRunPath(run.path) === selected
}

function runRootForPath(path: string, allowedRoots: string[]) {
  return [...allowedRoots]
    .sort((left, right) => right.length - left.length)
    .find((root) => path === root || path.startsWith(`${root.replace(/\/+$/, "")}/`))
    ?? allowedRoots[0]
}

function runFolderPath(root: string, name: string) {
  return `${root.replace(/\/+$/, "")}/${name.trim()}`
}

function validRunFolderName(name: string) {
  const value = name.trim()
  return Boolean(value) && value !== "." && value !== ".." && !/[\\/\0]/.test(value)
}

function runDisplayName(run: RunFolder) {
  return run.config.run_name?.trim() || run.name
}

function runMatchesSearch(run: RunFolder, search: string) {
  const needle = search.trim().toLocaleLowerCase()
  if (!needle) return true
  const searchable = [
    runDisplayName(run),
    run.name,
    run.path,
    run.root,
    run.config.intent ?? "",
    run.config.annotation_mode ?? "",
    run.contents.dataset_mode ?? "",
    ...run.contents.object_names,
    ...run.contents.sensors.flatMap((sensor) => [sensor.name, sensor.sensor_type, sensor.device_id, sensor.mounting_mode]),
    ...evidenceEntries(run).filter(([, , exists]) => exists).map(([, label]) => label),
  ].join("\n").toLocaleLowerCase()
  return searchable.includes(needle)
}

function jobTone(status: string) {
  if (status === "succeeded") return "success" as const
  if (["failed", "canceled", "cancelled"].includes(status)) return "destructive" as const
  return "warning" as const
}

function inventoryTone(state: Inventory["inventory_state"]) {
  if (state === "ready") return "success" as const
  if (state === "missing") return "neutral" as const
  return "warning" as const
}

function storageTone(status: string) {
  if (status === "ready") return "success" as const
  if (status === "warning") return "warning" as const
  if (status === "error") return "destructive" as const
  return "neutral" as const
}

function operationTitle(operation: TrackedOperation) {
  if (operation.kind === "refresh") return "Refreshing run-folder inventory"
  if (operation.kind === "move") return `Moving ${operation.runName ?? "run folder"}`
  return `Deleting ${operation.runName ?? "run folder"}`
}

function operationFromJob(job: Job): TrackedOperation | null {
  const kind = job.parameters.run_folder_operation
  if (kind !== "move" && kind !== "delete") return null
  const source = typeof job.parameters.source_run_root === "string"
    ? job.parameters.source_run_root
    : job.run_root ?? undefined
  const destination = typeof job.parameters.destination_run_root === "string"
    ? job.parameters.destination_run_root
    : undefined
  return {
    kind,
    runName: source?.split("/").filter(Boolean).at(-1),
    sourceRunRoot: source,
    destinationRunRoot: destination,
    job,
  }
}

function operationResult(job: Job): Record<string, unknown> | null {
  for (const line of [...job.tail].reverse()) {
    try {
      const value: unknown = JSON.parse(line)
      if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
    } catch {
      // Job logs can contain ordinary progress lines before the final JSON result.
    }
  }
  return null
}

function evidenceEntries(run: RunFolder) {
  return [
    ["raw_capture", "Raw capture", run.contents.evidence.raw_capture],
    ["synchronized", "Synchronized", run.contents.evidence.synchronized],
    ["calibration", "Calibration", run.contents.evidence.calibration],
    ["bop_export", "BOP export", run.contents.evidence.bop_export],
    ["bop_evaluation", "BOP evaluation", run.contents.evidence.bop_evaluation],
  ] as const
}

function RootCapacity({ root }: { root: RunFolderInventory["roots"][number] }) {
  const storage = root.storage
  const usedPercent = storage.free_fraction === null
    ? null
    : Math.max(0, Math.min(100, Math.round((1 - storage.free_fraction) * 100)))
  const detail = storage.error
    ?? (usedPercent === null
      ? "Filesystem capacity is unavailable."
      : `${formatBytes(storage.free_bytes)} free of ${formatBytes(storage.total_bytes)}`)

  return <Card data-testid="run-folder-root" data-root-path={root.path}>
    <CardContent className="pt-4">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted"><HardDrive aria-hidden="true" className="size-4 text-primary-strong" /></span>
          <div className="min-w-0">
            <div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Allowed run root</div>
            <div className="mt-1 truncate font-mono text-[11px] font-semibold" title={root.path}>{root.path}</div>
          </div>
        </div>
        <StatusBadge status={root.exists ? storage.status : "missing"} tone={root.exists ? storageTone(storage.status) : "destructive"} />
      </div>
      <div className="mt-4 flex items-end justify-between gap-3">
        <div><div className="font-display text-lg font-semibold">{formatBytes(storage.free_bytes)} free</div><div className="mt-0.5 text-[10px] text-muted-foreground">{detail}</div></div>
        {storage.filesystem_path && <div className="max-w-[42%] truncate text-right font-mono text-[9px] text-muted-foreground" title={storage.filesystem_path}>{storage.filesystem_path}</div>}
      </div>
      {usedPercent === null
        ? <div className="mt-3 rounded bg-muted px-2 py-1 text-[9px] font-medium text-muted-foreground">Capacity unavailable</div>
        : <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label={`Storage used for ${root.path}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={usedPercent}>
            <div className={cn("h-full rounded-full", storage.status === "error" ? "bg-destructive" : storage.status === "warning" ? "bg-warning" : "bg-primary")} style={{ width: `${usedPercent}%` }} />
          </div>}
    </CardContent>
  </Card>
}

function ContentsSummary({ run }: { run: RunFolder }) {
  const configuredSensors = run.contents.sensors
  const objectNames = run.contents.object_names
  const visibleSensors = configuredSensors.slice(0, 4)
  const visibleObjectNames = objectNames.slice(0, 6)
  return <div data-testid="run-folder-contents" className="space-y-3">
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge variant="outline">{run.contents.dataset_mode ? titleCase(run.contents.dataset_mode) : "Dataset mode unknown"}</Badge>
      {run.contents.resolution && <Badge variant="outline">{run.contents.resolution}</Badge>}
      {run.contents.fps !== null && <Badge variant="outline">{run.contents.fps} FPS</Badge>}
      {run.contents.synchronization_mode && <Badge variant="outline">{titleCase(run.contents.synchronization_mode)}</Badge>}
    </div>

    <div>
      <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[0.1em] text-muted-foreground"><Camera aria-hidden="true" className="size-3" />Sensors · {run.contents.enabled_sensor_count}/{run.contents.sensor_count} enabled</div>
      {configuredSensors.length
        ? <div className="mt-1.5 space-y-1">{visibleSensors.map((sensor) => <div className="min-w-0 text-[11px]" key={`${sensor.sensor_type}:${sensor.device_id}`}>
            <div className={cn("truncate font-semibold", !sensor.enabled && "text-muted-foreground line-through")} title={sensor.name}>{sensor.name}</div>
            <div className="truncate font-mono text-[9px] text-muted-foreground" title={`${sensor.sensor_type}:${sensor.device_id} · ${sensor.mounting_mode}`}>{sensor.sensor_type}:{sensor.device_id} · {titleCase(sensor.mounting_mode)}{sensor.enabled ? "" : " · disabled"}</div>
          </div>)}{configuredSensors.length > visibleSensors.length && <div className="text-[10px] text-muted-foreground">+{configuredSensors.length - visibleSensors.length} more sensor summaries</div>}</div>
        : <div className="mt-1 text-[11px] text-muted-foreground">No configured sensors</div>}
    </div>

    <div>
      <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[0.1em] text-muted-foreground"><Boxes aria-hidden="true" className="size-3" />Objects · {run.contents.object_count}</div>
      <div className="mt-1 break-words text-[11px] leading-relaxed text-muted-foreground">{objectNames.length ? <>{visibleObjectNames.join(" · ")}{objectNames.length > visibleObjectNames.length ? ` · +${objectNames.length - visibleObjectNames.length} more` : ""}</> : run.contents.dataset_mode === "objectless" ? "Objectless capture" : "No selected object instances"}</div>
      {run.contents.template_uuid && <div className="mt-1 truncate font-mono text-[9px] text-muted-foreground" title={run.contents.template_uuid}>Template {run.contents.template_uuid}</div>}
    </div>
  </div>
}

function EvidenceSummary({ run }: { run: RunFolder }) {
  const available = evidenceEntries(run).filter(([, , exists]) => exists)
  return <div className="flex max-w-[240px] flex-wrap gap-1.5">
    {available.length
      ? available.map(([id, label]) => <StatusBadge key={id} status="available" tone="success">{label}</StatusBadge>)
      : <span className="text-[11px] leading-relaxed text-muted-foreground">No durable capture or processing evidence yet.</span>}
  </div>
}

function archiveTone(state: string) {
  if (state === "succeeded") return "success" as const
  if (["failed", "canceled"].includes(state)) return "destructive" as const
  return "warning" as const
}

function ClusterStorageSection({ inventory }: { inventory: RunFolderInventory }) {
  const queryClient = useQueryClient()
  const { selectedRun } = useOperator()
  const [sourcePath, setSourcePath] = useState("")
  const [operator, setOperator] = useState(() => localStorage.getItem("posetestbot.clusterOperator") ?? "")
  const [restoreArchive, setRestoreArchive] = useState<ClusterArchive | null>(null)
  const [deleteArchive, setDeleteArchive] = useState<ClusterArchive | null>(null)
  const [deleteArchiveConfirmed, setDeleteArchiveConfirmed] = useState(false)
  const [destinationRoot, setDestinationRoot] = useState("")
  const [destinationName, setDestinationName] = useState("")
  const [remoteOperation, setRemoteOperation] = useState<RemoteArchiveOperation | null>(null)
  const notifiedRemoteJobs = useRef(new Set<string>())
  const runs = inventory.runs
  const effectiveSourcePath = runs.some((run) => run.path === sourcePath)
    ? sourcePath
    : runs.find((run) => isSelectedRunFolder(run, selectedRun))?.path ?? runs[0]?.path ?? ""

  const archives = useQuery({
    queryKey: ["cluster-archives"],
    queryFn: () => api<{ archives: ClusterArchive[]; integration: { enabled: boolean }; storage?: ClusterCapabilityDomain }>("/cluster/archives"),
    retry: false,
    refetchInterval: (state) => state.state.data?.archives.some((archive) => !["succeeded", "failed", "canceled"].includes(archive.state)) ? 2_000 : 15_000,
  })
  const remoteJob = useQuery({
    queryKey: ["cluster-job", remoteOperation?.jobId],
    queryFn: () => api<{ job: ClusterJob }>(`/cluster/jobs/${remoteOperation?.jobId}`),
    enabled: Boolean(remoteOperation),
    refetchInterval: (state) => state.state.data?.job.terminal ? false : 1_500,
  })
  useEffect(() => {
    const job = remoteJob.data?.job
    if (!job?.terminal || notifiedRemoteJobs.current.has(job.job_id)) return
    notifiedRemoteJobs.current.add(job.job_id)
    void queryClient.invalidateQueries({ queryKey: ["runs"] })
    void queryClient.invalidateQueries({ queryKey: ["run-folders"] })
    void queryClient.invalidateQueries({ queryKey: ["cluster-archives"] })
    void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    const operation = remoteOperation?.kind === "delete" ? "deletion" : "restore"
    if (job.state === "succeeded") toast.success(`Cluster archive ${operation} completed${operation === "restore" ? " and verified" : ""}`)
    else toast.error(`Cluster archive ${operation} did not complete`, { description: job.error ?? job.state })
  }, [queryClient, remoteJob.data, remoteOperation?.kind])

  const createArchive = useMutation({
    mutationFn: (run: RunFolder) => api<{ archive: ClusterArchive }>("/cluster/archives", {
      method: "POST",
      body: JSON.stringify({ run_root: run.path, expected_identity: run.identity, operator: operator.trim() }),
    }),
    onSuccess: ({ archive }) => {
      localStorage.setItem("posetestbot.clusterOperator", operator.trim())
      toast.success("Archive copy queued", { description: `${archive.archive_id} continues after navigation.` })
      queryClient.invalidateQueries({ queryKey: ["cluster-archives"] })
    },
    onError: (error) => toast.error("Archive request was refused", { description: errorMessage(error) }),
  })
  const restore = useMutation({
    mutationFn: (archive: ClusterArchive) => {
      if (!destinationRoot || !validRunFolderName(destinationName)) throw new Error("Choose one valid destination folder name")
      const destinationPath = runFolderPath(destinationRoot, destinationName)
      if (runs.some((run) => normalizeRunPath(run.path) === normalizeRunPath(destinationPath))) {
        throw new Error("Destination run folder already exists")
      }
      return api<{ job: ClusterJob }>(`/cluster/archives/${archive.archive_id}/restore`, {
        method: "POST",
        body: JSON.stringify({ destination_root: destinationRoot, destination_name: destinationName.trim(), operator: operator.trim() }),
      })
    },
    onSuccess: ({ job }) => {
      setRemoteOperation({ kind: "restore", jobId: job.job_id })
      setRestoreArchive(null)
      toast.success("Verified restore queued", { description: "The controller downloads, validates, safely extracts, and atomically publishes the run." })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Restore was not queued", { description: errorMessage(error) }),
  })
  const removeArchive = useMutation({
    mutationFn: (archive: ClusterArchive) => api<{ job: ClusterJob }>(`/cluster/archives/${archive.archive_id}`, {
      method: "DELETE",
      body: JSON.stringify({ confirm: true, operator: operator.trim() }),
    }),
    onSuccess: ({ job }) => {
      localStorage.setItem("posetestbot.clusterOperator", operator.trim())
      setRemoteOperation({ kind: "delete", jobId: job.job_id })
      setDeleteArchive(null)
      setDeleteArchiveConfirmed(false)
      toast.success("Cluster archive deletion queued", { description: `Job ${job.job_id} continues after navigation.` })
      void queryClient.invalidateQueries({ queryKey: ["cluster-archives"] })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Cluster archive deletion was not queued", { description: errorMessage(error) }),
  })
  const source = runs.find((run) => run.path === effectiveSourcePath) ?? null
  const integration = archives.data?.integration
  const storage = archives.data?.storage
  const storageMutationEnabled = storage?.mutation ?? Boolean(integration?.enabled)
  const storageConnected = storage?.ready ?? Boolean(integration?.enabled)
  const mutationReady = Boolean(integration?.enabled && storageConnected && storageMutationEnabled && operator.trim().length >= 2)
  const remoteOnlyCount = (archives.data?.archives ?? []).filter((archive) => !runs.some((run) => run.path === archive.source_run_root)).length
  const remoteOperationActive = Boolean(remoteOperation && !remoteJob.data?.job.terminal)
  const restoreNameValid = validRunFolderName(destinationName)
  const restoreDestinationPath = destinationRoot && restoreNameValid ? runFolderPath(destinationRoot, destinationName) : null
  const restoreCollision = Boolean(restoreDestinationPath && runs.some((run) => normalizeRunPath(run.path) === normalizeRunPath(restoreDestinationPath)))

  return <Card data-testid="cluster-storage-section" className="border-primary/25">
    <CardHeader>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><CardTitle className="flex items-center gap-2"><Archive className="size-5 text-primary-strong" />Cluster storage</CardTitle><CardDescription className="mt-1">Copy complete runs to durable PROJECT storage, restore verified archives, or permanently remove an archive. This capability is independent of every pose-estimator runtime.</CardDescription></div>
        {storage && <StatusBadge status={storage.ready ? storage.mutation ? "ready" : "read-only" : "unavailable"} tone={storage.ready ? storage.mutation ? "success" : "warning" : "destructive"} />}
        <Button variant="outline" size="sm" onClick={() => archives.refetch()} disabled={archives.isFetching}><RefreshCw className={archives.isFetching ? "animate-spin" : undefined} />Refresh remote inventory</Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-4">
      {archives.isError
        ? <div className="rounded-lg border bg-muted/25 p-3 text-xs text-muted-foreground">Cluster storage is unavailable: {errorMessage(archives.error)}. Local run-folder controls remain available.</div>
        : <>
          {integration && !integration.enabled && <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warning/35 bg-warning/5 p-3 text-xs text-warning-foreground"><div><div className="font-semibold">Cluster integration is disabled</div><div className="mt-1">Configure or start the fixed companion from Dashboard before archive copy, restore, or deletion.</div></div><Button asChild size="sm" variant="outline"><Link to="/dashboard">Open Dashboard</Link></Button></div>}
          {storage && !storageMutationEnabled && <div className="rounded-lg border border-warning/35 bg-warning/5 p-3 text-xs text-warning-foreground"><div className="font-semibold">Archive mutation is disabled</div><div className="mt-1">{storage.blockers.join(" ") || "Enable archive mutation in the controller; estimator qualification is not required."}</div></div>}
          {storage && !storage.ready && <div className="rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs text-destructive"><div className="font-semibold">Cluster storage is not ready</div><div className="mt-1">{storage.blockers.join(" ") || "The transfer connection or project quota check is unavailable."}</div></div>}
          <div className="grid gap-3 xl:grid-cols-[minmax(260px,1fr)_minmax(220px,0.65fr)_auto]">
            <div className="space-y-1.5"><Label>Local archive source</Label><Select value={effectiveSourcePath} onValueChange={setSourcePath}><SelectTrigger aria-label="Local archive source"><SelectValue placeholder="Choose a local run" /></SelectTrigger><SelectContent>{runs.map((run) => <SelectItem value={run.path} key={run.path}>{run.name} · {formatBytes(run.size_bytes)}</SelectItem>)}</SelectContent></Select></div>
            <div className="space-y-1.5"><Label htmlFor="archive-operator">Operator</Label><Input id="archive-operator" value={operator} onChange={(event) => setOperator(event.target.value)} placeholder="Name or lab account" maxLength={120} /></div>
            <div className="flex items-end"><Button variant="outline" disabled={!source || !mutationReady || createArchive.isPending} onClick={() => source && createArchive.mutate(source)}><CloudUpload />Archive copy</Button></div>
          </div>
          <div className="flex flex-wrap gap-2 text-[11px] text-muted-foreground"><span>{(archives.data?.archives.length ?? 0)} remote archives</span><span>·</span><span>{remoteOnlyCount} remote-only</span><span>·</span><span>PROJECT is durable project storage, not a backup tier</span></div>
          {remoteOperationActive && <div className="flex items-center justify-between gap-3 rounded-lg border border-warning/35 bg-warning/5 p-3 text-xs"><span className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin text-warning-foreground" />{remoteOperation?.kind === "delete" ? "Archive deletion" : "Verified restore"} is running in the controller.</span><Button asChild variant="outline" size="sm"><Link to="/jobs">Open Jobs</Link></Button></div>}
          <div className="max-h-[400px] overflow-auto rounded-lg border">
            <table className="w-full min-w-[1040px] text-left text-xs"><thead className="sticky top-0 border-b bg-muted text-[9px] font-bold uppercase tracking-wider text-muted-foreground"><tr><th className="px-3 py-2">Archive</th><th className="px-3 py-2">Source identity</th><th className="px-3 py-2">Verification</th><th className="px-3 py-2">Created</th><th className="px-3 py-2 text-right">Actions</th></tr></thead><tbody className="divide-y">{(archives.data?.archives ?? []).map((archive) => {
              const local = runs.find((run) => run.path === archive.source_run_root && run.identity.device === archive.source_identity.device && run.identity.inode === archive.source_identity.inode)
              return <tr key={archive.archive_id}><td className="px-3 py-3"><div className="font-mono font-semibold">{archive.archive_id}</div><div className="mt-1 max-w-[360px] truncate font-mono text-[10px] text-muted-foreground" title={archive.source_run_root}>{archive.source_run_root}</div></td><td className="px-3 py-3"><div>{local ? "Exact local source present" : "Remote-only or local identity changed"}</div><div className="mt-1 font-mono text-[10px] text-muted-foreground">dev {archive.source_identity.device} · ino {archive.source_identity.inode}</div></td><td className="px-3 py-3"><StatusBadge status={archive.state} tone={archiveTone(archive.state)} />{archive.verified && <div className="mt-1 flex items-center gap-1 text-[10px] text-success"><ShieldCheck className="size-3" />Receipt and hashes verified</div>}</td><td className="px-3 py-3">{formatDate(archive.created_at)}</td><td className="px-3 py-3"><div className="flex justify-end gap-2"><Button variant="outline" size="sm" disabled={!archive.verified || !mutationReady || remoteOperationActive} onClick={() => { setRestoreArchive(archive); setDestinationRoot(inventory.roots.find((root) => root.exists)?.path ?? ""); setDestinationName(archive.source_run_root.split("/").at(-1) ?? "") }}><CloudDownload />Restore</Button><Button variant="destructive" size="sm" disabled={!archive.verified || !mutationReady || remoteOperationActive || removeArchive.isPending} onClick={() => { setDeleteArchive(archive); setDeleteArchiveConfirmed(false) }}><Trash2 />Delete</Button></div></td></tr>
            })}{!archives.isPending && !(archives.data?.archives.length) && <tr><td className="px-3 py-6 text-center text-muted-foreground" colSpan={5}>No remote archives yet.</td></tr>}</tbody></table>
          </div>
          <p className="text-[10px] text-muted-foreground">Archive, restore, and deletion jobs continue after navigation. Monitor their controller-owned status from Jobs.</p>
        </>}
    </CardContent>

    <Dialog open={Boolean(restoreArchive)} onOpenChange={(open) => !open && !restore.isPending && setRestoreArchive(null)}><DialogContent data-testid="cluster-archive-restore-dialog"><DialogHeader><DialogTitle>Restore verified archive</DialogTitle><DialogDescription>The controller downloads into temporary storage, verifies the archive and every regular file, then atomically publishes the run below an approved root.</DialogDescription></DialogHeader><div className="space-y-3"><div className="space-y-1.5"><Label htmlFor="restore-destination-root">Destination root</Label><Select value={destinationRoot} onValueChange={setDestinationRoot}><SelectTrigger id="restore-destination-root" aria-label="Destination root"><SelectValue /></SelectTrigger><SelectContent>{inventory.roots.filter((root) => root.exists).map((root) => <SelectItem value={root.path} key={root.path}>{root.path} · {formatBytes(root.storage.free_bytes)} free</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label htmlFor="restore-name">Run folder name</Label><Input id="restore-name" aria-invalid={!restoreNameValid || restoreCollision} aria-describedby={!restoreNameValid || restoreCollision ? "restore-name-error" : undefined} value={destinationName} onChange={(event) => setDestinationName(event.target.value)} />{(!restoreNameValid || restoreCollision) && <p id="restore-name-error" role="alert" className="text-xs text-destructive">{restoreCollision ? "This destination run folder already exists. Choose another name or use the local run." : "Use one folder name only; paths, “.”, and “..” are not allowed."}</p>}</div></div><DialogFooter><Button variant="outline" onClick={() => setRestoreArchive(null)} disabled={restore.isPending}>Cancel</Button><Button onClick={() => restoreArchive && restore.mutate(restoreArchive)} disabled={!restoreArchive || !destinationRoot || !restoreNameValid || restoreCollision || !mutationReady || restore.isPending}>{restore.isPending ? <LoaderCircle className="animate-spin" /> : <CloudDownload />}Queue verified restore</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={Boolean(deleteArchive)} onOpenChange={(open) => { if (!open && !removeArchive.isPending) { setDeleteArchive(null); setDeleteArchiveConfirmed(false) } }}>
      <DialogContent data-testid="cluster-archive-delete-dialog">
        <DialogHeader>
          <DialogTitle>Permanently delete cluster archive?</DialogTitle>
          <DialogDescription>This removes the selected archived run from cluster PROJECT storage. It does not delete the local acquisition folder. The archive can no longer be restored after the queued controller job succeeds.</DialogDescription>
        </DialogHeader>
        {deleteArchive && <div className="space-y-3">
          <div className="flex items-start gap-3 rounded-lg border border-destructive/45 bg-destructive/10 p-4"><AlertTriangle aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-destructive" /><div className="min-w-0"><div className="font-semibold text-destructive">Permanent remote-data deletion</div><div className="mt-2 break-all font-mono text-[10px] font-semibold">{deleteArchive.archive_id}</div><div className="mt-1 break-all font-mono text-[10px] text-muted-foreground">Source: {deleteArchive.source_run_root}</div></div></div>
          <Label className="flex items-start gap-3 rounded-lg border p-3"><Checkbox data-testid="cluster-archive-delete-confirmation" checked={deleteArchiveConfirmed} onCheckedChange={(checked) => setDeleteArchiveConfirmed(checked === true)} /><span>I confirm that this verified cluster archive should be permanently deleted and will no longer be available for restore.</span></Label>
        </div>}
        <DialogFooter><Button variant="outline" onClick={() => { setDeleteArchive(null); setDeleteArchiveConfirmed(false) }} disabled={removeArchive.isPending}>Cancel</Button><Button variant="destructive" onClick={() => deleteArchive && removeArchive.mutate(deleteArchive)} disabled={!deleteArchive || !deleteArchiveConfirmed || !mutationReady || removeArchive.isPending}>{removeArchive.isPending ? <LoaderCircle className="animate-spin" /> : <Trash2 />}Queue archive deletion</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </Card>
}

function RunDetails({ run }: { run: RunFolder }) {
  const breakdown = Object.entries(run.breakdown).sort(([, left], [, right]) => right.size_bytes - left.size_bytes)
  return <details className="group">
    <summary className="cursor-pointer text-[11px] font-semibold text-primary-strong underline-offset-4 hover:underline">Storage breakdown and provenance</summary>
    <div className="mt-3 grid gap-3 border-l-2 border-primary/20 pl-3 xl:grid-cols-2">
      <div>
        <div className="text-[10px] font-bold uppercase tracking-[0.1em] text-muted-foreground">Breakdown</div>
        {breakdown.length
          ? <dl className="mt-1.5 space-y-1">{breakdown.map(([name, value]) => <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 text-[10px]" key={name}><dt className="truncate" title={name}>{titleCase(name)}</dt><dd className="font-mono text-muted-foreground">{formatBytes(value.size_bytes)} · {plural(value.file_count, "file")}</dd></div>)}</dl>
          : <div className="mt-1 text-[10px] text-muted-foreground">No category breakdown available.</div>}
      </div>
      <div>
        <div className="text-[10px] font-bold uppercase tracking-[0.1em] text-muted-foreground">Filesystem scan</div>
        <div className="mt-1.5 text-[10px] text-muted-foreground">{plural(run.directory_count, "directory")} · {plural(run.file_count, "file")} · {plural(run.symlink_count, "symlink")} · {formatBytes(run.allocated_bytes)} allocated</div>
        {run.scan_errors.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-4 text-[10px] text-destructive">{run.scan_errors.slice(0, 10).map((error, index) => <li key={`${index}:${error}`}>{error}</li>)}{run.scan_errors.length > 10 && <li>{run.scan_errors.length - 10} more scan errors; inspect the inventory job log for full context.</li>}</ul>}
      </div>
    </div>
  </details>
}

export function RunFoldersPage() {
  const queryClient = useQueryClient()
  const { bootstrap, currentWorkflow, runs: indexedRuns, selectedRun, selectRun } = useOperator()
  const [moveTarget, setMoveTarget] = useState<RunFolder | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<RunFolder | null>(null)
  const [destinationRoot, setDestinationRoot] = useState("")
  const [runSearch, setRunSearch] = useState("")
  const [runRootFilter, setRunRootFilter] = useState("all")
  const [runSort, setRunSort] = useState("recent")
  const [storageSearch, setStorageSearch] = useState("")
  const [newRunRoot, setNewRunRoot] = useState(() => runRootForPath(selectedRun, bootstrap.allowed_run_roots))
  const [newRunFolderName, setNewRunFolderName] = useState("")
  const [trackedOperation, setTrackedOperation] = useState<TrackedOperation | null>(null)
  const automaticRefreshSignature = useRef<string | null>(null)
  const handledTerminalJob = useRef<string | null>(null)
  const workflowHref = currentWorkflow ? activeWorkflowHref(currentWorkflow) : "/workflow/setup"

  const inventory = useQuery({
    queryKey: ["run-folders"],
    queryFn: () => api<RunFolderInventory>("/ui/run-folders"),
    refetchInterval: (state) => state.state.data?.inventory_state === "refreshing" && !state.state.data.refresh_job ? 1_000 : false,
  })

  const refreshInventory = useMutation({
    mutationFn: (automatic: boolean) => api<JobSubmission>("/ui/run-folders/refresh", { method: "POST", body: "{}" }).then((result) => ({ result, automatic })),
    onSuccess: ({ result, automatic }) => {
      setTrackedOperation({ kind: "refresh", job: result.job })
      if (!automatic) toast.success("Run-folder inventory refresh queued", { description: `Job ${result.job_id} continues in the background.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Run-folder inventory could not be refreshed", { description: errorMessage(error) }),
  })

  const inventoryRefreshOperation: TrackedOperation | null = inventory.data?.refresh_job && ACTIVE_JOB_STATUSES.has(inventory.data.refresh_job.status)
    ? { kind: "refresh", job: inventory.data.refresh_job }
    : null
  const inventoryStorageOperation = inventory.data?.operation_job && ACTIVE_JOB_STATUSES.has(inventory.data.operation_job.status)
    ? operationFromJob(inventory.data.operation_job)
    : null
  const effectiveOperation = trackedOperation ?? inventoryStorageOperation ?? inventoryRefreshOperation

  useEffect(() => {
    const data = inventory.data
    if (!data) return
    const refreshNeeded = data.inventory_state === "missing" || data.inventory_state === "stale" || data.stale
    if (!refreshNeeded) {
      automaticRefreshSignature.current = null
      return
    }
    const recoveryNeedsAttention = (data.maintenance?.unresolved_count ?? 0) > 0
    if (recoveryNeedsAttention || data.inventory_state === "refreshing" || effectiveOperation || refreshInventory.isPending) return
    const signature = `${data.inventory_state}:${data.generated_at ?? "never"}`
    if (automaticRefreshSignature.current === signature) return
    automaticRefreshSignature.current = signature
    refreshInventory.mutate(true)
  }, [effectiveOperation, inventory.data, refreshInventory])

  const operationJob = useQuery({
    queryKey: ["run-folder-operation-job", effectiveOperation?.job.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${effectiveOperation!.job.id}`),
    enabled: Boolean(effectiveOperation),
    refetchInterval: (state) => ACTIVE_JOB_STATUSES.has(state.state.data?.job.status ?? effectiveOperation?.job.status ?? "") ? 1_000 : false,
  })
  const currentOperationJob = operationJob.data?.job ?? effectiveOperation?.job ?? null
  const operationActive = Boolean(currentOperationJob && ACTIVE_JOB_STATUSES.has(currentOperationJob.status))
  const operationBlocking = Boolean(effectiveOperation)

  useEffect(() => {
    if (!effectiveOperation || !currentOperationJob || !TERMINAL_JOB_STATUSES.has(currentOperationJob.status)) return
    const key = `${currentOperationJob.id}:${currentOperationJob.status}`
    if (handledTerminalJob.current === key) return
    handledTerminalJob.current = key
    const refreshQueries = Promise.all([
      queryClient.invalidateQueries({ queryKey: ["run-folders"] }),
      queryClient.invalidateQueries({ queryKey: ["runs"] }),
      queryClient.invalidateQueries({ queryKey: ["jobs"] }),
    ])
    if (currentOperationJob.status === "succeeded") {
      const result = operationResult(currentOperationJob)
      if (effectiveOperation.kind === "move" && result?.source_cleanup_complete === false) {
        toast.warning("Run folder moved; source cleanup remains", {
          description: typeof result.source_cleanup_warning === "string"
            ? `${result.source_cleanup_warning} Recovery will retry during the next inventory refresh.`
            : "Recovery will retry during the next inventory refresh.",
        })
      } else {
        toast.success(
          effectiveOperation.kind === "refresh" ? "Run-folder inventory refreshed" : effectiveOperation.kind === "move" ? "Run folder moved" : "Run folder deleted",
          { description: effectiveOperation.kind === "move" && effectiveOperation.destinationRunRoot ? effectiveOperation.destinationRunRoot : effectiveOperation.runName },
        )
      }
    } else {
      toast.error(
        effectiveOperation.kind === "refresh" ? "Run-folder inventory refresh did not complete" : effectiveOperation.kind === "move" ? "Run folder was not moved" : "Run folder was not deleted",
        { description: currentOperationJob.message ?? `Job ${currentOperationJob.id} ended with status ${currentOperationJob.status}.` },
      )
    }
    void refreshQueries.finally(() => {
      setTrackedOperation((current) => current?.job.id === currentOperationJob.id ? null : current)
    })
  }, [currentOperationJob, effectiveOperation, queryClient])

  const moveRun = useMutation({
    mutationFn: ({ run, root }: { run: RunFolder; root: string }) => {
      const destination = inventory.data?.roots.find((item) => item.path === root)
      if (!destination?.identity) throw new Error("Refresh inventory before selecting this destination root")
      return api<RunFolderMutationResponse>("/ui/run-folders/move", {
        method: "POST",
        body: JSON.stringify({
          run_root: run.path,
          destination_root: root,
          expected_identity: run.identity,
          expected_destination_root_identity: destination.identity,
        }),
      })
    },
    onSuccess: (result, variables) => {
      setTrackedOperation({
        kind: "move",
        runName: variables.run.name,
        sourceRunRoot: result.source_run_root,
        destinationRunRoot: result.destination_run_root,
        job: result.job,
      })
      setMoveTarget(null)
      toast.success("Run-folder move queued", { description: `Job ${result.job_id} continues in the background and is visible in Jobs.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Run folder could not be moved", { description: errorMessage(error) }),
  })

  const deleteRun = useMutation({
    mutationFn: (run: RunFolder) => api<RunFolderMutationResponse>("/ui/run-folders", {
      method: "DELETE",
      body: JSON.stringify({
        run_root: run.path,
        confirm: true,
        expected_identity: run.identity,
      }),
    }),
    onSuccess: (result, run) => {
      setTrackedOperation({ kind: "delete", runName: run.name, sourceRunRoot: result.source_run_root, job: result.job })
      setDeleteTarget(null)
      toast.success("Run-folder deletion queued", { description: `Job ${result.job_id} continues in the background and is visible in Jobs.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Run folder could not be deleted", { description: errorMessage(error) }),
  })

  const runs = useMemo(
    () => [...(inventory.data?.runs ?? [])].sort((left, right) => right.size_bytes - left.size_bytes || right.modified_at.localeCompare(left.modified_at)),
    [inventory.data?.runs],
  )
  const selectableRuns = useMemo(() => {
    const values = (inventory.data?.runs ?? []).filter((run) => {
      if (runRootFilter !== "all" && run.root !== runRootFilter) return false
      return runMatchesSearch(run, runSearch)
    })
    return [...values].sort((left, right) => {
      if (runSort === "name") return runDisplayName(left).localeCompare(runDisplayName(right)) || left.path.localeCompare(right.path)
      if (runSort === "size") return right.size_bytes - left.size_bytes || right.modified_at.localeCompare(left.modified_at)
      return right.modified_at.localeCompare(left.modified_at) || left.path.localeCompare(right.path)
    })
  }, [inventory.data?.runs, runRootFilter, runSearch, runSort])
  const storageRuns = useMemo(
    () => runs.filter((run) => runMatchesSearch(run, storageSearch)),
    [runs, storageSearch],
  )
  const totalSize = runs.reduce((total, run) => total + run.size_bytes, 0)
  const totalFiles = runs.reduce((total, run) => total + run.file_count, 0)
  const activeInventoryRun = runs.find((run) => isSelectedRunFolder(run, selectedRun))
  const activeIndexedRun = indexedRuns.find((run) => run.path === selectedRun)
  const activeFolderName = selectedRun.split("/").filter(Boolean).at(-1) ?? selectedRun
  const activeRunName = activeInventoryRun?.config.run_name ?? activeIndexedRun?.run_name ?? activeFolderName
  const activeConfigured = activeInventoryRun?.config.valid ?? activeIndexedRun?.config_valid ?? false
  const maintenanceBlocking = (inventory.data?.maintenance?.unresolved_count ?? 0) > 0
  const destinationRoots = (inventory.data?.roots ?? []).filter((root) => root.path !== moveTarget?.root)
  const inventoryRefreshing = inventory.data?.inventory_state === "refreshing"
    || Boolean(inventory.data?.refresh_job && ACTIVE_JOB_STATUSES.has(inventory.data.refresh_job.status))
    || refreshInventory.isPending
    || Boolean(effectiveOperation?.kind === "refresh" && operationActive)
  const inventoryReadyForMutation = inventory.data?.inventory_state === "ready"
    && inventory.data.stale === false
    && !inventoryRefreshing
  const newRunInventoryReady = inventoryReadyForMutation
    && !inventory.isError
    && !operationBlocking
    && !maintenanceBlocking
  const newRunNameValid = validRunFolderName(newRunFolderName)
  const proposedNewRunPath = newRunNameValid ? runFolderPath(newRunRoot, newRunFolderName) : null
  const normalizedProposedNewRunPath = proposedNewRunPath ? normalizeRunPath(proposedNewRunPath) : null
  const knownRunPaths = [
    ...runs.map((run) => run.path),
    ...indexedRuns.map((run) => run.path),
    selectedRun,
  ]
  const newRunCollision = Boolean(normalizedProposedNewRunPath && knownRunPaths.some((path) => normalizeRunPath(path) === normalizedProposedNewRunPath))

  const openMove = (run: RunFolder) => {
    const target = (inventory.data?.roots ?? []).find((root) => root.path !== run.root && root.exists && root.identity)
    setDestinationRoot(target?.path ?? "")
    setMoveTarget(run)
  }

  const activateRun = (path: string, label: string) => {
    if (!selectRun(path)) {
      toast.error("Run folder must stay inside an allowed storage root")
      return
    }
    toast.success("Active run changed", { description: `${label} is now used by every run-owned page and action.` })
  }

  const activateNewRun = () => {
    if (!newRunInventoryReady) {
      toast.error("Current run-folder inventory is required", { description: "Refresh inventory before selecting a new acquisition folder." })
      return
    }
    if (!newRunNameValid || !proposedNewRunPath) {
      toast.error("Run folder name must be one folder, not a path")
      return
    }
    if (newRunCollision) {
      toast.error("Run folder already exists", { description: "Choose the existing run below or enter a different folder name." })
      return
    }
    if (!selectRun(proposedNewRunPath)) {
      toast.error("Run folder must stay inside an allowed storage root")
      return
    }
    setNewRunFolderName("")
    toast.success("New acquisition folder selected", { description: "Workflow creates and configures the folder when setup is saved." })
  }

  return <div className="space-y-6">
    <PageHeader
      eyebrow="Active run and storage"
      title="Run folders"
      description="Choose the acquisition folder used by every run-owned page, start a fresh folder for the next capture, and manage existing run storage."
      actions={<Button variant="outline" onClick={() => refreshInventory.mutate(false)} disabled={inventoryRefreshing || operationBlocking}><RefreshCw className={inventoryRefreshing ? "animate-spin" : undefined} />Refresh inventory</Button>}
    />
    {inventory.data
      ? <ClusterStorageSection inventory={inventory.data} />
      : <Card data-testid="cluster-storage-section" className="border-primary/25">
          <CardHeader><CardTitle className="flex items-center gap-2"><Archive className="size-5 text-primary-strong" />Cluster storage</CardTitle><CardDescription>Copy complete runs to durable PROJECT storage, restore verified archives, or permanently remove an archive. This capability is independent of every pose-estimator runtime.</CardDescription></CardHeader>
          <CardContent>{inventory.isPending
            ? <div className="space-y-2" aria-label="Loading local run inventory for cluster storage"><Skeleton className="h-10 w-full" /><Skeleton className="h-16 w-full" /></div>
            : <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs text-destructive"><span>Cluster storage cannot select a local source because run-folder inventory is unavailable: {errorMessage(inventory.error)}</span><Button variant="outline" size="sm" onClick={() => inventory.refetch()}><RefreshCw />Retry inventory</Button></div>}
          </CardContent>
        </Card>}
    <Card data-testid="active-run-selection" className="border-primary/35 bg-primary/5">
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><CardTitle>Active acquisition run</CardTitle><CardDescription className="mt-1">This is the storage and provenance context used by Workflow, Cell View, BOP Evaluation, Jobs, and every other run-owned action.</CardDescription></div>
          <StatusBadge status={activeConfigured ? "configured" : "not configured"} tone={activeConfigured ? "success" : "warning"} />
        </div>
      </CardHeader>
      <CardContent className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(420px,0.95fr)]">
        <div className="rounded-lg border bg-card p-4" data-testid="current-run-details">
          <div className="grid gap-4 md:grid-cols-[minmax(180px,0.45fr)_minmax(0,1fr)]">
            <div><div className="text-[9px] font-bold uppercase tracking-[0.13em] text-muted-foreground">Run name</div><div className="mt-1 break-words font-display text-xl font-semibold" data-testid="run-folder-active-name">{activeRunName}</div><p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">Human-readable metadata. It defaults to the folder name when Workflow setup is saved.</p></div>
            <div><div className="text-[9px] font-bold uppercase tracking-[0.13em] text-muted-foreground">Run folder</div><div className="mt-1 font-mono text-sm font-semibold">{activeFolderName}</div><div className="mt-1 break-all font-mono text-[10px] leading-relaxed text-muted-foreground" data-testid="run-folder-active-path">{selectedRun}</div><p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">Filesystem boundary for one acquisition and all of its raw and derived evidence.</p></div>
          </div>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t pt-4"><p className="max-w-2xl text-[11px] leading-relaxed text-muted-foreground">Use one sibling folder per physical acquisition. A multi-object template may place several objects in that single capture.</p><Button asChild size="sm"><Link to={workflowHref}>Open active run in Workflow</Link></Button></div>
        </div>
        <form className="rounded-lg border bg-card p-4" onSubmit={(event) => { event.preventDefault(); activateNewRun() }}>
          <div className="flex items-start gap-3"><span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary/10"><FolderPlus className="size-4 text-primary-strong" /></span><div><h2 className="text-sm font-semibold">Start another acquisition</h2><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">Select a new sibling folder now. It remains unconfigured until you save setup in Workflow; its run name will initially default to this folder name.</p></div></div>
          <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(220px,1fr)_minmax(190px,0.8fr)]">
            <div className="space-y-1.5"><Label htmlFor="new-run-root">Storage root</Label><Select value={newRunRoot} onValueChange={setNewRunRoot}><SelectTrigger id="new-run-root" aria-label="New run storage root"><SelectValue /></SelectTrigger><SelectContent>{bootstrap.allowed_run_roots.map((root) => <SelectItem value={root} key={root}>{root}</SelectItem>)}</SelectContent></Select></div>
            <div className="space-y-1.5"><Label htmlFor="new-run-folder-name">Run folder name</Label><Input id="new-run-folder-name" aria-invalid={Boolean(newRunFolderName) && (!newRunNameValid || newRunCollision)} aria-describedby={Boolean(newRunFolderName) && (!newRunNameValid || newRunCollision) ? "new-run-folder-error" : undefined} value={newRunFolderName} onChange={(event) => setNewRunFolderName(event.target.value)} placeholder="object_A_20260806_001" />{Boolean(newRunFolderName) && (!newRunNameValid || newRunCollision) && <p id="new-run-folder-error" role="alert" className="text-xs text-destructive">{newRunCollision ? "This run folder already exists. Choose it below or enter a new name." : "Use one folder name only; paths, “.”, and “..” are not allowed."}</p>}</div>
          </div>
          <div className="mt-3 rounded-md bg-muted p-3"><div className="text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Resulting folder</div><div className="mt-1 break-all font-mono text-[10px]" data-testid="new-run-path-preview">{proposedNewRunPath ?? `${newRunRoot.replace(/\/+$/, "")}/…`}</div></div>
          <div className="mt-3 flex items-center justify-between gap-3">
            {!newRunInventoryReady && <p id="new-run-inventory-reason" data-testid="new-run-inventory-reason" role="status" className="text-xs text-warning-foreground">A current run-folder inventory is required. Refresh inventory and resolve any active maintenance or storage operation first.</p>}
            <Button className="ml-auto" type="submit" aria-describedby={!newRunInventoryReady ? "new-run-inventory-reason" : undefined} disabled={!newRunInventoryReady || !newRunNameValid || newRunCollision}><FolderPlus />Use new run folder</Button>
          </div>
        </form>
      </CardContent>
    </Card>

    <Card data-testid="run-folder-chooser">
      <CardHeader className="border-b bg-muted/20">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle>Choose an existing run</CardTitle><CardDescription className="mt-1">Search by run name, folder, path, object, sensor, or acquisition intent. Selecting a run changes browser-local context; it does not modify that run.</CardDescription></div><div className="text-[10px] text-muted-foreground">{selectableRuns.length.toLocaleString()} of {runs.length.toLocaleString()} folders shown</div></div>
        <div className="mt-4 grid gap-3 md:grid-cols-[minmax(280px,1fr)_minmax(220px,0.55fr)_180px]">
          <div className="relative"><Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="Search run folders" className="pl-9" value={runSearch} onChange={(event) => setRunSearch(event.target.value)} placeholder="Search runs, objects, sensors…" /></div>
          <Select value={runRootFilter} onValueChange={setRunRootFilter}><SelectTrigger aria-label="Filter run folders by storage root"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All storage roots</SelectItem>{bootstrap.allowed_run_roots.map((root) => <SelectItem value={root} key={root}>{root}</SelectItem>)}</SelectContent></Select>
          <Select value={runSort} onValueChange={setRunSort}><SelectTrigger aria-label="Sort run folders"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="recent">Recently changed</SelectItem><SelectItem value="name">Run name</SelectItem><SelectItem value="size">Largest first</SelectItem></SelectContent></Select>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {inventory.isPending
          ? <div className="space-y-2 p-4"><Skeleton className="h-16" /><Skeleton className="h-16" /><Skeleton className="h-16" /></div>
          : inventory.isError
            ? <div className="p-5 text-sm text-destructive">Run-folder inventory is unavailable: {errorMessage(inventory.error)}</div>
            : selectableRuns.length === 0
              ? <div className="p-6 text-center text-sm text-muted-foreground">{runs.length ? "No run folders match these filters." : "No configured run folders have been discovered yet. Start a new acquisition above."}</div>
              : <div className="max-h-[430px] overflow-auto" data-testid="run-folder-selection-list"><table className="w-full min-w-[980px] text-left text-xs"><thead className="sticky top-0 z-10 border-b bg-muted text-[9px] font-bold uppercase tracking-wider text-muted-foreground"><tr><th className="px-4 py-2.5">Run</th><th className="px-4 py-2.5">Capture contents</th><th className="px-4 py-2.5">Evidence</th><th className="px-4 py-2.5">Last changed</th><th className="px-4 py-2.5 text-right">Selection</th></tr></thead><tbody className="divide-y">{selectableRuns.map((run) => {
                const active = isSelectedRunFolder(run, selectedRun)
                return <tr className={cn(active && "bg-primary/5")} data-testid="run-selection-row" data-run-path={run.path} key={run.path}>
                  <td className="px-4 py-3"><div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{runDisplayName(run)}</span>{active && <StatusBadge status="active" tone="informational">Active run</StatusBadge>}{!run.config.valid && <StatusBadge status="invalid" tone="destructive" />}</div><div className="mt-1 text-[10px] text-muted-foreground">Folder <span className="font-mono">{run.name}</span></div><div className="mt-1 max-w-[420px] truncate font-mono text-[9px] text-muted-foreground" title={run.path}>{run.path}</div></td>
                  <td className="px-4 py-3"><div>{plural(run.contents.object_count, "object instance")} · {run.contents.enabled_sensor_count}/{run.contents.sensor_count} sensors enabled</div><div className="mt-1 max-w-[320px] truncate text-[10px] text-muted-foreground" title={run.contents.object_names.join(" · ")}>{run.contents.object_names.length ? run.contents.object_names.join(" · ") : run.contents.dataset_mode === "objectless" ? "Objectless recording" : "No selected objects"}</div></td>
                  <td className="px-4 py-3"><EvidenceSummary run={run} /></td>
                  <td className="px-4 py-3"><div>{formatDate(run.modified_at)}</div><div className="mt-1 font-mono text-[9px] text-muted-foreground">{formatBytes(run.size_bytes)}</div></td>
                  <td className="px-4 py-3 text-right"><Button size="sm" variant={active ? "secondary" : "outline"} disabled={active} aria-label={active ? `${run.name} is the active run` : `Use ${run.name} as active run`} onClick={() => activateRun(run.path, runDisplayName(run))}>{active ? "Current run" : "Use this run"}</Button></td>
                </tr>
              })}</tbody></table></div>}
      </CardContent>
    </Card>
    <ProcessHandoff
      title="Manage storage here; acquire and process data in Workflow"
      description="Moving or deleting a run changes its managed storage, not its captured configuration. Return to the guided workflow to configure, record, synchronize, or export the active run."
      to={workflowHref}
      action="Open workflow"
    />

    {inventory.data?.maintenance && (inventory.data.maintenance.recovered_count > 0 || inventory.data.maintenance.unresolved_count > 0) && <Card data-testid="run-folder-maintenance" className={inventory.data.maintenance.unresolved_count > 0 ? "border-destructive/45 bg-destructive/5" : "border-primary/35 bg-primary/5"}>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle>{inventory.data.maintenance.unresolved_count > 0 ? "Storage recovery needs attention" : "Interrupted storage work recovered"}</CardTitle>
            <CardDescription className="mt-1">{inventory.data.maintenance.unresolved_count > 0
              ? "PoseTestBot preserved every path it could not verify. Correct the reported filesystem issue, then refresh inventory to retry the durable recovery record."
              : `${plural(inventory.data.maintenance.recovered_count, "interrupted operation")} recovered before this inventory was measured.`}</CardDescription>
          </div>
          <Button asChild size="sm" variant="outline"><Link to="/jobs">Open Jobs</Link></Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {inventory.data.maintenance.transactions.length > 0 && <div className="flex flex-wrap gap-1.5">{inventory.data.maintenance.transactions.map((item) => <StatusBadge key={item.transaction_id} status="recovered" tone="success">{titleCase(item.action)} · {item.transaction_id.slice(0, 8)}</StatusBadge>)}</div>}
        {inventory.data.maintenance.unresolved.length > 0 && <ul className="space-y-2">{inventory.data.maintenance.unresolved.map((item, index) => <li className="rounded border border-destructive/25 bg-card p-3 text-xs" key={item.transaction_id ?? `invalid:${index}`}>
          <div className="flex flex-wrap items-center gap-2"><StatusBadge status="attention" tone="destructive">{item.operation ? titleCase(item.operation) : "Unknown operation"}</StatusBadge><span className="font-semibold">{item.remnant_bytes !== null ? `${formatBytes(item.remnant_bytes)} retained` : "Retained size unavailable"}</span>{item.transaction_id && <span className="font-mono text-[10px] text-muted-foreground">{item.transaction_id}</span>}</div>
          <p className="mt-2 break-words text-muted-foreground">{item.error}</p>
        </li>)}</ul>}
      </CardContent>
    </Card>}

    {effectiveOperation && operationActive && currentOperationJob && <Card data-testid="run-folder-operation-status" className="border-warning/40 bg-warning/5">
      <CardContent className="flex flex-col gap-4 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <LoaderCircle aria-hidden="true" className="mt-0.5 size-5 shrink-0 animate-spin text-warning-foreground" />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{operationTitle(effectiveOperation)}</span><StatusBadge status={currentOperationJob.status} tone={jobTone(currentOperationJob.status)} /></div>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{effectiveOperation.kind === "refresh"
              ? "This background inventory and recovery job continues after navigation and cannot be canceled safely after submission. Jobs shows its resource lock, live output, and final status."
              : "This background storage job continues after navigation and cannot be canceled safely after submission. Jobs shows its resource lock, live output, and final status."}</p>
            {effectiveOperation.kind === "move" && effectiveOperation.sourceRunRoot && <div className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{effectiveOperation.sourceRunRoot} → {effectiveOperation.destinationRunRoot ?? "destination pending"}</div>}
          </div>
        </div>
        <Button asChild size="sm" variant="outline" className="shrink-0 bg-card"><Link to="/jobs">Open Jobs</Link></Button>
      </CardContent>
    </Card>}

    {inventory.data && <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_repeat(3,minmax(130px,auto))]">
      <Card>
        <CardContent className="flex h-full items-center justify-between gap-4 py-4">
          <div><div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Inventory snapshot</div><div className="mt-1 text-sm font-semibold">{inventory.data.generated_at ? formatDate(inventory.data.generated_at) : "Not generated yet"}</div></div>
          <StatusBadge status={inventory.data.inventory_state} tone={inventoryTone(inventory.data.inventory_state)}>{inventory.data.stale ? "stale" : inventory.data.inventory_state}</StatusBadge>
        </CardContent>
      </Card>
      <Card><CardContent className="py-4"><div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Run folders</div><div className="mt-1 font-display text-xl font-semibold">{runs.length.toLocaleString()}</div></CardContent></Card>
      <Card><CardContent className="py-4"><div className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Measured size <HelpTip label="measured run-folder size">Logical byte size of all regular files in each run. Allocated disk usage and scan errors remain available in row details.</HelpTip></div><div className="mt-1 font-display text-xl font-semibold">{formatBytes(totalSize)}</div></CardContent></Card>
      <Card><CardContent className="py-4"><div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Files indexed</div><div className="mt-1 font-display text-xl font-semibold">{totalFiles.toLocaleString()}</div></CardContent></Card>
    </div>}

    {inventory.data && <section aria-labelledby="run-folder-roots-heading" className="space-y-3">
      <div><h2 id="run-folder-roots-heading" className="font-display text-lg font-semibold">Allowed roots and capacity</h2><p className="mt-1 text-xs text-muted-foreground">Moves preserve the run-folder name and target one of these configured roots. Capacity is reported by the filesystem containing each root.</p></div>
      <div className="grid gap-3 lg:grid-cols-2">{inventory.data.roots.map((root) => <RootCapacity key={root.path} root={root} />)}</div>
    </section>}

    {inventory.isPending
      ? <div className="space-y-3">{Array.from({ length: 5 }).map((_, index) => <Skeleton className="h-40" key={index} />)}</div>
      : inventory.isError
        ? <Card className="border-destructive/40"><CardHeader><CardTitle>Run-folder inventory unavailable</CardTitle><CardDescription>{errorMessage(inventory.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={() => inventory.refetch()}><RefreshCw />Try again</Button></CardContent></Card>
        : runs.length === 0
          ? <EmptyState icon={FolderOpen} title={inventoryRefreshing ? "Run-folder inventory is being measured" : "No run folders found"} description={inventoryRefreshing ? "The background inventory job is measuring allowed roots. This page updates when it completes." : "Configured acquisition runs will appear after they contain a run configuration or dataset manifest and the inventory is refreshed."} />
          : <Card>
            <CardHeader className="border-b bg-muted/20">
              <div className="flex flex-wrap items-end justify-between gap-3"><div><CardTitle>Storage inventory and actions</CardTitle><CardDescription className="mt-1">Detailed folders are shown largest first. Select the active run in the chooser above; move and delete remain unavailable for that active folder.</CardDescription></div><div className="font-mono text-[10px] text-muted-foreground">{storageRuns.length.toLocaleString()} of {runs.length.toLocaleString()} folders · {formatBytes(totalSize)} · {plural(totalFiles, "file")}</div></div>
              <div className="relative mt-4 max-w-2xl"><Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input aria-label="Search storage inventory" className="bg-card pl-9" value={storageSearch} onChange={(event) => setStorageSearch(event.target.value)} placeholder="Search run name, folder, path, object, sensor, or evidence…" /></div>
            </CardHeader>
            <CardContent className="p-0">
              <div data-testid="run-folders-table" className="max-h-[720px] overflow-auto">
                <table className="w-full min-w-[1280px] table-fixed text-left">
                  <thead className="sticky top-0 z-10 border-b bg-muted text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">
                    <tr><th className="w-[19%] px-4 py-3">Run</th><th className="w-[15%] px-4 py-3">Measured size</th><th className="w-[25%] px-4 py-3">Configuration and contents</th><th className="w-[16%] px-4 py-3">Evidence</th><th className="w-[14%] px-4 py-3">Location</th><th className="w-[11%] px-4 py-3 text-right">Actions</th></tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {storageRuns.map((run) => {
                      const active = isSelectedRunFolder(run, selectedRun)
                      const actionDisabled = active || operationBlocking || maintenanceBlocking || !inventoryReadyForMutation
                      const hasDestination = (inventory.data?.roots ?? []).some((root) => root.exists && root.identity && root.path !== run.root)
                      const canMove = hasDestination && !actionDisabled
                      return <tr data-testid="run-folder-row" data-run-path={run.path} className="align-top" key={run.path}>
                        <td className="px-4 py-4">
                          <div className="flex flex-wrap items-center gap-2"><span className="break-words font-semibold">{run.config.run_name || run.name}</span>{active && <StatusBadge status="active" tone="informational">Active run</StatusBadge>}{!run.scan_complete && <StatusBadge status="partial" tone="warning">Partial scan</StatusBadge>}</div>
                          {run.config.run_name && run.config.run_name !== run.name && <div className="mt-1 text-[10px] text-muted-foreground">Folder {run.name}</div>}
                          <div className="mt-2 break-all font-mono text-[9px] leading-relaxed text-muted-foreground">{run.path}</div>
                          <div className="mt-3"><RunDetails run={run} /></div>
                        </td>
                        <td className="px-4 py-4">
                          <div data-testid="run-folder-size" className="font-display text-xl font-semibold tabular-nums">{formatBytes(run.size_bytes)}</div>
                          <div className="mt-1 text-[10px] text-muted-foreground">{formatBytes(run.allocated_bytes)} allocated</div>
                          <div className="mt-2 text-[10px] text-muted-foreground">{plural(run.file_count, "file")} · {plural(run.directory_count, "directory")}</div>
                          {run.scan_error_count > 0 && <div className="mt-2 flex items-start gap-1 text-[10px] leading-relaxed text-warning-foreground"><FileWarning aria-hidden="true" className="mt-0.5 size-3 shrink-0" />{plural(run.scan_error_count, "scan error")}; measured totals may be incomplete.</div>}
                        </td>
                        <td className="px-4 py-4">
                          {run.config.valid
                            ? <><div className="mb-3 flex flex-wrap items-center gap-1.5"><StatusBadge status="configured" tone="success" /><span className="text-[10px] text-muted-foreground">{run.config.intent ? titleCase(run.config.intent) : "Unknown intent"} · {run.config.annotation_mode ? titleCase(run.config.annotation_mode) : "Unknown annotation mode"}</span></div><ContentsSummary run={run} /></>
                            : <div className="space-y-3">
                                <div className="flex items-start gap-2 rounded border border-destructive/35 bg-destructive/5 p-3 text-[11px] leading-relaxed text-destructive"><AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" /><div><div className="font-semibold">Invalid run configuration</div><div className="mt-1 break-words text-muted-foreground">{run.config.error ?? "The configuration could not be read."}</div></div></div>
                                <ContentsSummary run={run} />
                              </div>}
                        </td>
                        <td className="px-4 py-4"><EvidenceSummary run={run} /></td>
                        <td className="px-4 py-4">
                          <div className="truncate font-mono text-[10px] font-semibold" title={run.root}>{run.root}</div>
                          <div className="mt-2 flex items-center gap-1 text-[10px] text-muted-foreground"><Clock3 aria-hidden="true" className="size-3" />{formatDate(run.modified_at)}</div>
                        </td>
                        <td className="px-4 py-4">
                          <div className="flex flex-col items-stretch gap-2">
                            <Button size="sm" variant="outline" aria-label={`Move ${run.name}`} disabled={!canMove} onClick={() => openMove(run)}><MoveRight />Move</Button>
                            <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" aria-label={`Delete ${run.name}`} disabled={actionDisabled} onClick={() => setDeleteTarget(run)}><Trash2 />Delete</Button>
                            {active && <p data-testid="run-folder-active-action-reason" className="text-left text-[10px] leading-relaxed text-warning-foreground">Switch the active run folder before moving or deleting this folder.</p>}
                            {!active && maintenanceBlocking && <p className="text-left text-[10px] leading-relaxed text-destructive">Resolve the storage-recovery issue above before changing run folders.</p>}
                            {!active && !maintenanceBlocking && !inventoryReadyForMutation && <p data-testid="run-folder-inventory-action-reason" className="text-left text-[10px] leading-relaxed text-warning-foreground">Wait for a current inventory before moving or deleting this folder.</p>}
                            {!active && !hasDestination && <p className="text-left text-[10px] leading-relaxed text-muted-foreground">No other available allowed root.</p>}
                          </div>
                        </td>
                      </tr>
                    })}
                    {storageRuns.length === 0 && <tr><td className="px-4 py-10 text-center text-sm text-muted-foreground" colSpan={6}>No storage rows match this search.</td></tr>}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>}

    <Dialog open={moveTarget !== null} onOpenChange={(open) => { if (!open && !moveRun.isPending) setMoveTarget(null) }}>
      <DialogContent data-testid="run-folder-move-dialog">
        <DialogHeader>
          <DialogTitle>Move {moveTarget?.name ?? "run folder"}?</DialogTitle>
          <DialogDescription>Move the complete folder to another allowed storage root. The operation is serialized as disk work, continues after navigation, and cannot be canceled after submission.</DialogDescription>
        </DialogHeader>
        {moveTarget && <div className="space-y-4">
          <div className="rounded-lg border bg-muted/35 p-3 text-xs">
            <div className="text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Source run folder</div>
            <div className="mt-1 break-all font-mono text-[11px] font-semibold">{moveTarget.path}</div>
            <div className="mt-2 text-muted-foreground">{formatBytes(moveTarget.size_bytes)} measured · {plural(moveTarget.file_count, "file")}</div>
          </div>
          <div className="space-y-2">
            <Label>Destination root</Label>
            <Select value={destinationRoot} onValueChange={setDestinationRoot}>
              <SelectTrigger aria-label="Destination root"><SelectValue placeholder="Choose an allowed root" /></SelectTrigger>
              <SelectContent>{destinationRoots.map((root) => <SelectItem value={root.path} disabled={!root.exists || !root.identity} key={root.path}><span className="flex flex-col gap-0.5"><span className="font-mono">{root.path}</span><span className="text-[9px] text-muted-foreground">{root.exists && root.identity ? `${formatBytes(root.storage.free_bytes)} free` : "Root unavailable; refresh inventory"}</span></span></SelectItem>)}</SelectContent>
            </Select>
          </div>
          {destinationRoot && <div className="rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs"><div className="text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Resulting path</div><div className="mt-1 break-all font-mono font-semibold">{destinationRoot.replace(/\/+$/, "")}/{moveTarget.name}</div></div>}
          <div className="flex items-start gap-3 rounded-lg border border-warning/35 bg-warning/5 p-3 text-xs leading-relaxed"><MoveRight aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning-foreground" /><p>The folder name stays the same. The old path is removed after the destination is durably published, so update any external references.</p></div>
        </div>}
        <DialogFooter><Button variant="outline" onClick={() => setMoveTarget(null)} disabled={moveRun.isPending}>Cancel</Button><Button onClick={() => moveTarget && destinationRoot && moveRun.mutate({ run: moveTarget, root: destinationRoot })} disabled={!moveTarget || !destinationRoot || moveRun.isPending}>{moveRun.isPending ? <LoaderCircle className="animate-spin" /> : <MoveRight />}Queue move</Button></DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !deleteRun.isPending) setDeleteTarget(null) }}>
      <DialogContent data-testid="run-folder-delete-dialog">
        <DialogHeader>
          <DialogTitle>Delete {deleteTarget?.name ?? "run folder"}?</DialogTitle>
          <DialogDescription>This permanently deletes the entire run folder, including raw capture data and all derived evidence. This action cannot be undone or canceled after submission.</DialogDescription>
        </DialogHeader>
        {deleteTarget && <div className="space-y-3">
          <div className="flex items-start gap-3 rounded-lg border border-destructive/45 bg-destructive/10 p-4"><AlertTriangle aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-destructive" /><div><div className="font-semibold text-destructive">Permanent acquisition-data deletion</div><p className="mt-1 text-xs leading-relaxed text-muted-foreground">The background job removes {formatBytes(deleteTarget.size_bytes)} across {plural(deleteTarget.file_count, "file")} from this exact folder:</p><div className="mt-2 break-all font-mono text-[10px] font-semibold">{deleteTarget.path}</div></div></div>
          {deleteTarget.scan_complete || <div className="flex items-start gap-2 rounded border border-warning/35 bg-warning/5 p-3 text-xs"><FileWarning aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning-foreground" /><span>The last inventory scan was incomplete. The deletion target remains the entire folder, not only the measured files.</span></div>}
        </div>}
        <DialogFooter><Button variant="outline" onClick={() => setDeleteTarget(null)} disabled={deleteRun.isPending}>Cancel</Button><Button variant="destructive" onClick={() => deleteTarget && deleteRun.mutate(deleteTarget)} disabled={!deleteTarget || deleteRun.isPending}>{deleteRun.isPending ? <LoaderCircle className="animate-spin" /> : <Trash2 />}Confirm delete</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}
