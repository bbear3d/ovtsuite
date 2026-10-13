---
title: Advanced and diagnostic tools
order: 80
---
# Advanced and diagnostic tools

Besides reading and writing, the flasher has buttons for diagnosis and
recovery. You do not need any of these for an ordinary read, write or backup.
Most are read-only probes used while support for a unit is worked out; they
are safe but of no use unless you know what they report.

## The Advanced section

The **Advanced** disclosure at the bottom hides the controls that are rarely
the right answer: EDC15 **Boot mode** and **Read internal ROM**, the
EDC16 diagnostic probes and GPT test listed [below](#edc16), the
Cluster and ABS troubleshooting aids ([below](#cluster-and-abs)), and the
ZF6HP **Transport:** override ([below](#zf6hp)). Its tooltip
says *Diagnostics and recovery. Nothing here is needed for an ordinary read,
write or backup.* It is shown only for the units that have something in
it (EDC16, EDC15, Cluster, ABS and ZF6HP). Whether it is open is remembered.
While EDC15 **Boot mode** or **Probe the bus instead of reading** is ticked,
or the ZF6HP **Transport:** is not **Automatic**, the section will not
collapse, so a setting that changes what Read or Write does is never out of
sight.

## EDC15

- **Read internal ROM** reads the processor's own 32 KB boot ROM over the
  normal K-line session (no boot pin, no boot mode). It is checked for the
  silent every-other-byte-doubled failure before it is saved, and you are
  warned to re-read if the dump looks suspect — a bad read of this ROM can
  look like a good one. With a project in front you are asked whether to
  keep the dump in it, as an attachment, or save it as a file. Greyed out
  while **Boot mode** is ticked.
- **Boot mode** is on the [EDC15 page](edc15.md#boot-mode).

## EDC16

All of these are **non-destructive** — they read or probe, nothing is erased
or written — unless the name says otherwise. The first two are on the main
button row; the rest are in the **Advanced** section, in three rows (the
probes, the RAM / immo / EEPROM reads, and the GPT test with its options).

![The Advanced section with EDC16 selected](../images/flash-edc16.png)

| Button | What it does |
|---|---|
| **Full read...** | Reads the whole external and internal flash into a project. See [EDC16](edc16.md#full-read). |
| **Write blocks...** | **Destructive.** Erases and rewrites the ticked blocks. See [EDC16](edc16.md#blocks-and-write-blocks). |
| **Boot read-level probe** | Tries to read the boot region at two security levels to find which, if any, unlocks it. Reads no data. |
| **Checksum-range probe** | Asks the ECU which address ranges its checksum routine will verify. |
| **Security level probe** | Sends seed requests with no key, to list which security levels the ECU has. Nothing unlocks. |
| **Read RAM / coding (probe)** | Reads a RAM address (default the coding struct 0x7FE9E4) at each level held. You can enter another address and length. |
| **Read immo/coding (L3)** | Reads the live coding and immobiliser state. |
| **Find EEPROM (L3)** | Searches RAM for the EEPROM (or its shadow). |
| **GPT crank/cam test** and **GPT → Find EEPROM** | With a Scanmatik SM2, drive crank/cam signals so the ECU believes it is running, then (the second) search for the EEPROM. **Crank only**, **Gap pattern (60-2)** and **Force EEPROM sweep** tune them. |

The three probes ask before they start and **Read RAM / coding (probe)** asks
for the address; these four show their results in a dialog. **Read
immo/coding (L3)**, **Find EEPROM (L3)** and the GPT buttons report on the
status line only. Over CAN only **Full read...**, **Write blocks...** and
those first four probes run; the others need K-line. All of them are for reverse-engineering, not for tuning. Leave them
alone unless you are following a specific procedure.

## Cluster and ABS

With **Cluster** or **ABS** selected, the **Advanced** section has two
troubleshooting aids. Neither is offered for any other unit.

- **Log CAN traffic to a file** writes every CAN frame of the next read to a
  `.log` file you choose, for inspection (it is a file, not part of a
  project). It does not change the read. (For
  the Cluster this is also how a security exchange would be captured.)
- **Probe the bus instead of reading** turns **Read flash** into a probe: it
  sends one TP 2.0 setup to the unit's address at the chosen CAN rate and
  reports every frame seen for about two seconds — to find out whether the
  unit answers, and on which CAN id. Nothing is read or changed. Set the CAN
  rate to **100000** to test the C6 comfort CAN. While it is ticked the
  **Advanced** section stays open.

## ZF6HP

![ZF6HP with Advanced open: the Transport override](../images/flash-zf6hp-transport.png)

**Transport:** chooses how the flasher talks to a ZF 6HP:

| Choice | Read flash | Write flash |
|---|---|---|
| **Automatic** (default) | TP 2.0 first; UDS only when nothing answers over TP 2.0 | TP 2.0 or UDS, from the open file |
| **TP 2.0 (6HP19 / 6HP26)** | TP 2.0 only | TP 2.0 |
| **UDS (6HP28)** | UDS only | UDS |

Leave it on **Automatic** unless a write's confirmation says the transport
was guessed and the guess is wrong, or you know the automatic read picks the
wrong way for your TCU. The choice is remembered. See
[Transmissions](transmissions.md#which-transport) for how Automatic decides.
