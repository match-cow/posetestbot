# PoseTestBot Installation

PoseTestBot is acquisition-first: the repository captures, calibrates,
synchronizes, optionally prepares ground-truth/masks, and exports BOP datasets.
Its only evaluation runtime is the optional Inspect-only official BOP19
dataset-validation path; it does not install or execute pose estimators.
Use Python 3.12 and `uv` for Python environment management. The project requires
`>=3.12,<3.13`; `uv` installs the matching interpreter when necessary. PoseTestBot targets only the
physical lab iiwa; normal setup and validation never execute capture automatically.

## Quick Setup

From the repository root:

```bash
bash scripts/install.sh --with-posegridgen --with-posetemplatecreator \
  --with-bop-toolkit
```

This safe project bootstrap:

- ensures `uv` is available,
- runs `uv sync --all-groups`,
- initializes and verifies the exact, clean PoseGridGen source submodule,
- initializes and verifies the exact, clean PoseTemplateCreator source submodule,
- initializes the exact, clean BOP Toolkit source submodule and synchronizes
  its separate locked `uv` runtime,
- checks required Python imports,
- checks optional acquisition runtimes,
- lists registered sensor adapters without opening hardware,
- verifies that the self-contained operator-console build is bundled.

The required Python environment also includes Matplotlib for reproducible,
headless calibration teaching plots plus `aiortc`, `aiohttp`, `aioice`, and
direct `av` support for the UGREEN room monitor's video-only WebRTC stream.
The worker binds offer/answer signaling to an ephemeral loopback port and runs
a local STUN binding responder on UDP port 3478. The Flask API proxies only
signaling; browsers use the advertised STUN port to obtain numeric candidates
and exchange media directly over the trusted lab LAN. Set
`POSETESTBOT_MONITOR_STUN_PORT` to use a different UDP port. TURN and Internet
NAT traversal remain out of scope.

If `UV_CACHE_DIR` is unset, the installer uses `/tmp/uv-cache`.
Browser binaries for Playwright UI tests are not installed by default.
Bun is not required for normal Python installation or runtime because the
locked production build is committed and packaged in the wheel.

Omit `--with-posegridgen` when this checkout only needs to consume existing
`calibration_target.v2` files. The Calibration Targets generator is then
reported unavailable, while calibration readers and the bundled UI continue
to work.

Omit `--with-posetemplatecreator` when this checkout only needs to browse,
classify, archive, restore, export, or metadata-import existing Workpiece
Catalogue entries and to read immutable pose-template bundles and run
selections. Client-side catalogue PLY previews continue to work. New CAD
inspection/conversion, exact slicing, pose-template preview, and PDF generation
are disabled until the pinned checkout is available.

Omit `--with-bop-toolkit` only when neither **Inspect → BOP Evaluation** nor the
evaluation-compatible **Pose + masks** ground-truth product is needed.
Annotation-free export and plain pose GT remain usable; the console reports the
missing optional runtime at the affected controls.

Use check-only mode to inspect an already configured environment without
installing or syncing:

```bash
bash scripts/install.sh --check-only
```

## Lab Host Setup

For an Ubuntu lab host where system packages and optional BlenderProc should be
installed by the script:

```bash
bash scripts/install.sh --with-system-packages --with-blenderproc \
  --with-bop-toolkit
```

`--with-system-packages` installs common Ubuntu packages for local development,
USB inspection, OpenCV runtime support, and build tooling. It does not install
vendor camera SDKs or proprietary packages.
The installed `v4l-utils` command is also used by the managed UGREEN monitor to
discover the camera's brightness range before a browser-requested automatic
brightness calibration; calibration remains unavailable if that control cannot
be inspected.

`--with-blenderproc` installs the validated BlenderProc 2.8.0 as a `uv` tool
when it is missing and replaces another detected BlenderProc version. The web
readiness check and render worker both reject every other version.

`--with-playwright-browsers` installs Chromium for Playwright browser UI tests
after the uv environment has been synchronized. Keep this opt-in on lab hosts
unless you are actively running browser coverage:

```bash
bash scripts/install.sh --with-playwright-browsers
```

Use `--with-web-build` only when changing the React/shadcn frontend. It requires
Bun, installs exactly the versions in `frontend/bun.lock`, removes stale build
output, and regenerates the bundled Flask assets. The Cell bundle includes
Three.js, React Three Fiber, and Drei; installed operation still requires
neither Bun nor a network connection:

```bash
bash scripts/install.sh --with-web-build
```

### Operator Console Run Storage

The web API permits run folders only below explicit server-approved roots. A
normal checkout enables both:

- `<repository>/working_data`; and
- `/mnt/working_data_ssd`, the lab acquisition SSD.

The top bar always shows the active acquisition's display name and exact folder
path; it does not contain the potentially long run list. Choose **Change** to
open **Inspect → Run folders**, where the active context, new-acquisition form,
and searchable/sortable existing-run chooser appear before the detailed storage
inventory. That detailed inventory has its own search and a bounded scrolling
table so storage actions remain usable as the run count grows. To start a run,
choose an approved storage root and one folder name;
the console derives the direct child `<root>/<run-folder>`, and Workflow writes
its `run_config.json` when setup is saved. Use a different sibling folder for
each acquisition (`<root>/run-a`, `<root>/run-b`, and so on). The optional
**Run display name** inside setup is human-readable metadata, defaults to the
folder name, and does not select or rename that folder. The Run folders page
also reports recursively measured size, saved sensor/object setup, and evidence
across both roots. Selection and inventory never open cameras or contact the
robot.

Inventory/recovery, confirmed deletion, and cross-root moves run as background
disk jobs and remain visible in **Jobs** after navigation. They cannot be
canceled after submission because an inventory refresh may be finishing an
interrupted, already confirmed transaction, and interruption during a
filesystem commit or permanent deletion is not a safe boundary. The active run
must be switched before either destructive action is available. A move accepts
only another configured run root, refuses an occupied destination, and leaves
a compatibility symlink at the original path so immutable path-bound evidence
remains resolvable.

Mutation requests bind both the discovered run-folder identity and the
inventory-time destination-root identity. They fail if either directory was
replaced or a mounted destination disappeared before the worker acquired its
disk lock. Trees containing nested filesystems or bind mounts are rejected
before isolation so moving or deleting one run cannot cross into separately
mounted data.

Move and delete workers keep atomic, fsynced transaction journals beside the
run roots. A later inventory refresh rolls back an uncommitted move, completes
a committed move, or resumes an already confirmed partial deletion. If path,
filesystem, or content evidence no longer matches, recovery preserves the
remaining trees, exposes a storage-maintenance warning and retained byte count
on **Run folders**, and blocks further mutations until an operator repairs the
reported condition.

Additional roots can be appended with the platform
path-separator-delimited `POSETESTBOT_WEB_RUN_ROOTS` variable (`:` on Linux).
The configured entries are the exact roots offered as move destinations; the
setting does not authorize arbitrary filesystem paths. For example:

```bash
export POSETESTBOT_WEB_RUN_ROOTS=/srv/posetestbot-runs:/data/archive
export POSETESTBOT_WEB_DEFAULT_RUN_ROOT=/mnt/working_data_ssd/current_run
uv run posetestbot-web
```

`POSETESTBOT_WEB_DEFAULT_RUN_ROOT` selects the initial run folder but does not
bypass containment: it must resolve below one of the approved roots. The
Dashboard polls capacity from the filesystem that will contain the selected
run, including a not-yet-created child folder. It warns below the smaller of
500 GiB or 15% free and reports critical capacity below the smaller of 100 GiB
or 5% free.

## Manual Prerequisites

### uv

The installer can install `uv` through the official Astral installer when it is
missing. To install it manually, follow Astral's current uv installation
instructions, then verify:

```bash
uv --version
uv sync --all-groups
```

Run project scripts through `uv`:

```bash
uv run python scripts/robot_status.py --json
uv run posetestbot-web
```

### PoseGridGen Calibration Targets

Printable target generation is source-checkout-only. Initialize the committed
submodule at `third_party/PoseGridGen` and verify the pinned revision through
the installer:

```bash
bash scripts/install.sh --with-posegridgen
bash scripts/install.sh --check-only \
  --with-posegridgen --with-posetemplatecreator
```

The required revision is
`9e6975901fe096bf65f7b7b599d7b82461d2e67c`. Generation is disabled if the
checkout is missing, dirty, at another revision, lacks the required backend
files, or cannot provide the renderer/OpenCV capabilities. PoseTestBot loads
only PoseGridGen's backend models, errors, fitting, scene, and rendering modules
under a private namespace; FastAPI and Uvicorn are not runtime dependencies.

The generator supports DIN A1 through A6 (including A5 and A6), Letter, and
Legal paper. Immutable bundles created with the preceding pinned PoseGridGen
revision remain valid and selectable; only new generation requires the current
checkout.

Use the operator console's **Calibration Targets** page to preview, fit, and
generate immutable source/spec/PDF bundles, then select one for a configured
run. The complete artifact and placement contract is documented in
[`docs/POSEGRIDGEN_CALIBRATION_TARGETS.md`](docs/POSEGRIDGEN_CALIBRATION_TARGETS.md).

### Workpiece Catalogue and PoseTemplateCreator Object Ground Truth

The bundled **Workpiece Catalogue** page stores the test-object manifest and
UUID-addressed CAD assets below `working_data/object_catalog/`. Existing
entries, editable metadata, archive/restore controls, JSON metadata
import/export, bounded isometric thumbnails, and the selected object's single
interactive bounded 3D preview do not require an additional service or
database.

Importing a new PLY/STL/OBJ (with an optional PNG texture), inspecting and
converting its mesh, and generating printable pose templates use the source
checkout at `third_party/PoseTemplateCreator`:

```bash
bash scripts/install.sh --with-posetemplatecreator
bash scripts/install.sh --check-only --with-posetemplatecreator
```

The required revision is
`97ddb9b7b756912deb8c2d2d6dde186b461e5d9d`. PoseTestBot refuses generation
when the checkout is missing, dirty, or at another revision. It privately loads
only the upstream constants, models, secure mesh parser, stable-orientation and
bounded-preview extraction, exact contour slicer, scene, and PDF renderer; the
upstream FastAPI server and React application are never imported or embedded.
The Python environment includes NetworkX for the pinned Trimesh stable-pose
path. Existing immutable bundles remain readable when the optional checkout is
unavailable. Catalogue storage, lifecycle, metre/mm geometry revisions, API,
and metadata-portability details are documented in
[`docs/WORKPIECE_CATALOGUE.md`](docs/WORKPIECE_CATALOGUE.md). The printable
template and object-GT workflow is documented in
[`docs/POSETEMPLATECREATOR_OBJECT_GT.md`](docs/POSETEMPLATECREATOR_OBJECT_GT.md).

### Workflow BOP Ground Truth

After the guided object-dataset workflow verifies its base BOP image/model
export, step 6 offers two explicit derived products:

- `pose` loads the immutable objects and calibrated cameras in BlenderProc
  2.8.0 and writes standard `scene_gt.json` model-to-camera rotations and
  translations. It does not fabricate visibility data and is deliberately not
  BOP19 evaluation-ready.
- `pose_and_masks` starts with the same pose evidence, then uses the pinned
  official BOP Toolkit Vispy renderer and the captured depth with the BOP19
  15 mm visibility tolerance. It writes one full-frame binary PNG per GT
  instance below `mask/` and `mask_visib/`, plus `scene_gt_info.json`
  `bbox_obj`/`bbox_visib` ROI, pixel counts, and visibility fractions.

Install both optional runtimes for the complete product:

```bash
bash scripts/install.sh --with-blenderproc --with-bop-toolkit
```

The console queues a single CPU/render/disk job and recovers it through
**Jobs** after navigation. For diagnostics, the same safe orchestration can be
started directly:

```bash
uv run python scripts/run_bop_annotations.py working_data/example_run \
  --mode pose
uv run python scripts/run_bop_annotations.py working_data/example_run \
  --mode pose_and_masks
```

Both commands require an already exported BOP v5 pose-estimation dataset, a
confirmed run-owned pose-template placement, exact matched robot poses,
rectified RGB-D frames, and the selected immutable calibration snapshot. They
write derived preparation/render evidence, replace `bop/` transactionally, and
record status below `processed/bop_annotations/`; they never alter raw capture
or selection/calibration snapshots.

### Inspect-only BOP Evaluation

The **Inspect → BOP Evaluation** page is a narrow dataset-validation exception
to PoseTestBot's acquisition-only boundary. It consumes an already exported,
annotation-bearing BOP v5 dataset and either:

- an immutable, already compatible standard BOP19 result CSV selected from the
  run's registered results; or
- a deterministic test-only result generated by adding small translation and
  rotation offsets to the dataset GT.

It does not run a pose estimator, convert an estimator's proprietary output,
or register an acquisition-pipeline stage. Install and verify its runtime with:

```bash
bash scripts/install.sh --with-bop-toolkit
bash scripts/install.sh --check-only --with-bop-toolkit
```

The required BOP Toolkit revision is
`cea62d651c7e395b2e1962b9749e4e89693c6ac4`. The installer initializes
`third_party/bop_toolkit`, rejects a missing, dirty, or differently pinned
checkout, and synchronizes the committed lock at
`tools/bop_toolkit_runtime/uv.lock`. The toolkit stays unmodified: a
PoseTestBot runtime-only adapter supplies the run's generic dataset layout.

The isolated runtime is intentional. The pinned official toolkit requires
NumPy below 2, while the main PoseTestBot environment requires NumPy 2 or
newer. Always update this optional environment with
`uv sync --project tools/bop_toolkit_runtime`; do not install toolkit
dependencies into the main project environment. Official VSD rendering uses
Vispy with EGL in headless mode and therefore also needs a working host
EGL/OpenGL implementation.

### Optional external cluster controller

Cluster storage and every pose-estimator runtime live in the separate
`match-cow/posetestbot-cluster` repository; do not install its drivers,
runtimes, or SSH credentials into PoseTestBot. Deploy that controller as a
loopback-only workstation service. Its archive capability is independently
gated from estimator execution, and FoundationPose is the first driver behind
its estimator registry. PoseTestBot exposes only service and capability
status, fixed-service start/stop, estimator-job
submission/logs/cancellation, immutable result import/download, and archive
copy/restore.

Use the companion's canonical
[FoundationPose cluster setup](https://github.com/match-cow/posetestbot-cluster/blob/main/docs/FOUNDATIONPOSE_CLUSTER_SETUP.md)
for the complete controller, archive-first, user-systemd, private SIF,
qualification, manifest, and acceptance workflow. Those deployment
instructions are intentionally not duplicated here.

At the PoseTestBot boundary, point the web process at the companion's existing
mode-0600 `.env` and name the fixed user-systemd unit:

```bash
POSETESTBOT_CLUSTER_ENV_FILE=/absolute/path/to/posetestbot-cluster/.env
POSETESTBOT_CLUSTER_SERVICE_UNIT=posetestbot-cluster.service
uv run posetestbot-web
```

`POSETESTBOT_CLUSTER_ENV_FILE` must be an absolute regular non-symlink file
with mode 0600. When it is present, cluster integration is enabled and the URL
defaults to the companion's `POSETESTBOT_CLUSTER_HOST` and
`POSETESTBOT_CLUSTER_PORT`. The web process must run as the same user that owns
the fixed service. It reads only the loopback host/port and API token; changing
them requires a web-process restart. SSH credentials, cluster paths, runtime
manifests, estimator settings, and service commands cannot be entered in the
browser or returned by `/cluster/*` responses.

The Dashboard reports service, connection, archive readiness, and estimator
readiness as distinct states. Lifecycle actions are fixed server-owned
`systemctl --user --no-block` jobs. Stopping requires confirmation because it
can interrupt archive transfer, estimator staging, or result collection.
Remote SLURM identity remains durable and is reconciled after restart.

For a persistent workstation deployment, instantiate both examples in
`deploy/systemd/` below `~/.config/systemd/user/`, replacing every `@...@`
placeholder with an absolute path. Set `@POSETESTBOT_UV_BIN_DIRECTORY@` to the
absolute directory reported by `dirname "$(command -v uv)"`; queued workers
need that directory even when the web executable itself is an absolute path.
Point the web unit's mode-0600 environment file at the two settings above, then
enable both units:

```bash
systemctl --user daemon-reload
systemctl --user enable --now posetestbot-cluster.service posetestbot-web.service
systemctl --user status posetestbot-cluster.service posetestbot-web.service
```

Both examples use `Restart=always`; an unexpected process exit is restarted,
while an explicit `systemctl --user stop` remains stopped. On a headless lab
workstation, user lingering must also be enabled by the workstation
administrator so the user manager starts at boot without an interactive
login. After rebuilding frontend assets or changing the web environment,
deploy with a controlled `systemctl --user restart posetestbot-web.service`.
Do not attach a source-file watcher to the production service: a restart can
interrupt process-owned local jobs and must happen at an intentional deploy
boundary.

In the console, the fixed **Cluster controller** card is in the Dashboard's
first readiness row. **Start** launches the configured companion; **Cluster
storage** opens the archive/restore panel directly below the **Run folders**
page header. **Pose Estimation** also links back to that storage panel, so
archive transfer does not depend on estimator readiness.

A controller result is imported only when the local dataset identity still
matches its staged snapshot. An intact historical CSV remains downloadable
after dataset drift, but evaluation remains blocked until the matching
snapshot is selected or restored.

Imported results must already use the BOP filename convention and the exact
`scene_id,im_id,obj_id,score,R,t,time` header. Each result is copied and
hash-bound below `processed/bop_evaluation/results/<result_id>/`. Queued
official BOP19 VSD, MSSD, and MSPD evaluation writes its immutable request,
progress, adapter/provenance, toolkit outputs, and final metric report below
`processed/bop_evaluation/evaluations/<evaluation_id>/`. These are derived
inspection artifacts; the exported `bop/` dataset and raw capture evidence are
not modified.

New annotation-bearing exports use the official BOP19 visibility target rule
(`visib_fract >= 0.1`). Inspect warns when an older export's target list does
not match that rule; such a run can validate its own exported contract, but its
scores must not be presented as leaderboard-comparable.

### RealSense D435

The Python package `pyrealsense2` is declared in `pyproject.toml` and installed
by `uv sync`. Physical RealSense discovery may still require USB access and
RealSense udev rules on the lab host.

Check visibility:

```bash
uv run python scripts/sensor_status.py --json
```

For the separate three-RealSense service/full-capture maintenance milestone,
require all three SDK-addressable devices explicitly:

```bash
uv run python scripts/sensor_status.py --expected realsense_d435=3 --check-expected
```

The expected count is based on cameras addressable through `pyrealsense2`.
RealSense devices seen only through USB descriptors remain in the status output
for troubleshooting, but do not pass capture-readiness checks. SDK-enumerated
cameras with a known `usb_type_descriptor` below USB 3 also fail readiness and
capture-plan preflight. Older status records that do not contain transport
metadata remain readable; a fresh status check is required before real capture.

Before capture, require every enabled/selected serial to be SDK-addressable and
to report a 3.x-or-newer descriptor when the transport version is known. All
three configured serials are required only for the separate three-camera
service/full-capture milestone; a disabled serial remains recorded but is
excluded from the current run. A USB2 fallback can be caused by a
marginal/non-SuperSpeed cable, port, connector, hub power, or an overcommitted
USB controller. Reseat or power-cycle only the affected USB connection without
moving its camera mount, use known-good SuperSpeed paths, and rerun the status
command. `lsusb -t` is useful read-only topology evidence, but the SDK
descriptor and successful stream warmup remain the capture gates.

When supported by the installed SDK and camera, status also records
`firmware_version` and the SDK's `recommended_firmware_version`. A numeric
difference produces a troubleshooting warning only; it does not weaken USB
readiness or prove that firmware caused a transport failure. PoseTestBot never
flashes camera firmware. Any persistent firmware change requires a separately
reviewed maintenance procedure and explicit device-specific authorization.

The **Devices** page labels each camera **Capture-ready**, **Not
capture-ready**, or **Disconnected** and shows the readiness reason. A camera
that is not ready cannot start a preview or snapshot or be newly selected for a
run; if it was already selected, it can still be deselected. In **Workflow →
Run Setup**, the **Enabled for capture and calibration** checkbox retains a
disabled camera's identity and metadata while excluding it from work. Keep at
least one camera enabled, then regenerate capture-plan and preflight artifacts
after any enable/disable change.

The **Default operator alias** on **Devices** is a reusable lab default stored
in the repository `working_data/sensor_aliases.json`. Save it with the
per-camera **Save alias** action; the inline unsaved state remains visible
until the server returns the saved value. Each field-level save retains records
for cameras that are currently disconnected. Workflow step 1 snapshots that
default as the editable, run-owned
`capture.sensors[].operator_alias` in `run_config.json`, with `display_name`
retained as the compatibility-facing effective label. A later edit to the
Devices default does not rename an existing run. Capture planning copies the
alias into `capture_plan.json` and `dataset_manifest.json` while physical
identity and folder naming remain bound to sensor type and device ID.

The **Mounting default** and supported **Orientation default** selectors on
**Devices** write that same lab-default file immediately and report success
only after the server returns the saved state. A failed immediate write reverts
the selector. Existing runs keep their run-owned values. Change those
explicitly with **Mounting for this run** and **Image orientation for this
run** in Workflow step 1, then save setup. Switching either value also requires
calibration evidence compatible with the new `static`/`eye_in_hand` and
normal/inverted interpretation. The **Include in next run** checkbox is only a
browser-local draft; Workflow step 1 owns durable camera membership.

One camera-calibration recording must contain one mounting group. An all-static
group uses a grid rigidly attached to `robot_flange`; select unknown placement
and let the static-camera solver estimate the attachment offset while solving
each `camera -> template_base` transform. The robot-carried grid supplies
multi-pose geometric excitation. PoseTestBot does not use the static cameras to
track the robot hand at runtime. An all eye-in-hand group uses a grid fixed
relative to `template_base`. Record the two groups in separate fresh runs,
publish them separately, and assign their exact per-camera sources together
only when configuring the later object-dataset run. Camera and target mounting
cannot be changed after raw capture evidence exists.

Static `camera -> template_base` profiles are reference-frame dependent. New
profiles retain the exact `sunrise_reference_frame_path` observed in
`robot_pose.v1` packets. Workflow step 1 exposes this as the required
**Robot-pose Sunrise reference**. Both static-camera calibration and ordinary
object capture use `/PoseTestBot/PoseTemplateBase`, so a promoted static
profile directly locates that camera in the frame of the printed pose template
and its objects. `/PoseTestBot/TemplateBase` remains only the parent of the
nine-frame calibration application's taught motion waypoints; it is not the
pose-stream reference or static-calibration result frame. The single-frame
static-camera alternative instead teaches
`/PoseTestBot/PoseTemplateBase/CalibrationStaticBottomMiddle`, moves 50 mm in
its local +Z direction to the generated center, and keeps the calibration
pattern at or above that taught bottom anchor. See
[the controller contract](docs/IIWA_SINGLE_FRAME_STATIC_CAMERA_CALIBRATION.md).
The same setting is available from the CLI:

```bash
uv run python scripts/create_run_config.py working_data/object_run \
  --robot-pose-sunrise-reference-frame-path /PoseTestBot/PoseTemplateBase
```

Selection fails closed when the destination omits this expectation, the static
profile predates v1 path provenance, or the paths differ. Existing raw pose
packets are rechecked as well. Preexisting static profiles produced from poses
expressed in `/PoseTestBot/TemplateBase` are not relabelled as
`/PoseTestBot/PoseTemplateBase`; recalibrate them against the intended result
frame. An eye-in-hand `camera -> robot_flange` profile does not depend on this
world-frame choice. Matching path strings establish software provenance only:
commissioning must still prove that a persistent Sunrise frame was not retaught
and is aligned to the intended physical datum.

#### Timestamp-aligned capture

`run_config.v3` supports one synchronization mode: `timestamp_aligned`.
RealSense, OAK-D Pro, and ZED 2i recordings retain their camera timestamp and
host-receive evidence; non-destructive synchronization pairs eligible frames
with the robot pose stream and records the applied calibration offset and pose
gap. PoseTestBot does not configure trigger roles, qualify a sync harness, or
claim simultaneous exposure across cameras.

### OAK-D Pro

DepthAI v3 is declared in `pyproject.toml` and installed by `uv sync`. Physical
OAK-D Pro discovery may still require USB access and udev permissions.

Check visibility:

```bash
uv run python scripts/sensor_status.py --expected oak_d_pro=1 --check-expected
```

The operator console supports an OAK-D Pro RGB preview at 640×480/6 fps. It
uses a non-blocking DepthAI v3 queue with a single latest frame. The Snapshot
control remains a one-frame aligned 1280×720 RGB-D acquisition, matching the
RealSense snapshot contract.

The OAK-D Pro participates through the same timestamp-aligned acquisition
contract as the other supported cameras.

### ZED 2i

The Stereolabs ZED SDK and `pyzed.sl` Python module are not ordinary PyPI
dependencies, so they are not installed by `uv sync` or by `scripts/install.sh`.
Install them with Stereolabs' SDK installer for the lab host and Python version,
then verify:

```bash
uv run python scripts/runtime_status.py --json
uv run python scripts/sensor_status.py --expected zed_2i=1 --check-expected
```

The current USB ZED 2i participates through the same timestamp-aligned
acquisition contract.

### BlenderProc

BlenderProc is only needed for non-dry-run optional GT scene validation and
pose generation. The full annotation mode uses the separately pinned BOP
Toolkit for masks and visibility against captured depth. Dry-run render
planning and ordinary acquisition checks do not require BlenderProc.
Explicit objectless render plans also skip BlenderProc completely.
Pose-template duplicate-instance GT is validated with BlenderProc 2.8.0. The
renderer rejects other versions before producing derived GT evidence.

Install through the PoseTestBot installer:

```bash
bash scripts/install.sh --with-blenderproc
```

Verify:

```bash
uv run python scripts/runtime_status.py --json
```

### Calibration Teaching Plot

Matplotlib is a direct project dependency installed by `uv sync`. Regenerating
the committed iiwa Workbench teaching SVG and PNG is headless and does not open
the robot or cameras:

```bash
MPLCONFIGDIR=/tmp/posetestbot-mpl UV_CACHE_DIR=/tmp/uv-cache \
  uv run python scripts/plot_iiwa_calibration_teaching_plan.py
```

The script validates `iiwa/calibration_teaching_plan.v2.json` and writes
`docs/images/iiwa_calibration_teaching_plan.svg` plus the corresponding PNG.
The procedure and printable sign-off sheet are linked from
`docs/IIWA_CALIBRATION_VARIANCE_PROPOSAL.md`.

### Playwright Browser Tests

The Python Playwright package is a dev dependency installed by
`uv sync --all-groups`, but browser binaries are intentionally optional. Install
Chromium only when running browser UI coverage:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run playwright install chromium
```

Then run the operator-console and sensor-preview browser tests:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -m playwright \
  tests/test_web_console_playwright.py tests/test_web_preview_playwright.py
```

The default `uv run pytest` selection excludes these marked browser modules, so
ordinary tests do not require the optional Chromium download.

The room-monitor coverage in that file uses an in-process synthetic aiortc
video track. It does not open the UGREEN camera or any RGB-D acquisition
device.

### UGREEN Room Monitor

The UGREEN USB camera (`0c45:2283`) is owned by a hidden managed service using
the resource `monitoring_camera:0c45:2283`. The service starts lazily and does
not open the V4L2 node until a browser requests WebRTC media. It requests MJPEG
640×480 at 30 fps with a one-frame V4L2 buffer, publishes VP8-preferred WebRTC
video, and releases the camera after 15 seconds without a connected peer. VP8
payloads are capped at 1100 bytes so the complete RTP datagram remains below
the 1280-byte Tailscale interface MTU after transport overhead. It has no JPEG
fallback. The generic RGB-D sensor preview controls remain latest-frame JPEG
streams.

Managed services are excluded from the normal Jobs list and held-resource
banner. Use `GET /jobs?include_services=1` for diagnostics. Monitor health is
persisted as `monitor_webrtc.v2` with one-second heartbeats, camera state,
capture/media counters, frame timestamps, peer counts, STUN port, and a
concrete failure reason. The status also records `vp8_packet_max_bytes` for
transport diagnostics. Legacy v1 artifacts remain readable but are never reused
as live state.

Starting or retrying the monitor from the dashboard is safe with respect to
the robot: it queues only the monitor worker and never runs an acquisition
pipeline or robot command. A physical monitor smoke test still requires
explicit operator authorization because it opens the USB camera.

All commands queued by any supported web entry point run behind a persisted
process supervisor. On graceful web shutdown, workload groups receive SIGTERM
and have five seconds to exit before SIGKILL. On a forced web-app SIGKILL,
Linux parent-death signaling wakes each supervisor, which verifies the owner
PID/start time and terminates the complete workload descendant group.

### Operator Console Development

The frontend lives in `frontend/` and follows the shadcn Vite layout with
React, TypeScript, Tailwind, Radix primitives, HashRouter, TanStack Query,
React Hook Form, Zod, Three.js, React Three Fiber, and Drei. Its production
output is `posetestbot/web/static/ui/`. The selected Workpiece Catalogue detail
loads the exact canonical PLY with those bundled client-side Three.js
dependencies. Compact catalogue cards use bounded, topology-scored recognition
meshes generated with the pinned `fast-simplification==0.1.13` runtime
dependency; the pose-template editor retains its smaller interaction-oriented
preview tier. No server rendering service or database is required.

```bash
cd frontend
bun install --frozen-lockfile
bun run typecheck
bun run lint
bun run build
```

Run the Flask server in another terminal when using `bun run dev`; Vite proxies
the existing API routes to `127.0.0.1:5000`. Never point browser tests at lab
hardware: use the mocked Playwright fixtures.

## Real Robot Profile

Robot status is read-only:

```bash
uv run python scripts/robot_status.py --json
```

Create and inspect a physical capture plan without executing it:

```bash
uv run python scripts/create_run_config.py working_data/test_run
uv run python scripts/run_pipeline_sequence.py working_data/test_run \
  --sequence real_full_capture_validation --plan-only
```

## Validation

Recommended local validation:

```bash
bash -n scripts/install.sh
bash scripts/install.sh --help
bash scripts/install.sh --check-only \
  --with-posegridgen --with-posetemplatecreator --with-bop-toolkit
cd frontend && bun run typecheck && bun run lint && bun run build
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_runtime_status.py tests/test_hardware_status.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -m playwright \
  tests/test_web_console_playwright.py tests/test_web_preview_playwright.py
UV_CACHE_DIR=/tmp/uv-cache uv build
git diff --check
```

## Troubleshooting

- `uv` missing: run `bash scripts/install.sh` without `--check-only`, or install
  `uv` manually and rerun `uv sync --all-groups`.
- Python import smoke fails: rerun `uv sync --all-groups`; add or update
  dependencies with `uv add ...` rather than hand-editing lock files.
- Calibration target generation is unavailable: run
  `git submodule update --init --checkout third_party/PoseGridGen`, confirm the
  checkout is clean at the pinned revision, then run
  `bash scripts/install.sh --check-only --with-posegridgen`.
- Pose-template inspection or generation is unavailable: run
  `git submodule update --init --checkout third_party/PoseTemplateCreator`,
  confirm the checkout is clean at the pinned revision, then run
  `bash scripts/install.sh --check-only --with-posetemplatecreator`. Existing
  Workpiece Catalogue records/assets, immutable bundles, and run selections
  remain readable without the source checkout; only new CAD conversion and
  pose-template geometry/rendering actions are unavailable.
- BOP evaluation is unavailable: run
  `git submodule update --init --checkout third_party/bop_toolkit`, confirm the
  checkout is clean at
  `cea62d651c7e395b2e1962b9749e4e89693c6ac4`, then run
  `bash scripts/install.sh --with-bop-toolkit`. Use
  `bash scripts/install.sh --check-only --with-bop-toolkit` to verify the
  existing isolated runtime without syncing it. If VSD rendering fails after
  the Python smoke passes, verify that the host provides a usable EGL/OpenGL
  stack; the worker selects headless EGL automatically.
- BOP result import is rejected: provide a canonical BOP19 CSV whose filename
  identifies the selected dataset/split and whose header is exactly
  `scene_id,im_id,obj_id,score,R,t,time`. PoseTestBot intentionally has no
  proprietary-result converter; conversion belongs with the external
  estimator/consumer project.
- Workpiece catalogue metadata import reports `skipped_missing_assets`: this is
  expected when matching UUID-addressed CAD assets are not installed locally.
  JSON import/export never embeds or restores CAD, canonical PLY, or PNG bytes;
  preserve or migrate the complete managed asset tree separately when binary
  portability is required.
- Room-monitor signaling is unavailable: inspect
  `GET /jobs?include_services=1`, confirm the managed `monitor-webrtc:ugreen`
  service is running, and inspect its `monitor_webrtc.v2` error reason. Allow
  the configured STUN UDP port (3478 by default) plus WebRTC media on the
  trusted lab LAN. The loopback signaling port is intentionally not exposed to
  browsers.
- Room-monitor diagnostics show packets arriving but zero received/decoded
  frames: confirm the active worker status reports
  `vp8_packet_max_bytes: 1100`. A worker started before the MTU fix must be
  restarted before retrying the browser connection.
- Plan the isolated UGREEN hardware smoke without opening the camera with
  `uv run python scripts/run_monitor_webrtc_smoke.py --plan-only`. Physical
  execution additionally requires explicit operator authorization and all
  three command acknowledgements: `--operator-authorized --allow-cameras
  --allow-real-robot`. Despite the shared lab safety gate, this monitor-only
  command contains no robot or acquisition-pipeline action.
- `pyzed.sl` missing: install the Stereolabs ZED SDK and Python bindings outside
  uv, then rerun `uv run python scripts/runtime_status.py --json`.
- Camera SDK imports succeed but devices are missing: check USB cabling, power,
  device permissions, and vendor udev rules on the lab host.
- Ground-truth generation is disabled: finish the base BOP v5 export, confirm
  the pose-template placement and selected calibration snapshot, then install
  BlenderProc with `bash scripts/install.sh --with-blenderproc`. The
  `pose_and_masks` product additionally needs
  `bash scripts/install.sh --with-bop-toolkit`.
- Playwright reports a missing Chromium executable: run
  `UV_CACHE_DIR=/tmp/uv-cache uv run playwright install chromium`, or use
  `bash scripts/install.sh --with-playwright-browsers`.
- Bundled web assets are missing: restore the committed
  `posetestbot/web/static/ui/` files or run
  `bash scripts/install.sh --with-web-build` on a machine with Bun.
- Real robot commands require deliberate `--allow-real-robot` and camera
  execution requires `--allow-cameras`.
