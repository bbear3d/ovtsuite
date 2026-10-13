# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch ESP8 ABS/ESP (VAG C6 A6, 4F0910517xx) OBD read + identify over CAN.

KWP2000 (ISO 14230) services carried inside VW TP 2.0 on a raw CAN channel --
the *same* transport the Climatronic / Cluster modules use, so this reuses the
suite's validated device + TP2.0 layers wholesale (see
:mod:`ovtsuite.flash.climatronic` for the interface enumeration, ``open_interface``
and the ``CanTap`` debug wrap; they are imported here, not duplicated).

WHAT IS KNOWN (solid -- derived from the firmware AND double-checked, not guessed):
* Transport: TP 2.0 / KWP2000 over CAN, logical dest **0x03** (VAG "ABS Brakes").
  Same framing as the Climatronic/Cluster path. Powertrain CAN (500 kbit/s).
* Part 4F0910517AL, "ESP8 quattro", big-endian ARM. Application image spans
  0x008000..0x0BFFFF (this is the single section carried in the factory SGO);
  the boot block 0x000000..0x007FFF is NOT in the SGO but IS readable live via
  ReadMemoryByAddress, so a 0x23 dump yields a MORE complete image than the SGO.
* **SecurityAccess seed->key -- BOTH levels fully recovered from the firmware**
  (handler FUN_00069A86, key check FUN_000699FA), and independently cross-checked:
  - WRITE / PROGRAMMING level (27 01 requestSeed / 27 02 sendKey):
    ``key = ROR((seed + 0x974C58AB) ^ (0xFEDCBA98 if carry else 0x98765432), 11)``
    -- which is exactly the VW "SA2" tape stored in the factory SGO metadata
    block. Implemented here by running that tape through :func:`crypto.sa2_execute`;
    verified byte-for-byte == the firmware formula for every seed incl. overflow
    edge cases. This is the level that gates the programming session, hence BOTH
    the raw memory dump (0x23) AND flashing.
  - READ / DIAGNOSTIC level (27 03 requestSeed / 27 04 sendKey):
    additive, ``key = seed + 0x2BD7`` (two further tiers ``+0x75FB`` and
    ``+0x9CE8`` set extra capability bits). Constants are in-app literals
    (0x69C78/7C/80). This level unlocks NORMAL-session protected access
    (coding/adaptation, ReadDataByLocalId 0x21) -- NOT the raw memory dump.

WHAT IS NOT YET PINNED (must come from a real ODIS ABS flash capture, exactly as
the Climatronic write path was recovered):
* **Flash WRITE protocol.** requestDownload (0x34) here targets RAM at
  **0x400000** (a ~2 KB window), 4-byte aligned, uncompressed-only -- i.e. OBD
  writing follows the classic Bosch "download a flash-loader into RAM, then let
  the loader erase/program flash" pattern. The loader, the erase RoutineControl
  (0x31) local-id, the transfer block cadence and the section-checksum routine
  are NOT in the application image. :meth:`write_flash` therefore refuses until a
  capture provides them. See project doc 4f0910517-security-access-seedkey.md.
* Exact 0x23 chunk size / whether the programming session drops+reconnects the
  TP2.0 channel on 10 85 (the Climatronic does an A8 drop). A wrong read chunk
  only draws a NegativeResponse -- harmless. Confirm the reconnect on the car.

SCOPE (this version): READ + IDENTIFY. StartDiagnosticSession (0x10),
ReadECUIdentification (0x1A), SecurityAccess (0x27, both levels),
ReadMemoryByAddress (0x23) full dump, ReadDataByLocalIdentifier (0x21). It never
erases or writes flash. The point is to obtain the live ABS image (incl. the
boot block) for analysis and for building/validating a flash-write path later.
"""
from __future__ import annotations

import struct
from typing import Callable, List, Optional, Sequence, Tuple

# Reuse the Climatronic device/interface/debug layer verbatim -- generic CAN +
# KWP2000 helpers, not HVAC-specific (same import cluster.py uses).
from .climatronic import (            # noqa: F401  (re-exported for the GUI)
    Interface,
    list_interfaces,
    open_interface,
    CanTap,
    NegativeResponse,
    CAN_RATE,
)
from .crypto import sa2_execute       # VW SA2 seed/key bytecode VM (shared)

# ---------------------------------------------------------------- ESP8 facts
# TP 2.0 logical address of the ABS/ESP module. VAG KWP address 0x03 = "ABS
# Brakes"; over CAN TP 2.0 the channel-setup goes to 0x200 with byte0=dest, the
# reply lands on 0x200+dest and carries the real comm ids (same rule as the
# Climatronic/Cluster/6HP paths). Confirm the reply ids with --probe on the car.
DEST_ABS = 0x03

# --- SecurityAccess: WRITE / PROGRAMMING level (27 01 / 27 02) ---------------
# The VW "SA2" tape stored UNENCRYPTED in the factory SGO metadata block for
# 4F0910517AL_0060 (24 bytes). Decodes to:
#   ADD 0x974C58AB; BCC{EOR 0xFEDCBA98; BRA} else {EOR 0x98765432}; FOR 11 {RSR}
# i.e. key = ROR((seed + 0x974C58AB) ^ (0xFEDCBA98 if add-carry else 0x98765432), 11).
# Running this tape through crypto.sa2_execute == the firmware key-check
# FUN_000699FA(param!=0) byte-for-byte, verified for all seeds incl. overflow.
# The firmware's own copy of the constants lives in the boot block (0x7FDC/E0/E4),
# which the SGO omits -- so the tape is the authoritative source for them.
ABS_WRITE_TAPE = bytes.fromhex("93974c58ab4a0787fedcba986b058798765432680b82494c")


def program_key(seed4: bytes, tape: bytes = ABS_WRITE_TAPE) -> bytes:
    """WRITE/PROGRAMMING-level (27 01/02) key for a big-endian ``seed4`` via the
    ESP8 SA2 tape. Seed/key are 32-bit; width follows the returned seed (4 seen)."""
    n = len(seed4) or 4
    key = sa2_execute(tape, int.from_bytes(bytes(seed4), "big"))
    return (key & ((1 << (8 * n)) - 1)).to_bytes(n, "big")


# --- SecurityAccess: READ / DIAGNOSTIC level (27 03 / 27 04) -----------------
# Additive. Firmware FUN_000699FA(param==0): (key - seed) is matched against
# 0x2BD7, then +0x4A24, then +0x26ED cumulatively; each match sets a different
# capability bit. Base unlock is seed+0x2BD7. Constants are in-app literals.
ABS_READ_CONST = 0x2BD7                 # tier 1 (base)
ABS_READ_CONST_T2 = ABS_READ_CONST + 0x4A24     # 0x75FB
ABS_READ_CONST_T3 = ABS_READ_CONST_T2 + 0x26ED  # 0x9CE8


def diag_key(seed4: bytes, const: int = ABS_READ_CONST) -> bytes:
    """READ/DIAGNOSTIC-level (27 03/04) key = (seed + const) mod 2^N, big-endian."""
    n = len(seed4) or 4
    val = (int.from_bytes(bytes(seed4), "big") + const) & ((1 << (8 * n)) - 1)
    return val.to_bytes(n, "big")


# --- flash / memory map ------------------------------------------------------
# The factory SGO carries ONE section, 0x008000..0x0BFFFF. A live 0x23 dump can
# also read the boot block (0x000000..0x007FFF), which the SGO lacks -- so the
# default dump region is the full 0x000000..0x0BFFFF (768 KiB).
ABS_IMAGE_BASE = 0x000000
ABS_IMAGE_END = 0x0C0000                # exclusive
ABS_SGO_SECTION: Tuple[int, int] = (0x008000, 0x0B8000)   # (base, size) = SGO section
ABS_FULL_REGION: Tuple[int, int] = (ABS_IMAGE_BASE, ABS_IMAGE_END - ABS_IMAGE_BASE)
# requestDownload(0x34) target window (RAM loader) -- for the WRITE path only.
ABS_LOADER_RAM_BASE = 0x400000
ABS_LOADER_RAM_WINDOW = 0x800


class AbsError(Exception):
    pass


class AbsEsp8:
    """Read/identify session with a C6 Bosch ESP8 ABS over TP 2.0 on CAN.

    ``channel`` is a raw CAN channel, from which a :class:`Tp20Channel` is built;
    or pass ``tp`` to reuse a channel the GUI/bridge already opened (matches the
    Cluster/Climatronic constructors exactly)."""

    def __init__(self, channel=None, dest: int = DEST_ABS, log=None,
                 timeout_ms: int = 2000, tp=None):
        self._log = log or (lambda s: None)
        self.timeout_ms = timeout_ms
        self.unlocked = False           # write/programming level reached
        self.diag_unlocked = False      # read/diagnostic level reached
        if tp is not None:
            self.tp = tp
        elif channel is not None:
            from .tp20_transport import Tp20Channel
            self.tp = Tp20Channel(channel, dest=dest, log=self._log,
                                  poll_timeout_ms=timeout_ms)
        else:
            raise AbsError("AbsEsp8 needs either a CAN channel or a tp channel")

    # -- lifecycle --
    def open(self):
        dest = getattr(self.tp, "dest", None)
        self._log("Opening TP 2.0 channel to ABS%s..."
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
                raise AbsError("empty response to 0x%02X" % sid)
            if resp[0] == 0x7F and len(resp) >= 3 and resp[2] == 0x78:
                continue
            break
        if resp[0] == 0x7F:
            raise NegativeResponse(sid, resp[2] if len(resp) >= 3 else 0)
        if resp[0] != (sid | 0x40):
            raise AbsError("unexpected response 0x%02X to service 0x%02X"
                           % (resp[0], sid))
        return resp[1:]

    # -- services --
    def start_session(self, sub: int = 0x89) -> bytes:
        """StartDiagnosticSession (0x10). 0x89 extended, 0x85 programming.
        NOTE: the programming session (0x85) is gated on the WRITE-level unlock
        (firmware FUN_00083A28 checks the 27 02 unlock bit), so call
        :meth:`unlock_program` BEFORE requesting 0x85."""
        return self.request(0x10, [sub])

    def tester_present(self):
        try:
            self.request(0x3E, [0x01])
        except AbsError:
            pass

    def read_ecu_identification(self, local_id: int = 0x9B) -> bytes:
        """ReadECUIdentification (0x1A). KWP2000 form ``1A <local_id>``. ESP8 is
        KWP2000 (not UDS): 0x9B is the standard VAG identification block."""
        return self.request(0x1A, [local_id & 0xFF])

    def read_data_by_local_id(self, local_id: int) -> bytes:
        """ReadDataByLocalIdentifier (0x21) -- normal-session measuring/adaptation
        blocks. Protected local-ids need the READ/diagnostic unlock (27 03/04)."""
        return self.request(0x21, [local_id & 0xFF])

    def unlock_program(self, key_fn: Optional[Callable[[bytes], bytes]] = None
                       ) -> Tuple[bytes, bytes]:
        """WRITE/PROGRAMMING SecurityAccess: 27 01 (requestSeed) then 27 02
        (sendKey). Default key = the ESP8 SA2 tape (:func:`program_key`). This is
        the level that opens the programming session and the raw memory dump."""
        body = self.request(0x27, [0x01])            # -> [0x01, seed...]
        seed = bytes(body[1:])
        if not seed or int.from_bytes(seed, "big") == 0:
            self._log("Seed 0 -> already unlocked (programming).")
            self.unlocked = True
            return seed, b""
        key = bytes(key_fn(seed)) if key_fn else program_key(seed)
        self._log("PROG seed %s -> key %s" % (seed.hex(), key.hex()))
        self.request(0x27, [0x02] + list(key))       # +ve or raises NegativeResponse
        self.unlocked = True
        self._log("Programming security access granted.")
        return seed, key

    def unlock_diag(self, const: int = ABS_READ_CONST,
                    key_fn: Optional[Callable[[bytes], bytes]] = None
                    ) -> Tuple[bytes, bytes]:
        """READ/DIAGNOSTIC SecurityAccess: 27 03 (requestSeed) then 27 04
        (sendKey). Default key = seed + 0x2BD7 (:func:`diag_key`). Unlocks
        normal-session protected coding/adaptation (0x21); NOT the memory dump."""
        body = self.request(0x27, [0x03])            # -> [0x03, seed...]
        seed = bytes(body[1:])
        if not seed or int.from_bytes(seed, "big") == 0:
            self._log("Seed 0 -> already unlocked (diagnostic).")
            self.diag_unlocked = True
            return seed, b""
        key = bytes(key_fn(seed)) if key_fn else diag_key(seed, const)
        self._log("DIAG seed %s -> key %s" % (seed.hex(), key.hex()))
        self.request(0x27, [0x04] + list(key))
        self.diag_unlocked = True
        self._log("Diagnostic security access granted.")
        return seed, key

    def read_memory(self, addr: int, size: int,
                    addr_bytes: int = 3, len_bytes: int = 1) -> bytes:
        """ReadMemoryByAddress (0x23). KWP2000 bare form ``23 <addr> <len>``
        (ESP8 is KWP2000). Only available in the programming session -- call
        :meth:`enter_programming` first. Widths parameterised; a wrong shape
        only draws a NegativeResponse (harmless on a read)."""
        req = list(addr.to_bytes(addr_bytes, "big")) + list(size.to_bytes(len_bytes, "big"))
        return self.request(0x23, req)

    def enter_programming(self, unlock: bool = True) -> None:
        """Bring the module into the session where ReadMemoryByAddress (0x23) and
        flashing live: (default session) 27 01/02 write-unlock, then 10 85.
        Order matters -- 10 85 is refused until the write-level unlock is set."""
        if unlock and not self.unlocked:
            self.unlock_program()
        self._log("Entering programming session (10 85)...")
        try:
            self.start_session(0x85)
        except NegativeResponse as ex:
            raise AbsError("programming session (10 85) refused: %s "
                           "(is the write-level unlock in place?)" % ex)
        # Some Bosch modules drop + reconnect the TP2.0 channel on 10 85 (cf. the
        # Climatronic A8 drop). If reads NAK right after this, reconnect the tp
        # channel here -- confirm the behaviour on the car.

    def dump_region(self, base: int, size: int, chunk: int = 0x40,
                    addr_bytes: int = 3, len_bytes: int = 1, on_percent=None,
                    is_stop=None) -> bytes:
        out = bytearray()
        last = -1
        while len(out) < size:
            if is_stop and is_stop():
                raise AbsError("dump stopped")
            want = min(chunk, size - len(out))
            got = self.read_memory(base + len(out), want, addr_bytes, len_bytes)
            if not got:
                raise AbsError("empty read at 0x%06X" % (base + len(out)))
            out.extend(got)
            pct = len(out) * 100 // size
            if on_percent and pct != last:
                on_percent(pct)
                last = pct
            self.tp.keepalive()
        return bytes(out[:size])

    def dump(self, regions: Optional[Sequence[Tuple[int, int]]] = None,
             chunk: int = 0x40, addr_bytes: int = 3, len_bytes: int = 1,
             program_session: bool = True, on_percent=None, is_stop=None,
             fill: int = 0xB6) -> bytes:
        """Read (base,size) regions into one flat image based at the lowest base.
        Default region = the full 0x000000..0x0BFFFF (incl. the boot block the
        SGO lacks). ``program_session`` runs the write-unlock + 10 85 first (0x23
        lives only in the programming session). Gaps are padded with ``fill``
        (0xB6 = the ESP8 flash fill byte) so image offset == addr - base."""
        regions = list(regions or [ABS_FULL_REGION])
        if program_session:
            self.enter_programming(unlock=True)
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

    # -- write (scaffold; refuses until an ODIS capture provides the loader) --
    def write_flash(self, *_a, **_k):
        """NOT IMPLEMENTED. OBD writing on this ESP8 uses the Bosch RAM-loader
        pattern: requestDownload (0x34) here targets RAM at 0x%06X (a ~%d-byte,
        4-byte-aligned, uncompressed-only window), i.e. you download a flash
        driver into RAM and it does the erase/program. The loader, the erase
        RoutineControl (0x31) local-id, the transfer cadence and the section
        checksum are not in the application image -- capture a real ODIS ABS
        flash (as was done for the Climatronic) to obtain them, then wire the
        0x34/0x36/0x37 + 0x31 sequence here.""" % (
            ABS_LOADER_RAM_BASE, ABS_LOADER_RAM_WINDOW)
        raise NotImplementedError(
            "ABS flash write needs an ODIS capture for the RAM loader + "
            "erase/checksum protocol (see write_flash docstring).")


# ---------------------------------------------------------------- bus probe
# Fixed TP 2.0 channel-setup broadcast id + request opcode (byte-identical to the
# validated Cluster/Climatronic probe: `<dest> C0 00 10 <lo> <hi> 01` to 0x200,
# reply on 0x200+dest).
SETUP_ID = 0x200
SETUP_REQ = 0xC0


def probe(device: Optional[str] = None, dest: int = DEST_ABS,
          baud: int = CAN_RATE, seconds: float = 2.0, listen_id: int = 0x300,
          log=None) -> dict:
    """Diagnostic, read-only: open the CAN bus with NO id filter, send ONE TP 2.0
    channel-setup broadcast to 0x200 for ``dest``, log every frame for
    ``seconds``. Mirrors :func:`cluster.probe`. Returns a summary dict."""
    log = log or (lambda s: None)
    _iface, dev, ch = open_interface(device, log=log)
    seen = {}
    setup = bytes([dest & 0xFF, SETUP_REQ, 0x00, 0x10,
                   listen_id & 0xFF, (listen_id >> 8) & 0xFF, 0x01])
    try:
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
            log("No frames -> wrong bus or wrong baud (ABS is on powertrain CAN, 500k).")
        else:
            log("Bus alive (%d frames) but nothing on %s -> module not on this bus "
                "or gateway not routing TP2.0 to dest 0x%02X."
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
        prog="ovtsuite-abs",
        description="Read/identify a VAG C6 Bosch ESP8 ABS (4F0910517xx) over CAN "
                    "(J2534 on Windows, OpenPort 2.0 elsewhere). Read-only.")
    ap.add_argument("--list", action="store_true", help="list CAN interfaces and exit")
    ap.add_argument("--device", help="interface label substring to select")
    ap.add_argument("--dest", type=lambda s: int(s, 0), default=DEST_ABS,
                    help="TP2.0 logical address (default 0x03 = ABS Brakes)")
    ap.add_argument("--baud", type=lambda s: int(s, 0), default=CAN_RATE,
                    help="CAN bit rate (default 500000; ABS is on powertrain CAN)")
    ap.add_argument("--probe", action="store_true",
                    help="diagnostic: pass-all, one 0x200 setup, log frames, exit")
    ap.add_argument("--probe-seconds", type=float, default=2.0)
    ap.add_argument("--id", action="store_true", help="read ECU identification (1A 9B)")
    ap.add_argument("--diag-unlock", action="store_true",
                    help="do the READ/diagnostic unlock (27 03/04, seed+0x2BD7)")
    ap.add_argument("--dump", metavar="FILE", help="dump flash to FILE (full image)")
    ap.add_argument("--region", help="dump region base:size (hex), default full 0:0xC0000")
    ap.add_argument("--chunk", type=lambda s: int(s, 0), default=0x40)
    args = ap.parse_args(argv)

    log = lambda s: print(s, flush=True)            # noqa: E731

    if args.list:
        for i in list_interfaces():
            print("%-40s [%s%s]" % (i.label, i.kind, "" if i.loadable else ", not loadable"))
        return 0

    if args.probe:
        s = probe(args.device, dest=args.dest, baud=args.baud,
                  seconds=args.probe_seconds, log=log)
        log("--- probe: %d frames; ids: %s; reply on %s: %s ---" % (
            s["frames_total"], ", ".join(s["unique_ids"]) or "(none)",
            s["setup_reply_id"], "YES" if s["setup_replied"] else "no"))
        return 0

    _iface, dev, ch = open_interface(args.device, log=log)
    try:
        abs_ = AbsEsp8(ch, dest=args.dest, log=log)
        abs_.open()
        try:
            abs_.start_session(0x89)
        except AbsError as ex:
            log("(session start: %s -- continuing)" % ex)
        if args.id:
            try:
                rec = abs_.read_ecu_identification()
                log("ECU ID (1A 9B): %s" % rec.hex())
                printable = bytes(b if 32 <= b < 127 else 0x2E for b in rec)
                log("        ascii: %s" % printable.decode("latin-1"))
            except AbsError as ex:
                log("ECU ID read failed: %s" % ex)
        if args.diag_unlock:
            try:
                seed, key = abs_.unlock_diag()
                if key:
                    log("Diag unlocked: seed=%s key=%s" % (seed.hex(), key.hex()))
            except AbsError as ex:
                log("Diagnostic unlock: %s" % ex)
        if args.dump:
            if args.region:
                b, s = (int(x, 0) for x in args.region.split(":"))
                regions = [(b, s)]
            else:
                regions = [ABS_FULL_REGION]
            log("Dumping flash (programming session + write-level unlock)...")
            data = abs_.dump(regions=regions, chunk=args.chunk,
                             on_percent=lambda p: log("  %d%%" % p) if p % 10 == 0 else None)
            with open(args.dump, "wb") as fh:
                fh.write(data)
            base0 = min(b for b, _ in regions)
            log("Wrote %d bytes to %s (base 0x%06X)" % (len(data), args.dump, base0))
    finally:
        try:
            abs_.close()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
