---
title: Welcome
order: 0
---
# OVT-Suite help

OVT-Suite (Old VAG Tuner Suite) reads, edits, compares and flashes the control
units of older VW-group cars: Bosch EDC16 and EDC15 engine ECUs, ZF 6HP and
8HP and Aisin transmissions, VDO instrument clusters and the Audi A6 / Q7
Climatronic.

![The main window with an EDC16 file open](images/main-window.png)

## New to OVT-Suite?

1. [Installing and updating](installing.md) — the installers, the modules,
   cables on Linux.
2. [Getting started](getting-started.md) — from importing your first file
   into a project to saving your first change.
3. [Before you start](flashing/before-you-start.md) — read this before you
   connect to a car.

## Working on a file

- [Projects (.ovt)](projects.md) — everything you work on is a project: its
  control units, branches, saved versions, logs and patches.
- [The main window](interface.md) — the parameter tree, tables, tabs and
  menus.
- [Editing and saving](editing.md) — changing values, undo, checksums and
  version history.
- [Editor reference](editor/index.md) — tables, comparing files, the Find
  panel, patches, the hex editor, file formats, exporting and every keyboard
  shortcut.
- [Parameter tools](tools.md) — fault codes, measuring blocks, gear ratios
  and speed scaling.
- [AI assistant](ai.md) — ask an AI about the calibration, a map, a log or
  the app, and review the changes it proposes; or connect your own AI app.

## Working with the car

- [ECU read / write](flashing.md) — reading and flashing every supported
  control unit.
- [Data logging](logging.md) — live values, presets, log files and fault
  codes.
- [Bus sniffer](sniffer.md) and [ECU simulator](simulator.md) — seeing what
  another tool does on the bus.
- [Cables and interfaces](cables.md) — which cables work, and how to set them
  up.

## Modules

Support for each kind of control unit comes in a [module](modules.md); each
brings its own pages, listed under **Modules** in the contents:
[Bosch EDC16](modules/edc16/index.md), [Bosch EDC15](modules/edc15/index.md),
[ZF 6HP](modules/zf6hp/index.md), [ZF 8HP](modules/zf8hp/index.md),
[Aisin TR-xx](modules/aisin/index.md), [VDO Cluster](modules/vdo/index.md)
and [4F/4L HVAC](modules/hvac/index.md).

## When something goes wrong

[Troubleshooting](troubleshooting.md) lists the app's messages and what to do
about them. **Help > About** shows the version and installed modules, with a
**Copy** button for bug reports.

> **Press F1** in any window to open the help page for it.
