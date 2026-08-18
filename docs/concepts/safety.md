# Safety and authorization

PoseTestBot separates **status**, **configuration**, **readiness evidence**, and
**physical execution authorization**. They are not interchangeable states.

## State meanings

| State | Meaning | Does it authorize motion? |
| --- | --- | --- |
| Configured | Required values were written and validated structurally | No |
| Connected | A process or device answered a connection/status request | No |
| Verified | Specific evidence passed a named check | No |
| Ready | All prerequisites for a named operation are present | No |
| Authorized | Operator explicitly submitted the current physical execution gates | Only for that gated request |

## Fresh capture gates

Writing the capture execution plan requires both literal JSON booleans in the
same request:

```json
{
  "run_root": "working_data/example",
  "allow_cameras": true,
  "allow_real_robot": true
}
```

Strings such as `"true"`, saved browser preferences, earlier acknowledgements,
and successful status calls do not satisfy this contract. Capture preflight
separately requires a fresh literal `allow_real_robot: true`.

## IIWA constraints

- The lab KUKA iiwa is the sole robot profile.
- Never execute physical capture without explicit operator authorization and
  both execution safety gates.
- The iiwa UDP `STOP` command is not a safety stop. It cannot interrupt active
  motion and exits the waiting calibration program.
- During repeated calibration, do not send UDP `STOP`; a manual Sunrise
  application restart would be required.
- Read-only status commands never grant capture permission.

## Failure policy

Missing, corrupt, contradictory, or non-reproducible evidence fails closed.
When calibration input evidence is complete and internally valid, this
research system may retain a result with prominent quality warnings instead of
blocking solely on conservative production-metrology thresholds. That
tolerance never weakens physical gates, containment, raw-data preservation, or
artifact-integrity validation.

## Safe software-only checks

```bash
uv run python scripts/robot_status.py --json
uv run python scripts/sensor_adapters.py --json
uv run python scripts/runtime_status.py --json
uv run python scripts/run_pipeline_sequence.py working_data/test_run \
  --sequence real_full_capture_validation --plan-only
```

Sensor discovery may query attached devices but does not start a physical
capture sequence. Hardware-touching or long-running work submitted through the
web console remains visible in **Jobs**.
