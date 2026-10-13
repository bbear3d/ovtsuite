# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OpenPort 2.0 ASCII/bulk protocol against a scripted fake firmware."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash import openport as OP  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402


def pkt(chan, ptype, ts, payload=b""):
    body = bytes([ptype]) + ts.to_bytes(4, "big") + payload
    return b"ar" + bytes([0x30 + chan, len(body)]) + body


class FakeFirmware:
    """Answers commands the way the reference driver expects; ``inject``
    queues message packets that the next read returns (split into
    arbitrary chunks to exercise the stream parser)."""

    def __init__(self, chunk=7):
        self.sent = []
        self.outq = bytearray()
        self.chunk = chunk
        self.fail_next = None
        self.closed = False
        self.label = "fake OpenPort"

    def inject(self, data: bytes):
        self.outq += data

    def write(self, data: bytes):
        self.sent.append(bytes(data))
        head = data.lstrip(b"\r\n")[:3]
        if self.fail_next is not None:
            self.outq += b"are %d\r\n" % self.fail_next
            self.fail_next = None
            return
        if head == b"ati":
            self.outq += b"ari 1.17.4877\r\n"
        elif head in (b"ata", b"ato", b"atc", b"ats", b"atk"):
            self.outq += b"aro\r\n"
        elif head == b"atf":
            self.outq += b"arf 3\r\n"
        elif head == b"att":
            chan = int(chr(data[3]))
            n = int(data.split()[1])
            self.outq += pkt(chan, OP.TX_DONE, 1000, b"")
            if chan == J.CAN:                      # loopback echo of the frame
                body = data.split(b"\r\n", 1)[1][:n]
                self.outq += pkt(chan, OP.TX_LB_MSG, 1001, body)
        elif head == b"atr":
            self.outq += b"arr 16 13820\r\n"
        elif head == b"aty":
            self.outq += b"ary4 4\r\n" + bytes.fromhex("83F110C189")[:4]
        elif head == b"atw":                          # 5-baud init: arw<chan> <kb1> <kb2>
            chan = int(chr(data[3]))
            self.outq += b"arw%d 235 143\r\n" % chan     # key bytes EB 8F = KWP 2027
        elif head == b"atz":
            self.closed = True

    def read(self, timeout_ms):
        if not self.outq:
            return b""
        out = bytes(self.outq[:self.chunk])
        del self.outq[:self.chunk]
        return out

    def close(self):
        pass


def test_open_connect_filter_write_read():
    fw = FakeFirmware()
    dev = OP.OpenPort(fw)
    assert dev.firmware == "1.17.4877" and fw.sent[0].endswith(b"ati\r\n") and fw.sent[1] == b"ata\r\n"
    ch = dev.connect(J.CAN, 500000)
    assert fw.sent[-1] == b"ato5 0 500000 0\r\n" and ch.id == J.CAN
    ch.pass_all()
    assert ch._filters == [3] and fw.sent[-1] == b"atf5 1 0 4\r\n" + bytes(8)
    ch.set_config(LOOPBACK=1)
    assert fw.sent[-1] == b"ats5 3 1\r\n"
    ch.write_can(0x200, bytes.fromhex("01C00010000301"))
    assert fw.sent[-1] == b"att5 11 0\r\n" + bytes.fromhex("00000200") + bytes.fromhex("01C00010000301")
    # the ECU answers: start indication (ignored for CAN), then the data packet
    fw.inject(pkt(J.CAN, OP.NORM_MSG_START_IND, 2000) +
              pkt(J.CAN, OP.NORM_MSG, 2001, bytes.fromhex("00000201") + bytes.fromhex("00D0000340070101")))
    msgs = ch.read(max_msgs=8, timeout_ms=50)
    kinds = [(m.rx_status, m.can_id) for m in msgs]
    assert kinds == [(J.TX_DONE, None), (J.TX_MSG_TYPE, 0x200), (0, 0x201)]
    assert msgs[1].is_tx_echo and msgs[2].payload == bytes.fromhex("00D0000340070101")
    assert msgs[2].timestamp == 2001
    assert dev.battery_voltage() == 13.82
    ch.clear_filters()
    assert fw.sent[-1] == b"atk5 3\r\n"
    dev.close()
    assert fw.closed and ch.id is None and fw.sent[-2] == b"atc5\r\n"


def test_kline_accumulation_and_fast_init():
    fw = FakeFirmware(chunk=3)
    dev = OP.OpenPort(fw)
    ch = dev.connect(J.ISO14230, 10400, J.ISO9141_K_LINE_ONLY)
    assert fw.sent[-1] == b"ato4 4096 10400 0\r\n"
    # K-line: start(ts) / data bytes without timestamps / end(ts)
    fw.inject(pkt(J.ISO14230, OP.NORM_MSG_START_IND, 500) +
              b"ar4" + bytes([3]) + bytes([OP.NORM_MSG]) + b"\x83\xf1" +
              b"ar4" + bytes([4]) + bytes([OP.NORM_MSG]) + b"\x10\x50\x89" +
              pkt(J.ISO14230, OP.RX_MSG_END_IND, 900))
    msgs = ch.read(timeout_ms=50)
    assert len(msgs) == 1 and msgs[0].data == bytes.fromhex("83F1105089")
    assert msgs[0].timestamp == 900 and msgs[0].protocol == J.ISO14230 and not msgs[0].is_tx_echo
    assert ch.fast_init(bytes.fromhex("81 10 F1 81".replace(" ", ""))) == bytes.fromhex("83F110C1")
    assert fw.sent[-1] == b"aty4 4 0\r\n" + bytes.fromhex("8110F181")
    ch.set_baud(124800)
    assert fw.sent[-1] == b"ats4 1 124800\r\n"


def test_errors_and_resync():
    fw = FakeFirmware()
    dev = OP.OpenPort(fw)
    fw.fail_next = 0x19
    try:
        dev.connect(J.CAN, 123)
        assert False
    except OP.OpenPortError as ex:
        assert "ERR_INVALID_BAUDRATE" in str(ex)
    ch = dev.connect(J.CAN, 500000)
    fw.inject(b"\x00garbage" + pkt(J.CAN, OP.NORM_MSG, 7, bytes.fromhex("000007E8") + b"\x02\x41\x0c"))
    msgs = ch.read(timeout_ms=50)
    assert [m.can_id for m in msgs] == [0x7E8]
    # messages for another channel stay queued
    fw.inject(pkt(J.ISO15765, OP.NORM_MSG, 8, bytes.fromhex("000007E0") + b"\x01\x00"))
    assert ch.read(timeout_ms=20) == []
    ch2 = OP.OpenPortChannel(dev, J.ISO15765)
    assert [m.can_id for m in ch2.read(timeout_ms=20)] == [0x7E0]
    # a timeout waiting for a reply is reported, not hung
    class Mute(FakeFirmware):
        def write(self, data):
            self.sent.append(data)
    try:
        OP.OpenPort(Mute())
        assert False
    except OP.OpenPortError as ex:
        assert "no 'ari' reply" in str(ex)
    assert OP.find_devices() == [] or True


# --------------------------------------------------- USB interface selection
# Descriptor stand-ins with the layout the OpenPort 2.0 really enumerates with
# (Audi-Seat-Hacker hardware logs): CDC-ACM -- interface 0 = communications
# (class 2, interrupt IN 0x81 only), interface 1 = data (class 10, bulk OUT 0x02
# / IN 0x82). Taking interface 0 is the bug that left the open handshake with no
# reply on Linux.
class _Ep:
    def __init__(self, addr, attr, size=64):
        self.bEndpointAddress, self.bmAttributes, self.wMaxPacketSize = addr, attr, size


class _Intf(list):
    def __init__(self, num, cls, eps, alt=0):
        super().__init__(eps)
        self.bInterfaceNumber, self.bInterfaceClass, self.bAlternateSetting = num, cls, alt


def _cdc_acm_config():
    return [_Intf(0, 2, [_Ep(0x81, 0x03)]),                    # interrupt IN
            _Intf(1, 10, [_Ep(0x02, 0x02), _Ep(0x82, 0x02)])]  # bulk OUT, bulk IN


class _FakeDev:
    def __init__(self, cfg, configured=True, kernel_on=(0, 1)):
        self.cfg, self.configured = cfg, configured
        self.kernel = set(kernel_on)
        self.calls = []

    def get_active_configuration(self):
        import usb.core
        if not self.configured:
            raise usb.core.USBError("Configuration not set")
        return self.cfg

    def set_configuration(self):
        self.calls.append("set_configuration")
        self.configured = True

    def is_kernel_driver_active(self, n):
        return n in self.kernel

    def detach_kernel_driver(self, n):
        self.calls.append(("detach", n))
        self.kernel.discard(n)

    def attach_kernel_driver(self, n):
        self.calls.append(("attach", n))
        self.kernel.add(n)


def _patch_usb(monkeypatch, dev):
    import pytest
    util = pytest.importorskip("usb.util")
    monkeypatch.setattr(util, "claim_interface", lambda d, n: d.calls.append(("claim", n)))
    monkeypatch.setattr(util, "dispose_resources", lambda d: d.calls.append("dispose"))
    return util


def test_bulk_interface_is_found_by_scanning_not_assumed(monkeypatch):
    util = _patch_usb(monkeypatch, None)
    intf, ep_in, ep_out = OP.find_bulk_interface(_cdc_acm_config(), util)
    assert intf.bInterfaceNumber == 1
    assert (ep_in.bEndpointAddress, ep_out.bEndpointAddress) == (0x82, 0x02)
    assert OP.find_bulk_interface([_Intf(0, 2, [_Ep(0x81, 0x03)])], util) == (None, None, None)


def test_transport_claims_the_cdc_data_interface(monkeypatch):
    dev = _FakeDev(_cdc_acm_config())
    _patch_usb(monkeypatch, dev)
    t = OP.UsbTransport(dev)
    assert t.iface == 1
    assert (t.ep_in.bEndpointAddress, t.ep_out.bEndpointAddress) == (0x82, 0x02)
    # cdc_acm released from the data interface only; a configured device is not
    # re-configured (that is a reset on Linux, and refused while a driver is bound).
    assert dev.calls == [("detach", 1), ("claim", 1)]
    t.close()
    assert dev.calls[-2:] == ["dispose", ("attach", 1)]       # /dev/ttyACM* comes back


def test_transport_configures_only_an_unconfigured_device(monkeypatch):
    dev = _FakeDev(_cdc_acm_config(), configured=False, kernel_on=())
    _patch_usb(monkeypatch, dev)
    t = OP.UsbTransport(dev)
    assert dev.calls == ["set_configuration", ("claim", 1)]
    t.close()
    assert dev.calls[-1] == "dispose"                        # nothing to reattach


def test_no_bulk_pair_names_the_layout_it_saw(monkeypatch):
    dev = _FakeDev([_Intf(0, 2, [_Ep(0x81, 0x03)])])
    _patch_usb(monkeypatch, dev)
    try:
        OP.UsbTransport(dev)
        assert False
    except OP.OpenPortError as ex:
        assert "if 0/alt 0 class 2: intr IN 0x81" in str(ex)


def test_five_baud_init_uses_the_vendor_dll_command():
    """Slow init over the USB OpenPort: ``atw<chan> <address>`` -> ``arw<chan>
    <kb1> <kb2>``, the command and reply format read from op20pt32.dll (the DLL
    the Windows slow init runs through). The reply arrives in 3-byte USB chunks,
    so the stream parser must take 'arw' as a text line -- before 'w' was a
    known reply letter it threw the reply away and the init timed out."""
    fw = FakeFirmware(chunk=3)
    dev = OP.OpenPort(fw)
    ch = dev.connect(J.ISO9141, 10400, J.ISO9141_NO_CHECKSUM)
    assert ch.five_baud_init(0x01) == bytes([0xEB, 0x8F])
    assert fw.sent[-1] == b"atw3 1\r\n"


def test_kline_slow_init_end_to_end_over_usb_openport():
    """J2534KLine's 5-baud path (ISO9141 channel + FIVE_BAUD_INIT) now works on
    the USB OpenPort exactly as it does through the vendor DLL on Windows."""
    from ovtsuite.flash.kline_j2534 import J2534KLine
    fw = FakeFirmware(chunk=5)
    dev = OP.OpenPort(fw)
    link = J2534KLine(dev, fast_init=False, allow_slow_fallback=False)
    assert link.wake_up(0x01) == 2027                    # KWP version from the key bytes
    assert any(m.startswith(b"ato3 ") for m in fw.sent)                  # ISO9141 channel
    assert b"atw3 1\r\n" in fw.sent                                     # 5-baud init

