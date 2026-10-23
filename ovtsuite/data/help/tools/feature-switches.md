---
title: Feature switches
order: 37
context: FeatureSwitchDialog
---
# Feature switches

**Tools > Feature switches...** turns functions of an EDC15 or EDC16 engine
ECU on and off: cruise control, the A/C type, EGR, the particulate filter's
fault paths, the smoke limiter's air-mass or boost-pressure input, the
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
   values**. **Keep** leaves a parameter as the file has it.
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
every table, table axis and fault path the setting changes, with its value
now and after.

| Function | Settings |
|---|---|
| **EGR** | **On (as calibrated)** puts the EGR tables back as the file was opened, and the EGR fault paths on. **Cut when the engine is warm** also fills the warmest coolant line of the EGR coolant correction, so EGR works while the engine warms up and stops above **Cut above:** (the axis is raised to that temperature when needed, never when another table shares the axis). **Off** sets the EGR request maps to 0 % on EDC16 (on EDC15 it raises the EGR gate thresholds, which holds the valve shut) and switches the EGR fault paths off. |
| **Diesel particulate filter (DPF) faults** | **All fault paths off** switches the filter's fault paths off. **Fault paths on, as the file was opened** puts each back as it was; **All fault paths on** also switches on those that were already off (with the ECU's usual warning-lamp class where no earlier class is known). **Now** says how many are off. It does not stop the ECU modelling soot or asking for regenerations; the filter configuration switch is shown, not changed, because what its values select has not been established. |
| **Smoke limiter: air mass or boost pressure** | Switching to boost pressure fills the boost-pressure limiter map from this file's air-mass limiter, so the limit stays what it is today (a 2 x 2 placeholder map is enlarged first), and fills a flat intake-temperature correction. A file that already limits by boost pressure keeps its own map. |

A fault path switched back on gets the class it had when the file was
opened. One that was already off then gets the class it has in the
comparison file (in a project, the file as first read); with no class to go
back to, it stays off (except with **All fault paths on**).

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
