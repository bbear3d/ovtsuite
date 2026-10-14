# Container formats: Dimsport .dim, and .MPC internal-flash images

Two related container formats, both reverse-engineered from real
BDM-tool dumps (`stock.dim` / `stock-1.dim` / `stock2.dim` / `test.dim`,
03G906016LQ and 03G906016N / EDC16U31) and a write/readback known-
plaintext pair for the `.MPC` layer. Modules: `ovtsuite/dim.py` and
`ovtsuite/mpc.py`. CLI: `ovtsuite dim-info` / `dim-extract` /
`dim-extract-mpc` / `dim-extract-eeprom` / `dim-build`, and `ovtsuite
checksum` (accepts a compressed `.MPC` directly). GUI: `TunerSession`
opens and saves a `.dim` directly -- see below. A `.dim`'s own chunk 1
turns out NOT to nest `.MPC`'s framing on any real dump checked (see
below); the two formats otherwise share nothing structurally, despite
both wrapping their content in obfuscation + zlib.

## `.dim` (Dimsport ECU dump container)

Derived from `DimExp.dll` (Delphi, exports `Init` / `Insert` / `Extract` /
`GetDimBuffer`). A flat chain of chunks, no global header, no index:

```
off   size  field
0x00    4   magic  "DS  " or "CMD "        (DimExp accepts both)
0x04    2   index  u16 LE   1,2,3,4...
0x06    2   type   u16 LE   0 = raw, 1 = obfuscated
0x08    4   A      u32 LE   payload length
0x0C    4   B      u32 LE   = A + 8
0x10    4   C      u32 LE   32-bit sum of the DE-OBFUSCATED payload
0x14    4   t1     u32 LE   key for the substitution table
0x18    4   t2     u32 LE   key for the byte-order scramble
0x1C    A   payload
```

Next chunk at `off + 0x14 + B == off + 0x1C + A`.

On every real EDC16U31 dump checked (5 so far): chunk 1 -> 0x81000
internal flash, already the plain image once this container's own layer
is decoded -- no further unwrap needed. (An earlier version of this doc
assumed chunk 1 needed a second decode through the `.mpc` layer below;
that assumption didn't hold on any real file -- `ovtsuite.mpc.is_encoded
is false on what chunk 1 decodes to.) chunk 2 -> 2 MiB external flash
(plain, once this container's own obfuscation is removed -- this is the
image `TunerSession` tunes); chunk 3 -> 4 KiB EEPROM; chunk 4 -> a
plain-text metadata line (type 0, not compressed) -- on a real dump this
is the VIN/model/date/coding line the tool recorded at read time. One dump
(`BRM Malone.dim`) has no chunk 1 at all, starting at index 2 -- an
unexplained variant, not yet handled specially.

### Payload obfuscation (type 1)

Two layers over a plain zlib stream, both keyed by the chunk's own t1/t2
and driven by Delphi's LCG:

```
RandSeed := RandSeed * $08088405 + 1        (mod 2^32)
Random(N) = (N * RandSeed) shr 32           (unsigned)
```

1. **Substitution table** -- `RandSeed := t1`; start from the identity
   table `P[0..255]`; do 1000 times: swap `P[Random(255)]` and
   `P[Random(255)]`. (`Random(255)` yields 0..254, so index 255 is never
   picked -- a quirk of the original tool, reproduced here.) The decode
   table is `P^-1`.
2. **Byte-order scramble** -- `RandSeed := t2`; `n := 2 * len`; generate
   pairs `a[i] := Random(len)`, `b[i] := Random(len)` for `i = 1..n`, in
   that interleaved order. Encode applies `swap(buf[a[i]], buf[b[i]])` for
   `i = 1..n`; decode replays them for `i = n..1`.

Decode: unswap (`i = n..1`), then `buf[j] := Pinv[buf[j]]`.
Encode: `buf[j] := P[buf[j]]`, then swap (`i = 1..n`).

`C` is the byte sum of the buffer at that midpoint -- after both layers
are removed, before inflate. The result is a zlib stream; inflated, it is
a 4-byte little-endian length followed by that many bytes of payload.

### Round-tripping

`ovtsuite.dim.replace_external_flash(chunks, new_bin)` /
`replace_internal_flash(chunks, new_image)` return a **new** chunk list
with just that one chunk re-encoded (same index/type/t1/t2/magic, so it
stays decodable by DimExp) -- every other chunk is carried through
byte-for-byte from the original parse, not decoded and re-encoded. This is
what `TunerSession` uses: open a `.dim`, chunk 2 becomes the binary you
tune, and `save()`/`save_as()` to a `.dim` path repacks it with internal
flash and EEPROM untouched. Save As to a `.bin` path instead exports just
the tuned external flash as a plain image, no container.

## `.MPC` (internal-flash image, BDM/Dimsport export)

The EDC16U31's internal flash (the MPC5xx program + library area, 0x81000
bytes: 512 KiB flash image + 4 KiB of `0xFF`) is exported/imported as:

```
file = XOR(0xD9, zlib_stream || trailer[4])
```

The whole file -- compressed stream and the 4 trailing bytes alike -- is
one uniform XOR-0xD9 layer (a real file's trailer, e.g. plain `0xDD`, is
stored on disk as `0x04`). De-XOR'd, the file starts `78 9C`, a standard
zlib header (CM=8/deflate, CINFO=7/32K window); the payload is a plain
RFC1950 stream, RFC1951 deflate inside with the trailing Adler-32. No
proprietary compression, no proprietary cipher. The 4 trailer bytes'
meaning is not identified and they are not needed to decode; `decode()` /
`encode()` carry them through unchanged so a re-encoded file stays
interchangeable with the tool that produced the original.

Verified against `03G906016KN.MPC` / `03G906016KN_acpacker_patch.MPC`
(`Reference/Binaries`) -- both are already-decoded 0x81000 images, and
both round-trip losslessly through `encode()`/`decode()`.

### The trailer bytes: what's been ruled out

10 real compressed `.mpc` samples (`Reference/Binaries/Encoded-MPC/`) were
checked for a formula -- they reduce to 6 distinct files once exact
duplicates (identical image, identical compressed stream, identical
trailer, just saved under different names) are collapsed. None of the
following matched the trailer, in either byte order, computed over either
the plain image or the de-XOR'd compressed stream (with or without the
trailer's own bytes): CRC-32 (the zlib/PKZIP variant, BZIP2/MPEG-2,
CRC-32C, CRC-32K, CRC-32Q, JAMCRC), Adler-32, Fletcher-32, a raw 32-bit
byte sum (the same style as a `.dim` chunk header's own `C` field), or the
trailer read as a Unix or DOS-packed timestamp. Two same-image pairs
(`MPC563-4-Flash-2.mpc` / `stock-038997016N.mpc`, and the image shared by
`stock.dim` / `stock-1.dim`'s chunk 1) do confirm the trailer is
reproducible for identical content rather than per-session random data --
so it's a function of *something* -- but what remains open. `mpc.encode()`
defaults to an empty trailer, which round-trips fine through this library
but isn't known to satisfy a real BDM/Dimsport tool; pass a real one via
`mpc.encode(image, trailer=...)` or `ovtsuite.dim.internal_flash_mpc(chunks,
trailer=...)` if you have one to preserve.

Decoded image layout:

```
0x00000 - 0x7FFFF   512 KiB MPC5xx flash image (PowerPC, vector table at 0)
0x80000 - 0x80FFF   4 KiB, 0xFF filled
```

That's `ovtsuite.edc16.checksum16.SIZE_KN` -- see `docs/CHECKSUMS.md` for the
program-area checksum this image carries.

### Usage

- **CLI** -- `ovtsuite checksum <file.mpc> --fix` accepts either a plain
  0x81000 image or a compressed `.MPC`; a compressed input is decoded,
  corrected, and re-compressed (trailer preserved) automatically.
- Standalone decode/encode: `tools/mpcdecode.py decode|encode|info` (a thin
  CLI over `ovtsuite/mpc.py`).
- Inside a `.dim`: chunk 1's payload, once the container's own
  obfuscation is removed, is already the plain image on every real dump
  checked -- not further wrapped in this `.mpc` framing, despite that
  being the original assumption here. `ovtsuite.dim.internal_flash(chunks)`
  handles both cases (a no-op if there's nothing further to unwrap).
  `ovtsuite.dim.internal_flash_mpc(chunks)` goes the other way -- pulls
  chunk 1 back out re-compressed into this `.mpc` form, e.g. to export a
  loose `.mpc` file from a `.dim`.

## Standalone `.dim` CLI

- `ovtsuite dim-info file.dim` -- list chunks (index, size, checksum, t1/t2,
  a content preview; chunk 1 also notes `[plain]` or `[mpc-wrapped]`).
- `ovtsuite dim-extract file.dim outdir/` -- writes each chunk as a loose
  file (`_1.mpc`, `_2.bin`, `_3.e2p`, ...) plus `dim.json` (chunk metadata,
  needed by `dim-build`) and, for chunk 1, an extra `_1_decoded.bin` with
  the internal flash fully decoded (a convenience -- `dim-build` reads the
  `.mpc` file back, not the decoded one).
- `ovtsuite dim-extract-mpc file.dim out.mpc [--compressed]` -- just the
  internal flash, as the plain 0x81000 (516 KB) image by default (the same
  form the Reference/Binaries KN.MPC samples and `ovtsuite checksum` use),
  or the compressed XOR-0xD9/zlib `.mpc` form (`--compressed` -- re-
  compressed fresh, with no trailer, unless chunk 1 was already in that
  form); a one-shot alternative to `dim-extract` when that's all you need.
- `ovtsuite dim-extract-eeprom file.dim out.e2p` -- just the EEPROM chunk.
- `ovtsuite dim-build outdir/ new.dim` -- repacks a `dim-extract`ed
  directory back into a `.dim`, self-checking the result.

`tools/dimtool.py` is the same thing as a standalone script (a thin CLI
over `ovtsuite/dim.py`), for extract/rebuild work outside of
`ovtsuite`'s own CLI.

## What does and doesn't understand `.dim` directly

`TunerSession` (so the GUI, `ovtsuite tune`, `ovtsuite export`, and anything
else that opens a session) opens and saves a `.dim` transparently -- see
above. The read-only diagnostic commands that load a binary directly
(`ovtsuite identify`, `scan`, `coverage`, `diff`) do not yet sniff `.dim`
themselves; run `dim-extract` first and point them at the resulting `_2.bin`
(external flash) instead.

## OVT native containers: `.edc16`, `.edc15`, `.6hp`, `.hvac`

One shared compressed block-directory format (`ovtsuite/container.py`);
the four extensions differ only by their 8-byte magic and the platform
they imply. `.edc16` stays byte-compatible with the original format.

| ext      | magic        | platform          |
|----------|--------------|-------------------|
| `.edc16` | `OVTEDC16`   | EDC16             |
| `.edc15` | `OVTEDC15`   | EDC15             |
| `.6hp`   | `OVT6HP\0\0` | ZF 6HP            |
| `.hvac`  | `OVTHVAC3`   | HVAC (v3)         |

The legacy `.hvac` (`OVTHVAC\x01`, v1/v2) was **not** compressed. It is
still read, and is upgraded to the compressed v3 on the next save.

Layout: magic, header (version 1, block count), then 28-byte directory
entries (name, type, comp, flags, address, orig_size, comp_size, crc32),
then the blocks. Each block is zlib-compressed, or stored raw when
compression would not shrink it. Block types: EXTERNAL=1 (main image),
BOOT=2, PROGRAM=3, CAL=4, MPC=5, EEPROM=6, LOG=7, DEFN=8, META=9 (JSON),
LOADER=10. Every flavour can carry EEPROM and MPC blocks whenever they
are available, so one file holds everything about a car's ECU.

### Definition block (DEFN)

`ovtsuite/defn.py` stores a snapshot of the finished definition: the
entity table, `by_key`, and every public session attribute, in a tagged
JSON vocabulary (never pickle, so opening a shared file cannot run code;
classes resolve only from `ovtsuite.*`). On open the snapshot replaces
the matcher and tree build. It is used only when all of these agree with
the current file and code: format version, platform, image length,
`code_digest`, `data_digest`, entity field layout, and a structural
`mask_digest` (the image minus parameter cells and axis breakpoints, so
calibration edits don't invalidate it). Any mismatch falls back to the
normal build; `session.defn_state` says `used`, `none`, or the reason.
`code_digest` covers only what builds a model: `defn.MODEL_VERSION`,
the base files in `defn.MODEL_FILES`, and the platform's module (id,
version, source). The app version and the rest of the app are left out,
so a release that does not touch model building keeps stored definitions
valid; a frozen build (no source) relies on `MODEL_VERSION` and the module
version alone. Bump `MODEL_VERSION` when model-building semantics change
(`tests/test_defn.py` records the `MODEL_FILES` hash to prompt it).
Cost: EDC15 files are roughly twice the raw image size. Inspect with
`ovtsuite container-info FILE`.

### Version history and backups

Every save of a native container logs one checkpoint (`VERSION` record in
the `LOG` block): the byte ranges that changed in the main image, and --
under `"regions"` -- those that changed in the EEPROM / MPC internal-flash
blocks (the hex editor's edits). Revert, merge, and "state at version" all
cover both. Because the history is inside the file, saving over an existing
native container makes no `.bak`; a plain image saved over itself, or a
legacy `.hvac` upgraded in place, still keeps one.
