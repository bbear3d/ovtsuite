---
title: OVT Diag
order: 58
context: DiagWindow, NewVehicleDialog
---
# OVT Diag

OVT Diag is the suite's vehicle diagnostic tool, a program of its own: it
talks to the car, not to a file. It scans a VW-group car, identifies every
control module, reads and clears their faults, shows live data, and runs the
module functions a workshop scanner offers: login, adaptation, coding, basic
settings and actuator tests. It uses the same cables as the flasher and the
logger: a serial K-line cable, a J2534 device, the USB OpenPort, or a paired
head unit.

Start it with the **OVT Diag** shortcut, or `ovtsuite --diag`. The window
opens maximized.

**Not yet tested on a car.** Everything that writes to a module (adaptation,
coding, basic settings, actuator tests, clearing faults) is experimental:
use it on a bench or on a car you can recover first.

## The window

The **top bar** holds the connection and the car:

- The **interface chip** (the dot and the cable's name) opens the
  connection settings: the **interface**, the **transport** and the CAN
  rate. The transport is how to reach the modules: **K-line** (one wire, one
  module at a time), **CAN / TP 2.0** (the gateway-era cars, several modules
  at once), or **UDS** (the ISO-TP modules of later cars). A car from the
  mid-2000s often needs a CAN scan for most modules and a K-line scan for
  the rest. The settings are locked while a session is open or a scan runs.
- The **vehicle chip** opens the list of cars in your [garage](#the-garage),
  **New vehicle...**, and the **generation**: which address table applies
  (which modules a car of that family has and where they are reached). Leave
  it on **All modules** only when you do not know the car; on K-line that is
  a long scan. Without a vehicle you can still scan and connect; nothing is
  saved.
- The **search field** (or Ctrl+K) opens the [command palette](#the-command-palette).
- The **trace chip** turns trace recording on and off, see
  [Recording a trace](#recording-a-trace).
- The **gear** holds the [definition pack](diag/definitions.md) actions and
  **Vehicle tools...**.

Below it, the **module rail** on the left, the **module area** on the right,
and the **bus trace** and **write journal** along the bottom.

### The module rail

Before a scan the rail is empty: OVT Diag does not know which modules the car
has, or which protocol each speaks, until they answer. **Scan all** (F5)
finds out. Afterwards the rail shows one card per module that answered:
address, name, part number, and a chip with the protocol it answered with --
`KW1281 @ 9600`, `KWP2000 @ 10400`, `KWP-TP2.0` or `UDS` -- and a red count
of its faults, or a green dot when it has none. A **live** marker shows a
module with an open session. Modules that did not answer are listed under
**Not answering**, with the reason in the tooltip.

To reach a module the scan did not find, use **+ Connect by address...**, or
open **All known addresses** to pick one from the generation's table. Hover a
row there to see which addresses the table knows for it.

At the bottom of the rail, **OBD Generic-II** and the **Garage** open the
[vehicle tools](#vehicle-tools).

### Scan

**Scan all** connects to every module of the table in turn (over CAN,
several at once), identifies it and reads its faults, then shows the
[scan report](#the-scan-report). Over CAN the scan asks the gateway for its
installation list first and visits only the modules the car says it has,
including ones the table does not know. **Stop** ends the scan after the
current module; a stopped scan is marked as such and the modules it never got
to are listed as *not tried*, so it is not mistaken for a complete one.

With a vehicle selected the scan is saved to the garage and compared with the
previous one: new and cleared faults, changed codings or software, modules
that no longer answer.

### Connect once, then work

Select a module and press **Connect** (or double-click its card, or press
Ctrl+L; any function button connects first). The session stays open -- the
module is kept alive between commands -- until you press **Disconnect**, the
link fails, or the window closes. The header shows the state: the protocol
and baud rate the module answered with, how long the session has been open,
and whether you are logged in. Everything on the tabs runs on that one
session, so a login stays in force for the adaptations and basic settings
that follow. Over K-line there is one session at a time; over CAN each
connected module has a channel of its own, and the others show under **also
live** on the Live data tab.

The tabs of a module:

- **Faults** -- see [Faults](diag/faults.md).
- **Live data** -- see [Live data](diag/live-data.md).
- **Adaptation, Coding, Actuator tests, Basic settings** and the **Login** and
  **Security access** buttons -- see [Writing to a module](diag/writing.md).
- **Identification** -- what the module said about itself.
- **Procedures** -- guided checks and jobs, see [Procedures](diag/procedures.md).

Ctrl+K finds any of these by name; see below.

### The command palette

Press **Ctrl+K**, or click the search field in the top bar, to search
everything the window knows about the car in one box. It dims the window and
lists matches grouped by kind:

- **Modules** -- the modules that answered; the red count is their faults.
- **Live values** -- every named cell of every measuring group of those
  modules, with its current reading while the module is live. A cell found
  through the factory catalogue also answers to the signal name
  (`Ldm_pIst`).
- **Faults** -- by text, by the VAG number (`01044` or `001044`), by the SAE
  code (`P0299`) or by the word in hex (`0x0414`). **active** is a fault that
  is present, **history** one that only occurred.
- **Coding** bits, **Adaptation** channels, **Actuator tests** and
  **Procedures** the definitions name for those modules.
- **Actions** -- Scan all, Connect / Disconnect, Open journal, Export the
  report as HTML, PDF or text, the vehicle tools, trace recording, Install
  pack, Reload definitions.

Type to narrow the list: every word must match, anywhere in the name or the
detail line. Start with `01:` to see only the things of module 01, or with `>`
to see only actions. The chips under the box (`All`, `Live values`, `Faults`
...) filter by kind; Tab steps through them.

**Enter** opens the choice: the module is selected and the tab showing it is
brought up -- a live value ticks its group on the **Live data** tab and
outlines its tile; a fault selects its row on **Faults**; a coding bit, an
adaptation channel or an actuator test goes to its tab and row; a procedure
is selected on **Procedures**. Choosing never opens a session by itself,
except that ticking a live group connects the module, as it does on the tab.
**Shift+Enter** on a live value only ticks its group in that module's live
view and leaves you where you are (a module without a session remembers it
for when it has one); the box stays open to add more. **Esc** closes it.

The list is rebuilt every time the box opens, from the last scan and the
sessions open now; nothing is read from the car for it.

The **Journal** button at the end of the tab row opens every write made to the
car.

### The scan report

The report page opens when a scan finishes, or from the vehicle's scans. Four
cards give the headline: modules reached, faults now, what changed since the
scan you compare with, and how long the scan took. Under them the table lists
every module with its part number, coding and status, and each fault as a card
with its text, status and freeze frame. A module that changed since the
compared scan is marked in orange. Press **Export HTML / PDF** or **Copy as
text** for a report to attach or paste. Select a module and press Enter to go
to it. The vehicle name in the header returns to the workspace.

### Vehicle tools

**Vehicle tools** (the gear menu, or the OBD and Garage rows of the rail) are
the functions that belong to the whole car rather than one module: the
**Gateway list**, **Vehicle live data** across several connected modules,
**OBD-II**, the **Journal**, and the **Garage**. See
[OBD-II and the gateway list](diag/obd.md).

### Recording a trace

Turn the trace chip on to write every byte on the link to a file while you
work; the **Bus trace** pane shows it as it happens and the file's name. Attach
it to a bug report; nothing in it identifies you beyond the car's own
identification.

## The garage

A vehicle is a folder of scans and a journal. **New vehicle** asks for a
name, the VIN and the generation. Each scan is kept with its date; the
report page shows any of them and compares two. The **Journal** lists every
write made to the car -- adaptations, codings, cleared faults -- with the
value before and after and whether the re-read confirmed it. The **Write
journal** pane at the bottom shows the latest entries.

The garage lives beside the app's settings (`<config>/diag/garage`).

## When a module does not answer

- **Nothing answers at any address** -- ignition on? The right transport for
  the car? On K-line, the right pin and a cable the module family accepts
  (see [Cables and interfaces](cables.md)).
- **One module does not answer** -- it may not be fitted, or it is reached
  over the other transport; the gateway list says which modules the car has.
- **A K-line module answers, then the session drops** -- the trace shows
  the first block exchange; the module may want a different baud rate, which
  OVT Diag tries in turn (10400, 9600, 4800) and remembers once found.

See also [Definition packs](diag/definitions.md) for how modules, faults and
measuring groups are named.
