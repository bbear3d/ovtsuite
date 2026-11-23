# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 block read over OBD (read session 10 86, requestUpload 0x35).

Reads the blocks the caller picks (the flasher's Boot / Program / Calibration /
MPC boxes) from :mod:`.edc16_blocks`. A block the ECU will not upload is never
asked for: boot is refused over OBD, so ticking Boot fills the boot region from
the boot database (a "virtual boot") instead of reading it.

Each range is read as ``upload_chunk``-byte uploads (64 KB by default; over
CAN/TP 2.0 one large upload drops the session). An upload the ECU refuses
(7F 35 / 7F 36) is split down to 16 KB, so a readable part of a partly refused
chunk is still read, and only the refused remainder is 0xFF. No answer, or an
upload that stays short, fails the read: 0xFF is never put in place of data
that was not read.
"""
from . import edc16_blocks as B
from .edc16 import StopRequested, KLineError, RangeRefused
from .transport import KLineTimeout

#: a refused upload is split down to this before it is padded
_MIN_READ_CHUNK = 0x4000


def _read_one_upload(fl, addr, sz, is_stop):
    """requestUpload(addr, sz) then stream exactly sz bytes. Raises KLineError
    (RangeRefused for a refusal) when it cannot."""
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
        # the upload ended early: the rest was never sent, so it is not padded
        raise KLineError("EDC16 upload ended after 0x%X of 0x%X bytes" % (len(got), sz))
    try:
        fl._request_transfer_exit()                 # per-upload exit (like MPPS)
    except Exception:
        pass
    return bytes(got)


def _dead_link(ex) -> bool:
    """No answer / the channel is gone -- as opposed to a refusal or a damaged
    packet, which another (smaller) upload can get past."""
    return isinstance(ex, KLineTimeout) or "disconnected" in str(ex).lower()


def _read_bisect(fl, addr, sz, on_stage, is_stop, counters):
    """Read [addr, addr+sz), splitting a refused or damaged upload in halves
    (aligned to _MIN_READ_CHUNK). Only a refusal is padded, and only at the
    smallest size; no answer fails at once, and a damaged upload that stays
    damaged at the smallest size fails the read."""
    where = "0x%06X-0x%06X" % (addr, addr + sz - 1)
    try:
        data = _read_one_upload(fl, addr, sz, is_stop)
        counters["ok"] += sz
        return data
    except StopRequested:
        raise
    except RangeRefused as ex:
        # a refusal leaves no upload pending and the session open
        if sz <= _MIN_READ_CHUNK:
            on_stage("The ECU refused %s (%s); it is left blank (0xFF)." % (where, ex))
            counters["refused"] += sz
            counters["padded"].append((addr, sz, str(ex)))
            return b"\xFF" * sz
    except KLineError as ex:
        if _dead_link(ex):
            raise KLineTimeout("EDC16 read stopped: no answer from the ECU at %s (%s); "
                               "the read is incomplete" % (where, ex))
        if sz <= _MIN_READ_CHUNK:
            raise KLineError("EDC16 read stopped: %s could not be read (%s); "
                             "the read is incomplete" % (where, ex))
        on_stage("%s did not read cleanly (%s); reading it in smaller pieces..."
                 % (where, ex))
        try:
            fl._request_transfer_exit()             # end the broken upload first
        except Exception:
            pass
    half = sz // 2
    half -= half % _MIN_READ_CHUNK
    if half <= 0:
        half = _MIN_READ_CHUNK
    left = _read_bisect(fl, addr, half, on_stage, is_stop, counters)
    right = _read_bisect(fl, addr + half, sz - half, on_stage, is_stop, counters)
    return left + right


#: what the status line calls each block
_NAMES = {"boot": "boot block", "program": "program", "cal": "calibration",
          "mpc": "internal flash (MPC)"}


def _name(b) -> str:
    return _NAMES.get(b.key, b.label.lower())


def _resolve(blocks):
    out = []
    for b in blocks:
        out.append(b if isinstance(b, B.Block) else B.get(b))
    order = {blk.key: i for i, blk in enumerate(B.ALL)}
    return sorted(out, key=lambda blk: order.get(blk.key, len(order)))


def _virtual_boot(ext, on_stage):
    """Fill the boot data extent of ``ext`` (a bytearray) from the boot DB.
    Returns the boot_virtual dict, or None when nothing was filled."""
    b0, b1 = B.BOOT.data
    try:
        from .. import bootdb
        if not bootdb.available():
            on_stage("Boot left blank: %s." % bootdb.missing_text())
            return None
        db = bootdb.load()
        did, part, proj = bootdb.identify(bytes(ext))
        vb = db.virtual_boot(dataset_id=did, part=part, project=proj)
    except Exception as ex:
        on_stage("Boot left blank: the boot database could not be used (%s)." % ex)
        return None
    if not vb:
        on_stage("Boot left blank: the boot database has no boot for this ECU "
                 "(dataset %s, part %s)." % (did, part))
        return None
    boot, quality, src = vb
    ext[b0:b1] = boot
    on_stage("Boot 0x%06X-0x%06X filled from the boot database (%s match, %s)."
             % (b0, b1 - 1, quality, src))
    return {"quality": quality, "source": src, "did": did, "part": part}


def read_blocks(fl, blocks=("program", "cal", "mpc"), *, upload_chunk=0x10000,
                speed="Medium", allow_fast=True, on_stage=None, on_percent=None,
                is_stop=None):
    """Read ``blocks`` (keys or edc16_blocks.Block) in one read session.

    Returns a dict: ``external`` -- the 2 MB external image (0xFF outside the
    blocks read), present when an external block was asked for; ``internal`` --
    the 0x81000 MPC image, when MPC was; ``boot_virtual`` -- how the boot was
    filled, when Boot was ticked and the boot DB matched; ``padded`` -- the
    (addr, size, why) ranges the ECU refused."""
    on_stage = on_stage or (lambda _m: None)
    chosen = _resolve(blocks)
    if not chosen:
        raise ValueError("no EDC16 block selected to read")
    reads = [b for b in chosen if b.readable]
    chunk = upload_chunk or 0x10000
    images = {}
    for b in chosen:
        if b.chip not in images:
            size = B.MPC_IMAGE_SIZE if b.chip == B.INTERNAL else B.FULL_SIZE
            images[b.chip] = bytearray(b"\xFF" * size)
    for b in chosen:
        if not b.readable:
            on_stage("%s is not readable over OBD, so it is not asked for." % b.label)

    total = sum(b.read[1] - b.read[0] for b in reads)
    done_bytes = 0
    padded = []
    if reads:
        on_stage("Connecting (%s)..." % fl.bus_name())
        fl.open_read_session(speed, allow_fast, is_stop)
        for b in reads:
            start, end = b.read
            img = images[b.chip]
            counters = {"ok": 0, "refused": 0, "padded": []}
            n = (end - start + chunk - 1) // chunk
            for ci in range(n):
                if is_stop and is_stop():
                    raise StopRequested("read stopped")
                addr = start + ci * chunk
                sz = min(chunk, end - addr)
                on_stage("Reading the %s, part %d of %d (0x%06X-0x%06X)..."
                         % (_name(b), ci + 1, n, addr, addr + sz - 1))
                data = _read_bisect(fl, addr, sz, on_stage, is_stop, counters)
                img[addr - b.base:addr - b.base + sz] = data
                done_bytes += sz
                if on_percent and total:
                    on_percent(min(100, done_bytes * 100 // total))
            for paddr, psz, why in counters["padded"]:
                on_stage("%s: 0x%06X-0x%06X padded with 0xFF -- the ECU refused it (%s)"
                         % (_name(b).capitalize(), paddr, paddr + psz - 1, why))
            padded.extend(counters["padded"])
            on_stage("The %s is read (%d KB)." % (_name(b), (end - start) // 1024))
        try:
            fl._try_quiet_stop()
        except Exception:
            pass

    out = {"padded": padded}
    if B.EXTERNAL in images:
        out["external"] = bytes(images[B.EXTERNAL])
    if B.INTERNAL in images:
        out["internal"] = bytes(images[B.INTERNAL])
    if B.BOOT in chosen:
        ext = images[B.EXTERNAL]
        vb = _virtual_boot(ext, on_stage)
        if vb:
            out["external"] = bytes(ext)
            out["boot_virtual"] = vb
    on_stage("Read complete.")
    return out
