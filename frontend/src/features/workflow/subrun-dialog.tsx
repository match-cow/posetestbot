import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { GitBranchPlus, LoaderCircle } from "lucide-react"
import { Link, useNavigate } from "react-router-dom"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { api, errorMessage, query } from "@/lib/api"
import type { Job, RunIndexItem, SubrunParent } from "@/lib/contracts"
import { useOperator } from "@/providers/operator-provider"

const FAILED = new Set(["failed", "canceled", "cancelled"])

/**
 * Start a new object-dataset subrun from a configured run: same cameras,
 * calibration snapshot, pose template and placement; only the physical
 * variant (material, environment, part copy) changes.
 */
export function NewSubrunButton({ parentRunRoot, size = "sm" }: { parentRunRoot: string; size?: "sm" | "default" }) {
  const [open, setOpen] = useState(false)
  return <>
    <Button type="button" variant="outline" size={size} onClick={() => setOpen(true)}><GitBranchPlus />New subrun from this run</Button>
    {open && <SubrunDialog parentRunRoot={parentRunRoot} onClose={() => setOpen(false)} />}
  </>
}

function SubrunDialog({ parentRunRoot, onClose }: { parentRunRoot: string; onClose: () => void }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { selectRun } = useOperator()
  const parent = useQuery({
    queryKey: ["subrun-parent", parentRunRoot],
    queryFn: () => api<SubrunParent>(query("/runs/subrun", { run_root: parentRunRoot })),
    retry: false,
  })
  const [folderName, setFolderName] = useState<string | null>(null)
  const [variant, setVariant] = useState("")
  const [job, setJob] = useState<{ id: string; runRoot: string } | null>(null)
  const effectiveFolder = folderName ?? parent.data?.default_folder_name ?? ""
  const create = useMutation({
    mutationFn: () => api<{ job_id: string; run_root: string }>("/runs/subrun", {
      method: "POST",
      body: JSON.stringify({ parent_run_root: parentRunRoot, folder_name: effectiveFolder.trim(), variant: variant.trim() }),
    }),
    onSuccess: (value) => {
      setJob({ id: value.job_id, runRoot: value.run_root })
      toast.success("Subrun creation queued", { description: `Job ${value.job_id} continues after navigation; status and output are available in Jobs.` })
      void queryClient.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Subrun was not queued", { description: errorMessage(error) }),
  })
  const status = useQuery({
    queryKey: ["subrun-job", job?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${job!.id}`),
    enabled: Boolean(job),
    refetchInterval: (state) => ["succeeded", ...FAILED].includes(state.state.data?.job.status ?? "") ? false : 1_000,
  })
  const jobStatus = status.data?.job.status
  useEffect(() => {
    if (jobStatus !== "succeeded" || !job) return
    void queryClient.invalidateQueries({ queryKey: ["runs"] })
    void queryClient.fetchQuery({ queryKey: ["runs"], queryFn: () => api<{ runs: RunIndexItem[] }>("/ui/runs") }).finally(() => {
      selectRun(job.runRoot)
      toast.success("Subrun ready", { description: "Steps 1 and 2 are inherited; continue with readiness." })
      onClose()
      navigate("/workflow/dataset?step=readiness")
    })
  }, [job, jobStatus, navigate, onClose, queryClient, selectRun])

  const variantMissing = !variant.trim()
  const folderMissing = !effectiveFolder.trim()
  const busy = create.isPending || (Boolean(job) && !FAILED.has(jobStatus ?? ""))
  const data = parent.data
  return <Dialog open onOpenChange={(value) => { if (!value && !busy) onClose() }}>
    <DialogContent className="max-w-2xl" data-testid="subrun-dialog">
      <DialogHeader>
        <DialogTitle>New subrun from this run</DialogTitle>
        <DialogDescription>Capture another variant (material, environment, part copy) without repeating setup. The subrun is a new sibling run folder with its own recording; this run is not changed.</DialogDescription>
      </DialogHeader>
      {parent.isPending && <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />Checking what can be reused…</p>}
      {parent.isError && <p role="alert" className="rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs text-destructive" data-testid="subrun-unavailable">{errorMessage(parent.error)}</p>}
      {data && <>
        <div className="rounded-lg border bg-muted/20 p-3 text-xs" data-testid="subrun-reused">
          <p className="font-semibold">Reused from {data.parent_run_name}</p>
          <dl className="mt-2 grid grid-cols-[9rem_minmax(0,1fr)] gap-x-3 gap-y-1.5">
            <dt className="text-muted-foreground">Cameras</dt><dd>{data.capture.sensors.map(s => s.display_name).join(", ")}</dd>
            <dt className="text-muted-foreground">Capture settings</dt><dd>{data.capture.resolution} · {data.capture.fps} fps · {data.capture.velocity_m_s.toFixed(2)} m/s</dd>
            <dt className="text-muted-foreground">Calibration</dt><dd><span className="font-mono">{data.calibration_bundle_sha256.slice(0, 12)}</span> from {data.calibration_sources.map(s => s.run_name ?? s.run_root.split("/").at(-1)).join(", ")}</dd>
            <dt className="text-muted-foreground">Pose template</dt><dd>{data.template_name ?? data.template_uuid.slice(0, 8)} · <span className="font-mono">{data.template_bundle_sha256.slice(0, 12)}</span></dd>
            <dt className="text-muted-foreground">Placement</dt><dd>Same measured placement (translation {data.placement_matrix.slice(0, 3).map(row => row[3].toFixed(1)).join(", ")} mm)</dd>
          </dl>
          <p className="mt-2 text-muted-foreground">Readiness, capture, and processing run again for the subrun. Ground truth stays optional in step 6.</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="subrun-variant">Variant description <span className="text-destructive">Required</span></Label>
          <Textarea id="subrun-variant" maxLength={240} rows={2} placeholder="e.g. anodised aluminium part, diffuse light" value={variant} onChange={e => setVariant(e.target.value)} aria-invalid={variantMissing} aria-describedby="subrun-variant-help" disabled={busy} />
          <p id="subrun-variant-help" className={variantMissing ? "text-[11px] font-medium text-destructive" : "text-[11px] text-muted-foreground"}>{variantMissing ? "Describe what differs in this subrun. Nothing is prefilled." : "Recorded with the subrun so variants stay distinguishable."}</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="subrun-folder">New run folder</Label>
          <Input id="subrun-folder" value={effectiveFolder} onChange={e => setFolderName(e.target.value)} disabled={busy} />
          <p className="text-[11px] text-muted-foreground">Created next to this run in {data.parent_run_root.split("/").slice(0, -1).join("/")}.</p>
        </div>
      </>}
      {job && <div role="status" className="rounded-lg border p-3 text-xs" data-testid="subrun-job-status">
        {FAILED.has(jobStatus ?? "")
          ? <p className="text-destructive">Subrun creation {jobStatus}: {status.data?.job.message ?? "see Jobs for details"}. No partial folder was kept.</p>
          : <p className="flex items-center gap-2"><LoaderCircle className="size-4 animate-spin" />Creating the subrun (job {job.id}). This continues after navigation.</p>}
        <Link className="mt-1 inline-block underline" to={query("/jobs", { search: job.id })}>Open in Jobs</Link>
      </div>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={busy}>Cancel</Button>
        <Button type="button" onClick={() => create.mutate()} disabled={!data || variantMissing || folderMissing || busy}>{busy ? <LoaderCircle className="animate-spin" /> : <GitBranchPlus />}Create subrun</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

/** Lineage banner for a subrun, or an offer to create one from a configured run. */
export function SubrunLineageBanner({ runRoot, lineage }: { runRoot: string; lineage?: { parent_run_root: string; variant: string } | null }) {
  const { runs, selectRun } = useOperator()
  if (!lineage) return null
  const parentName = runs.find(run => run.path === lineage.parent_run_root)?.run_name ?? lineage.parent_run_root.split("/").at(-1)
  return <div className="flex flex-col gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs sm:flex-row sm:items-center sm:justify-between" data-testid="subrun-lineage">
    <div><p className="font-semibold">Subrun of {parentName} · inherited setup</p><p className="mt-0.5 text-muted-foreground">Variant: {lineage.variant}. Cameras, calibration snapshot, pose template and placement were copied from the parent; edits here change only this subrun ({runRoot.split("/").at(-1)}).</p></div>
    <Button type="button" variant="outline" size="sm" onClick={() => selectRun(lineage.parent_run_root)}>Open parent run</Button>
  </div>
}

export function SubrunOffer({ runRoot, available }: { runRoot: string; available: boolean }) {
  if (!available) return null
  return <div className="flex flex-col gap-3 rounded-lg border bg-muted/20 p-4 text-xs sm:flex-row sm:items-center sm:justify-between" data-testid="subrun-offer">
    <div><p className="text-sm font-semibold">Capture another variant</p><p className="mt-1 max-w-3xl text-muted-foreground">Same cameras, calibration and pose-template placement; only the material, environment or part copy changes. A subrun starts directly at readiness in a new sibling run folder.</p></div>
    <NewSubrunButton parentRunRoot={runRoot} />
  </div>
}
