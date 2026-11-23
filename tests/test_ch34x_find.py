# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CH34x direct port names and device matching: the right cable after a replug,
never a guess among several, nothing offered on Linux."""
import types

import pytest

from ovtsuite.flash import ch34x


class Dev:
    def __init__(self, bus, address, ports=(), serial=None, pid=0x7523):
        self.bus, self.address, self.port_numbers = bus, address, tuple(ports)
        self.serial_number, self.idProduct = serial, pid


@pytest.fixture
def usbworld(monkeypatch):
    monkeypatch.setattr(ch34x.sys, "platform", "darwin")
    world = {"devs": []}

    def find(find_all=False, idVendor=None, idProduct=None):
        return [d for d in world["devs"] if d.idProduct == idProduct]

    usb = types.SimpleNamespace(core=types.SimpleNamespace(find=find))
    monkeypatch.setattr(ch34x, "_usb", lambda: usb)
    return world


def test_names_carry_the_port_path_and_old_names_still_parse(usbworld):
    usbworld["devs"] = [Dev(1, 7, (2, 4))]
    (name, _), = ch34x.list_ch34x_devices()
    assert name == "ch34x://1a86:7523:1:7@p2.4"
    assert ch34x._split_ident("1:7@p2.4") == ("1:7", "2.4")
    assert ch34x._split_ident("1:7") == ("1:7", "")
    assert ch34x._split_ident("SN@p1") == ("SN", "1")


def test_replug_into_the_same_socket_is_found_by_its_port_path(usbworld):
    a, b = Dev(1, 7, (2, 4)), Dev(1, 8, (2, 5))
    usbworld["devs"] = [a, b]
    names = dict((n, None) for n, _ in ch34x.list_ch34x_devices())
    saved = "ch34x://1a86:7523:1:8@p2.5"
    assert saved in names
    b.address = 12                                  # replugged: the address moved
    assert ch34x._find_device(saved) is b           # not devs[0]


def test_old_saved_name_without_a_path_still_matches_by_address(usbworld):
    a, b = Dev(1, 7, (2, 4)), Dev(1, 8, (2, 5))
    usbworld["devs"] = [a, b]
    assert ch34x._find_device("ch34x://1a86:7523:1:8") is b


def test_serial_number_wins(usbworld):
    a, b = Dev(1, 7, (1,), "AAA"), Dev(1, 8, (2,), "BBB")
    usbworld["devs"] = [a, b]
    assert ch34x._find_device("ch34x://1a86:7523:BBB@p9") is b


def test_one_remaining_cable_is_used_after_a_move(usbworld):
    only = Dev(2, 3, (4,))
    usbworld["devs"] = [only]
    assert ch34x._find_device("ch34x://1a86:7523:1:7@p2.4") is only


def test_several_cables_none_matching_asks_to_reselect(usbworld):
    usbworld["devs"] = [Dev(1, 7, (2, 4)), Dev(1, 8, (2, 5))]
    with pytest.raises(ch34x.Ch34xError, match="select the cable again"):
        ch34x._find_device("ch34x://1a86:7523:3:9@p9.9")


def test_no_cable_at_all(usbworld):
    with pytest.raises(ch34x.Ch34xError, match="no CH34x cable"):
        ch34x._find_device("ch34x://1a86:7523:3:9")


def test_linux_offers_nothing_and_refuses_to_open(usbworld, monkeypatch):
    usbworld["devs"] = [Dev(1, 7, (2, 4))]
    monkeypatch.setattr(ch34x.sys, "platform", "linux")
    assert ch34x.list_ch34x_devices() == []
    with pytest.raises(ch34x.Ch34xError, match="serial port"):
        ch34x._find_device("ch34x://1a86:7523:1:7")


def test_the_reader_pairs_begin_and_end_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(ch34x._realtime, "begin_thread", lambda: calls.append("begin"))
    monkeypatch.setattr(ch34x._realtime, "end_thread", lambda: calls.append("end"))
    import threading

    class USBTimeoutError(Exception):
        pass

    p = ch34x.Ch34xPort.__new__(ch34x.Ch34xPort)
    p._stop, p._err, p._buf, p._cv = False, None, bytearray(), threading.Condition()
    p._usb_core = types.SimpleNamespace(USBTimeoutError=USBTimeoutError)
    p._ep_in = 0x82
    p._dev = types.SimpleNamespace(read=lambda *a: (_ for _ in ()).throw(OSError("unplugged")))
    p._raw_bulk_in = lambda: None
    p._reader()
    assert calls == ["begin", "end"] and isinstance(p._err, OSError)
