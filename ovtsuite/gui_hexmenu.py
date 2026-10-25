# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The hex editor's right-click menu (:class:`gui.HexDialog`).

Copy (hex) / Copy as text / Paste, and -- worded as in the map table's
menu -- Copy selected bytes from comparison (primary view), Copy bytes to
primary (comparison view), Revert to as-read values / last saved values,
Go to table, Select all and Find next.

Every write is ONE undo step through the session (``session.transaction``
over ``edit_bytes``), so undo/redo, the dirty state and checksum
correction on save work as for typed bytes. A selection in the ASCII
column stands for the 16 bytes of its row.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

COLS = 16


# -- the selection --------------------------------------------------------

def selected_addrs(dlg) -> List[int]:
    """Sorted byte addresses the selection covers (an ASCII-column cell
    covers its row's bytes); the current byte when nothing is selected."""
    n = len(dlg.model.shown())
    sm = dlg.view.selectionModel()
    idxs = list(sm.selectedIndexes()) if sm is not None else []
    if not idxs and dlg.view.currentIndex().isValid():
        idxs = [dlg.view.currentIndex()]
    out = set()
    for ix in idxs:
        base = ix.row() * COLS
        cols = range(COLS) if ix.column() >= COLS else (ix.column(),)
        for c in cols:
            if base + c < n:
                out.add(base + c)
    return sorted(out)


def _runs(pairs) -> List[Tuple[int, bytes]]:
    """``[(addr, byte)]`` (sorted) as contiguous ``(start, bytes)`` runs."""
    runs = []
    for a, b in pairs:
        if runs and runs[-1][0] + len(runs[-1][1]) == a:
            runs[-1][1].append(b)
        else:
            runs.append((a, bytearray([b])))
    return [(s, bytes(d)) for s, d in runs]


def _say(dlg, text: str):
    try:
        dlg.info.setText(text)
    except Exception:
        pass


def _main_flash_primary(dlg) -> bool:
    """The primary's main flash is on screen and editable."""
    m = dlg.model
    return m.region == 0 and m.can_write()


def _after_write(dlg):
    dlg.model.refresh()
    tab = dlg.parent()
    if tab is not None and getattr(tab, "primary", None) is dlg.session:
        try:
            tab.model.refresh()                 # the table behind the editor
        except Exception:
            pass


def write_pairs(dlg, pairs, label: str) -> int:
    """Write ``[(addr, byte)]`` into the primary's main flash as one undo
    step; the number of bytes that changed."""
    pairs = [(a, b) for a, b in sorted(pairs)
             if a < len(dlg.session.binfile.data)
             and dlg.session.binfile.data[a] != b]
    if not pairs:
        return 0
    s = dlg.session
    with s.transaction(label):
        for start, data in _runs(pairs):
            s.edit_bytes(start, data)
    _after_write(dlg)
    return len(pairs)


# -- the actions ------------------------------------------------------------

def copy_hex(dlg) -> int:
    addrs = selected_addrs(dlg)
    if not addrs:
        return 0
    buf = dlg.model.shown()
    QtWidgets.QApplication.clipboard().setText(
        " ".join("%02X" % buf[a] for a in addrs))
    _say(dlg, "Copied %d byte(s) as hex" % len(addrs))
    return len(addrs)


def copy_text(dlg) -> int:
    addrs = selected_addrs(dlg)
    if not addrs:
        return 0
    buf = dlg.model.shown()
    QtWidgets.QApplication.clipboard().setText(
        "".join(chr(buf[a]) if 32 <= buf[a] < 127 else "." for a in addrs))
    _say(dlg, "Copied %d byte(s) as text" % len(addrs))
    return len(addrs)


def _cmp_pairs(dlg, addrs) -> List[Tuple[int, int]]:
    """``(addr, comparison byte)`` for the selected bytes that differ."""
    cmp_ = dlg.model.compare_shown()
    buf = dlg.session.binfile.data
    if cmp_ is None:
        return []
    return [(a, cmp_[a]) for a in addrs
            if a < len(cmp_) and a < len(buf) and buf[a] != cmp_[a]]


def from_comparison(dlg) -> int:
    """Primary view: overwrite the selected bytes with the comparison's."""
    if not _main_flash_primary(dlg):
        _say(dlg, "Only the primary's main flash takes comparison bytes")
        return 0
    pairs = _cmp_pairs(dlg, selected_addrs(dlg))
    if not pairs:
        _say(dlg, "The selected bytes already match the comparison")
        return 0
    n = write_pairs(dlg, pairs, "Copy bytes from comparison")
    _say(dlg, "Copied %d byte(s) from the comparison" % n)
    return n


def to_primary(dlg) -> int:
    """Comparison view: the selected comparison bytes into the primary at
    the same addresses."""
    m = dlg.model
    if m.which != "compare" or m.compare_shown() is None:
        return 0
    pairs = _cmp_pairs(dlg, selected_addrs(dlg))
    if not pairs:
        _say(dlg, "The selected comparison bytes already match the primary")
        return 0
    n = write_pairs(dlg, pairs, "Copy bytes to primary")
    _say(dlg, "Copied %d byte(s) to the primary" % n)
    return n


def _originals(dlg, which: str) -> Tuple[Optional[bytes], str]:
    """The image to revert to: the tab's own source when the editor belongs
    to a tab (a project's as-read image), else the session's."""
    tab = dlg.parent()
    if tab is not None and getattr(tab, "primary", None) is dlg.session:
        try:
            from . import gui_cellkeys as gck
            return (gck._as_read(tab) if which == "read"
                    else gck._last_saved(tab))
        except Exception:
            pass
    s = dlg.session
    data = getattr(s, "original", None) if which == "read" else (
        getattr(s, "_last_saved_snapshot", None) or getattr(s, "original", None))
    if data is None or len(data) != len(s.binfile.data):
        return None, ""
    return bytes(data), ("as opened" if which == "read" else "last saved")


def _revert_pairs(dlg, which: str, addrs) -> List[Tuple[int, int]]:
    orig, _what = _originals(dlg, which)
    buf = dlg.session.binfile.data
    if orig is None:
        return []
    return [(a, orig[a]) for a in addrs
            if a < len(orig) and a < len(buf) and buf[a] != orig[a]]


def revert(dlg, which: str = "read") -> int:
    """Put the selected bytes back to the as-read (``"read"``) or last
    saved (``"saved"``) values, as one undo step."""
    if not _main_flash_primary(dlg):
        return 0
    pairs = _revert_pairs(dlg, which, selected_addrs(dlg))
    if not pairs:
        _say(dlg, "The selected bytes already have those values")
        return 0
    _o, what = _originals(dlg, which)
    n = write_pairs(dlg, pairs, "Revert bytes to %s" % what)
    _say(dlg, "Reverted %d byte(s) to the %s values" % (n, what))
    return n


def entity_under(dlg, addr) -> Optional[object]:
    """The parameter object covering byte ``addr`` (main flash), or None."""
    if addr is None:
        return None
    reg = dlg.model.entity_at(addr)
    if not reg:
        return None
    name, start, _end = reg
    s = dlg.session
    try:
        ents = list(s.entities) + list(s.variant_entities())
    except Exception:
        ents = list(s.entities)
    for e in ents:
        if e.addr == start and e.name == name:
            return e
    return None


def go_to_table(dlg, entity) -> bool:
    tab = dlg.parent()
    fn = getattr(tab, "show_entity", None)
    if fn is None or entity is None:
        return False
    fn(entity)
    try:
        tab.window().raise_()
        tab.window().activateWindow()
    except Exception:
        pass
    return True


# -- the menu ---------------------------------------------------------------

def _add(menu, text: str, fn: Callable, why: Optional[str] = None):
    act = menu.addAction(text, fn)
    if why:
        act.setEnabled(False)
        act.setToolTip(why)
    return act


def build_menu(dlg, pos):
    """The menu for a right-click at ``pos`` (viewport coordinates)."""
    m = dlg.model
    menu = QtWidgets.QMenu(dlg)            # looked up now: tests replace it
    menu.setToolTipsVisible(True)
    addrs = selected_addrs(dlg)
    none = None if addrs else "No bytes selected"
    _add(menu, "Copy", lambda: copy_hex(dlg), none)
    _add(menu, "Copy as text", lambda: copy_text(dlg), none)
    if m.can_write():
        clip = QtWidgets.QApplication.clipboard().text().strip()
        _add(menu, "Paste", dlg._paste,
             None if clip else "The clipboard is empty")

    cmp_ = m.compare_shown()
    if cmp_ is not None:
        menu.addSeparator()
        same = None if _cmp_pairs(dlg, addrs) else (
            "The selected bytes already match" if addrs else "No bytes selected")
        if m.which == "compare":
            _add(menu, "Copy bytes to primary", lambda: to_primary(dlg), same)
        elif m.can_write():
            _add(menu, "Copy selected bytes from comparison",
                 lambda: from_comparison(dlg), same)

    if _main_flash_primary(dlg):
        acts = []
        for text, which, missing in (
                ("Revert to as-read values", "read",
                 "There is no original image to go back to"),
                ("Revert to last saved values", "saved",
                 "The file has not been saved yet")):
            orig, _w = _originals(dlg, which)
            if orig is None:
                acts.append((text, which, missing))
            else:
                acts.append((text, which, None if _revert_pairs(dlg, which, addrs)
                             else "The selected bytes already have those values"))
        menu.addSeparator()
        for text, which, why in acts:
            _add(menu, text, lambda w=which: revert(dlg, w), why)

    ix = dlg.view.indexAt(pos)
    under = dlg._addr_of(ix) if ix.isValid() else dlg._addr_of(
        dlg.view.currentIndex())
    ent = entity_under(dlg, under)
    if ent is not None and getattr(dlg.parent(), "show_entity", None):
        menu.addSeparator()
        menu.addAction("Go to table: %s" % ent.name,
                       lambda e=ent: go_to_table(dlg, e))

    menu.addSeparator()
    menu.addAction("Select all", dlg.view.selectAll)
    if dlg.find.text().strip():
        menu.addAction("Find next", dlg._find_next)
    return menu


def show_menu(dlg, pos):
    menu = build_menu(dlg, pos)
    menu.exec(dlg.view.viewport().mapToGlobal(pos))


def install(dlg):
    """Give the hex grid its right-click menu."""
    dlg.view.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
    dlg.view.customContextMenuRequested.connect(lambda pos: show_menu(dlg, pos))
