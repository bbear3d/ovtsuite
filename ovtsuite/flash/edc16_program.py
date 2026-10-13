# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 PROGRAM-AREA + INTERNAL-FLASH (MPC) write (armed via arm=True).

Extends the calibration-only write in :mod:`ovtsuite.flash.edc16` to the code
ranges the OEM (ODIS) flashes over OBD. It reuses the base
:class:`ovtsuite.flash.edc16.Edc16Flash` addressed primitives VERBATIM --
``_request_download`` (0x34), ``_erase_block`` (0x31 C4), ``_transfer_data``
(0x36), ``_request_transfer_exit`` (0x37), ``_verify_block`` (0x31 C5) -- and its
LVL1 security unlock (== the SGO SA2 key, proven byte-identical). It runs over
BOTH transports the base class supports (K-line prime->slow-init->0x85 session,
preferred/faster for the P475/LQ family; and CAN/TP2.0 10 85 + reset).

PROGRAM AREA -- written as ONE descriptor block, identical to the proven
calibration write cycle: a SINGLE RequestDownload over [0x40000, 0x180000), the
erase, ONE continuous TransferData stream, one transferExit, one 0x31 C5 checksum
over the whole block [0x40000, 0x17FFFF].  A bench run (2026-09-23) proved a
per-256 KB-sector loop fails: sector 1 wrote, but the ECU refused the transfer
after a SECOND, mid-block RequestDownload at 0x080000 -- the program area is a
single descriptor block (like each cal block) and accepts only ONE
RequestDownload.  Erase granularity is orthogonal: by default one 0x31 C4 spans
the whole block (as cal does); the module knob ``_PROGRAM_SECTOR_ERASE`` can
issue one erase per 256 KB sector after the single RequestDownload instead.

MPC (internal flash) -- addressed at VMA 0x404000, size 0x7C000, erase end
0x47FFFF. CONFIRMED against a real ODIS K-line capture (2026-09-24): ODIS writes
it with the ordinary per-block cycle (34 -> 31 C4 erase -> 36 -> 37 -> 31 C5) and
a single-block erase, identical addresses. The earlier NRC 0x74 was an illegal
byte count -- the MPC loader requires each 0x36 block to equal the ECU-declared
max (0xFD); _transfer_data now honours that (see edc16.py). Runs standalone
(MPC-only) or as block 3 of the full ODIS order (boot -> program -> MPC).

arm=False -> safe dry run: opens the real session, authenticates, and issues
RequestDownload per sector/range only (no erase, no transfer). arm=True ->
ERASE + WRITE. A failed/partial erase can brick the ECU; recover via BDM.
"""
from .edc16 import Edc16Flash, encrypt, StopRequested, KLineError, FULL_SIZE

# Program area = ODIS block 2: [0x40000, 0xA0000) (0x60000), checksum region
# [0x40000, 0x9FFFF].
#
# The ECU only checksums its OWN descriptor-aligned ranges.  0x31 C5 over ODIS's
# 0x40000..0x9FFFF returns 0x73 C5 (verdict) on the first 0x33 C5 poll; over the
# full 0x40000..0x17FFFF it returns 0x7F 33 22 (conditionsNotCorrect) forever and
# never completes (bench 2026-09-24: 785 polls / 62 s, no 0x73 C5).  So the ECU's
# checksum descriptors ARE ODIS's block boundaries -- which is why ODIS splits
# program at 0x9FFFF and never reflashes 0xA0000..0xBFFFF.  Matching ODIS gives a
# clean 0x73 C5 verify.  (Earlier "the oversized erase hangs" was wrong -- that
# was the un-drained checksum routine, fixed in _verify_block; but the oversized
# CHECKSUM is genuinely rejected, so the block stays ODIS-sized.)  To also write
# 0xA0000..0xBFFFF later, transfer the wider range but verify only 0x40000..0x9FFFF.
PROGRAM_START = 0x040000
PROGRAM_END = 0x0A0000              # exclusive -- ODIS block 2 (0x40000..0x9FFFF)
PROGRAM_SECTOR = 0x040000          # 256 KB -- the external-flash sector size
PROGRAM_VERIFY_END = 0x09FFFF
PROGRAM_MAX_END = 0x180000          # program descriptor upper bound (0x40000..0x17FFFF)

# Erase strategy for the program area (bench-flippable).  False (default): one
# 0x31 C4 spanning the whole program block, exactly like the proven cal write
# (the ECU's erase routine loops the physical sectors internally).  True: one
# 0x31 C4 per 256 KB sector after the single RequestDownload -- for testing
# Bear's per-sector-erase hypothesis.  Either way there is only ONE
# RequestDownload for the whole descriptor block.
_PROGRAM_SECTOR_ERASE = False

# Boot/low block. ODIS DOES reflash this -- it is block 1 of the ODIS capture
# (RequestDownload 0x010000 size 0x2FF00, erase 0x010000..0x03FFFF, checksum
# 0x010000..0x03FEFF).  Still not included by default (riskiest block), but it is
# the FIRST block in the full ODIS order (boot -> program -> MPC) and may be a
# precondition for the MPC write; pass include_boot=True to replay that order.
BOOT_ADDR = 0x010000
BOOT_DL_SIZE = 0x02FF00            # RequestDownload size (== transferred length)
BOOT_ERASE_END = 0x03FFFF          # 0x31 C4 erase end (sector-rounded)
BOOT_VERIFY_END = 0x03FEFF         # 0x31 C5 checksum end (data extent)

# Internal-flash (MPC) block, VMA-addressed. erase granularity UNCONFIRMED.
MPC_ADDR = 0x404000                # RequestDownload/erase target (VMA)
MPC_ERASE_END = 0x47FFFF
MPC_ENC = (0x4000, 0x80000)        # file range of the internal image to transfer (0x7C000)
MPC_VMA = 0x400000

# Calibration = ODIS blocks 4 & 5, written (in this order) AFTER the MPC, from the
# external image.  Block 4: RequestDownload 0x1C0000 size 0x3E000, erase
# 0x1C0000..0x1FFFFF (sector-rounded), checksum 0x1C0000..0x1FDFFF (data extent,
# ODIS 0x6F28).  Block 5: RequestDownload 0x180000 size 0x40000, erase/checksum
# 0x180000..0x1BFFFF (ODIS 0x5072).
CAL_A_START = 0x1C0000
CAL_A_END = 0x1FE000               # exclusive -- data 0x3E000 (0x1C0000..0x1FDFFF)
CAL_A_ERASE_END = 0x1FFFFF         # 0x31 C4 erase end (sector-rounded up)
CAL_A_VERIFY_END = 0x1FDFFF        # 0x31 C5 checksum end (data extent)
CAL_B_START = 0x180000
CAL_B_END = 0x1C0000               # exclusive -- data 0x40000 (0x180000..0x1BFFFF)
CAL_B_ERASE_END = 0x1BFFFF
CAL_B_VERIFY_END = 0x1BFFFF

# Tester TX inter-byte time (P4_MIN) for the DATA-TRANSFER phase of a J2534
# K-line write. The slow-init handshake needs P4_MIN=2 (see kline_j2534), but that
# spacing makes a full write take ~1 h; the fast-init read channel uses 0 with no
# trouble, so drop to 0 once the session/baud is up. Set back to 2 here if a bench
# run shows dropped TransferData bytes (a block checksum mismatch).
_TRANSFER_P4_MIN = 0


def _open_session(fl, image, speed, allow_fast, fast_init_prime, is_stop, on_stage,
                  prime_teardown=None, prime_trim=None):
    """Open the programming/download session over WHICHEVER transport ``fl.k`` is,
    reusing the base class's own both-bus logic (identical to write_flash)."""
    if getattr(fl.k, "is_tp20", False):
        def _prepare():
            fl.connect()
            fl._read_erase_signature()
            fl._lvl1_auth()
        fl._establish_session(0x85, speed, allow_fast, _prepare, is_stop)
        reset = getattr(fl.k, "reset_channel", None)
        if reset is not None:
            reset()
        fl._lvl1_auth()
    else:
        fl._open_kline_write_session(speed, allow_fast, fast_init_prime, is_stop,
                                     on_stage, prime_teardown=prime_teardown,
                                     prime_trim=prime_trim)
        fl._set_access_timing()
        # Session is up and the baud is switched; drop the tester inter-byte time
        # so the big TransferData frames stream fast (init needed the wider P4).
        setter = getattr(fl.k, "set_tx_interbyte", None)
        if setter is not None:
            setter(_TRANSFER_P4_MIN)


def _do_program(fl, image, *, arm, on_stage, on_percent, is_stop,
                sector_erase=None, force_full=True, verify_sectors=True,
                pre_verify=False, program_end=None):
    """Program area, written as ONE descriptor block -- identical to the proven
    calibration write cycle (``Edc16Flash._write_block``): a SINGLE
    RequestDownload over the whole program range, the erase, ONE continuous
    TransferData stream, one transferExit, one checksum verify.

    Bench result (2026-09-23): a per-256 KB-sector loop wrote sector 1 fine but
    the ECU refused the transfer after a SECOND, mid-block RequestDownload at
    0x080000.  The program area is a single descriptor block (like each cal
    block) and wants ONE RequestDownload -- a fresh RequestDownload at a mid-block
    address is not a valid re-entry.  So we mirror cal exactly.

    Erase granularity is a separate question from the download structure.  Cal
    erases a whole block span in one 0x31 C4 (the ECU's routine loops the physical
    sectors internally), and that is the default here too.  ``sector_erase`` True
    instead issues one 0x31 C4 per 256 KB sector AFTER the single RequestDownload,
    for bench-testing Bear's per-sector-erase hypothesis without a mid-block
    RequestDownload.  Default comes from the module knob ``_PROGRAM_SECTOR_ERASE``.
    """
    if sector_erase is None:
        sector_erase = _PROGRAM_SECTOR_ERASE
    # Source-driven extent: ODIS's 0x40000-0x9FFFF for a P350U8 stock, the full
    # 0x40000-0x17FFFF for a P475 (whose .sgo declares len 0x140000 + a trailer).
    prog_end = program_write_extent(image, program_end)
    prog_verify_end = prog_end - 1
    dl_size = prog_end - PROGRAM_START
    enc, cks = encrypt(image, PROGRAM_START, prog_end)
    on_stage("Program extent (source-driven): 0x%06X-0x%06X (0x%X bytes)"
             % (PROGRAM_START, prog_verify_end, dl_size))
    if is_stop and is_stop():
        raise StopRequested("stopped before program download")
    # Skip the whole program write if the ECU already holds this exact block.
    # This is the cal-style skip (Edc16Flash._write_block does the same per block),
    # but the program is ONE descriptor block accepting only ONE RequestDownload --
    # a mid-block RequestDownload for a partial write is refused (proven by the
    # 256 KB-sector bench failure) -- so the skip is necessarily all-or-nothing.
    # NO pre-write skip.  The ECU's 0x31 C5 checksum routine never returns a 73 C5
    # result (it NAKs the 0x33 C5 results poll -- bench 2026-09-23), so there is no
    # way to ask "does the ECU already hold this block?" before writing.  The
    # program is always written when armed.  force_full is accepted for API/GUI
    # compatibility but no longer gates a skip.
    if arm and pre_verify and _already_matches(
            fl, PROGRAM_START, prog_verify_end, cks, "program", on_stage):
        return
    on_stage("RequestDownload program @0x%06X size=0x%X"
             % (PROGRAM_START, dl_size))
    fl._request_download(PROGRAM_START, dl_size)      # 0x34 (whole block)
    if not arm:
        on_stage("DISARMED: program download accepted; skipping erase+write.")
        return
    if sector_erase:
        sectors = list(range(PROGRAM_START, prog_end, PROGRAM_SECTOR))
        for i, ss in enumerate(sectors):
            se = ss + PROGRAM_SECTOR
            on_stage("Erasing program sector %d/%d @0x%06X..0x%06X..."
                     % (i + 1, len(sectors), ss, se - 1))
            fl._erase_block(ss, se - 1)               # 0x31 C4  <-- POINT OF NO RETURN
    else:
        on_stage("Erasing program area @0x%06X..0x%06X..."
                 % (PROGRAM_START, prog_verify_end))
        fl._erase_block(PROGRAM_START, prog_verify_end)   # 0x31 C4  <-- POINT OF NO RETURN
    on_stage("Writing program area (0x%X bytes)..." % dl_size)
    fl._transfer_data(enc, on_percent)                # 0x36 (continuous stream)
    fl._request_transfer_exit()                       # 0x37
    # NOTE: 0x31 C5 is BLOCK-GRANULAR -- it checksums the WHOLE descriptor block
    # the start address falls in and IGNORES the end parameter (proven on the bench
    # 2026-09-23: every 256 KB sub-range reported MISMATCH while the whole-block
    # check passed on the same freshly written data).  So there is no per-sector
    # verify, and per-sector SKIP is impossible: the program is one block, verified
    # and skipped only as a whole.  verify_sectors is accepted for API stability.
    on_stage("Post-write checksum verify 0x%06X..0x%06X (expect 0x%04X)..."
             % (PROGRAM_START, prog_verify_end, cks))
    fl._verify_block(PROGRAM_START, prog_verify_end, cks)   # bounded; notes if unsupported
    fl.log("Program area write complete (0x%04X)." % cks)


def _do_mpc(fl, mpc_image, *, arm, on_stage, on_percent, pre_verify=False):
    """Internal flash (MPC) over the direct 0x34/0x36 servicer -- the SAME path
    ODIS uses (proven by an ODIS K-line capture, 2026-09-24).

    ODIS writes the internal flash at VMA 0x404000 (size 0x7C000) with the
    ordinary per-block cycle -- RequestDownload -> 31 C4 erase -> 36 TransferData
    -> 37 -> 31 C5 checksum -- and gets a POSITIVE 74 FD.  No RAM bootstrap, no
    special arming service.  The earlier NRC 0x74 on the first 0x36 was an
    ILLEGAL BYTE COUNT: this servicer requires each TransferData block to equal
    the ECU-declared max length (0xFD -> 252 data bytes); our old fixed 0xF8 (248)
    was tolerated by the program loader but refused by the MPC loader.  The base
    _transfer_data now honours the 0x74-declared max, so this path should take.

    This can be run standalone (MPC-only: include_program=False, include_mpc=True)
    OR as the 3rd block of the full ODIS order (boot -> program -> MPC), which is
    the safer bet if the MPC-only write is still refused (ODIS always reflashes
    boot+program first in the same session).  addr/size/erase-range all match the
    ODIS capture exactly.
    """
    enc, cks = encrypt(mpc_image, MPC_ENC[0], MPC_ENC[1])
    size = MPC_ENC[1] - MPC_ENC[0]
    if arm and pre_verify and _already_matches(
            fl, MPC_ADDR, MPC_ERASE_END, cks, "mpc", on_stage):
        return
    on_stage("RequestDownload mpc @0x%06X size=0x%X" % (MPC_ADDR, size))
    fl._request_download(MPC_ADDR, size)             # 0x34
    if not arm:
        on_stage("DISARMED: mpc download accepted; skipping erase+write.")
        return
    on_stage("Erasing mpc @0x%06X..0x%06X (single-block; granularity UNCONFIRMED)..."
             % (MPC_ADDR, MPC_ERASE_END))
    fl._erase_block(MPC_ADDR, MPC_ERASE_END)         # 0x31 C4  <-- POINT OF NO RETURN
    on_stage("Writing mpc (0x%X bytes, ODIS-identical direct path; block length "
             "matched to the ECU's 0x74-declared max)..." % size)
    fl._transfer_data(enc, on_percent)               # 0x36
    fl._request_transfer_exit()                      # 0x37
    fl._verify_block(MPC_ADDR, MPC_ERASE_END, cks)   # 0x31 C5
    fl.log("MPC written and verified.")


def _do_cal(fl, image, *, arm, on_stage, on_percent, is_stop=None, pre_verify=False):
    """Calibration = ODIS blocks 4 & 5, in ODIS order (0x1C0000 then 0x180000).
    Each is an ordinary descriptor-block cycle -- RequestDownload -> 31 C4 erase ->
    36 TransferData -> 37 -> 31 C5 checksum -- matching the ODIS capture exactly
    (stock checksums 0x6F28 and 0x5072)."""
    for name, start, end, erase_end, verify_end in (
            ("cal-A", CAL_A_START, CAL_A_END, CAL_A_ERASE_END, CAL_A_VERIFY_END),
            ("cal-B", CAL_B_START, CAL_B_END, CAL_B_ERASE_END, CAL_B_VERIFY_END)):
        if is_stop and is_stop():
            raise StopRequested("stopped before %s download" % name)
        enc, cks = encrypt(image, start, end)
        size = end - start
        if arm and pre_verify and _already_matches(
                fl, start, verify_end, cks, name, on_stage):
            continue
        on_stage("RequestDownload %s @0x%06X size=0x%X" % (name, start, size))
        fl._request_download(start, size)                 # 0x34 (whole block)
        if not arm:
            on_stage("DISARMED: %s download accepted; skipping erase+write." % name)
            continue
        on_stage("Erasing %s @0x%06X..0x%06X..." % (name, start, erase_end))
        fl._erase_block(start, erase_end)                 # 0x31 C4  <-- POINT OF NO RETURN
        on_stage("Writing %s (0x%X bytes)..." % (name, size))
        fl._transfer_data(enc, on_percent)                # 0x36 (continuous stream)
        fl._request_transfer_exit()                       # 0x37
        on_stage("Post-write checksum verify %s 0x%06X..0x%06X (expect 0x%04X)..."
                 % (name, start, verify_end, cks))
        fl._verify_block(start, verify_end, cks)          # 0x31 C5
        fl.log("%s write complete (0x%04X)." % (name, cks))


def _region_blank(buf, start, end):
    """True if [start, end) of buf is uniformly blank (all 0xFF or all 0x00) or
    absent -- i.e. nothing worth writing/reading there."""
    if buf is None or len(buf) < end:
        return True
    chunk = bytes(buf[start:end])
    if not chunk:
        return True
    return chunk == b"\xFF" * len(chunk) or chunk == b"\x00" * len(chunk)


def program_write_extent(image, declared_end=None):
    """End address (EXCLUSIVE) of the program block to flash from this source.

    ODIS uses the block's declared data length, not a fixed size: a P350U8 stock
    is 0x40000-0x9FFFF (0x60000), while a P475 program runs to 0x17FFFF (its .sgo
    section declares len 0x140000, and it carries a trailer in the last sector).
    So the extent is source-driven:
      * if the container declares the program section end, use it;
      * otherwise scan to the last non-0xFF byte in [PROGRAM_START, PROGRAM_MAX_END),
        rounded up to 0x10000.
    Floored at the ODIS extent (0xA0000) so we never write LESS than the proven
    block, and capped at PROGRAM_MAX_END."""
    if declared_end:
        return max(PROGRAM_END, min(int(declared_end), PROGRAM_MAX_END))
    region = bytes(image[PROGRAM_START:PROGRAM_MAX_END])
    i = len(region)
    while i > 0 and region[i - 1] == 0xFF:
        i -= 1
    if i <= 0:
        return PROGRAM_END
    end = (PROGRAM_START + i + 0xFFFF) & ~0xFFFF        # round up to 0x10000
    return max(PROGRAM_END, min(end, PROGRAM_MAX_END))


def available_blocks(image, mpc_image=None):
    """Which flash blocks the given source images actually contain data for.

    Returns a dict {"boot","program","cal","mpc": bool}.  A block is available
    only when its source region is present and not uniformly blank, so the UI can
    offer exactly the blocks that would carry real data and never write a blank
    region over a good one.  ``image`` is the 2 MB external flash; ``mpc_image``
    the optional 0x81000 internal-flash image (from a .dim)."""
    ok = lambda a, b: image is not None and len(image) >= b and not _region_blank(image, a, b)
    return {
        "boot": ok(BOOT_ADDR, BOOT_ADDR + BOOT_DL_SIZE),
        "program": ok(PROGRAM_START, PROGRAM_END),
        "cal": ok(CAL_A_START, CAL_A_END) or ok(CAL_B_START, CAL_B_END),
        "mpc": mpc_image is not None and not _region_blank(mpc_image, MPC_ENC[0], MPC_ENC[1]),
    }


def _already_matches(fl, start, verify_end, cks, name, on_stage):
    """Pre-write check: ask the ECU (0x31 C5) whether [start, verify_end] already
    holds the exact bytes we are about to write.  Returns True on a confirmed
    match (73 C5 00) so the caller can skip that block's erase+write.  Any other
    result -- mismatch, an unsupported range, or an inconclusive poll -- returns
    False so the block is written normally (never skip on doubt)."""
    try:
        cls = fl._checksum_probe_classify(start, verify_end, cks)
    except Exception as ex:                       # never let a probe error block a write
        on_stage("  %s pre-verify inconclusive (%s) -- writing." % (name, ex))
        return False
    if cls == "accepted:match":
        on_stage("  %s already holds this data (0x%04X verified) -- skipping erase+write."
                 % (name, cks))
        return True
    on_stage("  %s pre-verify: %s -- writing." % (name, cls))
    return False


def write_program(fl, image, mpc_image=None, *, arm=False, include_mpc=False,
                  include_boot=False, include_program=True, include_cal=False,
                  force_full=True, pre_verify=True, program_end=None,
                  verify_sectors=True, speed="Medium", allow_fast=True,
                  fast_init_prime="auto", on_stage=None, on_percent=None,
                  is_stop=None):
    """Write the PROGRAM area (256 KB sectors) and optionally the MPC internal
    flash over K-line or CAN. DISARMED unless ``arm=True`` -- see the banner.
    ``image`` is the full 2 MB external image (already checksum/signature-correct);
    ``mpc_image`` the 0x81000 internal-flash image (already corrected)."""
    if len(image) < FULL_SIZE:
        raise ValueError("EDC16 program write needs a full 2 MB image")
    on_stage = on_stage or (lambda _m: None)
    from .edc16 import fast_prime_from_image
    if fast_init_prime == "auto":
        fast_init_prime = fast_prime_from_image(image)

    if include_mpc and (mpc_image is None or len(mpc_image) < 0x81000):
        raise ValueError("MPC write needs the 0x81000 internal-flash image")

    # Pre-write verify (skip a block whose checksum already matches).  A "force
    # full write" deliberately rewrites everything, so it turns the skip off.
    pre_verify = bool(pre_verify) and not force_full

    on_stage("Connecting (%s)..." % ("CAN/TP2.0" if getattr(fl.k, "is_tp20", False)
                                     else "K-line"))
    _open_session(fl, image, speed, allow_fast, fast_init_prime, is_stop, on_stage)

    if include_boot:
        # Data length MUST equal the RequestDownload size, so encrypt exactly the
        # download window [BOOT_ADDR, BOOT_ADDR+BOOT_DL_SIZE) (ODIS: 0x2FF00).
        enc, cks = encrypt(image, BOOT_ADDR, BOOT_ADDR + BOOT_DL_SIZE)
        if arm and pre_verify and _already_matches(
                fl, BOOT_ADDR, BOOT_VERIFY_END, cks, "boot", on_stage):
            pass                                          # boot already correct -- skip entirely
        else:
            on_stage("RequestDownload boot @0x%06X size=0x%X" % (BOOT_ADDR, BOOT_DL_SIZE))
            fl._request_download(BOOT_ADDR, BOOT_DL_SIZE)
            if arm:
                on_stage("Erasing boot @0x%06X..0x%06X..." % (BOOT_ADDR, BOOT_ERASE_END))
                fl._erase_block(BOOT_ADDR, BOOT_ERASE_END)   # 0x31 C4  <-- POINT OF NO RETURN
                fl._transfer_data(enc, on_percent)
                fl._request_transfer_exit()
                fl._verify_block(BOOT_ADDR, BOOT_VERIFY_END, cks)

    if include_program:
        _do_program(fl, image, arm=arm, on_stage=on_stage, on_percent=on_percent,
                    is_stop=is_stop, force_full=force_full,
                    verify_sectors=verify_sectors, pre_verify=pre_verify,
                    program_end=program_end)

    if include_mpc:
        if is_stop and is_stop():
            fl._finalize()
            raise StopRequested("stopped before mpc")
        _do_mpc(fl, mpc_image, arm=arm, on_stage=on_stage, on_percent=on_percent,
                pre_verify=pre_verify)

    if include_cal:
        if is_stop and is_stop():
            fl._finalize()
            raise StopRequested("stopped before cal")
        _do_cal(fl, image, arm=arm, on_stage=on_stage, on_percent=on_percent,
                is_stop=is_stop, pre_verify=pre_verify)

    fl._finalize()
    on_stage("Done (%s)." % ("armed write" if arm else "disarmed dry run"))


def _open_read_session(fl, speed, allow_fast, is_stop):
    """Open the READ (0x86) upload session -- identical establishment to
    Edc16Flash.read_flash (connect + LVL3 read unlock), over whichever bus fl.k
    is.  Separate from the write session (0x85 + LVL1)."""
    def _prepare():
        fl.connect()
        fl.log("Requesting security access (read)...")
        fl._lvl3_auth()
    fl._establish_session(0x86, speed, allow_fast, _prepare, is_stop)


# Addresses to probe. Boot/low is the region the read session (LVL3) is refused
# on with NRC 0x33 (securityAccessDenied) -- the disassembly (gate 0x7c584) proves
# that is a per-region security-LEVEL gate, so a different level may grant it.
# 0x40000 (program) is the readable control: it must stay granted at every level.
_BOOT_PROBE_ADDR = 0x010000
_PROGRAM_CTRL_ADDR = 0x040000


def boot_read_probe(fl, *, addr=_BOOT_PROBE_ADDR, speed="Medium", allow_fast=True,
                    on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: find which security level and/or read SERVICE can READ the
    boot/low region over OBD.

    Two independent read paths are tested per region, because they go through
    DIFFERENT per-region security gates:
      * requestUpload (0x35)        -- gate 0x7c584, region_table @ r14-0x654
      * readMemoryByAddress (0x23)  -- handler 0xB1360, region_table @ r14-0x700
    Boot is refused via 0x35 at every level we hold, but the 0x23 gate is separate,
    so 0x23 may read boot even when 0x35 won't. Each is tried at LVL3 (read), LVL1
    (write/SA2), LVL5 (seed+K5, K5 read live) and LVL9 (LFSR). program (0x40000) is
    the readable control. Nothing is written; 0x23 reads only 0x20 bytes.

    Returns {level: {"boot": bool, "boot_rma": bool, "program": bool,
    "program_rma": bool}}."""
    on_stage = on_stage or (lambda _m: None)
    results = {}

    def _try(name):
        row = {}
        for rname, raddr in (("boot", addr), ("program", _PROGRAM_CTRL_ADDR)):
            if is_stop and is_stop():
                raise StopRequested("boot-probe stopped")
            # requestUpload (0x35)
            try:
                fl._request_upload(raddr, 0x100)
                row[rname] = True
                on_stage("  [%s] %s 0x%06X upload(0x35) -> GRANTED" % (name, rname, raddr))
                try:
                    fl._request_transfer_exit()
                except Exception:
                    pass
            except Exception as ex:
                row[rname] = False
                on_stage("  [%s] %s 0x%06X upload(0x35) -> refused (%s)" % (name, rname, raddr, ex))
            if is_stop and is_stop():
                raise StopRequested("boot-probe stopped")
            # readMemoryByAddress (0x23) -- the separate gate
            try:
                data = fl._read_memory_by_address(raddr, 0x20)
                row[rname + "_rma"] = True
                on_stage("  [%s] %s 0x%06X readMem(0x23) -> READ %s..."
                         % (name, rname, raddr, bytes(data[:8]).hex()))
            except Exception as ex:
                row[rname + "_rma"] = False
                on_stage("  [%s] %s 0x%06X readMem(0x23) -> refused (%s)" % (name, rname, raddr, ex))
        results[name] = row

    on_stage("Opening read session (%s)..." % ("CAN/TP2.0" if getattr(fl.k, "is_tp20", False)
                                               else "K-line"))
    _open_read_session(fl, speed, allow_fast, is_stop)      # 0x86 + LVL3
    on_stage("Read session open (LVL3 read auth active).")
    _try("LVL3(read 27 03/04)")

    for name, auth in (("LVL1(write 27 01/02)", fl._lvl1_auth),
                       ("LVL5(27 05/06)", fl._lvl5_auth),
                       ("LVL9(27 09/0A)", fl._lvl9_auth)):
        if is_stop and is_stop():
            break
        on_stage("Re-authenticating at %s..." % name)
        try:
            auth()
            on_stage("  %s auth OK." % name)
            _try(name)
        except Exception as ex:
            on_stage("  %s auth refused (%s)." % (name, ex))
            results[name] = {"boot": False, "boot_rma": False,
                             "program": None, "program_rma": None, "error": str(ex)}

    try:
        fl._try_quiet_stop()
    except Exception:
        pass

    hit = []
    for lv, r in results.items():
        if r.get("boot"):
            hit.append("%s via upload(0x35)" % lv)
        if r.get("boot_rma"):
            hit.append("%s via readMem(0x23)" % lv)
    if hit:
        on_stage("RESULT: boot IS readable -- %s." % "; ".join(hit))
    else:
        on_stage("RESULT: boot refused by BOTH services at every level tested.")
    return results


# Level-5 additive constant K5 lives in the RAM coding struct (0x7FE9E4), at
# +0x27 (a u16).  It is a per-ECU coding/EEPROM value, not a flash constant, so
# it must be read from the live ECU.  See the protocol doc's level-5 section.
CODING_STRUCT_ADDR = 0x7FE9E4
K5_STRUCT_OFFSET = 0x27


def ram_read_probe(fl, *, addr=CODING_STRUCT_ADDR, size=0x40, speed="Medium",
                   allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: read `size` bytes at 24-bit RAM `addr` via
    readMemoryByAddress (0x23), trying each security level we hold a key for
    (LVL3 read, then LVL1 write, then LVL9).  Reads only -- nothing is written.

    Primary use: pull the level-5 additive constant K5 = the u16 at the coding
    struct +0x27 (default addr 0x7FE9E4).  Secondary use: probe whether RAM (and
    thus the EEPROM shadow) is readable over OBD at all, and at which level.

    Returns {level_label: {"data": bytes|None, "k5": int|None, "error": str|None}}.
    """
    on_stage = on_stage or (lambda _m: None)
    results = {}
    is_coding = (addr == CODING_STRUCT_ADDR and size > K5_STRUCT_OFFSET + 1)

    def _try(name):
        row = {"data": None, "k5": None, "error": None}
        try:
            data = fl._read_memory_by_address(addr, size)
            row["data"] = data
            on_stage("  [%s] 0x%06X +0x%X -> %s" % (name, addr, size, data.hex()))
            if is_coding and len(data) > K5_STRUCT_OFFSET + 1:
                k5 = (data[K5_STRUCT_OFFSET] << 8) | data[K5_STRUCT_OFFSET + 1]
                row["k5"] = k5
                on_stage("    -> K5 = 0x%04X  => level-5 key = (seed + 0x%04X) & 0xFFFFFFFF"
                         % (k5, k5))
        except Exception as ex:
            row["error"] = str(ex)
            on_stage("  [%s] 0x%06X refused (%s)" % (name, addr, ex))
        results[name] = row
        return row["data"] is not None

    on_stage("Opening read session (%s)..." % ("CAN/TP2.0" if getattr(fl.k, "is_tp20", False)
                                               else "K-line"))
    _open_read_session(fl, speed, allow_fast, is_stop)      # 0x86 + LVL3
    on_stage("Read session open (LVL3 read auth active).")
    if not _try("LVL3(read 27 03/04)"):
        for name, auth in (("LVL1(write 27 01/02)", fl._lvl1_auth),
                           ("LVL9(27 09/0A)", fl._lvl9_auth)):
            if is_stop and is_stop():
                break
            try:
                auth()
                on_stage("  %s auth OK." % name)
                if _try(name):
                    break
            except Exception as ex:
                on_stage("  %s auth refused (%s)." % (name, ex))
                results[name] = {"data": None, "k5": None, "error": "auth: %s" % ex}
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    ok = [lv for lv, r in results.items() if r.get("data") is not None]
    if ok:
        on_stage("RESULT: RAM 0x%06X readable at %s." % (addr, ", ".join(ok)))
    else:
        on_stage("RESULT: RAM 0x%06X not readable at any level we hold "
                 "(readMemoryByAddress refused)." % addr)
    return results


# ---------------------------------------------------------------------------
# Immo / coding read (LVL3) -- dump the coding image + ImmCtl state and decode
# the immobiliser status byte.  NON-DESTRUCTIVE.  The region addresses are
# candidates derived from static disassembly of 03G906016LQ; a generous span is
# read so the immo byte is captured even if an offset is slightly off, and the
# live values are what confirm/tune them.
IMMO_STATUS_STRUCT_OFFSET = 0x56          # coding struct (0x7FE9E4) +0x56 -> immo status byte
IMMO_STATUS_STATES = {
    0x33: "immobiliser ACTIVE (33)", 0x73: "immobiliser ACTIVE (73)",
    0xF3: "immobiliser ACTIVE (F3)", 0x53: "record invalid (53)",
    0x00: "released (00)", 0x0F: "released (0F)",
    0x60: "OFF / not installed (60)",
}
CODING_MIRROR_ADDR = 0x8012C0             # external-RAM coding image ($3B record tables point here)
IMMO_STATE_ADDR = 0x7F9480                # ImmCtl RAM working state

# Widened after the first live read (2026-09-26): the immo struct at 0x7FE9E4 is
# larger than 0x100 and the code reads immo state/config cells across it (e.g.
# 0x7FEA24, and 0x7FEA3A which it compares to 0xF3 = immo active). coding_mirror
# 0x8012C0 read back empty on the bench ECU (kept for confirmation). A flash cell
# (0x1C0000, cal) is read too as an identity-mapping sanity check for $23.
_IMMO_CODING_REGIONS = [
    ("coding_struct", CODING_STRUCT_ADDR, 0x180),
    ("coding_mirror", CODING_MIRROR_ADDR, 0x80),
    ("immo_state",    IMMO_STATE_ADDR,    0x80),
    ("flash_check",   0x1C0000,           0x20),
]


def _read_span(fl, addr, size, is_stop=None, chunk=0x80):
    """readMemoryByAddress (0x23) an arbitrary span in <=chunk-byte reads."""
    out = bytearray()
    off = 0
    while off < size:
        if is_stop and is_stop():
            raise StopRequested("read stopped")
        n = min(chunk, size - off)
        out.extend(fl._read_memory_by_address(addr + off, n))
        off += n
    return bytes(out)


def find_eeprom(fl, *, lo=0x7F8000, hi=0x800000, step=0x80, speed="Medium",
                allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: locate the 4 KB EEPROM (or its RAM shadow) over OBD by its
    OWN checksum structure. Opens the LVL3 read session, $23-sweeps [lo,hi), then
    has the EDC16 module find the base where many 32-byte blocks satisfy
    (sum16 + BE16 chk) == the canonical counter (see
    :func:`ovtsuite.edc16.eeprom16.locate`). Robust to $23 region remapping: the
    base returned is the $23 address that reads EEPROM content. Reads only.

    Needs the EDC16 module (its EEPROM checksum layout); raises RuntimeError
    without it, before anything is sent.

    Returns {"base": int|None, "hits": int, "immo": (b1,b2), "blocks": {0x140:..,0x160:..}}.
    """
    from ..modules import registry as _registry
    cs = _registry.checksum("edc16")
    if cs is None or not hasattr(cs, "eeprom_locate"):
        raise RuntimeError("Finding the EEPROM uses its checksum layout. %s"
                           % _registry.missing_text("edc16", "the EEPROM scan"))
    on_stage = on_stage or (lambda _m: None)
    _open_read_session(fl, speed, allow_fast, is_stop)
    on_stage("Read session open (LVL3). Sweeping 0x%06X-0x%06X for the EEPROM..." % (lo, hi))
    img = {}
    a = lo
    while a < hi:
        if is_stop and is_stop():
            raise StopRequested("scan stopped")
        n = min(step, hi - a)
        try:
            d = fl._read_memory_by_address(a, n)
            for i, v in enumerate(d):
                img[a + i] = v
        except Exception as ex:
            on_stage("  read @%06X failed (%s)" % (a, ex))
        a += step
        if (a - lo) % 0x1000 == 0:
            on_stage("  ...swept to 0x%06X" % a)

    found = cs.eeprom_locate(img, lo, hi)
    out = {"base": found["base"], "hits": found["hits"], "immo": found["immo"],
           "blocks": found["blocks"]}
    if found["base"] is not None:
        base = found["base"]
        b1, b2 = found["immo"]
        on_stage("EEPROM FOUND: base 0x%06X (%d/%d canonical blocks valid)."
                 % (base, found["hits"], found["checked"]))
        on_stage("  immo 0x156/0x176 = 0x%02X/0x%02X -> %s"
                 % (b1 or 0, b2 or 0, "ON" if found["immo_on"] else "off/other"))
        for addr, blk in sorted(found["blocks"].items()):
            on_stage("  block 0x%03X @0x%06X: %s" % (addr, base + addr, blk.hex()))
    else:
        on_stage("EEPROM NOT found in 0x%06X-0x%06X - try a wider/different range." % (lo, hi))
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    return out


def gpt_verify_running(fl, *, rpm=850, samples=6, interval=0.5,
                       crank_pin=None, cam_pin=None, group=None,
                       stop_after=True, speed="Low", allow_fast=False,
                       use_gap_pattern=False, crank_only=True,
                       on_stage=None, is_stop=None):
    """Drive the SM2 crank/cam (GPT) signals and confirm the ECU recognises
    them by reading the RPM measuring block.

    Opens a KWP2000 diagnostic session over ``fl.k`` (the same J2534 device),
    starts the crank+cam PWM on that channel, then samples the engine-speed
    measuring block.  Returns a dict; ``recognized`` is True when the ECU
    reports a plausible non-zero RPM (the direct sign the crank signal is
    seen).  With ``stop_after=False`` the GPT outputs are left running so a
    subsequent ``find_eeprom`` pass runs with the engine "turning".

    This is the pre-flight check before hunting the EEPROM: no point sweeping
    if the ECU never believes it is rotating.
    """
    import time as _t
    from .diag import DiagSession
    from . import sm2_gpt
    on_stage = on_stage or (lambda s: None)
    crank_pin = sm2_gpt.DEFAULT_CRANK_PIN if crank_pin is None else crank_pin
    cam_pin = sm2_gpt.DEFAULT_CAM_PIN if cam_pin is None else cam_pin
    grp = sm2_gpt.RPM_GROUP if group is None else group

    # Ensure the J2534 channel is open so fl.k._ch is populated.
    # _ch is None at construction; it is normally set by wake_up() inside
    # diag.connect(), but we must start GPT *before* opening the KWP session
    # (the SM_PWM_SET IOCTL blocks ~10 s and would expire the EDC16 session
    # timer).  Calling _connect_channel() here opens the PassThru channel
    # only (no KWP init pulse) so we can obtain _ch for set_pwm.
    # diag.connect() will reuse the already-open channel and proceed straight
    # to the fast-init KWP pulse + startDiagnosticSession.
    kl = fl.k
    if getattr(kl, "_ch", None) is None and hasattr(kl, "_connect_channel"):
        kl._connect_channel()
    ch = getattr(kl, "_ch", None)
    if ch is None or not hasattr(ch, "set_pwm"):
        raise KLineError(
            "The active J2534 device/channel does not expose the SM2 PWM "
            "(set_pwm) vendor ioctl - GPT crank/cam needs a Scanmatik SM2.")

    # Start GPT BEFORE opening the KWP session.
    # The SM2 SM_PWM_SET IOCTL blocks for ~10 s while the firmware initialises
    # the GPT hardware.  If we open the diagnostic session first, the EDC16
    # P3 session-alive timer (~5 s) expires during that wait and the ECU
    # closes the session before the first measuring-block read is even sent.
    # Correct order: arm signals -> open fresh session -> read blocks.
    items = sm2_gpt.crank_cam_items(rpm, crank_pin=crank_pin, cam_pin=cam_pin,
                                    crank_only=crank_only)
    _mode = "crank only" if crank_only else "crank+cam"
    on_stage("Starting GPT %s: target %d rpm (crank pin %d%s)."
             " (SM2 firmware init may take ~10 s)"
             % (_mode, rpm, crank_pin,
                "" if crank_only else ", cam pin %d" % cam_pin))
    ch.set_pwm(items, ioctl_id=sm2_gpt.PWM_IOCTL)

    # Optional gap-pattern thread: alternates set_pwm rapidly to simulate
    # the 60-2 missing-tooth so the EDC16 crank decoder can synchronise.
    _gap_stop = None
    if use_gap_pattern:
        on_stage("Gap-pattern mode: starting 60-2 tooth simulation thread...")
        _, _gap_stop = sm2_gpt.gap_thread_start(
            ch, rpm, crank_pin=crank_pin, cam_pin=cam_pin,
            crank_only=crank_only)
        on_stage("  Gap-thread running (IOCTL latency measured on first cycle).")

    on_stage("GPT running. Opening KWP session to read measuring blocks...")

    diag = DiagSession(fl.k, ecu="EDC16", log=on_stage)
    diag.connect(speed=speed, is_stop=is_stop)

    readings = []
    try:
        for i in range(samples):
            if is_stop and is_stop():
                break
            _t.sleep(interval)
            try:
                # First iteration: dump all raw cells so the user can see
                # exactly what the ECU is returning (helps distinguish
                # "no response" from "response with 0 RPM").
                if i == 0:
                    raw = diag.read_group(grp)
                    if raw is None:
                        on_stage("  Group %d: no response (session lost?)." % grp)
                    elif not raw:
                        on_stage("  Group %d: empty response (ECU may not support this group)." % grp)
                    else:
                        parts = []
                        for _c in raw:
                            try:
                                _, _, _txt = _c.decoded
                                parts.append("f%d=%s" % (_c.fid, _txt))
                            except Exception:
                                parts.append("f%d(a=%d,b=%d)" % (_c.fid, _c.a, _c.b))
                        on_stage("  Group %d cells: %s" % (grp, ", ".join(parts)))
                rv = sm2_gpt.engine_speed(diag, grp)
            except (KLineError, Exception) as ex:  # noqa: BLE001 - report, keep sampling
                rv = None
                on_stage("  group read failed (%s)" % ex)
            readings.append(rv)
            # Distinguish None (no response) from 0.0 (ECU replied, not synced)
            if rv is None:
                rv_str = "n/a (no response)"
            elif rv < 1.0:
                rv_str = "0 rpm (ECU sees signal but crank not synchronised)"
            else:
                rv_str = "%.0f rpm" % rv
            on_stage("  RPM readback %d/%d: %s" % (i + 1, samples, rv_str))
    finally:
        if _gap_stop is not None:
            _gap_stop.set()      # stop the gap-pattern thread
        if stop_after:
            try:
                sm2_gpt.stop(ch, crank_pin, cam_pin, crank_only=crank_only)
                on_stage("GPT outputs disabled.")
            except Exception as ex:  # noqa: BLE001
                on_stage("GPT stop failed: %s" % ex)

    seen = [r for r in readings if r]
    recognized = any(r and r > 50 for r in seen)
    result = {"target_rpm": rpm, "readings": readings,
              "max_rpm": max(seen) if seen else 0.0,
              "recognized": recognized, "gpt_running": (not stop_after),
              "crank_pin": crank_pin, "cam_pin": cam_pin,
              "crank_only": crank_only, "items": items}
    if recognized:
        on_stage("ECU RECOGNISES crank/cam: engine speed read back "
                 "(max %.0f rpm). Safe to run EEPROM-find passes." % result["max_rpm"])
    else:
        on_stage("ECU did NOT report RPM - crank not recognised as uniform PWM. "
                 "Likely needs the gap pattern, different pins, or drive levels.")
    return result



def gpt_verify_and_find(fl, *, rpm=850, samples=6, interval=0.5,
                        crank_pin=None, cam_pin=None, group=None,
                        lo=0x7F8000, hi=0x800000, step=0x80,
                        speed="Low", allow_fast=False,
                        use_gap_pattern=False, force_eeprom=False,
                        crank_only=True,
                        on_stage=None, is_stop=None):
    """One-click: drive SM2 crank/cam GPT, confirm the ECU recognises the
    signals via measuring-block RPM, then sweep for the EEPROM with the
    signals still active.

    Sequence
    --------
    1. ``gpt_verify_running(..., stop_after=False)`` -- leaves GPT on.
    2. If ECU reported RPM > 50 (``recognized``): run ``find_eeprom``.
    3. Always disable GPT outputs on exit, even if step 2 raised.

    Returns
    -------
    dict with keys:
      ``gpt``      -- full result dict from :func:`gpt_verify_running`
      ``eeprom``   -- result dict from :func:`find_eeprom`, or ``None`` if
                     not recognised / skipped / stopped
      ``recognized`` -- convenience copy of ``gpt["recognized"]``
    """
    from . import sm2_gpt as _gpt
    on_stage = on_stage or (lambda s: None)
    _crank_pin = _gpt.DEFAULT_CRANK_PIN if crank_pin is None else crank_pin
    _cam_pin   = _gpt.DEFAULT_CAM_PIN   if cam_pin   is None else cam_pin

    # step 1 -- verify; leave GPT running
    gpt_result = gpt_verify_running(
        fl, rpm=rpm, samples=samples, interval=interval,
        crank_pin=_crank_pin, cam_pin=_cam_pin, group=group,
        stop_after=False,
        speed=speed, allow_fast=allow_fast,
        use_gap_pattern=use_gap_pattern, crank_only=crank_only,
        on_stage=on_stage, is_stop=is_stop)

    eeprom_result = None
    try:
        if not gpt_result["recognized"] and not force_eeprom:
            on_stage("ECU did not recognise crank/cam -- skipping EEPROM sweep."
                     " (enable 'Force EEPROM' to try anyway with GPT signals active)")
        elif is_stop and is_stop():
            on_stage("Stopped before EEPROM sweep.")
        else:
            if force_eeprom and not gpt_result["recognized"]:
                on_stage("Force EEPROM: sweeping despite no RPM readback "
                         "(GPT signals still active)...")
            else:
                on_stage("ECU is rotating. Starting EEPROM sweep with GPT active...")
            eeprom_result = find_eeprom(
                fl, lo=lo, hi=hi, step=step,
                speed="Medium", allow_fast=allow_fast,
                on_stage=on_stage, is_stop=is_stop)
    finally:
        # always kill the GPT outputs
        try:
            ch = getattr(fl.k, "_ch", None) or fl.k
            _gpt.stop(ch, _crank_pin, _cam_pin, crank_only=crank_only)
            on_stage("GPT outputs disabled.")
        except Exception as ex:  # noqa: BLE001
            on_stage("GPT stop failed: %s" % ex)

    return {"gpt": gpt_result, "eeprom": eeprom_result,
            "recognized": gpt_result["recognized"]}

def read_immo_coding(fl, *, regions=None, speed="Medium", allow_fast=True,
                     on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: open the LVL3 read session and readMemoryByAddress (0x23)
    the coding struct, the external coding mirror and the ImmCtl RAM state, then
    decode the immobiliser status byte (coding struct +0x56) and K5.

    Enabling first step for immo-off: read the live coding/immo image so the
    writable record and the off-value can be identified before any write.  Reads
    only -- nothing is written.

    Returns {"regions": {name: {"addr", "data"|None, "error"|None}},
             "immo_status": int|None, "immo_text": str, "k5": int|None}.
    """
    on_stage = on_stage or (lambda _m: None)
    regions = regions or _IMMO_CODING_REGIONS
    out = {"regions": {}, "immo_status": None, "immo_text": "unknown", "k5": None}

    on_stage("Opening read session (%s)..." % ("CAN/TP2.0" if getattr(fl.k, "is_tp20", False)
                                               else "K-line"))
    _open_read_session(fl, speed, allow_fast, is_stop)      # 0x86 + LVL3
    on_stage("Read session open (LVL3 read auth active).")

    for name, addr, size in regions:
        if is_stop and is_stop():
            break
        try:
            data = _read_span(fl, addr, size, is_stop)
            out["regions"][name] = {"addr": addr, "data": data, "error": None}
            on_stage("  %-13s 0x%06X +0x%03X -> %s%s"
                     % (name, addr, size, data[:16].hex(),
                        "..." if len(data) > 16 else ""))
        except Exception as ex:
            out["regions"][name] = {"addr": addr, "data": None, "error": str(ex)}
            on_stage("  %-13s 0x%06X refused (%s)" % (name, addr, ex))

    cs = out["regions"].get("coding_struct", {}).get("data")
    if cs and len(cs) > IMMO_STATUS_STRUCT_OFFSET:
        st = cs[IMMO_STATUS_STRUCT_OFFSET]
        out["immo_status"] = st
        out["immo_text"] = IMMO_STATUS_STATES.get(st, "unrecognised (0x%02X)" % st)
        on_stage("  -> immo status (struct+0x%02X = 0x%06X) = 0x%02X : %s"
                 % (IMMO_STATUS_STRUCT_OFFSET,
                    CODING_STRUCT_ADDR + IMMO_STATUS_STRUCT_OFFSET, st, out["immo_text"]))
        if len(cs) > K5_STRUCT_OFFSET + 1:
            out["k5"] = (cs[K5_STRUCT_OFFSET] << 8) | cs[K5_STRUCT_OFFSET + 1]
            on_stage("  -> K5 (struct+0x%02X) = 0x%04X" % (K5_STRUCT_OFFSET, out["k5"]))

    try:
        fl._try_quiet_stop()
    except Exception:
        pass

    if out["immo_status"] is not None:
        on_stage("RESULT: immo status 0x%02X (%s). Coding/immo image captured."
                 % (out["immo_status"], out["immo_text"]))
    else:
        on_stage("RESULT: coding struct not read -- see per-region status above.")
    return out


# Candidate ranges for the checksum-range probe.  0x31 C5 verifies its OWN
# descriptor-aligned ranges; the program BLOCK is 0x40000-0x17FFFF (self-header)
# but 0x31 C5 accepts only 0x40000-0x9FFFF and rejects the full block (7F 33 22),
# so the verifiable extent is finer than the block.  This probes what the ECU
# will actually checksum, especially the 0xA0000+ region (writable, but its
# verifiable sub-range is unknown).  All entries are (start, end_inclusive, label).
_CHECKSUM_PROBE_RANGES = [
    (0x040000, 0x09FFFF, "program 0x40000-0x9FFFF (ODIS; expect ACCEPT)"),
    (0x040000, 0x17FFFF, "program full 0x40000-0x17FFFF (expect REJECT)"),
    (0x0A0000, 0x17FFFF, "0xA0000-0x17FFFF"),
    (0x0A0000, 0x0FFFFF, "0xA0000-0xFFFFF"),
    (0x0A0000, 0x0BFFFF, "0xA0000-0xBFFFF"),
    (0x0A0000, 0x0BFEFF, "0xA0000-0xBFEFF"),
    (0x080000, 0x17FFFF, "0x80000-0x17FFFF"),
    (0x0C0000, 0x17FFFF, "0xC0000-0x17FFFF"),
    (0x0A0000, 0x0DFFFF, "0xA0000-0xDFFFF"),
]


def checksum_range_probe(fl, image=None, *, ranges=None, speed="Medium",
                         allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: find which [start,end] ranges the ECU's 0x31 C5 checksum
    routine accepts.  Opens the programming session (0x85 + LVL1) and, for each
    candidate range, sends 0x31 C5 <start> <end> <sum> and polls 0x33 C5, recording
    accepted (73 C5, valid range -- match or mismatch) vs rejected (7F 33 22 =
    conditionsNotCorrect, not a valid checksum range).  NOTHING is erased, written
    or transferred -- 0x31 C5 only computes/compares a sum.

    The checksum VALUE does not affect range-acceptance (a valid range with a wrong
    sum still returns 73 C5, just mismatch), so a real image is optional: if given,
    the correct sum is sent (so 00 = the ECU already holds that data); otherwise a
    dummy 0 is used and a valid range reports mismatch.

    Returns a list of (start, end, label, verdict)."""
    on_stage = on_stage or (lambda _m: None)
    if ranges is None:
        ranges = _CHECKSUM_PROBE_RANGES
    from .edc16 import fast_prime_from_image
    fip = fast_prime_from_image(image) if image is not None else False

    on_stage("Connecting + programming session (non-destructive)...")
    _open_session(fl, image, speed, allow_fast, fip, is_stop, on_stage)
    on_stage("Session open. Probing 0x31 C5 ranges (no erase, no write)...")

    results = []
    for (start, end, label) in ranges:
        if is_stop and is_stop():
            break
        cks = 0
        if image is not None and end + 1 <= len(image):
            try:
                _, cks = encrypt(image, start, end + 1)
            except Exception:
                cks = 0
        try:
            verdict = fl._checksum_probe_classify(start, end, cks)
        except Exception as ex:
            verdict = "error:%s" % ex
        on_stage("  0x%06X-0x%06X  %-38s -> %s" % (start, end, label, verdict))
        results.append((start, end, label, verdict))

    try:
        fl._finalize()
    except Exception:
        pass
    accepted = [r for r in results if r[3].startswith("accepted")]
    on_stage("Done. %d/%d ranges accepted by the 0x31 C5 routine."
             % (len(accepted), len(results)))
    return results


_SEED_PROBE_SUBFUNCS = [0x01, 0x03, 0x05, 0x07, 0x09, 0x0B, 0x0D, 0x0F, 0x11, 0x13]


def security_seed_probe(fl, *, subfuncs=None, speed="Medium", allow_fast=True,
                        on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE: enumerate the ECU's SecurityAccess levels.  Connects
    (default session) and, for each odd sub-function, sends 0x27 <sf> -- a seed
    request ONLY, never a key -- recording whether the ECU returns a seed (level
    exists) or NAKs (7F 27).  No key is sent, so nothing unlocks and nothing is
    written.  Known: 27 01/02 = write/SA2, 27 03/04 = read.  Any higher sub-function
    that returns a seed is a candidate level for the boot region (which the read
    gate refuses at the read/write levels).  Returns [(subfunc, verdict), ...]."""
    on_stage = on_stage or (lambda _m: None)
    if subfuncs is None:
        subfuncs = _SEED_PROBE_SUBFUNCS
    on_stage("Connecting (default session; non-destructive)...")
    fl.connect()
    on_stage("Requesting seeds (no keys sent)...")
    results = []
    for sf in subfuncs:
        if is_stop and is_stop():
            break
        try:
            v = fl._seed_probe_one(sf)
        except Exception as ex:
            v = "error:%s" % ex
        known = {0x01: " (write/SA2)", 0x03: " (read)"}.get(sf, "")
        on_stage("  27 %02X%s -> %s" % (sf, known, v))
        results.append((sf, v))
    try:
        fl._finalize()
    except Exception:
        pass
    got = [r for r in results if r[1].startswith("seed") or r[1].startswith("open")]
    on_stage("Done. %d of %d sub-functions are live security levels."
             % (len(got), len(results)))
    return results


def read_probe(fl, *, include_program=True, include_mpc=False, speed="Medium",
               allow_fast=True, on_stage=None, is_stop=None):
    """NON-DESTRUCTIVE read-grant probe: open ONE read (0x86) session, then for
    each enabled range issue requestUpload(0x35) and report whether the ECU grants
    it (02 75 FF).  The read-side analog of the write dry run.  Answers 'can the
    program area / MPC be READ over OBD?' without transferring the data.

    ONE session for all ranges (a fresh session per range re-inited the link
    after security access -- Bear's report); between ranges the pending upload is
    closed with transferExit(0x37) so the next requestUpload starts clean.
    Returns {name: bool}."""
    on_stage = on_stage or (lambda _m: None)
    ranges = []
    if include_program:
        ranges.append(("program", PROGRAM_START, PROGRAM_END - PROGRAM_START))
    if include_mpc:
        ranges.append(("mpc", MPC_ADDR, MPC_ENC[1] - MPC_ENC[0]))
    results = {}
    on_stage("Opening read session (%s)..." % ("CAN/TP2.0" if getattr(fl.k, "is_tp20", False)
                                               else "K-line"))
    _open_read_session(fl, speed, allow_fast, is_stop)
    on_stage("Read session open (0x86 + read security).")
    for i, (name, addr, size) in enumerate(ranges):
        if is_stop and is_stop():
            raise StopRequested("read-probe stopped")
        on_stage("RequestUpload %s @0x%06X size=0x%X (0x35)..." % (name, addr, size))
        try:
            fl._request_upload(addr, size)
            on_stage("  -> 0x35 ACCEPTED (02 75 FF): %s IS readable over OBD" % name)
            results[name] = True
            try:
                fl._request_transfer_exit()          # close the granted upload (0x37)
                on_stage("  -> upload closed (transferExit).")
            except Exception as ex:
                on_stage("  -> (transferExit after grant not clean: %s)" % ex)
        except Exception as ex:
            on_stage("  -> 0x35 REFUSED: %s (%s not readable this way)" % (ex, name))
            results[name] = False
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    on_stage("Read-probe done: %s"
             % ", ".join("%s=%s" % (k, "READABLE" if v else "refused")
                         for k, v in results.items()))
    return results


# Whole-chip read ranges. External = the full 2 MB flash from 0x0 (boot/low +
# program + cal). Internal = the full 0x81000 MPC image from VMA 0x400000.
EXT_FULL = (0x000000, FULL_SIZE)            # 0x0 .. 0x200000  (2 MB)
INT_FULL = (MPC_VMA, 0x81000)               # 0x400000 .. 0x481000  (528 KB)

# When a 64 KB upload chunk is refused, bisect down to this before 0xFF-padding.
# The readable internal-flash base (0x404000) is NOT 64 KB-aligned, so a plain
# 64 KB chunk at 0x400000 is wholly refused and loses the real 0x404000-0x410000
# data; bisecting recovers the readable sub-range (0x404000 is 0x4000-aligned).
_MIN_READ_CHUNK = 0x4000


def _read_one_upload(fl, addr, sz, is_stop):
    """requestUpload(addr, sz) then stream exactly sz bytes. Raises KLineError if
    the ECU refuses the range (caller decides whether to bisect/pad)."""
    fl._request_upload(addr, sz)
    got = bytearray()
    done = [False]
    while not done[0]:
        if is_stop and is_stop():
            try:
                fl._try_quiet_stop()
            except Exception:
                pass
            raise StopRequested("read stopped")
        got.extend(fl._read_flash_packet(done))
    del got[sz:]
    if len(got) < sz:
        got.extend(b"\xFF" * (sz - len(got)))
    try:
        fl._request_transfer_exit()                 # per-upload exit (like MPPS)
    except Exception:
        pass
    return bytes(got)


def _read_bisect(fl, addr, sz, on_stage, is_stop, counters):
    """Read [addr, addr+sz). On refusal, split (aligned to _MIN_READ_CHUNK) and
    retry each half so a non-readable sub-region only 0xFF-pads that part while
    readable neighbours are still captured. A negative requestUpload leaves no
    pending upload and does not end the session, so there is no teardown here."""
    try:
        data = _read_one_upload(fl, addr, sz, is_stop)
        counters["ok"] += sz
        return data
    except StopRequested:
        raise
    except KLineError as ex:
        if sz <= _MIN_READ_CHUNK:
            on_stage("  -> refused 0x%06X-0x%06X (%s) -- 0xFF" % (addr, addr + sz - 1, ex))
            counters["refused"] += sz
            return b"\xFF" * sz
        half = sz // 2
        half -= half % _MIN_READ_CHUNK
        if half <= 0:
            half = _MIN_READ_CHUNK
        left = _read_bisect(fl, addr, half, on_stage, is_stop, counters)
        right = _read_bisect(fl, addr + half, sz - half, on_stage, is_stop, counters)
        return left + right


def read_full(fl, *, include_program=True, include_mpc=False, full=False,
              upload_chunk=0x10000, speed="Medium", allow_fast=True,
              virtual_boot=False, on_stage=None, on_percent=None, is_stop=None):
    """REAL non-destructive read over OBD (0x86 read session, proven upload loop).

    ``full=True`` reads the WHOLE chips: external = 0x0..0x200000 (2 MB), internal
    = VMA 0x400000..0x481000 (0x81000).  ``full=False`` reads the sub-ranges
    (program 0x40000..0x17FFFF and the MPC 0x7C000 write-window) as before.

    Each range is uploaded in ``upload_chunk``-byte requestUpload transfers
    (default 64 KB) -- required over CAN/TP2.0 (a single big upload drops the
    session) and harmless over K-line.  Returns {name: bytes}, each exactly its
    range size."""
    on_stage = on_stage or (lambda _m: None)
    ranges = []
    if full:
        if include_program:
            ranges.append(("external", EXT_FULL[0], EXT_FULL[1]))
        if include_mpc:
            ranges.append(("internal", INT_FULL[0], INT_FULL[1]))
    else:
        if include_program:
            ranges.append(("program", PROGRAM_START, PROGRAM_END - PROGRAM_START))
        if include_mpc:
            ranges.append(("mpc", MPC_ADDR, MPC_ENC[1] - MPC_ENC[0]))
    chunk = upload_chunk or 0x10000
    out = {}
    on_stage("Opening read session (%s)..." % ("CAN/TP2.0" if getattr(fl.k, "is_tp20", False)
                                               else "K-line"))
    _open_read_session(fl, speed, allow_fast, is_stop)
    for name, base, total in ranges:
        if is_stop and is_stop():
            raise StopRequested("read stopped")
        region = bytearray()
        n = (total + chunk - 1) // chunk
        counters = {"ok": 0, "refused": 0}
        for ci in range(n):
            off = ci * chunk
            sz = min(chunk, total - off)
            addr = base + off
            on_stage("Reading %s 0x%06X-0x%06X (%d/%d)..."
                     % (name, addr, addr + sz - 1, ci + 1, n))
            # Stream granted 64 KB chunks whole; bisect only refused ones so a
            # readable sub-range inside a partly-refused chunk is still captured.
            region.extend(_read_bisect(fl, addr, sz, on_stage, is_stop, counters))
            if on_percent and total:
                on_percent(min(100, (off + sz) * 100 // total))
        if counters["refused"]:
            on_stage("  (%s: 0x%X of 0x%X bytes refused -- 0xFF-padded; readable "
                     "sub-ranges inside partly-refused chunks were still recovered)"
                     % (name, counters["refused"], total))
        out[name] = bytes(region)
        on_stage("  %s read complete: 0x%X bytes" % (name, len(out[name])))
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    # Virtual boot: the boot/low region (0x10000-0x3FEFF) is unreadable over OBD,
    # so it comes back 0xFF-padded. If asked, reconstruct it from the boot DB by
    # matching the read image's SW/part number (see ovtsuite.bootdb).
    if virtual_boot and full and out.get("external"):
        ext = bytearray(out["external"])
        b0, b1 = 0x10000, 0x10000 + 0x2FF00               # boot data extent
        cur = bytes(ext[b0:b1])
        if len(ext) >= b1 and cur.count(0xFF) > (b1 - b0) * 3 // 4:
            try:
                from .. import bootdb
                if bootdb.available():
                    db = bootdb.load()
                    did, part, proj = bootdb.identify(ext)
                    vb = db.virtual_boot(dataset_id=did, part=part, project=proj)
                    if vb:
                        boot, quality, src = vb
                        ext[b0:b1] = boot
                        out["external"] = bytes(ext)
                        out["boot_virtual"] = {"quality": quality, "source": src,
                                               "did": did, "part": part}
                        on_stage("Virtual boot: %s match (%s) -- boot 0x%06X-0x%06X "
                                 "filled from the boot DB." % (quality, src, b0, b1 - 1))
                    else:
                        on_stage("Virtual boot: no DB match for did=%s part=%s -- boot "
                                 "left 0xFF." % (did, part))
                else:
                    on_stage("Virtual boot: %s -- boot left 0xFF." % bootdb.missing_text())
            except Exception as ex:
                on_stage("Virtual boot skipped (%s)." % ex)
    on_stage("Read done: %s" % ", ".join("%s=0x%X" % (k, len(v)) for k, v in out.items()
                                         if isinstance(v, (bytes, bytearray))))
    return out
