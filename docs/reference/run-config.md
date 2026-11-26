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
| `bop` | Recorded BOP ground-truth outcome (`none` until chosen after capture) |
| `subrun` | Optional object-dataset subrun lineage; absent for ordinary runs |

`subrun`, when present, contains exactly `parent_run_id` (the original run's
canonical UUID, never the run's own), `parent_run_root` (absolute path),
`variant` (trimmed text, 1–240 characters, describing the material,
environment or part copy), and ISO-8601 `created_at`. It is allowed only for
`pose_template` dataset runs and is preserved by later setup saves.

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
Calibration intent requires `none`. New dataset runs start with `none`.
Dataset processing always writes the base annotation-free BOP export. When the
operator queues the optional post-capture annotation job (`POST
/bop/annotations`), the chosen mode is recorded here atomically; only
`bop.annotation_mode` changes, so the camera contract is untouched.

`capture.fps` must be a rate every enabled sensor delivers natively at
`capture.resolution` (`supported_fps` in `posetestbot.sensors.registry`:
RealSense D435 and OAK-D Pro 6/15/30 fps at 720p; ZED 2i 15/30/60 fps at
720p and 15/30/60/100 fps at 360p). Dataset-intent setup requires
`capture.velocity_m_s >= 0.05`. Both are checked when a setup is created or
changed, and capture-plan preflight rejects an unsupported rate; existing
runs remain readable.

## Create and inspect

```bash
uv run python scripts/create_run_config.py working_data/example \
  --intent dataset

curl --fail-with-body --get http://127.0.0.1:5000/run-config \
  --data-urlencode run_root=working_data/example
```

`POST /run-config` requires `run_root` and `intent`. An omitted
`annotation_mode` keeps the run's recorded value (`none` for a new run). It
serializes replacement with selection writers, writes atomically, updates
`dataset_manifest.json`, and refuses camera-contract changes after raw capture
evidence exists.
