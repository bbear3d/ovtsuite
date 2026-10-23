---
title: Feature switches
order: 37
context: FeatureSwitchDialog
---
# Feature switches

**Tools > Feature switches...** turns functions of an EDC15 or EDC16 engine
ECU on and off — cruise control, A/C type, EGR, the particulate filter, the
smoke limiter's air-mass or boost path and more. A function is usually
several calibration values that must change together; the window changes
them as one bundle, as **one undo step**, and shows every value before it
writes anything.

The window lists only the functions this file carries: a function whose
parameters the file's definition does not place is left out, and a file of
another platform (a gearbox, a cluster) does not offer the window at all.

## Change a function

1. Open the file and choose **Tools > Feature switches...**.
2. Pick the function on the left. The right side says what it does, which
   state the file is in now (**Current**), and any cautions.
3. Choose the state in **Set to:**. The table lists each parameter with its
   current and new value (a changed value is red) and where its meaning comes
   from (**Source**: the ECU's code, the A2L text, stock files, or derived from
   this file).
4. **Copies to write** appears when the value exists more than once — per
   dataset on EDC15 (Manual / Automatic), per coding copy on EDC16. Untick a
   copy to leave it as it is.
5. **Apply**. A summary lists every change; confirm, and it is written as one
   undo step (**Edit > Undo** takes all of it back). In a project, **Save as
   project version** (on by default) keeps a version named after the change.

Checksums are corrected when the file is saved, as for any other edit.

A switch that the ECU can also read from its EEPROM is set to read the flash
value, so the change takes effect whatever the EEPROM holds.

## Functions that edit tables

Some functions are not a single switch. The line under the table says what
the extra edit will do before you apply it.

| Function | What **Apply** also does |
|---|---|
| **EGR** | EDC16: zeroes the EGR demand maps (0 % requested). EDC15: the EGR master switch plus the air-mass thresholds that hold the valve shut, and masks the EGR fault reactions. Both: switches the EGR fault paths off in the same step (on EDC15 the AR1, AR2 and ARS paths). Turning EDC16 EGR back on restores the fault paths; the demand maps come back with **Undo** or a project version. |
| **Diesel particulate filter** | **deleted** switches the DPF fault paths off with the filter switch; **fitted** switches back on the ones that were on when the file was opened. |
| **Dynamic EGR** | Fills the warm coolant line of the EGR coolant correction, so EGR is cut once the engine is warm. **Warm point** sets the coolant temperature; when the map's axis does not reach it, its top breakpoint is raised to it (never when another table shares that axis). |
| **Smoke limiter — MAF vs MAP** | Switching to MAP fills the boost-pressure limiter from this file's own air-mass limiter, so the limit stays what it is today, referenced to boost. A 2 × 2 placeholder map is resized (or moved) to a usable size first; a flat intake-temperature correction is filled too. The line under the table says how closely the result matches a map the file already has. |

Fault paths switched back on return to the class they had when the file was
opened; a path that was already off stays off.

## Tips

- Informational entries (gearbox type, boost-control mode) are greyed: they
  show the values but write nothing.
- The window follows edits made elsewhere — **Undo**, the grid or another
  tool — while it is open.
- Turn fault codes on or off one by one in the
  [DTC / Fault-Path editor](dtc.md).
