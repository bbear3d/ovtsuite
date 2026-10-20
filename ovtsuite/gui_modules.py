# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Module Manager, and installing a module from inside the app.

Tools > Modules... lists every module the app knows -- installed or not --
with its version, variant (full or checksum-only), where it is installed,
who signed it and the newest version published on GitHub, and installs a
``.ovtmod`` (from a file or straight from GitHub, :mod:`ovtsuite.gui_getmodule`)
or removes one. Installing from the
app goes to the per-user modules folder (no administrator rights needed); the
module installers write the app's own folder. See docs/MODULES_PLAN.md.

A newly installed module is usable at once; one that replaces a module this
session has already loaded needs a restart (code that is running cannot be
swapped safely), and the dialog says so.
"""

import os
from typing import Optional

from PySide6 import QtCore, QtWidgets

from . import branding
from . import platforms as _platforms
from .modules import api as _api
from .modules import eula as _eula
from .modules import installer as _installer
from .modules import loader as _loader
from .modules import package as _package
from .modules import registry as _registry

FILE_FILTER = "%s modules (*%s);;All files (*)" % (branding.APP_NAME, _package.EXT)


def _status(mid: str):
    """(status text, version, where, signer) for the table."""
    spec = _registry.spec(mid)
    rec = _loader.installed_records().get(mid)
    problem = _registry.problems().get(mid)
    if mid in _eula.declined():
        return ("not loaded: licence agreement not accepted (asked again at the "
                "next start)", rec.manifest.version if rec is not None else "",
                getattr(rec, "root", ""), "")
    if spec is not None:
        what = ("installed" if spec.variant == _api.FULL and spec.has(_api.DEFINITIONS)
                else "installed (checksum only)" if spec.variant == _api.CHECKSUM_ONLY
                else "installed (no definitions yet)")
        if mid in _loader.disabled():
            what = "hidden (%s)" % _loader.ENV
        if rec is not None and mid in _loader.module_roots():
            # the app's own folder is "for all users" only when the app
            # itself is installed machine-wide (not a per-user install)
            where = _installer.scope_label(rec.scope, rec.root)
            return what, spec.version, where, rec.signed_by or "unsigned"
        return what, spec.version, "built in", "-"
    if mid in _loader.disabled():
        return "hidden (%s)" % _loader.ENV, "", "", ""
    if problem:
        ver = rec.manifest.version if rec is not None else ""
        return "refused: %s" % problem, ver, getattr(rec, "root", ""), ""
    return "not installed", "", "", ""


def install_from_file(parent, path: Optional[str] = None) -> Optional[str]:
    """Ask for a .ovtmod (unless given), install it for this user, and say
    what happened. Returns the installed module id, or None."""
    if not path:
        from .recent import get_last_dir, set_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            parent, "Install module", get_last_dir("modules"), FILE_FILTER)
        if not path:
            return None
        set_last_dir(os.path.dirname(path), "modules")
    try:
        checked = _installer.check(path)
    except (_installer.InstallError, _package.PackageError) as ex:
        QtWidgets.QMessageBox.warning(parent, "Install module", str(ex))
        return None
    man = checked.manifest
    was_loaded = _registry.is_installed(man.id)
    variant = ("checksum-only " if man.variant == _api.CHECKSUM_ONLY else "")
    if QtWidgets.QMessageBox.question(
            parent, "Install module",
            "Install the %s%s module %s?\n\n%s\nSigned by: %s"
            % (variant, man.name, man.version,
               _platforms.MODULES[man.id].description,
               checked.signed_by or "nobody (unsigned development build)")) \
            != QtWidgets.QMessageBox.StandardButton.Yes:
        return None
    eula = _api.eula_path(man.id)
    if eula in man.files:
        raw = checked.opened.zip.read(eula)
        if ModuleLicenceDialog(man.name, raw.decode("utf-8", "replace"), parent).exec() \
                != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        _eula.accept(man.id, _eula.digest(raw))
    try:
        _installer.install(path, scope=_installer.USER)
    except (OSError, _installer.InstallError, _package.PackageError) as ex:
        QtWidgets.QMessageBox.critical(parent, "Install module",
                                       "The module was not installed: %s" % ex)
        return None
    if was_loaded:
        QtWidgets.QMessageBox.information(
            parent, "Install module",
            "The %s module %s is installed. Restart %s to use it -- the "
            "version already loaded stays in use until then."
            % (man.name, man.version, branding.APP_NAME))
        return man.id
    rec = _installer.read_installed(
        os.path.join(_installer.user_dir(), man.id), _installer.USER)
    if rec is not None and _loader.mount(rec):
        _registry.reset()
    if _registry.is_installed(man.id):
        QtWidgets.QMessageBox.information(
            parent, "Install module", "The %s module %s is installed and ready."
            % (man.name, man.version))
    else:
        QtWidgets.QMessageBox.warning(
            parent, "Install module", "The %s module was installed but could "
            "not be loaded: %s" % (man.name, _registry.problems().get(man.id, "?")))
    return man.id


def confirm_licences(parent=None) -> None:
    """Ask, before the app or a tool starts, for every installed module whose
    licence agreement this user has not accepted (a Linux package, a module
    copied into place: see :mod:`ovtsuite.modules.eula`). A declined
    module is hidden for this run and asked about again next time."""
    try:
        todo = _eula.pending()
    except Exception:                # noqa: BLE001 - never stop the app starting
        return
    for p in todo:
        dlg = ModuleLicenceDialog(p.name, p.text, parent, installed=True)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            _eula.accept(p.id, p.digest)
        else:
            _eula.decline(p.id)


class ModuleLicenceDialog(QtWidgets.QDialog):
    """A closed-source module's licence agreement: accepted before it is
    installed from inside the app (its own installer shows the same text), or
    at the first start after a package installed it without asking
    (``installed=True``)."""

    def __init__(self, name: str, text: str, parent=None, installed: bool = False):
        super().__init__(parent)
        self.setWindowTitle(branding.title("Module licence"))
        self.resize(640, 520)
        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            ("The %s module is installed on this computer and is licensed under "
             "the agreement below. Accept it to use the module. If you decline, "
             "%s starts without it and asks again next time." % (name, branding.APP_NAME))
            if installed else
            ("The %s module is licensed under the agreement below. Accept it to "
             "install the module." % name))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.text = QtWidgets.QPlainTextEdit(text)
        self.text.setReadOnly(True)
        v.addWidget(self.text, 1)
        self.accept_box = QtWidgets.QCheckBox("I accept the agreement")
        v.addWidget(self.accept_box)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        ok.setText("Accept" if installed else "Install")
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText(
            "Decline" if installed else "Cancel")
        ok.setEnabled(False)
        self.accept_box.toggled.connect(ok.setEnabled)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class ModuleManagerDialog(QtWidgets.QDialog):
    COLS = ("Module", "Status", "Version", "Installed", "Signed by", "On GitHub")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(branding.title("Modules"))
        self.resize(920, 380)
        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Modules add editing (definitions, maps, tools) and checksum "
            "correction for one kind of control module. Identifying files, "
            "the hex editor, comparing, patches and the flasher, logger and "
            "sniffer work without any module.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sel)
        v.addWidget(self.table)
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        row = QtWidgets.QHBoxLayout()
        self.install_btn = QtWidgets.QPushButton("Install from file...")
        self.install_btn.clicked.connect(self._install)
        self.github_btn = QtWidgets.QPushButton("Install from GitHub")
        self.github_btn.setToolTip("Download the selected module's newest "
                                   "published version and install it")
        self.github_btn.clicked.connect(self._install_github)
        self.check_btn = QtWidgets.QPushButton("Check GitHub")
        self.check_btn.setToolTip("Look up the modules published on GitHub again")
        self.check_btn.clicked.connect(lambda: self._lookup(fresh=True))
        self.remove_btn = QtWidgets.QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove)
        self.unsigned = QtWidgets.QCheckBox("Allow unsigned modules (development)")
        self.unsigned.setChecked(_unsigned_pref())
        self.unsigned.toggled.connect(_set_unsigned_pref)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        for w in (self.install_btn, self.github_btn, self.check_btn,
                  self.remove_btn, self.unsigned):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)
        self.available = None               # GitHub's modules, once looked up
        self.github_error = ""
        from . import gui_getmodule as _gm
        if _gm.may_look_up():
            self._lookup()
        else:
            self.github_error = "click Check GitHub"
            self.refresh()

    def _lookup(self, fresh: bool = False):
        """Ask GitHub what it publishes (in the background)."""
        from . import gui_getmodule as _gm
        self.check_btn.setEnabled(False)
        self.github_error = "checking..."
        self.refresh()
        _gm.published_async(self, self._looked_up, fresh=fresh)

    def _looked_up(self, avail, error):
        try:
            self.check_btn.setEnabled(True)
        except RuntimeError:                # the dialog closed meanwhile
            return
        self.available, self.github_error = avail, error
        self.refresh()

    def _github(self, mid: str) -> str:
        from . import gui_getmodule as _gm
        if self.available is None:
            return self.github_error
        top = _gm.best(self.available, mid)
        if top is None:
            return "not published" if not self.github_error else "?"
        if not top.installed:
            return top.version
        return top.version + ("  (newer)" if top.newer else "  (current)")

    def _ids(self):
        return list(_platforms.MODULES)

    def refresh(self):
        ids = self._ids()
        self.table.setRowCount(len(ids))
        for r, mid in enumerate(ids):
            st = _status(mid)
            cells = (_platforms.MODULES[mid].name,) + st + (self._github(mid),)
            for c, text in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                if c == 0:
                    it.setToolTip(_platforms.MODULES[mid].description)
                    it.setData(QtCore.Qt.UserRole, mid)
                self.table.setItem(r, c, it)
        self._sel()

    def _selected(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(QtCore.Qt.UserRole)

    def _sel(self):
        mid = self._selected()
        rec = _loader.installed_records().get(mid) if mid else None
        self.remove_btn.setEnabled(rec is not None)
        from . import gui_getmodule as _gm
        top = _gm.best(self.available or {}, mid) if mid else None
        self.github_btn.setEnabled(top is not None and top.newer)
        note = _platforms.MODULES[mid].description if mid else ""
        if self.github_error and self.github_error not in (
                "checking...", "click Check GitHub"):
            note = (note + "\n" if note else "") + "GitHub: " + self.github_error
        self.note.setText(note)

    def _install_github(self):
        from . import gui_getmodule as _gm
        mid = self._selected()
        if mid and _gm.install(self, mid, self.available) is not None:
            self.available = None
            self.refresh()
            self._lookup(fresh=True)

    def _install(self):
        if install_from_file(self) is not None:
            self.refresh()

    def _remove(self):
        mid = self._selected()
        rec = _loader.installed_records().get(mid) if mid else None
        if rec is None:
            return
        name = _platforms.MODULES[mid].name
        if QtWidgets.QMessageBox.question(
                self, "Remove module",
                "Remove the %s module? Files of this kind will then open in the "
                "hex editor only, and their checksums will not be corrected."
                % name) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            _installer.uninstall(mid, rec.scope if rec.scope in (
                _installer.USER, _installer.SYSTEM) else os.path.dirname(rec.root))
        except OSError as ex:
            QtWidgets.QMessageBox.warning(
                self, "Remove module",
                "Could not remove it (%s). A module installed for all users is "
                "removed with its installer, from the system's app list." % ex)
            return
        QtWidgets.QMessageBox.information(
            self, "Remove module",
            "The %s module is removed. Restart %s to finish -- it stays in use "
            "until then." % (name, branding.APP_NAME))
        self.refresh()


def _unsigned_pref() -> bool:
    from .modules import signing
    return signing.allow_unsigned()


def _set_unsigned_pref(on: bool) -> None:
    from .appsettings import settings
    settings().setValue("modules/allow_unsigned", bool(on))
