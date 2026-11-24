# Lab safety

The console can move a KUKA LBR iiwa industrial robot. The software has
several safeguards, but **you** are responsible for the cell while a
recording runs.

## Rules for every session

1. **Get introduced first.** Do not start robot motion until your supervisor
   has shown you the cell, the KUKA smartPAD, and the emergency stop.
2. **Stay with the robot.** Watch the cell for the whole recording. Keep the
   emergency stop within reach.
3. **Clear the workcell.** Before you confirm a recording, make sure nobody is
   inside the cell, and that cables, tools, and loose objects cannot collide
   with the robot or the cameras.
4. **Check the setup on screen.** The authorization dialog shows the run
   folder, the robot address, and the speed. Only confirm when they are what
   you expect.
5. **One recording per run folder.** Start a new run folder for each new
   physical recording.

## What the buttons do and do not do

| Control | What it does | What it does **not** do |
| --- | --- | --- |
| **Check readiness** | Briefly opens the cameras to check that they deliver a frame | Never moves the robot |
| **Review and start capture** → checkbox → **Start recording** | Authorizes this one recording: opens the cameras and moves the robot | The permission is not remembered. Every recording needs a fresh confirmation |
| Dashboard **Start program** | Manually sends START to the waiting robot program (for tests) | Not needed for guided recordings, which send START themselves |
| Dashboard **Exit idle program** | Ends a robot program that is *waiting* | **Not an emergency stop.** It cannot stop a moving robot |
| Cancel a job on **Jobs** | Stops the local recording processes | Does not stop the robot. Use the emergency stop or smartPAD in an emergency |

!!! warning "In an emergency, use the hardware emergency stop"

    No button in the web console stops a moving robot. Use the emergency stop
    or the smartPAD.

!!! note "During repeated calibration"

    Do not press **Exit idle program** between calibration recordings. It
    quits the waiting calibration program on the robot, and someone then has
    to restart it manually on the controller.

## Status words in the console

The console uses status words precisely. None of them means "the robot may
move":

- **Configured**: settings are saved.
- **Connected**: a device or service answered.
- **Verified**: a specific check passed.
- **Ready**: everything needed for a step is in place.

Only your explicit confirmation in the recording dialog authorizes motion.

??? info "Technical details"

    The recording request carries two separate permissions (`allow_cameras`
    and `allow_real_robot`). Both must be explicitly true in the same
    request; the console collects them with one checkbox. See
    [Safety and authorization](../concepts/safety.md) for the full contract,
    and [Physical commissioning](../COMMISSIONING.md) for the lab acceptance
    tasks.
