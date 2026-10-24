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
   values**. The file's own value is marked **— current**; choosing it
   keeps the parameter as the file has it, even when the setting picked
   would change it.
5. Go on to other functions: each one keeps its changes, and **●** marks the
   ones changed. **Undo this feature's changes** forgets one function's
   changes.
6. **Apply** writes every function changed (a summary lists them first) as
   one undo step and leaves the window open; **OK** does the same and
   closes it; **Cancel** closes without writing anything.

Checksums are corrected when the file is saved; save with the main window's
**Save** (in a project, that keeps a version too).

## Copies: datasets and coding variants

Some values exist more than once. On EDC15 each *dataset* (Manual,
Automatic ...) has its own copy; on EDC16 some tables and values have a
second, *coding-variant* copy that other codings read (the 5-speed manual
coding's copy next to the DSG coding's, say). When a file has copies,
**Copy to view and change** at the top of the window picks which one the
whole window works on:

- **All copies** (the default) shows each copy's value in the **Now**
  column where they differ (*DSG: 1 · 5-speed manual: 0*), and a setting or
  value you pick goes to every copy. **Set to** says **Differs per copy**
  when the copies are getting different changes.
- One copy shows and changes that copy only; the **Now** column still names
  another copy's value beside it when it differs.

A value with no coding-variant copy is read by every coding: it shows the
same in both, and is written once. Hover over a parameter to see each
copy's value and address.

A switch the ECU reads from its EEPROM coding (its *Type* byte is 1) is set
to read this file's value instead; that byte is listed in the table too.

## Functions that edit tables or fault paths

Some functions are more than a switch. Under the parameters, the table lists
every table and fault path the function works through, with what it holds
now -- all of the time. Once a setting is picked, those it changes turn blue
with their new values; the rest say *(unchanged)*. Hover over a table to see
its cells as a grid, a changed cell as *old → new*. A table that is enlarged
(the smoke limiter's placeholder boost map) is listed with its new size, and
each of its axes with the breakpoints it gets. Double-click a table or
a parameter to open it in the main window (this window stays open).
Right-click a table and choose **View now and after the change...** to see it
in a window of its own -- its cells **Now** or **After the change**, the
changed ones shaded; the viewer only shows the table, the setting writes it.

Each fault path can be switched on (with the class it gets) or off on its
own in the **New** column, with or without a setting picked: its state now
is marked **— current**. Your choice for a path wins over what the setting
does to it, and is written on **Apply** with everything else, in the same
undo step.

| Function | Settings |
|---|---|
| **EGR** | **On (as calibrated)** puts the EGR tables back as the file was opened, and the EGR fault paths on. **Cut when the engine is warm** also fills the warmest coolant line of the EGR coolant correction, so EGR works while the engine warms up and stops above **Cut above:** (the axis is raised to that temperature when needed, never when another table shares the axis). **Off** holds the EGR valve shut through the tables the module names (they are listed under the parameters, with what each one gets) and switches the EGR fault paths off; the EGR maps themselves are not touched. |
| **Diesel particulate filter (DPF)** | Read from the switches that make the ECU run the filter -- the module names them, and they are the function's parameters, each editable in the **New** column. **Removed** switches them off and the filter's fault paths off: the calibration of a car without a filter, the way the same software is set up for one. **Fitted** switches them back on with the values stock filter files hold, and puts each fault path back as it was when the file was opened (or as the comparison has it). |
| **Boost pressure (MAP) sensor** (EDC16, EDC15) | Sets the ECU's boost-sensor linearisation to the Bosch sensor fitted: 1 (10-115 or 20-115 kPa), 2.5, 3.0, 3.5, 3.8, 4.0 or 6.0 bar **absolute** (boost is about 1 bar less). 2.5 and 3.0 bar (Bosch characteristic curve 1: 0.40 V at 200 hPa, 4.65 V at 2500 / 3000 hPa, e.g. 0 281 002 399 / 0 281 002 401) are written as stock VW files hold them; 3.5 and 4.0 bar (curve 2: 0.50 V at 500 hPa, 4.50 V at 3500 / 4000 hPa, e.g. 0 281 002 456, 0 281 002 316 / 0 281 006 060) come from Bosch's sensor catalogue, as do 3.8 and 6.0 bar (curve 2, 15-380 / 50-600 kPa, e.g. 1 267 030 835 / 0 281 002 420) and the two 1 bar sensors (curve 1, e.g. 0 261 230 052 / 0 261 230 020; they read up to 1.15 bar, so no boost: for a non-turbo engine, or a sensor before the turbo). The window recognises the sensor from the line the curve holds, and says so when it matches none. Only the curve changes: bring boost setpoints and limits within the new sensor's range yourself (the window warns when the largest boost setpoint is above it). Check the part number on the sensor itself. The catalogue lists no 1.5, 2 or 5 bar absolute sensor; a "2 bar" boost sensor is the 3.0 bar absolute one. On EDC15 each dataset has its own curve, listed per dataset. |
| **Electric heater outputs** (EDC16, EDC15) | The ECU outputs switching the electric heating elements that warm the cabin heater while the engine is cold: on EDC16 the heater switch for outputs 1 and 2 plus the number of connected elements; on EDC15 the number of glow-element outputs of the coolant heater. Files with coding variants (e.g. 1.9 TDI BEW) have a copy per coding: stock files have the outputs on for manual-gearbox codings and off for automatic ones (on EDC15, on in manual datasets, off in automatic ones). **On** sets three elements; enter the number fitted if fewer. |
| **Road speed source** (EDC15) | 1: a speed sender wired to the ECU's road-speed input (normally on the gearbox; its pulse constant is in VSS pulse settings). 2: a tachograph's speed output. 3: the CAN message from the ABS / ESP unit. 4: the CAN message from the instrument cluster. 3 and 4 exist only on later (P+ / VGK) software. |
| **Smoke limiter: air mass or boost pressure** | Switching to boost pressure fills the boost-pressure limiter map from this file's air-mass limiter, so the limit stays what it is today (a 2 x 2 placeholder map is enlarged first -- moved to free space when it cannot grow where it is -- with new engine-speed and boost-pressure axes), and fills a flat intake-temperature correction. A file that already limits by boost pressure keeps its own map. The list shows the air-mass limiter and the volumetric efficiency too: the fill reads them, it does not write them. |

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
