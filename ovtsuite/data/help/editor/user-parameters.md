---
title: User-defined parameters
order: 16
context: UserParamDialog
---
# User-defined parameters

When the definitions do not list a table you know is there -- you found it
in the [hex editor](hex-editor.md), or a forum post gives its address --
define it yourself. It then works like every other parameter: it opens in a
table, edits are undoable, it shows in a comparison and in patches, and it
can be found with the Find panel.

Right-click anywhere in the parameter tree and choose **Add parameter...**.

## The fields

| Field | What to enter |
|---|---|
| **Symbol** | A short name without spaces, unique in this file. |
| **Title** | The name the tree shows (the symbol when left empty). |
| **Folder** | A heading under **User defined** to keep it in (by default **Maps**, **Curves** or **Values**). |
| **Description** | Anything you want to remember about it. |
| **Kind** | **Map** (rows and columns, an X and a Y axis), **Curve** (one row and an X axis) or **Value** (a single cell). |
| **Address** | Where its first cell is, in hex, counted from the start of the binary as the hex editor shows it. |
| **Size** | Rows (the Y axis's points) and columns (the X axis's points). |
| **Cell** | 8, 16 or 32 bits, signed or not, or a 32-bit float. The byte order is always the binary's own. |
| **Stored** | For a map: whether the cells go along a row first (X varies fastest) or down a column first (Y varies fastest). If the preview looks scrambled, try the other. |
| **Equation** | How a stored number becomes the value shown, in terms of `X`: `X`, `X*0.1`, `(X)/(8192)`, `X*0.75-48`. Numbers, `X`, `+ - * /` and brackets. |
| **Units** | Shown beside the values. |

Each axis is one of:

- **None** -- the cells are numbered 0, 1, 2 ...
- **Stored in the binary** -- its address, point size, sign, equation and
  units, like the data. It has as many points as the table has columns (X)
  or rows (Y).
- **Fixed values** -- type the points, separated by commas.

The **Preview** shows the first values it would read, with the axes, and
the range of all of them. **OK** is available once everything fits: the
symbol is new, the equation can be read, and the data and axes are inside
the binary. What is wrong is listed in red above the buttons.

## Changing a detected parameter's definition

The parameters the app finds come from its definitions, and are found again
whenever a file opens -- so a change to one of them would be undone by the
next match, or by a module update. To change how a detected parameter is
read (its title, scaling, units, size, axes or address), make your own copy
of it:

1. Right-click it in the tree > **Duplicate as user parameter...**.
2. The window opens filled in with exactly what the parameter reads, named
   `<symbol>_edit` and titled *(edited)*. Change what you need; the
   **Preview** shows the result.
3. **Hide the original (\<symbol\>)** is ticked: the copy is then shown in the
   original's place in the tree, and **Find** and the parameter filter find
   the copy instead of the original. Untick it to keep both.
4. **OK**.

The copy is a user parameter like any other: kept with the project, never
changed by re-matching or a module update. To bring the original back,
**Edit user parameter...** the copy and untick **Hide the original**, or
delete the copy.

## Where they are kept

User parameters are listed in their own group, **User defined**, and are
kept **with the project** -- for that binary set, in every version and
branch of it -- and come back each time it opens. A tab that is not a
project yet keeps them until it is closed; saving it as a project keeps them
with the project. They are a description only: adding, changing or deleting
one never changes a byte of the binary.

To change one, right-click it > **Edit user parameter...** (or **Edit
parameter...** in its **Properties**). To remove it, right-click it >
**Delete user parameter**.

A user parameter is not checked against the ECU's own definition. If the
address, size or equation is wrong, the values shown are wrong too, and
editing them writes to whatever is really at that address. Check the
preview and the [hex editor](hex-editor.md) before you edit.
