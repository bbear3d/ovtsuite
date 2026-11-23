---
title: Procedures
order: 40
context: ProceduresPage, ProcedureDialog
---
# Procedures

A procedure is a scripted sequence run on the connected module: read some
groups, show the values, ask the user, wait for a condition, and -- with
confirmation -- write an adaptation. The bundled pack has a boost-pressure
check, an EGR check, an engine snapshot, a fault baseline and, for the
distributor-pump TDIs, the [TDI timing graph](#tdi-timing-graph-ve-pump) and
the block 000 full-throttle check; a [definition pack](definitions.md) can add
more.

## TDI timing graph (VE pump)

For the distributor-pump (VP37) TDIs on KW1281 -- engine codes AGR, AHF, ALH,
ASV, AGP and AQM. Measured value block 000 is ten raw values with no units
of their own; the app reads them the way the engine control unit formats
them, and the workshop manual's group 000 tables give the limits for each
engine code. Pick the engine code when the procedure asks.

**TDI timing graph (idle, VE pump)**: with the engine warm (coolant 80 °C or
more) and idling, block 000 is read continuously and drawn live as a graph:
coolant temperature across, start of injection up (degrees before top dead
centre; below zero is after it). The green band is the manual's window --
2° after to 3° before TDC for AGR/AHF/ALH/ASV, 1° after to 7° before for
AGP/AQM. The newest reading is the large dot, green inside the window and
red outside it; the earlier ones fade behind it. The values hold only while
the EGR is working, and it switches itself off after about two minutes of
idling: rev briefly or restart the engine if it has idled longer. It reads
for as long as you let it -- there is no shortest or longest time -- and the
line under the graph counts the readings; press **Finish** when you have seen
enough.

**Block 000 full-throttle check (VE pump)**: the manual's road test. In 3rd
gear, accelerate at full throttle through 3000 rpm while the procedure reads
block 000; the graph shows start of injection against engine speed with the
manual's window at 2900-3100 rpm. It too reads until a second person presses
**Finish**.

Both end with all ten values checked against the manual, each shown in its
units with the displayed number in brackets and the limit as the manual
prints it. The check uses the reading taken in the manual's engine-speed
range (idle, or 2900-3100 rpm); if the engine was never in it, the last
reading is checked and the result says so. Values the manual says to ignore
for an engine code are marked *ignore*. The result goes in the journal.

## The Procedures tab

On the left is the gallery of procedures under category filters (`All`,
`Service`, `Diagnosis`, ... and `Mine` for the ones in your own local pack).
Each card shows the name, the module it is for, how many steps it has and a
chip with its safety class. A card dimmed with **Not for this module** is a procedure for another
module or one this module's definitions cannot support; you can read it but
not run it. The dashed **+ New procedure** card opens your local definitions
folder (see [Writing one](#writing-one)).

Select a card and the right side shows its steps as numbered cards, what it
needs, and **Run**. Run needs a session on the module: connect first (the
button says so). Press **Run** and the cards follow the procedure: the card of
the step being done is open and the finished ones collapse to the values they
showed. A live step shows its table filling, a choice appears as options in
its card, a wait counts down, and the exact plan of any write is shown before
it is made, with the values it read and the values it will write. **Stop**
ends the procedure at the current step; nothing is written after a stop. When
it ends, the result card lists what was written and whether each re-read
confirmed it, and **Run again** repeats it.

**Show trace while writing** opens the bus trace pane at the bottom of the
window and records the trace to a file, so a write can be reviewed byte for
byte afterwards. The login line at the bottom says whether the procedure
needs a login on this module.

One procedure runs at a time on a session. Closing the window or
disconnecting while a question is waiting stops the procedure.

Ctrl+K finds procedures by name too, and selects the one you pick.

A write step always shows the value it read, the value it will write, and
asks; after the write it reads back and records both in the journal.

## Writing one

A procedure is a YAML document of kind `procedure` in a pack. The steps are
`read`, `show`, `choose`, `live`, `check`, `wait`, `log` and `write`; the
schema is printed by:

```
ovtsuite diag-defs schema procedure
```

Put it in your local pack (`<config>/diag/definitions/my-local`) and press
**Reload definitions**.

A `live` step reads for `seconds` (15 when not given), or with `seconds:
until done` until the user presses **Finish** (the button that is **Skip the
rest of this step** on a timed one).

A `live` step of block 0 on a distributor-pump engine can take `layout:
edc15vm` (read the raw bytes as values), `basic: true` (read it as a basic
setting), `plot:` (`x` and `y`: which two values, by number, to draw against
each other; `band: {engines: ..., condition: idle | full}` for the manual's
window) and `check:` (the same `engines` and `condition`: every value against
the manual afterwards). `engines` is `ahf` (AGR, AHF, ALH, ASV) or `agp` (AGP,
AQM), or `@id` for the answer to a `choose` step with that `id`.
