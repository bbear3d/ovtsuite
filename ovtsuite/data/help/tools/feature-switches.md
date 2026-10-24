---
title: Feature switches
order: 37
context: FeatureSwitchDialog
---
# Feature switches

**Tools > Feature switches...** turns functions of an EDC15 or EDC16 engine
ECU on and off: cruise control, the A/C type, EGR, the particulate filter,
the wideband O2 sensor, the smoke limiter's air-mass or boost-pressure input, the
gearbox type and more. A function is usually several calibration values that
must change together; the window changes them together, shows every value,
table and fault path before anything is written, and writes everything you
changed as **one undo step**.

The window lists only the functions this file carries (they come from the
EDC15 / EDC16 module). A file of another kind (a gearbox, a cluster) does not
offer the window.

## Change a function

1. Open the file and choose **Tools > Feature switches...**.
2. Pick the function on the left. The right side says what it does and what
   the file holds **now**: a setting's name when every value matches it,
   **partly** a setting when only some do (a tune that changed one switch of
   the set), or **no preset matches**.
3. **Set to:** starts at **Keep as is (no change)** for every function, so
   nothing is written unless you choose. Pick a setting: the **New** column
   shows the value each parameter gets.
4. Change any value yourself in the **New** column: pick it from the list (a
   value's meaning is shown) or type it. **Set to** then shows **Custom
   values**. The first entry, the file's own value, leaves a parameter as
   the file has it.
5. **Copies to change** appears when a value exists more than once (per
   dataset on EDC15, per coding copy on EDC16). Untick a copy to leave it.
6. Go on to other functions: each one keeps its changes, and **●** marks the
   ones changed. **Undo this feature's changes** forgets one function's
   changes.
7. **Apply** writes every function changed (a summary lists them first) as
   one undo step and leaves the window open; **OK** does the same and
   closes it; **Cancel** closes without writing anything.

In a project, **Save as project version** (on by default) keeps a version
named after the change. Checksums are corrected when the file is saved.

A switch the ECU reads from its EEPROM coding (its *Type* byte is 1) is set
to read this file's value instead; that byte is listed in the table too.

## Functions that edit tables or fault paths

Some functions are more than a switch. Under the parameters, the table lists
every table and fault path the function works through, with what it holds
now -- all of the time. Once a setting is picked, those it changes turn blue
with their new values; the rest say *(unchanged)*. Hover over a table to see
its cells as a grid, a changed cell as *old → new*. Double-click a table or
a parameter to open it in the main window (this window stays open).
Right-click a table and choose **View now and after the change...** to see it
in a window of its own -- its cells **Now** or **After the change**, the
changed ones shaded; the viewer only shows the table, the setting writes it.

| Function | Settings |
|---|---|
| **EGR** | **On (as calibrated)** puts the EGR tables back as the file was opened, and the EGR fault paths on. **Cut when the engine is warm** also fills the warmest coolant line of the EGR coolant correction, so EGR works while the engine warms up and stops above **Cut above:** (the axis is raised to that temperature when needed, never when another table shares the axis). **Off** holds the EGR valve shut through the tables the module names (they are listed under the parameters, with what each one gets) and switches the EGR fault paths off; the EGR maps themselves are not touched. |
| **Diesel particulate filter (DPF)** | Read from the switches that make the ECU run the filter -- the module names them, and they are the function's parameters, each editable in the **New** column. **Removed** switches them off and the filter's fault paths off: the calibration of a car without a filter, the way the same software is set up for one. **Fitted** switches them back on with the values stock filter files hold, and puts each fault path back as it was when the file was opened (or as the comparison has it). |
| **Electric auxiliary heater (PTC)** | The A2L's switch covers heating elements 1 and 2 (its own wording); it names none for a third. |
| **Smoke limiter: air mass or boost pressure** | Switching to boost pressure fills the boost-pressure limiter map from this file's air-mass limiter, so the limit stays what it is today (a 2 x 2 placeholder map is enlarged first), and fills a flat intake-temperature correction. A file that already limits by boost pressure keeps its own map. |

A fault path switched back on gets the class it had when the file was
opened. One that was already off then gets the class it has in the
comparison file (in a project, the file as first read); with no class to go
back to, it stays off -- except when a file opened without a filter is set
to **Fitted**: its paths then get the ECU's usual warning-lamp class.

## Ask the AI

**Ask AI about this feature...** opens the AI assistant with a question about
the selected function: what each setting does, what the file holds now and
what changing it would do. The assistant reads the functions with its
*feature_switches* tool and works out a change with *feature_switch_changes*;
a parameter change it proposes goes through your review like any other AI
change, and the tables and fault paths a setting also changes are applied
here.

## Tips

- Turn single fault codes on or off in the [DTC / Fault-Path editor](dtc.md).
- The window follows edits made elsewhere (**Undo**, the grid, another tool)
  while it is open.
