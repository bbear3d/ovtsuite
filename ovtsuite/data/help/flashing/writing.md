---
title: Writing
order: 30
---
# Writing to a control unit

Read [Before you start](before-you-start.md) first. A write erases parts of
the unit's flash (or EEPROM) and programs them again from your binary.

## Steps

1. Open what you want to write: in the main window, open the project's
   branch in a tab (or keep the tab of a read you edited) and make that tab
   the **active** one; in the OVT Flasher, use **Open file...** — a binary,
   or an `.ovt` project, which asks which binary, branch and version to
   write when it has more than one.
2. Open the flasher and check the **ECU** entry. It is chosen from the binary
   when its type has a flasher entry; check it anyway. (For **ZF6HP**
   the file also decides between TP 2.0 and UDS — see
   [Transmissions](transmissions.md#which-transport).)
3. Choose **Transport**, the cable, and for K-line the **Speed**.
4. Click **Write flash** (EDC15 EEPROM: **Write EEPROM**; EDC16 boot,
   program or MPC blocks: **Write blocks...**; see the unit's page for the
   others).
5. If the tab has unsaved changes in its project, you are asked first:
   **Save, then write** (the version written is then in the project's
   history), **Write without saving** (the changes are sent as they are on
   screen) or **Cancel**.
6. The checksums are corrected before anything connects to the unit. If
   they cannot be corrected (the module that does it is not installed, or it
   cannot read this file's checksum layout), you are asked whether to write
   the file uncorrected; if the correction fails, the write does not start
   and the message says why — see [Checksums](#checksums).
7. Read the confirmation. It says what will be erased and reprogrammed, for
   example *This will ERASE and REPROGRAM the ECU's calibration flash. An
   interrupted or wrong write can brick the ECU.* Answer **Yes** only if that
   is what you mean to do.
8. Wait. The status line shows each stage (connecting, erasing, writing,
   verifying), the bar the progress, the timer the elapsed time.
9. When the final message appears, do what it says (for an EDC16: ignition
   off for 5 seconds).

## Marked as flashed

After a successful write — flash, block write, flash + EEPROM, EEPROM write,
6HP28 UDS write or cluster write — the version written is marked in its
project as **last flashed**. The Project panel shows *⚡ flashed <date>* on
that version, and the set's and the tab's tooltips say *Last flashed <date>
(<branch>, saved version)*, or *unsaved edits* when it was written without
saving.

The OVT Flasher marks the version in the `.ovt` it wrote from — unless
OVT-Suite has that project open, which it says.

## Only what changed is written

A write does not blindly reprogram everything. Before erasing a part of the
flash, the flasher asks the unit to check that part against the file (the
unit computes a checksum over what it holds). A part that already matches is
skipped; only the parts that differ are erased and written.

| ECU | Parts | Pre-check |
|---|---|---|
| **EDC16** | The two calibration blocks | The ECU's own checksum routine over each block |
| **EDC15** | The 11 flash sectors | A checksum of each sector, computed by the loader in the ECU |
| **ZF6HP** — 6HP19 / 6HP26 (TP 2.0) | The two calibration areas | The TCU's own checksum routine over each area |
| **ZF6HP** — 6HP28 (UDS) | The calibration block, and the program blocks when the file carries a full valid program set | None: every block is written (the TCU only checks a block after it is written) |
| **Cluster (VDO K-line)** | All 128 EEPROM blocks of 16 bytes | None: nothing is read from the cluster first, and every block is written |
| **EDC15 EEPROM** | Bytes | The ECU's EEPROM is compared first |

If nothing differs, the write ends without erasing anything (for example
*Block 7 unchanged (checksum match) - skipped.*).

**Force full write** turns the pre-check off and rewrites every part the
write covers. Its tooltip says what that is for the selected unit, e.g.
*Rewrite every block, even the ones that already match.* (EDC16) or *Rewrite
all 11 sectors, even the ones that already match.* (EDC15). You do not need it to repair a part that was interrupted — a part
that does not match is written anyway. It is there for when you have a reason
not to trust the unit's own check.

## Verification

After each part is written, the unit is asked to check it again:

- **EDC16** and **ZF6HP** (6HP19, 6HP26 and 6HP28) — the unit's checksum routine over
  the block or area just written.
- **EDC15** — the sectors are verified after programming.
- **EDC15 EEPROM** and the **VDO cluster** — what was written is read back
  and compared.

> **EDC16:** if the ECU rejects or does not confirm the check after a block,
> the flasher logs a warning in the status line (*post-write checksum
> MISMATCH ... read back before trusting this write*) but the write still
> finishes. The status line moves on, so the warning is easy to miss. After
> an EDC16 write, read the unit again and compare it with your file before
> driving.

A read-back comparison is a good habit for every unit: read after writing,
compare the read with the tab you wrote (**File > Compare with open tab**),
and check there are no differences in the region the write covers.

## Checks before anything is erased

The flasher refuses, before connecting, a file that cannot be right:

| ECU | Refused when |
|---|---|
| **EDC16** | The file is not 512 KB, 1 MB or 2 MB, or its last 512 KB is not a recognisable EDC16 calibration. A 512 KB or 1 MB file is treated as the calibration region. |
| **EDC15** | The file is not exactly 512 KB, or not recognised as an EDC15 (V4.1) image, or (with the EDC15 module) its checksum ranges cannot be read from the file. |
| **ZF6HP** — 6HP19 / 6HP26 (TP 2.0) | The file is smaller than 1 MB. |
| **ZF6HP** — 6HP28 (UDS) | The file is neither a full 2 MB image nor a 512 KB calibration. |
| **Climatronic** | The file is not a `.sgo` / `.hvac` the flasher can parse, or it carries no flash loader (a raw `.bin` does not). |
| **Cluster (VDO K-line)** | The open file is not a recognised VDO Mk4 cluster EEPROM layout. It is not compared with the cluster: a file of another layout, or from another car, is not caught. |

None of these checks prove the file was made for this exact unit — see
[Before you start](before-you-start.md).

## Checksums

An edited file must carry a correct checksum, or the unit may reject it — or
stay in its boot mode until a correct file is written. Before a write, and
before anything connects to the unit, the flasher corrects the checksums in
the copy it sends **when the module for that unit is installed**, exactly as
**Save** does (on an EDC16 the KWP2000 content digest too). The tab and the
project are not changed by this. If the correction fails, the write does not
start: the message says why.

| ECU | Corrected by | Notes |
|---|---|---|
| **EDC16** | EDC16 module | |
| **EDC15** | EDC15 module | The write is refused rather than written with guessed checksum ranges. |
| **ZF6HP** — 6HP19 / 6HP26 (TP 2.0) | ZF 6HP module | The calibration checksum; also the program-area checksums when **Tools > Options... > Projects and saving > ZF 6HP: also correct the program-area checksums** is on. For a layout the module cannot read you are asked first (**Checksum cannot be corrected**); **No** cancels. |
| **Cluster (VDO K-line)** | VDO Cluster module | |
| **ZF6HP** — 6HP28 (UDS) | ZF 6HP module, where it can read the layout | Corrected when the module can read this file's checksum layout; the confirmation says which. Otherwise the file is sent exactly as it is and the confirmation says the checksum is neither checked nor corrected. |
| **Climatronic** | 4F/4L HVAC module | The internal flash checksum, as **Save** does. For a firmware whose checksum the module has not mapped you are asked first. |

When the module is missing you see **Checksum will not be corrected**; when
the module is there but cannot read this file's checksum layout (ZF6HP over TP 2.0) or
has no checksum mapped for its firmware (Climatronic), **Checksum cannot be
corrected**. Both explain that the file will be written exactly as it is. **No** (the default)
cancels; **Yes** writes it uncorrected. Answer **Yes** only for a file you
know is already correct — an unmodified read, for example. See
[Modules](../modules.md) to install one.

If an EDC16 still refuses a written block — its checksum routine answers
"conditions not correct" for 20 seconds after the block was written — the
write stops with that message rather than going on to the next block.
Correct the checksums (**Tools > Verify checksums**, or **Save**) and write
again; the ECU stays in programming mode until a correct file is written.

## Cancel during a write

**Cancel** asks the job to stop; the status line shows **Stopping...**. How
soon it stops depends on the unit: some finish the part they are writing
first, others (the 6HP19 / 6HP26, for one) stop in the middle of it. Either way,
stopping a write leaves the unit with some parts new and some old — or with a
part erased and not yet written again. Do not cancel a write to save time. If you
did, or the write failed, see [Backups and recovery](backups-and-recovery.md).

## Messages at the end

| Message | Meaning |
|---|---|
| **Write complete in m:ss.** | All parts were written (or skipped as already matching). EDC16 adds *Turn off the ignition for 5 seconds.* |
| A dialog titled **Flashing** with an error | The write stopped. The text says where. Read [Backups and recovery](backups-and-recovery.md) before switching anything off. |
| An error dialog saying **Stopped.** (or *write cancelled*) | You pressed **Cancel** and the job stopped. If it was a write, see [Backups and recovery](backups-and-recovery.md). |
