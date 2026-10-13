# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""K-line (ISO 14230 / KWP2000) ECU-side simulator for VAG TCUs.

Companion to :mod:`ovtsuite.flash.trx_sim` (TP2.0/CAN 09D) and
:mod:`ovtsuite.flash.ecu_sim` (ISO-TP/CAN UDS, incl. the ZF 6HP19 "GS19"
profile). This one speaks **K-line over a serial port** -- an FTDI cable or a
COM port -- so the OVT-Suite flasher's K-line paths can be developed against it.

The K-line transport here is ECU-agnostic: :class:`KLineEcu` drives any
responder exposing ``handle(payload) -> response`` (unframed bytes in/out), so
it serves BOTH the Aisin **09D** (:class:`~ovtsuite.flash.trx_sim.Trx09dResponder`)
and the ZF **6HP19** (:class:`~ovtsuite.flash.ecu_sim.Gs19Responder`) by reusing
their existing TP2.0/CAN service logic unchanged -- pick with ``make_responder``
/ the CLI ``--ecu`` flag. What this module adds is:

* the K-line serial transport (pyserial COM / FTDI, via the same open logic as
  :class:`ovtsuite.flash.transport.KLine`),
* ISO 14230 frame in/out from the ECU side (``[fmt][dest][src][len?][svc][body][cks]``,
  mirroring :mod:`ovtsuite.flash.kwp`), with half-duplex self-echo handling,
* the init handshake answered from the ECU side -- ISO 14230 **fast init**
  (StartCommunication) cleanly, 5-baud slow init best-effort,
* **baud escalation** (OUR flasher's, not ODIS): OVT-Suite raises the K-line
  with a StartDiagnosticSession carrying a trailing speed byte --
  ``10 89 <speed>`` (diag.py `_raise_baud` / `SPEEDS`): 0x87 -> 124800,
  0xA7 -> 244898 (0x00 = stay 10400). The sim answers the session positive at
  the OLD baud (via the responder), then switches the port to the new rate, so
  the flasher's follow-up ``21 01`` lands at the new speed. See
  :class:`BaudRaise` (override for a different scheme).

**Variants** -- both are served, selected by image size / an explicit range:

    ============  ==========  =====================  ===============
    variant       flash size  cal (read/write) range checksum K
    ============  ==========  =====================  ===============
    early K-line  512 KiB     0x010000 .. 0x07FFFF   0x00074004
    newer 1 MiB   1 MiB       0x002000 .. 0x0DFFFF   0x000AA804
    ============  ==========  =====================  ===============

Reads/writes use absolute addresses, so the responder serves whichever image is
loaded; the range only bounds the erase/checksum-verify routines. The checksum
is the same additive BE32 word-sum in both (see
:func:`ovtsuite.flash.aisin09d.compute_checksum`); only (range, K) differ.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

try:                                                   # package-relative
    from .transport import KLine, KLineError, KLineTimeout
    from . import trx_sim as _trx
except ImportError:                                    # flat (e.g. inside tooling)
    from transport import KLine, KLineError, KLineTimeout   # type: ignore
    import trx_sim as _trx                                   # type: ignore

TESTER = 0xF1                       # ISO 14230 tester address (kwp.TESTER)
START_COMM = 0x81                   # StartCommunication service
START_COMM_POS = 0xC1              # StartCommunication positive response
STOP_COMM = 0x82                    # StopCommunication service
START_SESSION = 0x10                # StartDiagnosticSession (carries the speed byte)
POS = 0x40

#: 5-baud slow init timing. The tester bit-bangs the address at 5 baud
#: (1 start + 8 bits + stop ~= 10 x 200 ms ~= 2.0 s); the ECU must send the
#: 0x55 sync only AFTER that, in the W1 window. This is the wait from the first
#: low byte we see to emitting 0x55. Shim (Scanmatik FIVE_BAUD_INIT, addr 0x02):
#: address drive starts at t0, TIMEOUT at t0+2.80 s -> the 0x55 must land in the
#: ~2.0..2.8 s slot; 2.1 s puts it just past the address end, inside W1.
#: APPROXIMATE over a UART -- tune if a bench run shows it landing early/late.
SLOW_INIT_WAIT_S = 2.1
SLOW_INIT_KEYBYTES = (0x6B, 0x8F)   # KB1,KB2 = keyword 0x8F6B -> KWP2000 (>=2000)

#: Our own flasher's K-line baud escalation (diag.py `SPEEDS` / `_raise_baud`):
#: StartDiagnosticSession with a trailing speed byte -- ``10 89 <speed>`` --
#: raises the link; the ECU replies ``50 89`` at the OLD baud, then both switch.
#: 0x00 = Low (stay 10400); only the faster ones actually re-baud here.
#: (ODIS does NOT escalate; OUR flasher does -- this is the real command.)
SPEED_BAUDS = {0x87: 124800, 0xA7: 244898}

#: Per-variant geometry. ``size`` is the full-flash image size the sim pads to;
#: ``rng`` is the (start, end) inclusive cal range; ``k`` is the checksum
#: constant (informational here -- the responder serves raw bytes).
@dataclass(frozen=True)
class Variant:
    name: str
    size: int
    rng: Tuple[int, int]
    k: int


VARIANTS = {
    "kline512": Variant("kline512", 0x80000, (0x10000, 0x7FFFF), 0x00074004),
    "trx1mb":   Variant("trx1mb",   0x100000, (0x2000, 0xDFFFF), 0x000AA804),
}


def variant_for_image(image: bytes) -> Variant:
    """Pick the variant whose flash size best fits ``image`` (<=512 KiB -> the
    early K-line 512 KiB part, otherwise the 1 MiB part)."""
    return VARIANTS["kline512"] if len(image) <= 0x80000 else VARIANTS["trx1mb"]


# --------------------------------------------------------------------------- #
# ISO 14230 frame I/O, ECU side                                               #
# --------------------------------------------------------------------------- #
def build_frame(payload: bytes, dest: int, src: int) -> bytes:
    """Frame one KWP2000 message. ``payload`` is service-id + body (what the
    responder returns). Always addressed (fmt bit 0x80), with a separate length
    byte when the count exceeds 63 -- exactly the shape
    :func:`ovtsuite.flash.kwp.build_frame` produces/consumes."""
    payload = bytes(payload)
    n = len(payload)
    out = bytearray()
    out.append(0x80 | (n if n <= 63 else 0))
    out.append(dest)
    out.append(src)
    if n > 63:
        out.append(n & 0xFF)
    out += payload
    out.append(sum(out) & 0xFF)
    return bytes(out)


class KLineEcu:
    """Drive a K-line serial port as the 09D ECU: answer the init handshake,
    then read KWP2000 requests and reply from ``responder`` until the tester
    ends the session. ``port`` is anything with ``read_byte``/``write_byte``/
    ``write_bytes``/``read``/``set_baud``/``clear_rx`` (a
    :class:`ovtsuite.flash.transport.KLine`, or a fake in tests)."""

    def __init__(self, port, responder, controller: int = 0x01,
                 key_bytes: Tuple[int, int] = (0x6B, 0x8F),
                 echo: bool = True, baud_raise: "Optional[BaudRaise]" = None,
                 log: Optional[Callable[[str], None]] = None):
        self.port = port
        self.responder = responder
        self.controller = controller
        self.key_bytes = key_bytes
        self.echo = echo                 # half-duplex K-line echoes our own TX
        self.baud_raise = baud_raise or BaudRaise()
        self._log = log or (lambda s: None)
        self.count = 0

    def log(self, msg: str):
        self._log(msg)

    # -- low level --
    def _drain_echo(self, n: int):
        """On a shared K-line wire the ECU reads back its own transmission;
        swallow ``n`` echoed bytes so they are not mistaken for a request."""
        if not self.echo:
            return
        try:
            self.port.read(n)
        except KLineTimeout:
            pass

    def _send(self, payload: bytes, dest: int, src: int):
        frame = build_frame(payload, dest, src)
        self.port.write_bytes(frame)
        self._drain_echo(len(frame))
        self.log("  TX %s" % frame.hex(" "))

    def _read_frame(self) -> Optional[Tuple[int, int, bytes]]:
        """Read one request frame -> (dest, src, payload=svc+body), or None on a
        clean idle timeout. Checksum is read but not enforced (lenient, like the
        tester's own receiver)."""
        fmt = self.port.read_byte()
        dest = src = None
        if fmt & 0x80:
            dest = self.port.read_byte()
            src = self.port.read_byte()
        n = fmt & 0x3F
        if n == 0:
            n = self.port.read_byte()
        payload = self.port.read(n)
        self.port.read_byte()                    # checksum (not enforced)
        if len(payload) != n:
            raise KLineError("kline-sim: short frame (want %d, got %d)" % (n, len(payload)))
        return dest, src, bytes(payload)

    # -- init --
    def do_init(self, deadline_s: float = 30.0) -> bool:
        """Answer the init handshake. Reads RAW bytes (init is not a clean KWP
        frame, so never parse it as one -- a 5-baud / break low shows up as a run
        of 0x00 on a UART, which must not be mis-read as an empty frame):

        * ``0x81`` -> ISO 14230 **fast init** StartCommunication; consume the rest
          of the frame and reply ``C1 <kb1> <kb2>``.
        * ``0x00`` -> **5-baud / break** low activity; this is the tester
          bit-banging the address at 5 baud. Wait out the whole ~2 s address
          transmission, then send the ``55 <kb1> <kb2>`` sync in the W1 window.

        5-baud is what the real modules (09D, GS19) use and what the flasher
        drives, so the sim must emulate it ECU-side; fast init is only here as a
        convenience for other test tools. Over a UART we cannot see the K-line
        edges directly -- a low bit arrives as a run of 0x00 framing bytes and a
        high bit as silence -- so we time the response off the first low rather
        than decode the address (:data:`SLOW_INIT_WAIT_S`)."""
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                b0 = self.port.read_byte()
            except KLineTimeout:
                continue
            if b0 == START_COMM:                      # 81 <tgt> <src> <81> <cks>
                tgt = src = None
                try:
                    rest = self.port.read(4)          # tgt, src, svc(0x81), cks
                    if len(rest) >= 2:
                        tgt, src = rest[0], rest[1]
                except KLineTimeout:
                    pass
                self.log("  RX StartCommunication (fast init)")
                self._send(bytes([START_COMM_POS, self.key_bytes[0], self.key_bytes[1]]),
                           dest=src if src is not None else TESTER,
                           src=tgt if tgt is not None else self.controller)
                return True
            if b0 == 0x00:                            # 5-baud address low bit
                self.log("  RX 0x00 (5-baud address) -> waiting out %.2fs address, "
                         "then sync" % SLOW_INIT_WAIT_S)
                self._answer_5baud()
                return True
            self.log("  RX 0x%02X during init (ignored)" % b0)   # stray; keep waiting
        return False

    def _answer_5baud(self):
        """Answer a 5-baud slow init once the first low byte has been seen.

        The tester drives the address for ~2 s (10 bit-times); we must NOT reply
        until that finishes or the J2534's FIVE_BAUD_INIT never sees the sync.
        So: drain the incoming 0x00 run for :data:`SLOW_INIT_WAIT_S`, clear RX so
        the buffered init bytes don't mask what follows, then emit
        ``55 <kb1> <kb2>`` in the W1 window -- and then return straight to serving.

        We deliberately do NOT perform the ISO 14230 inverted-keyword exchange
        (tester ``~KB2`` -> ECU ``~address``). Bench shim 2026-10-09 (Scanmatik
        SM2, addr 0x02): ``FIVE_BAUD_INIT`` returns NOERROR with keybytes ``6B 8F``
        from just our ``55 6B 8F`` -- the interface completes the init internally
        with no inversion handshake on the wire. An earlier version opened a read
        window here for ``~KB2``; with no inversion byte actually sent, that read
        instead swallowed the format byte of the tester's first real request
        (``82 1A F1 1A 9B``) and injected a bogus ``~address`` onto the bus,
        desyncing the serve loop (0 requests answered). If a raw-K-line tester
        that *does* drive ``~KB2`` on the wire ever needs it, add it back as a
        W4-bounded (<=50 ms) read so it cannot reach into the first request."""
        deadline = time.monotonic() + SLOW_INIT_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                self.port.clear_rx()                  # drop the slow-init 0x00 run
            except Exception:
                pass
        try:
            self.port.clear_rx()
        except Exception:
            pass
        kb1, kb2 = SLOW_INIT_KEYBYTES                 # keyword 0x8F6B -> KWP2000
        self.port.write_bytes(bytes([0x55, kb1, kb2]))
        self._drain_echo(3)
        self.log("  5-baud: sent 55 %02X %02X (sync + keyword) at +%.2fs; serving"
                 % (kb1, kb2, SLOW_INIT_WAIT_S))

    # -- main loop --
    def run(self, idle_timeout_s: float = 30.0):
        self.log("K-line 09D simulator: waiting for init...")
        if not self.do_init():
            self.log("no init within timeout; giving up")
            return
        self.log("communication started; serving requests")
        while True:
            try:
                got = self._read_frame()
            except KLineTimeout:
                self.log("idle timeout; session ended")
                return
            except KLineError as ex:
                self.log("  (ignoring malformed frame: %s)" % ex)
                continue
            dest, src, payload = got
            if not payload:
                continue
            svc = payload[0]
            self.log("  RX %s" % payload.hex(" "))
            rdest = src if src is not None else TESTER
            rsrc = dest if dest is not None else self.controller

            if svc == STOP_COMM:
                self._send(bytes([STOP_COMM | POS]), rdest, rsrc)
                self.log("StopCommunication; session ended")
                return

            # Dispatch first and reply at the CURRENT baud. For our flasher's
            # baud escalation (StartDiagnosticSession + speed byte, 10 89 <speed>)
            # the responder gives the positive 50 89, which must go out at the OLD
            # rate; THEN we switch the port so the next request is read faster.
            resp = self.responder.handle(payload)
            self.count += 1
            if resp is not None:
                self._send(resp, rdest, rsrc)
            new_baud = self.baud_raise.match(payload)
            if new_baud is not None:
                time.sleep(0.05)
                self.port.set_baud(new_baud)
                self.log("link speed -> %d baud" % new_baud)


@dataclass
class BaudRaise:
    """Recognise OUR flasher's K-line baud escalation and return the new serial
    baud (or None). OVT-Suite raises the link with a StartDiagnosticSession that
    carries a trailing speed byte -- ``10 <sub> <speed>`` (diag.py `_raise_baud`
    / `SPEEDS`): speed 0x87 -> 124800, 0xA7 -> 244898 (0x00 = Low, no change).
    The ECU answers the session positive at the OLD baud, then both ends switch
    (the flasher confirms with a 21 01 at the new rate). ODIS does NOT escalate;
    our flasher does. Override ``detector`` / ``speeds`` for a different scheme."""
    detector: Optional[Callable[[bytes], Optional[int]]] = None
    speeds: Dict[int, int] = field(default_factory=lambda: dict(SPEED_BAUDS))

    def match(self, payload: bytes) -> Optional[int]:
        if self.detector is not None:
            return self.detector(payload)
        if len(payload) >= 3 and payload[0] == START_SESSION and payload[2] in self.speeds:
            return self.speeds[payload[2]]
        return None


# --------------------------------------------------------------------------- #
# image loading (full-flash buffer at absolute addresses)                     #
# --------------------------------------------------------------------------- #
def load_09d_image(path: Optional[str], variant: Optional[Variant] = None) -> Tuple[bytes, Variant]:
    """Build a full-flash image for the sim to serve. Accepts a raw ``.bin``
    (used as-is, padded to the variant size) or a 09D ``.sgo`` (the cal payload
    is XOR-0xFF-decoded and placed at its section address in a 0xFF-filled
    full-flash buffer). Returns (image, variant)."""
    if not path:
        v = variant or VARIANTS["kline512"]
        return b"\xFF" * v.size, v
    raw = open(path, "rb").read()
    if raw[:16] == b"SGML Object File":
        addr, cal = _sgo_cal(raw)
        v = variant or (VARIANTS["kline512"] if addr >= 0x10000 or len(cal) <= 0x70000
                        else VARIANTS["trx1mb"])
        buf = bytearray(b"\xFF" * v.size)
        buf[addr:addr + len(cal)] = cal[: v.size - addr]
        return bytes(buf), v
    # raw bin
    v = variant or variant_for_image(raw)
    if len(raw) < v.size:
        raw = raw + b"\xFF" * (v.size - len(raw))
    return bytes(raw[: v.size]), v


def make_responder(profile_name: str, image: bytes, read_mode: str = "serve",
                   log: Optional[Callable[[str], None]] = None):
    """Build the K-line service responder for ``profile_name``, reusing the
    existing TP2.0/CAN responders unchanged. A KWP2000 module (trx_sim profile:
    ``aisin09d``, ``zf6hp19``) -> :class:`~ovtsuite.flash.trx_sim.Trx09dResponder`
    with that profile; otherwise, as a fallback, an ecu_sim UDS profile (e.g.
    the 6HP28/GS19) -> :class:`~ovtsuite.flash.ecu_sim.EcuResponder`. Returns
    (responder, controller_address). ecu_sim is imported lazily."""
    log = log or (lambda s: None)
    if profile_name in _trx.profile_names():
        prof = _trx.profile(profile_name)
        return (_trx.Trx09dResponder(prof, image=image, read_mode=read_mode, log=log),
                getattr(prof, "controller", 0x01))
    try:
        from . import ecu_sim as _es
    except ImportError:
        import ecu_sim as _es                 # type: ignore
    if profile_name in _es.profile_names():
        return (_es.EcuResponder(_es.profile(profile_name), image=image,
                                 read_mode=read_mode, log=log), 0x01)
    raise ValueError("unknown module %r" % profile_name)


def _sgo_cal(raw: bytes) -> Tuple[int, bytes]:
    """(section addr, plaintext cal) from a 09D SGO. Header is plaintext; the
    payload blob is whole-file XOR 0xFF. Mirrors sgo.py's section layout
    (addr=u24 BE @+0x00, slen=u32 LE @+0x15, blob @+0x19)."""
    ms = struct.unpack_from("<I", raw, 0x29)[0]
    ml = struct.unpack_from("<I", raw, ms)[0]
    bs = ms + ml + 4
    hdr = raw[bs:bs + 0x19]
    addr = struct.unpack_from(">I", b"\x00" + hdr[0:3], 0)[0]
    slen = struct.unpack_from("<I", hdr, 0x15)[0]
    blob = raw[bs + 0x19: bs + 0x19 + slen]
    cal = bytes(c ^ 0xFF for c in blob)
    return addr, cal


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def main(argv=None):   # pragma: no cover - hardware/CLI entry point
    import argparse
    ap = argparse.ArgumentParser(description="K-line ECU simulator (09D / 6HP19)")
    ap.add_argument("port", help="serial port: COMx, /dev/ttyUSBx, or FTDI:<serial> / FTDIIDX:<n>")
    ap.add_argument("--ecu", default="aisin09d", choices=["aisin09d", "gs19"],
                    help="which ECU to simulate (gs19 = ZF 6HP19)")
    ap.add_argument("--image", help=".bin or .sgo to serve (default: blank flash)")
    ap.add_argument("--variant", choices=sorted(VARIANTS), help="force a 09D variant (else auto)")
    ap.add_argument("--baud", type=int, default=10400, help="init baud (default 10400)")
    ap.add_argument("--controller", type=lambda s: int(s, 0), default=None,
                    help="KWP controller address (default: per-ECU)")
    ap.add_argument("--no-echo", action="store_true",
                    help="port is full-duplex (virtual COM pair), not a shared K-line wire")
    ap.add_argument("--mode", default="serve", choices=["serve", "silent"])
    args = ap.parse_args(argv)

    if args.ecu == "aisin09d":
        forced = VARIANTS[args.variant] if args.variant else None
        image, variant = load_09d_image(args.image, forced)
        print("serving 09D variant %s (flash 0x%X, cal 0x%X..0x%X)"
              % (variant.name, variant.size, variant.rng[0], variant.rng[1]))
    else:
        image = open(args.image, "rb").read() if args.image else b""
        print("serving 6HP19 (GS19); image %d bytes" % len(image))
    responder, default_ctrl = make_responder(args.ecu, image, read_mode=args.mode, log=print)
    controller = args.controller if args.controller is not None else default_ctrl
    port = KLine(args.port, baud=args.baud)
    ecu = KLineEcu(port, responder, controller=controller,
                   echo=not args.no_echo, baud_raise=BaudRaise(), log=print)
    try:
        ecu.run()
    except KeyboardInterrupt:
        print("stopped (%d requests handled)" % ecu.count)


__all__ = ["KLineEcu", "BaudRaise", "Variant", "VARIANTS", "variant_for_image",
           "make_responder", "build_frame", "load_09d_image",
           "TESTER", "START_COMM", "START_COMM_POS"]


if __name__ == "__main__":   # pragma: no cover
    main()
