import { useEffect, useMemo, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Box, LoaderCircle, TriangleAlert } from "lucide-react"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { api, errorMessage } from "@/lib/api"
import type {
  CatalogObject,
  Matrix4x4,
  PoseTemplateBundle,
  PoseTemplateContour,
  PoseTemplateOrientationThumbnail,
  PoseTemplatePreview,
  PoseTemplatePreviewMesh,
  PoseTemplateThumbnail,
} from "@/lib/contracts"
import { IDENTITY_MATRIX_4X4, projectIsometricMesh, type Matrix4x4Tuple } from "@/lib/isometric"
import { cn } from "@/lib/utils"

function faceColor(shade: number) {
  const base = [150, 169, 135]
  const channel = (value: number) => Math.max(0, Math.min(255, Math.round(18 + (value - 18) * shade)))
  return `rgb(${channel(base[0])} ${channel(base[1])} ${channel(base[2])})`
}

const SVG_FACE_LIMIT = 512

function CanvasMeshPreview({
  projection,
  span,
  centerX,
  centerY,
  padding,
  label,
  className,
  testId,
}: {
  projection: ReturnType<typeof projectIsometricMesh>
  span: number
  centerX: number
  centerY: number
  padding: number
  label: string
  className?: string
  testId?: string
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const draw = () => {
      const cssWidth = Math.max(1, canvas.clientWidth)
      const cssHeight = Math.max(1, canvas.clientHeight)
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.round(cssWidth * pixelRatio)
      canvas.height = Math.round(cssHeight * pixelRatio)
      const context = canvas.getContext("2d")
      if (!context) return
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0)
      context.fillStyle = "#10171d"
      context.fillRect(0, 0, cssWidth, cssHeight)
      const viewSpan = span + padding * 2
      const scale = Math.min(cssWidth, cssHeight) / viewSpan
      const offsetX = cssWidth / 2 - centerX * scale
      const offsetY = cssHeight / 2 - centerY * scale
      context.lineJoin = "round"
      context.strokeStyle = "rgba(225,235,218,.24)"
      context.lineWidth = Math.max(.35, span * scale / 420)
      projection.polygons.forEach((polygon) => {
        context.beginPath()
        polygon.points.forEach((point, index) => {
          const x = point.x * scale + offsetX
          const y = point.y * scale + offsetY
          if (index) context.lineTo(x, y)
          else context.moveTo(x, y)
        })
        context.closePath()
        context.fillStyle = faceColor(polygon.shade)
        context.fill()
        context.stroke()
      })
    }
    draw()
    if (typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(draw)
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [centerX, centerY, padding, projection, span])
  return <canvas
    ref={canvasRef}
    data-testid={testId}
    role="img"
    aria-label={`${label} isometric 3D preview`}
    className={cn("size-full bg-[#10171d]", className)}
  />
}

export function contourPoints(contour: PoseTemplateContour) {
  return contour.points
}

function footprintPath(contours: Array<Array<{ x_mm: number; y_mm: number }>>) {
  return contours.map((contour) => contour.map((point, index) => `${index ? "L" : "M"} ${point.x_mm} ${point.y_mm}`).join(" ") + " Z").join(" ")
}

export function IsometricMeshPreview({
  mesh,
  transform = IDENTITY_MATRIX_4X4,
  label,
  className,
  testId,
  commonSpan,
}: {
  mesh: PoseTemplatePreviewMesh
  transform?: Matrix4x4 | Matrix4x4Tuple
  label: string
  className?: string
  testId?: string
  commonSpan?: number
}) {
  const result = useMemo(() => {
    try {
      return { projection: projectIsometricMesh(mesh, transform as Matrix4x4Tuple), error: null }
    } catch (error) {
      return { projection: null, error: error instanceof Error ? error.message : "Preview geometry is invalid" }
    }
  }, [mesh, transform])

  if (!result.projection || result.error) {
    return <div data-testid={testId} className={cn("grid size-full min-h-16 place-items-center rounded-md bg-muted/55 px-2 text-center text-[10px] text-muted-foreground", className)} title={result.error ?? undefined}><TriangleAlert className="size-4" /><span className="sr-only">{label} isometric preview unavailable</span></div>
  }
  const { bounds, polygons } = result.projection
  const width = Math.max(0.01, bounds.maxX - bounds.minX)
  const height = Math.max(0.01, bounds.maxY - bounds.minY)
  const span = Math.max(width, height, commonSpan ?? 0.01)
  const centerX = (bounds.minX + bounds.maxX) / 2
  const centerY = (bounds.minY + bounds.maxY) / 2
  const padding = span * 0.09
  if (polygons.length > SVG_FACE_LIMIT) {
    return <CanvasMeshPreview
      projection={result.projection}
      span={span}
      centerX={centerX}
      centerY={centerY}
      padding={padding}
      label={label}
      className={className}
      testId={testId}
    />
  }
  return <svg
    data-testid={testId}
    role="img"
    aria-label={`${label} isometric 3D preview`}
    viewBox={`${centerX - span / 2 - padding} ${centerY - span / 2 - padding} ${span + padding * 2} ${span + padding * 2}`}
    preserveAspectRatio="xMidYMid meet"
    className={cn("size-full bg-[#10171d]", className)}
  >
    <title>{label} isometric 3D preview</title>
    {polygons.map((polygon) => <polygon
      key={polygon.faceIndex}
      data-face-index={polygon.faceIndex}
      points={polygon.points.map((point) => `${point.x},${point.y}`).join(" ")}
      fill={faceColor(polygon.shade)}
      stroke="rgba(225,235,218,.42)"
      strokeWidth={Math.max(width, height) / 420}
      strokeLinejoin="round"
    />)}
  </svg>
}

export function orientationAnalysisQueryKey(object: Pick<CatalogObject, "catalog_uuid" | "canonical_ply_sha256" | "geometry_revision">) {
  return ["pose-template-orientations", object.catalog_uuid, object.canonical_ply_sha256 ?? object.geometry_revision ?? 1] as const
}

export function orientationThumbnailQueryKey(object: Pick<CatalogObject, "catalog_uuid" | "canonical_ply_sha256" | "geometry_revision">) {
  return ["pose-template-orientation-thumbnail", object.catalog_uuid, object.canonical_ply_sha256 ?? object.geometry_revision ?? 1] as const
}

export function useWorkpieceOrientationThumbnail(object: CatalogObject, enabled = true) {
  return useQuery({
    queryKey: orientationThumbnailQueryKey(object),
    queryFn: () => api<PoseTemplateOrientationThumbnail>(`/pose-templates/workpieces/${object.catalog_uuid}/orientation-thumbnail`),
    enabled: enabled && Boolean(object.catalog_uuid),
    staleTime: Infinity,
    retry: false,
  })
}

export function workpieceThumbnailFailure(error: unknown) {
  const detail = errorMessage(error)
  if (detail.toLocaleLowerCase().includes("unsupported implementation revision")) {
    return {
      detail,
      message: "Preview/server revision mismatch. Restart PoseTestBot, then reload.",
      restartRequired: true,
    }
  }
  return {
    detail,
    message: "Card preview is stale or unavailable. Select this workpiece and refresh it.",
    restartRequired: false,
  }
}

export function WorkpieceIsometricThumbnail({ object, className }: { object: CatalogObject; className?: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [shouldLoad, setShouldLoad] = useState(false)
  useEffect(() => {
    const element = containerRef.current
    if (!element || shouldLoad) return
    if (typeof IntersectionObserver === "undefined") {
      const frame = window.requestAnimationFrame(() => setShouldLoad(true))
      return () => window.cancelAnimationFrame(frame)
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return
      setShouldLoad(true)
      observer.disconnect()
    }, { rootMargin: "240px" })
    observer.observe(element)
    return () => observer.disconnect()
  }, [shouldLoad])
  const thumbnail = useWorkpieceOrientationThumbnail(object, shouldLoad)
  const displayedFaces = thumbnail.data?.preview_mesh.faces.length ?? 0
  const approximation = thumbnail.data?.recognition_mesh_approximation
  const sourceFaces = approximation?.source_faces ?? object.extraction.faces
  const reduced = displayedFaces > 0 && sourceFaces > displayedFaces
  const warning = approximation?.strategy === "convex_proxy" || approximation?.topology_preserved === false
  const badge = approximation?.strategy === "convex_proxy" ? "Proxy" : warning ? "Approx" : "LOD"
  const strategy = {
    welded_source: "welded source surface",
    quadric_decimation: "quadric-decimated surface",
    spatial_clustering: "spatially clustered surface",
    convex_proxy: "convex safety proxy",
  }[approximation?.strategy ?? "quadric_decimation"]
  const detail = `${displayedFaces.toLocaleString()} of ${sourceFaces.toLocaleString()} source faces shown as a ${strategy}${warning ? "; source topology could not be retained within the card budget" : ""}`
  const failure = thumbnail.error ? workpieceThumbnailFailure(thumbnail.error) : null
  return <div ref={containerRef} className={cn("relative grid h-24 w-full place-items-center overflow-hidden rounded-md border bg-muted/30", className)} data-testid={`workpiece-thumbnail-${object.catalog_uuid}`}>
    {!shouldLoad ? <Box className="size-5 text-muted-foreground/45" />
      : thumbnail.isPending || (thumbnail.isFetching && !thumbnail.data) ? <LoaderCircle className="size-4 animate-spin text-muted-foreground" />
      : thumbnail.data ? <IsometricMeshPreview mesh={thumbnail.data.preview_mesh} label={object.name} testId={`workpiece-isometric-${object.catalog_uuid}`} />
        : <div
          className="grid place-items-center gap-1 px-2 text-center text-[9px] text-muted-foreground"
          data-testid={`workpiece-thumbnail-error-${object.catalog_uuid}`}
          title={failure?.detail}
        >
          {failure?.restartRequired ? <TriangleAlert className="size-5 text-warning" /> : <Box className="size-5" />}
          <span>{failure?.message ?? "Card preview is unavailable."}</span>
        </div>}
    {reduced ? <Tooltip><TooltipTrigger asChild><span
      className={cn("pointer-events-auto absolute bottom-1 right-1 rounded px-1 py-0.5 text-[8px] font-semibold uppercase tracking-wide text-white", warning ? "bg-amber-800/90" : "bg-black/65")}
      tabIndex={0}
      aria-label={`Bounded preview level of detail: ${detail}`}
    >{badge}</span></TooltipTrigger><TooltipContent className="max-w-72">{detail}</TooltipContent></Tooltip> : null}
  </div>
}

export function useTemplatePreview(templateUuid: string, enabled = true) {
  return useQuery({
    queryKey: ["pose-template-library-preview", templateUuid],
    queryFn: () => api<PoseTemplatePreview>(`/pose-templates/library/${templateUuid}/preview`),
    enabled: enabled && Boolean(templateUuid),
    staleTime: Infinity,
    retry: false,
  })
}

export function useTemplateThumbnail(templateUuid: string, enabled = true) {
  return useQuery({
    queryKey: ["pose-template-library-thumbnail", templateUuid],
    queryFn: () => api<PoseTemplateThumbnail>(`/pose-templates/library/${templateUuid}/thumbnail`),
    enabled: enabled && Boolean(templateUuid),
    staleTime: Infinity,
    retry: false,
  })
}

function TemplateFootprintSvg({ bundle, thumbnail }: { bundle: PoseTemplateBundle; thumbnail: PoseTemplateThumbnail }) {
  const page = thumbnail.page
  const [originX, originY] = thumbnail.configuration.page.origin_from_lower_left_mm
  const compensation = thumbnail.configuration.print_compensation
  const compensatedOriginX = page.width_mm / 2 + compensation.x_scale * (originX - page.width_mm / 2)
  const compensatedOriginY = page.height_mm / 2 + compensation.y_scale * (originY - page.height_mm / 2)
  return <>
    <svg
      viewBox={`0 0 ${page.width_mm} ${page.height_mm}`}
      role="img"
      aria-label={`${bundle.display_name} bounded footprint preview`}
      data-compensated-origin-mm={`${compensatedOriginX.toFixed(3)},${compensatedOriginY.toFixed(3)}`}
      className="max-h-full max-w-full bg-white shadow-sm ring-1 ring-black/15"
    >
      <rect width={page.width_mm} height={page.height_mm} fill="white" stroke="#cbd0c7" />
      <g transform={`translate(0 ${page.height_mm}) scale(1 -1)`}>
        <ellipse cx={compensatedOriginX} cy={compensatedOriginY} rx={2.5 * compensation.x_scale} ry={2.5 * compensation.y_scale} fill="#2374d8" />
        <g transform={`translate(${originX} ${originY})`}>
          {thumbnail.instances.map((instance) => <path
            key={instance.instance_uuid}
            d={footprintPath(instance.compensated_contours)}
            fill="rgba(177,203,33,.3)"
            stroke="#667600"
            strokeWidth=".8"
            fillRule="evenodd"
          />)}
        </g>
      </g>
    </svg>
    {thumbnail.approximation.truncated ? <span className="absolute bottom-1.5 right-1.5 rounded bg-amber-950/85 px-1.5 py-0.5 text-[8px] font-medium text-amber-100" title={`${thumbnail.approximation.included_points} of ${thumbnail.approximation.source_points} footprint points shown`}>Simplified</span> : null}
  </>
}

export function TemplateFootprintThumbnail({ bundle, className }: { bundle: PoseTemplateBundle; className?: string }) {
  const thumbnail = useTemplateThumbnail(bundle.template_uuid)
  return <div data-testid={`template-thumbnail-${bundle.template_uuid}`} className={cn("surface-grid relative grid h-40 place-items-center overflow-hidden rounded-lg border bg-muted/60 p-3", className)}>
    {thumbnail.isPending ? <LoaderCircle className="size-4 animate-spin text-muted-foreground" />
      : thumbnail.data ? <TemplateFootprintSvg bundle={bundle} thumbnail={thumbnail.data} />
        : <div className="grid place-items-center gap-1 text-[10px] text-muted-foreground"><TriangleAlert className="size-4" />Preview unavailable</div>}
  </div>
}
