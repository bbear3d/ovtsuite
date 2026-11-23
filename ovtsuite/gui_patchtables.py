# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Choosing whole tables for a patch.

A patch normally carries what changed between two binaries. **Add tables...** in
the Generate Patch window (and **Create patch from selected tables...** in the
parameter tree's menu) puts tables in it whole -- every cell and breakpoint, changed
or not (:func:`ovtsuite.patch.whole_entry`) -- taken from the file whose values the
patch carries.

:class:`TablePicker` is the list to choose from: every parameter of that file, grouped
the way the parameter tree groups them, with a search box that narrows a definition of
thousands of parameters to the few whose name, symbol, description or group matches
(every word typed must be found). A table already in the patch whole is not offered;
one found changed is, marked with how much changed, and picking it makes it whole
(:func:`whole_from_diff`): its breakpoints then go in too, changed or not.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from PySide6 import QtCore, QtGui, QtWidgets

from .gui_cellmarks import CellDelegate

_ROLE = QtCore.Qt.ItemDataRole.UserRole
CHECKED = QtCore.Qt.CheckState.Checked
UNCHECKED = QtCore.Qt.CheckState.Unchecked


def group_path(e) -> List[str]:
    """The headings a parameter sits under in the parameter tree: its group, then its
    function heading (or the ZF path)."""
    from . import catalogue as _catalogue
    from . import gui as _g
    path = list(getattr(e, "category_path", None) or ())
    if not path:
        path = [_catalogue.heading_text(e.categories[0] if e.categories else "", e.name)]
    return [_g.tier_name(e.tier)] + [_g._tr_de(p) for p in path]


def label_of(e) -> str:
    from . import gui as _g
    return _g._english(e)


def haystack(e, label: str) -> str:
    """What the search matches a parameter against (lower case): the same text the
    parameter tree's filter uses, and the group it is under."""
    from . import gui as _g
    return _g._filter_text(e, label) + "\0" + " ".join(group_path(e)).lower()


def matches(needle: str, text: str) -> bool:
    """Every word of ``needle`` is somewhere in ``text``."""
    return all(w in text for w in needle.lower().split())


class TablePicker(QtWidgets.QDialog):
    """A searchable, grouped list of parameters with a check box each."""

    def __init__(self, entities: Iterable, parent=None, *, taken: Iterable[str] = (),
                 preselected: Iterable[str] = (), title: str = "Add tables",
                 intro: str = "", partial: Optional[Dict[str, str]] = None):
        """``taken``: names not offered (in the patch whole). ``partial``:
        ``{name: note}`` of tables in the patch as found changed -- offered,
        with the note ("17/64 changed") in the *In the patch* column."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(780, 620)
        taken = set(taken)
        partial = dict(partial or {})
        pre = set(preselected)
        self._items: List[QtWidgets.QTreeWidgetItem] = []
        self._text: List[str] = []
        self._chosen: List = []

        v = QtWidgets.QVBoxLayout(self)
        if intro:
            lab = QtWidgets.QLabel(intro)
            lab.setWordWrap(True)
            v.addWidget(lab)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(
            "Search name, symbol, description or group -- every word must match")
        self.search.setClearButtonEnabled(True)
        v.addWidget(self.search)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Parameter", "Symbol", "Size", "In the patch"])
        self.tree.setAlternatingRowColors(True)         # the app's table standard
        self.tree.setItemDelegate(CellDelegate(self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 420)
        self.tree.setColumnWidth(1, 200)
        v.addWidget(self.tree, 1)

        self.tree.setUpdatesEnabled(False)
        heads = {}
        try:
            for e in entities:
                if e.name in taken:
                    continue
                parent_item = self.tree.invisibleRootItem()
                key = ()
                for h in group_path(e):
                    key += (h,)
                    node = heads.get(key)
                    if node is None:
                        node = QtWidgets.QTreeWidgetItem([h])
                        node.setFirstColumnSpanned(True)
                        parent_item.addChild(node)
                        heads[key] = node
                    parent_item = node
                label = label_of(e)
                size = ("1" if e.kind == "constant" else "%dx%d" % (e.rows, e.cols))
                note = partial.get(e.name, "")
                it = QtWidgets.QTreeWidgetItem([label, e.name, size, note])
                if note:
                    it.setToolTip(3, "In the patch as found changed. Checked, it "
                                     "goes in whole: every cell and breakpoint.")
                it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, CHECKED if e.name in pre else UNCHECKED)
                it.setData(0, _ROLE, e)
                parent_item.addChild(it)
                self._items.append(it)
                self._text.append(haystack(e, label))
        finally:
            self.tree.setUpdatesEnabled(True)
        self._heads = list(heads.values())
        self.tree.itemChanged.connect(lambda *_: self._count())

        row = QtWidgets.QHBoxLayout()
        self.info = QtWidgets.QLabel()
        row.addWidget(self.info, 1)
        shown = QtWidgets.QPushButton("Check shown")
        shown.setToolTip("Check every parameter the search leaves in the list")
        shown.clicked.connect(lambda: self.check_shown(True))
        row.addWidget(shown)
        none = QtWidgets.QPushButton("Uncheck all")
        none.clicked.connect(self.uncheck_all)
        row.addWidget(none)
        v.addLayout(row)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)               # after the typing pauses
        self._timer.timeout.connect(self._filter)
        self.search.textChanged.connect(lambda *_: self._timer.start())
        self._filter()
        self.search.setFocus()

    # ------------------------------------------------------------ search
    def _filter(self) -> None:
        needle = self.search.text().strip()
        self.tree.setUpdatesEnabled(False)
        try:
            for it, text in zip(self._items, self._text):
                it.setHidden(bool(needle) and not matches(needle, text))
            # a heading shows when something under it does; open while searching
            for h in reversed(self._heads):
                vis = any(not h.child(k).isHidden() for k in range(h.childCount()))
                h.setHidden(not vis)
                if needle and vis:
                    h.setExpanded(True)
            if not needle:
                for h in self._heads:
                    h.setExpanded(False)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._count()

    def shown(self) -> List[QtWidgets.QTreeWidgetItem]:
        return [it for it in self._items if not it.isHidden()]

    def check_shown(self, on: bool = True) -> None:
        self.tree.blockSignals(True)
        try:
            for it in self.shown():
                it.setCheckState(0, CHECKED if on else UNCHECKED)
        finally:
            self.tree.blockSignals(False)
        self._count()

    def uncheck_all(self) -> None:
        self.tree.blockSignals(True)
        try:
            for it in self._items:
                it.setCheckState(0, UNCHECKED)
        finally:
            self.tree.blockSignals(False)
        self._count()

    def checked(self) -> list:
        return [it.data(0, _ROLE) for it in self._items
                if it.checkState(0) == CHECKED]

    def _count(self) -> None:
        self.info.setText("%d checked -- %d of %d parameters shown" % (
            len(self.checked()), len(self.shown()), len(self._items)))

    def _accept(self) -> None:
        self._chosen = self.checked()
        self.accept()

    def chosen(self) -> list:
        """The parameters checked when OK was pressed."""
        return list(self._chosen)


def pick(parent, entities: Iterable, *, taken: Iterable[str] = (), title="Add tables",
         intro: str = "", partial: Optional[Dict[str, str]] = None) -> Optional[list]:
    """Run the picker; the chosen parameters, or None when cancelled."""
    dlg = TablePicker(entities, parent, taken=taken, title=title, intro=intro,
                      partial=partial)
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return dlg.chosen()


# ------------------------------------------------- found changed <-> whole
def _primary_entity(primary, name: str):
    from . import patch as _patch
    for e in list(primary.entities) + _patch._variants(primary):
        if e.name == name:
            return e
    return None


def whole_from_diff(primary, comparison, entry):
    """The patch entry ``entry`` (found changed between ``primary`` and
    ``comparison``) taken whole instead: the same values, every breakpoint
    written on apply too. None when its parameter cannot be found again."""
    from . import patch as _patch
    e = _primary_entity(primary, entry.name)
    if e is None:
        return None
    cp = primary.counterpart(comparison, e)
    if cp is None:
        return None
    w = _patch.whole_entry(cp, e)
    w.name = entry.name                  # the name it is found by on apply
    w.coding = entry.coding
    w.selected = True
    return w


def values_names(primary, comparison, entries) -> Dict[str, int]:
    """``{name in comparison: index}`` of the entries found changed (not
    whole) -- how the picker, which lists the comparison's parameters, knows
    them (a coding copy's name holds its address, which can differ)."""
    out = {}
    for i, pe in enumerate(entries):
        if getattr(pe, "whole", False):
            continue
        out[pe.name] = i
        e = _primary_entity(primary, pe.name)
        cp = primary.counterpart(comparison, e) if e is not None else None
        if cp is not None:
            out[cp.name] = i
    return out


def changed_note(pe) -> str:
    if getattr(pe, "relative", False):
        from .gui_relpatch import change_text
        return "change: " + change_text(pe)
    if pe.changed_cells is None:
        return "structure differs"
    return "%d/%d changed" % (pe.changed_cells, pe.total_cells)
