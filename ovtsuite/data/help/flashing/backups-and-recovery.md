---
title: Backups and recovery
order: 100
---
# Backups and recovery

Writing can fail part-way — a dropped cable, a power dip, a cancelled job.
What you can do then depends on the unit. **Nothing here is guaranteed:** some
units recover from a diagnostic cable, some need a bench programmer, and some
writes cannot be undone. The one thing that always helps is a backup read from
the unit before you changed anything.

## Make the backup first

Before any write, read the unit with the same flasher and save the read.
For an EDC15 use **Full backup** (flash and EEPROM together). A read goes
into a project as its **original as read**, which is never changed — edit a
branch, and if a write goes wrong, the original is what you write back. In
the main window, right-click it in the Project panel > **New branch from
this version...** and write that tab; in the OVT Flasher, **Open file...**
the `.ovt` and pick the original version. There are no separate backup files: the
backup is the project, so save it — the OVT Flasher saves a read as an
`.ovt` by default (a raw `.bin` keeps only the main flash, not an EDC15's
EEPROM). The Climatronic cannot be read over OBD; for it, the factory
`.sgo` is the backup. See [Reading](reading.md) and
[Before you start](before-you-start.md).

## The flasher does not back up for you

No unit is read and saved automatically before a write. The VDO cluster's
**Write flash**, for example, writes the whole EEPROM straight away, without
reading the cluster first. The backup you read and saved yourself is the only
copy of what the unit held.

## If a write is interrupted

**Do not switch the ignition off and do not unplug anything** while you work
out what happened. Then:

1. Read the error in the **Flashing** dialog and the last stage on the status
   line.
2. Check the cable, the battery/charger and the computer's power.
3. Try the write again, with your known-good version, at a **lower speed** if the
   connection was dropping. A write only rewrites the parts that do not
   match, so repeating it finishes what was left — the part that was being
   written when it stopped does not match, so it is written again.
4. If it keeps failing, try another cable, and read
   [Troubleshooting](troubleshooting.md).

### EDC16

After an interrupted calibration write, run **Write flash** again with your
file. An ECU left in its boot state after a failed **program** or **boot**
block write (the armed block write) may need a **BDM** bench programmer — the
app's own warnings say so, and this is why the block write is behind a stern
confirmation.

### EDC15

An EDC15 whose write was interrupted often answers in a recovery state (a
KWP2000 boot stub). The flasher detects this when it connects — the status
line says *soft-bricked ... switching to recovery mode* — and writes anyway,
so **Write flash** with your backup is the first thing to try. If the ECU
does not answer at all, EDC15 [boot mode](edc15.md#boot-mode) on the bench,
with the boot pin bridged, can reach the flash chip directly.

### Transmissions

- **ZF6HP:** write your backup again. The 6HP28 write warns that
  an interrupted **program** write can leave the TCU needing bench recovery;
  a calibration write is the lower-risk one. Do writes with the engine off
  and a stable supply.
- **Aisin TRx** is read-only, so there is nothing to recover from.

### Climatronic

Write the file again. The Climatronic write works block by block; writing the
same file again reprograms it. An interrupted or wrong write can brick the
module. It cannot be read over OBD, so keep the factory `.sgo` you started
from: it is your way back to stock.

### VDO cluster

Write again: every write sends the whole EEPROM and reads every block back
to verify it, so a repeated write finishes the job. To put the cluster back
the way it was, write the file you read from it before you started. The
flasher keeps no copy of its own, so without that read there is nothing to
go back to.

## After recovering

Read the unit again, compare it with the tab you wrote in the main window
(**File > Compare with open tab**), and check the car's behaviour and fault
codes before trusting the result.
