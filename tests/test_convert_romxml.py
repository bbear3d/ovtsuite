# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Converter: EcuFlash and RomRaider XML definitions.

The files here are cut down from the community SubaruDefs repository (the
same ROMs kept in both formats); the readers were checked against all of it
(331 ROMs, 53,074 tables: every one agrees between the formats except one
float RomRaider's file declares the wrong way round, and every table writes
and reads back unchanged in both) -- see docs/CONVERTER.md.

Run alone: ``python -m pytest tests/test_convert_romxml.py -q``.
"""

import os
import struct

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import fmt_romxml as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A2L = os.path.join(ROOT, "Reference", "A2L", "BPW.a2l")
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")

BASE = """<!--ECUFLASH SUBARU STANDARD UNITS ECU DEFINITION FILE-->
<rom>
  <romid>
    <xmlid>32BITBASE</xmlid>
    <internalidaddress>2000</internalidaddress>
    <make>Subaru</make>
  </romid>
  <scaling name="%1" units="%" toexpr="x/.84" frexpr="x*.84" format="%.1f" storagetype="float" endian="big" />
  <scaling name="RPM" units="RPM" toexpr="x" frexpr="x" format="%.0f" storagetype="float" endian="big" />
  <scaling name="BoostTarget" units="psi" toexpr="(x-760)*.01933677" frexpr="(x/.01933677)+760" format="%.2f" storagetype="uint16" endian="big" />
  <scaling name="counterthreshold1" units="counter" toexpr="x" frexpr="x" format="%.0f" storagetype="uint16" endian="big" />
  <scaling name="AdvanceU8" units="degrees" toexpr="(x*.3515625)-20" frexpr="(x+20)/.3515625" format="%.2f" storagetype="uint8" endian="big" />
  <scaling name="Patch" storagetype="bloblist" />
  <table name="Target Boost" category="Boost Control - Target" type="3D" level="4" scaling="BoostTarget">
    <table name="Throttle Plate Opening Angle" type="X Axis" elements="8" scaling="%1" />
    <table name="Engine Speed" type="Y Axis" elements="12" scaling="RPM" />
    <description>This map contains the desired boost targets.</description>
  </table>
  <table name="CL to OL Delay" category="Fueling - CL/OL Transition" type="2D" level="2" scaling="counterthreshold1">
    <table name="Delay Values" type="Static Y Axis" elements="9">
      <data>A1</data><data>A2</data><data>A3</data><data>B1</data><data>B2</data>
      <data>B3</data><data>C1</data><data>C2</data><data>C3</data>
    </table>
  </table>
  <table name="Base Timing Idle" category="Ignition Timing" type="1D" scaling="AdvanceU8" />
  <table name="Wastegate Fix" category="Boost Control" type="1D" scaling="Patch" />
</rom>
"""

ROM = """<rom>
  <romid>
    <xmlid>DZ1E402B</xmlid>
    <internalidaddress>2004</internalidaddress>
    <internalidstring>DZ1E402B</internalidstring>
    <make>Subaru</make>
    <model>Tribeca</model>
    <memmodel>SH7058</memmodel>
  </romid>
  <include>32BITBASE</include>
  <table name="Target Boost" address="c90d4">
    <table name="X" address="c906c" />
    <table name="Y" address="c908c" />
  </table>
  <table name="CL to OL Delay" address="c57f6">
    <table name="Y" elements="2" />
  </table>
  <table name="Base Timing Idle" address="c1000" />
  <table name="Wastegate Fix" address="c2000" />
</rom>
"""

ROMRAIDER = """<?xml version="1.0" encoding="UTF-8"?>
<roms>
 <rom>
  <romid><xmlid>32BITBASE</xmlid><make>Subaru</make><memmodel>SH7058</memmodel></romid>
  <table type="3D" name="Target Boost" category="Boost Control - Target" storagetype="uint16" endian="big" sizex="8" sizey="12">
   <scaling units="psi" expression="(x-760)*.01933677" to_byte="(x/.01933677)+760" format="0.00" fineincrement=".01" coarseincrement=".5" />
   <table type="X Axis" name="Throttle Plate Opening Angle" storagetype="float" endian="little">
    <scaling units="%" expression="x/.84" to_byte="x*.84" format="0.0" fineincrement=".3" coarseincrement="1" />
   </table>
   <table type="Y Axis" name="Engine Speed" storagetype="float" endian="little">
    <scaling units="RPM" expression="x" to_byte="x" format="#" fineincrement="50" coarseincrement="100" />
   </table>
  </table>
  <table type="2D" name="CL to OL Delay" category="Fueling - CL/OL Transition" storagetype="uint16" endian="big" sizey="9">
   <scaling units="counter" expression="x" to_byte="x" format="#" fineincrement="1" coarseincrement="100" />
   <table type="Static Y Axis" name="Delay Values" sizey="9">
    <data>A1</data><data>A2</data><data>A3</data><data>B1</data><data>B2</data>
    <data>B3</data><data>C1</data><data>C2</data><data>C3</data>
   </table>
  </table>
  <table type="Switch" name="Wastegate Fix" category="Boost Control" sizey="2">
   <state name="on" data="42 C8" /><state name="off" data="41 F0" />
  </table>
 </rom>
 <rom base="32BITBASE">
  <romid><xmlid>DZ1E402B</xmlid><internalidaddress>2004</internalidaddress>
   <internalidstring>DZ1E402B</internalidstring><make>Subaru</make><memmodel>SH7058</memmodel></romid>
  <table name="Target Boost" storageaddress="0xC90D4">
   <table type="X Axis" storageaddress="0xC906C" />
   <table type="Y Axis" storageaddress="0xC908C" />
  </table>
  <table name="CL to OL Delay" storageaddress="0xC57F6" sizey="2" />
  <table name="Wastegate Fix" storageaddress="0xC2000" />
 </rom>
 <rom base="32BITBASE">
  <romid><xmlid>A2WC500R</xmlid><internalidaddress>2004</internalidaddress>
   <internalidstring>A2WC500R</internalidstring><make>Subaru</make></romid>
  <table name="CL to OL Delay" storageaddress="0xC5000" sizey="2" />
 </rom>
</roms>
"""


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


def _metadata(tmp_path):
    """EcuFlash's metadata folder: the ROM in a model folder, the base in
    ``Bases``."""
    (tmp_path / "subaru" / "Bases").mkdir(parents=True)
    (tmp_path / "subaru" / "Tribeca").mkdir()
    (tmp_path / "subaru" / "Bases" / "32BITBASE.xml").write_text(BASE)
    rom = tmp_path / "subaru" / "Tribeca" / "DZ1E402B.xml"
    rom.write_text(ROM)
    return str(rom)


def test_sniff():
    assert C.sniff(ROM.encode()) == ("definition", "ecuflash")
    assert C.sniff(BASE.encode()) == ("definition", "ecuflash")
    assert C.sniff(ROMRAIDER.encode()) == ("definition", "romraider")


def test_ecuflash_rom_with_its_base(tmp_path):
    path = _metadata(tmp_path)
    d = C.load_definition(open(path, "rb").read(), path=path)
    t = {x.name: x for x in d.tables}
    assert set(t) == {"Target Boost", "CL to OL Delay", "Base Timing Idle"}
    assert any("bloblist" in w for w in d.warnings)
    tb = t["Target Boost"]
    assert (tb.kind, tb.rows, tb.cols, tb.order) == ("map", 12, 8, "row")
    assert tb.z.addr == 0xC90D4 and tb.z.bits == 16 and tb.z.endian == ">"
    assert tb.x.values.addr == 0xC906C and tb.x.values.float and tb.x.values.endian == ">"
    assert tb.y.values.addr == 0xC908C and tb.x.count == 8
    assert abs(tb.z.factor - .01933677) < 1e-12 and abs(tb.z.offset + 760 * .01933677) < 1e-9
    # the ROM's "Y" override sets the static axis' size; its texts are cut to it
    cl = t["CL to OL Delay"]
    assert cl.cols == 2 and cl.x.labels == ["A1", "A2"]
    assert t["Base Timing Idle"].z.bits == 8 and t["Base Timing Idle"].z.endian == ""
    assert d.info["rom_internalidstring"] == "DZ1E402B"


def test_ecuflash_without_its_base_says_what_is_missing(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    p = tmp_path / "a" / "b" / "DZ1E402B.xml"
    p.write_text(ROM)
    with pytest.raises(C.ConvertError, match="32BITBASE.xml"):
        C.load_definition(p.read_bytes(), path=str(p))


def test_romraider_legacy_float_rule_and_base_chain():
    d = C.load_definition(ROMRAIDER.encode(), rom="DZ1E402B")
    t = {x.name: x for x in d.tables}
    assert set(t) == {"Target Boost", "CL to OL Delay"}       # the Switch is reported
    assert any("Switch" in w for w in d.warnings)
    tb = t["Target Boost"]
    # endian="little" on a float in a legacy file is big-endian
    assert tb.x.values.endian == ">" and tb.y.values.endian == ">" and tb.z.endian == ">"
    assert (tb.rows, tb.cols) == (12, 8) and tb.x.values.addr == 0xC906C
    assert t["CL to OL Delay"].cols == 2 and t["CL to OL Delay"].x.labels == ["A1", "A2"]


def test_romraider_rom_found_from_the_binary():
    img = bytearray(0x10000)
    img[0x2004:0x200C] = b"A2WC500R"
    d = C.load_definition(ROMRAIDER.encode(), image=bytes(img))
    assert d.info["rom_xmlid"] == "A2WC500R"
    assert [x.z.addr for x in d.tables] == [0xC5000]
    with pytest.raises(C.ConvertError, match="ROMs"):
        C.load_definition(ROMRAIDER.encode())


def test_both_formats_say_the_same(tmp_path):
    path = _metadata(tmp_path)
    a = C.load_definition(open(path, "rb").read(), path=path)
    b = C.load_definition(ROMRAIDER.encode(), rom="DZ1E402B")
    for n in ("Target Boost", "CL to OL Delay"):
        x = next(t for t in a.tables if t.name == n)
        y = next(t for t in b.tables if t.name == n)
        assert (x.kind, x.rows, x.cols, x.z.addr, x.z.bits, x.z.endian) == \
            (y.kind, y.rows, y.cols, y.z.addr, y.z.bits, y.z.endian)
        assert x.z.equation_text() == y.z.equation_text()


@pytest.mark.parametrize("target", ["ecuflash", "romraider", "xdf", "a2l"])
def test_ecuflash_converts_and_reads_back(tmp_path, target):
    path = _metadata(tmp_path)
    res = C.run(C.Job(sources=[path], target=target,
                      out=str(tmp_path / ("o" + C.EXT[target])), size=0x100000))
    assert res.check == [], res.check
    assert res.tables_out == 3


@pytest.mark.parametrize("target", ["ecuflash", "romraider"])
def test_a2l_to_rom_xml(tmp_path, target):
    """A whole EDC16 A2L (little-endian words, maps column-major) written as
    EcuFlash / RomRaider XML reads back table for table."""
    if not (os.path.isfile(A2L) and os.path.isfile(LQ)):
        pytest.skip("reference file missing")
    out = str(tmp_path / ("bpw" + C.EXT[target]))
    res = C.run(C.Job(sources=[A2L], binary=LQ, target=target, out=out))
    assert res.check == []
    assert res.tables_out > 10000
    back = C.load_definition(open(out, "rb").read())
    assert back.byte_order() == ">"                 # BPW.a2l: BYTE_ORDER MSB_FIRST
    # EDC16 maps are column-major: written with swapxy, read back so
    assert any(t.kind == "map" and t.order == "col" for t in back.tables)
    assert any("id string" in w for w in res.warnings)


def test_mixed_byte_orders_survive_romraider(tmp_path):
    """One float stored little-endian among big-endian tables: RomRaider
    applies a stated memory-model order to every table, so none is stated
    and each table carries its own (a float's the legacy way round)."""
    from ovtsuite.definitions.model import Definition, Storage, TableDef
    d = Definition(type="json", tables=[
        TableDef(name="Big", kind="constant", z=Storage(addr=0x10, bits=16, endian=">")),
        TableDef(name="LittleFloat", kind="constant",
                 z=Storage(addr=0x20, bits=32, float=True, endian="<"))])
    text, n = R.dumps_romraider(d)
    assert "<memmodel>" in text and n == 2
    back = {t.name: t for t in R.load_romraider(text.encode()).tables}
    assert back["Big"].z.endian == ">" and back["LittleFloat"].z.endian == "<"
    # and one order for everything is stated once
    d.tables = d.tables[:1]
    assert '<memmodel endian="big">' in R.dumps_romraider(d)[0]


def test_values_through_rom_xml(tmp_path):
    """A float axis and a uint16 map read through the EcuFlash definition
    from a binary laid out the way it says."""
    path = _metadata(tmp_path)
    img = bytearray(0x100000)
    for i in range(8):
        struct.pack_into(">f", img, 0xC906C + 4 * i, 0.84 * 10 * i)    # 0, 10 ... 70 %
    for i in range(12):
        struct.pack_into(">f", img, 0xC908C + 4 * i, 500.0 * (i + 1))
    for r in range(12):
        for c in range(8):
            struct.pack_into(">H", img, 0xC90D4 + 2 * (r * 8 + c), 760 + r * 8 + c)
    b = tmp_path / "rom.bin"
    b.write_bytes(bytes(img))
    out = str(tmp_path / "v.dcm")
    C.run(C.Job(sources=[path], binary=str(b), target="dcm", out=out))
    from ovtsuite.convert import calvalues as V
    # a DCM name is one token: "Target Boost" is written Target_Boost
    tb = {p.name: p for p in V.load(open(out, "rb").read()).params}["Target_Boost"]
    # stored as float32, so 0.84*10*i comes back to within float32 precision
    assert all(abs(a - e) < 1e-5 for a, e in zip(tb.x[:3], [0.0, 10.0, 20.0]))
    assert tb.y[:2] == [500.0, 1000.0]
    # row-major: the second stored value is row 0, column 1
    assert abs(tb.z[1] - 1 * .01933677) < 1e-9 and abs(tb.z[8] - 8 * .01933677) < 1e-9
    # and the values go back in by that name
    blank = tmp_path / "blank.bin"
    blank.write_bytes(bytes(0x100000))
    res = C.run(C.Job(sources=[path, out], binary=str(blank), target="bin",
                      out=str(tmp_path / "back.bin")))
    assert res.check == [] and not [k for k in res.loss if "Target" in k]
    assert (tmp_path / "back.bin").read_bytes()[0xC906C:0xC9214] == bytes(img[0xC906C:0xC9214])


# ------------------------------------------------- choosing a RomRaider ROM
def _rr_files(tmp_path, idstr=b"A2WC500R"):
    defs = tmp_path / "ecu_defs.xml"
    defs.write_text(ROMRAIDER)
    img = bytearray(0x10000)
    img[0x2004:0x2004 + len(idstr)] = idstr
    b = tmp_path / "car.bin"
    b.write_bytes(bytes(img))
    return str(defs), str(b)


def test_default_rom():
    from ovtsuite.convert import fmt_romxml as R
    # three ROMs, two of which place tables: it has to be named or found
    assert R.default_rom(ROMRAIDER.encode()) is None
    one = ROMRAIDER.replace('<table name="CL to OL Delay" storageaddress="0xC5000" '
                            'sizey="2" />', "")
    assert R.default_rom(one.encode()) == "DZ1E402B"


def test_cli_lists_roms_and_marks_the_binary(tmp_path, capsys):
    from ovtsuite.cli import main
    defs, b = _rr_files(tmp_path)
    assert main(["convert", defs, "--list-roms", "--bin", b]) == 0
    out = capsys.readouterr().out
    assert "3 ROMs" in out and "this binary: A2WC500R" in out
    assert [l for l in out.splitlines() if l.startswith(" *")][0].split()[1] == "A2WC500R"


def test_cli_rom_names_the_rom(tmp_path):
    from ovtsuite.cli import main
    defs, b = _rr_files(tmp_path, idstr=b"NOTAROM!")
    out = tmp_path / "dz.json"
    # the binary is none of them: without --rom it stops, with it the ROM is used
    assert main(["convert", defs, "--bin", b, "--to", "json", "-o", str(out)]) == 1
    assert main(["convert", defs, "--bin", b, "--rom", "DZ1E402B", "--to", "json",
                 "-o", str(out), "--allow-loss"]) == 0
    d = C.load_definition(out.read_bytes())
    assert sorted(t.z.addr for t in d.tables) == [0xC57F6, 0xC90D4]


def test_ai_tools_list_and_choose_the_rom(tmp_path):
    from ovtsuite.ai.tools import Context, Registry
    from ovtsuite.convert import ai_tools
    reg = Registry()
    ai_tools.register(reg)
    defs, b = _rr_files(tmp_path)
    ok, r = reg.call("inspect_file_format", Context(None), {"path": defs, "binary": b})
    assert ok, r
    assert r["format"] == "romraider" and r["rom_count"] == 3
    assert [x["xmlid"] for x in r["roms"]] == ["32BITBASE", "DZ1E402B", "A2WC500R"]
    assert r["binary_is"] == "A2WC500R"
    out = str(tmp_path / "dz.json")
    ok, r = reg.call("convert_files", Context(None),
                     {"sources": [defs], "target": "json", "out": out, "rom": "DZ1E402B"})
    assert ok, r
    assert r["tables_out"] == 2
