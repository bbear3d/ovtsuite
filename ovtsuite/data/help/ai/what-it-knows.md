---
title: What the AI knows
order: 30
---
# What the AI knows

The AI works through the same knowledge the app has: the definitions that
name each parameter, the modules that read each control unit, and these help
pages. It does not guess addresses or read raw bytes, so its answers are only
as complete as the app's support for the file.

## For every file

Whatever the control unit, the AI can:

- **identify** the file: platform, software, how fully the app supports it,
  the checksum state, and what **File information** shows (part number,
  software and dataset, blocks, container). This works on every platform,
  including those with checksum-only support;
- **list and search parameters** by name, description, category or units,
  the same search as the **Find** panel, and read any parameter in full:
  description, values in real units, axes, allowed range, and how securely
  its address was established;
- **compare** with the tab's comparison — for a project, the original as
  read — parameter by parameter, and read the **version history**;
- **check the checksums** (they are corrected when you save, never by the
  AI);
- **read data logs** kept in a project, through a summary first and then
  short windows around the moments it wants to look at;
- **search and read these help pages**, so it can answer *how do I ...?*;
- in the app, **show you** a table with the cells it means, a category of the
  parameter tree, or the tree filtered to what it found.

### What it is never given

- **VIN, immobiliser ID or number, SKC and login values** are withheld, on
  every platform: the AI is told they exist but not what they hold, and it
  cannot propose a change to them. **File information** and the project's
  vehicle details reach it without them.
- With an **online** service, anything that still looks like a VIN or an
  immobiliser value in a question or an answer is replaced with a
  placeholder before sending (see [Privacy](settings.md#privacy)).
- A parameter of more than 4096 cells is not sent in one piece.

## What each module adds

Each installed module also gives the AI what its own tool windows show:

| Module | The AI can also |
|---|---|
| Bosch EDC16 | list the fault codes and propose switching them off or back on; read the measuring blocks as a scan tool numbers them, the output tests, the codings stored in the flash and what each selects, the coding-variant copies of parameters (automatic / manual, A/C), the speed-sensor (VSS) scaling, and the gear-ratio, gear-recognition and cruise set; run **Tune from logs** on your data logs and turn its suggestions into a plan |
| Bosch EDC15 | list the fault codes and propose switching them off or back on; read the output tests |
| ZF 6HP | list the shift schedules and the lock-up (TCC) maps per coding, and which lock-up characteristics each gear actually uses (from the calibration's own selection table, across all the TCC maps); find the shift-feel maps for a given shift and clutch, decoded from ZF's own names; list the fault codes per coding and propose switching them (by fault class); run the shift torque report on gearbox logs — the torque the TCU asked for at each shift against what the engine delivered, which tells a gearbox calibration problem from an engine-side limit |
| VDO cluster | read the cluster's identification (layout, ROM build, coding, odometer), its K-value and speedometer maps, and what would stop a K-value change from taking effect; work out a new K-value for a tyre change or a measured speed error; list the needle / LED sweep patches that fit this exact ROM build, and propose one |
| Other modules (ZF 8HP, Aisin, Climatronic ...) | everything under [For every file](#for-every-file): the parameters their definitions name, and **File information** |

The module pages say more about each of these: for example [Fault codes
(ZF 6HP)](../modules/zf6hp/fault-codes.md) or the [needle sweep
patches](../modules/vdo/needle-sweep.md).

## Asking well

- **Name the goal, not just the map**: *"firmer 2-3 upshifts in S without
  touching D"* lets it find the right maps and keep the rest alone.
- **Point at what you mean**: select the parameter first, or use the
  right-click **Ask AI** items, so *"this map"* is unambiguous.
- **Give it a comparison** when you ask what changed: the original as read,
  another version, or another file.
- **Log first** for drivability problems: a log of the problem, kept in the
  project, gives it something to measure instead of guessing.
