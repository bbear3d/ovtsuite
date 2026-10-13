# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pass-through over a network link to KW-Dash running on the car's head unit.

The cable stays plugged into the head unit; this module makes it look local.
``RemoteKLine`` presents the same facade ``edc16.py`` / ``edc15.py`` / ``diag.py``
already drive (``wake_up`` / ``read`` / ``write_*`` / ``set_baud``), and
``RemoteTp20`` presents the same surface ``CanKLine(tp=...)`` already accepts --
so neither state machine changes at all.  It is the fourth link alongside
``transport.KLine`` (serial), ``kline_j2534.J2534KLine`` and
``tp20_transport.CanKLine``.

WHY THE OPS ARE SHAPED THE WAY THEY ARE
---------------------------------------
Everything with a deadline to the ECU runs on the head unit, in one operation:
the fast-init break pulse and its 50 ms Twup anchor, the 5-baud bit-bang, the
half-duplex echo drain, and the whole TP 2.0 acknowledgement loop.  That last
one is not caution -- acknowledgements arriving 40-75 ms late over a *subprocess
pipe on the same machine* passed the ECU's ~50 ms retransmit timer and it hung
up mid-read.  A radio hop is strictly worse than that pipe was.

So this end sends whole KWP messages and never bytes or CAN frames.  A 512 KB
read is ~530,000 bytes; one round trip each at even 5 ms would be 44 minutes of
network on a job the cable does in two and a half.  At message granularity the
cost is two round trips per message -- about 15% on a K-line write, and nothing
measurable over CAN, where the ECU's own ~608 B/s cap swamps it.
"""

import json
import socket
import struct

from .. import branding
from .transport import KLineError, KLineTimeout

PROTOCOL_VERSION = 1

#: How this end identifies itself on the wire.  Deliberately NOT derived from
#: the product name: the head unit keys a stored pairing by this string, so a
#: rename of the suite must not silently invalidate every token already issued.
#: The human-readable label sent beside it is the branded one, and that may move.
CLIENT_ID = "tuner"

_PREFIX = 4
_MAX_FRAME = 256 * 1024

#: Framing limit, not an ECU one -- the job layer refuses an image larger than the
#: biggest controller takes, which is a smaller number and a different question.
_MAX_BLOB = 4 * 1024 * 1024

#: Bridge error codes that mean "the controller did not answer", which the flash
#: state machines already handle as ``KLineTimeout`` and retry or fall back on.
#: Everything else is a ``BridgeError`` -- still a ``KLineError``, so an existing
#: ``except KLineError`` keeps working, but not something to retry blindly.
_TIMEOUT_CODES = ("timeout",)


class BridgeError(KLineError):
    """The head unit refused, or the link to it failed.

    Subclasses ``KLineError`` on purpose: every caller in this package already
    handles that, and a pass-through failure is a link failure by any other name.
    """

    def __init__(self, message, code=""):
        KLineError.__init__(self, message)
        self.code = code


def _send_frame(sock, text, blob=b""):
    """A 4-byte big-endian length, that many bytes of UTF-8 JSON, then the blob.

    The blob carries no length of its own: the frame already declared it in a
    ``"blob"`` member.  Hex inside the JSON was fine for thin pass-through, where
    a payload is a K-line frame; a staged job carries a 512 KB image, which hex
    would double while handing the JSON parser a megabyte of string.
    """
    body = text.encode("utf-8")
    if len(body) > _MAX_FRAME:
        raise BridgeError("Frame of %d bytes is past the %d-byte cap."
                          % (len(body), _MAX_FRAME))
    sock.sendall(struct.pack(">I", len(body)) + body)
    if blob:
        sock.sendall(blob)


def _recv_exact(sock, count):
    chunks = []
    have = 0
    while have < count:
        chunk = sock.recv(min(65536, count - have))
        if not chunk:
            raise BridgeError("The head unit closed the connection.", "link_failed")
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)


def _recv_frame(sock):
    """One frame, and the raw body it declared.  Returns ``(reply, blob)``."""
    length = struct.unpack(">I", _recv_exact(sock, _PREFIX))[0]
    if length > _MAX_FRAME:
        # The stream position is no longer known, so there is nothing to answer
        # with: any reply would be read at the wrong offset.
        raise BridgeError("Head unit declared a %d-byte frame." % length, "link_failed")
    text = _recv_exact(sock, length).decode("utf-8")
    reply = json.loads(text)
    declared = int(reply.get("blob", 0) or 0)
    if declared < 0 or declared > _MAX_BLOB:
        raise BridgeError("Head unit declared a %d-byte body." % declared, "link_failed")
    # Read it even if this end did not expect one: skipping the declared bytes is
    # what keeps the stream aligned for every message after it.
    blob = _recv_exact(sock, declared) if declared else b""
    return reply, blob


class BridgeClient:
    """One connection to KW-Dash's pass-through.

    Pair once with the PIN shown on the head unit; keep the token it returns and
    every later connect authenticates with an HMAC over a nonce the head unit
    chose, so a recorded session replays into nothing.
    """

    def __init__(self, host, port=35510, client_id=CLIENT_ID, name=None,
                 token=None, timeout=30.0, ssl_context=None, log=None):
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.name = name or branding.APP_NAME
        self.token = token
        self.timeout = float(timeout)
        self.ssl_context = ssl_context
        self._log = log or (lambda s: None)
        self._sock = None
        self.capabilities = {}
        self.nonce = b""

    # -- lifecycle --
    def connect(self):
        """Open the socket, greet, and authenticate if a token is already held.

        A connection that has only greeted has NOT taken the cable: the head unit
        keeps its gauges running until :meth:`claim`.  That is what lets this end
        read the capabilities and refuse an impossible job without interrupting
        anything.
        """
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.ssl_context is not None:
            sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        self._sock = sock

        greeting = self.call("hello", version=PROTOCOL_VERSION,
                             client=self.client_id, name=self.name)
        self.nonce = bytes.fromhex(greeting.get("nonce", ""))
        self.capabilities = {
            "cable": greeting.get("cable", ""),
            "can": bool(greeting.get("can", False)),
            "five_baud": bool(greeting.get("five_baud", False)),
            "max_speed": int(greeting.get("max_speed", 0x50)),
            "paired": bool(greeting.get("paired", False)),
        }

        if self.token:
            self.authenticate()
        return self.capabilities

    def peer_fingerprint(self):
        """SHA-256 of the head unit's certificate, or b"" on a link without one.

        Over the DER the handshake gave us -- the same bytes the head unit hashes
        its own certificate to -- so neither end ever has to send it.
        """
        import hashlib

        sock = self._sock
        getter = getattr(sock, "getpeercert", None)
        if getter is None:
            return b""
        der = getter(binary_form=True)
        return hashlib.sha256(der).digest() if der else b""

    def pair(self, pin):
        """Exchange the PIN on the head unit's screen for a long-term token.

        THE PIN IS NEVER SENT.  It is used as the key of an HMAC over what this
        connection actually is: the head unit's certificate fingerprint followed
        by the nonce it just issued.  That matters at exactly this moment and no
        other -- the certificate is self-signed, so on a FIRST connect there is
        nothing pinned to compare it against, and anyone on the same Wi-Fi could
        offer one of their own.  A relay in the middle holds a different
        certificate, so the proof it passes on does not verify at the real head
        unit, and it cannot compute its own without the six digits on the dash.

        On Bluetooth there is no certificate and the binding is the nonce alone:
        still replay-proof, and the link itself is already authenticated by BT
        pairing before any of this reaches it.
        """
        import hashlib
        import hmac

        binding = self.peer_fingerprint() + self.nonce
        proof = hmac.new(str(pin).encode("utf-8"), binding, hashlib.sha256).digest()
        reply = self.call("auth", pin_proof=proof.hex())
        self.token = bytes.fromhex(reply["token"])
        return self.token

    def authenticate(self):
        """Prove knowledge of the token over this connection's binding.

        THE SAME BINDING THE PIN IS PROVED OVER -- certificate fingerprint, then
        nonce -- and not the nonce alone, which is what this used to be and was
        wrong.  Pairing binds the PIN to the certificate so a machine in the
        middle cannot pass a pairing on; every connect after the first uses the
        token instead, and a MAC over the nonce alone says nothing about which
        certificate this end is looking at.  The relay takes that MAC, replays it
        to the real head unit over its own connection, and is in.  The careful
        half of the design was the half that runs once.

        Over a link with no certificate -- Bluetooth -- the binding is the nonce
        alone, as before: BT pairing has already authenticated that link.
        """
        import hashlib
        import hmac

        if not self.token:
            raise BridgeError("No token: pair with the PIN on the head unit first.",
                              "auth_failed")
        binding = self.peer_fingerprint() + self.nonce
        mac = hmac.new(self.token, binding, hashlib.sha256).digest()
        self.call("auth", mac=mac.hex())

    def claim(self):
        """Take the cable, which stops the dashboard polling."""
        self.call("claim")

    def release(self):
        """Give the cable back and let the gauges resume."""
        self.call("release")

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one request, one reply --
    def call(self, op, blob=b"", **fields):
        reply, _ = self.call_blob(op, blob=blob, **fields)
        return reply

    def call_blob(self, op, blob=b"", **fields):
        """The same, but keeping whatever raw body came back.  Returns ``(reply, blob)``."""
        if self._sock is None:
            raise BridgeError("Not connected to a head unit.", "link_failed")

        fields["op"] = op
        if blob:
            fields["blob"] = len(blob)
        _send_frame(self._sock, json.dumps(fields), blob)
        reply, out = _recv_frame(self._sock)

        if reply.get("ok"):
            return reply, out

        code = reply.get("code", "")
        message = reply.get("error", "the head unit refused %r" % op)
        if code in _TIMEOUT_CODES:
            raise KLineTimeout("pass-through: " + message)
        raise BridgeError("pass-through: " + message, code)

    # -- staged jobs: the head unit runs the whole thing --
    def stage(self, job, image=b""):
        """Hand over a descriptor and its image.

        THE LAST MOMENT AT WHICH REFUSING IS FREE.  Everything the head unit can
        check without touching the controller is checked here -- what the cable
        can do, whether the blocks are consistent, whether the image that arrived
        is the one they describe.  After the job starts there may be an erased
        block, and an EDC16 in that state comes back through boot mode.
        """
        message = dict(job)
        message.pop("op", None)
        return self.call("job_put", blob=bytes(image), **message)

    def start(self, from_block=0):
        """Run the staged job, optionally resuming from a block already done."""
        return self.call("job_start", **{"from": int(from_block)})

    def status(self):
        """How it is going.  Answerable by a laptop that has just reconnected."""
        return self.call("job_status")

    def cancel(self):
        """Ask it to stop at its next SAFE point, which is never mid-erase."""
        return self.call("job_cancel")

    def fetch(self):
        """Collect what a finished read produced.  Returns the bytes."""
        _, blob = self.call_blob("job_fetch")
        return blob

    def wait(self, poll=1.0, timeout=3600.0, on_progress=None):
        """Poll until the job is over, and hand back the last status.

        Polling rather than a pushed notification: a status every second or two
        over a fifteen-minute job costs nothing, and the link may be absent for
        part of it -- which is the whole point of staging.
        """
        import time

        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            reply = self.status()
            if on_progress is not None and reply != last:
                on_progress(reply)
                last = reply
            if reply.get("status") in ("finished", "failed", "cancelled"):
                return reply
            time.sleep(poll)
        raise KLineTimeout("pass-through: the job did not finish in %.0fs" % timeout)


def _keyword(data):
    """ISO 14230 keyword from the two KW bytes: KW2031 is ``EF 8F``."""
    if len(data) < 2:
        return 0
    return ((data[1] & 0x7F) << 7) | (data[0] & 0x7F)


class RemoteKLine:
    """K-line look-alike (the subset edc16/edc15/diag use) over the pass-through.

    Built like :class:`tp20_transport.CanKLine` rather than like
    :class:`transport.KLine`, and for the same reason: written bytes are buffered
    until they make a whole frame, the frame goes out as ONE operation, and the
    reply is fed back into a byte stream the callers read as if it were a UART.
    A byte-at-a-time link over a network is not slow, it is unusable.

    Unlike ``CanKLine`` there is no reframing guesswork here.  The cable on the
    other end really is speaking K-line, so the whole frame crosses intact and
    the whole reply comes back intact -- including negative responses, pending
    responses and any trailing rubbish, because the parsing lives here and an
    implementation that decided what a reply meant would have to be kept in step
    with it.
    """

    is_tp20 = False

    def __init__(self, client, baud=10400, prefer_fast_init=True, target=0x10,
                 address=0x01, log=None):
        self.client = client
        self.baud = int(baud)
        self.prefer_fast_init = bool(prefer_fast_init)
        self.target = int(target)
        self.address = int(address)
        self._log = log or (lambda s: None)
        self._timeout = 8.0
        self._tx = bytearray()          # written but not yet a whole frame
        self._rx = bytearray()          # ready to be read back
        self.last_sync = 0x55

    # -- lifecycle --
    def wake_up(self, address=0x01, even_parity=False, tries=3):
        """Run the init on the head unit and return the keyword.

        One operation, not a sequence of remote steps.  Fast init anchors
        StartCommunication to Twup = 50 ms measured from the START of a 25 ms
        break pulse -- a drift to 66 ms killed every attempt on the serial
        reference -- and 5-baud is 200 ms a bit.  Neither survives a network hop
        in the middle, so neither has one.
        """
        self._tx.clear()
        self._rx.clear()

        if self.prefer_fast_init:
            reply = self.client.call("fast_init", target=self.target)
        else:
            reply = self.client.call("slow_init", address=address or self.address,
                                     baud=self.baud)
        return _keyword(bytes.fromhex(reply.get("keyword", "")))

    def stop_comm(self):
        """StopCommunication (0x82), for the fast-to-slow teardown before a write."""
        self.client.call("stop_comm")

    def close(self):
        self._tx.clear()
        self._rx.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- line state --
    @property
    def read_timeout(self):
        return self._timeout

    @read_timeout.setter
    def read_timeout(self, seconds):
        self._timeout = float(seconds)

    def set_baud(self, baud):
        self.baud = int(baud)
        self.client.call("set_baud", baud=self.baud)

    def set_break(self, on):
        """Nothing to do: the init that needs the break line runs on the head unit."""

    def clear_rx(self):
        self._rx.clear()
        self.client.call("clear_rx")

    # -- writes: buffer, flush whole frames --
    def write_raw(self, data):
        # Raw write: the caller expects the half-duplex echo to still be readable.
        self._feed(bytes(data), echo=True)

    def write_bytes(self, data):
        self._feed(bytes(data), echo=False)

    def write_byte(self, b):
        self._feed(bytes([b & 0xFF]), echo=False)

    def _feed(self, data, echo):
        self._tx += data
        while True:
            n = self._complete_frame_len(self._tx)
            if n is None:
                return
            frame = bytes(self._tx[:n])
            del self._tx[:n]
            self._exchange(frame, echo)

    @staticmethod
    def _complete_frame_len(buf):
        """Length of a complete K-line frame at the front of buf, or None."""
        if not buf:
            return None
        fmt = buf[0]
        i = 3 if fmt & 0x80 else 1
        n = fmt & 0x3F
        if n == 0:
            if len(buf) <= i:
                return None
            n = buf[i]
            i += 1
        total = i + n + 1               # + checksum
        return total if len(buf) >= total else None

    def _exchange(self, frame, echo):
        reply = self.client.call("xfer", data=frame.hex(),
                                 timeout=int(self._timeout * 1000))
        if echo:
            # The head unit drained the echo off the wire; put it back, because
            # the caller wrote raw precisely so that it could read it.
            self._rx += frame
        self._rx += bytes.fromhex(reply.get("data", ""))

    # -- reads: serve from the reassembled byte stream --
    def read_byte(self):
        if not self._rx:
            raise KLineTimeout("RemoteKLine: no data")
        return self._rx.pop(0)

    def read(self, n):
        if len(self._rx) < n:
            out = bytes(self._rx)
            self._rx.clear()
            raise KLineTimeout("RemoteKLine: short read (%d/%d)" % (len(out), n))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def read_complement(self, b):
        actual = self.read_byte()
        if actual != ((~b) & 0xFF):
            raise KLineError("RemoteKLine: complement mismatch")


class RemoteTp20:
    """A TP 2.0 channel that lives on the head unit.

    The same surface as :class:`j2534.BridgeTp20` -- ``open`` / ``send`` /
    ``recv`` / ``keepalive`` / ``close`` -- so ``CanKLine(tp=RemoteTp20(client))``
    needs no change anywhere.  That is the whole CAN path: the channel's
    acknowledgement timing never crosses the network, and the KWP payloads do.
    """

    def __init__(self, client, dest=0x01, rate=500000, log=None):
        self.client = client
        self.dest = int(dest)
        self.rate = int(rate)
        self._log = log or (lambda s: None)

    def open(self):
        self.client.call("tp_open", dest=self.dest, rate=self.rate)

    def send(self, payload):
        self.client.call("tp_send", data=bytes(payload).hex())

    def recv(self, timeout_ms=None):
        reply = self.client.call("tp_recv", timeout=int(timeout_ms or 5000))
        return bytes.fromhex(reply.get("data", ""))

    def keepalive(self):
        self.client.call("tp_keepalive")

    def close(self):
        self.client.call("tp_close")


__all__ = [
    "PROTOCOL_VERSION",
    "BridgeError",
    "BridgeClient",
    "RemoteKLine",
    "RemoteTp20",
]
