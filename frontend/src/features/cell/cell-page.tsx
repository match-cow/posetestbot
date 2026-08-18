import { Component, Suspense, useEffect, useMemo, useRef, useState } from "react"
import { Canvas, type ThreeEvent, useLoader, useThree } from "@react-three/fiber"
import { OrbitControls, useTexture } from "@react-three/drei"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { BufferGeometry, Color, DoubleSide, Float32BufferAttribute, Line, LineBasicMaterial, LineSegments, Quaternion, Shape, Vector3 } from "three"
import { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js"
import { AlertTriangle, Box, Camera, CirclePause, CirclePlay, Crosshair, Eye, EyeOff, Focus, RotateCcw, Route, ScanLine } from "lucide-react"
import { HelpTip } from "@/components/help-tip"
import { PageHeader } from "@/components/page-header"
import { ProcessHandoff } from "@/components/process-handoff"
import { StatusBadge, type StatusTone } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, query } from "@/lib/api"
import type { CellEntity, CellPose, CellScene, CellTimelineMetadata, CellTimelinePage, CellTrajectoryMetadata, CellTransform } from "@/lib/contracts"
import { titleCase } from "@/lib/utils"
import { activeWorkflowHref } from "@/lib/workflow-session"
import { useOperator } from "@/providers/operator-provider"

const PAGE_SIZE = 2_000
const LAYERS = ["reference_frame", "template", "robot_base", "robot_flange", "tcp", "camera", "object", "calibration_target"]
const REFERENCE_GRID_SIZE_MM = 1_600
const REFERENCE_GRID_DIVISIONS = 32
const REFERENCE_GRID_CLEARANCE_MM = 12
const REFERENCE_GRID_THICKNESS_MM = 6
const PRINT_SURFACE_THICKNESS_MM = 3
const PRINT_LAYER_GAP_MM = 0.3
const PRINT_LAYER_THICKNESS_MM = 0.2
type Preset = "perspective" | "top" | "front"
type CameraFrameModality = "rgb" | "depth"
type CameraFrameViewMode = CameraFrameModality | "rgb_depth"
type CellPresentation = {
  mode: string
  transform: CellTransform
}

const IDENTITY_PRESENTATION: CellPresentation = {
  mode: "reference_z_up",
  transform: {
    semantics: "entity_to_parent",
    parent_frame: "display",
    translation_mm: [0, 0, 0],
    rotation_quaternion_wxyz: [1, 0, 0, 0],
  },
}

function hasWebGL() {
  try {
    const canvas = document.createElement("canvas")
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"))
  } catch {
    return false
  }
}

function transformProps(transform: CellTransform) {
  const [w, x, y, z] = transform.rotation_quaternion_wxyz
  return {
    position: transform.translation_mm as [number, number, number],
    quaternion: new Quaternion(x, y, z, w),
  }
}

function cellPresentation(scene: CellScene): CellPresentation {
  const raw = scene.coordinate_system.presentation as Partial<CellPresentation> | undefined
  if (!raw || typeof raw.mode !== "string" || !raw.transform) return IDENTITY_PRESENTATION
  return { mode: raw.mode, transform: raw.transform }
}

function cellTrajectory(scene: CellScene): CellTrajectoryMetadata {
  if (scene.trajectory) return scene.trajectory
  const referenceFrame = typeof scene.coordinate_system.reference_frame === "string" ? scene.coordinate_system.reference_frame : "template_base"
  const referenceFrameLabel = typeof scene.coordinate_system.reference_frame_label === "string" ? scene.coordinate_system.reference_frame_label : referenceFrame === "template_base" ? "PoseTemplateBase" : titleCase(referenceFrame)
  return {
    entity_id: "robot_flange",
    label: "Robot flange",
    reference_frame: referenceFrame,
    reference_frame_label: referenceFrameLabel,
    source_timeline_id: scene.default_timeline_id,
    derivation: "legacy_cell_scene_recorded_robot_flange_preview",
  }
}

function statusColor(status: CellEntity["status"]) {
  if (status === "unresolved") return "#ef4444"
  if (status === "recorded") return "#22c55e"
  if (status === "reference") return "#f59e0b"
  return "#38bdf8"
}

function finiteNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function fixed(value: unknown, digits: number) {
  return finiteNumber(value)?.toFixed(digits) ?? "—"
}

function number(value: unknown, digits: number, suffix = "") {
  const formatted = fixed(value, digits)
  return formatted === "—" ? formatted : `${formatted}${suffix}`
}

function vector(values: readonly unknown[] | null | undefined, digits: number) {
  return values?.map((value) => fixed(value, digits)).join(", ") ?? "—"
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return <div><span className="text-muted-foreground">{label}</span><div className="mt-1 break-all font-mono text-[10px]">{value}</div></div>
}

function AxisLegend() {
  return <span data-testid="cell-axis-legend" aria-label="Coordinate axes: X red, Y green, Z blue" className="whitespace-nowrap font-mono">
    axes <span className="text-red-400">X</span>/<span className="text-emerald-400">Y</span>/<span className="text-blue-400">Z</span>
  </span>
}

function Matrix({ values, testId = "cell-calibration-matrix" }: { values: readonly (readonly unknown[])[]; testId?: string }) {
  return <pre data-testid={testId} className="overflow-x-auto rounded bg-muted p-3 text-[9px] leading-5">{values.map((row) => row.map((value) => fixed(value, 6).padStart(12)).join(" ")).join("\n")}</pre>
}

function entityStatusTone(status: CellEntity["status"]): StatusTone {
  switch (status) {
    case "recorded":
      return "success"
    case "unresolved":
      return "destructive"
    case "planned":
    case "reference":
      return "informational"
    case "not_configured":
      return "neutral"
  }
}

function SelectionDetails({ entity }: { entity: CellEntity | null }) {
  if (!entity) return <div className="py-6 text-center text-sm text-muted-foreground"><Box className="mx-auto mb-2 size-5" />Nothing selected</div>
  const calibration = entity.calibration
  const staticWorkcellCalibration = calibration?.mounting_mode === "static"
  const calibrationFrameLabel = staticWorkcellCalibration
    && calibration?.extrinsics.from === "camera"
    && calibration.extrinsics.to === "template_base"
    ? "camera → PoseTemplateBase"
    : calibration ? `${calibration.extrinsics.from} → ${calibration.extrinsics.to}` : ""
  const solver = calibration?.evidence.promotion_solver_provenance
  const solverLabel = solver?.pnp_method || solver?.extrinsic_method
    ? [solver.pnp_method, solver.extrinsic_method].filter(Boolean).join(" + ")
    : null
  const pdfUrl = typeof entity.geometry.pdf_url === "string" ? entity.geometry.pdf_url : null
  return <div className="space-y-4 text-xs">
    <div className="flex items-center justify-between gap-2"><span className="text-sm font-semibold">{entity.label}</span><StatusBadge status={entity.status} tone={entityStatusTone(entity.status)} /></div>
    <div><span className="text-muted-foreground">Type</span><div>{titleCase(entity.type)}</div></div>
    {entity.unresolved_reason && <div className="rounded border border-destructive/30 bg-destructive/5 p-3 text-destructive">{entity.unresolved_reason}</div>}
    {entity.transform && !calibration && <div className="space-y-3 rounded border p-3">
      <div className="font-semibold">Entity transform</div>
      <Detail label="Frame" value={`${entity.id} → ${entity.transform.parent_frame ?? "root"}`} />
      <Detail label="Translation mm" value={vector(entity.transform.translation_mm, 3)} />
      <Detail label="Quaternion WXYZ" value={vector(entity.transform.rotation_quaternion_wxyz, 7)} />
      {entity.geometry.placement_known === false && <div className="rounded border border-amber-500/30 bg-amber-500/8 p-2 text-amber-700 dark:text-amber-300">Shown at the reference origin because no physical board placement was promoted for this run.</div>}
      {pdfUrl && <a className="inline-flex text-primary underline underline-offset-4" href={pdfUrl} target="_blank" rel="noreferrer">Open exact calibration-target PDF</a>}
    </div>}
    {calibration && <div data-testid="cell-calibration-evidence" className="space-y-4 rounded border border-success/30 p-3">
      <div className="flex items-center justify-between gap-2"><div><div className="font-semibold">{staticWorkcellCalibration ? "Reusable object-capture transform" : "Calibration extrinsic"}</div><div className="mt-1 font-mono text-[10px]" data-testid="cell-calibration-transform-frames">{calibrationFrameLabel}</div>{staticWorkcellCalibration && <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">Primary static-camera profile for placing later object observations in PoseTemplateBase.</p>}</div><StatusBadge status={calibration.status} tone="success" /></div>
      <Matrix values={calibration.extrinsics.matrix} />
      <div className="grid grid-cols-1 gap-3">
        <Detail label="Quaternion WXYZ" value={vector(calibration.extrinsics.rotation_quaternion_wxyz, 7)} />
        <Detail label="Translation mm" value={vector(calibration.extrinsics.translation_mm, 4)} />
      </div>
      {calibration.companion_transform && <div data-testid="cell-calibration-companion" className="space-y-3 border-t pt-3">
        <div><div className="font-semibold">{staticWorkcellCalibration ? "Moving-grid attachment estimate · supporting evidence" : "Fixed-grid placement estimate · supporting evidence"}</div><div className="mt-1 font-mono text-[10px]" data-testid="cell-calibration-companion-frames">{calibration.companion_transform.from} → {calibration.companion_transform.to}</div>{staticWorkcellCalibration && <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">Required to use the robot-carried grid's many poses, but not the reusable camera output or a hand-tracking result.</p>}</div>
        <Matrix values={calibration.companion_transform.matrix} testId="cell-calibration-companion-matrix" />
        <Detail label="Quaternion WXYZ" value={vector(calibration.companion_transform.rotation_quaternion_wxyz, 7)} />
        <Detail label="Translation mm" value={vector(calibration.companion_transform.translation_mm, 4)} />
      </div>}
      <div className="grid grid-cols-2 gap-2 border-t pt-3">
        <Detail label="Observations / inliers" value={`${calibration.quality.num_observations} / ${calibration.quality.num_inliers}`} />
        <Detail label="Mean reprojection" value={number(calibration.quality.mean_reprojection_error_px, 3, " px")} />
        <Detail label="Max reprojection" value={number(calibration.quality.max_reprojection_error_px, 3, " px")} />
        <Detail label="Outlier count" value={calibration.quality.outlier_count ?? "—"} />
        <Detail label="Outlier ratio" value={number(calibration.quality.outlier_ratio, 4)} />
        <Detail label="Held-out translation" value={number(calibration.quality.residual_translation_mm, 3, " mm")} />
        <Detail label="Held-out rotation" value={number(calibration.quality.residual_rotation_deg, 3, "°")} />
        <Detail label="Held-out residual summary" value={calibration.quality.held_out_residuals ? JSON.stringify(calibration.quality.held_out_residuals) : "—"} />
      </div>
      <div className="space-y-3 border-t pt-3">
        <Detail label="Profile" value={calibration.profile_id} />
        <Detail label="Mount" value={`${calibration.mounting_mode} · ${calibration.rig_position}`} />
        <Detail label="Method" value={calibration.evidence.method ?? "—"} />
        <Detail label="Calibration dataset" value={calibration.evidence.calibration_dataset_id ?? "—"} />
        <Detail label="Dataset sync delta" value={number(calibration.evidence.sync_delta_ms, 3, " ms")} />
        <Detail label="Promoted solver" value={solverLabel ?? "—"} />
        <Detail label="Promotion attempt" value={calibration.evidence.promotion_attempt_id ?? "—"} />
        <Detail label="Promotion candidate" value={calibration.evidence.promotion_candidate_id ?? "—"} />
        <Detail label="Multi-camera bundle" value={calibration.evidence.promotion_multi_camera_bundle_id ?? "—"} />
        <Detail label="Target / intrinsic" value={`${calibration.evidence.target_id ?? "—"} / ${calibration.evidence.intrinsic_profile_id ?? "—"}`} />
        <Detail label="Calibrated" value={calibration.evidence.calibrated_at ?? "—"} />
        <Detail label="Promoted" value={calibration.evidence.promoted_at ?? "—"} />
        <Detail label="Operator / promoted by" value={`${calibration.evidence.operator ?? "—"} / ${calibration.evidence.promoted_by ?? "—"}`} />
        <Detail label="Profile source" value={calibration.evidence.profile_source} />
      </div>
    </div>}
    <details className="rounded border"><summary className="cursor-pointer p-2 font-medium">Raw provenance</summary><pre data-testid="cell-raw-provenance" className="max-h-48 overflow-auto border-t bg-muted p-3 text-[10px]">{JSON.stringify({ entity: entity.provenance, calibration: entity.calibration ?? null }, null, 2)}</pre></details>
  </div>
}

class MeshBoundary extends Component<{ children: React.ReactNode; fallback: React.ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}

function PlyMesh({ entity, onSelect }: { entity: CellEntity; onSelect: () => void }) {
  const url = String(entity.geometry.mesh_url)
  const geometry = useLoader(PLYLoader, url)
  const textureUrl = entity.geometry.texture_url
  if (typeof textureUrl === "string" && textureUrl) {
    return <TexturedPly geometry={geometry} textureUrl={textureUrl} onSelect={onSelect} />
  }
  const vertexColors = Boolean(geometry.getAttribute("color"))
  return <mesh geometry={geometry} onClick={(event) => selectEvent(event, onSelect)} castShadow receiveShadow>
    <meshStandardMaterial color={vertexColors ? "white" : "#94a3b8"} vertexColors={vertexColors} roughness={0.72} metalness={0.05} />
  </mesh>
}

function TexturedPly({ geometry, textureUrl, onSelect }: { geometry: BufferGeometry; textureUrl: string; onSelect: () => void }) {
  const texture = useTexture(textureUrl)
  return <mesh geometry={geometry} onClick={(event) => selectEvent(event, onSelect)} castShadow receiveShadow>
    <meshStandardMaterial map={texture} color="white" roughness={0.72} />
  </mesh>
}

function selectEvent(event: ThreeEvent<MouseEvent>, callback: () => void) {
  event.stopPropagation()
  callback()
}

function CameraGeometry({ color, onSelect, geometry }: { color: string; onSelect: () => void; geometry: CellEntity["geometry"] }) {
  const object = useMemo(() => {
    const width = Number(geometry.width || 1280)
    const height = Number(geometry.height || 720)
    const fx = Number(geometry.fx || width)
    const fy = Number(geometry.fy || height)
    const cx = Number(geometry.cx || width / 2)
    const cy = Number(geometry.cy || height / 2)
    const depth = Number(geometry.depth_mm || 180)
    const corners = [[0, 0], [width, 0], [width, height], [0, height]].map(([u, v]) => new Vector3((u - cx) * depth / fx, (v - cy) * depth / fy, depth))
    const origin = new Vector3()
    const segments: number[] = []
    const add = (a: Vector3, b: Vector3) => segments.push(a.x, a.y, a.z, b.x, b.y, b.z)
    corners.forEach((corner) => add(origin, corner))
    corners.forEach((corner, index) => add(corner, corners[(index + 1) % corners.length]))
    const buffer = new BufferGeometry()
    buffer.setAttribute("position", new Float32BufferAttribute(segments, 3))
    return new LineSegments(buffer, new LineBasicMaterial({ color }))
  }, [color, geometry])
  return <group name="camera-proxy" onClick={(event) => selectEvent(event, onSelect)}>
    <primitive object={object} />
    <mesh name="camera-housing" position={[0, 0, -13]} castShadow receiveShadow>
      <boxGeometry args={[70, 42, 26]} />
      <meshStandardMaterial color={color} roughness={0.58} metalness={0.12} />
    </mesh>
    <mesh name="camera-lens" position={[0, 0, 1.8]} rotation={[Math.PI / 2, 0, 0]}>
      <cylinderGeometry args={[8, 10, 3.6, 24]} />
      <meshStandardMaterial color="#07111f" roughness={0.32} metalness={0.28} />
    </mesh>
    <axesHelper name="camera-coordinate-frame" args={[80]} />
  </group>
}

function TemplatePlane({ entity, onSelect }: { entity: CellEntity; onSelect: () => void }) {
  const texture = useTexture(String(entity.geometry.asset_url))
  const width = Number(entity.geometry.width_mm || 420)
  const height = Number(entity.geometry.height_mm || 297)
  return <group onClick={(event) => selectEvent(event, onSelect)}>
    <mesh position={[0, 0, -PRINT_SURFACE_THICKNESS_MM / 2]} receiveShadow>
      <boxGeometry args={[width, height, PRINT_SURFACE_THICKNESS_MM]} />
      <meshStandardMaterial color="#f8fafc" roughness={0.95} />
    </mesh>
    <mesh position={[0, 0, PRINT_LAYER_GAP_MM]} scale={[1, -1, 1]}>
      <planeGeometry args={[width, height]} />
      <meshBasicMaterial map={texture} transparent opacity={0.82} depthWrite={false} polygonOffset polygonOffsetFactor={-1} polygonOffsetUnits={-1} />
    </mesh>
  </group>
}

function TargetGeometry({ entity, color, onSelect }: { entity: CellEntity; color: string; onSelect: () => void }) {
  const grid = entity.geometry.grid_size as number[] | undefined
  const bounds = entity.geometry.target_bounds as { width_mm?: number; height_mm?: number; x_mm?: number; y_mm?: number } | undefined
  const marker = Number(entity.geometry.marker_length_mm || entity.geometry.square_length_mm || 40)
  const gap = Number(entity.geometry.marker_separation_mm || marker)
  const width = Number(bounds?.width_mm) || (grid ? marker + Math.max(0, grid[0] - 1) * gap : 200)
  const height = Number(bounds?.height_mm) || (grid ? marker + Math.max(0, grid[1] - 1) * gap : 150)
  const x = Number(bounds?.x_mm) || 0
  const y = Number(bounds?.y_mm) || 0
  const markers = Array.isArray(entity.geometry.markers) ? entity.geometry.markers : []
  return <group onClick={(event) => selectEvent(event, onSelect)}>
    <mesh position={[x + width / 2, y + height / 2, PRINT_SURFACE_THICKNESS_MM / 2]} receiveShadow>
      <boxGeometry args={[width + 8, height + 8, PRINT_SURFACE_THICKNESS_MM]} />
      <meshStandardMaterial color="#f8fafc" roughness={0.9} />
    </mesh>
    {markers.map((raw, index) => {
      const item = raw as { corners_mm?: number[][] }
      const corners = item.corners_mm ?? []
      const xs = corners.map((point) => Number(point[0])).filter(Number.isFinite)
      const ys = corners.map((point) => Number(point[1])).filter(Number.isFinite)
      if (!xs.length || !ys.length) return null
      const minX = Math.min(...xs); const maxX = Math.max(...xs)
      const minY = Math.min(...ys); const maxY = Math.max(...ys)
      return <mesh key={index} position={[(minX + maxX) / 2, (minY + maxY) / 2, -PRINT_LAYER_GAP_MM - PRINT_LAYER_THICKNESS_MM / 2]}>
        <boxGeometry args={[maxX - minX, maxY - minY, PRINT_LAYER_THICKNESS_MM]} />
        <meshBasicMaterial color="#020617" />
      </mesh>
    })}
    {!markers.length && <mesh position={[x + width / 2, y + height / 2, -PRINT_LAYER_GAP_MM - PRINT_LAYER_THICKNESS_MM / 2]}>
      <boxGeometry args={[width, height, PRINT_LAYER_THICKNESS_MM]} />
      <meshStandardMaterial color={color} transparent opacity={0.45} />
    </mesh>}
  </group>
}

function PoseTemplateFootprint({ entity, onSelect }: { entity: CellEntity; onSelect: () => void }) {
  const page = entity.geometry.page as { width_mm?: number; height_mm?: number } | undefined
  const pageConfiguration = entity.geometry.page_configuration as { origin_from_lower_left_mm?: number[] } | undefined
  const width = Number(page?.width_mm || 420)
  const height = Number(page?.height_mm || 297)
  const origin = pageConfiguration?.origin_from_lower_left_mm ?? [15, 15]
  const contourGeometry = entity.geometry.contours
  const shapes = useMemo(() => (Array.isArray(contourGeometry) ? contourGeometry : []).flatMap((raw) => {
    const item = raw as { instance_uuid?: string; contours?: Array<Array<{ x_mm?: number; y_mm?: number }>> }
    return (item.contours ?? []).flatMap((points, index) => {
      const finite = points.map((point) => [Number(point.x_mm), Number(point.y_mm)] as const).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
      if (finite.length < 3) return []
      const shape = new Shape()
      shape.moveTo(finite[0][0], finite[0][1])
      finite.slice(1).forEach(([x, y]) => shape.lineTo(x, y))
      shape.closePath()
      return [{ key: `${item.instance_uuid ?? "instance"}-${index}`, shape }]
    })
  }), [contourGeometry])
  return <group onClick={(event) => selectEvent(event, onSelect)}>
    <mesh position={[width / 2 - Number(origin[0] || 0), height / 2 - Number(origin[1] || 0), -PRINT_SURFACE_THICKNESS_MM / 2]} receiveShadow>
      <boxGeometry args={[width, height, PRINT_SURFACE_THICKNESS_MM]} />
      <meshStandardMaterial color="#f8fafc" roughness={0.95} />
    </mesh>
    {shapes.map(({ key, shape }) => <mesh key={key} position={[0, 0, PRINT_LAYER_GAP_MM]}>
      <shapeGeometry args={[shape]} />
      <meshBasicMaterial color="#a3b51d" transparent opacity={0.58} side={DoubleSide} depthWrite={false} polygonOffset polygonOffsetFactor={-1} polygonOffsetUnits={-1} />
    </mesh>)}
    <mesh position={[0, 0, PRINT_LAYER_GAP_MM + 0.3]}><circleGeometry args={[3, 24]} /><meshBasicMaterial color="#2374d8" side={DoubleSide} /></mesh>
  </group>
}

function EntityVisual({ entity, onSelect }: { entity: CellEntity; onSelect: () => void }) {
  const color = statusColor(entity.status)
  const kind = String(entity.geometry.kind || entity.type)
  if (kind === "axes") return <axesHelper args={[Number(entity.geometry.size_mm || 100)]} onClick={(event) => selectEvent(event, onSelect)} />
  if (kind === "svg_plane") return <TemplatePlane entity={entity} onSelect={onSelect} />
  if (kind === "mesh") return <MeshBoundary fallback={<mesh onClick={(event) => selectEvent(event, onSelect)}><boxGeometry args={[30, 30, 30]} /><meshStandardMaterial color="#f59e0b" wireframe /></mesh>}><Suspense fallback={null}><PlyMesh entity={entity} onSelect={onSelect} /></Suspense></MeshBoundary>
  if (kind === "camera_frustum") return <CameraGeometry color={color} onSelect={onSelect} geometry={entity.geometry} />
  if (kind === "calibration_target") return <TargetGeometry entity={entity} color={color} onSelect={onSelect} />
  if (kind === "pose_template_footprint") return <PoseTemplateFootprint entity={entity} onSelect={onSelect} />
  if (kind === "robot_base") return <mesh position={[0, 0, 45]} rotation={[Math.PI / 2, 0, 0]} onClick={(event) => selectEvent(event, onSelect)}><cylinderGeometry args={[85, 105, 90, 32]} /><meshStandardMaterial color={color} roughness={0.55} /></mesh>
  if (kind === "flange_proxy") return <group name="robot-flange-proxy" onClick={(event) => selectEvent(event, onSelect)}>
    <mesh name="robot-flange-body" position={[0, 0, -15]} rotation={[Math.PI / 2, 0, 0]} castShadow receiveShadow>
      <cylinderGeometry args={[42, 42, 30, 32]} />
      <meshStandardMaterial color={color} roughness={0.48} metalness={0.2} />
    </mesh>
    <mesh name="robot-flange-face" position={[0, 0, 0.5]}>
      <torusGeometry args={[31, 3, 8, 32]} />
      <meshStandardMaterial color="#dbe5ef" roughness={0.35} metalness={0.45} />
    </mesh>
    <axesHelper name="robot-flange-coordinate-frame" args={[90]} />
  </group>
  if (kind === "tcp") return <group onClick={(event) => selectEvent(event, onSelect)}><axesHelper args={[70]} /><mesh position={[0, 0, 25]} rotation={[Math.PI / 2, 0, 0]}><cylinderGeometry args={[3, 8, 50, 12]} /><meshStandardMaterial color={color} /></mesh></group>
  return null
}

function EntityTree({ entity, childrenByParent, visible, pose, onSelect }: { entity: CellEntity; childrenByParent: Map<string | null, CellEntity[]>; visible: Set<string>; pose: CellPose | null; onSelect: (entity: CellEntity) => void }) {
  const transform = entity.id === "robot_flange" && pose ? pose.transform : entity.transform
  if (!transform) return null
  const children = childrenByParent.get(entity.id) ?? []
  return <group name={entity.id} {...transformProps(transform)}>
    {visible.has(entity.type) && <EntityVisual entity={entity} onSelect={() => onSelect(entity)} />}
    {children.map((child) => <EntityTree key={child.id} entity={child} childrenByParent={childrenByParent} visible={visible} pose={pose} onSelect={onSelect} />)}
  </group>
}

function Trajectory({ poses }: { poses: CellPose[] }) {
  const line = useMemo(() => {
    const points = poses.map((pose) => new Vector3(...pose.transform.translation_mm))
    const geometry = new BufferGeometry().setFromPoints(points)
    return new Line(geometry, new LineBasicMaterial({ color: new Color("#a855f7") }))
  }, [poses])
  if (poses.length < 2) return null
  // This connects exact sampled positions visually; it never creates playback poses.
  return <primitive object={line} />
}

function CameraRig({ preset, resetToken }: { preset: Preset; resetToken: number }) {
  const { camera, invalidate } = useThree()
  const controls = useRef<React.ElementRef<typeof OrbitControls>>(null)
  useEffect(() => {
    camera.up.set(0, 0, 1)
    const positions: Record<Preset, [number, number, number]> = {
      perspective: [650, -700, 520],
      top: [0, 0, 1050],
      front: [0, -1050, 180],
    }
    camera.position.set(...positions[preset])
    controls.current?.target.set(0, 0, 80)
    controls.current?.update()
    invalidate()
  }, [camera, invalidate, preset, resetToken])
  return <OrbitControls ref={controls} makeDefault enablePan enableRotate enableZoom dampingFactor={0.08} />
}

function ReferenceGrid() {
  const gridZ = -REFERENCE_GRID_CLEARANCE_MM
  return <group name="raised-reference-grid">
    <mesh name="reference-grid-platform" position={[0, 0, gridZ - REFERENCE_GRID_THICKNESS_MM / 2]} receiveShadow>
      <boxGeometry args={[REFERENCE_GRID_SIZE_MM, REFERENCE_GRID_SIZE_MM, REFERENCE_GRID_THICKNESS_MM]} />
      <meshStandardMaterial color="#0b1727" roughness={0.96} />
    </mesh>
    <gridHelper name="reference-grid-lines" args={[REFERENCE_GRID_SIZE_MM, REFERENCE_GRID_DIVISIONS, "#475569", "#253247"]} position={[0, 0, gridZ + 0.15]} rotation={[Math.PI / 2, 0, 0]} />
  </group>
}

function CellCanvas({ scene, visible, pose, trajectory, selected, onSelect, preset, resetToken }: { scene: CellScene; visible: Set<string>; pose: CellPose | null; trajectory: boolean; selected: CellEntity | null; onSelect: (entity: CellEntity) => void; preset: Preset; resetToken: number }) {
  const presentation = cellPresentation(scene)
  const trajectoryMetadata = cellTrajectory(scene)
  const children = useMemo(() => {
    const map = new Map<string | null, CellEntity[]>()
    for (const entity of scene.entities) {
      if (!entity.transform) continue
      const parent = entity.transform.parent_frame
      map.set(parent, [...(map.get(parent) ?? []), entity])
    }
    return map
  }, [scene.entities])
  const roots = children.get(null) ?? []
  return <Canvas data-testid="cell-webgl-canvas" data-presentation-mode={presentation.mode} data-presentation-quaternion={presentation.transform.rotation_quaternion_wxyz.join(",")} data-reference-grid-clearance-mm={REFERENCE_GRID_CLEARANCE_MM} data-trajectory-entity-id={trajectoryMetadata.entity_id} data-trajectory-reference-frame={trajectoryMetadata.reference_frame} frameloop="demand" shadows camera={{ position: [650, -700, 520], near: 1, far: 10000, fov: 42, up: [0, 0, 1] }} onPointerMissed={() => selected && onSelect(selected)}>
    <color attach="background" args={["#08111f"]} />
    <ambientLight intensity={0.75} />
    <directionalLight position={[350, -250, 700]} intensity={1.8} castShadow />
    <ReferenceGrid />
    <group {...transformProps(presentation.transform)}>
      {roots.map((entity) => <EntityTree key={entity.id} entity={entity} childrenByParent={children} visible={visible} pose={pose} onSelect={onSelect} />)}
      {trajectory && <Trajectory poses={scene.trajectory_preview} />}
    </group>
    <CameraRig preset={preset} resetToken={resetToken} />
  </Canvas>
}

function CameraModalityPanel({ selectedRun, timeline, pose, loading, modality, outOfRange }: { selectedRun: string; timeline: CellTimelineMetadata; pose: CellPose | null; loading: boolean; modality: CameraFrameModality; outOfRange: boolean }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const camera = timeline.camera
  const evidence = timeline.camera_frames[modality]
  const rotation = camera?.image_presentation.display_rotation_degrees ?? 0
  const modalityLabel = modality === "rgb" ? "RGB" : "Depth"
  const frameUrl = pose && evidence.available ? query("/ui/cell-scene/camera-frame", {
    run_root: selectedRun,
    timeline_id: timeline.id,
    frame_id: pose.frame_id,
    modality,
  }) : null
  const failed = frameUrl !== null && failedUrl === frameUrl

  return <section data-testid="cell-camera-frame-panel" data-camera-id={timeline.id} data-modality={modality} className="min-w-0 border-t border-white/10 first:border-t-0">
    <div className="flex items-center justify-between gap-2 px-4 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-300">{modalityLabel}</div>
      <StatusBadge status={evidence.available ? "recorded" : "unavailable"} tone={evidence.available ? "success" : "neutral"}>{evidence.available ? "Available" : "Unavailable"}</StatusBadge>
    </div>
    <div className="grid aspect-[4/3] max-h-[420px] w-full place-items-center overflow-hidden bg-black/25 p-3" aria-live="polite">
      {frameUrl && !failed
        ? <img key={frameUrl} data-testid="cell-camera-frame-image" data-modality={modality} data-display-rotation-degrees={rotation} src={frameUrl} alt={`${camera?.display_name ?? timeline.label} ${modalityLabel} frame ${pose?.frame_id ?? ""}`} className="max-h-full max-w-full object-contain" style={rotation === 180 ? { transform: "rotate(180deg)" } : undefined} decoding="async" onError={() => setFailedUrl(frameUrl)} />
        : <div className="max-w-xs text-center"><Camera aria-hidden="true" className="mx-auto mb-3 size-8 text-slate-500" /><div className="text-sm font-semibold">{!evidence.available ? `${modalityLabel} data unavailable` : outOfRange ? "No frame at this shared index" : loading ? `Loading exact ${modalityLabel} frame…` : failed ? `${modalityLabel} frame unavailable` : "Select a recorded frame"}</div>{failed && <p className="mt-2 text-xs leading-relaxed text-slate-400">This exact timeline entry has no matching {modalityLabel} PNG. Move the slider to inspect another retained frame.</p>}</div>}
    </div>
    {modality === "depth" && <div data-testid="cell-depth-legend" className="border-t border-white/10 px-4 py-2 text-[10px] text-slate-400"><div className="mb-1 h-1.5 rounded-full bg-[linear-gradient(90deg,#b40426,#fdae61,#66c2a5,#3288bd,#30123b)]" /><div className="flex justify-between"><span>Near · {timeline.camera_frames.depth.preview_min_depth_mm.toFixed(0)} mm</span><span>Far · {timeline.camera_frames.depth.preview_max_depth_mm.toFixed(0)} mm</span></div><div className="mt-1">Fixed-range colour preview; zero or invalid depth is black. The retained uint16 depth PNG is unchanged.</div></div>}
  </section>
}

function CameraTimelineColumn({ timeline, selectedRun, frame, modalities }: { timeline: CellTimelineMetadata; selectedRun: string; frame: number; modalities: CameraFrameModality[] }) {
  const offset = Math.floor(frame / PAGE_SIZE) * PAGE_SIZE
  const outOfRange = frame >= timeline.frame_count
  const available = modalities.some((modality) => timeline.camera_frames[modality].available)
  const timelineQuery = useQuery({
    queryKey: ["cell-timeline", selectedRun, timeline.id, offset],
    queryFn: () => api<CellTimelinePage>(query("/ui/cell-scene/timeline", { run_root: selectedRun, timeline_id: timeline.id, offset, limit: PAGE_SIZE })),
    enabled: available && !outOfRange,
  })
  const pose = timelineQuery.data?.poses.find((item) => item.index === frame) ?? null
  const camera = timeline.camera
  return <article data-testid="cell-camera-column" data-camera-id={timeline.id} className="min-w-0 overflow-hidden rounded-lg border border-slate-800 bg-slate-950 text-slate-100">
    <header className="flex items-start justify-between gap-3 px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-semibold"><Camera aria-hidden="true" className="size-4 shrink-0 text-sky-400" />{camera?.display_name ?? timeline.label}</div>
        <div className="mt-1 truncate font-mono text-[10px] text-slate-400">{camera ? `${camera.sensor_folder} · ${titleCase(camera.sensor_type)} · ${titleCase(camera.mounting_mode)}` : timeline.label}</div>
      </div>
      <div className="shrink-0 text-right text-[10px] text-slate-400">{timeline.frame_count} matched<br />frames</div>
    </header>
    <div data-testid="cell-camera-orientation" data-inverted={camera?.inverted ? "true" : "false"} className={camera?.inverted ? "border-t border-amber-400/20 bg-amber-400/10 px-4 py-2 text-[10px] text-amber-200" : "border-t border-white/10 bg-white/[0.025] px-4 py-2 text-[10px] text-slate-400"}>{camera?.inverted ? `Inverted mount · ${camera.image_presentation.correction === "capture" ? "stored frame already corrected by 180° at capture" : "rotated 180° for display in Cell"}` : "Normal mount · stored frame shown directly"}</div>
    <div>{modalities.map((modality) => <CameraModalityPanel key={modality} selectedRun={selectedRun} timeline={timeline} pose={pose} loading={timelineQuery.isFetching} modality={modality} outOfRange={outOfRange} />)}</div>
    <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-white/10 px-4 py-3 text-[11px] text-slate-400">
      <div className="truncate font-mono text-slate-200">{pose?.frame_id ?? "No exact frame loaded"}</div>
      <div>{pose ? `Shared index ${pose.index + 1} · camera frame ${pose.index + 1} of ${timeline.frame_count}` : outOfRange ? `Shared index ${frame + 1} exceeds this camera's ${timeline.frame_count} frames` : "Waiting for timeline"}</div>
      <div className="w-full">Exact per-camera matched frame · no interpolation</div>
    </footer>
  </article>
}

function CameraFramesSection({ timelines, selectedTimelineIds, selectedRun, frame, viewMode, open, onToggle, onToggleTimeline, onSelectAll, onClearSelection, onViewModeChange }: { timelines: CellTimelineMetadata[]; selectedTimelineIds: Set<string>; selectedRun: string; frame: number; viewMode: CameraFrameViewMode; open: boolean; onToggle: () => void; onToggleTimeline: (timelineId: string) => void; onSelectAll: () => void; onClearSelection: () => void; onViewModeChange: (mode: CameraFrameViewMode) => void }) {
  const selectedTimelines = timelines.filter((item) => selectedTimelineIds.has(item.id))
  const modalities: CameraFrameModality[] = viewMode === "rgb_depth" ? ["rgb", "depth"] : [viewMode]
  const rgbCount = timelines.filter((item) => item.camera_frames.rgb.available).length
  const depthCount = timelines.filter((item) => item.camera_frames.depth.available).length
  const frameGridColumns = selectedTimelines.length > 2 ? "md:grid-cols-2 2xl:grid-cols-3" : selectedTimelines.length === 2 ? "md:grid-cols-2" : ""
  return <Card data-testid="cell-camera-frames">
    <CardHeader>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0"><CardTitle className="flex items-center gap-2 text-base"><Camera aria-hidden="true" className="size-4" />Camera evidence</CardTitle><CardDescription className="mt-1">{timelines.length} camera{timelines.length === 1 ? "" : "s"} retain image data. Select any combination for side-by-side RGB or colourized metric-depth inspection.</CardDescription></div>
        <Button type="button" size="sm" variant={open ? "default" : "outline"} aria-expanded={open} aria-controls="cell-camera-frame-content" onClick={onToggle}>{open ? "Hide frames" : "Show frames"}</Button>
      </div>
    </CardHeader>
    {open && <CardContent id="cell-camera-frame-content" className="space-y-4 border-t pt-4">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto]">
        <div><div className="flex items-center justify-between gap-3"><Label className="text-xs font-semibold">Cameras to compare · {selectedTimelines.length} selected</Label><div className="flex gap-1"><Button type="button" size="sm" variant="ghost" onClick={onSelectAll}>All</Button><Button type="button" size="sm" variant="ghost" onClick={onClearSelection}>None</Button></div></div><div className="mt-2 grid gap-2 md:grid-cols-2 xl:grid-cols-3">{timelines.map((item) => {
          const camera = item.camera
          const selected = selectedTimelineIds.has(item.id)
          const modalitiesAvailable = [item.camera_frames.rgb.available && "RGB", item.camera_frames.depth.available && "depth"].filter(Boolean).join(" + ")
          return <Label key={item.id} className="flex min-w-0 cursor-pointer items-start gap-2 rounded border p-3 text-xs"><Checkbox checked={selected} onCheckedChange={() => onToggleTimeline(item.id)} aria-label={`Show ${camera?.display_name ?? item.label}`} /><span className="min-w-0"><span className="block truncate font-semibold">{camera?.display_name ?? item.label}</span><span className="mt-0.5 block truncate font-mono text-[9px] text-muted-foreground">{camera?.sensor_folder ?? item.label} · {item.frame_count} frames</span><span className="mt-1 block text-[9px] text-muted-foreground">{modalitiesAvailable || "No displayable data"}{camera?.inverted ? " · inverted mount" : ""}</span></span></Label>
        })}</div></div>
        <div><Label className="text-xs font-semibold">Data shown</Label><div className="mt-2 flex flex-wrap gap-2" role="group" aria-label="Camera data shown">{([["rgb", "RGB", rgbCount], ["depth", "Depth", depthCount], ["rgb_depth", "RGB + depth", Math.max(rgbCount, depthCount)]] as const).map(([value, label, count]) => <Button key={value} type="button" size="sm" variant={viewMode === value ? "default" : "outline"} aria-label={`Show ${label}`} aria-pressed={viewMode === value} disabled={count === 0} onClick={() => onViewModeChange(value)}>{label}<span className="rounded bg-black/10 px-1.5 font-mono text-[9px]">{count}</span></Button>)}</div></div>
      </div>
      <div className="rounded border border-sky-500/25 bg-sky-500/5 px-3 py-2 text-[10px] leading-relaxed text-muted-foreground">The shared slider applies the same ordinal to each selected camera&apos;s own timestamp-matched timeline. Side-by-side display does not claim simultaneous exposure.</div>
      {selectedTimelines.length > 0
        ? <div data-testid="cell-camera-frame-grid" className={`grid items-start gap-4 ${frameGridColumns}`}>{selectedTimelines.map((timeline) => <CameraTimelineColumn key={timeline.id} timeline={timeline} selectedRun={selectedRun} frame={frame} modalities={modalities} />)}</div>
        : <div className="rounded-lg border border-dashed px-5 py-10 text-center text-sm text-muted-foreground">Select at least one camera above to show retained frame evidence.</div>}
    </CardContent>}
  </Card>
}

export function CellPage() {
  const { currentWorkflow, selectedRun } = useOperator()
  const sceneQuery = useQuery({ queryKey: ["cell-scene", selectedRun], queryFn: () => api<CellScene>(query("/ui/cell-scene", { run_root: selectedRun })) })
  if (sceneQuery.isPending) return <div className="grid min-h-[60vh] place-items-center text-sm text-muted-foreground">Building cell scene…</div>
  if (sceneQuery.isError || !sceneQuery.data) return <Card className="border-destructive/40"><CardHeader><CardTitle>Cell scene unavailable</CardTitle><CardDescription>{sceneQuery.error instanceof Error ? sceneQuery.error.message : "The selected run could not be composed."}</CardDescription></CardHeader></Card>
  const workflowHref = currentWorkflow ? activeWorkflowHref(currentWorkflow) : "/workflow/setup"
  return <CellSceneView key={`${selectedRun}:${sceneQuery.data.default_timeline_id ?? "none"}`} selectedRun={selectedRun} scene={sceneQuery.data} workflowHref={workflowHref} />
}

function CellSceneView({ selectedRun, scene, workflowHref }: { selectedRun: string; scene: CellScene; workflowHref: string }) {
  const client = useQueryClient()
  const [timelineId, setTimelineId] = useState(scene.default_timeline_id ?? "")
  const [frame, setFrame] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [trajectory, setTrajectory] = useState(true)
  const [showCameraFrames, setShowCameraFrames] = useState(false)
  const [cameraViewMode, setCameraViewMode] = useState<CameraFrameViewMode>("rgb")
  const [selectedCameraTimelineIds, setSelectedCameraTimelineIds] = useState<Set<string>>(() => {
    const initial = scene.timelines.find((item) => item.id === scene.default_timeline_id && item.camera_frames.available)
      ?? scene.timelines.find((item) => item.camera_frames.available)
    return new Set(initial ? [initial.id] : [])
  })
  const [visible, setVisible] = useState(() => new Set(LAYERS))
  const [selected, setSelected] = useState<CellEntity | null>(null)
  const [preset, setPreset] = useState<Preset>("perspective")
  const [resetToken, setResetToken] = useState(0)
  const [webgl] = useState(() => hasWebGL())
  const presentation = cellPresentation(scene)
  const targetAligned = presentation.mode === "calibration_target_front"
  const trajectoryMetadata = cellTrajectory(scene)
  const legacySceneResponse = scene.trajectory === undefined
  const trajectoryLabel = `${trajectoryMetadata.label} trajectory · ${trajectoryMetadata.reference_frame_label}`

  const timeline = scene?.timelines.find((item) => item.id === timelineId)
  const cameraTimelines = scene.timelines.filter((item) => item.camera_frames?.available === true)
  const offset = Math.floor(frame / PAGE_SIZE) * PAGE_SIZE
  const timelineQuery = useQuery({
    queryKey: ["cell-timeline", selectedRun, timelineId, offset],
    queryFn: () => api<CellTimelinePage>(query("/ui/cell-scene/timeline", { run_root: selectedRun, timeline_id: timelineId, offset, limit: PAGE_SIZE })),
    enabled: Boolean(timelineId),
  })
  const page = timelineQuery.data
  const pose = page?.poses.find((item) => item.index === frame) ?? null

  useEffect(() => {
    if (!page || !timelineId) return
    for (const adjacent of [page.previous_offset, page.next_offset]) {
      if (adjacent === null) continue
      void client.prefetchQuery({ queryKey: ["cell-timeline", selectedRun, timelineId, adjacent], queryFn: () => api<CellTimelinePage>(query("/ui/cell-scene/timeline", { run_root: selectedRun, timeline_id: timelineId, offset: adjacent, limit: PAGE_SIZE })) })
    }
  }, [client, page, selectedRun, timelineId])

  useEffect(() => {
    if (!playing || !timeline) return
    const timer = window.setInterval(() => setFrame((value) => value + 1 >= timeline.frame_count ? 0 : value + 1), 150)
    return () => window.clearInterval(timer)
  }, [playing, timeline])

  const toggleLayer = (layer: string) => setVisible((current) => { const next = new Set(current); if (next.has(layer)) next.delete(layer); else next.add(layer); return next })
  const selectTimeline = (value: string) => {
    setTimelineId(value)
    setFrame(0)
    setPlaying(false)
  }
  const toggleCameraFrames = () => {
    if (showCameraFrames) {
      setShowCameraFrames(false)
      return
    }
    if (selectedCameraTimelineIds.size === 0 && cameraTimelines.length > 0) {
      setSelectedCameraTimelineIds(new Set([cameraTimelines[0].id]))
    }
    setShowCameraFrames(true)
  }
  const toggleCameraTimeline = (value: string) => setSelectedCameraTimelineIds((current) => {
    const next = new Set(current)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    return next
  })
  const selectAllCameraTimelines = () => setSelectedCameraTimelineIds(new Set(cameraTimelines.map((item) => item.id)))
  const clearCameraTimelines = () => setSelectedCameraTimelineIds(new Set())
  const unresolved = scene.entities.filter((entity) => entity.status === "unresolved")
  const unresolvedCameras = unresolved.filter((entity) => entity.type === "camera")
  const otherIssues = scene.warnings.filter((warning) => warning.code !== "missing_calibration_profiles")
    .map((warning) => warning.message)
    .concat(unresolved.filter((entity) => entity.type !== "camera").map((entity) => `${entity.label}: ${entity.unresolved_reason}`))

  return <div className="space-y-5">
    <PageHeader eyebrow="Dataset contents" title="Cell View" description="Read-only inspection of cell geometry, exact flange poses, and retained synchronized RGB-D evidence." />
    <ProcessHandoff title="Inspect evidence here; change it in Workflow" description="Cell never edits transforms, images, or depth data. Use it to compare geometry and retained evidence, then return to the guided workflow to resolve missing calibration, capture, synchronization, or export evidence." to={workflowHref} action="Open workflow" />
    {legacySceneResponse && <div data-testid="cell-scene-version-warning" className="flex items-start gap-3 rounded-lg border border-amber-500/35 bg-amber-500/8 p-4 text-sm"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" /><div><div className="font-semibold">Cell backend restart required</div><p className="mt-1 text-xs text-muted-foreground">This response predates target-trajectory metadata. Cell View remains usable and shows the recorded robot-flange path; restart the PoseTestBot web service to load the PoseTemplateBase calibration-target trajectory.</p></div></div>}
    {(scene.warnings.length > 0 || unresolved.length > 0) && <div className="flex items-start gap-3 rounded-lg border border-amber-500/35 bg-amber-500/8 p-4 text-sm"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" /><div className="min-w-0"><div className="font-semibold">Partial cell scene</div><div className="mt-1 text-xs text-muted-foreground">{unresolvedCameras.length > 0 ? `${unresolvedCameras.length} camera${unresolvedCameras.length === 1 ? " is" : "s are"} hidden until this run has matching promoted calibration profiles. The recorded trajectory and available template evidence remain visible.` : "Available scene evidence remains visible."}</div>{otherIssues.length > 0 && <details className="mt-2"><summary className="cursor-pointer text-xs font-medium">Show {otherIssues.length} provenance detail{otherIssues.length === 1 ? "" : "s"}</summary><ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-muted-foreground">{otherIssues.map((message, index) => <li key={index}>{message}</li>)}</ul></details>}</div></div>}
    <Card className="overflow-hidden"><CardHeader className="border-b"><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle>3D cell</CardTitle><CardDescription data-testid="cell-coordinate-convention" className="flex flex-wrap items-center gap-1">{targetAligned ? "Target-aligned · right-handed · origin top-left · +X right · +Y down · +Z into grid · millimetres" : "PoseTemplateBase · right-handed · +Z up · millimetres"} · no pose interpolation · <AxisLegend /> <HelpTip label="cell coordinate and timeline display">Transforms are rendered from stored evidence in millimetres. Local coordinate frames use red X, green Y, and blue Z axes. For static-camera calibration, the fixed camera and the robot-carried target trajectory stay in PoseTemplateBase; the target path composes its promoted grid-to-flange attachment with each exact recorded flange pose. Timeline playback never interpolates or invents a robot pose between frames.</HelpTip></CardDescription></div><div className="flex flex-wrap gap-2"><Button size="sm" variant={preset === "perspective" ? "default" : "outline"} onClick={() => setPreset("perspective")}><Focus />Perspective</Button><Button size="sm" variant={preset === "top" ? "default" : "outline"} onClick={() => setPreset("top")}><ScanLine />Top</Button><Button size="sm" variant={preset === "front" ? "default" : "outline"} onClick={() => setPreset("front")}><Crosshair />Front</Button><Button size="sm" variant="outline" aria-label="Reset cell view" onClick={() => { setPreset("perspective"); setResetToken((value) => value + 1) }}><RotateCcw /></Button></div></div></CardHeader>
      <CardContent className="p-0">{webgl ? <div className="h-[620px] min-w-0"><CellCanvas scene={scene} visible={visible} pose={pose} trajectory={trajectory} selected={selected} onSelect={setSelected} preset={preset} resetToken={resetToken} /></div> : <div data-testid="cell-webgl-fallback" className="grid h-[620px] min-w-0 place-items-center p-10 text-center"><div><AlertTriangle className="mx-auto mb-3 size-8 text-amber-500" /><div className="font-semibold">WebGL is unavailable</div><p className="mt-2 max-w-md text-sm text-muted-foreground">The component and provenance list remains available. Use a browser with WebGL support to orbit the scene.</p></div></div>}</CardContent>
      <div className="space-y-3 border-t bg-muted/20 p-4"><div className="flex flex-wrap items-end gap-3"><div className="w-[310px] shrink-0 space-y-1"><Label className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">3D pose timeline source</Label><Select value={timelineId || "none"} onValueChange={(value) => selectTimeline(value === "none" ? "" : value)}><SelectTrigger aria-label="Timeline"><SelectValue placeholder="No trajectory" /></SelectTrigger><SelectContent>{scene.timelines.length ? scene.timelines.map((item) => <SelectItem key={item.id} value={item.id}>{item.camera ? `${item.camera.display_name} · ${item.camera.sensor_folder}` : item.label} · {item.frame_count} frames</SelectItem>) : <SelectItem value="none">No trajectory</SelectItem>}</SelectContent></Select></div><Button size="icon" variant="outline" aria-label={playing ? "Pause timeline" : "Play timeline"} disabled={!timeline} onClick={() => setPlaying((value) => !value)}>{playing ? <CirclePause /> : <CirclePlay />}</Button><div className="min-w-[280px] flex-1 pb-2"><input aria-label="Frame scrubber" className="w-full accent-primary" type="range" min={0} max={Math.max(0, (timeline?.frame_count ?? 1) - 1)} value={frame} disabled={!timeline} onChange={(event) => { setFrame(Number(event.target.value)); setPlaying(false) }} /></div><div className="w-28 pb-2 text-right font-mono text-xs">{timeline ? `${frame + 1} / ${timeline.frame_count}` : "No frames"}</div></div><div className="flex flex-wrap justify-between gap-2 text-[11px] text-muted-foreground"><span>{pose ? `Exact 3D pose frame ${pose.frame_id}${pose.motion ? ` · ${pose.motion}` : ""}` : timelineQuery.isFetching ? "Loading exact pose page…" : "Select a recorded frame"}</span><span>{showCameraFrames ? "Selected RGB-D tiles below use this shared ordinal" : "Adjacent pose pages prefetch automatically"}</span></div></div>
    </Card>
    {cameraTimelines.length > 0 && <CameraFramesSection timelines={cameraTimelines} selectedTimelineIds={selectedCameraTimelineIds} selectedRun={selectedRun} frame={frame} viewMode={cameraViewMode} open={showCameraFrames} onToggle={toggleCameraFrames} onToggleTimeline={toggleCameraTimeline} onSelectAll={selectAllCameraTimelines} onClearSelection={clearCameraTimelines} onViewModeChange={setCameraViewMode} />}
    <div className="grid items-start gap-5 xl:grid-cols-[300px_minmax(0,1fr)_340px]">
      <Card><CardHeader><CardTitle className="text-base">Scene visibility</CardTitle><CardDescription>Choose which geometry remains visible in the 3D scene above.</CardDescription></CardHeader><CardContent className="grid grid-cols-2 gap-2">{LAYERS.map((layer) => <Label key={layer} className="flex items-center gap-2 rounded border p-2 text-xs"><Checkbox checked={visible.has(layer)} onCheckedChange={() => toggleLayer(layer)} />{titleCase(layer)}</Label>)}<Label data-testid="cell-trajectory-control" className="col-span-2 flex items-center gap-2 rounded border p-2 text-xs"><Checkbox checked={trajectory} onCheckedChange={(value) => setTrajectory(value === true)} /><Route className="size-3.5" />{trajectoryLabel}</Label></CardContent></Card>
      <Card><CardHeader><CardTitle className="text-base">Selection evidence</CardTitle><CardDescription>Click geometry above or choose a recorded component at right.</CardDescription></CardHeader><CardContent><SelectionDetails entity={selected} /></CardContent></Card>
      <Card><CardHeader><CardTitle className="text-base">Recorded components</CardTitle><CardDescription>{scene.object_selection.objectless ? "Objectless RGB-D run" : `${scene.object_selection.instance_count} pose-template instance(s)`}</CardDescription></CardHeader><CardContent className="max-h-[520px] space-y-2 overflow-auto">{scene.entities.map((entity) => <button key={entity.id} type="button" className="flex w-full items-center gap-2 rounded border px-3 py-2 text-left text-xs hover:bg-muted" onClick={() => setSelected(entity)}>{entity.type === "camera" ? <Camera className="size-3.5" /> : entity.status === "unresolved" ? <EyeOff className="size-3.5 text-destructive" /> : <Eye className="size-3.5" />}<span className="min-w-0 flex-1 truncate">{entity.label}</span><StatusBadge status={entity.status} tone={entityStatusTone(entity.status)} /></button>)}</CardContent></Card>
    </div>
  </div>
}
