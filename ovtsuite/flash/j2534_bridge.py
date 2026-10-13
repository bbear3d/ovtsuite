# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""32-bit helper process for 32-bit-only J2534 DLLs.

Most J2534 04.04 devices (the Scanmatik 2 PRO among them) ship only a 32-bit
DLL, and a 64-bit Python cannot load one in-process.  This script runs under
a 32-bit interpreter (or is frozen into ``j2534bridge32.exe``), loads the DLL
with the same ctypes wrapper the app uses, and services one request per
line on stdin, one reply per line on stdout (JSON, byte strings as hex).
:class:`ovtsuite.flash.j2534.BridgePassThru` is the client side and looks
exactly like :class:`ovtsuite.flash.j2534.PassThru`.

Usage: ``python j2534_bridge.py <dll path>``.  The first line written is the
ready banner ``{"ok": true, "r": {"bits": 32, "device_id": n}}``; a load or
open failure is reported the same way (``ok`` false) and the process exits.
The module is self-contained apart from ``j2534.py`` beside it, so both are
bundled as plain files into the frozen app and run under any 32-bit Python.
"""
import json
import os
import sys
import struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                                   # imported as part of the package
    from .. import branding
except ImportError:                                    # run as a plain script / frozen
    # This module is deliberately launchable as ``python j2534_bridge.py <dll>``
    # under a separate 32-bit interpreter, where there is no parent package and
    # a relative import cannot resolve. Load branding.py by path so the product
    # name still has exactly one source (see ovtsuite/branding.py).
    import importlib.util as _ilu
    _bp = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "branding.py")
    if os.path.exists(_bp):
        _spec = _ilu.spec_from_file_location("_branding", _bp)
        branding = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(branding)
    else:                                              # frozen: only this dir ships
        class branding:                                # noqa: N801  (namespace shim)
            APP_NAME = os.environ.get("OVT_APP_NAME", "")
try:
    import j2534 as J                                  # bundled copy beside this file
    import tp20_transport as TP
except ImportError:                                    # running from the source tree
    from ovtsuite.flash import j2534 as J
    from ovtsuite.flash import tp20_transport as TP

# Optional: the TRx (Aisin 09D) ECU-side TP 2.0 responder, run IN-BRIDGE so a
# read dump is one IPC per KWP message instead of ~40 per-frame round-trips.
# Imported defensively -- older/stripped bridge builds simply won't offer the
# tp_ecu_* ops, and the client detects that and falls back to its own loop.
try:
    import trx_sim as TRX                              # bundled copy beside this file
except ImportError:
    try:
        from ovtsuite.flash import trx_sim as TRX
    except Exception:
        TRX = None

# The tester-side ISO-TP engine, run IN-BRIDGE so a UDS calibration read is one
# IPC per chunk (send + reassemble + ride-out responsePending next to the DLL)
# instead of ~35 per-frame read round-trips -- the dominant cost over this
# bridge and the reason a 512 KB read crawled and stalled. Imported defensively;
# a stripped build simply won't offer the iso_* ops and the client falls back to
# the per-frame path.
try:
    import isotp_transport as IT                       # bundled copy beside this file
except ImportError:
    try:
        from ovtsuite.flash import isotp_transport as IT
    except Exception:
        IT = None


class _FrameTap:
    """Wraps a CAN channel and writes every frame to a capture file (the same
    format the Bus Sniffer / sniff.capture reads).  Runs inside the bridge so
    the debug log works even on the fast in-bridge TP path."""

    def __init__(self, ch, path):
        self._ch = ch
        self.protocol = getattr(ch, "protocol", None)
        self._fh = open(path, "w", encoding="utf-8", newline="\n")
        self._fh.write("# %s CAN debug trace (bridge)\n" % branding.APP_NAME)
        self._n = 0

    def _line(self, cid, data, tx, extended):
        import time as _t
        ident = ("%08X" % cid) if extended else ("%03X" % cid)
        self._fh.write("(%.6f) %s %s#%s\n" % (_t.time(), "can-tx" if tx else "can",
                                              ident, bytes(data).hex().upper()))
        self._n += 1
        if self._n % 50 == 0:
            self._fh.flush()

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        try:
            self._line(int(can_id), payload, True, extended)
        except Exception:
            pass
        return self._ch.write_can(can_id, payload, extended, timeout_ms)

    def write_can_many(self, can_id, payloads, extended=False, timeout_ms=100):
        payloads = list(payloads)
        for p in payloads:
            try:
                self._line(int(can_id), p, True, extended)
            except Exception:
                pass
        return self._ch.write_can_many(can_id, payloads, extended, timeout_ms)

    def read(self, max_msgs=16, timeout_ms=100):
        msgs = self._ch.read(max_msgs, timeout_ms)
        for m in msgs:
            try:
                self._line(int(m.can_id or 0), m.payload, bool(getattr(m, "is_tx_echo", False)),
                           bool(getattr(m, "tx_flags", 0) & 0x100))
            except Exception:
                pass
        return msgs

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._ch, name)


def _reply(out, ok, r=None, err=""):
    out.write((json.dumps({"ok": ok, "r": r, "err": err}) + "\n").encode("utf-8"))
    out.flush()


def _iso_request(io, payload, can_id=None, timeout_ms=2000, p2_star_ms=5000,
                 overall_ms=20000, fail_fast=True):
    """One UDS request over an in-bridge IsoTp: flush, send once, reassemble the
    response and ride out responsePending (0x78) -- the whole exchange next to
    the DLL, so the 64-bit client pays ONE IPC for the entire chunk. Mirrors
    ``Zf6hpUdsClient.request``. Returns the final response bytes, or None on a
    timeout/transport error (the client then re-sends, as it always has)."""
    import time as _t
    # Clean the bus before the request, the way the OEM tool does and the
    # client-side _flush_bus did per chunk -- in-bridge it costs no IPC.
    try:
        io.can.clear_rx()
    except Exception:
        pass
    io._pending = []
    io.send(payload, can_id=can_id)
    deadline = _t.monotonic() + overall_ms / 1000.0
    pending = False
    first = True
    while True:
        remaining = int((deadline - _t.monotonic()) * 1000)
        if remaining <= 0:
            return None
        window = min(timeout_ms if first else p2_star_ms, remaining)
        first = False
        try:
            resp = io.recv(window)
        except IT.KLineError:                # KLineTimeout is a subclass
            if pending or not fail_fast:
                continue                     # proven alive / long op -- keep waiting
            return None                      # silent bus -- let the client re-send
        if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:
            pending = True                   # responsePending -- ECU alive, working
            continue
        return resp


def _iso_request_keepalive(io, payload, keepalive_interval_s=1.0, resend_interval_s=3.5,
                           poll_ms=500, overall_ms=60000):
    """A WRITE request whose ECU may go SILENT while it works (a block erase),
    ridden out IN-BRIDGE: hold the session with testerPresent 3E80 and re-send
    the request until the ECU answers. Mirrors Zf6hpUdsClient.request_with_
    keepalive; one IPC for the whole erase. Returns the final response, or None
    on overall timeout."""
    import time as _t
    tp = bytes([0x3E, 0x80])                 # suppressPositiveResponse keepalive

    def _flush():
        try:
            io.can.clear_rx()
        except Exception:
            pass

    _flush()
    io._pending = []
    io.send(payload)
    now = _t.monotonic()
    deadline = now + overall_ms / 1000.0
    last_keepalive = now
    last_resend = now
    while True:
        remaining = deadline - _t.monotonic()
        if remaining <= 0:
            return None
        try:
            resp = io.recv(min(poll_ms, max(1, int(remaining * 1000))))
        except IT.KLineError:
            resp = b""
        if resp:
            if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:
                last_resend = _t.monotonic()   # alive & working -- defer resend
                continue
            return resp
        t = _t.monotonic()
        if t - last_keepalive >= keepalive_interval_s:
            try:
                io.send(tp)
            except Exception:
                pass
            last_keepalive = t
        if resend_interval_s and (t - last_resend >= resend_interval_s):
            _flush()
            io._pending = []
            io.send(payload)
            last_resend = t


def serve(dll_path: str, inp, out):
    try:
        dev = J.PassThru(dll_path)
    except Exception as ex:
        _reply(out, False, err=str(ex))
        return 1
    _reply(out, True, {"bits": struct.calcsize("P") * 8, "device_id": dev.device_id.value})
    chans = {}
    tp = {"chan": None, "tp": None, "tap": None}          # in-bridge TP 2.0 state
    ecu = {"chan": None, "srv": None,                     # in-bridge TRx ECU responder
           "drain_write": True, "held": False}            # continuous transferData drain
    uds = {"chan": None, "io": None, "tap": None,         # in-bridge UDS ISO-TP (6HP28 read)
           "func_id": 0x7DF}
    try:
        for line in inp:
            if not line.strip():
                continue
            try:
                q = json.loads(line.decode("utf-8"))
                op = q["op"]
                ch = chans.get(q.get("cid")) if "cid" in q else None
                if op == "tp_open":
                    # run the entire TP 2.0 channel inside the bridge: one IPC
                    # per KWP message instead of ~40 per-frame round trips
                    c = dev.connect(q["protocol"], q["baud"], q.get("flags", 0))
                    c.pass_all()
                    c.clear_rx()
                    tp["chan"] = c
                    if q.get("log_path"):
                        tp["tap"] = _FrameTap(c, q["log_path"])
                        c = tp["tap"]
                    p = q.get("params") or {}
                    params = TP.Tp20Params(**{k: p[k] for k in
                                              ("block_size", "t1_ms", "t3_ms", "ack_every",
                                               "ignore_ecu_floor", "pace_ecu",
                                               "tx_ack_every", "inter_send_ms") if k in p})
                    channel = TP.Tp20Channel(c, dest=q.get("dest", 0x01),
                                             listen_id=q.get("listen", 0x300), params=params)
                    channel.open()
                    tp["tp"] = channel
                    r = {"tx_id": channel.tx_id, "rx_id": channel.rx_id,
                         "ecu_block_size": channel.ecu_block_size,
                         "ecu_interframe_ms": channel.ecu_interframe_ms}
                elif op == "tp_reopen":
                    # re-run the TP 2.0 setup on the existing channel (the ECU
                    # resets its transport when it enters the programming session)
                    channel = tp["tp"]
                    channel.open()
                    r = {"tx_id": channel.tx_id, "rx_id": channel.rx_id,
                         "ecu_block_size": channel.ecu_block_size,
                         "ecu_interframe_ms": channel.ecu_interframe_ms}
                elif op == "tp_send":
                    tp["tp"].send(bytes.fromhex(q["payload"]))
                    r = None
                elif op == "tp_recv":
                    r = tp["tp"].recv(q.get("timeout", 2000)).hex()
                elif op == "tp_keepalive":
                    tp["tp"].keepalive()
                    r = None
                elif op == "tp_close":
                    try:
                        if tp["tp"] is not None:
                            tp["tp"].close()
                    finally:
                        if tp["tap"] is not None:
                            tp["tap"].close()
                        tp = {"chan": None, "tp": None, "tap": None}
                    r = None
                elif op == "tp_ecu_open":
                    # TRx ECU-side TP 2.0 responder, run in-bridge. The framing +
                    # block-ACK loop runs next to the DLL (per-frame DLL writes,
                    # no IPC per frame) so recv()/send() are one IPC per message.
                    if TRX is None:
                        raise J.J2534Error("trx_sim is not available in this bridge build")
                    c = dev.connect(q["protocol"], q["baud"], q.get("flags", 0))
                    c.pass_all()
                    c.clear_rx()
                    ecu["chan"] = c
                    a1 = bytes.fromhex(q["a1"]) if q.get("a1") else None
                    # rebuild the bench-timing model from the dict the 64-bit side
                    # serialized, so response latency / stalls / frame drops apply
                    # in-bridge next to the DLL (an unclean-bus simulation).
                    tmg = None
                    td = q.get("timing")
                    if td and hasattr(TRX, "TrxTiming"):
                        try:
                            tmg = TRX.TrxTiming.from_dict(td)
                        except Exception:
                            tmg = None
                    srv = TRX.Tp20Ecu(c, dest=q.get("dest"),
                                      ecu_rx_id=q.get("ecu_rx", 0x740), a1_params=a1,
                                      timing=tmg)
                    srv.trace_frames = False             # no per-frame trace over IPC
                    ecu["srv"] = srv
                    ecu["drain_write"] = bool(q.get("drain_write", True))
                    ecu["held"] = False
                    r = {"ecu_rx": q.get("ecu_rx", 0x740)}
                elif op == "tp_ecu_recv":
                    # A plain read serves one small request per block, so the
                    # single-message path is fine. A WRITE, though, streams ~37
                    # frames per transferData back-to-back: between the 64-bit
                    # side's `76` and its next recv() (two IPC hops) the bridge
                    # isn't draining CAN, and a no-ACK frame of the next block is
                    # lost -> the next message assembles from mid-data and NAKs
                    # (7F FF 11). So when a transferData (0x36) stream starts, we
                    # drain it CONTINUOUSLY in-bridge: ACK each block with 76 here
                    # (no round trip) and batch the payloads back for capture. The
                    # LAST block of a batch is left un-ACKed and "held" so the
                    # tester stays flow-controlled across the batch boundary (it
                    # waits for that 76) and never streams into a gap; the next
                    # call ACKs it first. 0x36 in the capture sim is always
                    # accepted (bare 76), which is what makes the in-bridge ACK
                    # safe; anything else returns as a normal single message.
                    srv = ecu["srv"]
                    if srv is None:
                        r = {"payload": None, "frames_seen": 0}
                    else:
                        if ecu.get("held"):
                            srv.send(b"\x76")            # release the held block
                            ecu["held"] = False
                        pl = srv.recv(q.get("timeout", 100))
                        if pl is None:
                            r = {"payload": None,
                                 "frames_seen": getattr(srv, "frames_seen", 0)}
                        elif ecu.get("drain_write") and pl and pl[0] == 0x36 \
                                and hasattr(TRX, "drain_transfer_batch"):
                            blocks, tail, held = TRX.drain_transfer_batch(
                                srv, pl, cap=64, idle_ms=1500)
                            ecu["held"] = held
                            r = {"batch": blocks, "tail": tail,
                                 "frames_seen": getattr(srv, "frames_seen", 0)}
                        else:
                            r = {"payload": pl.hex(),
                                 "frames_seen": getattr(srv, "frames_seen", 0)}
                elif op == "tp_ecu_send":
                    ecu["srv"].send(bytes.fromhex(q["payload"]))
                    r = None
                elif op == "tp_ecu_close":
                    try:
                        if ecu["chan"] is not None:
                            ecu["chan"].disconnect()
                    finally:
                        ecu = {"chan": None, "srv": None,
                               "drain_write": True, "held": False}
                    r = None
                elif op == "iso_open":
                    # Open a raw CAN channel and wrap it in the tester-side
                    # ISO-TP engine IN-BRIDGE. All reassembly + flow control +
                    # responsePending now runs next to the DLL; the client drives
                    # it one IPC per UDS request via iso_request / iso_functional.
                    if IT is None:
                        raise J.J2534Error("isotp_transport is not available in this bridge build")
                    c = dev.connect(q["protocol"], q["baud"], q.get("flags", 0))
                    c.pass_all()
                    c.clear_rx()
                    uds["chan"] = c
                    src = c
                    if q.get("log_path"):
                        uds["tap"] = _FrameTap(c, q["log_path"])
                        src = uds["tap"]
                    uds["io"] = IT.IsoTp(src, tx_id=q.get("tx_id", 0x7E1),
                                         rx_id=q.get("rx_id", 0x7E9))
                    uds["func_id"] = q.get("func_id", 0x7DF)
                    r = {"tx_id": uds["io"].tx_id, "rx_id": uds["io"].rx_id}
                elif op == "iso_request":
                    if uds["io"] is None:
                        raise J.J2534Error("iso channel not open")
                    resp = _iso_request(
                        uds["io"], bytes.fromhex(q["payload"]), can_id=q.get("can_id"),
                        timeout_ms=q.get("timeout_ms", 2000),
                        p2_star_ms=q.get("p2_star_ms", 5000),
                        overall_ms=q.get("overall_ms", 20000),
                        fail_fast=q.get("fail_fast", True))
                    r = {"resp": resp.hex() if resp is not None else None}
                elif op == "iso_request_keepalive":
                    if uds["io"] is None:
                        raise J.J2534Error("iso channel not open")
                    resp = _iso_request_keepalive(
                        uds["io"], bytes.fromhex(q["payload"]),
                        keepalive_interval_s=q.get("keepalive_interval_s", 1.0),
                        resend_interval_s=q.get("resend_interval_s", 3.5),
                        poll_ms=q.get("poll_ms", 500), overall_ms=q.get("overall_ms", 60000))
                    r = {"resp": resp.hex() if resp is not None else None}
                elif op == "iso_functional":
                    if uds["io"] is None:
                        raise J.J2534Error("iso channel not open")
                    res = uds["io"].functional_scan(
                        bytes.fromhex(q["request"]), listen_ms=q.get("listen_ms", 500),
                        func_id=q.get("func_id", uds.get("func_id", 0x7DF)))
                    r = {("%X" % cid): data.hex() for cid, data in res.items()}
                elif op == "iso_flush":
                    if uds["io"] is not None:
                        uds["io"]._pending = []
                        for fn in ("clear_rx", "clear_tx"):
                            f = getattr(uds["chan"], fn, None)
                            if f is not None:
                                try:
                                    f()
                                except Exception:
                                    pass
                    r = None
                elif op == "iso_close":
                    try:
                        if uds["chan"] is not None:
                            uds["chan"].disconnect()
                    finally:
                        if uds.get("tap") is not None:
                            uds["tap"].close()
                        uds = {"chan": None, "io": None, "tap": None, "func_id": 0x7DF}
                    r = None
                elif op == "connect":
                    c = dev.connect(q["protocol"], q["baud"], q.get("flags", 0))
                    chans[c.id] = c
                    r = c.id
                elif op == "disconnect":
                    ch.disconnect()
                    chans.pop(q["cid"], None)
                    r = None
                elif op == "add_filter":
                    r = ch.add_filter(q["ftype"], bytes.fromhex(q["mask"]), bytes.fromhex(q["pattern"]),
                                      q.get("flags", 0),
                                      bytes.fromhex(q["flow"]) if q.get("flow") is not None else None)
                elif op == "clear_filters":
                    ch.clear_filters()
                    r = None
                elif op == "read":
                    r = [[m.protocol, m.rx_status, m.tx_flags, m.timestamp, m.data.hex()]
                         for m in ch.read(q.get("max", 32), q.get("timeout", 100))]
                elif op == "write":
                    ch.write(bytes.fromhex(q["data"]), q.get("flags", 0), q.get("timeout", 100))
                    r = None
                elif op == "set_config":
                    ch.set_config(**q["params"])
                    r = None
                elif op == "set_pwm":
                    ch.set_pwm(q["items"], q.get("ioctl_id", 0x8008))
                    r = None
                elif op == "clear_rx":
                    ch.clear_rx()
                    r = None
                elif op == "clear_tx":
                    ch.clear_tx()
                    r = None
                elif op == "five_baud_init":
                    r = ch.five_baud_init(q["address"]).hex()
                elif op == "fast_init":
                    r = ch.fast_init(bytes.fromhex(q["request"])).hex()
                elif op == "battery_voltage":
                    r = dev.battery_voltage()
                elif op == "close":
                    _reply(out, True)
                    break
                else:
                    raise J.J2534Error("unknown op %r" % op)
                _reply(out, True, r)
            except Exception as ex:                    # noqa: BLE001  -- report, keep serving
                _reply(out, False, err=str(ex))
    finally:
        try:
            dev.close()
        except Exception:
            pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: j2534_bridge.py <dll path>\n")
        return 2
    return serve(argv[0], sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
