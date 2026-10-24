---
title: Measuring blocks
order: 20
context: MsBlockDialog
---
# Measuring blocks

**Tools > Measuring blocks...** (Ctrl+M) shows, and lets you change, which
signals each *measuring block* of the engine ECU reports. A measuring block is
the numbered group of live values a scan tool or the
[data logger](../logging.md) reads in one request — block 1 might be engine
speed, injected quantity, injection duration and coolant temperature.

Changing a block changes what the car's ECU sends for that block number once
the file is written to it. The usual reason is logging: putting the signals
you want side by side into one block means fewer blocks per log, and fewer
blocks per log means more samples per second.

It works on **Bosch EDC16** and **Bosch EDC15** files; on other files the menu
item is greyed out.

![Measuring blocks on an EDC16 file, populated blocks only](../images/tools-msblock.png)

## Change a cell

1. Open the file and choose **Tools > Measuring blocks...**.
2. Find the block: type its number (exactly, e.g. `6`) or part of a signal
   name into the filter box. **Populated only** (on by default) hides empty
   blocks.
3. Click the cell (an editable cell shows a small arrow) and pick another
   signal from the list that opens — typing filters it. The change is held,
   shaded yellow, until you press **Apply**. Or select a cell and type an id, press F2, or
   right-click it and choose **Change this cell...**. An id the file has no
   definition for is refused with a note under the table, rather than
   written. To select cells without opening the list, click the **Block**
   column or hold Ctrl or Shift.
4. Select a cell to see its full name, and on EDC16 its internal signal number
   and display scaling, in the panel under the table.
5. Press **Apply** to write every held change as one undo step (**Discard**
   forgets them; closing the window with changes held asks which).
6. Save (**File > Save**: a new version in the project) and write it to the
   car (see [ECU read / write](../flashing.md)).

Each cell shows *id - name*. **255** is an empty slot. On EDC16 the id **0**
is a real signal (engine speed), not an empty one.

What **Apply** writes is an ordinary edit: **Edit > Undo** (Ctrl+Z) takes
it back in one step. Choosing the value a cell already holds is no change.

## EDC16: up to eight cells per block

The ECU builds block *N* from two rows of the calibration's block table: row
*N* gives cells 1–4 and row *N* + 127 gives cells 5–8. So the window lists
blocks **1–127**, each with **Cell 1** to **Cell 8** (hover over the Cell 5–8
headings for a reminder). Rows 128 and up are never blocks of their own — a
scan tool asking for block 133 gets "N/A"; its values arrive as cells 5–8 of
block 6. A block whose own row is empty but whose upper row is filled still
answers, with cells 1–4 empty.

| | EDC16 | EDC15 |
|---|---|---|
| Blocks | 1–127, up to eight cells each | numbered blocks per dataset, four cells: the **Block** column shows the dataset and the number, e.g. **Manual 01** |
| A cell holds | an ESB entry: an internal signal plus the scaling the scan tool shows it with | the number of a value descriptor in the same dataset |
| Signal names | from the app's signal list for the software family, where it has one; otherwise the cell shows its id and internal signal number | from the file's own descriptors and message table |
| **Related tables** | the calibration's other signal-block tables (adjustment, test and basic-setting displays); **Show in editor** selects one in the main window and leaves this window open | not used |

Some EDC16 software builds share their signal numbering with a closely
related build: the 038906016Q, for example, uses the same signal-number table
as the P350 family. When the file's own table is byte-for-byte the same as
that family's, its signal names are used, and the note above the table says
which family they came from. When the table differs, no names are borrowed.

## EDC15: datasets and descriptors

An EDC15 file holds a set of blocks per dataset (Manual, Automatic...). Pick
one in the dataset box, or leave **All datasets**. Tick **Apply edits to every
dataset** to make a change in all of them at once (one undo step) — otherwise
change the dataset your car runs (see [EDC15 datasets](../modules/edc15/datasets.md)).

The **Descriptors** tab lists what each cell id points at: the **Message**
number picks the value the ECU reads, **Display type** and **Norm** tell the
scan tool how to scale it. Rescaling a descriptor is how, for example, the
boost display is matched to a larger MAP sensor. Hex (`0x0F40`) is accepted.
A descriptor change is held until **Apply**, like a cell's.

## Buttons

| Button | What it does |
|---|---|
| **Clear selected block(s)** | Empties every cell of the selected blocks (255) -- held until **Apply**. Also on the right-click menu. |
| **Copy block(s) from comparison** | Only with a comparison file of the same ECU type open: sets the selected blocks' cells to what the comparison shows in the same block -- held until **Apply**. Signals are matched by meaning (the same internal signal, or on EDC15 the same message number), so this works between different software versions; a signal the primary file has no definition for is left as it is, and the panel says how many. |
| **Show in editor** | (EDC16) Selects the table chosen in **Related tables** in the main window. |
| **Apply** | Writes every held change (shaded yellow) as one undo step. |
| **Discard** | Forgets the held changes. |
| **Close** | Closes the window (asking first when changes are held); what was applied stays in the tab until you save or undo it. |

With a comparison open, its blocks are a read-only table of their own under
the primary's -- the same blocks on the same rows, so nothing is off to the
side to scroll to. The two scroll together, keep the same column widths and
the same selected cell; drag the bar between them to share the room. A cell
that shows a different signal from the primary is highlighted in both, and
the primary's tooltip names the comparison's signal. Cells are compared by
what they show, not by their names: the same id with the same definition in
both files is the same signal even when it has no name (a bare number, or a
"CAN bus signal" entry), so comparing a file with itself or with its own
original highlights nothing. Between different software versions a named
signal is matched by its name, so the same signal under another id is not
highlighted either. Untick **Show
comparison** to hide the lower table: the shading and tooltips stay.

## Tips

- Change blocks the factory left empty, or ones you never read, rather than
  the low-numbered blocks a workshop or another tool relies on. Cells 5–8 of
  a block are a good place for extra values: most scan tools only show four.
- The logging presets (and **What to log...** in
  [Tune from logs](../modules/edc16/logtune.md)) work out which blocks to read
  from the calibration that is open in the app. After rearranging blocks,
  log with the version you actually wrote to the car open in a tab (the
  Project panel marks it *⚡ flashed*).
