---
title: Procedures
order: 40
context: ProceduresPage, ProcedureDialog
---
# Procedures

A procedure is a scripted sequence run on the connected module: read some
groups, show the values, ask the user, wait for a condition, and -- with
confirmation -- write an adaptation. The bundled pack has a boost-pressure
check, an EGR check, an engine snapshot and a fault baseline; a
[definition pack](definitions.md) can add more.

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
