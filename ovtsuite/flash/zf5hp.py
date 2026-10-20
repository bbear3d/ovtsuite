# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP19 / 5HP24 TCU flashing over K-line KWP2000 -- EXPERIMENTAL.

**This is not wired into the GUI and must not be, until it is confirmed on a
bench TCU.** Call :meth:`Zf5hpFlash.write_image` directly for bench testing,
with ``i_have_bench_confirmed=True``; without it the write refuses rather than
risk bricking a unit on an unproven sequence.

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
* whether a full OBD *read* is offered at all (almost certainly not -- the
  memory-read path is boot-ROM-resident; "read" in practice is identify +
  pull the matching ``.sgo`` from SGODatabase).

See project docs ``zf5hp19_feasibility.md`` §5-6 and ``zf5hp_module_plan.md`` §5.
"""
from __future__ import annotations

import struct
from typing import Callable, List, Optional

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
SID_SECURITY = 0x27
SID_START_ROUTINE = 0x31
SID_REQ_DOWNLOAD = 0x34
SID_TRANSFER = 0x36
SID_TRANSFER_EXIT = 0x37

SESSION_PROGRAMMING = 0x85            # captured "10 85" on the sibling TCUs
IDENT_PART_NUMBER = 0x9B              # readEcuIdentification -> spare-part number

#: Max payload bytes per transferData frame (conservative K-line default; a
#: real capture may show the TCU's negotiated block size).
TRANSFER_CHUNK = 0xFE


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


class Zf5hpFlash:
    """A K-line KWP2000 session with a ZF 5HP TCU.

    ``link`` is a :class:`ovtsuite.flash.transport.KLine`. ``sa2_tape`` is the
    12-byte tape from the source ``.sgo`` metadata (``SgoContainer`` callers
    can read it from the parsed container)."""

    def __init__(self, link, sa2_tape: bytes, log: Optional[Callable] = None):
        self.kwp = _kwp.Kwp2000(link, controller=CONTROLLER, log=log)
        self.tape = sa2_tape
        self._log = log or (lambda s: None)

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
        bench: it does not modify flash)."""
        self.send_key(security_key(self.tape, self.request_seed()))

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
