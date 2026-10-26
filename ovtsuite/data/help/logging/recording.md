---
title: Recording and the live chart
order: 40
---
# Recording and the live chart

Once connected and with cells selected, the lower right of the connection tab
records the log.

## Record a log

1. Check where the log goes. When the controller's binary is open in a
   project tab, **Keep the log in the project <name> (<set>)** is ticked and
   the log is kept in the project (see [Logs in the
   project](../logging.md#logs-in-the-project)). Untick it to write a file:
   **Log file:** is filled in for you (see below); type another path or
   press **...** to choose one.
2. Press **Start logging**. If no cell is ticked, the logger asks you to tick
   at least one.
3. Drive. Every row reads all the groups your cells come from, writes one
   line to the log, updates the **Value** column of **Selected cells** and
   adds a point to the strip chart. The counter beside the buttons shows the
   rows written and the rate over the last few seconds, for example
   "812 rows, 6.4 Hz". While you are connected but not logging, it shows how
   often the group you are browsing is read, for example "8.1 Hz".
4. **Pause** (a toggle) stops writing rows but keeps the session open with
   keep-alive messages — use it while you wait or turn round. Press it again
   to carry on in the same file.
5. **Stop** ends the log. The connection stays open and goes back to
   browsing. The status line says how many rows were written and where
   (*... rows kept in the project (saved with it).* for a log kept in the
   project), and the log opens in the [Log file](log-files.md) tab. A row
   that was being read as you pressed **Stop** is not added.

You can stop and start again without disconnecting; each **Start logging**
makes a new log. **Disconnect** while logging stops the log the same way and
then closes the session. Closing the logger window stops logging, keeps the
log and disconnects; it waits at most a few seconds for the connections to
close.

In OVT Logger, a log kept in a project is written into the `.ovt` in the
background: the status line says "putting them into the project..." until it
is done.

## The log file name

When the log is written to a file, the default name is the name of the open file for this controller (or the
controller's name when no file for it is open), the date and time, and `.csv`,
in the folder you last saved a log to — for example
`03G906016LQ_20261010_213250.csv`. A preset adds its short name:
`03G906016LQ_20261010_213250-boost.csv`. If that name is already used by the
other tab of a dual log, or the file exists, `-2` (`-3`, ...) is added.

After **Stop** the box gets a fresh name with the new time, so the next log
never overwrites the last one. A name you type yourself is used as it is: if
that file exists, starting a log replaces it.

Rows are written to disk as they arrive, so a log is complete up to the last
row even if the session is cut off. A log kept in the project is recorded to
a working copy the same way, and goes into the project when you press
**Stop** or the session ends.

## The live chart

The strip along the bottom of the tab charts the last 30 seconds of the
current log.

- Every selected cell is a line in its own colour, scaled to its own minimum
  and maximum over what is on screen — the chart shows the shape and timing of
  each value, not values against a common axis.
- The legend across the top gives each cell's name and its latest value.
- The time at the left and right edges is seconds since logging started.
- Cells that are not numbers (or read "N/A") leave gaps.

For a whole log, with a cursor and the rows beside it, use the
[Log file](log-files.md) tab.

## What "N/A" means

A cell is written as "N/A" when its group did not answer on that row — the
control unit refused the group, or it is a group the ECU lists but does not
serve. If a whole column is N/A, that group is not available on this control
unit; browse it before logging to check (the status line says "Group N: not
available on this ECU."). See [Troubleshooting](troubleshooting.md).

Next: [Dual logging](dual-logging.md).
