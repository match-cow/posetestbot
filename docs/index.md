# PoseTestBot documentation

PoseTestBot is the lab's test bench for **6D object pose estimation**. A KUKA
iiwa robot moves RGB-D cameras around a set of objects. PoseTestBot records the
images together with the robot poses, calibrates the cameras, and exports a
ready-to-use [BOP](https://bop.felk.cvut.cz/) dataset. You can then run a pose
estimator on that dataset and measure its accuracy, all from one web page,
the **operator console**.

This documentation is written for students and researchers who *use* the
console for a project. You do not need to read the code or the API to get
your data.

<div class="grid cards" markdown>

-   **New here?**

    ---

    Read the 10-minute overview of the whole process, from the empty
    workcell to an evaluated dataset.

    [Quickstart →](quickstart.md)

-   **Find your way around**

    ---

    What each part of the console does, and where to look when something
    is running.

    [Console tour →](guide/console-tour.md)

-   **Calibrate the cameras**

    ---

    Record a printed grid and publish a camera calibration you can reuse
    for later recordings.

    [Calibrate cameras →](guide/calibrate-cameras.md)

-   **Record a dataset**

    ---

    Place your objects, let the robot record them, and export a BOP
    dataset with ground truth.

    [Record a dataset →](guide/record-dataset.md)

-   **Run a pose estimator**

    ---

    Send the dataset to the cluster (FoundationPose) and look at the
    estimated poses frame by frame.

    [Pose estimation →](guide/pose-estimation.md)

-   **Evaluate results**

    ---

    Official BOP scores, robot-consistency, and depth-quality studies for
    different surface finishes.

    [Evaluate pose accuracy →](guide/evaluate-poses.md)

</div>

!!! danger "Safety first"

    PoseTestBot moves a real industrial robot. Never start a recording unless
    you have been introduced to the cell by your supervisor, the workcell is
    clear, and you can reach the emergency stop. Read
    [Lab safety](guide/lab-safety.md) before your first session.

## The process at a glance

| Step | What happens | How often | Guide |
| --- | --- | --- | --- |
| 1. Calibrate cameras | The robot shows a printed grid to the cameras | Once per camera setup, then reused | [Calibrate cameras](guide/calibrate-cameras.md) |
| 2. Prepare objects | Upload CAD models, print a sheet that defines object poses | Once per object set | [Objects and templates](guide/objects-and-templates.md) |
| 3. Record a dataset | The robot moves the cameras around your objects; PoseTestBot exports BOP | Every recording | [Record a dataset](guide/record-dataset.md) |
| 4. Estimate poses | A pose estimator runs on the GPU cluster | Per experiment | [Pose estimation](guide/pose-estimation.md) |
| 5. Evaluate | BOP scores, robot consistency, depth quality | Per experiment | [Pose accuracy](guide/evaluate-poses.md), [depth quality](guide/evaluate-depth.md) |

Steps 1 and 2 are often already done for you: if the cameras have not been
moved and your objects are already in the catalogue, you can start directly
with step 3.

## Where to go for details

The **User guide** tab explains every task step by step. The
**Technical reference** tab is for when you need exact details, such as the
file formats, the HTTP API, the run-folder layout, or how a metric is
computed. Lab staff will find installation, robot integration, and
commissioning under **Lab administration**.
