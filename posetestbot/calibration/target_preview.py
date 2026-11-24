"""Small raster previews derived from immutable calibration-target geometry."""

from __future__ import annotations

import math
from typing import Any, Mapping

import cv2
import numpy as np

from posetestbot.calibration.targets import normalize_calibration_target_spec


def render_target_preview_png(
    value: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any] | None = None,
    max_dimension: int = 720,
    padding: int = 16,
) -> bytes:
    """Render exact stored ArUco IDs on their configured printable page."""

    target = normalize_calibration_target_spec(value)
    if target["target_type"] != "aruco_grid":
        raise ValueError("Calibration-target previews require an ArUco grid")
    if max_dimension < 64:
        raise ValueError("Calibration-target preview max_dimension must be at least 64")
    if padding < 0 or padding * 2 >= max_dimension:
        raise ValueError("Calibration-target preview padding is invalid")

    bounds = target["target_bounds"]
    width_mm = float(bounds["width_mm"])
    height_mm = float(bounds["height_mm"])
    # The spec stores physical geometry. The PDF, and so the page placement
    # in the source manifest, is drawn at the print-compensated size.
    compensation = target["print_compensation"]
    print_x = float(compensation["x_percent"]) / 100.0
    print_y = float(compensation["y_percent"]) / 100.0
    page = source_manifest.get("page_bounds") if source_manifest else None
    placement = source_manifest.get("target_bounds") if source_manifest else None
    if isinstance(page, Mapping) and isinstance(placement, Mapping):
        page_width_mm = float(page["width_mm"])
        page_height_mm = float(page["height_mm"])
        if not all(
            math.isfinite(item) and item > 0
            for item in (page_width_mm, page_height_mm)
        ):
            raise ValueError("Calibration-target preview page bounds are invalid")
        if not math.isclose(
            float(placement["width_mm"]), width_mm * print_x, abs_tol=1e-3
        ) or not math.isclose(
            float(placement["height_mm"]), height_mm * print_y, abs_tol=1e-3
        ):
            raise ValueError("Calibration-target preview placement does not match the target")
        scale = max_dimension / max(page_width_mm, page_height_mm)
        scale_x, scale_y = scale * print_x, scale * print_y
        canvas_width = max(1, math.ceil(page_width_mm * scale))
        canvas_height = max(1, math.ceil(page_height_mm * scale))
        offset_x = (float(placement["x_mm"]) - float(page.get("x_mm", 0.0))) * scale
        offset_y = (float(placement["y_mm"]) - float(page.get("y_mm", 0.0))) * scale
    else:
        scale = (max_dimension - 2 * padding) / max(width_mm, height_mm)
        canvas_width = max(1, math.ceil(width_mm * scale)) + 2 * padding
        canvas_height = max(1, math.ceil(height_mm * scale)) + 2 * padding
        offset_x = float(padding)
        offset_y = float(padding)
        scale_x = scale_y = scale
    canvas = np.full((canvas_height, canvas_width), 255, dtype=np.uint8)

    try:
        dictionary_id = getattr(cv2.aruco, target["dictionary"])
        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
    except (AttributeError, cv2.error) as exc:
        raise ValueError(
            f"Installed OpenCV does not support {target['dictionary']}"
        ) from exc

    origin_x = float(bounds["x_mm"])
    origin_y = float(bounds["y_mm"])
    for marker in target["markers"]:
        corners = np.asarray(marker["corners_mm"], dtype=float)
        x0 = round(offset_x + (float(corners[:, 0].min()) - origin_x) * scale_x)
        x1 = round(offset_x + (float(corners[:, 0].max()) - origin_x) * scale_x)
        y0 = round(offset_y + (float(corners[:, 1].min()) - origin_y) * scale_y)
        y1 = round(offset_y + (float(corners[:, 1].max()) - origin_y) * scale_y)
        marker_width = max(1, x1 - x0)
        marker_height = max(1, y1 - y0)

        # Generate above thumbnail resolution when necessary, then use nearest-neighbour
        # scaling so the dictionary cells stay hard-edged in the small card preview.
        source_size = max(32, marker_width, marker_height)
        marker_image = cv2.aruco.generateImageMarker(
            dictionary, int(marker["id"]), source_size
        )
        if marker_image.shape != (marker_height, marker_width):
            marker_image = cv2.resize(
                marker_image,
                (marker_width, marker_height),
                interpolation=cv2.INTER_NEAREST,
            )
        canvas[y0 : y0 + marker_height, x0 : x0 + marker_width] = marker_image

    encoded, payload = cv2.imencode(".png", canvas)
    if not encoded:
        raise RuntimeError("OpenCV could not encode the calibration-target preview")
    return payload.tobytes()
