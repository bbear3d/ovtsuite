---
title: Part-load shift strategy and log replay
order: 39
context: PartLoadPanel, AxisPanel, ReplayBox, EfficiencyDialog
---
# Part-load shift strategy and log replay

The shift map editor (**Tools > Shift map editor...**, with a ZF 6HP or ZF
8HP tab in front) has two steps for the rows below full load. It also has a
**Replay** box that shows what any edit does to fuel and response on the
drives you have logged. The full-load points come from the
[full-load step](full-load-shifts.md), so both steps use the same engine.

## Part-load strategy (economy / sport)

An upshift is set by the engine speed it leaves the next gear at. That speed
rises with the pedal:

- at light pedal it sits at a **floor** (the lowest the next gear may pull
  from);
- from the **From** pedal it rises, along a **Curve**;
- at full load it reaches the full-load point.

The downshift that undoes it sits at **Down at** of that speed (the
hysteresis).

| Strategy | Floor | From | Curve | What it does |
|---|---|---|---|---|
| Economy | 1250 rpm | 25 % | 1.6 | upshifts early at light pedal, holds the gear only when you ask for a lot |
| Balanced | 1500 rpm | 15 % | 1.3 | in between |
| Sport | 1850 rpm | 5 % | 1.0 | keeps the engine higher and rises from the start |

- **By mode:** on a ZF 6HP, D programs get Economy and S programs get Sport by
  default. Other programs are left alone, and so is M. Each row's numbers can
  be changed; a changed preset reads *Custom*. A gearbox without D/S groups
  has one row for the ticked schedules.
- **How far** moves each cell part of the way, keeping some of the line's own
  shape.
- **Rows where the ECU asks for full torque get the full-load points.** Your
  engine's driver's wish may ask for full torque before 100 % pedal. When it
  does, at every gear and every speed from 1500 to 3500 rpm, the gearbox rows
  from that pedal up are full load in engine terms.
- **Downshifts too** writes the paired downshifts.
- The check **Upshifts land with the converter locked** warns where an
  upshift would land below the next gear's lock-up point (the lock-up map's
  OR, for that program). There the converter is still open after the shift,
  which costs what an early, economy shift was for.
- The 100 % and kickdown rows are left to the full-load step.

## Pedal axis to the ECU's full torque

This is the other way to line the gearbox up with the engine. It moves the
gearbox's pedal *breakpoints* instead of the values. The top part-load row
(97.3 % on a 6HP19) moves to where the ECU asks for full torque, read from
the ECU, and the rows above **Keep rows below** follow in proportion. The
0 %, 100 % and kickdown breakpoints stay where they are.

**Move the lock-up (TCC) maps' pedal axes too** keeps lock-up meeting the
shifts at the same pedal. The manual-mode (TIPAN) tables are not moved. The
chart shows the lines on their new pedal axis, and Apply writes the axes with
any other step as one undo.

The full-load step has the same choice as a tick box: *Full load from where
the ECU asks for full torque*.

## Replay on the project's logs

**Replay** drives the project's logged trips again, once with the lines as
they are and once as the edit would leave them.

- **Which logs:** gearbox logs paired with the engine logs taken at the same
  time (they pair by start time, within 10 s).
- **What stays and what changes:** each logged moment keeps its road speed,
  its pedal and the power the engine gave. The lines pick the gear.
- **Fuel:** what the ECU's own torque-to-quantity map needs at that gear's
  engine speed. On a BHW it matches the logged injection quantity to within
  half a percent.
- **Which lines:** each logged program reads its own lines. D reads its
  blend; a special program such as cruise reads its own schedule; anything
  else reads the ticked schedules.

| Column | Meaning |
|---|---|
| l/100 km | fuel over the logged distance (the change in brackets) |
| Short of power | time the chosen gear could not give the power that was used |
| Shifts/h | how busy the box is |
| Mean rpm | the average engine speed |
| In logged gear | how often the replay picked the gear the log shows (high before the edit means the replay follows the car) |

The first **Replay** reads the logs in the background. A progress bar shows
which log it is on, and **Cancel** stops it; the editor stays usable
meanwhile. Later replays reuse what was read and take a second or two.

**Follow the preview** updates the *After* row as the step changes. The
replay does not know how you would drive with different gearing, so compare
*Before* with *After*, not with the fuel figure in the logs.

## Efficiency map

**Efficiency map...** in the Replay box draws the engine's fuel per kWh
(g/kWh) over engine speed and brake torque, worked out from the ECU's own
torque-to-quantity map, up to the full-load curve. Each colour band is a
range of g/kWh; the deepest colour is the most efficient.

After a **Replay**, it also marks where the logged driving ran. Hollow
circles are today's lines and filled circles the edit's, sized by the time
spent there. An economy strategy pulls the circles left and up, toward the
efficient middle; a sport one pushes them right. The line under the map
gives the fuel per kWh over all the logged work, before and after.
