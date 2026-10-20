---
title: Keyboard shortcuts
order: 90
---
# Keyboard shortcuts

Every key the main window and its editing windows use, in one place. The
menu shortcuts work from anywhere in the main window. These are the
defaults: any menu item's shortcut can be changed, added or removed in
**Tools > Options... > Keyboard shortcuts** (see
[Options](../options.md#keyboard-shortcuts)).

## File

| Keys | Action |
|---|---|
| Ctrl+N | **File > New project...** |
| Ctrl+O | **File > Open project...** |
| Ctrl+Shift+T | **File > Reopen last closed project** |
| Ctrl+Shift+I | **File > Import > Into a new project...** |
| Ctrl+I | **File > File information...** |
| Ctrl+W | **File > Close tab** |
| Ctrl+S | **File > Save** -- the whole project (corrects the checksums) |
| Ctrl+Alt+S | **File > Save this branch only** |

## Edit

| Keys | Action |
|---|---|
| Ctrl+Z | **Edit > Undo** |
| Ctrl+Y | **Edit > Redo** |
| Ctrl+F | **Edit > Find...** — open the Find panel |
| Ctrl+Shift+F | **Edit > Find in definition...** — the Find panel, searching only the definition file's maps |
| F3 | **Edit > Find next result** |
| Shift+F3 | **Edit > Find previous result** |
| Ctrl+C | **Edit > Copy cells** |
| Ctrl+V | **Edit > Paste cells** |

### Edit > Cells

They act on the selected map cells while the table has the focus, each as one
undo step.

| Keys | Action |
|---|---|
| Alt+Up / Alt+Down | **Step up** / **Step down** -- by the map's own step |
| Alt+Shift+Up / Alt+Shift+Down | **Step up x10** / **Step down x10** |
| Alt+PgUp / Alt+PgDn | **Increase 1%** / **Decrease 1%** |
| Ctrl+Shift+L | **Interpolate selection** -- along a row, down a column, or a block from its four corners, by the selection's shape |
| Ctrl+Shift+M | **Smooth selection** (one 3x3 pass) |
| Ctrl+R | **Copy from comparison** |
| Ctrl+Shift+R | **Revert to as-read values** -- the project's original read (the file as opened, outside a project) |
| F11 | **Revert to last saved values** -- the values the selected cells had when the file or project was last saved (the file as opened, before the first save). Also on the toolbar and the table's right-click menu. |
| | **Set step for this map...** -- the step Alt+Up / Alt+Down use for this map, remembered per map. Unset, it is one raw step: the smallest change the map can hold. |

## View

| Keys | Action |
|---|---|
| Ctrl+T | **View > Swap X and Y axes** |
| Ctrl+F4 | **View > Close table** |
| Ctrl+PgDown | **View > Next table** |
| Ctrl+PgUp | **View > Previous table** |
| Ctrl+Shift+P | **View > Project panel** -- expand it, or collapse it to its strip |
| Ctrl+Tab | **View > Next tab** |
| Ctrl+Shift+Tab | **View > Previous tab** |
| Ctrl+= (or Ctrl++) / Ctrl+- / Ctrl+0, or Ctrl + mouse wheel | Zoom the table (or the hex editor) in, out, back to normal -- remembered, and the same for every tab |

## Tools

| Keys | Action |
|---|---|
| Ctrl+D | **Tools > DTC / Fault-Path editor...** |
| Ctrl+M | **Tools > Measuring blocks...** |
| Ctrl+G | **Tools > Gear ratios...** |
| Ctrl+U | **Tools > VSS pulse settings...** |
| Ctrl+H | **Tools > Hex editor...** |
| Ctrl+E | **Tools > ECU read / write...** |
| Ctrl+L | **Tools > Data logging...** |

## Help

| Keys | Action |
|---|---|
| F1 | Help for what you are working in: a panel with its own page (such as the Find panel), else the window in front (the help's front page when neither has a page of its own). |

## In the table

| Keys | Action |
|---|---|
| Arrow keys | Move between cells. |
| Shift+arrow keys | Extend the selection. |
| Ctrl+A | Select the whole grid. |
| Ctrl+arrow keys | Jump to the edge of the data, as in Excel: to the end of a run of values, or across blank cells to the next value. The axes, the map and the comparison are separate regions. |
| Ctrl+Shift+arrow keys | Extend the selection to the edge of the data. |
| Typing a value | Replaces the current cell's value. |
| F2 | Edit the current cell's value. |
| Enter | Keep the value typed. |
| Ctrl+Enter (while editing) | Keep the value typed and write it into every selected cell. |
| Left at the start / Right at the end of the text (while editing) | Keep the value and move to the cell on the left / right. |
| Esc | Leave the cell as it was. |
| = | A sum for every selected cell, typed in a box at the cell: `+5`, `-5`, `*1.05`, `/2`, `=120`, `+3%`, or an expression in `x`, the cell's value (`x*1.1+2`). Enter applies it, Esc cancels. Values are as shown (in a ZF converted view, engine rpm or road speed). |
| Ctrl+3 | Show / hide the **3D surface** above the table |
| Enter in the **Modify selection** box | **Apply** |

Mouse: double-click a cell to edit it; right-click for **Copy**,
**Paste** and copying from another tab.

## In the 3D surface

| Keys / mouse | Action |
|---|---|
| Click a point | Select its cell (in the table too). |
| Shift+click, Shift+drag, Shift+arrows | Extend the selection. |
| Ctrl+click | Add a cell to the selection, or take it out. |
| Ctrl+drag up/down on a selected point | Raise / lower every selected cell (one undo step; Esc cancels). |
| Arrows | Move the current cell the way the arrow points on the screen. |
| Ctrl+arrows, Ctrl+Shift+arrows | Jump / select to the edge of the data, as in the table. |
| [ / ], PgUp / PgDn, drag | Rotate, tilt. |
| + / -, mouse wheel | Zoom. |
| Home | Reset the view and the height scale. |
| Edit > Cells keys, Ctrl+Z, Ctrl+Y | As in the table. |
| Esc | Cancel a Ctrl+drag. |
| Ctrl+3 | Hide the surface. |

See [3D surface](3d-surface.md).

## In the tree and the tabs

| Mouse | Action |
|---|---|
| Click a parameter | Open it in the preview table tab. |
| Double-click a parameter | Open it in a table tab that stays open. |
| Middle-click a parameter | Open it in a new table tab. |
| Double-click a table tab | Keep it open. |
| Middle-click a table tab or a project tab | Close it. |
| Right-click a table tab | Keep open, close, close others, copy the symbol name. |

## In the Find panel

| Keys | Action |
|---|---|
| Enter | Search; once results are listed, next result. |
| Shift+Enter | Previous result. |
| Ctrl+Enter | Open the selected result in its own table tab. |
| Down / Up | Next / previous result. |
| Esc | Close the panel (from the query box). |
| Enter in the results | Open the result in its own table tab. |
| Esc in the results | Back to the query box. |

## In the hex editor

| Keys | Action |
|---|---|
| Two hex digits | Write the byte and move to the next. |
| F2 | Edit the current byte. |
| Ctrl+C | Copy the selected bytes as hex. |
| Ctrl+V | Paste hex at the selected byte. |
| Enter in **Go to offset:** | Go. |
| Enter in **Find:** | Find next. |

## In Version history

| Keys | Action |
|---|---|
| F2 | Rename the selected version. |

## In the Project panel

| Keys | Action |
|---|---|
| Esc | Close the panel when it is collapsed and has opened over the window. |

## In the help window

| Keys | Action |
|---|---|
| Alt+Left (Cmd+[ on a Mac) | Back. |
| Alt+Right (Cmd+] on a Mac) | Forward. |
| Ctrl+F | Go to the help's search box. |

See also: [The main window](../interface.md), [Tables and editing
values](tables-and-editing.md), [The Find panel](search.md), [Hex
editor](hex-editor.md).
