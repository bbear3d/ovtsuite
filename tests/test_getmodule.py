# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Modules from the GitHub releases (ovtsuite.updates.available_modules,
ovtsuite.gui_getmodule): which packages are published, the fall-back to an
older one, the offer when a file opens and the Module Manager's column. No
network: the releases are made up."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ovtsuite import updates as up  # noqa: E402


def _rel(ver, *names, pre=False):
    return up.Release("v" + ver, ver, ver, "", "", [
        up.Asset(n, 1500000, "https://example.invalid/" + n) for n in names],
        prerelease=pre)


RELEASES = [
    _rel("0.5.0", "edc16-2.0.0.ovtmod", pre=True),          # never offered
    _rel("0.4.9", "edc16-1.0.12.ovtmod", "OVTSuite-Base-0.4.9-setup.exe"),
    _rel("0.4.8", "edc16-1.0.12.ovtmod", "edc16-1.0.12-checksum.ovtmod",
         "edc15-1.0.11.ovtmod", "nosuch-1.0.0.ovtmod"),
    _rel("0.4.7", "edc16-1.0.11.ovtmod", "edc15-1.0.10.ovtmod"),
]


def test_published_packages_newest_first_from_the_newest_release():
    inst = [up.InstalledModule("edc15", "Bosch EDC15", "1.0.11")]
    avail = up.available_modules(RELEASES, inst)
    e16 = avail[("edc16", "full")]
    assert [m.version for m in e16] == ["1.0.12", "1.0.11"]
    assert e16[0].release.version == "0.4.9", "the newest release carrying it"
    assert e16[0].newer and not e16[0].installed
    assert [m.version for m in avail[("edc16", "checksum")]] == ["1.0.12"]
    e15 = avail[("edc15", "full")][0]
    assert e15.installed == "1.0.11" and not e15.newer
    assert not any(k[0] == "nosuch" for k in avail), "unknown ids left out"
    # the variant asked for, else the other
    assert up.module_choices(avail, "edc16", "checksum")[0].variant == "checksum"
    only = {("edc15", "checksum"): avail[("edc15", "full")]}
    assert up.module_choices(only, "edc15", "full") == avail[("edc15", "full")]
    assert up.module_choices(avail, "zf6hp") == []


# ------------------------------------------------------------------ GUI side
try:
    from PySide6 import QtWidgets
except ImportError:                                     # pragma: no cover
    QtWidgets = None


@pytest.fixture
def app(isolated_qsettings):
    if QtWidgets is None:
        pytest.skip("PySide6 is not usable here")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def gm(app, monkeypatch):
    from ovtsuite import gui_getmodule
    gui_getmodule.forget()
    monkeypatch.setattr(up, "installed_modules", lambda: [])
    yield gui_getmodule
    gui_getmodule.forget()


def test_install_falls_back_when_the_newest_is_refused(gm, monkeypatch):
    from ovtsuite import gui_modules, gui_update
    from ovtsuite.modules import installer
    avail = up.available_modules(RELEASES, [])
    fetched, installed = [], []

    def fake_download(parent, offer, assets, **kw):
        fetched.append(assets[0].name)
        return [(assets[0], "/tmp/" + offer.release.version + "/" + assets[0].name,
                 "signed checksum list")]

    def fake_check(path):
        if "/0.4.9/" in path:
            raise installer.InstallError("needs module API 9; this app has 8")
        return object()

    monkeypatch.setattr(gui_update, "_download", fake_download)
    monkeypatch.setattr(installer, "check", fake_check)
    monkeypatch.setattr(gui_modules, "install_from_file",
                        lambda parent, path: installed.append(path) or "edc16")
    assert gm.install(None, "edc16", avail) == "edc16"
    assert fetched == ["edc16-1.0.12.ovtmod", "edc16-1.0.11.ovtmod"]
    assert installed == ["/tmp/0.4.7/edc16-1.0.11.ovtmod"]


def test_install_says_so_when_nothing_is_published(gm, monkeypatch):
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: told.append(a[2]) or 0))
    assert gm.install(None, "zf6hp", {}) is None
    assert "not published" in told[0]


def test_the_offer_when_a_file_opens(gm, monkeypatch):
    calls = []
    monkeypatch.setattr(gm, "_fetch", lambda timeout=15.0:
                        calls.append(timeout) or up.available_modules(RELEASES, []))
    # not allowed to ask GitHub unprompted: a button that looks it up on click
    box = QtWidgets.QMessageBox()
    btn, avail = gm.offer_button(box, "edc16")
    assert btn.text() == "Get the module from GitHub..." and avail is None
    assert calls == []
    # allowed: looked up (once, then cached), the version on the button
    monkeypatch.setattr(gm, "may_look_up", lambda: True)
    box = QtWidgets.QMessageBox()
    box.setInformativeText("The file opens in the hex editor.")
    btn, avail = gm.offer_button(box, "edc16")
    assert btn.text() == "Download and install (1.0.12, 1.5 MB)"
    assert box.defaultButton() is btn and "published on GitHub" in box.informativeText()
    gm.offer_button(QtWidgets.QMessageBox(), "edc16")
    assert calls == [gm.OPEN_TIMEOUT], "asked GitHub once"
    btn, avail = gm.offer_button(QtWidgets.QMessageBox(), "zf6hp")
    assert btn is None, "nothing published for it: no button"


def test_the_module_manager_shows_what_github_publishes(gm, monkeypatch):
    from ovtsuite import gui_modules
    dlg = gui_modules.ModuleManagerDialog()
    col = gui_modules.ModuleManagerDialog.COLS.index("On GitHub")
    row = next(r for r in range(dlg.table.rowCount())
               if dlg.table.item(r, 0).data(gui_modules.QtCore.Qt.UserRole) == "edc16")
    assert dlg.table.item(row, col).text() == "click Check GitHub", "not asked unprompted"
    dlg._looked_up(up.available_modules(RELEASES, []), "")
    dlg.table.selectRow(row)
    if gui_modules._registry.spec("edc16") is None:
        assert dlg.table.item(row, col).text() == "1.0.12"
        assert dlg.github_btn.isEnabled()
    else:                                   # in the source tree: built in
        assert dlg.table.item(row, col).text().startswith("1.0.12")
    dlg.close()
