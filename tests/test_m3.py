# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""M3 tests: tiers, session/model editing, XDF export, GUI flows."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def test_tiers_partition():
    from ovtsuite import kb
    from ovtsuite.tiers import tier_of
    k = kb.load("p350") if "p350" in kb.available() else None
    if k is None:
        pytest.skip("no KB")
    from ovtsuite import groups as G
    tiers = {tier_of(t.name, t.description) for t in k.tables}
    assert tiers <= set(G.GROUP_NAMES)
    # module membership (the catalogue lifts the headline maps into Power)
    assert tier_of("EngPrt_trqLimP_MAP") == G.OTHER_ENGINE
    assert tier_of("PCR_pBDesBas_MAP") == G.OTHER_ENGINE
    assert tier_of("DSM_CDCDfp_VSACD_Max_C") == G.DTC
    # emissions modules: only the catalogue's EGR/DPF/swirl maps stay in
    # Emissions; regen variants and the rest are supporting maps
    assert tier_of("AirCtl_rCtlRgn1a_MAP") == G.OTHER_ENGINE
    assert tier_of("PCR_pBDesRgn1a_MAP") == G.OTHER_ENGINE
    # ...and the baked catalogue does
    by = {t.name: t for t in k.tables}
    assert by["EngPrt_trqLimP_MAP"].cat["g"] == G.POWER
    assert by["PCR_pBDesMaxAP_MAP"].cat["k"] == "Boost Limiter"


@needs_ref
def test_session_loads_and_tiers():
    from ovtsuite.session import TunerSession
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    # Q is a U8A7 build: it routes to p350's exact U8A7 sub-family
    assert s.family == "p350_u8a7"
    assert len(s.entities) > 5000
    from ovtsuite import groups as G
    assert all(e.tier in G.GROUP_NAMES for e in s.entities)
    power = [e for e in s.entities if e.tier == G.POWER]
    assert 20 < len(power) < 80          # the maps a tune is made of


@needs_ref
def test_oversized_dims_use_stored_counts():
    from ovtsuite.session import TunerSession
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    e = next(x for x in s.entities if x.name == "EngPrt_trqLimP_MAP")
    # declared 25x4 reserved; stored 20x3
    assert (e.rows, e.cols) == (20, 3)
    assert (e.declared_rows, e.declared_cols) == (25, 4)


@needs_ref
def test_edit_undo_redo_roundtrip():
    from ovtsuite.session import TunerSession
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    e = next(x for x in s.entities if x.name == "EngPrt_trqLimP_MAP")
    before = e.cell(8, 0)
    s.edit_cells(e, [(8, 0, before + 25)])
    assert abs(e.cell(8, 0) - (before + 25)) < 1.0
    assert s.modified
    s.undo()
    assert e.cell(8, 0) == before
    assert not s.modified
    s.redo()
    assert abs(e.cell(8, 0) - (before + 25)) < 1.0


@needs_ref
def test_column_major_map_indexing():
    """A boost map should climb monotonically with rpm along a column."""
    from ovtsuite.session import TunerSession
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    e = next(x for x in s.entities if x.name == "PCR_pBDesBas_MAP")
    vals = e.values()
    # last row (highest load) should have higher boost than first row
    assert sum(vals[-1]) > sum(vals[0])


@needs_ref
def test_export_roundtrips_corrected_dims():
    from ovtsuite.session import TunerSession
    from ovtsuite.xdf_export import export_xdf
    from ovtsuite.xdf import parse_xdf
    import tempfile
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    xml = export_xdf(s.entities, "test", "038906016Q")
    with tempfile.NamedTemporaryFile("w", suffix=".xdf", delete=False) as fh:
        fh.write(xml)
        path = fh.name
    d = parse_xdf(path)
    os.unlink(path)
    t = next(x for x in d.tables if x.name == "EngPrt_trqLimP_MAP")
    assert (t.rows, t.cols) == (20, 3)          # not the reserved 25x4
    # task-group category present as first membership
    assert "Power" in t.categories


@needs_ref
def test_save_writes_edits():
    from ovtsuite.session import TunerSession
    from ovtsuite.binary import BinaryFile
    import tempfile
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    e = next(x for x in s.entities if x.name == "EngPrt_trqLimP_MAP")
    s.edit_cells(e, [(8, 0, e.cell(8, 0) + 30)])
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
        out = fh.name
    s.save(out)
    reloaded = BinaryFile.load(out)
    os.unlink(out)
    assert reloaded.read(e.addr + e.index(8, 0) * e.step, e.bits,
                         e.signed) == s.binfile.read(
                             e.addr + e.index(8, 0) * e.step, e.bits, e.signed)


@needs_ref
def test_gui_flows_offscreen():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets, QtCore
    except Exception:
        pytest.skip("PySide6 not installed")
    from ovtsuite.gui import MainWindow
    from ovtsuite.session import TunerSession
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    prim = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    comp = TunerSession(os.path.join(BIN, "038906016AB.bin"))
    w = MainWindow(prim, comp)
    w.show()
    app.processEvents()
    from ovtsuite import groups as G
    assert w.tree.topLevelItemCount() == len(
        {e.tier for e in prim.entities + prim.variant_entities()})
    assert w.tree.topLevelItem(0).text(0).startswith(
        G.GROUP_NAMES[G.POWER])                # Power first

    def find(item):
        for i in range(item.childCount()):
            c = item.child(i)
            e = c.data(0, QtCore.Qt.UserRole)
            if e is not None and e.name == "EngPrt_trqLimP_MAP":
                return c
            r = find(c)
            if r:
                return r
        return None
    node = None
    for i in range(w.tree.topLevelItemCount()):
        node = find(w.tree.topLevelItem(i))
        if node:
            break
    assert node is not None
    w.tree.setCurrentItem(node)
    app.processEvents()
    m = w.model
    # the grid carries the X band as row 0 and the Y band as column 0, and
    # with a comparison loaded its values follow under a banner row
    assert m._cmp                              # comparison highlighting active
    # (the comparison block repeats the X band under its banner)
    assert (m.rowCount(), m.columnCount()) == (2 * 20 + 1 + 2 * m._roff,
                                               3 + m._coff)
    sep = m.sep_row()
    assert sep == 20 + m._roff
    assert "Comparison - 038906016AB.bin" in m.data(m.index(sep, 0), QtCore.Qt.DisplayRole)
    assert w.table.rowSpan(sep, 0) == 1 and w.table.columnSpan(sep, 0) == m.columnCount()
    assert not (m.flags(m.index(sep + 1, m._coff)) & QtCore.Qt.ItemIsEditable)
    idx = m.index(8 + m._roff, m._coff)
    m.setData(idx, "277", QtCore.Qt.EditRole)
    assert prim.modified
    w.undo()
    assert not prim.modified
