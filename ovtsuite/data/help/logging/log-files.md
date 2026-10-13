---
title: Log files
order: 60
---
# Log files

Logs are CSV — kept in the car's project, or as files. The logger writes its
own layout (the same one the KWHack tool uses, so logs move between the two
unchanged), reads that layout and the VCDS CSV layout, and exports either.
The **Log file** tab opens a log, charts it and shows its rows.

![The Log file tab with a ZF 6HP19 drive log](../images/log-viewer.png)

## Review a log

1. Open the **Log file** tab. A log you have just stopped is already there.
2. To open another, press **Open log...**. It lists the logs kept in the
   open projects — under each project, newest first, with the set they were
   recorded against — and **From a file...** for a `.csv` on disk (with no
   kept logs, the file picker opens straight away). In the OVT Logger it
   lists the logs of the project it has open. A kept log also opens from
   the Project panel: double-click it. The layout — native or VCDS — is
   recognised by itself.
3. The line at the top right gives the file name, the number of channels and
   rows, the length in seconds and the layout, for example
   "... - 8 channels, 1757 rows, 407.7 s (kwhack layout)".
4. **Click the chart** to put a cursor there: the matching row is selected in
   the table, and the legend shows every channel's value at that moment.
   Selecting a row in the table moves the cursor to it.

## The chart and the table

- The chart shows the whole log. As in the live chart, each channel is scaled
  to its own range, so it shows when things happen and how they move together.
- The time base is seconds from the first row; the table's first column,
  **Time (s)**, is the same.
- A column whose cells are not plain numbers is not drawn.
- The table holds the first 4000 rows of a long log (the line at the top says
  so); the chart and the exports always use every row.

## Names in old or foreign logs

When a log only has generic column names ("Group 11 / Cell 3", or a VCDS
"Group 11 - Field 3" column), the viewer names those columns from an open
file whose measuring-block map covers the log's groups — with dual logging,
from whichever connection's map matches. Open the car's own file first to get
names in an old log.

## Export

| Button | Writes | Default name |
|---|---|---|
| **Export as VCDS CSV...** | the VCDS CSV layout, which existing VCDS-log viewers and analysers open | the log's name + `_vcds.csv` |
| **Export as KWHack CSV...** | the native layout — useful for turning a VCDS log into one | the log's name + `_kwhack.csv` |

Both export the log that is open in the tab, every row. A log kept in a
project is exported as it is with **File > Export > Data log...**, or
**Export...** on its right-click menu in the Project panel.

## The native layout

```
# Cells: 1.1,1.2,1.3,1.4,16.1,16.2,16.3,16.4
Timestamp,Engine speed (Tcu_nEng),Turbine speed (G182) (Tcu_nTurb),...
2026-10-08 16:27:59.498,864 rpm,864 rpm,...
```

- The first line lists the logged cells as group.cell (cell numbers start
  at 1).
- The header names each column: the cell's description and, where known, its
  signal name in brackets. **Tune from logs** uses that signal name to tell
  which column is which.
- Each row starts with the date and wall-clock time to the millisecond, then
  every cell as the control unit's display text — value and unit, for example
  `864 rpm`, or `N/A` when its group did not answer.
- Dual-logging files are stamped from the same clock, so their times can be
  matched row by row.

A cell the logger had no formula for is written as its three raw bytes in
brackets, for example `(177 128 12)`. When the log is opened again, those are
decoded with whatever formulas the app knows by then, so updating the app can
recover such a column.

## The VCDS CSV layout

On export the file gets a short header (date, the app's version, the control
unit's identification when known), a row of group/field markers, the column
titles and a row of units, then one row per sample. Each cell has its own
time column in seconds from the start; values are written as plain numbers,
since the units are on their own row. When a VCDS log is opened, the groups,
labels, units and start time are read from those header rows.

Next: [Reading fault codes](fault-codes.md).
