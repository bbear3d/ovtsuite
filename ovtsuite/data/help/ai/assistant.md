---
title: The assistant panel
order: 10
context: AssistantDock
---
# The assistant panel

The assistant is the panel on the right of the main window. Ask it about the
calibration in the active tab, a map, a data log, or how to do something in
the app, and it answers in the panel.

![The assistant panel: the answer, a link to the map it explained, and a proposal with Preview, Accept and Reject](../images/ai-panel.png)

It needs an AI service first: **Tools > AI > Settings...**, see [AI settings
and privacy](settings.md). Until **Use AI inside OVT-Suite** is ticked there,
nothing is sent anywhere and the panel says so, with a link to the settings.

## Opening, resizing and folding it

- It starts folded to a slim strip down the right edge, labelled **AI
  assistant**. Click the strip, or press **Ctrl+Shift+A** (**Tools > AI >
  Assistant**), to open it.
- Drag its left edge to make it wider or narrower.
- The arrow in its title folds it back to the strip; **Ctrl+Shift+A** does
  the same. The cross hides it completely — **Ctrl+Shift+A** brings it back.
- It opens the way you left it: folded or open, and at the same width.

## Asking

Type in the box at the bottom and press **Ctrl+Enter** or click **Send**.
Ask the way you would ask a person who knows the car and the app:

- *"What does this map do?"* — with a parameter selected in the tree.
- *"What changed against the original?"*
- *"Is the smoke limiter holding back fuel above 3000 rpm?"*
- *"Which maps limit torque in first gear?"*
- *"Summarise the last log: anything abnormal?"*
- *"Raise boost 100 mbar from 2000 to 3500 rpm and keep it safe."*
- *"How do I write the TCU?"* — answered from these help pages.

The answer appears as it is written. While the AI works, the line above
**Send** says what it is doing (*Reading FlMng_qPresSmoke_MAP...*,
*Searching help for flashing...*). **New chat** starts over with an empty
conversation (the AI forgets the earlier questions, so start a new chat when
you change subject).

**Stop** ends the answer at once: text being written stops where it is (what
has arrived stays in the panel) and nothing more is asked of the service. A
look-up the AI has already started — reading a map, comparing the file — is
let finish first; these run beside the window, so it stays responsive and
you can keep working while the AI reads.

### Include

Under the conversation, **Include** says what the AI is told you are looking
at:

- **this tab** — the active tab: its file or project, its comparison, and
  whether it has unsaved edits (the AI sees them as they are).
- **the selected parameter** — so *"this map"* means the parameter selected
  in the tree or open in the table.

Untick either to leave it out. With **this tab** unticked, the AI can still
open files you name and answer questions about the app.

## What it shows you

- **Tables.** When the AI talks about a parameter it can open its table in
  the tab and select the cells it means — *"these are the cells above
  3000 rpm"*. It can also open a category of the parameter tree, or filter the
  tree to what it found. It never edits a table itself.
- **Parameter links.** The parameters an answer is about are listed under it
  (**Parameters:**). Click one to open its table.
- **Proposals.** A change the AI suggests is shown under its answer as a
  **Proposal**, with **Preview** · **Accept** · **Reject** · **Review...**:
  - **Preview** shows the tab compared against the result, so the table marks
    every cell the proposal would change. Click **Preview** again to get your
    own comparison back.
  - **Accept** applies it (see [what accepting does](proposals.md#accepting)).
  - **Reject** drops it.
  - **Review...** opens **Tools > AI > Proposals...** at it, with the full
    list of parameters and cells.
- **Plans.** A goal that needs several steps arrives as a **Plan** with
  **Preview result** · **Accept plan** · **Reject plan** · **Review steps...**;
  see [Plans](proposals.md#plans).
- **Notes** in italics: a preview started or ended, values withheld or
  removed before sending, a local model used because the online service could
  not be reached.

The top line shows the service and model in use, and how many tokens this
chat has used (what an online service charges for). **Settings** opens the
AI settings.

## Asking from where you are

You do not have to type the question: these put a ready-made one to the
assistant and open the panel.

| Where | What to use | What it asks |
|---|---|---|
| A parameter in the tree, or the open table | Right-click > **Ask AI: explain this parameter** | What the parameter does, how it is used, what its values mean. |
| | Right-click > **Ask AI: what changed in it** | What differs from the comparison in that parameter, and what it does to the car. Needs a comparison. |
| | Right-click > **Ask AI: what changed in this file** | Every difference from the comparison, grouped by system, with the significant ones explained and the risks pointed out. Needs a comparison. |
| The **Find** panel | Type a description, click **Ask AI** (next to **Find**) | Finds the parameters that match a description rather than a name — *"the map that limits torque in first gear"*. |
| A log in the **Project** panel | Right-click > **Summarise with AI...** | What was driven, the ranges of the key signals, anything abnormal. |
| The **Help** menu | **Ask the AI about the app...** | Questions about using the app, answered from these help pages. |

## Good to know

- **The AI works with what the app knows.** It reads the parameters the
  app's definitions name, with their real units, so it is as good on a file
  as the app's support for it. On a file with checksum-only support it can
  still read the **File information** facts, but there are no maps to read.
- **Logs are summarised, not sent whole.** The AI first gets each channel's
  range, average and when the extremes happened, then asks for short windows
  around the events it wants to look at.
- **Smaller models do less.** A model on this PC is free and private, but a
  small one may misread a map or give up on a long task. For plans and
  log analysis, use a larger model or an online service.
- **It can be wrong.** Check proposals with **Preview** before you accept
  them, and log a test drive after.
