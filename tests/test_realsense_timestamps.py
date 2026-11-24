"""RealSense frame timestamps: which clock each recorded value comes from."""

from __future__ import annotations

from posetestbot.sensors.realsense import (
    _frame_stem_from_sensor_timestamp_ns,
    _frame_timestamp_ns,
    _realsense_timestamp_metadata,
    _timestamp_domain_name,
)


class FakeTimestampDomain:
    def __init__(self, name: str):
        self.name = name


class FakeFrame:
    def __init__(
        self,
        *,
        timestamp_ms: float,
        timestamp_domain: str,
        metadata: dict[str, int],
    ):
        self._timestamp_ms = timestamp_ms
        self._timestamp_domain = FakeTimestampDomain(timestamp_domain)
        self._metadata = metadata

    def get_timestamp(self) -> float:
        return self._timestamp_ms

    def get_frame_timestamp_domain(self) -> FakeTimestampDomain:
        return self._timestamp_domain

    def supports_frame_metadata(self, metadata_key: str) -> bool:
        return metadata_key in self._metadata

    def get_frame_metadata(self, metadata_key: str) -> int:
        return self._metadata[metadata_key]


class FakeFrameMetadataValue:
    backend_timestamp = "backend_timestamp"
    frame_timestamp = "frame_timestamp"
    sensor_timestamp = "sensor_timestamp"
    time_of_arrival = "time_of_arrival"


class FakeRs:
    frame_metadata_value = FakeFrameMetadataValue


def test_realsense_timestamp_helpers_record_sensor_clock_details() -> None:
    color_frame = FakeFrame(
        timestamp_ms=1_701_234_567_890.123,
        timestamp_domain="global_time",
        metadata={
            "backend_timestamp": 11,
            "frame_timestamp": 12,
            "sensor_timestamp": 13,
            "time_of_arrival": 14,
        },
    )
    depth_frame = FakeFrame(
        timestamp_ms=1_701_234_567_891.456,
        timestamp_domain="hardware_clock",
        metadata={
            "frame_timestamp": 22,
            "sensor_timestamp": 23,
        },
    )

    assert _frame_timestamp_ns(color_frame) == 1_701_234_567_890_123_008
    assert (
        _frame_stem_from_sensor_timestamp_ns(_frame_timestamp_ns(color_frame))
        == "1701234567890"
    )
    assert _timestamp_domain_name(depth_frame) == "hardware_clock"

    metadata = _realsense_timestamp_metadata(color_frame, depth_frame, FakeRs)

    assert metadata["color_timestamp_domain"] == "global_time"
    assert metadata["depth_timestamp_domain"] == "hardware_clock"
    assert metadata["color_backend_timestamp"] == 11
    assert metadata["color_time_of_arrival"] == 14
    assert metadata["depth_frame_timestamp"] == 22
    assert metadata["depth_sensor_timestamp"] == 23
