---
title: Fault code report
order: 11
context: FaultCodeReport
---
# Fault code report

After every read of the ECU's fault codes — and after **Clear codes on ECU**,
which reads them again — the **Fault code report** window shows what was
found. The **Fault codes from ECU** window closes on the result, and the
report stays open beside the main window without blocking it: you can work
in the editor, the grid or the [AI assistant](../ai/assistant.md) panel with
the report still on screen. A new read (or a kept read opened from the
Project panel) for the same file or set takes the place of a report still
open. See
[Reading fault codes](../logging/fault-codes.md) for the read itself.

## What it shows

The top of the window says when the codes were read, which controller
(**EDC16**, **EDC15**, a gearbox) and the identification the ECU reported, the
cable and transport (**K-line** or **CAN**), and the open file. After a clear
it says how many codes were cleared and which came straight back — a fault
that is still present is stored again as soon as the ECU checks it.

One row per code:

| Column | What it is |
|---|---|
| **VAG code** | The number a VAG scan tool shows: five digits, or six for an EDC16 that reports SAE words |
| **OBD-II** | The P-code, when the code has one |
| **Description** | The same text as the fault-path editor's **Description** |
| **Status** | The status byte the ECU sent with the code — see [The status byte](#the-status-byte) |
| **Fault type** | The status's fault type (EDC15) or fault symptom (EDC16, gearboxes) in words |
| **Intermittent** | EDC15: ticked when the scan tool would write `-10`. EDC16 and gearboxes: ticked when the code is stored but the fault is not present at the time of the read |
| **Fault paths** | The open file's fault paths that can set this code, each with **on** or **off**. "none in this file" (shaded) when no fault path sets it |
| **Change** | **new** when the read before did not have it; a greyed row marked **gone** is a code the read before had and this one does not |

When the ECU has no codes stored, the window says **No fault codes stored**
(or, after a clear, that the ECU now reports none).

## The status byte

The two engine ECUs send a different kind of status with each code; hover
over the **Status** or **Fault type** cell for what it says.

- **EDC15** (read over KW1281) sends a VAG *fault type* and a sporadic
  flag, shown the way a VAG scan tool writes them: `07-10` is fault type
  7, "signal too low", with the `-10` (sporadic) flag. Nearly every EDC15
  code comes with fault type **35**, which has no words: the five-digit
  code itself already says what failed. The `-10` flag means the fault
  came back after it had healed — not that it is absent now.
- **EDC16** (and the gearboxes, read over KWP2000) send a set of status
  bits, shown as a hex number such as `0x60`: whether the fault is
  **present** now (when it is not, VCDS calls the code *Intermittent*, and
  so does the **Intermittent** column), whether it is **stored** (some
  EDC16 software sends **not yet confirmed** instead, until the fault has
  been seen often enough to count), whether the fault path has **not been
  tested since switch-on**, and whether it lights the **system lamp** —
  plus a fault *symptom* number. An EDC16 almost always sends symptom
  **0** ("none given"); when its calibration gives one, 1, 2, 4 and 8
  stand for upper limit exceeded, lower limit exceeded, no signal and
  implausible signal. There is no `07-10` form for these codes.
- On EDC16 software that reports its codes as SAE words (P447, and the
  later P475 and P529 software), the code is written with six digits, the
  way VCDS writes it — `000259` for P0103 — with its P-code beside it.

## History

Each read is kept in the project of the open file, as an attachment of its
set (**Project panel > Attachments**, listed as "Fault codes" with the date
and number of codes). The **History** list shows this set's earlier reads,
newest first; pick one to see it, with its **new** and **gone** codes
against the read before it. Double-click a read in the Project panel to open
its report at any time. Without a project, the report still works but says
the read was not kept.

## Actions

Select one or more rows first; with none selected the actions take every
code.

- **Search** opens your web browser on a search for the selected codes: the
  VAG code, P-code, description and the ECU's part number. The arrow beside
  it picks the site — **Google** (the first time), **Bing**, **DuckDuckGo**,
  the **TDIClub forum**, the **MyTurboDiesel forum** (both searched through
  Google, restricted to the forum) or the **Ross-Tech wiki** (by VAG code).
  The site you pick is remembered. The app itself sends nothing anywhere;
  your browser does the search.
- **Ask AI** hands the [AI assistant](../ai/assistant.md) a question about the
  selected codes: their status, description and fault paths, asking for the
  likely causes, the checks to make on the car, and whether disabling the
  fault path is appropriate.
- **Ask AI about this report** hands it the whole report together with this
  set's earlier reads — which codes are new, gone, recurring, or came back
  after a clear — and asks it to interpret the pattern.

Both AI buttons put the question in the assistant panel; the report stays
open beside it, so you can read the answer and come back to the codes, or
ask about another one. When the assistant is off, or the file's tab has
been closed since the read, they say so instead. Nothing is sent to an AI
service unless you have turned the assistant on.

The same actions are on the table's right-click menu.

## Buttons

- **Copy** copies the report as text.
- **Save as...** saves it as a text file or a CSV file (one row per code).
- **Print...** prints the text report.
- **Show in fault paths** (the default) closes the report, brings the
  fault-path editor that asked for the read forward — switching to its file's
  tab when another one is in front, and restoring it when minimised — and
  switches its list to **Stored on the ECU**. It works at any time while the report is open, as
  long as that editor is still open (closed since, the button just closes
  the report; read the codes again from the editor).
- **Close** closes the report. The editor already has the codes from the
  moment the read finished: its note says what was stored and the rows that
  set a code are shaded, but its list is not switched to the stored codes.

See also: [DTC / Fault-Path editor](dtc.md),
[Reading fault codes](../logging/fault-codes.md).
