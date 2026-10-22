# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC15 live data: KW1281 only, at 9600, odd address parity (a KWP2000 answer
is the flash-programming responder and is refused with the reason), the ident
text, and KW1281 over a J2534 device through the byte-stream link."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite.flash import diag as D  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash.kline_j2534_raw import J2534RawKLine  # noqa: E402
from ovtsuite.flash.transport import KLineError, KLineTimeout, adjust_parity  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_td", os.path.join(os.path.dirname(__file__), "test_datalog.py"))
_td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_td)
MockEdc15, _Link = _td.MockEdc15, _td._Link


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)


# ------------------------------------------------------------------ ident
def _blk(title, data, counter=1):
    return bytes([len(data) + 3, counter, title]) + bytes(data)


def test_kw1281_identity_text_coding_and_wsc():
    # the top bit of the first character flags "more via ReadIdent"; the text
    # is one string cut into blocks; a 00-led ASCII block is coding + WSC
    blocks = [_blk(0xF6, bytes([ord("0") | 0x80]) + b"38906012GN 1,9"),
              _blk(0xF6, b"l R4 EDC  SG  4308"),
              _blk(0xF6, bytes([0x00, 0x00, 0x04, 0x30, 0x39])),   # coding 2, WSC 12345
              _blk(0x09, b"")]
    text, coding, wsc = D.kw1281_identity(blocks)
    assert text == "038906012GN 1,9l R4 EDC SG 4308"
    assert coding == 2 and wsc == 12345


def test_kw1281_identity_wsc_overflow_bit():
    text, coding, wsc = D.kw1281_identity([_blk(0xF6, bytes([0x00, 0x00, 0x05, 0x00, 0x01]))])
    assert text == "" and coding == 2 and wsc == 65537


# --------------------------------------------------------- serial session
class _CountingLink(_Link):
    def __init__(self, ecu):
        super().__init__(ecu)
        self.wakes = []

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        self.wakes.append((self.port.baudrate, address, even_parity))
        return super().wake_up(address, even_parity, tries)


def test_edc15_is_9600_odd_parity_kw1281_and_reconnects_the_same_way():
    ecu = MockEdc15()
    link = _CountingLink(ecu)
    s = D.DiagSession(link, "EDC15", fast_kw1281=True)
    assert s.connect() == 9600
    assert s.kw.fast and not s.is_kwp2000
    assert s.identity == "038906012A 1.9l R4 EDC"
    assert s.read_group(1)[0].text == "800 rpm"
    ecu.mode = "idle"                         # a dropout
    s.reconnect()
    assert s.read_group(1)[0].text == "800 rpm"
    assert link.wakes == [(9600, 0x01, False)] * 2


class _SilentLink(_Link):
    """Never answers -- what a wrong-baud or dead line looks like."""

    def __init__(self, ecu, sync=-1):
        super().__init__(ecu)
        self.wakes = []
        self._sync = sync

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        self.wakes.append((self.port.baudrate, even_parity))
        self.last_sync = self._sync
        raise KLineError("Unexpected sync byte")


def test_edc15_no_answer_tries_nothing_but_9600_odd():
    link = _SilentLink(MockEdc15())
    s = D.DiagSession(link, "EDC15")
    with pytest.raises(KLineError):
        s.connect()
    assert link.wakes == [(9600, False)]


@pytest.mark.parametrize("sync", [0x95, 0xB5])
def test_edc15_kwp2000_reply_read_at_9600_says_kwp2000_mode(sync):
    s = D.DiagSession(_SilentLink(MockEdc15(), sync), "EDC15")
    with pytest.raises(KLineError, match="sync 0x%02X.*KWP2000 mode.*no measuring blocks" % sync):
        s.connect()


class _Kwp2000KeyLink(_Link):
    """A link (e.g. a J2534 device) that hands back KWP2000 key bytes."""

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        return ((0x8F & 0x7F) << 7) + (0x6B & 0x7F)      # 6B 8F = KW 2027


def test_edc15_kwp2000_key_bytes_are_refused_not_followed():
    s = D.DiagSession(_Kwp2000KeyLink(MockEdc15()), "EDC15")
    with pytest.raises(KLineError, match="KW 2027.*KWP2000 mode.*no measuring blocks"):
        s.connect()
    assert s.kwp is None and s.kw is None


def test_edc15_unknown_group_reply_is_unavailable_not_a_dropout():
    ecu = MockEdc15()
    s = D.DiagSession(_Link(ecu), "EDC15")
    s.connect()
    orig = ecu._send_block

    def send(title, data):          # answer group 2 with the 02 "with text" form
        if title == 0x0A and ecu.log and ecu.log[-1] == (0x29, b"\x02"):
            title, data = 0x02, bytes([0x8D, 0x00, 0x02]) + b"ab"
        orig(title, data)
    ecu._send_block = send
    assert s.read_group(2) is None
    assert s.read_group(1)[0].text == "800 rpm"      # still in step


# ------------------------------------------------- KW1281 over J2534 (raw)
class _Msg:
    def __init__(self, data, echo=False):
        self.payload = bytes(data)
        self.is_tx_echo = echo


class _RawChannel:
    """A J2534 ISO9141 channel with a KW1281 EDC15 behind it: LOOPBACK 0
    (no echo), every received byte its own message (P1_MAX 1 ms)."""

    def __init__(self, ecu, refuse=()):
        self.ecu = ecu
        self.config = {}
        self.refuse = set(refuse)
        self.inits = []
        self.closed = False

    def pass_all(self):
        pass

    def set_config(self, **kw):
        for k, v in kw.items():
            if k in self.refuse:
                raise J.J2534Error("ERR_NOT_SUPPORTED")
            self.config[k] = v
            if k == "DATA_RATE":
                self.ecu.baudrate = v

    def clear_rx(self):
        self.ecu.rx.clear()

    def five_baud_init(self, address):
        self.inits.append(address)
        e = self.ecu
        e._broke = True
        e.reset_input_buffer()               # the mock puts 55 01 8A (9600) out
        if bytes(e.rx[:1]) != b"\x55":
            e.rx.clear()
            raise J.J2534Error("ERR_FAILED")
        kb = bytes(e.rx[1:3])
        e.rx.clear()                          # the device consumes sync + key bytes
        if self.config.get("FIVE_BAUD_MOD") == 1:
            e.parse += bytes([(~kb[1]) & 0xFF])   # the device sends ~KB2 itself
            e._consume()
        return kb

    def write(self, data, flags=0, timeout_ms=100):
        e = self.ecu
        n = len(e.rx)
        e.write(bytes(data))
        del e.rx[n:n + len(data)]            # LOOPBACK 0: no echo from the line

    def read(self, max_msgs=32, timeout_ms=100):
        out = [_Msg([b]) for b in self.ecu.rx[:max_msgs]]
        del self.ecu.rx[:len(out)]
        return out

    def disconnect(self):
        self.closed = True


class _RawDev:
    def __init__(self, ecu, refuse=()):
        self.ecu, self.refuse = ecu, refuse
        self.connects = []
        self.ch = None

    def connect(self, protocol, baud, flags=0):
        self.connects.append((protocol, baud, flags))
        self.ecu.baudrate = baud
        self.ch = _RawChannel(self.ecu, self.refuse)
        return self.ch


def test_raw_j2534_link_runs_a_kw1281_session():
    ecu = MockEdc15()
    dev = _RawDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    assert dev.connects[0] == (J.ISO9141, 9600, J.ISO9141_NO_CHECKSUM | J.ISO9141_K_LINE_ONLY)
    cfg = dev.ch.config
    assert cfg["P1_MAX"] == 2 and cfg["P3_MIN"] == 0 and cfg["P4_MIN"] == 0
    assert cfg["LOOPBACK"] == 0 and cfg["FIVE_BAUD_MOD"] == 1
    s = D.DiagSession(link, "EDC15", fast_kw1281=True)
    assert s.connect() == 9600
    assert dev.ch.inits == [adjust_parity(0x01, False)]
    assert s.identity == "038906012A 1.9l R4 EDC"
    assert [c.text for c in s.read_group(1)] == ["800 rpm", "50.0 mg/stroke", "12.0 V", "90.0 °C"]
    assert s.read_group(5) is None
    assert [(c.code, c.status) for c in s.read_dtcs()] == [(17664, 0x23)]
    s.keepalive()
    s.close()
    assert ecu.log[-1][0] == 0x06
    link.close()
    assert dev.ch.closed


def test_raw_j2534_link_baud_change_reopens_when_data_rate_refused():
    ecu = MockEdc15()
    dev = _RawDev(ecu, refuse=("DATA_RATE",))
    link = J2534RawKLine(dev, baud=10400)
    link.set_baud(9600)
    assert [c[1] for c in dev.connects] == [10400, 9600]
    link.set_baud(9600)                       # same rate: nothing reopened
    assert len(dev.connects) == 2


def test_raw_j2534_link_says_why_kw1281_fails_without_five_baud_mod():
    ecu = MockEdc15()
    dev = _RawDev(ecu, refuse=("FIVE_BAUD_MOD",))
    link = J2534RawKLine(dev, baud=9600)
    assert not link.five_baud_mod_ok

    def no_answer(address):                   # mode 0 waits for ~address: none comes
        raise J.J2534Error("ERR_TIMEOUT")
    dev.ch.five_baud_init = no_answer
    with pytest.raises(KLineError, match="FIVE_BAUD_MOD"):
        link.wake_up(0x01, tries=1)


def test_raw_j2534_link_write_raw_leaves_its_echo():
    ecu = MockEdc15()
    link = J2534RawKLine(_RawDev(ecu), baud=10400)
    link.write_raw(b"\x12\x34")
    assert link.read(2) == b"\x12\x34"
    link.read_timeout = 0.01
    with pytest.raises(KLineTimeout):
        link.read_byte()


def test_logger_picks_the_raw_link_for_edc15_only():
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from ovtsuite.datalog import gui_log as G
    from ovtsuite.flash.kline_j2534 import J2534KLine
    ecu = MockEdc15()
    assert isinstance(G.kline_over_j2534(_RawDev(ecu), "EDC15", True, None), J2534RawKLine)

    class _KwpDev:
        def connect(self, *a, **k):
            raise AssertionError("J2534KLine opens its channel lazily")
    assert isinstance(G.kline_over_j2534(_KwpDev(), "EDC16", True, None), J2534KLine)
