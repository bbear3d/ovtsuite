# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""32-bit J2534 bridge: server + client over real pipes with a fake device."""
import os
import subprocess
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash import j2534_bridge as B  # noqa: E402


class FakeChannel:
    def __init__(self, cid, protocol):
        self.id, self.protocol, self.log = cid, protocol, []
        self.queue = []

    def add_filter(self, ftype, mask, pattern, flags=0, flow=None):
        self.log.append(("filter", ftype, mask, pattern, flags, flow))
        return 7

    def clear_filters(self):
        self.log.append(("clear_filters",))

    def read(self, max_msgs=32, timeout_ms=100):
        out, self.queue = self.queue[:max_msgs], self.queue[max_msgs:]
        return out

    def write(self, data, flags=0, timeout_ms=100):
        self.log.append(("write", data, flags))
        self.queue.append(J.Msg(self.protocol, J.TX_MSG_TYPE, flags, 5, data))

    def set_config(self, **params):
        self.log.append(("config", params))

    def set_pwm(self, items, ioctl_id=0x8008):
        self.log.append(("pwm", ioctl_id, items))

    def clear_rx(self):
        self.queue = []

    def clear_tx(self):
        pass

    def five_baud_init(self, address):
        return bytes([0x8F, 0xE9])

    def fast_init(self, request):
        if request[0] != 0x81:
            raise J.J2534Error("PassThruIoctl 0x05: ERR_TIMEOUT (0x09)")
        return bytes.fromhex("83F110C18FE9")

    def disconnect(self):
        self.log.append(("disconnect",))
        self.id = None


class FakePassThru:
    instances = []

    def __init__(self, dll):
        if dll.endswith("missing.dll"):
            raise J.J2534Error("cannot load %s" % dll)
        self.dll = dll
        self.device_id = J.c_uint32(3)
        self.channels = []
        self.closed = False
        FakePassThru.instances.append(self)

    def connect(self, protocol, baud, flags=0):
        ch = FakeChannel(100 + len(self.channels), protocol)
        ch.baud, ch.flags = baud, flags
        self.channels.append(ch)
        return ch

    def battery_voltage(self):
        return 12.6

    def close(self):
        self.closed = True


class PipeProc:
    """Popen look-alike whose other end is the bridge server in a thread."""

    def __init__(self, dll):
        r1, w1 = os.pipe()          # client -> server
        r2, w2 = os.pipe()          # server -> client
        self.stdin = os.fdopen(w1, "wb")
        self.stdout = os.fdopen(r2, "rb")
        self.stderr = open(os.devnull, "rb")
        srv_in, srv_out = os.fdopen(r1, "rb"), os.fdopen(w2, "wb")
        self.rc = None

        def run():
            try:
                self.rc = B.serve(dll, srv_in, srv_out)
            finally:
                srv_out.close()
                srv_in.close()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, timeout=None):
        self.thread.join(timeout)
        return self.rc

    def kill(self):
        pass


def test_bridge_roundtrip(monkeypatch):
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    assert dev.device_id == 3
    ch = dev.connect(J.CAN, 500000, J.CAN_ID_BOTH)
    assert ch.id == 100 and ch.protocol == J.CAN
    real = FakePassThru.instances[-1].channels[0]
    assert (real.baud, real.flags) == (500000, J.CAN_ID_BOTH)
    ch.pass_all()
    assert real.log[-1] == ("filter", J.PASS_FILTER, bytes(4), bytes(4), 0, None) and ch._filters == [7]
    ch.set_config(LOOPBACK=1, DATA_RATE=250000)
    assert real.log[-1] == ("config", {"LOOPBACK": 1, "DATA_RATE": 250000})
    ch.write_can(0x200, bytes.fromhex("01C00010000301"))
    msgs = ch.read(8, 10)
    assert len(msgs) == 1 and msgs[0].can_id == 0x200 and msgs[0].is_tx_echo
    assert msgs[0].payload == bytes.fromhex("01C00010000301") and msgs[0].timestamp == 5
    assert ch.read() == []
    assert dev.battery_voltage() == 12.6
    k = dev.connect(J.ISO14230, 10400)
    assert k.five_baud_init(0x10) == bytes([0x8F, 0xE9])
    assert k.fast_init(bytes.fromhex("8110F181")) == bytes.fromhex("83F110C18FE9")
    try:
        k.fast_init(bytes.fromhex("C110F181"))
        assert False
    except J.J2534Error as ex:
        assert "ERR_TIMEOUT" in str(ex)             # error crossed the pipe, bridge kept serving
    ch.clear_filters()
    assert real.log[-1] == ("clear_filters",)
    dev.close()
    assert dev.proc is None and ch.id is None and FakePassThru.instances[-1].closed
    assert k.id is None


def test_bridge_kline_exchange_is_one_round_trip(monkeypatch):
    # a K-line request and its whole reply -- here split over two messages,
    # behind the device's TX echo -- come back from one bridge call
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    k = dev.connect(J.ISO9141, 124800)
    real = FakePassThru.instances[-1].channels[0]
    real.queue = [J.Msg(J.ISO9141, 0, 0, 1, bytes.fromhex("0001")),
                  J.Msg(J.ISO9141, 0, 0, 2, bytes.fromhex("7677"))]
    calls = []
    orig = dev._call
    monkeypatch.setattr(dev, "_call", lambda op, **kw: calls.append(op) or orig(op, **kw))
    msgs = k.kline_exchange(bytes.fromhex("0002367F"), 1000, 500)
    assert calls == ["kline_exchange"]
    assert real.log[-1] == ("write", bytes.fromhex("0002367F"), 0)
    assert b"".join(m.data for m in msgs) == bytes.fromhex("00017677")
    dev.close()


def test_bridge_kline_exchange_falls_back_on_an_older_bridge(monkeypatch):
    # a bridge built before kline_exchange answers "unknown op": the exchange
    # is then done with write/read calls, and not asked for again
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    k = dev.connect(J.ISO9141, 124800)
    real = FakePassThru.instances[-1].channels[0]
    calls = []
    orig = dev._call

    def old_bridge(op, **kw):
        calls.append(op)
        if op == "kline_exchange":
            raise J.J2534Error("unknown op 'kline_exchange'")
        return orig(op, **kw)
    monkeypatch.setattr(dev, "_call", old_bridge)
    for _ in range(2):
        real.queue = [J.Msg(J.ISO9141, 0, 0, 1, bytes.fromhex("00017677"))]
        msgs = k.kline_exchange(bytes.fromhex("0002367FB7"), 1000, 500)
        assert b"".join(m.data for m in msgs) == bytes.fromhex("00017677")
    assert calls.count("kline_exchange") == 1
    assert real.log[-1] == ("write", bytes.fromhex("0002367FB7"), 0) or \
        ("write", bytes.fromhex("0002367FB7"), 0) in real.log
    dev.close()


def test_bridge_kline_exchange_finds_the_reply_behind_a_stray_byte(monkeypatch):
    # the bridge's own wait ends at the reply, not at the timeout, when a
    # stray byte comes first -- judged against the request it is answering
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    k = dev.connect(J.ISO9141, 124800)
    real = FakePassThru.instances[-1].channels[0]
    real.queue = [J.Msg(J.ISO9141, 0, 0, 1, bytes.fromhex("FF")),
                  J.Msg(J.ISO9141, 0, 0, 2, bytes.fromhex("83F1106703AA")),
                  J.Msg(J.ISO9141, 0, 0, 3, bytes.fromhex("98"))]
    import time as _t
    t0 = _t.monotonic()
    msgs = k.kline_exchange(None, 1000, 3000, expect=bytes.fromhex("8210F12703AD"))
    assert _t.monotonic() - t0 < 2.0
    assert b"".join(m.data for m in msgs) == bytes.fromhex("FF83F1106703AA98")
    dev.close()


def test_bridge_kline_xfer_is_one_round_trip(monkeypatch):
    # the byte-stream link's step: what is waiting, the write, the reply
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    k = dev.connect(J.ISO9141, 9600)
    real = FakePassThru.instances[-1].channels[0]
    real.queue = [J.Msg(J.ISO9141, 0, 0, 1, b"\x0F")]
    orig_write = real.write

    def write(data, flags=0, timeout_ms=100):
        orig_write(data, flags, timeout_ms)                 # TX echo, then the complement
        real.queue.append(J.Msg(J.ISO9141, 0, 0, 6, bytes([(~data[0]) & 0xFF])))
    real.write = write
    calls = []
    orig = dev._call
    monkeypatch.setattr(dev, "_call", lambda op, **kw: calls.append(op) or orig(op, **kw))
    before, after = k.kline_xfer(b"\x12", 1000, 50, True)
    assert calls == ["kline_xfer"]
    assert [m.data for m in before] == [b"\x0F"] and [m.data for m in after] == [b"\xED"]
    dev.close()


def test_bridge_kline_xfer_falls_back_on_an_older_bridge(monkeypatch):
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    k = dev.connect(J.ISO9141, 9600)
    real = FakePassThru.instances[-1].channels[0]
    calls = []
    orig = dev._call

    def old_bridge(op, **kw):
        calls.append(op)
        if op == "kline_xfer":
            raise J.J2534Error("unknown op 'kline_xfer'")
        return orig(op, **kw)
    monkeypatch.setattr(dev, "_call", old_bridge)
    for b in (b"\x12", b"\x34"):
        real.queue = [J.Msg(J.ISO9141, 0, 0, 1, b"\x0F")]
        before, after = k.kline_xfer(b, 1000, 10, True)
        assert [m.data for m in before] == [b"\x0F"] and after == []   # echo left out
        assert ("write", b, 0) in real.log
    assert calls.count("kline_xfer") == 1 and "write" in calls and "read" in calls
    dev.close()


def test_bridge_set_pwm(monkeypatch):
    """The GPT crank/cam vendor ioctl (Scanmatik SM2, id 0x8008 by default)
    round-trips over the 32-bit bridge: item pairs, an overridden ioctl_id,
    and BridgeChannel's own uint32 value-masking all have to cross the pipe
    intact, since the real target DLL is the only thing that ever sees
    them again (see ovtsuite/flash/sm2_gpt.py)."""
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    dev = J.BridgePassThru("fake.dll", proc=PipeProc("fake.dll"))
    ch = dev.connect(J.CAN, 500000)
    real = FakePassThru.instances[-1].channels[-1]

    ch.set_pwm([(0xC001, 1), (0xC002, 4_000_000), (0xC004, 1)])
    assert real.log[-1] == ("pwm", 0x8008, [[0xC001, 1], [0xC002, 4_000_000], [0xC004, 1]])

    # a non-default vendor ioctl id (e.g. SM_VENDOR_800C) and a value that
    # needs uint32 masking both have to survive the JSON round trip.
    ch.set_pwm([(0xC00A, -1)], ioctl_id=0x800C)
    assert real.log[-1] == ("pwm", 0x800C, [[0xC00A, 0xFFFFFFFF]])
    dev.close()


def test_bridge_load_failure(monkeypatch):
    monkeypatch.setattr(B.J, "PassThru", FakePassThru)
    try:
        J.BridgePassThru("missing.dll", proc=PipeProc("missing.dll"))
        assert False
    except J.J2534Error as ex:
        assert "cannot load" in str(ex)


def test_bridge_script_runs_as_process():
    """The real script under this interpreter: bad DLL -> ok:false banner."""
    script = os.path.join(os.path.dirname(__file__), "..", "ovtsuite", "flash", "j2534_bridge.py")
    p = subprocess.run([sys.executable, script, "/nonexistent/j2534.dll"], capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 1 and '"ok": false' in p.stdout and "cannot load" in p.stdout


def test_open_device_choices(monkeypatch):
    me = 64 if sys.maxsize > 2 ** 32 else 32
    d = J.Device("Scanmatik 2 PRO", "Scanmatik", "C:\\x\\sm2_j2534.dll", 32)
    monkeypatch.setattr(J, "_runner_cache", [])          # no 32-bit runner
    assert d.loadable == (me == 32)
    if me == 64:
        assert not J.usable(d)
        try:
            J.open_device(d)
            assert False
        except J.J2534Error as ex:
            assert "32-bit" in str(ex)
        monkeypatch.setattr(J, "_runner_cache", ["py", "-3-32", "bridge.py"])
        assert J.usable(d)


_CHATTY = r"""
import sys
for i in range(4000):                 # ~1 MB: far past any OS pipe buffer
    sys.stderr.write("dll chatter %05d %s\n" % (i, "y" * 240))
sys.stderr.flush()
sys.stdout.write('{"ok": true, "r": {"device_id": 9}}\n')
sys.stdout.flush()
sys.stdin.readline()
sys.stderr.write("bridge died: boom\n")
sys.stderr.flush()
"""

_HANG = r"""
import sys, time
sys.stdout.write('{"ok": true, "r": {"device_id": 1}}\n')
sys.stdout.flush()
time.sleep(60)
"""


def test_bridge_stderr_is_drained_and_kept_for_the_error(tmp_path):
    """A bridge that writes more to stderr than the pipe holds used to block
    there while this side sat in readline() on its stdout: a hang with no
    error. The drain keeps the tail for the message when the bridge dies."""
    script = tmp_path / "chatty.py"
    script.write_text(_CHATTY)
    got = {}

    def run():
        try:
            dev = J.BridgePassThru("x.dll", runner=[sys.executable, str(script)])
            got["dev"] = dev
            dev._call("battery_voltage")
        except J.J2534Error as ex:
            got["err"] = str(ex)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "the bridge blocked on a full stderr pipe"
    assert got["dev"].device_id == 9
    assert "bridge died: boom" in got["err"]
    assert len(got["dev"]._stderr) <= J.STDERR_KEEP


def test_bridge_kill_unblocks_a_hung_call(tmp_path):
    """The logger's last resort on Disconnect: a call into a DLL that never
    returns is ended by killing the bridge, which the blocked reader sees as
    EOF and an error -- the worker unwinds instead of hanging."""
    script = tmp_path / "hang.py"
    script.write_text(_HANG)
    dev = J.BridgePassThru("x.dll", runner=[sys.executable, str(script)])
    got = {}

    def run():
        try:
            dev._call("battery_voltage")
        except J.J2534Error as ex:
            got["err"] = str(ex)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(0.3)
    assert t.is_alive()
    dev.kill()
    t.join(10)
    assert not t.is_alive() and "exited" in got["err"]
