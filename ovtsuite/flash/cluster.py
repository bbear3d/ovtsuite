# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Instrument cluster (VAG C6 A6, 4F0910930x) OBD read + identify over CAN.

KWP2000 (ISO 14230) services carried inside VW TP 2.0 on a raw CAN channel --
the *same* transport the Climatronic module uses, so this reuses the suite's
validated device + TP2.0 layers wholesale (see :mod:`ovtsuite.flash.climatronic`
for the interface enumeration, ``open_interface`` and the ``CanTap`` debug wrap;
they are imported here, not duplicated).

WHAT IS KNOWN (solid):
* Transport: TP 2.0 / KWP2000 over CAN, logical dest **0x17** (VAG instrument
  cluster).  Identical framing to the climatronic path, which is bench-validated.
* Flash map: taken from the factory SGO containers for this part (SW 0110/0170/
  0240).  Two sections -- **sec0 @0x007000 (~0x39000 B)** and
  **sec1 @0x090000 (~0x1B0000 B)** -- a ~2.3 MB 32-bit MCU image; 0x0..0x7000
  (vectors/boot) is not in the container.

WHAT IS NOT YET PINNED (must come from the real module or a capture):
* **SecurityAccess seed->key**.  It could NOT be reverse engineered statically:
  the SGO section payloads are AES-128-ECB with a fixed external key that is not
  in the SGOs and not in the local ODIS install, so the cluster firmware can't be
  disassembled from them (see project doc 4f0910930c-cluster-sgo-and-speedo.md).
  ``unlock`` is therefore *pluggable*: pass ``key_fn(seed)->key`` (from a captured
  27 03 / 27 04 exchange or a known algorithm), or use ``--no-unlock`` to try a
  read without security access first.  The additive-family helper from the
  Climatronic is offered as an *experiment only* (``--add-const``); it is NOT
  confirmed for this MCU and a wrong key on a read is harmless (just a NAK).
* Exact ReadMemoryByAddress framing.  The KWP2000/VAG-standard
  ``23 <addr:3> <len:1>`` form is the default and is fully parameterised; a wrong
  shape only draws a NegativeResponse on a read.

SCOPE: READ + IDENTIFY only -- StartDiagnosticSession (0x10), ReadECUIdentification
(0x1A), SecurityAccess (0x27) and ReadMemoryByAddress (0x23).  It never erases or
writes flash.  The whole point is to obtain the original plaintext binary of
*this* cluster (which exists nowhere else) so the +3% speedo-display bias can be
found and patched, leaving the odometer and the CAN-broadcast speed untouched.
"""
from __future__ import annotations

import os
import struct
from typing import Callable, List, Optional, Sequence, Tuple

# Reuse the Climatronic device/interface/debug layer verbatim -- these are generic
# CAN + KWP2000 helpers, not HVAC-specific.
from .climatronic import (            # noqa: F401  (re-exported for the GUI)
    Interface,
    list_interfaces,
    open_interface,
    CanTap,
    NegativeResponse,
    CAN_RATE,
)
from .crypto import sa2_execute       # VW SA2 seed/key bytecode VM (18/18-verified)

# ---------------------------------------------------------------- cluster facts
# TP 2.0 logical address of the instrument cluster.  0x17 is the KW1281/KWP
# address; over CAN TP 2.0 through the gateway the cluster answers the
# channel-setup at dest 0x07 -- confirmed by a VCDS->cluster sniff
# (Reference/Logs/VCDS-Cluster-TP2.0.log): `200#07C0...` -> reply on 0x207,
# ECU TX 0x300 / tester TX 0x751.  The cluster is a UDS module there (1A 9B ->
# 7F 1A 11 serviceNotSupported; 22 xxxx ReadDataByIdentifier works).
DEST_CLUSTER = 0x07

# Programming-level SecurityAccess seed/key -- a VW "SA2" bytecode tape, read
# UNENCRYPTED from the factory SGO container's metadata block (u32 length + tape
# at file offset 0x1B7), run via crypto.sa2_execute.
#
# The C6 FACELIFT (C6PA) clusters -- which match Bear's part number -- carry a
# 22-byte tape that differs ONLY by display type:
#   SUB 0x0530F101; FOR 6 { RSL; if-carry EOR <k> }; ADD 0x0200C030; FINISH
#   HL (colour)   : EOR 0x05CA6782   ("A###_C6PA_HL_*.sgo")
#   ML (mono FIS) : EOR 0x04CA6781   ("A###_C6PA_ML_*.sgo")
# identical across SW versions within a display type.  Pick the tape that matches
# the cluster's display.  (The C6PAR refresh variant uses EOR 0x05CA6785; the
# earlier pre-facelift 4F0910930C files carried a simpler FOR-5 tape,
# 6805814a05870a221289494c -- kept below for reference.)  All UNVERIFIED against a
# real cluster seed/key pair; confirm on the car.
CLUSTER_SA2_TAPE_HL = bytes.fromhex("840530f1016806814a058705ca678249930200c0304c")
CLUSTER_SA2_TAPE_ML = bytes.fromhex("840530f1016806814a058704ca678149930200c0304c")
CLUSTER_SA2_TAPE_PREFACELIFT = bytes.fromhex("6805814a05870a221289494c")
# Default to the facelift colour tape (Bear's cluster is a C6 facelift); override
# via the `display` arg / --display CLI flag for a mono-FIS cluster.
CLUSTER_SA2_TAPE = CLUSTER_SA2_TAPE_HL
_TAPES = {"hl": CLUSTER_SA2_TAPE_HL, "ml": CLUSTER_SA2_TAPE_ML,
          "prefacelift": CLUSTER_SA2_TAPE_PREFACELIFT}


def sa2_key(seed4: bytes, tape: bytes = CLUSTER_SA2_TAPE) -> bytes:
    """The 4 key bytes (big-endian) for a big-endian ``seed4`` via the cluster's
    SA2 tape.  Seed/key widths follow the seed the ECU returns (4 bytes seen)."""
    n = len(seed4) or 4
    key = sa2_execute(tape, int.from_bytes(bytes(seed4), "big"))
    return (key & ((1 << (8 * n)) - 1)).to_bytes(n, "big")

# Flash layout, read straight out of the factory SGO section tables.  (base,size)
# per section.  The C6 FACELIFT (C6PA) clusters -- matching Bear's part -- have
# display-specific maps (from A###_C6PA_{HL,ML}_*.sgo, SW 0133; other SW versions
# vary the sizes slightly):
CLUSTER_SECTIONS_HL: List[Tuple[int, int]] = [
    (0x040000, 0x04B8C8),
    (0x1B0000, 0x03B8E0),
    (0x220000, 0x1D31C4),
]
CLUSTER_SECTIONS_ML: List[Tuple[int, int]] = [
    (0x040000, 0x05E470),
    (0x0C0000, 0x02A010),
]
# Pre-facelift 4F0910930C (0110/0170/0240) map, kept for reference.
CLUSTER_SECTIONS_PREFACELIFT: List[Tuple[int, int]] = [
    (0x007000, 0x039000),
    (0x090000, 0x1B0E10),
]
_SECTIONS = {"hl": CLUSTER_SECTIONS_HL, "ml": CLUSTER_SECTIONS_ML,
             "prefacelift": CLUSTER_SECTIONS_PREFACELIFT}
SGO_SECTIONS = CLUSTER_SECTIONS_HL      # default (facelift colour)


class ClusterError(Exception):
    pass


# ---------------------------------------------------------------- seed/key hooks
def additive_key(seed: bytes, const: int) -> bytes:
    """EXPERIMENT ONLY: the Climatronic additive family, key = (seed+const) mod 2^N
    over ``len(seed)`` big-endian bytes.  NOT confirmed for this cluster's MCU --
    offered so a plausible guess can be tried on a (harmless) read.  Prefer a
    captured/known ``key_fn`` instead."""
    n = len(seed)
    val = (int.from_bytes(bytes(seed), "big") + const) & ((1 << (8 * n)) - 1)
    return val.to_bytes(n, "big")


# ---------------------------------------------------------------- KWP over TP2.0
class Cluster:
    """Read/identify session with a C6 instrument cluster over TP 2.0 on CAN.

    ``channel`` is a raw CAN channel (write_can/read/pass_*), from which a
    :class:`Tp20Channel` is built; or pass ``tp`` to reuse a channel the GUI/
    bridge already opened (matches the Climatronic constructor exactly)."""

    def __init__(self, channel=None, dest: int = DEST_CLUSTER, log=None,
                 timeout_ms: int = 2000, tp=None, display: str = "hl"):
        self._log = log or (lambda s: None)
        self.timeout_ms = timeout_ms
        self.unlocked = False
        # display type picks the SGO-derived SA2 tape + flash section map
        self.sa2_tape = _TAPES.get(display, CLUSTER_SA2_TAPE_HL)
        self.sections = _SECTIONS.get(display, CLUSTER_SECTIONS_HL)
        if tp is not None:
            self.tp = tp
        elif channel is not None:
            from .tp20_transport import Tp20Channel
            self.tp = Tp20Channel(channel, dest=dest, log=self._log,
                                  poll_timeout_ms=timeout_ms)
        else:
            raise ClusterError("Cluster needs either a CAN channel or a tp channel")

    # -- lifecycle --
    def open(self):
        dest = getattr(self.tp, "dest", None)
        self._log("Opening TP 2.0 channel to cluster%s..."
                  % ("" if dest is None else " (dest 0x%02X)" % dest))
        self.tp.open()

    def close(self):
        try:
            self.tp.close()
        except Exception:
            pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one KWP request/response, absorbing responsePending (0x78) --
    def request(self, sid: int, data: bytes = b"") -> bytes:
        self.tp.send(bytes([sid]) + bytes(data))
        for _ in range(64):
            resp = self.tp.recv(self.timeout_ms)
            if not resp:
                raise ClusterError("empty response to 0x%02X" % sid)
            if resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x78:
                continue
            break
        if resp[0] == 0x7F:
            raise NegativeResponse(sid, resp[2] if len(resp) >= 3 else 0)
        if resp[0] != (sid | 0x40):
            raise ClusterError("unexpected response 0x%02X to service 0x%02X"
                               % (resp[0], sid))
        return resp[1:]

    # -- services --
    def start_session(self, sub: int = 0x89) -> bytes:
        """StartDiagnosticSession.  0x89 extended (default); 0x85 programming."""
        return self.request(0x10, [sub])

    def tester_present(self):
        try:
            self.request(0x3E, [0x01])
        except ClusterError:
            pass

    def read_ecu_identification(self, did: int = 0xF187) -> bytes:
        """UDS ReadDataByIdentifier (0x22).  The C6 cluster is UDS -- KWP 0x1A 9B
        returns 7F 1A 11; VCDS reads 0x22 DIDs (F187 spare-part number, F190 VIN,
        F19E, F191 HW).  Default 0xF187.  Returns [did_hi,did_lo, data...]."""
        return self.request(0x22, [(did >> 8) & 0xFF, did & 0xFF])

    def unlock(self, key_fn: Optional[Callable[[bytes], bytes]] = None,
               level_const: Optional[int] = None) -> Tuple[bytes, bytes]:
        """SecurityAccess: request seed (27 01) then send key (27 02).

        Default key = the cluster's SA2 tape (:data:`CLUSTER_SA2_TAPE`, read from
        the factory SGO metadata; the write/programming-level unlock).  Override
        with ``key_fn`` (seed(bytes)->key(bytes)) for a different level, or
        ``level_const`` to try the additive family (experiment).  A zero seed
        means already unlocked.  Sub-functions 0x01 requestSeed / 0x02 sendKey."""
        body = self.request(0x27, [0x01])            # -> [0x01, seed...]
        seed = bytes(body[1:])
        if not seed or int.from_bytes(seed, "big") == 0:
            self._log("Seed is zero/empty -> already unlocked (or no access needed).")
            self.unlocked = True
            return seed, b""
        if key_fn is not None:
            key = bytes(key_fn(seed))
        elif level_const is not None:
            key = additive_key(seed, level_const)
            self._log("(experiment) additive key const=0x%X" % level_const)
        else:
            key = sa2_key(seed, self.sa2_tape)       # default: the SGO SA2 tape (per display)
            self._log("SA2 tape key")
        self._log("Seed %s -> key %s" % (seed.hex(), key.hex()))
        self.request(0x27, [0x02] + list(key))       # +ve or raises NegativeResponse
        self.unlocked = True
        self._log("Security access granted.")
        return seed, key

    def read_memory(self, addr: int, size: int,
                    addr_bytes: int = 3, len_bytes: int = 1, alfid: bool = True) -> bytes:
        """ReadMemoryByAddress (0x23).  UDS form (default, since the cluster is
        UDS): ``23 <ALFID> <addr> <len>`` where ALFID = (len_bytes<<4)|addr_bytes.
        Set ``alfid=False`` for the bare KWP form ``23 <addr> <len>``.  Widths
        parameterised.  NOTE: not yet confirmed on the cluster (VCDS didn't dump
        memory) -- a wrong shape just NAKs, harmless on a read."""
        req = []
        if alfid:
            req.append(((len_bytes & 0xF) << 4) | (addr_bytes & 0xF))
        req += list(addr.to_bytes(addr_bytes, "big")) + list(size.to_bytes(len_bytes, "big"))
        return self.request(0x23, req)

    def dump_region(self, base: int, size: int, chunk: int = 0x40,
                    addr_bytes: int = 3, len_bytes: int = 1, on_percent=None,
                    is_stop=None) -> bytes:
        out = bytearray()
        last = -1
        while len(out) < size:
            if is_stop and is_stop():
                raise ClusterError("dump stopped")
            want = min(chunk, size - len(out))
            got = self.read_memory(base + len(out), want, addr_bytes, len_bytes)
            if not got:
                raise ClusterError("empty read at 0x%06X" % (base + len(out)))
            out.extend(got)
            pct = len(out) * 100 // size
            if on_percent and pct != last:
                on_percent(pct)
                last = pct
            self.tp.keepalive()
        return bytes(out[:size])

    def dump(self, regions: Optional[Sequence[Tuple[int, int]]] = None,
             chunk: int = 0x40, addr_bytes: int = 3, len_bytes: int = 1,
             on_percent=None, is_stop=None, fill: int = 0xFF) -> bytes:
        """Read the given (base,size) regions (default: this cluster's SGO section
        map, per display) into a single flat image based at the lowest region base.
        Gaps between regions are padded with ``fill`` so image offset == addr - base."""
        regions = list(regions or self.sections)
        base0 = min(b for b, _ in regions)
        end = max(b + s for b, s in regions)
        image = bytearray([fill]) * (end - base0)
        total = sum(s for _, s in regions)
        done = 0
        last = -1
        for b, s in regions:
            def _pct(_p, _b=b, _s=s):
                nonlocal done, last
                cur = done + (_p * _s // 100)
                g = cur * 100 // total
                if on_percent and g != last:
                    on_percent(g)
                    last = g
            data = self.dump_region(b, s, chunk, addr_bytes, len_bytes,
                                    on_percent=_pct, is_stop=is_stop)
            image[b - base0:b - base0 + len(data)] = data
            done += s
        return bytes(image)


# ---------------------------------------------------------------- bus probe
# The TP 2.0 channel-setup broadcast id (fixed) and the request opcode -- the
# exact frame the working transport sends (verified byte-identical to a captured
# ZF-6HP session: `<dest> C0 00 10 00 03 01` to 0x200, reply on 0x200+dest).
SETUP_ID = 0x200
SETUP_REQ = 0xC0


def probe(device: Optional[str] = None, dest: int = DEST_CLUSTER,
          baud: int = CAN_RATE, seconds: float = 2.0, listen_id: int = 0x300,
          log=None) -> dict:
    """Diagnostic, read-only: open the CAN bus at ``baud`` with NO id filter
    (pass_all), send ONE TP 2.0 channel-setup broadcast to 0x200 for ``dest``,
    and log *every* frame seen for ``seconds``.

    This exists because a silent connect is ambiguous: it could be the wrong bus,
    the wrong baud, or the gateway answering on an id the normal path's hardware
    filter drops.  The probe removes the filter and the guessing -- it shows,
    structurally, whether the bus is alive at this baud and what (if anything)
    replies to the setup, and on which id.  Returns a summary dict; also emits
    each frame via ``log`` in the sniffer wire format."""
    log = log or (lambda s: None)
    _iface, dev, ch = open_interface(device, log=log)
    seen = {}                                   # can_id -> [count, first_payload_hex]
    setup = bytes([dest & 0xFF, SETUP_REQ, 0x00, 0x10,
                   listen_id & 0xFF, (listen_id >> 8) & 0xFF, 0x01])
    try:
        # Re-open the raw channel at the requested baud with a pass-all filter.
        try:
            ch.close()
        except Exception:
            pass
        from . import j2534 as _j
        raw = dev.connect(_j.CAN, baud)
        raw.pass_all()
        raw.clear_rx()
        import time as _t
        log("Probe: bus @ %d bps, setup 0x200#%s (dest 0x%02X), listening %.1fs..."
            % (baud, setup.hex().upper(), dest, seconds))
        raw.write_can(SETUP_ID, setup)
        deadline = _t.time() + seconds
        while _t.time() < deadline:
            for m in raw.read(32, 50) or []:
                cid = getattr(m, "can_id", None)
                pl = bytes(getattr(m, "payload", b""))
                rec = seen.setdefault(cid, [0, pl.hex().upper()])
                rec[0] += 1
                log("(%.6f) can %03X#%s" % (_t.time(), cid or 0, pl.hex().upper()))
        # A positive setup reply would be on 0x200+dest with opcode 0xD0..0xD8.
        reply_id = SETUP_ID + dest
        reply = seen.get(reply_id)
        summary = {
            "baud": baud, "dest": dest, "setup": setup.hex().upper(),
            "frames_total": sum(v[0] for v in seen.values()),
            "unique_ids": {("0x%03X" % k): v for k, v in sorted(seen.items())},
            "setup_reply_id": "0x%03X" % reply_id,
            "setup_replied": reply is not None,
        }
        if reply is not None:
            log("Setup REPLY on %s: %s" % (summary["setup_reply_id"], reply[1]))
        elif summary["frames_total"] == 0:
            log("No frames at all -> wrong bus or wrong baud (try 100000 for C6 comfort CAN).")
        else:
            log("Bus is alive (%d frames) but nothing on %s -> module not on this bus, "
                "or gateway not routing TP2.0 to dest 0x%02X (try 100000 / comfort CAN)."
                % (summary["frames_total"], summary["setup_reply_id"], dest))
        return summary
    finally:
        try:
            dev.close()
        except Exception:
            pass


# ---------------------------------------------------------------- CLI
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="ovtsuite-cluster",
        description="Read/identify a VAG C6 instrument cluster (4F0910930x) over "
                    "CAN (J2534 on Windows, OpenPort 2.0 elsewhere). Read-only.")
    ap.add_argument("--list", action="store_true", help="list CAN interfaces and exit")
    ap.add_argument("--device", help="interface label substring to select")
    ap.add_argument("--dest", type=lambda s: int(s, 0), default=DEST_CLUSTER,
                    help="TP2.0 logical address (default 0x17 = cluster)")
    ap.add_argument("--baud", type=lambda s: int(s, 0), default=CAN_RATE,
                    help="CAN bit rate (default 500000; try 100000 for C6 comfort CAN)")
    ap.add_argument("--probe", action="store_true",
                    help="diagnostic: pass-all, send one 0x200 setup, log every frame "
                         "seen for a couple of seconds, then exit (no channel needed)")
    ap.add_argument("--probe-seconds", type=float, default=2.0)
    ap.add_argument("--session", type=lambda s: int(s, 0), default=0x89,
                    help="diagnostic session sub-function (default 0x89; try 0x85)")
    ap.add_argument("--no-unlock", action="store_true",
                    help="skip security access (try a read without 0x27 first)")
    ap.add_argument("--add-const", type=lambda s: int(s, 0), default=None,
                    help="EXPERIMENT: try additive seed+CONST key (unconfirmed here)")
    ap.add_argument("--display", choices=["hl", "ml", "prefacelift"], default="hl",
                    help="cluster display type -> SA2 key + flash map: hl=facelift colour "
                         "(default), ml=facelift mono FIS, prefacelift=old 4F0910930C")
    ap.add_argument("--id", action="store_true", help="read ECU identification (UDS 0x22 F187)")
    ap.add_argument("--dump", metavar="FILE", help="dump the SGO-mapped flash to FILE")
    ap.add_argument("--chunk", type=lambda s: int(s, 0), default=0x40)
    ap.add_argument("--addr-bytes", type=int, default=3)
    ap.add_argument("--len-bytes", type=int, default=1)
    args = ap.parse_args(argv)

    log = lambda s: print(s, flush=True)            # noqa: E731

    if args.list:
        for i in list_interfaces():
            print("%-40s [%s%s]" % (i.label, i.kind, "" if i.loadable else ", not loadable"))
        return 0

    if args.probe:
        summary = probe(args.device, dest=args.dest, baud=args.baud,
                        seconds=args.probe_seconds, log=log)
        log("--- probe summary ---")
        log("bus @ %d bps, dest 0x%02X, setup 0x200#%s"
            % (summary["baud"], summary["dest"], summary["setup"]))
        log("frames: %d; ids: %s" % (summary["frames_total"],
                                     ", ".join(summary["unique_ids"]) or "(none)"))
        log("setup reply on %s: %s" % (summary["setup_reply_id"],
                                       "YES" if summary["setup_replied"] else "no"))
        return 0

    _iface, dev, ch = open_interface(args.device, log=log)
    try:
        cl = Cluster(ch, dest=args.dest, log=log, display=args.display)
        cl.open()
        try:
            log("Session 0x%02X..." % args.session)
            cl.start_session(args.session)
        except ClusterError as ex:
            log("(session start: %s -- continuing)" % ex)
        if args.id:
            try:
                rec = cl.read_ecu_identification()
                log("ECU ID (0x22 F187): %s" % rec.hex())
                printable = bytes(b if 32 <= b < 127 else 0x2E for b in rec)
                log("        ascii: %s" % printable.decode("latin-1"))
            except ClusterError as ex:
                log("ECU ID read failed: %s" % ex)
        if not args.no_unlock:
            try:
                seed, key = cl.unlock(level_const=args.add_const)
                if key:
                    log("Unlocked: seed=%s key=%s" % (seed.hex(), key.hex()))
            except ClusterError as ex:
                log("Security access: %s" % ex)
                log("Continuing without unlock (read may NAK).")
        if args.dump:
            log("Dumping SGO-mapped flash (%s, %d regions)..." % (args.display, len(cl.sections)))
            data = cl.dump(chunk=args.chunk, addr_bytes=args.addr_bytes,
                           len_bytes=args.len_bytes,
                           on_percent=lambda p: log("  %d%%" % p) if p % 10 == 0 else None)
            with open(args.dump, "wb") as fh:
                fh.write(data)
            base0 = min(b for b, _ in cl.sections)
            log("Wrote %d bytes to %s (base 0x%06X)" % (len(data), args.dump, base0))
    finally:
        try:
            cl.close()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
