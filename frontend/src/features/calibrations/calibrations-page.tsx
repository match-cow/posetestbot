import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { AlertTriangle, Camera, Crosshair, FolderOpen, Library, LoaderCircle, RefreshCw, Save, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { EmptyState } from "@/components/empty-state"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage } from "@/lib/api"
import { daysSince, formatAge, formatDate } from "@/lib/utils"

interface CalibrationProfile {
  profile_id: string
  /** Date of this camera's capture evidence. */
  calibrated_at: string | null
  promoted_at?: string | null
  sensor_type: string
  sensor_id: string
  sensor_name: string | null
  mounting_mode: string
  status: string
  resolution: [number, number]
  method: string | null
  quality: {
    num_observations: number
    num_inliers: number
    mean_reprojection_error_px: number | null
    residual_translation_mm: number | null
    residual_rotation_deg: number | null
  }
}

interface RunReference {
  run_root: string
  run_name: string
}

interface Calibration {
  bundle_sha256: string
  name: string
  valid: boolean
  issues: string[]
  calibrated_at: string | null
  valid_profile_count: number
  profiles: CalibrationProfile[]
  /** Where the selectable copy lives; "snapshot" means only inside dataset runs. */
  availability: "run" | "library" | "snapshot"
  selectable: boolean
  source_run: RunReference | null
  retained: { path: string; retained_at: string; reason: "run_deleted" | "operator_request" | "promotion"; origin: RunReference & { kind: string } } | null
  snapshot_runs: RunReference[]
  used_by: Array<RunReference & { selected_at: string | null; sensor_keys: string[] }>
}

interface UnusableCalibration {
  name: string
  path: string
  issues: string[]
  availability: "run" | "library"
}

interface CalibrationOverview {
  library_root: string
  age_warning_days?: number | null
  calibrations: Calibration[]
  unusable: UnusableCalibration[]
}

const OVERVIEW_KEY = ["calibration-overview"]

function mountingLabel(value: string) {
  return value === "static" ? "Static" : value === "eye_in_hand" ? "Robot-mounted" : value
}

function Availability({ calibration }: { calibration: Calibration }) {
  if (calibration.availability === "run") {
    return <div>
      <StatusBadge tone="success">Calibration run folder</StatusBadge>
      <div className="mt-2 break-all font-mono text-[9px] leading-relaxed text-muted-foreground">{calibration.source_run?.run_root}</div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">{calibration.retained ? "A copy is already kept in the calibration library." : "Still stored with its capture data. Deleting the run folder asks whether to keep this calibration."}</p>
    </div>
  }
  if (calibration.availability === "library") {
    const origin = calibration.retained?.origin
    const detail = calibration.retained?.reason === "run_deleted"
      ? "Kept when its calibration run folder was deleted"
      : calibration.retained?.reason === "promotion" ? "Kept at publication" : `Kept from ${origin?.run_name ?? "a run"}`
    return <div>
      <StatusBadge tone="informational">Calibration library</StatusBadge>
      <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">{detail} · {formatDate(calibration.retained?.retained_at)}</p>
      {origin && <div className="mt-1 break-all font-mono text-[9px] leading-relaxed text-muted-foreground">from {origin.run_root}</div>}
    </div>
  }
  return <div>
    <StatusBadge tone="warning">Only inside dataset runs</StatusBadge>
    <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">Its calibration run folder is gone. New runs cannot select it until it is kept in the library, and it is lost when the last run holding it is deleted.</p>
  </div>
}

function Cameras({ profiles, newest, ageWarningDays }: { profiles: CalibrationProfile[]; newest: string | null; ageWarningDays?: number | null }) {
  const valid = profiles.filter((profile) => profile.status === "valid")
  if (!valid.length) return <span className="text-[11px] text-destructive">No valid camera profile</span>
  return <ul className="space-y-2">{valid.map((profile) => {
    const error = profile.quality.mean_reprojection_error_px
    const age = daysSince(profile.calibrated_at)
    const older = age !== daysSince(newest)
    const verifyMount = age != null && ageWarningDays != null && age >= ageWarningDays
    return <li key={profile.profile_id} className="text-[11px]">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5"><span className="font-semibold">{profile.sensor_name ?? profile.sensor_id}</span><span className="text-muted-foreground">{mountingLabel(profile.mounting_mode)} · {profile.resolution[0]} × {profile.resolution[1]}</span>{older && <span className="font-semibold text-warning-foreground">{formatAge(profile.calibrated_at)}</span>}</div>
      <div className="mt-0.5 font-mono text-[9px] text-muted-foreground">{profile.sensor_type}:{profile.sensor_id} · {error === null ? "reprojection error not recorded" : `${error.toFixed(2)} px mean reprojection error`} · {profile.quality.num_inliers}/{profile.quality.num_observations} inliers</div>
      {profile.promoted_at && <div className="mt-0.5 text-[10px] text-muted-foreground">Published {formatDate(profile.promoted_at)}</div>}
      {verifyMount && <div className="mt-0.5 text-[10px] font-semibold text-warning-foreground">{ageWarningDays}+ days since capture. Confirm the mount before reuse.</div>}
    </li>
  })}</ul>
}

export function CalibrationsPage() {
  const queryClient = useQueryClient()
  const [deleteTarget, setDeleteTarget] = useState<Calibration | UnusableCalibration | null>(null)
  const overview = useQuery({ queryKey: OVERVIEW_KEY, queryFn: () => api<CalibrationOverview>("/ui/calibrations/library") })

  const refreshLists = () => {
    void queryClient.invalidateQueries({ queryKey: OVERVIEW_KEY })
    // Dataset setup offers the same calibrations as per-camera sources.
    void queryClient.invalidateQueries({ queryKey: ["calibration-library"] })
  }
  const keep = useMutation({
    mutationFn: (calibration: Calibration) => api<{ name: string }>("/ui/calibrations/library/retain", {
      method: "POST",
      body: JSON.stringify({ run_root: calibration.source_run?.run_root ?? calibration.snapshot_runs[0]?.run_root, bundle_sha256: calibration.bundle_sha256 }),
    }),
    onSuccess: (result) => { toast.success("Calibration kept in the library", { description: `${result.name} can now be selected in dataset setup.` }); refreshLists() },
    onError: (error) => { toast.error("Calibration was not kept", { description: errorMessage(error) }); refreshLists() },
  })
  const remove = useMutation({
    mutationFn: (digest: string) => api<unknown>(`/ui/calibrations/library/${digest}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
    onSuccess: () => { toast.success("Calibration deleted from the library"); setDeleteTarget(null); refreshLists() },
    onError: (error) => { toast.error("Calibration was not deleted", { description: errorMessage(error) }); refreshLists() },
  })

  const calibrations = overview.data?.calibrations ?? []
  const unusable = overview.data?.unusable ?? []
  const selectable = calibrations.filter((calibration) => calibration.selectable)
  const stranded = calibrations.filter((calibration) => calibration.availability === "snapshot")
  const newestAge = daysSince(selectable[0]?.calibrated_at)
  const deleteDigest = deleteTarget
    ? "bundle_sha256" in deleteTarget ? deleteTarget.bundle_sha256 : deleteTarget.path.split("/").at(-1) ?? ""
    : ""
  const deleteUsage = deleteTarget && "used_by" in deleteTarget ? deleteTarget.used_by.length : 0

  return <div className="space-y-6">
    <PageHeader
      eyebrow="Reusable library · camera geometry"
      title="Calibrations"
      description="Every promoted camera calibration on this machine and how old it is. A calibration kept here stays selectable after its calibration run folder is deleted, so capture storage can be reclaimed without recalibrating."
      actions={<Button variant="outline" onClick={() => overview.refetch()} disabled={overview.isFetching}><RefreshCw className={overview.isFetching ? "animate-spin" : undefined} />Refresh</Button>}
    />
    <ProcessHandoff
      title="Feeds Object dataset · step 1"
      description="Dataset setup assigns one selectable calibration to each enabled camera and copies it into the run. Runs that already selected a calibration keep their own copy, so deleting one here never changes an existing dataset."
      to="/workflow/dataset"
      action="Open dataset setup"
    />

    {overview.isPending
      ? <div className="space-y-3">{Array.from({ length: 3 }).map((_, index) => <Skeleton className="h-28" key={index} />)}</div>
      : overview.isError
        ? <Card className="border-destructive/40"><CardHeader><CardTitle>Calibrations unavailable</CardTitle><CardDescription>{errorMessage(overview.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={() => overview.refetch()}><RefreshCw />Try again</Button></CardContent></Card>
        : <>
          <div className="grid gap-3 md:grid-cols-3" data-testid="calibration-summary">
            <Card><CardContent className="pt-4"><div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Selectable for new runs</div><div className="mt-1 font-display text-2xl font-semibold tabular-nums">{selectable.length}</div><div className="mt-0.5 text-[10px] text-muted-foreground">in a calibration run folder or the library</div></CardContent></Card>
            <Card><CardContent className="pt-4"><div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Newest selectable</div><div className="mt-1 font-display text-2xl font-semibold tabular-nums">{newestAge === null ? "—" : formatAge(selectable[0].calibrated_at)}</div><div className="mt-0.5 text-[10px] text-muted-foreground">{selectable.length ? "Recalibrate after any camera or mount has moved." : "Promote a calibration or keep one below."}</div></CardContent></Card>
            <Card className={stranded.length ? "border-warning/40 bg-warning/5" : undefined}><CardContent className="pt-4"><div className="text-[10px] font-bold uppercase tracking-[0.12em] text-muted-foreground">Only inside dataset runs</div><div className="mt-1 font-display text-2xl font-semibold tabular-nums">{stranded.length}</div><div className="mt-0.5 text-[10px] text-muted-foreground">{stranded.length ? "Keep in the library to reuse or protect them." : "Nothing depends on a single dataset run."}</div></CardContent></Card>
          </div>

          {calibrations.length === 0
            ? <EmptyState icon={Crosshair} title="No calibrations found" description="Complete and promote the camera-calibration workflow. The promoted calibration appears here and in dataset setup." action={<Button asChild><Link to="/workflow/calibration">Start camera calibration</Link></Button>} />
            : <Card>
              <CardHeader className="border-b bg-muted/20"><CardTitle>All calibrations</CardTitle><CardDescription className="mt-1">Age is based on the recorded camera frames. Bundles are sorted by their newest capture, with each older camera’s age shown separately. Recalibrate whenever a camera or mount moves.</CardDescription></CardHeader>
              <CardContent className="p-0">
                <div data-testid="calibrations-table" className="overflow-auto">
                  <table className="w-full min-w-[1180px] table-fixed text-left">
                    <thead className="border-b bg-muted text-[9px] font-bold uppercase tracking-[0.12em] text-muted-foreground">
                      <tr><th className="w-[19%] px-4 py-3">Calibration</th><th className="w-[11%] px-4 py-3">Age</th><th className="w-[26%] px-4 py-3">Cameras</th><th className="w-[20%] px-4 py-3">Where it lives</th><th className="w-[13%] px-4 py-3">Used by</th><th className="w-[11%] px-4 py-3 text-right">Actions</th></tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {calibrations.map((calibration) => <tr data-testid="calibration-row" data-bundle={calibration.bundle_sha256} data-availability={calibration.availability} className="align-top" key={calibration.bundle_sha256}>
                        <td className="px-4 py-4">
                          <div className="break-words font-semibold">{calibration.name}</div>
                          <div className="mt-1 font-mono text-[9px] text-muted-foreground" title={calibration.bundle_sha256}>bundle {calibration.bundle_sha256.slice(0, 16)}…</div>
                          {!calibration.valid && <div className="mt-2 flex items-start gap-1 text-[10px] text-destructive"><AlertTriangle aria-hidden="true" className="mt-0.5 size-3 shrink-0" />{calibration.issues[0] ?? "Not usable"}</div>}
                        </td>
                        <td className="px-4 py-4">
                          <div data-testid="calibration-age" className="font-display text-lg font-semibold tabular-nums">{formatAge(calibration.calibrated_at)}</div>
                          <div className="mt-1 text-[10px] text-muted-foreground">{calibration.calibrated_at ? formatDate(calibration.calibrated_at) : "Capture date unavailable"}</div>
                        </td>
                        <td className="px-4 py-4"><Cameras profiles={calibration.profiles} newest={calibration.calibrated_at} ageWarningDays={overview.data?.age_warning_days} /></td>
                        <td className="px-4 py-4"><Availability calibration={calibration} /></td>
                        <td className="px-4 py-4">
                          {calibration.used_by.length
                            ? <ul className="space-y-1 text-[11px]">{calibration.used_by.map((run) => <li key={run.run_root} className="break-words" title={`${run.run_root} · ${run.sensor_keys.join(", ")}`}>{run.run_name}</li>)}</ul>
                            : <span className="text-[11px] text-muted-foreground">No run on this machine</span>}
                        </td>
                        <td className="px-4 py-4">
                          <div className="flex flex-col items-end gap-2">
                            {!calibration.retained && (calibration.source_run || calibration.snapshot_runs.length > 0) && <Button size="sm" aria-label={`Keep ${calibration.name} in the library`} onClick={() => keep.mutate(calibration)} disabled={keep.isPending || !calibration.valid}>{keep.isPending && keep.variables?.bundle_sha256 === calibration.bundle_sha256 ? <LoaderCircle className="animate-spin" /> : <Save />}Keep in library</Button>}
                            {calibration.availability === "library" && <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" aria-label={`Delete ${calibration.name} from the library`} onClick={() => setDeleteTarget(calibration)}><Trash2 />Delete</Button>}
                            {calibration.availability === "run" && <Button asChild size="sm" variant="outline"><Link to="/run-folders" aria-label={`Manage the run folder of ${calibration.name}`}><FolderOpen />Run folders</Link></Button>}
                          </div>
                        </td>
                      </tr>)}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>}

          {unusable.length > 0 && <Card className="border-destructive/40" data-testid="unusable-calibrations">
            <CardHeader><CardTitle className="flex items-center gap-2"><AlertTriangle aria-hidden="true" className="size-4 text-destructive" />Unusable calibration files</CardTitle><CardDescription>These failed integrity or schema validation and are never offered to dataset setup.</CardDescription></CardHeader>
            <CardContent className="space-y-3">{unusable.map((item) => <div key={item.path} className="flex flex-wrap items-start justify-between gap-3 rounded-lg border p-3 text-xs">
              <div className="min-w-0"><div className="font-semibold">{item.name} · {item.availability === "library" ? "calibration library" : "calibration run folder"}</div><div className="mt-1 break-all font-mono text-[9px] text-muted-foreground">{item.path}</div><div className="mt-1 text-destructive">{item.issues[0]}</div></div>
              {item.availability === "library"
                ? <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" onClick={() => setDeleteTarget(item)}><Trash2 />Delete</Button>
                : <Button asChild size="sm" variant="outline"><Link to="/run-folders"><FolderOpen />Run folders</Link></Button>}
            </div>)}</CardContent>
          </Card>}

          <p className="flex items-center gap-2 text-[10px] text-muted-foreground"><Library aria-hidden="true" className="size-3" />Calibration library: <span className="font-mono">{overview.data.library_root}</span><Camera aria-hidden="true" className="ml-3 size-3" /><Link to="/workflow/calibration" className="font-semibold text-primary-strong underline underline-offset-4">Record a new calibration</Link></p>
        </>}

    <Dialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !remove.isPending) setDeleteTarget(null) }}>
      <DialogContent data-testid="calibration-delete-dialog">
        <DialogHeader>
          <DialogTitle>Delete {deleteTarget?.name ?? "calibration"} from the library?</DialogTitle>
          <DialogDescription>This permanently removes the library copy. New dataset runs can no longer select this calibration. It cannot be undone.</DialogDescription>
        </DialogHeader>
        <div className="rounded-lg border bg-muted/35 p-3 text-xs leading-relaxed">{deleteUsage > 0
          ? `${deleteUsage} existing dataset run${deleteUsage === 1 ? "" : "s"} on this machine already selected it. Each keeps its own snapshot and is not affected.`
          : "No dataset run on this machine holds a snapshot, so this is the last copy."}</div>
        <DialogFooter><Button variant="outline" onClick={() => setDeleteTarget(null)} disabled={remove.isPending}>Cancel</Button><Button variant="destructive" onClick={() => remove.mutate(deleteDigest)} disabled={!deleteDigest || remove.isPending}>{remove.isPending ? <LoaderCircle className="animate-spin" /> : <Trash2 />}Delete calibration</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}
