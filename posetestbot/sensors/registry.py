"""Registry of supported RGB-D sensor capture adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from posetestbot.sensors.contracts import MountingMode, SensorType


AUTO_DEVICE_IDS = {"auto"}


@dataclass(frozen=True)
class SensorAdapterSpec:
    """Static capabilities for one supported sensor family."""

    sensor_type: SensorType
    display_name: str
    sdk_module: str
    capture_script: str
    folder_prefix: str
    supported_resolutions: tuple[str, ...]
    # Frame rates the sensor SDK delivers natively for each supported
    # resolution. Capture setup offers and validates only these values.
    supported_fps: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    live_rgb_preview_supported: bool = False
    default_resolution: str = "720p"
    mounting_modes: tuple[MountingMode, ...] = (
        MountingMode.EYE_IN_HAND,
        MountingMode.STATIC,
    )
    aligned_depth_to: str = "rgb"
    timestamp_source: str = "sensor_and_host"
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sensor_type"] = self.sensor_type.value
        data["supported_resolutions"] = list(self.supported_resolutions)
        data["supported_fps"] = {
            resolution: list(self.supported_fps.get(resolution, ()))
            for resolution in self.supported_resolutions
        }
        data["mounting_modes"] = [mode.value for mode in self.mounting_modes]
        data["notes"] = list(self.notes)
        return data


SENSOR_ADAPTERS: dict[SensorType, SensorAdapterSpec] = {
    SensorType.REALSENSE_D435: SensorAdapterSpec(
        sensor_type=SensorType.REALSENSE_D435,
        display_name="Intel RealSense D435",
        sdk_module="pyrealsense2",
        capture_script="scripts/capture_realsense_720p.py",
        folder_prefix="realsense",
        supported_resolutions=("720p",),
        supported_fps={"720p": (6, 15, 30)},
        live_rgb_preview_supported=True,
        notes=(
            "Depth is aligned to color by the capture script.",
            "Current capture script is 720p-only.",
        ),
    ),
    SensorType.OAK_D_PRO: SensorAdapterSpec(
        sensor_type=SensorType.OAK_D_PRO,
        display_name="Luxonis OAK-D Pro",
        sdk_module="depthai",
        capture_script="scripts/capture_luxonis_720p.py",
        folder_prefix="luxonis",
        supported_resolutions=("720p",),
        supported_fps={"720p": (6, 15, 30)},
        live_rgb_preview_supported=True,
        notes=(
            "DepthAI stereo depth is aligned to RGB by the capture script.",
            "Current capture script is 720p-only.",
        ),
    ),
    SensorType.ZED_2I: SensorAdapterSpec(
        sensor_type=SensorType.ZED_2I,
        display_name="Stereolabs ZED 2i",
        sdk_module="pyzed.sl",
        capture_script="scripts/capture_zed_2i.py",
        folder_prefix="zed_2i",
        supported_resolutions=("720p", "360p"),
        supported_fps={"720p": (15, 30, 60), "360p": (15, 30, 60, 100)},
        notes=(
            "Captures left RGB plus depth aligned to the left RGB stream.",
            "Requires the Stereolabs ZED SDK Python module outside ordinary PyPI.",
        ),
    ),
}


def is_auto_device_id(device_id: str) -> bool:
    return device_id.strip().lower() in AUTO_DEVICE_IDS


def get_sensor_adapter(sensor_type: SensorType | str) -> SensorAdapterSpec:
    try:
        normalized = (
            sensor_type
            if isinstance(sensor_type, SensorType)
            else SensorType(sensor_type)
        )
    except ValueError as exc:
        valid = ", ".join(sensor.value for sensor in SensorType)
        raise ValueError(
            f"Unsupported sensor type {str(sensor_type)!r}; expected one of: {valid}"
        ) from exc
    try:
        return SENSOR_ADAPTERS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"No capture adapter is registered for {normalized.value}"
        ) from exc


def list_sensor_adapters() -> list[dict[str, Any]]:
    return [
        adapter.to_dict()
        for adapter in sorted(
            SENSOR_ADAPTERS.values(), key=lambda item: item.sensor_type.value
        )
    ]


def sensor_folder_name(sensor_type: SensorType | str, device_id: str) -> str:
    adapter = get_sensor_adapter(sensor_type)
    suffix = "auto" if is_auto_device_id(device_id) else device_id.strip()
    return f"{adapter.folder_prefix}_{suffix}"


def capture_script_for_sensor(sensor_type: SensorType | str, resolution: str) -> str:
    adapter = get_sensor_adapter(sensor_type)
    if resolution not in adapter.supported_resolutions:
        supported = ", ".join(adapter.supported_resolutions)
        raise ValueError(
            f"{adapter.display_name} capture planning supports {supported}; "
            f"got {resolution!r}."
        )
    return adapter.capture_script


def supported_fps_for(sensor_type: SensorType | str, resolution: str) -> tuple[int, ...]:
    """Return the native frame rates for one sensor family and resolution."""

    adapter = get_sensor_adapter(sensor_type)
    capture_script_for_sensor(sensor_type, resolution)
    return tuple(adapter.supported_fps.get(resolution, ()))


def validate_sensor_fps(
    sensor_type: SensorType | str, resolution: str, fps: int
) -> None:
    """Reject a frame rate the sensor cannot deliver at this resolution."""

    supported = supported_fps_for(sensor_type, resolution)
    if fps not in supported:
        adapter = get_sensor_adapter(sensor_type)
        raise ValueError(
            f"{adapter.display_name} supports {', '.join(map(str, supported))} fps "
            f"at {resolution}; got {fps}."
        )


def build_sensor_capture_command(
    *,
    sensor_type: SensorType | str,
    device_id: str,
    output_folder: str,
    fps: int,
    resolution: str,
    max_frames: int | None = None,
    warmup_frames: int | None = None,
    inverted: bool = False,
) -> list[str]:
    """Build the current script-backed capture command for one sensor."""

    normalized = (
        sensor_type if isinstance(sensor_type, SensorType) else SensorType(sensor_type)
    )
    if inverted and normalized != SensorType.REALSENSE_D435:
        raise ValueError("Sensor inverted=true is only supported for RealSense D435")
    if warmup_frames is not None and warmup_frames < 0:
        raise ValueError("warmup_frames must be greater than or equal to 0")
    script = capture_script_for_sensor(sensor_type, resolution)
    command = [
        "uv",
        "run",
        "python",
        script,
        output_folder,
        "--fps",
        str(fps),
    ]
    if max_frames and max_frames > 0:
        command.extend(["--max_frames", str(max_frames)])
    if warmup_frames and warmup_frames > 0:
        command.extend(["--warmup-frames", str(warmup_frames)])
    if not is_auto_device_id(device_id):
        command.extend(["--device", device_id])
    if normalized == SensorType.REALSENSE_D435 and inverted:
        command.append("--inverted")
    if normalized == SensorType.ZED_2I:
        command.extend(["--resolution", resolution])
    return command
