# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The question after importing a file into a flash set that already links an
EEPROM / MPC the file also holds: keep the linked one, add the file's as a new
version of it, or as a companion set of its own. The verdicts and the work are
:mod:`ovtsuite.project.importparts`; this is the dialog and the refreshing of
the open tabs. Cancelling keeps every linked one."""

from __future__ import annotations

from typing import Dict, List

from PySide6 import QtWidgets

from .project import importparts as _ip

_TEXT = {
    _ip.KEEP: "Keep the linked %s",
    _ip.VERSION: "Add the file's as a new version of the linked %s",
    _ip.SEPARATE: "Add it as a separate %s set and link it to this flash set",
}


class ImportPartsDialog(QtWidgets.QDialog):
    """One group per differing part, the verdict above its choices (Keep is
    the default). An identical part is listed without choices."""

    def __init__(self, win, set_name: str, filename: str,
                 verdicts: List["_ip.PartVerdict"]):
        super().__init__(win)
        self.setWindowTitle("Import: EEPROM / MPC in the file")
        self.verdicts = list(verdicts)
        self.radios: Dict[str, Dict[str, QtWidgets.QRadioButton]] = {}
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "%s also holds parts that %s already links. Compare them with "
            "the linked ones and choose; closing this window keeps all the "
            "linked ones and changes nothing." % (filename, set_name))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        for v in self.verdicts:
            lay.addWidget(self._group(v))
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.setMinimumWidth(520)

    def _group(self, v) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("%s (linked: %s)" % (v.label, v.linked.name))
        lay = QtWidgets.QVBoxLayout(box)
        verdict = QtWidgets.QLabel("The file's %s: %s" % (v.label, v.text))
        verdict.setWordWrap(True)
        verdict.setObjectName("verdict_" + v.role)
        lay.addWidget(verdict)
        if v.others:
            shared = QtWidgets.QLabel(
                "Also used by: %s. A new version changes the %s of those "
                "flash sets too." % (", ".join(v.others), v.label))
            shared.setWordWrap(True)
            lay.addWidget(shared)
        if v.warn_immobiliser:
            warn = QtWidgets.QLabel(
                "An EEPROM carries the immobiliser data of the car it came "
                "from. Putting another car's EEPROM into this one can stop "
                "the engine starting or lock the car; only do it when you "
                "know it is the right one.")
            warn.setWordWrap(True)
            warn.setObjectName("immo_warning")
            warn.setStyleSheet("font-weight: bold;")
            lay.addWidget(warn)
        self.radios[v.role] = {}
        for how in v.choices():
            r = QtWidgets.QRadioButton(_TEXT[how] % v.label)
            r.setObjectName("%s_%s" % (v.role, how))
            r.setChecked(how == _ip.KEEP)
            self.radios[v.role][how] = r
            lay.addWidget(r)
        return box

    def choices(self) -> Dict[str, str]:
        out = {}
        for role, rs in self.radios.items():
            out[role] = next((h for h, r in rs.items() if r.isChecked()),
                             _ip.KEEP)
        return out


def refresh_after(win, doc, applied) -> None:
    """Bring the open tabs of every affected flash (and of a companion that
    got a version) up to date."""
    from . import gui_project as _gp
    flashes = {f for a in applied for f in a.flashes}
    touched = {c for a in applied for c in a.touched}
    for t in doc.tabs(win):
        sid = t.project_ref.sid
        try:
            if sid in touched:
                if not t.is_dirty():
                    _gp.reload_tab(t)
            elif sid in flashes:
                _gp.sync_companions(t)
        except Exception:
            pass
    _gp.refresh_panel(win)


def offer(win, doc, sid: str, held: Dict[str, bytes], filename: str,
          dialog=None) -> list:
    """Compare ``held`` (what :func:`importers.import_read` / ``import_bytes``
    filled: the file's EEPROM / MPC for roles the set links) with the linked
    companions and ask about the ones that differ. Nothing is shown when
    none does. Returns what was done (:class:`importparts.Applied`);
    ``dialog``: a replacement for the dialog's ``exec`` (tests)."""
    proj = doc.project
    verdicts = _ip.pending(proj, sid, held)
    if not verdicts:
        return []
    dlg = ImportPartsDialog(win, proj.set(sid).name, filename, verdicts)
    ok = (dialog or (lambda d: d.exec()))(dlg)
    if not ok:
        return []
    try:
        done = _ip.apply(proj, sid, verdicts, dlg.choices(), filename)
    except ValueError as ex:
        QtWidgets.QMessageBox.warning(win, "Import", str(ex))
        return []
    refresh_after(win, doc, done)
    return done
