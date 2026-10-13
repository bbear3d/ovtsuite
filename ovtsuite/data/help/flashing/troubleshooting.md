---
title: Troubleshooting
order: 110
---
# Troubleshooting the flasher

See also the app-wide [Troubleshooting](../troubleshooting.md) page and
[Cables and interfaces](../cables.md).

## No cable in the list

The **Port** / **Device** list shows **Scanning for devices...** while it
looks, then the cables it found.

- Click **Refresh** after plugging a cable in.
- Over **CAN**, only J2534 devices, an OpenPort on USB and CAN-capable head
  units are listed. Over **K-line**, serial and FTDI cables, J2534 devices and
  head units are listed.
- With nothing plugged in, the CAN list shows *(no J2534 device or OpenPort
  found)*. When the reason is something you can fix, it is added, for example
  *(no J2534 device or OpenPort found -- OpenPort: pyusb is not installed)*.
  On Linux and macOS the OpenPort needs **pyusb** and **libusb**; see
  [Cables](../cables.md).
- On Linux, a cable that is listed but will not open is usually a permissions
  problem — see [Cables](../cables.md#linux-access).
- A directly-driven FTDI cable marked **(in use)** is held by another program.
  Close it.

If choosing CAN once left the dialog's dropdowns looking stuck, that was an
old fault that is fixed; the device scan now runs in the background, so the
dialog stays responsive while it looks.

## The unit does not answer

The error is shown in a **Flashing** dialog and on the status line.

- Check the ignition is on and the cable is seated at both ends.
- Check the **ECU** entry matches the unit, and **Transport** is right
  (K-line vs CAN).
- **K-line:** try a lower **Speed**. For an older EDC16, try **Disable Fast
  Init**; for a later one, leave fast init on. A J2534 device does the init in
  hardware.
- **CAN:** confirm the CAN rate (500000 for engine/transmission units). For a
  comfort-CAN unit (the Cluster or ABS) a bus probe at **100000** can confirm
  it is on the bus — see [Advanced](advanced.md#cluster-and-abs).
- Make sure nothing else is using the cable (the data logger, the sniffer,
  another program).

## A transfer fails part-way

- Lower the **Speed** and try again. An unstable cable or a low battery shows
  up as a read or write that stops in the middle.
- Keep the battery on a charger.
- Reads lose nothing: just read again. For writes, see
  [Backups and recovery](backups-and-recovery.md).

## Checksum will not be corrected

This appears before a write when the module that corrects this unit's
checksum is not installed. The file will be written **exactly as it is**.
Answer **Yes** only for a file you know is already correct (an unmodified
read, say); otherwise install the module (**Tools > Modules...**) and write
again. See [Writing](writing.md#checksums).

**Checksum cannot be corrected** is the same question when the module is
installed but cannot handle this file: a ZF6HP image whose checksum layout
it cannot read, or a Climatronic firmware whose checksum it has not mapped.
The same advice applies.

## The write finished but the car misbehaves

- Read the unit again (**Add to <project>** puts the read in the project you
  wrote from, as a new branch) and compare the two tabs: in the tab you
  wrote, **File > Compare with open tab** and pick the read. For an EDC16, a post-write checksum warning on the status line may have
  scrolled past — a read-back comparison catches it.
- Check that the file was made for this exact unit (part number and software
  version). The flasher checks size and format, not fitment.
- Clear fault codes with your usual tool and road-test carefully.

## The job seems frozen

A read or write can sit on one stage for a while — an erase in particular can
take several seconds with no visible change, and the flasher waits for the
unit rather than giving up. The timer keeps counting. Give it time before
assuming it has stopped; do not unplug anything.
