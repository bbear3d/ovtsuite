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
| Typing a value | Replaces the current cell's value. |
| F2 | Edit the current cell's value. |
| Enter | Keep the value typed. |
| Esc | Leave the cell as it was. |
| Ctrl+3 | **3D surface** |
| Enter in the **Modify selection** box | **Apply** |

Mouse: double-click a cell to edit it; right-click for **Copy**,
**Paste** and copying from another tab.

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
