# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools > Tune from logs...

Pick data logs (and, optionally, earlier logs as a cruise baseline), set the
lambda target, press Analyse. The findings open in one tab, the proposed cell
changes in another, grouped by what they correct so a group can be left out.
Apply writes the chosen groups as ONE undoable edit through the same plan
validator the CLI uses; nothing is written before that.
"""
from __future__ import annotations

import os
from typing import Dict, List

from PySide6 import QtCore, QtGui, QtWidgets

from ..autotune.plan import Plan
from ..autotune.validate import COMMON_CHECKS
from . import analysis as _an
from . import shifttrq as _shifttrq
from .report import to_html, to_text
from .roles import RECIPES, TCU_RECIPES, recipe_groups
from .samples import runs_from_paths

_GROUP_TITLES = {
    "pilot": "Boost pilot (actuator feed-forward)",
    "smoke": "Smoke limiter",
    "soi": "Start of injection",
}


def _group_of(name: str) -> str:
    if name.startswith("PCR_rBPCtl"):
        return "pilot"
    if name.startswith("InjCrv_phiBas"):
        return "soi"
    return "smoke"


class LogTuneDialog(QtWidgets.QDialog):
    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self._window = window
        self.session = window.primary
        # Two independent halves: the engine analysis (EDC16 calibrations) and
        # the ZF 6HP shift torque report (any calibration carrying the
        # KF_MEHAUPT intervention maps). Each shows only where it can run.
        self._engine = getattr(self.session, "platform", "") == "edc16"
        self._tcu = _shifttrq.available(self.session)
        self.setWindowTitle("Tune from logs")
        self.resize(1100, 760)
        self._result = None
        self._namer = None
        try:
            from ..datalog.channels import ChannelNamer
            self._namer = ChannelNamer(self.session)
        except Exception:
            self._namer = None

        v = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        v.addLayout(top)

        # -- logs
        lg = QtWidgets.QGroupBox("Logs to analyse")
        lv = QtWidgets.QVBoxLayout(lg)
        self.logs = QtWidgets.QListWidget()
        self.logs.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        lv.addWidget(self.logs)
        lb = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Add...")
        b.clicked.connect(lambda: self._add(self.logs))
        lb.addWidget(b)
        b = QtWidgets.QPushButton("Remove")
        b.clicked.connect(lambda: self._remove(self.logs))
        lb.addWidget(b)
        lb.addStretch(1)
        guide = QtWidgets.QPushButton("What to log...")
        guide.clicked.connect(self._show_guide)
        lb.addWidget(guide)
        lv.addLayout(lb)
        top.addWidget(lg, 3)

        bg = QtWidgets.QGroupBox("Baseline (optional, cruise comparison)")
        bv = QtWidgets.QVBoxLayout(bg)
        self.base = QtWidgets.QListWidget()
        bv.addWidget(self.base)
        bb = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Add...")
        b.clicked.connect(lambda: self._add(self.base))
        bb.addWidget(b)
        b = QtWidgets.QPushButton("Remove")
        b.clicked.connect(lambda: self._remove(self.base))
        bb.addWidget(b)
        bb.addStretch(1)
        bv.addLayout(bb)
        top.addWidget(bg, 2)
        bg.setVisible(self._engine)

        # -- options
        og = QtWidgets.QGroupBox("Targets")
        of = QtWidgets.QFormLayout(og)
        o = _an.Options()
        self.lam = QtWidgets.QDoubleSpinBox()
        self.lam.setRange(1.05, 1.60)
        self.lam.setSingleStep(0.01)
        self.lam.setValue(o.lambda_target)
        self.lam.setToolTip("Full-load lambda the smoke limiter is calibrated to. "
                            "1.15-1.20 is the usual range for a PD engine without an EGT sensor.")
        of.addRow("Lambda target", self.lam)
        self.floor = QtWidgets.QDoubleSpinBox()
        self.floor.setRange(1.00, 1.50)
        self.floor.setSingleStep(0.01)
        self.floor.setValue(o.lambda_floor)
        self.floor.setToolTip("No planned change may allow richer than this.")
        of.addRow("Lambda floor", self.floor)
        self.do_pilot = QtWidgets.QCheckBox("Correct boost pilot")
        self.do_pilot.setChecked(True)
        of.addRow(self.do_pilot)
        self.do_smoke = QtWidgets.QCheckBox("Correct smoke limiter")
        self.do_smoke.setChecked(True)
        of.addRow(self.do_smoke)
        self.do_soi = QtWidgets.QCheckBox("Advance start of injection where it ends late")
        self.do_soi.setToolTip("Opt-in. Under load, moves the SOI maps read at the logged coolant temperature up to 1 deg per pass "
                               "toward the end-of-injection target, never past the SOI limiter.")
        of.addRow(self.do_soi)
        self.do_warmup = QtWidgets.QCheckBox("Tune warm-up (coolant below 70 \u00b0C)")
        self.do_warmup.setChecked(True)
        self.do_warmup.setToolTip("Rows logged below 70 \u00b0C coolant correct only the maps the ECU "
                                  "reads while warming up (chosen by its own coolant curves), so they "
                                  "never move the warm tune. Unticked, they are reported only.")
        of.addRow(self.do_warmup)
        self.eoi = QtWidgets.QDoubleSpinBox()
        self.eoi.setRange(4.0, 12.0)
        self.eoi.setSingleStep(0.5)
        self.eoi.setValue(o.eoi_target)
        self.eoi.setSuffix(" deg ATDC")
        of.addRow("End-of-injection target", self.eoi)
        self.trial = QtWidgets.QDoubleSpinBox()
        self.trial.setRange(0.0, 2.0)
        self.trial.setSingleStep(0.5)
        self.trial.setSuffix(" deg")
        self.trial.setToolTip("Economy A/B test: advance the cruise cells (1250-2750 rpm, 5-25 mg) "
                              "by this much, then compare cruise logs before and after.")
        of.addRow("Economy SOI trial", self.trial)
        top.addWidget(og, 2)
        og.setVisible(self._engine)

        run = QtWidgets.QHBoxLayout()
        self.go = QtWidgets.QPushButton("Analyse")
        self.go.setDefault(True)
        self.go.clicked.connect(self._analyse)
        run.addWidget(self.go)
        self.go.setVisible(self._engine)
        # ZF 6HP only: the torque-intervention report. Shown when the loaded
        # calibration actually carries the per-event intervention family, which
        # is a structural test rather than a guess from the file name.
        self.trq_btn = QtWidgets.QPushButton("Shift torque report")
        self.trq_btn.setToolTip(
            "ZF 6HP: for each shift in the logs, what the TCU asked the engine for\n"
            "(KF_MEHAUPT<from><to>, sport variant when the logged program is a sport\n"
            "state) against what engine torque actually did. Read-only.\n\n"
            "Needs the 'Torque intervention during shifts' logging preset -- groups\n"
            "1 + 9 -- because it reads engine torque and finds the shift from the\n"
            "turbine/output ratio.")
        self.trq_btn.clicked.connect(self._shift_torque)
        self.trq_btn.setVisible(self._tcu)
        if self._tcu and not self._engine:
            self.trq_btn.setDefault(True)
        run.addWidget(self.trq_btn)
        self.summary = QtWidgets.QLabel(
            "Add logs, then Analyse. Calibration-only checks (and "
            "the economy trial) run with no logs too." if self._engine else
            "Add logs taken with the 'Torque intervention during shifts' "
            "preset, then Shift torque report.")
        self.summary.setStyleSheet("color: gray")
        run.addWidget(self.summary, 1)
        v.addLayout(run)

        # -- results
        self.tabs = QtWidgets.QTabWidget()
        self.findings = QtWidgets.QTextBrowser()
        self.tabs.addTab(self.findings, "Findings")
        ch = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(ch)
        cv.setContentsMargins(0, 4, 0, 0)
        self.groups_box = QtWidgets.QHBoxLayout()
        cv.addLayout(self.groups_box)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Map", "X", "Y", "Old", "New", "Change",
                                              "Samples", "Why"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(7, QtWidgets.QHeaderView.Stretch)
        cv.addWidget(self.table, 1)
        self.tabs.addTab(ch, "Proposed changes")
        self.tabs.setTabVisible(1, self._engine)
        v.addWidget(self.tabs, 1)

        bot = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save report...")
        self.save_btn.clicked.connect(self._save_report)
        self.save_btn.setEnabled(False)
        bot.addWidget(self.save_btn)
        self.save_btn.setVisible(self._engine)
        bot.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply selected changes")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        bot.addWidget(self.apply_btn)
        self.apply_btn.setVisible(self._engine)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        bot.addWidget(close)
        v.addLayout(bot)
        self._group_checks: Dict[str, QtWidgets.QCheckBox] = {}
        self._applied = set()

    # ------------------------------------------------------------ lists
    def _project_log_path(self, doc, a) -> str:
        path = os.path.join(doc.tmpdir, "logs", a.id, a.filename)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(doc.project.log_bytes(a.id))
        return path

    def _add(self, lst):
        from .. import recent as _recent
        ref = getattr(self._window, "project_ref", None)
        if ref is not None and ref.doc.project.logs:
            # the project's own logs first (this binary's at the top), a
            # file from disk after
            doc = ref.doc
            menu = QtWidgets.QMenu(self)
            logs = sorted(doc.project.logs,
                          key=lambda a: (a.set != ref.sid, a.added), reverse=False)
            for a in logs:
                act = menu.addAction("%s   (%s)" % (a.filename,
                                                    a.added.replace("T", " ")[:16]))
                act.setData(a.id)
            menu.addSeparator()
            other = menu.addAction("From a file...")
            got = menu.exec(QtGui.QCursor.pos())
            if got is None:
                return
            if got is not other:
                a = next(x for x in doc.project.logs if x.id == got.data())
                p = self._project_log_path(doc, a)
                if not lst.findItems(p, QtCore.Qt.MatchExactly):
                    lst.addItem(p)
                return
        paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Data logs", _recent.get_last_dir("log"), "Data logs (*.csv);;All files (*)")
        for p in paths:
            if not lst.findItems(p, QtCore.Qt.MatchExactly):
                lst.addItem(p)
        if paths:
            _recent.set_last_dir(os.path.dirname(paths[0]), "log")

    @staticmethod
    def _remove(lst):
        for it in lst.selectedItems():
            lst.takeItem(lst.row(it))

    @staticmethod
    def _paths(lst) -> List[str]:
        return [lst.item(i).text() for i in range(lst.count())]

    def _guide_entries(self):
        """(recipe, groups, missing) for every preset this window can use: the
        engine presets on an EDC16 calibration (planned on its block map), the
        shift torque preset where the report is available (planned on the
        transmission's measuring blocks, as Data logging does)."""
        out = []
        if self._engine:
            from ..datalog.served import unserved_for
            for rec in RECIPES:
                groups, missing = (recipe_groups(self._namer, rec,
                                                 avoid=unserved_for(self._namer))
                                   if self._namer else ([], list(rec.needs)))
                out.append((rec, groups, missing))
        if self._tcu:
            from ..datalog.msblock6hp import Blocks6hp
            for rec in TCU_RECIPES:
                if rec.key == "shifttrq":
                    groups, missing = recipe_groups(Blocks6hp(), rec,
                                                    rec.max_groups)
                    out.append((rec, groups, missing))
        return out

    def _show_guide(self):
        lines = []
        for rec, groups, missing in self._guide_entries():
            lines.append("<p><b>%s</b> &mdash; preset <i>%s</i>, measuring blocks %s%s<br>%s</p>" % (
                rec.title, rec.key, ", ".join(map(str, groups)) or "-",
                (" (missing: %s)" % ", ".join(missing)) if missing else "", rec.how))
        lines.append("<p>Pick the preset in <b>Data logging</b>; it ticks exactly these blocks "
                     "for this calibration. Fewer blocks per log = more samples per second.</p>")
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("What to log")
        box.setTextFormat(QtCore.Qt.RichText)
        box.setText("".join(lines))
        box.exec()

    # ------------------------------------------------------------ analyse
    def _options(self) -> _an.Options:
        o = _an.Options(lambda_target=self.lam.value(), lambda_floor=self.floor.value(),
                        correct_pilot=self.do_pilot.isChecked(),
                        correct_smoke=self.do_smoke.isChecked())
        o.correct_soi = self.do_soi.isChecked()
        o.tune_warmup = self.do_warmup.isChecked()
        o.eoi_target = self.eoi.value()
        o.soi_cruise_trial = self.trial.value()
        return o

    def _shift_torque(self):
        """The ZF 6HP torque-intervention report: asked vs delivered, per shift.

        Deliberately separate from Analyse and read-only. It proposes no cell
        changes, because what to change depends on which way the answer comes
        out: if the engine delivers what is asked, the TCU maps are the lever;
        if it saturates below, the ECU gate (Gearbx_trqIncMax_C) is, and raising
        a TCU map would write a number with no effect.
        """
        paths = self._paths(self.logs)
        if not paths:
            QtWidgets.QMessageBox.information(
                self, "Shift torque report",
                "Add at least one log taken with the 'Torque intervention during "
                "shifts' preset (groups 1 + 9).")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            runs = runs_from_paths(paths, self._namer)
            rep = _shifttrq.analyse(self.session, runs)
        except Exception as ex:          # a malformed log must not take the app down
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "Shift torque report",
                                           "Could not analyse:\n%s" % ex)
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        import html as _html
        self.findings.setHtml("<pre style='font-family:monospace'>%s</pre>"
                              % _html.escape(_shifttrq.to_text(rep)))
        n = len(rep.events)
        clipped = sum(1 for e in rep.events if e.clipped)
        self.summary.setText("%d shift(s) measured%s."
                             % (n, "; %d asked for more than the %.0f Nm ECU gate"
                                % (clipped, rep.gate_nm) if clipped else ""))
        self.summary.setStyleSheet("color: %s" % ("#b26a00" if clipped else "#2e7d32"))
        self.save_btn.setEnabled(False)
        self.apply_btn.setEnabled(False)

    def _analyse(self):
        if self.floor.value() > self.lam.value():
            QtWidgets.QMessageBox.warning(self, "Tune from logs",
                                          "The lambda floor must not be above the target.")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            runs = runs_from_paths(self._paths(self.logs), self._namer)
            base = runs_from_paths(self._paths(self.base), self._namer)
            self._result = _an.analyse(self.session, runs, self._options(), base)
        except Exception as ex:          # a malformed log must not take the app down
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "Tune from logs", "Could not analyse:\n%s" % ex)
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        res = self._result
        self.findings.setHtml(to_html(res))
        self._fill_changes(res)
        n = len(res.plan.effective_writes)
        crit = sum(1 for f in res.findings if f.severity == _an.CRITICAL)
        warn = sum(1 for f in res.findings if f.severity == _an.WARN)
        self.summary.setText("%d critical, %d warning(s); %d cell change(s) proposed%s."
                             % (crit, warn, n, "" if res.plan.ok or not n else " (blocked)"))
        self.summary.setStyleSheet("color: %s" % ("#c62828" if crit else "#b26a00" if warn else "#2e7d32"))
        self.save_btn.setEnabled(True)
        stale = frozenset(self._paths(self.logs)) in self._applied
        self.apply_btn.setEnabled(bool(n) and res.plan.ok and not stale)
        if stale and n:
            self.summary.setText(self.summary.text() + " These logs were already applied -- "
                                 "flash the result and log again before the next pass.")
            self.summary.setStyleSheet("color: #b26a00")

    def _fill_changes(self, res):
        while self.groups_box.count():
            w = self.groups_box.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._group_checks = {}
        counts: Dict[str, int] = {}
        for c in res.cells:
            g = _group_of(c.map)
            counts[g] = counts.get(g, 0) + 1
        self.groups_box.addWidget(QtWidgets.QLabel("Include:"))
        for g, n in counts.items():
            cb = QtWidgets.QCheckBox("%s (%d)" % (_GROUP_TITLES.get(g, g), n))
            cb.setChecked(True)
            self._group_checks[g] = cb
            self.groups_box.addWidget(cb)
        self.groups_box.addStretch(1)
        self.table.setRowCount(len(res.cells))
        for r, c in enumerate(res.cells):
            vals = [c.map, "%g" % c.x, "%g" % c.y, "%.2f" % c.old, "%.2f" % c.new,
                    "%+.2f" % (c.new - c.old), "%.1f" % c.weight, c.reason]
            for k, t in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(t)
                if 1 <= k <= 6:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if k == 5:
                    it.setForeground(QtGui.QColor("#2e7d32" if c.new >= c.old else "#c62828"))
                self.table.setItem(r, k, it)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(7, QtWidgets.QHeaderView.Stretch)
        if res.cells:
            self.tabs.setCurrentIndex(0)

    # ------------------------------------------------------------ apply / save
    def _selected_plan(self) -> Plan:
        keep = {g for g, cb in self._group_checks.items() if cb.isChecked()}
        src = self._result.plan
        plan = Plan(self.session, "Tune from logs")
        for w in src.effective_writes:
            if _group_of(w.target.name) in keep:
                plan.set(w.target, w.row, w.col, w.new, w.reason)
        return plan

    def _apply(self):
        if self._result is None:
            return
        plan = self._selected_plan()
        if not plan.effective_writes:
            QtWidgets.QMessageBox.information(self, "Tune from logs", "Nothing selected.")
            return
        tuner = _an.LogTuner(self.session, [], self._options())
        plan.validate(list(COMMON_CHECKS) + tuner._checks())
        if plan.errors:
            QtWidgets.QMessageBox.warning(self, "Tune from logs",
                                          "Refusing to apply:\n" + "\n".join(str(p) for p in plan.errors[:8]))
            return
        text = ("Write %d cell(s) across %d map(s)?\n\nThis is one undoable edit "
                "(Edit > Undo). Checksums are corrected when you save."
                % (len(plan.effective_writes), len(plan.targets())))
        if QtWidgets.QMessageBox.question(self, "Tune from logs", text) != QtWidgets.QMessageBox.Yes:
            return
        n = plan.apply()
        # Corrections are relative to what the car did on THOSE logs; applying
        # them again would stack the same step twice.
        self._applied.add(frozenset(self._paths(self.logs)))
        self.apply_btn.setEnabled(False)
        self.summary.setText("Applied %d cell(s). Flash, re-log, and run this again." % (n or 0))
        self.summary.setStyleSheet("color: #2e7d32")

    def _save_report(self):
        if self._result is None:
            return
        from .. import recent as _recent
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save report", os.path.join(_recent.get_last_dir("log"), "logtune-report.txt"),
            "Text (*.txt);;HTML (*.html)")
        if not path:
            return
        body = to_html(self._result) if path.lower().endswith((".html", ".htm")) else to_text(self._result, 10000)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
