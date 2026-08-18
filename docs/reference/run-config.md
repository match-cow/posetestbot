# Run configuration (`run_config.v3`)

`run_config.json` records run intent. It selects sensors, frame conventions,
dataset mode, immutable calibration/template inputs, and the default pipeline
sequence. It does **not** persist physical execution authorization.

## Top-level fields

| Field | Contract |
| --- | --- |
| `schema_version` | Exactly `run_config.v3` |
| `run_name` | Human-readable run name |
| `run_root` | Run directory, subject to web containment when used through HTTP |
| `robot_profile` | Sole real-lab iiwa profile; `mode` must be `real` |
| `capture` | Resolution, FPS, velocity, participating sensors, and synchronization |
| `frames` | Robot-pose convention, dataset reference frame, and fixed transforms |
| `dataset_mode` | `objectless` or `pose_template` |
| `pose_template` | Run-owned selection reference for `pose_template` mode, otherwise `null` |
| `calibration_profiles` | Path to exact run-selected extrinsic profiles or `null` |
| `intrinsic_calibration_profiles` | Path to exact run-selected intrinsic profiles or `null` |
| `calibration_profile_selection` | Hash-bound selection metadata or `null` |
| `calibration_target` | Hash-bound run-owned target selection or `null` |
| `pipeline` | Registered sequence, plan-only flag, and validated options |

Retired legacy fields such as `object_folder` and `selected_objects` are
rejected instead of silently migrated.

## Capture section

```json
{
  "capture": {
    "resolution": "720p",
    "fps": 6,
    "velocity_m_s": 0.1,
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

At least one sensor must be enabled. Sensor type, mounting mode, resolution,
orientation, and device ID are checked against registered contracts. The only
supported synchronization object contains exactly the schema and
`timestamp_aligned` mode; roles, group IDs, triggers, scopes, and alternative
implementations are rejected.

`operator_alias` is the run-owned label. `display_name` remains its effective
compatibility value. Neither field changes sensor physical identity or output
folder naming.

## Frame section

```json
{
  "frames": {
    "robot_pose": {
      "from": "robot_flange",
      "to": "template_base",
      "convention": "kuka_abc_radians"
    },
    "dataset_reference_frame": "template_base",
    "fixed_transforms": []
  }
}
```

Each fixed transform requires `from`, `to`, a normalized
`rotation_quaternion_wxyz` with four finite values, and `translation_mm` with
three finite values. The source defaults to `operator_configured`.

## Dataset and immutable selections

`objectless` mode cannot reference a pose template. `pose_template` mode uses:

```json
{
  "dataset_mode": "pose_template",
  "pose_template": {
    "selection_artifact": "pose_template_selection.json"
  }
}
```

Calibration selection metadata must reference
`calibration_profile_selection.json`, contain a lowercase SHA-256
`bundle_sha256`, and supply both exact profile paths. A selected target records
its target ID, run-relative bundle path, source/spec/PDF/configuration/geometry
hashes, and validated placement.

These objects are written by their domain selection APIs. Clients should not
fabricate hashes or paths.

## Pipeline section

```json
{
  "pipeline": {
    "sequence_id": "real_full_capture_validation",
    "plan_only": true,
    "options": {}
  }
}
```

The sequence must exist in `GET /pipeline/sequences`, and options are validated
against its stage registry. `allow_cameras` and `allow_real_robot` are rejected
anywhere inside persisted options. Those gates must be freshly submitted to
the physical capture endpoint.

## Create and inspect

```bash
uv run python scripts/create_run_config.py working_data/example
curl --fail-with-body --get http://127.0.0.1:5000/run-config \
  --data-urlencode run_root=working_data/example
```

The HTTP `POST /run-config` writer serializes replacement with the same lock
used by pose-template selection, validates all sections, writes atomically, and
updates the run manifest.
