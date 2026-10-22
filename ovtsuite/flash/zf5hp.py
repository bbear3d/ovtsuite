# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP19 / 5HP24 TCU flashing over K-line KWP2000 -- EXPERIMENTAL.

**The WRITE path is not wired into the GUI and must not be, until it is
confirmed on a bench TCU.** Call :meth:`Zf5hpFlash.write_image` directly for
bench testing, with ``i_have_bench_confirmed=True``; without it the write
refuses rather than risk bricking a unit on an unproven sequence.

The READ path (:meth:`Zf5hpFlash.read_image`) never modifies flash:
``readMemoryByAddress`` (service 0x23) only reads, and a wrong address/length
only draws a NAK, which the read logs and skips. Run it on a bench, or in the
car with the ignition on and the **engine off**: by default it enters the
programming session (0x85), in which the TCU stops controlling the gearbox
until the session ends or the ignition is cycled. It is the
standard KWP2000 upload primitive the EDC15 (``read_cpu_range``) and Aisin 09D
(``read_image``) flashers already use. Its purpose here is to pull a **full
1 MiB image**: the 512 KiB dumps in the corpus are the *low half* of a 1 MiB
TCU (AM29F800-class), and the upper half -- where the code that consumes the
0x60xxx/0x65xxx calibration bank lives -- was never dumped (see
``zf5hp_batch_and_shift_maps_2026_10_20.md``). The flash sector map
(DiaGRA ``[AL 600 / GS 19.04]``) is 0x8000-0x6FF7F, 0x90080-0xAFFFF,
0xB0000-0xDFFFF, 0xE0080-0xFFFFF.

What is known and implemented honestly:

* **Transport** -- K-line KWP2000 (ISO 14230), via :class:`ovtsuite.flash.kwp.Kwp2000`,
  the same primitive the EDC15 flasher uses. The TCU answers as controller 0x01.
* **Security access** -- the ``.sgo`` carries the VW SA2 bytecode tape; the key
  is ``crypto.sa2_execute(tape, seed)``. This half of the handshake is real and
  testable (:meth:`security_key`); it is symmetric with the TCU's seed
  generator by the SA2 standard, but that generator lives in the MCU boot ROM,
  not in any flash dump, so the pairing is unconfirmed until a bench run.
* **Transfer encoding** -- the TCU decrypts (``GEHEIM``) and decompresses the
  BCB Type1 area itself (requestDownload dataFormatIdentifier 0x11), so
  transferData carries the area's BCB stream verbatim; :func:`area_stream`
  takes it straight from the container section.

What is UNVERIFIED and therefore parameterised, never hardcoded from a guess:

* the eraseFlash routine's local id and arguments (``erase_routine``),
* the requestDownload addressing/length format (``download_request``),
* for the read: the diagnostic-session id and whether securityAccess is needed
  before ``readMemoryByAddress`` -- both parameterised, with the read
  recovering automatically if the TCU answers securityAccessDenied (0x33).

See project docs ``zf5hp19_feasibility.md`` §5-6 and ``zf5hp_module_plan.md`` §5.
"""
from __future__ import annotations

import struct
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    from . import kwp as _kwp
    from . import crypto as _crypto
    from .transport import KLineError
except ImportError:                       # flat import (e.g. in a bridge)
    import kwp as _kwp                     # type: ignore
    import crypto as _crypto               # type: ignore
    from transport import KLineError       # type: ignore

CONTROLLER = 0x01

SID_START_DIAG = 0x10
SID_READ_ECU_ID = 0x1A
SID_READ_MEMORY = 0x23               # readMemoryByAddress (the safe upload path)
SID_SECURITY = 0x27
SID_START_ROUTINE = 0x31
SID_REQ_DOWNLOAD = 0x34
SID_TRANSFER = 0x36
SID_TRANSFER_EXIT = 0x37
SID_STOP_COMM = 0x82

SESSION_PROGRAMMING = 0x85            # captured "10 85" on the sibling TCUs
IDENT_PART_NUMBER = 0x9B              # readEcuIdentification -> spare-part number

NRC_SECURITY_DENIED = 0x33            # securityAccessDenied: unlock, then retry

#: Max payload bytes per transferData frame (conservative K-line default; a
#: real capture may show the TCU's negotiated block size).
TRANSFER_CHUNK = 0xFE

#: Max bytes requested per readMemoryByAddress frame. A KWP2000 response with a
#: length byte carries well over this; 0xFC keeps the 3-byte-addr + 1-byte-len
#: request (the EDC15/09D convention) safely under the single-length-byte count.
READ_CHUNK = 0xFC

#: The full TCU address space of the 1 MiB (AM29F800-class) units.
IMAGE_SIZE = 0x100000

#: The flash regions to read, ``(start, end_inclusive)``. These are the DiaGRA
#: ``[AL 600 / GS 19.04]`` erase sector map, plus the boot/vector region below
#: 0x8000 and the low data block, so a full dump is a standalone 1 MiB image
#: whose low half is byte-comparable to the existing 512 KiB corpus reads.
READ_SPANS: Tuple[Tuple[int, int], ...] = (
    (0x00000, 0x6FF7F),               # boot + vectors + app block 1
    (0x70000, 0x7FFFF),               # low calibration data block
    (0x90080, 0xAFFFF),               # upper app block
    (0xB0000, 0xDFFFF),               # upper app block
    (0xE0080, 0xFFFFF),               # upper app block
)


class Zf5hpError(KLineError):
    pass


def security_key(tape: bytes, seed: bytes) -> bytes:
    """The 4-byte securityAccess key for ``seed`` (big-endian), via the SA2
    bytecode ``tape`` from the ``.sgo`` metadata. Pure function -- the one
    part of the write handshake that can be checked without a TCU."""
    if len(seed) != 4:
        raise Zf5hpError("expected a 4-byte seed, got %d bytes" % len(seed))
    key = _crypto.sa2_execute(tape, struct.unpack(">I", seed)[0])
    return struct.pack(">I", key & 0xFFFFFFFF)


def area_stream(container, addr: int = 0x8000) -> bytes:
    """The BCB Type1 wire stream for the container's area at ``addr`` -- the
    exact bytes transferData sends (the TCU decrypts/decompresses them itself).

    This is the section's stored blob with the container's outer XOR 0xFF
    undone -- i.e. the original wire bytes verbatim for an unedited area, and
    the re-encoded bytes for an edited one (``ovtsuite.sgo.apply_image`` writes
    the new blob before this is called). Using the stored blob rather than
    re-encoding here keeps an unedited flash byte-faithful and sidesteps the
    open question of whether the TCU decoder accepts a different literal/RLE
    split than Bosch's packer produced."""
    for s in container.sections:
        if s.addr == addr and s.scheme == "bcb1":
            return bytes(b ^ 0xFF for b in s.blob)
    raise Zf5hpError("no decoded BCB area at 0x%X in this container" % addr)


def _chunks(data: bytes, n: int) -> List[bytes]:
    return [data[i:i + n] for i in range(0, len(data), n)]


def _addr3(a: int) -> bytes:
    return bytes([(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF])


class Zf5hpFlash:
    """A K-line KWP2000 session with a ZF 5HP TCU.

    ``link`` is a :class:`ovtsuite.flash.transport.KLine`. ``sa2_tape`` is the
    12-byte tape from the source ``.sgo`` metadata (``SgoContainer`` callers
    can read it from the parsed container); it is only needed for the write
    path and for reading a security-locked region, so it is optional."""

    def __init__(self, link, sa2_tape: Optional[bytes] = None,
                 log: Optional[Callable] = None):
        self.kwp = _kwp.Kwp2000(link, controller=CONTROLLER, log=log)
        self.tape = sa2_tape
        self._log = log or (lambda s: None)
        self._unlocked = False
        #: a handshake was tried and failed: not tried again (each wrong key
        #: counts towards the TCU's attempt limit and lockout delay)
        self._unlock_failed = False

    # -- read-only, safe ---------------------------------------------------
    def read_part_number(self) -> str:
        msg = self.kwp.send_receive(SID_READ_ECU_ID, bytes([IDENT_PART_NUMBER]))
        return msg.body[1:].split(b"\x00")[0].decode("latin-1", "replace").strip()

    def request_seed(self) -> bytes:
        msg = self.kwp.send_receive(SID_SECURITY, b"\x01")
        return bytes(msg.body[1:5])

    def send_key(self, key: bytes) -> None:
        self.kwp.send_receive(SID_SECURITY, b"\x02" + key)

    def unlock(self) -> None:
        """Full securityAccess handshake using the SA2 tape (safe to try on a
        bench: it does not modify flash). Needs ``sa2_tape`` -- it is the SA2
        bytecode from the matching ``.sgo``."""
        if not self.tape:
            raise Zf5hpError(
                "securityAccess needs the SA2 tape from the matching .sgo "
                "(construct Zf5hpFlash with sa2_tape=...).")
        try:
            seed = self.request_seed()
            if len(seed) != 4:
                raise Zf5hpError("securityAccess: the seed is %d bytes, not 4 "
                                 "(%s)" % (len(seed), seed.hex() or "empty"))
            if any(seed):
                self.send_key(security_key(self.tape, seed))
            # (a zero seed: already unlocked)
        except Exception:
            self._unlock_failed = True
            raise
        self._unlocked = True

    # -- session helpers ---------------------------------------------------
    def connect(self, *, wake: bool = True, init_addr: int = CONTROLLER,
                session: Optional[int] = SESSION_PROGRAMMING) -> None:
        """Bring the link up for a session: optionally 5-baud/fast wake the
        K-line, then startDiagnosticSession. ``wake=False`` assumes the caller
        has already woken the link; ``session=None`` skips the session change
        (some TCUs answer readMemoryByAddress in the default session)."""
        self.kwp.controller = init_addr     # replies come from that address
        if wake:
            self.kwp.k.wake_up(address=init_addr)
        if session is not None:
            self.kwp.send_receive(SID_START_DIAG, bytes([session]))

    def stop(self) -> None:
        """stopCommunication (best effort; never raises)."""
        try:
            self.kwp.send_receive(SID_STOP_COMM, b"")
        except Exception:
            pass

    # -- read: SAFE (readMemoryByAddress never writes flash) ---------------
    def read_memory(self, addr: int, length: int, *, chunk: int = READ_CHUNK,
                    on_chunk: Optional[Callable[[int], None]] = None,
                    is_stop: Optional[Callable[[], bool]] = None) -> bytes:
        """Read ``length`` bytes from CPU-bus ``addr`` via readMemoryByAddress
        (0x23), 3-byte address + 1-byte length, in ``chunk``-byte requests --
        the EDC15/09D convention. Raises on NAK; :meth:`read_image` wraps this
        to tolerate NAKs per region. Auto-unlocks once on securityAccessDenied
        if a tape is available."""
        out = bytearray()
        off = 0
        while off < length:
            if is_stop and is_stop():
                raise Zf5hpError("read cancelled")
            n = min(chunk, length - off)
            a = addr + off
            body = _addr3(a) + bytes([n])
            try:
                msg = self.kwp.send_receive(SID_READ_MEMORY, body)
            except _kwp.NegativeResponse as nr:
                if (nr.nrc == NRC_SECURITY_DENIED and self.tape
                        and not self._unlocked and not self._unlock_failed):
                    self._log("zf5hp: readMemory 0x%06X securityAccessDenied; "
                              "unlocking and retrying" % a)
                    self.unlock()
                    msg = self.kwp.send_receive(SID_READ_MEMORY, body)
                else:
                    raise
            data = msg.body
            if len(data) != n:
                # never placed: a short, long or address-echoing reply would
                # shift everything after it in the image
                raise Zf5hpError("readMemory 0x%06X: %d bytes back for %d asked"
                                 % (a, len(data), n))
            out += data
            off += len(data)
            if on_chunk:
                on_chunk(len(data))
        return bytes(out)

    def read_image(self, spans: Sequence[Tuple[int, int]] = READ_SPANS, *,
                   image_size: int = IMAGE_SIZE, chunk: int = READ_CHUNK,
                   unlock_first: bool = True, do_connect: bool = True,
                   session: Optional[int] = SESSION_PROGRAMMING,
                   init_addr: int = CONTROLLER,
                   on_percent: Optional[Callable[[int], None]] = None,
                   on_stage: Optional[Callable[[str], None]] = None,
                   is_stop: Optional[Callable[[], bool]] = None) -> bytes:
        """Dump the whole TCU into an ``image_size`` (default 1 MiB) buffer, each
        region placed at its real address, bytes outside the read regions left
        0xFF. SAFE: readMemoryByAddress never modifies flash; a region the TCU
        NAKs is logged and left 0xFF (the read moves to the next region rather
        than guessing the rest).

        ``do_connect`` drives the whole session (wake + startDiagnosticSession +
        optional unlock); pass ``do_connect=False`` to read on a link the caller
        already set up. ``unlock_first`` does the securityAccess handshake up
        front when a tape is present (locked regions also auto-unlock on the
        first 0x33, so this is just to avoid a wasted NAK).

        Returns the image bytes; the low half is byte-comparable to the 512 KiB
        corpus reads, the upper half is the previously-undumped code/data."""
        stage = on_stage or self._log
        if do_connect:
            stage("Connecting to the ZF 5HP TCU (K-line KWP2000)...")
            self.connect(wake=True, init_addr=init_addr, session=session)
            try:
                pn = self.read_part_number()
                if pn:
                    stage("TCU part number: %s" % pn)
            except Exception as ex:
                stage("readEcuId skipped (%s)" % ex)
        if (unlock_first and self.tape and not self._unlocked
                and not self._unlock_failed):
            try:
                self.unlock()
                stage("securityAccess unlocked")
            except Exception as ex:
                stage("securityAccess failed (%s); locked regions are "
                      "skipped" % ex)
        try:
            buf = bytearray(b"\xFF" * image_size)
            total = sum((end - start + 1 + chunk - 1) // chunk
                        for start, end in spans) or 1
            done = 0
            for start, end in spans:
                length = end - start + 1
                stage("Reading 0x%05X-0x%05X (%d KiB)..."
                      % (start, end, length // 1024))
                off = 0
                while off < length:
                    if is_stop and is_stop():
                        raise Zf5hpError("read cancelled")
                    n = min(chunk, length - off)
                    a = start + off
                    try:
                        data = self.read_memory(a, n, chunk=chunk, is_stop=is_stop)
                    except _kwp.NegativeResponse as nr:
                        stage("0x%05X NAK'd (NRC 0x%02X); skipping rest of region"
                              % (a, nr.nrc))
                        done += (length - off + chunk - 1) // chunk
                        break
                    buf[a:a + len(data)] = data
                    off += len(data)
                    done += 1
                    if on_percent:
                        on_percent(min(99, int(100 * done / total)))
            if on_percent:
                on_percent(100)
            return bytes(buf)
        finally:
            if do_connect:
                self.stop()

    # -- write: EXPERIMENTAL, bench-gated ----------------------------------
    def write_image(self, container, *, erase_routine, download_request,
                    i_have_bench_confirmed: bool = False, progress=None) -> None:
        """Rewrite the TCU's single area from ``container`` (a decoded
        :class:`ovtsuite.sgo.SgoContainer`). REFUSES unless
        ``i_have_bench_confirmed`` is True, because the erase and
        requestDownload framing is unverified (see the module docstring).

        ``erase_routine`` -> bytes: the startRoutineByLocalId body that erases
        the area. ``download_request`` -> bytes: the requestDownload body
        (addressing + length + dataFormatIdentifier 0x11). Both come from a
        real flash-tool capture, not from this module."""
        if not i_have_bench_confirmed:
            raise Zf5hpError(
                "ZF 5HP write is experimental and unverified: the erase and "
                "requestDownload framing has not been confirmed on a bench TCU. "
                "Refusing to flash. See zf5hp19_feasibility.md §5-6.")
        stream = area_stream(container)
        self.kwp.send_receive(SID_START_DIAG, bytes([SESSION_PROGRAMMING]))
        self.unlock()
        self.kwp.send_receive(SID_START_ROUTINE, bytes(erase_routine))
        self.kwp.send_receive(SID_REQ_DOWNLOAD, bytes(download_request))
        for i, chunk in enumerate(_chunks(stream, TRANSFER_CHUNK), 1):
            self.kwp.send_receive(SID_TRANSFER, bytes([i & 0xFF]) + chunk)
        self.kwp.send_receive(SID_TRANSFER_EXIT, b"")


def _sa2_tape_from_sgo(path: str) -> Optional[bytes]:
    """The SA2 bytecode tape of a ``.sgo``. Not available yet: where the tape
    sits in the metadata block is not established for the ZF 5HP, and a
    guessed tape would send wrong keys (each one counting towards the TCU's
    lockout). Pass the tape itself (``--sa2-tape``)."""
    raise Zf5hpError(
        "reading the SA2 tape from a .sgo is not supported yet; pass it as "
        "--sa2-tape HEX")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: pull a full 1 MiB image from a bench/car TCU over K-line.

        python -m ovtsuite.flash.zf5hp read <PORT> <OUT.bin> [--sa2-tape HEX]
               [--session 0xNN|none] [--init-addr 0xNN]
               [--no-unlock] [--baud 10400]

    Read-only: it never writes flash. Ignition on, engine off: the default
    programming session stops the TCU controlling the gearbox. Example:
        python -m ovtsuite.flash.zf5hp read COM5 5hp19_full.bin --session none
    """
    import argparse
    import sys
    try:
        from .transport import KLine
    except ImportError:
        from transport import KLine            # type: ignore

    ap = argparse.ArgumentParser(prog="ovtsuite.flash.zf5hp",
                                 description="ZF 5HP TCU full-image read (K-line KWP2000)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="dump a full 1 MiB image to a file")
    r.add_argument("port", help="serial port (COM5, /dev/ttyUSB0, FTDI:serial, ...)")
    r.add_argument("out", help="output .bin path")
    r.add_argument("--sgo", help="matching .sgo, for the SA2 tape if a region "
                   "is locked (not supported yet: use --sa2-tape)")
    r.add_argument("--sa2-tape", help="the SA2 tape as hex, if a region is locked")
    r.add_argument("--baud", type=int, default=10400)
    r.add_argument("--init-addr", default="0x01",
                   help="5-baud init address (default 0x01; the thread notes diag 0x02)")
    r.add_argument("--session", default=hex(SESSION_PROGRAMMING),
                   help="startDiagnosticSession id (default 0x85); 'none' to skip")
    r.add_argument("--no-unlock", action="store_true",
                   help="do not attempt securityAccess up front")
    args = ap.parse_args(list(argv) if argv is not None else None)

    def _int(s):
        return int(s, 0)

    try:
        tape = bytes.fromhex(args.sa2_tape) if args.sa2_tape else (
            _sa2_tape_from_sgo(args.sgo) if args.sgo else None)
    except (ValueError, Zf5hpError) as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    session = None if str(args.session).lower() == "none" else _int(args.session)

    last = [-1]

    def on_pct(p):
        if p != last[0]:
            last[0] = p
            print("\r  %3d%%" % p, end="", file=sys.stderr, flush=True)

    with KLine(args.port, baud=args.baud) as link:
        flash = Zf5hpFlash(link, sa2_tape=tape,
                           log=lambda s: print(s, file=sys.stderr))
        img = flash.read_image(unlock_first=not args.no_unlock,
                               session=session, init_addr=_int(args.init_addr),
                               on_percent=on_pct)
    print("", file=sys.stderr)
    with open(args.out, "wb") as f:
        f.write(img)
    read = sum(1 for b in img if b != 0xFF)
    print("wrote %s (%d bytes, %d non-0xFF)" % (args.out, len(img), read),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
