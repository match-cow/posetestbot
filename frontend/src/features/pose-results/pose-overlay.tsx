import { Component, Suspense, useMemo } from "react"
import { Canvas, useLoader } from "@react-three/fiber"
import {
  AxesHelper,
  BoxGeometry,
  DoubleSide,
  EdgesGeometry,
  Matrix4,
  PerspectiveCamera,
  Vector3,
} from "three"
import { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js"

import type {
  BopInspectionEstimate,
  BopInspectionFrame,
  BopInspectionGroundTruth,
} from "@/lib/contracts"

export interface PoseOverlayLayers {
  estimateSurface: boolean
  estimateWireframe: boolean
  estimateAxes: boolean
  estimateBox: boolean
  gtSurface: boolean
  gtWireframe: boolean
  gtAxes: boolean
  gtBox: boolean
  estimateOpacity: number
  gtOpacity: number
}

interface OverlayErrorBoundaryProps {
  children: React.ReactNode
  onError: (message: string) => void
}

export class OverlayErrorBoundary extends Component<OverlayErrorBoundaryProps, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: unknown) {
    this.props.onError(error instanceof Error ? error.message : "The geometry overlay could not be rendered.")
  }

  render() {
    return this.state.failed ? null : this.props.children
  }
}

function projectionCamera(camK: number[], imageSize: [number, number]) {
  const [width, height] = imageSize
  const [fx, skew, cx, , fy, cy] = camK
  const near = 1
  const far = 1_000_000
  const camera = new PerspectiveCamera()
  camera.projectionMatrix.set(
    2 * fx / width, -2 * skew / width, 1 - 2 * cx / width, 0,
    0, 2 * fy / height, 2 * cy / height - 1, 0,
    0, 0, -(far + near) / (far - near), -2 * far * near / (far - near),
    0, 0, -1, 0,
  )
  camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert()
  camera.matrixWorld.identity()
  camera.matrixWorldInverse.identity()
  camera.near = near
  camera.far = far
  ;(camera as PerspectiveCamera & { manual?: boolean }).manual = true
  return camera
}

function opencvPoseMatrix(pose: BopInspectionEstimate | BopInspectionGroundTruth) {
  const r = pose.rotation
  const t = pose.translation_mm
  return new Matrix4().set(
    r[0], r[1], r[2], t[0],
    -r[3], -r[4], -r[5], -t[1],
    -r[6], -r[7], -r[8], -t[2],
    0, 0, 0, 1,
  )
}

function ModelPose({
  pose,
  modelUrl,
  color,
  surface,
  wireframe,
  axes,
  box,
  opacity,
  renderOrder,
}: {
  pose: BopInspectionEstimate | BopInspectionGroundTruth
  modelUrl: string
  color: string
  surface: boolean
  wireframe: boolean
  axes: boolean
  box: boolean
  opacity: number
  renderOrder: number
}) {
  const source = useLoader(PLYLoader, modelUrl)
  const geometry = useMemo(() => {
    const cloned = source.clone()
    if (!cloned.getAttribute("normal")) cloned.computeVertexNormals()
    cloned.computeBoundingBox()
    return cloned
  }, [source])
  const poseMatrix = useMemo(() => opencvPoseMatrix(pose), [pose])
  const boxGeometry = useMemo(() => {
    if (!geometry.boundingBox) return null
    const size = geometry.boundingBox.getSize(new Vector3())
    return new EdgesGeometry(new BoxGeometry(size.x, size.y, size.z))
  }, [geometry])
  const boxCenter = useMemo(() => geometry.boundingBox?.getCenter(new Vector3()) ?? new Vector3(), [geometry])
  const axisLength = useMemo(() => {
    if (!geometry.boundingBox) return 40
    return Math.max(20, geometry.boundingBox.getSize(new Vector3()).length() * 0.55)
  }, [geometry])
  const axesHelper = useMemo(() => {
    const helper = new AxesHelper(axisLength)
    const materials = Array.isArray(helper.material) ? helper.material : [helper.material]
    for (const material of materials) {
      material.depthTest = false
      material.depthWrite = false
      material.transparent = true
    }
    return helper
  }, [axisLength])

  return <group matrix={poseMatrix} matrixAutoUpdate={false}>
    {surface && <mesh geometry={geometry} renderOrder={renderOrder}>
      <meshBasicMaterial color={color} transparent opacity={opacity} depthTest={false} depthWrite={false} side={DoubleSide} />
    </mesh>}
    {wireframe && <mesh geometry={geometry} renderOrder={renderOrder + 1}>
      <meshBasicMaterial color={color} transparent opacity={Math.max(0.75, opacity)} wireframe depthTest={false} depthWrite={false} side={DoubleSide} />
    </mesh>}
    {box && boxGeometry && <lineSegments geometry={boxGeometry} position={boxCenter} renderOrder={renderOrder + 2}>
      <lineBasicMaterial color={color} transparent opacity={0.95} depthTest={false} depthWrite={false} />
    </lineSegments>}
    {axes && <primitive object={axesHelper} renderOrder={renderOrder + 3} />}
  </group>
}

function OverlayScene({ frame, layers }: { frame: BopInspectionFrame; layers: PoseOverlayLayers }) {
  return <>
    {frame.estimates.map((estimate) => {
      const modelUrl = frame.model_urls[String(estimate.obj_id)]
      return modelUrl ? <ModelPose
        key={`estimate-${estimate.obj_id}-${estimate.rank}`}
        pose={estimate}
        modelUrl={modelUrl}
        color="#22d3ee"
        surface={layers.estimateSurface}
        wireframe={layers.estimateWireframe}
        axes={layers.estimateAxes}
        box={layers.estimateBox}
        opacity={layers.estimateOpacity}
        renderOrder={10 + estimate.rank * 4}
      /> : null
    })}
    {frame.ground_truth.map((groundTruth) => {
      const modelUrl = frame.model_urls[String(groundTruth.obj_id)]
      return modelUrl ? <ModelPose
        key={`gt-${groundTruth.obj_id}-${groundTruth.gt_id}`}
        pose={groundTruth}
        modelUrl={modelUrl}
        color="#d946ef"
        surface={layers.gtSurface}
        wireframe={layers.gtWireframe}
        axes={layers.gtAxes}
        box={layers.gtBox}
        opacity={layers.gtOpacity}
        renderOrder={200 + groundTruth.gt_id * 4}
      /> : null
    })}
  </>
}

export function PoseOverlay({ frame, layers }: { frame: BopInspectionFrame; layers: PoseOverlayLayers }) {
  const camera = useMemo(
    () => projectionCamera(frame.camera.cam_K, frame.camera.image_size),
    [frame.camera.cam_K, frame.camera.image_size],
  )
  return <div className="pointer-events-none absolute inset-0" data-testid="pose-geometry-overlay">
    <Canvas
      camera={camera}
      frameloop="always"
      gl={{ alpha: true, antialias: false, preserveDrawingBuffer: true, powerPreference: "high-performance" }}
      onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
      style={{ background: "transparent" }}
    >
      <Suspense fallback={null}><OverlayScene frame={frame} layers={layers} /></Suspense>
    </Canvas>
  </div>
}
