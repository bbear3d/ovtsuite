---
title: Placing a definition file
order: 62
context: PlacementDialog
---
# Placing a definition file

A definition file -- an A2L, a DAMOS, a WinOLS project or map pack, an XDF --
gives the addresses of the software it was made for. On that software every
table is where the file says. On another software of the same ECU (another
dataset, a newer program version) parts of the calibration have moved: a
table that grew or shrank pushes everything after it along, and a block may
sit somewhere else altogether.

WinOLS applies one offset to every address and shows whatever is there. OVT
places **each table on its own evidence**, and shows only the tables
something confirms. What nothing confirms is left out unless you ask for it.

## What counts as evidence

- **The format's own facts.** An A2L that stores its axis sizes in the binary
  says so in its record layouts, and a DAMOS block starts with its input's
  address and its point count: these must hold where a table is placed, and
  its stored axes must rise or fall throughout.
- **The original image.** A WinOLS project carries the binary its maps were
  made on; for an A2L you can add its HEX file (or the binary it describes).
  A table's header and axis bytes from that image, found once in this file,
  place it even when it moved.
- **The order of the tables.** Records lie one after another: a table
  confirmed in this file puts the next one right after it, and a single value
  between two confirmed tables that moved alike moved with them.

An XDF or a map pack states nothing about its records but their axes, and the
axes alone cannot move a table (shifted by the word ahead of them -- often a
point count -- they rise just as well). Without its original image, such a
file is used at its own offset, as before, unless its axes show it does not
fit this file at all.

## The placement dialog

Attaching a definition places it first. When every table is where the file
says -- the definition is for this very software -- it is simply attached.
Otherwise **Place definition** shows what was found:

| Line | Means |
|---|---|
| **Where the definition puts them** | Confirmed at the file's own address (at the offset shown). |
| **Found moved** | Confirmed somewhere else: the original image's bytes found once nearby, or laid out right after a confirmed table. |
| **Moved with the tables around them** | Its header holds at the offset the tables beside it moved by. |
| **Between confirmed tables** | Nothing of its own to check (a single value, a table with fixed axes), but the confirmed tables on both sides moved alike. |
| **Where the definition puts them (nothing to check)** | An XDF or map pack on the software it fits. |
| **Unverified** | Nothing confirms it anywhere in this file. Left out unless shown. |

Below that are the **offset** (definition address minus file offset: with
0x800000, a table at 0x8D1000 is read at 0xD1000) and how many of the tables
that could be checked hold at it. **Regions** lists which parts of the
definition moved, and by how much.

Options:

- **Find the offset** / **Use offset** -- give the offset yourself when you
  know it; the tables are still checked at it.
- **Look for tables that moved** -- off, every table is checked at the one
  offset only.
- **Also show unverified tables** -- they are added in a group of their own,
  **Unverified - check before writing**, and each one's note says so.
- **Original image** -- an A2L's HEX (or the binary the definition was made
  for). With it far more of a moved definition is placed, and confirmed by
  bytes rather than by structure. A WinOLS project brings its own.
- **Hide the module's built-in maps** -- shown when a module also defines the
  binary. Ticked, the tree holds only this definition's maps; unticked, they
  sit beside the module's in a group of their own, **From <file name>**. The
  module still identifies the binary and corrects its checksums either way;
  its tuning tools that rely on its own map list are unavailable while its
  maps are hidden.

**Recalculate** places the definition again with the options shown; **OK**
keeps them with the project. Every placed table's note (shown beside the
table) says how it was placed: *Moved +24 bytes from the definition's address
(reference bytes found once)*.

To look at it again later, right-click the set in the Project panel and pick
**Definition placement...**; **Hide the module's built-in maps** is on the same
menu, as a tick that switches between the two at once. The choices are kept in the project and used
every time it opens; the placement itself is worked out once and reused while
it still fits the file (editing values does not move tables).

## How far to trust it

Measured on definitions of one software placed on another of the same
project, against that software's own definition:

- **With the original image** (an A2L with its HEX, a WinOLS project): four
  in five tables or more placed, and of those a few in a thousand differ
  from the other software's own definition.
- **By structure alone** (an A2L or DAMOS without its image): fewer placed --
  from a fifth to three quarters, depending on how much moved -- and up to a
  few in a hundred of them may be wrong where the calibration repeats the
  same shape many times over. Add the original image when you have it.
- **Another generation** of the software (an MSA15 V3.0 DAMOS on V3.1, say):
  the record headers hold no more often than by chance, so almost nothing is
  shown -- the definition is not for this file.

Before writing a tune made with a definition from another software, compare
the changed tables with what you expect -- as you would with any definition
from elsewhere.
