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

## How the 1 MB chip is addressed -- what is and is not known

Disassembling the IROM (`ovtsuite/c166dis.py`) shows a **global external
chip-select** and firmware that only addresses segments <= 0x0F, reading code and
calibration from the CPU window **0x80000-0xFFFFF** (the chip's top 512 KB on a
1 MB part; the whole chip on a 512 KB part). That window is bench-proven.

An earlier version of this note claimed the programming window **0x200000**
also exposes the whole chip for *reading*. **That was wrong for 512 KB ECUs and
is unverified for 1 MB ones.** A bench log of a 512 KB ECU showed every read at
0x2xxxxx returning an open-bus pattern (`<segment> 00` repeated), not flash:
0x200000 is a programming (erase/program) window. A detector that trusted it
called that 512 KB ECU "1 MB" and produced a 1 MB file of pattern.

What is therefore **not known**: how to *read* the lower 512 KB of a 1 MB part.
It is not reachable at 0x080000 (that shows the upper half) and 0x2xxxxx did not
read as flash on the ECU tried. The size probe now logs what each window returns
so a bench run on a 1 MB ECU can answer this.

## Implementation

`FlashLayout` captures everything that differs: `size`, the erase-sector table
(`SECTORS` / `SECTORS_1M`), the boot block, and the read/write windows. The
write/erase/checksum path already addressed flash at `SECTOR_WRITE_BASE`
(0x200000), so it generalises to 1 MB by extending the sector table and the
range only. The whole-chip read loop reads at `layout.read_base` (0x080000 for
512 KB — byte-identical to before; 0x200000 for 1 MB so the lower half is
reachable).

### Size detection -- `Edc15Flash.detect_layout()`

The A19 aliasing idea is sound (a 512 KB part does not decode A19, so addresses
differing only in A19 alias), but it needs a *readable* window that reaches both
halves, so the probe first **proves the candidate window is real flash**: the
candidate (0x2F0000) must read identically to the proven 0x080000 window at the
matching chip cell. If it does not, 0x200000 is not a read window on this ECU
and the part is taken to be 512 KB (the bench-proven behaviour), with the raw
probe bytes logged. Only when the window is proven does it test A19. Sector
loader: compared on the ECU with CmdB2; flash loader: short reads.
`read_flash` also refuses an image with fewer than 64 distinct byte values (an
unmapped read) instead of handing back a bogus file.

Consequence: until a 1 MB ECU is benched, a 1 MB part whose lower half is not
reachable is treated as 512 KB and yields only its upper half.

## Not covered

* **Run on the head unit** (`staged.py`) bakes 512 KB geometry into the job and
  runs autonomously with no live detection, so it refuses a 1 MB image; use a
  direct connection for 1 MB.
* **Boot mode** (`edc15boot.py`) is still 512 KB; it configures its own bus and
  would be extended separately.
* **Checksum correction** of a 1 MB image is up to the EDC15 module; without a
  correction routine the write proceeds uncorrected (as for 512 KB) or, if the
  module recognises the layout but cannot read its ranges, is refused.
