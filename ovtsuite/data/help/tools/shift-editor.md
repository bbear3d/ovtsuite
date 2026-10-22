---
title: Shift map and lock-up editors
order: 37
context: GearboxEditDialog, BlendDialog
---
# Shift map and lock-up editors

**Tools > ZF 6HP > Shift map editor...** and **Lock-up (TCC) editor...** (and
the same under ZF 8HP) edit many schedules at once: every shift program's
upshift and downshift lines, or every lock-up characteristic's thresholds.
An edit is a list of **steps**, previewed before anything is written, and
written as **one undoable step** (Edit > Undo).

## Choosing what to edit

- **Schedules / Characteristics** (left) -- tick the shift programs (SKL) or
  lock-up characteristics (WK) to change. The tree groups them by coding bank
  and program; a **Preset** ticks a common selection, and a selection can be
  saved with the project. Unused lock-up characteristics, and those whose
  gear could not be located, are listed last in each bank.
- **Line** -- the lines a step changes: upshifts and downshifts (1>2, 2>1 ...)
  or, for lock-up, RO / OR / RC (open, slip, full lock). **Opposite shift**
  (**Paired threshold** for lock-up) moves each line's pair with it.
- **Units** -- engine rpm, output rpm or road speed; the maps store output
  rpm, and the gear's ratio converts.
- The chart shows the ticked schedules against pedal, the factory line
  dashed. **Upshifts** draws the upshift out of each lock-up gear, **Effective
  line** the threshold the blended programs give, **Logged shifts** (in a
  project with logs) where the car actually shifted.

## Steps

| Step | What it does |
|---|---|
| **Offset** | Adds rpm to every cell reached |
| **Scale** | Multiplies every cell (0.92 = 8 % earlier) |
| **Slide along pedal** | Moves the line's shape to lower or higher pedal: a downshift then needs less (or more) pedal |
| **Band (keep order)** | Maps the selection's spread onto a range: the schedules keep their order and spacing |
| **Set to value** | Sets the line to a curve, or part of the way to it |
| **Copy from schedule** | Moves toward another schedule's line, by pedal |
| **Smooth** | Pulls each row toward its neighbours |
| **Follow an upshift** (lock-up) | Sets a threshold to a share of the upshift out of its gear in a chosen program ("lock fully at 85 % of SKL 12's 3>4") |
| **Set gap to the paired line** | Sets the gap between a line and its pair: an upshift and the downshift that undoes it, or lock-up's RO and OR |
| **Never (write "off")** (lock-up) | Writes the "disabled" value, so the threshold is never reached |
| **Full-load optimum** (shift) | Full-throttle upshifts from the engine ECU's torque -- see [Full-load shift points](full-load-shifts.md) |
| **Part-load strategy** and **Pedal axis to the ECU's full torque** (shift) | Economy or sport below full load, and the gearbox pedal lined up with the engine's -- see [Part-load shift strategy](part-load-shifts.md) |

Each step can be limited to a pedal range, tapered to nothing at its edges,
and set to **Raise only** or **Lower only**. **Add step** adds it to **Steps in
this edit**; the steps apply in order, and **Remove** drops one.

## Checks and applying

**Checks (advisory)** flag what usually makes a gearbox misbehave: a line
falling as the pedal rises, engine rpm too low after a shift, lock-up not
below the upshift, schedules that were identical no longer identical, a step
steeper than the factory's, an upshift landing with the converter open.
They warn; they do not block.

**Apply...** writes the edit as one step, with a version label; **Save as a
project version** saves it in the project's history too. **Blends...**
(shift editor) sets how the programs are blended into the line the car
drives, fitted from logs when the project has them.

See also the [ZF 6HP shift and lock-up schedules](../modules/zf6hp/shift-and-tcc.md).
