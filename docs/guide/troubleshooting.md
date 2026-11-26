# Troubleshooting and FAQ

## First checks

When something does not work, check these three places first:

1. **The reason next to the disabled button.** The console always explains
   why an action is unavailable.
2. **Jobs → Log** of the failed job. The last lines usually name the cause.
3. **Dashboard status cards.** Are the cameras connected? Is there enough
   storage? Is the cluster ready?

## Cameras

**A camera is missing on the Dashboard or Devices page.**
Check its USB cable and click **Refresh discovery** on **Devices**. RealSense D435
cameras need USB 3 ports and cables.

**Readiness says a camera is blocked although it is listed.**
Another process holds the camera: a preview on **Devices**, a still-running
or crashed recording, or another program. Stop the previews, check **Jobs**
for an active capture, and run the check again. If it persists, ask lab
staff.

## Workflow

**I cannot save the setup.**
A required choice is still empty or invalid, for example a frame rate that
not every ticked camera supports.
Or the active run belongs to the other workflow: a calibration run cannot
become a dataset run, and vice versa. Start a new run folder.

**The settings in step 1 are locked.**
The run already contains a recording. Settings that the recording depends on
cannot change anymore. Start a new run folder.

**I recorded with the wrong setup.**
Nothing is lost, because the old run stays as it was. Create a new run folder
and record again. Delete the old run on **Run folders** if nobody needs it.

**No calibration is offered for my cameras.**
The calibration must match each camera exactly: same camera, resolution,
mounting, and orientation. Check **Calibrations**, or
[calibrate](calibrate-cameras.md) with your current settings.

## Robot

**The robot does not move after Start recording.**
The robot application must be launched on the KUKA controller and waiting for
START. Check the job log on **Jobs**, and ask lab staff to check the
controller.

**Should I press "Exit idle program" when I am done?**
Only if your supervisor asks you to. It only ends a *waiting* program, and
during repeated calibration it forces a manual restart on the controller.
It is never an emergency stop.

## Results

**Pose Estimation or BOP Evaluation says the dataset is not suitable.**
Both need *pose ground truth plus masks*. If you chose a different outcome,
record a new run with the right choice in step 1.

**MP4 export is disabled.**
FFmpeg with libx264 is missing on the lab PC. Use the ZIP export, or ask lab
staff to install it (see [Installation](../getting-started/installation.md#pose-results-mp4-exports)).

**Can I delete a single bad frame or rename files in the run folder?**
No. Raw data is never edited by hand, because PoseTestBot verifies file
hashes and refuses manipulated inputs. Filter frames in your own analysis
instead.

## Still stuck?

Note the run folder name and the job ID from **Jobs**, then contact your
supervisor or the lab staff. Lab staff can find technical background in the
[Detailed workflow reference](../OPERATOR_WORKFLOWS.md).
