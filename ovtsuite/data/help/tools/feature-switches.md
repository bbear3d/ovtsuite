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
EDC15 / EDC16 module). A function the module knows but will not offer on
this file is listed under the functions, greyed, as **Not available on this
file**, with the reason: EGR on an EDC15 file whose definition places
the EGR switch on a byte that holds neither 0 nor 1 (a placement in doubt,
so nothing is written there), or places neither the switch nor the EGR
coolant correction; or cruise control on an EDC15 file whose definition
places the cruise control type on a byte holding a value that is no type
the software knows. An EDC15 file with the coolant correction but no EGR
switch still offers EGR, as **On** and **Cut when the engine is warm**
only: those change just the coolant correction, and **Off** needs the
switch. A file of another kind (a gearbox, a cluster) does not offer the
window.

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
**Apply to coding:** (EDC16) or **Apply to dataset:** (EDC15) at the top of
the window picks which ones the whole window shows and changes:

- **All codings** / **All datasets** (the default) shows each copy's value
  in the **Now** column where they differ (*DSG: 1 · 5-speed manual: 0*),
  and a setting or value you pick goes to every copy. **Set to** says
  **Differs per copy** when the copies are getting different changes.
- One coding or dataset shows and changes that copy only; the **Now**
  column still names another copy's value beside it when it differs.
- **Choose datasets...** (or **Choose codings...**, on a file with three or
  more copies) opens a list to tick several, but not all: the selector then
  shows them (*Datasets: Manual, Automatic 2*), and edits go to those only.

A value with no coding-variant copy is read by every coding: it shows the
same in both, and is written once. Hover over a parameter to see each
copy's value and address.

A switch the ECU reads from its EEPROM coding (its *Type* byte is 1) is set
to read this file's value instead; that byte is listed in the table too.

## Functions that edit tables or fault paths

Some functions are more than a switch. Under the parameters, the table lists
every table and fault path the function works through, with what it holds
now -- all of the time. Once a setting is picked, those it changes turn blue
with their new values; the rest stay grey and say *(unchanged)*.

The colours are the same for every row: **blue** is what **Apply** will
change -- a parameter, an EEPROM *Type* byte, a table or a fault path, with
its picker in the **New** column -- **grey** is a table or fault path the
function uses that stays as it is, and **bold** marks a change you made
yourself. A parameter that stays as it is keeps the normal text colour.
Text under the table (descriptions, part numbers) can be selected and
copied. Hover over a table to see
its cells as a grid, a changed cell as *old → new*. A table that is enlarged
(the smoke limiter's placeholder boost map, the EGR coolant correction) is listed with its new size, and
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
| **EGR** | **On (as calibrated)** puts the EGR tables back as the file was opened, and the EGR fault paths on. **Cut when the engine is warm** also fills the warmest coolant line of the EGR coolant correction, so EGR works while the engine warms up and stops above **Cut above:**. When the coolant axis does not reach that temperature, **Keep the OEM breakpoints** (ticked by default) grows the table where it is, adding breakpoints after the warmest OEM one so every OEM breakpoint and value is kept; without it, or when the table cannot grow, the two warmest breakpoints are raised to the set temperature instead (never when another table shares the axis). See [Cut when warm: the coolant axis](#cut-when-warm-the-coolant-axis) below. **Off** holds the EGR valve shut through the tables the module names (they are listed under the parameters, with what each one gets) and switches the EGR fault paths off; the EGR maps themselves are not touched. |
| **Diesel particulate filter (DPF)** | Read from the switches that make the ECU run the filter -- the module names them, and they are the function's parameters, each editable in the **New** column. **Removed** switches them off and the filter's fault paths off: the calibration of a car without a filter, the way the same software is set up for one. **Fitted** switches them back on with the values stock filter files hold, and puts each fault path back as it was when the file was opened (or as the comparison has it). |
| **Boost pressure (MAP) sensor** (EDC16, EDC15) | Sets the ECU's boost-sensor curve to the Bosch sensor fitted; see [Boost pressure (MAP) sensor](#boost-pressure-map-sensor) below. |
| **Gearbox type** (EDC16) | **Manual** (0), **Automatic (torque converter / Tiptronic)** (1), **DSG (dual-clutch)** (1) and **Multitronic (CVT)** (3). A torque-converter automatic and a DSG are the same value: stock files of both hold 1 (e.g. 038906016N coding 150034, Tiptronic; 038906016AB coding 150038 and 03G906016AC, DSG), so a file holding 1 shows as *Automatic ... or DSG*. Multitronic files (03G906016LQ, GC, MF) hold 3. The A2L also names 2 (*automatisch shift*) and 4 (automated manual, "Z-car only"); no stock file uses either, so they are offered only as values in the **New** column, not as settings. |
| **Electric heater outputs** (EDC16, EDC15) | The ECU outputs switching the electric heating elements that warm the cabin heater while the engine is cold: on EDC16 the heater switch for outputs 1 and 2 plus the number of connected elements; on EDC15 the number of glow-element outputs of the coolant heater. Files with coding variants (e.g. 1.9 TDI BEW) have a copy per coding: stock files have the outputs on for manual-gearbox codings and off for automatic ones (on EDC15, on in manual datasets, off in automatic ones). **On** sets three elements; enter the number fitted if fewer. |
| **Cruise control stalk source** (EDC15) | **Cruise control off** (0), **Stalk wired to the ECU** (3, what every stock file of the corpus holds) or **Over CAN (GRA message)** (2, needs a gateway that sends it), with the stalk's input pins switched to match. MSA15 / EDC15V software has no cruise control over CAN and offers only the first two. On files whose definition comes from the disassembly (038906012FD / GN, 038906012HA, the later PD software) the definition places the cruise control type where the code that reads it points. |
| **Road speed source** (EDC15) | 1: a speed sender wired to the ECU's road-speed input (normally on the gearbox; its pulse constant is in VSS pulse settings). 2: a tachograph's speed output. 3: the CAN message from the ABS / ESP unit. 4: the CAN message from the instrument cluster. 3 and 4 exist only on later (P+ / VGK) software. |
| **Auto to Manual Conversion** (EDC15 VE pump) | The automatic and manual datasets of a VE-pump file were set up for slightly different injection pump / nozzle builds, and a manual car runs better on the automatic setup. **Automatic map** copies the injection pump's quantity-adjuster voltage map, values and breakpoints, from the automatic dataset into each manual dataset whose map has the same size, as one undo step. Where the automatic datasets hold different maps, **Copy from:** picks the one to copy. **Manual map** is the manual dataset's own map, as the file was opened (choose it to put the map back). A file whose manual map already equals the automatic one shows **Automatic map**, with nothing to change. Unit-injector (PD) files have no such map and do not offer it. |
| **Injection pump: 10 mm → 11 mm plunger** (EDC15 VE pump) | For a VE pump rebuilt with an 11 mm plunger (pump head) in place of the stock 10 mm one. The fuel a VE pump delivers per stroke follows the plunger's area, so at the same quantity-adjuster position an 11 mm plunger delivers (11/10)² = 1.21 times as much. **11 mm plunger** multiplies the fuel-quantity axis of the injection pump's quantity-adjuster voltage map by 1.21 in every dataset (51 mg/stroke becomes 61.71), keeping the voltages, so the ECU drives the adjuster to the right position for the fuel it asks for. Requested fuel, the smoke and torque limiters and the rest are not changed: the engine gets the same fuel as before; raise the limiters yourself to use the extra capacity. **10 mm (stock)** puts the stock breakpoints back; applying 11 mm twice does not scale twice. Chosen together with the automatic pump voltage map above, the map is copied first and then scaled. After fitting the pump, set the injection timing and check the idle and full-load quantities on the car; new nozzles are a separate change. Unit-injector (PD) files do not offer it. |
| **Smoke limiter: air mass or boost pressure** | Switching to boost pressure fills the boost-pressure limiter map from this file's air-mass limiter, so the limit stays what it is today (a 2 x 2 placeholder map is enlarged first -- moved to free space when it cannot grow where it is -- with new engine-speed and boost-pressure axes), and fills a flat intake-temperature correction. A file that already limits by boost pressure keeps its own map. The list shows the air-mass limiter and the volumetric efficiency too: the fill reads them, it does not write them. |

On EDC15 files whose definition comes from the disassembly (038906012FD /
GN, 038906012HA, the later PD software) EGR works through the same switch:
the definition places it (and on PD software the air-mass gate and the
EGR-on duty) where the code that reads it points, and the EGR fault paths --
listed there by number (*F0D_X*) -- are recognised by their fault codes
(P1402, P1441, P3102-P3105, P0401 / P0402 or P3130 / P3131). Those
definitions do not place the EGR fault-reaction masks: **Off** leaves them
as they are and says so.

A fault path switched back on gets the class it had when the file was
opened. One that was already off then gets the class it has in the
comparison file (in a project, the file as first read); with no class to go
back to, it stays off -- except when a file opened without a filter is set
to **Fitted**: its paths then get the ECU's usual warning-lamp class.

## Boost pressure (MAP) sensor

The ECU turns the boost sensor's voltage into pressure through a two-point
curve (on EDC15 each dataset has its own). Every Bosch boost sensor these ECUs use is linear, so the two
points describe it fully. **Set to** offers these sensors by their range
(*2.5 bar*); pressures are **absolute** (boost is about 1 bar less). Under
the table, the window names the sensor's Bosch part numbers and the two
points written:

| Setting | Bosch curve | Range | Part numbers (examples) |
|---|---|---|---|
| 1 bar (10-115 kPa) | 1: 0.40 V at p1, 4.65 V at p2 | 100-1150 hPa | 0 261 230 052, 0 261 230 030, 0 261 230 022 |
| 1 bar (20-115 kPa) | 1 | 200-1150 hPa | 0 261 230 020, 0 261 230 013 |
| 2.5 bar | 1 | 200-2500 hPa | 0 281 002 487, 0 261 230 042, 0 281 002 399 |
| 3 bar | 1 | 200-3000 hPa | 0 281 002 437, 0 281 002 401 |
| 3.5 bar | 2: 0.50 V at p1, 4.50 V at p2 | 500-3500 hPa | 0 281 002 456, 0 281 002 244 |
| 3.8 bar | 2 | 150-3800 hPa | 1 267 030 835, 0 273 300 017, 0 261 230 036 |
| 4 bar | 2 | 500-4000 hPa | 0 281 002 316, 0 281 002 576, 0 281 006 060 |
| 6 bar | 2 | 500-6000 hPa | 0 281 002 420, 0 273 300 012 |

- **2.5 and 3 bar** are written as stock VW files hold them: 0.40 V =
  200 hPa and 4.83 V = 2600 hPa / 4.65 V = 3000 hPa. The others are written
  as the Bosch catalogue's end points.
- The window recognises the sensor from the **line** the curve holds (a
  line run on to another voltage is still that sensor), and says so when it
  matches none -- a tuner's own curve, or the MSA15 028906021CK's (0 V =
  100 hPa, 4.75 V = 2600 hPa), which is none of these sensors.
- **The 1 bar sensors** read up to 1.15 bar, so no boost: for a non-turbo
  engine, or a sensor before the turbo. With **6 bar**, check the boost maps
  and limits reach as high as you need. The catalogue lists no 1.5, 2 or
  5 bar absolute sensor; a "2 bar" boost sensor is the 3 bar absolute one.
- **Only the curve changes.** Bring boost setpoints and limits within the
  new sensor's range yourself; the window warns when the largest boost
  setpoint is above it.
- **Signal-range check:** its lower voltage limit (0.68 V on stock files)
  is left as it is. With a 3.5 or 4 bar
  sensor it stands for about 650 hPa, the ambient pressure at about 3600 m:
  higher up, an idling engine could set a sensor signal fault.
- **Check the part number printed on the sensor**: sensors that look the
  same can have different ranges.
- On EDC15 files whose definition does not name the curve (038906012FD /
  GN, 038906012HA, the later PD software), the window finds it by what it
  holds -- a two-point curve at the sensor's two ends lying on one of the
  lines above -- and says in the list that it was found by its values.
  Its axis is shown in ADC counts (4.89 mV each).

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

## Cut when warm: the coolant axis

**EGR > Cut when the engine is warm** fills the warmest coolant line of the
EGR coolant-temperature correction. On EDC15 a dataset can hold three of
them -- the EGR setpoint correction (in mg of air) and two corrections of
the EGR duty (in %) -- and a file may calibrate more than one, so every one
the dataset holds is cut (a duty correction at 100 %).
Many stock maps end well below the usual cut
temperature (the last coolant breakpoint is often about 51 °C), so a line at
**Cut above:** has to be made:

- **Keep the OEM breakpoints** ticked (the default): the table is resized
  where it is, by the app's own resize (as **Resize axes...** does), and
  breakpoints are added after the warmest OEM one -- one a degree below the
  set temperature, holding the warmest OEM line, and one at it, filled with
  the cut. A map that ended at 51 °C then ends at *51, 79, 80 °C*: EGR runs
  as calibrated up to 79 °C and is cut within that last degree.
  Sensor-fault breakpoints above (EDC15, about 148-150 °C) stay last. The
  list shows the table with its new size and its axis with the breakpoints
  added.
- The box is greyed when the table cannot grow where it is -- its record
  has no room before the next object (beyond the space its definition
  reserves), it shares its axes with other maps, or its axis already reaches
  the temperature -- and its tooltip says which. Then, or with the box
  clear, the two warmest breakpoints are raised instead (the second to a
  degree below the set temperature, so the cut is as sudden), and those two
  OEM breakpoints are overwritten.
- Some EDC15 maps already end in spare breakpoints far above any coolant
  temperature (about 148, 149 and 150 °C) whose lines hold the same as the
  warmest real one. Those are used instead: the first becomes the breakpoint
  a degree below the set temperature, the second the set temperature, and
  they and the last are filled with the cut. A map whose real breakpoints
  end at 18 °C then reads *18, 79, 80, 150 °C*, with nothing before 79 °C
  changed and no resizing needed.
- A map with only two coolant breakpoints keeps its cold one: only its
  warmest is raised.
- Either way the axis keeps rising as the ECU stores it: no two breakpoints
  share a stored value, and the second-warmest is at least one stored step
  below the warmest.

It is one undo step with everything else **Apply** writes. Only the copy
being written (the dataset) is resized; a coding-variant copy is resized
only together with its main copy, in **Resize axes...**.
