# Run configuration (`run_config.v4`)

`run_config.json` is the current run-intent contract. PoseTestBot accepts
exactly `run_config.v4`; it does not migrate v3 or accept a generic pipeline
section. Physical execution acknowledgements are deliberately absent and must
be submitted afresh for each capture request.

## Top-level fields

| Field | Contract |
| --- | --- |
| `schema_version` | Exactly `run_config.v4` |
| `run_id` | Immutable UUID bound into robot-pose packets and run artifacts |
| `run_name` | Nonempty operator-facing name |
| `run_root` | Owning directory; web requests also enforce approved-root containment |
| `robot_profile` | Exact sole lab profile, including fixed robot and receiver endpoints |
| `capture` | Explicit intent, sensor contract, resolution, rate, velocity, and synchronization |
| `frames` | Robot reference-frame provenance, dataset frame, and typed fixed transforms |
| `dataset_mode` | `objectless` or `pose_template` |
| `pose_template` | Run-owned selection reference for pose-template datasets, otherwise `null` |
| `calibration_profiles` | Exact run-owned extrinsic-profile snapshot path or `null` |
| `intrinsic_calibration_profiles` | Exact run-owned intrinsic-profile snapshot path or `null` |
| `calibration_profile_selection` | Hash-bound v2 selection pointer or `null` |
| `calibration_target` | Hash-bound selected target bundle or `null` |
| `bop` | Explicit eventual annotation mode |

Unknown or retired fields, including `pipeline`, `object_folder`, and
`selected_objects`, are rejected.

## Capture contract

```json
{
  "capture": {
    "intent": "dataset",
    "resolution": "720p",
    "fps": 6,
    "velocity_m_s": 0.01,
    "sensors": [
      {
        "sensor_type": "realsense_d435",
        "device_id": "825412070181",
        "display_name": "Wrist camera",
        "operator_alias": "Wrist camera",
        "mounting_mode": "eye_in_hand",
        "enabled": true,
        "calibration_profile_id": null,
        "inverted": false,
        "metadata": {}
      }
    ],
    "synchronization": {
      "schema_version": "capture_synchronization.v1",
      "mode": "timestamp_aligned"
    }
  }
}
```

`intent` is exactly `calibration` or `dataset`. At least one sensor is enabled.
Sensor types must be exact registry identifiers: `realsense_d435`,
`oak_d_pro`, or `zed_2i`. Short aliases are rejected. Device identity,
mounting, supported resolution, and RealSense-only inversion are validated.

The synchronization object has exactly the two shown fields. Every camera
must provide current timestamp metadata; filename timestamps and missing-value
fallbacks are not accepted.

`operator_alias` is the run-owned label. `display_name` is the effective label
snapshotted for the run. Neither changes physical identity or folder naming.

## Frames and fixed transforms

```json
{
  "frames": {
    "robot_pose": {
      "from": "robot_flange",
      "to": "template_base",
      "convention": "kuka_abc_radians",
      "sunrise_reference_frame_path": "/PoseTestBot/PoseTemplateBase"
    },
    "dataset_reference_frame": "template_base",
    "fixed_transforms": []
  }
}
```

Robot-pose reference metadata must agree with strict `robot_pose.v1` packets.
Each fixed transform has `from`, `to`, a normalized four-value
`rotation_quaternion_wxyz`, a three-value `translation_mm`, and an explicit or
default `source`.

## Immutable selections

`pose_template` datasets reference `pose_template_selection.json`; objectless
runs must keep `pose_template` null. Reusable calibration selection always
uses `calibration_profile_selection.v2` and exact snapshots below
`processed/calibration_inputs/<bundle_sha256>/`. Target and pose-template
selection APIs write their own hash-bound objects; clients should not invent
asset paths or hashes.

`bop.annotation_mode` is one of `none`, `pose`, or `pose_and_masks`.
Calibration intent requires `none`. Dataset processing always writes the base
annotation-free BOP export; the configured non-`none` mode is fulfilled later
by the explicit optional annotation job.

## Create and inspect

```bash
uv run python scripts/create_run_config.py working_data/example \
  --intent dataset --annotation-mode none

curl --fail-with-body --get http://127.0.0.1:5000/run-config \
  --data-urlencode run_root=working_data/example
```

`POST /run-config` requires `run_root`, `intent`, and `annotation_mode`. It
serializes replacement with selection writers, writes atomically, updates
`dataset_manifest.json`, and refuses camera-contract changes after raw capture
evidence exists.
