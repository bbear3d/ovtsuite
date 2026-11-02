# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P5: the generic UDS engine (services 10/11/14/19/22/2E/2F/31/27)
against a scripted UDS ECU, with and without a definitions pack."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_uds import DirectUdsLink, UdsEcu  # noqa: E402

from ovtsuite.diag import defs as D
from ovtsuite.diag import uds as U


def _client(ecu, resolver=None, ident=None, key_lookup=None):
    return U.UdsClient(DirectUdsLink(ecu), resolver=resolver, ident=ident,
                       key_lookup=key_lookup or (lambda name, seed: ecu.key if name == "test_key" else None))


def _zf6hp28_ecu():
    return UdsEcu(
        dids={0xF187: b"8K0927158C ", 0xF189: b"0020", 0xF191: b"0B6927156  ", 0xF1A3: b"H04",
              0xF197: b"0B6 20TFSIUSA", 0xF1A5: bytes.fromhex("00 00 00 00 18 35"),
              0x1234: b"\x3C", 0x2104: b"\x4E", 0xF40C: b"\x0B\xB8"},
        dtcs=[(bytes.fromhex("02 99 00"), 0x08), (bytes.fromhex("17 14 00"), 0x04)],
        coding=bytes.fromhex("00 10 02"), seed=bytes.fromhex("11 22 33 44"), key=bytes.fromhex("AA BB CC DD"),
        security_level=3, routines={0x0203: b"\x00", 0xF405: b""}, writable={0x0600, 0x1234})


def test_status_and_did_codec():
    st = U.decode_dtc_status(0x2F)
    assert st["present"] and st["pending"] and "present" in st["text"] and "testFailed" in st["flags"]
    assert U.decode_dtc_status(0x08)["confirmed"] and not U.decode_dtc_status(0x08)["present"]
    assert U.dtc_to_sae(bytes.fromhex("02 99 00")) == "P0299"
    assert U.dtc_to_sae(bytes.fromhex("02 99 13")) == "P0299-13"
    # typed decode
    assert U.decode_did_value(b"\x4E", {"type": "u8", "scale": {"kind": "linear", "factor": 1, "offset": -40, "unit": "°C"}}) == (38.0, "38 °C")
    assert U.decode_did_value(b"\x0B\xB8", {"type": "u16", "scale": {"kind": "linear", "factor": 0.25, "unit": "rpm"}})[0] == 750.0
    assert U.decode_did_value(b"\x02", {"type": "u8", "scale": {"kind": "table", "table": {"2": "Drive"}}}) == ("Drive", "Drive")
    assert U.decode_did_value(b"WAU", {"type": "ascii"}) == ("WAU", "WAU")
    assert U.decode_did_value(b"\xFF\x9C", {"type": "s16"})[0] == -100
    # encode is the inverse for the writable types
    assert U.encode_did_value(38.0, {"type": "u8", "scale": {"kind": "linear", "factor": 1, "offset": -40}}) == b"\x4E"
    assert U.encode_did_value("00 10 03", {"type": "bytes"}) == bytes.fromhex("00 10 03")


def test_identity_faults_coding_and_session():
    ecu = _zf6hp28_ecu()
    c = _client(ecu)
    assert c.open_session(U.SESSION_EXTENDED)[0] == 0x03 and ecu.session == 0x03 and c.session == 0x03
    ident = c.identity()
    assert ident["part"] == "8K0927158C" and ident["sw"] == "0020" and ident["hw_version"] == "H04"
    assert ident["system_name"] == "0B6 20TFSIUSA" and ident["wsc"] == 6197          # from the F1A5 pack
    dtcs = c.read_dtcs()
    assert [d.sae for d in dtcs] == ["P0299", "P1714"]
    assert dtcs[0].status_info["confirmed"] and dtcs[1].status_info["pending"]
    assert dtcs[0].code == "029900"
    # coding read, then write refused until security, then written
    assert c.read_coding() == bytes.fromhex("00 10 02")
    from ovtsuite.diag.uds import Refused
    with pytest.raises(Refused):
        c.write_coding(bytes.fromhex("00 10 03"))
    assert c.security_access("test_key", level=3) is True and ecu.unlocked
    c.write_coding(bytes.fromhex("00 10 03"))
    assert ecu.coding == bytes.fromhex("00 10 03")
    c.clear_dtcs()
    assert ecu.cleared == 1 and c.read_dtcs() == []


def test_tester_present_is_answered():
    ecu = _zf6hp28_ecu()
    c = _client(ecu)
    c.tester_present()                                   # no exception: the module answered 7E 00
    assert ecu.requests[-1] == b"\x3E\x00"


def test_security_access_zero_seed_wrong_key_and_unknown_algorithm():
    ecu = UdsEcu(seed=bytes(4), security_level=3)         # all-zero seed = already open
    c = _client(ecu)
    assert c.security_access("test_key") is True
    ecu2 = UdsEcu(seed=bytes.fromhex("11 22 33 44"), key=bytes.fromhex("AA BB CC DD"))
    c2 = U.UdsClient(DirectUdsLink(ecu2), key_lookup=lambda name, seed: b"\x00\x00\x00\x00")
    assert c2.security_access("anything") is False       # wrong key -> invalidKey -> False
    from ovtsuite.diag.uds import Refused
    c3 = U.UdsClient(DirectUdsLink(ecu2), key_lookup=lambda name, seed: None)
    with pytest.raises(Refused):                          # unknown algorithm: reported, never guessed
        c3.security_access("mystery")


def test_routines_io_control_and_reset():
    ecu = _zf6hp28_ecu()
    c = _client(ecu)
    assert c.routine(0x0203) == b"\x00"
    assert ecu.requests[-1] == bytes.fromhex("31 01 02 03")
    c.io_control(0x1234, U.IO_ADJUST, b"\x01")
    assert ecu.io_state[0x1234] == b"\x01" and ecu.requests[-1] == bytes.fromhex("2F 12 34 03 01")
    c.io_return(0x1234)
    assert ecu.requests[-1] == bytes.fromhex("2F 12 34 00")
    c.ecu_reset(0x01)
    assert ecu.reset_count == 1


def test_dids_named_from_the_definitions():
    ecu = _zf6hp28_ecu()
    from ovtsuite.diag.defs.model import Pack, Doc, Match
    doc = {"kind": "dids", "match": {"addr": "02", "protocol": "uds"}, "dids": [
        {"id": "did.1234", "did": 0x1234, "name": "ATF temperature", "type": "u8",
         "scale": {"kind": "linear", "factor": 1, "offset": -40, "unit": "°C"}, "live": True},
        {"id": "did.2104", "did": 0x2104, "name": "Gear", "type": "u8",
         "scale": {"kind": "table", "table": {"0x4E": "7th"}}}]}
    pack = Pack(id="t", version="0", title="", publisher="", license="", requires={}, depends=[], coverage={},
                path="", origin="user")
    pack.docs.append(Doc(kind="dids", match=Match.from_doc(doc["match"]), body=doc, path="t.yaml"))
    res = D.Resolver([pack])
    ident = D.Identity(addr="02", protocol="uds")
    c = U.UdsClient(DirectUdsLink(ecu), resolver=res, ident=ident)
    r = c.read_did(0x1234)
    assert r.name == "ATF temperature" and r.value == 20.0 and r.text == "20 °C"
    g = c.read_group([0x1234, 0x2104])
    assert [x.text for x in g] == ["20 °C", "7th"]
    assert D.validate_pack(pack).ok


def test_session_uds_path_identify_faults_coding_security():
    """ModuleSession over a UDS link: connect -> identify via DIDs, faults
    via 19 02, long coding read, security access via the named key, long
    coding write after unlock."""
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from mock_uds import FakeIsoIo
    from ovtsuite.diag import link as L
    from ovtsuite.diag.session import ModuleSession
    from ovtsuite.diag.uds import UdsLink

    ecu = _zf6hp28_ecu()
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    row = res.module_address("02", "C6")
    # the key algorithm the definitions name (zf6hp_read) is replaced here so the
    # test does not depend on the real crypto being importable
    link = L.Link(UdsLink(FakeIsoIo(ecu)), [], "uds")
    sess = ModuleSession(link, row, res, generation="C6")
    sess.uds_key_lookup = None
    sess.connect()
    # point the UDS client's key lookup at the test key (the definitions still
    # select it by name; this only supplies the algorithm body for the test)
    sess.uds._key_for = lambda name, seed: ecu.key if name == "zf6hp_read" else None
    assert sess.protocol == "uds" and sess.info.part == "8K0927158C" and sess.info.sw == "0020"
    # the keep-alive the live session sends between operations: 3E 00 with its
    # 7E 00 answer (3E 80 would be unanswered, and silence reads as a dead link)
    sess.keepalive()
    assert ecu.requests[-1] == b"\x3E\x00"
    assert sess.info.coding_bytes == "001002" and sess.info.extra["identification"] == "uds_dids"
    faults = sess.read_faults()
    assert [f.sae for f in faults] == ["P0299", "P1714"] and faults[0].proto == "uds"
    assert faults[0].status_text.startswith("0x08") and "confirmed" in faults[0].status_text
    assert sess.read_long_coding() == bytes.fromhex("00 10 02")
    assert sess.variant("coding") == "uds_0600" and sess.variant("security_access") == "uds_seed_key"
    assert sess.security_access() is True and ecu.unlocked
    # a wrong-length or empty coding is refused before anything reaches the module
    for bad in (b"", bytes.fromhex("00 10"), bytes.fromhex("00 10 03 04")):
        with pytest.raises(ValueError):
            sess.write_long_coding(bad)
    assert ecu.coding == bytes.fromhex("00 10 02")
    assert sess.write_long_coding(bytes.fromhex("00 10 03")) is True and ecu.coding == bytes.fromhex("00 10 03")
    assert sess.info.coding_bytes == "001003"                          # re-identified after the write
    from ovtsuite.diag.session import NotSupported
    with pytest.raises(NotSupported):
        sess.read_group_full(1)                       # UDS has no numbered groups
    sess.clear_faults()
    assert ecu.cleared == 1
    sess.close()
    assert sess.uds is None
