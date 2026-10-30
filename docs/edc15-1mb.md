# EDC15 1 MB flash support

The EDC15 flasher (`ovtsuite/flash/edc15.py`) drives both the 512 KB and the
1 MB Bosch EDC15P variants from one code path. This note records what the 1 MB
part is and why the implementation is the small thing it is, because the
conclusion is not obvious and an earlier guess (a bank-switch GPIO) was wrong.

## The parts

* **512 KB** — Am29F400BT (top boot), e.g. Audi A6/C5 `4B*907401*` 2.5 V6 TDI.
* **1 MB** — Am29F800BT (top boot), e.g. Audi A4/B6 `8E0907401*` 2.5 V6 TDI.

Both are EDC15P on an Infineon C167, same V4.1 firmware generation, and they
run the **same** C167 internal boot ROM (IROM): 16-bit sum 0x3796, reset vector
`JMPS 00H,0374H`. The KWP2000 boot stub lives in the chip's top 16 KB sector —
0x7C000 on the 512 KB part, 0xFC000 on the 1 MB part.

## How the flash is addressed (bench-proven on a 512 KB ECU)

A survey of a 512 KB ECU (bench log) shows the flash answers reads at **every
external CPU address except the 0x200000-0x3FFFFF block**, which returns an
open-bus pattern (`<segment> 00` repeated): that block is the write-only
programming window the loader erases/programs through. Everywhere else the chip
simply sees the CPU address lines:

| CPU address read | 512 KB ECU shows |
|---|---|
| 0x070000, 0x170000, 0x470000, 0x870000 (A19 = 0) | the flash (chip 0x70000) |
| 0x0F0000, 0x1F0000, 0x8F0000 (A19 = 1) | the same flash cell |
| 0x270000, 0x2F0000, 0x370000, 0x3F0000 | open bus (programming block) |

So `chip_byte = cpu & (size - 1)`. A 512 KB part ignores A19 (all of the above
alias). A 1 MB part decodes it: A19 = 0 windows show the **lower** half, A19 = 1
windows the **upper** half. The firmware reads from 0x080000 (A19 = 1), which is
why it only ever sees the upper half of a 1 MB part. Boot mode already reads at
0x800000 (A19 = 0).

An earlier version of this note said the 0x200000 programming window could be
read; it cannot, and a detector built on that called a 512 KB ECU "1 MB".

**Inferred, not yet bench-proven on a 1 MB ECU:** that A19 reaches the 1 MB chip
as a CPU address line (the firmware shows no bank-switch GPIO), so that
**0x100000-0x17FFFF** reads the lower half, and that the 0x200000 programming
block reaches the full 1 MB (lower half 0x200000-0x27FFFF, upper half
0x280000-0x2FFFFF). The upper half is read through the firmware's own 0x080000
window, which is proven.

## Implementation

`FlashLayout` captures size, erase-sector table (`SECTORS` / `SECTORS_1M`), boot
block and windows: 512 KB reads at 0x080000 (unchanged, bench-proven). A 1 MB part
reads its lower half at 0x100000 and its upper half at 0x080000 (`cpu_read()`
picks the window; no read packet or checksum span straddles the two). Both sizes
erase/program through 0x200000 + chip offset.

### Size detection -- `Edc15Flash.detect_layout()`

Signature-free test of whether CPU A19 reaches the chip, using only windows
proven to alias on a real ECU (`A19_HI` 0x0F0000/0x1F0000/0x8F0000, `A19_LO`
0x170000/0x470000/0x870000):

1. the A19=1 windows must read identically and look like flash;
2. the A19=0 windows must read identically to each other;
3. A19=0 equal to A19=1 -> 512 KB; different -> 1 MB.

Anything inconclusive keeps 512 KB (the bench-proven behaviour) and logs a window
survey. A 1 MB part on which A19 never reaches the chip is therefore also taken
as 512 KB. Sector loader: compared on the ECU with CmdB2 (only 16-bit results
cross the wire); flash loader: short reads. `read_flash` also refuses an image
with fewer than 64 distinct byte values.

## Not covered

* **Run on the head unit** (`staged.py`) bakes 512 KB geometry into the job and
  runs autonomously with no live detection, so it refuses a 1 MB image; use a
  direct connection for 1 MB.
* **Boot mode** (`edc15boot.py`) is still 512 KB; it configures its own bus and
  would be extended separately.
* **Checksum correction** of a 1 MB image is up to the EDC15 module; without a
  correction routine the write proceeds uncorrected (as for 512 KB) or, if the
  module recognises the layout but cannot read its ranges, is refused.
