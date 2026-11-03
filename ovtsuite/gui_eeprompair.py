# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pairing an ECU's EEPROM with its flash in a project (EDC15, EDC16).

An EEPROM image is a set of its own until it is *paired*: then it is the
``eeprom`` member of the flash's set, and the EEPROM editor on that flash's
tab opens it (Tools > EEPROM editor) instead of asking for a file. Pairing is
offered wherever the two meet:

* importing an EEPROM while the project already holds that control unit's
  flash (one flash: asked; several: asked which),
* importing an EEPROM and a flash in one go (paired without asking, or asked
  which flash when there are several),
* importing a flash while the project holds an EEPROM image of its own,
* Tools > EEPROM editor on a flash with no EEPROM when the project holds one
  of its own, and the project panel's *Pair with a flash...*.

The project work is :mod:`ovtsuite.project.importers` (``pair_eeprom``,
``merge_eeprom_set``); this module asks the questions and brings the open
tabs up to date.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PySide6 import QtWidgets

from . import platforms as _platforms

#: ``choose`` answers
SEPARATE = ""           # keep it a set of its own
CANCEL = None


# ------------------------------------------------------------- identification
def eeprom_ecu(path: str) -> str:
    """The control unit (``"edc15"`` / ``"edc16"``) whose EEPROM the file at
    ``path`` is, or "". By content, never by name; only files of an EEPROM's
    size are read."""
    try:
        if os.path.getsize(path) not in (512, 4096):
            return ""
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    return data_eeprom_ecu(data)


def data_eeprom_ecu(data: bytes) -> str:
    from .platforms.identify import identify
    try:
        got = identify(bytes(data))
    except Exception:
        return ""
    return _platforms.EEPROM_OF.get(got.platform, "")


def _ecu_name(ecu: str) -> str:
    return _platforms.platform(ecu).short


# ------------------------------------------------------------------ asking
def choose(win, title: str, text: str, names: List[str], *,
           pair_label: str = "Pair", separate_label: str = "Keep separate"):
    """Ask which of ``names`` to pair with: the index chosen, ``SEPARATE``
    ("not any of them") or ``CANCEL``. One name is a yes/no/cancel question;
    several are a list."""
    SB = QtWidgets.QMessageBox.StandardButton
    if len(names) == 1:
        box = QtWidgets.QMessageBox(win)
        box.setWindowTitle(title)
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setText(text)
        yes = box.addButton("%s with %s" % (pair_label, names[0]),
                            QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(separate_label,
                           QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(SB.Cancel)
        box.setDefaultButton(yes)
        box.exec()
        got = box.clickedButton()
        if got is yes:
            return 0
        if got is no:
            return SEPARATE
        return CANCEL
    none = "(none -- %s)" % separate_label.lower()
    pick, ok = QtWidgets.QInputDialog.getItem(
        win, title, text + "\n\nPair with:", list(names) + [none], 0, False)
    if not ok:
        return CANCEL
    return SEPARATE if pick == none else list(names).index(pick)


def _set_names(project, sets) -> List[str]:
    out = []
    for s in sets:
        note = ""
        if s.member("eeprom") is not None:
            note = " (has an EEPROM: it is replaced by a new version)"
        out.append("%s%s" % (s.name, note))
    return out


def ask_flash_for_eeprom(win, project, eeprom_name: str, ecu: str, sets, *,
                        importing: bool = True):
    """Which of flash ``sets`` the EEPROM ``eeprom_name`` is to be the
    EEPROM of: a set, ``SEPARATE`` or ``CANCEL``."""
    if not sets:
        return SEPARATE
    if importing:
        text = ("%s is an %s EEPROM. Import it as the companion of the %s "
                "flash in this project?" % (eeprom_name, _ecu_name(ecu),
                                            sets[0].name)
                if len(sets) == 1 else
                "%s is an %s EEPROM, and this project holds more than one "
                "%s flash. Which flash is it the EEPROM of?" % (
                    eeprom_name, _ecu_name(ecu), _ecu_name(ecu)))
    else:
        text = "Which %s flash is %s the EEPROM of?" % (_ecu_name(ecu),
                                                        eeprom_name)
    got = choose(
        win, "Import EEPROM" if importing else "Pair EEPROM", text,
        _set_names(project, sets), pair_label="Pair",
        separate_label="Import it separately" if importing else "Cancel")
    return sets[got] if isinstance(got, int) else got


def ask_eeprom_for_flash(win, project, flash_name: str, ecu: str, sets):
    """Which EEPROM-only ``sets`` is the new flash set ``flash_name`` to take
    as its EEPROM: a set, ``SEPARATE`` or ``CANCEL``."""
    if not sets:
        return SEPARATE
    got = choose(
        win, "Pair EEPROM",
        "This project holds an %s EEPROM on its own (%s). Use it as the "
        "EEPROM of %s?" % (_ecu_name(ecu), ", ".join(s.name for s in sets),
                           flash_name)
        if len(sets) == 1 else
        "This project holds more than one %s EEPROM on its own. Which one "
        "is the EEPROM of %s?" % (_ecu_name(ecu), flash_name),
        [s.name for s in sets], pair_label="Pair", separate_label="Keep apart")
    return sets[got] if isinstance(got, int) else got


# ------------------------------------------------------------------- acting
def tabs_of(win, doc, sid):
    return [t for t in doc.tabs(win) if t.project_ref.sid == sid]


def _blocked_by_edits(win, doc, *sids) -> Optional[str]:
    """Why pairing must wait: a tab of one of the sets holds unsaved edits
    (the new version is made from what was saved, so they would not be in
    it)."""
    for sid in sids:
        for t in tabs_of(win, doc, sid):
            if t.is_dirty():
                return ("The tab \"%s\" has unsaved changes. Save or close it "
                        "first: pairing adds a version made from what is "
                        "saved." % t.tab_label())
    return None


def refresh(win, doc, flash_sid) -> None:
    """Bring the open tabs of the flash set up to date with its new head
    (they were opened without the EEPROM)."""
    from . import gui_project as _gp
    for t in tabs_of(win, doc, flash_sid):
        if not t.is_dirty():
            _gp.reload_tab(t)
    _gp.refresh_panel(win)


def merge(win, doc, eeprom_sid: str, flash_sid: str) -> bool:
    """Pair the project's stand-alone EEPROM set with a flash set; the
    EEPROM's tab closes. True when done (said otherwise)."""
    from .project import importers as _imp
    from . import gui_project as _gp
    why = _blocked_by_edits(win, doc, eeprom_sid, flash_sid)
    if why:
        QtWidgets.QMessageBox.information(win, "Pair EEPROM", why)
        return False
    proj = doc.project
    tabs = tabs_of(win, doc, eeprom_sid)
    doc.__dict__["_hold"] = True        # its last tab going keeps the project
    try:
        for t in tabs:
            win._remove_workspace(t, remember=False)
        _imp.merge_eeprom_set(proj, eeprom_sid, flash_sid)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(win, "Pair EEPROM", str(ex))
        return False
    finally:
        doc.__dict__.pop("_hold", None)
    if not doc.tabs(win):
        s = proj.set(flash_sid)
        _gp.open_branch(win, doc, s.id, s.active_branch or s.branches[0].id)
    refresh(win, doc, flash_sid)
    return True


def pair_file(win, doc, flash_sid: str, data: bytes, filename: str) -> bool:
    """Add the EEPROM image ``data`` to flash set ``flash_sid`` of an open
    project."""
    from .project import importers as _imp
    why = _blocked_by_edits(win, doc, flash_sid)
    if why:
        QtWidgets.QMessageBox.information(win, "Import EEPROM", why)
        return False
    try:
        _imp.pair_eeprom(doc.project, flash_sid, data, filename)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(win, "Import EEPROM", str(ex))
        return False
    refresh(win, doc, flash_sid)
    return True


# --------------------------------------------------- from the editor / panel
def offer_pair_eeprom(ws) -> bool:
    """Tools > EEPROM editor on a flash tab that has no EEPROM: when its
    project holds an EEPROM of this control unit on its own, offer to pair
    it. True when it was paired (the caller then opens the editor on it)."""
    from .project import importers as _imp
    from . import gui_project as _gp
    ref = getattr(ws, "project_ref", None)
    if ref is None or ref.set.platform not in _platforms.EEPROM_FOR:
        return False
    sets = _imp.standalone_eeprom_sets(ref.doc.project, ref.set.platform)
    if not sets:
        return False
    got = ask_eeprom_for_flash(ws.win, ref.doc.project, ref.set.name,
                               ref.set.platform, sets)
    if not hasattr(got, "id"):
        return False
    return merge(ws.win, ref.doc, got.id, ref.set.id)


def pair_with_flash(win, doc, eeprom_sid: str) -> bool:
    """Project panel: pair the EEPROM set with a flash set chosen from the
    project's flash sets of that control unit."""
    from .project import importers as _imp
    proj = doc.project
    e = proj.set(eeprom_sid)
    ecu = _platforms.EEPROM_OF.get(e.platform)
    sets = _imp.flash_sets_for(proj, ecu) if ecu else []
    if not sets:
        QtWidgets.QMessageBox.information(
            win, "Pair EEPROM",
            "The project holds no %s flash to pair %s with. Import the flash "
            "into this project first." % (_ecu_name(ecu or ""), e.name))
        return False
    got = ask_flash_for_eeprom(win, proj, e.name, ecu, sets, importing=False)
    if not hasattr(got, "id"):
        return False
    return merge(win, doc, e.id, got.id)


# ------------------------------------------------------------ import engine
def plan(paths):
    """``(order, ecus)``: ``paths`` with the EEPROM images last (a flash
    imported in the same go is there when they are), and ``{path: ecu}`` for
    the EEPROM images ("" for the rest)."""
    ecus = {p: eeprom_ecu(p) for p in paths}
    return ([p for p in paths if not ecus[p]] + [p for p in paths if ecus[p]],
            ecus)


def import_eeprom(win, doc, proj, path: str, ecu: str, came, before):
    """One EEPROM image ``path`` of control unit ``ecu`` met on import.

    ``came`` are the sets this import made so far, ``before`` the ids of the
    sets the project held before it, ``doc`` the open project or None (a new
    one). Returns the flash set it was paired with, ``SEPARATE`` (import it
    as a set of its own) or ``CANCEL`` (leave the file out); problems are
    said. The flash that came in with the EEPROM wins without a question;
    else the project's flash of the unit is asked about."""
    from .project import importers as _imp
    name = os.path.basename(path)
    mine = [s for s in came if s.platform == ecu]
    sets = mine if len(mine) == 1 else (mine or [
        s for s in _imp.flash_sets_for(proj, ecu)
        if s.id in before and doc is not None])
    if not sets:
        return SEPARATE
    target = sets[0] if len(mine) == 1 else ask_flash_for_eeprom(
        win, proj, name, ecu, sets)
    if not hasattr(target, "id"):
        return target
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if doc is not None and target.id in before:
            return target if pair_file(win, doc, target.id, data, name) \
                else CANCEL
        _imp.pair_eeprom(proj, target.id, data, name)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(win, "Import EEPROM",
                                      "%s: %s" % (name, ex))
        return CANCEL
    return target


def flash_met_eeprom(win, doc, proj, s, before, with_eeprom) -> bool:
    """A flash set ``s`` was just imported: when the project (``doc`` open)
    held an EEPROM of its unit on its own, ask whether it is this flash's.
    ``with_eeprom``: control units whose EEPROM comes in with this import
    (they pair with it instead). True when paired."""
    from .project import importers as _imp
    if doc is None or s.platform not in _platforms.EEPROM_FOR \
            or s.platform in with_eeprom:
        return False
    alone = [e for e in _imp.standalone_eeprom_sets(proj, s.platform)
             if e.id in before]
    if not alone:
        return False
    got = ask_eeprom_for_flash(win, proj, s.name, s.platform, alone)
    return hasattr(got, "id") and merge(win, doc, got.id, s.id)
