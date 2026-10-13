# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The bus sniffer and data logger as standalone tools (``--sniffer``,
``--logger``), the way ``--flasher`` runs the flasher: no main window, their own
taskbar identity and icon.

Offscreen Qt, guarded like test_gui_flasher_entry.py: any failure to import Qt
is a skip.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import cli  # noqa: E402
import ovtsuite.gui as gui  # noqa: E402
from ovtsuite.datalog import standalone  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, app):
    """Saved logger settings go to a temp dir, never the developer's own."""
    QtCore.QSettings.setPath(QtCore.QSettings.Format.NativeFormat,
                             QtCore.QSettings.Scope.UserScope, str(tmp_path))
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                             QtCore.QSettings.Scope.UserScope, str(tmp_path))
    yield


def _session(platform, name):
    return types.SimpleNamespace(platform=platform, is_edc15=platform == "edc15",
                                 binfile=types.SimpleNamespace(name=name), path=name)


@pytest.fixture
def fake_sessions(monkeypatch):
    """TunerSession stand-in: the file name says which platform it is."""
    import ovtsuite.session as session_mod

    def fake(path):
        base = os.path.basename(path)
        if base.startswith("bad"):
            raise ValueError("not a calibration")
        return _session(base.split("_")[0], base)
    monkeypatch.setattr(session_mod, "TunerSession", fake)
    # opening a binary remembers its folder in the real config dir; not from a test
    monkeypatch.setattr(standalone._recent, "set_last_dir", lambda *a, **k: None)


# ---------------------------------------------------------------- launch flags
def test_sniffer_and_logger_flags_launch_only_that_tool(monkeypatch):
    seen = []
    monkeypatch.setattr(gui, "run_sniffer", lambda: seen.append("sniffer") or 0)
    monkeypatch.setattr(gui, "run_logger", lambda paths=(): seen.append(list(paths)) or 0)
    assert cli.main(["--sniffer"]) == 0
    assert cli.main(["--logger"]) == 0
    assert cli.main(["--logger", "edc16_car.bin", "zf6hp_tcu.bin"]) == 0
    assert seen == ["sniffer", [], ["edc16_car.bin", "zf6hp_tcu.bin"]]


@pytest.mark.parametrize("tool", ["flasher", "sniffer", "logger"])
def test_each_tool_wears_its_own_icon(app, tool):
    try:
        gui.use_tool_icon(tool)
        assert gui._APP_ICON_FILES[0] == "ovtsuite_%s.ico" % tool
        assert not gui.app_icon().isNull()
    finally:
        gui._APP_ICON_FILES = ("ovtsuite.ico",)
        gui._APP_ICON = None


def test_a_dialog_hidden_by_escape_is_still_closed(app, monkeypatch):
    """Escape only hides a QDialog; the worker-stopping closeEvent must still run
    before the standalone process exits."""
    closed = []

    class Dlg(QtWidgets.QDialog):
        def exec(self):
            self.reject()
            return 0

        def closeEvent(self, ev):
            closed.append(True)
            super().closeEvent(ev)

    monkeypatch.setattr(gui, "_set_windows_app_id", lambda app_id: None)
    assert gui._run_tool("sniffer", "x", Dlg) == 0
    assert closed
    gui._APP_ICON_FILES, gui._APP_ICON = ("ovtsuite.ico",), None


# ---------------------------------------------------------------- logger host
def test_the_host_keeps_one_binary_per_controller(app, fake_sessions):
    host = standalone.StandaloneLogHost()
    assert host.primary is None and host.workspaces() == []
    assert host.open_path("edc16_a.bin")
    assert host.open_path("zf6hp_t.bin")
    assert host.names() == ["edc16_a.bin", "zf6hp_t.bin"]
    assert host.open_path("edc16_b.bin")          # replaces the engine file
    assert host.names() == ["zf6hp_t.bin", "edc16_b.bin"]
    assert host.primary.binfile.name == "edc16_b.bin"


def test_a_bad_binary_is_reported_and_keeps_what_was_open(app, fake_sessions, monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    host = standalone.StandaloneLogHost()
    host.open_path("edc16_a.bin")
    assert host.open_path("bad.bin") is False
    assert shown and host.names() == ["edc16_a.bin"]


def test_standalone_logger_opens_binaries_in_the_dialog(app, fake_sessions):
    from ovtsuite.datalog.gui_log import DataLogDialog
    host = standalone.StandaloneLogHost()
    dlg = DataLogDialog(host)
    try:
        assert "none" in dlg.bin_lbl.text()
        assert "Open project or binary" in dlg.pane.src_hint.text()
        assert dlg.open_binary("zf6hp_tcu.bin")
        assert dlg.bin_lbl.text() == "zf6hp_tcu.bin"
        assert dlg.pane.conn.ecu() == "ZF6HP"      # the front pane follows the file
    finally:
        dlg.close()


def test_the_main_app_logger_has_no_binaries_row(app):
    from ovtsuite.datalog.gui_log import DataLogDialog

    class _Window(QtWidgets.QWidget):
        primary = None

        def workspaces(self):
            return []

    w = _Window()
    dlg = DataLogDialog(w)
    try:
        assert not hasattr(dlg, "bin_lbl")
        assert "in a tab" in dlg.pane.src_hint.text()
    finally:
        dlg.close()


def test_a_bare_file_path_opens_in_the_gui(tmp_path, monkeypatch):
    """What a file association or a drop on the exe passes: just the path."""
    f = tmp_path / "car.edc16"
    f.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(gui, "run", lambda binary=None, compare=None, new_instance=False:
                        seen.append(binary) or 0)
    assert cli.main([str(f)]) == 0
    assert seen == [str(f)]


# ---------------------------------------------------------------- tool launchers
def _entry_module():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "packaging", "entry.py")
    spec = importlib.util.spec_from_file_location("ovt_frozen_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("exe, tool", [
    (r"C:\Program Files\OVT Flasher\OVTFlasher.exe", "flasher"),
    ("/opt/ovt/OVTSniffer", "sniffer"),
    (r"C:\x\ovtlogger.EXE", "logger"),
    (r"C:\x\OVTSuite.exe", None),
])
def test_a_launcher_runs_only_its_tool(exe, tool):
    assert _entry_module()._tool_for_exe(exe) == tool


def test_a_tool_launcher_passes_its_arguments_to_the_tool(monkeypatch):
    entry = _entry_module()
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "executable", r"C:\OVT\OVTLogger.exe")
    monkeypatch.setattr(sys, "argv", [r"C:\OVT\OVTLogger.exe", "car.bin"])
    assert entry._main() == 0
    assert seen == [["--logger", "car.bin"]]


def test_the_logger_names_cells_from_a_project_and_keeps_the_log_in_it(
        app, tmp_path, monkeypatch):
    """OVT Logger opens an .ovt (one binary: no question) and a finished log
    goes into that project; while the main app holds the project, the log
    is kept beside it instead."""
    from conftest import BINARIES
    from ovtsuite import project as ovt
    from ovtsuite import gui_ovt_file
    src = os.path.join(BINARIES, "03G906016LQ.bin")
    if not os.path.exists(src):
        pytest.skip("reference binary missing")
    proj, _s = ovt.import_file(src)
    car = str(tmp_path / "car.ovt")
    proj.save(car)
    host = standalone.StandaloneLogHost()
    assert host.open_path(car)
    ws = host.current
    assert ws.project_ref is not None and ws.primary.platform == "edc16"
    log = tmp_path / "drive.csv"
    log.write_text("t,rpm\n0,800\n")
    assert host.keep_log(ws, str(log))
    q = ovt.Project.load(car)
    assert [lg.filename for lg in q.logs] == ["drive.csv"]
    # the main app has it open: refused, kept beside the project
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: None)
    lf = gui_ovt_file._lock_file(car)
    assert lf.tryLock(0)
    try:
        log2 = tmp_path / "work" / "second.csv"
        log2.parent.mkdir()
        log2.write_text("t\n0\n")
        assert host.keep_log(ws, str(log2)) is False
        assert (tmp_path / "second.csv").exists()
    finally:
        lf.unlock()
    assert len(ovt.Project.load(car).logs) == 1
