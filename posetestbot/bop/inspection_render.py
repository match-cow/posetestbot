"""CPU x-ray rendering of BOP poses, in original-resolution RGB pixels.

The layer order, colors, near/far planes and opacity rules mirror pose-overlay.tsx.
There is deliberately no observed-depth test, lighting, label, or UI chrome.
"""

from __future__ import annotations

import itertools

import cv2
import numpy as np
import trimesh


GEOMETRY_DEFAULTS = {
    "estimateSurface": True,
    "estimateWireframe": False,
    "estimateAxes": False,
    "estimateBox": False,
    "gtSurface": False,
    "gtWireframe": True,
    "gtAxes": False,
    "gtBox": False,
    "estimateOpacity": 0.38,
    "gtOpacity": 0.32,
}
MASK_DEFAULTS = {
    "full": False,
    "visible": False,
    "fullOpacity": 0.28,
    "visibleOpacity": 0.42,
}
# Tailwind amber-300 / lime-300, expressed as sRGB bytes.
MASK_COLORS = {"full": (255, 210, 48), "visible": (187, 244, 81)}
POSE_COLORS = {"estimate": (34, 211, 238), "gt": (217, 70, 239)}


def _srgb(linear):
    linear = np.clip(linear, 0, 1)
    return np.where(
        linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055
    )


def geometry_color(color):
    """Match R3F's default ACES filmic exposure and Three's sRGB output."""
    srgb = np.asarray(color) / 255
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    incoming = np.array(
        [
            [0.59719, 0.35458, 0.04823],
            [0.07600, 0.90834, 0.01566],
            [0.02840, 0.13383, 0.83777],
        ]
    )
    outgoing = np.array(
        [
            [1.60475, -0.53108, -0.07367],
            [-0.10208, 1.10813, -0.00605],
            [-0.00327, -0.07276, 1.07602],
        ]
    )
    v = incoming @ (linear / 0.6)
    fitted = (v * (v + 0.0245786) - 0.000090537) / (
        v * (0.983729 * v + 0.4329510) + 0.238081
    )
    return np.rint(_srgb(outgoing @ fitted) * 255).astype(np.uint8)


def blend(image, selection, color, opacity):
    image[selection] = np.rint(
        image[selection] * (1 - opacity) + np.asarray(color) * opacity
    ).astype(np.uint8)


def load_geometry(path):
    mesh = trimesh.load(path, force="mesh", process=False)
    if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
        raise ValueError("Evaluation model must contain triangle geometry")
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if not np.isfinite(vertices).all():
        raise ValueError("Evaluation model vertices must be finite")
    low, high = mesh.bounds
    corners = np.array(list(itertools.product(*zip(low, high, strict=True))))
    box_edges = np.array(
        [(i, j) for i in range(8) for j in range(i + 1, 8) if (i ^ j) in (1, 2, 4)]
    )
    axis_length = max(20, np.linalg.norm(high - low) * 0.55)
    return {
        "vertices": vertices,
        "faces": faces,
        "edges": np.asarray(mesh.edges),
        "corners": corners,
        "box_edges": box_edges,
        "axes": np.vstack((np.zeros(3), np.eye(3) * axis_length)),
        "center": (low + high) / 2,
    }


def _clip(points, planes, *, closed):
    """Clip polygons/segments in homogeneous pixel coordinates, before division."""
    points = list(points)
    for normal, offset in planes:
        if not points:
            break
        output = []
        pairs = (
            zip(points, points[1:] + points[:1])
            if closed
            else [(points[0], points[-1])]
        )
        for a, b in pairs:
            da, db = np.dot(normal, a[:3]) + offset, np.dot(normal, b[:3]) + offset
            if da >= 0:
                output.append(a)
            if (da >= 0) != (db >= 0):
                output.append(a + da / (da - db) * (b - a))
            if not closed and db >= 0:
                output.append(b)
        points = output
    return np.asarray(points)


def _triangle(image, points, color, opacity):
    # Pixel-center sampling and a half-open edge rule avoid double blending the
    # shared diagonal of adjacent triangles (cv2.fillPoly includes both edges).
    a, b, c = points
    ab, ac = b - a, c - a
    area = ab[0] * ac[1] - ab[1] * ac[0]
    if abs(area) < 1e-10:
        return
    if area < 0:
        b, c = c, b
    low = np.maximum(0, np.floor(np.min(points, axis=0))).astype(int)
    high = np.minimum(image.shape[1::-1], np.ceil(np.max(points, axis=0))).astype(int)
    if np.any(high <= low):
        return
    # Bound temporary raster storage even for a very large projected triangle.
    for y in range(low[1], high[1], 64):
        end = min(y + 64, high[1])
        xs, ys = np.meshgrid(np.arange(low[0], high[0]) + 0.5, np.arange(y, end) + 0.5)
        inside = np.ones(xs.shape, dtype=bool)
        for p, q in ((a, b), (b, c), (c, a)):
            dx, dy = q - p
            edge = dx * (ys - p[1]) - dy * (xs - p[0])
            inclusive = dy < 0 or (dy == 0 and dx > 0)
            inside &= (edge > 0) | ((edge == 0) & inclusive)
        blend(image[y:end, low[0] : high[0]], inside, color, opacity)


def _line(image, points, color, opacity):
    if len(points) < 2:
        return
    attributes = points
    points = points[:, :2] / points[:, 2:3]
    if np.linalg.norm(points[-1] - points[0]) < 1e-9:
        return
    low = np.maximum(0, np.floor(points.min(axis=0)) - 1).astype(int)
    high = np.minimum(image.shape[1::-1], np.ceil(points.max(axis=0)) + 2).astype(int)
    if np.any(high <= low):
        return
    mask = np.zeros((high[1] - low[1], high[0] - low[0]), np.uint8)
    ends = np.rint(points - low).astype(int)
    cv2.line(mask, tuple(ends[0]), tuple(ends[-1]), 255, 1, cv2.LINE_8)
    selection = mask > 0
    if attributes.shape[1] == 6:
        # AxesHelper uses untone-mapped linear vertex-color gradients, with
        # perspective interpolation even when a segment crosses the near plane.
        ys, xs = np.nonzero(selection)
        positions = np.column_stack((xs, ys)) + low + 0.5
        delta = points[-1] - points[0]
        t = np.clip(
            (positions - points[0]) @ delta / max(np.dot(delta, delta), 1e-12), 0, 1
        )[:, None]
        za, zb = attributes[0, 2], attributes[-1, 2]
        linear = ((1 - t) * attributes[0, 3:] / za + t * attributes[-1, 3:] / zb) / (
            (1 - t) / za + t / zb
        )
        color = _srgb(linear) * 255
    blend(image[low[1] : high[1], low[0] : high[0]], selection, color, opacity)


def render_geometry(image, frame, models, settings):
    """Composite masks first, then all geometry by WebGL renderOrder/depth."""
    height, width = image.shape[:2]
    k = np.asarray(frame["camera"]["cam_K"]).reshape(3, 3).copy()
    # The viewer's projection uses fx, skew, cx, fy, cy.
    k[1, 0] = 0
    k[2] = [0, 0, 1]
    planes = [
        (np.array([0, 0, 1]), -1),
        (np.array([0, 0, -1]), 1_000_000),
        (np.array([1, 0, 0]), 0),
        (np.array([-1, 0, width]), 0),
        (np.array([0, 1, 0]), 0),
        (np.array([0, -1, height]), 0),
    ]
    layers = []
    for kind, poses in (
        ("estimate", frame["estimates"]),
        ("gt", frame["ground_truth"]),
    ):
        for pose in poses:
            if not any(
                settings[kind + layer]
                for layer in ("Surface", "Wireframe", "Box", "Axes")
            ):
                continue
            model = models[pose["obj_id"]]
            rotation = np.asarray(pose["rotation"]).reshape(3, 3)
            translation = np.asarray(pose["translation_mm"])
            depth = (rotation @ model["center"] + translation)[2]
            order = (
                10 + pose["rank"] * 4 if kind == "estimate" else 200 + pose["gt_id"] * 4
            )
            for offset, layer in enumerate(("Surface", "Wireframe", "Box", "Axes")):
                if settings[kind + layer]:
                    layer_depth = depth
                    if layer == "Axes":
                        layer_depth = (
                            rotation @ (model["axes"].max(axis=0) / 2) + translation
                        )[2]
                    layers.append(
                        (
                            order + offset,
                            -layer_depth,
                            kind,
                            layer,
                            model,
                            rotation,
                            translation,
                        )
                    )
    for _, _, kind, layer, model, rotation, translation in sorted(
        layers, key=lambda row: row[:2]
    ):
        color = geometry_color(POSE_COLORS[kind])
        opacity = settings[kind + "Opacity"]
        points = model[
            "axes" if layer == "Axes" else "corners" if layer == "Box" else "vertices"
        ]
        projected = (points @ rotation.T + translation) @ k.T
        if layer == "Surface":
            # Three.js transparent DoubleSide materials draw back faces, then
            # front faces, with depth testing/writing disabled for this viewer.
            # Bound temporary clipping storage independently of mesh size.
            for back_faces in (True, False):
                for start in range(0, len(model["faces"]), 4096):
                    triangles = projected[model["faces"][start : start + 4096]]
                    side = (np.linalg.det(triangles) >= 0) == back_faces
                    # Most triangles need no per-plane Python clipping.
                    distances = np.stack(
                        [triangles @ normal + offset for normal, offset in planes],
                        axis=-1,
                    )
                    inside = (distances >= 0).all(axis=(1, 2))
                    outside = (distances < 0).all(axis=1).any(axis=1)
                    for idx in np.flatnonzero(side & ~outside):
                        triangle = triangles[idx]
                        polygon = (
                            triangle
                            if inside[idx]
                            else _clip(triangle, planes, closed=True)
                        )
                        if len(polygon) < 3:
                            continue
                        pixels = polygon[:, :2] / polygon[:, 2:3]
                        for i in range(1, len(pixels) - 1):
                            _triangle(image, pixels[[0, i, i + 1]], color, opacity)
        else:
            edges = (
                model["edges"]
                if layer == "Wireframe"
                else model["box_edges"]
                if layer == "Box"
                else [(0, 1), (0, 2), (0, 3)]
            )
            alpha = (
                max(0.75, opacity)
                if layer == "Wireframe"
                else 0.95
                if layer == "Box"
                else 1
            )
            for _pass in range(2 if layer == "Wireframe" else 1):
                for i, edge in enumerate(edges):
                    segment = projected[list(edge)]
                    if layer == "Axes":
                        colors = [
                            ([1, 0, 0], [1, 0.6, 0]),
                            ([0, 1, 0], [0.6, 1, 0]),
                            ([0, 0, 1], [0, 0.6, 1]),
                        ][i]
                        segment = np.column_stack((segment, colors))
                    _line(image, _clip(segment, planes, closed=False), color, alpha)
    return image
