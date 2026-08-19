import { Component, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { Bounds, Html, OrbitControls, useBounds } from "@react-three/drei"
import { Canvas, useLoader } from "@react-three/fiber"
import { Box, Grid3X3, MousePointer2, ScanSearch } from "lucide-react"
import { Box3, BufferGeometry, DoubleSide, Float32BufferAttribute, Group, Matrix4, Vector3 } from "three"
import { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js"
import type { Matrix4x4, PoseTemplateBundle, PoseTemplatePreview, PoseTemplatePreviewMesh } from "@/lib/contracts"
import { cn } from "@/lib/utils"

function hasWebGLSupport() {
  if (typeof document === "undefined") return false
  try {
    const canvas = document.createElement("canvas")
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"))
  } catch {
    return false
  }
}

class SceneBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}

class MeshBoundary extends Component<{
  children: ReactNode
  fallback: ReactNode
  onFailure: (message: string) => void
}, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: unknown) {
    this.props.onFailure(error instanceof Error ? error.message : "Exact PLY could not be loaded")
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}

function threeMatrix(value: Matrix4x4) {
  return new Matrix4().set(
    value[0][0], value[0][1], value[0][2], value[0][3],
    value[1][0], value[1][1], value[1][2], value[1][3],
    value[2][0], value[2][1], value[2][2], value[2][3],
    value[3][0], value[3][1], value[3][2], value[3][3],
  )
}

function meshLabelPosition(previewMesh: PoseTemplatePreviewMesh): [number, number, number] {
  const bounds = new Box3()
  previewMesh.vertices.forEach((vertex) => bounds.expandByPoint(new Vector3(...vertex)))
  if (bounds.isEmpty()) return [0, 0, 8]
  const center = bounds.getCenter(new Vector3())
  const size = bounds.getSize(new Vector3())
  return [center.x, center.y, bounds.max.z + Math.max(size.x, size.y, size.z) * .14]
}

function FallbackMesh({ previewMesh, color, selected, onSelect }: {
  previewMesh: PoseTemplatePreviewMesh
  color: string
  selected: boolean
  onSelect: () => void
}) {
  const geometry = useMemo(() => {
    const result = new BufferGeometry()
    result.setAttribute("position", new Float32BufferAttribute(previewMesh.vertices.flat(), 3))
    result.setIndex(previewMesh.faces.flat())
    result.computeVertexNormals()
    result.computeBoundingSphere()
    return result
  }, [previewMesh])

  useEffect(() => () => geometry.dispose(), [geometry])

  return <mesh
    geometry={geometry}
    castShadow
    receiveShadow
    onClick={(event) => { event.stopPropagation(); onSelect() }}
  >
    <meshStandardMaterial
      color={color}
      emissive={selected ? color : "#000000"}
      emissiveIntensity={selected ? .18 : 0}
      roughness={.68}
      metalness={.04}
      side={DoubleSide}
    />
  </mesh>
}

interface ExactMeshDetail {
  status: "exact" | "fallback"
  vertices?: number
  faces?: number
  dimensions?: [number, number, number]
  error?: string
}

function ExactImmutableMesh({ url, color, selected, onSelect, onDetail }: {
  url: string
  color: string
  selected: boolean
  onSelect: () => void
  onDetail: (detail: ExactMeshDetail) => void
}) {
  const geometry = useLoader(PLYLoader, url)
  const detail = useMemo<ExactMeshDetail>(() => {
    geometry.computeBoundingBox()
    geometry.computeBoundingSphere()
    if (!geometry.getAttribute("normal")) geometry.computeVertexNormals()
    const dimensions = geometry.boundingBox?.getSize(new Vector3()) ?? new Vector3()
    const positions = geometry.getAttribute("position")
    return {
      status: "exact",
      vertices: positions?.count ?? 0,
      faces: geometry.index ? geometry.index.count / 3 : (positions?.count ?? 0) / 3,
      dimensions: [dimensions.x, dimensions.y, dimensions.z],
    }
  }, [geometry])
  const vertexColors = Boolean(geometry.getAttribute("color"))

  useEffect(() => onDetail(detail), [detail, onDetail])
  useEffect(() => () => {
    geometry.dispose()
    useLoader.clear(PLYLoader, url)
  }, [geometry, url])

  return <mesh
    geometry={geometry}
    castShadow
    receiveShadow
    onClick={(event) => { event.stopPropagation(); onSelect() }}
  >
    <meshStandardMaterial
      color={vertexColors ? "white" : color}
      vertexColors={vertexColors}
      emissive={selected ? color : "#000000"}
      emissiveIntensity={selected ? .16 : 0}
      roughness={.62}
      metalness={.05}
      side={DoubleSide}
    />
  </mesh>
}

const COLORS = ["#a9bf36", "#45a6d1", "#df923f", "#ae7bc8", "#53ad7c", "#d96c6c", "#5f86d6", "#c5a441"]

type RenderableInstance = PoseTemplatePreview["instances"][number] & {
  transform: Matrix4x4
  previewMesh: PoseTemplatePreviewMesh
}

function exactMeshUrl(bundle: PoseTemplateBundle, instance: RenderableInstance) {
  const path = `/pose-templates/library/${encodeURIComponent(bundle.template_uuid)}/assets/${encodeURIComponent(instance.instance_uuid)}/canonical_ply`
  const sha256 = instance.catalog.canonical_ply_sha256
  return sha256 ? `${path}?sha256=${encodeURIComponent(sha256)}` : path
}

function ObjectInstance({ bundle, instance, index, color, selected, onSelect, onDetail }: {
  bundle: PoseTemplateBundle
  instance: RenderableInstance
  index: number
  color: string
  selected: boolean
  onSelect: () => void
  onDetail: (detail: ExactMeshDetail) => void
}) {
  const group = useRef<Group>(null)
  const bounds = useBounds()
  const matrix = useMemo(() => threeMatrix(instance.transform), [instance.transform])
  const labelPosition = useMemo(() => meshLabelPosition(instance.previewMesh), [instance.previewMesh])
  const fallback = <FallbackMesh previewMesh={instance.previewMesh} color={color} selected={selected} onSelect={onSelect} />

  useEffect(() => {
    if (selected && group.current) bounds.refresh(group.current).clip().fit()
  }, [bounds, selected])

  return <group ref={group} matrix={matrix} matrixAutoUpdate={false}>
    <MeshBoundary
      fallback={fallback}
      onFailure={(error) => onDetail({ status: "fallback", error })}
    >
      <Suspense fallback={fallback}>
        <ExactImmutableMesh
          url={exactMeshUrl(bundle, instance)}
          color={color}
          selected={selected}
          onSelect={onSelect}
          onDetail={onDetail}
        />
      </Suspense>
    </MeshBoundary>
    <Html position={labelPosition} center zIndexRange={[40, 0]} pointerEvents="none">
      <div
        className={cn(
          "grid size-6 place-items-center rounded-full border-2 border-white font-mono text-[9px] font-black text-white shadow-[0_2px_10px_rgba(0,0,0,.65)] transition-transform",
          selected && "scale-125 ring-2 ring-white/35",
        )}
        style={{ backgroundColor: color }}
        aria-hidden="true"
      >
        {index + 1}
      </div>
    </Html>
  </group>
}

type FocusTarget = "objects" | "sheet" | string

function ObjectCollection({ bundle, renderable, origin, focus, onFocus, onDetail }: {
  bundle: PoseTemplateBundle
  renderable: RenderableInstance[]
  origin: [number, number]
  focus: FocusTarget
  onFocus: (focus: FocusTarget) => void
  onDetail: (instanceUuid: string, detail: ExactMeshDetail) => void
}) {
  const group = useRef<Group>(null)
  const bounds = useBounds()

  useEffect(() => {
    if (focus === "objects" && group.current) bounds.refresh(group.current).clip().fit()
  }, [bounds, focus])

  return <group ref={group} position={[origin[0], origin[1], 0]}>
    {renderable.map((instance, index) => <ObjectInstance
      key={instance.instance_uuid}
      bundle={bundle}
      instance={instance}
      index={index}
      color={COLORS[index % COLORS.length]}
      selected={focus === instance.instance_uuid}
      onSelect={() => onFocus(instance.instance_uuid)}
      onDetail={(detail) => onDetail(instance.instance_uuid, detail)}
    />)}
  </group>
}

function SheetFocus({ active, width, height }: { active: boolean; width: number; height: number }) {
  const bounds = useBounds()
  useEffect(() => {
    if (!active) return
    bounds.refresh(new Box3(
      new Vector3(0, 0, -1),
      new Vector3(width, height, 1),
    )).clip().fit()
  }, [active, bounds, height, width])
  return null
}

function EmptyScene({ message }: { message: string }) {
  return <div className="grid size-full place-items-center bg-muted/35 p-6 text-center text-xs text-muted-foreground">
    <div><Box className="mx-auto mb-2 size-6" />{message}</div>
  </div>
}

function formatCount(value: number | undefined) {
  return value === undefined ? null : Math.round(value).toLocaleString()
}

function formatDimensions(value: [number, number, number] | undefined) {
  if (!value) return null
  return value.map((dimension) => dimension.toLocaleString(undefined, { maximumFractionDigits: 1 })).join(" × ")
}

export function TemplateScenePreview({ bundle, preview }: { bundle: PoseTemplateBundle; preview: PoseTemplatePreview }) {
  const [webgl] = useState(hasWebGLSupport)
  const [focus, setFocus] = useState<FocusTarget>("objects")
  const [meshDetails, setMeshDetails] = useState<Record<string, ExactMeshDetail>>({})
  const page = preview.page
  const origin = preview.configuration?.page?.origin_from_lower_left_mm
  const renderable = preview.instances.flatMap((instance) => {
    const transform = instance.pose_template_from_object?.matrix
    const meshHash = instance.preview_mesh_sha256
    const previewMesh = meshHash ? preview.preview_meshes?.[meshHash] : undefined
    return transform && previewMesh ? [{ ...instance, transform, previewMesh }] : []
  })
  const updateMeshDetail = useCallback((instanceUuid: string, detail: ExactMeshDetail) => {
    setMeshDetails((current) => {
      const previous = current[instanceUuid]
      if (
        previous?.status === detail.status
        && previous.vertices === detail.vertices
        && previous.faces === detail.faces
        && previous.error === detail.error
        && previous.dimensions?.every((value, index) => value === detail.dimensions?.[index])
      ) return current
      return { ...current, [instanceUuid]: detail }
    })
  }, [])

  if (!webgl) return <EmptyScene message="Interactive 3D is unavailable in this browser. The exact footprint preview remains available above." />
  if (!page || !origin) return <EmptyScene message="This template does not satisfy the current immutable-preview page contract." />
  if (!renderable.length) return <EmptyScene message="This template does not satisfy the current bounded-preview contract. Its exact footprint remains available above." />
  const width = page.width_mm
  const height = page.height_mm

  return <div
    className="grid size-full grid-cols-[minmax(0,1fr)_15rem] overflow-hidden bg-[#0b1218]"
    data-testid="selected-template-scene"
    data-origin-offset-mm={`${origin[0]},${origin[1]}`}
  >
    <div className="relative min-w-0 overflow-hidden">
      <SceneBoundary fallback={<EmptyScene message="The immutable 3D assets could not be displayed." />}>
        <Canvas
          aria-label={`Interactive exact 3D preview of ${bundle.display_name}`}
          camera={{ position: [width * 1.2, -height * .85, Math.max(width, height) * .9], up: [0, 0, 1], fov: 38, near: .1, far: Math.max(width, height) * 12 }}
          dpr={[1, 1.5]}
          frameloop="demand"
          shadows
        >
          <color attach="background" args={["#0b1218"]} />
          <hemisphereLight args={["#f1f7fc", "#12191f", 1.65]} />
          <directionalLight position={[width * .35, -height * .4, Math.max(width, height)]} intensity={2.35} castShadow />
          <directionalLight position={[-width * .2, height, height * .3]} intensity={.8} />
          <mesh position={[width / 2, height / 2, -.65]} receiveShadow>
            <planeGeometry args={[width, height]} />
            <meshStandardMaterial color="#e9ece5" roughness={.96} />
          </mesh>
          <group position={[origin[0], origin[1], -.58]}>
            <mesh>
              <ringGeometry args={[2.2, 3.2, 36]} />
              <meshBasicMaterial color="#287fd5" side={DoubleSide} />
            </mesh>
            <axesHelper args={[18]} position={[0, 0, .08]} />
          </group>
          <Bounds fit clip observe margin={1.45} maxDuration={.55}>
            <ObjectCollection
              bundle={bundle}
              renderable={renderable}
              origin={origin}
              focus={focus}
              onFocus={setFocus}
              onDetail={updateMeshDetail}
            />
            <SheetFocus active={focus === "sheet"} width={width} height={height} />
          </Bounds>
          <OrbitControls makeDefault enablePan enableRotate enableZoom dampingFactor={.08} />
        </Canvas>
      </SceneBoundary>
      <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-1.5 rounded bg-black/70 px-2 py-1 text-[9px] text-slate-200">
        <MousePointer2 className="size-3" />Drag to orbit · wheel to zoom · select an object to focus
      </div>
    </div>

    <aside className="flex min-h-0 flex-col border-l border-white/10 bg-[#101820]" aria-label="Objects in this immutable template">
      <div className="border-b border-white/10 p-3">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-[10px] font-bold uppercase tracking-[.14em] text-slate-200">Objects · {renderable.length}</div>
            <div className="mt-0.5 text-[9px] text-emerald-300">Exact immutable PLY detail</div>
          </div>
          <ScanSearch className="size-4 text-slate-500" />
        </div>
        <div className="mt-2 grid grid-cols-2 gap-1">
          <button
            type="button"
            aria-pressed={focus === "objects"}
            aria-label="Fit all objects"
            onClick={() => setFocus("objects")}
            className={cn("rounded border px-2 py-1.5 text-[9px] font-semibold text-slate-300 transition-colors hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400", focus === "objects" ? "border-sky-400/70 bg-sky-400/15 text-white" : "border-white/10")}
          >
            <ScanSearch className="mr-1 inline size-3" />Objects
          </button>
          <button
            type="button"
            aria-pressed={focus === "sheet"}
            aria-label="Fit printed sheet"
            onClick={() => setFocus("sheet")}
            className={cn("rounded border px-2 py-1.5 text-[9px] font-semibold text-slate-300 transition-colors hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400", focus === "sheet" ? "border-sky-400/70 bg-sky-400/15 text-white" : "border-white/10")}
          >
            <Grid3X3 className="mr-1 inline size-3" />Sheet
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2" data-testid="selected-template-object-index">
        {renderable.map((instance, index) => {
          const detail = meshDetails[instance.instance_uuid]
          const dimensions = formatDimensions(detail?.dimensions)
          const faceCount = formatCount(detail?.faces)
          const orientation = instance.orientation?.label
          const objectId = `obj_${String(instance.catalog.obj_id).padStart(6, "0")}`
          return <button
            type="button"
            key={instance.instance_uuid}
            aria-pressed={focus === instance.instance_uuid}
            aria-label={`Focus ${instance.catalog.name}, ${objectId}, instance ${index + 1}`}
            onClick={() => setFocus(instance.instance_uuid)}
            className={cn(
              "w-full rounded-md border p-2 text-left transition-colors hover:border-white/25 hover:bg-white/[.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400",
              focus === instance.instance_uuid ? "border-sky-400/70 bg-sky-400/10" : "border-white/10 bg-black/10",
            )}
          >
            <div className="flex items-start gap-2">
              <span className="grid size-5 shrink-0 place-items-center rounded-full border border-white/50 font-mono text-[8px] font-black text-white" style={{ backgroundColor: COLORS[index % COLORS.length] }}>{index + 1}</span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[10px] font-semibold text-white" title={instance.catalog.name}>{instance.catalog.name}</span>
                <span className="mt-0.5 block font-mono text-[8px] text-slate-400">{objectId}</span>
              </span>
            </div>
            <span className="mt-1.5 block border-t border-white/10 pt-1.5 text-[8px] leading-relaxed text-slate-400">
              {detail?.status === "exact"
                ? <>{dimensions ? `${dimensions} mm` : "Exact dimensions"}{faceCount ? ` · ${faceCount} faces` : ""}</>
                : detail?.status === "fallback" ? "Bounded fallback · exact PLY unavailable" : "Loading exact PLY…"}
              {orientation ? <span className="mt-0.5 block truncate text-slate-500" title={orientation}>{orientation}</span> : null}
            </span>
          </button>
        })}
      </div>
      <div className="border-t border-white/10 px-3 py-2 text-[8px] leading-relaxed text-slate-500">
        Select a numbered object to inspect its recognition geometry. Use <span className="text-slate-300">Sheet</span> to verify printed placement.
      </div>
    </aside>
  </div>
}
