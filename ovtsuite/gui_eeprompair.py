# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pairing an ECU's EEPROM (EDC15, EDC16) or internal flash (EDC16 MPC) with
its flash in a project.

An EEPROM or MPC image is a **companion**: a set of its own, with its own
versions and tab, exported on its own. *Pairing* links it to a flash set of
the same control unit (``ovtsuite.project.companions``): the flash follows the
companion's latest version, its tab holds it in the EEPROM / MPC part, and the
EEPROM editor on that tab edits it (saved as a version of the companion). One
companion can serve several flashes. Pairing is offered wherever they meet:

* importing an EEPROM while the project already holds that control unit's
  flash (one flash: asked; several: asked which),
* importing an EEPROM and a flash in one go (paired without asking, or asked
  which flash when there are several),
* importing a flash while the project holds an EEPROM image of its own,
* the project panel's *Pair with a flash...*.

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
    """The control unit (``"edc15"`` / ``"edc16"``) whose EEPROM or internal
    flash (MPC) the file at ``path`` is, or "". By content, never by name; only
    files of such a size are read."""
    from . import mpc as _mpc
    try:
        n = os.path.getsize(path)
        plain = n in (512, 4096) or 0x80000 <= n <= 0x81000
        if not plain and not 0x1000 <= n <= 0x81000:
            return ""
        with open(path, "rb") as fh:
            data = fh.read()
        # any other size: only a compressed .MPC is worth identifying
        if not plain and not _mpc.is_encoded(data):
            return ""
    except OSError:
        return ""
    return data_eeprom_ecu(data)


def kind_of_platform(pid: str) -> str:
    """"MPC" for the EDC16 internal flash, else "EEPROM" -- what a dialog
    calls the companion."""
    return "MPC" if _platforms.COMPANION_OF.get(pid, ("", ""))[1] == "mpc" \
        else "EEPROM"


def data_kind(data: bytes) -> str:
    """"MPC" or "EEPROM" for the companion image ``data``."""
    from .platforms.identify import identify
    try:
        return kind_of_platform(identify(bytes(data)).platform)
    except Exception:
        return "EEPROM"


def data_eeprom_ecu(data: bytes) -> str:
    from .platforms.identify import identify
    try:
        got = identify(bytes(data))
    except Exception:
        return ""
    return _platforms.COMPANION_OF.get(got.platform, ("",))[0]


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


def _set_names(project, sets, kind: str = "EEPROM") -> List[str]:
    out = []
    for s in sets:
        note = ""
        if s.member(kind.lower()) is not None:
            note = " (has an %s: it is replaced by a new version)" % kind
        out.append("%s%s" % (s.name, note))
    return out


def ask_flash_for_eeprom(win, project, eeprom_name: str, ecu: str, sets, *,
                        importing: bool = True, kind: str = "EEPROM"):
    """Which of flash ``sets`` the EEPROM / MPC ``eeprom_name`` (``kind``) is
    to be the companion of: a set, ``SEPARATE`` or ``CANCEL``."""
    if not sets:
        return SEPARATE
    if importing:
        text = ("%s is an %s %s. Import it as the companion of the %s "
                "flash in this project?" % (eeprom_name, _ecu_name(ecu), kind,
                                            sets[0].name)
                if len(sets) == 1 else
                "%s is an %s %s, and this project holds more than one "
                "%s flash. Which flash is it the %s of?" % (
                    eeprom_name, _ecu_name(ecu), kind, _ecu_name(ecu), kind))
    else:
        text = "Which %s flash is %s the %s of?" % (_ecu_name(ecu),
                                                    eeprom_name, kind)
    got = choose(
        win, ("Import " if importing else "Pair ") + kind, text,
        _set_names(project, sets, kind), pair_label="Pair",
        separate_label="Import it separately" if importing else "Cancel")
    return sets[got] if isinstance(got, int) else got


def ask_eeprom_for_flash(win, project, flash_name: str, ecu: str, sets, *,
                         kind: str = "EEPROM"):
    """Which EEPROM / MPC-only ``sets`` (``kind``) is the new flash set
    ``flash_name`` to take as its companion: a set, ``SEPARATE`` or
    ``CANCEL``."""
    if not sets:
        return SEPARATE
    got = choose(
        win, "Pair " + kind,
        "This project holds an %s %s on its own (%s). Use it as the "
        "%s of %s?" % (_ecu_name(ecu), kind, ", ".join(s.name for s in sets),
                       kind, flash_name)
        if len(sets) == 1 else
        "This project holds more than one %s %s on its own. Which one "
        "is the %s of %s?" % (_ecu_name(ecu), kind, kind, flash_name),
        [s.name for s in sets], pair_label="Pair", separate_label="Keep apart")
    return sets[got] if isinstance(got, int) else got


# ------------------------------------------------------------------- acting
def tabs_of(win, doc, sid):
    return [t for t in doc.tabs(win) if t.project_ref.sid == sid]


def refresh(win, doc, flash_sid) -> None:
    """Bring the open tabs of the flash set up to date with its new head
    (they were opened without the EEPROM). A tab with unsaved edits keeps
    them and takes only the companion's part (``sync_companions``); a clean
    one is reloaded."""
    from . import gui_project as _gp
    for t in tabs_of(win, doc, flash_sid):
        _gp.sync_companions(t)
    _gp.refresh_panel(win)


def merge(win, doc, eeprom_sid: str, flash_sid: str) -> bool:
    """Link the project's companion set (EEPROM / MPC) to a flash set. The
    companion keeps its own tab and versions; the flash's tab is brought up to
    date (a tab with unsaved edits keeps them and takes only the companion's
    part). True when done (said otherwise)."""
    from .project import importers as _imp
    try:
        _imp.merge_eeprom_set(doc.project, eeprom_sid, flash_sid)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(win, "Pair", str(ex))
        return False
    refresh(win, doc, flash_sid)
    return True


def pair_file(win, doc, flash_sid: str, data: bytes, filename: str) -> bool:
    """Import the EEPROM / MPC image ``data`` into an open project as a
    companion set linked to flash set ``flash_sid``."""
    from .project import importers as _imp
    try:
        _imp.pair_eeprom(doc.project, flash_sid, data, filename)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(win, "Import", str(ex))
        return False
    refresh(win, doc, flash_sid)
    return True


# --------------------------------------------------- from the editor / panel
def pair_with_flash(win, doc, eeprom_sid: str) -> bool:
    """Project panel: pair the EEPROM set with a flash set chosen from the
    project's flash sets of that control unit."""
    from .project import importers as _imp
    proj = doc.project
    e = proj.set(eeprom_sid)
    ecu = _platforms.COMPANION_OF.get(e.platform, ("",))[0]
    sets = _imp.flash_sets_for(proj, ecu) if ecu else []
    if not sets:
        QtWidgets.QMessageBox.information(
            win, "Pair " + kind_of_platform(e.platform),
            "The project holds no %s flash to pair %s with. Import the flash "
            "into this project first." % (_ecu_name(ecu or ""), e.name))
        return False
    got = ask_flash_for_eeprom(win, proj, e.name, ecu, sets, importing=False,
                               kind=kind_of_platform(e.platform))
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
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as ex:
        QtWidgets.QMessageBox.warning(win, "Import", "%s: %s" % (name, ex))
        return CANCEL
    kind = data_kind(data)
    target = sets[0] if len(mine) == 1 else ask_flash_for_eeprom(
        win, proj, name, ecu, sets, kind=kind)
    if not hasattr(target, "id"):
        return target
    try:
        if doc is not None and target.id in before:
            return target if pair_file(win, doc, target.id, data, name) \
                else CANCEL
        _imp.pair_eeprom(proj, target.id, data, name)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(win, "Import " + kind,
                                      "%s: %s" % (name, ex))
        return CANCEL
    return target


def flash_met_eeprom(win, doc, proj, s, before, with_eeprom) -> bool:
    """A flash set ``s`` was just imported: when the project (``doc`` open)
    held an EEPROM / internal flash of its unit that no flash links, ask
    whether it goes with this flash (per kind: EEPROM, MPC). ``with_eeprom``:
    control units whose companion comes in with this import (they pair with it
    instead). True when something was linked."""
    from .project import importers as _imp
    if doc is None or s.platform not in _platforms.EEPROM_FOR \
            or s.platform in with_eeprom:
        return False
    done = False
    alone = [e for e in _imp.standalone_eeprom_sets(proj, s.platform)
             if e.id in before]
    for role in ("eeprom", "mpc"):
        cands = [e for e in alone if _platforms.COMPANION_OF[e.platform][1] == role]
        if not cands:
            continue
        got = ask_eeprom_for_flash(win, proj, s.name, s.platform, cands,
                                   kind="MPC" if role == "mpc" else "EEPROM")
        if hasattr(got, "id") and merge(win, doc, got.id, s.id):
            done = True
    return done


# ------------------------------------------------------------ version names
def describe_change(old: bytes, new: bytes) -> str:
    """"EEPROM edit: VIN, Immobiliser" -- the fields of the layout that fits
    the EEPROM whose values differ between ``old`` and ``new`` (just "EEPROM
    edit" when none can be told)."""
    try:
        from . import gui_eeprom
        lays = gui_eeprom.layouts_for(bytes(new))
        names = []
        if lays and old is not None and len(old) == len(new):
            lay = lays[0]
            for f in lay.fields:
                if lay.get(bytes(old), f) != lay.get(bytes(new), f):
                    names.append(f.name)
        if names:
            more = "" if len(names) <= 4 else " +%d more" % (len(names) - 4)
            return "EEPROM edit: %s%s" % (", ".join(names[:4]), more)
    except Exception:
        pass
    return "EEPROM edit"
