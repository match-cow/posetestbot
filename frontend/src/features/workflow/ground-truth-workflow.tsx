import { QueryProblem } from "@/components/query-problem"
import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowRight, CheckCircle2, LoaderCircle, RefreshCw, Send } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { HelpTip } from "@/components/help-tip"
import { TemplateFootprintThumbnail, useTemplatePreview } from "@/components/geometry-previews"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api, errorMessage, query } from "@/lib/api"
import type { Job, PoseTemplateBundle } from "@/lib/contracts"
import { jobFailureDetail } from "@/lib/jobs"
import { useOperator } from "@/providers/operator-provider"
import { TemplateScenePreview } from "../pose-templates/template-scene-preview"

interface SelectionStatus {
  selection: null | {
    template_uuid: string
    placement_confirmed: boolean
    instances: Array<{ instance_uuid: string; name: string; obj_id: number; template_base_from_object: { translation_mm: number[] } }>
  }
  replacement_blockers: string[]
  ready: boolean
}

function placementMatrix(values: number[]) {
  const [x, y, z, roll, pitch, yaw] = values
  const [r, p, w] = [roll, pitch, yaw].map((value) => value * Math.PI / 180)
  const [cr, sr, cp, sp, cw, sw] = [Math.cos(r), Math.sin(r), Math.cos(p), Math.sin(p), Math.cos(w), Math.sin(w)]
  return [
    [cw * cp, cw * sp * sr - sw * cr, cw * sp * cr + sw * sr, x],
    [sw * cp, sw * sp * sr + cw * cr, sw * sp * cr - cw * sr, y],
    [-sp, cp * sr, cp * cr, z],
    [0, 0, 0, 1],
  ]
}

const OPERATOR_NAME_KEY = "posetestbot.operatorName"

export function GroundTruthWorkflow() {
  const { selectedRun, selectedRunEpoch } = useOperator()
  const client = useQueryClient()
  const [template, setTemplate] = useState("")
  // Raw text, blank until measured: zeros would be a confirmable placement nobody entered,
  // and parsing on every keystroke swallowed a leading minus sign.
  const [placement, setPlacement] = useState(["", "", "", "", "", ""])
  const [confirmation, setConfirmation] = useState<{ runRoot: string; runEpoch: number; value: boolean }>({ runRoot: "", runEpoch: -1, value: false })
  const [operator, setOperator] = useState(() => localStorage.getItem(OPERATOR_NAME_KEY) ?? "")
  const [pendingJob, setPendingJob] = useState<{ id: string; runRoot: string } | null>(null)
  const placementValues = placement.map((value) => value.trim() === "" ? Number.NaN : Number(value))
  const placementComplete = placementValues.every(Number.isFinite)
  const confirmed = placementComplete && confirmation.runRoot === selectedRun && confirmation.runEpoch === selectedRunEpoch && confirmation.value
  const library = useQuery({ queryKey: ["pose-template-library"], queryFn: () => api<{ templates: PoseTemplateBundle[] }>("/pose-templates/library") })
  const status = useQuery({ queryKey: ["pose-template-run", selectedRun], queryFn: () => api<SelectionStatus>(query("/pose-templates/runs/selection", { run_root: selectedRun })), retry: false })
  const active = useMemo(() => library.data?.templates.filter((item) => item.archive.state === "active") ?? [], [library.data])
  const hasSavedSelection = Boolean(status.data?.selection)
  const selectedBundle = active.find((item) => item.template_uuid === template)
  const selectedPreview = useTemplatePreview(template, Boolean(selectedBundle))
  const selectionJob = useQuery({
    queryKey: ["pose-template-selection-job", pendingJob?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingJob!.id}`),
    enabled: Boolean(pendingJob),
    refetchInterval: (queryState) => ["succeeded", "failed", "canceled", "cancelled"].includes(queryState.state.data?.job.status ?? "") ? false : 600,
  })
  useEffect(() => {
    const job = selectionJob.data?.job
    if (!pendingJob || !job || !["succeeded", "failed", "canceled", "cancelled"].includes(job.status)) return
    if (job.status === "succeeded") {
      toast.success("Pose template selection saved")
      void client.invalidateQueries({ queryKey: ["pose-template-run", pendingJob.runRoot] })
      void client.invalidateQueries({ queryKey: ["run-config", pendingJob.runRoot] })
      void client.invalidateQueries({ queryKey: ["overview", pendingJob.runRoot] })
    } else {
      toast.error("Pose template selection was not saved", { description: jobFailureDetail(job) })
    }
    queueMicrotask(() => setPendingJob(null))
  }, [client, pendingJob, selectionJob.data])
  const select = useMutation({
    mutationFn: (submission: { runRoot: string; templateUuid: string; placementValues: number[]; confirmed: boolean; operator: string }) => api<{ job_id: string }>("/pose-templates/runs/selection", {
      method: "POST",
      body: JSON.stringify({
        run_root: submission.runRoot,
        template_uuid: submission.templateUuid,
        placement: { matrix: placementMatrix(submission.placementValues) },
        confirmed: submission.confirmed,
        operator: submission.operator,
      }),
    }),
    onSuccess: (value, submission) => { setPendingJob({ id: value.job_id, runRoot: submission.runRoot }); toast.success("Pose template selection queued", { description: `Job ${value.job_id} continues after navigation; status and output are available in Jobs.` }); void client.invalidateQueries({ queryKey: ["jobs"] }) },
    onError: (error) => toast.error("Selection was not queued", { description: errorMessage(error) }),
  })

  const chooseTemplate = (templateUuid: string) => {
    setTemplate(templateUuid)
    setConfirmation({ runRoot: selectedRun, runEpoch: selectedRunEpoch, value: false })
  }
  const updatePlacement = (index: number, value: string) => {
    setPlacement((current) => current.map((item, itemIndex) => itemIndex === index ? value : item))
    setConfirmation({ runRoot: selectedRun, runEpoch: selectedRunEpoch, value: false })
  }

  return <div className="grid grid-cols-[.72fr_1.28fr] gap-4" data-testid="ground-truth-workflow">
    <Card data-testid="saved-pose-template-selection">
      <CardHeader><div className="flex items-start justify-between"><div><CardTitle>Selected pose template</CardTitle><CardDescription className="mt-1">The printed template, object identities, and measured position saved with this run.</CardDescription></div><StatusBadge status={status.data?.ready ? "ready" : status.data?.selection ? "waiting" : "missing"} tone={status.data?.ready ? "success" : "destructive"} /></div></CardHeader>
      <CardContent className="space-y-3">
        {status.data?.selection ? <>
          <div className="rounded-lg bg-muted p-3 text-xs"><div className="font-semibold">Template {status.data.selection.template_uuid.slice(0, 8)}</div><div className="mt-1 text-muted-foreground">{status.data.selection.instances.length} resolved physical instance{status.data.selection.instances.length === 1 ? "" : "s"} · placement {status.data.selection.placement_confirmed ? "confirmed" : "not confirmed"}</div></div>
          <div className="space-y-1">{status.data.selection.instances.map((item) => <div key={item.instance_uuid} className="flex justify-between rounded border px-3 py-2 text-xs"><span>{item.name} · obj_{String(item.obj_id).padStart(6, "0")}</span><span className="font-mono text-muted-foreground">{item.template_base_from_object.translation_mm.map((value) => value.toFixed(1)).join(", ")} mm</span></div>)}</div>
        </> : status.isError ? <QueryProblem title="Run template selection unavailable" error={status.error} retry={() => void status.refetch()} /> : status.isPending ? <p role="status" className="p-4 text-xs">Loading run template selection…</p> : <div className="rounded-lg border border-dashed px-5 py-10 text-center text-xs text-muted-foreground">No pose template is selected. This is required for an object-labelled dataset because it establishes which physical objects are present and where the printed template sits in the workcell.</div>}
        {status.data?.ready && <div className="flex items-start gap-2 rounded-lg border border-success/30 bg-success/5 p-3 text-xs text-success" data-testid="saved-pose-template-ready"><CheckCircle2 className="mt-0.5 size-4 shrink-0" /><span>Object identities and measured placement are ready for dataset recording and BOP export.</span></div>}
        {status.data?.replacement_blockers.length ? <div className="rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs"><div className="font-semibold text-destructive">Selection replacement blocked</div>{status.data.replacement_blockers.map((item) => <div className="mt-1 font-mono text-muted-foreground" key={item}>{item}</div>)}</div> : null}
        <Button variant="outline" onClick={() => status.refetch()}><RefreshCw />Refresh selection</Button>
      </CardContent>
    </Card>

    <Card data-testid="pose-template-selection-draft">
      <CardHeader><CardTitle>{hasSavedSelection ? "Replacement selection draft" : "New selection draft"}</CardTitle><CardDescription>{hasSavedSelection ? "The saved pose template at left remains active. Use this separate draft only when intentionally replacing it with a different template or measured position." : "Choose the exact printed pose template used in the cell, then enter its measured position relative to the workcell reference. Cards use simplified footprints for browsing; selecting one loads its exact immutable object geometry and identity details."}</CardDescription></CardHeader>
      <CardContent className="space-y-5">
        {library.isError && <QueryProblem title="Pose-template library unavailable" error={library.error} retry={() => void library.refetch()} />}
        <div>
          <Label>Pose template {hasSavedSelection ? <span className="font-normal text-muted-foreground">Replacement choice</span> : <span className="text-destructive">Required</span>}</Label>
          <div className="mt-2 grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Pose template">
            {active.map((item) => {
              const selected = item.template_uuid === template
              return <button
                type="button"
                role="radio"
                aria-checked={selected}
                aria-label={`Select ${item.display_name}`}
                className={`overflow-hidden rounded-lg border p-2 text-left transition-colors ${selected ? "border-primary bg-primary/5 ring-2 ring-primary/25" : "hover:border-foreground/25"}`}
                onClick={() => chooseTemplate(item.template_uuid)}
                key={item.template_uuid}
              >
                <TemplateFootprintThumbnail bundle={item} className="h-28" />
                <div className="mt-2 flex items-start justify-between gap-2"><div><div className="text-xs font-semibold">{item.display_name}</div><div className="mt-0.5 text-[10px] text-muted-foreground">{item.instances.length} immutable instance{item.instances.length === 1 ? "" : "s"}</div></div>{selected && <CheckCircle2 className="size-4 shrink-0 text-primary" />}</div>
              </button>
            })}
          </div>
          {!active.length && <div className="mt-2 rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">No active immutable template versions are available.</div>}
        </div>

        {selectedBundle && <div className="overflow-hidden rounded-lg border">
          <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-2"><div><div className="text-xs font-semibold">{selectedBundle.display_name}</div><div className="text-[10px] text-muted-foreground">Exact immutable object assets · Z up</div></div><span className="text-[10px] text-muted-foreground">Identify · focus · verify placement</span></div>
          <div className="h-[25rem]">{selectedPreview.isPending ? <div className="grid size-full place-items-center text-xs text-muted-foreground"><LoaderCircle className="mr-2 inline size-4 animate-spin" />Loading exact scene…</div> : selectedPreview.data ? <TemplateScenePreview bundle={selectedBundle} preview={selectedPreview.data} /> : <div className="grid size-full place-items-center text-xs text-muted-foreground">The immutable preview is unavailable.</div>}</div>
        </div>}

        <div className="@container space-y-3">
          <div><Label className="inline-flex items-center gap-1">Measured template position in the workcell <HelpTip label="measured template position">This six-degree-of-freedom transform places the origin printed on the reusable pose template into the fixed workcell frame used by the exported dataset.</HelpTip></Label><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">Translation and rotation from the blue origin printed on the template into <code>template_base</code>, the dataset's fixed workcell reference.</p><div className="mt-2 grid grid-cols-2 gap-2 @sm:grid-cols-3 @2xl:grid-cols-6" data-testid="template-placement-grid">{["X mm", "Y mm", "Z mm", "Roll °", "Pitch °", "Yaw °"].map((label, index) => <div key={label}><Label className="text-[10px]">{label}</Label><Input aria-label={`Template placement ${label}`} type="number" step="0.1" value={placement[index]} onChange={(event) => updatePlacement(index, event.target.value)} /></div>)}</div>{!placementComplete && <p data-testid="template-placement-required" className="mt-2 text-[11px] font-medium text-destructive">Enter all six measured values before confirming. Nothing is assumed.</p>}<Button type="button" variant="outline" size="sm" className="mt-2" onClick={() => { setPlacement(["0", "0", "0", "0", "0", "0"]); setConfirmation({ runRoot: selectedRun, runEpoch: selectedRunEpoch, value: false }) }}>Template origin sits exactly on PoseTemplateBase (all zeros)</Button></div>
          <div className="max-w-[210px]"><Label>Operator name <span className="text-destructive">Required</span></Label><Input aria-label="Operator name" placeholder="Who measured this placement" value={operator} onChange={(event) => setOperator(event.target.value)} /></div>
        </div>
        <div className="flex items-start gap-2 rounded-lg border bg-muted/40 p-3"><Checkbox id="confirm-placement" checked={confirmed} disabled={!placementComplete} onCheckedChange={(value) => setConfirmation({ runRoot: selectedRun, runEpoch: selectedRunEpoch, value: value === true })} /><div><Label htmlFor="confirm-placement">I confirm this measured physical placement</Label><p className="mt-1 text-[11px] text-muted-foreground">Changing the run, template, or any placement value clears this confirmation. Print compensation never changes Ground Truth.</p></div></div>
        <div className="flex flex-wrap items-center gap-3"><Button onClick={() => { localStorage.setItem(OPERATOR_NAME_KEY, operator.trim()); select.mutate({ runRoot: selectedRun, templateUuid: template, placementValues: placementValues, confirmed, operator: operator.trim() }) }} disabled={!template || status.isError || status.isPending || library.isError || !operator.trim() || !placementComplete || !confirmed || select.isPending || Boolean(pendingJob) || Boolean(status.data?.replacement_blockers.length)}><Send />{select.isPending ? "Queueing…" : pendingJob ? "Selection running…" : hasSavedSelection ? "Replace run selection" : "Select for run"}</Button>{pendingJob && <div className="flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground"><span className="flex items-center gap-1.5 font-mono"><LoaderCircle className="size-3 animate-spin" />{pendingJob.id} · {pendingJob.runRoot === selectedRun ? selectionJob.data?.job.status ?? "queued" : "another run"}</span><span>Continues after navigation.</span><Link className="inline-flex items-center gap-1 font-semibold text-primary-strong underline-offset-4 hover:underline" to="/jobs">Open Jobs<ArrowRight className="size-3" /></Link></div>}</div>
      </CardContent>
    </Card>
  </div>
}
