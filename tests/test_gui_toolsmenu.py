# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools menu shows only what the active tab can use (ovtsuite.gui_toolsmenu)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES, ZF6HP_REFERENCE  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "5.0TFSI-Stock.bin")
BD = os.path.join(BINARIES, "EDC15", "038906012BD.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


@pytest.fixture
def win(monkeypatch):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    w = gui.MainWindow()
    w.show()
    yield w
    w.close_discarding()


def _shown(win):
    """The Tools menu as it opens: {text: visible} (sub-menus by title)."""
    m = win.tools_menu
    m.aboutToShow.emit()
    out = {}
    for a in m.actions():
        if not a.isSeparator():
            out[a.text()] = a.isVisible()
    return out


def test_an_empty_tab_shows_only_the_window_wide_tools(win):
    shown = _shown(win)
    assert shown["ECU read / write..."] and shown["Data logging..."]
    assert shown["Modules..."]
    assert not shown["Hex editor..."] and not shown["DTC / Fault-Path editor..."]
    win.tools_menu.aboutToHide.emit()
    assert win.tools_menu.actions()[0].isVisible()     # back as it closes


def test_another_platforms_tools_are_hidden(win):
    if not os.path.exists(LQ) or not os.path.exists(ZF):
        pytest.skip("reference binaries missing")
    assert win.open_path(LQ)
    shown = _shown(win)
    assert shown["DTC / Fault-Path editor..."] and shown["Hex editor..."]
    assert not shown["Shift map editor..."]
    assert not shown["Lock-up (TCC) editor..."]
    win.tools_menu.aboutToHide.emit()
    assert win.open_path(ZF, new_tab=True)
    shown = _shown(win)
    assert shown["Hex editor..."]
    assert shown["Shift map editor..."] and shown["Lock-up (TCC) editor..."]
    win.tools_menu.aboutToHide.emit()


def test_engine_settings_tools_leave_a_gearbox_tab(win):
    """Gear ratios and VSS are the engine ECU's: not offered on a TCU."""
    if not os.path.exists(LQ) or not os.path.exists(ZF):
        pytest.skip("reference binaries missing")
    assert win.open_path(LQ)
    shown = _shown(win)
    assert shown["Gear ratios..."] and shown["VSS pulse settings..."]
    win.tools_menu.aboutToHide.emit()
    assert win.open_path(ZF, new_tab=True)
    shown = _shown(win)
    assert not shown["Gear ratios..."] and not shown["VSS pulse settings..."]
    win.tools_menu.aboutToHide.emit()


def test_no_module_has_a_sub_menu_of_its_own(win):
    """The modules' tools are items of Tools itself: the only sub-menu left
    is the app's own AI menu."""
    subs = [a.text() for a in win.tools_menu.actions() if a.menu() is not None]
    assert subs == ["AI"]
    for name in ("edc16_menu", "zf6hp_menu", "zf8hp_menu", "vdo_menu"):
        assert not hasattr(win, name)


def _act(win, text):
    return next(a for a in win.tools_menu.actions() if a.text() == text)


def test_moved_tools_sit_in_their_sections_with_icons(win):
    texts = [a.text() for a in win.tools_menu.actions()]
    # the gearbox editors with the other table editors, before the settings
    assert texts.index("Feature switches...") < texts.index("Shift map editor...") \
        < texts.index("Lock-up (TCC) editor...") < texts.index("Gear ratios...")
    for text in ("Shift map editor...", "Lock-up (TCC) editor..."):
        assert not _act(win, text).icon().isNull()
    from ovtsuite.modules import registry
    if registry.is_installed("edc16"):
        # with ECU read / write and the converter: it builds a flash image
        assert texts.index("Converter (definitions, values and binaries)...") \
            < texts.index("Block builder...") < texts.index("Modules...")
        assert not _act(win, "Block builder...").icon().isNull()
    if registry.is_installed("vdo"):
        assert texts.index("VSS pulse settings...") < texts.index("Needle sweep...") \
            < texts.index("Hex editor...")
    assert "Correct program-area checksums" not in texts


def test_the_block_builder_is_for_an_edc16_tab_only(win):
    from ovtsuite.modules import registry
    if not registry.is_installed("edc16"):
        pytest.skip("the EDC16 module is not installed")
    if not all(os.path.exists(p) for p in (LQ, ZF, BD)):
        pytest.skip("reference binaries missing")
    assert not _shown(win)["Block builder..."]          # no tab
    win.tools_menu.aboutToHide.emit()
    assert win.open_path(LQ)
    assert _shown(win)["Block builder..."]
    win.tools_menu.aboutToHide.emit()
    for other in (BD, ZF):                              # EDC15, ZF 6HP
        assert win.open_path(other, new_tab=True)
        shown = _shown(win)
        assert not shown["Block builder..."], other
        assert not shown.get("Needle sweep...", False)
        win.tools_menu.aboutToHide.emit()
    assert _act(win, "Block builder...").isVisible()   # back as it closes


def test_module_tools_come_from_the_modules_declarations(win, monkeypatch):
    """A module's ``menu_tools`` hook decides its items: text, section,
    icon and the tabs they are for."""
    from ovtsuite import gui_modtools
    from ovtsuite.modules import api
    found = {t.text: (spec.id, t) for spec, t in gui_modtools.tools()}
    from ovtsuite.modules import registry
    if registry.is_installed("edc16"):
        mid, t = found["Block builder..."]
        assert mid == "edc16" and t.section == api.TOOLS_FILES
    if registry.is_installed("vdo"):
        mid, t = found["Needle sweep..."]
        assert mid == "vdo" and t.section == api.TOOLS_SETTINGS
    ran = []
    tool = api.MenuTool("Probe...", lambda w: ran.append(w), "elsewhere")
    spec = api.ModuleSpec(id="probe", name="Probe", version="0.0.1",
                          platforms=("probe",), capabilities=())
    monkeypatch.setattr(gui_modtools, "tools", lambda: [(spec, tool)])
    m = QtWidgets.QMenu()
    assert gui_modtools.add(win, m, gui_modtools.SECTIONS[0]) == []
    (a,) = gui_modtools.add(win, m, gui_modtools.SECTIONS[-1])  # unknown: last
    a.trigger()
    assert ran == [win]
    assert not gui_modtools.applies(win, spec, tool)    # no tab open
