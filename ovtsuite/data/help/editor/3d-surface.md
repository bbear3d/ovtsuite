---
title: 3D surface
order: 35
context: SurfacePanel, Surface3DView
---
# 3D surface

The 3D surface draws a map as a landscape — height and colour both show the
value — so a step, a spike or an uneven region stands out in a way it does
not in a grid of numbers. You can also edit the map on it: pick points,
raise or lower them with the mouse, or use the cell keys of the table.

The surface is shown in the tab itself, above the table, with a splitter
between them: drag the splitter to give the surface or the table more room.

1. Open a map (a table with at least two rows and two columns).
2. Click **3D surface** on the edit bar, or press Ctrl+3. The button stays
   pressed while the surface is shown.
3. To hide it again, click **3D surface** again, press Ctrl+3 again, or click
   **×** at the right of the surface's control row.

Whether the surface is shown, and where the splitter is, are remembered: a
new tab, and the app the next time it starts, open the same way. Each tab
shows or hides its own surface. While it is hidden it costs nothing — it is
neither drawn nor kept up to date, and catches up as soon as it is shown.

The surface follows the table: open another map and the surface shows that
one (a curve or a single value shows "Not a 2-D map"); an edit — made on the
surface, in the grid, by a tool or by **Undo** / **Redo** — redraws it at
once, and so do **Swap axes** and a converted Z view (the surface shows the
values as the table shows them).

![A map and its comparison as 3D surfaces](../images/main-3d.png)

With a comparison open (a project tab starts out compared with the original
as read), the panel shows two surfaces side by side — **Primary** and
**Comparison -** *name* — on one height scale and one colour scale, and
turning or zooming one turns or zooms the other, so their shapes can be
compared directly. Close the comparison first for a single surface.

## Selecting points

Each point of the surface is a cell of the map, and the points you select
on the surface are the cells selected in the table, and the other way
round. Selected points are marked with white dots; the current cell has a
ring, and its position and value are shown at the top left.

| Mouse | What it does |
|---|---|
| Click a point | Selects that cell (and only that one). |
| Shift+click a point, or Shift+drag | Selects the block from the current cell to the point. |
| Ctrl+click a point | Adds the cell to the selection, or takes it out. |
| Drag (away from a point) | Rotates the surface. |
| Mouse wheel | Zooms in and out. |

## Raising and lowering points

Hold Ctrl, press on a selected point and drag up or down: every selected
point moves by the same amount while you drag, in steps of the map's own
step (see **Edit > Cells > Set step for this map...**). Release the mouse to
write it — the whole drag is one **Undo** step. Press Esc before releasing
to cancel. "Off" cells of a converted ZF view are left as they are, and a
read-only table cannot be changed: on a saved version opened read-only the
surface turns, zooms and selects, but does not edit (its title says
*(read-only)*).

## Keys

The surface takes the keys while it has the focus (click it first):

| Keys | What it does |
|---|---|
| Arrows | Move the current cell, as in the table. |
| Shift+arrows | Extend the selection. |
| Ctrl+arrows | Rotate (left/right) and tilt (up/down) the surface. |
| + / - | Zoom in / out. |
| Home | Back to the starting angle and zoom. |
| Alt+Up / Alt+Down, Alt+Shift+Up / Down | Selected cells one step / ten steps up or down. |
| Alt+PgUp / Alt+PgDn | Selected cells +1 % / -1 %. |
| Ctrl+Shift+L / Ctrl+Shift+M | Interpolate / smooth the selection. |
| Ctrl+R / Ctrl+Shift+R | Copy the selection from the comparison / back to the as-read values. |
| Ctrl+Z / Ctrl+Y | Undo / redo. |
| Esc | Cancel a drag. |
| Ctrl+3 | Hide the surface (the keys go back to the table). |

The cell keys are those of **Edit > Cells** (see
[Keyboard shortcuts](keyboard-shortcuts.md#edit-cells)); a shortcut changed in
**Tools > Options... > Keyboard shortcuts** changes here too.

## The control row

Along the top of the surface: the map's name, messages (such as how many
cells a drag moved), and

| Control | What it does |
|---|---|
| **Reset view** | Back to the starting angle and zoom. |
| **Wireframe** | Draws only the grid lines, so the far side shows through. |
| **Height** | Stretches or flattens the surface (display only). |
| **×** | Hides the surface (as **3D surface** / Ctrl+3 does). |

Colours run from blue (lowest value) to red (highest). The line along the
bottom of each surface gives the value range and units, and the number of
X and Y points.

The comparison surface can be turned and zoomed but not edited.
