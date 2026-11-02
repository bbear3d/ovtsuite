---
title: OBD-II and the gateway list
order: 60
---
# OBD-II and the gateway list

## OBD-II

The **OBD-II** page of the vehicle tools (the **OBD Generic-II** row at the bottom of the module rail) is the generic emissions interface every car since 2001
(petrol) or 2004 (diesel) answers, whatever its make: the MIL state and
readiness monitors, stored, pending and permanent codes with their standard
texts, the freeze frame, the live PIDs the engine supports, and the VIN and
calibration identifiers. Over CAN it is the functional request every
emissions module answers; over K-line it is the ISO 9141-2 or ISO 14230-4
init at the OBD address.

**Read** takes a snapshot; **Clear** clears the emissions codes (and the
readiness monitors with them) after asking, then reads again. **Copy** puts
the snapshot on the clipboard.

This is the standard subset only. The module's own faults, with the VAG
codes and texts, are on the [Faults](faults.md) tab of a connected module.

## The gateway installation list

On a car with a diagnostic gateway (CAN / TP 2.0), the gateway keeps a list
of the modules the car has, each with its status and its address on the bus.
The **Gateway list** page of the vehicle tools reads it; a scan over CAN reads it first and visits
exactly those modules.

A module the list names that the bundled address table does not know is
shown with the address the car gave it. **Save new addresses to my local
pack** records those rows in your [local pack](definitions.md#your-local-pack),
so the next scan knows them, and so they can be contributed.
