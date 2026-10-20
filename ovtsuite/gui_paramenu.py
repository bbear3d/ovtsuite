# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The parameter tree's right-click menu, a parameter's Properties window,
and the dialog for parameters the user defines (:mod:`ovtsuite.userparams`).

The window code reaches this module through one hook: ``Workspace``
calls :func:`attach` on its tree. Everything the menu does goes through the
tab's own methods (``open_table``, ``open_hex``, ``copy_from_tab``,
``open_surface`` ...), so it behaves exactly like the menus and buttons that
already do those things.
"""

from __future__ import annotations

import html
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

from . import userparams as _up


# ------------------------------------------------------------------ helpers
def _g():
    from . import gui
    return gui


def entity_of(item):
    """The parameter a tree row holds, or None for a heading."""
    if item is None:
        return None
    e = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
    return e if e is not None and hasattr(e, "rows") else None


def _view_only(ws) -> bool:
    return bool(getattr(ws, "view_only", None))


def _step(ax) -> int:
    return 4 if getattr(ax, "is_float", False) else max(1, ax.bits // 8)


def spans(e) -> List[Tuple[str, int, int]]:
    """``(part, start, end)`` of every byte range ``e`` has in the binary:
    its values, then each axis stored there (an axis a definition gives as
    numbers has none)."""
    out = [("Values", e.addr, e.end)]
    for tag, ax in (("X axis", e.x), ("Y axis", e.y)):
        if ax is not None and ax.addr is not None and ax.count:
            out.append((tag, ax.addr, ax.addr + ax.count * _step(ax)))
    return out


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return ("%.6g" % v)
    return str(v)


def values_tsv(e) -> str:
    """The table as tab-separated text, axes as the header row and column
    (as the grid's Copy gives them), for a spreadsheet."""
    vals = e.values()
    xs = e.x_values() if e.x is not None else []
    ys = e.y_values() if e.y is not None else []
    rows = []
    hx = len(xs) == e.cols and e.cols > 1
    hy = len(ys) == e.rows and e.rows > 1
    if hx:
        rows.append(([""] if hy else []) + [_fmt(x) for x in xs])
    for r, row in enumerate(vals):
        rows.append(([_fmt(ys[r])] if hy else []) + [_fmt(v) for v in row])
    return "\n".join("\t".join(r) for r in rows)


def _cell_format(bits, signed, is_float, endian) -> str:
    order = "little-endian" if endian == "<" else "big-endian"
    if is_float:
        return "32-bit float, %s" % order
    if bits == 8:
        return "8-bit %s" % ("signed" if signed else "unsigned")
    return "%d-bit %s, %s" % (bits, "signed" if signed else "unsigned", order)


def _range(vals) -> Optional[Tuple[float, float]]:
    fin = [v for v in vals if isinstance(v, (int, float)) and v == v]
    return (min(fin), max(fin)) if fin else None


def _comparison_counterpart(ws, e):
    c = getattr(ws, "comparison", None)
    if c is None or ws.primary is None:
        return None
    try:
        return ws.primary.counterpart(c, e)
    except Exception:
        return None


# ------------------------------------------------------------ properties
def properties(ws, e) -> dict:
    """What the Properties window shows, as plain data:
    ``{"general": [(label, text)], "storage": [row dicts], "values":
    [(label, text)]}``. A storage row has ``part, start, end, bytes, cells,
    format, equation, units, given`` (``start`` None for an axis given as
    numbers)."""
    g = _g()
    main, axes = g._parse_desc(e)
    endian = getattr(getattr(e, "binfile", None), "endian", ">")
    title = g._english(e)
    general = [("Title", title), ("Symbol", e.name or "(none)")]
    curated = ""
    try:
        curated = g._catalogue.display_name(e) or ""
    except Exception:
        pass
    if curated and curated != title:
        general.append(("Catalogue name", curated))
    shape = g._shape_text(e)
    general.append(("Kind", g._kind_text(e) + ("  (%s)" % shape if shape else "")))
    general.append(("Units", e.units or "-"))
    desc = (e.description or "").strip()
    if main:
        general.append(("Description", main))
    for tag in ("Z", "X", "Y"):
        if axes.get(tag):
            general.append(("%s describes" % tag, axes[tag]))
    if desc and not main:
        general.append(("Description", desc))
    general.append(("Group", g.tier_name(e.tier)))
    path = list(getattr(e, "category_path", None) or ())
    if path:
        from .signal_names import translate_german
        general.append(("Category", " / ".join(translate_german(p)
                                                for p in path)))
    elif e.categories:
        general.append(("Categories", ", ".join(e.categories)))
    if getattr(e, "selector", "") and getattr(e, "set_index", ""):
        general.append(("Set member", "#%s, picked by %s%s" % (
            e.set_index, e.selector,
            (": " + e.set_note) if getattr(e, "set_note", "") else "")))
    if getattr(e, "coding", ""):
        general.append(("Coding", e.coding))
    from .model import placement_trusted
    src = e.source or "-"
    if _up.is_user(e):
        src = "defined by you (user parameter)"
    elif e.source == "definition":
        src = "from the definition file packaged with this binary"
    elif not placement_trusted(e):
        src += " (address inferred -- check before editing)"
    general.append(("Placement", src))
    if e.note and not _up.is_user(e):
        general.append(("Placement note", e.note))

    storage = []
    n = e.n_cells
    storage.append({
        "part": "Values", "start": e.addr, "end": e.end,
        "bytes": e.end - e.addr, "cells": "%d x %d" % (e.rows, e.cols),
        "format": _cell_format(e.bits, e.signed, e.is_float, endian)
        + ("" if e.kind != "map" else
           (", columns first (Y fastest)" if e.order == "col"
            else ", rows first (X fastest)")),
        "equation": e.equation or "X", "units": e.units or "",
        "given": False})
    for tag, ax in (("X axis", e.x), ("Y axis", e.y)):
        if ax is None:
            continue
        if ax.addr is None:
            storage.append({
                "part": tag, "start": None, "end": None, "bytes": 0,
                "cells": "%d" % (len(ax.values or []) or ax.count),
                "format": "given as values by the definition",
                "equation": "", "units": ax.units or "", "given": True})
            continue
        step = _step(ax)
        signed = ax.signed if ax.signed is not None else False
        fmt = _cell_format(ax.bits if not ax.is_float else 32, signed,
                           ax.is_float, endian)
        if ax.signed is None and not ax.is_float:
            fmt += " (sign read from the values)"
        storage.append({
            "part": tag, "start": ax.addr, "end": ax.addr + ax.count * step,
            "bytes": ax.count * step, "cells": "%d" % ax.count,
            "format": fmt, "equation": ax.equation or "X",
            "units": ax.units or "", "given": False})

    values = []
    try:
        rng = _range([v for row in e.values() for v in row])
    except Exception:
        rng = None
    if rng is not None:
        values.append(("Values now", "%s to %s %s" % (
            _fmt(rng[0]), _fmt(rng[1]), e.units or "")))
    for tag, ax, get in (("X axis", e.x, e.x_values), ("Y axis", e.y, e.y_values)):
        if ax is None:
            continue
        try:
            r = _range(get())
        except Exception:
            r = None
        if r is not None:
            values.append((tag, "%s to %s %s" % (_fmt(r[0]), _fmt(r[1]),
                                                ax.units or "")))
    try:
        lo, hi = e.value_range
        if not e.is_float:
            values.append(("Storable", "%s to %s %s (raw %d to %d)" % (
                _fmt(lo), _fmt(hi), e.units or "", *e.raw_range)))
    except Exception:
        pass
    try:
        eq = e.eq
        if eq.linear:
            values.append(("Conversion", "physical = raw x %s + %s" % (
                _fmt(eq.factor), _fmt(eq.offset))))
    except Exception:
        pass
    cp = _comparison_counterpart(ws, e)
    if getattr(ws, "comparison", None) is not None:
        if cp is None:
            values.append(("Comparison", "no matching parameter"))
        else:
            try:
                same = cp.values() == e.values()
            except Exception:
                same = None
            values.append(("Comparison", "at 0x%06X%s" % (
                cp.addr, "" if same is None else
                (", same values" if same else ", values differ"))))
    return {"general": general, "storage": storage, "values": values, "n": n}


def properties_text(ws, e) -> str:
    """The Properties window as plain text (its Copy all)."""
    p = properties(ws, e)
    out = []
    w = max(len(k) for k, _v in p["general"] + p["values"]) + 2
    for k, v in p["general"]:
        out.append("%s%s" % ((k + ":").ljust(w), v))
    out.append("")
    out.append("Storage")
    for r in p["storage"]:
        if r["given"]:
            out.append("  %-7s given as values (%s points) %s" % (
                r["part"], r["cells"], r["units"]))
            continue
        out.append("  %-7s 0x%06X-0x%06X  %d bytes  %s  %s  eq %s  %s" % (
            r["part"], r["start"], r["end"] - 1, r["bytes"], r["cells"],
            r["format"], r["equation"], r["units"]))
    if p["values"]:
        out.append("")
        for k, v in p["values"]:
            out.append("%s%s" % ((k + ":").ljust(w), v))
    return "\n".join(out).rstrip() + "\n"


class ParameterPropertiesDialog(QtWidgets.QDialog):
    """Everything known about one parameter: what it is, where each part of
    it is stored and how, its equations and its current range. Non-modal;
    each storage row can be shown in the hex editor."""

    def __init__(self, ws, e, parent=None):
        super().__init__(parent or ws)
        self.ws, self.entity = ws, e
        g = _g()
        self.setWindowTitle("Properties - %s" % (g._english(e) or e.name))
        try:
            self.setWindowIcon(g.app_icon())
        except Exception:
            pass
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        p = properties(ws, e)
        v = QtWidgets.QVBoxLayout(self)

        gen = QtWidgets.QGroupBox("General")
        form = QtWidgets.QFormLayout(gen)
        for k, val in p["general"]:
            lbl = QtWidgets.QLabel(html.escape(str(val)))
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            if k == "Symbol":
                lbl.setText("<code>%s</code>" % html.escape(str(val)))
            form.addRow(k + ":", lbl)
        v.addWidget(gen)

        st = QtWidgets.QGroupBox("Storage")
        sv = QtWidgets.QVBoxLayout(st)
        cols = ["Part", "Start", "End", "Bytes", "Cells", "Format",
                "Equation", "Units", ""]
        self.table = QtWidgets.QTableWidget(len(p["storage"]), len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hex_buttons = []
        for i, r in enumerate(p["storage"]):
            given = r["given"]
            cells = [r["part"],
                     "" if given else "0x%06X" % r["start"],
                     "" if given else "0x%06X" % (r["end"] - 1),
                     "" if given else str(r["bytes"]),
                     r["cells"], r["format"], r["equation"], r["units"]]
            for c, text in enumerate(cells):
                self.table.setItem(i, c, QtWidgets.QTableWidgetItem(text))
            if not given:
                b = QtWidgets.QPushButton("Show in hex")
                b.setToolTip("Open the hex editor with these bytes selected")
                b.setEnabled(not _view_only(ws))
                b.clicked.connect(lambda _c=False, a=r["start"], z=r["end"]:
                                  show_in_hex(self.ws, a, z))
                self.table.setCellWidget(i, len(cols) - 1, b)
                self.hex_buttons.append(b)
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()   # rows as tall as their buttons
        self.table.horizontalHeader().setStretchLastSection(True)
        sv.addWidget(self.table)
        v.addWidget(st, 1)                  # a taller window: more Storage

        if p["values"]:
            vals = QtWidgets.QGroupBox("Values")
            vf = QtWidgets.QFormLayout(vals)
            for k, val in p["values"]:
                lbl = QtWidgets.QLabel(html.escape(str(val)))
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(
                    QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
                vf.addRow(k + ":", lbl)
            v.addWidget(vals)

        btns = QtWidgets.QHBoxLayout()
        self.copy_btn = QtWidgets.QPushButton("Copy all")
        self.copy_btn.setToolTip("Copy these properties as plain text")
        self.copy_btn.clicked.connect(self.copy_all)
        btns.addWidget(self.copy_btn)
        if _up.is_user(e) and not _view_only(ws):
            ed = QtWidgets.QPushButton("Edit parameter...")
            ed.clicked.connect(lambda: (self.close(), edit_parameter(ws, e)))
            btns.addWidget(ed)
        btns.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        btns.addWidget(close)
        v.addLayout(btns)
        self.setSizeGripEnabled(True)
        self._fit_storage()

    def _fit_storage(self):
        """Wide enough for every Storage column and tall enough for every
        row (a few at most), within the screen; the window stays resizable."""
        t = self.table
        frame = 2 * t.frameWidth()
        width = frame + t.verticalScrollBar().sizeHint().width() + sum(
            t.columnWidth(c) for c in range(t.columnCount()))
        rows = min(t.rowCount(), 8)         # more than that: it scrolls
        height = frame + t.horizontalHeader().sizeHint().height() + sum(
            t.rowHeight(i) for i in range(rows))
        t.setMinimumHeight(height)
        t.setMinimumWidth(min(width, 900))
        screen = self.screen().availableGeometry() if self.screen() else None
        hint = self.sizeHint()
        w, h = max(720, hint.width()), max(560, hint.height())
        if screen is not None:
            w = min(w, int(screen.width() * 0.9))
            h = min(h, int(screen.height() * 0.9))
        self.resize(w, h)

    def copy_all(self):
        QtWidgets.QApplication.clipboard().setText(
            properties_text(self.ws, self.entity))


def show_properties(ws, e):
    d = ParameterPropertiesDialog(ws, e)
    d.setWindowModality(QtCore.Qt.WindowModality.NonModal)
    d.show()
    return d


# --------------------------------------------------------------- actions
def show_in_hex(ws, start, end):
    """Open the tab's hex editor with bytes ``[start, end)`` selected."""
    dlg = ws.open_hex()
    if dlg is None:
        dlg = ws.tool("hex")
    if dlg is not None and hasattr(dlg, "select_range"):
        dlg.select_range(start, end)
    return dlg


class _ComparisonTab:
    """The tab's comparison, shaped like the other tab ``copy_from_tab``
    copies from."""

    def __init__(self, ws):
        self.primary = ws.comparison

    def display_name(self):
        return "the comparison (%s)" % self.primary.binfile.name


def copy_from_comparison(ws, e) -> int:
    """The whole of ``e`` from the comparison, in one undoable step (as
    Copy whole table from tab does)."""
    ws.open_table(e)
    ws.table.clearSelection()
    return ws.copy_from_tab(_ComparisonTab(ws))


def open_surface(ws, e, on=True):
    """Show 3D surface: ``e`` in the table, the surface above it (or hide
    the surface again)."""
    from . import gui_surface3d as _s3d
    if not on:
        _s3d.set_shown(ws, False)
        return
    ws.open_table(e)
    ws.open_surface()


def _copy(text):
    QtWidgets.QApplication.clipboard().setText(text or "")


def _set_expanded(item, on: bool):
    item.setExpanded(on)
    for i in range(item.childCount()):
        ch = item.child(i)
        if ch.childCount():
            _set_expanded(ch, on)


# ------------------------------------------------------------------ menu
def build_menu(ws, item, parent=None) -> Optional[QtWidgets.QMenu]:
    """The right-click menu for tree row ``item`` (None: the empty space
    below the rows). None when the tab has no file."""
    if getattr(ws, "primary", None) is None:
        return None
    m = QtWidgets.QMenu(parent or ws.tree)
    e = entity_of(item)
    ro = _view_only(ws)
    if e is not None:
        _entity_actions(ws, m, e, ro)
    elif item is not None:
        m.addAction("Expand all", lambda: _set_expanded(item, True))
        m.addAction("Collapse all", lambda: _set_expanded(item, False))
    m.addSeparator()
    add = m.addAction("Add parameter...", lambda: add_parameter(ws))
    add.setEnabled(not ro)
    add.setToolTip("Define a table or value the definitions do not have")
    try:
        from . import gui_ai
        gui_ai._ai_actions(ws, m, e)
    except Exception:
        pass
    m.setToolTipsVisible(True)
    return m


def _entity_actions(ws, m, e, ro):
    m.addAction("Open", lambda: ws.open_table(e))
    m.addAction("Open in a new table tab", lambda: ws.open_table(e, new_tab=True))
    m.addAction("Keep open", lambda: ws.open_table(e, pin=True))
    m.addSeparator()
    hx = m.addMenu("Show in hex editor")
    for part, a, z in spans(e):
        label = {"Values": "Map data", "X axis": "X axis",
                 "Y axis": "Y axis"}[part]
        if part == "Values" and e.kind == "constant":
            label = "Value"
        act = hx.addAction("%s  (0x%06X)" % (label, a),
                           lambda a=a, z=z: show_in_hex(ws, a, z))
        act.setEnabled(not ro)
    if ro:
        hx.setEnabled(False)
        hx.setToolTip("Not in a read-only tab")
    m.addAction("Properties...", lambda: show_properties(ws, e))
    if e.kind in ("map", "curve") and not _up.is_user(e):
        from . import gui_resize as _gr
        why = _gr.availability(ws, e)
        rs = m.addAction("Resize axes...", lambda: _gr.resize_table(ws, e))
        rs.setEnabled(why is None)
        rs.setToolTip(why or "Change how many breakpoints this table has")
    from . import gui_eoi as _ge
    if _ge.involved(ws, e):
        m.addAction("End of injection...", lambda: _ge.open_for(ws, e))
    cp = m.addMenu("Copy")
    if e.name:
        cp.addAction("Symbol", lambda: _copy(e.name))
    cp.addAction("Title", lambda: _copy(_g()._english(e)))
    cp.addAction("Address  (0x%06X)" % e.addr, lambda: _copy("0x%06X" % e.addr))
    cp.addAction("Values", lambda: _copy(values_tsv(e)))
    cp.addAction("Properties", lambda: _copy(properties_text(ws, e)))
    from . import gui_surface3d as _s3d
    sf = m.addAction("Show 3D surface")
    sf.setCheckable(True)
    sf.setChecked(_s3d.is_shown(ws))
    sf.toggled.connect(lambda on: open_surface(ws, e, on))
    sf.setEnabled((e.rows >= 2 and e.cols >= 2) or sf.isChecked())
    if getattr(ws, "comparison", None) is not None:
        m.addSeparator()
        m.addAction("Show differences...", ws.show_differences)
        other = _comparison_counterpart(ws, e)
        cc = m.addAction("Copy from comparison",
                         lambda: copy_from_comparison(ws, e))
        cc.setEnabled(other is not None and not ro)
        if other is None:
            cc.setToolTip("The comparison has no matching parameter")
    if _up.is_user(e):
        m.addSeparator()
        ed = m.addAction("Edit user parameter...", lambda: edit_parameter(ws, e))
        dl = m.addAction("Delete user parameter", lambda: delete_parameter(ws, e))
        ed.setEnabled(not ro)
        dl.setEnabled(not ro)
    elif e.kind in ("map", "curve", "constant", "indexed") and \
            not getattr(e, "text_kind", ""):
        m.addSeparator()
        du = m.addAction("Duplicate as user parameter...",
                         lambda: duplicate_parameter(ws, e))
        du.setEnabled(not ro)
        du.setToolTip("Your own copy of this definition, to change as you "
                      "like; kept with the project, never overwritten")


def attach(ws):
    """Give the tab's parameter tree its right-click menu."""
    tree = ws.tree
    tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)

    def menu_at(pos):
        m = build_menu(ws, tree.itemAt(pos))
        if m is not None:
            m.exec(tree.viewport().mapToGlobal(pos))
    tree.customContextMenuRequested.connect(menu_at)


# ------------------------------------------------------- user parameters
def _store(ws):
    """The tab's user parameters, kept: in its project, on its comparison
    when that is a version of the same file, and in its tree."""
    specs = _up.specs(ws.primary)
    ref = getattr(ws, "project_ref", None)
    if ref is not None:
        ref.doc.project.set_user_params(ref.sid, specs)
        try:
            from . import gui_project as _gp
            _gp.refresh_panel(ws.win)
            _gp.autosave_soon(ws.win, ref.doc)
        except Exception:
            pass
    c = getattr(ws, "comparison", None)
    if c is not None and getattr(ws, "cmp_link", None) is None and \
            len(c.binfile.data) == len(ws.primary.binfile.data):
        try:
            _up.apply(c, specs)
        except Exception:
            pass
    ws._changed_cache = None
    ws._tree_sig = None                 # a parameter hidden or shown again
    ws._populate()


def _close_tables_of(ws, e):
    i = ws._table_index(e)
    if i >= 0:
        ws.close_table(i)


def add_parameter(ws, spec=None):
    """Ask for a new user parameter and add it. Returns the entity."""
    if ws.primary is None or _view_only(ws):
        return None
    d = UserParamDialog(ws, spec)
    if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    e = _up.put(ws.primary, d.spec())
    _store(ws)
    if e is not None:
        ws.show_entity(e)
    return e


def duplicate_parameter(ws, e):
    """Duplicate as user parameter: the dialog, started from what ``e``
    reads, standing in for it (its original hidden unless unticked).
    Returns the new entity."""
    if ws.primary is None or _view_only(ws) or _up.is_user(e):
        return None
    try:
        spec = _up.from_entity(e, ws.primary)
    except ValueError as ex:
        QtWidgets.QMessageBox.information(ws, "Duplicate as user parameter", str(ex))
        return None
    return add_parameter(ws, spec)


def edit_parameter(ws, e):
    if ws.primary is None or _view_only(ws) or not _up.is_user(e):
        return None
    d = UserParamDialog(ws, getattr(e, "user_spec", None), editing=e.name)
    if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    _close_tables_of(ws, e)
    n = _up.put(ws.primary, d.spec(), replace=e.name)
    _store(ws)
    if n is not None:
        ws.show_entity(n)
    return n


def delete_parameter(ws, e, confirm=True) -> bool:
    if ws.primary is None or _view_only(ws) or not _up.is_user(e):
        return False
    if confirm:
        r = QtWidgets.QMessageBox.question(
            ws, "Delete user parameter",
            "Delete %s? Only its definition goes; the bytes in the binary "
            "are not changed." % e.name)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
    _close_tables_of(ws, e)
    _up.remove(ws.primary, e.name)
    _store(ws)
    return True


class _AxisBox(QtWidgets.QGroupBox):
    """One axis of a user parameter: stored in the binary, given as values,
    or none (the cells are numbered)."""

    changed = QtCore.Signal()

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        f = QtWidgets.QFormLayout(self)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("None (numbered 0, 1, 2 ...)", "none")
        self.mode.addItem("Stored in the binary", "binary")
        self.mode.addItem("Fixed values", "values")
        f.addRow("Axis:", self.mode)
        self.addr = QtWidgets.QLineEdit()
        self.addr.setPlaceholderText("hex, e.g. 1C2A40")
        f.addRow("Address:", self.addr)
        self.bits = QtWidgets.QComboBox()
        for b in (8, 16, 32):
            self.bits.addItem("%d-bit" % b, b)
        self.bits.setCurrentIndex(1)
        self.signed = QtWidgets.QCheckBox("Signed")
        self.flt = QtWidgets.QCheckBox("Float")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.bits)
        row.addWidget(self.signed)
        row.addWidget(self.flt)
        row.addStretch(1)
        f.addRow("Point:", row)
        self.eq = QtWidgets.QLineEdit("X")
        f.addRow("Equation:", self.eq)
        self.values = QtWidgets.QLineEdit()
        self.values.setPlaceholderText("e.g. 0, 500, 1000, 1500")
        f.addRow("Values:", self.values)
        self.units = QtWidgets.QLineEdit()
        f.addRow("Units:", self.units)
        self._form = f
        for w in (self.mode, self.bits):
            w.currentIndexChanged.connect(self._sync)
        for w in (self.signed, self.flt):
            w.toggled.connect(self._sync)
        for w in (self.addr, self.eq, self.values, self.units):
            w.textChanged.connect(lambda _t: self.changed.emit())
        self._sync()

    def _sync(self, *_a):
        m = self.mode.currentData()
        for w in (self.addr, self.bits, self.signed, self.flt, self.eq):
            w.setEnabled(m == "binary")
        if self.flt.isChecked():
            self.bits.setCurrentIndex(2)
            self.signed.setEnabled(False)
        self.values.setEnabled(m == "values")
        self.units.setEnabled(m != "none")
        self.changed.emit()

    def set_spec(self, ax):
        if not ax:
            self.mode.setCurrentIndex(0)
            return
        if ax.get("values") is not None:
            self.mode.setCurrentIndex(2)
            self.values.setText(", ".join(_fmt(float(v)) for v in ax["values"]))
        else:
            self.mode.setCurrentIndex(1)
            self.addr.setText("%X" % int(ax.get("addr") or 0))
            self.bits.setCurrentIndex(max(0, self.bits.findData(int(ax.get("bits") or 16))))
            self.signed.setChecked(bool(ax.get("signed")))
            self.flt.setChecked(bool(ax.get("float")))
            self.eq.setText(ax.get("equation") or "X")
        self.units.setText(ax.get("units") or "")

    def spec(self):
        """The axis as a spec dict, None for none; ValueError for text that
        is not a number."""
        m = self.mode.currentData()
        if m == "none":
            return None
        if m == "values":
            txt = self.values.text().replace(";", ",")
            vals = [float(t) for t in txt.replace(",", " ").split()]
            return {"values": vals, "units": self.units.text().strip()}
        return {"addr": _parse_addr(self.addr.text()),
                "bits": self.bits.currentData(),
                "signed": self.signed.isChecked(),
                "float": self.flt.isChecked(),
                "equation": self.eq.text().strip() or "X",
                "units": self.units.text().strip()}


def _parse_addr(text) -> int:
    t = (text or "").strip().replace("_", "")
    if t.lower().startswith("0x"):
        t = t[2:]
    if not t:
        raise ValueError("no address")
    return int(t, 16)


class UserParamDialog(QtWidgets.QDialog):
    """Define (or change) a parameter of your own: where its data is, its
    shape, how a cell is stored and converted, and its axes. A live
    preview shows the values it would read; OK is on when it fits the
    binary."""

    def __init__(self, ws, spec=None, editing: Optional[str] = None,
                 parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.session = ws.primary
        self.editing = editing
        g = _g()
        self._replaces = (spec or {}).get("replaces")
        self.setWindowTitle("Edit user parameter" if editing
                            else "Duplicate as user parameter" if self._replaces
                            else "Add parameter")
        try:
            self.setWindowIcon(g.app_icon())
        except Exception:
            pass
        self.resize(820, 640)
        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "A table or value the definitions do not have. It is listed under "
            "<b>User defined</b>, edits like any other table, and is kept with "
            "the project%s." % (
                "" if getattr(ws, "project_ref", None) is not None else
                " (this tab is not a project yet: it is kept when you save it "
                "as one)"))
        intro.setWordWrap(True)
        v.addWidget(intro)

        cols = QtWidgets.QHBoxLayout()
        left = QtWidgets.QVBoxLayout()
        cols.addLayout(left, 1)
        right = QtWidgets.QVBoxLayout()
        cols.addLayout(right, 1)
        v.addLayout(cols, 1)

        about = QtWidgets.QGroupBox("Parameter")
        f = QtWidgets.QFormLayout(about)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("symbol, e.g. MyBoostLimit_MAP")
        f.addRow("Symbol:", self.name)
        self.title = QtWidgets.QLineEdit()
        self.title.setPlaceholderText("shown in the tree (default: the symbol)")
        f.addRow("Title:", self.title)
        self.folder = QtWidgets.QLineEdit()
        self.folder.setPlaceholderText("folder under User defined (optional)")
        f.addRow("Folder:", self.folder)
        self.desc = QtWidgets.QLineEdit()
        f.addRow("Description:", self.desc)
        self.hide_orig = QtWidgets.QCheckBox()
        self.hide_orig.setToolTip(
            "The tree, Find and the parameter filter then show this copy, in "
            "the original's place, instead of the original")
        f.addRow("", self.hide_orig)
        self.hide_orig.setVisible(bool(self._replaces))
        if self._replaces:
            self.hide_orig.setText("Hide the original (%s)"
                                   % self._replaces.get("name", ""))
        left.addWidget(about)

        data = QtWidgets.QGroupBox("Data")
        f = QtWidgets.QFormLayout(data)
        self.kind = QtWidgets.QComboBox()
        self.kind.addItem("Map (rows x columns, two axes)", "map")
        self.kind.addItem("Curve (one row, one axis)", "curve")
        self.kind.addItem("Value (one cell)", "constant")
        f.addRow("Kind:", self.kind)
        self.addr = QtWidgets.QLineEdit()
        self.addr.setPlaceholderText("hex, e.g. 1C2A40")
        f.addRow("Address:", self.addr)
        shape = QtWidgets.QHBoxLayout()
        self.rows = QtWidgets.QSpinBox()
        self.rows.setRange(1, 1024)
        self.cols = QtWidgets.QSpinBox()
        self.cols.setRange(1, 1024)
        shape.addWidget(QtWidgets.QLabel("rows (Y)"))
        shape.addWidget(self.rows)
        shape.addWidget(QtWidgets.QLabel("columns (X)"))
        shape.addWidget(self.cols)
        shape.addStretch(1)
        f.addRow("Size:", shape)
        cell = QtWidgets.QHBoxLayout()
        self.bits = QtWidgets.QComboBox()
        for b in (8, 16, 32):
            self.bits.addItem("%d-bit" % b, b)
        self.signed = QtWidgets.QCheckBox("Signed")
        self.flt = QtWidgets.QCheckBox("Float")
        cell.addWidget(self.bits)
        cell.addWidget(self.signed)
        cell.addWidget(self.flt)
        cell.addStretch(1)
        f.addRow("Cell:", cell)
        self.order = QtWidgets.QComboBox()
        self.order.addItem("Rows first (X varies fastest)", "row")
        self.order.addItem("Columns first (Y varies fastest)", "col")
        f.addRow("Stored:", self.order)
        self.eq = QtWidgets.QLineEdit("X")
        self.eq.setToolTip("Raw to physical, in X: e.g. X*0.1, (X)/(8192), "
                           "X*0.01-40")
        f.addRow("Equation:", self.eq)
        self.units = QtWidgets.QLineEdit()
        f.addRow("Units:", self.units)
        endian = getattr(self.session.binfile, "endian", ">")
        bo = QtWidgets.QLabel("%s (this binary's)" % (
            "Little-endian" if endian == "<" else "Big-endian"))
        bo.setStyleSheet("color: gray")
        f.addRow("Byte order:", bo)
        left.addWidget(data)
        left.addStretch(1)

        self.xbox = _AxisBox("X axis (columns)")
        self.ybox = _AxisBox("Y axis (rows)")
        right.addWidget(self.xbox)
        right.addWidget(self.ybox)

        pv = QtWidgets.QGroupBox("Preview")
        pl = QtWidgets.QVBoxLayout(pv)
        self.preview = QtWidgets.QTableWidget()
        self.preview.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setMinimumHeight(150)
        pl.addWidget(self.preview)
        self.range_lbl = QtWidgets.QLabel("")
        self.range_lbl.setStyleSheet("color: gray")
        pl.addWidget(self.range_lbl)
        right.addWidget(pv, 1)

        self.errors = QtWidgets.QLabel("")
        self.errors.setWordWrap(True)
        self.errors.setStyleSheet("color: #c0392b")
        v.addWidget(self.errors)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)

        self._set(spec or _up.blank())
        for w in (self.name, self.title, self.folder, self.desc, self.addr,
                  self.eq, self.units):
            w.textChanged.connect(self._update)
        for w in (self.kind, self.bits, self.order):
            w.currentIndexChanged.connect(self._update)
        for w in (self.rows, self.cols):
            w.valueChanged.connect(self._update)
        for w in (self.signed, self.flt):
            w.toggled.connect(self._update)
        self.xbox.changed.connect(self._update)
        self.ybox.changed.connect(self._update)
        self._update()

    # -- the form <-> a spec
    def _set(self, s):
        s = _up.normalise(s)
        self.name.setText(s.get("name", ""))
        self.title.setText(s.get("title", ""))
        self.folder.setText(s.get("folder", ""))
        self.desc.setText(s.get("description", ""))
        self.kind.setCurrentIndex(max(0, self.kind.findData(s["kind"])))
        self.addr.setText("%X" % s["addr"] if s.get("addr") or s.get("name")
                          else "")
        self.rows.setValue(s["rows"])
        self.cols.setValue(s["cols"])
        self.bits.setCurrentIndex(max(0, self.bits.findData(s["bits"])))
        self.signed.setChecked(bool(s.get("signed")))
        self.flt.setChecked(bool(s.get("float")))
        self.order.setCurrentIndex(max(0, self.order.findData(s.get("order", "row"))))
        self.eq.setText(s.get("equation") or "X")
        self.units.setText(s.get("units") or "")
        self.xbox.set_spec(s.get("x"))
        self.ybox.set_spec(s.get("y"))
        self.hide_orig.setChecked(bool(s.get("hide")))

    def spec(self) -> dict:
        """The form as a spec (ValueError for a field that is not a
        number)."""
        kind = self.kind.currentData()
        s = {"name": self.name.text().strip(),
             "title": self.title.text().strip(),
             "folder": self.folder.text().strip(),
             "description": self.desc.text().strip(),
             "kind": kind, "addr": _parse_addr(self.addr.text()),
             "rows": self.rows.value(), "cols": self.cols.value(),
             "bits": self.bits.currentData(),
             "signed": self.signed.isChecked(),
             "float": self.flt.isChecked(),
             "order": self.order.currentData(),
             "equation": self.eq.text().strip() or "X",
             "units": self.units.text().strip(),
             "x": self.xbox.spec() if kind != "constant" else None,
             "y": self.ybox.spec() if kind == "map" else None,
             "replaces": self._replaces,
             "hide": bool(self._replaces) and self.hide_orig.isChecked()}
        return _up.normalise(s)

    def errors_for(self) -> List[str]:
        try:
            s = self.spec()
        except ValueError:
            return ["Enter the addresses in hex (e.g. 1C2A40) and fixed axis "
                    "values as numbers separated by commas."]
        taken = {e.name for e in self.session.entities
                 if e.name != self.editing}
        return _up.validate(s, len(self.session.binfile.data), taken)

    def _update(self, *_a):
        kind = self.kind.currentData()
        self.rows.setEnabled(kind == "map")
        self.cols.setEnabled(kind != "constant")
        self.order.setEnabled(kind == "map")
        self.xbox.setEnabled(kind != "constant")
        self.ybox.setEnabled(kind == "map")
        if self.flt.isChecked():
            self.bits.setCurrentIndex(2)
        self.signed.setEnabled(not self.flt.isChecked())
        errs = self.errors_for()
        self.errors.setText("\n".join(errs))
        self.buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(not errs)
        self._preview(None if errs and not self._shape_ok() else self._safe_spec())

    def _shape_ok(self) -> bool:
        """The data itself fits (the symbol may still be missing): worth a
        preview."""
        try:
            s = self.spec()
        except ValueError:
            return False
        s = dict(s, name=s.get("name") or "preview")
        return not _up.validate(s, len(self.session.binfile.data))

    def _safe_spec(self):
        try:
            s = self.spec()
        except ValueError:
            return None
        return dict(s, name=s.get("name") or "preview")

    def _preview(self, s):
        t = self.preview
        t.clear()
        if s is None:
            t.setRowCount(0)
            t.setColumnCount(0)
            self.range_lbl.setText("")
            return
        try:
            p = _up.preview(s, self.session.binfile, limit=12)
        except Exception as ex:
            t.setRowCount(0)
            t.setColumnCount(0)
            self.range_lbl.setText("Cannot read it: %s" % ex)
            return
        z = p["z"]
        t.setRowCount(len(z))
        t.setColumnCount(len(z[0]) if z else 0)
        xs, ys = p["x"], p["y"]
        t.setHorizontalHeaderLabels(
            [_fmt(xs[c]) if c < len(xs) else str(c) for c in range(t.columnCount())])
        t.setVerticalHeaderLabels(
            [_fmt(ys[r]) if r < len(ys) else str(r) for r in range(t.rowCount())])
        for r, row in enumerate(z):
            for c, val in enumerate(row):
                t.setItem(r, c, QtWidgets.QTableWidgetItem(_fmt(val)))
        t.resizeColumnsToContents()
        rng = p["range"]
        more = (s["rows"] > len(z) or s["cols"] > (len(z[0]) if z else 0))
        self.range_lbl.setText(
            ("Values %s to %s" % (_fmt(rng[0]), _fmt(rng[1])) if rng else "")
            + ("  (first 12 x 12 shown)" if more else ""))


__all__ = ["attach", "build_menu", "entity_of", "spans", "values_tsv",
           "properties", "properties_text", "ParameterPropertiesDialog",
           "show_properties", "show_in_hex", "copy_from_comparison",
           "UserParamDialog", "add_parameter", "edit_parameter",
           "delete_parameter", "duplicate_parameter"]
