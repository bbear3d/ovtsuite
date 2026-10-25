# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shift editor's part-load strategy step, its "pedal axis to the ECU's
full torque" step, and the replay of the project's logs.

The strategies and the axis move are :mod:`ovtsuite.gearbox_edit.partload`;
the replay is :mod:`ovtsuite.gearbox_edit.replay` on the logs
:mod:`ovtsuite.shift_replay` reads. The full-load points, the ECU and its
version come from the editor's full-load step (:mod:`ovtsuite.gui_fullload`),
so the two steps always agree on the engine.
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6 import QtCore, QtWidgets

from . import gearbox_edit as GE
from .gearbox_edit import partload as PL
from .gui_compact import WrapCheckBox, compact_combo, fixed_width

LEAVE = "Leave alone"
CUSTOM = "Custom"
#: default strategy per mode (Bear, 2026-10-22: D economy, S sport)
DEFAULTS = {"D": PL.ECONOMY.name, "S": PL.SPORT.name, "": LEAVE}
ROW_TITLES = {"D": "D programs", "S": "S programs", "": "Other programs"}


#: log loads still running after their window closed (kept until done)
_ORPHANS = set()


class _StrategyRow:
    """One mode's strategy: a preset (or Custom / Leave alone) and its four
    numbers, which a preset fills in and editing turns into Custom."""

    def __init__(self, panel, grid: QtWidgets.QGridLayout, row: int, mode: str,
                 default: str):
        self.panel, self.mode = panel, mode
        self.box = QtWidgets.QComboBox()
        for name in list(PL.PRESETS) + [CUSTOM, LEAVE]:
            self.box.addItem(name)
        self.floor = QtWidgets.QSpinBox(minimum=800, maximum=4000, singleStep=50, suffix=" rpm")
        self.floor.setToolTip("Engine rpm after the upshift at light pedal: the lowest the "
                              "next gear is allowed to pull from")
        self.start = QtWidgets.QDoubleSpinBox(decimals=0, minimum=0, maximum=95, suffix=" %")
        self.start.setToolTip("Pedal where the post-shift speed starts to rise")
        self.curve = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0.3, maximum=4.0,
                                              singleStep=0.1)
        self.curve.setToolTip("How the rise goes: above 1 it stays low and rises late "
                              "(economy), below 1 it rises early (sport)")
        self.gap = QtWidgets.QSpinBox(minimum=50, maximum=98, suffix=" %")
        self.gap.setToolTip("The downshift that undoes it sits at this share of the "
                            "post-shift speed (hysteresis)")
        compact_combo(self.box, 8)
        # the mode's name and its preset on one line, the numbers under the
        # column heads below it: narrow enough for the editor's side pane
        grid.addWidget(QtWidgets.QLabel(ROW_TITLES.get(mode, mode)), 2 * row - 1, 0, 1, 2)
        grid.addWidget(self.box, 2 * row - 1, 2, 1, 2)
        for c, w in enumerate((self.floor, self.start, self.curve, self.gap)):
            grid.addWidget(w, 2 * row, c)
        self._busy = False
        self.box.setCurrentText(default)
        self._preset()
        self.box.currentIndexChanged.connect(self._preset)
        for w in (self.floor, self.start, self.curve, self.gap):
            w.valueChanged.connect(self._edited)

    def _preset(self, *_):
        name = self.box.currentText()
        st = PL.PRESETS.get(name)
        self._busy = True
        if st is not None:
            self.floor.setValue(int(st.floor))
            self.start.setValue(st.p_start)
            self.curve.setValue(st.curve)
            self.gap.setValue(int(round(st.gap * 100)))
        self._busy = False
        on = name != LEAVE
        for w in (self.floor, self.start, self.curve, self.gap):
            w.setEnabled(on)
        self.panel.changed.emit()

    def _edited(self, *_):
        if self._busy:
            return
        if self.box.currentText() in PL.PRESETS:
            self.box.blockSignals(True)
            self.box.setCurrentText(CUSTOM)
            self.box.blockSignals(False)
        self.panel.changed.emit()

    def strategy(self) -> Optional[PL.Strategy]:
        name = self.box.currentText()
        if name == LEAVE:
            return None
        return PL.Strategy(name, float(self.floor.value()), float(self.start.value()),
                           float(self.curve.value()), self.gap.value() / 100.0)


class PartLoadPanel(QtWidgets.QWidget):
    """The part-load strategy step's controls."""

    changed = QtCore.Signal()

    def __init__(self, editor):
        super().__init__(editor)
        self.ed = editor
        sset = editor.sset
        modes = {PL.mode_of(s.group) for s in sset.schedules}
        self.by_mode = bool(modes & {"D", "S"})
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        g = QtWidgets.QGridLayout()
        g.setHorizontalSpacing(3)
        for c, t in enumerate(("Floor", "From", "Curve", "Down at")):
            lab = QtWidgets.QLabel(t)
            lab.setStyleSheet("color: palette(mid);")
            g.addWidget(lab, 0, c)
        self.rows: Dict[str, _StrategyRow] = {}
        if self.by_mode:
            for r, m in enumerate(("D", "S", ""), 1):
                self.rows[m] = _StrategyRow(self, g, r, m, DEFAULTS[m])
        else:
            self.rows["*"] = _StrategyRow(self, g, 1, "Ticked schedules", PL.BALANCED.name)
        v.addLayout(g)
        f = QtWidgets.QFormLayout()
        self.fraction = QtWidgets.QSpinBox(minimum=1, maximum=100, value=100, suffix=" %")
        self.fraction.setToolTip("How far each cell moves toward the strategy: less keeps "
                                 "more of the line's own shape")
        fixed_width(self.fraction)
        f.addRow("How far", self.fraction)
        self.ft_chk = WrapCheckBox("Rows where the ECU asks for full torque get the "
                                   "full-load points")
        self.ft_chk.setChecked(True)
        self.ft_chk.setToolTip(
            "From the pedal where the engine's driver's wish already asks for full torque "
            "(every gear, 1500-3500 rpm) the rows are full load in engine terms. The other "
            "way to line the two up is the step 'Pedal axis to the ECU's full torque'.")
        f.addRow(self.ft_chk)
        self.down_chk = QtWidgets.QCheckBox("Downshifts too")
        self.down_chk.setChecked(True)
        self.down_chk.setToolTip("Write the downshift that undoes each upshift as well, "
                                 "at the strategy's hysteresis")
        f.addRow(self.down_chk)
        self.anchor_lbl = QtWidgets.QLabel()
        self.anchor_lbl.setWordWrap(True)
        self.anchor_lbl.setStyleSheet("color: palette(mid);")
        f.addRow(self.anchor_lbl)
        v.addLayout(f)
        self.fraction.valueChanged.connect(lambda *_: self.changed.emit())
        for w in (self.ft_chk, self.down_chk):
            w.toggled.connect(lambda *_: self.changed.emit())

    def activate(self):
        fl = getattr(self.ed, "fl_panel", None)
        if fl is not None:
            fl.activate()

    def _anchors(self):
        fl = getattr(self.ed, "fl_panel", None)
        plan = getattr(fl, "plan", None) if fl is not None else None
        if plan is not None and plan.targets:
            self.anchor_lbl.setText(
                "Full-load points from the engine ECU (the full-load step's settings); "
                "the strategy reaches them at full load.")
            return plan.outputs(), fl
        self.anchor_lbl.setText(
            "No engine ECU in this project: each schedule's own 100 % row is the full-load "
            "point the strategy reaches.")
        return None, fl

    def make_op(self, scope: GE.Scope) -> Optional[PL.PartLoadTarget]:
        anchors, fl = self._anchors()
        if self.by_mode:
            assign = PL.assign_by_mode({"D": self.rows["D"].strategy(),
                                        "S": self.rows["S"].strategy(), "M": None},
                                       self.rows[""].strategy())
        else:
            st = self.rows["*"].strategy()
            assign = lambda _s: st                                          # noqa: E731
        ft = fl.full_torque() if (fl is not None and self.ft_chk.isChecked()) else None
        sc = GE.Scope(scope.schedules, [c for c in scope.lines
                                        if self.ed.sset.line(c).kind == "up"],
                      direction=scope.direction)
        if not sc.lines:
            return None
        return PL.PartLoadTarget(sc, assign, anchors=anchors,
                                 fraction=self.fraction.value() / 100.0, full_torque=ft,
                                 downshifts=self.down_chk.isChecked())


class AxisPanel(QtWidgets.QWidget):
    """The "pedal axis to the ECU's full torque" step's controls."""

    changed = QtCore.Signal()

    def __init__(self, editor):
        super().__init__(editor)
        self.ed = editor
        f = QtWidgets.QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.ft = QtWidgets.QDoubleSpinBox(decimals=1, minimum=40, maximum=99.5, value=90,
                                           suffix=" %")
        self.ft.setToolTip("Pedal where the engine already asks for full torque: the "
                           "gearbox's top part-load row is moved here")
        self.ft_lbl = QtWidgets.QLabel()
        self.ft_lbl.setWordWrap(True)
        self.ft_lbl.setStyleSheet("color: palette(mid);")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.ft)
        row.addWidget(self.ft_lbl, 1)
        f.addRow("Full torque at", row)
        self.keep = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=90, value=50,
                                             suffix=" %")
        self.keep.setToolTip("Breakpoints below this stay where they are; those above it "
                             "(up to the top part-load row) are squeezed toward full torque")
        fixed_width(self.keep)
        f.addRow("Keep rows below", self.keep)
        self.tcc_chk = WrapCheckBox("Move the lock-up (TCC) maps' pedal axes too")
        self.tcc_chk.setChecked(True)
        self.tcc_chk.setToolTip(
            "So lock-up keeps meeting the shifts at the same pedal. The manual-mode "
            "(TIPAN) tables are not moved.")
        f.addRow(self.tcc_chk)
        self.preview = QtWidgets.QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("color: palette(mid);")
        f.addRow(self.preview)
        self._seeded = False
        for w in (self.ft, self.keep):
            w.valueChanged.connect(lambda *_: self.changed.emit())
        self.tcc_chk.toggled.connect(lambda *_: self.changed.emit())

    def activate(self):
        fl = getattr(self.ed, "fl_panel", None)
        if fl is not None:
            fl.activate()
        if not self._seeded and fl is not None:
            fine = [x * 0.5 for x in range(100, 200)]
            got = fl.full_torque(fine)
            self.ft.blockSignals(True)
            if got is not None:
                self.ft.setValue(got)
                self.ft_lbl.setText("from the ECU's driver's wish")
            else:
                self.ft_lbl.setText("the ECU does not reach full torque before 100 %")
            self.ft.blockSignals(False)
            self._seeded = True

    def _tcc_set(self):
        if getattr(self, "_tcc", False) is False:
            try:
                from .gui_gearbox_edit import schedule_set_for
                self._tcc = schedule_set_for(self.ed.session, "tcc")
            except Exception:
                self._tcc = None
        return self._tcc

    def _tcc_entities(self, scope: GE.Scope):
        """``(maps, skipped)``: the lock-up maps whose pedal axis moves with
        the ticked shift programs -- a map moves only when everything it
        serves is ticked (its axis is shared by every characteristic in it);
        ``skipped`` names the maps that also serve a program not ticked."""
        return PL.tcc_maps(self._tcc_set(), self.ed.sset, scope.schedules)

    def make_op(self, scope: GE.Scope) -> Optional[PL.RemapPedalAxis]:
        extra, skipped = self._tcc_entities(scope) if self.tcc_chk.isChecked() else ((), [])
        op = PL.RemapPedalAxis(scope, self.ft.value(), keep_below=self.keep.value(),
                               extra=extra)
        sel = scope.schedules
        if sel:
            old = list(self.ed.sset.schedules[sel[0]].pedal)
            new = op.new_axis(old)
            text = "%s: %s" % (
                self.ed.sset.schedules[sel[0]].key,
                "  ".join("%.1f→%.1f" % (a, b) for a, b in zip(old, new) if abs(a - b) > 0.05)
                if new else "nothing to move")
            if self.tcc_chk.isChecked():
                text += "\nLock-up maps moved with them: %d." % len(extra)
                if skipped:
                    text += (" Left as they are, because they also serve programs "
                             "not ticked: %s." % ", ".join(skipped))
            self.preview.setText(text)
        return op


# ---------------------------------------------------------------- replay
class _LoadWorker(QtCore.QObject):
    """Reads and pairs the project's logs off the GUI thread."""

    progress = QtCore.Signal(str, int, int)
    done = QtCore.Signal(object, str)          # (Drives or None, message)

    def __init__(self, project, tcu_sid, ecu_sids, fuel):
        super().__init__()
        self.args = (project, tcu_sid, ecu_sids, fuel)
        self.stop = False

    def run(self):
        from . import shift_replay as SR
        try:
            d = SR.drives(*self.args, progress=lambda *a: self.progress.emit(*a),
                          cancel=lambda: self.stop)
            self.done.emit(d, "")
        except SR.Cancelled:
            self.done.emit(None, "Cancelled.")
        except Exception as ex:                 # reported in the box
            self.done.emit(None, "The logs could not be read: %s" % ex)


class ReplayBox(QtWidgets.QGroupBox):
    """Replay of the project's paired gearbox + engine logs, before and after
    the edit being set up. The logs are read once, in the background, with a
    progress bar (a project's worth takes tens of seconds); every replay after
    that takes a second or two."""

    def __init__(self, editor):
        super().__init__("Replay on the project's logs")
        self.ed = editor
        self.drives = None
        self.fuel = None
        self.tmax = None
        self._before = None
        self._thread = None
        self._worker = None
        v = QtWidgets.QVBoxLayout(self)
        from .gui_gearbox_edit import _Flow
        row_w = QtWidgets.QWidget()
        row = _Flow(row_w)
        self.run_btn = QtWidgets.QPushButton("Replay")
        self.run_btn.setToolTip(
            "Drive the project's logged trips again with the lines as they are and as the "
            "edit would leave them: each logged moment keeps its speed, pedal and the power "
            "the engine gave; the lines pick the gear and the ECU's own fuel map says what "
            "it burns. Needs gearbox and engine logs taken together.")
        self.run_btn.clicked.connect(lambda: self.run())
        self.live_chk = QtWidgets.QCheckBox("Follow the preview")
        self.live_chk.setChecked(True)
        self.map_btn = QtWidgets.QPushButton("Efficiency map\u2026")
        self.map_btn.setToolTip(
            "The engine's fuel use per kWh over speed and torque, from the ECU's own maps, "
            "with where the logged driving ran -- before and after the edit")
        self.map_btn.clicked.connect(self.open_map)
        row.addWidget(self.run_btn)
        row.addWidget(self.live_chk)
        row.addWidget(self.map_btn)
        v.addWidget(row_w)
        prog = QtWidgets.QHBoxLayout()
        self.bar = QtWidgets.QProgressBar()
        self.bar.setTextVisible(True)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        prog.addWidget(self.bar, 1)
        prog.addWidget(self.cancel_btn)
        self.prog_w = QtWidgets.QWidget()
        self.prog_w.setLayout(prog)
        prog.setContentsMargins(0, 0, 0, 0)
        self.prog_w.setVisible(False)
        v.addWidget(self.prog_w)
        self.prog_lbl = QtWidgets.QLabel()
        self.prog_lbl.setStyleSheet("color: palette(mid);")
        self.prog_lbl.setVisible(False)
        sp = self.prog_lbl.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        self.prog_lbl.setSizePolicy(sp)
        v.addWidget(self.prog_lbl)
        self.table = QtWidgets.QTableWidget(2, 5)
        self.table.setHorizontalHeaderLabels(["l/100 km", "Short of power", "Shifts/h",
                                              "Mean rpm", "In logged gear"])
        self.table.setVerticalHeaderLabels(["Before", "After"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setMaximumHeight(72)
        v.addWidget(self.table)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: palette(mid);")
        v.addWidget(self.note)
        self._timer = QtCore.QTimer(self, singleShot=True, interval=500)
        self._timer.timeout.connect(self._after)
        ok = getattr(editor.ws, "project_ref", None) is not None and \
            getattr(editor, "fl_panel", None) is not None
        self.run_btn.setEnabled(ok)
        self.map_btn.setEnabled(ok)
        if not ok:
            self.note.setText("Open the gearbox from a project that also holds the engine "
                              "ECU and logs taken with both.")

    # ------------------------------------------------------------ loading
    def _prepare(self) -> Optional[tuple]:
        """The engine (on the GUI thread: sessions are built here) and what the
        loader needs; None with a note when there is nothing to load."""
        from . import fullload_ecu as fe
        from . import shift_replay as SR
        fl = self.ed.fl_panel
        fl.activate()
        m = fl.model()
        if m is None:
            self.note.setText("No engine ECU with a torque model in this project.")
            return None
        self.fuel = SR.EngineFuel(m)
        proj = fl.project
        ecus = [s.id for s in fe.ecu_sets(proj)]
        if not SR.pairs(proj, self.ed.ws.project_ref.sid, ecus):
            self.note.setText("No gearbox log in this project has an engine log taken with "
                              "it (they pair by start time, within 10 s).")
            return None
        return proj, self.ed.ws.project_ref.sid, ecus, self.fuel

    def _progress(self, label, done, total):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.bar.setFormat("%p %")
        self.prog_lbl.setText(label)

    def _loaded(self, drives, message):
        from . import shift_replay as SR
        self.prog_w.setVisible(False)
        self.prog_lbl.setVisible(False)
        self.run_btn.setEnabled(True)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None
        if drives is None or not drives.samples:
            self.drives = None
            self.note.setText(message or "No usable samples in the paired logs.")
            return
        self.drives = drives
        self.tmax = SR.envelope_fn(self.ed.fl_panel.curve, drives.envelope)
        self._replay_now()

    def run(self, wait: bool = False):
        """Replay before and after; reads the logs first (in the background,
        or right here with ``wait``)."""
        if self.drives is not None:
            self._replay_now()
            return
        if self._thread is not None:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            args = self._prepare()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if args is None:
            return
        self.run_btn.setEnabled(False)
        self.prog_w.setVisible(True)
        self.prog_lbl.setVisible(True)
        self._progress("Pairing the logs", 0, 1)
        self._worker = _LoadWorker(*args)
        self._worker.progress.connect(self._progress)
        self._worker.done.connect(self._loaded)
        if wait:
            self._worker.run()                 # signals deliver directly
            return
        self._thread = QtCore.QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def cancel(self):
        if self._worker is not None:
            self._worker.stop = True
            self.prog_lbl.setText("Cancelling\u2026")

    def closeEvent(self, ev):
        self.cancel()
        super().closeEvent(ev)

    def shutdown(self):
        """Stop a load still running (the editor is closing). One that does
        not stop within 5 s (cancel is seen between logs) is let finish on
        its own: its thread outlives the window, then goes."""
        self.cancel()
        t, w = self._thread, self._worker
        if t is None:
            return
        t.quit()
        if not t.wait(5000):
            t.setParent(None)
            _ORPHANS.add((t, w))
            t.finished.connect(lambda t=t, w=w: (_ORPHANS.discard((t, w)),
                                                 t.deleteLater()))
        self._thread = None

    # ------------------------------------------------------------- replay
    def _source(self, grid, pedal_of):
        from .gearbox_edit import replay as RP
        sset = self.ed.sset
        sel = self.ed._selected()
        bank = sset.schedules[sel[0]].bank if sel else 0
        corners = RP.program_corners(sset, self.ed._blends(), sel, bank)
        return RP.line_source(sset, grid, pedal_of, corners)

    def _ratios(self):
        r = {}
        for ln in self.ed.sset.lines:
            if ln.gear_left and ln.ratio_left:
                r[ln.gear_left] = ln.ratio_left
            if ln.gear_entered and ln.ratio_entered:
                r[ln.gear_entered] = ln.ratio_entered
        return r

    def _metrics(self, grid, pedal_of, points=None):
        from .gearbox_edit import replay as RP
        up, dn = self._source(grid, pedal_of)
        return RP.replay(self.drives.samples, self._ratios(), up, dn,
                         self.fuel.litres_per_hour, self.tmax, points=points)

    def _replay_now(self):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            sset = self.ed.sset
            edit = self.ed.edit
            self._before = self._metrics(edit.origin, lambda i: sset.schedules[i].pedal)
            self._fill(0, self._before)
            self._after()
            d = self.drives
            self.note.setText(
                "%d paired logs, %.1f h, %.0f km. Each program reads its blend (D), its own "
                "schedule (special programs such as cruise) or else the ticked schedules. Open "
                "loop: compare before with after, not with the logs' own fuel figure." % (
                    d.logs, d.hours, d.km))
        except Exception as ex:
            self.note.setText("The replay failed: %s" % ex)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def edit_changed(self):
        if self.drives is not None and self.live_chk.isChecked():
            self._timer.start()

    def _after(self):
        if self.drives is None or self.ed.result is None:
            return
        res = self.ed.result
        sset = self.ed.sset
        try:
            after = self._metrics(res.grid, lambda i: res.pedal_of(sset, i))
        except Exception as ex:          # (from the live timer too)
            self.note.setText("The replay failed: %s" % ex)
            return
        self._fill(1, after)

    def _fill(self, row: int, m):
        b = self._before
        vals = ["%.2f" % m.l100, "%.1f %% of time" % m.short_pct, "%.0f" % m.shifts_h,
                "%.0f" % m.mean_rpm, "%.0f %%" % (m.agree * 100)]
        if row == 1 and b is not None and b.l100:
            vals[0] += "  (%+.1f %%)" % (100.0 * (m.l100 - b.l100) / b.l100)
        for c, t in enumerate(vals):
            it = QtWidgets.QTableWidgetItem(t)
            it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.table.setItem(row, c, it)
        self.table.resizeColumnsToContents()

    # ------------------------------------------------------ efficiency map
    def open_map(self):
        from .gui_effmap import EfficiencyDialog
        fl = self.ed.fl_panel
        fl.activate()
        m = fl.model()
        if m is None:
            self.note.setText("No engine ECU with a torque model in this project.")
            return
        from . import shift_replay as SR
        fuel = self.fuel or SR.EngineFuel(m)
        self.fuel = fuel
        before = after = None
        if self.drives is not None:
            sset = self.ed.sset
            before, after = [], []
            self._metrics(self.ed.edit.origin, lambda i: sset.schedules[i].pedal, before)
            res = self.ed.result
            if res is not None:
                self._metrics(res.grid, lambda i: res.pedal_of(sset, i), after)
        dlg = EfficiencyDialog(self, fuel, fl.curve, before, after,
                               self.drives is None)
        dlg.show()


__all__ = ["PartLoadPanel", "AxisPanel", "ReplayBox"]
