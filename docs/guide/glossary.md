# Glossary

Active run
:   The run folder that the console currently shows, selected in the top
    bar. It is remembered per browser.

ArUco grid / calibration target
:   A printed board with square markers. Cameras detect it to compute their
    calibration.

BOP
:   *Benchmark for 6D Object Pose estimation*, a standard dataset format and
    evaluation protocol. PoseTestBot exports BOP datasets and evaluates
    results with the official BOP Toolkit.

BOP19 result CSV
:   The standard file format for pose-estimator output. One line per
    estimated object pose.

Calibration (intrinsics / extrinsics)
:   *Intrinsics* describe a camera's lens (focal length, image centre,
    distortion). *Extrinsics* describe where the camera is relative to the
    robot.

Cluster / controller
:   The lab's GPU cluster and the companion service that runs pose estimators
    (such as FoundationPose) on it.

FoundationPose
:   The pose estimator currently available through **Pose Estimation**.

Ground truth (GT)
:   The known, correct object poses, derived from the printed pose template,
    its measured placement, and the robot poses. Optionally also object masks.

Job
:   A background task such as a recording, processing, or evaluation. Shown on
    **Jobs**.

Mask / visible mask
:   An image showing which pixels belong to an object. The *visible* mask
    excludes parts hidden behind other objects.

Mounting (robot-mounted / static)
:   Whether a camera is attached to the robot and moves with it, or stands
    still in the cell.

Pose (6D pose)
:   An object's position (3D translation) and orientation (3D rotation)
    relative to a camera.

Pose template
:   A printable sheet showing the outline of each object in a defined
    position. Placing objects on it gives their exact poses.

Promote / publish
:   Making a reviewed calibration available for reuse by other runs.

Readiness check
:   An automatic check that everything needed for a recording is in place,
    including briefly opening the cameras. It never moves the robot.

Rectification
:   Removing lens distortion from images so that a simple pinhole camera model
    applies.

Run / run folder
:   One recording (calibration or dataset) and everything derived from it, in
    one folder.

Scene (BOP)
:   In a PoseTestBot export, the image sequence of one camera.

Synchronization
:   Matching each camera frame to the robot pose at the same moment, using
    timestamps.

Workpiece
:   An object in the **Workpiece Catalogue**, with its CAD model and a
    permanent BOP object ID.
