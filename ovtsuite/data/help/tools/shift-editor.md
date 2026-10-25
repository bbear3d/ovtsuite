---
title: Shift map and lock-up editors
order: 37
context: GearboxEditDialog, BlendDialog
---
# Shift map and lock-up editors

**Tools > Shift map editor...** and **Tools > Lock-up (TCC) editor...**, shown
while a ZF 6HP or ZF 8HP tab is in front, edit many schedules at once: every shift program's
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

## Comparing with another file

When the tab has a comparison file (**File > Import as comparison...**, or
**Compare with open tab** / **saved version** -- see
[Comparing files](../editor/comparison.md)), the editor
builds the comparison's schedules the same way and pairs them by name (SKL
11.0 with SKL 11.0). A line above the chart says how many paired; a schedule
missing from the comparison, or of another shape, is named there and left
out. If the comparison has no schedules of this kind (another gearbox, other
lines), it says so and the comparison features stay off.

**Show comparison** (on by default) switches all of this on and off together:

- **On the chart** each schedule's comparison line is drawn faded and dashed
  in the schedule's own colour, behind this file's line, in whichever unit is
  chosen. Hovering a line gives both values at that pedal and the difference.
- **Under the table** a read-only copy of the value table shows the
  comparison: same rows, columns and units, headed *Comparison: file (read
  only)*. It scrolls, sizes its columns and moves its current cell with the
  table above (either way). Cells that differ are shaded in both tables with
  the "differs from comparison" colour, and each cell's tooltip gives the
  other file's value. The table shows the edit's preview, so a cell a step
  has already brought to the comparison's value is no longer shaded (a cell
  the edit changes keeps the edit's own highlight).
- Where a paired schedule's pedal axis differs from the comparison's, its
  line is still drawn (on its own axis) but its cells are not compared,
  shaded or copied.

**Copying from the comparison.** Right-click selected cells in either table
for **Copy selected cells from comparison**, or **Copy whole line from
comparison** to take every pedal row of the shown line for the selected
schedules; right-click a line on the chart to copy that schedule's whole
line. Each adds a step to **Steps in this edit** -- previewed, removable with
**Remove**, and written with the other steps by **Apply...** as one undoable
step. Values are copied exactly, including a stored "off". Copying works
with **Show comparison** off too.

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
They warn; they do not block. The box beside a check can instead have it fix
the edit (**Fix: ...**); hover an entry for what that fix does.

**Apply...** writes the edit as one step, with a version label; **Save as a
project version** saves it in the project's history too. **Blends...**
(shift editor) sets how the programs are blended into the line the car
drives, fitted from logs when the project has them.

See also the [ZF 6HP shift and lock-up schedules](../modules/zf6hp/shift-and-tcc.md).
