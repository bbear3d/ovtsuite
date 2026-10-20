# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Resize axes... dialog (:mod:`ovtsuite.resize`).

Reached from a parameter's right-click menu in the tree and from the table's
own right-click menu. One table and every copy of it (its coding-variant
copies, the same table in the other datasets) change together, as one step
you can undo; in a project the change is kept with the set so the table
keeps its new size however the module's definition is rebuilt.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6 import QtCore, QtWidgets

from . import resize as _rz


def _g():
    from . import gui
    return gui


def _fmt(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    return ("%.4f" % f).rstrip("0").rstrip(".")


def _view_only(ws) -> bool:
    return bool(getattr(ws, "view_only", None))


def availability(ws, e) -> Optional[str]:
    """None when ``e`` can be offered for resizing in tab ``ws``, else why
    not (the menu item's tooltip)."""
    s = getattr(ws, "primary", None)
    if s is None or e is None:
        return "no table"
    if _view_only(ws):
        return "Not in a read-only tab"
    if e.kind not in ("map", "curve"):
        return "Only maps and curves have axes to resize"
    if getattr(e, "source", "") == "user":
        return "A user parameter's size is changed in Edit user parameter..."
    codec = _rz.codec_for(s)
    if codec is None:
        return "The installed module for this ECU can't resize tables"
    main = _rz.main_of(s, e)
    if codec.start_of(main) is None:
        return codec.why(main)
    return None


class ResizeDialog(QtWidgets.QDialog):
    """Choose a table's new number of breakpoints and what they are."""

    def __init__(self, ws, e, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.session = ws.primary
        self.codec = _rz.codec_for(self.session)
        self.e = _rz.main_of(self.session, e)
        self.lims = _rz.all_limits(self.session, self.e, self.codec)
        self.is_map = self.e.kind == "map"
        g = _g()
        title = g._english(self.e) or self.e.name
        self.setWindowTitle("Resize %s" % title)
        try:
            self.setWindowIcon(g.app_icon())
        except Exception:
            pass
        self.resize(760, 600)
        main = self.lims[0]
        prev = next((m for m in _rz.mods(self.session)
                     if _rz.key_of(m) == (self.e.name, self.e.group or "",
                                          self.e.kind)), None)
        self.stock = tuple(prev["stock"]) if prev else (main.nx, main.ny)

        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Changes how many breakpoints <b>%s</b> has. The values are "
            "recalculated on the new breakpoints from the table as it is now "
            "-- exactly what the ECU interpolates there -- so the engine sees "
            "the same values it did inside the old range. Edit them "
            "afterwards. %s"
            % (html_escape(self.e.name),
               "The change is kept with the project, so the table keeps its "
               "size when the definition is rebuilt."
               if getattr(ws, "project_ref", None) is not None else
               "<i>This tab is not a project yet: save it as one to keep the "
               "new size when the definition is rebuilt.</i>"))
        intro.setWordWrap(True)
        intro.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(intro)

        # -- how
        reloc_why = (self.codec.relocate_why(self.session, self.e)
                     if self.codec is not None else _rz.RELOCATE_WHY)
        self._reloc_free = self._free_room()
        how = QtWidgets.QGroupBox("Method")
        hl = QtWidgets.QVBoxLayout(how)
        self.inplace = QtWidgets.QRadioButton(
            "In place: into the space the definition reserves for it "
            "(up to %s here)" % _rz.fmt_dims(self.e, main.max_nx, main.max_ny))
        self.inplace.setChecked(True)
        self.relocate = QtWidgets.QRadioButton(
            "Move it to free space and re-point the ECU at it"
            + (" (%d bytes free)" % self._reloc_free if reloc_why is None
               else ""))
        self.relocate.setEnabled(reloc_why is None)
        self.relocate.setToolTip(reloc_why or
                                 "Write the table to free space in the "
                                 "dataset and re-point the ECU's pointer at "
                                 "it, so it can grow past its reserved space")
        hl.addWidget(self.inplace)
        hl.addWidget(self.relocate)
        if reloc_why is not None:
            n = QtWidgets.QLabel(reloc_why[0].upper() + reloc_why[1:] + ".")
            n.setWordWrap(True)
            n.setStyleSheet("color: gray")
            hl.addWidget(n)
        v.addWidget(how)

        # -- size
        size = QtWidgets.QGroupBox("Size")
        f = QtWidgets.QFormLayout(size)
        row = QtWidgets.QHBoxLayout()
        self.nx = QtWidgets.QSpinBox()
        self.nx.setRange(2, max(main.nx, min(_rz.MAX_POINTS,
                                             max(main.nx, main.decl_nx))))
        self.nx.setValue(main.nx)
        row.addWidget(QtWidgets.QLabel("X points"))
        row.addWidget(self.nx)
        self.inplace.toggled.connect(self._method_changed)
        self.ny = QtWidgets.QSpinBox()
        self.ny.setRange(2, max(main.ny, min(_rz.MAX_POINTS,
                                             max(main.ny, main.decl_ny))))
        self.ny.setValue(main.ny)
        if self.is_map:
            row.addSpacing(12)
            row.addWidget(QtWidgets.QLabel("Y points"))
            row.addWidget(self.ny)
        row.addStretch(1)
        f.addRow("New size:", row)
        facts = "now %s" % _rz.fmt_dims(self.e, main.nx, main.ny)
        if self.stock != (main.nx, main.ny):
            facts += ", stock %s" % _rz.fmt_dims(self.e, *self.stock)
        if not main.reason:
            facts += "; the definition reserves %s, %d bytes free here" % (
                _rz.fmt_dims(self.e, max(main.nx, main.decl_nx),
                             max(main.ny, main.decl_ny)), main.room)
        fl = QtWidgets.QLabel(facts)
        fl.setStyleSheet("color: gray")
        f.addRow("", fl)
        self.fill = QtWidgets.QComboBox()
        self.fill.addItem("Keep the existing points, add new ones past the end",
                          "extend")
        self.fill.addItem("Spread the points evenly over the current range",
                          "even")
        f.addRow("New breakpoints:", self.fill)
        self.axes = QtWidgets.QTableWidget()
        self.axes.setRowCount(2 if self.is_map else 1)
        self.axes.setVerticalHeaderLabels(["X", "Y"] if self.is_map else ["X"])
        self.axes.setFixedHeight(86 if self.is_map else 58)
        self.axes.setToolTip("The breakpoints, in %s. Type over any of them; "
                             "they must keep rising (or falling)."
                             % (self._axis_units() or "display units"))
        f.addRow("Breakpoints:", self.axes)
        v.addWidget(size)

        # -- copies
        cp = QtWidgets.QLabel(self._copies_text())
        cp.setWordWrap(True)
        v.addWidget(cp)
        if self.e.source not in ("anchored", "struct", "verified", "placed"):
            w = QtWidgets.QLabel(
                "This table's address comes from the offset model, not a byte "
                "match. Its record was checked in the image, but check the "
                "result before you flash it.")
            w.setWordWrap(True)
            w.setStyleSheet("color: #b9770e")
            v.addWidget(w)

        # -- preview
        pv = QtWidgets.QGroupBox("Values after resizing (preview)")
        pl = QtWidgets.QVBoxLayout(pv)
        self.preview = QtWidgets.QTableWidget()
        self.preview.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        pl.addWidget(self.preview)
        v.addWidget(pv, 1)

        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #b9770e")
        v.addWidget(self.note)
        self.version = QtWidgets.QCheckBox()
        self.version.setChecked(True)
        if getattr(ws, "project_ref", None) is not None:
            v.addWidget(self.version)
        self.errors = QtWidgets.QLabel("")
        self.errors.setWordWrap(True)
        self.errors.setStyleSheet("color: #c0392b")
        v.addWidget(self.errors)
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.Help)
        self.ok = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Resize")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.helpRequested.connect(self._help)
        v.addWidget(self.buttons)

        self._fill_axes()
        self.nx.valueChanged.connect(self._fill_axes)
        self.ny.valueChanged.connect(self._fill_axes)
        self.fill.currentIndexChanged.connect(self._fill_axes)
        self.axes.itemChanged.connect(lambda *_a: self._update())
        self.plans: List[_rz.Plan] = []
        self._update()

    # -- helpers ---------------------------------------------------------
    def _free_room(self) -> int:
        if self.codec is None or self.codec.relocate_why(
                self.session, self.e) is not None:
            return 0
        try:
            spans = self.codec.free_spans(self.session, self.e)
        except Exception:
            return 0
        return max((l for _a, l in spans), default=0)

    def relocating(self) -> bool:
        return self.relocate.isChecked()

    def _method_changed(self, *_a):
        """Raise the point ceiling to the structural max when relocating (the
        reserved-space cap no longer applies), and back when in place."""
        main = self.lims[0]
        if self.relocating():
            cap = _rz.MAX_POINTS
        else:
            cap = min(_rz.MAX_POINTS, max(main.nx, main.decl_nx))
            capy = min(_rz.MAX_POINTS, max(main.ny, main.decl_ny))
        self.nx.setMaximum(max(self.nx.value(), _rz.MAX_POINTS if
                               self.relocating() else cap))
        if self.is_map:
            self.ny.setMaximum(max(self.ny.value(), _rz.MAX_POINTS if
                                   self.relocating() else capy))
        self._fill_axes()

    def _axis_units(self) -> str:
        ax = self.e.x if self.is_map else _rz.curve_axis(self.e)
        return getattr(ax, "units", "") or ""

    def _copies_text(self) -> str:
        others = self.lims[1:]
        if not others:
            return "No other copies of this table."
        parts = []
        for l in others:
            o = l.entity
            where = (o.group or "") if o.source != "variant" else (
                "coding-variant copy at 0x%X" % o.addr)
            parts.append("%s%s" % (where or o.name,
                                   " (%s)" % l.reason if l.reason else ""))
        return ("Resized with it, so every coding reads the same table: "
                + "; ".join(parts) + ".")

    def _old(self):
        e = self.e
        if self.is_map:
            return e.x_values(), e.y_values()
        return e.axis_values(_rz.curve_axis(e)), []

    def _fill_axes(self, *_a):
        ox, oy = self._old()
        how = self.fill.currentData()
        xs = _rz.spread(ox, self.nx.value(), how)
        ys = _rz.spread(oy, self.ny.value(), how) if self.is_map else []
        n = max(len(xs), len(ys))
        self.axes.blockSignals(True)
        try:
            self.axes.clear()
            self.axes.setColumnCount(n)
            self.axes.setVerticalHeaderLabels(["X", "Y"] if self.is_map
                                              else ["X"])
            for r, vals in enumerate([xs, ys] if self.is_map else [xs]):
                for c in range(n):
                    it = QtWidgets.QTableWidgetItem(
                        _fmt(vals[c]) if c < len(vals) else "")
                    if c >= len(vals):
                        it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
                    self.axes.setItem(r, c, it)
            self.axes.resizeColumnsToContents()
        finally:
            self.axes.blockSignals(False)
        self._update()

    def _row(self, r: int, n: int) -> List[float]:
        out = []
        for c in range(n):
            it = self.axes.item(r, c)
            txt = it.text().strip() if it is not None else ""
            out.append(float(txt.replace(",", ".")))
        return out

    def breakpoints(self):
        """``(new_x, new_y)`` as typed (display units). ValueError if not
        numbers."""
        xs = self._row(0, self.nx.value())
        ys = self._row(1, self.ny.value()) if self.is_map else []
        return xs, ys

    def _update(self):
        self.plans = []
        why = ""
        try:
            xs, ys = self.breakpoints()
        except ValueError:
            why = "Every breakpoint must be a number."
            xs = ys = None
        if not why:
            try:
                if self.relocating():
                    self.plans = _rz.plan(self.session, self.e, xs, ys,
                                          self.codec, mode="relocate")
                else:
                    self.plans = [_rz.plan_one(self.session, l.entity, xs, ys,
                                               self.codec, l)
                                  for l in self.lims]
            except ValueError as ex:
                why = str(ex)
        same = bool(self.plans) and all(p.noop for p in self.plans)
        if same and not why:
            why = "That is the table as it is."
        self.errors.setText(why)
        self.ok.setEnabled(bool(self.plans) and not why)
        self.note.setText((self._mid_note() or self._prepage_note())
                          if self.plans and not why else "")
        lab = self.label()
        self.version.setText("Save the project now, as a version named \"%s\""
                             % lab + ("  (the tab's other unsaved edits go "
                                      "into it too)" if self.ws.is_dirty()
                                      else ""))
        self._preview(xs, ys)

    def _mid_cov(self):
        """The mid-block coverage of the main plan, or None (last-in-block or
        a platform without it)."""
        cov = getattr(self.plans[0], "coverage", None) if self.plans else None
        return cov if cov and cov.get("mode") == "mid" else None

    def _mid_note(self) -> str:
        cov = self._mid_cov()
        if not cov:
            return ""
        parts = []
        if cov["data_ptrs"]:
            parts.append("%d pointer-table entr%s" % (
                cov["data_ptrs"], "y" if cov["data_ptrs"] == 1 else "ies"))
        if cov["code_sites"]:
            parts.append("%d code site%s" % (
                cov["code_sites"], "" if cov["code_sites"] == 1 else "s"))
        return ("This table is in the middle of its block: it is moved to free "
                "space and the %s that reach it are redirected. The original "
                "stays as a fallback, so anything not redirected reads the "
                "stock table. Not yet bench-proven — flash only with BDM "
                "recovery ready, and check the whole table logs as edited."
                % " and ".join(parts))

    def _prepage_note(self) -> str:
        """The caution for moving an EDC15P pre-page table, when relocating
        and the codec offers one; else empty."""
        if not self.relocating():
            return ""
        fn = getattr(self.codec, "relocate_unproven", None)
        return (fn(self.session, self.e) or "") if fn else ""

    def _prepage_move(self) -> bool:
        return bool(self._prepage_note())

    def label(self) -> str:
        nx, ny = self.nx.value(), (self.ny.value() if self.is_map else 1)
        main = self.lims[0]
        verb = "Move + resize" if self.relocating() else "Resize"
        return "%s %s %s -> %s" % (
            verb, self.e.name, _rz.fmt_dims(self.e, main.nx, main.ny),
            _rz.fmt_dims(self.e, nx, ny))

    def _preview(self, xs, ys):
        t = self.preview
        t.clear()
        if not self.plans:
            t.setRowCount(0)
            t.setColumnCount(0)
            return
        e = self.e
        ox, oy = self._old()
        old = e.values()
        if self.is_map:
            z = _rz.resample(ox, oy, old, xs, ys)
        else:
            line = [v for row in old for v in row]
            z = [[_rz._interp(ox, line, x) for x in xs]]
        t.setRowCount(len(z))
        t.setColumnCount(len(z[0]) if z else 0)
        t.setHorizontalHeaderLabels([_fmt(x) for x in xs])
        t.setVerticalHeaderLabels([_fmt(y) for y in ys] if self.is_map
                                  else [""])
        for r, row in enumerate(z):
            for c, val in enumerate(row):
                it = QtWidgets.QTableWidgetItem(_fmt(e.clamp(val)))
                inside = (_inside(ox, xs[c]) and
                          (not self.is_map or _inside(oy, ys[r])))
                if not inside:
                    it.setToolTip("Outside the old axis range: the edge value "
                                  "is held here, as the ECU did")
                    it.setForeground(QtCore.Qt.GlobalColor.darkGray)
                t.setItem(r, c, it)
        t.resizeColumnsToContents()

    def _help(self):
        try:
            from . import gui_help
            gui_help.show_help(self, topic="editor/resizing-tables")
        except Exception:
            pass

    def _accept(self):
        self._update()
        if not self.ok.isEnabled():
            return
        self.accept()


def _inside(axis, v) -> bool:
    if not axis:
        return True
    lo, hi = min(axis), max(axis)
    return lo <= v <= hi


def html_escape(s: str) -> str:
    import html
    return html.escape(s or "")


def resize_table(ws, e) -> bool:
    """Ask for ``e``'s new size and resize it (and its copies) in one undo
    step. True when it was resized."""
    why = availability(ws, e)
    if why:
        QtWidgets.QMessageBox.information(ws, "Resize axes", why)
        return False
    d = ResizeDialog(ws, e)
    if d.exec() != QtWidgets.QDialog.DialogCode.Accepted or not d.plans:
        return False
    if d._mid_cov() is not None:
        r = QtWidgets.QMessageBox.warning(
            ws, "Move a mid-block table",
            "%s is in the middle of its calibration block. Moving it is "
            "not yet proven on a vehicle: a reference the tool could not "
            "find would read the stock table, and a wrong flash needs BDM "
            "recovery. Continue?" % e.name,
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
    elif d._prepage_move():
        r = QtWidgets.QMessageBox.warning(
            ws, "Move a pre-page table",
            "%s is in the ECU's pre-page. Moving it re-points the pre-page "
            "pointer table and leaves the original as a stock fallback. This "
            "is not yet proven on a vehicle, and a wrong flash needs BDM "
            "recovery. Continue?" % e.name,
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
    s = ws.primary
    label = d.label()
    try:
        _rz.execute(s, d.plans, label)
    except Exception as ex:
        QtWidgets.QMessageBox.warning(ws, "Resize axes",
                                      "Nothing was changed: %s" % ex)
        return False
    after_resize(ws)
    ref = getattr(ws, "project_ref", None)
    if ref is not None and d.version.isChecked():
        try:
            from . import gui_project as _gp
            _gp.save(ws, tab_only=True, label=label)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(
                ws, "Resize axes", "The table was resized, but the project "
                "could not be saved: %s" % ex)
    try:
        ws.statusBar().showMessage(
            "%s (%d cop%s). Undo puts it back." % (
                label, len(d.plans), "y" if len(d.plans) == 1 else "ies"),
            8000)
    except Exception:
        pass
    return True


def after_resize(ws) -> None:
    """Bring the tab in line with a resize: keep the modifiers with the
    project and a same-file comparison, rebuild the tree (a coding-variant
    copy is named after its address, which moved) and redraw open tables."""
    s = ws.primary
    lst = _rz.mods(s)
    ref = getattr(ws, "project_ref", None)
    if ref is not None:
        try:
            ref.doc.project.set_layout_mods(ref.sid, lst)
            from . import gui_project as _gp
            _gp.refresh_panel(ws.win)
            _gp.autosave_soon(ws.win, ref.doc)
        except Exception:
            pass
    c = getattr(ws, "comparison", None)
    if c is not None and len(c.binfile.data) == len(s.binfile.data):
        # a version of the same file: it holds the size ITS bytes hold
        try:
            _rz.set_mods(c, _rz.merge(_rz.mods(c), lst))
            _rz.follow_headers(c)
        except Exception:
            pass
    ws._changed_cache = None
    ws._tree_sig = None
    try:
        ws._populate()
    except Exception:
        pass
    try:
        for t in ws.tables():
            if t.entity is not None:
                ws._dress_table_tab(ws._table_index(t.entity))
    except Exception:
        pass
    try:
        ws.model.refresh()
    except Exception:
        pass
    try:
        ws._status()
    except Exception:
        pass


__all__ = ["ResizeDialog", "availability", "resize_table", "after_resize"]
