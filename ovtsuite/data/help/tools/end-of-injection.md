---
title: End of injection
order: 35
context: EoiDialog, TargetDialog, CollapseDialog
---
# End of injection

**Tools > End of injection...** shows when the injection ends on a
unit-injector (PD) engine with a Bosch EDC15P or EDC16 ECU, and lets you set
it. **End of injection...** is also in the right-click menu of the
start-of-injection and injection-duration tables. The window belongs to the
tab: it stays open beside the grid and follows every edit and undo.

It needs the Bosch EDC15 or EDC16 module. Distributor-pump (VP37) software is
not supported: that pump meters fuel with a spill collar, and the ECU never
calculates an injection duration.

## How the ECU gets there

The ECU stores no end of injection. It works one out from two sets of tables:

- the **start of injection (SOI)**: one of several tables, or a blend of two,
  picked by coolant temperature;
- the **duration** (crank degrees): one of several tables, or a blend of two,
  picked by the SOI itself, because the cam pushes the injector plunger at a
  different speed depending on where the delivery starts.

A selector curve gives a table number with a fraction: 3.84 means 16 % of
table 3 and 84 % of table 4. The end of injection is then

**EOI = duration − SOI**, in degrees **after** TDC. A negative EOI is before
TDC. Tune from logs uses the same convention.

The window does exactly this with the tables in your file.

## The window

- **Coolant**: which SOI tables the ECU uses (warm by default).
- **Dataset** (EDC15): the dataset to show.
- **Show**: **EOI**, **SOI** (degrees before TDC), **Duration**, or
  **Table no.** (which duration tables are read, with the fraction).
- **View**: *What the ECU uses* is the grid over the SOI table's own
  breakpoints. A **Duration table** entry shows that table on its own axes:
  white cells are used alone at this coolant temperature, shaded ones are
  blended with a neighbouring table, grey ones are not used at all.

EOI cells are coloured from before TDC (green) to late (red). An *italic*
value is one where the SOI limiter (EDC16 `InjCrv_phiMIMax_MAP`) caps the SOI.

Click a cell to see how it was worked out: which SOI tables were blended, the
selector value, the two duration tables and their weights, and the result.
**Go to SOI table** and **Go to duration table** open the table with the
cells marked.

## Changing it

Type a value into a cell. What changes depends on what is shown:

| Show | Typing a value | Fuelling |
|---|---|---|
| **EOI** | Moves the SOI until the EOI is what you typed, keeping the ECU's own choice of duration table | Unchanged |
| **SOI** | Moves the SOI tables the coolant picks | Unchanged |
| **Duration** | Changes every duration-table cell the ECU reads there, so its duration moves by exactly that much | **Changed**: on a PD engine the duration is the fuel quantity. The window asks first. |

In a **Duration table** view, a value is written into that table only.

**Set target EOI...** does the first row for many cells at once: one target,
optionally only where the injection ends later than it, for the selected cells
or the whole grid, and for the SOI tables you tick. Each change is one undo
step.

**Advanced > Collapse to one duration table...** writes the duration the
ECU uses today, at a reference coolant temperature, into one table and points
the selector at it. It is optional, and it is not needed to edit the
duration. It gives up what the other tables do (the quantity at other start
angles), and the dialog shows how far the duration would move at other
coolant temperatures before you confirm.

## What is not included

The numbers are for steady running at sea level, with fuel at its reference
temperature. The ECU also corrects the SOI for intake-air temperature,
altitude and transients, and the quantity for fuel temperature. To see the
real values while driving, log them: the **Injection timing** preset in
[Data logging](../logging.md) records start and duration, and adds a calculated
end-of-injection column.

If the definition for your file does not place every table the calculation
needs, the window lists the missing ones instead of guessing.
