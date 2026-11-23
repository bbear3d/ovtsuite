# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TP 2.0 / KWP decoding, capture files, J2534 structures."""
import ctypes
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.sniff import tp20 as T  # noqa: E402
from ovtsuite.sniff import capture as C  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402


def _frames():
    """A VCDS-style session: setup, params, 1A 9B request, multi-frame reply."""
    t = 0.0
    fr = []

    def add(cid, hexs, tx=False):
        nonlocal t
        t += 0.002
        fr.append(T.Frame(t, cid, bytes.fromhex(hexs.replace(" ", "")), tx))
    add(0x200, "01 C0 00 10 00 03 01")           # tester: listen on 0x300
    add(0x201, "00 D0 00 03 40 07 01")           # ECU: sends on 0x300, listens on 0x740
    add(0x740, "A0 0F 8A FF 32 FF")              # params request
    add(0x300, "A1 0F 8A FF 4A FF")              # params response
    add(0x740, "10 00 02 1A 9B")                 # last frame, ack expected: len 2, 1A 9B
    add(0x300, "B1")                             # ack, next seq 1
    add(0x300, "20 00 13 5A 9B 30 33 38")        # more, no ack: len 0x13 = 19
    add(0x300, "21 39 30 36 30 31 36 41")
    add(0x300, "12 42 20 20 34 39 39 39")        # last, ack expected (19 bytes total)
    add(0x740, "B3")
    add(0x740, "A3")                             # keep-alive
    add(0x300, "A1 0F 8A FF 4A FF")
    add(0x740, "A8")                             # disconnect
    add(0x7E8, "02 41 0C")                       # unrelated frame
    return fr


def test_tp20_decode_session():
    dec = T.Tp20Decoder()
    evs = []
    for f in _frames():
        evs += dec.feed(f)
    kinds = [e.kind for e in evs]
    assert kinds[0] == "setup" and "0x300" in evs[0].text
    assert kinds[1] == "setup-ack" and "0x740" in evs[1].text and "0x300" in evs[1].text
    assert kinds[2] == "param" and evs[2].channel == "T->E" and "BS 15" in evs[2].text
    assert kinds[3] == "param" and evs[3].channel == "E->T"
    msgs = [e for e in evs if e.kind == "message"]
    assert len(msgs) == 2
    assert msgs[0].payload == bytes.fromhex("1A9B") and msgs[0].channel == "T->E"
    assert msgs[0].text.startswith("readEcuIdentification 9b")
    assert msgs[1].channel == "E->T" and len(msgs[1].payload) == 19
    assert msgs[1].payload[:2] == bytes.fromhex("5A9B")
    assert msgs[1].payload[2:].decode() == "038906016AB  4999"
    assert msgs[1].text.startswith("readEcuIdentification + 9b")
    acks = [e for e in evs if e.kind == "ack"]
    assert len(acks) == 2 and "next seq 1" in acks[0].text
    assert any(e.kind == "param" and "disconnect" in e.text for e in evs)
    assert evs[-1].kind == "raw"                   # 0x7E8 is not a TP channel
    assert not dec.channels                        # closed by A8


def test_tp20_timing_and_refusal():
    assert T._tval(0x8A) == "100 ms"          # 10 ms units x 10 (VCDS T1)
    assert T._tval(0x32) == "5 ms"
    dec = T.Tp20Decoder()
    dec.feed(T.Frame(0.0, 0x200, bytes.fromhex("01C00010000301")))
    ev = dec.feed(T.Frame(0.1, 0x201, bytes.fromhex("00D60003400701")))[0]
    assert ev.kind == "setup-ack" and "refused" in ev.text and not dec.channels


def test_kwp_descriptions():
    assert T.describe_kwp(bytes.fromhex("1089")) == "startDiagnosticSession 89"
    assert T.describe_kwp(bytes.fromhex("5089")) == "startDiagnosticSession + 89"
    assert T.describe_kwp(bytes.fromhex("7F2178")) == "NAK readDataByLocalId: responsePending"
    assert T.describe_kwp(bytes.fromhex("3E")) == "testerPresent"
    d, txt = T.decode_kline(bytes.fromhex("8110F13E"))
    assert d == "T->E" and txt == "testerPresent"
    d, txt = T.decode_kline(bytes.fromhex("83F1105089"))
    assert d == "E->T" and txt.startswith("startDiagnosticSession +")
    d, txt = T.decode_kline(bytes.fromhex("80F11003610100"))       # length byte form
    assert d == "E->T" and txt.startswith("readDataByLocalId + 01")


def test_capture_roundtrip_and_candump():
    recs = [C.Record(1757160000.123456, "can", 0x200, bytes.fromhex("01C00010000301")),
            C.Record(1757160000.124, "can", 0x18DAF110, b"\x02\x01\x0c", tx=True, extended=True),
            C.Record(1757160001.5, "kline", 0, bytes.fromhex("8110F13E"))]
    d = tempfile.mkdtemp()
    p = os.path.join(d, "cap.log")
    C.write(p, recs, "test capture")
    lines = open(p).read().splitlines()
    assert lines[0] == "# test capture"
    assert lines[1] == "(1757160000.123456) can 200#01C00010000301"
    assert lines[2] == "(1757160000.124000) can-tx 18DAF110#02010C"
    assert lines[3] == "(1757160001.500000) kline 0#8110F13E"
    back = C.read(p)
    assert [(r.bus, r.can_id, r.data, r.tx, r.extended) for r in back] == \
        [(r.bus, r.can_id, r.data, r.tx, r.extended) for r in recs]
    r = C.parse_line("(1699999999.000100) can0 7E8#0641 0C1AF8")
    assert r is None                                  # candump data has no spaces
    r = C.parse_line("(1699999999.000100) can0 7E8#06410C1AF8")
    assert r.bus == "can" and r.can_id == 0x7E8 and r.data == bytes.fromhex("06410C1AF8")


def test_j2534_structs_and_msg():
    assert ctypes.sizeof(J.PASSTHRU_MSG) == 6 * 4 + 4128   # ULONG is 32-bit in J2534
    m = J.PassThru._msg(J.CAN, (0x200).to_bytes(4, "big") + b"\x01\xc0", 0)
    assert m.ProtocolID == J.CAN and m.DataSize == 6
    msg = J.Msg(J.CAN, 0, 0, 1000, bytes(m.Data[:m.DataSize]))
    assert msg.can_id == 0x200 and msg.payload == b"\x01\xc0" and not msg.is_tx_echo
    assert J.Msg(J.ISO14230, J.TX_MSG_TYPE, 0, 0, b"\x81\x10\xf1\x3e").is_tx_echo
    assert J.list_devices() == [] or os.name == "nt"


# ------------------------------------------------------------- K-line tap
from ovtsuite.sniff import kline as KL  # noqa: E402


def _cks(h):
    b = bytes.fromhex(h.replace(" ", ""))
    return b + bytes([sum(b) & 0xFF])


def test_kline_framer_kwp_and_speed_follow():
    """Bytes as a passive FTDI tap sees them, one burst per read."""
    bursts = [(0.00, bytes.fromhex("55")), (0.02, bytes.fromhex("EF")), (0.03, bytes.fromhex("8F")),
              (0.05, bytes.fromhex("70")), (0.08, bytes.fromhex("FE")),
              (0.16, bytes.fromhex("8210F11089")), (0.18, bytes.fromhex("1C")),      # 10 89 + cks
              (0.20, bytes.fromhex("82F1")), (0.21, bytes.fromhex("1050895C")),
              (0.30, _cks("83 10 F1 10 86 87")), (0.31, _cks("83 F1 10 50 86 87")),
              (0.40, bytes.fromhex("80F11003610100") + bytes([(0x80 + 0xF1 + 0x10 + 3 + 0x61 + 1) & 0xFF]))]
    f = KL.Framer()
    out = []
    for t, b in bursts:
        out += f.feed(t, b)
    kinds = [x.kind for x in out]
    assert kinds == ["init", "kwp", "kwp", "kwp", "kwp", "kwp"]
    assert "2031" in out[0].note and out[0].data == bytes.fromhex("55EF8F70FE")
    assert out[1].data == bytes.fromhex("8210F11089")            # checksum stripped
    assert f.new_baud == 124800 and "124800" in out[4].note
    assert out[5].data == bytes.fromhex("80F11003610100")        # explicit length byte form
    assert KL.speed_byte_to_baud(0x50) == 38400 and KL.speed_byte_to_baud(0xA7) == 249600


def test_kline_framer_resync_and_kw1281():
    f = KL.Framer()
    junk = bytes.fromhex("FC")
    out = f.feed(0.0, junk)
    assert out == []                                  # could still be a frame start
    out = f.feed(0.5, _cks("81 10 F1 3E"))
    assert [x.kind for x in out] == ["raw", "kwp"]   # the junk flushed after the gap
    assert out[1].data == bytes.fromhex("8110F13E")
    blk = b""
    for x in (0x05, 0x01, 0xF6, 0x41, 0x42):
        blk += bytes([x, (~x) & 0xFF])
    blk += b"\x03"
    f2 = KL.Framer()
    out = f2.feed(0.0, bytes.fromhex("55018A75") + blk)
    assert [x.kind for x in out] == ["init", "kw1281"]
    assert out[1].data == bytes.fromhex("0501F64142") and "KW1281" in out[0].note


def test_capture_reads_kwhack_kline_sniffer_log():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "mpps.log")
    with open(p, "w") as fh:
        fh.write("=== kline_sniffer.py capture started: port=COM4 baud=10400 mode=fixed-baud ===\n"
                 "[t=31428.695716]    5 bytes: 82 10 F1 10 89\n"
                 "[t=31428.712195]    1 bytes: 1C\n"
                 "=== BAUD CHANGED (negotiated): 10400 -> 38400 (startDiagnosticSession session=0x85 speed=0x50) ===\n")
    recs = C.read(p)
    assert [(r.bus, r.data.hex()) for r in recs] == [("kraw", "8210f11089"), ("kraw", "1c")]
    f = KL.Framer()
    out = []
    for r in recs:
        out += f.feed(r.t, r.data)
    assert len(out) == 1 and out[0].kind == "kwp"
    # a kraw record survives the native capture format
    p2 = os.path.join(d, "native.log")
    C.write(p2, recs)
    assert [r.bus for r in C.read(p2)] == ["kraw", "kraw"]


# ------------------------------------------------- several sources at once
from ovtsuite.sniff import merge as M  # noqa: E402


def test_capture_source_tag_roundtrip_and_backward_compat():
    recs = [C.Record(1757160000.5, "can", 0x200, b"\x01\xc0", src="A"),
            C.Record(1757160000.6, "kline", 0, bytes.fromhex("8110F13E"), tx=True, src="B"),
            C.Record(1757160000.7, "kraw", 0, b"\x82\x10", src="B"),
            C.Record(1757160000.8, "can", 0x300, b"\xa1")]               # untagged
    d = tempfile.mkdtemp()
    p = os.path.join(d, "multi.log")
    C.write(p, recs, "two sources")
    lines = [ln for ln in open(p).read().splitlines() if not ln.startswith("#")]
    assert lines[0] == "(1757160000.500000) can 200#01C0 @A"
    assert lines[1] == "(1757160000.600000) kline-tx 0#8110F13E @B"
    assert lines[3] == "(1757160000.800000) can 300#A1"                   # no tag written
    back = C.read(p)
    assert [(r.src, r.bus, r.tx, r.data) for r in back] == [(r.src, r.bus, r.tx, r.data) for r in recs]
    # an old log, a plain candump line and a KWHack burst line carry no source
    assert C.parse_line("(1757160000.123456) can 200#01C00010000301").src == ""
    assert C.parse_line("(1699999999.000100) can0 7E8#06410C1AF8").src == ""
    assert C.parse_line("[t=1.5]    2 bytes: 82 10").src == ""
    w = C.Writer(os.path.join(d, "w.log"), "h")
    w.add(recs[1])
    w.close()
    assert C.read(os.path.join(d, "w.log"))[0].src == "B"


def _rec(t, src, bus="can", cid=0x200, data=b"\x00"):
    return C.Record(t, bus, cid, data, src=src)


def test_reorderer_merges_sources_in_time_order():
    ro = M.Reorderer(window=0.1)
    # source A delivers a burst late, source B an interleaved one
    ro.add([_rec(10.00, "A"), _rec(10.04, "A"), _rec(10.08, "A")])
    ro.add([_rec(10.02, "B"), _rec(10.06, "B")])
    assert ro.release(9.9) == []                     # nothing old enough yet
    out = ro.release(10.15)
    assert [(r.t, r.src) for r in out] == [(10.00, "A"), (10.02, "B"), (10.04, "A")]
    ro.add([_rec(10.07, "B")])                       # still inside the window: sorted in
    assert [(r.t, r.src) for r in ro.release(10.5)] == [(10.06, "B"), (10.07, "B"), (10.08, "A")]
    ro.add([_rec(11.0, "A"), _rec(11.0, "B")])
    assert [r.src for r in ro.drain()] == ["A", "B"]  # equal times keep arrival order
    assert len(ro) == 0


def test_device_clock_unwraps_and_corrects_drift():
    dc = M.DeviceClock()
    # the device clock runs 100 ppm fast against the shared one over 200 s
    t_shared = 1000.0
    last = None
    for i in range(0, 2001):
        true = i * 0.1
        ts = int(true * 1e6 * 1.0001) & 0xFFFFFFFF
        last = dc.map(ts, t_shared + true + 0.002)      # frames read 2 ms after they happen
    # one-shot anchoring would be 20 ms out by now; re-anchoring keeps it within a few ms
    assert abs(last - (t_shared + 200.0 + 0.002)) < 0.008
    # 32-bit wrap (every ~71.6 minutes) does not jump back
    dc = M.DeviceClock()
    a = dc.map(0xFFFFFF00, 50.0)
    b = dc.map(0x00000100, 50.001)
    assert b > a and abs((b - a) - 512e-6) < 1e-6


def test_shared_clock_is_monotonic_from_one_epoch():
    ticks = iter([5.0, 5.0, 5.5, 7.0])
    c = M.SharedClock(wall=1000.0, perf=lambda: next(ticks))
    assert c.now() == 1000.0 and c.now() == 1000.5 and c.now() == 1002.0


def _qt():
    from ovtsuite import gui as _g
    import pytest
    if not _g.HAVE_QT:
        pytest.skip("needs a working PySide6 QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    return QtCore, QtWidgets


def _dialog(monkeypatch, tmp_path):
    """A SnifferDialog with fake workers (no hardware, no threads) and the
    message boxes recorded rather than shown."""
    QtCore, QtWidgets = _qt()
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import recent
    from ovtsuite.sniff import gui_sniff as S
    monkeypatch.setattr(recent, "get_last_dir", lambda *a, **k: str(tmp_path))
    boxes = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _n=name, **k: boxes.append((_n, a[2]))))

    class Fake(QtCore.QObject):
        frames = QtCore.Signal(list)
        opened = QtCore.Signal(str)
        failed = QtCore.Signal(str)
        baud_changed = QtCore.Signal(int)
        ended = QtCore.Signal()
        made = []

        def __init__(self, *a, clock=None, src="", **k):
            super().__init__()
            self.a, self.src, self.sent, self.stopped = a, src, [], False
            Fake.made.append(self)

        def start(self):
            self.opened.emit("fake %s" % self.src)

        def stop(self):
            self.stopped = True
            self.ended.emit()

        def wait(self, *_):
            pass

        def send(self, cid, data):
            self.sent.append((cid, data))

    class FakeCan(Fake):
        mode = "can"

    class FakeTap(Fake):
        def set_baud(self, b):
            pass

    FakeCan.made = []
    Fake.made = FakeCan.made
    monkeypatch.setattr(S, "SnifferWorker", FakeCan)
    monkeypatch.setattr(S, "SerialTapWorker", FakeTap)
    d = S.SnifferDialog()
    return d, S, boxes, FakeCan, FakeTap


def _two_sources(d, S):
    d.dev_box.clear()
    d.dev_box.addItem("fake openport", S.OPENPORT_USB)
    d.src_box.setCurrentIndex(0)
    d.mode_box.setCurrentIndex(0)                    # CAN
    d.path_edit.setText("")
    d.rec_chk.setChecked(False)
    row = d._add_source()                            # B: serial K-line tap
    row.dev_box.clear()
    row.dev_box.addItem("fake port", "/dev/ttyFAKE")
    return row


import pytest  # noqa: E402


class _Dev:                                          # a registered J2534 device, as list_devices gives
    loadable = True
    bits = 64

    def __init__(self, name):
        self.name = name


@pytest.mark.slow
def test_dialog_two_sources_run_merge_and_stop(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        row = _two_sources(d, S)
        path = str(tmp_path / "two.log")
        d.rec_chk.setChecked(True)
        d.path_edit.setText(path)
        d._toggle()
        assert sorted(d._workers) == ["A", "B"] and not boxes
        wa, wb = d._workers["A"], d._workers["B"]
        assert wa.src == "A" and wb.src == "B"
        assert not row.kind_box.isEnabled()          # locked while running
        # B delivers a burst that is older than a frame A already delivered
        t0 = d._clock.now()
        ta = [_rec(t0 + 0.00, "A", data=bytes.fromhex("01C0001000030 1".replace(" ", ""))[:7]),
              _rec(t0 + 0.02, "A", cid=0x740, data=b"\xa3")]
        tb = [C.Record(t0 + 0.01, "kraw", 0, bytes.fromhex("8110F13E") + bytes([sum(bytes.fromhex("8110F13E")) & 0xFF]),
                       src="B")]
        wa.frames.emit(ta)
        wb.frames.emit(tb)
        d._flush()                                   # inside the window: nothing shown yet
        assert d.model.rowCount() == 0 and len(d._reorder) == 3
        wa.stop()                                    # A ends; B carries on, nothing lost
        assert sorted(d._workers) == ["B"] and d.start_btn.text() == "Stop"
        wb.stop()                                    # the last one ends: everything flushed
        assert d.start_btn.text() == "Start" and d.start_btn.isEnabled()
        assert [r.src for r in d._records] == ["A", "B", "A"]
        assert [r.t for r in d._records] == sorted(r.t for r in d._records)
        # the writer got them in time order, tagged
        lines = [ln for ln in open(path).read().splitlines() if not ln.startswith("#")]
        assert [ln.rsplit("@", 1)[1] for ln in lines] == ["A", "B", "A"]
        assert not d.table.isColumnHidden(S.FrameModel.C_SRC)
        # per-source dt: A's second frame is 20 ms after A's first, not 10 ms after B's
        dts = {(r.src, round(t, 3)): dt for r, _t, _k, dt in d.model.rows for t in [r.t - t0]}
        a2 = [dt for (r, _t, k, dt) in d.model.rows if r.src == "A" and r.can_id == 0x740][0]
        assert abs(a2 - 0.02) < 1e-6
        # hiding a source filters the view, not the records
        d._src_checks["B"].setChecked(False)
        assert {r.src for r, *_ in d.model.rows} == {"A"} and len(d._records) == 3
    finally:
        d.close()


@pytest.mark.slow
def test_dialog_rejects_same_device_twice_and_isolates_failures(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        row = _two_sources(d, S)
        row.kind_box.setCurrentIndex(0)              # B: a second J2534 CAN on the same OpenPort
        row.dev_box.clear()
        row.dev_box.addItem("fake openport", S.OPENPORT_USB)
        d._toggle()
        assert not d._workers and boxes and "only be opened once" in boxes[-1][1]
        # two taps on one port: same message
        d.src_box.setCurrentIndex(1)
        d.dev_box.clear()
        d.dev_box.addItem("p", "/dev/ttyFAKE")
        row.kind_box.setCurrentIndex(2)
        row.dev_box.clear()
        row.dev_box.addItem("p", "/dev/ttyFAKE")
        boxes.clear()
        d._toggle()
        assert not d._workers and "same port" in boxes[-1][1]
        # now valid: one source failing leaves the other capturing
        row.dev_box.clear()
        row.dev_box.addItem("p2", "/dev/ttyFAKE2")
        boxes.clear()
        d._toggle()
        assert sorted(d._workers) == ["A", "B"]
        wa, wb = d._workers["A"], d._workers["B"]
        wa.failed.emit("no cable")
        wa.stop()
        assert boxes and "Source A: no cable" in boxes[-1][1]
        assert list(d._workers) == ["B"] and d.start_btn.text() == "Stop"
        wb.stop()
        assert d.start_btn.text() == "Start"
    finally:
        d.close()


@pytest.mark.slow
def test_dialog_send_targets_chosen_source_and_decoders_are_per_source(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        row = _two_sources(d, S)
        row.kind_box.setCurrentIndex(0)              # B: a second CAN source
        row.dev_box.clear()
        row.dev_box.addItem("second device", _Dev("other"))
        assert d.send_src.count() == 2 and d.send_src.isVisibleTo(d)
        d.send_src.setCurrentIndex(d.send_src.findData("B"))
        d.send_id.setText("740")
        d.send_data.setText("A3")
        d._toggle()
        assert sorted(d._workers) == ["A", "B"] and not boxes
        d._send()
        assert d._workers["B"].sent == [(0x740, b"\xa3")] and d._workers["A"].sent == []
        # the same CAN ids on two buses decode independently: A sees a channel
        # setup, B sees the same frames without it, so only A gets TP 2.0 rows
        t = d._clock.now()
        setup = [("200", "01 C0 00 10 00 03 01"), ("201", "00 D0 00 03 40 07 01")]
        ra = [C.Record(t + i * 0.001, "can", int(c, 16), bytes.fromhex(h), src="A")
              for i, (c, h) in enumerate(setup)]
        rb = [C.Record(t + 0.0005 + i * 0.001, "can", 0x300, b"\xb1", src="B") for i in range(2)]
        for w, rr in ((d._workers["A"], ra), (d._workers["B"], rb)):
            w.frames.emit(rr)
        d._workers["A"].stop()
        d._workers["B"].stop()
        kinds = {(r.src, k) for r, _t, k, _dt in d.model.rows}
        assert ("A", "setup") in kinds
        assert not any(s == "B" and k != "raw" for s, k in kinds)
        assert set(d._decoders) == {"A", "B"} and d._decoders["A"] is not d._decoders["B"]
    finally:
        d.close()


@pytest.mark.slow
def test_dialog_single_source_still_untagged(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        d.dev_box.clear()
        d.dev_box.addItem("fake openport", S.OPENPORT_USB)
        d.rec_chk.setChecked(False)
        d._toggle()
        w = d._worker
        assert w is not None and w.src == "" and not d._merge
        assert d.table.isColumnHidden(S.FrameModel.C_SRC) and not d.send_src.isVisibleTo(d)
        w.frames.emit([C.Record(1.0, "can", 0x123, b"\x01")])
        d._flush()                                   # no merge window for one source
        assert d.model.rowCount() == 1 and d._records[0].src == ""
        w.stop()
        assert d._worker is None
    finally:
        d.close()


def test_device_clock_without_a_hardware_clock_uses_host_time():
    dc = M.DeviceClock()                              # OpenPort K-line packets: ts always 0
    out = [dc.map(0, 100.0 + i * 0.5) for i in range(6)]
    assert out == [100.0 + i * 0.5 for i in range(6)] and dc.no_clock
    # a stuck non-zero counter is detected over time too
    dc = M.DeviceClock()
    out = [dc.map(777, 10.0 + i * 0.1) for i in range(12)]
    assert dc.no_clock and abs(out[-1] - 11.1) < 1e-9
    # and a real, advancing clock never is
    dc = M.DeviceClock()
    for i in range(50):
        dc.map(i * 1000, 5.0 + i * 0.001)
    assert not dc.no_clock


@pytest.mark.slow
def test_dialog_zero_timestamp_source_is_stamped_in_order_and_fast(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        d.dev_box.clear()
        d.dev_box.addItem("fake openport", S.OPENPORT_USB)
        d.rec_chk.setChecked(False)
        d._toggle()
        # a source whose records carry host stamps (as the worker now makes them)
        dc = M.DeviceClock()
        t = 1000.0
        recs = [C.Record(dc.map(0, t + i * 0.01), "kline", 0, b"\x81\x10\xf1\x3e\xc0") for i in range(3000)]
        assert [r.t for r in recs] == sorted(r.t for r in recs)
        d._ingest(recs, live=False)
        assert len(d._records) == 3000 and d.model.rowCount() == 3000
        # a late record is placed by bisection, ties after equals
        d._store(C.Record(1010.005, "kline", 0, b"\x00"))
        assert [r.t for r in d._records] == sorted(r.t for r in d._records)
    finally:
        d.close()


@pytest.mark.slow
def test_dialog_hidden_source_hides_its_messages_and_t0_is_the_earliest(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        _two_sources(d, S).kind_box.setCurrentIndex(0)
        d._add_source  # noqa: B018
        t = 500.0
        mk = lambda dt, cid, h, src: C.Record(t + dt, "can", cid, bytes.fromhex(h), src=src)  # noqa: E731
        seq = [(0.10, 0x200, "01 C0 00 10 00 03 01"), (0.11, 0x201, "00 D0 00 03 40 07 01"),
               (0.12, 0x740, "10 00 02 1A 9B"), (0.13, 0x300, "B1")]
        recs = [mk(dt, c, h, "A") for dt, c, h in seq]
        d._note_src("A")
        d._note_src("B")
        d._ingest(recs, live=False)
        assert d.msgs.rowCount() == 1
        assert abs(float(d.msgs.item(0, 0).text()) - 0.02) < 1e-3
        # a record stamped earlier than everything shown, arriving late: t0 moves back
        d._ingest([mk(0.0, 0x555, "00", "B")], live=False)
        assert d.model.t0 == t and float(d.msgs.item(0, 0).text()) > 0.119
        assert all(float(d.model.data(d.model.index(i, 0))) >= 0 for i in range(d.model.rowCount()))
        d._src_checks["A"].setChecked(False)          # rebuild: A's messages go with its rows
        assert d.msgs.rowCount() == 0
        d._src_checks["A"].setChecked(True)
        assert d.msgs.rowCount() == 1
    finally:
        d.close()


@pytest.mark.slow
def test_dialog_send_to_a_stopped_source_says_so_and_template_follows_source(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        row = _two_sources(d, S)
        row.kind_box.setCurrentIndex(1)               # B: J2534 K-line
        row.dev_box.clear()
        row.dev_box.addItem("second device", _Dev("other"))
        d.send_src.setCurrentIndex(d.send_src.findData("B"))
        assert d.send_data.text() == "81 10 F1 3E"    # B is K-line though A is CAN
        d.send_src.setCurrentIndex(d.send_src.findData("A"))
        assert d.send_data.text() == "01 C0 00 10 00 03 01"
        d._toggle()
        assert sorted(d._workers) == ["A", "B"]
        d.send_src.setCurrentIndex(d.send_src.findData("B"))
        d._workers["B"].stop()
        boxes.clear()
        d._send()
        assert boxes and "Source B has stopped" in boxes[-1][1]
        d._workers["A"].stop()
    finally:
        d.close()


@pytest.mark.slow
def test_capture_file_is_rewritten_sorted_when_a_late_record_lands_out_of_order(monkeypatch, tmp_path):
    d, S, boxes, FakeCan, FakeTap = _dialog(monkeypatch, tmp_path)
    try:
        d.dev_box.clear()
        d.dev_box.addItem("fake openport", S.OPENPORT_USB)
        path = str(tmp_path / "one.log")
        d.rec_chk.setChecked(True)
        d.path_edit.setText(path)
        d._toggle()
        w = d._worker
        w.frames.emit([C.Record(10.0, "can", 0x1, b"\x01"), C.Record(10.5, "can", 0x2, b"\x02")])
        d._flush()
        w.frames.emit([C.Record(10.2, "can", 0x3, b"\x03")])   # later than any window
        d._flush()
        w.stop()
        got = [r.t for r in C.read(path)]
        assert got == [10.0, 10.2, 10.5]
    finally:
        d.close()
