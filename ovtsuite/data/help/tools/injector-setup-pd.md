---
title: Injector setup (unit injectors)
order: 36
context: InjectorDialog, FlowChart, InjectorPresetDialog
---
# Injector setup (unit injectors)

**Tools > Injector setup...** fits other injectors or nozzles to a
unit-injector (PD) engine with a Bosch EDC15P or EDC16 ECU. It needs the
Bosch EDC15 or EDC16 module. On a distributor-pump (VP37) file the same menu
item opens the EDC15 module's pump tool instead.

Injector setup does **not** add power. It makes the fuel the ECU asks for
the fuel the new hardware delivers, so the engine runs as before. Raising the
limiters to use the extra capacity is ordinary tuning, done afterwards.

## Why the injector matters to the calibration

A PD ECU has no "injector size" setting. The camshaft pushes each injector's
plunger; the ECU starts delivery by closing the injector's solenoid valve and
ends it by opening it. So it turns the quantity it wants into **crank degrees
of delivery** with the duration maps. There are several of them, and the
start of injection picks which, because the cam pushes the plunger at a
different speed depending on where delivery starts. Those maps, with the
curve that picks them, describe one hardware set: the cam lobe, the injector
body and the nozzle together.

The injector body's solenoid has its own calibration as well: when the valve
is expected to close (BIP detection) and how it is driven. That changes with
the injector body, not with the nozzle.

## The window

![Injector setup on a BPW file, PD150 injectors chosen: fuel per crank degree before and after, the tables it will write, and what it found](../images/tools-injector-setup.png)

**This file** shows the hardware the calibration is set up for, and in a
project the injectors recorded as fitted (**Fitted**): the closest
preset and how far apart they are. "The same calibration" means every point
agrees within 0.05°. "The same hardware" means within 1 % on average: the
same injectors, with the calibration changed in places. "Closest" means the
file is modified, or carries hardware there is no preset for. A **Not stock**
line lists signs in the file's own structure that it has been modified, such
as the lambda probe switched off with every one of its fault paths silenced.

With a comparison file open beside this one (the tab's comparison), a
**Comparison** row says what it is set up for, the same way; untick **Show
comparison** to hide it, with the comparison column of the table list and the
comparison's curves in the chart (below).

An EDC15 file can carry several datasets (for example *Automatic*,
*Manual 1* and *Manual 2*), each with its own duration maps. Each one is
identified on its own; datasets with the same answer are listed together
("All three datasets: ..."). When they are not set up for the same hardware
the window says so, in orange, with what each one is set up for: fit them one
at a time (tick only the datasets to change), or check which dataset the car
runs before fitting.

**New hardware** has two tabs: **Preset** fits a known hardware set,
**From logs** corrects the duration maps from logs (below).

- **Fitting**: a factory injector set, an aftermarket nozzle or one of your
  own presets. The line under it says what the preset is based on and how
  sure it is:
  - *exact (from a stock file)*: the duration maps of a factory file;
  - *starting point*: an aftermarket nozzle, worked out from its factory base
    nozzle with the extra flow its maker states. Refine it from logs.
  - *estimated*: a nozzle with no flow figure, assumed to deliver what its
    factory base does, so the base's maps are written as they are. The
    **Wuzetem 1043** is one: a reproduction of the stock DSLA150P1043, not
    measured. Check it from logs after fitting.
- **What changes**: *Complete injectors* also writes the new bodies' solenoid
  calibration; *Nozzles only* keeps this file's own. Choosing an aftermarket
  nozzle switches to *Nozzles only*; going back to a factory set or one of
  your own switches back to *Complete injectors*, unless you changed it by
  hand.
- **Start of injection**:
  - *Keep the centre of injection* (recommended) moves the start by half the
    change in duration, so the middle of the injection stays put. Bosch's own
    recalibrations for bigger injectors on the same engine follow this most
    closely.
  - *Keep the end of injection* moves the start by the whole change.
  - *Keep the start of injection* leaves it alone; injection then ends
    earlier with bigger injectors.
- **Also the start (cranking) maps**: the start-of-injection maps the ECU
  uses while the engine cranks (engine speed × coolant) are moved by the same
  rule. Starting reads the same duration maps, so the duration there changes
  with the new hardware; the start is moved for the quantity the ECU starts
  with: the ECU's own start-quantity map where the definition has one (EDC15
  `mrwSTMGRKF`), else the start quantity End of injection uses (40 mg unless
  set). Where that map gives no fuel, the cell is left as it is. Untick it to
  leave them as they are. Bosch's own start maps for other injectors differ from
  both by a degree or two, so check that the engine starts well after the
  change. It applies with *Keep the centre* or *Keep the end of injection*.
- **Use the preset's map selection curve**: when this file has as many
  duration maps and selector points as the preset, the preset's curve is
  written too and the maps match it exactly.
- **Also the coding-variant copies** (EDC16 files with a second copy of the
  tables): the same change in every copy.
- On an EDC15 file with several datasets, tick the datasets to change. Each
  one is planned with its own maps, and the table list names the dataset in
  front of each table.

**Preview**: fuel per crank degree before (dashed) and after (solid) at 20, 30
and 45 mg, and the comparison file's (dotted) when shown; every table that
will be written (double-click one to open it in the file's tab, with the cells
this changes marked, and bring the main window forward; the line under the
list says if it could not); and what was found. With a comparison, the
**Comparison** column compares each table as it will be written with the
comparison's table of the same name: *the same*, or how many cells differ and
by how much (orange), or that the comparison does not have it. The list
selects whole rows, as the other editing windows do. Drag the bar between the
settings and the preview to share out the height; on a short screen the
settings scroll. The window opens at the size you left it. A warning that the full-load quantity runs past the
duration maps' last quantity breakpoint means no more fuel is delivered above
it: extend the axis with **Resize axes** if the new injectors are meant to
give more.

- **Record in the project as fitted** (in a project): Apply also records
  that the car has this hardware and that the versions saved from now on are
  set up for it (File > Project information..., **Fitted hardware**). The
  dyno chart then does not read a version set up for other injectors through
  the reference's duration maps. Undo takes the record back with the change,
  one Apply at a time: undoing a second Apply takes back only its own record,
  and undoing the first one too takes back the first record. When an Apply
  completed a record made with **Record as fitted...**, undo puts that record
  back as it was instead of deleting it. Redo puts the records back.
- **Record as fitted...** (in a project) records the chosen preset and the
  day it went in without changing the file: when the injectors were fitted
  before the file was set up for them, or the file already is (then this
  version is recorded as set up for them; with several datasets, only when
  every dataset ticked is). A later Apply of the same preset completes that
  record instead of adding another.

**Apply** writes everything as one undo step and keeps the window open;
**OK** does the same and closes, or only closes when Apply has already written
it and nothing has changed since; **Cancel** closes without writing more (what
Apply wrote stays: Edit > Undo takes it back). Applying again finds nothing
left to write: a value is compared as the file stores it, so rounding is not
a change. **Help** (or F1) opens this page. **End of injection...** opens the
End of injection view to check the result.

## Your presets

**Save as preset...** keeps the hardware this file is set up for, to fit it
to other files: its duration maps and, for complete injectors, the solenoid
calibration. Give it a name and, if you like, the camshaft, injector body,
nozzle and notes; with several datasets, pick the dataset whose calibration it
keeps. It is listed under **Your presets** and chosen straight away. If you
already have a preset with that name, you are asked before it is replaced.

Beside **Fitting**, for your own presets only (factory and aftermarket
presets cannot be changed):

- **Edit...** changes the name, parts and notes. Tick **Take the calibration
  from this file again** to replace its maps with what the open file is set up
  for now.
- **Rename...** changes the name only.
- **Delete** removes it, after asking. This cannot be undone.

Renaming or editing keeps the preset's identity, so hardware recorded in a
project with it still refers to it. Two of your presets cannot have the same
name.

## Aftermarket nozzles

An aftermarket preset starts from the factory nozzle it is based on (the
DSLA150P1043 of the ASZ / ARL) and assumes the extra flow the maker states.
Where that figure is extra hole area, it is an upper limit: a pump-driven
injector never passes more than the extra area allows. So the engine is
asked for a little less fuel than it gets, which is the safe side. Log the
car afterwards and correct the maps from the logs.

## From logs

The ECU never measures the fuel, so the **From logs** tab works out what the
hardware really delivers from data logs, and corrects the duration maps so it
delivers what the ECU asks for. Use it after fitting an aftermarket preset,
or for nozzles there is no preset for (keep the file as it is and start from
there).

1. Fit the nearest preset (or none), write the file and drive.
2. Log with the Data logging preset **Injector delivery**. The line at the
   top of the tab names it and the measuring blocks it uses on this file.
3. Add the logs under **With the new hardware**. Logs from the project come
   first in the Add menu, each marked with the injectors the project records
   for when it was added; with an injector change recorded, **Every log added
   since the change** adds them all at once (and, under **With the old
   hardware**, **Every log added before the change**).
4. Check the preview, then **Apply**. The logs are taken off the list once
   applied, because they were taken with the maps just corrected.
5. Drive and log again, and repeat until nothing is left to correct.

Two things measure the fuel delivered:

- **The lambda probe** (EDC16). Measuring block 40 carries the ECU's own
  lambda reading: with the air mass, it gives the fuel actually burnt. Warm,
  steady driving at mid and full load is what it reads well (lambda 1.05 to
  2.5). No logs from before the change are needed.
- **The same work, before and after** (EDC15 and EDC16). At the same steady
  point (idle in neutral, or the same road speed in the same gear) the engine
  needs the same fuel whatever injectors are fitted. Add logs of those points
  taken before the change, with the old injectors and their own calibration,
  under **With the old hardware**. Where the ECU now asks for less to do the
  same work, the new hardware delivers that much more. This covers idle and
  part load, where the lambda probe reads poorly.

Only warm (coolant 70 °C or more), steady rows are used. The result is shown
at every breakpoint of the duration maps it has enough data for; the others
stay as they are. One pass changes a breakpoint by at most 15 %: when the
preview says a correction was held, apply, log again and repeat. The start of
injection follows the setting at the bottom of the tab, as for a preset.

On an EDC15 file with several datasets, a log that records the ECU's ident
(a VCDS-layout log) is used only for the ticked datasets it names (automatic,
or both manual ones, which share it), and a log without one for every dataset
ticked; the log's tooltip says which, and the assistant does the same.

## Not covered

Camshaft changes, piezo unit injectors, and 16-valve aftermarket nozzles.
The start (cranking) quantity itself is not changed: the engine is asked for
the same fuel while starting, and the new duration maps deliver it.
