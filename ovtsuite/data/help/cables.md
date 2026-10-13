---
title: Cables and interfaces
order: 70
---
# Cables and interfaces

To read or write a control unit you need a cable between the computer and the
car's diagnostic socket (or a bench harness). Which one depends on how the
unit talks:

- **K-line** units (EDC16, EDC15, the VDO cluster) use a cheap **KKL** serial
  cable, or a J2534 device.
- **CAN** units (the ZF/Aisin transmissions, Climatronic, the C6 cluster and
  ABS, and the later EDC16) need a **J2534** interface — the **Tactrix
  OpenPort 2.0** is the tested one.

Pick the cable in the flasher's **Port** (K-line) or **Device** (CAN) list;
the [flasher's overview](flashing.md) explains the rest of the dialog.

## Choosing a cable

| You want to work on | Use |
|---|---|
| EDC15, older K-line EDC16, VDO cluster | An **FTDI** KKL cable (best), or a CH340/CH341 KKL cable |
| Later CAN-init EDC16 over K-line | A **J2534** device (reliable hardware fast init), or an FTDI cable |
| Any CAN unit (transmissions, Climatronic, C6 cluster, ABS, CAN EDC16) | A **Tactrix OpenPort 2.0** (or a registered J2534 device on Windows) |
| The cable in the car, driving from indoors | A KW-Dash [head unit](flashing/head-unit.md) over the network |

An **FTDI** cable is the best K-line choice: it can be driven directly, which
lowers latency and makes sessions quicker, and it is the only kind that can
hold the EDC16 **High** speed. A **CH340/CH341** cable works but is capped at
the Medium speed. If you buy one cable for K-line, buy an FTDI one.

The two pages below cover each kind in detail:

- [K-line (KKL) cables](cables/kline.md) — FTDI and CH34x, direct vs serial.
- [J2534 devices and the OpenPort 2.0](cables/j2534.md) — including the
  OpenPort over USB on Linux and macOS, and Windows drivers.

## Linux access

On Linux the package installs a udev rule (`70-ovtsuite.rules`) that gives the
logged-in user access to the OpenPort 2.0, FTDI K-line cables and CH34x K-line
cables **without root and without joining the `dialout` or `plugdev`
groups** — and tells ModemManager to leave the OpenPort alone. Plug the cable
in after installing and it works.

If you run from source instead of the package, you do not get that rule. Then
either add your user to the group that owns the serial device, or install an
equivalent udev rule yourself. See [K-line cables](cables/kline.md#linux) and
[J2534 / OpenPort](cables/j2534.md#linux-and-macos-openport-over-usb) for the
specifics.

## Nothing is listed

- Click **Refresh** after plugging the cable in.
- An empty list says why, e.g. *OpenPort: pyusb is not installed*.
- See [Troubleshooting the flasher](flashing/troubleshooting.md#no-cable-in-the-list).
