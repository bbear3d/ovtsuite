# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A stand-in for KW-Dash's pass-through, in Python, over a real socket.

Mirrors ``KWDash.Core/Bridge`` closely enough to drive ``remote.py`` end to end
without a head unit: the same 4-byte big-endian framing, the same op names, the
same error codes, the same PIN-then-HMAC pairing.  It is NOT the authority on
the protocol -- the C# is -- which is why the two are also run against each
other directly (``KWDash.Core.Tests --bridge-serve <port>`` and the real client).
This one exists so the Python suite stays self-contained and fast.

The cable underneath is a recorder, because most of what the client has to get
right is not the reply, it is whether a whole K-line frame left in one piece.
"""
import hashlib
import hmac
import json
import os
import socket
import struct
import threading

PROTOCOL_VERSION = 1


class FakeCable:
    """Records every call, answers the same way KWDash's FakeBridgeTarget does."""

    def __init__(self):
        self.calls = []
        self.reply = bytes([0x03, 0xF1, 0x10])
        self.fail_next = None           # (code, message) to raise once

    def __call__(self, op, request):
        self.calls.append((op, request))
        if self.fail_next is not None:
            code, message = self.fail_next
            self.fail_next = None
            raise _Refused(code, message)
        return self.reply


class _Refused(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


_CABLE_OPS = (
    "fast_init", "slow_init", "stop_comm", "set_speed", "set_baud", "xfer", "clear_rx",
    "tp_open", "tp_send", "tp_recv", "tp_keepalive", "tp_close",
)

_JOB_OPS = ("job_put", "job_start", "job_status", "job_cancel", "job_fetch")


class MockBridge:
    """Listens on loopback; one connection at a time is plenty for a test."""

    def __init__(self, cable=None, cable_name="ch34x", can=False, five_baud=True,
                 max_speed=0x87):
        self.cable = cable if cable is not None else FakeCable()
        self.cable_name = cable_name
        self.can = can
        self.five_baud = five_baud
        self.max_speed = max_speed
        #: The certificate this head unit would be offering, as the real one's
        #: ChannelBinding reports it. Empty is a link with no certificate -- which
        #: is what this plaintext mock really is, and what Bluetooth really is.
        #: A test that wants to reason about pinning sets it.
        self.fingerprint = b""
        self.pin = "%06d" % (int.from_bytes(os.urandom(4), "big") % 1000000)
        self.tokens = {}
        self.holder = None
        self.claims = []
        self.job = None
        self.job_image = b""
        self.job_result = b""
        self.job_status = "idle"
        self.job_done = 0
        self.job_total = 0
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one connection --
    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn):
        state = {"stage": "greeting", "client": "", "nonce": b""}
        try:
            while True:
                head = _read_exact(conn, 4)
                if head is None:
                    return
                body = _read_exact(conn, struct.unpack(">I", head)[0])
                if body is None:
                    return
                request = json.loads(body.decode("utf-8"))
                # The blob has no length of its own: the frame declared it, and the
                # bytes have to be read whether or not this end wanted them, or every
                # message after this one is read from the wrong offset.
                declared = int(request.get("blob", 0) or 0)
                blob = _read_exact(conn, declared) if declared else b""
                if declared and blob is None:
                    return
                reply = self._handle(state, request, blob or b"")
                out_blob = b""
                if reply.get("ok") and reply.get("blob"):
                    out_blob = self.job_result[:int(reply["blob"])]
                out = json.dumps(reply).encode("utf-8")
                conn.sendall(struct.pack(">I", len(out)) + out + out_blob)
        finally:
            if self.holder is state:
                self.holder = None
                self.claims.append("released")
            conn.close()

    def _job(self, op, request, blob):
        """Just enough of a runner to exercise the client: stage, run, report, hand back.

        The REAL validation and the real EDC16 state machines are the C# ones, and
        they are checked against this client directly (crosscheck_staged.py).  This
        is here so the Python suite stays self-contained and fast, and it checks the
        one thing that is this end's own job: that the descriptor and the image
        arrive intact and in the right relationship to each other.
        """
        if op == "job_put":
            blocks = request.get("blocks", [])
            regions = request.get("regions", [])
            needed = max([b["offset"] + b["length"] for b in blocks] or [0])
            if needed > len(blob):
                return _error("unsupported",
                              "the blocks need %d bytes and %d arrived" % (needed, len(blob)))
            self.job = request
            self.job_image = blob
            self.job_status = "staged"
            self.job_done = 0
            self.job_total = len(blocks) or len(regions)
            return self._job_reply(bytes=len(blob))

        if self.job is None:
            return _error("bad_state", "nothing is staged")

        if op == "job_start":
            self.job_done = int(request.get("from", 0))
            self.job_status = "running"
            # No thread: the fake finishes the moment it is asked to start, which is
            # what a client that polls has to cope with anyway.
            self.job_done = self.job_total
            self.job_status = "finished"
            if self.job.get("kind") == "read":
                total = sum(r["size"] for r in self.job.get("regions", []))
                self.job_result = bytes((i * 7) & 0xFF for i in range(total))
            return self._job_reply()

        if op == "job_cancel":
            self.job_status = "cancelled"
            return self._job_reply()

        if op == "job_fetch":
            if self.job_status != "finished":
                return _error("bad_state", "the job is %s" % self.job_status)
            return self._job_reply(blob=len(self.job_result))

        return self._job_reply()

    def _job_reply(self, **extra):
        reply = {
            "ok": True,
            "job": (self.job or {}).get("id", ""),
            "status": self.job_status,
            "stage": "",
            "done": self.job_done,
            "total": self.job_total,
        }
        reply.update(extra)
        return reply

    def _handle(self, state, request, blob=b""):
        op = request.get("op")

        if op == "hello":
            if request.get("version") != PROTOCOL_VERSION:
                return _error("version_mismatch", "wrong version", version=PROTOCOL_VERSION)
            state["client"] = request.get("client", "")
            state["nonce"] = os.urandom(16)
            state["stage"] = "unauthenticated"
            return {
                "ok": True,
                "version": PROTOCOL_VERSION,
                "nonce": state["nonce"].hex(),
                "paired": state["client"] in self.tokens,
                "cable": self.cable_name,
                "can": self.can,
                "five_baud": self.five_baud,
                "max_speed": self.max_speed,
            }

        if op == "auth":
            if state["stage"] == "greeting":
                return _error("bad_state", "say hello first")
            # Certificate fingerprint, then nonce -- for the PIN and for the token
            # alike. The token used to be proved over the nonce on its own, which
            # let a relay take a MAC and replay it; both proofs say which
            # certificate the client is looking at now.
            binding = self.fingerprint + state["nonce"]
            if "pin_proof" in request:
                want = hmac.new(
                    self.pin.encode("utf-8"), binding, hashlib.sha256).digest()
                if not hmac.compare_digest(
                        want, bytes.fromhex(request.get("pin_proof", ""))):
                    return _error("auth_failed", "wrong PIN")
                token = os.urandom(32)
                self.tokens[state["client"]] = token
                state["stage"] = "authenticated"
                return {"ok": True, "token": token.hex()}
            token = self.tokens.get(state["client"])
            want = hmac.new(token or b"\0" * 32, binding, hashlib.sha256).digest()
            if token is None or not hmac.compare_digest(
                    want, bytes.fromhex(request.get("mac", ""))):
                return _error("auth_failed", "wrong token")
            state["stage"] = "authenticated"
            return {"ok": True}

        if state["stage"] in ("greeting", "unauthenticated"):
            return _error("auth_failed", "%s needs an authenticated session" % op)

        if op == "claim":
            if self.holder is not None and self.holder is not state:
                return _error("busy", "another client is using the cable")
            if self.holder is not state:
                self.holder = state
                self.claims.append("claimed")
            state["stage"] = "claimed"
            return {"ok": True}

        if op == "release":
            if self.holder is state:
                self.holder = None
                self.claims.append("released")
            state["stage"] = "authenticated"
            return {"ok": True}

        if state["stage"] != "claimed":
            return _error("not_claimed", "%s needs the cable" % op)

        if op in _JOB_OPS:
            return self._job(op, request, blob)

        if op not in _CABLE_OPS:
            return _error("unknown_op", "no op named %r" % op)

        try:
            data = self.cable(op, request)
        except _Refused as refusal:
            return _error(refusal.code, refusal.message)

        if op in ("fast_init", "slow_init"):
            return {"ok": True, "keyword": "ef8f"}
        if op == "set_speed":
            return {"ok": True, "baud": 124800}
        if op in ("xfer", "tp_recv"):
            return {"ok": True, "data": bytes(data).hex()}
        return {"ok": True}


def _error(code, message, **extra):
    reply = {"ok": False, "code": code, "error": message}
    reply.update(extra)
    return reply


def _read_exact(conn, count):
    chunks = []
    have = 0
    while have < count:
        chunk = conn.recv(count - have)
        if not chunk:
            return None
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)
