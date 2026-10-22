---
title: Dyno chart
order: 37
context: DynoPanel, DynoDialog
---
# Dyno chart

The dyno chart draws the power and torque the open calibration **asks for at
full load**, rpm by rpm, beside a reference -- by default the original of the
same file. It works for Bosch EDC15 and EDC16 files and needs that module.

- **Dyno** on the edit bar (Ctrl+4) shows it in the tab, above the table. It
  follows every edit and undo.
- **Details...** on the panel, or **Tools > Dyno chart...**, opens the full
  window: the reference, the conditions, every engine speed, and export.

It is a calculation, not a measurement. The engine makes these figures only if
the turbo reaches its target boost and the injectors and fuel system deliver
the fuel; nothing here knows about a worn turbo or a clogged filter.

## Reading the chart

| What | Meaning |
|---|---|
| Orange line, left axis | Power |
| Blue line, right axis | Torque |
| Solid | This file |
| Dashed | The reference |
| Dots | This file's peak power and peak torque |
| Coloured strip under the curves | Which limit governs at each engine speed. Hover for its name; **click a band to open that map** |

The line above the chart gives the peaks and the gain over the reference.
Power can be shown in **kW** (default), **PS** or **hp**; torque in **Nm**
(default), **lb·ft** or **kgf·m**. The choice is remembered.

## How it is worked out

At each engine speed the ECU fuels to the **lowest** of its full-load limits:

| | EDC16 | EDC15 |
|---|---|---|
| Driver's wish at 100 % pedal | `AccPed_trqEng0..6` (Nm) -- the maps the gear selector `AccPed_stGearSel_CUR` picks | `mrwFVH_KF` (mg) |
| Torque limiters | `EngPrt_trqLimP_MAP` at the air pressure, `EngPrt_trqNLim_CUR`, the gearbox's per-gear limits | `mrwBDB_KF` at the air pressure (MSA15: the curve `mrwBDB_KL`) |
| Torque to fuel | `FMTC_trq2qBas_MAP` -- read clamped at its last torque breakpoint, as the ECU does | (none: the limits are already in mg) |
| Smoke limiter -- **one**, as the switches select | by quantity or lambda (`FlMng_swtqLimSmkQ_C`); by air mass or boost (`FlMng_swtSmokeVariant_C`) | by air mass or boost (`cowBEG_BOO`); one map or the coolant set `mrwBRA0..nKR` (`cowRauchKR`) |
| Torque from fuel | `FMTC_trq2qBas_MAP` inverted, less the friction map `EngM_trqFrc_MAP` | (q − friction quantity `mrwREI_KF`) / specific consumption `mrwKFVB_KF` |

The air the smoke limiters read is worked out from the **target boost** over
the engine's displacement, and because the boost target itself depends on the
quantity, the two are iterated until they agree.

On EDC16 the torque in that chain is the ECU's *inner* torque. The chart shows
torque at the crank: inner torque less the ECU's own friction map
(`EngM_trqFrc_MAP`). Stock 03G906016LQ (BPW, rated 103 kW / 320 Nm) comes out
at 103.5 kW and 317 Nm.

On EDC15 the same is done in quantity: torque = (q − `mrwREI_KF`) /
`mrwKFVB_KF`, the ECU's own model; stock 038906019GQ (ASZ, rated 96 kW /
310 Nm) comes out at 94.4 kW and 313 Nm. A definition without those two maps
falls back to fuel energy at the **efficiency** set in the window (default
40 %), which reads about 12 % high.

Checked against 79 stock files of known rating: with the ECU's own torque
model the calculated peak power is within 5 % of the rating on 26 of 41 and
within 10 % on 31.

### The reference

A tune often changes what a milligram *means*: raising the torque-to-fuel map,
or rescaling the injection-duration maps. Read through its own maps such a
file can look no stronger than stock. So this file's fuelling is read through
the **reference's** maps:

1. On a unit-injector (PD) engine, this file's start of injection and duration
   are worked out (the same tables the [End of injection](end-of-injection.md)
   view uses), and the quantity the reference would need for that same
   duration at that same start is looked up in the reference's duration maps.
2. That quantity becomes torque through the reference's torque-to-fuel map
   and friction (EDC16), or fuel energy (EDC15).

Choose the reference in **Reference** on the panel or **Compare with** in the
window:

| Choice | The reference is |
|---|---|
| **Original** (default) | The file as first opened -- in a project, the set as read |
| **Comparison** | The tab's comparison file |
| **Version: ...** (window) | Any saved version in the history |
| **None** | This file's own maps |

Past the reference's last breakpoint the value is extended in proportion; the
table marks those rows with \*. Untick **Carry the quantity through the
duration maps** to take the ECU's quantity as it is.

## The window

- **Dataset** (EDC15) -- the coding picks which dataset the ECU runs.
- **Driver's wish** (EDC16) -- one map per gear; the default is the highest of
  them.
- **Smoke limit** -- the ECU reads one smoke limiter, chosen by the file's
  switches; that one is selected and marked "(the file's switch)". Pick
  another to see what it would allow. If the definition lacks the switch, the
  air-mass limiter is assumed and the notes say so.
- **Conditions** -- air pressure, charge and coolant temperature, volumetric
  efficiency, the **boost the turbo makes** (0 = it reaches the target; set it
  to see what a smaller turbo leaves), efficiency (EDC15), and **drivetrain
  loss** (0 = at the crank; more for wheel figures).
- **Overboost** (EDC16, only where the file calibrates an overboost torque
  limit, e.g. the Touareg R50 V10) -- applies `EngPrt_trqLimPBoost_MAP` in
  full. The ECU allows it only for a limited time (`EngPrt_tiBoostMax_C`), so
  it is off by default and the chart shows the steady limit.
- **Engine** -- displacement and cylinders. EDC16 files store both (the
  displacement as `ASMod_volEng_C`, or the cubic capacity sent to the
  instrument cluster); EDC15 stores the cylinder count, and the displacement
  comes from the definition's engine. If either is unknown the chart says so
  and waits for you to enter it.
- The table lists every engine speed: power, torque, quantity, the quantity in
  the reference's terms, the limit that governs, boost and air. Double-click a
  **Limited by** cell to open that map.
- **Export CSV...**, **Save image...** and **Copy image** export the curves.

`mrwBDB2_KL` is not used: the ECU reads it only in the ECO mode of the
automated gearbox (ASG), and the full-load limit is 100 mg otherwise.

On the EDC15 definitions worked out from disassembly (late PD, the 1.9 TDI
VE ECUs 038906012FD/GN/HA) and on PD files of another build than a
definition's own, the smoke switches, smoke maps and torque model are found
through the code that reads them, so switching the smoke limit to boost and
resizing the boost map is followed there as on the DAMOS-defined files.

## When it is not offered, or stops short

- A file with no definition of its own (EDC15 "structure" family): its maps
  are found by shape and their scaling is borrowed, so no figures are given.
- A map the definition places only by projection (marked PROJECTED in the
  table) is not used. If the chart needs it, it says which one instead of
  drawing a curve; if not, it lists it in the notes.

## Not included

Transient smoke, hot-fuel and overheating cuts, exhaust regeneration, and the
fault and start-up paths. The figures are those of a warm engine running
steadily at full load.
