---
title: Full-load shift points
order: 38
context: GearboxEditDialog, FullLoadPanel
---
# Full-load shift points from the engine's torque

The shift map editor (**Tools > ZF 6HP > Shift map editor...**, and the same
under ZF 8HP) has a step called **Full-load optimum (engine torque)**. It sets
the full-throttle upshifts from the torque of an engine ECU kept in the **same
project**, then blends them into the rest of each line.

## Where the torque comes from

- **Engine ECU / Version**: an ECU set of the project. By default this is the
  version recorded as flashed, which is what is in the car, else the branch
  head. When that set is open in another tab, *Open tab, with unsaved edits*
  works from what the tab holds now.
- The torque is the [dyno chart](dyno.md)'s full-load curve. Quantity is turned
  into torque through that set's original (as read), under the conditions the
  dyno window last used.
- With no ECU in the project, or the gearbox not opened from a project, the step
  says so and changes nothing.

## How the point is worked out

At one road speed, a lower gear gives more thrust while the engine makes more
power there than it would in the next gear. The best upshift is where that
stops: the **crossover**. When the lower gear still pulls harder at the
**ceiling** (by default 200 rpm under the engine's cut-off), the ceiling sets the
point. The *Why* column says which.

Two corrections turn that speed into a map value:

- **Lead.** A map value is where the gearbox *starts* the shift; the engine
  keeps climbing until the ratio changes. The value is the crossover less that
  climb.
  - *From logs* measures it from full-throttle upshifts in the project's logs,
    read against the version that was flashed, and needs a blend for the
    program the gearbox reported.
  - *Physics* (used where there are no logged shifts) works it out from the
    torque, the gear, the vehicle mass and the final drive and tyre size under
    **Vehicle** in the table view.
  - *Manual* takes rpm values, one per upshift.
- **Converter slip.** In a gear whose lock-up maps leave the converter open at
  full load in the selected programs (usually 1st), the engine runs faster than
  the map's speed by the slip you set.

The table lists each upshift: the engine rpm where it should happen, the lead
and where it came from, the map value (as the editor shows it: output rpm ×
the gear's ratio) and why. The chart below draws the power through the gears
with today's points (dashed, for the first selected schedule) against the
optimum.

## How it is written

- **Rows.**
  - *100 % and kickdown to the optimum* (the default) sets both full-load rows.
  - *Kickdown to the optimum, 100 % keeps its offset* moves the kickdown row and
    keeps each program's 100 % row as far below it as it was.
- **Blend.**
  - *Keep the factory shape* (the default) scales each line between the blend
    pedal and full load, so a factory step stays, in proportion.
  - *Ease in* moves the rows by a smooth fraction of the change.
  - *Full-load rows only* leaves the part-load rows alone.
- **Spread steps steeper than** lifts the rows below a rise that is steeper
  than that multiple of the line's own steepest factory rise, down to 50 %
  pedal at most. The check *No step steeper than 1.5 × the factory's* reports
  any that remain.
- **Only if it gains** leaves a line alone where moving it gains less average
  power than that. Upper gears are often within a percent already.
- **Kickdown downshifts move with their upshift** keeps the kickdown
  hysteresis.
- **Leave manual (M) programs alone** is on by default.
- **Full load from where the ECU asks for full torque** treats the gearbox rows
  from that pedal up as full load (see [part-load](part-load-shifts.md) for the
  other way, moving the pedal axis).

The step may raise or lower a line. Use **Direction** to limit it to one way.
As with every step, the preview shows each cell, the checks are advisory, and
**Apply** writes one undoable step.

It is a calculation from what the calibration asks for. Above peak power the
real torque depends on the turbo reaching its target, so confirm the result
with a logged full-throttle pull. Once the project holds such logs, *From logs*
uses them.
