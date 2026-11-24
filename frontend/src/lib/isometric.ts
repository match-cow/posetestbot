/*
 * Dependency-free mesh projection adapted from PoseTemplateCreator's
 * frontend/src/editor/isometric.ts at c675193 (carried by 97ddb9b).
 * The fixed orthographic frame and painter ordering intentionally match the
 * upstream printable-template preview.
 * Copyright 2026 Leibniz University Hannover; MIT licensed. The complete
 * notice is retained in THIRD_PARTY_NOTICES.md.
 */

const SQRT_THREE_OVER_TWO = Math.sqrt(3) / 2
const SQRT_THREE = Math.sqrt(3)
const EPSILON = 1e-12

export type Vector3Tuple = readonly [number, number, number]
export type TriangleFace = readonly [number, number, number]
export type Matrix4x4Tuple = readonly [
  readonly [number, number, number, number],
  readonly [number, number, number, number],
  readonly [number, number, number, number],
  readonly [number, number, number, number],
]

export interface PreviewMeshLike {
  vertices: readonly Vector3Tuple[]
  faces: readonly TriangleFace[]
}

export interface IsometricPoint {
  x: number
  y: number
}

export interface IsometricBounds {
  minX: number
  minY: number
  maxX: number
  maxY: number
}

export interface ProjectedTrianglePolygon {
  points: readonly [IsometricPoint, IsometricPoint, IsometricPoint]
  depth: number
  shade: number
  faceIndex: number
}

export interface ProjectedIsometricMesh {
  polygons: readonly ProjectedTrianglePolygon[]
  bounds: IsometricBounds
}

interface WorldPoint {
  x: number
  y: number
  z: number
}

interface ProjectedWorldPoint extends IsometricPoint {
  depth: number
}

export const IDENTITY_MATRIX_4X4: Matrix4x4Tuple = [
  [1, 0, 0, 0],
  [0, 1, 0, 0],
  [0, 0, 1, 0],
  [0, 0, 0, 1],
]

function assertFinite(value: number, label: string) {
  if (!Number.isFinite(value)) throw new Error(`${label} must be finite.`)
}

function normalized(value: number) {
  return Math.abs(value) < EPSILON ? 0 : value
}

function assertMatrix(matrix: Matrix4x4Tuple) {
  matrix.forEach((row, rowIndex) => row.forEach((value, columnIndex) => assertFinite(value, `Matrix value [${rowIndex}][${columnIndex}]`)))
}

function transformVertex(vertex: Vector3Tuple, matrix: Matrix4x4Tuple): WorldPoint {
  vertex.forEach((value, index) => assertFinite(value, `Vertex coordinate ${index}`))
  const [x, y, z] = vertex
  const transformedX = matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3]
  const transformedY = matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3]
  const transformedZ = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3]
  const homogeneous = matrix[3][0] * x + matrix[3][1] * y + matrix[3][2] * z + matrix[3][3]
  if (!Number.isFinite(homogeneous) || Math.abs(homogeneous) < EPSILON) throw new Error("Mesh transform produced an invalid homogeneous coordinate.")
  const point = {
    x: normalized(transformedX / homogeneous),
    y: normalized(transformedY / homogeneous),
    z: normalized(transformedZ / homogeneous),
  }
  assertFinite(point.x, "Transformed X")
  assertFinite(point.y, "Transformed Y")
  assertFinite(point.z, "Transformed Z")
  return point
}

/** Project a world point with PoseTemplateCreator's fixed orthographic view. */
export function projectIsometricPoint(point: Vector3Tuple): ProjectedWorldPoint {
  const [x, y, z] = point
  assertFinite(x, "Projected X input")
  assertFinite(y, "Projected Y input")
  assertFinite(z, "Projected Z input")
  return {
    x: normalized(SQRT_THREE_OVER_TWO * (x - y)),
    y: normalized(-0.5 * (x + y) - z),
    depth: normalized((-x - y + z) / SQRT_THREE),
  }
}

function projectedBounds(points: readonly ProjectedWorldPoint[]): IsometricBounds {
  if (!points.length) return { minX: 0, minY: 0, maxX: 0, maxY: 0 }
  return points.reduce<IsometricBounds>((bounds, point) => ({
    minX: Math.min(bounds.minX, point.x),
    minY: Math.min(bounds.minY, point.y),
    maxX: Math.max(bounds.maxX, point.x),
    maxY: Math.max(bounds.maxY, point.y),
  }), { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity })
}

function triangleShade(first: WorldPoint, second: WorldPoint, third: WorldPoint) {
  const firstEdge = { x: second.x - first.x, y: second.y - first.y, z: second.z - first.z }
  const secondEdge = { x: third.x - first.x, y: third.y - first.y, z: third.z - first.z }
  const normal = {
    x: firstEdge.y * secondEdge.z - firstEdge.z * secondEdge.y,
    y: firstEdge.z * secondEdge.x - firstEdge.x * secondEdge.z,
    z: firstEdge.x * secondEdge.y - firstEdge.y * secondEdge.x,
  }
  const length = Math.hypot(normal.x, normal.y, normal.z)
  if (length < EPSILON) return 0.62
  const light = { x: -0.32, y: -0.41, z: 0.854 }
  const alignment = Math.abs((normal.x * light.x + normal.y * light.y + normal.z * light.z) / length / Math.hypot(light.x, light.y, light.z))
  return 0.52 + 0.48 * alignment
}

function vertexAt<T>(vertices: readonly T[], vertexIndex: number, faceIndex: number): T {
  const vertex = vertices[vertexIndex]
  if (!Number.isInteger(vertexIndex) || vertexIndex < 0 || vertex === undefined) throw new Error(`Face ${faceIndex} references an invalid vertex.`)
  return vertex
}

/** Transform, project, shade, and back-to-front sort every mesh triangle. */
export function projectIsometricMesh(mesh: PreviewMeshLike, transform: Matrix4x4Tuple): ProjectedIsometricMesh {
  assertMatrix(transform)
  const worldVertices = mesh.vertices.map((vertex) => transformVertex(vertex, transform))
  const projectedVertices = worldVertices.map((point) => projectIsometricPoint([point.x, point.y, point.z]))
  const polygons = mesh.faces.map<ProjectedTrianglePolygon>((face, faceIndex) => {
    const firstWorld = vertexAt(worldVertices, face[0], faceIndex)
    const secondWorld = vertexAt(worldVertices, face[1], faceIndex)
    const thirdWorld = vertexAt(worldVertices, face[2], faceIndex)
    const first = vertexAt(projectedVertices, face[0], faceIndex)
    const second = vertexAt(projectedVertices, face[1], faceIndex)
    const third = vertexAt(projectedVertices, face[2], faceIndex)
    return {
      points: [{ x: first.x, y: first.y }, { x: second.x, y: second.y }, { x: third.x, y: third.y }],
      depth: (first.depth + second.depth + third.depth) / 3,
      shade: triangleShade(firstWorld, secondWorld, thirdWorld),
      faceIndex,
    }
  })
  polygons.sort((left, right) => left.depth - right.depth || left.faceIndex - right.faceIndex)
  return { polygons, bounds: projectedBounds(projectedVertices) }
}
