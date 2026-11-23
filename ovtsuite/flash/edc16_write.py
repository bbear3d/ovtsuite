# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 block write: boot, program, MPC and calibration, over K-line or CAN.

The blocks are those of :mod:`.edc16_blocks`; each of their write spans is one
ordinary programming cycle, the one ODIS runs: RequestDownload (0x34) -> erase
(0x31 C4) -> one continuous TransferData (0x36) -> transferExit (0x37) ->
checksum (0x31 C5). Blocks run in the ODIS order (boot -> program -> MPC ->
calibration) in one programming session.

``arm=False`` is a dry run: the session is opened and each RequestDownload
issued, but nothing is erased or written. ``arm=True`` erases and writes; a
failed or interrupted erase can leave the ECU unbootable (recover via BDM).

How the block sizes and the one-RequestDownload-per-block rule were found is
in ``docs/EDC16_FLASH_BLOCKS.md``.
"""
from . import edc16_blocks as B
from .edc16 import encrypt, fast_prime_from_image, StopRequested, FULL_SIZE

#: Erase the program block one 256 KB sector at a time (after its single
#: RequestDownload) instead of in one 0x31 C4. Off: one erase, as for cal.
_PROGRAM_SECTOR_ERASE = False


def available_blocks(image, mpc_image=None) -> dict:
    """{key: bool}: the blocks the source holds data for (see edc16_blocks)."""
    return B.available(image, mpc_image)


def _project(image):
    """The dataset project code ('P350', 'P475', ...) or None."""
    try:
        from .edc16 import image_project
        return image_project(image)
    except Exception:
        return None


def _dataset(image):
    """The full dataset id ('P475AAXE', 'P350U8A9', ...) or None."""
    try:
        from ..fileinfo import ident16
        return (ident16(bytes(image)).get("dataset_id") or "").upper() or None
    except Exception:
        return None


def _family_extent(image):
    """The proven program extent for this image's project/generation, or None
    (unproven project, unreadable id, or a blank program area)."""
    proj = _project(image)
    ext = B.PROGRAM_EXTENT_BY_PROJECT.get(proj)
    if ext is None:
        return None
    if all(b == 0xFF for b in bytes(image[B.PROGRAM_START:B.PROGRAM_END])):
        return None                               # nothing to size from
    ds = _dataset(image) or ""
    if ds[4:6] in B.PROGRAM_SHORT_GENERATIONS:
        return B.PROGRAM_END
    return ext


def program_extent(image, declared_end=None) -> int:
    """End (exclusive) of the program data to write from this source.

    ODIS writes the block's declared data length: 0x40000-0x9FFFF for a P350U8,
    0x40000-0x17FFFF for a P475 (whose .sgo section declares 0x140000). So the
    container's declared end is used when there is one; otherwise the image is
    scanned to its last non-0xFF byte, rounded up to 0x10000. Never less than
    the ODIS extent, never past the program descriptor.

    Real data in 0xA0000-0xBFFFF (a P350U8 stock image has some) is ignored
    unless data also exists at or above 0xC0000: ODIS never writes it and the
    ECU's 0x31 C5 does not verify it, so a write reaching it fails at the
    check. The dataset project code, when the image carries one, is a second
    check: ``edc16_blocks.PROGRAM_EXTENT_BY_PROJECT`` (P350 and the P475U8
    generation -> 0xA0000, P447/P529/other P475 -> 0x180000), applied only to
    projects the SGO evidence proves; any other project falls back to the
    scan."""
    lo, hi = B.PROGRAM_END, B.PROGRAM_MAX_END
    if declared_end:
        return max(lo, min(int(declared_end), hi))
    gap_end = B.PROGRAM_GAP_END
    if any(b != 0xFF for b in bytes(image[gap_end:hi])):
        top = hi                                  # a long (P475-style) program
    else:
        top = lo                                  # the gap's data is not written
    fam = _family_extent(image)
    if fam is not None:
        return fam
    region = bytes(image[B.PROGRAM_START:top])
    i = len(region)
    while i > 0 and region[i - 1] == 0xFF:
        i -= 1
    if i <= 0:
        return lo
    end = (B.PROGRAM_START + i + 0xFFFF) & ~0xFFFF
    return max(lo, min(end, hi))


def _spans(block, program_end):
    if block is B.PROGRAM:
        end = program_end
        return (B.WriteSpan("program", B.PROGRAM_START, end - B.PROGRAM_START,
                            end - 1, end - 1),)
    return block.writes


#: what the status line calls each write span
_NAMES = {"boot": "boot block", "program": "program",
          "cal-A": "calibration (upper half)", "cal-B": "calibration (lower half)",
          "mpc": "internal flash (MPC)"}


def _name(span) -> str:
    return _NAMES.get(span.name, span.name)


def _kb(n: int) -> str:
    return "%d KB" % (n // 1024) if n % 1024 == 0 else "%.1f KB" % (n / 1024)


def _already_matches(fl, span, cks, on_stage) -> bool:
    """Ask the ECU (0x31 C5) whether it already holds ``span`` exactly. True
    only on a confirmed match (73 C5 00); a mismatch, an unsupported range or
    an inconclusive poll writes the span (never skip on doubt)."""
    try:
        cls = fl._checksum_probe_classify(span.addr, span.verify_end, cks)
    except Exception as ex:                       # a probe error never blocks a write
        on_stage("Couldn't check whether the %s already matches (%s); writing it."
                 % (_name(span), ex))
        return False
    if cls == "accepted:match":
        on_stage("The ECU already holds this %s (checksum 0x%04X); skipping it."
                 % (_name(span), cks))
        return True
    fl.log("%s pre-check: %s; writing it." % (_name(span), cls))
    return False


def _write_span(fl, span, enc, cks, *, arm, pre_verify, on_stage, on_percent,
                sector_erase=False):
    if arm and pre_verify and _already_matches(fl, span, cks, on_stage):
        return
    on_stage("Preparing to write %s to the %s at 0x%06X..."
             % (_kb(span.size), _name(span), span.addr))
    fl._request_download(span.addr, span.size)               # 0x34, the whole span
    if not arm:
        on_stage("Dry run: the ECU accepted the %s; nothing erased or written."
                 % _name(span))
        return
    if sector_erase:
        starts = list(range(span.addr, span.end, B.PROGRAM_SECTOR))
        for i, s in enumerate(starts):
            e = min(s + B.PROGRAM_SECTOR, span.erase_end + 1) - 1
            on_stage("Erasing the %s, sector %d of %d (0x%06X-0x%06X)..."
                     % (_name(span), i + 1, len(starts), s, e))
            fl._erase_block(s, e)                            # 0x31 C4  <-- no return
    else:
        on_stage("Erasing the %s at 0x%06X-0x%06X. This can take up to a minute..."
                 % (_name(span), span.addr, span.erase_end))
        fl._erase_block(span.addr, span.erase_end)           # 0x31 C4  <-- no return
    on_stage("Writing %s to the %s..." % (_kb(span.size), _name(span)))
    fl._transfer_data(enc, on_percent)                       # 0x36
    fl._request_transfer_exit()                              # 0x37
    on_stage("Checking the %s (expecting checksum 0x%04X)..." % (_name(span), cks))
    fl._verify_block(span.addr, span.verify_end, cks)        # 0x31 C5
    fl.log("The %s is written and checked (0x%04X)." % (_name(span), cks))


def write_blocks(fl, image, mpc_image=None, *, blocks=("program",), arm=False,
                 force_full=True, pre_verify=True, program_end=None,
                 speed="Medium", allow_fast=True, fast_init_prime="auto",
                 on_stage=None, on_percent=None, is_stop=None):
    """Write the ``blocks`` (keys of edc16_blocks) from the source images.

    ``image`` is the 2 MB external flash, ``mpc_image`` the 0x81000 internal
    flash; both already checksum-correct. Each span is first checked against
    the ECU (0x31 C5) and skipped when it already matches, unless
    ``force_full``. ``program_end`` is the container-declared program end, if
    any (see :func:`program_extent`)."""
    on_stage = on_stage or (lambda _m: None)
    if len(image) < FULL_SIZE:
        raise ValueError("EDC16 block write needs a full 2 MB image")
    chosen = B.ordered(blocks)
    if not chosen:
        raise ValueError("no EDC16 block selected to write")
    mpc_image = B.pad_mpc(mpc_image)            # 0x80000.. padded to 0x81000
    if B.MPC in chosen and (mpc_image is None or len(mpc_image) < B.MPC_IMAGE_SIZE):
        raise ValueError(
            "MPC write needs the internal-flash image (at least 0x%X bytes); "
            "the one given is %s" % (B.MPC_IMAGE_MIN,
                                     "missing" if mpc_image is None
                                     else "0x%X bytes" % len(mpc_image)))
    if fast_init_prime == "auto":
        fast_init_prime = fast_prime_from_image(image)
    pre_verify = bool(pre_verify) and not force_full

    # Encrypt everything before the session opens: a TP 2.0 channel left idle
    # across a long local step is dropped by the ECU (see Edc16Flash.write_flash).
    on_stage("Encrypting...")
    prog_end = program_extent(image, program_end)
    if B.PROGRAM in chosen:
        on_stage("The program runs 0x%06X-0x%06X in this file (%s)."
                 % (B.PROGRAM_START, prog_end - 1, _kb(prog_end - B.PROGRAM_START)))
    work = []
    for block in chosen:
        src = mpc_image if block.chip == B.INTERNAL else image
        for span in _spans(block, prog_end):
            enc, cks = encrypt(src, span.addr - block.base, span.end - block.base)
            work.append((block, span, enc, cks))

    on_stage("Connecting (%s)..." % fl.bus_name())
    fl.open_program_session(speed, allow_fast, fast_init_prime, is_stop, on_stage)
    for block, span, enc, cks in work:
        if is_stop and is_stop():
            fl._finalize()
            raise StopRequested("stopped before %s" % span.name)
        _write_span(fl, span, enc, cks, arm=arm, pre_verify=pre_verify,
                    on_stage=on_stage, on_percent=on_percent,
                    sector_erase=block is B.PROGRAM and _PROGRAM_SECTOR_ERASE)
    fl._finalize()
    on_stage("Done." if arm else "Done (dry run: nothing was written).")
