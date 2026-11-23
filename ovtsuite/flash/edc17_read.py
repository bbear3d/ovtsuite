# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC17 (CP14) identification and flash read over OBD: KWP2000 on CAN TP 2.0.

What is known, from a sniffed run of a third-party tool on an EDC17CP14
(Jetta TDI, ``03L997030N``) -- see ``tests/test_edc17_read.py``, which replays
those frames:

* TP 2.0 to logical address 0x01 (setup on 0x200, tester 0x740, ECU 0x300),
  KWP2000 inside it. Identification is ``1A 97`` (engine code), ``1A 8A``
  (system data), ``1A 90`` (VIN), ``1A 9B`` (part number, software, system
  type) and ``1A 9C`` (flash counters).
* Security is the EDC16 read level: ``27 03`` seed, ``27 04`` key, and the key
  is exactly :func:`ovtsuite.flash.edc16.edc16_lvl3_key`. Then ``10 86``.
* The tool then asked for memory with ``35 aa aa aa 00 ss ss ss`` -- the same
  requestUpload the EDC16 read uses -- in 0x1000 steps over 0x000000-0x1FFFFF,
  0x800000-0x9FFFFF and 0x200000-0x3FFFFF. **Every request was refused with
  7F 35 52.** So what the ECU wants before it uploads is NOT known; the sniff
  has no successful read.

The 0x000000 and 0x800000 windows are the two flash banks of a 4 MB EDC17
image (0x80000000 and 0x80800000, 2 MB each, the second at file offset
0x200000 as in ``sgo.image``).

So this module does the parts that are known (identify, unlock, the EDC16
upload machinery over TP 2.0) and keeps a :func:`probe` that tries each way
into memory and reports exactly what the ECU answered, so the first run on a
car shows what is missing. :func:`read_image` refuses to hand back an image
when the ECU gave up nothing, rather than a file of 0xFF.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import crypto as C
from . import edc16 as E
from . import edc16_read as R
from .edc16 import StopRequested, KLineError, RangeRefused
from .transport import KLineTimeout

#: EDC17CP14 write-level SA2 tape, read out of three .sgm containers that
#: otherwise share nothing (different projects P680 and P908, different part
#: numbers/software: 03L906022LS/3610, 03L906022QF/6211, 03L906022KB/7088) --
#: all three carry this exact 12-byte tape, so it is not per-part or
#: per-project. Confirmed on a real EDC17CP14 (03L997030N/9983) on the bench
#: 2026-11-15: ``27 01`` seed 4F271309 -> this tape's key BFD0E7E8 -> ``27 02``
#: answered ``67 02 34`` (access granted), in the programming session (0x85),
#: sub-functions 01/02 -- the SAME sub-functions EDC16's write level uses,
#: just with this algorithm instead of edc16_lvl1_key (exactly as read-level
#: security reuses EDC16's 27 03/04 with edc16_lvl3_key -- see module
#: docstring). The read-level tape would need a .sgm with the read SA2
#: sequence; none of ours has one, so only write-level is implemented here.
SA2_TAPE_CP14 = bytes.fromhex("6805814a05870a221289494c")

#: image offset where the second flash bank sits in a 4 MB file
BANK_B_OFFSET = 0x200000
IMAGE_SIZE = 0x400000


@dataclass(frozen=True)
class Bank:
    name: str
    addr: int          # ECU address (24 bit, as requestUpload takes it)
    size: int
    offset: int        # offset in the image file


BANKS = (Bank("flash bank A (0x80000000)", 0x000000, 0x200000, 0x000000),
         Bank("flash bank B (0x80800000)", 0x800000, 0x200000, BANK_B_OFFSET))

#: where the probe asks: both bank starts and ends, and the 0x200000 window
#: the third-party tool also tried
PROBE_ADDRS = (0x000000, 0x001000, 0x100000, 0x1FF000,
               0x200000, 0x3FF000, 0x800000, 0x9FF000)


# ------------------------------------------------------------ identification
@dataclass
class Edc17Ident:
    engine_code: str = ""
    system_data: str = ""       # 1A 8A, e.g. 37/1/EDC17_CP14/5/P1152//C1152K3ZL///
    family: str = ""            # EDC17_CP14
    vin: str = ""
    part_number: str = ""
    software: str = ""
    system_type: str = ""       # R4 2.0l TDI G000SG
    flash_attempts: Optional[int] = None
    flash_completed: Optional[int] = None
    raw: dict = field(default_factory=dict)     # local id -> reply bytes (after 5A xx)

    def summary(self) -> str:
        rows = [("VIN", self.vin), ("Hardware number", self.part_number),
                ("Software number", self.software), ("System type", self.system_type),
                ("Engine code", self.engine_code), ("System data", self.family or self.system_data)]
        if self.flash_attempts is not None:
            rows += [("Flash attempts", "%d" % self.flash_attempts),
                     ("Flash completed", "%d" % self.flash_completed)]
        return "\n".join("%-16s %s" % (k + ":", v) for k, v in rows if v != "")


def _text(b: bytes) -> str:
    return bytes(x for x in b if 32 <= x < 127).decode("ascii").strip()


def parse_ident(raw: dict) -> Edc17Ident:
    """``raw``: local identifier -> the reply after ``5A <lid>`` (bytes)."""
    i = Edc17Ident(raw=dict(raw))
    if 0x97 in raw:
        i.engine_code = _text(raw[0x97])
    if 0x8A in raw:
        i.system_data = _text(raw[0x8A].split(b"\x00")[0])
        m = re.search(r"EDC17_?\w+?(?=/)", i.system_data)
        i.family = m.group(0) if m else ""
    if 0x90 in raw:
        i.vin = _text(raw[0x90])
    if 0x9B in raw:
        d = raw[0x9B]
        # 11 characters of part number, a space, the 4-character software
        # number; then ECU-specific bytes and the system type text
        i.part_number = _text(d[:11])
        i.software = _text(d[12:16])
        m = re.search(rb"[A-Z]\d [\d.,]+l .*", d[16:])
        if m:
            i.system_type = _text(m.group(0))
    if 0x9C in raw and len(raw[0x9C]) >= 3:
        i.flash_attempts = raw[0x9C][1]
        i.flash_completed = raw[0x9C][2]
    return i


def _kwp(fl, payload: bytes, timeout_ms: int = 3000) -> bytes:
    """One KWP request on the TP 2.0 channel; a responsePending (7F xx 78) is
    waited out. Returns the positive reply or raises KLineError on a 7F."""
    tp = fl.k.tp
    tp.send(bytes(payload))
    for _ in range(40):
        r = tp.recv(timeout_ms)
        if len(r) >= 3 and r[0] == 0x7F and r[1] == payload[0]:
            if r[2] == 0x78:
                continue
            raise KLineError("%s refused: 7F %02X %02X (%s)"
                             % (payload.hex(" "), r[1], r[2], E._nrc_name(r[2])))
        return bytes(r)
    raise KLineTimeout("%s: the ECU kept answering 'pending'" % payload.hex(" "))


def open_write_session(fl, on_stage=None):
    """Enter the programming session (0x85) and unlock write-level security
    (27 01 seed / 27 02 key, the SA2 tape) -- confirmed on real hardware (see
    SA2_TAPE_CP14 above). ``fl`` must already be connected (:meth:`connect`
    or after :func:`identify`).

    Over TP 2.0 the 0x85 session resets the ECU's transport, so the channel
    is set up again (``reset_channel``) before the security exchange, exactly
    as EDC16's own write path does (``Edc16Flash.open_program_session``) --
    on the stale channel the first post-session request gets no answer."""
    on_stage = on_stage or (lambda _m: None)
    r = _kwp(fl, bytes([0x10, 0x85]))
    if len(r) < 2 or r[0] != 0x50:
        raise KLineError("EDC17 could not enter the programming session: %s" % r.hex(" "))
    on_stage("Programming session (10 85) entered; re-opening the TP 2.0 channel...")
    fl.k.reset_channel()
    r = _kwp(fl, bytes([0x27, 0x01]))
    if len(r) < 6 or r[0] != 0x67:
        raise KLineError("EDC17 write-level requestSeed: unexpected %s" % r.hex(" "))
    seed = int.from_bytes(r[2:6], "big")
    key = C.sa2_execute(SA2_TAPE_CP14, seed)
    r = _kwp(fl, bytes([0x27, 0x02]) + key.to_bytes(4, "big"))
    if len(r) < 2 or r[0] != 0x67:
        raise KLineError("EDC17 write-level sendKey: unexpected %s" % r.hex(" "))
    on_stage("Write-level security access granted (seed %08X -> key %08X)." % (seed, key))


def identify(fl, on_stage=None) -> Edc17Ident:
    """Open the TP 2.0 channel and read the five identification blocks."""
    on_stage = on_stage or (lambda _m: None)
    on_stage("Connecting (%s)..." % fl.bus_name())
    fl.connect()
    raw = {}
    for lid in (0x97, 0x8A, 0x90, 0x9B, 0x9C):
        try:
            r = _kwp(fl, bytes([0x1A, lid]))
        except KLineError as ex:
            on_stage("Identification %02X not answered (%s)." % (lid, ex))
            continue
        if len(r) >= 2 and r[0] == 0x5A and r[1] == lid:
            raw[lid] = r[2:]
    return parse_ident(raw)


# --------------------------------------------------------------------- probe
@dataclass
class ProbeResult:
    method: str          # "35 requestUpload" | "23 readMemoryByAddress"
    addr: int
    ok: bool
    detail: str          # the data's first bytes, or what the ECU said
    data: bytes = b""


def probe(fl, addrs=PROBE_ADDRS, size: int = 0x10, on_stage=None,
          is_stop=None, use_23: bool = True, session: str = "read") -> List[ProbeResult]:
    """Try to read ``size`` bytes at each address by requestUpload (and
    readMemoryByAddress). Nothing is written; the answer to every request is
    reported.

    ``session``: "read" opens 27 03/04 + 10 86 (:meth:`open_read_session`);
    "write" opens 10 85 + 27 01/02 with the SA2 tape (:func:`open_write_session`,
    confirmed to grant access on real hardware -- the read session refuses
    every request with 7F xx 52, the write session with 7F xx 90; neither NRC
    is in KWP2000's standard table, and what unlocks an actual upload is still
    unknown)."""
    on_stage = on_stage or (lambda _m: None)
    if session == "write":
        fl.connect()
        open_write_session(fl, on_stage)
    else:
        fl.open_read_session("Medium", False, is_stop, reopen=False)
    out = []
    for a in addrs:
        if is_stop and is_stop():
            raise StopRequested("probe stopped")
        try:
            data = R._read_one_upload(fl, a, size, is_stop)
            res = ProbeResult("35 requestUpload", a, True, data[:16].hex(" "), data)
        except StopRequested:
            raise
        except KLineTimeout as ex:
            res = ProbeResult("35 requestUpload", a, False, "no answer (%s)" % ex)
            out.append(res)
            on_stage("%s at 0x%06X: %s" % (res.method, a, res.detail))
            break                                   # the link is gone: stop asking
        except KLineError as ex:
            res = ProbeResult("35 requestUpload", a, False, str(ex))
        out.append(res)
        on_stage("%s at 0x%06X: %s" % (res.method, a, res.detail))
        if use_23:
            try:
                data = fl._read_memory_by_address(a, min(size, 0xFF))
                res = ProbeResult("23 readMemoryByAddress", a, True, data[:16].hex(" "), data)
            except StopRequested:
                raise
            except KLineError as ex:
                res = ProbeResult("23 readMemoryByAddress", a, False, str(ex))
            out.append(res)
            on_stage("%s at 0x%06X: %s" % (res.method, a, res.detail))
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------- read
def read_image(fl, banks=BANKS, *, upload_chunk: int = 0x10000, reopen: bool = True,
               on_stage=None, on_percent=None, is_stop=None) -> dict:
    """Read ``banks`` into a 4 MB image (0xFF where nothing was read).

    Returns ``{"image": bytes, "padded": [(addr, size, why)], "ok": bytes_read,
    "padded_bytes": n, "complete": bool, "warning": str or None}``. A range the
    ECU refused is left 0xFF and listed in ``padded``; ``complete`` is False
    then, and ``warning`` says plainly how much of the 4 MB file is NOT a read
    (a single-bank ECU, which refuses the second bank, gives a 4 MB file that
    is half blank: callers must show the warning, not save it as a full read).
    Raises KLineError when the ECU refused every upload: an image of 0xFF is
    never returned as if it were a read."""
    on_stage = on_stage or (lambda _m: None)
    on_stage("Connecting (%s)..." % fl.bus_name())
    fl.open_read_session("Medium", False, is_stop, reopen=reopen)
    image = bytearray(b"\xFF" * IMAGE_SIZE)
    counters = {"ok": 0, "refused": 0, "padded": []}
    total = sum(b.size for b in banks)
    done = 0
    for b in banks:
        n = (b.size + upload_chunk - 1) // upload_chunk
        for ci in range(n):
            if is_stop and is_stop():
                raise StopRequested("read stopped")
            addr = b.addr + ci * upload_chunk
            sz = min(upload_chunk, b.addr + b.size - addr)
            on_stage("Reading %s, part %d of %d (0x%06X-0x%06X)..."
                     % (b.name, ci + 1, n, addr, addr + sz - 1))
            data = R._read_bisect(fl, addr, sz, on_stage, is_stop, counters)
            off = b.offset + (addr - b.addr)
            image[off:off + sz] = data
            done += sz
            if on_percent:
                on_percent(min(100, done * 100 // total))
    try:
        fl._try_quiet_stop()
    except Exception:
        pass
    if counters["ok"] == 0:
        why = counters["padded"][0][2] if counters["padded"] else "no data"
        raise KLineError("The ECU refused every upload (%s); nothing was read. "
                         "Run the probe to see what it will answer." % why)
    on_stage("Read complete: %d KB read, %d KB refused."
             % (counters["ok"] // 1024, counters["refused"] // 1024))
    padded = counters["padded"]
    warning = padded_warning(padded, counters["ok"])
    if warning:
        on_stage("WARNING: " + warning)
    return {"image": bytes(image), "padded": padded, "ok": counters["ok"],
            "padded_bytes": counters["refused"], "complete": not padded,
            "warning": warning}


def padded_warning(padded, ok: int) -> Optional[str]:
    """The sentence that says a read is NOT the whole flash, or None when
    nothing was padded. Names the ranges (merged) and the share left 0xFF."""
    if not padded:
        return None
    runs = []
    for addr, size, _why in sorted(padded):
        if runs and runs[-1][1] == addr:
            runs[-1][1] = addr + size
        else:
            runs.append([addr, addr + size])
    lost = sum(b - a for a, b in runs)
    where = ", ".join("0x%06X-0x%06X" % (a, b - 1) for a, b in runs[:6])
    if len(runs) > 6:
        where += ", ..."
    return ("INCOMPLETE READ: the ECU refused %d KB (%s); that part of the file is 0xFF "
            "padding, not flash contents (%d KB were really read). Do not use this file "
            "as a full image or write it back." % (lost // 1024, where, ok // 1024))


def make_flash(can, dest: int = 0x01, log=None) -> "E.Edc16Flash":
    """An :class:`Edc16Flash` on a TP 2.0 link over the CAN channel ``can``."""
    from .tp20_transport import CanKLine
    fl = E.Edc16Flash(CanKLine(can, dest=dest, log=log), log=log)
    fl._fast = True
    return fl


# ----------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    from . import j2534 as J
    ap = argparse.ArgumentParser(description="EDC17 identify / probe / read over CAN TP 2.0")
    ap.add_argument("action", choices=("ident", "probe", "read"))
    ap.add_argument("--device", default="SM2",
                    help="J2534 device: an index, or part of its name (default SM2)")
    ap.add_argument("--session", choices=("read", "write"), default="read",
                    help="probe only: which security level to open first")
    ap.add_argument("--out", default="edc17_read.bin")
    a = ap.parse_args(argv)
    devs = J.list_devices()        # a 32-bit DLL under 64-bit Python goes via the bridge
    for i, d in enumerate(devs):
        print("%d: %s" % (i, d.label))
    if a.device.isdigit() and int(a.device) < len(devs):
        pick = devs[int(a.device)]
    else:
        pick = next((d for d in devs if a.device.lower() in d.label.lower()), None)
    if pick is None:
        print("no J2534 device matches %r" % a.device)
        return 2
    with J.open_device(pick) as dev:
        ch = dev.connect(J.CAN, 500000)
        ch.pass_all()          # J2534 delivers nothing without a filter (OpenPort)
        fl = make_flash(ch, log=print)
        try:
            if a.action == "ident":
                print(identify(fl, print).summary())
            elif a.action == "probe":
                for r in probe(fl, on_stage=print, session=a.session):
                    pass
            else:
                res = read_image(fl, on_stage=print, on_percent=lambda p: print("%d%%" % p))
                out = a.out
                if not res["complete"]:
                    # never leave a half-blank file looking like a whole read
                    out = a.out + ".INCOMPLETE"
                    print(res["warning"])
                open(out, "wb").write(res["image"])
                print("wrote", out)
        finally:
            fl.k.close()
            ch.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
