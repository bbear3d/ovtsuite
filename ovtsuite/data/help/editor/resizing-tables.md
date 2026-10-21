---
title: Resizing a table
order: 17
context: ResizeDialog
---
# Resizing a table

Some Bosch EDC15 and EDC16 tables use fewer breakpoints than the space the
definition reserves for them. Each table carries its own point counts, and the
ECU reads them on every call. A table like that can be given more points, or
fewer, without moving anything else in the calibration. For example, a 6 x 8
driver's-wish map can become 9 x 8 so it covers higher engine speeds.

Right-click a map or curve in the tree, or in its table, and choose
**Resize axes...**. The item is greyed out when the table can't be resized;
hover over it to see why.

## The dialog

| Part | What it does |
|---|---|
| **Method** | **In place** grows or shrinks the table inside the space its definition reserves for it, up to the size shown. **Move it to free space (relocate)** writes the table to spare room in the dataset and re-points the ECU's pointer at it, so it can grow past its reserved space. Relocate is available on EDC15 ECUs; on EDC16 it is listed but not available yet, with the reason shown. |
| **New size** | The new number of X points (and Y points for a map). The line under it shows the current size, the stock size if the table has been resized before, the size the definition reserves, and how many bytes are free. |
| **New breakpoints** | How the breakpoints are first filled in: keep the existing points and add more past the end with the last step, or spread the points evenly over the current range. |
| **Breakpoints** | The breakpoints themselves, in the axis's units. Type over any of them. They must keep rising (or falling). |
| **Values after resizing** | The table as it will be. Each value is what the ECU would have interpolated at that breakpoint from the table as it is now, so the engine sees the same values within the old axis range. Beyond the old range the edge value is held, as the ECU does, and those cells are shown in grey. Edit the values afterwards. |
| **Save the project now...** | In a project, saves straight away as a version named after the resize, so it stands out in the version history. |

**Resize** is greyed out, with the reason in red, while the new size does not
fit or a breakpoint is not usable.

## Copies change together

A table can have more than one copy:

- An EDC16 coding-variant copy (for example the automatic-gearbox copy in the
  secondary block).
- The same table in each EDC15 dataset.

Every copy is resized with the table, so each coding reads the same shape.
If any copy has no room for the new size, nothing is resized.

## After resizing

- The resize is one step: **Undo** puts the bytes and the table's shape back.
- Checksums and signatures are corrected when you save, as for any edit.
- The table's Properties note says it was resized and gives its stock size.
- **Revert to last saved values** (F11) and **Revert to as-read values**
  (Ctrl+Shift+R) are refused for a table resized or moved since that
  version -- its old values no longer line up with its cells. Undo the
  resize, or go back through the version history, instead.
- **Revert Patch** leaves a table resized or moved after the patch as it
  is, and says so; the rest of the patch is reverted.

## Kept with the project

A project remembers which tables were resized and every size each has had.
Every time the set opens, after the module has built its definition, each
resized table takes the size its own record in the binary holds. This means:

- an updated module, which rebuilds the definition from stock, does not put a
  resized table back to its stock size;
- a version saved before the resize, or the original as read, still opens at
  the stock size.

A tab that is not a project yet keeps the change until it is closed. Save it
as a project to keep it for good.

## Comparing with an older version

When the comparison is a version from before the resize, the comparison side
of the table has the stock size. Its values are resampled onto the resized
table's breakpoints, and the banner says so: `comparison: the comparison is
8x6, this table 8x9 -- resampled onto this table's axes`. See [Comparing
files](comparison.md).

## Relocating (moving a table)

When a table needs to grow past the space reserved for it, choose **Move it to
free space** in the dialog. The enlarged table is written to spare room at the
end of its dataset, and the pointer the ECU uses to reach it is re-pointed at
the new copy. The original is left where it was as a fallback, so an ECU that
ever ignores the new pointer still finds a valid table.

- Every copy (each coded dataset) is moved into its own free space, all or
  none, so each coding reads the same table.
- The dialog shows how many bytes are free and refuses, with the reason, when
  the table won't fit or the ECU pointer for it can't be found.
- Like an in-place resize, a relocation is one undo step, is kept with the
  project, and is confirmed on every open by reading the ECU's pointer. A
  version from before the move, or an updated module, still finds the old
  table at its stock size.

Relocating is available on EDC15, and on EDC16. On EDC16 the ECU reaches a
table as a block base plus a fixed offset, so the whole block is moved and the
block pointer re-pointed. When the table is in the middle of its block, the
block is copied as-is and the enlarged table added at the end, with the
references that reach it redirected and the original left as a fallback; the
dialog shows what was redirected and a warning. **Moving a mid-block table on
EDC16 is not yet proven on a vehicle — flash it only with BDM recovery ready,
and confirm the whole table logs as you edited it.** A table the ECU reaches
only through a shared routine, or whose address the code builds directly, is
refused with the reason; resize those in place.

Some EDC15 ECUs (the unit-injector "EDC15P" cars) keep extra tables in a
**pre-page** just below the main calibration — the smoke limiters live there.
These can now be moved too: they have their own pointer table, and the moved
copy goes into free space elsewhere in the same dataset with the original left
as a fallback. There is usually little erased room in these ECUs, so a large
table is often refused for lack of space. **Moving a pre-page table is not yet
proven on a vehicle — flash it only with BDM recovery ready, and confirm the
whole table logs as you edited it.**

## What can't be resized (yet)

- **Map sets** that share one pair of axes, each map's values stored apart
  -- on EDC15 PD software the smoke limiters `mrwBRA0..2KR` and the
  start-of-injection sets such as `fnwSWGK0..9KR`. A set can only change size
  all together; the menu's tooltip names the set.
- Fixed-layout records with no point counts, such as the EDC15 fault-code
  conversion tables (`xcwFSPK...`): they aren't tuning maps.
- Tables with 32-bit or floating-point axes.
- Axes past 64 points.
- A table the ECU reaches in a way the tool can't re-point can't be relocated
  (it is refused, with the reason), and can still be resized in place.
