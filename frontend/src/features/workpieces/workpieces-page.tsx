import { useEffect, useMemo, useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Archive,
  Box,
  Download,
  FileJson,
  FileUp,
  Layers3,
  LoaderCircle,
  PackageOpen,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Scaling,
  Tag,
  Trash2,
  X,
} from "lucide-react"
import { Link } from "react-router-dom"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api, errorMessage } from "@/lib/api"
import type { CatalogObject, Job } from "@/lib/contracts"
import { jobFailureDetail, jobStatusTone } from "@/lib/jobs"
import { WorkpieceIsometricThumbnail } from "@/components/geometry-previews"
import { WorkpiecePreviews } from "./workpiece-previews"

type Workpiece = CatalogObject

interface WorkpieceStatus {
  schema_version: string
  available: boolean
  status: string
  reason: string | null
  catalog_root: string
  formats: string[]
  limits: { cad_bytes: number; batch_bytes: number }
  counts: { active: number; archived: number; total: number }
  unit_corrections?: { supported: boolean; requires_archived: boolean }
}

interface CatalogueResponse {
  objects: Workpiece[]
  [key: string]: unknown
}

interface UploadResponse {
  job_id: string
  request_id?: string
  job?: Job
}

interface ImportResponse {
  schema_version: string
  updated: string[]
  unchanged: string[]
  skipped_missing_assets: string[]
}

interface AttributeRow {
  id: number
  key: string
  value: string
}

interface AttributeValidation {
  invalidRowIds: Set<number>
  message: string | null
}

interface MetadataDraft {
  name: string
  alias: string
  description: string
  tags: string
  groups: string
  attributes: AttributeRow[]
}

interface UploadDraft extends MetadataDraft {
  cad: File | null
  texture: File | null
}

type CatalogueAction = "archive" | "restore" | "delete"
type UnitConversion = "meter_to_millimeter" | "millimeter_to_meter"

const ALL_FILTER = "__all__"
const EDIT_ATTRIBUTE_VALIDATION_TOAST = "workpiece-edit-attribute-validation"
const UPLOAD_ATTRIBUTE_VALIDATION_TOAST = "workpiece-upload-attribute-validation"
const terminalJobStates = new Set(["succeeded", "failed", "canceled"])
let nextAttributeId = 0

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

function geometryRevisionSummary(item: Workpiece) {
  const revision = item.geometry_revisions?.find((entry) => entry.revision === item.geometry_revision)
  const operation = revision?.operation
  if (!operation || typeof operation !== "object" || Array.isArray(operation)) return "Imported canonical geometry"
  const conversion = operation.conversion === "meter_to_millimeter"
    ? "Metre → millimetre correction"
    : operation.conversion === "millimeter_to_meter"
      ? "Millimetre → metre correction"
      : null
  const operator = typeof operation.operator === "string" ? operation.operator : null
  return [conversion ?? "Imported canonical geometry", operator ? `by ${operator}` : null].filter(Boolean).join(" · ")
}

function attributeRow(key = "", value = ""): AttributeRow {
  nextAttributeId += 1
  return { id: nextAttributeId, key, value }
}

function emptyMetadata(): MetadataDraft {
  return { name: "", alias: "", description: "", tags: "", groups: "", attributes: [] }
}

function emptyUpload(): UploadDraft {
  return { ...emptyMetadata(), cad: null, texture: null }
}

function metadataFor(item: Workpiece): MetadataDraft {
  const attributes = Object.entries(item.attributes ?? {}).map(([key, value]) => attributeRow(key, value))
  return {
    name: item.name,
    alias: item.alias ?? "",
    description: item.description ?? "",
    tags: (item.tags ?? []).join(", "),
    groups: (item.groups ?? []).join(", "),
    attributes,
  }
}

function parseList(value: string) {
  const seen = new Set<string>()
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => {
      const normalized = item.toLocaleLowerCase()
      if (!item || seen.has(normalized)) return false
      seen.add(normalized)
      return true
    })
}

function validateAttributeRows(rows: AttributeRow[]): AttributeValidation {
  const invalidRowIds = new Set<number>()
  const blankRows: number[] = []
  const rowsByNormalizedKey = new Map<string, { label: string; rowIds: number[]; rowNumbers: number[] }>()

  rows.forEach((row, index) => {
    const key = row.key.trim()
    if (!key) {
      invalidRowIds.add(row.id)
      blankRows.push(index + 1)
      return
    }

    const normalizedKey = key.toLocaleLowerCase()
    const existing = rowsByNormalizedKey.get(normalizedKey)
    if (existing) {
      existing.rowIds.push(row.id)
      existing.rowNumbers.push(index + 1)
    } else {
      rowsByNormalizedKey.set(normalizedKey, { label: key, rowIds: [row.id], rowNumbers: [index + 1] })
    }
  })

  const duplicateMessages: string[] = []
  rowsByNormalizedKey.forEach(({ label, rowIds, rowNumbers }) => {
    if (rowIds.length < 2) return
    rowIds.forEach((id) => invalidRowIds.add(id))
    duplicateMessages.push(`“${label}” in rows ${rowNumbers.join(", ")}`)
  })

  const messages: string[] = []
  if (blankRows.length) messages.push(`Add a name or remove attribute row${blankRows.length === 1 ? "" : "s"} ${blankRows.join(", ")}.`)
  if (duplicateMessages.length) messages.push(`Attribute names must be unique, ignoring capitalization: ${duplicateMessages.join("; ")}.`)
  return { invalidRowIds, message: messages.join(" ") || null }
}

function attributesRecord(rows: AttributeRow[]) {
  const validation = validateAttributeRows(rows)
  if (validation.message) throw new Error(validation.message)

  const attributes: Record<string, string> = {}
  rows.forEach((row) => {
    attributes[row.key.trim()] = row.value.trim()
  })
  return attributes
}

function metadataPayload(draft: MetadataDraft) {
  return {
    name: draft.name.trim(),
    alias: draft.alias.trim() || null,
    description: draft.description.trim() || null,
    tags: parseList(draft.tags),
    groups: parseList(draft.groups),
    attributes: attributesRecord(draft.attributes),
  }
}

function formatBytes(value: number | undefined) {
  if (!Number.isFinite(value)) return "—"
  if ((value ?? 0) < 1_000_000) return `${Math.round((value ?? 0) / 1_000)} kB`
  return `${((value ?? 0) / 1_000_000).toFixed(1)} MB`
}

function formatBounds(bounds: number[][] | undefined) {
  if (!Array.isArray(bounds) || bounds.length < 2) return "—"
  const lower = bounds[0] ?? []
  const upper = bounds[1] ?? []
  const dimensions = [0, 1, 2].map((index) => Number(upper[index]) - Number(lower[index]))
  if (!dimensions.every(Number.isFinite)) return "—"
  return dimensions.map((value) => `${value.toFixed(1)}`).join(" × ") + " mm"
}

function formatScaledBounds(bounds: number[][] | undefined, factor: number) {
  if (!Array.isArray(bounds) || bounds.length < 2) return "—"
  const dimensions = [0, 1, 2].map((index) => (Number(bounds[1]?.[index]) - Number(bounds[0]?.[index])) * factor)
  if (!dimensions.every(Number.isFinite)) return "—"
  return dimensions.map((value) => value >= 1000 ? value.toLocaleString(undefined, { maximumFractionDigits: 1 }) : value.toFixed(value < 1 ? 3 : 1)).join(" × ") + " mm"
}

function templateLabel(value: unknown, index: number) {
  if (typeof value === "string") return value
  if (!value || typeof value !== "object") return `Template ${index + 1}`
  const item = value as Record<string, unknown>
  return String(item.display_name ?? item.name ?? item.template_uuid ?? `Template ${index + 1}`)
}

function Field({ label, htmlFor, children, hint }: { label: string; htmlFor?: string; children: React.ReactNode; hint?: string }) {
  return <div className="space-y-1.5">
    <Label htmlFor={htmlFor}>{label}</Label>
    {children}
    {hint && <p className="text-[10px] leading-relaxed text-muted-foreground">{hint}</p>}
  </div>
}

function AttributeEditor({ rows, onChange, prefix, validation }: { rows: AttributeRow[]; onChange: (rows: AttributeRow[]) => void; prefix: string; validation?: AttributeValidation }) {
  const update = (id: number, key: "key" | "value", value: string) => onChange(rows.map((row) => row.id === id ? { ...row, [key]: value } : row))
  const remove = (id: number) => onChange(rows.filter((row) => row.id !== id))
  const errorId = `${prefix}-attribute-error`

  return <div className="space-y-2" data-testid={`${prefix}-attributes`}>
    <div className="flex items-center justify-between gap-3">
      <div><Label>Optional attributes</Label><p className="mt-1 text-[10px] text-muted-foreground">Custom string metadata, such as material, mass, or fixture family.</p></div>
      <Button size="sm" variant="outline" onClick={() => onChange([...rows, attributeRow()])}><Plus />Add attribute</Button>
    </div>
    <div className="space-y-2">
      {rows.map((row, index) => <div className="grid grid-cols-[minmax(0,.8fr)_minmax(0,1.2fr)_34px] gap-2" key={row.id}>
        <Input
          aria-label={`Attribute ${index + 1} name`}
          aria-describedby={validation?.message ? errorId : undefined}
          aria-invalid={validation?.invalidRowIds.has(row.id) || undefined}
          className={validation?.invalidRowIds.has(row.id) ? "border-destructive focus-visible:ring-destructive/40" : undefined}
          data-testid={`${prefix}-attribute-key-${index}`}
          placeholder="Attribute"
          value={row.key}
          onChange={(event) => update(row.id, "key", event.target.value)}
        />
        <Input
          aria-label={`Attribute ${index + 1} value`}
          data-testid={`${prefix}-attribute-value-${index}`}
          placeholder="Value"
          value={row.value}
          onChange={(event) => update(row.id, "value", event.target.value)}
        />
        <Button variant="ghost" size="icon" aria-label={`Remove attribute ${index + 1}`} onClick={() => remove(row.id)}><X /></Button>
      </div>)}
      {rows.length === 0 && <p className="rounded-md border border-dashed px-3 py-2 text-[11px] text-muted-foreground">No custom attributes.</p>}
    </div>
    {validation?.message && <p id={errorId} role="alert" data-testid={`${prefix}-attribute-error`} className="text-xs font-medium text-destructive">{validation.message}</p>}
  </div>
}

function MetadataFields({ draft, setDraft, prefix, attributeValidation }: { draft: MetadataDraft; setDraft: (draft: MetadataDraft) => void; prefix: string; attributeValidation?: AttributeValidation }) {
  return <div className="space-y-4">
    <div className="grid grid-cols-2 gap-3">
      <Field label="Display name" htmlFor={`${prefix}-name`}><Input id={`${prefix}-name`} data-testid={`${prefix}-name`} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required /></Field>
      <Field label="Alias" htmlFor={`${prefix}-alias`} hint="An optional short label used by operators."><Input id={`${prefix}-alias`} data-testid={`${prefix}-alias`} value={draft.alias} onChange={(event) => setDraft({ ...draft, alias: event.target.value })} /></Field>
    </div>
    <Field label="Description" htmlFor={`${prefix}-description`}><Textarea id={`${prefix}-description`} data-testid={`${prefix}-description`} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="How this workpiece is identified or used" /></Field>
    <div className="grid grid-cols-2 gap-3">
      <Field label="Tags" htmlFor={`${prefix}-tags`} hint="Comma-separated labels for filtering."><Input id={`${prefix}-tags`} data-testid={`${prefix}-tags`} value={draft.tags} onChange={(event) => setDraft({ ...draft, tags: event.target.value })} placeholder="metal, reflective, small" /></Field>
      <Field label="Groups" htmlFor={`${prefix}-groups`} hint="Comma-separated collections or test families."><Input id={`${prefix}-groups`} data-testid={`${prefix}-groups`} value={draft.groups} onChange={(event) => setDraft({ ...draft, groups: event.target.value })} placeholder="validation set, line A" /></Field>
    </div>
    <AttributeEditor rows={draft.attributes} onChange={(attributes) => setDraft({ ...draft, attributes })} prefix={prefix} validation={attributeValidation} />
  </div>
}

function Tokens({ values, variant = "secondary" }: { values: string[]; variant?: "secondary" | "outline" }) {
  if (!values.length) return <span className="text-xs text-muted-foreground">None</span>
  return <div className="flex flex-wrap gap-1.5">{values.map((value) => <Badge key={value} variant={variant} className="normal-case tracking-normal">{value}</Badge>)}</div>
}

function WorkpieceCard({ item, selected, onSelect }: { item: Workpiece; selected: boolean; onSelect: () => void }) {
  const tags = item.tags ?? []
  const groups = item.groups ?? []
  return <Card data-testid={`workpiece-card-${item.catalog_uuid}`} className={`relative ${selected ? "border-primary/60 bg-accent/45 shadow-sm" : "transition-colors hover:border-foreground/20 hover:bg-secondary/30"} ${item.state === "archived" ? "opacity-70" : ""}`}>
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      aria-label={`Select ${item.name}`}
      className="absolute inset-0 z-0 rounded-[10px] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/55"
    />
    <CardContent className="pointer-events-none relative z-10 flex gap-3 p-3.5">
      <WorkpieceIsometricThumbnail object={item} className="h-[76px] w-[92px] shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0"><div className="truncate text-sm font-semibold">{item.name}</div>{item.alias && <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{item.alias}</div>}</div>
          <StatusBadge status={item.state} tone={item.state === "active" ? "informational" : "neutral"} />
        </div>
        <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
          <span className="font-mono">obj_{String(item.obj_id).padStart(6, "0")}</span>
          <span>{item.source_format.toUpperCase()}</span>
        </div>
        {(tags.length > 0 || groups.length > 0) && <div className="mt-2 flex flex-wrap gap-1">{groups.slice(0, 2).map((value) => <Badge variant="outline" key={`group-${value}`} className="normal-case tracking-normal">{value}</Badge>)}{tags.slice(0, 2).map((value) => <Badge variant="secondary" key={`tag-${value}`} className="normal-case tracking-normal">{value}</Badge>)}</div>}
      </div>
    </CardContent>
  </Card>
}

function BackgroundJobProgress({ testId, title, description, jobId, status }: { testId: string; title: string; description: string; jobId: string; status: string }) {
  return <Card className="border-primary/40 bg-accent/25" data-testid={testId}>
    <CardContent className="flex items-center justify-between gap-4 py-4">
      <div className="flex min-w-0 items-center gap-3">
        <LoaderCircle className="size-5 shrink-0 animate-spin text-primary-strong" />
        <div className="min-w-0">
          <div className="text-sm font-semibold">{title}</div>
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{description} This background work continues after navigation; Jobs shows its live status and output.</p>
          <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground">{jobId}</p>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button asChild size="sm" variant="outline"><Link to="/jobs">Open Jobs</Link></Button>
        <StatusBadge status={status} tone={jobStatusTone(status)} />
      </div>
    </CardContent>
  </Card>
}

export function WorkpiecesPage() {
  const client = useQueryClient()
  const [search, setSearch] = useState("")
  const [tagFilter, setTagFilter] = useState(ALL_FILTER)
  const [groupFilter, setGroupFilter] = useState(ALL_FILTER)
  const [stateFilter, setStateFilter] = useState("active")
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadDraft, setUploadDraft] = useState<UploadDraft>(emptyUpload)
  const [uploadValidationAttempted, setUploadValidationAttempted] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [editDraft, setEditDraft] = useState<MetadataDraft>(emptyMetadata)
  const [editValidationAttempted, setEditValidationAttempted] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importFile, setImportFile] = useState<File | null>(null)
  const [unitCorrectionOpen, setUnitCorrectionOpen] = useState(false)
  const [unitConversion, setUnitConversion] = useState<UnitConversion>("meter_to_millimeter")
  const [unitCorrectionOperator, setUnitCorrectionOperator] = useState("")
  const [unitCorrectionConfirmed, setUnitCorrectionConfirmed] = useState(false)
  const [confirmation, setConfirmation] = useState<{ action: CatalogueAction; item: Workpiece } | null>(null)
  const [pendingUpload, setPendingUpload] = useState<{ id: string; filename: string; startedAt: number } | null>(null)
  const [pendingCorrection, setPendingCorrection] = useState<{ id: string; name: string; startedAt: number } | null>(null)
  const [pendingPreview, setPendingPreview] = useState<{ id: string; catalogUuid: string; name: string; startedAt: number } | null>(null)
  const uploadAttributeValidation = useMemo(() => validateAttributeRows(uploadDraft.attributes), [uploadDraft.attributes])
  const editAttributeValidation = useMemo(() => validateAttributeRows(editDraft.attributes), [editDraft.attributes])

  useEffect(() => {
    if (uploadValidationAttempted && !uploadAttributeValidation.message) {
      toast.dismiss(UPLOAD_ATTRIBUTE_VALIDATION_TOAST)
    }
  }, [uploadAttributeValidation.message, uploadValidationAttempted])

  useEffect(() => {
    if (editValidationAttempted && !editAttributeValidation.message) {
      toast.dismiss(EDIT_ATTRIBUTE_VALIDATION_TOAST)
    }
  }, [editAttributeValidation.message, editValidationAttempted])

  const status = useQuery({ queryKey: ["workpiece-status"], queryFn: () => api<WorkpieceStatus>("/workpieces/status") })
  const catalogue = useQuery({ queryKey: ["workpiece-catalog"], queryFn: () => api<CatalogueResponse>("/workpieces/catalog") })
  const objects = useMemo(() => catalogue.data?.objects ?? [], [catalogue.data?.objects])

  const allTags = useMemo(() => facetValues(objects.flatMap((item) => item.tags ?? [])), [objects])
  const allGroups = useMemo(() => facetValues(objects.flatMap((item) => item.groups ?? [])), [objects])
  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase()
    return objects.filter((item) => {
      if (stateFilter !== "all" && item.state !== stateFilter) return false
      if (tagFilter !== ALL_FILTER && !matchesFacet(item.tags ?? [], tagFilter)) return false
      if (groupFilter !== ALL_FILTER && !matchesFacet(item.groups ?? [], groupFilter)) return false
      if (!needle) return true
      const haystack = [
        item.name,
        item.alias ?? "",
        item.description ?? "",
        item.source_filename,
        item.catalog_uuid,
        ...(item.tags ?? []),
        ...(item.groups ?? []),
        ...Object.entries(item.attributes ?? {}).flat(),
      ].join(" ").toLocaleLowerCase()
      return haystack.includes(needle)
    })
  }, [groupFilter, objects, search, stateFilter, tagFilter])
  const effectiveSelectedId = selectedId && objects.some((item) => item.catalog_uuid === selectedId)
    ? selectedId
    : objects.find((item) => item.state === "active")?.catalog_uuid ?? objects[0]?.catalog_uuid ?? null
  const selected = objects.find((item) => item.catalog_uuid === effectiveSelectedId) ?? null

  const upload = useMutation({
    mutationFn: async (draft: UploadDraft) => {
      if (!draft.cad) throw new Error("Choose a CAD file before uploading.")
      const form = new FormData()
      form.set("cad", draft.cad)
      if (draft.texture) form.set("texture", draft.texture)
      const payload = metadataPayload(draft)
      form.set("name", payload.name)
      form.set("alias", payload.alias ?? "")
      form.set("description", payload.description ?? "")
      form.set("tags", JSON.stringify(payload.tags))
      form.set("groups", JSON.stringify(payload.groups))
      form.set("attributes", JSON.stringify(payload.attributes))
      return api<UploadResponse>("/workpieces/catalog/upload", { method: "POST", body: form })
    },
    onSuccess: (result, draft) => {
      setPendingUpload({ id: result.job_id, filename: draft.cad?.name ?? draft.name, startedAt: Date.now() })
      setUploadOpen(false)
      setUploadDraft(emptyUpload())
      setUploadValidationAttempted(false)
      toast.success("Workpiece inspection queued", { description: `${draft.cad?.name} · job ${result.job_id}` })
    },
    onError: (error) => toast.error("Workpiece upload failed", { description: errorMessage(error) }),
  })

  const uploadJob = useQuery({
    queryKey: ["workpiece-catalog-upload-job", pendingUpload?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingUpload!.id}`),
    enabled: Boolean(pendingUpload),
    refetchInterval: (queryState) => terminalJobStates.has(queryState.state.data?.job.status ?? "") ? false : 600,
  })

  useEffect(() => {
    const job = uploadJob.data?.job
    if (!pendingUpload || !job || !terminalJobStates.has(job.status)) return
    if (job.status === "succeeded") {
      const seconds = Math.max(1, Math.round((Date.now() - pendingUpload.startedAt) / 1_000))
      toast.success("Workpiece added to the catalogue", { description: `${pendingUpload.filename} · ${seconds} s` })
      void client.invalidateQueries({ queryKey: ["workpiece-catalog"] })
      void client.invalidateQueries({ queryKey: ["workpiece-status"] })
    } else {
      toast.error("Workpiece inspection did not complete", { description: jobFailureDetail(job) })
    }
    queueMicrotask(() => setPendingUpload(null))
  }, [client, pendingUpload, uploadJob.data?.job])

  const updateMetadata = useMutation({
    mutationFn: ({ id, draft }: { id: string; draft: MetadataDraft }) => api<Workpiece>(`/workpieces/catalog/${id}`, { method: "PATCH", body: JSON.stringify(metadataPayload(draft)) }),
    onSuccess: (item) => {
      toast.success("Workpiece metadata saved", { description: item.name })
      setEditOpen(false)
      setEditValidationAttempted(false)
      void client.invalidateQueries({ queryKey: ["workpiece-catalog"] })
    },
    onError: (error) => toast.error("Metadata was not saved", { description: errorMessage(error) }),
  })

  const catalogueAction = useMutation<Workpiece | { status: string; asset_cleanup?: { status: string; last_error?: string | null } }, Error, { action: CatalogueAction; item: Workpiece }>({
    mutationFn: ({ action, item }: { action: CatalogueAction; item: Workpiece }) => action === "delete"
      ? api<{ status: string }>(`/workpieces/catalog/${item.catalog_uuid}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) })
      : api<Workpiece>(`/workpieces/catalog/${item.catalog_uuid}/${action}`, { method: "POST" }),
    onSuccess: (result, variables) => {
      const verb = variables.action === "delete" ? "deleted" : variables.action === "archive" ? "archived" : "restored"
      if (variables.action === "delete" && "status" in result && result.status === "deleted_cleanup_pending") {
        toast.warning("Catalogue identity retired; file cleanup is pending", { description: result.asset_cleanup?.last_error ?? variables.item.name })
      } else {
        toast.success(`Workpiece ${verb}`, { description: variables.item.name })
      }
      if (variables.action === "delete" && selectedId === variables.item.catalog_uuid) setSelectedId(null)
      setConfirmation(null)
      void client.invalidateQueries({ queryKey: ["workpiece-catalog"] })
      void client.invalidateQueries({ queryKey: ["workpiece-status"] })
    },
    onError: (error) => toast.error("Catalogue action failed", { description: errorMessage(error) }),
  })

  const importCatalogue = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.set("catalog", file)
      return api<ImportResponse>("/workpieces/catalog/import", { method: "POST", body: form })
    },
    onSuccess: (result) => {
      const changed = result.updated.length
      toast.success("Catalogue metadata imported", { description: `${changed} updated · ${result.unchanged.length} unchanged${result.skipped_missing_assets.length ? ` · ${result.skipped_missing_assets.length} missing assets skipped` : ""}` })
      setImportOpen(false)
      setImportFile(null)
      void client.invalidateQueries({ queryKey: ["workpiece-catalog"] })
      void client.invalidateQueries({ queryKey: ["workpiece-status"] })
    },
    onError: (error) => toast.error("Catalogue import failed", { description: errorMessage(error) }),
  })

  const correctUnits = useMutation({
    mutationFn: ({ item, conversion, operator }: { item: Workpiece; conversion: UnitConversion; operator: string }) => {
      if (item.state !== "archived") throw new Error("Archive this workpiece before correcting its model units.")
      if (!item.canonical_ply_sha256) throw new Error("The current canonical mesh hash is unavailable; refresh the catalogue before retrying.")
      return api<{ job_id: string }>(`/workpieces/catalog/${item.catalog_uuid}/unit-corrections`, {
        method: "POST",
        body: JSON.stringify({
          conversion,
          confirm: true,
          operator: operator.trim(),
          expected_geometry_revision: item.geometry_revision ?? 1,
          expected_canonical_sha256: item.canonical_ply_sha256,
        }),
      })
    },
    onSuccess: (result, values) => {
      setPendingCorrection({ id: result.job_id, name: values.item.name, startedAt: Date.now() })
      setUnitCorrectionOpen(false)
      setUnitCorrectionConfirmed(false)
      toast.success("Unit correction queued", { description: `${values.item.name} · job ${result.job_id}` })
    },
    onError: (error) => toast.error("Unit correction was not queued", { description: errorMessage(error) }),
  })

  const correctionJob = useQuery({
    queryKey: ["workpiece-unit-correction-job", pendingCorrection?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingCorrection!.id}`),
    enabled: Boolean(pendingCorrection),
    refetchInterval: (queryState) => terminalJobStates.has(queryState.state.data?.job.status ?? "") ? false : 600,
  })

  useEffect(() => {
    const job = correctionJob.data?.job
    if (!pendingCorrection || !job || !terminalJobStates.has(job.status)) return
    if (job.status === "succeeded") {
      const seconds = Math.max(1, Math.round((Date.now() - pendingCorrection.startedAt) / 1_000))
      toast.success("Workpiece units corrected", { description: `${pendingCorrection.name} · ${seconds} s` })
      void client.invalidateQueries({ queryKey: ["workpiece-catalog"] })
      void client.invalidateQueries({ queryKey: ["pose-template-orientations"] })
      void client.invalidateQueries({ queryKey: ["pose-template-orientation-thumbnail"] })
    } else {
      toast.error("Unit correction did not complete", { description: jobFailureDetail(job) })
    }
    queueMicrotask(() => setPendingCorrection(null))
  }, [client, correctionJob.data?.job, pendingCorrection])

  const regeneratePreview = useMutation({
    mutationFn: (item: Workpiece) => api<{ job_id: string }>(
      `/pose-templates/workpieces/${item.catalog_uuid}/orientations`,
      { method: "POST", body: "{}" },
    ),
    onSuccess: (result, item) => {
      setPendingPreview({ id: result.job_id, catalogUuid: item.catalog_uuid, name: item.name, startedAt: Date.now() })
      toast.success("Recognition preview refresh queued", { description: `${item.name} · job ${result.job_id}` })
    },
    onError: (error) => toast.error("Recognition preview was not queued", { description: errorMessage(error) }),
  })

  const previewJob = useQuery({
    queryKey: ["workpiece-preview-job", pendingPreview?.id],
    queryFn: () => api<{ job: Job }>(`/jobs/${pendingPreview!.id}`),
    enabled: Boolean(pendingPreview),
    refetchInterval: (queryState) => terminalJobStates.has(queryState.state.data?.job.status ?? "") ? false : 600,
  })

  useEffect(() => {
    const job = previewJob.data?.job
    if (!pendingPreview || !job || !terminalJobStates.has(job.status)) return
    if (job.status === "succeeded") {
      const seconds = Math.max(1, Math.round((Date.now() - pendingPreview.startedAt) / 1_000))
      toast.success("Recognition preview refreshed", { description: `${pendingPreview.name} · ${seconds} s` })
      void client.invalidateQueries({ queryKey: ["pose-template-orientations"] })
      void client.invalidateQueries({ queryKey: ["pose-template-orientation-thumbnail"] })
    } else {
      toast.error("Recognition preview did not complete", { description: jobFailureDetail(job) })
    }
    queueMicrotask(() => setPendingPreview(null))
  }, [client, pendingPreview, previewJob.data?.job])

  const refresh = () => {
    void status.refetch()
    void catalogue.refetch()
    void client.invalidateQueries({ queryKey: ["pose-template-orientations"] })
    void client.invalidateQueries({ queryKey: ["pose-template-orientation-thumbnail"] })
  }
  const openUpload = () => {
    setUploadValidationAttempted(false)
    setUploadOpen(true)
  }
  const openEditor = (item: Workpiece) => {
    setEditDraft(metadataFor(item))
    setEditValidationAttempted(false)
    setEditOpen(true)
  }
  const submitUpload = (event: FormEvent) => {
    event.preventDefault()
    if (!uploadDraft.cad || !uploadDraft.name.trim()) return
    setUploadValidationAttempted(true)
    if (uploadAttributeValidation.message) {
      toast.error("Fix the custom attributes", {
        id: UPLOAD_ATTRIBUTE_VALIDATION_TOAST,
        description: uploadAttributeValidation.message,
      })
      return
    }
    upload.mutate(uploadDraft)
  }
  const submitEdit = (event: FormEvent) => {
    event.preventDefault()
    if (!selected || !editDraft.name.trim()) return
    setEditValidationAttempted(true)
    if (editAttributeValidation.message) {
      toast.error("Fix the custom attributes", {
        id: EDIT_ATTRIBUTE_VALIDATION_TOAST,
        description: editAttributeValidation.message,
      })
      return
    }
    updateMetadata.mutate({ id: selected.catalog_uuid, draft: editDraft })
  }
  const filtersActive = Boolean(search || tagFilter !== ALL_FILTER || groupFilter !== ALL_FILTER || stateFilter !== "active")
  const clearFilters = () => { setSearch(""); setTagFilter(ALL_FILTER); setGroupFilter(ALL_FILTER); setStateFilter("active") }
  const counts = status.data?.counts ?? {
    active: objects.filter((item) => item.state === "active").length,
    archived: objects.filter((item) => item.state === "archived").length,
    total: objects.length,
  }
  const serviceAvailable = status.data?.available !== false
  const unitCorrectionAvailable = status.data?.unit_corrections?.supported ?? serviceAvailable
  const unitCorrectionDisabledReason = selected
    ? !unitCorrectionAvailable
      ? "PoseTemplateCreator is required for unit correction."
      : selected.state === "active"
        ? "Archive this workpiece to enable unit correction."
        : pendingCorrection
          ? "Wait for the current unit-correction job to finish."
          : null
    : null

  return <div className="space-y-6" data-testid="workpieces-page">
    <PageHeader
      eyebrow="Asset library"
      title="Workpiece Catalogue"
      description="Manage the canonical CAD assets and searchable metadata used to identify physical test objects across PoseTestBot."
      actions={<>
        <Button variant="outline" onClick={refresh} aria-label="Refresh workpiece catalogue"><RefreshCw className={status.isFetching || catalogue.isFetching ? "animate-spin" : ""} />Refresh</Button>
        <Button asChild variant="outline" data-testid="workpiece-catalog-export"><a href="/workpieces/catalog/export" download><Download />Export JSON</a></Button>
        <Button variant="outline" onClick={() => setImportOpen(true)} data-testid="workpiece-catalog-import"><FileJson />Import JSON</Button>
        <HelpTip label="catalogue JSON portability">JSON import and export move metadata only. Back up or copy the managed object_catalog asset tree separately to move CAD, canonical PLY, and texture bytes.</HelpTip>
        <Button onClick={openUpload} disabled={!serviceAvailable || Boolean(pendingUpload)} aria-describedby={!serviceAvailable ? "workpiece-service-disabled-reason" : undefined} data-testid="workpiece-upload-button"><FileUp />Add workpiece</Button>
      </>}
    />
    <ProcessHandoff
      title="Active workpieces become pose-template choices"
      description="This is a global reusable library: changes here do not mutate the active run. Manage stable object identity and canonical geometry here, then choose physical resting orientations and arrange active workpieces into an immutable printable pose template."
      to="/pose-templates"
      action="Arrange pose template"
    />

    <Card className={serviceAvailable ? "border-success/30" : "border-destructive/40"} data-testid="workpiece-catalog-status">
      <CardContent className="flex items-center justify-between gap-5 py-3.5">
        <div className="flex items-center gap-3">
          <div className={`grid size-9 place-items-center rounded-lg ${serviceAvailable ? "bg-success/10 text-success" : "bg-destructive/10 text-destructive"}`}><PackageOpen className="size-5" /></div>
          <div><div className="flex items-center gap-2 text-sm font-semibold">CAD inspection service <StatusBadge status={serviceAvailable ? "ready" : status.data?.status} tone={serviceAvailable ? "success" : status.isError ? "destructive" : "warning"} /></div><p id={!serviceAvailable ? "workpiece-service-disabled-reason" : undefined} className="mt-0.5 text-[11px] text-muted-foreground">{status.isError ? errorMessage(status.error) : status.data?.reason ?? "Canonical mesh inspection is available."}</p>{status.data?.catalog_root && <p className="mt-0.5 max-w-3xl truncate font-mono text-[9px] text-muted-foreground" title={status.data.catalog_root}>Persistent JSON and assets · {status.data.catalog_root}</p>}</div>
        </div>
        <div className="text-right text-[10px] leading-5 text-muted-foreground">{status.data?.formats?.map((format) => format.toUpperCase()).join(" · ") || "PLY · STL · OBJ"}<br />{formatBytes(status.data?.limits?.cad_bytes)} per CAD file</div>
      </CardContent>
    </Card>

    {pendingUpload && <BackgroundJobProgress testId="workpiece-upload-progress" title={`Inspecting ${pendingUpload.filename}`} description="Canonical conversion and mesh inspection run in the background. This catalogue refreshes automatically." jobId={pendingUpload.id} status={uploadJob.data?.job.status ?? "queued"} />}

    {pendingCorrection && <BackgroundJobProgress testId="workpiece-unit-correction-progress" title={`Correcting units for ${pendingCorrection.name}`} description="A new canonical geometry revision is being derived. The retained upload and published template snapshots stay unchanged." jobId={pendingCorrection.id} status={correctionJob.data?.job.status ?? "queued"} />}

    {pendingPreview && <BackgroundJobProgress testId="workpiece-preview-progress" title={`Refreshing recognition preview for ${pendingPreview.name}`} description="Stable-orientation analysis and the bounded catalogue-card mesh are being regenerated." jobId={pendingPreview.id} status={previewJob.data?.job.status ?? "queued"} />}

    <div className="grid grid-cols-3 gap-3" aria-label="Workpiece catalogue summary">
      <Card><CardContent className="flex items-center justify-between py-4"><div><div className="metric-number">{counts.total}</div><div className="mt-1 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">Total objects</div></div><Box className="size-6 text-muted-foreground" /></CardContent></Card>
      <Card><CardContent className="flex items-center justify-between py-4"><div><div className="metric-number text-success">{counts.active}</div><div className="mt-1 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">Active</div></div><Tag className="size-6 text-success" /></CardContent></Card>
      <Card><CardContent className="flex items-center justify-between py-4"><div><div className="metric-number text-muted-foreground">{counts.archived}</div><div className="mt-1 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">Archived</div></div><Archive className="size-6 text-muted-foreground" /></CardContent></Card>
    </div>

    <Card>
      <CardContent className="grid grid-cols-[minmax(220px,1.5fr)_minmax(130px,.75fr)_minmax(130px,.75fr)_minmax(125px,.6fr)_auto] items-end gap-3 py-4">
        <Field label="Search catalogue" htmlFor="workpiece-search"><div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input id="workpiece-search" aria-label="Search workpieces" data-testid="workpiece-search" className="pl-9" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Name, alias, tag, UUID…" /></div></Field>
        <Field label="Tag"><Select value={tagFilter} onValueChange={setTagFilter}><SelectTrigger aria-label="Filter by tag" data-testid="workpiece-tag-filter"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={ALL_FILTER}>All tags</SelectItem>{allTags.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent></Select></Field>
        <Field label="Group"><Select value={groupFilter} onValueChange={setGroupFilter}><SelectTrigger aria-label="Filter by group" data-testid="workpiece-group-filter"><SelectValue /></SelectTrigger><SelectContent><SelectItem value={ALL_FILTER}>All groups</SelectItem>{allGroups.map((value) => <SelectItem value={value} key={value}>{value}</SelectItem>)}</SelectContent></Select></Field>
        <Field label="State"><Select value={stateFilter} onValueChange={setStateFilter}><SelectTrigger aria-label="Filter by state" data-testid="workpiece-state-filter"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All states</SelectItem><SelectItem value="active">Active</SelectItem><SelectItem value="archived">Archived</SelectItem></SelectContent></Select></Field>
        <Button variant="ghost" onClick={clearFilters} disabled={!filtersActive}><X />Clear</Button>
      </CardContent>
    </Card>

    {catalogue.isPending ? <Card><CardContent className="grid min-h-80 place-items-center text-sm text-muted-foreground"><div><LoaderCircle className="mx-auto mb-2 size-5 animate-spin" />Loading workpieces…</div></CardContent></Card>
      : catalogue.isError ? <Card className="border-destructive/40"><CardHeader><CardTitle>Catalogue unavailable</CardTitle><CardDescription>{errorMessage(catalogue.error)}</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={() => catalogue.refetch()}><RefreshCw />Try again</Button></CardContent></Card>
        : objects.length === 0 ? <EmptyState icon={Box} title="No workpieces yet" description="Upload a PLY, STL, or OBJ file to build the persistent catalogue." action={<Button onClick={openUpload} disabled={!serviceAvailable} aria-describedby={!serviceAvailable ? "workpiece-service-disabled-reason" : undefined}><FileUp />Add first workpiece</Button>} />
          : <div className="grid items-start gap-5 xl:grid-cols-[minmax(285px,.72fr)_minmax(0,1.65fr)]">
            <Card data-testid="workpiece-catalog-list">
              <CardHeader className="border-b"><div className="flex items-center justify-between gap-3"><div><CardTitle>Objects</CardTitle><CardDescription className="mt-1">{filtered.length} of {objects.length} visible</CardDescription></div><Layers3 className="size-5 text-muted-foreground" /></div></CardHeader>
              <CardContent className="max-h-[74rem] space-y-2 overflow-y-auto p-3">
                {filtered.map((item) => <WorkpieceCard item={item} selected={item.catalog_uuid === effectiveSelectedId} onSelect={() => setSelectedId(item.catalog_uuid)} key={item.catalog_uuid} />)}
                {filtered.length === 0 && <div className="py-12 text-center"><Search className="mx-auto mb-2 size-5 text-muted-foreground" /><div className="text-sm font-semibold">No matches</div><p className="mt-1 text-xs text-muted-foreground">Adjust or clear the catalogue filters.</p><Button className="mt-3" size="sm" variant="outline" onClick={clearFilters}>Clear filters</Button></div>}
              </CardContent>
            </Card>

            {selected ? <Card data-testid="workpiece-selected-object">
              <CardHeader className="border-b">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0"><div className="mb-2 flex items-center gap-2"><StatusBadge status={selected.state} tone={selected.state === "active" ? "informational" : "neutral"} /><span className="font-mono text-[10px] text-muted-foreground">obj_{String(selected.obj_id).padStart(6, "0")}</span></div><CardTitle className="text-xl leading-tight">{selected.name}</CardTitle>{selected.alias && <CardDescription className="mt-1 text-sm">{selected.alias}</CardDescription>}</div>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button variant="outline" onClick={() => openEditor(selected)}><Pencil />Edit metadata</Button>
                    <Button
                      variant="outline"
                      disabled={selected.state !== "archived" || !unitCorrectionAvailable || Boolean(pendingCorrection)}
                      aria-describedby={unitCorrectionDisabledReason ? "unit-correction-disabled-reason" : undefined}
                      title={!unitCorrectionAvailable ? "PoseTemplateCreator is required for unit correction" : selected.state === "active" ? "Archive this workpiece before correcting its model units" : "Create a corrected canonical geometry revision"}
                      onClick={() => { setUnitConversion("meter_to_millimeter"); setUnitCorrectionOperator(""); setUnitCorrectionConfirmed(false); setUnitCorrectionOpen(true) }}
                    ><Scaling />Correct model units</Button>
                    <Button variant="outline" onClick={() => setConfirmation({ action: selected.state === "active" ? "archive" : "restore", item: selected })}>{selected.state === "active" ? <Archive /> : <RotateCcw />}{selected.state === "active" ? "Archive" : "Restore"}</Button>
                    <Button variant="ghost" className="text-destructive hover:text-destructive" aria-label={`Delete ${selected.name}`} title="Permanently delete this workpiece" onClick={() => setConfirmation({ action: "delete", item: selected })}><Trash2 />Delete</Button>
                  </div>
                </div>
                {unitCorrectionDisabledReason && <p id="unit-correction-disabled-reason" className="mt-3 text-right text-xs text-muted-foreground">{unitCorrectionDisabledReason}</p>}
              </CardHeader>
              <CardContent className="space-y-6 pt-5">
                <section aria-labelledby="workpiece-preview-heading"><div className="mb-3 flex items-end justify-between gap-3"><div><h3 id="workpiece-preview-heading" className="text-sm font-semibold">3D preview</h3><p className="mt-0.5 text-[11px] text-muted-foreground">The selected detail loads the full canonical PLY so holes, recesses, ports, and other identifying features remain visible. Drag to rotate and scroll to zoom.</p></div><div className="flex shrink-0 items-center gap-2"><Badge variant="outline">{selected.source_format.toUpperCase()}</Badge><Button size="sm" variant="outline" disabled={!serviceAvailable || regeneratePreview.isPending || Boolean(pendingPreview)} onClick={() => regeneratePreview.mutate(selected)} title="Queue stable-orientation analysis and rebuild the bounded catalogue-card mesh"><RefreshCw className={pendingPreview?.catalogUuid === selected.catalog_uuid ? "animate-spin" : undefined} />Refresh card preview</Button></div></div><WorkpiecePreviews key={`${selected.catalog_uuid}:${selected.canonical_ply_sha256 ?? selected.geometry_revision ?? 1}`} object={selected} /></section>

                {selected.description && <div className="rounded-lg border bg-muted/25 p-4 text-sm leading-relaxed">{selected.description}</div>}

                <div className="grid grid-cols-2 gap-5">
                  <section className="space-y-2"><div className="flex items-center gap-2 text-xs font-semibold"><Tag className="size-3.5" />Tags</div><Tokens values={selected.tags ?? []} /></section>
                  <section className="space-y-2"><div className="flex items-center gap-2 text-xs font-semibold"><Layers3 className="size-3.5" />Groups</div><Tokens values={selected.groups ?? []} variant="outline" /></section>
                </div>

                <div className="grid grid-cols-4 overflow-hidden rounded-lg border text-xs">
                  <div className="border-r p-3" data-testid="workpiece-dimensions"><div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground"><span>Dimensions</span><HelpTip label="model dimensions" className="size-4 normal-case"><span className="font-semibold">Wrong model scale?</span> Archive this workpiece first, then use <span className="font-semibold">Correct model units</span>. Existing immutable templates keep their original geometry snapshot.</HelpTip></div><div className="mt-1 font-mono text-[11px]">{formatBounds(selected.extraction.bounds_mm)}</div></div>
                  <div className="border-r p-3"><div className="text-[10px] uppercase tracking-wide text-muted-foreground">Vertices</div><div className="mt-1 font-mono text-[11px]">{selected.extraction.vertices.toLocaleString()}</div></div>
                  <div className="border-r p-3"><div className="text-[10px] uppercase tracking-wide text-muted-foreground">Faces</div><div className="mt-1 font-mono text-[11px]">{selected.extraction.faces.toLocaleString()}</div></div>
                  <div className="p-3"><div className="text-[10px] uppercase tracking-wide text-muted-foreground">Mesh</div><div className="mt-1 font-mono text-[11px]">{selected.extraction.watertight ? "Watertight" : "Open"}</div></div>
                </div>

                <div className="grid grid-cols-2 gap-5">
                  <section><h3 className="text-sm font-semibold">Custom attributes</h3>{Object.keys(selected.attributes ?? {}).length ? <dl className="mt-3 overflow-hidden rounded-lg border text-xs">{Object.entries(selected.attributes ?? {}).map(([key, value], index) => <div className={`grid grid-cols-[minmax(100px,.7fr)_minmax(0,1.3fr)] gap-3 px-3 py-2 ${index ? "border-t" : ""}`} key={key}><dt className="text-muted-foreground">{key}</dt><dd className="break-words font-medium">{value}</dd></div>)}</dl> : <p className="mt-2 text-xs text-muted-foreground">No custom attributes.</p>}</section>
                  <section><h3 className="text-sm font-semibold">Pose-template usage</h3><div className="mt-3 rounded-lg border p-3"><div className="text-2xl font-semibold tracking-tight">{selected.usage?.template_count ?? 0}</div><div className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">Referencing templates</div>{selected.usage?.templates?.length ? <div className="mt-3 space-y-1 border-t pt-2 text-[11px]">{selected.usage.templates.slice(0, 4).map((item, index) => <div className="truncate" key={`${templateLabel(item, index)}-${index}`}>{templateLabel(item, index)}</div>)}</div> : null}</div></section>
                </div>

                <section className="rounded-lg border bg-muted/20 p-4"><div className="flex items-start justify-between gap-4"><div className="min-w-0"><div className="text-xs font-semibold">Stored assets</div><div className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={selected.source_filename}>{selected.source_filename}</div><div className="mt-1 truncate font-mono text-[9px] text-muted-foreground" title={selected.catalog_uuid}>{selected.catalog_uuid}</div><div className="mt-2 text-[10px] text-muted-foreground">Geometry revision <span className="font-mono text-foreground">r{selected.geometry_revision ?? 1}</span> · retained source → mm <span className="font-mono text-foreground">×{(selected.source_to_mm_scale ?? 1).toLocaleString(undefined, { maximumSignificantDigits: 8 })}</span></div><div className="mt-0.5 text-[10px] text-muted-foreground">{geometryRevisionSummary(selected)}</div></div><div className="flex flex-wrap justify-end gap-2"><Button size="sm" variant="outline" asChild><a href={`/workpieces/catalog/${selected.catalog_uuid}/assets/canonical_ply`} download><Download />Canonical PLY</a></Button><Button size="sm" variant="outline" asChild><a href={`/workpieces/catalog/${selected.catalog_uuid}/assets/source`} download><Download />Source</a></Button>{selected.assets.texture && <Button size="sm" variant="outline" asChild><a href={`/workpieces/catalog/${selected.catalog_uuid}/assets/texture`} download><Download />Texture</a></Button>}</div></div></section>
              </CardContent>
            </Card> : <EmptyState icon={Box} title="Select a workpiece" description="Choose an object from the catalogue to inspect its geometry, metadata, and assets." />}
          </div>}

    <Dialog open={uploadOpen} onOpenChange={(open) => { if (!upload.isPending) setUploadOpen(open) }}>
      <DialogContent className="max-h-[92vh] max-w-2xl overflow-y-auto" data-testid="workpiece-upload-dialog">
        <form onSubmit={submitUpload} className="space-y-5">
          <DialogHeader><DialogTitle>Add workpiece</DialogTitle><DialogDescription>Upload the original CAD asset and optional PNG texture. PoseTestBot retains the source, creates a canonical PLY, and stores portable metadata in JSON.</DialogDescription></DialogHeader>
          <div className="grid grid-cols-2 gap-3 rounded-lg border bg-muted/20 p-3">
            <Field label="CAD file" htmlFor="workpiece-upload-cad" hint="PLY, STL, or OBJ within the configured size limit."><Input id="workpiece-upload-cad" data-testid="workpiece-cad-input" type="file" accept=".ply,.stl,.obj" required onChange={(event) => { const cad = event.target.files?.[0] ?? null; setUploadDraft((current) => ({ ...current, cad, name: current.name || cad?.name.replace(/\.[^.]+$/, "") || "" })) }} /></Field>
            <Field label="Optional texture" htmlFor="workpiece-upload-texture" hint="PNG texture retained beside the source asset."><Input id="workpiece-upload-texture" data-testid="workpiece-texture-input" type="file" accept="image/png,.png" onChange={(event) => setUploadDraft((current) => ({ ...current, texture: event.target.files?.[0] ?? null }))} /></Field>
          </div>
          <MetadataFields draft={uploadDraft} setDraft={(draft) => setUploadDraft((current) => ({ ...current, ...draft }))} prefix="workpiece-upload" attributeValidation={uploadValidationAttempted ? uploadAttributeValidation : undefined} />
          <DialogFooter><Button variant="outline" onClick={() => setUploadOpen(false)} disabled={upload.isPending}>Cancel</Button><Button type="submit" disabled={!uploadDraft.cad || !uploadDraft.name.trim() || upload.isPending}>{upload.isPending ? <LoaderCircle className="animate-spin" /> : <FileUp />}Upload and inspect</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>

    <Dialog open={editOpen} onOpenChange={(open) => { if (!updateMetadata.isPending) setEditOpen(open) }}>
      <DialogContent className="max-h-[92vh] max-w-2xl overflow-y-auto" data-testid="workpiece-metadata-dialog">
        <form onSubmit={submitEdit} className="space-y-5">
          <DialogHeader><DialogTitle>Edit {selected?.name ?? "workpiece"}</DialogTitle><DialogDescription>Names, labels, and custom attributes remain exportable JSON metadata. Geometry and stable object identity are unchanged.</DialogDescription></DialogHeader>
          <MetadataFields draft={editDraft} setDraft={setEditDraft} prefix="workpiece-edit" attributeValidation={editValidationAttempted ? editAttributeValidation : undefined} />
          <DialogFooter><Button variant="outline" onClick={() => setEditOpen(false)} disabled={updateMetadata.isPending}>Cancel</Button><Button type="submit" disabled={!editDraft.name.trim() || updateMetadata.isPending}>{updateMetadata.isPending ? <LoaderCircle className="animate-spin" /> : <Pencil />}Save metadata</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>

    <Dialog open={unitCorrectionOpen} onOpenChange={(open) => { if (!correctUnits.isPending) setUnitCorrectionOpen(open) }}>
      <DialogContent className="max-w-2xl" data-testid="workpiece-unit-correction-dialog">
        <DialogHeader><DialogTitle>Correct model units for {selected?.name ?? "workpiece"}</DialogTitle><DialogDescription>This creates a new canonical geometry revision from the retained source. It does not rewrite published pose-template or run snapshots.</DialogDescription></DialogHeader>
        {selected && <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3" role="radiogroup" aria-label="Unit correction">
            {([
              ["meter_to_millimeter", "File was authored in metres — enlarge ×1000", "Use when a metre-authored CAD file was interpreted as millimetres."],
              ["millimeter_to_meter", "Model is 1000× too large — shrink ÷1000", "Use when the displayed millimetre model is one thousand times too large."],
            ] as const).map(([value, label, hint]) => <button
              type="button"
              role="radio"
              aria-checked={unitConversion === value}
              className={`rounded-lg border p-4 text-left transition-colors ${unitConversion === value ? "border-primary bg-primary/5 ring-1 ring-primary/35" : "hover:bg-muted/40"}`}
              onClick={() => { setUnitConversion(value); setUnitCorrectionConfirmed(false) }}
              key={value}
            ><span className="block text-sm font-semibold">{label}</span><span className="mt-1 block text-[11px] leading-relaxed text-muted-foreground">{hint}</span></button>)}
          </div>
          <div className="grid grid-cols-2 overflow-hidden rounded-lg border text-xs">
            <div className="border-r p-4"><div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Current dimensions</div><div className="mt-2 font-mono">{formatScaledBounds(selected.extraction.bounds_mm, 1)}</div></div>
            <div className="p-4"><div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">After correction</div><div className="mt-2 font-mono text-primary-strong">{formatScaledBounds(selected.extraction.bounds_mm, unitConversion === "meter_to_millimeter" ? 1000 : 0.001)}</div></div>
          </div>
          <Field label="Operator" htmlFor="unit-correction-operator" hint="Recorded with the geometry revision for auditability."><Input id="unit-correction-operator" aria-label="Unit correction operator" value={unitCorrectionOperator} onChange={(event) => { setUnitCorrectionOperator(event.target.value); setUnitCorrectionConfirmed(false) }} placeholder="Name or operator ID" /></Field>
          <Label className="flex items-start gap-3 rounded-lg border border-warning/35 bg-warning/5 p-4"><Checkbox aria-label="Confirm unit correction" checked={unitCorrectionConfirmed} onCheckedChange={(value) => setUnitCorrectionConfirmed(value === true)} /><span><span className="block font-semibold">I checked the before and after dimensions</span><span className="mt-1 block text-xs font-normal text-muted-foreground">The catalogue UUID and BOP object ID stay stable, while the canonical mesh hash and geometry revision change.</span></span></Label>
        </div>}
        <DialogFooter><Button variant="outline" onClick={() => setUnitCorrectionOpen(false)} disabled={correctUnits.isPending}>Cancel</Button><Button onClick={() => selected && correctUnits.mutate({ item: selected, conversion: unitConversion, operator: unitCorrectionOperator })} disabled={!unitCorrectionAvailable || !selected || selected.state !== "archived" || !unitCorrectionOperator.trim() || !unitCorrectionConfirmed || correctUnits.isPending}>{correctUnits.isPending ? <LoaderCircle className="animate-spin" /> : <Scaling />}Queue unit correction</Button></DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={confirmation !== null} onOpenChange={(open) => { if (!open && !catalogueAction.isPending) setConfirmation(null) }}>
      <DialogContent data-testid="workpiece-action-confirmation">
        <DialogHeader>
          <DialogTitle>{confirmation?.action === "delete" ? "Permanently delete" : confirmation?.action === "archive" ? "Archive" : "Restore"} {confirmation?.item.name}?</DialogTitle>
          <DialogDescription>{confirmation?.action === "delete"
            ? `This permanently removes the retained source, canonical mesh, texture, and catalogue record. ${confirmation.item.usage?.template_count ? `${confirmation.item.usage.template_count} pose template(s) currently reference it; the server may block deletion to preserve those bundles.` : "This action cannot be undone."}`
            : confirmation?.action === "archive"
              ? "The workpiece remains stored and exportable, but is hidden from active-object workflows and future template selection."
              : "The workpiece returns to active-object workflows and future template selection."}</DialogDescription>
        </DialogHeader>
        <DialogFooter><Button variant="outline" onClick={() => setConfirmation(null)} disabled={catalogueAction.isPending}>Cancel</Button><Button variant={confirmation?.action === "delete" ? "destructive" : "default"} onClick={() => confirmation && catalogueAction.mutate(confirmation)} disabled={catalogueAction.isPending}>{catalogueAction.isPending ? <LoaderCircle className="animate-spin" /> : confirmation?.action === "delete" ? <Trash2 /> : confirmation?.action === "archive" ? <Archive /> : <RotateCcw />}Confirm {confirmation?.action}</Button></DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog open={importOpen} onOpenChange={(open) => { if (!importCatalogue.isPending) setImportOpen(open) }}>
      <DialogContent data-testid="workpiece-import-dialog">
        <DialogHeader><DialogTitle>Import catalogue metadata</DialogTitle><DialogDescription>Import a previously exported catalogue JSON. Records without matching persistent assets are skipped; CAD files are never fabricated or overwritten by metadata import.</DialogDescription></DialogHeader>
        <Field label="Catalogue JSON" htmlFor="workpiece-import-file"><Input id="workpiece-import-file" aria-label="Catalogue JSON file" data-testid="workpiece-import-input" type="file" accept="application/json,.json" onChange={(event) => setImportFile(event.target.files?.[0] ?? null)} /></Field>
        <DialogFooter><Button variant="outline" onClick={() => setImportOpen(false)} disabled={importCatalogue.isPending}>Cancel</Button><Button onClick={() => importFile && importCatalogue.mutate(importFile)} disabled={!importFile || importCatalogue.isPending}>{importCatalogue.isPending ? <LoaderCircle className="animate-spin" /> : <FileJson />}Import metadata</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
}
