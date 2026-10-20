---
title: Exporting
order: 65
---
# Exporting

**File > Export** writes part of the project in front, or a description of
it, to a file of its own. Everything is made from the project's **last saved
version** (you are offered to save first), whose checksums were corrected
when it was saved. Every item is always listed; the ones that do not apply
to the tab in front are greyed out.

| Item | Writes | Available for |
|---|---|---|
| **Binary (.bin)...** | One part of the set — the flash, the internal flash (MPC) or the EEPROM — as a plain `.bin`. | Every set. |
| **Original file (.sgo / .dim / .frf)...** | The file the set was imported from, with the saved version in it -- byte for byte the original when nothing changed. An edited `.frf` cannot be rebuilt yet: the export says so, and **Binary (.bin)...** is the way out. | A set imported from a `.sgo`, `.dim` or `.frf`. |
| **Definition as XDF...** | A TunerPro XDF describing every parameter of this binary. | Any binary whose module provides definitions. |
| **Definition as A2L...** | An ASAP2 description (`.a2l`) of every parameter of this binary, for WinOLS and other tools that import A2L. | Any binary whose module provides definitions. |
| **Map pack for WinOLS (.kp, experimental)...** | A WinOLS map pack of the same parameters. Experimental: see below. | Any binary whose module provides definitions. |
| **Data log...** | One of the project's logs, as a CSV file. | A project that keeps logs. See [Data logging](../logging.md#logs-in-the-project). |
| **Patch...** | One of the project's patches, as a `.patch` file. | A project that keeps patches. See [Patches](patches.md). |
| **Attachment...** | One of the project's attachments, as the file it was. | A project with attachments. |

**Binary (.bin)...** asks which part when the set has more than one.
**Data log...**, **Patch...** and **Attachment...** each ask which of the
project's to export when it keeps more than one.

## Definition as XDF

**Definition as XDF...** writes a definition file for TunerPro that matches
**this** binary — its addresses, sizes, scaling and units — so the same
parameters can be viewed in TunerPro. The tables are arranged in folders by
tree group, then by function, as in the parameter tree. Characters TunerPro
cannot show are replaced with plain ones (`°` becomes `deg`, `→` becomes
`->`).

The XDF fits the binary it was exported from. Another software version keeps
its tables at other addresses and needs its own XDF.

## Definition as A2L

**Definition as A2L...** writes the same parameters as an ASAP2 description:
each value, curve and map with its address, size, data type, byte order,
scaling and units, its axes (an axis shared by several tables is described
once) and the tree's folders as functions. Addresses are offsets in the
binary file, as WinOLS and this app expect when importing an A2L for a file;
a tool that works with the control unit's own memory addresses (INCA,
CANape) would need them moved to the flash's base address.

A few things cannot be said in an A2L, and the export lists them when it is
done: a ZF 6HP shift or lock-up map converts each column by its own gear
ratio, which an A2L cannot express, so it is described in the stored units
(output speed).

To bring these parameters into WinOLS, import the A2L into the WinOLS
project for this binary.

## Map pack for WinOLS (experimental)

**Map pack for WinOLS (.kp, experimental)...** writes a WinOLS map pack.
The format is only partly known: the app reads every pack it writes, but
WinOLS may not open it -- use **Definition as A2L...** when it does not. A
map that the binary stores column by column (as EDC16 does) appears turned
in the pack, with its axes swapped, because a map pack stores tables row by
row. Only parameters with a linear scaling fit a map pack; the others, and
text such as part numbers, are left out and listed when the export is done.

The same exports are available from the command line:
`ovtsuite export <binary> <file>.a2l` (or `.kp`, `.xdf`, or `--format`).

## Saving

The app saves only OVT projects (`.ovt`); every other format is an export.
The files of earlier versions of the app (`.6hp`, `.edc16`, `.edc15`,
`.edc15bak` and the like) can be imported but are never written. See
[Projects](../projects.md) and [Files and formats](files-and-formats.md).
