import { useEffect, useMemo, useState } from "react"
import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Controller, useForm, useWatch } from "react-hook-form"
import { Camera, CheckCircle2, Gauge, LoaderCircle, RefreshCw, Save, TriangleAlert } from "lucide-react"
import { toast } from "sonner"
import { z } from "zod"
import { POSE_TEMPLATE_BASE_SUNRISE_PATH } from "@/components/calibration-arrangement"
import { HelpTip } from "@/components/help-tip"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, ApiError, errorMessage, query } from "@/lib/api"
import type { CaptureSynchronization, RunConfig, SensorStatus } from "@/lib/contracts"
import { useOperator } from "@/providers/operator-provider"

const DEFAULT_CAPTURE_SPEED_M_S = 0.01
const MAX_CALIBRATION_CAPTURE_SPEED_M_S = 0.03
const MAX_DATASET_CAPTURE_SPEED_M_S = 1

const setupSchema = (maximumCaptureSpeedMps: number) => z.object({
  run_name: z.string().optional(),
  resolution: z.string().min(1),
  fps: z.number().int().min(1).max(60),
  velocity: z.number().min(0.01).max(maximumCaptureSpeedMps),
  annotation_mode: z.enum(["none", "pose", "pose_and_masks"]),
})

type SetupValues = z.infer<ReturnType<typeof setupSchema>>
type RunSensor = RunConfig["capture"]["sensors"][number]
type MountingMode = "eye_in_hand" | "static"
const UNCONFIGURED_MOUNTING = "unconfigured"
const isConfiguredMounting = (value: unknown): value is MountingMode => value === "eye_in_hand" || value === "static"
const mountingLabel = (value: unknown) => value === "static" ? "Static" : value === "eye_in_hand" ? "Robot-mounted" : "Mounting not configured"
type RunConfigResponse = {
  config: RunConfig
  preflight?: unknown
  camera_contract?: { mutable: boolean; blockers: string[] }
  [key: string]: unknown
}
export type WorkflowIntent = "calibration" | "dataset"

type CalibrationIssue = { code: string; message: string; sensor_key?: string }

type CalibrationProfileSummary = {
  profile_id: string
  sensor_type: string
  sensor_id: string
  mounting_mode: string
  status?: string
  resolution?: [number, number]
  intrinsic_profile_id?: string | null
  calibrated_at?: string | null
  method?: string | null
  quality?: { num_observations?: number; num_inliers?: number; mean_reprojection_error_px?: number | null }
}

type CalibrationSensorMapping = {
  sensor_key: string
  profile_id: string
  intrinsic_profile_id: string
  mounting_mode: string
  resolution?: string
  orientation?: string
}

type CalibrationSource = {
  source_run_root: string
  source_run_name: string
  bundle_sha256: string | null
  valid: boolean
  compatible: boolean
  issues: CalibrationIssue[]
  calibration_profiles: {
    sha256: string
    valid_profile_count: number
    profiles: CalibrationProfileSummary[]
  }
  intrinsic_calibration_profiles: {
    sha256: string
    profile_count: number
    profiles?: Array<{ profile_id: string; sensor_id: string; resolution: [number, number]; orientation: string }>
  }
  sensor_profile_mapping?: CalibrationSensorMapping[]
  sensor_profiles?: Record<string, string>
}

type CalibrationSelectionArtifact = {
  schema_version: string
  selected_at: string
  source: {
    kind?: "composite"
    run_root?: string
    run_name: string
    bundle_sha256: string
  }
  sources?: Array<{
    run_root: string
    run_name: string
    bundle_sha256: string
    selected_sensor_keys: string[]
  }>
  snapshot: {
    calibration_profiles: { relative_path: string; sha256: string }
    intrinsic_calibration_profiles: { relative_path: string; sha256: string }
  }
  sensor_profiles?: Record<string, string>
}

type CalibrationLibrarySelected =
  | (CalibrationSelectionArtifact & { valid: true; issues: CalibrationIssue[] })
  | { valid: false; issues: CalibrationIssue[] }

type CalibrationLibraryResponse = {
  selected: CalibrationLibrarySelected | null
  replacement_blockers?: string[]
  calibrations: CalibrationSource[]
}

type CalibrationSelectionResponse = {
  calibration_profiles: string
  intrinsic_calibration_profiles: string
  sensor_profiles?: Record<string, string>
  sensor_profile_mapping: Array<{ sensor_key: string; profile_id: string }>
  selection: CalibrationSelectionArtifact
}

const sensorKey = (sensor: { sensor_type: string; device_id: string }) => `${sensor.sensor_type}:${sensor.device_id}`
const CALIBRATION_RESOLUTION_SIZES: Record<string, [number, number]> = {
  "720p": [1280, 720],
  "360p": [672, 376],
}

function calibrationMappingFor(
  source: CalibrationSource,
  sensor: RunSensor,
  resolution: string,
): CalibrationSensorMapping | null {
  const key = sensorKey(sensor)
  const orientation = sensor.inverted ? "inverted" : "normal"
  const listed = source.sensor_profile_mapping?.find((mapping) => mapping.sensor_key === key)
  if (listed
    && listed.mounting_mode === sensor.mounting_mode
    && (!listed.resolution || listed.resolution === resolution)
    && (!listed.orientation || listed.orientation === orientation)) return listed

  const imageSize = CALIBRATION_RESOLUTION_SIZES[resolution]
  if (!imageSize) return null
  const profiles = source.calibration_profiles.profiles.filter((profile) => profile.status === "valid"
    && profile.sensor_type === sensor.sensor_type
    && profile.sensor_id === sensor.device_id
    && profile.mounting_mode === sensor.mounting_mode
    && profile.resolution?.[0] === imageSize[0]
    && profile.resolution?.[1] === imageSize[1])
  if (profiles.length !== 1) return null
  const profile = profiles[0]
  const intrinsicProfiles = source.intrinsic_calibration_profiles.profiles?.filter((intrinsic) => intrinsic.sensor_id === sensor.device_id
    && intrinsic.resolution[0] === imageSize[0]
    && intrinsic.resolution[1] === imageSize[1]
    && intrinsic.orientation === orientation
    && (!profile.intrinsic_profile_id || intrinsic.profile_id === profile.intrinsic_profile_id)) ?? []
  if (intrinsicProfiles.length !== 1) return null
  return {
    sensor_key: key,
    profile_id: profile.profile_id,
    intrinsic_profile_id: intrinsicProfiles[0].profile_id,
    mounting_mode: sensor.mounting_mode,
    resolution,
    orientation,
  }
}
const TIMESTAMP_SYNCHRONIZATION: CaptureSynchronization = {
  schema_version: "capture_synchronization.v1",
  mode: "timestamp_aligned",
}

function sharedResolutions(status: SensorStatus | undefined, enabledSensors: RunSensor[]) {
  let shared: string[] | null = null
  for (const sensor of enabledSensors) {
    const family = status?.families.find((item) => item.sensor_type === sensor.sensor_type)
    const supported = Array.isArray(family?.supported_resolutions)
      ? family.supported_resolutions.filter((item): item is string => typeof item === "string")
      : []
    if (!supported.length) continue
    shared = shared === null ? supported : shared.filter((item) => supported.includes(item))
  }
  return shared?.length ? shared : ["720p"]
}

function defaultSetupValues(): SetupValues {
  return {
    run_name: "",
    resolution: "720p",
    fps: 6,
    velocity: DEFAULT_CAPTURE_SPEED_M_S,
    annotation_mode: "none",
  }
}

function setupValuesFromConfig(
  config: RunConfig,
  maximumCaptureSpeedMps: number,
): SetupValues {
  return {
    run_name: config.run_name,
    resolution: config.capture.resolution,
    fps: config.capture.fps,
    velocity: Math.min(config.capture.velocity_m_s, maximumCaptureSpeedMps),
    annotation_mode: config.bop.annotation_mode,
  }
}

export function RunSetup({ intent = "calibration" }: { intent?: WorkflowIntent }) {
  const { selectedRun } = useOperator()
  return <RunSetupForContext key={`${intent}:${selectedRun}`} intent={intent} selectedRun={selectedRun} />
}

function RunSetupForContext({ intent, selectedRun }: { intent: WorkflowIntent; selectedRun: string }) {
  const queryClient = useQueryClient()
  const maximumCaptureSpeedMps = intent === "dataset"
    ? MAX_DATASET_CAPTURE_SPEED_M_S
    : MAX_CALIBRATION_CAPTURE_SPEED_M_S
  const captureSpeedHelp = intent === "dataset"
    ? "The dataset workflow sends a structured robot_command.v1 request up to 1.00 m/s. Full capture uses an A1 joint PTP, and the commissioned Sunrise app independently caps A1 at 3°/s. These are ordinary software limits, not safety-rated limits."
    : "Calibration raster legs use a scaled Cartesian LIN speed and a structured robot_command.v1 request capped at 0.03 m/s. This is not a safety-rated limit."
  const captureSpeedHint = intent === "dataset"
    ? "Choose 0.01–1.00 m/s. Requests above 0.03 m/s require the commissioned structured-command app. Full capture is an A1 joint PTP; the commissioned app also caps A1 at 3°/s. Speed alone cannot guarantee sharp frames—exposure time and lighting still matter."
    : "Choose 0.01–0.03 m/s. Full capture is an A1 joint PTP; calibration motion remains in the conservative commissioned range. Speed alone cannot guarantee sharp frames—exposure time and lighting still matter."
  const sensors = useQuery({ queryKey: ["sensors", "status"], queryFn: () => api<SensorStatus>("/sensors/status"), staleTime: 10_000 })
  const existing = useQuery({ queryKey: ["run-config", selectedRun], queryFn: () => api<RunConfigResponse>(query("/run-config", { run_root: selectedRun })), retry: false })
  const calibrations = useQuery({
    queryKey: ["calibration-library", selectedRun],
    queryFn: () => api<CalibrationLibraryResponse>(query("/ui/calibrations", { run_root: selectedRun })),
    enabled: intent === "dataset",
    retry: false,
  })
  const calibrationSources = calibrations.data?.calibrations ?? []
  const form = useForm<SetupValues>({
    resolver: zodResolver(setupSchema(maximumCaptureSpeedMps)),
    defaultValues: existing.data?.config
      ? setupValuesFromConfig(existing.data.config, maximumCaptureSpeedMps)
      : defaultSetupValues(),
  })
  const [enabledOverrides, setEnabledOverrides] = useState<Record<string, Record<string, boolean>>>({})
  const [operatorAliasDrafts, setOperatorAliasDrafts] = useState<Record<string, string>>({})
  const [mountingModeDrafts, setMountingModeDrafts] = useState<Record<string, MountingMode>>({})
  const [orientationDrafts, setOrientationDrafts] = useState<Record<string, boolean>>({})
  const [calibrationAssignments, setCalibrationAssignments] = useState<{
    runRoot: string
    sourceBySensor: Record<string, string>
  } | null>(null)
  const [confirmReplacement, setConfirmReplacement] = useState(false)
  const setupLookupPending = existing.isPending
  const setupNotFound = existing.isError && existing.error instanceof ApiError && existing.error.status === 404
  const setupLookupFailed = existing.isError && !setupNotFound
  const setupControlsDisabled = setupLookupPending || setupLookupFailed
  const cameraContractLocked = existing.data?.camera_contract?.mutable === false
  const cameraContractBlockers = existing.data?.camera_contract?.blockers ?? []

  const detectedByKey = useMemo(() => new Map(
    (sensors.data?.families.flatMap((family) => family.devices) ?? []).map((device) => [sensorKey(device), device]),
  ), [sensors.data])
  const configuredSensors = useMemo<RunSensor[]>(() => {
    const configured = existing.data?.config.capture.sensors ?? []
    const rows = configured.map((sensor) => {
      const detected = detectedByKey.get(sensorKey(sensor))
      return {
        ...detected,
        ...sensor,
        operator_alias: sensor.operator_alias,
        enabled: sensor.enabled ?? true,
      } as RunSensor
    })
    if (!cameraContractLocked) {
      const configuredKeys = new Set(rows.map(sensorKey))
      for (const [key, detected] of detectedByKey) {
        if (configuredKeys.has(key)) continue
        rows.push({
          ...detected,
          display_name: detected.effective_display_name ?? detected.display_name ?? detected.device_id,
          operator_alias: detected.alias ?? null,
          mounting_mode: isConfiguredMounting(detected.mounting_mode) ? detected.mounting_mode : UNCONFIGURED_MOUNTING,
          enabled: false,
          inverted: Boolean(detected.inverted),
        } as RunSensor)
      }
    }
    return rows.map((sensor) => {
      const savedMountingMode = isConfiguredMounting(sensor.mounting_mode)
        ? sensor.mounting_mode
        : UNCONFIGURED_MOUNTING
      return {
        ...sensor,
        mounting_mode: mountingModeDrafts[sensorKey(sensor)] ?? savedMountingMode,
        inverted: Object.prototype.hasOwnProperty.call(orientationDrafts, sensorKey(sensor))
          ? orientationDrafts[sensorKey(sensor)]
          : Boolean(sensor.inverted),
      }
    })
  }, [cameraContractLocked, detectedByKey, existing.data?.config.capture.sensors, mountingModeDrafts, orientationDrafts])

  const operatorAliasFor = (sensor: RunSensor) => {
    const key = sensorKey(sensor)
    return Object.prototype.hasOwnProperty.call(operatorAliasDrafts, key)
      ? operatorAliasDrafts[key]
      : sensor.operator_alias ?? ""
  }
  const cameraLabel = (sensor: RunSensor) => {
    const alias = operatorAliasFor(sensor).trim()
    if (alias) return alias
    const detected = detectedByKey.get(sensorKey(sensor))
    if (detected?.display_name) return detected.display_name
    return sensor.operator_alias ? sensorKey(sensor) : sensor.display_name || sensor.device_id
  }
  const updateRunMountingMode = (sensor: RunSensor, mountingMode: MountingMode) => {
    if (cameraContractLocked) return
    const key = sensorKey(sensor)
    const savedSensor = existing.data?.config.capture.sensors.find((item) => sensorKey(item) === key)
    const detectedMountingMode = detectedByKey.get(key)?.mounting_mode
    const savedMountingMode = isConfiguredMounting(savedSensor?.mounting_mode)
      ? savedSensor.mounting_mode
      : isConfiguredMounting(detectedMountingMode)
        ? detectedMountingMode
        : UNCONFIGURED_MOUNTING
    setMountingModeDrafts((current) => {
      const next = { ...current }
      if (mountingMode === savedMountingMode) delete next[key]
      else next[key] = mountingMode
      return next
    })
    setConfirmReplacement(false)
  }
  const updateRunOrientation = (sensor: RunSensor, inverted: boolean) => {
    if (cameraContractLocked) return
    const key = sensorKey(sensor)
    const savedSensor = existing.data?.config.capture.sensors.find((item) => sensorKey(item) === key)
    const savedInverted = Boolean(savedSensor?.inverted ?? detectedByKey.get(key)?.inverted)
    setOrientationDrafts((current) => {
      const next = { ...current }
      if (inverted === savedInverted) delete next[key]
      else next[key] = inverted
      return next
    })
    setConfirmReplacement(false)
  }
  const hasMountingChanges = Object.keys(mountingModeDrafts).length > 0
  const hasOrientationChanges = Object.keys(orientationDrafts).length > 0
  const enabledBySensor = enabledOverrides[selectedRun] ?? {}
  const isEnabled = (sensor: RunSensor) => enabledBySensor[sensorKey(sensor)] ?? sensor.enabled ?? true
  const enabledSensors = configuredSensors.filter(isEnabled)
  const missingMountingSensorKeys = enabledSensors.filter((sensor) => !isConfiguredMounting(sensor.mounting_mode)).map(sensorKey)
  const cameraMountingReady = missingMountingSensorKeys.length === 0
  const selectedResolution = useWatch({ control: form.control, name: "resolution" })
  const savedEnabledSensorKeys = (existing.data?.config.capture.sensors ?? [])
    .filter((sensor) => sensor.enabled !== false)
    .map(sensorKey)
    .sort()
  const selectedEnabledSensorKeys = enabledSensors.map(sensorKey).sort()
  const hasEnabledMembershipChanges = Boolean(existing.data?.config)
    && savedEnabledSensorKeys.join("\n") !== selectedEnabledSensorKeys.join("\n")
  const hasResolutionChanges = Boolean(existing.data?.config)
    && selectedResolution !== existing.data?.config.capture.resolution
  const hasCameraContractChanges = hasMountingChanges
    || hasOrientationChanges
    || hasEnabledMembershipChanges
    || hasResolutionChanges
  const resolutionOptions = sharedResolutions(sensors.data, enabledSensors)
  const activeSourceBySensor = calibrationAssignments?.runRoot === selectedRun
    ? calibrationAssignments.sourceBySensor
    : {}
  const calibrationAssignmentRows = enabledSensors.map((sensor) => {
    const key = sensorKey(sensor)
    const eligibleSources = calibrationSources.filter((source) => source.valid
      && Boolean(source.bundle_sha256)
      && Boolean(calibrationMappingFor(source, sensor, selectedResolution)))
    const selectedSource = eligibleSources.find((source) => source.source_run_root === activeSourceBySensor[key]) ?? null
    const selectedMapping = selectedSource ? calibrationMappingFor(selectedSource, sensor, selectedResolution) : null
    return { sensor, key, eligibleSources, selectedSource, selectedMapping }
  })
  const hasCalibrationDraft = calibrationAssignmentRows.some((row) => Boolean(activeSourceBySensor[row.key]))
  const calibrationAssignmentsComplete = calibrationAssignmentRows.length > 0
    && calibrationAssignmentRows.every((row) => Boolean(row.selectedSource && row.selectedMapping))
  const existingConfig = intent === "dataset" ? existing.data?.config : undefined
  const existingCalibration = existingConfig?.calibration_profiles ?? null
  const existingIntrinsicCalibration = existingConfig?.intrinsic_calibration_profiles ?? null
  const configuredSelectionHash = existingConfig?.calibration_profile_selection?.bundle_sha256 ?? null
  const librarySelection = calibrations.data?.selected ?? null
  const currentSelectedBundleHash = librarySelection?.valid === true
    ? librarySelection.source.bundle_sha256
    : null
  const existingSelectionComplete = Boolean(existingCalibration && existingIntrinsicCalibration && configuredSelectionHash)
  const existingCalibrationReady = existingSelectionComplete
    && librarySelection?.valid === true
    && currentSelectedBundleHash === configuredSelectionHash
    && !hasCameraContractChanges
  const hasInvalidExistingSelection = existingSelectionComplete
    && !calibrations.isPending
    && (!librarySelection || librarySelection.valid === false || currentSelectedBundleHash !== configuredSelectionHash)
  const replacingSelection = Boolean(hasCalibrationDraft && currentSelectedBundleHash)
  const replacementBlockers = calibrations.data?.replacement_blockers ?? []
  const replacementBlocked = replacingSelection && replacementBlockers.length > 0
  const replacementConfirmed = !replacingSelection || confirmReplacement
  const calibrationReady = cameraMountingReady && (intent === "calibration"
    || (hasCalibrationDraft
      ? calibrationAssignmentsComplete && replacementConfirmed && !replacementBlocked
      : existingCalibrationReady))

  useEffect(() => {
    const config = existing.data?.config
    if (!config) return
    form.reset(setupValuesFromConfig(config, maximumCaptureSpeedMps))
  }, [existing.data, form, intent, maximumCaptureSpeedMps])

  const save = useMutation({
    mutationFn: async (values: SetupValues) => {
      if (!sensors.data) throw new Error("Camera discovery must finish before saving this setup")
      if (enabledSensors.length === 0) throw new Error("Select at least one camera in Workflow step 1")
      if (!cameraMountingReady) throw new Error(`Choose a Static or Robot-mounted value for: ${missingMountingSensorKeys.join(", ")}`)
      const unavailable = enabledSensors.filter((sensor) => {
        const detected = detectedByKey.get(sensorKey(sensor))
        return !detected || detected.connected === false || detected.capture_ready === false
      })
      if (unavailable.length) throw new Error(`Enabled cameras are not ready: ${unavailable.map(sensorKey).join(", ")}`)
      let calibrationProfiles = existing.data?.config.calibration_profiles ?? ""
      let intrinsicProfiles = intent === "dataset"
        ? existing.data?.config.intrinsic_calibration_profiles ?? ""
        : ""
      let sensorProfiles: Record<string, string> = {}
      let expectedCalibrationBundleSha256 = existingCalibrationReady ? configuredSelectionHash : null

      if (intent === "dataset" && hasCalibrationDraft) {
        if (!calibrationAssignmentsComplete) throw new Error("Choose a calibration source for every enabled camera")
        if (replacingSelection && !confirmReplacement) throw new Error("Confirm that you want to replace the current calibration selection")
        const selectionsBySource = new Map<string, { source: CalibrationSource; sensorKeys: string[] }>()
        for (const row of calibrationAssignmentRows) {
          if (!row.selectedSource?.bundle_sha256 || !row.selectedMapping) {
            throw new Error(`Choose a valid calibration source for ${row.key}`)
          }
          const grouped = selectionsBySource.get(row.selectedSource.source_run_root)
          if (grouped) grouped.sensorKeys.push(row.key)
          else selectionsBySource.set(row.selectedSource.source_run_root, {
            source: row.selectedSource,
            sensorKeys: [row.key],
          })
        }
        const selected = await api<CalibrationSelectionResponse>("/ui/calibrations/select", {
          method: "POST",
          body: JSON.stringify({
            run_root: selectedRun,
            source_selections: [...selectionsBySource.values()].map(({ source, sensorKeys }) => ({
              source_run_root: source.source_run_root,
              expected_bundle_sha256: source.bundle_sha256,
              sensor_keys: sensorKeys,
            })),
            expected_current_bundle_sha256: currentSelectedBundleHash,
            confirm_replace: replacingSelection && confirmReplacement,
            resolution: values.resolution,
            sensors: enabledSensors.map((sensor) => ({
              sensor_type: sensor.sensor_type,
              device_id: sensor.device_id,
              mounting_mode: sensor.mounting_mode,
              inverted: Boolean(sensor.inverted),
            })),
          }),
        })
        calibrationProfiles = selected.calibration_profiles
        intrinsicProfiles = selected.intrinsic_calibration_profiles
        sensorProfiles = selected.sensor_profiles ?? selected.selection.sensor_profiles ?? Object.fromEntries(selected.sensor_profile_mapping.map((item) => [item.sensor_key, item.profile_id]))
        expectedCalibrationBundleSha256 = selected.selection.source.bundle_sha256
      }
      if (intent === "dataset" && (!calibrationProfiles || !intrinsicProfiles || !expectedCalibrationBundleSha256)) {
        throw new Error("Choose a compatible saved calibration before saving this dataset setup")
      }

      const savedSensorKeys = new Set((existing.data?.config.capture.sensors ?? []).map(sensorKey))
      const selectedSensors = configuredSensors.filter((sensor) => isEnabled(sensor) || savedSensorKeys.has(sensorKey(sensor))).map((sensor) => {
        const key = sensorKey(sensor)
        const operatorAlias = operatorAliasFor(sensor).trim() || null
        const detected = detectedByKey.get(key)
        const fallbackDisplayName = detected?.display_name
          ?? (sensor.operator_alias ? sensorKey(sensor) : sensor.display_name)
          ?? sensor.device_id
        const metadata = sensor.metadata
        return {
          sensor_type: sensor.sensor_type,
          device_id: sensor.device_id,
          display_name: operatorAlias ?? fallbackDisplayName,
          operator_alias: operatorAlias,
          mounting_mode: sensor.mounting_mode,
          enabled: isEnabled(sensor),
          calibration_profile_id: sensorProfiles[key] ?? (
            mountingModeDrafts[key] || Object.prototype.hasOwnProperty.call(orientationDrafts, key)
              ? null
              : sensor.calibration_profile_id ?? null
          ),
          inverted: Boolean(sensor.inverted),
          metadata: metadata && typeof metadata === "object" && !Array.isArray(metadata)
            ? metadata
            : {},
        }
      })
      const runValues = {
        run_name: values.run_name,
        resolution: values.resolution,
        fps: values.fps,
        velocity_m_s: values.velocity,
      }
      return api<RunConfigResponse>("/run-config", {
        method: "POST",
        body: JSON.stringify({
          run_root: selectedRun,
          ...runValues,
          sensors: selectedSensors,
          intent,
          annotation_mode: intent === "calibration" ? "none" : values.annotation_mode,
          calibration_profiles: calibrationProfiles || null,
          intrinsic_calibration_profiles: intrinsicProfiles || null,
          ...(intent === "dataset" ? { expected_calibration_bundle_sha256: expectedCalibrationBundleSha256 } : {}),
          dataset_mode: intent === "dataset" ? "pose_template" : "objectless",
          synchronization: TIMESTAMP_SYNCHRONIZATION,
        }),
      })
    },
    onSuccess: (data) => {
      toast.success(intent === "calibration" ? "Calibration recording setup saved" : "Object dataset setup saved")
      queryClient.setQueryData<RunConfigResponse>(["run-config", selectedRun], (current) => current ? { ...current, ...data } : data)
      setMountingModeDrafts((current) => Object.fromEntries(Object.entries(current).filter(([key, mountingMode]) => {
        const saved = data.config.capture.sensors.find((sensor) => sensorKey(sensor) === key)
        return saved?.mounting_mode !== mountingMode
      })))
      setOrientationDrafts((current) => Object.fromEntries(Object.entries(current).filter(([key, inverted]) => {
        const saved = data.config.capture.sensors.find((sensor) => sensorKey(sensor) === key)
        return Boolean(saved?.inverted) !== inverted
      })))
      setCalibrationAssignments(null)
      setConfirmReplacement(false)
      void queryClient.invalidateQueries({ queryKey: ["run-config", selectedRun] })
      void queryClient.invalidateQueries({ queryKey: ["calibration-library", selectedRun] })
      void queryClient.invalidateQueries({ queryKey: ["overview", selectedRun] })
      void queryClient.invalidateQueries({ queryKey: ["workflow-status", selectedRun] })
      void queryClient.invalidateQueries({ queryKey: ["runs"] })
    },
    onError: (error) => toast.error("Setup was not saved", { description: errorMessage(error) }),
    onSettled: () => {
      if (intent === "dataset") void queryClient.invalidateQueries({ queryKey: ["calibration-library", selectedRun] })
    },
  })

  return <div className="space-y-5" data-testid={`${intent}-run-setup`}>
    {cameraContractLocked && <div id="camera-contract-lock-reason" data-testid="camera-contract-lock" className="flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 p-4 text-xs"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" /><div><div className="font-semibold">Capture reference and camera contract fixed for this acquired run</div><p className="mt-1 leading-relaxed text-muted-foreground">Robot-pose Sunrise reference, camera identity, enabled membership, mounting, orientation, resolution, frame rate, and synchronization can no longer change because captured or derived evidence depends on them. Start a fresh run for a different arrangement.</p>{cameraContractBlockers.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-4 font-mono text-[10px] text-muted-foreground">{cameraContractBlockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>}</div></div>}

    <Card data-testid="run-camera-selection-section" role="region" aria-labelledby="run-camera-selection-heading">
      <CardHeader className="gap-3 border-b sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle id="run-camera-selection-heading" className="flex items-center gap-2 text-base"><Camera className="size-4" />Select cameras for this recording</CardTitle>
          <CardDescription className="mt-1 max-w-3xl">Camera selection happens only here in Workflow step 1. Alias, mounting, orientation, and selection below belong to this run after you save setup; later Devices-default changes do not overwrite them.</CardDescription>
        </div>
        <div className="shrink-0 rounded-lg border bg-muted/30 px-3 py-2 text-right">
          <div className="text-lg font-semibold leading-none">{enabledSensors.length}</div>
          <div className="mt-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">of {configuredSensors.length} selected</div>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        <div className="text-xs text-muted-foreground">{configuredSensors.length - enabledSensors.length} not selected · {sensors.data?.total_connected ?? 0} connected</div>
        {configuredSensors.length === 0 && <div className="mt-3 rounded border border-warning/40 bg-warning/10 p-3 text-xs">No cameras are available. Use Devices to troubleshoot discovery, then return to Workflow step 1.</div>}
        {configuredSensors.length > 0 && enabledSensors.length === 0 && <div className="mt-3 rounded border border-warning/40 bg-warning/10 p-3 text-xs" data-testid="run-camera-selection-required">Select at least one camera here before saving this recording setup.</div>}
        <div className="mt-4 space-y-3">{configuredSensors.map((sensor) => {
          const enabled = isEnabled(sensor)
          const key = sensorKey(sensor)
          const detected = detectedByKey.get(key)
          const label = cameraLabel(sensor)
          const safeKey = key.replaceAll(/[^a-zA-Z0-9_-]/g, "-")
          const aliasInputId = `run-operator-alias-${safeKey}`
          const mountingInputId = `run-mounting-mode-${safeKey}`
          const orientationInputId = `run-orientation-${safeKey}`
          const readiness = !detected ? "not detected" : detected.capture_ready === false ? "not ready" : "ready"
          return <div data-testid="run-camera-row" data-sensor-key={key} data-camera-state={enabled ? "selected" : "not-selected"} key={key} className={`rounded-lg border p-4 text-xs transition-colors ${enabled ? "border-primary/35 bg-primary/5" : "border-dashed border-border bg-muted/15"}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <Label className="flex min-w-0 cursor-pointer items-start gap-3">
                <Checkbox className="mt-0.5" data-testid="run-camera-selection" aria-label={`Select ${label} for this run`} aria-describedby={cameraContractLocked ? "camera-contract-lock-reason" : undefined} checked={enabled} onCheckedChange={(checked) => { if (!cameraContractLocked) setEnabledOverrides((current) => ({ ...current, [selectedRun]: { ...current[selectedRun], [key]: checked === true } })) }} disabled={setupControlsDisabled || save.isPending || cameraContractLocked} />
                <span className="min-w-0"><span className="block truncate font-semibold">{label}</span><span className="mt-0.5 block font-mono text-[10px] font-normal text-muted-foreground">{key}</span><span className="mt-1 block text-[11px] font-normal text-muted-foreground">Use for this recording</span></span>
              </Label>
              <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${enabled ? "bg-success/15 text-success" : "bg-muted-foreground/15 text-muted-foreground"}`}>{enabled ? "Selected" : "Not selected"}</span>
            </div>
            <div className={`mt-4 grid gap-4 lg:grid-cols-3 ${enabled ? "" : "opacity-70"}`}>
              <div className="space-y-1.5"><Label htmlFor={aliasInputId} className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Operator alias for this run</Label><Input id={aliasInputId} data-testid="run-camera-alias" aria-label={`Operator alias for ${key}`} value={operatorAliasFor(sensor)} placeholder={detected?.display_name ?? sensor.display_name ?? sensor.device_id} onChange={(event) => setOperatorAliasDrafts((current) => ({ ...current, [key]: event.target.value }))} disabled={setupControlsDisabled || save.isPending} /><p className="text-[10px] leading-relaxed text-muted-foreground">Saved in <code>run_config.json</code>. Capture planning copies a selected alias into the plan and manifest; later Devices-default changes do not rename this run.</p></div>
              <div className="space-y-1.5"><Label htmlFor={mountingInputId} className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Mounting for this run</Label><Select value={isConfiguredMounting(sensor.mounting_mode) ? sensor.mounting_mode : UNCONFIGURED_MOUNTING} onValueChange={(value) => { if (value !== UNCONFIGURED_MOUNTING) updateRunMountingMode(sensor, value as MountingMode) }} disabled={setupControlsDisabled || save.isPending || cameraContractLocked}><SelectTrigger id={mountingInputId} data-testid="run-camera-mounting" aria-label={`Mounting for ${key}`} aria-describedby={cameraContractLocked ? "camera-contract-lock-reason" : undefined}><SelectValue /></SelectTrigger><SelectContent><SelectItem value={UNCONFIGURED_MOUNTING} disabled>Mounting not configured</SelectItem><SelectItem value="eye_in_hand">Robot-mounted</SelectItem><SelectItem value="static">Static</SelectItem></SelectContent></Select><p className={`text-[10px] leading-relaxed ${isConfiguredMounting(sensor.mounting_mode) ? "text-muted-foreground" : "font-medium text-destructive"}`}>{isConfiguredMounting(sensor.mounting_mode) ? <>Save setup to write this run-owned value to <code>run_config.json</code>. The Devices default does not overwrite an existing run.</> : "Required: choose the camera's physical mount before saving this run."}</p></div>
              <div className="space-y-1.5"><Label htmlFor={orientationInputId} className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Image orientation for this run</Label><Select value={sensor.inverted ? "inverted" : "normal"} onValueChange={(value) => updateRunOrientation(sensor, value === "inverted")} disabled={setupControlsDisabled || save.isPending || cameraContractLocked || sensor.sensor_type !== "realsense_d435"}><SelectTrigger id={orientationInputId} data-testid="run-camera-orientation" aria-label={`Image orientation for ${key}`} aria-describedby={cameraContractLocked ? "camera-contract-lock-reason" : undefined}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="normal">Normal</SelectItem><SelectItem value="inverted">Inverted (180°)</SelectItem></SelectContent></Select><p className="text-[10px] leading-relaxed text-muted-foreground">Save setup to persist this run-owned value. Changing it requires orientation-compatible calibration; override is available only for RealSense D435 cameras.</p></div>
            </div>
            <div className={`mt-3 flex items-center gap-1 border-t pt-3 text-[10px] ${(readiness !== "ready" || !isConfiguredMounting(sensor.mounting_mode)) && enabled ? "text-destructive" : "text-muted-foreground"}`}><Gauge className="size-3" />{isConfiguredMounting(sensor.mounting_mode) ? `${mountingLabel(sensor.mounting_mode)} camera` : "Mounting not configured"} · {sensor.inverted ? "inverted 180°" : "normal orientation"} · {readiness}</div>
          </div>
        })}</div>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>{intent === "calibration" ? "Calibration recording setup" : "Object dataset recording setup"}</CardTitle><CardDescription>{intent === "calibration" ? "Choose the cameras and capture settings used to record the printed grid." : "Choose the cameras, capture settings, and a previously saved calibration that covers every enabled camera."}</CardDescription></CardHeader>
      <CardContent><form className="space-y-6" aria-busy={setupLookupPending} onSubmit={form.handleSubmit((values) => {
        if (!setupControlsDisabled) save.mutate(values)
      })}>
        <fieldset className="space-y-6" disabled={setupControlsDisabled}>
        <div className="grid gap-4 sm:grid-cols-2"><Field id="run-name" label="Run display name (optional)" hint="Human-readable metadata only. Leave blank to use the folder name; changing it does not select or rename storage." error={form.formState.errors.run_name?.message}><Input id="run-name" {...form.register("run_name")} placeholder="Defaults to folder name" /></Field><Field id="resolution" label="Image resolution" hint="Only modes shared by every enabled camera are offered."><Controller control={form.control} name="resolution" render={({ field }) => <Select value={field.value} onValueChange={field.onChange} disabled={cameraContractLocked}><SelectTrigger id="resolution" aria-describedby={cameraContractLocked ? "camera-contract-lock-reason" : undefined}><SelectValue /></SelectTrigger><SelectContent>{resolutionOptions.map((resolution) => <SelectItem value={resolution} key={resolution}>{resolution === "720p" ? "1280 × 720" : resolution === "360p" ? "672 × 376" : resolution}</SelectItem>)}</SelectContent></Select>} /></Field></div>
        <div className="grid gap-4 sm:grid-cols-2"><Field id="fps" label={<span className="inline-flex items-center gap-1">Frames per second <HelpTip label="frames per second">The requested RGB-D frame rate for each enabled camera. Higher rates create more data and may exceed a camera or USB connection's supported mode.</HelpTip></span>} hint="How many RGB-D frames each camera should request per second." error={form.formState.errors.fps?.message}><Input id="fps" type="number" min={1} max={60} disabled={cameraContractLocked} aria-describedby={cameraContractLocked ? "camera-contract-lock-reason" : undefined} {...form.register("fps", { valueAsNumber: true })} /></Field><Field id="velocity" label={<span className="inline-flex items-center gap-1">{intent === "dataset" ? "Requested robot capture speed (m/s)" : "Robot capture speed limit (m/s)"} <HelpTip label="robot capture speed">{captureSpeedHelp}</HelpTip></span>} hint={captureSpeedHint} error={form.formState.errors.velocity?.message}><Input id="velocity" type="number" min="0.01" max={maximumCaptureSpeedMps} step="0.01" {...form.register("velocity", { valueAsNumber: true })} /></Field></div>

        <section className="space-y-3 rounded-lg border bg-muted/15 p-4" data-testid="robot-pose-reference-setup" aria-labelledby="robot-pose-reference-heading">
          <div><h3 id="robot-pose-reference-heading" className="text-sm font-semibold">Fixed PoseTemplateBase robot-pose reference</h3><p className="mt-1 text-xs leading-relaxed text-muted-foreground">Every <code>robot_pose.v1</code> packet must report the commissioned Application Data path below together with this run's exact ID. The setting is not browser-editable.</p></div>
          <div data-testid="robot-pose-reference-path" className="rounded border bg-background p-3 font-mono text-xs">{POSE_TEMPLATE_BASE_SUNRISE_PATH}</div>
          {intent === "calibration" && <div className="rounded border border-primary/25 bg-primary/5 p-3 text-xs leading-relaxed" data-testid="calibration-result-reference"><span className="font-semibold">Static-camera output:</span> The reusable camera → PoseTemplateBase result is bound to this frame. The moving grid provides observations and a jointly estimated attachment offset; it does not change the published destination frame.</div>}
          <div className="rounded border border-warning/35 bg-warning/5 p-3 text-xs leading-relaxed"><span className="font-semibold">Controller contract:</span> Packets with a different frame path, missing run ID, or a non-current schema are rejected before capture evidence can be accepted.</div>
        </section>

        {intent === "dataset" && <section className="space-y-3 rounded-lg border bg-muted/15 p-4" data-testid="capture-synchronization-setup" aria-labelledby="capture-synchronization-heading">
          <div><h3 id="capture-synchronization-heading" className="text-sm font-semibold">Cross-camera timing</h3><p className="mt-1 text-xs leading-relaxed text-muted-foreground">All PoseTestBot acquisitions use timestamp-aligned RGB-D streams.</p></div>
          <div className="rounded border border-primary/25 bg-primary/5 p-3 text-xs leading-relaxed"><div className="font-semibold">Timestamp association</div><p className="mt-1 text-muted-foreground">Each camera preserves its own RGB-D stream and timestamps. Processing associates frames and robot poses using the promoted calibration timing evidence; it does not claim simultaneous camera exposures.</p></div>
        </section>}

        {intent === "dataset" && <section className="space-y-3 rounded-lg border bg-muted/15 p-4" data-testid="bop-annotation-mode-setup" aria-labelledby="bop-annotation-mode-heading">
          <div><h3 id="bop-annotation-mode-heading" className="text-sm font-semibold">BOP annotation outcome</h3><p className="mt-1 text-xs leading-relaxed text-muted-foreground">The base image/model export is always produced first. Ground-truth rendering remains a separate optional background job after processing.</p></div>
          <Controller control={form.control} name="annotation_mode" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger id="annotation-mode" aria-label="BOP annotation mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="none">Base BOP dataset only</SelectItem><SelectItem value="pose">Optional pose ground truth</SelectItem><SelectItem value="pose_and_masks">Optional pose + masks</SelectItem></SelectContent></Select>} />
          <p className="text-[11px] leading-relaxed text-muted-foreground">This explicit run contract records the intended final dataset evidence. Selecting an annotated outcome does not queue rendering.</p>
        </section>}

        {intent === "dataset" && <section className="space-y-3" aria-labelledby="saved-calibration-heading">
          <div><h3 id="saved-calibration-heading" className="text-sm font-semibold">Saved camera calibration <span className="text-destructive">Required</span></h3><p className="mt-1 text-xs leading-relaxed text-muted-foreground">Choose a promoted source for each enabled camera. Static and robot-mounted cameras may come from different calibration runs; PoseTestBot combines only the assigned profile pairs into one immutable, run-owned snapshot.</p></div>
          {existingCalibrationReady && !hasCalibrationDraft && <div className="flex items-start gap-2 rounded-lg border border-success/35 bg-success/5 p-3 text-xs"><CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" /><div><div className="font-semibold">A verified calibration snapshot is selected</div><div className="mt-1 break-all font-mono text-[10px] text-muted-foreground">{existingCalibration}</div><p className="mt-1 text-muted-foreground">Both profile files and the selection record agree on bundle <span className="font-mono">{configuredSelectionHash?.slice(0, 16)}…</span>. Assign sources below only if you intend to replace it.</p></div></div>}
          {hasCameraContractChanges && !hasCalibrationDraft && <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" /><div><div className="font-semibold">Camera contract changed for this run</div><p className="mt-1 text-muted-foreground">The previous calibration cannot be reused implicitly after camera membership, resolution, mounting, or image-orientation changes. Assign a compatible promoted source to every enabled camera before saving.</p></div></div>}
          {hasInvalidExistingSelection && !hasCalibrationDraft && <div className="flex items-start gap-2 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" /><div><div className="font-semibold">Saved calibration selection needs attention</div><p className="mt-1 text-muted-foreground">{librarySelection?.valid === false && librarySelection.issues[0] ? librarySelection.issues[0].message : "The selected snapshot does not match the bundle recorded in run_config.json."} Assign and validate saved calibrations before recording.</p></div></div>}
          {calibrations.isPending ? <div className="flex items-center gap-2 rounded-lg border p-4 text-xs text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />Loading promoted calibrations…</div> : calibrationSources.length === 0 ? <div className="rounded-lg border border-dashed p-4 text-xs text-muted-foreground">No reusable promoted calibrations were found. Complete and promote the camera-calibration workflow first.</div> : <div className="space-y-3" data-testid="calibration-source-assignments">{calibrationAssignmentRows.map(({ sensor, key, eligibleSources, selectedSource, selectedMapping }) => {
            const selectId = `calibration-source-${key.replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`
            const relevantIssues = calibrationSources.flatMap((source) => source.issues.filter((issue) => issue.sensor_key === key))
            return <div className="rounded-lg border p-3" data-testid="calibration-source-assignment" data-sensor-key={key} key={key}>
              <div className="flex flex-wrap items-start justify-between gap-2"><div><Label htmlFor={selectId} className="font-semibold">{cameraLabel(sensor)}</Label><div className="mt-0.5 font-mono text-[9px] text-muted-foreground">{key}</div></div><span className="rounded-full bg-muted px-2 py-1 text-[10px] font-semibold">{mountingLabel(sensor.mounting_mode)}</span></div>
              {eligibleSources.length ? <div className="mt-3 space-y-2"><Select value={selectedSource?.source_run_root ?? ""} onValueChange={(sourceRunRoot) => { setCalibrationAssignments((current) => ({ runRoot: selectedRun, sourceBySensor: { ...(current?.runRoot === selectedRun ? current.sourceBySensor : {}), [key]: sourceRunRoot } })); setConfirmReplacement(false) }}><SelectTrigger id={selectId} aria-label={`Calibration source for ${cameraLabel(sensor)}`}><SelectValue placeholder="Choose a promoted calibration run" /></SelectTrigger><SelectContent>{eligibleSources.map((source) => {
                const mapping = calibrationMappingFor(source, sensor, selectedResolution)
                return <SelectItem value={source.source_run_root} key={source.source_run_root}><span className="flex min-w-0 flex-col"><span>{source.source_run_name}</span><span className="truncate font-mono text-[9px] text-muted-foreground">{mapping?.profile_id} · {source.source_run_root}</span></span></SelectItem>
              })}</SelectContent></Select>{selectedMapping && <p className="text-[10px] text-muted-foreground">Profile <span className="font-mono">{selectedMapping.profile_id}</span> · lens <span className="font-mono">{selectedMapping.intrinsic_profile_id}</span></p>}</div> : <div className="mt-3 rounded border border-destructive/30 bg-destructive/5 p-2 text-[11px] text-destructive">{isConfiguredMounting(sensor.mounting_mode) ? `No promoted source matches this camera identity, ${sensor.mounting_mode === "static" ? "static" : "robot-mounted"} mount, orientation, and selected resolution.` : "Choose this camera's mounting before selecting a calibration source."}{relevantIssues[0] ? ` ${relevantIssues[0].message}` : ""}</div>}
            </div>
          })}</div>}
          {calibrationSources.length > 0 && <details className="rounded-lg border bg-muted/20"><summary className="cursor-pointer px-3 py-2 text-[11px] font-semibold">Available source-run provenance</summary><div className="space-y-2 border-t p-3">{calibrationSources.map((source) => <div className="text-[10px]" key={source.source_run_root}><div className="font-semibold">{source.source_run_name}</div><div className="break-all font-mono text-muted-foreground">{source.source_run_root}</div><div className="mt-0.5 text-muted-foreground">{source.calibration_profiles.valid_profile_count} valid camera profile{source.calibration_profiles.valid_profile_count === 1 ? "" : "s"} · bundle {source.bundle_sha256?.slice(0, 16) ?? "unavailable"}…</div></div>)}</div></details>}
          {hasCalibrationDraft && calibrationAssignmentsComplete && <div className="rounded-lg border border-primary/25 bg-primary/5 p-3 text-xs"><div className="font-semibold">Ready to combine and validate</div><p className="mt-1 text-muted-foreground">The server will recheck every assignment, build a deterministic profile collection, and preserve each source run and sensor mapping in the selection record.</p></div>}
          {hasCalibrationDraft && !calibrationAssignmentsComplete && <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs"><div className="font-semibold">A source is still required for every enabled camera</div><p className="mt-1 text-muted-foreground">Partial selections are kept as a browser-local draft and cannot change the run.</p></div>}
          {replacementBlocked && <div className="flex items-start gap-2 rounded-lg border border-destructive/35 bg-destructive/5 p-3 text-xs"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" /><div><div className="font-semibold">This run can no longer change calibration</div><p className="mt-1 text-muted-foreground">Derived dataset evidence already depends on the current snapshot. Start a new run to use another calibration.</p><ul className="mt-2 list-disc space-y-1 pl-4 text-[11px] text-muted-foreground">{replacementBlockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></div></div>}
          {replacingSelection && !replacementBlocked && <Label className="flex cursor-pointer items-start gap-3 rounded-lg border border-warning/40 bg-warning/10 p-3 text-xs"><Checkbox aria-label="Confirm replacing the current calibration selection" checked={confirmReplacement} onCheckedChange={(checked) => setConfirmReplacement(checked === true)} /><span><span className="block font-semibold">Replace the current calibration selection</span><span className="mt-1 block font-normal text-muted-foreground">I understand this replaces bundle <span className="font-mono">{currentSelectedBundleHash?.slice(0, 12)}…</span> with the camera-by-camera combination above. The server will reject the change if the current selection changed since this page loaded.</span></span></Label>}
        </section>}

        </fieldset>
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
          {setupLookupPending
            ? <p className="inline-flex items-center gap-2 text-xs text-warning" role="status"><LoaderCircle className="size-3.5 animate-spin" />Loading the active run’s saved setup. Save is disabled until this lookup finishes.</p>
            : setupLookupFailed
              ? <div className="flex min-w-0 items-start gap-2 text-xs" role="alert"><TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" /><div><div className="font-semibold text-destructive">The active run’s setup could not be loaded</div><p className="mt-1 text-muted-foreground">{errorMessage(existing.error)} Existing setup may still be present, so saving remains disabled.</p><Button type="button" variant="outline" size="sm" className="mt-2" onClick={() => existing.refetch()} disabled={existing.isFetching}><RefreshCw className={existing.isFetching ? "animate-spin" : undefined} />Retry setup lookup</Button></div></div>
              : !cameraMountingReady
                ? <p className="text-xs font-medium text-destructive" data-testid="run-mounting-required">Choose Static or Robot-mounted for every enabled camera. PoseTestBot will not assume a mounting.</p>
                : null}
          <Button type="submit" className="sm:ml-auto" disabled={setupControlsDisabled || save.isPending || sensors.isPending || enabledSensors.length === 0 || !calibrationReady}>{setupLookupPending || save.isPending ? <LoaderCircle className="animate-spin" /> : <Save />}{save.isPending ? (hasCalibrationDraft ? "Validating and saving…" : "Saving…") : (hasCalibrationDraft ? "Validate and save setup" : "Save setup")}</Button>
        </div>
      </form></CardContent>
    </Card>
  </div>
}

function Field({ id, label, hint, error, children }: { id: string; label: React.ReactNode; hint?: string; error?: string; children: React.ReactNode }) {
  return <div className="space-y-2"><Label htmlFor={id}>{label}</Label>{children}{hint && <p className="text-[11px] leading-relaxed text-muted-foreground">{hint}</p>}{error && <p className="text-xs text-destructive">{error}</p>}</div>
}
