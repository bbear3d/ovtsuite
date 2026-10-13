# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The GUI with a binary its module does not support (docs/MODULES_PLAN.md).

Bear's call (2026-10-10): a binary whose module is not installed -- or that no
module supports -- opens anyway: a dialog says what it is and what would
support it, then the file is in the hex editor, and the tools that need a
parameter tree are disabled rather than failing. Driven offscreen with
synthetic images, so no reference material is needed.
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow          # the GUI


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec",
                        lambda self: shown.append(self.text()) or 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[2]) or 0))
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: shown.append("file info"))
    gui._shown = shown
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


def _action(w, text):
    from PySide6 import QtGui
    return next(a for a in w.findChildren(QtGui.QAction) if a.text() == text)


def _aisin(tmp_path):
    from ovtsuite.platforms import aisin
    d = bytearray(b"\x11" * 0xE0000)
    struct.pack_into(">IIII", d, aisin.D09_DESC, 1, aisin.D09_START,
                     aisin.D09_END, 0)
    p = tmp_path / "09D927750HN_test.bin"
    p.write_bytes(bytes(d))
    return str(p)


def test_an_unrecognised_binary_opens_in_the_hex_editor(G, win, tmp_path):
    p = tmp_path / "mystery.bin"
    p.write_bytes(b"\x5A" * 0x2000)
    assert win.open_path(str(p))
    ws = win.current
    assert ws.primary.platform == "unknown"
    assert "file info" not in G._shown
    assert any("Unrecognised binary" in t for t in G._shown), G._shown
    assert "hex" in ws._tools, "the hex editor opens after the dialog"
    assert "BIN" in ws.tab_label()
    win._refresh_enabled()
    for text in ("DTC / Fault-Path editor...", "Measuring blocks...",
                 "Gear ratios...", "VSS pulse settings..."):
        assert not _action(win, text).isEnabled(), text
    assert _action(win, "Hex editor...").isEnabled()
    assert _action(win, "Verify checksums...").isEnabled()


def test_verify_checksums_names_what_is_missing(G, win, tmp_path):
    p = tmp_path / "mystery.bin"
    p.write_bytes(b"\x5A" * 0x2000)
    win.open_path(str(p))
    G._shown.clear()
    win.current.verify_checksums()
    assert G._shown and "module supports this binary" in G._shown[-1]


def test_a_checksum_only_platform_says_so(G, win, tmp_path, monkeypatch):
    assert win.open_path(_aisin(tmp_path))
    ws = win.current
    assert ws.primary.platform == "aisin" and ws.primary.support == "checksum"
    assert any("TR-60SN" in t for t in G._shown), G._shown
    assert "hex" in ws._tools
    G._shown.clear()
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: asked.append(a[2]) or QtWidgets.QMessageBox.StandardButton.No))
    ws.verify_checksums()
    assert asked and "MISMATCH" in asked[-1]


def test_a_binary_whose_module_is_hidden_opens_limited(tmp_path):
    """EDC16 hidden exactly as if it were not installed: an EDC16 image still
    opens, says which module would support it, and lands in the hex editor."""
    import subprocess
    p = tmp_path / "edc16.bin"
    d = bytearray(b"\xFF" * 0x80000)
    d[0x4000:0x4008] = b"\xFA\xDE\xCA\xFE\xCA\xFE\xAF\xFE"
    p.write_bytes(bytes(d))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = r'''
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtCore, QtWidgets
app = QtWidgets.QApplication([])
QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, %r)
QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
shown = []
QtWidgets.QMessageBox.exec = lambda self: shown.append(self.text()) or 0
from ovtsuite import gui
w = gui.MainWindow()
assert w.open_path(%r)
ws = w.current
assert ws.primary.platform == "edc16" and ws.primary.support == "none"
assert any("Install the Bosch EDC16 module" in t for t in shown), shown
assert "hex" in ws._tools
w.close_discarding()
print("ok")
''' % (str(tmp_path), str(p))
    env = dict(os.environ, OVT_DISABLE_MODULES="edc16",
               PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr
