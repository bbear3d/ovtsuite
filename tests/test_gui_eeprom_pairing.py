# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An ECU EEPROM in the main window: it opens in the EEPROM editor (not the
hex editor), and importing it asks about pairing it with the project's flash
of the same control unit (gui_eeprompair). Offscreen Qt, synthetic images.

(The flash sets here are never opened as tabs: a synthetic image is no flash
the EDC15 module can build a tree from, and pairing does not need one.)"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from test_eeprom_pairing import eeprom15, flash15  # noqa: E402
from ovtsuite import gui_eeprompair as pair  # noqa: E402
from ovtsuite import gui_files as gf  # noqa: E402
from ovtsuite.project import Project  # noqa: E402
from ovtsuite.project import importers as imp  # noqa: E402
from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@pytest.fixture
def editor_calls(G, monkeypatch):
    """Record the EEPROM editor opening on a tab instead of running it."""
    calls = []
    monkeypatch.setattr(G.Workspace, "open_own_eeprom_editor",
                        lambda self: calls.append(self.primary.platform))
    return calls


def test_a_standalone_eeprom_opens_in_the_eeprom_editor_not_the_hex_editor(
        win, tmp_path, editor_calls, monkeypatch):
    hexed = []
    monkeypatch.setattr(type(win.current), "open_hex",
                        lambda self: hexed.append(1))
    path = _write(tmp_path, "car eeprom.bin", eeprom15())
    assert gf.import_files(win, [path], target="new")
    QtWidgets.QApplication.processEvents()
    ws = win.current
    assert ws.primary.platform == "edc15eeprom"
    assert ws.primary.support == "none" and ws.project_ref is not None
    assert editor_calls == ["edc15eeprom"] and hexed == []
    badge = type(win.current).__module__ and __import__(
        "ovtsuite.gui", fromlist=["x"]).platform_badge("edc15eeprom")
    assert badge[1] == "EDC15 EEPROM"


def test_the_editor_edits_go_into_the_tab_as_one_undoable_edit(
        win, tmp_path, G, monkeypatch):
    from ovtsuite import gui_eeprom
    from ovtsuite.edc15 import eeprom15 as E
    path = _write(tmp_path, "car eeprom.bin", eeprom15())
    # (the editor that opens by itself on import is not the one under test)
    with monkeypatch.context() as m:
        m.setattr(G.Workspace, "open_own_eeprom_editor", lambda self: None)
        gf.import_files(win, [path], target="new")
        QtWidgets.QApplication.processEvents()
    ws = win.current
    seen = {}

    class Fake:
        def __init__(self, data, on_save):
            self.data = E.apply(data, E.FIELD_BY_KEY["vin"], "WVWZZZ1JZ3W123456")
            self.on_save = on_save

        def exec(self):
            self.on_save(self.data)

    def fake_open(parent, data, **kw):
        seen["data"] = bytes(data)
        return Fake(bytes(data), kw["on_save"])
    monkeypatch.setattr(gui_eeprom, "open_editor", fake_open)
    ws.open_own_eeprom_editor()
    assert seen["data"] == eeprom15()
    assert bytes(ws.primary.binfile.data) != eeprom15()
    assert ws.is_dirty()
    ws.primary.undo_stack.undo(ws.primary.binfile)
    assert bytes(ws.primary.binfile.data) == eeprom15()


def _doc(win, proj):
    from ovtsuite import gui_project as gp
    doc = gp.ProjectDoc(proj, None)
    gp.docs(win).append(doc)
    return doc


def test_eeprom_imported_with_a_flash_pairs_without_asking(
        win, tmp_path, monkeypatch):
    monkeypatch.setattr(pair, "choose", lambda *a, **k: pytest.fail("asked"))
    proj = Project("p")
    f = _write(tmp_path, "tune.bin", flash15())
    e = _write(tmp_path, "ee.bin", eeprom15())
    made, paired = gf._import_each(win, proj, [e, f])          # EEPROM listed first
    assert [s.platform for _p, s in made] == ["edc15"]
    assert [p for p, _s in paired] == [e]
    assert len(proj.sets) == 1
    s = proj.sets[0]
    assert proj.state(s.id, s.active.head)["eeprom"] == eeprom15()


def test_eeprom_imported_beside_one_flash_in_the_project_asks_to_pair_it(
        win, tmp_path, monkeypatch):
    proj = Project("p")
    fl = imp.import_bytes(proj, flash15(), "flash.bin")
    doc = _doc(win, proj)
    asked = []

    def choose(win_, title, text, names, **k):
        asked.append((text, names))
        return 0
    monkeypatch.setattr(pair, "choose", choose)
    e = _write(tmp_path, "ee.bin", eeprom15())
    made, paired = gf._import_each(win, proj, [e], doc=doc)
    assert made == [] and [s for _p, s in paired] == [fl]
    assert asked and "companion" in asked[0][0] and asked[0][1] == ["flash"]
    assert proj.state(fl.id, fl.active.head)["eeprom"] == eeprom15()
    assert len(proj.sets) == 1


def test_declining_keeps_the_eeprom_a_set_of_its_own(win, tmp_path, monkeypatch):
    proj = Project("p")
    fl = imp.import_bytes(proj, flash15(), "flash.bin")
    doc = _doc(win, proj)
    monkeypatch.setattr(pair, "choose", lambda *a, **k: pair.SEPARATE)
    e = _write(tmp_path, "ee.bin", eeprom15())
    made, paired = gf._import_each(win, proj, [e], doc=doc)
    assert paired == [] and [s.platform for _p, s in made] == ["edc15eeprom"]
    assert fl.member("eeprom") is None


def test_cancelling_leaves_the_eeprom_out(win, tmp_path, monkeypatch):
    proj = Project("p")
    imp.import_bytes(proj, flash15(), "flash.bin")
    doc = _doc(win, proj)
    monkeypatch.setattr(pair, "choose", lambda *a, **k: pair.CANCEL)
    e = _write(tmp_path, "ee.bin", eeprom15())
    made, paired = gf._import_each(win, proj, [e], doc=doc)
    assert made == [] and paired == [] and len(proj.sets) == 1


def test_several_flashes_ask_which_one(win, tmp_path, monkeypatch):
    proj = Project("p")
    a = imp.import_bytes(proj, flash15(), "a.bin")
    b = imp.import_bytes(proj, flash15(), "b.bin")
    doc = _doc(win, proj)
    seen = []

    def choose(win_, title, text, names, **k):
        seen.append((text, names))
        return 1                                    # the second one
    monkeypatch.setattr(pair, "choose", choose)
    e = _write(tmp_path, "ee.bin", eeprom15())
    made, paired = gf._import_each(win, proj, [e], doc=doc)
    assert "more than one" in seen[0][0] and len(seen[0][1]) == 2
    assert [s for _p, s in paired] == [b]
    assert b.member("eeprom") is not None and a.member("eeprom") is None


def test_a_flash_imported_beside_a_lone_eeprom_asks_to_pair_it(
        win, tmp_path, monkeypatch):
    from ovtsuite import gui_project as gp
    opened = []
    monkeypatch.setattr(gp, "open_branch", lambda *a, **k: opened.append(a))
    proj = Project("p")
    e = imp.import_bytes(proj, eeprom15(), "ee.bin")
    doc = _doc(win, proj)
    asked = []

    def choose(win_, title, text, names, **k):
        asked.append(text)
        return 0
    monkeypatch.setattr(pair, "choose", choose)
    f = _write(tmp_path, "tune.bin", flash15())
    made, paired = gf._import_each(win, proj, [f], doc=doc)
    assert asked and "on its own" in asked[0]
    assert [s.platform for _p, s in made] == ["edc15"]
    assert e not in proj.sets and len(proj.sets) == 1
    s = made[0][1]
    assert proj.state(s.id, s.active.head)["eeprom"] == eeprom15()


def test_pairing_waits_for_unsaved_edits(win, tmp_path, monkeypatch):
    class Tab:
        project_ref = type("R", (), {"sid": None})()

        def is_dirty(self):
            return True

        def tab_label(self):
            return "tab"
    proj = Project("p")
    fl = imp.import_bytes(proj, flash15(), "flash.bin")
    doc = _doc(win, proj)
    tab = Tab()
    tab.project_ref.sid = fl.id
    monkeypatch.setattr(type(doc), "tabs", lambda self, w: [tab])
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a[2])))
    assert pair.pair_file(win, doc, fl.id, eeprom15(), "ee.bin") is False
    assert told and "unsaved" in told[0] and fl.member("eeprom") is None


# --------------------------------------------- renaming in the Project panel
def _item(panel, kind, aid=None):
    """The tree item of a kept log/patch/attachment (or any kind) in the panel."""
    out = []

    def walk(it):
        for i in range(it.childCount()):
            ch = it.child(i)
            data = ch.data(0, __import__("ovtsuite.gui_project", fromlist=["x"])._ROLE)
            if data and data[0] == kind and (aid is None or data[2] == aid):
                out.append(ch)
            walk(ch)
    walk(panel.tree.invisibleRootItem())
    return out[0] if out else None


def test_f2_and_the_menu_rename_a_kept_log_and_attachment(win, tmp_path, monkeypatch):
    from ovtsuite import gui_project as gp
    proj = Project("p")
    imp.import_bytes(proj, eeprom15(), "ee.bin")
    lg = proj.add_log("run1.csv", b"a,b\n1,2\n")
    at = proj.add_attachment("notes.txt", b"hello")
    doc = _doc(win, proj)
    gp.show_panel(win)
    panel = gp.panel(win)
    answers = iter(["pull in 3rd", "dyno sheet.pdf"])
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (next(answers), True)))
    li = _item(panel, "log", lg.id)
    panel.tree.setCurrentItem(li)
    panel._f2()                                      # F2 on the selected log
    assert proj.logs[0].filename == "pull in 3rd.csv"
    ai = _item(panel, "attachment", at.id)
    assert ai is not None and ai.text(0) == "notes.txt"
    panel._rename_att(ai.data(0, gp._ROLE))
    assert proj.attachments[0].filename == "dyno sheet.pdf"
    panel.refresh()
    assert _item(panel, "log", lg.id).text(0).endswith("pull in 3rd.csv")
    assert proj.log_bytes(lg.id) == b"a,b\n1,2\n"
    assert doc.project.modified_since_save


# ------------------------------------------------------------- Export menu
def _visible(menu):
    return [a.text() for a in menu.actions() if a.isVisible() and not a.isSeparator()]


def test_export_menu_lists_only_what_applies_to_the_tab(win, tmp_path, editor_calls):
    path = _write(tmp_path, "car eeprom.bin", eeprom15())
    gf.import_files(win, [path], target="new")
    QtWidgets.QApplication.processEvents()
    win._refresh_enabled()
    items = _visible(win.export_menu)
    assert "EEPROM (.bin)..." in items and "Binary (.bin)..." in items
    assert "PCMFlash EDC16 File..." not in [a.text() for a in win.export_menu.actions()
                                            if a.isVisible()]
    for hidden in ("Definition as XDF...", "Definition as A2L...", "Data log...",
                   "Original file (.sgo / .dim / .frf)..."):
        assert hidden not in items
    # no stray separators at the ends
    shown = [a for a in win.export_menu.actions() if a.isVisible()]
    assert not shown[0].isSeparator() and not shown[-1].isSeparator()
    # a tab with no EEPROM of its own offers no EEPROM export
    assert win.current.stored_eeprom() == eeprom15()


def test_the_pcmflash_item_is_named_for_edc16(win):
    assert "PCMFlash EDC16 File..." in [a.text() for a in win.export_menu.actions()]


# ---------------------------------------- Tools > EEPROM editor visibility
def test_the_eeprom_editor_item_is_there_only_for_a_tab_that_stores_an_eeprom(
        win, tmp_path, editor_calls, monkeypatch):
    from ovtsuite import gui_eeprom
    assert not win._ee_act.isVisible()                 # nothing open
    path = _write(tmp_path, "car eeprom.bin", eeprom15())
    gf.import_files(win, [path], target="new")
    QtWidgets.QApplication.processEvents()
    win._refresh_enabled()
    assert win._ee_act.isVisible() and win._ee_act.isEnabled()
    # a tab that stores no EEPROM (a flash with none, an unrecognised file)
    monkeypatch.setattr(type(win.current), "stored_eeprom", lambda self: None)
    win._refresh_enabled()
    assert not win._ee_act.isVisible()
    assert gui_eeprom.layouts_for(eeprom15())


# ------------------------------------------------ EEPROM edits are versions
def test_describe_change_names_the_fields_that_moved():
    from ovtsuite.edc15 import eeprom15 as E
    old = eeprom15()
    new = E.apply(old, E.FIELD_BY_KEY["vin"], "WVWZZZ1JZ3W999999")
    assert pair.describe_change(old, new) == "EEPROM edit: VIN"
    assert pair.describe_change(old, old) == "EEPROM edit"


def test_saving_an_eeprom_edit_is_a_version_named_for_it(win, tmp_path, monkeypatch):
    from ovtsuite import gui_eeprom
    from ovtsuite.edc15 import eeprom15 as E
    path = _write(tmp_path, "car eeprom.bin", eeprom15())
    with monkeypatch.context() as m:
        m.setattr(type(win), "show_file_info", lambda *a, **k: None, raising=False)
        from ovtsuite import gui
        m.setattr(gui.Workspace, "open_own_eeprom_editor", lambda self: None)
        gf.import_files(win, [path], target="new")
        QtWidgets.QApplication.processEvents()
    ws = win.current
    ws.primary.edit_bytes(0, E.apply(eeprom15(), E.FIELD_BY_KEY["vin"],
                                     "WVWZZZ1JZ3W999999"))
    from ovtsuite import gui_project as gp
    assert gp._commit(ws)
    s = ws.project_ref.set
    assert s.checkpoint(s.active.head).label == "EEPROM edit: VIN"
