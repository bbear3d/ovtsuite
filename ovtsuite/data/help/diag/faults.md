---
title: Faults
order: 10
---
# Faults

The **Faults** tab lists the stored faults of the connected module: the
code, its text, the decoded status, and the freeze frame when the module
serves one.

## Codes and texts

A fault code is shown the way the module reports it:

- **KW1281 and most KWP2000 modules** report a VAG five-digit number
  (`01044`); when a standard P-code corresponds it is shown beside it.
- **Engine ECUs of the EDC16 family** report SAE words (`P0299`); the VAG
  number is shown beside it when one is defined.
- **UDS modules** report three-byte codes, shown as `P1714-00`.

Which form a module uses comes from its [definition](definitions.md); when
the definitions are silent, a KWP2000 word is read as a VAG number.

The text comes from the definitions packs first (the EDC16 module ships its
fault texts), then from the standard P-code list. A code with no text is
shown as *(no text)*: **Copy** puts the list on the clipboard to look it up,
and the **Ask AI** button of the main app's fault report works the same way
on a scan report.

## Status

The status byte is decoded in words -- stored, present, intermittent, the
test conditions -- with the controller family's own meaning where the suite
knows it (EDC15, EDC16). The raw byte is in the tooltip.

## Clearing faults

**Clear faults** asks first, clears the module's memory, reads the faults
again and shows what remains; the before and after lists go into the
vehicle's [journal](../diag.md#the-garage) when a vehicle is selected.
Faults that are present again at once point at a live problem.

Clearing does not need a login on most modules; one that refuses says so.
