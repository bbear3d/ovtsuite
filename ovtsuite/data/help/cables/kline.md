---
title: K-line (KKL) cables
order: 10
---
# K-line (KKL) cables

A KKL cable is a USB-to-K-line adapter — the common "VAG-COM / KKL 409"
cables. The app uses two kinds, told apart by the USB chip inside: **FTDI**
and **CH340/CH341 (CH34x)**. Both appear in the flasher's **Port** list on
the **K-line** transport.

## FTDI cables (recommended)

An FTDI cable is the better choice:

- It can be driven **directly** (bypassing the operating system's serial
  driver), which sets the chip's latency timer low and makes K-line sessions
  noticeably quicker. The app does this by itself when it can; such a cable is
  listed as **(FTDI direct)**.
- It is the **only** kind that can hold the EDC16 **High** speed (244898
  baud).

A directly-driven FTDI cable that another program already holds is listed with
**(in use)** — close the other program.

If the direct route is not available, an FTDI cable still works through the
ordinary serial port (a `COM` port on Windows, `/dev/ttyUSB*` on Linux,
`/dev/cu.*` on macOS); it is then listed by its port name. You lose only the
lower latency.

## CH340 / CH341 cables

CH34x cables work for K-line but **cannot hold the High speed** — the flasher
removes **High** from the speed list for them and caps them at **Medium**.
They always go through the serial port. For EDC15, EDC16 at Medium and the VDO
cluster they are fine.

## Which speed

In the flasher, **Speed** is Low / Medium / High. Start with **Medium**; drop
to **Low** if a connection is unreliable. **High** is shown only for a cable
that can hold it. EDC15 EEPROM work always runs at the low speed. See the
[flasher overview](../flashing.md#speed-and-init).

## Fast init

The later EDC16 ECUs need the ISO 14230 fast init to wake up. On a serial KKL
cable the app does this with a break pulse; it is the default. The **Disable
Fast Init** box forces the 5-baud slow init for older ECUs. A J2534 device
(below) does the fast init in hardware, which is more reliable — worth using
for a stubborn later EDC16.

## J2534 for K-line

A J2534 device can also do K-line, and does the init in hardware. Such entries
are marked **(J2534)** in the K-line list. See
[J2534 devices](j2534.md).

## Platform notes

### Windows

A KKL cable needs its USB-serial driver (FTDI's VCP driver, or the CH340
driver) so it appears as a `COM` port. The direct-FTDI route uses FTDI's D2XX
driver, which Windows Update usually installs with the cable. When the driver
is missing, the Windows installer offers **Download and install FTDI's USB
driver**, ticked. It downloads the driver from Microsoft's driver catalog and
asks for administrator permission to install it. You can also install it
yourself from ftdichip.com (Drivers > VCP / D2XX). The app does not ship the
D2XX library itself.

### Linux

The installed package's udev rule gives you FTDI and CH34x K-line cables
without root or group membership. Running from source instead, add your user
to the group that owns `/dev/ttyUSB*` (often `dialout`) and re-log in, or
install a udev rule of your own. The direct-FTDI route needs access to the USB
device.

### macOS

FTDI and CH34x cables appear as `/dev/cu.*`. Recent macOS includes drivers for
both; for the direct-FTDI (libusb) route, the app talks to the chip over
libusb. No group membership is involved.
