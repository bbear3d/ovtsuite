---
title: Parameter tools
order: 28
---
# Parameter tools

The **Tools** menu has editors for things that are not ordinary maps: fault
codes, the signals a scan tool sees, gear ratios and speed scaling. Each works
on the file open in the current tab, needs that file's module (for its
definitions), and records every change as an undoable edit that is saved with
the project.

| Tool | Opens with | For |
|---|---|---|
| [DTC / Fault-Path editor](tools/dtc.md) | **Tools > DTC / Fault-Path editor...** (Ctrl+D) | Switching fault codes on or off, and restoring them from stock |
| [Measuring blocks](tools/measuring-blocks.md) | **Tools > Measuring blocks...** (Ctrl+M) | Which signals each measuring-block group shows to a scan tool or the logger |
| [End of injection](tools/end-of-injection.md) | **Tools > End of injection...** | When the injection ends on an EDC15P / EDC16 unit-injector engine, and moving it |
| [Dyno chart](tools/dyno.md) | **Tools > Dyno chart...**, or **Dyno** on the edit bar (Ctrl+4) | The power and torque an EDC15 / EDC16 file asks for at full load, against the original or another reference |
| [Feature switches](tools/feature-switches.md) | **Tools > Feature switches...** | Turning EDC15 / EDC16 functions on and off as one bundle: cruise, A/C, EGR, DPF, smoke limiter by MAF or MAP and more |
| [Full-load shift points](tools/full-load-shifts.md) | **Tools > Shift map editor...**, step *Full-load optimum* | Full-throttle upshifts set from the torque of the project's engine ECU, blended into the rest of the maps |
| [Part-load shift strategy and log replay](tools/part-load-shifts.md) | **Tools > Shift map editor...**, steps *Part-load strategy* and *Pedal axis to the ECU's full torque*, box *Replay* | Economy for D and sport for S, the gearbox pedal lined up with the engine's, and what an edit does to fuel and response on your logged drives |
| [Gear ratios](tools/gear-ratios.md) | **Tools > Gear ratios...** (Ctrl+G) | Gearbox ratios and gear recognition |
| [VSS pulse settings](tools/vss.md) | **Tools > VSS pulse settings...** (Ctrl+U) | Vehicle-speed scaling, e.g. after a tyre-size change |

To convert definitions, calibration values and binaries between formats
(XDF, A2L, DAMOS, WinOLS, DCM, CDFX, HEX ...) with no file open, or to write
a DCM or CDFX into a binary, use the [Converter](converter.md).

ZF 6HP and ZF 8HP transmissions have their own fault-code window: see [ZF 6HP fault codes](modules/zf6hp/fault-codes.md) and [ZF 8HP fault codes](modules/zf8hp/fault-codes.md).

The [AI assistant](ai/what-it-knows.md#what-each-module-adds) reads what
these tools show, too: ask it *"which fault codes are switched off?"* or
*"what does measuring block 11 show?"*, and it can propose switching fault
codes for you to review.

The [EEPROM editor](tools/eeprom.md) (**Tools > EEPROM editor...**) edits an
EDC15 or EDC16 EEPROM. Tools that belong to one module are in **Tools**
with the others, shown only while a tab they work on is in front, such as
**Tools > Needle sweep...** for a VDO cluster; see
[the Tools menu](interface.md#tools).
