---
title: Before you start
order: 10
---
# Before you start

A read is harmless: it only asks the unit for data. A **write erases parts of
the unit's flash and programs them again**. If it stops half-way — power dip,
cable knocked, computer asleep — the unit can be left without a working
program. Many such units can be written again; some cannot without opening
them on the bench. Nothing here can guarantee recovery, so prepare as if it
could not be done.

## Checklist before any write

1. **Have a backup of this unit, read from this unit, saved to disk.** Read
   it first with the same flasher (see [Reading](reading.md)): the read goes
   into a project as its **original as read**, which is never changed. Save
   the project (the OVT Flasher saves it as an `.ovt` straight away) where you
   will find it again, and make your edits on a branch. For an EDC15, use
   **Read flash + EEPROM** (flash and EEPROM together). (The Climatronic cannot be
   read over OBD: keep its factory `.sgo` instead.)
2. **Battery support.** Connect a battery charger or power supply that holds
   a steady voltage for the whole job. A write can take many minutes with the
   ignition on, fans and modules awake; a battery that drops low during an
   erase is a common way to lose a unit.
3. **Switch off what you can**: lights, blower, heated seats, radio. Leave
   the engine **off** (ignition on unless the unit's page says otherwise).
4. **The computer on mains power**, with sleep, hibernate and lid-close
   sleep off. The flasher asks the operating system to keep the computer and
   the display awake while a job runs (Windows; macOS; Linux where systemd is
   present), but this is best effort and does not cover closing the lid.
5. **Nothing else using the cable**: close other diagnostic programs and the
   app's own [data logger](../logging.md) and [bus sniffer](../sniffer.md)
   before you start. A directly-driven FTDI cable that another program holds
   is listed with **(in use)**.
6. **A cable you trust.** Do a read with it first. If reads are unreliable,
   a write will be too — see [Cables and interfaces](../cables.md).
7. **The right file for this unit.** The flasher checks the file's size and
   format (see [Writing](writing.md#checks-before-anything-is-erased)), but
   it does not check that the file was made for this exact part number or
   software version. Make sure it was — ideally by starting from a read of
   this unit.
8. **Time.** Do not start a write you may have to interrupt.

## During the job

- Do not switch the ignition off, open doors that wake other modules, or
  unplug anything until the status line says the job has finished and the
  final message has appeared.
- Do not start the engine.
- Leave the computer alone; do not let it go to sleep.
- **Cancel** asks the flasher to stop. During a read or a check that is
  harmless. During a write it can still leave a unit partly
  written — use it only when something is clearly going wrong, and read
  [Backups and recovery](backups-and-recovery.md) first if you can.

## After a write

- Follow the final message. An EDC16 asks for the ignition to be off for
  5 seconds; the VDO cluster asks for an ignition off/on so it reloads its
  EEPROM.
- Start the car and check it behaves. Read the unit's fault codes with your
  usual tool.
- Keep the backup until you are sure you will not need it.

## On the bench

Bench work (a unit on the table, powered from a supply) follows the same
rules: a stable supply, a reliable cable, and a backup first. EDC15
[boot mode](edc15.md#boot-mode) is bench-only, because the ECU's boot pin
must be bridged while it powers up.
