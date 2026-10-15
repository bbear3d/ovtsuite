# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The parameter tree's right-click menu, a parameter's Properties window,
the hex editor's range selection, and user-defined parameters
(ovtsuite.userparams, ovtsuite.gui_paramenu).

Built on a synthetic image (no reference binaries needed): its parameters
are user parameters, which is also what exercises them end to end. The
model and storage tests are fast; the window tests are marked slow.
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ovtsuite import userparams as up  # noqa: E402

SIZE = 0x10000
Z, X, Y = 0x1000, 0x2000, 0x2100


def _image():
    data = bytearray(SIZE)
    for i in range(64):
        struct.pack_into(">H", data, Z + 2 * i, i * 10)
    for i in range(8):
        struct.pack_into(">H", data, X + 2 * i, 1000 + i * 500)
        struct.pack_into(">H", data, Y + 2 * i, i * 100)
    struct.pack_into(">H", data, 0x3000, 1234)
    return data


def _map_spec(name="MyMap", **kw):
    s = dict(up.blank(), name=name, title="My boost map", addr=Z, rows=8,
             cols=8, equation="X*0.1", units="hPa",
             x={"addr": X, "bits": 16, "equation": "X", "units": "rpm"},
             y={"addr": Y, "bits": 16, "equation": "X", "units": "mg"})
    s.update(kw)
    return s


def _value_spec():
    return dict(up.blank("constant"), name="MyLimit", addr=0x3000,
                equation="X", units="rpm")


def _session(tmp_path, specs=None):
    from ovtsuite.session import TunerSession
    p = tmp_path / "t.bin"
    p.write_bytes(bytes(_image()))
    s = TunerSession(str(p))
    up.apply(s, specs if specs is not None else [_map_spec(), _value_spec()])
    return s


# ---------------------------------------------------------------- model
def test_validate_and_entity(tmp_path):
    s = _session(tmp_path, [])
    assert up.validate(up.normalise(_map_spec()), SIZE) == []
    errs = up.validate(up.normalise(_map_spec(addr=SIZE - 4)), SIZE)
    assert any("outside the binary" in x for x in errs)
    assert up.validate(up.normalise(_map_spec(name="has space")), SIZE)
    assert up.validate(up.normalise(_map_spec(equation="import os")), SIZE)
    assert up.validate(up.normalise(_map_spec()), SIZE, taken={"MyMap"})
    bad_axis = _map_spec(x={"values": [1, 2, 3]})
    assert any("3 values given" in x
               for x in up.validate(up.normalise(bad_axis), SIZE))

    e = up.put(s, _map_spec())
    assert e.source == "user" and up.is_user(e)
    assert e.values()[1][:3] == [8.0, 9.0, 10.0]
    assert e.x_values()[:3] == [1000.0, 1500.0, 2000.0]
    assert e.y_values()[:2] == [0.0, 100.0]
    # an ordinary, undoable edit
    s.edit_cells(e, [(0, 0, 5.0)])
    assert e.cell(0, 0) == 5.0
    s.undo()
    assert e.cell(0, 0) == 0.0


def test_put_replace_remove(tmp_path):
    s = _session(tmp_path)
    assert {e.name for e in s.entities} == {"MyMap", "MyLimit"}
    up.put(s, _map_spec(name="Renamed"), replace="MyMap")
    assert {e.name for e in s.entities} == {"Renamed", "MyLimit"}
    assert ("user", "Renamed", Z) in s.by_key
    assert up.remove(s, "MyLimit")
    assert [e.name for e in s.entities] == ["Renamed"]
    assert not up.remove(s, "nothing")
    # a spec that does not fit this image is kept but not shown
    up.apply(s, [_map_spec(), _map_spec(name="Far", addr=SIZE)])
    assert [e.name for e in s.entities] == ["MyMap"]
    assert len(up.specs(s)) == 2


def test_fixed_axis_curve_and_preview(tmp_path):
    s = _session(tmp_path, [])
    spec = dict(up.blank("curve"), name="C", addr=Z, cols=4,
                x={"values": [0, 10, 20, 30], "units": "%"})
    e = up.put(s, spec)
    assert e.kind == "curve" and e.rows == 1 and e.x.addr is None
    assert e.x_values() == [0.0, 10.0, 20.0, 30.0]
    p = up.preview(_map_spec(), s.binfile, limit=4)
    assert len(p["z"]) == 4 and len(p["z"][0]) == 4
    assert p["range"] == (0.0, 63.0)


def test_project_keeps_them(tmp_path):
    from ovtsuite.project.project import Project
    proj = Project("p")
    st = proj.add_set("ECU")
    assert proj.user_params(st.id) == []
    proj.set_user_params(st.id, [_map_spec()])
    assert proj.modified_since_save
    back = Project.from_bytes(proj.to_bytes())
    got = back.user_params(st.id)
    assert [x["name"] for x in got] == ["MyMap"] and got[0]["addr"] == Z
    back.set_user_params(st.id, [])
    assert back.user_params(st.id) == []


def test_snapshot_leaves_them_out():
    from ovtsuite import defn
    assert "user" in defn._ADDED_ON_OPEN and "definition" in defn._ADDED_ON_OPEN


def test_opening_a_project_set_adds_them(tmp_path):
    pytest.importorskip("PySide6")
    from ovtsuite import gui_project
    from ovtsuite.project.importers import import_bytes
    from ovtsuite.project.project import Project
    from ovtsuite.session import TunerSession
    proj = Project("p")
    st = import_bytes(proj, bytes(_image()), "t.bin")
    proj.set_user_params(st.id, [_map_spec()])
    p = tmp_path / "t.bin"
    p.write_bytes(bytes(_image()))
    sess = TunerSession(str(p))
    gui_project._apply_definition(proj, st.id, sess)
    assert [e.name for e in sess.entities if up.is_user(e)] == ["MyMap"]


# ------------------------------------------------------------------ GUI
def _qt():
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError as exc:                          # no Qt
        pytest.skip("PySide6 is not usable here: %s" % exc)
    return QtCore, QtWidgets


@pytest.fixture
def win(tmp_path, isolated_qsettings, monkeypatch):
    QtCore, QtWidgets = _qt()
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a) or 0))
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    s = _session(tmp_path)
    s.support = "definition"            # a binary its definitions describe
    w = gui.MainWindow(s)
    w.resize(1100, 700)
    w.show()
    QtWidgets.QApplication.processEvents()
    w._told = told
    yield w
    w.close_discarding()


def _texts(menu):
    return [a.text() for a in menu.actions() if a.text()]


def _sub(menu, title):
    return next(a.menu() for a in menu.actions()
                if a.menu() is not None and a.text() == title)


def _ent(ws, name):
    return next(e for e in ws.primary.entities if e.name == name)


@pytest.mark.slow
def test_leaf_and_heading_menus(win):
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    e = _ent(ws, "MyMap")
    item = ws._find_tree_item(e)
    assert item is not None
    m = pm.build_menu(ws, item)
    t = _texts(m)
    for want in ("Open", "Open in a new table tab", "Keep open",
                 "Show in hex editor", "Properties...", "Copy", "Show 3D surface",
                 "Add parameter...", "Edit user parameter...",
                 "Delete user parameter", "Ask AI: explain this parameter"):
        assert want in t, want
    assert "Copy from comparison" not in t          # no comparison loaded
    hx = _texts(_sub(m, "Show in hex editor"))
    assert [x.split("  ")[0] for x in hx] == ["Map data", "X axis", "Y axis"]
    cp = _texts(_sub(m, "Copy"))
    assert [x.split("  ")[0] for x in cp] == ["Symbol", "Title", "Address",
                                             "Values", "Properties"]
    # a single value: no axes, no 3-D surface
    v = pm.build_menu(ws, ws._find_tree_item(_ent(ws, "MyLimit")))
    assert [x.split("  ")[0] for x in _texts(_sub(v, "Show in hex editor"))] == ["Value"]
    assert not next(a for a in v.actions() if a.text() == "Show 3D surface").isEnabled()
    # a heading
    head = item.parent()
    hm = pm.build_menu(ws, head)
    assert _texts(hm)[:2] == ["Expand all", "Collapse all"]
    top = head.parent()
    top.setExpanded(False)
    head.setExpanded(False)
    next(a for a in pm.build_menu(ws, top).actions()
         if a.text() == "Expand all").trigger()
    assert top.isExpanded() and head.isExpanded()


@pytest.mark.slow
def test_menu_actions_open_and_copy(win):
    QtCore, QtWidgets = _qt()
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    e = _ent(ws, "MyMap")
    m = pm.build_menu(ws, ws._find_tree_item(e))
    next(a for a in m.actions() if a.text() == "Open in a new table tab").trigger()
    assert ws.model.entity is e
    cp = _sub(m, "Copy")
    clip = QtWidgets.QApplication.clipboard()
    next(a for a in cp.actions() if a.text().startswith("Address")).trigger()
    assert clip.text() == "0x001000"
    next(a for a in cp.actions() if a.text() == "Symbol").trigger()
    assert clip.text() == "MyMap"
    tsv = pm.values_tsv(e)
    lines = tsv.split("\n")
    assert lines[0].split("\t")[:3] == ["", "1000", "1500"]
    assert lines[2].split("\t")[:3] == ["100", "8", "9"]


@pytest.mark.slow
def test_show_in_hex_selects_the_range(win):
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    e = _ent(ws, "MyMap")
    dlg = pm.show_in_hex(ws, e.addr, e.end)
    assert dlg is not None and ws.tool("hex") is dlg
    sel = dlg._selected_addrs()
    assert sel[0] == Z and sel[-1] == Z + 127 and len(sel) == 128
    # an axis, reusing the open editor
    assert pm.show_in_hex(ws, X, X + 16) is dlg
    assert dlg._selected_addrs() == list(range(X, X + 16))
    assert "0x002000" in dlg.info.text()


@pytest.mark.slow
def test_properties_window(win):
    QtCore, QtWidgets = _qt()
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    e = _ent(ws, "MyMap")
    p = pm.properties(ws, e)
    gen = dict(p["general"])
    assert gen["Title"] == "My boost map" and gen["Symbol"] == "MyMap"
    assert gen["Group"] == "User defined"
    parts = [(r["part"], r["start"], r["end"], r["bytes"]) for r in p["storage"]]
    assert parts == [("Values", Z, Z + 128, 128), ("X axis", X, X + 16, 16),
                     ("Y axis", Y, Y + 16, 16)]
    assert p["storage"][0]["equation"] == "X*0.1"
    assert "16-bit unsigned, big-endian" in p["storage"][0]["format"]
    vals = dict(p["values"])
    assert vals["Values now"].startswith("0 to 63")
    d = pm.show_properties(ws, e)
    assert isinstance(d, pm.ParameterPropertiesDialog) and not d.isModal()
    assert d.table.rowCount() == 3 and len(d.hex_buttons) == 3
    d.hex_buttons[1].click()
    assert ws.tool("hex")._selected_addrs()[0] == X
    d.copy_all()
    text = QtWidgets.QApplication.clipboard().text()
    assert "0x001000-0x00107F" in text and "MyMap" in text and "X*0.1" in text
    d.close()


@pytest.mark.slow
def test_comparison_items(win, tmp_path):
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    data = _image()
    for i in range(64):
        struct.pack_into(">H", data, Z + 2 * i, 500)
    cp_path = tmp_path / "c.bin"
    cp_path.write_bytes(bytes(data))
    from ovtsuite.session import TunerSession
    c = TunerSession(str(cp_path))
    up.apply(c, up.specs(ws.primary))
    ws.set_comparison(c)
    e = _ent(ws, "MyMap")
    m = pm.build_menu(ws, ws._find_tree_item(e))
    t = _texts(m)
    assert "Show differences..." in t
    cc = next(a for a in m.actions() if a.text() == "Copy from comparison")
    assert cc.isEnabled()
    cc.trigger()
    assert e.cell(3, 3) == 50.0
    ws.primary.undo()
    assert e.cell(0, 0) == 0.0
    vals = dict(pm.properties(ws, e)["values"])
    assert "values differ" in vals["Comparison"]


@pytest.mark.slow
def test_view_only_greys_out_changes(win):
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    ws.view_only = {"id": "x"}
    try:
        m = pm.build_menu(ws, ws._find_tree_item(_ent(ws, "MyMap")))
        assert not _sub(m, "Show in hex editor").isEnabled()
        for name in ("Add parameter...", "Edit user parameter...",
                     "Delete user parameter"):
            assert not next(a for a in m.actions() if a.text() == name).isEnabled()
        assert pm.add_parameter(ws) is None
    finally:
        ws.view_only = None


@pytest.mark.slow
def test_user_param_dialog_and_tree(win, monkeypatch):
    QtCore, QtWidgets = _qt()
    from ovtsuite import gui_paramenu as pm
    ws = win.current
    d = pm.UserParamDialog(ws)
    ok = d.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()                    # nothing filled in yet
    d.name.setText("Boost2")
    d.addr.setText("FFF0")                       # runs off the end
    d.rows.setValue(8)
    d.cols.setValue(8)
    assert not ok.isEnabled() and "outside the binary" in d.errors.text()
    d.addr.setText("1000")
    d.eq.setText("X*0.1")
    assert ok.isEnabled(), d.errors.text()
    assert d.preview.rowCount() == 8 and d.preview.item(1, 0).text() == "8"
    d.name.setText("MyMap")                      # taken
    assert not ok.isEnabled()
    d.name.setText("Boost2")
    d.xbox.mode.setCurrentIndex(2)               # fixed values
    d.xbox.values.setText("1, 2, 3")
    assert "3 values given" in d.errors.text()
    d.xbox.values.setText("1 2 3 4 5 6 7 8")
    assert ok.isEnabled()
    spec = d.spec()
    assert spec["x"] == {"values": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
                         "units": ""}

    # Add parameter... with that form accepted
    monkeypatch.setattr(pm.UserParamDialog, "exec",
                        lambda self: (self._set(spec),
                                      QtWidgets.QDialog.DialogCode.Accepted)[1])
    e = pm.add_parameter(ws)
    assert e is not None and e.name == "Boost2"
    item = ws._find_tree_item(e)
    assert item is not None
    top = item
    while top.parent() is not None:
        top = top.parent()
    assert top.data(0, QtCore.Qt.UserRole + 1) == "User defined"
    assert ws.model.entity is e
    # edit it (renamed), then delete it
    spec2 = dict(spec, name="Boost3")
    monkeypatch.setattr(pm.UserParamDialog, "exec",
                        lambda self: (self._set(spec2),
                                      QtWidgets.QDialog.DialogCode.Accepted)[1])
    n = pm.edit_parameter(ws, e)
    assert n.name == "Boost3"
    assert "Boost2" not in {x.name for x in ws.primary.entities}
    assert pm.delete_parameter(ws, n, confirm=False)
    assert "Boost3" not in {x.name for x in ws.primary.entities}
    assert ws._find_tree_item(n) is None
