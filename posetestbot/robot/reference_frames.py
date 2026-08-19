"""Reference-frame provenance for robot-pose streams and static calibration.

The symbolic ``template_base`` frame used by repository artifacts is not enough
to distinguish two different Sunrise Application Data frames.  Modern robot
pose packets therefore carry the exact absolute Sunrise path used to express
the streamed flange pose.  These helpers validate and retain that identity
without pretending that path equality proves a frame has not been retaught.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping


ROBOT_POSE_PACKET_SCHEMA_VERSION = "robot_pose.v1"
ROBOT_POSE_REFERENCE_SCHEMA_VERSION = "robot_pose_reference.v1"
POSE_TEMPLATE_BASE_SUNRISE_PATH = "/PoseTestBot/PoseTemplateBase"


def normalize_sunrise_reference_frame_path(value: Any) -> str:
    """Return one canonical absolute Sunrise Application Data frame path."""

    if not isinstance(value, str):
        raise ValueError("Sunrise reference frame path must be a string")
    path = value.strip()
    if not path.startswith("/") or path.endswith("/") or "//" in path:
        raise ValueError(
            "Sunrise reference frame path must be absolute, must not contain "
            "empty components, and must not end with '/'"
        )
    if any(component in {".", ".."} for component in path.split("/")):
        raise ValueError("Sunrise reference frame path must not contain . or ..")
    return path


def configured_sunrise_reference_frame_path(
    config: Mapping[str, Any],
) -> str:
    """Read the required exact robot-pose reference from a current run config."""

    frames = config.get("frames")
    robot_pose = frames.get("robot_pose") if isinstance(frames, Mapping) else None
    if not isinstance(robot_pose, Mapping):
        raise ValueError("Current run config has no robot-pose frame contract")
    value = robot_pose.get("sunrise_reference_frame_path")
    if value is None:
        raise ValueError("Current run config has no Sunrise reference-frame path")
    return normalize_sunrise_reference_frame_path(value)


def robot_pose_reference_evidence(raw_poses: Mapping[str, Any]) -> dict[str, Any]:
    """Extract one immutable reference identity from a raw robot-pose artifact.

    Every record must retain the strict ``robot_pose.v1`` source packet,
    including its run ID and exact Sunrise reference-frame provenance.
    """

    if not isinstance(raw_poses, Mapping) or not raw_poses:
        raise ValueError("Raw robot pose artifact must be a non-empty JSON object")

    identities: set[tuple[str, str, str, str, str]] = set()
    pose_count = 0
    for key, raw_record in raw_poses.items():
        if not isinstance(raw_record, Mapping):
            raise ValueError(f"Robot pose {key!r} must be a JSON object")
        packet = raw_record.get("source_packet")
        if not isinstance(packet, Mapping):
            raise ValueError(
                f"Robot pose {key!r} must retain its robot_pose.v1 source_packet"
            )
        pose_count += 1
        schema_version = str(packet.get("schema_version") or "")
        run_id = str(packet.get("run_id") or "")
        from_frame = str(packet.get("from_frame") or "")
        to_frame = str(packet.get("to_frame") or "")
        if schema_version != ROBOT_POSE_PACKET_SCHEMA_VERSION:
            raise ValueError(
                f"Robot pose {key!r} source packet schema must be "
                f"{ROBOT_POSE_PACKET_SCHEMA_VERSION}"
            )
        try:
            canonical_run_id = str(uuid.UUID(run_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                f"Robot pose {key!r} source packet has an invalid run_id"
            ) from exc
        if run_id != canonical_run_id:
            raise ValueError(
                f"Robot pose {key!r} source packet run_id is not canonical"
            )
        if packet.get("packet_kind") != "pose":
            raise ValueError(
                f"Robot pose {key!r} source packet must have packet_kind=pose"
            )
        if from_frame != "robot_flange" or to_frame != "template_base":
            raise ValueError(
                f"Robot pose {key!r} source packet must map robot_flange to "
                "template_base"
            )
        path = normalize_sunrise_reference_frame_path(
            packet.get("sunrise_reference_frame_path")
        )
        if path != POSE_TEMPLATE_BASE_SUNRISE_PATH:
            raise ValueError(
                f"Robot pose {key!r} does not use {POSE_TEMPLATE_BASE_SUNRISE_PATH}"
            )
        identities.add((schema_version, run_id, from_frame, to_frame, path))
    if len(identities) != 1:
        raise ValueError(
            "Raw robot pose artifact changes Sunrise reference-frame identity"
        )
    schema_version, run_id, from_frame, to_frame, path = next(iter(identities))
    return {
        "schema_version": ROBOT_POSE_REFERENCE_SCHEMA_VERSION,
        "status": "verified",
        "packet_schema_version": schema_version,
        "run_id": run_id,
        "from": from_frame,
        "to": to_frame,
        "sunrise_reference_frame_path": path,
        "pose_count": pose_count,
    }


def verified_sunrise_reference_frame_path(value: Any) -> str | None:
    """Return the path from verified reference evidence, else ``None``.

    The function is intentionally strict for a mapping claiming ``verified``;
    malformed profile metadata must fail closed.
    """

    if not isinstance(value, Mapping) or value.get("status") != "verified":
        return None
    if value.get("schema_version") != ROBOT_POSE_REFERENCE_SCHEMA_VERSION:
        raise ValueError(
            "Verified robot-pose reference evidence has an unsupported schema"
        )
    if value.get("packet_schema_version") != ROBOT_POSE_PACKET_SCHEMA_VERSION:
        raise ValueError(
            "Verified robot-pose reference evidence must originate from robot_pose.v1"
        )
    if value.get("from") != "robot_flange" or value.get("to") != "template_base":
        raise ValueError(
            "Verified robot-pose reference evidence must map robot_flange to "
            "template_base"
        )
    return normalize_sunrise_reference_frame_path(
        value.get("sunrise_reference_frame_path")
    )
