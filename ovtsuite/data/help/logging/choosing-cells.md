---
title: Choosing what to log
order: 20
---
# Choosing what to log

A control unit reports its live values in **measuring blocks** (groups): each
group is a numbered set of usually four **cells**. You log a value by ticking
its cell. Every row of the log reads all the groups your ticked cells come
from, so the fewer groups you use, the more rows per second you get.

## Browse and tick cells

1. Pick a group in **Group:**. Each entry shows the group number and what its
   cells carry, for example "11 - Engine Speed, Boost Pressure Setpoint, ...".
   The number box beside it takes any group number from 1 to 255, including
   ones the file does not list.
2. The table below lists the group's cells (the first group's as soon as the
   logger opens). Tick **Log** on a cell to add it; untick it to remove it.
3. When connected, the **Value** column shows the live reading of the group
   you are browsing, so you can check a cell is alive before you log it.

The ticked cells are listed under **Selected cells**, each as **G**group**/**cell
(for example G11/3) with its signal name. **Remove** takes out the rows you
have selected in that list; **Clear** empties it.

## Where cell names come from

The line under the cell table says which source is naming the cells.

| ECU | Names |
|---|---|
| **EDC16** | From the car's own file: the calibration's group map points each cell at a signal, shown as its description and its name, for example "Boost Pressure (filtered) (BPSCD_pFltVal)". |
| **EDC15** | From the car's own file: the measuring-block descriptors, for example "engine speed (G1/1)". |
| **ZF6HP** | From a built-in list of the 6HP19's blocks 1–17 ("Transmission cells are named from the 6HP block catalogue"). No file is needed. A few cells (group 15, cells 1 and 3) are not identified and log as "unlabelled". |
| **ZF6HP-UDS** | The 6HP28's one live-data block, shown as group 161 with 41 cells, each named by the controller's own signal number ("6HP signal 0x1F [tD]"). The values are raw counts: no scaling to physical units is applied yet. |
| **Aisin TRx** | Not named yet: cells are "Group N / Cell M". |

For an engine ECU the file has to be **the car's own** — the group map
differs between software versions, and a different file names cells
plausibly but wrongly.

In the main window the logger uses the open tab whose file matches the
connection's controller (an EDC16 tab for **EDC16**, a ZF 6HP tab for
**ZF6HP**), not simply the tab in front. If no tab holds such a file, it
falls back to the active tab; with no usable file the hint says "No
measuring-block map for this controller is open, so cells are unnamed" and
cells log as "Group N / Cell M".

Names are fixed when logging starts: switching tabs during a log does not
rename its columns.

## Presets

**Preset:** ticks, in one go, the fewest groups of this calibration that
carry everything a particular analysis needs, and shows under the box which
blocks it chose and how to drive for it. **(choose cells by hand)** leaves
the selection alone.

- Applying a preset replaces the selected cells.
- It also adds the preset's short name to the log file name (for example
  "...-boost.csv"); picking another preset swaps the tag.
- If something the analysis needs is in no group the ECU serves, the hint
  starts with "Not in any block this ECU serves:" and the names of the values
  that are missing (for example "Boost (specified)").
- Engine presets need the car's file open; without it the hint says "No
  cells: no file is open for ...".

Which presets there are, and how to drive each one, is on
[Presets and how to drive them](presets.md).

Next: [Recording and the live chart](recording.md).
