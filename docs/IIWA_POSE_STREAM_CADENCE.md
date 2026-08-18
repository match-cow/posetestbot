# IIWA pose-stream cadence

## Current contract

The three IIWA motion applications use a separate read-only Sunrise cyclic
background task with a **10 ms `BestEffort` request** (100 Hz nominal). For the
KLI/UDP path, **50 Hz measured end to end is the commissioning target**.
Missing that target creates reviewable evidence; it does not by itself fail an
otherwise complete calibration attempt.

KLI and `BestEffort` are not hard real-time guarantees. A deterministic
sub-10-ms control path would require a separately designed and licensed
real-time interface such as Sunrise.FRI; it is outside this repository.

| Evidence band | Target | Interpretation |
| --- | ---: | --- |
| Cyclic request | 10 ms / 100 Hz nominal | Software scheduling request |
| Median host receive | at least 50 Hz | Commissioning target |
| p95 host gap | at most 25 ms | Commissioning target |
| Maximum host gap | at most 40 ms | Commissioning target |

Calibration uses the separate
`constant_latency_nearest_pose_motion_lomo_warn_keep_zero.v5` timing policy.
Nearest matches beyond 20 ms are warnings and 150 ms is the hard boundary.
Weak, ambiguous, boundary, or inconsistent automatic-offset evidence retains
recorded 0 ms timing with a visible warning. Missing/corrupt pose evidence and
failed geometric checks remain blocking.

## Source implementation

The shared implementation consists of:

- `iiwa/PoseTestBotPoseStreamTask.java`, an automatic-compatible
  `RoboticsAPICyclicBackgroundTask` using `CycleBehavior.BestEffort`;
- `iiwa/PoseTestBotPoseStreamFunction.java`, its application interface; and
- the full-capture, nine-frame calibration, and single-frame static-camera
  applications, which execute blocking motion while starting/stopping the
  independent sampler.

The background task contains no motion command or motion-parameter mutation.
It resolves the selected Application Data frame, reads the flange pose,
constructs a strict `robot_pose.v1` packet, and sends UDP. Every packet binds:

- the run UUID and increasing sequence;
- `robot_flange → template_base` and the exact Sunrise reference path;
- controller monotonic/wall diagnostic timestamps;
- target period, previous pose delta, and pose-query duration; and
- KUKA XYZ millimetres plus A/B/C radians.

The Python receiver requires this packet shape and retains it under
`source_packet`. Host receive/wall time is the synchronization clock; sender
time remains diagnostic because controller and host clocks are not assumed
aligned. A fatal sampler fault or a motion segment with zero poses cannot emit
a successful terminal marker.

## Workbench and physical acceptance

Before deployment:

1. Add the cyclic task through Workbench's background-task workflow and
   configure it for automatic start.
2. Include exactly one task-function provider and compile all current Java
   sources against the installed Sunrise.OS API.
3. Record the controller, project, source revision, tool/load, camera rig,
   frames, operator, reviewer, and compile/simulation evidence.
4. Commission each application's endpoint and swept path in T1 at reduced
   override under the lab safety process.
5. With explicit authorization and both capture acknowledgements, retain a
   supervised trial. Never send UDP Stop during repeated calibration.
6. Generate derived cadence evidence:

   ```bash
   UV_CACHE_DIR=/tmp/uv-cache uv run python \
     scripts/report_robot_pose_cadence.py <run-root> --write
   ```

The reporter writes `processed/robot_pose_cadence_report.json` and does not
modify raw poses. Compare sender and host evidence: slow sender cadence or long
query duration suggests controller workload; good sender cadence with poor
host cadence or sequence loss suggests delivery/receiver scheduling.

See [Physical commissioning](COMMISSIONING.md) and the [nine-frame teaching
checklist](IIWA_CALIBRATION_TEACHING_CHECKLIST.md).
