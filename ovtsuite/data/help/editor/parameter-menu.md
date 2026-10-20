---
title: The parameter menu and Properties
order: 15
context: ParameterPropertiesDialog
---
# The parameter menu and Properties

Right-click a parameter in the tree for everything you can do with it in one
place. Right-click a heading to open or close everything under it.

## Right-click a parameter

| Item | What it does |
|---|---|
| **Open** | Opens it in the preview table tab, as a click does. |
| **Open in a new table tab** | Opens it in a tab of its own beside the one showing, as a middle-click does. |
| **Keep open** | Opens it in a table tab that stays open, as a double-click does. |
| **Show in hex editor** | Opens the [hex editor](hex-editor.md) with the parameter's bytes selected: **Map data** (or **Value**), **X axis** or **Y axis**. An axis the definition gives as numbers is not stored in the binary and is not listed. |
| **Properties...** | Opens the parameter's Properties window (below). |
| **Resize axes...** | Maps and curves: gives the table more (or fewer) breakpoints. See [Resizing a table](resizing-tables.md). Greyed out, with the reason as its tooltip, when the table can't be resized. |
| **Copy** | Copies to the clipboard its **Symbol**, its **Title** (the name the tree shows), its **Address** (where its data starts, in hex), its **Values** (tab-separated, with the axes as the first row and column, ready to paste into a spreadsheet) or its **Properties** as text. |
| **Show 3D surface** | Opens the parameter and shows the [3D surface](3d-surface.md) above its table; ticked while the surface is shown, and choosing it then hides it. Only for a map with at least two rows and two columns. |
| **Show differences...** | With a comparison open: the list of every parameter that differs (as **Edit > Show differences...**). See [Comparing files](comparison.md). |
| **Copy from comparison** | With a comparison open: overwrites the whole parameter with the comparison's, in one step you can undo. When the axes differ you are asked whether to copy them too. Greyed out when the comparison has no matching parameter. |
| **Add parameter...** | Defines a table or value of your own. See [User-defined parameters](user-parameters.md). |
| **Edit user parameter...**, **Delete user parameter** | Only on a parameter you defined. |
| **Duplicate as user parameter...** | On a detected parameter: your own copy of its definition, to change as you like and kept with the project; the original can be hidden so the tree and searches show the copy instead. See [Changing a detected parameter's definition](user-parameters.md#changing-a-detected-parameters-definition). |
| **Ask AI** items | See [The assistant panel](../ai/assistant.md). |

In a read-only tab (a saved version opened to look at), the items that
would change the binary, and the hex editor, are greyed out.

## Right-click a heading

| Item | What it does |
|---|---|
| **Expand all** | Opens the heading and every heading under it. |
| **Collapse all** | Closes them again. |
| **Add parameter...** | As above. |

## Properties

The Properties window lists everything known about one parameter. It stays
open beside the main window, so you can open several and compare them.

- **General** -- its title and symbol, kind and size, units, description
  (and what each axis describes, where the definition says), its group and
  category, and how its address was established in this file. A parameter
  whose address was inferred rather than confirmed says so: check it before
  editing.
- **Storage** -- one row for its values and one for each axis: the first
  and last byte, how many bytes, how many cells or points, how each is stored
  (bits, signed or not, float, byte order, and for a map whether rows or
  columns come first), the equation that turns a stored number into the
  value shown, and the units. An axis given as numbers by the definition says
  **given as values by the definition**. **Show in hex** opens the hex editor
  with that row's bytes selected.
- **Values** -- the lowest and highest value it holds now, each axis's range,
  the range a cell can store, the conversion as a factor and offset, and --
  with a comparison open -- where the comparison keeps it and whether its
  values differ.

**Copy all** copies the window as plain text, for a note or a forum post.
