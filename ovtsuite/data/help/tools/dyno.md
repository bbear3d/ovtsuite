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
  window: the reference, other runs to compare (another tab, a branch or
  version of any open project, a file, a pinned curve, a data log), the
  conditions, every engine speed, what changed, road speed, and export.

It is a calculation, not a measurement. The engine makes these figures only if
the turbo reaches its target boost and the injectors and fuel system deliver
the fuel; nothing here knows about a worn turbo or a clogged filter.

![The dyno chart of an EDC16 file: power and torque by engine speed, the limiter governing each band, every engine speed in the table and the run's conditions](../images/tools-dyno.png)

## Reading the chart

| What | Meaning |
|---|---|
| Solid line, left axis | Power |
| Dashed line, right axis | Torque |
| Colour | The run: this file blue, the reference orange, each run added its own colour (named in the legend above the chart). A run keeps its colour when another is removed |
| Points | A data log: filled for power, rings for torque |
| Dots with figures | This file's peak power and peak torque |
| Coloured strip under the curves | Which limit governs at each engine speed in this file. Hover for its name; **click a band to open that map** |

Hover over the chart for every run's figures at that engine speed.

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

On an EDC15 **unit-injector (PD)** file the torque is also held to the brake
thermal efficiency the stock 130 to 160 PS calibrations of that engine reach
at each engine speed (about 43 % at 2000 to 2500 rpm, falling to 35.8 % at 4000 rpm).
The 100, 105 and 115 PS calibrations carry a `mrwKFVB_KF` that gives more
torque per mg than that from 3000 rpm up -- 11 % more at 4000 rpm, so a stock
115 PS file read about 132 hp -- although their rating and fuel quantity agree
with the others'. A file under the ceiling (the 130 PS and stronger ones) is
not changed; one above it is held to it, and the chart's notes say how far
and where. A tuned file still gains in proportion to its fuel.

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

Step 1 assumes the same injectors. A run that **Fitted hardware** records
as set up for other unit injectors or nozzles than the reference (see
[Injector setup](injector-setup-pd.md)) -- or, with no record, whose duration
maps are exactly another injector preset's than the reference's -- is not
read through the reference's duration maps: its own maps are right for its
own injectors, so its quantity is taken as it asks for it, and the notes
under the chart say so. A close match of the maps is not enough for that
(see [Fitted hardware](#fitted-hardware)).

Choose the reference in **Reference** on the panel or **Compare with** in the
window:

| Choice | The reference is |
|---|---|
| **Original** (default) | The file as first opened -- in a project, the set as read |
| **Comparison** | The tab's comparison file |
| **Version: ...** (window) | Any saved version in the history |
| A run you added | Another tab, a project's branch or version, a file, a pinned curve (see below) |
| **None** | This file's own maps |

Every run on the chart is read through the same reference, at the same
conditions, so the runs compare like for like. A long version or file name
is shortened in the lists (in full in the tooltip); it never widens the
window or the panel.

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
- **Engine** -- displacement and cylinders, filled in from the file; change
  either to override it (back to the file's figure, it follows the file
  again). EDC16 files store both: the displacement is the cubic capacity sent
  to the instrument cluster (`FrmMng_volCubCap_C`, 2.0 l on 03G906016LQ),
  else the air model's `ASMod_volEng_C` -- which on some 2.0 files is a 1.90
  carried over from the 1.9, so the note under the fields names both when
  they differ. EDC15 stores the cylinder count, and the displacement comes
  from the definition's engine. If either is unknown the chart says so and
  waits for you to enter it.
- The table lists every engine speed: power, torque, quantity, the quantity in
  the reference's terms, the limit that governs, boost and air. Double-click a
  **Limited by** cell to open that map.
- **Export CSV...** writes every run, one row per engine speed (and the road
  speed, when the chart is drawn across it), after `#` lines giving the file,
  the runs, the reference and the conditions. **Save image...** and **Copy
  image** give the chart with a caption under it: the file, each run's peaks,
  and the conditions -- so it reads on its own.

### Other runs

**Add** puts another curve on the chart, up to six:

| Add | The run |
|---|---|
| **This file > Original / Version** | This file at a saved version |
| **Open tab** | Another tab's file as it is now -- it follows that tab's edits |
| **Open project** | Any set of any open project: a branch's newest version, or any version of it |
| **File...** | A binary on disk |
| **Data log** | A log kept in an open project, or a log file (see below) |

**Pin this curve** keeps this file's curve as it is now, so you can edit and
watch the live curve move against it; pins last until the tab closes.
Untick a run to hide it. Right-click a run to make it the reference, compare
this file with it in **What changed**, rename a pin, or remove it.

A run from another file is read with that file's own definition (the open
tab's, or the file loaded afresh) -- never this file's, even when the two are
the same size.

### Data logs

A log's injected quantity at full load (pedal at 90 % or more; every sample
when the log has no pedal position) is gathered into 100 rpm bins and read
through the same conversion as the calculated curves. It is drawn as points:
what the ECU delivered on the road, beside what the calibration asks for. A
log needs the engine speed and the injected quantity; the measuring blocks of
[Data logging](../logging.md) carry both.

### What changed

The **What changed** tab compares this file with the reference or any other
run. The engine-speed range is cut where the governing limit of either run
changes; for each band it gives the limit in each, how far power, torque and
quantity moved, and which of the maps behind those limits differ between the
two files (how many cells, by how much, whether the breakpoints moved).
Double-click a band to open the first of those maps.

### Presets

**Preset** sets the conditions in one go: the standard (sea level, warm), a
cold or hot day, altitude, at the wheels or at the crank. **Save...** keeps
the conditions as a preset of your own; **Delete** removes one of yours.

### Road speed

**Draw across: Road speed** draws the runs against road speed in a gear
instead of engine speed. It needs the car's **gearbox** and **tyre**:

- **Gearbox** -- choose one from the list of common VAG gearboxes (hover
  over one for its gear ratios, final drive and where the figures come from),
  or make your own with **New...**: its gear ratios, final drive(s) and, for
  a twin-shaft box such as a DSG, which gears use which final drive.
  **Edit...** changes or deletes one of yours. Gearbox codes cover many
  variants; check the ratios against your gearbox's code letters.
- **Tyre** -- the size (`225/45 R17`) or the rolling circumference in metres
  (`1.93`). A size is turned into a rolling circumference 3 % under the
  tyre's unloaded one.

Road speed per 1000 rpm = 60 x rolling circumference / (gear ratio x final
drive). Enter your own **km/h per 1000 rpm** for a gear to override it (0
goes back to the gearbox's), and show it in km/h or mph.

EDC16 files also keep the ECU's own gear model (overall ratios and the
gear-recognition bands), offered as **The ECU's gear model**. It is what the
ECU uses to recognise the gear and is often not the car's gearbox (stock
03G906016LQ's main block is labelled for a CVT and gives 95 km/h at
2000 rpm in 6th), so choose the gearbox
where you know it. EDC15 keeps no gear ratios at all, only the thresholds
between gears, so there the gearbox must be chosen.

### In a project

In a project tab the conditions, the engine, the runs you added (except pins
and other tabs, which do not last), the reference and the road-speed settings
belong to the set and are kept in the project -- a change to them is a change
to the project, saved with it. A plain file's conditions are the app's.

### Fitted hardware

The dyno works out what each run was made on: from the **fitted hardware**
a project records for it (unit injectors, or a VE pump head and nozzles --
see [Injector setup](injector-setup-pd.md)), from the project the run comes
from:

| Run | Its hardware |
|---|---|
| This file | What its branch is set up for, with the tab's unsaved edits |
| The original, a version of this file, a version of this set | What that version is set up for |
| Another tab of a project (this one or another) | What that tab's branch is set up for in its own project, with its unsaved edits |
| A branch or version of another project | What that version is set up for in that project (an open project as it is now, saved or not; one not open, as its file says) |
| A file on disk that is a project (.ovt) | What its set's branch head is set up for, as the file says (it is read, not opened or locked) |
| A pinned run | What this file had when it was pinned (a record made later does not change it) |
| A data log kept in a project | What the car had when it was **recorded** (the log's own date; when the log has none, the day it was added to the project) |
| A log file not kept in a project | When the log carries the ECU's ident (a VCDS-layout log) and one open project set has that part number: what that car had on the log's own date; otherwise not known |

A run no project records hardware for -- a plain file, a tab that is not
in a project, a set with no hardware recorded -- is identified **from its
maps**: a unit-injector file's duration maps against the injector presets,
an EDC15 VE file's pump voltage map against the factory maps. It is shown
as, for example, "looks set up for PD150 injectors (from the maps)", and
"not an exact match" is added when the maps are only close to the preset
(within 1 % on average) or are a factory map with the pump head changed.
Hardware from the maps is a guess at what the calibration is for, not a
record of what is on the car:

* It counts in the "Not the same hardware" note, where it is always marked
  "(from the maps)". Against a record it is compared by what both say (the
  preset; the pump head and the nozzle); a run with nothing recorded ("as
  it came") is not told apart from it.
* It stops the quantity being carried across the reference's duration maps
  (step 1 above) only when the match is **exact** -- the maps are the
  preset's own. A close match leaves the duration basis as it is.
* It is worked out once per file content: the chart does not identify a
  file again until its bytes change.

A file whose datasets are set up for different hardware is identified by
the dataset the chart shows. Logs have no maps, so a log no project records
is not marked.

Hover over a run in the list, or its name in the chart's legend, to see its
hardware ("as it came" when no change is recorded for it). When the runs
shown were made on different hardware, the notes under the chart say which
parts differ and what each run had of them; a change recorded in Injector
setup (in this project, another tab's or another open project's) redraws
the chart.

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
