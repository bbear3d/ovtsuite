---
title: Live data
order: 20
---
# Live data

The **Live data** tab shows the measuring groups of the connected module --
the numbered groups of KW1281 and KWP2000 modules, four cells each with the
module's own scaling and unit.

On the left is the list of groups the module's definitions or the factory
catalogue name, each with the names of its cells. Tick the groups you want;
the filter field narrows the list by group or cell name. A group the module
does not serve is greyed out. To read a group that is not in the list, type
its number under **Group number** and press **Add**.

On the right, every cell of every ticked group is a tile with its name, value
and unit and a bar showing where the value sits in its usual range; a tile is
outlined when a value is out of range. The line over the tiles shows how many
groups are read, how many readings a second the link delivers (the ticked
groups are read in turn, so more groups means a lower rate), and how many
values there are. Reads run while this tab is showing and stop when you leave
it or the session ends.

**Graph** swaps the tiles for a strip chart of the last minute. Each line
is scaled to its own range, because the units differ; click a name in the
legend to hide or show a line.

**Log to file** writes every reading to a CSV file until you press **Stop
logging**: a time column and one column per cell. Open it in a spreadsheet to
chart a run.

## Adding a value from search

Press Ctrl+K and type part of a value's name (`boost`, `rail pressure`) or its
signal name: the box lists the named cells of every module. **Enter** goes to
that module's **Live data** tab with the cell's group ticked and its tile
outlined for a moment. **Shift+Enter** only ticks the group in the module's
live view and keeps you where you are, so several values can be added from one
search; a module that has no session yet remembers the group for when it does.
The right-hand text of a result is its current reading while the module is
being read. See the [command palette](../diag.md#the-command-palette).

## Where the names come from

Cell names come from the [definition packs](definitions.md) when a pack names
them, otherwise from the factory catalogue of the module's part number (the
same catalogue the logger uses for the EDC15 and EDC16 engine ECUs). A cell
with no name shows its group and position (`Group 011 cell 3`).

UDS modules have no numbered groups; the tab lists the data identifiers the
module's definitions flag as live, as one group, and reads those.

## Other connected modules

Over CAN, other modules with an open session appear under the tiles as
**also live, same session**; click one to go to it. **Open in split view**
opens **Vehicle tools > Vehicle live data**, which reads groups from several
connected modules in one table.

## When you need more

For presets, many hours of logging, and the higher K-line rates, use
[OVT Logger](../logging.md): it is built for the engine and gearbox ECUs the
suite has definitions for.
