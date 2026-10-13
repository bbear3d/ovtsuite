# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Installing a closed-source module from inside the app asks to accept its
licence agreement first (ovtsuite.gui_modules.ModuleLicenceDialog)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow          # the GUI


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever the dialogs remember goes to the test's own settings."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def hvac_pkg(tmp_path, monkeypatch):
    from ovtsuite.modules import build, package, signing
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ovtsuite")
    out = tmp_path / "hvac.ovtmod"
    package.build(build._specs(src)["hvac"], src, str(out), compile_pyc=False)
    monkeypatch.setattr(signing, "allow_unsigned", lambda: True)
    return str(out)


def _run(monkeypatch, path, accept):
    from ovtsuite import gui_modules as G
    calls = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    shown = []

    def exec_(self):
        shown.append(self.text.toPlainText())
        return (QtWidgets.QDialog.DialogCode.Accepted if accept
                else QtWidgets.QDialog.DialogCode.Rejected)
    monkeypatch.setattr(G.ModuleLicenceDialog, "exec", exec_)

    def install(*a, **k):
        calls.append(a)
        raise OSError("stopped by the test")
    monkeypatch.setattr(G._installer, "install", install)
    G.install_from_file(None, path)
    return shown, calls


def test_declining_the_agreement_installs_nothing(app, hvac_pkg, monkeypatch):
    shown, calls = _run(monkeypatch, hvac_pkg, accept=False)
    assert shown and "MODULE LICENCE AGREEMENT" in shown[0]
    assert calls == []


def test_accepting_the_agreement_installs(app, hvac_pkg, monkeypatch):
    shown, calls = _run(monkeypatch, hvac_pkg, accept=True)
    assert shown and len(calls) == 1


def test_install_button_waits_for_the_tick(app):
    from ovtsuite.gui_modules import ModuleLicenceDialog
    d = ModuleLicenceDialog("HVAC", "terms")
    ok = d.findChild(QtWidgets.QDialogButtonBox).button(
        QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    d.accept_box.setChecked(True)
    assert ok.isEnabled()


# -- first start: modules a package installed without asking ----------------
@pytest.fixture
def fake_installed(tmp_path, monkeypatch):
    """One closed module 'hvac' installed under tmp_path, as the loader sees it."""
    from types import SimpleNamespace
    from ovtsuite.modules import eula, loader
    root = tmp_path / "modules" / "hvac"
    (root / "hvac").mkdir(parents=True)
    (root / "hvac" / "EULA.txt").write_bytes(b"MODULE LICENCE AGREEMENT\nterms\n")
    rec = SimpleNamespace(manifest=SimpleNamespace(name="4F/4L HVAC", version="0.1.6"),
                          root=str(root), scope="user", signed_by="")
    monkeypatch.setattr(loader, "module_roots", lambda: {"hvac": str(root)})
    monkeypatch.setattr(loader, "installed_records", lambda: {"hvac": rec})
    monkeypatch.setattr(loader, "disabled", lambda: frozenset())
    hidden = []
    monkeypatch.setattr(loader, "set_disabled", lambda ids: hidden.append(set(ids)))
    yield root, hidden
    eula._declined.clear()


def test_a_module_is_pending_until_accepted_by_installer_or_user(app, fake_installed):
    from ovtsuite.modules import eula
    root, _hidden = fake_installed
    (p,) = eula.pending()
    assert p.id == "hvac" and "MODULE LICENCE AGREEMENT" in p.text
    marker = root / "hvac" / eula.MARKER
    marker.write_text(p.digest.upper())                  # Inno's hex, any case
    assert eula.pending() == []
    marker.unlink()
    assert len(eula.pending()) == 1
    eula.accept("hvac", p.digest)
    assert eula.pending() == []
    # a changed agreement is asked again
    (root / "hvac" / "EULA.txt").write_bytes(b"MODULE LICENCE AGREEMENT\nnew terms\n")
    assert len(eula.pending()) == 1


def test_declining_at_first_start_hides_the_module_for_this_run(app, fake_installed,
                                                                monkeypatch):
    from ovtsuite import gui_modules as G
    from ovtsuite.modules import eula
    _root, hidden = fake_installed
    seen = []

    def exec_(self):
        seen.append(self.accept_box.text())
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(G.ModuleLicenceDialog, "exec", exec_)
    G.confirm_licences()
    assert seen and hidden == [{"hvac"}]
    assert "licence agreement not accepted" in G._status("hvac")[0]
    assert len(eula.pending()) == 1                      # asked again next time


def test_accepting_at_first_start_is_remembered(app, fake_installed, monkeypatch):
    from ovtsuite import gui_modules as G
    from ovtsuite.modules import eula
    monkeypatch.setattr(G.ModuleLicenceDialog, "exec",
                        lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    G.confirm_licences()
    assert eula.pending() == [] and eula.declined() == {}
