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

## How the 1 MB chip is addressed (no bank switching)

Disassembling the IROM (with `ovtsuite/c166dis.py`) shows it sets up a **global
external chip-select** (`BUSCON0`) and jumps into flash — its interrupt vectors
target segment 0x0F and its firmware entry segment 0x09. The running firmware
only ever addresses segments ≤ 0x0F, i.e. a 1 MB (20-bit) space, and reads its
code and calibration from the flash in the CPU window **0x80000–0xFFFFF**.

Because the chip-select is global and the chip's address lines are driven by the
CPU bus, `chip_byte = cpu & (size - 1)`. The programming window the loader
already uses, **0x200000**, therefore exposes the **whole chip linearly**:

| CPU window | chip bytes reached |
|---|---|
| 0x080000–0x0FFFFF | the chip's top 512 KB only (A19 = 1) |
| **0x200000–0x2FFFFF** | **the entire chip** (file offset = CPU − 0x200000) |

So a 1 MB part needs exactly one extra CPU address line (A19) compared with a
512 KB part, and that line is a real bus line, **not** a GPIO bank-select. A
512 KB part simply does not decode A19, so two addresses differing only in A19
alias to the same cell — which is what size detection keys on. The firmware uses
only the top 512 KB (via the 0x80000 window); the lower 512 KB is spare on these
cars (blank, or legacy map tables), which is why it is reachable at
0x200000–0x27FFFF but the firmware never goes there.

## Implementation

`FlashLayout` captures everything that differs: `size`, the erase-sector table
(`SECTORS` / `SECTORS_1M`), the boot block, and the read/write windows. The
write/erase/checksum path already addressed flash at `SECTOR_WRITE_BASE`
(0x200000), so it generalises to 1 MB by extending the sector table and the
range only. The whole-chip read loop reads at `layout.read_base` (0x080000 for
512 KB — byte-identical to before; 0x200000 for 1 MB so the lower half is
reachable).

### Size detection — `Edc15Flash.detect_layout()`

A purely structural "is A19 decoded?" probe, with no content signature: compare
the chip's top sector region against its lower-half alias in the 0x200000
window. Identical ⇒ A19 not decoded ⇒ 512 KB; different ⇒ 1 MB (a 512 KB part
cannot produce a difference, so a difference is conclusive). When the sector
loader is running the comparison is done **on the ECU** with its checksum
command (CmdB2) — only the two 16-bit results cross the K-line, so detection is
independent of connection speed. With the whole-chip read loader (reads only),
it falls back to a short read-and-compare.

Detection runs automatically at the start of a read/write; the file size must
then match the detected flash or the write stops before erasing anything.

## Not covered

* **Run on the head unit** (`staged.py`) bakes 512 KB geometry into the job and
  runs autonomously with no live detection, so it refuses a 1 MB image; use a
  direct connection for 1 MB.
* **Boot mode** (`edc15boot.py`) is still 512 KB; it configures its own bus and
  would be extended separately.
* **Checksum correction** of a 1 MB image is up to the EDC15 module; without a
  correction routine the write proceeds uncorrected (as for 512 KB) or, if the
  module recognises the layout but cannot read its ranges, is refused.
