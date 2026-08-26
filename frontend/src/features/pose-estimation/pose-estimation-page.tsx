import { useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Archive, ArrowRight, Box, CheckCircle2, Cpu, Download, ExternalLink, FileCheck2, FileJson, LoaderCircle, RefreshCw, ScanSearch, Send, Server } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"

import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { api, errorMessage, query } from "@/lib/api"
import type { BopResultSubmission, ClusterEstimatorSettings, ClusterJob, ClusterPoseSetup } from "@/lib/contracts"
import { formatDate } from "@/lib/utils"
import { useOperator } from "@/providers/operator-provider"

const ACTIVE = new Set(["preparing", "transferring", "submitted", "pending", "running", "collecting", "canceling"])
const SUCCESS = new Set(["succeeded", "succeeded-with-warning"])

interface ImportedResult {
  result: BopResultSubmission
  created: boolean
  evaluation_url: string
  inspection_url: string
  download_url: string
  package_url: string
  provenance_url: string
}

interface ScopedSubmission {
  runRoot: string
  jobId: string
}

interface ScopedImport extends ScopedSubmission {
  value: ImportedResult
}

interface SettingsDraft {
  scope: string
  values: Record<string, string | number[]>
}

function shortHash(value?: string | null) {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "not available"
}

function stateTone(state?: string) {
  if (state && SUCCESS.has(state)) return "success" as const
  if (state === "failed" || state === "canceled") return "destructive" as const
  if (state && ACTIVE.has(state)) return "warning" as const
  return "neutral" as const
}

function Metric({ label, value, detail }: { label: string; value: React.ReactNode; detail?: string }) {
  return <div className="rounded-lg border bg-muted/20 px-3 py-2.5">
    <div className="text-[9px] font-bold uppercase tracking-[0.14em] text-muted-foreground">{label}</div>
    <div className="mt-1 font-mono text-sm font-semibold tabular-nums">{value}</div>
    {detail && <div className="mt-1 text-[10px] leading-relaxed text-muted-foreground">{detail}</div>}
  </div>
}

export function PoseEstimationPage() {
  const queryClient = useQueryClient()
  const { selectedRun } = useOperator()
  const [operator, setOperator] = useState(() => localStorage.getItem("posetestbot.clusterOperator") ?? "")
  const [estimatorId, setEstimatorId] = useState("")
  const [profileId, setProfileId] = useState("")
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft | null>(null)
  const [submittedJob, setSubmittedJob] = useState<ScopedSubmission | null>(null)
  const [importedResult, setImportedResult] = useState<ScopedImport | null>(null)

  const setup = useQuery({
    queryKey: ["cluster-pose-setup", selectedRun, estimatorId],
    queryFn: () => api<ClusterPoseSetup>(query("/cluster/pose-estimation/setup", { run_root: selectedRun, estimator_id: estimatorId || undefined })),
    refetchInterval: 10_000,
  })
  const estimators = setup.data?.estimators ?? []
  const effectiveEstimatorId = estimators.some((estimator) => estimator.estimator_id === estimatorId)
    ? estimatorId
    : setup.data?.estimator_id ?? estimators[0]?.estimator_id ?? ""
  const selectedEstimator = estimators.find((estimator) => estimator.estimator_id === effectiveEstimatorId) ?? setup.data?.estimator ?? null
  const history = useQuery({
    queryKey: ["cluster-jobs", selectedRun, effectiveEstimatorId],
    queryFn: () => api<{ jobs: ClusterJob[] }>(query("/cluster/jobs", { run_root: selectedRun, estimator_id: effectiveEstimatorId, limit: 50 })),
    enabled: Boolean(effectiveEstimatorId),
    retry: false,
    refetchInterval: (state) => state.state.data?.jobs.some((job) => ACTIVE.has(job.state)) ? 2_000 : 10_000,
  })
  const latestRunJob = useMemo(
    () => history.data?.jobs[0] ?? null,
    [history.data],
  )
  const submittedJobId = submittedJob?.runRoot === selectedRun ? submittedJob.jobId : null
  const selectedJobId = submittedJobId ?? latestRunJob?.job_id ?? null
  const job = useQuery({
    queryKey: ["cluster-job", selectedJobId],
    queryFn: () => api<{ job: ClusterJob }>(query(`/cluster/jobs/${selectedJobId}`, { include_log: true })),
    enabled: Boolean(selectedJobId),
    refetchInterval: (state) => ACTIVE.has(state.state.data?.job.state ?? "") ? 1_500 : false,
  })
  const currentJobBase = job.data?.job ?? (latestRunJob?.job_id === selectedJobId ? latestRunJob : null)
  const currentJob = currentJobBase
    ? {
        ...currentJobBase,
        collection: latestRunJob?.job_id === currentJobBase.job_id
          ? latestRunJob.collection
          : currentJobBase.collection,
      }
    : null
  const enabledProfiles = setup.data?.enabled_profiles ?? []
  const effectiveProfileId = enabledProfiles.some((profile) => profile.profile_id === profileId)
    ? profileId
    : enabledProfiles[0]?.profile_id ?? ""
  const collectedResponse = importedResult?.runRoot === selectedRun && importedResult.jobId === currentJob?.job_id
    ? importedResult.value
    : null
  const collectedResult = collectedResponse?.result ?? currentJob?.collection?.result ?? null
  const settingsDescriptor = selectedEstimator?.job_settings ?? null
  const sensorSequences = useMemo(() => setup.data?.sensor_sequences ?? [], [setup.data?.sensor_sequences])
  const settingsScope = `${selectedRun}\u0000${effectiveEstimatorId}`
  const defaultSettingsValues = useMemo(() => {
    const values: Record<string, string | number[]> = {}
    for (const field of settingsDescriptor?.fields ?? []) {
      values[field.key] = field.control === "enum"
        ? field.default
        : sensorSequences.filter((sensor) => sensor.tracking_eligible).map((sensor) => sensor.scene_id)
    }
    return values
  }, [sensorSequences, settingsDescriptor])
  const settingsValues = settingsDraft?.scope === settingsScope ? settingsDraft.values : defaultSettingsValues
  const updateSetting = (key: string, value: string | number[]) => {
    setSettingsDraft({ scope: settingsScope, values: { ...settingsValues, [key]: value } })
  }
  const settingsBlockers = useMemo(() => {
    const blockers: string[] = []
    for (const field of settingsDescriptor?.fields ?? []) {
      const value = settingsValues[field.key]
      if (field.control === "enum") {
        if (typeof value !== "string" || !field.options.some((option) => option.value === value)) blockers.push(`Choose ${field.label.toLowerCase()}.`)
      } else {
        const selected = Array.isArray(value) ? value : []
        if (selected.length < field.minimum_selected) blockers.push("Select at least one eligible sensor sequence.")
        if (selected.some((sceneId) => !sensorSequences.some((sensor) => sensor.scene_id === sceneId && sensor.tracking_eligible))) blockers.push("The sensor selection contains an ineligible sequence.")
      }
    }
    return [...new Set(blockers)]
  }, [sensorSequences, settingsDescriptor, settingsValues])
  const estimatorSettings = settingsDescriptor
    ? settingsDescriptor.fields.reduce<Record<string, string | number[]>>(
        (settings, field) => ({ ...settings, [field.key]: settingsValues[field.key] }),
        { schema_version: settingsDescriptor.value_schema_version },
      ) as ClusterEstimatorSettings
    : null

  const submit = useMutation({
    mutationFn: () => api<{ job: ClusterJob }>("/cluster/pose-estimation/jobs", {
      method: "POST",
      body: JSON.stringify({ run_root: selectedRun, estimator_id: effectiveEstimatorId, profile_id: effectiveProfileId, operator: operator.trim(), ...(estimatorSettings ? { estimator_settings: estimatorSettings } : {}) }),
    }),
    onSuccess: ({ job: submitted }) => {
      localStorage.setItem("posetestbot.clusterOperator", operator.trim())
      setSubmittedJob({ runRoot: selectedRun, jobId: submitted.job_id })
      setImportedResult(null)
      toast.success(`${selectedEstimator?.display_name ?? effectiveEstimatorId} job accepted`, { description: "Work continues after navigation. Monitor it from Jobs." })
      queryClient.invalidateQueries({ queryKey: ["cluster-jobs", selectedRun, effectiveEstimatorId] })
    },
    onError: (error) => toast.error("Estimator job was not submitted", { description: errorMessage(error) }),
  })
  const collectResult = useMutation({
    mutationFn: (jobId: string) => api<ImportedResult>(`/cluster/jobs/${jobId}/import-result`, {
      method: "POST",
      body: JSON.stringify({ run_root: selectedRun }),
    }),
    onSuccess: (value, jobId) => {
      setImportedResult({ runRoot: selectedRun, jobId, value })
      toast.success(value.created ? "Cluster result collected" : "Collected result verified")
      void queryClient.invalidateQueries({ queryKey: ["cluster-jobs", selectedRun, effectiveEstimatorId] })
      void queryClient.invalidateQueries({ queryKey: ["bop-evaluation", "setup", selectedRun] })
      void queryClient.invalidateQueries({ queryKey: ["bop-inspection"] })
    },
    onError: (error) => toast.error("Cluster result was not collected", { description: errorMessage(error) }),
  })

  const selectedProfile = enabledProfiles.find((profile) => profile.profile_id === effectiveProfileId)
  const canSubmit = Boolean(setup.data?.ready && effectiveEstimatorId && effectiveProfileId && operator.trim().length >= 2 && settingsBlockers.length === 0 && !submit.isPending)
  const runtime = setup.data?.runtime

  return <div className="space-y-6" data-testid="pose-estimation-page">
    <PageHeader
      eyebrow="Inspect · external estimator"
      title="Pose Estimation"
      description="Stage the active run's immutable BOP export to a qualified estimator runtime selected from the external cluster controller. This is not an acquisition stage."
      actions={<>
        <Button asChild variant="outline"><Link to="/run-folders"><Archive />Cluster storage</Link></Button>
        <Button variant="outline" onClick={() => { void setup.refetch(); void history.refetch() }} disabled={setup.isFetching || history.isFetching}><RefreshCw className={setup.isFetching || history.isFetching ? "animate-spin" : undefined} />Refresh evidence</Button>
      </>}
    />
    <ProcessHandoff
      title="Consumes the dataset produced by Workflow"
      description="Pose Estimation reads an already exported, annotation-bearing BOP dataset. The external job remains durable after navigation; return through Jobs, then evaluate its immutable BOP19 CSV on BOP Evaluation."
      to="/workflow/setup"
      action="Return to workflow"
    />

    {setup.isPending
      ? <div className="grid gap-4 xl:grid-cols-2"><Skeleton className="h-[360px]" /><Skeleton className="h-[360px]" /></div>
      : setup.isError
        ? <Card className="border-destructive/40"><CardHeader><CardTitle>Readiness unavailable</CardTitle><CardDescription>{errorMessage(setup.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={() => setup.refetch()}><RefreshCw />Try again</Button></CardContent></Card>
        : setup.data && <>
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(440px,0.85fr)]">
            <Card>
              <CardHeader className="pb-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div><CardTitle className="flex items-center gap-2"><FileCheck2 className="size-5 text-primary-strong" />Dataset snapshot</CardTitle><CardDescription className="mt-1 break-all font-mono">{selectedRun}</CardDescription></div>
                  <StatusBadge status={setup.data.ready ? "ready" : "blocked"} tone={setup.data.ready ? "success" : "destructive"}>{setup.data.ready ? "Ready to submit" : `${setup.data.blockers.length} blocker${setup.data.blockers.length === 1 ? "" : "s"}`}</StatusBadge>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-5">
                  <Metric label="Dataset" value={setup.data.dataset.dataset_alias} />
                  <Metric label="Scenes" value={setup.data.dataset.scene_count} />
                  <Metric label="Frames" value={setup.data.dataset.frame_count} />
                  <Metric label="Objects" value={setup.data.dataset.model_count} />
                  <Metric label="Target instances" value={setup.data.dataset.target_count} />
                </div>
                <div className="grid gap-2 lg:grid-cols-2">
                  <Metric label="Dataset identity" value={shortHash(setup.data.dataset.dataset_sha256)} detail="Revalidated before submission and again before local import." />
                  <Metric label="Annotation contract" value={setup.data.annotation_mode ?? "missing"} detail={setup.data.oracle_mask_contract ? "Visible BOP mask_visib GT instance masks are oracle inputs for this method." : "The export is checked against the selected method's advertised input contract."} />
                </div>
                {setup.data.oracle_mask_contract && <div className="rounded-lg border border-warning/35 bg-warning/5 p-3 text-xs leading-relaxed text-warning-foreground">
                  <div className="flex items-center gap-2 font-semibold"><AlertTriangle className="size-4" />Oracle-mask qualification</div>
                  <p className="mt-1">This qualified run does not measure detection or segmentation. Visible GT instance masks initialize and recover tracks; independent mode uses one per target. Scores remain 1.0, and camera-local tracks are never fused across sensors.</p>
                </div>}
                {setup.data.blockers.length > 0 && <div className="space-y-2" aria-label="Pose estimation blockers">
                  {setup.data.blockers.map((blocker) => <div key={`${blocker.code}-${blocker.message}`} className="flex gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive"><AlertTriangle className="mt-0.5 size-4 shrink-0" /><span>{blocker.message}</span></div>)}
                </div>}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3"><CardTitle className="flex items-center gap-2"><Server className="size-5 text-primary-strong" />Controller & estimator runtime</CardTitle><CardDescription>Choose only from server-installed methods and qualified resource profiles.</CardDescription></CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-2 gap-2">
                  <Metric label="Controller" value={setup.data.controller.available ? setup.data.controller.ready ? "connected" : "not ready" : "unavailable"} />
                  <Metric label="Integration" value={setup.data.controller.integration.enabled ? "enabled" : "disabled"} />
                  <Metric label="Estimator" value={selectedEstimator?.display_name ?? "not installed"} />
                  <Metric label="Runtime ID" value={runtime?.runtime_id ?? "not qualified"} />
                  <Metric label="Container" value={shortHash(runtime?.container?.sha256 ?? runtime?.sif_sha256)} />
                  <Metric label="Qualification" value={shortHash(runtime?.qualification_manifest_sha256)} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="cluster-estimator">Estimator method</Label>
                  <Select value={effectiveEstimatorId} onValueChange={setEstimatorId} disabled={!estimators.length}>
                    <SelectTrigger id="cluster-estimator"><SelectValue placeholder="No estimator installed" /></SelectTrigger>
                    <SelectContent>{estimators.map((estimator) => <SelectItem key={estimator.estimator_id} value={estimator.estimator_id}>{estimator.display_name}{estimator.ready ? " · ready" : " · blocked"}</SelectItem>)}</SelectContent>
                  </Select>
                  {selectedEstimator && <p className="text-[11px] leading-relaxed text-muted-foreground">{selectedEstimator.output_contract ?? "No output contract"} · {selectedEstimator.input_contracts.join(", ") || "No compatible input contract"}</p>}
                </div>
                {settingsDescriptor && <div data-testid="estimator-settings-form" className="space-y-4 rounded-lg border border-primary/25 bg-primary/5 p-4">
                  <div><div className="text-xs font-semibold">Estimator settings</div><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">Browser-local draft for this run and estimator. Submission validates it against the unchanged export, then stores it immutably with the controller job.</p></div>
                  {settingsDescriptor.fields.map((field) => field.control === "enum"
                    ? <div key={field.key} className="space-y-2">
                      <Label htmlFor={`estimator-setting-${field.key}`}>{field.label}</Label>
                      <Select value={typeof settingsValues[field.key] === "string" ? settingsValues[field.key] as string : ""} onValueChange={(value) => updateSetting(field.key, value)}>
                        <SelectTrigger id={`estimator-setting-${field.key}`} data-testid={`estimator-setting-${field.key}`}><SelectValue /></SelectTrigger>
                        <SelectContent>{field.options.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-[11px] leading-relaxed text-muted-foreground">{field.description}</p>
                    </div>
                    : <div key={field.key} className="space-y-2">
                      <div><Label>{field.label}</Label><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{field.description}</p></div>
                      <div className="grid gap-2" data-testid={`estimator-setting-${field.key}`}>
                        {sensorSequences.map((sensor) => {
                          const selected = Array.isArray(settingsValues[field.key]) && (settingsValues[field.key] as number[]).includes(sensor.scene_id)
                          return <Label key={sensor.scene_id} className={`flex items-start gap-3 rounded-md border p-3 ${sensor.tracking_eligible ? "cursor-pointer bg-card" : "bg-muted/40 opacity-65"}`}>
                            <Checkbox
                              aria-label={`Select ${sensor.display_name} scene ${sensor.scene_id}`}
                              data-testid="estimator-sensor-selection"
                              checked={selected}
                              disabled={!sensor.tracking_eligible}
                              onCheckedChange={(checked) => {
                                const current = Array.isArray(settingsValues[field.key]) ? settingsValues[field.key] as number[] : []
                                updateSetting(field.key, checked === true ? [...current, sensor.scene_id].sort((left, right) => left - right) : current.filter((sceneId) => sceneId !== sensor.scene_id))
                              }}
                            />
                            <span className="min-w-0"><span className="block text-xs font-semibold">{sensor.display_name}</span><span className="mt-0.5 block font-mono text-[9px] font-normal text-muted-foreground">{sensor.sensor_id} · scene {sensor.scene_id}</span><span className="mt-1 block text-[10px] font-normal text-muted-foreground">{sensor.mounting_mode} · {sensor.frame_count.toLocaleString()} frames · {sensor.target_count.toLocaleString()} targets</span>{sensor.tracking_blocker && <span className="mt-1 block text-[10px] font-normal text-destructive">{sensor.tracking_blocker}</span>}</span>
                          </Label>
                        })}
                      </div>
                    </div>)}
                  {settingsBlockers.map((message) => <p key={message} role="alert" className="text-[11px] font-medium text-destructive">{message}</p>)}
                </div>}
                <div className="space-y-2">
                  <Label htmlFor="cluster-profile">Server-owned resource profile</Label>
                  <Select value={effectiveProfileId} onValueChange={setProfileId} disabled={!enabledProfiles.length}>
                    <SelectTrigger id="cluster-profile"><SelectValue placeholder="No qualified profile" /></SelectTrigger>
                    <SelectContent>{setup.data.enabled_profiles.map((profile) => <SelectItem key={profile.profile_id} value={profile.profile_id}>{profile.profile_id} · {profile.gres} · {profile.walltime}</SelectItem>)}</SelectContent>
                  </Select>
                  {selectedProfile && <p className="text-[11px] leading-relaxed text-muted-foreground">{selectedProfile.partition} · {selectedProfile.gres} · {selectedProfile.cpus} CPU · {selectedProfile.memory} · {selectedProfile.walltime}{selectedProfile.max_targets ? ` · bounded to ${selectedProfile.max_targets} targets` : ""}</p>}
                </div>
                <div className="space-y-2"><Label htmlFor="cluster-operator">Operator / submitter</Label><Input id="cluster-operator" value={operator} onChange={(event) => setOperator(event.target.value)} placeholder="Name or lab account" maxLength={120} /></div>
                <Button className="w-full" onClick={() => submit.mutate()} disabled={!canSubmit}>{submit.isPending ? <LoaderCircle className="animate-spin" /> : <Send />}{submit.isPending ? "Submitting…" : `Submit ${selectedEstimator?.display_name ?? "estimator"} job`}</Button>
                <p className="text-[11px] leading-relaxed text-muted-foreground">Submission creates a durable controller record and SLURM job. Work continues after navigation. <Link className="font-semibold text-primary-strong underline-offset-4 hover:underline" to="/jobs">Open Jobs</Link>.</p>
              </CardContent>
            </Card>
          </div>

          {currentJob && <Card data-testid="pose-estimation-current-job" className={ACTIVE.has(currentJob.state) ? "border-primary/35" : undefined}>
            <CardHeader className="pb-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><CardTitle className="flex items-center gap-2"><Cpu className="size-5 text-primary-strong" />Latest job for active run</CardTitle><CardDescription className="mt-1 font-mono">{currentJob.job_id}</CardDescription></div>
                <StatusBadge status={currentJob.state} tone={stateTone(currentJob.state)} />
              </div>
            </CardHeader>
            <CardContent className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.7fr)]">
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-3 2xl:grid-cols-7">
                <Metric label="SLURM job" value={currentJob.slurm_job_id ?? "pending"} />
                <Metric label="Estimator" value={String(currentJob.payload.estimator_id ?? "unreported")} />
                <Metric label="Profile" value={String(currentJob.payload.profile_id ?? "—")} />
                <Metric label="Updated" value={formatDate(currentJob.updated_at)} />
                <Metric label="Estimates" value={currentJob.result?.estimate_count ?? "—"} detail={currentJob.result ? `${currentJob.result.failure_count} target failures retained` : undefined} />
                <Metric label="Mode" value={currentJob.payload.estimator_settings?.execution_mode ?? "legacy independent"} />
                <Metric label="Sensor scenes" value={currentJob.payload.estimator_settings?.selected_scene_ids.join(", ") ?? "all"} detail={currentJob.result?.profile_excluded_target_count ? `${currentJob.result.profile_excluded_target_count} profile-bounded targets excluded` : undefined} />
              </div>
              <div className="flex flex-col justify-center gap-2">
                {ACTIVE.has(currentJob.state) && <div className="flex items-center gap-2 text-sm"><LoaderCircle className="size-4 animate-spin text-primary-strong" />Remote work is durable and still running.</div>}
                {currentJob.error && <p className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">{currentJob.error}</p>}
                {SUCCESS.has(currentJob.state) && !collectedResult && <Button variant="outline" onClick={() => collectResult.mutate(currentJob.job_id)} disabled={collectResult.isPending}>{collectResult.isPending ? <LoaderCircle className="animate-spin" /> : <Download />}{collectResult.isPending ? "Collecting result…" : collectResult.isError ? "Retry result collection" : "Collect result"}</Button>}
                {collectedResult && <div className="rounded-lg border border-success/35 bg-success/5 p-3" data-testid="pose-estimation-collected-result">
                  <div className="flex items-center gap-2 text-sm font-semibold text-success"><CheckCircle2 className="size-4" />Immutable BOP19 result collected</div>
                  <p className="mt-1 break-all font-mono text-[9px] text-muted-foreground">{collectedResult.result_id} · {shortHash(collectedResult.sha256)}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button asChild size="sm"><Link to={collectedResponse?.inspection_url ?? `/pose-results?result_id=${collectedResult.result_id}`}><ScanSearch />Inspect poses</Link></Button>
                    <Button asChild size="sm" variant="outline"><Link to={collectedResponse?.evaluation_url ?? `/bop-evaluation?result_id=${collectedResult.result_id}`}>Evaluate<ArrowRight /></Link></Button>
                    <Button asChild size="sm" variant="outline"><a href={collectedResponse?.package_url ?? query(`/bop/evaluation/results/${collectedResult.result_id}/package`, { run_root: selectedRun })}><Box />Package</a></Button>
                    <Button asChild size="sm" variant="outline"><a href={collectedResponse?.download_url ?? query(`/bop/evaluation/results/${collectedResult.result_id}/download`, { run_root: selectedRun })}><Download />CSV</a></Button>
                    {collectedResult.provenance_available && <Button asChild size="sm" variant="outline"><a href={collectedResponse?.provenance_url ?? query(`/bop/evaluation/results/${collectedResult.result_id}/provenance`, { run_root: selectedRun })}><FileJson />Provenance</a></Button>}
                  </div>
                </div>}
                <Button asChild variant="ghost" size="sm"><Link to="/jobs">View logs and all cluster jobs<ExternalLink /></Link></Button>
              </div>
            </CardContent>
          </Card>}
        </>}
  </div>
}
