# System, sensors, and monitoring API

These endpoints expose read-only capability/status evidence, run-owned hardware
snapshots, sensor aliases and previews, and the optional room-monitor service.
Status is not physical execution authorization.

## System and lifecycle

| Method and path | Contract |
| --- | --- |
| `GET /sensors/adapters` | Static sensor-family registry; does not open hardware |
| `GET /runtime/status` | Acquisition runtime visibility, including optional BlenderProc and ZED bindings |
| `GET /robot/status` | Read-only iiwa connectivity/status evidence |
| `GET /hardware/status?run_root=…` | Load the run-owned `hardware_status_report.json` |
| `POST /hardware/status` | Collect and write a hardware snapshot for `run_root`; `no_sensors` and `no_runtimes` may disable sections |
| `GET /system/lifecycle` | Report whether managed backend restart is configured and whether local jobs block it |
| `POST /system/restart-backend` | Request the fixed configured user-service restart after explicit confirmation; arbitrary commands/units are rejected |

Example snapshot request:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{"run_root":"working_data/example"}' \
  http://127.0.0.1:5000/hardware/status
```

The response embeds `hardware_status_report.v1`. An `overall_status` of
`error` is returned with HTTP `409` while retaining the report as evidence.

## Sensor aliases and status

| Method and path | Contract |
| --- | --- |
| `GET /sensors/status` | Discover current supported sensors and claimed-device state |
| `GET /sensors/aliases` | Read reusable lab-default operator aliases |
| `PUT /sensors/aliases` | Replace validated aliases in `working_data/sensor_aliases.json` |

Aliases are global defaults. Workflow setup snapshots `operator_alias` into
`run_config.json`; later alias changes do not rename an existing run. Physical
identity and sensor-folder naming remain bound to sensor type and device ID.

## Sensor previews and snapshots

| Method and path | Contract |
| --- | --- |
| `POST /sensors/previews` | Start/reuse per-camera preview jobs; claims camera resources |
| `GET /sensors/previews` | List active and recently known previews |
| `GET /sensors/previews/<job_id>` | Inspect one preview job |
| `GET /sensors/previews/<job_id>/latest.jpg` | Return the latest bounded preview image |
| `POST /sensors/previews/<job_id>/stop` | Stop one preview |
| `POST /sensors/previews/stop` | Stop selected/all preview jobs |
| `POST /sensors/snapshots` | Queue one-shot images for selected sensors |
| `GET /sensors/snapshots/<job_id>` | Inspect snapshot progress/result metadata |
| `GET /sensors/snapshots/<job_id>/image` | Return a completed snapshot image |

Preview endpoints are diagnostic acquisition tools. They can open cameras and
therefore conflict with physical capture jobs through the local resource model.

## Room monitor

| Method and path | Contract |
| --- | --- |
| `GET /monitoring/webcam` | Inspect the current monitor worker and health |
| `POST /monitoring/webcam` | Start or recover the configured monitor worker |
| `POST /monitoring/webcam/<job_id>/brightness/autocalibrate` | Proxy bounded brightness calibration to the worker |
| `POST /monitoring/webcam/<job_id>/webrtc/offer` | Exchange a bounded SDP offer for a browser WebRTC session |

The monitor is separate from dataset cameras and does not provide capture
readiness. Worker transport failures normally return `503`; invalid or stale
job identifiers return `404`/`409`.

See the [complete route index](../http-api-routes.md) for image and asset
aliases.
