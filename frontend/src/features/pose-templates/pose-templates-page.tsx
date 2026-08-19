import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Archive, CheckCircle2, Copy, Download, LoaderCircle, PackageSearch, Plus, RefreshCw, RotateCcw, Save, Search, Sparkles, Trash2, X } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { contourPoints, IsometricMeshPreview, orientationAnalysisQueryKey, orientationThumbnailQueryKey, TemplateFootprintThumbnail, WorkpieceIsometricThumbnail } from "@/components/geometry-previews"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, api, errorMessage } from "@/lib/api"
import type { CatalogObject, Job, PoseTemplateBundle, PoseTemplateOrientation, PoseTemplateOrientationAnalysis, PoseTemplateOrientationThumbnail, PoseTemplatePreview, PoseTemplateSourceStatus } from "@/lib/contracts"
import { projectIsometricMesh } from "@/lib/isometric"
import { jobFailureDetail, jobStatusTone } from "@/lib/jobs"
import { TemplateLayoutCanvas, type PositionedTemplateInstance } from "./template-layout-canvas"

const ALL_FILTER = "__all__"
const TERMINAL_JOB_STATES = new Set(["succeeded", "failed", "canceled"])
type LibraryAction = "archive" | "restore" | "clone" | "delete"
interface LibraryActionResult {
  job_id?: string
  status?: string
  cleanup_job_error?: string
  asset_cleanup?: { last_error?: string | null }
}
interface PendingTemplateCleanup {
  item: PoseTemplateBundle
  id?: string
  error?: string
}

function facetValues(values: string[]) {
  const byCasefoldedValue = new Map<string, string>()
  values.forEach((value) => {
    const key = value.toLocaleLowerCase()
    if (!byCasefoldedValue.has(key)) byCasefoldedValue.set(key, value)
  })
  return [...byCasefoldedValue.values()].sort((left, right) => left.localeCompare(right, undefined, { sensitivity: "base" }))
}

function matchesFacet(values: string[], selected: string) {
  const key = selected.toLocaleLowerCase()
  return values.some((value) => value.toLocaleLowerCase() === key)
}
const PAGE_SIZES: Record<string, [number, number]> = { A0: [841, 1189], A1: [594, 841], A2: [420, 594], A3: [297, 420], A4: [210, 297] }

function createInstanceUuid() {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID()
  const bytes = new Uint8Array(16)
  if (typeof globalThis.crypto?.getRandomValues === "function") globalThis.crypto.getRandomValues(bytes)
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function pageDimensions(size: string, orientation: string) {
  const dimensions = PAGE_SIZES[size] ?? PAGE_SIZES.A3
  return orientation === "portrait"
    ? { width_mm: dimensions[0], height_mm: dimensions[1] }
    : { width_mm: dimensions[1], height_mm: dimensions[0] }
}

function orientationAnalysisRequired(error: unknown) {
  return error instanceof ApiError
    && (error.status === 404 || error.status === 409)
    && typeof error.body === "object"
    && error.body !== null
    && error.body.analysis_required === true
}

async function delay(milliseconds: number, signal?: AbortSignal) {
  await new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds))
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError")
}

async function waitForJob(jobId: string, signal?: AbortSignal, attempts = 180) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const { job } = await api<{ job: Job }>(`/jobs/${jobId}`, { signal })
    if (job.status === "succeeded") return job
    if (TERMINAL_JOB_STATES.has(job.status)) throw new Error(jobFailureDetail(job))
    await delay(500, signal)
  }
  throw new Error("The queued job did not finish within the expected time.")
}

async function waitForPreview(requestId: string, jobId: string | undefined, signal: AbortSignal) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    try {
      return await api<PoseTemplatePreview>(`/pose-templates/preview/${requestId}`, { signal })
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error
    }
    if (jobId && attempt % 4 === 3) {
      try {
        const { job } = await api<{ job: Job }>(`/jobs/${jobId}`, { signal })
        if (TERMINAL_JOB_STATES.has(job.status) && job.status !== "succeeded") throw new Error(jobFailureDetail(job))
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error
      }
    }
    await delay(500, signal)
  }
  throw new Error("The exact preview did not finish within the expected time.")
}

function orientationBounds(orientation: PoseTemplateOrientation) {
  const points = orientation.contours.flatMap(contourPoints)
  if (!points.length) return { minX: 0, minY: 0, maxX: 1, maxY: 1 }
  return points.reduce((bounds, point) => ({
    minX: Math.min(bounds.minX, point.x_mm), minY: Math.min(bounds.minY, point.y_mm),
    maxX: Math.max(bounds.maxX, point.x_mm), maxY: Math.max(bounds.maxY, point.y_mm),
  }), { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity })
}

function orientationPreviewQuality(object: CatalogObject, analysis: PoseTemplateOrientationAnalysis) {
  // preview_mesh is the deliberately tiny printable-layout proxy. Orientation
  // choice is a recognition task, so prefer the topology-aware bounded surface.
  const mesh = analysis.recognition_mesh ?? analysis.preview_mesh
  const approximation = analysis.recognition_mesh_approximation
  const sourceFaces = approximation?.source_faces ?? object.extraction.faces
  const displayedFaces = mesh.faces.length
  const proxy = !analysis.recognition_mesh || approximation?.strategy === "convex_proxy"
  const reduced = sourceFaces > displayedFaces
  const label = proxy
    ? "Proxy fallback"
    : approximation?.topology_preserved === false
      ? "High-detail bounded LOD"
      : reduced
        ? "Topology-aware high-detail LOD"
        : "Full recognition surface"
  const detail = proxy
    ? `${displayedFaces.toLocaleString()}-face fallback; refresh stable-orientation analysis if identifying features are missing`
    : `${displayedFaces.toLocaleString()} of ${sourceFaces.toLocaleString()} source faces`
  return { mesh, sourceFaces, displayedFaces, proxy, label, detail }
}

function BaseContourPreview({ orientation }: { orientation: PoseTemplateOrientation }) {
  const bounds = orientationBounds(orientation)
  const width = Math.max(.01, bounds.maxX - bounds.minX)
  const height = Math.max(.01, bounds.maxY - bounds.minY)
  const padding = Math.max(width, height) * .08
  return <svg role="img" aria-label={`${orientation.label} exact selected slice contour`} viewBox={`${bounds.minX - padding} ${bounds.minY - padding} ${width + padding * 2} ${height + padding * 2}`} className="size-full bg-white">
    <title>{orientation.label} exact selected slice contour</title>
    <path transform={`translate(0 ${bounds.minY + bounds.maxY}) scale(1 -1)`} d={orientation.contours.map((contour) => contourPoints(contour).map((point, index) => `${index ? "L" : "M"} ${point.x_mm} ${point.y_mm}`).join(" ") + " Z").join(" ")} fill="rgba(177,203,33,.32)" fillRule="evenodd" stroke="#667600" strokeWidth={Math.max(width, height) / 250} />
  </svg>
}

export function PoseTemplatesPage() {
  const client = useQueryClient()
  const [name, setName] = useState("Object pose template")
  const [description, setDescription] = useState("")
  const [paperSize, setPaperSize] = useState("A3")
  const [pageOrientation, setPageOrientation] = useState("landscape")
  const [scaleX, setScaleX] = useState(1)
  const [scaleY, setScaleY] = useState(1)
  const [instances, setInstances] = useState<PositionedTemplateInstance[]>([])
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [tagFilter, setTagFilter] = useState(ALL_FILTER)
  const [groupFilter, setGroupFilter] = useState(ALL_FILTER)
  const [chooser, setChooser] = useState<{ object: CatalogObject; analysis: PoseTemplateOrientationAnalysis; orientationId: string } | null>(null)
  const [previewState, setPreviewState] = useState<{ signature: string; data: PoseTemplatePreview } | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [pendingLibraryJob, setPendingLibraryJob] = useState<{ id: string; kind: "generate" | "clone" } | null>(null)
  const [pendingCleanup, setPendingCleanup] = useState<PendingTemplateCleanup | null>(null)
  const [deleteConfirmation, setDeleteConfirmation] = useState<PoseTemplateBundle | null>(null)
  const previewSequence = useRef(0)

  const source = useQuery({ queryKey: ["pose-template-source"], queryFn: () => api<PoseTemplateSourceStatus>("/pose-templates/status") })
  const catalog = useQuery({ queryKey: ["workpiece-catalog"], queryFn: () => api<{ objects: CatalogObject[] }>("/workpieces/catalog") })
  const library = useQuery({ queryKey: ["pose-template-library"], queryFn: () => api<{ templates: PoseTemplateBundle[] }>("/pose-templates/library") })
  const libraryJob = useQuery({
    queryKey: ["pose-template-library-job", pendingLibraryJob?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingLibraryJob!.id}`),
    enabled: Boolean(pendingLibraryJob),
    refetchInterval: (queryState) => TERMINAL_JOB_STATES.has(queryState.state.data?.job.status ?? "") ? false : 600,
  })
  const cleanupJob = useQuery({
    queryKey: ["pose-template-cleanup-job", pendingCleanup?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingCleanup!.id}`),
    enabled: Boolean(pendingCleanup?.id),
    refetchInterval: (queryState) => TERMINAL_JOB_STATES.has(queryState.state.data?.job.status ?? "") ? false : 600,
  })
  useEffect(() => {
    const job = libraryJob.data?.job
    if (!pendingLibraryJob || !job || !TERMINAL_JOB_STATES.has(job.status)) return
    if (job.status === "succeeded") {
      toast.success(pendingLibraryJob.kind === "clone" ? "Immutable template cloned" : "Immutable template generated")
      void client.invalidateQueries({ queryKey: ["pose-template-library"] })
    } else {
      toast.error(pendingLibraryJob.kind === "clone" ? "Template clone did not complete" : "Template generation did not complete", { description: jobFailureDetail(job) })
    }
    queueMicrotask(() => setPendingLibraryJob(null))
  }, [client, libraryJob.data?.job, pendingLibraryJob])
  useEffect(() => {
    const job = cleanupJob.data?.job
    if (!pendingCleanup?.id || !job || !TERMINAL_JOB_STATES.has(job.status)) return
    if (job.status === "succeeded") {
      toast.success("Pose-template file cleanup finished", { description: pendingCleanup.item.display_name })
      queueMicrotask(() => setPendingCleanup(null))
    } else {
      const detail = jobFailureDetail(job)
      if (pendingCleanup.error === detail) return
      toast.warning("Pose template is deleted, but file cleanup needs attention", { description: detail })
      queueMicrotask(() => setPendingCleanup((current) => current ? { ...current, error: detail } : current))
    }
  }, [cleanupJob.data?.job, pendingCleanup])
  const objects = useMemo(() => catalog.data?.objects ?? [], [catalog.data?.objects])
  const allTags = useMemo(() => facetValues(objects.filter((item) => item.state === "active").flatMap((item) => item.tags)), [objects])
  const allGroups = useMemo(() => facetValues(objects.filter((item) => item.state === "active").flatMap((item) => item.groups)), [objects])
  const activeObjects = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase()
    return objects.filter((item) => item.state === "active"
      && (tagFilter === ALL_FILTER || matchesFacet(item.tags, tagFilter))
      && (groupFilter === ALL_FILTER || matchesFacet(item.groups, groupFilter))
      && (!needle || [item.name, item.alias ?? "", item.description ?? "", ...item.tags, ...item.groups, ...Object.entries(item.attributes).flat()].join(" ").toLocaleLowerCase().includes(needle)))
  }, [groupFilter, objects, search, tagFilter])
  const page = pageDimensions(paperSize, pageOrientation)
  const maxInstances = source.data?.capabilities?.limits.instances ?? 200
  const selectedInstance = instances.find((item) => item.instance_uuid === selectedInstanceId) ?? null
  const configuration = useMemo(() => ({
    display_name: name,
    description: description || null,
    page: { size: paperSize, orientation: pageOrientation },
    print_compensation: { x_scale: scaleX, y_scale: scaleY },
    instances: instances.map(({ instance_uuid, catalog_uuid, orientation_id, pose }) => ({ instance_uuid, catalog_uuid, orientation_id, pose })),
  }), [description, instances, name, pageOrientation, paperSize, scaleX, scaleY])
  const signature = useMemo(() => JSON.stringify(configuration), [configuration])
  const freshPreview = previewState?.signature === signature ? previewState.data : null
  const chooserAnalysis = chooser?.analysis
  const chooserObject = chooser?.object
  const chooserOrientations = chooserAnalysis?.orientations
  const chooserPreview = useMemo(
    () => chooserObject && chooserAnalysis ? orientationPreviewQuality(chooserObject, chooserAnalysis) : null,
    [chooserAnalysis, chooserObject],
  )

  const cacheOrientationAnalysis = (object: CatalogObject, analysis: PoseTemplateOrientationAnalysis) => {
    client.setQueryData(orientationAnalysisQueryKey(object), analysis)
    const orientation = analysis.orientations[0]
    if (!orientation) return
    client.setQueryData<PoseTemplateOrientationThumbnail>(orientationThumbnailQueryKey(object), {
      schema_version: "pose_template_orientation_thumbnail.v1",
      catalog_uuid: object.catalog_uuid,
      catalog: { catalog_uuid: object.catalog_uuid, name: object.name, obj_id: object.obj_id },
      source: { canonical_ply_sha256: object.canonical_ply_sha256, geometry_revision: object.geometry_revision },
      preview_mesh: analysis.recognition_mesh ?? analysis.preview_mesh,
      recognition_mesh_approximation: analysis.recognition_mesh_approximation,
      orientation: { orientation_id: orientation.orientation_id, label: orientation.label, rank: orientation.rank ?? 1, probability: orientation.probability, slice_z_mm: orientation.slice_z_mm, source_to_placed: orientation.source_to_placed },
    })
  }

  const orientationRequest = useMutation({
    mutationFn: async (object: CatalogObject) => {
      const key = orientationAnalysisQueryKey(object)
      const cached = client.getQueryData<PoseTemplateOrientationAnalysis>(key)
      if (cached) return { object, analysis: cached }
      try {
        const analysis = await api<PoseTemplateOrientationAnalysis>(`/pose-templates/workpieces/${object.catalog_uuid}/orientations`)
        cacheOrientationAnalysis(object, analysis)
        return { object, analysis }
      } catch (error) {
        if (!orientationAnalysisRequired(error)) throw error
      }
      const queued = await api<{ job_id: string }>(`/pose-templates/workpieces/${object.catalog_uuid}/orientations`, { method: "POST", body: "{}" })
      await waitForJob(queued.job_id)
      const analysis = await api<PoseTemplateOrientationAnalysis>(`/pose-templates/workpieces/${object.catalog_uuid}/orientations`)
      cacheOrientationAnalysis(object, analysis)
      return { object, analysis }
    },
    onSuccess: ({ object, analysis }) => {
      const first = analysis.orientations[0]
      if (!first) { toast.error("No printable stable orientation was found", { description: object.name }); return }
      setChooser({ object, analysis, orientationId: first.orientation_id })
    },
    onError: (error) => toast.error("Stable orientations are unavailable", { description: errorMessage(error) }),
  })

  useEffect(() => {
    const sequence = previewSequence.current + 1
    previewSequence.current = sequence
    if (!source.data?.available || !instances.length) {
      queueMicrotask(() => { if (previewSequence.current === sequence) { setPreviewBusy(false); setPreviewError(null) } })
      return
    }
    const controller = new AbortController()
    queueMicrotask(() => { if (previewSequence.current === sequence) { setPreviewBusy(true); setPreviewError(null) } })
    const timer = window.setTimeout(async () => {
      try {
        let queued: { request_id: string; job_id?: string } | null = null
        for (let attempt = 0; attempt < 12; attempt += 1) {
          try {
            queued = await api<{ request_id: string; job_id?: string }>("/pose-templates/preview", { method: "POST", body: JSON.stringify({ configuration }), signal: controller.signal })
            break
          } catch (error) {
            if (error instanceof ApiError && error.status === 409 && attempt < 11) { await delay(500, controller.signal); continue }
            throw error
          }
        }
        if (!queued) throw new Error("Preview could not be queued")
        const data = await waitForPreview(queued.request_id, queued.job_id, controller.signal)
        if (!controller.signal.aborted && previewSequence.current === sequence) setPreviewState({ signature, data })
      } catch (error) {
        if (!controller.signal.aborted && previewSequence.current === sequence) setPreviewError(errorMessage(error))
      } finally {
        if (!controller.signal.aborted && previewSequence.current === sequence) setPreviewBusy(false)
      }
    }, 450)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [configuration, instances.length, signature, source.data?.available])

  const generate = useMutation({
    mutationFn: () => api<{ job_id: string }>("/pose-templates/generate", { method: "POST", body: JSON.stringify({ configuration }) }),
    onSuccess: (value) => {
      toast.success("Immutable template generation queued", { description: `Job ${value.job_id}` })
      setPendingLibraryJob({ id: value.job_id, kind: "generate" })
      void client.invalidateQueries({ queryKey: ["jobs"] })
    },
    onError: (error) => toast.error("Generation failed", { description: errorMessage(error) }),
  })
  const libraryAction = useMutation<LibraryActionResult, Error, { item: PoseTemplateBundle; action: LibraryAction }>({
    mutationFn: ({ item, action }) => action === "delete"
      ? api<LibraryActionResult>(`/pose-templates/library/${item.template_uuid}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) })
      : api<LibraryActionResult>(`/pose-templates/library/${item.template_uuid}/${action}`, { method: "POST", body: action === "clone" ? JSON.stringify({}) : undefined }),
    onSuccess: (value, variables) => {
      if (variables.action === "clone" && value.job_id) {
        setPendingLibraryJob({ id: value.job_id, kind: "clone" })
        toast.success("Clone generation queued", { description: `Job ${value.job_id}` })
        void client.invalidateQueries({ queryKey: ["jobs"] })
      } else if (variables.action === "delete" && value.status) {
        if (value.status === "deleted_cleanup_pending") {
          const cleanupError = value.cleanup_job_error ?? value.asset_cleanup?.last_error ?? undefined
          setPendingCleanup({ item: variables.item, id: value.job_id, error: value.job_id ? undefined : cleanupError })
          if (value.job_id) {
            toast.success("Pose template deleted", { description: `File cleanup continues after navigation in job ${value.job_id}.` })
            void client.invalidateQueries({ queryKey: ["jobs"] })
          } else {
            toast.warning("Pose template deleted; file cleanup is pending", { description: cleanupError ?? variables.item.display_name })
          }
        } else {
          toast.success("Pose template deleted", { description: variables.item.display_name })
          setPendingCleanup((current) => current?.item.template_uuid === variables.item.template_uuid ? null : current)
        }
        setDeleteConfirmation(null)
        client.removeQueries({ queryKey: ["pose-template-library-thumbnail", variables.item.template_uuid] })
        void client.invalidateQueries({ queryKey: ["pose-template-library"] })
      } else {
        toast.success(`Template ${variables.action}d`)
        void client.invalidateQueries({ queryKey: ["pose-template-library"] })
      }
    },
    onError: (error, variables) => toast.error(variables.action === "delete" ? "Pose template was not deleted" : "Template action failed", { description: errorMessage(error) }),
  })

  const addChosenOrientation = () => {
    if (!chooser || instances.length >= maxInstances) return
    const orientation = chooser.analysis.orientations.find((item) => item.orientation_id === chooser.orientationId)
    if (!orientation) return
    const offset = (instances.length % 6) * 12
    const instance: PositionedTemplateInstance = {
      instance_uuid: createInstanceUuid(), catalog_uuid: chooser.object.catalog_uuid, orientation_id: orientation.orientation_id,
      pose: { x_mm: 40 + offset, y_mm: 40 + offset, rotation_deg: 0 }, object: chooser.object, orientation, preview_mesh: chooserPreview?.mesh ?? chooser.analysis.preview_mesh,
    }
    setInstances((current) => [...current, instance])
    setSelectedInstanceId(instance.instance_uuid)
    setChooser(null)
  }
  const updatePose = (id: string, pose: Partial<PositionedTemplateInstance["pose"]>) => setInstances((current) => current.map((item) => item.instance_uuid === id ? { ...item, pose: { ...item.pose, ...pose } } : item))
  const removeInstance = (id: string) => { setInstances((current) => current.filter((item) => item.instance_uuid !== id)); if (selectedInstanceId === id) setSelectedInstanceId(null) }
  const filterActive = Boolean(search || tagFilter !== ALL_FILTER || groupFilter !== ALL_FILTER)
  const clearFilters = () => { setSearch(""); setTagFilter(ALL_FILTER); setGroupFilter(ALL_FILTER) }
  const orientationDisabledReason = !source.data?.available
    ? source.data?.reason ?? "Pose-template geometry analysis is unavailable."
    : instances.length >= maxInstances
      ? `This template already contains the maximum of ${maxInstances} instances. Remove one before adding another.`
      : null
  const generationDisabledReason = !source.data?.available
    ? source.data?.reason ?? "Pose-template geometry analysis is unavailable."
    : instances.length === 0
      ? "Choose at least one workpiece orientation before generating a template."
      : previewError
        ? `Resolve the validation error above: ${previewError}`
        : !freshPreview?.valid && !previewBusy
          ? "Wait for a valid exact server preview before generating the immutable version."
          : null
  const orientationProjectionSpan = useMemo(() => chooserOrientations && chooserPreview ? Math.max(.01, ...chooserOrientations.flatMap((orientation) => {
    const bounds = projectIsometricMesh(chooserPreview.mesh, orientation.source_to_placed).bounds
    return [bounds.maxX - bounds.minX, bounds.maxY - bounds.minY]
  })) : undefined, [chooserOrientations, chooserPreview])

  return <div className="space-y-7" data-testid="pose-templates-page">
    <PageHeader eyebrow="Object ground truth" title="Pose Templates" description="Choose catalogue workpieces, confirm how each one physically rests, arrange its exact selected-slice contours, and publish an immutable printable version." actions={<Button variant="outline" onClick={() => { void source.refetch(); void catalog.refetch(); void library.refetch() }}><RefreshCw />Refresh</Button>} />
    <ProcessHandoff
      title="Publish here, then select and place in the dataset workflow"
      description="Template generation adds a reusable immutable version to the library; it does not alter the active run. Object-dataset step 2 snapshots one version and records its measured physical placement."
      to="/workflow/dataset?step=template"
      action="Select for dataset"
    />

    <Card className={source.data?.available ? "border-success/35" : "border-warning/50"}><CardContent className="flex items-start justify-between gap-5 pt-4"><div><div className="flex items-center gap-2 text-sm font-semibold">PoseTemplateCreator <StatusBadge status={source.data?.status} tone={source.data?.available ? "success" : source.isError ? "destructive" : "warning"} /></div><p className="mt-1 text-xs text-muted-foreground">{source.data?.available ? `Pinned revision ${source.data.revision?.slice(0, 12)} · stable-pose analysis and exact closed-contour validation available` : source.data?.reason ?? "Checking source checkout…"}</p>{!source.data?.available && <code className="mt-2 block rounded bg-muted px-2 py-1 text-[11px]">bash scripts/install.sh --with-posetemplatecreator</code>}</div>{source.data?.capabilities && <div className="text-right text-[11px] text-muted-foreground">PLY · STL · OBJ<br />up to {source.data.capabilities.limits.instances} instances</div>}</CardContent></Card>
    {pendingLibraryJob && <Card className="border-primary/35 bg-primary/5" data-testid="pose-template-library-job"><CardContent className="flex items-center justify-between gap-4 py-4"><div className="flex min-w-0 items-center gap-3"><LoaderCircle className="size-4 shrink-0 animate-spin text-primary" /><div className="min-w-0"><div className="text-sm font-semibold">{pendingLibraryJob.kind === "clone" ? "Cloning immutable template" : "Generating immutable template"}</div><div className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground"><span className="font-mono">{pendingLibraryJob.id}</span> · this background work continues after navigation; Jobs shows its live status and output.</div></div></div><div className="flex shrink-0 items-center gap-2"><Button size="sm" variant="outline" asChild><Link to="/jobs">Open Jobs</Link></Button><StatusBadge status={libraryJob.data?.job.status ?? "queued"} tone={jobStatusTone(libraryJob.data?.job.status ?? "queued")} /></div></CardContent></Card>}
    {pendingCleanup && <Card className={pendingCleanup.error ? "border-warning/50 bg-warning/5" : "border-primary/35 bg-primary/5"} data-testid="pose-template-cleanup-job"><CardContent className="flex items-center justify-between gap-4 py-4"><div className="flex items-center gap-3">{pendingCleanup.error ? <Trash2 className="size-4 text-warning" /> : <LoaderCircle className="size-4 animate-spin text-primary" />}<div><div className="text-sm font-semibold">{pendingCleanup.error ? "Deleted template cleanup needs attention" : "Cleaning deleted template files"}</div><div className="mt-0.5 text-[10px] text-muted-foreground">{pendingCleanup.item.display_name}{pendingCleanup.id ? <span className="font-mono"> · {pendingCleanup.id}</span> : null} · cleanup continues after navigation</div>{pendingCleanup.error && <div className="mt-1 max-w-3xl text-[10px] text-warning">{pendingCleanup.error}</div>}</div></div><div className="flex items-center gap-2">{pendingCleanup.error && <Button size="sm" variant="outline" disabled={libraryAction.isPending} onClick={() => libraryAction.mutate({ item: pendingCleanup.item, action: "delete" })}><RefreshCw />Retry cleanup</Button>}<Button size="sm" variant="outline" asChild><Link to="/jobs">Open Jobs</Link></Button>{pendingCleanup.id && <StatusBadge status={cleanupJob.data?.job.status ?? "queued"} tone={jobStatusTone(cleanupJob.data?.job.status ?? "queued")} />}</div></CardContent></Card>}

    <section className="space-y-3" aria-labelledby="choose-workpieces-heading">
      <div className="flex items-end justify-between gap-4"><div><div className="mb-1 text-[10px] font-bold uppercase tracking-[.16em] text-primary-strong">Template authoring · 1 of 3</div><h2 id="choose-workpieces-heading" className="text-lg font-semibold">Choose catalogue workpieces</h2><p className="text-xs text-muted-foreground">Only active workpieces can be added. Tags and groups narrow the catalogue independently.</p></div><Button asChild variant="outline"><Link to="/workpieces"><PackageSearch />Manage catalogue</Link></Button></div>
      <Card><CardContent className="grid items-end gap-3 py-4 lg:grid-cols-[minmax(230px,1fr)_180px_180px_auto]"><div className="space-y-1.5"><Label htmlFor="template-workpiece-search">Search</Label><div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input id="template-workpiece-search" aria-label="Filter template workpieces" className="pl-9" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Name, alias, attribute…" /></div></div><div className="space-y-1.5"><Label>Tag</Label><Select value={tagFilter} onValueChange={setTagFilter}><SelectTrigger aria-label="Filter template workpieces by tag"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={ALL_FILTER}>All tags</SelectItem>{allTags.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label>Group</Label><Select value={groupFilter} onValueChange={setGroupFilter}><SelectTrigger aria-label="Filter template workpieces by group"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={ALL_FILTER}>All groups</SelectItem>{allGroups.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent></Select></div><Button variant="ghost" onClick={clearFilters} disabled={!filterActive}><X />Clear</Button></CardContent></Card>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{activeObjects.map((item) => <Card key={item.catalog_uuid} data-testid={`template-workpiece-${item.catalog_uuid}`}><CardContent className="space-y-3 p-3"><WorkpieceIsometricThumbnail object={item} className="h-32" /><div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="truncate text-sm font-semibold">{item.name}</div><div className="mt-0.5 text-[10px] text-muted-foreground">{item.alias ? `${item.alias} · ` : ""}obj_{String(item.obj_id).padStart(6, "0")}</div></div><StatusBadge status={item.state} tone={item.state === "active" ? "informational" : "neutral"} /></div>{item.tags.length + item.groups.length > 0 && <div className="flex flex-wrap gap-1">{item.groups.slice(0, 2).map((value) => <Badge variant="outline" key={`group-${value}`}>{value}</Badge>)}{item.tags.slice(0, 2).map((value) => <Badge variant="secondary" key={`tag-${value}`}>{value}</Badge>)}</div>}<Button className="w-full" size="sm" disabled={!source.data?.available || instances.length >= maxInstances || orientationRequest.isPending} aria-describedby={orientationDisabledReason ? "pose-template-orientation-disabled-reason" : undefined} onClick={() => orientationRequest.mutate(item)} aria-label={`Choose orientation for ${item.name}`}>{orientationRequest.isPending && orientationRequest.variables?.catalog_uuid === item.catalog_uuid ? <LoaderCircle className="animate-spin" /> : <Sparkles />}Choose orientation</Button></CardContent></Card>)}</div>
      {orientationDisabledReason && <p id="pose-template-orientation-disabled-reason" data-testid="pose-template-disabled-action-reason" className="rounded-md border border-warning/35 bg-warning/5 px-3 py-2 text-xs text-muted-foreground">{orientationDisabledReason}</p>}
      {!catalog.isPending && !activeObjects.length && <Card><CardContent className="py-10 text-center text-sm text-muted-foreground">{objects.length ? "No active workpieces match these filters." : <>No workpieces yet. <Link className="text-primary underline underline-offset-4" to="/workpieces">Open Workpiece Catalogue</Link> to add one.</>}</CardContent></Card>}
    </section>

    <section className="space-y-3" aria-labelledby="arrange-template-heading"><div><div className="mb-1 text-[10px] font-bold uppercase tracking-[.16em] text-primary-strong">Template authoring · 2 of 3</div><h2 id="arrange-template-heading" className="text-lg font-semibold">Arrange selected slice contours</h2><p className="text-xs text-muted-foreground">Drag to move, use the round handle to rotate, or enter exact planar values. Arrow keys nudge 1 mm; Shift nudges 10 mm.</p></div>
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.35fr)_360px]">
        <div data-testid="pose-template-preview-canvas"><TemplateLayoutCanvas instances={instances} selectedId={selectedInstanceId} page={page} onSelect={setSelectedInstanceId} onPose={updatePose} onRemove={removeInstance} /></div>
        <div className="space-y-4"><Card><CardHeader><CardTitle className="text-base">Template setup</CardTitle><CardDescription>All object coordinates are relative to the blue origin, 15 mm from the lower-left page corner.</CardDescription></CardHeader><CardContent className="space-y-4"><div className="space-y-1.5"><Label htmlFor="template-name">Name</Label><Input id="template-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div className="space-y-1.5"><Label htmlFor="template-description">Description</Label><Textarea id="template-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div><div className="grid grid-cols-2 gap-3"><div className="space-y-1.5"><Label>Paper</Label><Select value={paperSize} onValueChange={setPaperSize}><SelectTrigger aria-label="Template paper size"><SelectValue /></SelectTrigger><SelectContent>{Object.keys(PAGE_SIZES).map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectContent></Select></div><div className="space-y-1.5"><Label>Page orientation</Label><Select value={pageOrientation} onValueChange={setPageOrientation}><SelectTrigger aria-label="Template page orientation"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="landscape">Landscape</SelectItem><SelectItem value="portrait">Portrait</SelectItem></SelectContent></Select></div></div><div className="grid grid-cols-2 gap-3"><div className="space-y-1.5"><Label className="flex items-center gap-1">X print % <HelpTip label="template X print compensation">Scales the authored X placement and contours about the page centre to compensate for a measured printer error. The server rechecks printable bounds.</HelpTip></Label><Input aria-label="X print %" type="number" step="0.1" min="50" max="150" value={Number((scaleX * 100).toFixed(3))} onChange={(event) => setScaleX(Number(event.target.value) / 100)} /></div><div className="space-y-1.5"><Label className="flex items-center gap-1">Y print % <HelpTip label="template Y print compensation">Scales Y independently. Keep 100% unless the physical print was measured and requires compensation.</HelpTip></Label><Input aria-label="Y print %" type="number" step="0.1" min="50" max="150" value={Number((scaleY * 100).toFixed(3))} onChange={(event) => setScaleY(Number(event.target.value) / 100)} /></div></div></CardContent></Card>
        {selectedInstance ? <Card data-testid="selected-template-instance"><CardHeader><div className="flex items-start justify-between gap-3"><div><CardTitle className="text-base">{selectedInstance.object.name}</CardTitle><CardDescription>{selectedInstance.orientation.label} · {Math.round(selectedInstance.orientation.probability * 100)}% stability estimate · selected slice {selectedInstance.orientation.slice_z_mm.toFixed(2)} mm</CardDescription></div><Button variant="ghost" size="icon" aria-label={`Remove ${selectedInstance.object.name} instance`} onClick={() => removeInstance(selectedInstance.instance_uuid)}><Trash2 /></Button></div></CardHeader><CardContent className="space-y-3"><div className="h-36 overflow-hidden rounded-lg border"><IsometricMeshPreview mesh={selectedInstance.preview_mesh} transform={selectedInstance.orientation.source_to_placed} label={`${selectedInstance.object.name} ${selectedInstance.orientation.label}`} testId="selected-instance-isometric" /></div><div className="grid grid-cols-3 gap-2">{(["x_mm", "y_mm", "rotation_deg"] as const).map((key) => <div className="space-y-1" key={key}><Label className="text-[10px]">{key === "x_mm" ? "X mm" : key === "y_mm" ? "Y mm" : "Rotation °"}</Label><Input aria-label={`${selectedInstance.object.name} ${key === "x_mm" ? "X mm" : key === "y_mm" ? "Y mm" : "Rotation °"}`} type="number" step="0.1" value={selectedInstance.pose[key]} onChange={(event) => updatePose(selectedInstance.instance_uuid, { [key]: Number(event.target.value) })} /></div>)}</div></CardContent></Card> : <Card><CardContent className="py-8 text-center text-xs text-muted-foreground">Select an arranged object to inspect its stable pose and exact planar placement.</CardContent></Card>}
        <Card className={freshPreview?.valid ? "border-success/35" : previewError ? "border-destructive/40" : "border-primary/25"}><CardContent className="space-y-3 pt-4"><div className="flex items-start justify-between gap-3"><div><div className="text-sm font-semibold">Exact server validation</div><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">The current configuration must finish validation before immutable generation is enabled. X/Y print compensation scales about the page centre and can push a contour outside the printable bounds; this check catches that before publication.</p></div>{previewBusy || (instances.length > 0 && !freshPreview && !previewError) ? <LoaderCircle className="size-4 animate-spin text-primary" /> : <StatusBadge status={freshPreview?.valid ? "valid" : previewError ? "failed" : "waiting"} tone={freshPreview?.valid ? "success" : previewError ? "destructive" : "warning"} />}</div>{freshPreview?.errors[0] && <p className="text-xs text-destructive">{freshPreview.errors[0].message}</p>}{previewError && <p className="text-xs text-destructive">{previewError}</p>}<Button className="w-full" aria-describedby={generationDisabledReason ? "pose-template-generation-disabled-reason" : undefined} onClick={() => generate.mutate()} disabled={!source.data?.available || !freshPreview?.valid || previewBusy || generate.isPending || Boolean(pendingLibraryJob)}><Save />{generate.isPending ? "Queueing…" : pendingLibraryJob ? "Library job running…" : "Generate immutable version"}</Button>{generationDisabledReason && <p id="pose-template-generation-disabled-reason" data-testid="pose-template-generation-disabled-reason" className="text-[11px] leading-relaxed text-muted-foreground">{generationDisabledReason}</p>}</CardContent></Card></div>
      </div>
    </section>

    <section className="space-y-3">
      <div><div className="mb-1 text-[10px] font-bold uppercase tracking-[.16em] text-primary-strong">Template authoring · 3 of 3</div><h2 className="text-lg font-semibold">Immutable template library</h2><p className="text-xs text-muted-foreground">Cards use bounded footprint thumbnails; a Simplified label means points or secondary contours were reduced for fast browsing. The PDF and immutable full preview remain exact. Archive is reversible; permanent deletion removes only the global library version, while run-owned snapshots remain unchanged.</p></div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{library.data?.templates.map((item) => <Card key={item.template_uuid} data-testid={`pose-template-library-card-${item.template_uuid}`}><CardContent className="space-y-3 p-3">
        <TemplateFootprintThumbnail bundle={item} />
        <div className="flex items-start justify-between gap-3"><div><div className="font-semibold">{item.display_name}</div><div className="mt-1 text-[10px] text-muted-foreground">{item.instances.length} instance{item.instances.length === 1 ? "" : "s"} · {item.template_uuid.slice(0, 8)}</div></div><StatusBadge status={item.archive.state} tone={item.archive.state === "active" ? "informational" : "neutral"} /></div>
        <div className="flex flex-wrap gap-2">
          <Button asChild size="sm" variant="outline"><a href={`/pose-templates/library/${item.template_uuid}/download/pdf`}><Download />PDF</a></Button>
          <Button asChild size="sm" variant="outline"><a href={`/pose-templates/library/${item.template_uuid}/download/manifest`}><Download />Manifest</a></Button>
          <Button size="sm" variant="outline" disabled={Boolean(pendingLibraryJob) || libraryAction.isPending} onClick={() => libraryAction.mutate({ item, action: "clone" })}><Copy />Clone</Button>
          <Button size="sm" variant="ghost" disabled={libraryAction.isPending} onClick={() => libraryAction.mutate({ item, action: item.archive.state === "active" ? "archive" : "restore" })}>{item.archive.state === "active" ? <Archive /> : <RotateCcw />}{item.archive.state === "active" ? "Archive" : "Restore"}</Button>
          <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" aria-label={`Delete ${item.display_name}`} title="Permanently delete this pose-template version" disabled={libraryAction.isPending} onClick={() => setDeleteConfirmation(item)}><Trash2 />Delete</Button>
        </div>
      </CardContent></Card>)}</div>
    </section>

    <Dialog open={deleteConfirmation !== null} onOpenChange={(open) => { if (!open && !libraryAction.isPending) setDeleteConfirmation(null) }}>
      <DialogContent data-testid="pose-template-delete-confirmation">
        <DialogHeader>
          <DialogTitle>Permanently delete {deleteConfirmation?.display_name ?? "this pose template"}?</DialogTitle>
          <DialogDescription>The library entry is removed immediately. Its PDF, preview, and copied object assets are then cleaned in a background job that continues after navigation and is visible in Jobs. Existing run-owned snapshots remain intact. This action cannot be undone.</DialogDescription>
        </DialogHeader>
        <DialogFooter><Button variant="outline" onClick={() => setDeleteConfirmation(null)} disabled={libraryAction.isPending}>Cancel</Button><Button variant="destructive" onClick={() => deleteConfirmation && libraryAction.mutate({ item: deleteConfirmation, action: "delete" })} disabled={libraryAction.isPending}>{libraryAction.isPending ? <LoaderCircle className="animate-spin" /> : <Trash2 />}Confirm delete</Button></DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={chooser !== null} onOpenChange={(open) => { if (!open) setChooser(null) }}><DialogContent className="max-h-[92vh] max-w-7xl overflow-y-auto" data-testid="orientation-chooser"><DialogHeader><DialogTitle>Choose how {chooser?.object.name ?? "this workpiece"} rests</DialogTitle><DialogDescription>PoseTemplateCreator found stable orientations. Compare the same-scale high-detail recognition surface with the exact selected slice contour; the tiny printable-layout proxy is not used here. A 0 mm slice is true contact; a positive slice is an adaptive printable cross-section near the base (typically 0.5–5% of object height).</DialogDescription></DialogHeader>{chooser && chooserPreview && <div className="grid grid-cols-1 gap-3 lg:grid-cols-2" role="radiogroup" aria-label={`Stable orientation for ${chooser.object.name}`}>{chooser.analysis.orientations.map((orientation) => { const bounds = orientationBounds(orientation); const selected = chooser.orientationId === orientation.orientation_id; return <button type="button" role="radio" aria-checked={selected} className={`overflow-hidden rounded-lg border text-left transition-colors ${selected ? "border-primary bg-primary/5 ring-2 ring-primary/30" : "hover:border-foreground/25"}`} onClick={() => setChooser({ ...chooser, orientationId: orientation.orientation_id })} key={orientation.orientation_id}><div className="grid grid-cols-2 gap-px bg-border"><div className="relative h-44 bg-[#10171d]" data-preview-quality={chooserPreview.proxy ? "proxy" : "recognition"} data-displayed-faces={chooserPreview.displayedFaces} data-source-faces={chooserPreview.sourceFaces}><span className="absolute left-2 top-2 z-10 rounded bg-black/60 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white">High-detail 3D</span><span className="absolute right-2 top-2 z-10 rounded bg-black/60 px-1.5 py-0.5 text-[9px] font-medium text-slate-200">{chooserPreview.displayedFaces.toLocaleString()} faces</span><IsometricMeshPreview mesh={chooserPreview.mesh} transform={orientation.source_to_placed} commonSpan={orientationProjectionSpan} label={`${chooser.object.name} ${orientation.label}`} testId={`orientation-isometric-${orientation.orientation_id}`} /></div><div className="relative h-44 bg-white"><span className="absolute left-2 top-2 z-10 rounded bg-white/85 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-slate-700">Exact slice</span><BaseContourPreview orientation={orientation} /></div></div><div className={`border-t px-3 py-1.5 text-[9px] ${chooserPreview.proxy ? "bg-warning/10 text-warning" : "bg-muted/30 text-muted-foreground"}`} data-testid={`orientation-preview-quality-${orientation.orientation_id}`}>{chooserPreview.label} · {chooserPreview.detail}</div><div className="p-3"><div className="flex items-center justify-between gap-2"><span className="text-xs font-semibold">{orientation.label}</span>{selected && <CheckCircle2 className="size-4 text-primary" />}</div><div className="mt-1 text-[10px] text-muted-foreground">{Math.round(orientation.probability * 100)}% stability estimate · {(bounds.maxX - bounds.minX).toFixed(1)} × {(bounds.maxY - bounds.minY).toFixed(1)} mm · slice z {orientation.slice_z_mm.toFixed(2)} mm</div></div></button>})}</div>}{instances.length >= maxInstances && <p className="rounded border border-warning/35 bg-warning/5 p-3 text-xs text-muted-foreground">This template already contains the maximum of {maxInstances} instances. Remove one before adding another orientation.</p>}<DialogFooter><Button variant="outline" onClick={() => setChooser(null)}>Cancel</Button><Button onClick={addChosenOrientation} disabled={!chooser || instances.length >= maxInstances}><Plus />Add selected orientation</Button></DialogFooter></DialogContent></Dialog>
  </div>
}
