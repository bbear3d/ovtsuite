# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The flasher's global trace (ovtsuite.flash.flashtrace): off unless the option
is on, one file in the Bus Sniffer's format, rotated at the size limit, with
the 32-bit bridge's half limited the same way, and every ECU's worker using it
(there is no per-ECU CAN log any more)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore
except ImportError as exc:                              # no Qt
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import options as opt  # noqa: E402
from ovtsuite.flash import flashtrace as FT  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from ovtsuite.sniff import capture as C  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "options.ini")
    monkeypatch.setattr(opt, "_store", lambda: QtCore.QSettings(
        path, QtCore.QSettings.Format.IniFormat))
    return path


def _turn_on(tmp_path, mb=1, keep=3, name="flash.log"):
    opt.set("flash/trace_on", True)
    opt.set("flash/trace_file", str(tmp_path / name))
    opt.set("flash/trace_max_mb", mb)
    opt.set("flash/trace_keep", keep)
    return str(tmp_path / name)


# ------------------------------------------------------------- the options
def test_off_by_default_and_the_default_file_is_in_documents():
    assert opt.get("flash/trace_on") is False
    assert FT.begin("EDC16 read") is None               # nothing is opened
    assert opt.BY_KEY["flash/trace_file"].kind == "file"
    assert opt.get("flash/trace_file") == ""            # empty: the default
    p = FT.default_path()
    assert p.startswith(FT.documents_dir())
    assert os.path.basename(p) == "flash-trace.log"


def test_the_option_keys_and_defaults_match_the_module():
    for key, default in ((FT.KEY_ON, False), (FT.KEY_FILE, ""),
                         (FT.KEY_MAX_MB, FT.DEFAULT_MAX_MB),
                         (FT.KEY_KEEP, FT.DEFAULT_KEEP)):
        assert opt.BY_KEY[key].default == default
        assert opt.BY_KEY[key].page == opt.FLASHER


def test_an_empty_file_option_means_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(FT, "default_path", lambda: str(tmp_path / "d" / "t.log"))
    opt.set("flash/trace_on", True)
    tr = FT.begin("x")
    assert tr is not None and tr.path == str(tmp_path / "d" / "t.log")
    tr.close()
    assert os.path.exists(tmp_path / "d" / "t.log")      # its folder was made


def test_begin_reports_a_file_it_cannot_open(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    opt.set("flash/trace_on", True)
    opt.set("flash/trace_file", str(blocker / "sub" / "t.log"))   # a folder under a file
    said = []
    assert FT.begin("x", log=said.append) is None
    assert said and "flash trace off" in said[0]


# ------------------------------------------------------------- the file
def test_rotates_at_the_limit_and_keeps_only_a_few(tmp_path):
    path = str(tmp_path / "t.log")
    f = FT.TraceFile(path, 4096, keep=2)
    for i in range(400):                                 # ~ 30 bytes a line
        f.write("(%.6f) can 200#%016X" % (i, i))
    f.close()
    assert f.rotations >= 2
    names = sorted(os.listdir(tmp_path))
    assert names == ["t.log", "t.log.1", "t.log.2"]      # no .3: two kept
    for n in names:
        assert os.path.getsize(tmp_path / n) <= 4096     # never past the limit
    # the newest lines are in the live file, the oldest are gone
    last = open(path).read()
    assert "%016X" % 399 in last
    assert "%016X" % 0 not in "".join(open(tmp_path / n).read() for n in names)
    assert open(path).readline().startswith("# continued:")


def test_keep_zero_just_empties_a_full_file(tmp_path):
    f = FT.TraceFile(str(tmp_path / "t.log"), 4096, keep=0)
    for i in range(400):
        f.write("(%.6f) can 200#%016X" % (i, i))
    f.close()
    assert os.listdir(tmp_path) == ["t.log"]
    assert os.path.getsize(tmp_path / "t.log") <= 4096


def test_a_lower_keep_removes_the_files_left_over(tmp_path):
    path = str(tmp_path / "t.log")
    for n in (1, 2, 3, 4):
        (tmp_path / ("t.log.%d" % n)).write_text("old")
    f = FT.TraceFile(path, 4096, keep=1)
    for i in range(400):
        f.write("(%.6f) can 200#%016X" % (i, i))
    f.close()
    assert sorted(os.listdir(tmp_path)) == ["t.log", "t.log.1"]


def test_the_file_is_added_to_across_sessions(tmp_path):
    path = str(tmp_path / "t.log")
    for text in ("first", "second"):
        f = FT.TraceFile(path, 1 << 20)
        f.write("# " + text)
        f.close()
    assert open(path).read().splitlines() == ["# first", "# second"]


def test_a_failing_file_never_raises(tmp_path):
    f = FT.TraceFile(str(tmp_path / "t.log"), 4096)
    f._fh.close()                                        # as if the disk went
    f.write("a line")                                    # ValueError inside: swallowed
    assert f.error and not f.alive
    f.write("another")                                   # dropped, quietly
    f.close()


# ------------------------------------------------------------- the session
def test_can_frames_and_status_lines_reload_in_the_sniffer(tmp_path):
    ecu = MockTp20Ecu(lambda req: b"\x50\x89" if req == b"\x10\x89" else b"\x7e")
    tr = FT.FlashTrace(str(tmp_path / "t.log"), 1 << 20, 3, "EDC16 read")
    tr.note("Opening CAN (TP 2.0)...")
    ch = TT.Tp20Channel(tr.tap(ecu), dest=0x01)
    ch.open()
    ch.send(b"\x10\x89")
    assert ch.recv() == b"\x50\x89"
    ch.close()
    tr.close()
    text = open(tmp_path / "t.log").read()
    assert "EDC16 read" in text and "Opening CAN (TP 2.0)" in text
    recs = C.read(str(tmp_path / "t.log"))               # the status lines are comments
    assert recs and all(r.bus == "can" for r in recs)
    assert any(r.tx and r.can_id == 0x200 for r in recs)  # our setup TX
    assert any(r.can_id == 0x300 for r in recs)           # an ECU frame
    assert tr.frames == len(recs)


def test_the_tap_counts_the_bus_even_with_no_trace():
    ecu = MockTp20Ecu(lambda req: b"\x7e")
    tap = FT.TracedChannel(ecu)                           # tracing off: just the tally
    ch = TT.Tp20Channel(tap, dest=0x01)
    ch.open()
    ch.close()
    assert tap.tx > 0 and tap.rx > 0 and tap.frames == tap.tx + tap.rx


class _RxChannel:
    """A channel that hands out canned J2534 messages on read()."""

    def __init__(self, msgs):
        self.msgs = list(msgs)

    def read(self, max_msgs=16, timeout_ms=100):
        out, self.msgs = self.msgs, []
        return out

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        return None


def test_a_received_29_bit_frame_is_flagged_from_rx_status(tmp_path):
    from ovtsuite.flash import j2534 as J
    tr = FT.FlashTrace(str(tmp_path / "x.log"), 1 << 20, 3)
    ext = J.Msg(J.CAN, J.CAN_29BIT_ID, 0, 0, (0x18DAF110).to_bytes(4, "big") + b"\x01\x02")
    std = J.Msg(J.CAN, 0, 0, 0, (0x7E8).to_bytes(4, "big") + b"\x03")
    TracedChannel = FT.TracedChannel(_RxChannel([ext, std]), tr)
    TracedChannel.read()
    tr.close()
    recs = C.read(str(tmp_path / "x.log"))
    assert [(r.can_id, r.extended, r.tx) for r in recs] == [(0x18DAF110, True, False),
                                                           (0x7E8, False, False)]


def test_tx_echoes_are_not_counted_as_extra_tx_frames(tmp_path):
    from ovtsuite.flash import j2534 as J
    tr = FT.FlashTrace(str(tmp_path / "e.log"), 1 << 20, 3)
    echo = J.Msg(J.CAN, J.TX_MSG_TYPE, 0, 0, (0x200).to_bytes(4, "big") + b"\x01")
    real = J.Msg(J.CAN, 0, 0, 0, (0x201).to_bytes(4, "big") + b"\x02")
    tap = FT.TracedChannel(_RxChannel([echo, real]), tr)
    tap.write_can(0x200, b"\x01")
    tap.read()
    tr.close()
    assert (tap.tx, tap.rx) == (1, 1)
    assert [(r.can_id, r.tx) for r in C.read(str(tmp_path / "e.log"))] == [(0x200, True),
                                                                          (0x201, False)]


def test_kline_bytes_both_ways(tmp_path):
    tr = FT.FlashTrace(str(tmp_path / "k.log"), 1 << 20, 3)
    cb = tr.kline_cb()
    cb("tx", b"\x81\x10\xf1\x3e")
    cb("rx", b"\x83\xf1\x10\x7e")
    tr.close()
    recs = C.read(str(tmp_path / "k.log"))
    assert [(r.bus, r.tx, r.data.hex()) for r in recs] == [
        ("kraw", True, "8110f13e"), ("kraw", False, "83f1107e")]


def test_the_bridge_gets_its_own_file_with_the_same_limits(tmp_path):
    tr = FT.FlashTrace(str(tmp_path / "t.log"), 5 << 20, 2)
    bt = tr.bridge_trace()
    tr.close()
    assert bt["log_path"] == str(tmp_path / "t-bridge.log")
    assert bt["log_limits"] == {"log_max_bytes": 5 << 20, "log_keep": 2}
    assert FT.bridge_file("a/b/trace") == "a/b/trace-bridge.log"


def test_the_bridge_tap_rotates_like_the_trace_file(tmp_path):
    from ovtsuite.flash import j2534_bridge as B

    class _Ch:
        protocol = 5

        def write_can(self, *a):
            return 1

        def read(self, *a):
            return []

    path = str(tmp_path / "t-bridge.log")
    tap = B._FrameTap(_Ch(), path, max_bytes=4096, keep=1)
    for i in range(500):
        tap.write_can(0x200 + (i % 8), b"\x01\x02\x03\x04\x05\x06\x07\x08")
    tap.close()
    assert sorted(os.listdir(tmp_path)) == ["t-bridge.log", "t-bridge.log.1"]
    for n in os.listdir(tmp_path):
        assert os.path.getsize(tmp_path / n) <= 4096 + 80   # (a line over, at most)
    assert C.read(path)                                      # the same capture format


# ------------------------------------------------------------- the worker
@pytest.fixture
def gui_flash():
    from ovtsuite.flash import gui_flash as G
    return G


def test_the_worker_opens_the_trace_only_when_on(gui_flash, tmp_path):
    w = gui_flash._Worker("read", "EDC16", None, "Medium")
    w._begin_trace()
    assert w._trace is None and w._bridge_trace() == {}
    path = _turn_on(tmp_path)
    said = []
    w.stage.connect(said.append)
    w._begin_trace()
    assert w._trace is not None and w._trace.path == path
    assert any("Flash trace -> " in s for s in said)


def test_the_workers_status_lines_result_and_errors_are_traced(gui_flash, tmp_path):
    path = _turn_on(tmp_path)
    w = gui_flash._Worker("write", "Aisin TRx", None, "Medium", image=b"\0")
    w._begin_trace()
    w.stage.emit("Erasing sector 3...")
    w.failed.emit("The ECU said no")
    w._end_trace()
    w.stage.emit("after the end: not traced")
    text = open(path).read()
    assert "Aisin TRx write" in text and "Erasing sector 3" in text
    assert "FAILED: The ECU said no" in text and "after the end" not in text
    assert text.rstrip().endswith("====")                   # closed with its footer


class _Capture:
    """Stands in for a link class: keeps what it was built with."""
    made = []

    def __init__(self, *a, **k):
        type(self).made.append((a, k))


@pytest.fixture
def captured(monkeypatch):
    _Capture.made = []
    return _Capture.made


def test_a_k_line_link_gets_the_byte_hook(gui_flash, tmp_path, monkeypatch, captured):
    _turn_on(tmp_path)
    monkeypatch.setattr(gui_flash, "KLine", _Capture)
    w = gui_flash._Worker("read", "EDC15", "COM9", "Low", fast_init=False)
    w._begin_trace()
    w._open_link()
    hook = captured[0][1]["on_trace"]
    hook("tx", bytes([0x81]))
    w._end_trace()
    assert " kraw-tx 0#81" in open(tmp_path / "flash.log").read()


def test_a_k_line_link_gets_no_hook_while_tracing_is_off(gui_flash, monkeypatch, captured):
    monkeypatch.setattr(gui_flash, "KLine", _Capture)
    w = gui_flash._Worker("read", "EDC15", "COM9", "Low", fast_init=False)
    w._begin_trace()
    w._open_link()
    assert captured[0][1]["on_trace"] is None


def test_a_can_channel_is_tapped_for_any_ecu(gui_flash, tmp_path, monkeypatch, captured):
    from ovtsuite.flash import tp20_transport
    _turn_on(tmp_path)

    class _Ch:
        def pass_all(self):
            pass

        def clear_rx(self):
            pass

    class _Dev:
        def connect(self, *a):
            return _Ch()

    monkeypatch.setattr(gui_flash, "open_j2534", lambda d: _Dev())
    monkeypatch.setattr(tp20_transport, "CanKLine", _Capture)
    w = gui_flash._Worker("read", "EDC16", None, "Medium", transport="can",
                          can_dev="dummy")
    w._begin_trace()
    w._open_link()
    assert isinstance(captured[0][0][0], FT.TracedChannel)   # what the TP 2.0 link drives
    w._end_trace()


def test_the_32_bit_bridge_is_told_where_to_write(gui_flash, tmp_path, monkeypatch, captured):
    from ovtsuite.flash import j2534, tp20_transport
    path = _turn_on(tmp_path, mb=2, keep=1)
    seen = {}

    class _Bridge(j2534.BridgePassThru):
        def __init__(self):
            pass

        def open_tp20(self, **k):
            seen.update(k)
            return object()

    monkeypatch.setattr(gui_flash, "open_j2534", lambda d: _Bridge())
    monkeypatch.setattr(tp20_transport, "CanKLine", _Capture)
    w = gui_flash._Worker("read", "ZF6HP", None, "Medium", transport="can",
                          can_dev="dummy")
    w._begin_trace()
    w._open_link()
    w._end_trace()
    assert seen["log_path"] == FT.bridge_file(path)
    assert seen["log_limits"] == {"log_max_bytes": 2 << 20, "log_keep": 1}


def test_a_bridge_is_given_no_log_arguments_while_tracing_is_off(gui_flash, monkeypatch,
                                                                captured):
    from ovtsuite.flash import j2534, tp20_transport
    seen = {}

    class _Bridge(j2534.BridgePassThru):
        def __init__(self):
            pass

        def open_tp20(self, **k):
            seen.update(k)
            return object()

    monkeypatch.setattr(gui_flash, "open_j2534", lambda d: _Bridge())
    monkeypatch.setattr(tp20_transport, "CanKLine", _Capture)
    w = gui_flash._Worker("read", "ZF6HP", None, "Medium", transport="can",
                          can_dev="dummy")
    w._begin_trace()
    w._open_link()
    assert "log_path" not in seen and "log_limits" not in seen


def test_the_can_logging_of_the_old_workers_is_gone(gui_flash):
    import inspect
    assert "can_log" not in inspect.signature(gui_flash._Worker.__init__).parameters
    from ovtsuite.flash import climatronic, cluster, abs_esp8
    for mod in (climatronic, cluster, abs_esp8):
        assert not hasattr(mod, "CanTap")
    assert not hasattr(gui_flash, "_make_can_log_emit")
    assert not os.path.exists(os.path.join(os.path.dirname(FT.__file__), "tracelog.py"))


# ------------------------------------------------------ the bridge's half, merged
def _bridge_frames(path, n, start=0):
    from ovtsuite.flash import j2534_bridge as B

    class _Ch:
        protocol = 5

        def write_can(self, *a):
            return 1

        def read(self, *a):
            return []

    tap = B._FrameTap(_Ch(), path, max_bytes=1 << 20, keep=3)
    for i in range(start, start + n):
        tap.write_can(0x200 + i, b"\x01\x02\x03\x04")
    tap.close()


def test_the_bridges_frames_are_added_to_the_trace_and_its_file_deleted(tmp_path):
    tr = FT.FlashTrace(str(tmp_path / "t.log"), 1 << 20, 3, "ZF6HP read")
    bt = tr.bridge_trace()
    tr.note("Opening CAN (UDS)...")
    _bridge_frames(bt["log_path"], 5)                        # the bridge's file, as it left it
    tr.close()
    assert os.listdir(tmp_path) == ["t.log"]                 # one file is left
    text = open(tmp_path / "t.log").read()
    assert "Opening CAN (UDS)" in text and "CAN frames from the 32-bit bridge" in text
    assert text.index("Opening CAN") < text.index("CAN frames from") < text.index("# ==== end")
    recs = C.read(str(tmp_path / "t.log"))
    assert [r.can_id for r in recs] == [0x200 + i for i in range(5)]
    assert "5 CAN frames" in text                            # the footer counts them
    assert "CAN trace (bridge)" not in text                  # its own header line is not copied


def test_a_bridge_file_that_rotated_is_merged_oldest_first(tmp_path):
    from ovtsuite.flash import j2534_bridge as B

    class _Ch:
        protocol = 5

        def write_can(self, *a):
            return 1

        def read(self, *a):
            return []

    tr = FT.FlashTrace(str(tmp_path / "t.log"), 1 << 20, 2)
    path = tr.bridge_trace()["log_path"]
    tap = B._FrameTap(_Ch(), path, max_bytes=4096, keep=2)   # rotates several times
    for i in range(300):
        tap.write_can(0x100 + (i % 0x400), b"\xAA" * 8)
        tap._line(0x7FF, i.to_bytes(4, "big"), True, False)
    tap.close()
    assert os.path.exists(path + ".1")
    tr.close()
    assert os.listdir(tmp_path) == ["t.log"]
    seqs = [int.from_bytes(r.data, "big") for r in C.read(str(tmp_path / "t.log"))
            if r.can_id == 0x7FF]
    assert seqs == sorted(seqs) and seqs[-1] == 299           # in order, ending at the newest


def test_only_the_newest_frames_that_fit_are_merged(tmp_path):
    tr = FT.FlashTrace(str(tmp_path / "t.log"), 4096, 1)
    base = tr.bridge_trace()["log_path"]
    open(base + ".2", "w").write("(1.0) can 111#AA\n" * 400)     # older than the limit can hold
    open(base + ".1", "w").write("(2.0) can 222#BB\n" * 100)
    open(base, "w").write("(3.0) can 333#CC\n" * 100)
    tr.close()
    assert os.listdir(tmp_path) and not any("bridge" in n for n in os.listdir(tmp_path))
    ids = {r.can_id for r in C.read(str(tmp_path / "t.log"))}
    assert 0x333 in ids and 0x111 not in ids                   # the oldest file was left out
    assert os.path.getsize(tmp_path / "t.log") <= 4096


def test_frames_left_by_a_job_that_never_merged_are_picked_up(tmp_path):
    base = FT.bridge_file(str(tmp_path / "t.log"))
    open(base, "w").write("(9.0) can 444#DD\n")                 # a crash left this
    tr = FT.FlashTrace(str(tmp_path / "t.log"), 1 << 20, 3)    # a later job, no bridge in it
    tr.close()
    assert not os.path.exists(base)
    assert [r.can_id for r in C.read(str(tmp_path / "t.log"))] == [0x444]


def test_an_unreadable_bridge_file_is_left_and_the_trace_says_so(tmp_path, monkeypatch):
    tr = FT.FlashTrace(str(tmp_path / "t.log"), 1 << 20, 3)
    base = tr.bridge_trace()["log_path"]
    open(base, "w").write("(9.0) can 444#DD\n")
    real = open

    def _no(path, *a, **k):
        if str(path) == base:
            raise PermissionError("locked")
        return real(path, *a, **k)
    monkeypatch.setattr("builtins.open", _no)
    tr.close()
    monkeypatch.undo()
    assert os.path.exists(base)                                 # kept, not lost
    assert "stay in" in open(tmp_path / "t.log").read()


def test_the_workers_end_merges_the_bridge_file(gui_flash, tmp_path):
    path = _turn_on(tmp_path)
    w = gui_flash._Worker("read", "ZF6HP", None, "Medium")
    w._begin_trace()
    bt = w._bridge_trace()
    _bridge_frames(bt["log_path"], 3)
    w._end_trace()
    assert not any("bridge" in n for n in os.listdir(tmp_path))   # merged, then deleted
    assert len(C.read(path)) == 3
