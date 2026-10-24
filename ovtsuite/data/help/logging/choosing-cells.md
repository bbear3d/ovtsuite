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
   cells carry, for example "11 - Engine speed, Boost pressure setpoint, ...".
   The number box beside it takes any group number from 1 to 255, including
   ones the file does not list.
2. The table below lists the group's cells (the first group's as soon as the
   logger opens). Tick **Log** on a cell to add it; untick it to remove it.
   Only the cells that carry a signal are listed: an EDC16 group has room
   for eight, but a cell the file leaves empty is not shown (and a preset
   does not log it). Once connected, the list shows the cells the ECU
   actually sends for that group.
3. When connected, the **Value** column shows the live reading of the group
   you are browsing, so you can check a cell is alive before you log it.

The ticked cells are listed under **Selected cells**, each as **G**group**/**cell
(for example G11/3) with its signal name. **Remove** takes out the rows you
have selected in that list; **Clear** empties it.

## Where cell names come from

The line under the cell table says which source is naming the cells.

| ECU | Names |
|---|---|
| **EDC16** | From the car's own file: the calibration's group map points each cell at a signal, shown as its description and its name, for example "Boost pressure (filtered) (BPSCD_pFltVal)". With no file open, from the factory catalogue (see below). |
| **EDC15** | From the car's own file: the measuring-block descriptors of the dataset the ECU says it is coded to (Manual, Automatic...), for example "Engine speed (G1/1)". With no file open, from the factory catalogue (see below). |
| **ZF6HP** | From a built-in list of the 6HP19's blocks 1–17 ("Transmission cells are named from the 6HP block catalogue"). No file is needed. A few cells (group 15, cells 1 and 3) are not identified and log as "unlabelled". |
| **ZF6HP-UDS** | The 6HP28's UDS data identifiers (DIDs), one per group, listed by DID in hex ("F40C - Engine speed", "1024 - RAM u16, rolling history, slot 1 of 7"). The known signals come first: engine speed, transmission input and output speed, road speed, ATF temperature, coolant, both supply voltages, ignition status and the brake-signal flags. Those marked "(raw)" are identified but not yet scaled. The F4xx DIDs (engine speed, road speed, coolant, load, pedal, voltage) are scaled to physical units. The rest are raw counts at the width the transmission's own table gives them, labelled like "6HP28 DID 1024 (u16)"; "computed" DIDs are one cell holding the whole answer in hex. Log a few F4xx DIDs alongside the raw ones: they are what names the raw columns later. |
| **Aisin TRx** | Not named yet: cells are "Group N / Cell M". |

For an engine ECU the file has to be **the car's own** — the group map
differs between software versions, and a different file names cells
plausibly but wrongly.

In the main window the logger uses the open tab whose file matches the
connection's controller (an EDC16 tab for **EDC16**, a ZF 6HP tab for
**ZF6HP**), not simply the tab in front. If no tab holds such a file, it
falls back to the active tab.

### With no file open: the factory catalogue

When no open file can name an EDC16 or EDC15's cells, the logger names them
from a catalogue of factory files that ships with the **Bosch EDC16** and
**Bosch EDC15** modules, looked up by the identity the ECU reports when you
**Connect**:

- **EDC16**: the part number and software version (for example
  "03G906016LQ" software 0971).
- **EDC15**: the identification text, which also says which dataset
  (manual "SG" or automatic "AG") the ECU is coded to.

It takes the first of these that the catalogue has:

1. **The same part number and software.** The hint reads "Cells named from
   the factory calibration of 03G906016LQ 0971".
2. **The same part number, other software.** Where the part's software
   versions differ, the layout most of them share is used (on EDC15, of the
   same gearbox). The hint names the software it used.
3. **The generic layout** of the platform: each cell named only where most
   part numbers agree on it, so some cells stay "Group N / Cell M". This is
   also what names the cells before you connect, so presets can be chosen
   straight away; connecting then replaces it with the ECU's own entry.

The catalogue describes factory calibrations. If the car's measuring blocks
were edited, or the names come from step 2 or 3, open the car's file: an
open file always takes precedence. Without the module installed, cells log
as "Group N / Cell M". The identity is kept after **Disconnect**, so the
cells stay named.

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
- Engine presets come from the car's file when it is open, otherwise from the
  factory catalogue (see above).

Which presets there are, and how to drive each one, is on
[Presets and how to drive them](presets.md).

Next: [Recording and the live chart](recording.md).
