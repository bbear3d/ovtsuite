---
title: DTC / Fault-Path editor
order: 10
context: DtcDialog
---
# DTC / Fault-Path editor

**Tools > DTC / Fault-Path editor...** (Ctrl+D) lists every trouble code the
open engine calibration can set, and lets you switch each one off or back on.
Use it when a fault is set by something the car genuinely does not have — a
part that was never fitted, or a system that changed in an engine or gearbox
swap — or to put a calibration's codes back the way the factory had them.

It works on **Bosch EDC16** and **Bosch EDC15** files (it needs the matching
module and a file the app has a definition for). On a **ZF 6HP** gearbox file
the same menu item opens the gearbox's own fault-code editor instead, which
works differently — see [ZF 6HP](../modules/zf6hp/index.md).

![The DTC / Fault-Path editor on an EDC16 file, filtered to EGR](../images/tools-dtc.png)

> Switching a code off stops the ECU storing it and reacting to it. It does
> not fix whatever made the code appear. Only switch off a code when you know
> why it is set.

## Switch a code off or on

1. Open the file and choose **Tools > DTC / Fault-Path editor...**.
2. Type part of a name, a fault number or an OBD-II code (e.g. `P0380`) into
   **Filter fault paths...** to find the code. Tick **Enabled only** to hide
   the codes that are already off.
3. Untick the box in the **On** column to switch the code off; tick it to
   switch it back on. The row turns grey when the code is off, and the line at
   the top counts the codes that are on and off.
4. Save (**File > Save**, Ctrl+S): the edits become a new version of the
   tab's branch in the project, and the checksums are corrected as it is
   saved.

Every change is an ordinary edit: **Edit > Undo** (Ctrl+Z) takes it back. The
window can stay open while you work in the main window, and it follows edits
made there (undo, a patch, the table grid).

## What "off" means

The two engine platforms store a fault differently, so the editor changes a
different value on each.

| | EDC16 | EDC15 |
|---|---|---|
| A row is | one fault path (`Dfp_...`) | one sub-fault of a fault path, once per dataset — the dataset is shown in brackets, e.g. **GZS_I (Manual)** |
| **Class** shows | the path's error class: the number that decides whether the fault is stored and which warning lamp it lights | the time until the fault is finally set, in the ECU's raw units (10 ms steps) |
| Switching off | sets the class to 0 (no reaction) | sets that time to "never" (the value the factory calibration itself uses for faults that are not fitted) |
| Switching back on | restores the class it had before; if none is known, a class that lights the MIL and stores the fault | restores the time it had before; if none is known, 1.0 s |

The value a code had before you switched it off is remembered while the file
is open. For a file that was already modified before you opened it, use
**Restore all from stock...** (below) to get the factory values back.

On an EDC15 file, each dataset has its own copy of every fault path. Change
the dataset your car uses — or all of them if you are not sure. See
[EDC15 datasets](../modules/edc15/datasets.md).

## The columns

| Column | What it shows |
|---|---|
| **On** | Ticked when the code is active (it is stored and/or lights a lamp). |
| **Fault path** | The calibration's own name for the fault. |
| **VAG code** | The five-digit VW fault number a scan tool shows. **\*** marks a number taken from the app's reference table (see below); **-** means none is known. |
| **OBD-II** | The standard P-code, where the fault has one. |
| **Class** | See the table above. |
| **Cmp on**, **Cmp class** | Only with a comparison file open: the same fault in the comparison. Highlighted where it differs from the primary. |
| **MIL** | **MIL** when the code lights the engine warning lamp. |
| **Description** | The standard OBD-II text for the P-code where there is one, otherwise a plain description of the fault path. |

Hover over a row to see its fault path, P-code and description together.

On some EDC16 software the block that stores the VAG fault numbers cannot be
read reliably. The editor then says so under the list, and recovers the
numbers from its reference table by OBD-II code or fault name; those numbers
are marked **\***.

## Buttons

| Button | What it does |
|---|---|
| **Read codes from ECU...** | Opens the **Fault codes from ECU** window, which reads the codes stored in the car's ECU (see [Reading fault codes](../logging/fault-codes.md)). The matching rows are then highlighted, and codes the ECU has stored that are not in this file are listed under the table. |
| **Restore all from stock...** | Copies every fault path's class (EDC16) or time (EDC15) from a stock binary of the same software, as one undoable step — a quick way to undo all fault-code changes in a tune. In a project you are offered the set's original (as read): **Use the original**, or **Another file...** to pick a stock file. |
| **Copy differences from comparison** | Only with a comparison open. Sets every fault path whose state or class differs to the comparison's value, as one undoable step. |
| **Close** | Closes the window. Your changes stay in the tab until you save or undo them. |

With a comparison open you can also select rows, right-click and choose
**Copy comparison state to primary** to copy just those.

## Tips

- An EDC16 class number is not a simple on/off: different classes light
  different lamps and store the fault differently. Prefer switching a code off
  and on with the tick box, or copying from a stock file, over typing class
  numbers into the fault-code tables in the main window.
- The fault-path settings are also in the main window's tree under
  **Fault codes (DTCs)**, grouped by system, if you need to see the other
  values a fault path carries.

![The same editor on an EDC15 file: one row per dataset](../images/tools-dtc-edc15.png)
