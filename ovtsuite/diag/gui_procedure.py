# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The procedure runner: one guided procedure, step by step.

The engine (:mod:`.procedures`) runs on the module's live-session thread;
:class:`GuiPrompter` carries its questions to the GUI as signals and
blocks the engine until the user answers. :class:`PromptView` is the part
that renders one question -- the message, a ``choose`` as radio rows, the
plan of a write, the rows of a ``show`` / ``live`` step, a ``wait``
countdown -- so a host only adds its own buttons: :class:`ProcedureDialog`
here (steps list plus Yes / No / Continue / Stop), and the Procedures tab
(:mod:`.gui_procedures_page`) in its step cards. A write is never sent
without the explicit confirmation shown with the exact values.
"""
import queue
import time
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..gui_cellmarks import CellDelegate
from .gui_widgets import _Themed, restyle, tokens
from .procedures import Prompter

_STOP = object()


class GuiPrompter(QtCore.QObject, Prompter):
    ask = QtCore.Signal(str, object)         # kind, payload -> the dialog

    def __init__(self):
        QtCore.QObject.__init__(self)
        self._q: "queue.Queue" = queue.Queue()
        self.stopped = False

    # ---- the dialog answers
    def answer(self, value):
        self._q.put(value)

    def stop(self):
        self.stopped = True
        self._q.put(_STOP)

    def _take(self):
        # once stopped nothing blocks any more: a step that goes on after a
        # stop (the engine's read step ignores the show's answer) must not
        # wait for a user who has gone, nor ask a question nobody can answer
        if self.stopped:
            return None
        v = self._q.get()
        if v is _STOP:
            self.stopped = True
            return None
        return v

    # ---- the engine asks (worker thread)
    def choose(self, step_id, label, options, default=None):
        if self.stopped:
            return None
        self.ask.emit("choose", {"id": step_id, "label": label, "options": options, "default": default})
        return self._take()

    def confirm(self, text, detail=""):
        if self.stopped:
            return False
        self.ask.emit("confirm", {"text": text, "detail": detail})
        return bool(self._take())

    def show(self, text, rows=None):
        if self.stopped:
            return False
        self.ask.emit("show", {"text": text, "rows": rows})
        self._take()
        return not self.stopped

    def live(self, title, read, seconds, text=""):
        if self.stopped:
            return False
        self.ask.emit("live_start", {"title": title, "text": text, "seconds": seconds})
        end = time.time() + seconds
        while time.time() < end and not self.stopped:
            try:
                rows = read()
            except Exception as ex:
                rows = [["", "", "error: %s" % ex, ""]]
            self.ask.emit("live_rows", {"rows": rows, "left": max(0.0, end - time.time())})
            try:
                v = self._q.get(timeout=0.15)
                if v is _STOP:
                    self.stopped = True
                elif v == "skip":
                    break
            except queue.Empty:
                pass
        self.ask.emit("live_end", {})
        return not self.stopped

    def wait(self, text, seconds):
        if self.stopped:
            return False
        self.ask.emit("wait", {"text": text, "seconds": seconds})
        end = time.time() + seconds
        while time.time() < end and not self.stopped:
            try:
                v = self._q.get(timeout=0.1)
                if v is _STOP:
                    self.stopped = True
                elif v == "skip":
                    break
            except queue.Empty:
                pass
        return not self.stopped

    def progress(self, step_index, total, text):
        self.ask.emit("progress", {"index": step_index, "total": total, "text": text})


def _radio_qss() -> str:
    """The radio indicator drawn from the palette: a clear ring on any card
    in either theme, a filled dot while chosen."""
    t = tokens()
    c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
    return ("QRadioButton{background:transparent; spacing:9px;}"
            "QRadioButton::indicator{width:12px; height:12px; border-radius:7px; border:1.5px solid %s;"
            " background:transparent;}"
            "QRadioButton::indicator:hover{border-color:%s;}"
            "QRadioButton::indicator:checked{border:1.5px solid %s;"
            " background:qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,"
            " stop:0 %s, stop:0.5 %s, stop:0.62 transparent);}"
            % (c(t.muted), c(t.text), c(t.accent), c(t.accent), c(t.accent)))


class _ChoiceRow(_Themed):
    """One option of a ``choose`` step: a radio button on a row that is
    tinted while it is the chosen one (the whole row is the click target)."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.radio = QtWidgets.QRadioButton(label)
        restyle(self.radio, _radio_qss)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.addWidget(self.radio)
        self.radio.toggled.connect(lambda _on: self.update())

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.radio.setChecked(True)
            return
        super().mousePressEvent(ev)

    def paintEvent(self, _ev):
        if not self.radio.isChecked():
            return
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(t.accent_bg)
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)


class PromptView(QtWidgets.QWidget):
    """The engine's current question, drawn: ``apply(kind, payload)`` takes
    what :class:`GuiPrompter` emits and returns the mode the host should
    offer buttons for (``confirm`` / ``show`` / ``choose`` / ``live`` /
    ``wait`` / ``idle``; ``""`` when nothing changes, as for ``live_rows``).
    The view has no buttons: the host answers through
    :meth:`GuiPrompter.answer` (``selected_key()`` for a ``choose``).

    ``compact`` hides the table until it has rows and sizes it to them
    (the step cards of the Procedures tab)."""
    selectionChanged = QtCore.Signal()

    def __init__(self, parent=None, compact: bool = False):
        super().__init__(parent)
        self._compact = compact
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.message = QtWidgets.QLabel("")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.message)
        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(110)
        self.detail.setVisible(False)
        v.addWidget(self.detail)
        self.choices = QtWidgets.QWidget()
        self.choices_l = QtWidgets.QVBoxLayout(self.choices)
        self.choices_l.setContentsMargins(0, 0, 0, 0)
        self.choices_l.setSpacing(2)
        self._radios: List[QtWidgets.QRadioButton] = []
        self._rows_w: List[QtWidgets.QWidget] = []
        self._group = QtWidgets.QButtonGroup(self)
        v.addWidget(self.choices)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Cell / channel", "Name", "Value", "Raw"])
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        # the app's table standard (gui_cellmarks): alternating rows so the
        # selection is drawn solid, the delegate keeps selected text legible
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegate(CellDelegate(self.table))
        v.addWidget(self.table, 1)
        self.countdown = QtWidgets.QLabel("")
        v.addWidget(self.countdown)
        self.mode: Optional[str] = None
        self.choices.setVisible(False)
        if compact:
            self.table.setVisible(False)
            self.message.setVisible(False)
            self.countdown.setVisible(False)

    # ---- state
    def clear(self):
        self.mode = None
        self.message.setText("")
        self.detail.setVisible(False)
        self._set_choices(None, None)
        self.choices.setVisible(False)
        self.set_rows(None)
        self.countdown.setText("")
        self._sync()

    def _sync(self):
        if self._compact:
            self.message.setVisible(bool(self.message.text()))
            self.countdown.setVisible(bool(self.countdown.text()))
            n = self.table.rowCount()
            self.table.setVisible(n > 0)
            if n:
                rows = min(n, 8)
                h = self.table.horizontalHeader().height() + rows * self.table.verticalHeader().defaultSectionSize() + 4
                self.table.setFixedHeight(h)

    def selected_key(self):
        return next((r.property("key") for r in self._radios if r.isChecked()), None)

    def set_rows(self, rows):
        t = self.table
        t.setRowCount(0)
        for r in rows or []:
            i = t.rowCount()
            t.insertRow(i)
            for c, val in enumerate(list(r)[:4]):
                t.setItem(i, c, QtWidgets.QTableWidgetItem("" if val is None else str(val)))
        self._sync()

    def _set_choices(self, options, default):
        for r in self._rows_w:
            self.choices_l.removeWidget(r)
            r.deleteLater()
        for b in self._radios:
            self._group.removeButton(b)
        self._rows_w, self._radios = [], []
        for key, label in (options or {}).items():
            row = _ChoiceRow(str(label))
            r = row.radio
            r.setProperty("key", key)
            self._group.addButton(r)
            r.toggled.connect(lambda _on: self.selectionChanged.emit())
            if key == default:
                r.setChecked(True)
            self.choices_l.addWidget(row)
            self._radios.append(r)
            self._rows_w.append(row)
        if self._radios and not any(r.isChecked() for r in self._radios):
            self._radios[0].setChecked(True)

    def apply(self, kind: str, payload) -> str:
        p = payload or {}
        if kind == "confirm":
            self.message.setText(p.get("text", ""))
            self.detail.setPlainText(p.get("detail", ""))
            self.detail.setVisible(bool(p.get("detail")))
            self._set_choices(None, None)
            self.choices.setVisible(False)
            if self._compact:
                self.set_rows(None)
            self.mode = "confirm"
        elif kind == "show":
            self.message.setText(p.get("text", ""))
            self.detail.setVisible(False)
            self._set_choices(None, None)
            self.choices.setVisible(False)
            self.set_rows(p.get("rows"))
            self.countdown.setText("")
            self.mode = "show"
        elif kind == "choose":
            self.message.setText(p.get("label", ""))
            self.detail.setVisible(False)
            self._set_choices(p.get("options"), p.get("default"))
            self.choices.setVisible(True)
            if self._compact:
                self.set_rows(None)
                self.countdown.setText("")
            self.mode = "choose"
        elif kind == "live_start":
            if self._compact:           # the step card already carries the title
                self.message.setText(p.get("text", "") or "")
            else:
                self.message.setText("%s\n%s" % (p.get("title", ""), p.get("text", "")))
            self.detail.setVisible(False)
            self._set_choices(None, None)
            self.choices.setVisible(False)
            self.set_rows(None)
            self.mode = "live"
        elif kind == "live_rows":
            self.set_rows(p.get("rows"))
            self.countdown.setText("%.0f s left" % float(p.get("left", 0)))
            self._sync()
            return ""
        elif kind == "live_end":
            self.countdown.setText("")
            self.mode = "idle"
        elif kind == "wait":
            self.message.setText(p.get("text", ""))
            self.countdown.setText("%.0f s" % float(p.get("seconds", 0)))
            self.mode = "wait"
        else:
            return ""
        self._sync()
        return self.mode


def step_label(step) -> str:
    """One step of a procedure document as a line (``show: Last reading:``):
    its kind and the first text it carries."""
    kind = next(iter(step))
    spec = step[kind]
    label = kind
    if isinstance(spec, dict) and (spec.get("text") or spec.get("title") or spec.get("label")):
        label += ": " + str(spec.get("title") or spec.get("label") or spec.get("text"))[:90]
    elif isinstance(spec, str):
        label += ": " + spec[:90]
    elif isinstance(spec, list):
        label += ": " + ", ".join(str(x if not isinstance(x, dict) else list(x)[0]) for x in spec)
    return label


class ProcedureDialog(QtWidgets.QDialog):
    """Shows one running procedure. ``start(run)`` is what the window calls
    with a callable that posts the engine to the session thread."""
    finished_run = QtCore.Signal(object)     # RunResult

    def __init__(self, doc: Dict, module_label: str, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle("%s - %s" % (doc.get("name", doc.get("id", "Procedure")), module_label))
        self.resize(760, 560)
        self.prompter = GuiPrompter()
        self.prompter.ask.connect(self._on_ask)
        self._result = None
        v = QtWidgets.QVBoxLayout(self)
        head = QtWidgets.QLabel("<b>%s</b><br>%s" % (doc.get("name", ""), doc.get("description", "")))
        head.setWordWrap(True)
        v.addWidget(head)
        safety = str(doc.get("safety") or "read-only")
        s = QtWidgets.QLabel({"read-only": "Reads only; nothing is written.",
                              "write": "Writes to the module after your confirmation of the exact values; every write is journalled and re-read.",
                              "actuates": "Actuates outputs on the module; follow the preconditions."}.get(safety, safety))
        s.setStyleSheet("color: %s" % ("#4fc08d" if safety == "read-only" else "#e5705d"))
        s.setWordWrap(True)
        v.addWidget(s)
        self.steps = QtWidgets.QListWidget()
        self.steps.setMaximumHeight(110)
        for i, st in enumerate(doc.get("steps") or []):
            self.steps.addItem("%d. %s" % (i + 1, step_label(st)))
        v.addWidget(self.steps)
        # the question itself: the shared view (the Procedures tab embeds the same one)
        self.view = PromptView()
        self.message, self.detail, self.choices = self.view.message, self.view.detail, self.view.choices
        self.choices_l, self.table, self.countdown = self.view.choices_l, self.view.table, self.view.countdown
        v.addWidget(self.view, 1)
        row = QtWidgets.QHBoxLayout()
        self.yes_btn = QtWidgets.QPushButton("Yes, write")
        self.yes_btn.clicked.connect(lambda: self._answer(True))
        self.no_btn = QtWidgets.QPushButton("No")
        self.no_btn.clicked.connect(lambda: self._answer(False))
        self.next_btn = QtWidgets.QPushButton("Continue")
        self.next_btn.clicked.connect(self._continue)
        self.skip_btn = QtWidgets.QPushButton("Skip the rest of this step")
        self.skip_btn.clicked.connect(lambda: self.prompter.answer("skip"))
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        for b in (self.yes_btn, self.no_btn, self.next_btn, self.skip_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.stop_btn)
        v.addLayout(row)
        self._mode = None
        self._buttons(None)
        self.message.setText("Starting...")

    @property
    def _radios(self):
        return self.view._radios

    # ---- plumbing
    def _buttons(self, mode: Optional[str]):
        self._mode = mode
        self.yes_btn.setVisible(mode == "confirm")
        self.no_btn.setVisible(mode == "confirm")
        self.next_btn.setVisible(mode in ("show", "choose"))
        self.skip_btn.setVisible(mode in ("live", "wait"))
        self.stop_btn.setEnabled(self._result is None)
        self.choices.setVisible(mode == "choose")

    def _answer(self, value):
        self._buttons(None)
        self.prompter.answer(value)

    def _continue(self):
        if self._mode == "choose":
            key = self.view.selected_key()
            if key is None:
                return
            self._answer(key)
            return
        self._answer(True)

    def _stop(self):
        self.prompter.stop()
        self.stop_btn.setEnabled(False)
        self.message.setText("Stopping...")

    def _rows(self, rows):
        self.view.set_rows(rows)

    def _on_ask(self, kind: str, payload):
        p = payload or {}
        if kind == "progress":
            i = int(p.get("index", 0))
            self.steps.setCurrentRow(i)
            self.countdown.setText("")
            return
        mode = self.view.apply(kind, p)
        if mode:
            self._buttons(None if mode == "idle" else mode)

    def finish(self, result):
        self._result = result
        self._buttons(None)
        self.stop_btn.setEnabled(False)
        words = {"done": "Done.", "cancelled": "Stopped: %s" % result.message, "failed": "Failed: %s" % result.message,
                 "blocked": "Blocked: %s" % result.message}
        self.message.setText("<b>%s</b>" % words.get(result.status, result.status))
        if result.writes:
            self._rows([["%03d" % w.entry_id if isinstance(w.entry_id, int) else w.entry_id, "",
                         "%s -> %s" % ("-" if w.before is None else w.before, w.wanted),
                         "" if w.verified is None else ("verified" if w.verified else "NOT verified")]
                        for w in result.writes])
        self.next_btn.setText("Close")
        self.next_btn.setVisible(True)
        self.next_btn.clicked.disconnect()
        self.next_btn.clicked.connect(self.accept)
        self.finished_run.emit(result)

    def closeEvent(self, ev):
        if self._result is None:
            self.prompter.stop()
        super().closeEvent(ev)

    def reject(self):
        # Escape hides the dialog without a closeEvent: the engine blocked in
        # the prompter must still be released, or its session thread never ends
        if self._result is None:
            self.prompter.stop()
        super().reject()


__all__ = ["ProcedureDialog", "GuiPrompter", "PromptView", "step_label"]
