---
title: Reading
order: 20
---
# Reading a control unit

A read copies the unit's memory into a project. It changes nothing in the unit,
and it is the backup you will need if a later write goes wrong — so read
every unit before you write to it, and save the read.

## Steps

1. Connect the cable; ignition on, engine off.
2. Open **Tools > ECU read / write...** (or the OVT Flasher).
3. Choose the **ECU**. For a unit that only talks over CAN, **Transport**
   switches to **CAN / TP 2.0 (J2534 / OpenPort)** by itself.
4. Choose the cable in **Port** / **Device**; click **Refresh** if it is
   missing. For K-line, choose a **Speed** (start with **Medium**).
5. Click **Read flash**. (The Climatronic cannot be read over OBD; only
   **Write flash** is offered for it.)
6. Watch the status line and the bar. The timer on the right shows the time
   taken so far.
7. When the read finishes, save it (see below).

If a read fails part-way, nothing is lost and nothing in the unit changed:
the dialog shows the reason, and you can simply read again — at a lower speed
if the connection was dropping.

## Where the read goes

Every read goes into an **OVT project**, with the read itself as the
project's original -- the first version of its history, never changed (see
[Projects](../projects.md)). In the main window it opens as a project tab;
when a project tab is in front you are asked: **Add to <project>** (as a new
branch of a set of the same kind, or else a new set) or **New project**
(closing the question also makes a new project, so a read is never lost). In
the OVT Flasher it is saved as an `.ovt` project by default; you can choose to
save the raw image exactly as read (`.bin`) instead. A raw `.bin` holds only
the main flash: the EEPROM of an EDC15 **Full backup** and the MPC flash of
an EDC16 **Full read...** are kept only in an `.ovt`.

| ECU | What is read |
|---|---|
| **EDC16** (**Read flash**) | The 512 KB calibration region (0x180000–0x1FFFFF), as a 2 MB image with the rest filled with 0xFF |
| **EDC16** (**Full read...**) | The whole external flash and the internal (MPC) flash, as one set |
| **EDC15** (**Read flash**) | The whole 512 KB flash |
| **EDC15** (**Read EEPROM**) | The 512-byte EEPROM -- with an EDC15 project in front, you are asked **Add to <project>** (a new branch of its ECU set, *EEPROM read <date>*: the flash of the version in front plus the read EEPROM) or **New project**. Either way the [EEPROM editor](../tools/eeprom.md) then opens on it (with the EDC15 module). |
| **EDC15** (**Full backup**) | Flash, then EEPROM, as one set (flash + EEPROM) |
| **ZF6HP** — 6HP19 / 6HP26 (TP 2.0) | The calibration window, in a 1 MB image filled with 0x00 outside it |
| **ZF6HP** — 6HP28 (UDS) | The 512 KB calibration window, in a 2 MB image at its flash address, 0xFF below it (see [Transmissions](transmissions.md#6hp28-uds)) |
| **Aisin TRx** | The whole 1 MB flash |
| **Cluster** | Experimental read of the cluster flash |
| **Cluster (VDO K-line)** | The 2 KB EEPROM, with what the cluster reported (ROM version, coding) |
| **ABS** | The flash dump |

A read is in the crash-recovery store as soon as it opens, before it is
saved. **Save** asks for the project's name the first time.

Two things a read can produce are not binaries of a set: the EDC15
**internal-ROM** dump (**Advanced**) can be kept as an attachment of the
project in front (you are asked) or saved as a file, and the **CAN traffic
logs** of the Cluster and ABS debug option are always files.

> A **calibration read** contains only the part of the flash the diagnostic
> port gives access to — not the unit's whole memory. That is all a normal
> calibration write needs, but it is not a complete image for a bench
> programmer.

## Reading over CAN

- The CAN bit rate next to **Transport** should stay at **500000 bit/s** for
  the engine and transmission units.
- The speed box is greyed out: the unit sets the pace on CAN.
- EDC16 reads over CAN are made in 64 KB pieces; that is normal.

## Tips

- Read twice and compare the two reads in the main window (**File > Compare
  with open tab**) if you have any doubt about a cable. Two reads that
  match are a good sign the cable and speed are reliable.
- A read takes from under a minute to several minutes depending on the unit,
  the cable and the speed. The computer is kept awake meanwhile.
- See [Troubleshooting](troubleshooting.md) if the unit does not answer.
