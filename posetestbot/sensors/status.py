"""JSON-friendly sensor status snapshots for CLI/API/UI surfaces."""

from __future__ import annotations

import importlib.util
import re
from importlib import metadata as importlib_metadata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from posetestbot.sensors import discovery
from posetestbot.sensors.aliases import (
    alias_record_for_device,
    load_sensor_aliases,
)
from posetestbot.sensors.contracts import SensorDeviceInfo, SensorType
from posetestbot.sensors.oak_d_pro import depthai_version_supported
from posetestbot.sensors.registry import SENSOR_ADAPTERS

SCHEMA_VERSION = "sensor_status.v1"
REALSENSE_MIN_USB_MAJOR = 3

SENSOR_FAMILY_LABELS: Mapping[SensorType, str] = {
    sensor_type: adapter.display_name
    for sensor_type, adapter in SENSOR_ADAPTERS.items()
}

SENSOR_SDK_MODULES: Mapping[SensorType, str] = {
    sensor_type: adapter.sdk_module
    for sensor_type, adapter in SENSOR_ADAPTERS.items()
}

SENSOR_SDK_REQUIREMENTS: Mapping[SensorType, str | None] = {
    SensorType.REALSENSE_D435: None,
    SensorType.OAK_D_PRO: ">=3,<4",
    SensorType.ZED_2I: None,
}

DISCOVERERS: Mapping[SensorType, Callable[[], list[SensorDeviceInfo]]] = {
    SensorType.REALSENSE_D435: discovery.discover_realsense_d435,
    SensorType.OAK_D_PRO: discovery.discover_oak_d_pro,
    SensorType.ZED_2I: discovery.discover_zed_2i,
}


@dataclass(frozen=True)
class SensorFamilyStatus:
    sensor_type: SensorType
    display_name: str
    sdk_module: str
    sdk_available: bool
    sdk_version: str | None
    sdk_requirement: str | None
    expected_count: int | None
    connected_count: int
    capture_ready_count: int
    meets_expected: bool | None
    devices: list[SensorDeviceInfo]
    error: str | None = None
    diagnostics: list[dict] | None = None

    def as_dict(
        self,
        *,
        aliases: Mapping[str, Mapping[str, object]] | None = None,
    ) -> dict:
        value = asdict(self)
        value["sensor_type"] = self.sensor_type.value
        value["devices"] = [
            sensor_device_to_dict(
                device,
                aliases=aliases,
                sdk_available=self.sdk_available,
            )
            for device in self.devices
        ]
        value["diagnostics"] = list(self.diagnostics or [])
        adapter = SENSOR_ADAPTERS.get(self.sensor_type)
        if adapter is not None:
            capabilities = adapter.to_dict()
            value["supported_resolutions"] = capabilities["supported_resolutions"]
            value["supported_fps"] = capabilities["supported_fps"]
        return value


def sensor_device_to_dict(
    device: SensorDeviceInfo,
    *,
    aliases: Mapping[str, Mapping[str, object]] | None = None,
    sdk_available: bool | None = None,
) -> dict:
    alias_record = alias_record_for_device(
        aliases or {},
        sensor_type=device.sensor_type,
        device_id=device.device_id,
    )
    alias = str(alias_record.get("alias") or "").strip() or None
    effective_display_name = alias or device.display_name
    capture_ready, capture_readiness_reason = _device_capture_readiness(
        device,
        sdk_available=sdk_available,
    )
    data = {
        "sensor_type": device.sensor_type.value,
        "device_id": device.device_id,
        "display_name": device.display_name,
        "alias": alias,
        "effective_display_name": effective_display_name,
        "connected": device.connected,
        "capture_ready": capture_ready,
        "capture_readiness_reason": capture_readiness_reason,
        "live_rgb_preview_supported": SENSOR_ADAPTERS[
            device.sensor_type
        ].live_rgb_preview_supported,
        "metadata": dict(device.metadata),
    }
    if alias_record.get("mounting_mode") not in {None, ""}:
        data["mounting_mode"] = str(alias_record["mounting_mode"])
    if alias_record.get("inverted") is not None:
        data["inverted"] = bool(alias_record["inverted"])
    return {
        **data,
    }


def _device_capture_readiness(
    device: SensorDeviceInfo,
    *,
    sdk_available: bool | None,
) -> tuple[bool, str | None]:
    """Return whether a discovery record can identify a capture SDK device.

    RealSense USB descriptor discovery is intentionally retained when
    librealsense cannot enumerate a camera.  Those records are useful physical
    diagnostics, but they are not addressable by ``capture_realsense_720p.py``
    and therefore must not satisfy capture-readiness checks.
    """

    if not device.connected:
        return False, "disconnected"
    if device.sensor_type != SensorType.REALSENSE_D435:
        return True, None
    if sdk_available is False:
        return False, "sdk_unavailable"
    if str(device.metadata.get("discovery") or "") == "lsusb_descriptor":
        return False, "not_enumerated_by_sdk"
    usb_major = realsense_usb_major_version(
        device.metadata.get("usb_type_descriptor")
    )
    if usb_major is not None and usb_major < REALSENSE_MIN_USB_MAJOR:
        return False, "usb_connection_below_superspeed"
    return True, None


def realsense_usb_major_version(value: object) -> int | None:
    """Parse librealsense's numeric USB transport descriptor when available.

    Older discovery fixtures and SDK builds may omit this metadata.  Missing or
    unrecognized values intentionally return ``None`` so those records retain
    their prior readiness behavior.
    """

    if value is None or isinstance(value, bool):
        return None
    match = re.fullmatch(
        r"\s*(?:usb\s*)?(?P<major>\d+)(?:\.\d+)?\s*",
        str(value),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return int(match.group("major"))


def _unready_device_details(
    device: SensorDeviceInfo,
    *,
    reason: str | None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "device_id": device.device_id,
        "reason": reason,
        "discovery": device.metadata.get("discovery"),
    }
    usb_descriptor = device.metadata.get("usb_type_descriptor")
    if usb_descriptor is not None and usb_descriptor != "":
        details["usb_type_descriptor"] = usb_descriptor
    usb_major = realsense_usb_major_version(usb_descriptor)
    if usb_major is not None:
        details["usb_major"] = usb_major
    return details


def _realsense_firmware_mismatches(
    devices: list[SensorDeviceInfo],
) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for device in devices:
        if device.sensor_type != SensorType.REALSENSE_D435:
            continue
        actual = str(device.metadata.get("firmware_version") or "").strip()
        recommended = str(
            device.metadata.get("recommended_firmware_version") or ""
        ).strip()
        relation = _numeric_version_relation(actual, recommended)
        if relation in {"older", "newer"}:
            mismatches.append(
                {
                    "device_id": device.device_id,
                    "firmware_version": actual,
                    "recommended_firmware_version": recommended,
                    "relation": relation,
                }
            )
    return mismatches


def _numeric_version_relation(actual: str, recommended: str) -> str | None:
    """Compare dotted numeric firmware versions without imposing an update policy."""

    def components(value: str) -> tuple[int, ...] | None:
        parts = value.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            return None
        return tuple(int(part) for part in parts)

    actual_parts = components(actual)
    recommended_parts = components(recommended)
    if actual_parts is None or recommended_parts is None:
        return None
    width = max(len(actual_parts), len(recommended_parts))
    actual_padded = actual_parts + (0,) * (width - len(actual_parts))
    recommended_padded = recommended_parts + (0,) * (
        width - len(recommended_parts)
    )
    if actual_padded < recommended_padded:
        return "older"
    if actual_padded > recommended_padded:
        return "newer"
    return "match"


def sdk_module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def sdk_module_version(module_name: str) -> str | None:
    distribution_name = module_name.split(".", 1)[0]
    try:
        return importlib_metadata.version(distribution_name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _sensor_family_diagnostics(
    *,
    sensor_type: SensorType,
    sdk_module: str,
    sdk_available: bool,
    sdk_version: str | None,
    sdk_requirement: str | None,
    expected_count: int | None,
    connected_count: int,
    capture_ready_count: int,
    unready_devices: list[dict],
    firmware_mismatches: list[dict[str, str]],
    error: str | None,
) -> list[dict]:
    diagnostics: list[dict] = []
    if error:
        error_lower = error.lower()
        hints = [
            "Run the status command on the lab host with camera USB devices visible.",
        ]
        if "udev" in error_lower or "usb" in error_lower:
            hints.append(
                "Check USB/udev access; containers usually need /dev/bus/usb mounted and a USB device cgroup rule."
            )
        diagnostics.append(
            {
                "code": "discovery_error",
                "severity": "error",
                "message": f"{SENSOR_FAMILY_LABELS[sensor_type]} discovery failed: {error}",
                "hints": hints,
            }
        )
    if not sdk_available:
        diagnostics.append(
            {
                "code": "sdk_unavailable",
                "severity": "warning",
                "message": f"Python SDK module {sdk_module!r} is not importable.",
                "hints": [
                    f"Install or activate the {sdk_module} Python SDK in the uv environment.",
                ],
            }
        )
    if (
        sensor_type == SensorType.OAK_D_PRO
        and sdk_available
        and sdk_version is not None
        and not depthai_version_supported(sdk_version)
    ):
        diagnostics.append(
            {
                "code": "sdk_version_unsupported",
                "severity": "error",
                "message": (
                    f"Python SDK module {sdk_module!r} is {sdk_version}; "
                    f"OAK-D Pro capture requires DepthAI {sdk_requirement}."
                ),
                "hints": [
                    "Update the uv environment with `uv add \"depthai>=3,<4\"`.",
                ],
            }
        )
    if firmware_mismatches:
        diagnostics.append(
            {
                "code": "realsense_firmware_recommendation_mismatch",
                "severity": "warning",
                "message": (
                    "One or more RealSense firmware versions differ from the "
                    "troubleshooting recommendation reported by the installed "
                    "librealsense SDK. This does not identify the cause of a USB "
                    "transport failure."
                ),
                "devices": firmware_mismatches,
                "hints": [
                    "Treat the SDK value as compatibility evidence, not an automatic update instruction.",
                    "Review the matching RealSense SDK release guidance under lab change control before any firmware maintenance.",
                    "PoseTestBot does not flash or update camera firmware.",
                ],
            }
        )
    low_speed_devices = [
        device
        for device in unready_devices
        if device.get("reason") == "usb_connection_below_superspeed"
    ]
    if low_speed_devices:
        diagnostics.append(
            {
                "code": "realsense_usb_below_superspeed",
                "severity": "error",
                "message": (
                    f"{len(low_speed_devices)} SDK-enumerated RealSense device(s) "
                    "are connected below SuperSpeed; PoseTestBot capture requires "
                    f"a USB major version of at least {REALSENSE_MIN_USB_MAJOR}."
                ),
                "devices": low_speed_devices,
                "hints": [
                    "Reseat or power-cycle only the affected USB connection without moving the calibrated camera mount.",
                    "Use a known-good SuperSpeed cable and USB 3 port or adequately powered USB 3 hub; avoid overcommitting one USB controller.",
                    "Rerun sensor status and require usb_type_descriptor 3.x or newer for every configured RealSense before capture.",
                ],
            }
        )
    if unready_devices:
        diagnostics.append(
            {
                "code": "devices_not_capture_ready",
                "severity": "warning",
                "message": (
                    f"Detected {connected_count} "
                    f"{SENSOR_FAMILY_LABELS[sensor_type]} device record(s), but "
                    f"only {capture_ready_count} are capture-ready."
                ),
                "devices": unready_devices,
                "hints": [
                    "USB-only descriptor records are diagnostic evidence and do not satisfy expected capture-ready counts.",
                    "SDK-enumerated RealSense devices below SuperSpeed also do not satisfy capture-ready counts.",
                    "Check camera power, USB topology, permissions, and SDK enumeration before capture.",
                ],
            }
        )
    if expected_count is not None and capture_ready_count < expected_count:
        hints = [
            "Verify the expected lab cameras are physically connected and powered.",
        ]
        if sensor_type in {SensorType.REALSENSE_D435, SensorType.OAK_D_PRO}:
            hints.append(
                "Verify USB permissions and udev rules for camera discovery."
            )
        if sensor_type == SensorType.ZED_2I:
            hints.append(
                "Verify the ZED SDK and pyzed.sl Python bindings are installed."
            )
        diagnostics.append(
            {
                "code": "expected_count_not_met",
                "severity": "warning",
                "message": (
                    f"Capture-ready {capture_ready_count} of expected "
                    f"{expected_count} {SENSOR_FAMILY_LABELS[sensor_type]} "
                    f"device(s); {connected_count} device record(s) were detected."
                ),
                "hints": hints,
            }
        )
    return diagnostics


def collect_sensor_family_status(
    sensor_type: SensorType,
    *,
    expected_count: int | None = None,
    discoverer: Callable[[], list[SensorDeviceInfo]] | None = None,
) -> SensorFamilyStatus:
    sdk_module = SENSOR_SDK_MODULES[sensor_type]
    sdk_available = sdk_module_available(sdk_module)
    sdk_version = sdk_module_version(sdk_module) if sdk_available else None
    sdk_requirement = SENSOR_SDK_REQUIREMENTS[sensor_type]
    discoverer = discoverer or DISCOVERERS[sensor_type]

    devices: list[SensorDeviceInfo] = []
    error = None
    try:
        devices = discoverer()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    connected_count = len(devices)
    readiness = [
        _device_capture_readiness(device, sdk_available=sdk_available)
        for device in devices
    ]
    capture_ready_count = sum(is_ready for is_ready, _reason in readiness)
    unready_devices = [
        _unready_device_details(device, reason=reason)
        for device, (is_ready, reason) in zip(devices, readiness, strict=True)
        if not is_ready
    ]
    firmware_mismatches = _realsense_firmware_mismatches(devices)
    meets_expected = None
    if expected_count is not None:
        meets_expected = capture_ready_count >= expected_count
    diagnostics = _sensor_family_diagnostics(
        sensor_type=sensor_type,
        sdk_module=sdk_module,
        sdk_available=sdk_available,
        sdk_version=sdk_version,
        sdk_requirement=sdk_requirement,
        expected_count=expected_count,
        connected_count=connected_count,
        capture_ready_count=capture_ready_count,
        unready_devices=unready_devices,
        firmware_mismatches=firmware_mismatches,
        error=error,
    )

    return SensorFamilyStatus(
        sensor_type=sensor_type,
        display_name=SENSOR_FAMILY_LABELS[sensor_type],
        sdk_module=sdk_module,
        sdk_available=sdk_available,
        sdk_version=sdk_version,
        sdk_requirement=sdk_requirement,
        expected_count=expected_count,
        connected_count=connected_count,
        capture_ready_count=capture_ready_count,
        meets_expected=meets_expected,
        devices=devices,
        error=error,
        diagnostics=diagnostics,
    )


def collect_sensor_status(
    *,
    expected_counts: Mapping[SensorType, int | None] | None = None,
    discoverers: Mapping[SensorType, Callable[[], list[SensorDeviceInfo]]] | None = None,
    aliases: Mapping[str, Mapping[str, object]] | None = None,
    alias_path: str | None = None,
) -> dict:
    expected_counts = expected_counts or {}
    discoverers = discoverers or DISCOVERERS
    if aliases is None:
        aliases = load_sensor_aliases(alias_path) if alias_path is not None else load_sensor_aliases()
    families = []
    for sensor_type in SensorType:
        families.append(
            collect_sensor_family_status(
                sensor_type,
                expected_count=expected_counts.get(sensor_type),
                discoverer=discoverers.get(sensor_type),
            ).as_dict(aliases=aliases)
        )

    expected_counts_requested = any(
        family["expected_count"] is not None for family in families
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "families": families,
        "total_connected": sum(family["connected_count"] for family in families),
        "total_capture_ready": sum(
            family["capture_ready_count"] for family in families
        ),
        "all_expected_connected": all(
            family["meets_expected"] is not False for family in families
        ),
        "expected_counts_requested": expected_counts_requested,
    }


def parse_expected_counts(values: list[str]) -> dict[SensorType, int | None]:
    expected_counts: dict[SensorType, int | None] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected count must be SENSOR_TYPE=COUNT, got {value!r}")
        sensor_type_value, count_value = value.split("=", 1)
        try:
            sensor_type = SensorType(sensor_type_value)
        except ValueError as exc:
            valid = ", ".join(sensor.value for sensor in SensorType)
            raise ValueError(
                f"Unknown sensor type {sensor_type_value!r}; expected one of: {valid}"
            ) from exc

        if count_value.lower() in {"none", "unknown", "-"}:
            expected_counts[sensor_type] = None
            continue
        try:
            count = int(count_value)
        except ValueError as exc:
            raise ValueError(
                f"Expected count for {sensor_type.value} must be an integer"
            ) from exc
        if count < 0:
            raise ValueError(f"Expected count for {sensor_type.value} cannot be negative")
        expected_counts[sensor_type] = count
    return expected_counts
