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
| [Gear ratios](tools/gear-ratios.md) | **Tools > Gear ratios...** (Ctrl+G) | Gearbox ratios and gear recognition |
| [VSS pulse settings](tools/vss.md) | **Tools > VSS pulse settings...** (Ctrl+U) | Vehicle-speed scaling, e.g. after a tyre-size change |
| [End of injection](tools/end-of-injection.md) | **Tools > End of injection...** | When the injection ends on an EDC15P / EDC16 unit-injector engine, and moving it |

ZF 6HP transmissions have their own [fault-code editor](modules/zf6hp/fault-codes.md).

The [AI assistant](ai/what-it-knows.md#what-each-module-adds) reads what
these tools show, too: ask it *"which fault codes are switched off?"* or
*"what does measuring block 11 show?"*, and it can propose switching fault
codes for you to review.

The [EEPROM editor](tools/eeprom.md) (**Tools > EEPROM editor...**) edits an
EDC15 or EDC16 EEPROM. Tools that belong to one module are in that module's
sub-menu of **Tools**, such as **Tools > VDO Cluster > Needle sweep...**; see
[the Tools menu](interface.md#tools).
