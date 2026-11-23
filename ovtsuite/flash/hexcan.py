# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CAN channel over a Ross-Tech HEX-USB+CAN cable.

``docs/HEX_USB_CAN_PROTOCOL.md`` reverse-engineers this cable's protocol from
USB captures and a disassembly of VCDS.exe. What that doc calls solved is
implemented here, byte for byte:

* the ``[0x53/0x4D][LEN][PAYLOAD][XOR-8]`` framing (``HC::SendCommand``'s own
  disassembly matched the captures exactly);
* the open sequence (ping, open/configure, GetVersion, the genuine-cable
  challenge, identity check, the two device-memory read passes, CAN
  controller open and the fixed filter/param set VCDS itself sends);
* the genuine-cable challenge's cipher -- standard TEA, 32 rounds, with the
  hardcoded key recovered from ``VCDS.exe``. Only round-type 1's response is
  ever checked by VCDS's own code (round-types 2/3/5 are computed and sent
  the same way VCDS sends them, but nothing reads their result -- see the
  doc's "Anti-clone" section), so that is all this module verifies either;
* ``HC::SCM`` ("SendCANMsg")'s transmit frame layout for ``0xB8``, a
  bit-exact ID/DLC/data/counter encoding confirmed by disassembly.

What is **not** solved, and is not guessed at here: the receive/poll half of
the ``0xB7``/``0xB8`` tunnel. Its dominant traffic does not fit
``HC::SCM``'s own frame shape, is not a real per-module CAN id, and the
function that actually builds a ``0xB7`` reply has not been located -- see
the doc's "The CAN tunnel" section. :meth:`HexCanChannel.read` raises rather
than parse that traffic as if it were CAN frames: guessing a frame layout
that turns out wrong would corrupt a diagnostic session silently, the same
reasoning ``ovtsuite/diag/canmux.py`` gives for never guessing which module a
frame came from. The RX filter words (``0xB3``/``0xB4``) are reproduced
exactly as VCDS sends them (the diagnostic-channel pair seen in every
capture); what their bits mean in general is not decoded, so
:class:`HexCanChannel` does not offer a general ``pass_ids``.

The open sequence has now been exercised against a real cable and gets
genuine ``0x4D`` replies well into it -- ping, open/configure, GetVersion,
the auth challenge, identity and both memory-read passes have each answered
for real in one live run or another (see "Live hardware test" in the
protocol doc, 2026-11-21 update), though a single run has not yet gone all
the way through ``open()`` without a mid-sequence miss. The fix was not a
transport-level detail at all: every prior attempt, including the earlier
version of this module, sent the whole session at a single baud rate from
the first byte; the real cable only listens for the ping and open/configure
at 19200 baud, switching to the ~115385 rate everything else runs at only
*after* both of those replies arrive. :meth:`open` now reproduces that ramp,
plus the device's own unprompted "ready" beacon right after the switch
(waited for, not replied to) and the fact that round-type 3 of the challenge
can come back malformed on real hardware -- consistent with nothing ever
checking it (see the "Anti-clone" section) -- so round-types 2/3/5 are now
fire-and-forget. Every command retries with backoff
(``HC::KMode``'s own ping loop does the same rather than expecting an
answer on the first try); a cable that started a handshake and was not let
finish it (an aborted earlier attempt, a killed process) needs a real power
cycle, not just a fresh handle, before it answers again -- confirmed on the
bench: a logical USB re-enumeration (``FT_CyclePort``) is not enough, the
cable comes back echoing garbled bytes until physically unplugged and
replugged.
"""
import struct
import time
from typing import List, Optional

from . import j2534 as J

CMD_PING = 0xA0
CMD_OPEN = 0x03
CMD_ACK = 0xFE
CMD_VERSION = 0x02
CMD_CHALLENGE = 0x09
CMD_IDENTITY = 0x04
CMD_UNKNOWN_19 = 0x19
CMD_MEM_READ = 0x0B
CMD_CAN_OPEN1 = 0xB0
CMD_CAN_OPEN2 = 0xB1
CMD_CAN_PARAM = 0xB2
CMD_CAN_MASK = 0xB3
CMD_CAN_FILTER = 0xB4
CMD_CAN_SMALL = 0xB5
CMD_CAN_TX = 0xB8
CMD_CAN_RX = 0xB7

WAKE_BYTE = 0xA5
SOF_HOST, SOF_DEVICE = 0x53, 0x4D

#: ``0x03`` open/configure payload, constant across every capture
OPEN_CONFIG = bytes([0x00, 0xC2, 0x01, 0x00])
#: the CAN controller param/filter/small set VCDS sends on every open, in
#: order -- byte-for-byte from all five captures (docs/HEX_USB_CAN_PROTOCOL.md,
#: "Open sequence"). Not a general filter encoding: this is the one
#: diagnostic-channel configuration seen on the wire.
CAN_PARAM = bytes([0xB8, 0x05])
CAN_MASKS = (bytes([0x00, 0xFF, 0xE0, 0xFF, 0x00]), bytes([0x01, 0xFF, 0xE0, 0x00, 0x00]))
CAN_FILTERS = (
    bytes.fromhex("0040200000"), bytes.fromhex("0143e00000"), bytes.fromhex("0260000000"),
    bytes.fromhex("0300000000"), bytes.fromhex("0400000000"), bytes.fromhex("05ef400000"),
)
CAN_SMALL = (bytes([0x00, 0x04]), bytes([0x01, 0x00]))

#: the TEA cipher recovered from HC's cmd-0x09 handler (VA 0x140082ea0 in
#: VCDS.exe) -- see docs/HEX_USB_CAN_PROTOCOL.md, "Anti-clone"
_MASK = 0xFFFFFFFF
_DELTA = 0x9E3779B9
_K0, _K1, _K2, _K3 = 0xB9C6A1BD, 0x81D2CB2D, 0x0C29337A, 0x5F412A79


def tea_encrypt(a: int, b: int) -> tuple:
    """(a, b): two u32 words -> the cable's expected round-type-1 response."""
    z, y, s = a & _MASK, b & _MASK, 0
    for _ in range(32):
        s = (s + _DELTA) & _MASK
        z = (z + ((((y << 4) & _MASK) + _K0) & _MASK ^ ((y + s) & _MASK) ^ (((y >> 5) + _K1) & _MASK))) & _MASK
        y = (y + ((((z << 4) & _MASK) + _K2) & _MASK ^ ((z + s) & _MASK) ^ (((z >> 5) + _K3) & _MASK))) & _MASK
    return z, y


def checksum(frame_without_checksum: bytes) -> int:
    x = 0
    for b in frame_without_checksum:
        x ^= b
    return x


def build_frame(cmd: int, data: bytes = b"", sof: int = SOF_HOST) -> bytes:
    """``[SOF][LEN][cmd][data][XOR-8]`` -- ``HC::SendCommand``'s framing.
    ``sof`` defaults to the host's ``0x53``; tests build a device-side
    ``0x4D`` reply frame with ``sof=SOF_DEVICE`` so its checksum is computed
    over the right leading byte."""
    payload = bytes([cmd]) + bytes(data)
    head = bytes([sof, len(payload) + 3]) + payload
    return head + bytes([checksum(head)])


class HexCanError(Exception):
    """The cable did not answer, or answered in a way the protocol doc does
    not account for."""


class _FrameBuffer:
    """Accumulates raw bytes from the port and splits out complete,
    checksum-valid ``[SOF][LEN]...[XOR-8]`` frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[tuple]:
        self._buf += data
        out = []
        i, n = 0, len(self._buf)
        consumed = 0
        while i < n:
            sof = self._buf[i]
            if sof in (SOF_HOST, SOF_DEVICE) and i + 1 < n:
                ln = self._buf[i + 1]
                if ln >= 3 and i + ln <= n:
                    frame = bytes(self._buf[i:i + ln])
                    if checksum(frame[:-1]) == frame[-1]:
                        out.append((sof, frame[2:-1]))
                        i += ln
                        consumed = i
                        continue
            i += 1
        if consumed:
            del self._buf[:consumed]
        return out


class HexCanChannel:
    """One CAN channel on a Ross-Tech HEX-USB+CAN cable.

    ``port`` is anything with the pyserial surface
    :func:`ovtsuite.flash.ftdi.open_port` returns (``write`` / ``read`` /
    ``in_waiting`` / ``timeout`` / ``reset_input_buffer`` / ``close``).
    """

    def __init__(self, port, open_: bool = True, reply_timeout: float = 0.3, ping_tries: int = 6):
        self.port = port
        self.reply_timeout = reply_timeout
        #: HC::KMode's own ping loop retries rather than expecting an
        #: answer on the first try (see :meth:`_retry`); a test double that
        #: never answers can lower this to avoid real sleeping.
        self.ping_tries = ping_tries
        self._rbuf = _FrameBuffer()
        self.tx_counter = 0
        if open_:
            self.open()

    # ---- framing I/O ----
    def _poll_in_waiting(self) -> int:
        n = getattr(self.port, "in_waiting", 0)
        return n() if callable(n) else n

    def _drain_for(self, seconds: float):
        """Discard whatever arrives for a fixed time (the wake byte's echo,
        which nothing needs to look at)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            n = self._poll_in_waiting()
            if n:
                self._rbuf.feed(self.port.read(n))
            else:
                time.sleep(0.001)

    def command(self, cmd: int, data: bytes = b"", timeout: Optional[float] = None) -> bytes:
        """Send one framed command, return the device's reply payload
        verbatim. Raises :class:`HexCanError` if the device never answers
        with its own ``0x4D`` frame within the timeout -- an echo of the
        host's own bytes (which always arrives first, see the module
        docstring) does not count as an answer, so this keeps draining past
        it rather than giving up on the first frame seen.

        The reply is *not* filtered by its own first byte matching ``cmd``:
        most commands (``GetVersion``, the challenge, identity, memory
        reads) echo their own command byte as the reply's first byte with
        the real data following, but the ping and open/configure replies
        are a bare ``[0xFE]`` ack with no echoed command byte at all. Only
        one command is ever outstanding at a time here, so whichever
        ``0x4D`` frame arrives next is unambiguously the reply to the one
        just sent -- callers that need to tell data from an echoed command
        byte check ``payload[0]`` themselves (see :meth:`_challenge`)."""
        self.port.write(build_frame(cmd, data))
        deadline = time.monotonic() + (timeout if timeout is not None else self.reply_timeout)
        while time.monotonic() < deadline:
            n = self._poll_in_waiting()
            if n:
                for sof, payload in self._rbuf.feed(self.port.read(n)):
                    if sof == SOF_DEVICE:
                        return payload
            else:
                time.sleep(0.001)
        raise HexCanError(
            "HEX-USB+CAN cable did not answer cmd 0x%02X (echo only, or no reply at all -- "
            "see docs/HEX_USB_CAN_PROTOCOL.md, \"Live hardware test\")" % cmd)

    def _send(self, cmd: int, data: bytes = b""):
        """Write a framed command without waiting for any reply."""
        self.port.write(build_frame(cmd, data))

    def _wait_frame(self, timeout: float) -> bytes:
        """Wait for the device's next ``0x4D`` frame without sending
        anything first -- the one point in the open sequence where the
        device signals on its own (see :meth:`open`), not in reply to a
        command."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = self._poll_in_waiting()
            if n:
                for sof, payload in self._rbuf.feed(self.port.read(n)):
                    if sof == SOF_DEVICE:
                        return payload
            else:
                time.sleep(0.001)
        raise HexCanError(
            "HEX-USB+CAN cable: no unprompted reply at the new baud rate -- "
            "see docs/HEX_USB_CAN_PROTOCOL.md, \"Live hardware test\"")

    def _retry(self, cmd: int, data: bytes = b"", tries: int = 6) -> bytes:
        """``HC::KMode``'s own ping loop backs off ``300 ms * try + 300 ms``
        rather than expecting a reply on the first attempt -- a real cable
        does not always answer the very first try (see
        docs/HEX_USB_CAN_PROTOCOL.md, "Live hardware test", 2026-11-21
        update), so this mirrors that instead of treating one miss as
        fatal."""
        last: Optional[HexCanError] = None
        for i in range(tries):
            try:
                return self.command(cmd, data)
            except HexCanError as e:
                last = e
                # whatever is left in the buffer after a miss (an echo with
                # no reply yet, or garbled bytes) must not bleed into how
                # the next attempt's reply gets parsed
                self._rbuf = _FrameBuffer()
                time.sleep((i * 0.3) + 0.3)
        raise last

    # ---- open sequence ----
    def _set_baud(self, baud: int):
        self.port.baudrate = baud
        reset = getattr(self.port, "reset_input_buffer", None)
        if reset:
            reset()
        self._rbuf = _FrameBuffer()

    def open(self):
        """Ping and open/configure run at 19200 baud; the rest of the
        session runs at ~115385 (the closest a stock FTDI divisor gets to
        "115200" -- ``FT_SetBaudRate(115200)``/pyserial asking for 115200
        both land on exactly that divisor already, so passing 115200 here
        is the byte-identical request). This ramp -- 9600, then 19200 held
        through the ping/open-config handshake, only switching to 115200
        *after* both replies arrive -- is the one thing earlier attempts at
        this cable got wrong: every prior live-hardware attempt sent
        everything, including the very first ping, at 115200/115384 from
        the start, which the cable apparently never heard as a byte-for-byte
        valid frame. See docs/HEX_USB_CAN_PROTOCOL.md, "Live hardware test",
        2026-11-18 update, for the capture evidence (exact SET_BAUD_RATE
        vendor requests correlated to byte offsets in the open sequence).

        Starts by discarding whatever is already sitting in the input
        buffer: a cable that was mid-handshake from an earlier, abandoned
        attempt (a killed process, a previous open() that raised partway
        through) can still have a stale reply queued up, which would
        otherwise be misread as the reply to this session's first
        command."""
        # DTR and RTS are deasserted from byte zero of the whole session --
        # a fresh read of the capture's own MODEM_CTRL control transfers
        # (not just the bulk data this doc's earlier passes focused on)
        # shows both cleared before the wake byte or the first ping even go
        # out, re-cleared again after the ping's reply and again after
        # open/configure's. A serial port's own default (pyserial leaves
        # DTR high unless told otherwise) is not what the real cable's
        # session ever sees, at any baud -- clearing them only after the
        # jump to 115200, as an earlier version of this method did, runs
        # the whole 19200 ping/open exchange with the wrong line state.
        if hasattr(self.port, "dtr"):
            self.port.dtr = False
        if hasattr(self.port, "rts"):
            self.port.rts = False
        self._drain_for(0.2)
        self._rbuf = _FrameBuffer()
        self._set_baud(9600)
        time.sleep(0.05)
        self._set_baud(19200)
        time.sleep(0.05)
        self.port.write(bytes([WAKE_BYTE]))
        self._drain_for(0.3)                  # discard the echo of the wake byte
        self._retry(CMD_PING, tries=self.ping_tries)
        # VCDS re-asserts 19200 and re-clears DTR/RTS here too (both already
        # at those values -- electrically a no-op) and then waits ~100 ms
        # before sending open/configure -- and, crucially, purges the RX
        # buffer *again* right at the end of that wait, immediately before
        # the 0x03 frame goes out, not just once before the wait starts.
        # Confirmed from a millisecond-timestamped replay of one of
        # pcap4.pcapng's 85 reconnects. A live capture of this module's own
        # attempt (USBPcap, not just the serial driver's view) showed the
        # same handful of bytes repeating with a slow drift across retries
        # even though nothing had been sent yet to produce them -- something
        # the cable emits unprompted during that ~100 ms gap, which this
        # method used to leave sitting in the buffer for the *next* read to
        # pick up and misparse as open/configure's reply. One purge before
        # the wait and none after was the bug; the real cable's own reply to
        # open/configure, decoded from the capture, is a plain 4-byte
        # `[0x4D,0x04,0xFE]` ack -- never actually corrupted on the wire.
        self._set_baud(19200)
        if hasattr(self.port, "dtr"):
            self.port.dtr = False
        if hasattr(self.port, "rts"):
            self.port.rts = False
        time.sleep(0.1)
        reset = getattr(self.port, "reset_input_buffer", None)
        if reset:
            reset()
        self._rbuf = _FrameBuffer()
        self._retry(CMD_OPEN, OPEN_CONFIG, tries=self.ping_tries)
        self._set_baud(115200)
        # re-cleared once more here, matching the capture -- harmless if
        # they were already low, but kept for fidelity to the real session.
        if hasattr(self.port, "dtr"):
            self.port.dtr = False
        if hasattr(self.port, "rts"):
            self.port.rts = False
        # The capture shows a long idle gap (~220 ms) here, then the
        # *device* sends an unprompted "FD" frame on its own -- not a reply
        # to anything the host sent -- before the host answers with its own
        # "FE" (sent, not waited on) and, after a second idle gap
        # (~250 ms), moves on to GetVersion. Getting this order backwards
        # (sending FE first and waiting for a reply, as an earlier version
        # of this method did) is what produced garbled bytes on the bench.
        # Whether this beacon always arrives promptly is not yet certain,
        # so a few misses here are tolerated rather than treated as fatal
        # -- the host's own "FE" is sent either way.
        time.sleep(0.25)
        try:
            self._wait_frame(1.0)
        except HexCanError:
            for _ in range(self.ping_tries - 1):
                try:
                    self._wait_frame(1.0)
                    break
                except HexCanError:
                    continue
        self._send(CMD_ACK)
        time.sleep(0.3)
        self._retry(CMD_VERSION, tries=self.ping_tries)
        self._challenge(1)
        self._retry(CMD_IDENTITY, tries=self.ping_tries)
        self._retry(CMD_UNKNOWN_19, bytes([0x00]), tries=self.ping_tries)
        for i in range(8):
            self._retry(CMD_MEM_READ, struct.pack("<H", i), tries=self.ping_tries)
        self._retry(CMD_PING, tries=self.ping_tries)
        for i in range(8):
            self._retry(CMD_MEM_READ, struct.pack("<H", i), tries=self.ping_tries)
        for round_type in (5, 2, 3):           # sent, not checked -- see module docstring
            self._challenge(round_type, verify=False)
        self._retry(CMD_CAN_OPEN1, tries=self.ping_tries)
        self._retry(CMD_CAN_OPEN2, tries=self.ping_tries)
        self._retry(CMD_CAN_PARAM, CAN_PARAM, tries=self.ping_tries)
        for mask in CAN_MASKS:
            self._retry(CMD_CAN_MASK, mask, tries=self.ping_tries)
        for i, flt in enumerate(CAN_FILTERS):
            self._retry(CMD_CAN_FILTER, flt, tries=self.ping_tries)
        for small in CAN_SMALL:
            self._retry(CMD_CAN_SMALL, small, tries=self.ping_tries)

    def _challenge(self, round_type: int, verify: bool = True):
        import random
        a, b = random.getrandbits(32), random.getrandbits(32)
        data = bytes([round_type]) + struct.pack("<II", a, b)
        if not verify:
            # round-types 2/3/5: VCDS computes and sends these but nothing
            # ever reads the result (see the module docstring), and on the
            # bench the cable's reply to round-type 3 didn't even come back
            # as a well-formed frame -- fire-and-forget, then flush whatever
            # eventually arrives so it cannot be mistaken for the next
            # command's reply.
            self._send(CMD_CHALLENGE, data)
            self._drain_for(0.3)
            return
        reply = self._retry(CMD_CHALLENGE, data, tries=self.ping_tries)
        if round_type == 1:
            # the reply echoes the 0x09 command byte first, then 8 data bytes
            if len(reply) < 9 or reply[0] != CMD_CHALLENGE:
                raise HexCanError("cmd 0x09 round-type 1: unexpected reply %s" % reply.hex())
            expect = tea_encrypt(a, b)
            got = struct.unpack_from("<II", reply, 1)
            if got != expect:
                raise HexCanError(
                    "cmd 0x09 round-type 1: cable's response %08x %08x does not match "
                    "tea_encrypt(a, b) = %08x %08x -- not a genuine cable, or the recovered "
                    "key/cipher is wrong" % (got[0], got[1], expect[0], expect[1]))

    # ---- transmit (HC::SCM / SendCANMsg -- fully decoded) ----
    def write_can(self, can_id: int, payload: bytes, extended: bool = False, timeout_ms: int = 100):
        payload = bytes(payload)
        dlc = len(payload)
        if dlc > 8:
            raise HexCanError("CAN frame of %d bytes (max 8)" % dlc)
        if extended:
            d1 = (can_id >> 21) & 0xFF
            d2 = 0x08 | (((can_id >> 18) & 7) << 5) | ((can_id >> 16) & 3)
            d3 = (can_id >> 8) & 0xFF
            d4 = can_id & 0xFF
        else:
            d1 = (can_id >> 3) & 0xFF
            d2 = (can_id & 7) << 5
            d3 = d4 = 0
        body = (bytes([0x00, d1, d2, d3, d4, dlc]) + payload.ljust(8, b"\x00")
                + struct.pack("<H", self.tx_counter))
        self.tx_counter = (self.tx_counter + 1) & 0xFFFF
        self.command(CMD_CAN_TX, body, timeout=max(timeout_ms / 1000.0, self.reply_timeout))

    # ---- receive: not decoded, see module docstring ----
    def read(self, max_msgs: int = 32, timeout_ms: int = 100) -> List[J.Msg]:
        raise HexCanError(
            "the HEX-USB+CAN receive/poll frame layout (0xB7/0xB8) is not decoded -- "
            "see docs/HEX_USB_CAN_PROTOCOL.md, \"The CAN tunnel\"")

    def pass_all(self, extended: bool = False):
        pass

    def clear_rx(self):
        pass

    def close(self):
        self.port.close()

    disconnect = close


__all__ = ["HexCanChannel", "HexCanError", "build_frame", "tea_encrypt", "checksum"]
