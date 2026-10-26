# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fault paths found from the image (EDC16 DSM tables, EDC15 fault-set
calls), every code of every error type, and the editor window on top."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

from ovtsuite.session import TunerSession  # noqa: E402
from ovtsuite.dtc import DtcTable  # noqa: E402

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
LQ = os.path.join(BIN, "03G906016LQ.bin")
Q = os.path.join(BIN, "038906016Q.Bin")
GC = os.path.join(BIN, "03G906016GC.bin")
TAWI = os.path.join(BIN, "038906016AH_p529tawi.bin")
BD = os.path.join(BIN, "EDC15", "038906012BD.bin")
A15 = os.path.join(BIN, "EDC15", "038906012A.bin")
GQ = os.path.join(BIN, "EDC15", "038906019GQ.bin")


def _need(*paths):
    for p in paths:
        if not os.path.exists(p):
            pytest.skip("%s not present" % os.path.basename(p))


@pytest.fixture(scope="module")
def lq():
    _need(LQ)
    return DtcTable(TunerSession(LQ))


# ---- EDC16: where the tables are --------------------------------------------
def test_lq_tables_come_from_the_pointer_table(lq):
    lay = lq.layout
    assert lay.n == 384 and lay.table_at == 0x4D0A4
    assert (lay.fmk, lay.cdc, lay.cdk, lay.cla) == (
        0x1CEB50, 0x1CECD0, 0x1CF8D0, 0x1D04D0)
    assert lay.encoding == "sae" and lay.agree > 100
    assert all(fp.name_source == "definition" for fp in lq.paths.values())


def test_every_error_type_is_read(lq):
    """Four codes per path, not just the first: AFSCD_SRCAirm's Max/Min are
    P0103/P0102 with customer codes 0x0103/0x0102, ACCD's four VAG codes
    are all different."""
    afs = lq.paths["AFSCD_SRCAirm"]
    assert [t.pcode for t in afs.types[:2]] == ["P0103", "P0102"]
    assert [t.vag for t in afs.types[:2]] == [0x0103, 0x0102]
    assert lq.paths["ACCD"].vag_codes == [5719, 5718, 1605, 5636]
    assert sum(1 for fp in lq.paths.values() if fp.pcodes) >= 170


def test_customer_codes_are_never_replaced(lq):
    """P475 stores the SAE word, so 0x0101 is what the ECU holds for P0101.
    Nothing is swapped for another project's number (the old p350 table)."""
    for fp in lq.paths.values():
        for t in fp.types:
            if t.vag is not None:
                assert t.vag == t.cdk_raw


def test_placeholders_are_not_codes(lq):
    """A type without a customer code holds the path's own DSM number
    (33000 + n on P475); it is not offered as a VAG code, but an ECU read
    of it still finds the path."""
    pan = lq.paths["ACCIRevPanErr"]
    assert pan.own == 476
    assert 33476 not in pan.vag_codes and 33476 in pan.stored_codes
    assert lq.find_code(33476) == [pan]


def test_search_finds_every_form_of_a_code(lq):
    hay = lq.paths["AFSCD_SRCAirm"].search_text()
    for form in ("p0102", "p0103", "00259", "0x0103", "mass air"):
        assert form in hay


def test_p350_stores_the_five_digit_number():
    _need(Q)
    t = DtcTable(TunerSession(Q))
    assert t.layout.encoding == "vag"
    afs = t.paths["AFSCD_SRCAirm"]
    assert afs.vag_codes[:2] == [16487, 16486]          # P0103 / P0102
    # own numbers (n < 1000) are placeholders, not codes
    assert all(c >= 1000 for fp in t.paths.values() for c in fp.vag_codes)
    for fp in t.paths.values():
        assert fp.vag_str == "-" or all(
            len(x) >= 5 and x.isdigit() for x in fp.vag_str.split(", "))


def test_gc_codes_found_without_a_definition_for_them():
    """p475_84's definition places no CDC/CDK; the tables are read anyway."""
    _need(GC)
    t = DtcTable(TunerSession(GC))
    assert t.layout.n == 388
    assert sum(1 for fp in t.paths.values() if fp.vag_codes) > 300


def test_a_definition_on_the_wrong_paths_is_not_used():
    """The P529 TAWI definition is the P475 path list on a different path
    set: its names contradict the paths' own DSM numbers, so the catalogue
    names them instead (BIP01 is DSM 358 on every project)."""
    _need(TAWI)
    t = DtcTable(TunerSession(TAWI))
    assert t.layout.definition_wrong > 100
    assert not any(fp.name_source == "definition" for fp in t.paths.values())
    assert t.paths["BIP01"].own == 358
    assert sum(1 for fp in t.paths.values() if fp.named) > 350


def test_a_structural_path_is_editable_and_undoable():
    """A path the definition does not name still toggles, through a class
    byte the table makes itself."""
    _need(TAWI)
    s = TunerSession(TAWI)
    t = DtcTable(s)
    fp = next(f for f in t.paths.values()
              if not f.named and t.is_enabled(f))
    was = fp.current_class
    t.set_enabled(fp, False)
    assert fp.current_class == 0 and s.binfile.data[fp.cla.addr] == 0
    s.undo()
    assert fp.current_class == was


def test_set_many_is_one_undo_step(lq):
    s = lq.session
    fps = [fp for fp in lq.paths.values() if lq.is_enabled(fp)][:10]
    lq.set_many(fps, False)
    assert not any(lq.is_enabled(fp) for fp in fps)
    s.undo()
    assert all(lq.is_enabled(fp) for fp in fps)


def test_class_one_is_enabled(lq):
    """Class 1 lights no lamp but the fault is stored; only class 0 is off."""
    acc = lq.paths["ACCD"]
    assert acc.current_class == 1 and lq.is_enabled(acc)
    assert not lq.lights_mil(acc)


# ---- EDC15: which path and bit a sub-fault files under ------------------------
def test_edc15_code_follows_the_fault_call_not_the_name():
    """``fbwELDSpR`` is called as path LD1, bit 4: its code is LD1's CC4
    (17964), not anything of LDS."""
    _need(BD)
    t = DtcTable(TunerSession(BD))
    fp = t.paths["Manual:LDS_pR"]
    assert (fp.owner, fp.bit) == ("LD1", 4)
    assert fp.vag_code == 17964
    k = t.paths["Manual:LDS_K"]
    assert (k.owner, k.bit, k.vag_code) == ("LDS", 2, 17954)


def test_edc15_call_mapping_agrees_with_pd_records():
    """038906019GQ stores each record's own code; 038906012A's call sites
    must point at the same codes for the same sub-faults."""
    _need(A15, GQ)
    a = DtcTable(TunerSession(A15))
    g = DtcTable(TunerSession(GQ))
    gq = {fp.name: fp.vag_code for fp in g.paths.values() if fp.vag_code}
    same = diff = 0
    for fp in a.paths.values():
        if fp.owner and fp.vag_code and fp.name in gq:
            same += fp.vag_code == gq[fp.name]
            diff += fp.vag_code != gq[fp.name]
    # different software, three years apart: most codes are unchanged
    assert same > 150 and same > 3 * diff


def test_sae_build_shows_six_digit_vag_codes(lq):
    """LQ stores SAE code words: the VAG column reads as VCDS prints them
    (P0103's word 0x0103 -> 000259), and both that and the old 5-digit
    form still find the path."""
    assert lq.layout.encoding == "sae"
    fp = lq.paths["AFSCD_SRCAirm"]
    assert fp.encoding == "sae"
    assert "000259" in fp.vag_str.split(", ")
    hay = fp.search_text()
    assert "000259" in hay and "00259" in hay


# ---- the window ----------------------------------------------------------------
def test_dialog_search_and_bulk_toggle(lq):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(lq.session)
    d.filter.setText("000259")                # a VAG code as VCDS shows it
    app.processEvents()
    keys = {d.table.item(r, 0).data(0x100) for r in range(d.table.rowCount())}
    assert "AFSCD_SRCAirm" in keys
    d.filter.setText("p0102")
    app.processEvents()
    assert d.table.rowCount() >= 1
    d.table.selectAll()
    sel = d._selected()
    was = {fp.name: fp.current_class for fp in sel}
    d._set_selected(False)
    assert sel and not any(d.dtc.is_enabled(fp) for fp in sel)
    lq.session.undo()                         # the whole switch at once
    assert {fp.name: fp.current_class for fp in sel} == was
    d.close()


# ---- the MIL, apart from the fault -------------------------------------------
def test_mil_off_keeps_the_fault(lq):
    """EGRCD_CBV is class 11 (MIL, stored). MIL off moves it to the class
    closest to its own without the MIL -- class 1 on P475 -- so the fault is
    still stored; MIL on restores class 11 exactly; undo too."""
    s = lq.session
    fp = lq.paths["EGRCD_CBV"]
    assert fp.current_class == 11 and lq.lights_mil(fp)
    assert lq.mil_target(fp, False) == 1
    lq.set_mil(fp, False)
    assert lq.is_enabled(fp) and not lq.lights_mil(fp)
    assert fp.current_class == 1
    lq.set_mil(fp, True)
    assert fp.current_class == 11
    s.undo()
    s.undo()
    assert fp.current_class == 11


def test_mil_keeps_the_system_lamp(lq):
    """A MIL class that also lights the system lamp goes to one that still
    does (class 12 -> class 2 on P475)."""
    c12 = lq.classes[12]
    assert c12.mil and c12.syslamp
    assert lq._closest_class(c12, False) == 2
    assert lq.classes[2].syslamp and not lq.classes[2].mil


def test_mil_needs_an_enabled_path_with_a_known_class(lq):
    off = next(fp for fp in lq.paths.values() if not lq.is_enabled(fp))
    assert not lq.can_set_mil(off) and lq.mil_note(off)


def test_mil_many_is_one_undo_step(lq):
    s = lq.session
    fps = [fp for fp in lq.paths.values() if lq.lights_mil(fp)][:8]
    was = [fp.current_class for fp in fps]
    lq.set_mil_many(fps, False)
    assert not any(lq.lights_mil(fp) for fp in fps)
    assert all(lq.is_enabled(fp) for fp in fps)
    s.undo()
    assert [fp.current_class for fp in fps] == was


def test_edc15_mil_is_the_path_byte_bit():
    """EDC15: bit 1 of fbwS<PATH>PRI, for the whole path in its dataset;
    the defect time (the fault itself) is untouched."""
    _need(BD)
    s = TunerSession(BD)
    t = DtcTable(s)
    fp = t.paths["Manual:LDS_K"]
    assert t.lights_mil(fp) and int(fp.pri.raw_cell(0, 0)) == 130
    scope = t.mil_scope(fp)
    assert fp in scope and t.paths["Manual:LDS_O"] in scope
    t.set_mil(fp, False)
    assert int(fp.pri.raw_cell(0, 0)) == 128
    assert t.is_enabled(fp) and fp.current_class == 48
    assert not t.lights_mil(t.paths["Manual:LDS_O"])
    s.undo()
    assert t.lights_mil(fp)


def test_dialog_mil_box(lq):
    """Unticking a row's MIL box keeps the fault and drops the lamp."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6 import QtCore
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(lq.session)
    d.filter.setText("EGRCD_CBV")
    app.processEvents()
    col = d._cols.index("MIL")
    it = d.table.item(0, col)
    assert it.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable
    it.setCheckState(QtCore.Qt.CheckState.Unchecked)
    app.processEvents()
    fp = d.dtc.paths["EGRCD_CBV"]
    assert d.dtc.is_enabled(fp) and not d.dtc.lights_mil(fp)
    lq.session.undo()
    assert d.dtc.lights_mil(fp)
    d.close()


# ---- the glow-plug lamp (DSM system lamp / EDC15 DIA) --------------------------
def test_glow_lamp_is_the_system_lamp_and_switches_alone(lq):
    """Class 12 lights the MIL and the system (glow-plug) lamp; glow off
    moves it to its twin 11 -- MIL kept, OBD readiness kept -- and back."""
    s = lq.session
    fp = next(f for f in lq.paths.values() if f.current_class == 12)
    assert lq.lights_glow(fp) and lq.lights_mil(fp)
    lq.set_lamp(fp, "sys", False)
    assert fp.current_class == 11
    assert lq.is_enabled(fp) and lq.lights_mil(fp) and not lq.lights_glow(fp)
    lq.set_lamp(fp, "sys", True)
    assert fp.current_class == 12
    s.undo()
    s.undo()
    assert fp.current_class == 12


def test_both_lamps_off_keeps_the_fault(lq):
    s = lq.session
    fp = next(f for f in lq.paths.values() if f.current_class == 12)
    lq.set_lamp(fp, "mil", False)
    lq.set_lamp(fp, "sys", False)
    assert fp.current_class == 1 and lq.is_enabled(fp)
    s.undo()
    s.undo()
    assert fp.current_class == 12


def test_edc15_glow_lamp_is_the_dia_bit():
    _need(BD)
    s = TunerSession(BD)
    t = DtcTable(s)
    fp = next(f for f in t.paths.values()
              if f.pri is not None and int(f.pri.raw_cell(0, 0)) == 134)
    assert t.lights_glow(fp) and t.lights_mil(fp)
    t.set_lamp(fp, "sys", False)
    assert int(fp.pri.raw_cell(0, 0)) == 130 and t.lights_mil(fp)
    s.undo()
    assert t.lights_glow(fp)


# ---- reading the ECU -------------------------------------------------------------
def test_ecu_codes_are_found_and_highlighted(lq, monkeypatch):
    """A code read from the ECU is the stored CDK word: the rows that can
    set it are highlighted and 'Stored on the ECU' shows just them; a code
    no path stores is listed as not found."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui as _g  # noqa: F401  (colours)
    from ovtsuite.flash.diag import Dtc
    from ovtsuite.datalog import gui_log
    from ovtsuite.gui_dtc import DtcDialog, SHOW_ECU

    class FakeRead:
        def __init__(self, *a, **k):
            self.codes = [Dtc(0x0103, 0x24), Dtc(31420, 0x24)]

        def exec(self):
            return 1
    monkeypatch.setattr(gui_log, "EcuCodesDialog", FakeRead)
    d = DtcDialog(lq.session)
    d._read_ecu_codes()
    app.processEvents()
    assert d.show_box.currentText() == SHOW_ECU
    keys = [d.table.item(r, 0).data(0x100) for r in range(d.table.rowCount())]
    # every path that can set 00259, and nothing else (codes can be shared)
    assert "AFSCD_SRCAirm" in keys
    assert set(keys) == {k for k, fp in d.dtc.paths.items()
                         if 0x0103 in fp.stored_codes}
    # a stored fault is light violet (gui_cellmarks), not the comparison's
    # orange: that one means "differs from the comparison"
    from ovtsuite.gui_cellmarks import STORED_BG
    assert d.table.item(0, 1).background().color() == STORED_BG
    assert not d.dtc.find_code(31420)
    # LQ reports SAE words: the unmatched code is written as VCDS does,
    # six digits and the code it decodes to
    assert d._code_encoding() == "sae"
    assert "031420 C3ABC" in d.ecu_note.text()
    d.close()


def test_report_left_open_shows_the_codes_in_fault_paths_later(lq, monkeypatch):
    """The fault code report does not block: when the read window closes the
    editor already has the codes (shaded, the note), not yet the ECU view;
    the report's Show in fault paths, pressed later, switches to it."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui as _g  # noqa: F401  (colours)
    from ovtsuite.flash.diag import Dtc
    from ovtsuite.datalog import gui_log
    from ovtsuite.gui_dtc import DtcDialog, SHOW_ECU
    seen = {}

    class FakeRead:
        def __init__(self, *a, report=None, **k):
            seen["ctx"] = report
            self.codes = [Dtc(0x0103, 0x24)]
            self.show_paths = False          # the report is still open

        def exec(self):
            return 1
    monkeypatch.setattr(gui_log, "EcuCodesDialog", FakeRead)
    d = DtcDialog(lq.session)
    d._read_ecu_codes()
    app.processEvents()
    assert d.show_box.currentText() != SHOW_ECU
    assert 0x0103 in d._ecu_codes and "1 code(s) stored" in d.ecu_note.text()
    seen["ctx"].on_show_paths()              # the report's Show in fault paths
    app.processEvents()
    assert d.show_box.currentText() == SHOW_ECU
    keys = [d.table.item(r, 0).data(0x100) for r in range(d.table.rowCount())]
    assert keys and all(0x0103 in d.dtc.paths[k].stored_codes for k in keys)
    d.close()


def test_search_finds_a_code_by_the_other_vag_numbering(lq):
    """LQ stores the SAE word (0x3000 for P3000); a scan tool on an older
    VW ECU shows the same fault as 19456. Either number finds it."""
    from ovtsuite.dtc import code_forms
    assert "19456" in code_forms("P3000") and "12288" in code_forms("P3000")
    assert "16485" in code_forms("P0101") and "00257" in code_forms("P0101")
    hits = [fp.name for fp in lq.paths.values() if "19456" in fp.search_text()]
    assert hits == ["GlwCD_LampCan"]


def test_every_older_vag_number_finds_its_fault_on_a_newer_ecu(lq):
    """The P350 (038906016Q) stores 5-digit VAG numbers; the LQ the SAE word.
    Every number the P350 stores, for a code the LQ also has, finds it there
    -- not just 19456."""
    _need(Q)
    from ovtsuite.dtc import vag_text
    q = DtcTable(TunerSession(Q))
    where = {}
    for fp in lq.paths.values():
        for t in fp.types:
            if t.vag_pcode:
                where.setdefault(t.vag_pcode, []).append(fp)
    pairs = {(t.vag, t.vag_pcode) for fp in q.paths.values() for t in fp.types
             if t.vag and t.vag_pcode in where}
    assert len(pairs) > 200
    missing = [(n, pc) for n, pc in pairs
               if not any(vag_text(n) in fp.search_text() for fp in where[pc])]
    assert missing == []


def test_fault_code_report_gets_the_editors_paths_and_lookups(lq):
    """The report's rows come from the same lookups as the editor's VAG code
    / OBD-II / Description columns, with each matching path's state."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dtc_report as R
    from ovtsuite.flash.diag import Dtc
    from ovtsuite.gui_dtc import DtcDialog
    d = DtcDialog(lq.session)
    ctx = d._report_context()
    assert ctx.encoding == R.ENC_SAE
    r = R.build_read([Dtc(0x0103, 0x24), Dtc(31420, 0x24)], paths_for=ctx.paths_for,
                     describe=ctx.describe, encoding=ctx.encoding)
    hit, miss = r.codes
    assert (hit.vag, miss.vag, miss.pcode) == ("000259", "031420", "C3ABC")
    fps = [fp for fp in d.dtc.paths.values() if 0x0103 in fp.stored_codes]
    assert sorted(p.name for p in hit.paths) == sorted({fp.dfp for fp in fps})
    assert hit.desc == fps[0].code_desc() and hit.pcode == fps[0].pcode_str
    assert all(p.enabled == d.dtc.is_enabled(fp) for p, fp in zip(hit.paths, fps))
    assert miss.paths == [] and miss.paths_text() == "none in this file"
    d.close()
